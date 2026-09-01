"""Opravne strategie.

Kazda strategia je funkcia `strategia(ctx) -> dict`. `ctx` obsahuje cestu k
poskodenemu suboru, vystupny priecinok, nastroje, databazu hlaviciek,
pouzivatelske volby a funkciu na zapis do priebehu (`log`).

Ziadna strategia NIKDY nezapisuje do povodneho suboru - vsetko ide do noveho.
"""
from __future__ import annotations

import os
import shutil

from . import carve, h264_params, mp4
from .util import human, safe_name

COPY_CHUNK = 8 << 20


class Ctx:
    def __init__(self, path: str, outdir: str, toolbox, db, options: dict | None = None,
                 log=None):
        self.path = os.path.abspath(path)
        self.outdir = os.path.abspath(outdir)
        self.toolbox = toolbox
        self.db = db
        self.options = options or {}
        self._log = log or (lambda m: None)
        os.makedirs(self.outdir, exist_ok=True)

    def log(self, msg: str):
        self._log(msg)

    def out(self, suffix: str, ext: str | None = None) -> str:
        base, orig_ext = os.path.splitext(os.path.basename(self.path))
        # "video.mp4.locked" -> "video.mp4" -> "video"
        base2, inner = os.path.splitext(base)
        if inner.lower() in (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".mts", ".m2ts", ".ts"):
            base = base2
            orig_ext = inner
        ext = ext or (orig_ext if orig_ext.lower() in (".mp4", ".mov", ".ts") else ".mp4")
        return os.path.join(self.outdir, safe_name(f"{base}_{suffix}{ext}"))


# ---------------------------------------------------------------------------

def _copy_with_prefix(src: str, dst: str, prefix: bytes, zero_until: int = 0,
                      log=None) -> int:
    """Vytvori novy subor: nova hlavicka + povodne data od rovnakeho offsetu.

    Bajty az po `zero_until` (zasifrovany balast) sa vynuluju, aby dekoder
    nezakopol o nahodne data. Vsetko za touto hranicou ostava nedotknute a na
    POVODNOM offsete - to je pre platnost indexu kluceve.
    """
    size = os.path.getsize(src)
    written = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(prefix)
        written = len(prefix)
        if zero_until > written:
            n = zero_until - written
            blank = b"\0" * min(n, COPY_CHUNK)
            while n > 0:
                take = min(n, COPY_CHUNK)
                fo.write(blank[:take])
                n -= take
            written = zero_until
        fi.seek(written)
        while True:
            data = fi.read(COPY_CHUNK)
            if not data:
                break
            fo.write(data)
            written += len(data)
            if log and written % (512 << 20) < COPY_CHUNK:
                log(f"  zapisanych {human(written)} z {human(size)}")
    return written


