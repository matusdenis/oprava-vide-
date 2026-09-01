"""Diagnostika poskodeneho suboru - co prezilo a ako sa to da zachranit."""
from __future__ import annotations

import os

from . import carve, mp4
from .util import entropy_profile, hexdump, human


def _mp4_report(mm, size: int, log=None) -> dict:
    """Rozbor suboru typu MP4 / MOV."""
    out = {"typ": "mp4"}
    st = mp4.locate_intact_structure(mm, size)
    if st is None:
        out["struktura"] = None
        out["stav"] = "žiadna neporušená štruktúra boxov"
        return out

    out["hranica_struktury"] = st["boundary"]
    out["zvysne_bajty_na_konci"] = st["trailing"]
    out["boxy"] = [{"typ": b.type_str(), "offset": b.offset, "velkost": b.size,
                    "orezany": b.truncated} for b in st["boxes"]]

    moov = next((b for b in st["boxes"] if b.type == b"moov"), None)
    fragmented = any(b.type == b"moof" for b in st["boxes"])
    out["fragmentovany"] = fragmented

    if moov is None:
        out["index_moov"] = None
        out["stav"] = "index moov neprežil"
        return out

    moov.children = mp4.parse_tree(mm, moov.data_offset, moov.end, size)
    tracks = mp4.parse_tracks(mm, moov, size)
    out["index_moov"] = {"offset": moov.offset, "velkost": moov.size,
                         "pocet_stop": len(tracks)}

    dmg = mp4.detect_damage_end(mm, tracks, st["boundary"], size)
    out["poskodenie"] = dmg
    damage_end = dmg.get("damage_end")

    video = next((t for t in tracks if t.handler == "vide"), None)
    if video is not None and damage_end is not None:
        hevc = video.codec.lower() in ("hvc1", "hev1", "dvh1", "dvhe")
        out["mapa_poskodenia"] = mp4.damage_map(mm, video, st["boundary"], hevc=hevc)

    stopy = []
    for t in tracks:
        idx, cas = (t.first_intact_keyframe(damage_end) if damage_end else (None, None))
        stopy.append({
            "typ": t.handler, "kodek": t.codec, "sirka": t.width, "vyska": t.height,
            "trvanie_s": round(t.duration_sec, 3), "pocet_chunkov": len(t.chunk_offsets),
            "vzorkovacia_frekvencia": t.sample_rate, "kanaly": t.channels,
            "stratene_chunky": t.lost_chunks(damage_end) if damage_end else None,
            "prvy_klucovy_snimok_s": round(cas, 3) if cas is not None else None,
        })
    out["stopy"] = stopy
    out["stav"] = "index moov prežil"
    return out


def _ts_report(mm, size: int) -> dict:
    sync = carve.find_ts_sync(mm, size)
    return {"typ": "mpegts", "synchronizacia": sync,
            "stav": "nájdený platný paket" if sync else "neplatný tok paketov"}


