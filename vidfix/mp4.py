"""Parser ISO BMFF (MP4 / MOV / M4V / 3GP) a primitiva na opravu hlavicky.

Kluc k oprave po ransomveri:
Vacsina ransomveru sifruje iba zaciatok suboru (typicky 64 KiB az niekolko MiB).
Kamery, telefony a drony zapisuju index `moov` az na KONIEC suboru, takze index
casto prezije. Offsety v tabulke `stco`/`co64` su ABSOLUTNE pozicie v subore -
preto sa poskodeny zaciatok nesmie odstranit, ale musi sa nahradit rovnako
dlhou, ale platnou hlavickou. Presne to robi `build_prefix()`.
"""
from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field

from .util import printable_type

# Boxy, ktore sa legitimne vyskytuju na najvyssej urovni suboru
TOP_LEVEL_TYPES = {
    b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot",
    b"moof", b"mfra", b"meta", b"uuid", b"sidx", b"ssix", b"prft", b"emsg",
    b"junk", b"pict", b"PICT", b"cmov", b"ctab", b"mdia",
}

# Boxy, ktore v sebe priamo obsahuju dalsie boxy
CONTAINER_TYPES = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"udta",
    b"mvex", b"moof", b"traf", b"mfra", b"tref", b"gmhd", b"wave", b"schi",
    b"sinf", b"rinf", b"mfra",
}

# Typy, ktore hladame ako "kotvy" v poskodenom subore
ANCHOR_TYPES = (b"moov", b"mdat", b"moof", b"free", b"skip", b"mfra", b"wide", b"ftyp")

MAX_BOX = 1 << 42  # rozumny horny limit velkosti boxu (4 TiB)
MAX_SAMPLES = 20_000_000  # poistka proti nezmyselnym tabulkam


@dataclass
class Box:
    offset: int          # pozicia zaciatku boxu v subore
    size: int            # celkova velkost boxu vratane hlavicky
    type: bytes          # 4-znakovy typ
    header_size: int     # 8, 16 (64-bit) alebo 16 (uuid)
    children: list = field(default_factory=list)
    truncated: bool = False

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def data_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def data_size(self) -> int:
        return self.size - self.header_size

    def type_str(self) -> str:
        return self.type.decode("latin-1")

    def find(self, *path: bytes):
        """Najde prvy vnoreny box podla cesty, napr. box.find(b'mvhd')."""
        node = self
        for want in path:
            nxt = None
            for c in node.children:
                if c.type == want:
                    nxt = c
                    break
            if nxt is None:
                return None
            node = nxt
        return node

    def find_all(self, want: bytes) -> list:
        out = []
        stack = [self]
        while stack:
            n = stack.pop()
            for c in n.children:
                if c.type == want:
                    out.append(c)
                stack.append(c)
        return out

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "size": self.size,
            "type": self.type_str(),
            "children": [c.to_dict() for c in self.children],
        }


def read_box_header(mm, offset: int, filesize: int):
    """Precita hlavicku boxu na danom offsete. Vracia Box alebo None."""
    if offset < 0 or offset + 8 > filesize:
        return None
    hdr = mm[offset:offset + 8]
    if len(hdr) < 8:
        return None
    size = struct.unpack(">I", hdr[0:4])[0]
    btype = hdr[4:8]
    if not printable_type(btype):
        return None
    header_size = 8
    if size == 1:
        if offset + 16 > filesize:
            return None
        size = struct.unpack(">Q", mm[offset + 8:offset + 16])[0]
        header_size = 16
    elif size == 0:
        # box siaha do konca suboru
        size = filesize - offset
    if size < header_size or size > MAX_BOX:
        return None
    if btype == b"uuid":
        header_size += 16
        if size < header_size:
            return None
    return Box(offset=offset, size=size, type=btype, header_size=header_size)