def strategia_mp4_graft(ctx: Ctx) -> dict:
    """Nahradi zasifrovany zaciatok novou hlavickou (index moov prezil).

    Toto je najlepsi mozny vysledok: video sa obnovi cele okrem tej casti,
    ktoru ransomver naozaj prepisal, a nie je na to potrebny ziadny zdravy vzor.
    """
    ctx.log("=== Nahradenie hlavičky s využitím prežitého indexu moov ===")
    f, mm, size = mp4.open_mm(ctx.path)
    vystupy = []
    try:
        st = mp4.locate_intact_structure(mm, size)
        if st is None:
            raise RuntimeError("V súbore sa nenašla žiadna neporušená štruktúra boxov.")
        moov = next((b for b in st["boxes"] if b.type == b"moov"), None)
        if moov is None:
            raise RuntimeError("Index moov neprežil — použi stratégiu „Vyrezanie obrazu“.")
        moov.children = mp4.parse_tree(mm, moov.data_offset, moov.end, size)
        tracks = mp4.parse_tracks(mm, moov, size)
        ctx.log(f"Index moov nájdený na offsete {moov.offset} ({human(moov.size)}), "
                f"stôp: {len(tracks)}")

        dmg = mp4.detect_damage_end(mm, tracks, st["boundary"], size)
        damage_end = ctx.options.get("hranica_poskodenia") or dmg.get("damage_end")
        if damage_end is None:
            damage_end = 0
            ctx.log("Hranicu poškodenia sa nepodarilo zmerať — zašifrovaný balast "
                    "sa nebude nulovať.")
        else:
            ctx.log(f"Koniec zašifrovanej časti: {damage_end} B ({human(damage_end)}), "
                    f"metóda: {dmg.get('method')}")

        video = next((t for t in tracks if t.handler == "vide"), None)
        header_id = ctx.options.get("hlavicka") or ctx.db.suggest_header(
            container="mov" if ctx.path.lower().endswith(".mov") else "mp4",
            codec=video.codec if video else "",
            fragmented=any(b.type == b"moof" for b in st["boxes"]))
        ftyp = ctx.db.header_bytes(header_id)
        if ftyp is None:
            raise RuntimeError(f"Hlavička „{header_id}“ nie je v databáze.")
        ctx.log(f"Použitá hlavička z databázy: {header_id} ({len(ftyp)} B)")

        media_end = moov.offset
        prefix = mp4.build_graft_header(media_end, ftyp)
        if damage_end and damage_end < len(prefix):
            raise RuntimeError(
                f"Zašifrovaný úsek ({damage_end} B) je kratší ako nová hlavička "
                f"({len(prefix)} B) — súbor nie je poškodený spôsobom, ktorý táto "
                f"stratégia rieši.")

        dst = ctx.out("opraveny")
        ctx.log(f"Zapisujem {dst} …")
        zero_until = damage_end if ctx.options.get("vynulovat_balast", True) else len(prefix)
        written = _copy_with_prefix(ctx.path, dst, prefix, zero_until, log=ctx.log)
        ctx.log(f"Hotovo, zapísaných {human(written)}")
        vystupy.append({"cesta": dst, "popis": "Opravený súbor v plnej dĺžke "
                                               "(začiatok môže byť rušivý)"})

        # kontrola vysledku
        stav = ctx.toolbox.probe(dst)
        if stav.get("_ok"):
            ctx.log("ffprobe/ffmpeg súbor úspešne prečítal.")

        # orezanie poskodeneho zaciatku po prvy klucovy snimok
        if ctx.options.get("orezat", True) and video is not None and damage_end:
            idx, cas = video.first_intact_keyframe(damage_end)
            if cas is not None:
                ctx.log(f"Prvý neporušený kľúčový snímok je v čase {cas:.3f} s "
                        f"— orezávam bez straty kvality.")
                trimmed = ctx.out("orezany")
                # k casu pridavame 2 ms: pri hodnote tesne PRED klucovym snimkom
                # by ffmpeg skocil na predchadzajuci a poskodeny zaciatok by ostal
                res = ctx.toolbox.run("ffmpeg", [
                    "-y", "-v", "error", "-ss", f"{cas + 0.002:.6f}", "-i", dst,
                    "-c", "copy", "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", trimmed], log=ctx.log)
                if res["code"] == 0 and os.path.exists(trimmed):
                    vystupy.append({"cesta": trimmed,
                                    "popis": f"Čistý súbor bez poškodeného začiatku "
                                             f"(začína v čase {cas:.1f} s)"})
                else:
                    ctx.log("Orezanie sa nepodarilo — ostáva aspoň súbor v plnej dĺžke.")
            else:
                ctx.log("Nenašiel sa kľúčový snímok za poškodenou časťou.")

        kontrola = ctx.toolbox.playable(vystupy[-1]["cesta"], seconds=8)
        return {"ok": True, "vystupy": vystupy,
                "zhrnutie": ("Index moov prežil, hlavička bola nahradená. "
                             f"Nenávratne zničený je len začiatok ({human(damage_end)})."),
                "kontrola": kontrola, "hranica_poskodenia": damage_end}
    finally:
        mm.close()
        f.close()


