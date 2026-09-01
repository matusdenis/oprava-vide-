"""Vyrezavanie surovych dat, ked index `moov` neprezil.

Ked ransomver zasifroval zaciatok suboru, v ktorom bol ulozeny index (tzv.
faststart subory), tabulky vzoriek su nenavratne prec. Vtedy sa da video
zachranit len tak, ze sa z tela suboru vyrezu samotne obrazove data a nanovo
sa zabalia. Tento modul to vie pre:

  * MPEG-TS / M2TS / AVCHD - format sa synchronizuje sam, staci najst prvy
    platny paket (uspesnost takmer 100 %)
  * H.264 / H.265 v MP4 - vzorky su [4B dlzka][NAL]; ak stream obsahuje
    parametre SPS/PPS priamo v tele (bezne pri kamerach), da sa z neho
    poskladat plnohodnotny surovy stream .h264 / .h265
"""
from __future__ import annotations

import os
import struct

TS_PACKET_SIZES = (188, 192, 204, 208)
START_CODE = b"\x00\x00\x00\x01"


# ---------------------------------------------------------------------------
# MPEG-TS / M2TS
# ---------------------------------------------------------------------------

def find_ts_sync(mm, size: int, search_limit: int = 64 * 1024 * 1024) -> dict | None:
    """Najde prvy offset, od ktoreho ide pravidelny sled TS paketov.

    Vracia {'offset': ..., 'packet_size': ...} alebo None.
    """
    limit = min(size, search_limit)
    for pkt in TS_PACKET_SIZES:
        pos = 0
        while pos < limit:
            idx = mm.find(b"\x47", pos, limit)
            if idx < 0:
                break
            # overime 20 po sebe iducich paketov
            ok = True
            for k in range(1, 20):
                p = idx + k * pkt
                if p >= size:
                    ok = k >= 8      # koniec suboru, staci kratsia seria
                    break
                if mm[p] != 0x47:
                    ok = False
                    break
            if ok:
                return {"offset": idx, "packet_size": pkt}
            pos = idx + 1
    return None


def find_ts_parameters_offset(mm, size: int, sync_offset: int, packet_size: int,
                              backoff_packets: int = 600) -> int | None:
    """Najde miesto v TS toku, od ktoreho su k dispozicii parametre SPS.

    V TS su NAL jednotky ulozene v tvare Annex-B, takze SPS sa da najst priamo
    ako start kod 00 00 01 67. Ked zasifrovany zaciatok zoberie prvy klucovy
    snimok, dalsi moze byt aj niekolko MB dalej - a dovtedy nevie ziadny
    prehravac obraz dekodovat. Preto pre prebalenie zacneme az tesne pred nim.
    """
    for pattern in (b"\x00\x00\x01\x67", b"\x00\x00\x01\x27"):
        idx = mm.find(pattern, sync_offset)
        if idx >= 0:
            pkt_index = (idx - sync_offset) // packet_size
            start_pkt = max(0, pkt_index - backoff_packets)
            return sync_offset + start_pkt * packet_size
    return None


def carve_ts(src_path: str, dst_path: str, offset: int, log=None,
             chunk: int = 8 << 20) -> int:
    """Skopiruje subor od prveho platneho TS paketu dalej."""
    written = 0
    with open(src_path, "rb") as fi, open(dst_path, "wb") as fo:
        fi.seek(offset)
        while True:
            data = fi.read(chunk)
            if not data:
                break
            fo.write(data)
            written += len(data)
            if log and written % (256 << 20) < chunk:
                log(f"  zapísaných {written // (1 << 20)} MiB")
    return written