# Zaznamy v boxe `stsd` maju pred vnorenymi boxmi pevnu hlavicku:
# 78 bajtov pri obraze (VisualSampleEntry), 28 pri zvuku (AudioSampleEntry).
SAMPLE_ENTRY_OFFSETS = (78, 28, 8)


def parse_tree(mm, start: int, end: int, filesize: int, depth: int = 0,
               max_depth: int = 8, in_stsd: bool = False) -> list:
    """Rekurzivne rozparsuje boxy v intervale [start, end)."""
    out = []
    off = start
    guard = 0
    while off + 8 <= end and guard < 100000:
        guard += 1
        box = read_box_header(mm, off, filesize)
        if box is None or box.end > end or box.size <= 0:
            break
        if in_stsd and depth < max_depth:
            # zaznam kodeku (avc1, hvc1, mp4a...) - vnorene boxy (avcC, esds)
            # zacinaju az za pevnou hlavickou, ktorej dlzka zavisi od typu stopy
            for posun in SAMPLE_ENTRY_OFFSETS:
                skus = read_box_header(mm, box.data_offset + posun, filesize)
                if skus is not None and skus.end <= box.end:
                    box.children = parse_tree(mm, box.data_offset + posun, box.end,
                                              filesize, depth + 1, max_depth)
                    break
        elif box.type in CONTAINER_TYPES and depth < max_depth:
            box.children = parse_tree(mm, box.data_offset, box.end, filesize, depth + 1, max_depth)
        elif box.type == b"stsd" and depth < max_depth:
            # stsd = full box (4B verzia/flags) + 4B pocet zaznamov, potom zaznamy
            box.children = parse_tree(mm, box.data_offset + 8, box.end, filesize,
                                      depth + 1, max_depth, in_stsd=True)
        out.append(box)
        off = box.end
    return out


def chain_from(mm, start: int, filesize: int, tail_slack: int = 0):
    """Prejde retazec boxov od `start`. Vracia (zoznam boxov, koncovy offset).

    Sluzi na overenie, ci je dany offset skutocny zaciatok neposkodenej
    struktury: platny retazec konci presne na konci suboru (pripadne s malou
    rezervou, ak ransomver pripojil na koniec vlastnu znacku).
    """
    boxes = []
    off = start
    guard = 0
    while off < filesize and guard < 200000:
        guard += 1
        box = read_box_header(mm, off, filesize)
        if box is None:
            break
        if box.end > filesize:
            # Posledny box presahuje koniec suboru. Je to bud orezany media box,
            # alebo uz nejde o box (napr. znacka pripojena ransomverom).
            if box.type in TOP_LEVEL_TYPES:
                box.size = filesize - box.offset
                box.truncated = True
                boxes.append(box)
                off = filesize
            break
        boxes.append(box)
        off = box.end
        if box.size == 0:
            break
    return boxes, off


def is_plausible_chain(boxes: list, end: int, filesize: int, tail_slack: int = 4096) -> bool:
    """Je retazec boxov dostatocne dobry na to, aby sme ho povazovali za realny?"""
    if not boxes:
        return False
    types = {b.type for b in boxes}
    # musi obsahovat aspon jeden "tazky" box a skoncit (takmer) na konci suboru
    meaningful = types & {b"moov", b"mdat", b"moof", b"mfra"}
    if not meaningful:
        return False
    if end < filesize - tail_slack:
        return False
    return True


def find_anchor_offsets(mm, filesize: int, types=ANCHOR_TYPES, limit_per_type: int = 4000):
    """Najde vsetky pozicie, kde sa v subore vyskytuje nazov niektoreho boxu.

    Nazov boxu lezi 4 bajty za zaciatkom boxu, takze kandidat na zaciatok
    boxu je (pozicia_nazvu - 4).
    """
    found = []
    for t in types:
        pos = 0
        count = 0
        while count < limit_per_type:
            pos = mm.find(t, pos)
            if pos < 0:
                break
            if pos >= 4:
                found.append((pos - 4, t))
                count += 1
            pos += 1
    found.sort()
    return found