def strategia_ts_resync(ctx: Ctx) -> dict:
    """Zahodi zasifrovany zaciatok a nacita subor od prveho platneho TS paketu."""
    ctx.log("=== Znovunájdenie paketov MPEG-TS ===")
    f, mm, size = mp4.open_mm(ctx.path)
    try:
        sync = carve.find_ts_sync(mm, size)
        if not sync:
            raise RuntimeError("Nenašiel sa platný sled TS paketov.")
        ctx.log(f"Prvý platný paket na offsete {sync['offset']}, "
                f"veľkosť paketu {sync['packet_size']} B")
    finally:
        mm.close()
        f.close()

    raw = ctx.out("zachraneny", ".ts")
    n = carve_written = carve.carve_ts(ctx.path, raw, sync["offset"], log=ctx.log)
    ctx.log(f"Vyrezaných {human(n)}")
    vystupy = [{"cesta": raw, "popis": "Surový tok od prvého platného paketu"}]

    remux = ctx.out("zachraneny", ".mp4")
    res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error", "-fflags", "+genpts+igndts",
                                     "-i", raw, "-c", "copy",
                                     "-movflags", "+faststart", remux], log=ctx.log)
    if res["code"] == 0 and os.path.exists(remux) and os.path.getsize(remux) > 1024:
        vystupy.append({"cesta": remux, "popis": "Prebalené do MP4 (lepšia kompatibilita)"})
    else:
        # Ak zasifrovany zaciatok zobral prvy klucovy snimok, su parametre SPS az
        # o niekolko MB dalej a prehravac dovtedy nema z coho dekodovat. Skusime
        # este raz od miesta, kde sa parametre prvy raz objavia.
        if os.path.exists(remux):
            os.remove(remux)
        ctx.log("Priame prebalenie neuspelo — hľadám miesto s parametrami SPS.")
        f2, mm2, size2 = mp4.open_mm(ctx.path)
        try:
            zac = carve.find_ts_parameters_offset(mm2, size2, sync["offset"],
                                                  sync["packet_size"])
        finally:
            mm2.close()
            f2.close()
        if zac and zac > sync["offset"]:
            ctx.log(f"Parametre SPS sú až od offsetu ~{human(zac)} — odtiaľ prebaľujem.")
            raw2 = ctx.out("zachraneny_od_klucoveho_snimku", ".ts")
            carve.carve_ts(ctx.path, raw2, zac, log=ctx.log)
            vystupy.append({"cesta": raw2,
                            "popis": "Tok od prvého použiteľného kľúčového snímku"})
            res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error",
                                             "-fflags", "+genpts+igndts", "-i", raw2,
                                             "-c", "copy", "-movflags", "+faststart",
                                             remux], log=ctx.log)
            if res["code"] == 0 and os.path.exists(remux) and os.path.getsize(remux) > 1024:
                vystupy.append({"cesta": remux,
                                "popis": "Prebalené do MP4 (lepšia kompatibilita)"})
            elif os.path.exists(remux):
                os.remove(remux)
        if not any(v["cesta"].endswith(".mp4") for v in vystupy):
            # Posledna moznost: rozbalime TS pakety vlastnymi silami, parametre
            # SPS/PPS skopirujeme z neskorsieho klucoveho snimku na zaciatok a
            # vysledok prebalime ako surovy H.264. Nezavisi to na demuxeri ffmpeg,
            # ktory na poskodenych tokoch nezriedka zlyhava.
            ctx.log("Skúšam vlastné rozbalenie TS paketov na surový obrazový stream.")
            f3, mm3, size3 = mp4.open_mm(ctx.path)
            try:
                es = ctx.out("obraz", ".h264")
                info = carve.ts_extract_video(mm3, size3, sync["offset"],
                                              sync["packet_size"], es, log=ctx.log)
            finally:
                mm3.close()
                f3.close()
            if info["bytes"]:
                ps = carve.parameter_sets_anywhere(es)
                if ps:
                    ctx.log(f"Parametre SPS/PPS nájdené v súbore ({len(ps)} B) "
                            f"— kopírujem ich na začiatok streamu.")
                    with open(es, "rb") as fi:
                        telo = fi.read()
                    with open(es, "wb") as fo:
                        fo.write(ps)
                        fo.write(telo)
                vystupy.append({"cesta": es, "popis": "Surový obrazový stream z TS"})
                res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error", "-f", "h264",
                                                 "-i", es, "-c", "copy",
                                                 "-movflags", "+faststart", remux],
                                      log=None)
                if res["code"] == 0 and os.path.exists(remux) and os.path.getsize(remux) > 1024:
                    vystupy.append({"cesta": remux, "popis": "Zachránené video (bez zvuku)"})
                elif os.path.exists(remux):
                    os.remove(remux)
        if not any(v["cesta"].endswith(".mp4") for v in vystupy):
            ctx.log("Prebalenie do MP4 sa nepodarilo. Výsledný .ts súbor sa dá prehrať "
                    "napríklad vo VLC, ktorý si začiatok toku nájde sám.")
    return {"ok": True, "vystupy": vystupy,
            "zhrnutie": f"Zahodených prvých {human(sync['offset'])}, zvyšok zachránený.",
            "kontrola": ctx.toolbox.playable(vystupy[-1]["cesta"], seconds=8)}