def ts_video_pid(mm, size: int, sync_offset: int, packet_size: int,
                 max_packets: int = 20000) -> int | None:
    """Urci PID video stopy - hlada PES hlavicku so stream_id 0xE0 az 0xEF."""
    pocty = {}
    pos = sync_offset
    n = 0
    while pos + packet_size <= size and n < max_packets:
        if mm[pos] != 0x47:
            pos += 1
            continue
        pid = ((mm[pos + 1] & 0x1F) << 8) | mm[pos + 2]
        pusi = mm[pos + 1] & 0x40
        adapt = (mm[pos + 3] >> 4) & 0x3
        payload = pos + 4
        if adapt in (2, 3):
            payload += 1 + mm[pos + 4]
        if pusi and payload + 4 <= pos + packet_size:
            if mm[payload:payload + 3] == b"\x00\x00\x01":
                sid = mm[payload + 3]
                if 0xE0 <= sid <= 0xEF:
                    pocty[pid] = pocty.get(pid, 0) + 1
        pos += packet_size
        n += 1
    if not pocty:
        return None
    return max(pocty, key=pocty.get)


def ts_extract_video(mm, size: int, sync_offset: int, packet_size: int, dst_path: str,
                     pid: int | None = None, log=None) -> dict:
    """Vytiahne z TS toku surovy obrazovy stream (Annex-B).

    Nezavisi na demuxeri ffmpeg - hodi sa, ked ten na poskodenom toku zlyha.
    Z kazdeho paketu danej stopy sa odstrani hlavicka TS aj PES a zvysok sa
    zapise za sebou. Vysledok je priamo pouzitelny .h264 subor.
    """
    if pid is None:
        pid = ts_video_pid(mm, size, sync_offset, packet_size)
    if pid is None:
        return {"bytes": 0, "packets": 0, "pid": None, "path": dst_path}
    if log:
        log(f"  obrazová stopa má PID {pid}")
    written = pakety = 0
    with open(dst_path, "wb") as fo:
        pos = sync_offset
        while pos + packet_size <= size:
            if mm[pos] != 0x47:
                pos += 1
                continue
            if (((mm[pos + 1] & 0x1F) << 8) | mm[pos + 2]) != pid:
                pos += packet_size
                continue
            pusi = mm[pos + 1] & 0x40
            adapt = (mm[pos + 3] >> 4) & 0x3
            payload = pos + 4
            if adapt in (2, 3):
                payload += 1 + mm[pos + 4]
            if adapt == 2 or payload >= pos + packet_size:
                pos += packet_size
                continue
            if pusi:
                # preskocenie hlavicky PES: 00 00 01 <sid> <dlzka:2> <flagy:2> <hdrlen>
                if mm[payload:payload + 3] == b"\x00\x00\x01" and payload + 9 <= pos + packet_size:
                    hdr_len = mm[payload + 8]
                    payload += 9 + hdr_len
            if payload < pos + packet_size:
                fo.write(mm[payload:pos + packet_size])
                written += pos + packet_size - payload
                pakety += 1
            pos += packet_size
    if log:
        log(f"  vytiahnutých {pakety} paketov, {written // 1024} KiB")
    return {"bytes": written, "packets": pakety, "pid": pid, "path": dst_path}


# ---------------------------------------------------------------------------
# H.264 / H.265 - vzorky s dlzkovym prefixom
# ---------------------------------------------------------------------------

VIDEO_NAL_TYPES_H264 = {1, 5, 6, 7, 8, 9}      # rez, IDR, SEI, SPS, PPS, AUD
VIDEO_NAL_TYPES_H265 = {0, 1, 19, 20, 21, 32, 33, 34, 35, 39}


def nal_here(mm, p: int, size: int, hevc: bool = False, min_len: int = 1,
             max_len: int = 16 << 20, strict_type: bool = True, deep: bool = False):
    """Ak na pozicii `p` zacina vierohodna vzorka [4B dlzka][NAL], vrati jej dlzku."""
    if p + 5 > size:
        return None
    n = int.from_bytes(mm[p:p + 4], "big")
    if n < min_len or n > max_len or p + 4 + n > size:
        return None
    hdr = mm[p + 4]
    if hdr & 0x80:
        return None
    t = ((hdr >> 1) & 0x3F) if hevc else (hdr & 0x1F)
    allowed = VIDEO_NAL_TYPES_H265 if hevc else VIDEO_NAL_TYPES_H264
    if strict_type and t not in allowed:
        return None
    if not strict_type and (t == 0 or (not hevc and t > 23)):
        return None
    if deep and not validate_nal_payload(mm[p + 4:p + 4 + min(n, 64)], hevc):
        return None
    return n


