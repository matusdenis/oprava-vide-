"""Rekonstrukcia parametrov H.264 (SPS/PPS), ked bola hlavicka zasifrovana.

Bez SPS a PPS sa video neda dekodovat - su v nich rozlisenie a nastavenia,
podla ktorych sa citaju hlavicky jednotlivych snimkov. V MP4 su ulozene v boxe
`avcC` vnutri `moov`; ak ransomver zasifroval zaciatok suboru, su prec.

Tento modul ich vie POSKLADAT NANOVO a spravnu kombinaciu najde skusanim:
kazdy kandidat sa predradi vyrezanemu streamu, kusok sa skusi dekodovat a
vyhra ten, pri ktorom ffmpeg dekoduje najviac snimkov. Nespravne parametre
sa prejavia okamzite - dekoder nespracuje takmer nic.
"""
from __future__ import annotations

import os
import re

from . import carve

START_CODE = b"\x00\x00\x00\x01"

# Rozlisenia, ktore sa skusaju, ked pouzivatel nevie povedat, ake video to bolo.
BEZNE_ROZLISENIA = [
    (1920, 1080), (1280, 720), (3840, 2160), (1080, 1920), (720, 1280),
    (2560, 1440), (2704, 1520), (640, 480), (854, 480), (1440, 1080),
    (2160, 3840), (4096, 2160),
]


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, value: int, n: int):
        for i in range(n - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ue(self, value: int):
        value += 1
        n = value.bit_length()
        self.u(0, n - 1)
        self.u(value, n)

    def se(self, value: int):
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def trailing(self):
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)

    def bytes(self) -> bytes:
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            byte = 0
            for b in self.bits[i:i + 8]:
                byte = (byte << 1) | b
            out.append(byte)
        return bytes(out)


def _escape(data: bytes) -> bytes:
    """Vlozi ochranne bajty (emulation prevention), aby v tele nevznikol start kod."""
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def build_sps(width: int, height: int, profile_idc: int = 100, level_idc: int = 40,
              log2_max_frame_num_minus4: int = 0, pic_order_cnt_type: int = 0,
              log2_max_poc_lsb_minus4: int = 2, max_num_ref_frames: int = 1) -> bytes:
    """Poskladá SPS podla ISO/IEC 14496-10 (H.264), sekcia 7.3.2.1."""
    w = BitWriter()
    w.u(profile_idc, 8)
    w.u(0, 8)                       # constraint_set flags + reserved
    w.u(level_idc, 8)
    w.ue(0)                         # seq_parameter_set_id
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128):
        w.ue(1)                     # chroma_format_idc = 4:2:0
        w.ue(0)                     # bit_depth_luma_minus8
        w.ue(0)                     # bit_depth_chroma_minus8
        w.u(0, 1)                   # qpprime_y_zero_transform_bypass_flag
        w.u(0, 1)                   # seq_scaling_matrix_present_flag
    w.ue(log2_max_frame_num_minus4)
    w.ue(pic_order_cnt_type)
    if pic_order_cnt_type == 0:
        w.ue(log2_max_poc_lsb_minus4)
    w.ue(max_num_ref_frames)
    w.u(0, 1)                       # gaps_in_frame_num_value_allowed_flag
    mb_w = (width + 15) // 16
    mb_h = (height + 15) // 16
    w.ue(mb_w - 1)
    w.ue(mb_h - 1)
    w.u(1, 1)                       # frame_mbs_only_flag
    w.u(1, 1)                       # direct_8x8_inference_flag
    crop_r = (mb_w * 16 - width) // 2
    crop_b = (mb_h * 16 - height) // 2
    if crop_r or crop_b:
        w.u(1, 1)                   # frame_cropping_flag
        w.ue(0)
        w.ue(crop_r)
        w.ue(0)
        w.ue(crop_b)
    else:
        w.u(0, 1)
    w.u(0, 1)                       # vui_parameters_present_flag
    w.trailing()
    return bytes([0x67]) + _escape(w.bytes())