def strategia_mp4_carve(ctx: Ctx) -> dict:
    """Vyreze obrazovy stream a chybajuce parametre najde skusanim z databazy."""
    ctx.log("=== Vyrezanie obrazu a rekonštrukcia parametrov ===")
    f, mm, size = mp4.open_mm(ctx.path)
    vystupy = []
    try:
        est = carve.estimate_encrypted_prefix(mm, size, log=ctx.log)
        hint = ctx.options.get("hranica_poskodenia")
        if hint is None:
            hint = est.get("koniec") or 0
        hevc = bool(ctx.options.get("hevc"))
        ctx.log(f"Hľadám začiatok obrazového streamu od offsetu {hint} …")
        found = carve.find_nal_stream(mm, size, hint=hint, hevc=hevc, log=ctx.log)
        if not found and hint:
            ctx.log("Skúšam hľadať od začiatku súboru …")
            found = carve.find_nal_stream(mm, size, hint=0, hevc=hevc, log=ctx.log)
        if not found:
            raise RuntimeError(
                "V súbore sa nenašiel žiadny súvislý obrazový stream. Súbor je "
                "pravdepodobne zašifrovaný celý, alebo používa iný kodek ako H.264/H.265.")

        raw = ctx.out("vyrezany", ".h264" if not hevc else ".h265")
        params = carve.collect_parameter_sets(mm, found["offset"], size, hevc=hevc)
        if params:
            ctx.log(f"Parametre SPS/PPS sa našli priamo v tele streamu ({len(params)} B).")
        stat = carve.extract_annexb(mm, found["offset"], size, raw, hevc=hevc,
                                    sps_pps=params, max_len=found["max_len"], log=ctx.log)
        ctx.log(f"Vyrezaných {stat['nals']} NAL jednotiek, {human(stat['bytes'])}, "
                f"preskočených medzier: {stat['gaps']}")
    finally:
        mm.close()
        f.close()

    if not stat["nals"]:
        raise RuntimeError("Nepodarilo sa vyrezať žiadne obrazové dáta.")

    # Hotove parametre, ktore stoja za vyskusanie ako prve: najdene priamo v
    # tele streamu a vytiahnute zo zdraveho suboru z rovnakeho zariadenia.
    with open(raw, "rb") as fi:
        vzorka = fi.read(16 << 20)
    extra = []
    if not hevc:
        sps = carve.find_sps_in_annexb(vzorka)
        info = carve.plausible_sps(sps) if sps else None
        if info:
            ctx.log(f"V tele streamu sú parametre {info['sirka']}×{info['vyska']}, "
                    f"profil {info['profil']} — vyskúšam ich ako prvé.")
            extra.append({"popis": f"parametre z tela streamu "
                                   f"({info['sirka']}x{info['vyska']})",
                          "blob": b"", "kandidat": None})
    vzor = ctx.options.get("vzor")
    if vzor and os.path.exists(vzor):
        try:
            ps = mp4.avcc_parameter_sets(vzor)
            if ps:
                ctx.log(f"Zo vzorového súboru {os.path.basename(vzor)} vytiahnuté "
                        f"parametre ({len(ps)} B) — vyskúšam ich ako prvé.")
                extra.insert(0, {"popis": f"parametre zo vzoru "
                                          f"{os.path.basename(vzor)}",
                                 "blob": ps, "kandidat": None})
        except (OSError, ValueError) as exc:
            ctx.log(f"Vzorový súbor sa nedá prečítať: {exc}")

    hlavicka = b""
    najdene = None
    if not hevc:
        rozlisenia = None
        if ctx.options.get("sirka") and ctx.options.get("vyska"):
            rozlisenia = [(int(ctx.options["sirka"]), int(ctx.options["vyska"]))]
            ctx.log(f"Používam zadané rozlíšenie {rozlisenia[0][0]}×{rozlisenia[0][1]}.")
        najdene = h264_params.find_best_headers(
            ctx.toolbox, raw, ctx.outdir, resolutions=rozlisenia,
            quick=bool(ctx.options.get("rychle_hladanie", True)),
            log=ctx.log, extra=extra)
        if najdene and najdene["vysledok"]["skore"] > 0:
            hlavicka = najdene["hlavicka"]
            parametre_ok = True
        else:
            parametre_ok = False
            ctx.log("Žiadne parametre nezabrali. Skús zadať presné rozlíšenie "
                    "pôvodného videa, alebo dodaj zdravý súbor z rovnakého zariadenia.")
    else:
        parametre_ok = bool(params)

    final_raw = raw
    if hlavicka:
        final_raw = ctx.out("vyrezany_s_hlavickou", ".h264")
        with open(final_raw, "wb") as fo:
            fo.write(hlavicka)
            with open(raw, "rb") as fi:
                shutil.copyfileobj(fi, fo, COPY_CHUNK)
    vystupy.append({"cesta": final_raw, "popis": "Surový obrazový stream"})

    fps = ctx.options.get("fps") or 30
    dst = ctx.out("zachraneny", ".mp4")
    res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error", "-r", str(fps),
                                     "-f", "hevc" if hevc else "h264", "-i", final_raw,
                                     "-c", "copy", "-movflags", "+faststart", dst],
                          log=ctx.log)
    if res["code"] == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024:
        vystupy.append({"cesta": dst, "popis": f"Zachránené video ({fps} snímkov/s)"})
    elif os.path.exists(dst) and os.path.getsize(dst) <= 1024:
        os.remove(dst)
        ctx.log("Prebalenie do MP4 sa nepodarilo — ostáva surový stream, ktorý sa dá "
                "prehrať napr. vo VLC alebo prebaliť ručne.")

    zhrnutie = (f"Vyrezaných {stat['nals']} snímkov. "
                + (f"Parametre: {najdene['kandidat']['sirka']}×"
                   f"{najdene['kandidat']['vyska']}."
                   if (najdene and hlavicka and najdene.get("kandidat"))
                   else ("Parametre prevzaté z hotovej hlavičky." if hlavicka
                         else "Parametre sa nepodarilo určiť."))
                + " Zvuk sa bez indexu zachrániť nedá.")
    return {"ok": bool(vystupy), "vystupy": vystupy, "zhrnutie": zhrnutie,
            "kontrola": ctx.toolbox.playable(vystupy[-1]["cesta"], seconds=8),
            "parametre": najdene["kandidat"] if najdene else None}