def nal_chain_length(mm, start: int, end: int, hevc: bool = False,
                     max_nals: int = 40) -> int:
    """Kolko platnych NAL jednotiek tesne za sebou nasleduje od `start`."""
    pos = start
    count = 0
    while count < max_nals:
        n = nal_here(mm, pos, end, hevc, strict_type=False)
        if n is None:
            break
        count += 1
        pos += 4 + n
    return count


def _resync(mm, pos: int, end: int, hevc: bool, window: int, min_len: int,
            max_len: int = 4 << 20):
    """Najde dalsiu vierohodnu video vzorku najviac `window` bajtov dopredu.

    Medzery vznikaju uplne bezne: v subore su video a zvuk prelozene, takze za
    kazdou video vzorkou byva kusok zvuku, ktory NAL jednotkou nie je.
    """
    stop = min(pos + window, end)
    p = pos
    while p < stop:
        idx = mm.find(b"\x00", p, stop)
        if idx < 0:
            return None
        if nal_here(mm, idx, end, hevc, min_len=min_len, max_len=max_len,
                    deep=True) is not None:
            return idx
        p = idx + 1
    return None


def walk_stats(mm, start: int, end: int, hevc: bool = False, max_nals: int = 120,
               gap_window: int = 1 << 20, min_len: int = 256,
               budget: int = 8 << 20, max_len: int = 256 << 10) -> dict:
    """Prejde stream od `start` a vrati statistiku - ako "video" to vyzera.

    Od spravneho zaciatku vzorky vyjde vela NAL jednotiek, ktore priestor
    presne "vydlazdia" (pokrytie okolo 99 %) a medzi nimi su len kratke useky
    zvuku. Od nahodneho miesta v zasifrovanych datach vyjde malo jednotiek s
    nezmyselne velkymi dlzkami, takze pokrytie aj ich pocet zaostavaju.
    """
    pos = start
    limit = min(end, start + budget)
    nals = covered = gaps = 0
    while pos + 5 <= limit and nals < max_nals:
        n = nal_here(mm, pos, end, hevc, min_len=1, max_len=max_len,
                     strict_type=False, deep=True)
        if n is None:
            nxt = _resync(mm, pos + 1, limit, hevc, gap_window, min_len, max_len)
            if nxt is None:
                break
            gaps += 1
            pos = nxt
            continue
        nals += 1
        covered += 4 + n
        pos += 4 + n
    span = max(1, pos - start)
    return {"nals": nals, "gaps": gaps, "covered": covered, "span": span,
            "coverage": round(covered / span, 4), "end": pos}


# Dve kola hladania: najprv prisne (bezne videa), potom volnejsie pre
# vysokobitratove zaznamy (4K), kde ma jeden snimok aj niekolko MB.
SCAN_PASSES = (
    {"max_len": 256 << 10, "min_nals": 20, "min_coverage": 0.90, "popis": "prísne"},
    {"max_len": 4 << 20, "min_nals": 8, "min_coverage": 0.90, "popis": "voľnejšie (vysoký dátový tok)"},
)