def build_pps(entropy_coding_mode: int = 1, deblocking_control: int = 1,
              num_ref_idx_l0: int = 1) -> bytes:
    """Poskladá PPS podla ISO/IEC 14496-10, sekcia 7.3.2.2."""
    w = BitWriter()
    w.ue(0)                         # pic_parameter_set_id
    w.ue(0)                         # seq_parameter_set_id
    w.u(entropy_coding_mode, 1)
    w.u(0, 1)                       # bottom_field_pic_order_in_frame_present_flag
    w.ue(0)                         # num_slice_groups_minus1
    w.ue(num_ref_idx_l0 - 1)
    w.ue(num_ref_idx_l0 - 1)
    w.u(0, 1)                       # weighted_pred_flag
    w.u(0, 2)                       # weighted_bipred_idc
    w.se(0)                         # pic_init_qp_minus26
    w.se(0)                         # pic_init_qs_minus26
    w.se(0)                         # chroma_qp_index_offset
    w.u(deblocking_control, 1)
    w.u(0, 1)                       # constrained_intra_pred_flag
    w.u(0, 1)                       # redundant_pic_cnt_present_flag
    w.trailing()
    return bytes([0x68]) + _escape(w.bytes())


def header_blob(sps: bytes, pps: bytes) -> bytes:
    return START_CODE + sps + START_CODE + pps


def candidate_grid(resolutions=None, quick: bool = False) -> list:
    """Zoznam kombinacii parametrov, ktore sa budu skusat.

    Poradie je od najbeznejsich nastaveni po tie zriedkavejsie, aby sa spravna
    kombinacia nasla co najskor.
    """
    resolutions = resolutions or BEZNE_ROZLISENIA
    profiles = [(100, 1), (77, 1), (66, 0)]          # (profile_idc, CABAC?)
    if quick:
        frame_nums = [0, 2]
        pocs = [(0, 2), (2, 0)]
        refs = [1, 3]
    else:
        frame_nums = [0, 1, 2, 4]
        pocs = [(0, 2), (2, 0), (0, 0), (0, 4)]
        refs = [1, 2, 3, 4]
    out = []
    for (w, h) in resolutions:
        for (prof, cabac) in profiles:
            for fn in frame_nums:
                for (poc, poclsb) in pocs:
                    for ref in refs:
                        out.append({
                            "sirka": w, "vyska": h, "profile_idc": prof,
                            "cabac": cabac, "log2_max_frame_num_minus4": fn,
                            "pic_order_cnt_type": poc,
                            "log2_max_poc_lsb_minus4": poclsb,
                            "max_num_ref_frames": ref,
                        })
    return out


def blob_for(candidate: dict) -> bytes:
    sps = build_sps(candidate["sirka"], candidate["vyska"],
                    profile_idc=candidate["profile_idc"],
                    log2_max_frame_num_minus4=candidate["log2_max_frame_num_minus4"],
                    pic_order_cnt_type=candidate["pic_order_cnt_type"],
                    log2_max_poc_lsb_minus4=candidate["log2_max_poc_lsb_minus4"],
                    max_num_ref_frames=candidate["max_num_ref_frames"])
    pps = build_pps(entropy_coding_mode=candidate["cabac"])
    return header_blob(sps, pps)


_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def score_headers(toolbox, blob: bytes, sample: bytes, workdir: str,
                  timeout: int = 180, hevc: bool = False) -> dict:
    """Predradi kandidatske hlavicky vzorke streamu a skusi ju dekodovat.

    Rozhoduje nie samotny pocet snimkov, ale ich pomer k poctu chybovych
    hlaseni: pri nespravnom rozliseni dekoder nieco vyprodukuje tiez, ale
    zahlti sa chybami. Kvalita = snimky / (snimky + chyby).
    """
    test_path = os.path.join(workdir, "_test_hlavicka." + ("h265" if hevc else "h264"))
    with open(test_path, "wb") as f:
        f.write(blob)
        f.write(sample)
    ffmpeg = toolbox.path("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg nie je k dispozícii")
    import subprocess
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-stats",
                            "-f", "hevc" if hevc else "h264", "-i", test_path,
                            "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout)
        frames = 0
        errors = 0
        for line in r.stderr.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _FRAME_RE.search(line)
            if m and line.startswith("frame="):
                frames = max(frames, int(m.group(1)))
            else:
                errors += 1
        kvalita = frames / (frames + errors) if (frames + errors) else 0.0
        return {"snimky": frames, "chyby": errors, "kvalita": round(kvalita, 4),
                "skore": round(frames * kvalita, 2)}
    except subprocess.TimeoutExpired:
        return {"snimky": 0, "chyby": 9999, "kvalita": 0.0, "skore": 0.0}
    finally:
        try:
            os.remove(test_path)
        except OSError:
            pass