def strategia_untrunc(ctx: Ctx) -> dict:
    """Spusti untrunc so vzorovym suborom (dodanym alebo umelo vyrobenym)."""
    ctx.log("=== untrunc so vzorovým súborom ===")
    if not ctx.toolbox.path("untrunc"):
        raise RuntimeError(
            "Nástroj untrunc nie je nainštalovaný. Nastav k nemu cestu v záložke "
            "Nástroje, alebo použi stratégiu „Vyrezanie obrazu“, ktorá untrunc "
            "nepotrebuje.")
    vzor = ctx.options.get("vzor")
    if vzor and os.path.exists(vzor):
        ctx.log(f"Používam dodaný vzorový súbor: {vzor}")
    else:
        vzor = os.path.join(ctx.outdir, "_vzor.mp4")
        sirka = int(ctx.options.get("sirka") or 1920)
        vyska = int(ctx.options.get("vyska") or 1080)
        ctx.log(f"Žiadny zdravý vzor nie je k dispozícii — vyrábam umelý "
                f"({sirka}×{vyska}) pomocou ffmpeg.")
        carve.synthesize_reference(ctx.toolbox, vzor, width=sirka, height=vyska,
                                   fps=int(ctx.options.get("fps") or 30), log=ctx.log)
    res = ctx.toolbox.run("untrunc", [vzor, ctx.path], log=ctx.log)
    cakany = ctx.path + "_fixed.mp4"
    vystupy = []
    if os.path.exists(cakany):
        dst = ctx.out("untrunc")
        shutil.move(cakany, dst)
        vystupy.append({"cesta": dst, "popis": "Výsledok nástroja untrunc"})
    if not vystupy:
        raise RuntimeError("untrunc nevytvoril žiadny výstup:\n" + res["output"][-800:])
    return {"ok": True, "vystupy": vystupy,
            "zhrnutie": "Index bol dopočítaný podľa vzorového súboru.",
            "kontrola": ctx.toolbox.playable(vystupy[0]["cesta"], seconds=8)}