def find_nal_stream(mm, size: int, hint: int = 0, hevc: bool = False,
                    min_len: int = 256, log=None, limit: int | None = None,
                    max_candidates: int = 40000) -> dict | None:
    """Najde prvu skutocnu video vzorku od pozicie `hint`.

    Vzorky nie su v subore nijako zarovnane, preto sa hlada bajt po bajte.
    Rychlost zachranuje predfilter (dlzkovy prefix zacina nulovym bajtom) a
    kazdy kandidat sa overi prechadzkou - az ked od neho vyjde dlhy sled NAL
    jednotiek s takmer uplnym pokrytim, ide o skutocny zaciatok streamu.
    """
    end = size if limit is None else min(size, limit)
    for scan in SCAN_PASSES:
        pos = max(0, hint)
        tried = 0
        reported = pos
        while pos < end and tried < max_candidates:
            idx = mm.find(b"\x00", pos, end)
            if idx < 0:
                break
            if nal_here(mm, idx, size, hevc, min_len=min_len,
                        max_len=scan["max_len"], deep=True) is not None:
                tried += 1
                st = walk_stats(mm, idx, size, hevc, max_len=scan["max_len"])
                if st["nals"] >= scan["min_nals"] and st["coverage"] >= scan["min_coverage"]:
                    if log:
                        log(f"  začiatok obrazového streamu na offsete {idx} "
                            f"({st['nals']} NAL jednotiek, pokrytie "
                            f"{st['coverage'] * 100:.1f} %, kolo: {scan['popis']})")
                    return {"offset": idx, "max_len": scan["max_len"],
                            "statistika": st, "kolo": scan["popis"]}
            pos = idx + 1
            if log and pos - reported > (32 << 20):
                reported = pos
                log(f"  prehľadaných {pos // (1 << 20)} MiB…")
        if log:
            log(f"  kolo „{scan['popis']}“ nenašlo začiatok, skúšam voľnejšie kritériá")
    return None


def find_nal_stream_start(mm, size: int, **kw) -> int | None:
    """Ako `find_nal_stream`, ale vracia iba offset."""
    r = find_nal_stream(mm, size, **kw)
    return r["offset"] if r else None


def extract_annexb(mm, start: int, end: int, dst_path: str, hevc: bool = False,
                   sps_pps: bytes = b"", log=None, min_len: int = 256,
                   gap_window: int = 1 << 20, max_len: int = 256 << 10) -> dict:
    """Prevedie vzorky s dlzkovym prefixom na surovy stream Annex-B.

    Annex-B (start kody 00 00 00 01) vie ffmpeg nacitat aj uplne bez akejkolvek
    hlavicky kontajnera - preto je to idealny medzikrok pri zachrane.
    """
    written = nals = gaps = 0
    have_sps = have_pps = False
    pos = start
    with open(dst_path, "wb") as fo:
        if sps_pps:
            fo.write(sps_pps)
            written += len(sps_pps)
            have_sps = have_pps = True
        while pos + 5 <= end:
            n = nal_here(mm, pos, end, hevc, min_len=1, max_len=max_len,
                         strict_type=False, deep=True)
            if n is None:
                nxt = _resync(mm, pos + 1, end, hevc, gap_window, min_len, max_len)
                if nxt is None:
                    break
                gaps += 1
                pos = nxt
                continue
            nal = mm[pos + 4:pos + 4 + n]
            t = ((nal[0] >> 1) & 0x3F) if hevc else (nal[0] & 0x1F)
            if hevc:
                have_sps = have_sps or t == 33
                have_pps = have_pps or t == 34
            else:
                have_sps = have_sps or t == 7
                have_pps = have_pps or t == 8
            fo.write(START_CODE)
            fo.write(nal)
            written += 4 + n
            nals += 1
            pos += 4 + n
            if log and nals % 20000 == 0:
                log(f"  spracovaných {nals} NAL jednotiek ({written // (1 << 20)} MiB)")
    return {"bytes": written, "nals": nals, "gaps": gaps,
            "has_sps": have_sps, "has_pps": have_pps, "path": dst_path}