def analyze(path: str, db, toolbox=None, log=None) -> dict:
    """Kompletna diagnostika suboru."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(4096)
        f.seek(max(0, size - 4096))
        tail = f.read(4096)

    rep = {
        "subor": os.path.basename(path),
        "cesta": os.path.abspath(path),
        "velkost": size,
        "velkost_citatelne": human(size),
        "hexdump_zaciatku": hexdump(head, 0, 160),
        "kandidati_kontajnera": db.identify(head, tail, os.path.basename(path)),
    }

    f, mm, _ = mp4.open_mm(path)
    try:
        prvy = rep["kandidati_kontajnera"][0]["id"] if rep["kandidati_kontajnera"] else None
        detail = None
        # MP4 skusame vzdy - podpis na zaciatku byva zasifrovany, takze sa
        # nedá spolahnut len na priponu a magicke bajty
        detail = _mp4_report(mm, size, log=log)
        if detail.get("struktura", "x") is None or detail.get("stav") in (
                "žiadna neporušená štruktúra boxov",):
            ts = _ts_report(mm, size)
            if ts["synchronizacia"]:
                detail = ts
        elif prvy == "mpegts":
            ts = _ts_report(mm, size)
            if ts["synchronizacia"]:
                detail = ts
        rep["detail"] = detail

        # statisticky odhad zasifrovanej casti (funguje aj bez indexu)
        rep["odhad_sifrovanej_casti"] = carve.estimate_encrypted_prefix(mm, size)
    finally:
        mm.close()
        f.close()

    rep["entropia"] = entropy_profile(path)
    rep["strategie"] = navrhni_strategie(rep)
    return rep


def navrhni_strategie(rep: dict) -> list:
    """Zostavi zoznam odporucanych postupov od najlepsieho po zaloznu moznost."""
    out = []
    d = rep.get("detail") or {}
    typ = d.get("typ")

    if typ == "mpegts" and d.get("synchronizacia"):
        out.append({
            "id": "ts_resync", "nazov": "Znovunájdenie tokových paketov (MPEG-TS)",
            "vhodnost": "výborná",
            "popis": ("Formát TS sa synchronizuje sám — nemá žiadny centrálny index. "
                      "Stačí zahodiť zašifrovaný začiatok a čítať od prvého platného "
                      "paketu. Ak pritom padol za obeť aj kľúčový snímok, program "
                      "skopíruje parametre obrazu z neskoršieho miesta v súbore."),
            "ocakavany_vysledok": "Video aj zvuk okrem zašifrovaného začiatku."})

    if typ == "mp4":
        if d.get("index_moov"):
            dmg = d.get("poskodenie") or {}
            ok = dmg.get("damage_end") is not None
            out.append({
                "id": "mp4_graft",
                "nazov": "Nahradenie hlavičky (index moov prežil)",
                "vhodnost": "výborná" if ok else "dobrá",
                "popis": ("Index s tabuľkami vzoriek na konci súboru je neporušený. "
                          "Zašifrovaný začiatok sa nahradí novou hlavičkou z databázy, "
                          "ktorá je PRESNE rovnako dlhá — všetky ostatné dáta tak zostanú "
                          "na pôvodných pozíciách a offsety v indexe naďalej sedia. "
                          "Zdravý vzorový súbor na to nie je potrebný."),
                "ocakavany_vysledok": ("Celé video aj zvuk okrem zašifrovaného začiatku; "
                                       "ten sa dá automaticky orezať.")})
        else:
            out.append({
                "id": "mp4_carve",
                "nazov": "Vyrezanie obrazu a rekonštrukcia parametrov",
                "vhodnost": "stredná",
                "popis": ("Index moov bol uložený na začiatku súboru a ransomvér ho "
                          "prepísal. Z tela súboru sa preto vyrežú samotné obrazové dáta "
                          "a chýbajúce parametre H.264 (rozlíšenie, profil, spôsob "
                          "kódovania) program nájde skúšaním kombinácií z databázy — "
                          "vyhrá tá, pri ktorej sa video naozaj dekóduje."),
                "ocakavany_vysledok": ("Obraz bez zvuku. Zvuk sa bez indexu spoľahlivo "
                                       "priradiť nedá.")})
            out.append({
                "id": "untrunc",
                "nazov": "untrunc so vzorovým súborom",
                "vhodnost": "dobrá, ak je vzor k dispozícii",
                "popis": ("Nástroj untrunc dokáže index dopočítať podľa zdravého súboru "
                          "z ROVNAKÉHO zariadenia. Ak žiadny nemáš, program ho skúsi "
                          "vyrobiť umelo pomocou ffmpeg — výsledok potom závisí od toho, "
                          "ako presne sa trafia pôvodné nastavenia kodeku."),
                "ocakavany_vysledok": "Video aj zvuk, ak vzor sedí s pôvodným nastavením."})

    out.append({
        "id": "ffmpeg_remux", "nazov": "Priamy pokus o prečítanie cez ffmpeg",
        "vhodnost": "rýchly test",
        "popis": ("Najprv sa oplatí skúsiť, či súbor nedokáže prečítať samotný ffmpeg. "
                  "Trvá pár sekúnd a nič nepokazí."),
        "ocakavany_vysledok": "Funguje len pri ľahkom poškodení."})
    return out