def strategia_ffmpeg_remux(ctx: Ctx) -> dict:
    """Rychly test: dokaze subor precitat priamo ffmpeg?"""
    ctx.log("=== Priamy pokus o prebalenie cez ffmpeg ===")
    dst = ctx.out("prebaleny")
    res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error", "-err_detect", "ignore_err",
                                     "-i", ctx.path, "-c", "copy",
                                     "-movflags", "+faststart", dst], log=ctx.log)
    if res["code"] != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 4096:
        if os.path.exists(dst):
            os.remove(dst)
        return {"ok": False, "vystupy": [],
                "zhrnutie": ("ffmpeg súbor prečítať nedokáže — poškodenie je vážne, "
                             "použi niektorú z ostatných stratégií."),
                "kontrola": {"ok": False}}
    return {"ok": True, "vystupy": [{"cesta": dst, "popis": "Prebalený súbor"}],
            "zhrnutie": "ffmpeg súbor prečítal, poškodenie bolo len ľahké.",
            "kontrola": ctx.toolbox.playable(dst, seconds=8)}


STRATEGIE = {
    "mp4_graft": strategia_mp4_graft,
    "mp4_carve": strategia_mp4_carve,
    "ts_resync": strategia_ts_resync,
    "untrunc": strategia_untrunc,
    "ffmpeg_remux": strategia_ffmpeg_remux,
}


def spusti(strategy_id: str, ctx: Ctx) -> dict:
    fn = STRATEGIE.get(strategy_id)
    if fn is None:
        raise ValueError(f"Neznáma stratégia: {strategy_id}")
    return fn(ctx)