def collect_parameter_sets(mm, start: int, end: int, hevc: bool = False,
                           scan_nals: int = 6000) -> bytes:
    """Pozbiera SPS/PPS (resp. VPS/SPS/PPS) najdene priamo v tele streamu."""
    out = []
    seen = set()
    pos = start
    count = 0
    while pos + 5 <= end and count < scan_nals:
        n = int.from_bytes(mm[pos:pos + 4], "big")
        if n <= 0 or pos + 4 + n > end:
            break
        nal = mm[pos + 4:pos + 4 + n]
        t = ((nal[0] >> 1) & 0x3F) if hevc else (nal[0] & 0x1F)
        wanted = (32, 33, 34) if hevc else (7, 8)
        if t in wanted and nal[:24] not in seen:
            seen.add(nal[:24])
            out.append(START_CODE + bytes(nal))
        pos += 4 + n
        count += 1
    return b"".join(out)


def parameter_sets_anywhere(path: str, max_nals: int = 400000) -> bytes:
    """Najde v surovom Annex-B streame prvu platnu dvojicu SPS + PPS.

    Kluc k zachrane tokov, ktorym ransomver zobral zaciatok: parametre sa v
    subore opakuju pri kazdom klucovom snimku, takze aj ked prvy vyskyt padol
    za obet sifrovaniu, dalsi sa najde o kus dalej - staci ho skopirovat na
    zaciatok a prehravac uz vie obraz dekodovat.
    """
    import mmap as _mmap
    sps = pps = None
    with open(path, "rb") as f:
        if os.path.getsize(path) < 8:
            return b""
        mm = _mmap.mmap(f.fileno(), 0, access=_mmap.ACCESS_READ)
        try:
            pos = 0
            seen = 0
            while seen < max_nals:
                idx = mm.find(b"\x00\x00\x01", pos)
                if idx < 0:
                    break
                seen += 1
                nxt = mm.find(b"\x00\x00\x01", idx + 3)
                nal = mm[idx + 3:nxt if nxt > 0 else min(len(mm), idx + 3 + (1 << 20))]
                if nal:
                    t = nal[0] & 0x1F
                    if t == 7 and sps is None and plausible_sps(bytes(nal)):
                        sps = bytes(nal)
                    elif t == 8 and pps is None and 2 <= len(nal) <= 256:
                        pps = bytes(nal)
                if sps and pps:
                    break
                if nxt < 0:
                    break
                pos = nxt
        finally:
            mm.close()
    if sps and pps:
        return START_CODE + sps + START_CODE + pps
    return b""


# ---------------------------------------------------------------------------
# Minimalny parser SPS (H.264) - potrebny na urcenie rozlisenia
# ---------------------------------------------------------------------------