def locate_intact_structure(mm, filesize: int, tail_slack: int = 1 << 20):
    """Najde najskorsi offset, od ktoreho subor tvori platnu strukturu boxov.

    Vracia dict s klucmi:
      boundary  - offset prvej neposkodenej struktury (0 = subor je v poriadku)
      boxes     - zoznam boxov od tejto hranice
      end       - kde retazec konci
      trailing  - kolko bajtov ostava za koncom retazca (pripojena znacka ransomveru)
    """
    # 1) Ak subor zacina platnym ftyp a retazec dobehne na koniec, je v poriadku.
    first = read_box_header(mm, 0, filesize)
    if first is not None and first.type in (b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"):
        boxes, end = chain_from(mm, 0, filesize)
        if is_plausible_chain(boxes, end, filesize, tail_slack):
            return {"boundary": 0, "boxes": boxes, "end": end, "trailing": filesize - end}

    # 2) Inak hladame kotvy a overujeme, ktora zacina platny retazec.
    best = None
    for cand_off, cand_type in find_anchor_offsets(mm, filesize):
        if cand_off < 0 or cand_off >= filesize:
            continue
        box = read_box_header(mm, cand_off, filesize)
        if box is None or box.type != cand_type:
            continue
        # zjavne nezmyselne male/velke boxy preskocime
        if box.size < 8 or box.end > filesize + 16:
            continue
        # moov musi obsahovat mvhd alebo trak hned na zaciatku
        if box.type == b"moov":
            inner = read_box_header(mm, box.data_offset, filesize)
            if inner is None or inner.type not in (b"mvhd", b"trak", b"cmov", b"udta", b"iods", b"meta"):
                continue
        if box.type == b"moof":
            inner = read_box_header(mm, box.data_offset, filesize)
            if inner is None or inner.type != b"mfhd":
                continue
        boxes, end = chain_from(mm, cand_off, filesize)
        if is_plausible_chain(boxes, end, filesize, tail_slack):
            if best is None or cand_off < best["boundary"]:
                best = {"boundary": cand_off, "boxes": boxes, "end": end,
                        "trailing": filesize - end}
    return best


# ---------------------------------------------------------------------------
# Sample tables - potrebne na zistenie, ktore data prezili
# ---------------------------------------------------------------------------

@dataclass
class Track:
    track_id: int = 0
    handler: str = ""
    codec: str = ""
    timescale: int = 1
    duration: int = 0
    width: int = 0
    height: int = 0
    chunk_offsets: list = field(default_factory=list)
    sample_sizes: list = field(default_factory=list)
    stsc: list = field(default_factory=list)     # (first_chunk, samples_per_chunk, desc_idx)
    stts: list = field(default_factory=list)     # (count, delta)
    sample_rate: int = 0
    channels: int = 0
    uniform_sample_size: int = 0
    sample_count: int = 0
    stss: list = field(default_factory=list)   # 1-zalozene cisla synchronizacnych vzoriek

    def size_of_sample(self, idx: int) -> int | None:
        if self.uniform_sample_size:
            return self.uniform_sample_size if idx < self.sample_count else None
        if 0 <= idx < len(self.sample_sizes):
            return self.sample_sizes[idx]
        return None

    @property
    def duration_sec(self) -> float:
        return self.duration / self.timescale if self.timescale else 0.0

    def samples_per_chunk_list(self) -> list:
        """Rozvinie stsc do poctu vzoriek pre kazdy chunk."""
        n = len(self.chunk_offsets)
        out = [0] * n
        if not self.stsc:
            return out
        for i, (first, spc, _desc) in enumerate(self.stsc):
            start = max(0, first - 1)
            end = (self.stsc[i + 1][0] - 1) if i + 1 < len(self.stsc) else n
            for c in range(start, min(end, n)):
                out[c] = spc
        return out

    def sample_index_of_chunk(self) -> list:
        """Index prvej vzorky v kazdom chunku."""
        spc = self.samples_per_chunk_list()
        out = []
        acc = 0
        for c in spc:
            out.append(acc)
            acc += c
        return out

    def sample_time(self, sample_idx: int) -> float:
        """Cas (v sekundach) danej vzorky podla tabulky stts."""
        if not self.stts or not self.timescale:
            return 0.0
        remaining = sample_idx
        t = 0
        for count, delta in self.stts:
            if remaining <= count:
                t += remaining * delta
                return t / self.timescale
            t += count * delta
            remaining -= count
        return t / self.timescale

    def first_intact_time(self, boundary: int) -> float | None:
        """Cas prvej vzorky, ktora lezi za hranicou poskodenia."""
        if not self.chunk_offsets:
            return None
        idx_of_chunk = self.sample_index_of_chunk()
        for i, off in enumerate(self.chunk_offsets):
            if off >= boundary:
                return self.sample_time(idx_of_chunk[i] if i < len(idx_of_chunk) else 0)
        return None

    def sample_offsets(self, limit: int = 5_000_000) -> list:
        """Byte offset kazdej vzorky (odvodeny z chunk offsetov a velkosti)."""
        spc = self.samples_per_chunk_list()
        offsets = []
        idx = 0
        for ci, chunk_off in enumerate(self.chunk_offsets):
            pos = chunk_off
            for _ in range(spc[ci] if ci < len(spc) else 0):
                if len(offsets) >= limit:
                    return offsets
                offsets.append(pos)
                size = self.size_of_sample(idx)
                if size is None:
                    return offsets
                pos += size
                idx += 1
        return offsets

    def first_intact_keyframe(self, damage_end: int):
        """(index, cas) prveho klucoveho snimku, ktory lezi za poskodenou castou.

        Presne v tomto bode sa da video bezstratovo orezat (`ffmpeg -ss`), lebo
        dekoder tam nepotrebuje ziadny predchadzajuci snimok.
        """
        offsets = self.sample_offsets()
        if not offsets:
            return None, None
        if self.stss:
            candidates = (s - 1 for s in self.stss)
        else:
            candidates = range(len(offsets))   # bez stss je kazda vzorka klucova
        for idx in candidates:
            if 0 <= idx < len(offsets) and offsets[idx] >= damage_end:
                return idx, self.sample_time(idx)
        return None, None

    def lost_chunks(self, boundary: int) -> int:
        return sum(1 for o in self.chunk_offsets if o < boundary)


def _read(mm, off, n):
    return mm[off:off + n]


def parse_tracks(mm, moov: Box, filesize: int) -> list:
    """Vytiahne z boxu `moov` zoznam stop aj s tabulkami vzoriek."""
    tracks = []
    for trak in [c for c in moov.children if c.type == b"trak"]:
        tr = Track()
        tkhd = trak.find(b"tkhd")
        if tkhd:
            d = _read(mm, tkhd.data_offset, min(tkhd.data_size, 92))
            if len(d) >= 4:
                version = d[0]
                try:
                    if version == 1 and len(d) >= 32:
                        tr.track_id = struct.unpack(">I", d[20:24])[0]
                    elif len(d) >= 20:
                        tr.track_id = struct.unpack(">I", d[12:16])[0]
                    if len(d) >= 84:
                        w, h = struct.unpack(">II", d[-8:])
                        tr.width, tr.height = w >> 16, h >> 16
                except struct.error:
                    pass
        mdhd = trak.find(b"mdia", b"mdhd")
        if mdhd:
            d = _read(mm, mdhd.data_offset, min(mdhd.data_size, 40))
            try:
                if d and d[0] == 1 and len(d) >= 28:
                    tr.timescale = struct.unpack(">I", d[20:24])[0]
                    tr.duration = struct.unpack(">Q", d[24:32])[0] if len(d) >= 32 else 0
                elif len(d) >= 20:
                    tr.timescale = struct.unpack(">I", d[12:16])[0]
                    tr.duration = struct.unpack(">I", d[16:20])[0]
            except struct.error:
                pass
        hdlr = trak.find(b"mdia", b"hdlr")
        if hdlr:
            d = _read(mm, hdlr.data_offset, min(hdlr.data_size, 24))
            if len(d) >= 12:
                tr.handler = d[8:12].decode("latin-1", "replace")
        stbl = trak.find(b"mdia", b"minf", b"stbl")
        if stbl:
            stsd = stbl.find(b"stsd")
            if stsd and stsd.children:
                entry = stsd.children[0]
                tr.codec = entry.type_str()
                if tr.handler == "soun":
                    d = _read(mm, entry.data_offset, min(entry.data_size, 32))
                    if len(d) >= 28:
                        try:
                            tr.channels = struct.unpack(">H", d[16:18])[0]
                            tr.sample_rate = struct.unpack(">I", d[24:28])[0] >> 16
                        except struct.error:
                            pass
            stco = stbl.find(b"stco")
            co64 = stbl.find(b"co64")
            if stco:
                d = _read(mm, stco.data_offset, stco.data_size)
                if len(d) >= 8:
                    n = struct.unpack(">I", d[4:8])[0]
                    n = min(n, (len(d) - 8) // 4)
                    tr.chunk_offsets = list(struct.unpack(f">{n}I", d[8:8 + 4 * n]))
            elif co64:
                d = _read(mm, co64.data_offset, co64.data_size)
                if len(d) >= 8:
                    n = struct.unpack(">I", d[4:8])[0]
                    n = min(n, (len(d) - 8) // 8)
                    tr.chunk_offsets = list(struct.unpack(f">{n}Q", d[8:8 + 8 * n]))
            stsc = stbl.find(b"stsc")
            if stsc:
                d = _read(mm, stsc.data_offset, stsc.data_size)
                if len(d) >= 8:
                    n = struct.unpack(">I", d[4:8])[0]
                    n = min(n, (len(d) - 8) // 12)
                    for i in range(n):
                        off = 8 + 12 * i
                        tr.stsc.append(struct.unpack(">III", d[off:off + 12]))
            stsz = stbl.find(b"stsz")
            if stsz:
                d = _read(mm, stsz.data_offset, min(stsz.data_size, 12))
                if len(d) >= 12:
                    uniform = struct.unpack(">I", d[4:8])[0]
                    count = struct.unpack(">I", d[8:12])[0]
                    count = min(count, MAX_SAMPLES)
                    if uniform:
                        tr.uniform_sample_size = uniform
                        tr.sample_count = count
                    else:
                        raw = _read(mm, stsz.data_offset + 12, 4 * count)
                        n = min(count, len(raw) // 4)
                        tr.sample_sizes = list(struct.unpack(f">{n}I", raw[:4 * n]))
                        tr.sample_count = n
            stss = stbl.find(b"stss")
            if stss:
                d = _read(mm, stss.data_offset, stss.data_size)
                if len(d) >= 8:
                    n = struct.unpack(">I", d[4:8])[0]
                    n = min(n, (len(d) - 8) // 4, MAX_SAMPLES)
                    tr.stss = list(struct.unpack(f">{n}I", d[8:8 + 4 * n]))
            stts = stbl.find(b"stts")
            if stts:
                d = _read(mm, stts.data_offset, stts.data_size)
                if len(d) >= 8:
                    n = struct.unpack(">I", d[4:8])[0]
                    n = min(n, (len(d) - 8) // 8)
                    for i in range(n):
                        off = 8 + 8 * i
                        tr.stts.append(struct.unpack(">II", d[off:off + 8]))
        tracks.append(tr)
    return tracks


# ---------------------------------------------------------------------------
# Stavba novej hlavicky
# ---------------------------------------------------------------------------

def box(btype: bytes, payload: bytes) -> bytes:
    """Zabali data do 32-bitoveho boxu."""
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def ftyp_box(major: bytes = b"isom", minor: int = 512,
             compatible=(b"isom", b"iso2", b"avc1", b"mp41")) -> bytes:
    payload = major + struct.pack(">I", minor) + b"".join(compatible)
    return box(b"ftyp", payload)


def free_box(total: int) -> bytes:
    """Vypln `free` s presnou celkovou velkostou `total` (min. 8)."""
    if total < 8:
        raise ValueError("Box free musí mať aspoň 8 bajtov")
    return struct.pack(">I", total) + b"free" + b"\0" * (total - 8)


def mdat_header64(total: int) -> bytes:
    """64-bitova hlavicka mdat s celkovou velkostou `total` (16 bajtov)."""
    return struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", total)


def mdat_header32(total: int) -> bytes:
    return struct.pack(">I", total) + b"mdat"


def build_graft_header(media_end: int, ftyp: bytes | None = None) -> bytes:
    """Postavi novy zaciatok suboru: [ftyp][64-bitova hlavicka mdat].

    Vysledok ma zvycajne 48 bajtov a prepise iba zasifrovany zaciatok.
    Vsetky dalsie bajty ostanu na POVODNYCH offsetoch, takze tabulky
    stco/co64 v prezitom `moov` naďalej ukazuju spravne.

    `media_end` je offset, kde media data koncia - zvycajne zaciatok `moov`.
    """
    if ftyp is None:
        ftyp = ftyp_box()
    mdat_start = len(ftyp)
    mdat_total = media_end - mdat_start
    if mdat_total < 16:
        raise ValueError("Žiadne použiteľné dáta pred indexom moov")
    return ftyp + mdat_header64(mdat_total)


def build_prefix(boundary: int, media_end: int, ftyp: bytes | None = None) -> bytes:
    """Ako build_graft_header, ale doplnene nulami presne do `boundary` bajtov.

    Pouziva sa, ked vieme, kde konci zasifrovana oblast - balast sa vynuluje,
    aby dekoder nezakopol o nahodne bajty vyzerajuce ako platne NAL jednotky.
    """
    header = build_graft_header(media_end, ftyp)
    if boundary < len(header):
        raise ValueError(
            f"Poškodený začiatok ({boundary} B) je kratší ako nová hlavička "
            f"({len(header)} B)")
    return header + b"\0" * (boundary - len(header))


# ---------------------------------------------------------------------------
# Zistenie, kde konci zasifrovana oblast
# ---------------------------------------------------------------------------

NAL_H264_OK = set(range(1, 24))
NAL_H265_OK = set(range(0, 41))


def looks_like_avc_sample(mm, offset: int, limit: int, hevc: bool = False,
                          length_size: int = 4) -> bool:
    """Overi, ci na danom offsete zacina platna vzorka H.264 / H.265.

    Vzorky su v MP4 ulozene ako [4B dlzka][NAL][4B dlzka][NAL]... Test overi,
    ze dlzka sedi do chunku a ze hlavicka NAL jednotky dava zmysel. Nahodne
    (zasifrovane) data maju sancu prejst radovo 1 : 100 000, a kedze hranicu
    este overujeme na viacerych po sebe iducich chunkoch, je vysledok spolahlivy.
    """
    if offset < 0 or offset + length_size + 1 > len(mm):
        return False
    limit = max(16, limit)
    slack = limit + 4096

    def nal_ok(hdr_byte: int) -> bool:
        if hdr_byte & 0x80:            # forbidden_zero_bit musi byt 0
            return False
        if hevc:
            return ((hdr_byte >> 1) & 0x3F) in NAL_H265_OK
        return (hdr_byte & 0x1F) in NAL_H264_OK

    n = int.from_bytes(mm[offset:offset + length_size], "big")
    if n <= 0 or n > slack:
        return False
    if not nal_ok(mm[offset + length_size]):
        return False

    return True


def validate_chunk(mm, track, chunk_index: int, max_samples: int = 4,
                   hevc: bool = False) -> bool:
    """Prisna kontrola chunku podla tabuliek stsc + stsz.

    Vzorky v chunku musia presne "vydlazdit" priestor: sucet dlzok NAL jednotiek
    sa musi rovnat velkosti vzorky z tabulky stsz. Zasifrovane data toto
    nesplnia prakticky nikdy.
    """
    if chunk_index >= len(track.chunk_offsets):
        return False
    spc = track.samples_per_chunk_list()
    first_idx = track.sample_index_of_chunk()
    if chunk_index >= len(spc) or not spc[chunk_index]:
        return False
    pos = track.chunk_offsets[chunk_index]
    s0 = first_idx[chunk_index]
    checked = 0
    for j in range(min(spc[chunk_index], max_samples)):
        size = track.size_of_sample(s0 + j)
        if size is None or size <= 0 or pos + size > len(mm):
            return False
        # vzorka musi byt presne pokryta NAL jednotkami
        p = pos
        end = pos + size
        while p + 5 <= end:
            n = int.from_bytes(mm[p:p + 4], "big")
            if n <= 0 or p + 4 + n > end:
                return False
            hdr = mm[p + 4]
            if hdr & 0x80:
                return False
            if hevc:
                if ((hdr >> 1) & 0x3F) > 40:
                    return False
            elif (hdr & 0x1F) not in NAL_H264_OK:
                return False
            p += 4 + n
        if p != end:
            return False
        pos = end
        checked += 1
    return checked > 0


def detect_damage_end(mm, tracks: list, struct_start: int, filesize: int) -> dict:
    # Pri neposkodenom subore zacina struktura na offsete 0 - vtedy je hornou
    # hranicou pre offsety chunkov koniec suboru, nie nula.
    limit = struct_start if struct_start > 0 else filesize
    """Najde koniec zasifrovanej oblasti pomocou tabuliek vzoriek.

    Zasifrovana cast je vzdy suvisly zaciatok suboru, takze staci najst prvy
    chunk video stopy, ktoreho obsah vyzera ako platne video data. Hladame
    polenim intervalu a vysledok overime linearne.
    """
    video = None
    for t in tracks:
        if t.handler == "vide" and t.chunk_offsets:
            video = t
            break
    if video is None:
        return {"damage_end": None, "method": "žiadna obrazová stopa s tabuľkou úsekov",
                "confident": False}

    hevc = video.codec.lower() in ("hvc1", "hev1", "hvcc", "dvh1", "dvhe")
    offsets = sorted(o for o in video.chunk_offsets if 0 <= o < limit)
    if not offsets:
        return {"damage_end": None, "method": "úseky mimo rozsahu", "confident": False}

    strict = bool(video.stsc) and (video.uniform_sample_size or video.sample_sizes)
    index_of = {o: i for i, o in enumerate(video.chunk_offsets)}

    def ok(i: int) -> bool:
        start = offsets[i]
        if strict:
            ci = index_of.get(start)
            if ci is not None:
                return validate_chunk(mm, video, ci, hevc=hevc)
        okno = (offsets[i + 1] - start) if i + 1 < len(offsets) else (limit - start)
        okno = max(16, min(okno, 8 * 1024 * 1024))
        return looks_like_avc_sample(mm, start, okno, hevc=hevc)

    if ok(0):
        return {"damage_end": 0, "method": "prvý úsek je neporušený", "confident": True,
                "damaged_chunks": 0, "total_chunks": len(video.chunk_offsets)}

    lo, hi = 0, len(offsets) - 1
    if not ok(hi):
        return {"damage_end": None, "method": "ani posledný úsek nevyzerá platne",
                "confident": False}
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    # overenie: par chunkov za hranicou musi byt tiez v poriadku
    good = sum(1 for j in range(hi, min(hi + 6, len(offsets))) if ok(j))
    confident = good >= min(3, len(offsets) - hi)
    return {
        "damage_end": offsets[hi],
        "method": ("prísna kontrola tabuliek stsc/stsz" if strict
                   else "validácia NAL jednotiek"),
        "confident": confident,
        "damaged_chunks": hi,
        "total_chunks": len(video.chunk_offsets),
    }


def open_mm(path: str):
    """Otvori subor a namapuje ho do pamate (read-only)."""
    f = open(path, "rb")
    size = os.path.getsize(path)
    if size == 0:
        f.close()
        raise ValueError("Súbor je prázdny")
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    return f, mm, size


def damage_map(mm, track, struct_start: int, max_checks: int = 4000, hevc: bool = False) -> dict:
    """Zisti, ktore casti suboru su poskodene - vratane "skakaveho" sifrovania.

    Niektory ransomver nesifruje suvisly zaciatok, ale kazdy N-ty blok. Vtedy
    neplati predpoklad "poskodeny je len zaciatok", preto prejdeme rovnomerne
    rozlozenu vzorku chunkov a zostavime mapu poskodenych useko.
    """
    n = len(track.chunk_offsets)
    if n == 0:
        return {"checked": 0, "bad": 0, "ranges": [], "contiguous_prefix": True}
    step = max(1, n // max_checks)
    indices = list(range(0, n, step))
    results = []
    for i in indices:
        results.append((i, validate_chunk(mm, track, i, hevc=hevc)))

    ranges = []
    start = None
    for i, good in results:
        if not good and start is None:
            start = track.chunk_offsets[i]
        elif good and start is not None:
            ranges.append([start, track.chunk_offsets[i]])
            start = None
    if start is not None:
        ranges.append([start, struct_start])

    bad = sum(1 for _, g in results if not g)
    contiguous = len(ranges) <= 1 and (not ranges or ranges[0][0] <= track.chunk_offsets[0])
    return {
        "checked": len(results),
        "bad": bad,
        "ratio_bad": round(bad / max(1, len(results)), 4),
        "ranges": ranges,
        "contiguous_prefix": contiguous,
        "step": step,
    }


def avcc_parameter_sets(path: str) -> bytes:
    """Vytiahne SPS/PPS z boxu `avcC` v zdravom MP4/MOV subore.

    Ak ma pouzivatel k dispozicii hoci len jeden neposkodeny subor z rovnakeho
    zariadenia (aj uplne ine, kratke video), su v nom presne tie parametre,
    ktore poskodenemu suboru chybaju.
    """
    f, mm, size = open_mm(path)
    try:
        boxes, _ = chain_from(mm, 0, size)
        moov = next((b for b in boxes if b.type == b"moov"), None)
        if moov is None:
            return b""
        moov.children = parse_tree(mm, moov.data_offset, moov.end, size)
        out = []
        for avcc in moov.find_all(b"avcC"):
            d = mm[avcc.data_offset:avcc.end]
            if len(d) < 7:
                continue
            pos = 5
            num_sps = d[pos] & 0x1F
            pos += 1
            for _ in range(num_sps):
                if pos + 2 > len(d):
                    break
                n = int.from_bytes(d[pos:pos + 2], "big")
                pos += 2
                out.append(b"\x00\x00\x00\x01" + d[pos:pos + n])
                pos += n
            if pos < len(d):
                num_pps = d[pos]
                pos += 1
                for _ in range(num_pps):
                    if pos + 2 > len(d):
                        break
                    n = int.from_bytes(d[pos:pos + 2], "big")
                    pos += 2
                    out.append(b"\x00\x00\x00\x01" + d[pos:pos + n])
                    pos += n
        return b"".join(out)
    finally:
        mm.close()
        f.close()