def find_best_headers(toolbox, stream_path: str, workdir: str, resolutions=None,
                      sample_bytes: int = 3 << 20, quick: bool = False,
                      log=None, max_tries: int = 2500,
                      good_quality: float = 0.80, good_frames: int = 20,
                      extra=None, hevc: bool = False) -> dict | None:
    """Skusa kombinacie parametrov, kym nenajde take, pri ktorych sa video dekoduje.

    Ak pouzivatel pozna rozlisenie povodneho videa, staci ho zadat - pocet
    kombinacii tym klesne radovo a hladanie trva sekundy namiesto minut.
    """
    with open(stream_path, "rb") as f:
        sample = f.read(sample_bytes)
    if not sample:
        return None
    # Najprv sa skusia hotove hlavicky (najdene v tele streamu alebo vytiahnute
    # z ineho suboru z rovnakeho zariadenia) - byvaju presne spravne.
    polozky = []
    for e in (extra or []):
        kandidat = e.get("kandidat")
        if kandidat is None and e.get("blob"):
            # Aj pri hotovej hlavičke vieme povedať, aké rozlíšenie prináša —
            # stačí z nej prečítať SPS.
            sps = (carve.find_nal_in_annexb(e["blob"], 33, hevc=True) if hevc
                   else carve.find_sps_in_annexb(e["blob"]))
            info = ((carve.plausible_hevc_sps(sps) if hevc else carve.plausible_sps(sps))
                    if sps else None)
            if info:
                kandidat = {"sirka": info["sirka"], "vyska": info["vyska"],
                            "profile_idc": info["profile_idc"], "profil": info["profil"],
                            "zdroj": "vzorový súbor"}
        polozky.append({"popis": e["popis"], "blob": e["blob"], "kandidat": kandidat})
    # Parametre H.265 sa poskladať naslepo nedajú (profile_tier_level má príliš
    # veľa kombinácií), preto sa pri ňom skúšajú len hotové hlavičky.
    grid = [] if hevc else candidate_grid(resolutions, quick=quick)[:max_tries]
    polozky += [{"popis": None, "blob": None, "kandidat": c} for c in grid]
    if log:
        log(f"  skúšam až {len(polozky)} možností parametrov "
            f"{'H.265' if hevc else 'H.264'} "
            f"(vzorka {len(sample) // (1 << 20)} MiB)…")
    best = None
    for i, item in enumerate(polozky, 1):
        cand = item["kandidat"] or {}
        blob = item["blob"] if item.get("blob") else blob_for(cand)
        res = score_headers(toolbox, blob, sample, workdir, hevc=hevc)
        if best is None or res["skore"] > best["vysledok"]["skore"]:
            best = {"kandidat": cand, "vysledok": res, "hlavicka": blob}
            if log and res["skore"] > 0:
                nazov = item["popis"] or (
                    f"{cand.get('sirka')}x{cand.get('vyska')} "
                    f"profil={cand.get('profile_idc')} "
                    f"{'CABAC' if cand.get('cabac') else 'CAVLC'}")
                log(f"  [{i}/{len(polozky)}] {nazov} → {res['snimky']} snímkov, "
                    f"kvalita {res['kvalita'] * 100:.0f} %")
        if res["kvalita"] >= good_quality and res["snimky"] >= good_frames:
            if log:
                nazov = item["popis"] or (
                    f"{cand.get('sirka')}x{cand.get('vyska')}, profil "
                    f"{cand.get('profile_idc')}, "
                    f"{'CABAC' if cand.get('cabac') else 'CAVLC'}")
                log(f"  NAŠIEL SOM parametre: {nazov} — {res['snimky']} snímkov, "
                    f"kvalita {res['kvalita'] * 100:.0f} %")
            break
        if log and i % 100 == 0:
            log(f"  … vyskúšaných {i} z {len(polozky)} možností")
    return best