class _BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bit(self) -> int:
        byte = self.pos >> 3
        if byte >= len(self.data):
            raise EOFError
        b = (self.data[byte] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self) -> int:
        """Exp-Golomb bez znamienka."""
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("neplatny exp-Golomb kod")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _unescape(data: bytes) -> bytes:
    """Odstrani emulation prevention bajty (00 00 03 -> 00 00)."""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def parse_h264_sps(sps: bytes) -> dict | None:
    """Vytiahne zo SPS profil, uroven a rozlisenie."""
    try:
        if not sps:
            return None
        if sps[0] & 0x1F != 7:
            return None
        r = _BitReader(_unescape(sps[1:]))
        profile_idc = r.bits(8)
        r.bits(8)                      # constraint flags + reserved
        level_idc = r.bits(8)
        r.ue()                         # seq_parameter_set_id
        chroma_format_idc = 1
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = r.ue()
            if chroma_format_idc == 3:
                r.bit()
            r.ue()                     # bit_depth_luma_minus8
            r.ue()                     # bit_depth_chroma_minus8
            r.bit()                    # qpprime_y_zero_transform_bypass_flag
            if r.bit():                # seq_scaling_matrix_present_flag
                for i in range(8 if chroma_format_idc != 3 else 12):
                    if r.bit():
                        last = next_ = 8
                        for _ in range(16 if i < 6 else 64):
                            if next_:
                                next_ = (last + r.se() + 256) % 256
                            last = next_ or last
        r.ue()                         # log2_max_frame_num_minus4
        pic_order_cnt_type = r.ue()
        if pic_order_cnt_type == 0:
            r.ue()
        elif pic_order_cnt_type == 1:
            r.bit()
            r.se()
            r.se()
            for _ in range(r.ue()):
                r.se()
        r.ue()                         # max_num_ref_frames
        r.bit()                        # gaps_in_frame_num_value_allowed_flag
        width_mbs = r.ue() + 1
        height_map = r.ue() + 1
        frame_mbs_only = r.bit()
        if not frame_mbs_only:
            r.bit()                    # mb_adaptive_frame_field_flag
        r.bit()                        # direct_8x8_inference_flag
        crop_l = crop_r = crop_t = crop_b = 0
        if r.bit():                    # frame_cropping_flag
            crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()
        sub_w = 2 if chroma_format_idc in (1, 2) else 1
        sub_h = 2 if chroma_format_idc == 1 else 1
        width = width_mbs * 16 - (crop_l + crop_r) * sub_w
        height = (2 - frame_mbs_only) * height_map * 16 - (crop_t + crop_b) * sub_h
        names = {66: "baseline", 77: "main", 88: "extended", 100: "high",
                 110: "high10", 122: "high422", 244: "high444"}
        return {
            "profile_idc": profile_idc,
            "profil": names.get(profile_idc, str(profile_idc)),
            "level_idc": level_idc,
            "uroven": f"{level_idc // 10}.{level_idc % 10}",
            "sirka": width,
            "vyska": height,
        }
    except (EOFError, ValueError, IndexError):
        return None


ZNAME_PROFILY = {66, 77, 88, 100, 110, 122, 244, 44, 83, 86, 118, 128}


def plausible_sps(nal: bytes) -> dict | None:
    """Vrati rozparsovane SPS, len ak su hodnoty naozaj vierohodne.

    Bez tejto kontroly by za SPS prehlasilo nahodne data - staci, aby nahodou
    presli exp-Golomb kodovanim.
    """
    info = parse_h264_sps(nal)
    if not info:
        return None
    if info["profile_idc"] not in ZNAME_PROFILY:
        return None
    if not (128 <= info["sirka"] <= 16384 and 96 <= info["vyska"] <= 16384):
        return None
    if info["sirka"] % 2 or info["vyska"] % 2:
        return None
    if not (10 <= info["level_idc"] <= 62):
        return None
    return info


def find_sps_in_annexb(data: bytes) -> bytes | None:
    """Najde prvu SPS jednotku v surovom Annex-B streame."""
    pos = 0
    while True:
        idx = data.find(START_CODE, pos)
        if idx < 0:
            return None
        nxt = data.find(START_CODE, idx + 4)
        nal = data[idx + 4:nxt if nxt > 0 else len(data)]
        if nal and (nal[0] & 0x1F) == 7:
            return nal
        if nxt < 0:
            return None
        pos = nxt


def validate_nal_payload(nal: bytes, hevc: bool = False) -> bool:
    """Overi, ci obsah NAL jednotky dava zmysel (nie len jej hlavicka).

    Toto je hlavna poistka proti falosnym zhodam v zasifrovanych datach:
    hlavicka rezu musi obsahovat platne exp-Golomb hodnoty, SPS sa musi dat
    rozparsovat na rozumne rozlisenie a pod.
    """
    if len(nal) < 2:
        return False
    if hevc:
        t = (nal[0] >> 1) & 0x3F
        if t in (32, 33, 34):
            return len(nal) >= 4
        if t <= 21:
            return len(nal) >= 3
        return True
    t = nal[0] & 0x1F
    try:
        if t in (1, 5):                       # rez obrazu
            r = _BitReader(_unescape(nal[1:12]))
            first_mb = r.ue()
            slice_type = r.ue()
            pps_id = r.ue()
            return first_mb < 200000 and slice_type <= 9 and pps_id <= 255
        if t == 7:                            # SPS
            return bool(plausible_sps(nal))
        if t == 8:                            # PPS
            r = _BitReader(_unescape(nal[1:8]))
            pps_id = r.ue()
            sps_id = r.ue()
            return pps_id <= 255 and sps_id <= 31
        if t == 6:                            # SEI
            i = 0
            payload_type = 0
            while i < len(nal) - 1 and nal[1 + i] == 0xFF:
                payload_type += 255
                i += 1
            if 1 + i >= len(nal):
                return False
            payload_type += nal[1 + i]
            return payload_type <= 200
        if t == 9:                            # oddelovac pristupovej jednotky
            return len(nal) <= 4
    except (EOFError, ValueError, IndexError):
        return False
    return True


