"""Diagnostika poskodeneho suboru - co prezilo a ako sa to da zachranit."""
from __future__ import annotations

import os

from . import carve, mp4
from .util import hexdump, human


def profil_sifrovania(mm, size: int, vzoriek: int = 120) -> dict:
    """Zmapuje, ktoré časti súboru sú zašifrované a ktoré pôvodné.

    Vzorkuje sa celý súbor, nielen jeho začiatok — až tak sa dá povedať, či
    ransomvér prepísal len hlavičku, alebo celý obsah. Pri malých súboroch
    (rádovo desiatky MB) býva zašifrované všetko, pri veľkých len začiatok,
    lebo šifrovať celý viacgigabajtový súbor by trvalo príliš dlho.
    """
    blok = 262144
    n = max(1, size // blok)
    krok = max(1, n // max(1, vzoriek))
    body = []
    for i in range(0, n, krok):
        h = carve.chi2_rovnomernosti(mm, i * blok, blok)
        body.append({"offset": i * blok, "chi2": round(h, 1),
                     "sifrovane": h < carve.PRAH_CHI2})
    if not body:
        return {"body": [], "podiel": 0.0, "cely_subor": False, "prve_povodne": None}
    sifrovanych = sum(1 for b in body if b["sifrovane"])
    podiel = sifrovanych / len(body)
    prve = next((b["offset"] for b in body if not b["sifrovane"]), None)
    return {"body": body, "podiel": round(podiel, 4),
            "cely_subor": podiel > 0.90, "prve_povodne": prve,
            "blok": blok}


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
        rep["mapa_sifrovania"] = profil_sifrovania(mm, size)
    finally:
        mm.close()
        f.close()

    rep["strategie"] = navrhni_strategie(rep)
    return rep


def navrhni_strategie(rep: dict) -> list:
    """Zostavi zoznam odporucanych postupov od najlepsieho po zaloznu moznost."""
    out = []
    d = rep.get("detail") or {}
    typ = d.get("typ")

    # Zašifrovaný celý súbor nemá zmysel opravovať — nie je v ňom čo zachrániť.
    # Radšej to povedať rovno, než nechať používateľa čakať na neúspech.
    mapa = rep.get("mapa_sifrovania") or {}
    if mapa.get("cely_subor") or mapa.get("podiel", 0) > 0.55:
        return [{
            "id": "nezachranitelne",
            "nazov": "Tento súbor sa zachrániť nedá",
            "vhodnost": "žiadna",
            "popis": (f"Prepísaných je {mapa.get('podiel', 0) * 100:.0f} % obsahu "
                      "súboru, nielen jeho začiatok. Ransomvér tu použil prekladané "
                      "šifrovanie — šifruje po blokoch a medzi nimi necháva kusy "
                      "pôvodných dát. Tie sú však také rozdrobené a je ich tak málo, "
                      "že sa z nich súvislé video poskladať nedá; navyše je prepísaný "
                      "aj index na konci súboru. Zachrániť sa dajú videá, kde ostal "
                      "neporušený index a väčšina dát — pri veľkých súboroch, kde by "
                      "šifrovanie celého obsahu trvalo príliš dlho."),
            "ocakavany_vysledok": ("Bez dešifrovacieho kľúča nič. Skús priponu súboru "
                                   "vyhľadať na nomoreransom.org — pre niektoré rodiny "
                                   "ransomvéru existuje bezplatný dešifrovač."),
        }]

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


def rychly_verdikt(cesta: str, vzoriek: int = 12) -> dict:
    """Rýchlo posúdi jeden súbor: dá sa zachrániť, alebo je zašifrovaný celý?

    Neprechádza celý súbor — vzorkuje rovnomerne rozložené bloky a pozrie sa,
    či na konci prežil index. Na jeden súbor to trvá desatiny sekundy, takže
    sa dá takto pretriediť aj priečinok so stovkami videí.
    """
    vysledok = {"subor": os.path.basename(cesta), "cesta": os.path.abspath(cesta)}
    try:
        size = os.path.getsize(cesta)
    except OSError as exc:
        return {**vysledok, "verdikt": "chyba", "poznamka": str(exc)}
    vysledok["velkost"] = size
    vysledok["velkost_citatelne"] = human(size)
    if size < (64 << 10):
        return {**vysledok, "verdikt": "chyba", "poznamka": "príliš malý súbor"}
    try:
        f, mm, _ = mp4.open_mm(cesta)
    except (OSError, ValueError) as exc:
        return {**vysledok, "verdikt": "chyba", "poznamka": str(exc)}
    try:
        blok = 262144
        n = max(1, size // blok)
        krok = max(1, n // max(1, vzoriek))
        hodnoty = [carve.chi2_rovnomernosti(mm, i * blok, blok)
                   for i in range(0, n, krok)][:vzoriek + 2]
        sifrovanych = sum(1 for h in hodnoty if h < carve.PRAH_CHI2)
        podiel = sifrovanych / max(1, len(hodnoty))
        vysledok["podiel_sifrovania"] = round(podiel, 3)
        if podiel > 0.985:
            return {**vysledok, "verdikt": "nezachranitelne", "postup": None,
                    "poznamka": "zašifrovaný celý obsah"}
        if podiel > 0.55:
            # Prekladané šifrovanie: medzi zašifrovanými blokmi ostávajú kusy
            # pôvodných dát, ale je ich tak málo a sú tak rozdrobené, že sa
            # z nich súvislé video poskladať nedá.
            return {**vysledok, "verdikt": "nezachranitelne", "postup": None,
                    "poznamka": f"prekladané šifrovanie — prepísaných "
                                f"{podiel * 100:.0f} % obsahu"}
        moov = mp4.najdi_moov(mm, size)
        if moov is not None:
            return {**vysledok, "verdikt": "dobre", "postup": "mp4_graft",
                    "poznamka": "index prežil — obraz aj zvuk"}
        sync = carve.find_ts_sync(mm, size, search_limit=8 << 20)
        if sync:
            return {**vysledok, "verdikt": "dobre", "postup": "ts_resync",
                    "poznamka": "tok MPEG-TS"}
        return {**vysledok, "verdikt": "ciastocne", "postup": "mp4_carve",
                "poznamka": "index zničený — dá sa vyrezať obraz"}
    finally:
        mm.close()
        f.close()


def prehlad_priecinka(subory: list, log=None) -> dict:
    """Pretriedi zoznam súborov na zachrániteľné a nezachrániteľné."""
    polozky = []
    for i, cesta in enumerate(subory, 1):
        polozky.append(rychly_verdikt(cesta))
        if log and (i % 25 == 0 or i == len(subory)):
            log(f"  preverených {i} z {len(subory)}…")
    poradie = {"dobre": 0, "ciastocne": 1, "nezachranitelne": 2, "chyba": 3}
    polozky.sort(key=lambda p: (poradie.get(p.get("verdikt"), 9), -p.get("velkost", 0)))
    pocty = {}
    for p in polozky:
        pocty[p.get("verdikt")] = pocty.get(p.get("verdikt"), 0) + 1
    return {"polozky": polozky, "pocty": pocty, "spolu": len(polozky)}


def rozbor(path: str, db, toolbox=None, log=print) -> dict:
    """Podrobná diagnostika súboru na zistenie, čo sa v ňom vlastne deje.

    Vypíše, ktoré časti súboru vyzerajú zašifrované, čo zo štruktúry prežilo
    a čo sa dá nájsť z obrazových dát. Slúži na to, aby sa pri neúspešnej
    oprave dalo povedať, či je súbor ešte zachrániteľný.
    """
    from . import carve as _carve

    size = os.path.getsize(path)
    log(f"Súbor:    {os.path.basename(path)}")
    log(f"Veľkosť:  {human(size)} ({size} B)")

    f, mm, _ = mp4.open_mm(path)
    try:
        with open(path, "rb") as fh:
            log("\nPrvých 64 bajtov:")
            log(hexdump(fh.read(64), 0, 64))

        # 1a) husta mapa - kazdy blok, nie len vzorka
        blok_m = 262144
        n_blokov = size // blok_m
        if n_blokov <= 8192:
            log(f"\nPodrobná mapa ({n_blokov} blokov po 256 KiB):")
            log("  # = zašifrované,  . = pôvodné dáta\n")
            znaky = []
            neporusene = []
            zaciatok = None
            for i in range(n_blokov):
                sif = _carve.chi2_rovnomernosti(mm, i * blok_m, blok_m) < _carve.PRAH_CHI2
                znaky.append("#" if sif else ".")
                if not sif and zaciatok is None:
                    zaciatok = i * blok_m
                elif sif and zaciatok is not None:
                    neporusene.append((zaciatok, i * blok_m))
                    zaciatok = None
            if zaciatok is not None:
                neporusene.append((zaciatok, n_blokov * blok_m))
            for r in range(0, len(znaky), 64):
                log(f"  {r * blok_m // (1 << 20):>5} MiB  " + "".join(znaky[r:r + 64]))
            celkom = sum(b - a for a, b in neporusene)
            log(f"\n  Neporušených dát: {human(celkom)} "
                f"({celkom / max(1, size) * 100:.1f} %) v {len(neporusene)} súvislých úsekoch")
            for a, b in neporusene[:14]:
                log(f"    {human(a):>10} – {human(b):>10}   ({human(b - a)})")
            if len(neporusene) > 14:
                log(f"    … a ďalších {len(neporusene) - 14}")

        # 1) kde je subor zasifrovany - chi-kvadrat po 256 KiB
        blok = 262144
        log(f"\nRovnomernosť dát (chí-kvadrát na {blok // 1024} KiB blokoch)")
        log("  zašifrované ≈ 230-275, video 400 a viac\n")
        body = []
        for i in range(min(24, max(1, size // blok))):
            body.append((i * blok, _carve.chi2_rovnomernosti(mm, i * blok, blok)))
        krok = max(1, (size // blok) // 16)
        for i in range(24, max(1, size // blok), krok):
            body.append((i * blok, _carve.chi2_rovnomernosti(mm, i * blok, blok)))
        sifrovanych = 0
        for off, h in body:
            stav = "zašifrované" if h < _carve.PRAH_CHI2 else "pôvodné dáta"
            if h < _carve.PRAH_CHI2:
                sifrovanych += 1
            log(f"  {off:>13} ({human(off):>10})  chí² {h:>10.0f}   {stav}")
        podiel = sifrovanych / max(1, len(body))
        log(f"\n  Zašifrovaných vzoriek: {sifrovanych} z {len(body)} "
            f"({podiel * 100:.0f} %)")

        # 2) co prezilo zo struktury
        log("\nŠtruktúra súboru:")
        st = mp4.locate_intact_structure(mm, size)
        if st:
            log(f"  neporušená štruktúra od offsetu {st['boundary']} "
                f"({human(st['boundary'])})")
            for b in st["boxes"][:12]:
                log(f"    box {b.type_str()}  offset {b.offset}  {human(b.size)}")
        else:
            log("  žiadna neporušená štruktúra boxov MP4")
        moov = mp4.najdi_moov(mm, size)
        log(f"  index moov: {'nájdený na ' + str(moov.offset) if moov else 'nenájdený'}")
        ts = _carve.find_ts_sync(mm, size)
        log(f"  MPEG-TS pakety: {ts if ts else 'nenájdené'}")

        # 3) obrazove data
        log("\nHľadanie obrazových dát:")
        for hevc in (False, True):
            r = _carve.find_nal_stream(mm, size, hint=0, hevc=hevc)
            if r:
                s2 = r["statistika"]
                log(f"  {'H.265' if hevc else 'H.264'}: offset {r['offset']} "
                    f"({human(r['offset'])}), {s2['nals']} NAL jednotiek, "
                    f"pokrytie {s2['coverage'] * 100:.1f} %, medzier {s2['gaps']}")
            else:
                log(f"  {'H.265' if hevc else 'H.264'}: nič")
    finally:
        mm.close()
        f.close()

    # 4) vzory v okoli
    log("\nVzorové súbory v okolí:")
    priecinok = os.path.dirname(os.path.abspath(path))
    najdene = 0
    try:
        with os.scandir(priecinok) as it:
            subory = [e.path for e in it if e.is_file()]
    except OSError:
        subory = []
    for c in sorted(subory)[:400]:
        if os.path.abspath(c) == os.path.abspath(path):
            continue
        try:
            ps = mp4.codec_parameter_sets(c)
        except (OSError, ValueError):
            continue
        if not ps:
            continue
        sps = _carve.find_sps_in_annexb(ps)
        info = _carve.plausible_sps(sps) if sps else None
        if not info:
            sps = _carve.find_nal_in_annexb(ps, 33, hevc=True)
            info = _carve.plausible_hevc_sps(sps) if sps else None
            kodek = "H.265"
        else:
            kodek = "H.264"
        if info:
            najdene += 1
            log(f"  {os.path.basename(c)[:40]:42s} {kodek} "
                f"{info['sirka']}×{info['vyska']} {info['profil']}")
        if najdene >= 12:
            log("  … (ďalšie neuvádzam)")
            break
    if not najdene:
        log("  žiadne použiteľné parametre v okolitých súboroch")

    log("\nZÁVER:")
    if podiel > 0.9:
        log("  Súbor vyzerá byť zašifrovaný celý alebo takmer celý.")
        log("  V takom prípade sa dáta bez dešifrovacieho kľúča obnoviť nedajú.")
    elif moov:
        log("  Index moov prežil — použi postup „Nahradenie hlavičky“.")
    else:
        log("  Index moov neprežil, ale časť dát je pôvodná — skús vyrezanie obrazu.")
    return {"zasifrovanych_vzoriek": sifrovanych, "vzoriek": len(body)}