def zero_density(mm, offset: int, block: int = 65536) -> float:
    """Podiel nulovych bajtov v bloku.

    Sifrovane data su rovnomerne nahodne, takze nul je presne 1/256 (0,39 %).
    Komprimovane video ma nul podstatne viac (bezne 0,8 az 3 %). Rozdiel je pri
    64 KiB bloku statisticky velmi vyrazny, takze sa da spolahlivo urcit, kde
    zasifrovana cast konci.
    """
    data = mm[offset:offset + block]
    if not data:
        return 0.0
    return data.count(0) / len(data)


def estimate_encrypted_prefix(mm, size: int, block: int = 65536,
                              threshold: float = 0.0060, log=None) -> dict:
    """Odhadne dlzku zasifrovaneho zaciatku podla podielu nulovych bajtov."""
    n_blocks = max(1, size // block)
    first_good = None
    densities = []
    for i in range(min(n_blocks, 4096)):
        d = zero_density(mm, i * block, block)
        densities.append(round(d, 5))
        if d >= threshold:
            # potvrdenie: nasledujuce bloky musia byt tiez "nesifrovane"
            confirm = [zero_density(mm, (i + k) * block, block) for k in range(1, 4)
                       if (i + k) * block < size]
            if sum(1 for c in confirm if c >= threshold) >= max(1, len(confirm) - 1):
                first_good = i * block
                break
    if first_good is None:
        return {"koniec": None, "spolahlivost": "nízka",
                "poznamka": "Nepodarilo sa nájsť neporušenú časť — súbor môže byť zašifrovaný celý.",
                "hustoty": densities[:64]}
    if log:
        log(f"  štatistický odhad konca zašifrovanej časti: {first_good} B")
    return {"koniec": first_good, "spolahlivost": "dobra" if first_good else "dobra",
            "prah": threshold, "hustoty": densities[:64]}


# ---------------------------------------------------------------------------
# Nahradny (synteticky) vzorovy subor pre untrunc
# ---------------------------------------------------------------------------

def synthesize_reference(toolbox, dst_path: str, width: int = 1920, height: int = 1080,
                         fps: int = 30, profile: str = "high", audio: bool = True,
                         seconds: int = 8, log=None) -> str:
    """Vyrobi zdravy vzorovy MP4 pomocou ffmpeg.

    Nastroj untrunc potrebuje zdravy subor s ROVNAKYM nastavenim kodeku. Ked
    pouzivatel ziadny taky nema, vyrobime ho umelo podla parametrov zistenych
    z poskodeneho suboru (rozlisenie a profil zo SPS).
    """
    args = ["-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-profile:v", profile,
             "-pix_fmt", "yuv420p", "-preset", "veryfast"]
    if audio:
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest"]
    args += [dst_path]
    res = toolbox.run("ffmpeg", args, log=log)
    if res["code"] != 0 or not os.path.exists(dst_path):
        raise RuntimeError("Nepodarilo sa vyrobiť vzorový súbor pomocou ffmpeg:\n"
                           + res["output"][-800:])
    return dst_path
