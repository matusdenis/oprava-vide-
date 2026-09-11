#!/usr/bin/env python3
"""Oprava videa poskodeneho ransomverom.

Spustenie s grafickym rozhranim (otvori sa v prehliadaci):
    python3 vidfix.py

Prikazovy riadok:
    python3 vidfix.py analyza  POSKODENY_SUBOR
    python3 vidfix.py oprav    POSKODENY_SUBOR [-o PRIECINOK] [-s STRATEGIA]
    python3 vidfix.py nastroje
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vidfix import __version__                      # noqa: E402
from vidfix.analyze import (analyze, prehlad_priecinka,      # noqa: E402
                            rozbor, skontroluj_vysledok)
from vidfix.headerdb import HeaderDB                # noqa: E402
from vidfix.repair import (Ctx, STRATEGIE, najdi_medzikroky,  # noqa: E402
                           najdi_videa, over_vystup, spusti,
                           spusti_davku)
from vidfix.tools import Toolbox                    # noqa: E402
from vidfix.util import human, najdi_cestu          # noqa: E402


def _over_cestu(cesta: str, priecinok: bool = False) -> str | None:
    """Overi cestu a pri chybe poradi, namiesto vypisania chyboveho zasobnika."""
    najdena = najdi_cestu(cesta)
    if najdena and (os.path.isdir(najdena) if priecinok else os.path.isfile(najdena)):
        if os.path.abspath(najdena) != os.path.abspath(cesta):
            print(f"(cesta nájdená v inom zápise diakritiky: {najdena})")
        return najdena
    co = "Priecinok" if priecinok else "Subor"
    print(f"{co} sa nenasiel:\n  {cesta}\n", file=sys.stderr)
    rodic = najdi_cestu(os.path.dirname(cesta.rstrip(os.sep))) if os.sep in cesta else None
    if rodic and os.path.isdir(rodic):
        try:
            polozky = sorted(os.listdir(rodic))[:15]
            print(f"V priecinku {rodic} je:", file=sys.stderr)
            for p in polozky:
                print(f"  {p}", file=sys.stderr)
            if len(polozky) == 15:
                print("  …", file=sys.stderr)
        except OSError:
            pass
    else:
        print("Nedostupny je uz aj nadradeny priecinok — je disk pripojeny?",
              file=sys.stderr)
        print("Pripojene disky:", file=sys.stderr)
        try:
            for d in sorted(os.listdir("/Volumes")):
                print(f"  /Volumes/{d}", file=sys.stderr)
        except OSError:
            pass
    print("\nTIP: napis prikaz aj s medzerou na konci a potom pretiahni subor "
          "z Findera\n     priamo do okna terminalu — cesta sa vlozi presne.",
          file=sys.stderr)
    return None


def prikaz_analyza(args) -> int:
    subor = _over_cestu(args.subor)
    if not subor:
        return 2
    args.subor = subor
    db, tb = HeaderDB(), Toolbox()
    rep = analyze(args.subor, db, tb, log=lambda m: print(m, file=sys.stderr))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    d = rep.get("detail") or {}
    print(f"Súbor:     {rep['subor']}  ({rep['velkost_citatelne']})")
    kand = rep.get("kandidati_kontajnera") or []
    print(f"Formát:    {kand[0]['nazov'] if kand else 'neurčený'}")
    odhad = (rep.get("odhad_sifrovanej_casti") or {}).get("koniec")
    print(f"Šifrovaný začiatok: {odhad if odhad is not None else 'neurčený'} B")
    print(f"Stav:      {d.get('stav', '?')}")
    for s in d.get("stopy", []):
        print(f"  stopa {s['typ']:5s} {s['kodek']:6s} "
              f"{(str(s['sirka']) + 'x' + str(s['vyska'])) if s['sirka'] else '':>10s} "
              f"{s['trvanie_s']:>8.2f} s   stratené úseky: {s['stratene_chunky']}/{s['pocet_chunkov']}")
    print("\nOdporúčané postupy:")
    for s in rep.get("strategie", []):
        print(f"  [{s['id']:14s}] {s['nazov']}  ({s['vhodnost']})")
        print(f"                   {s['popis']}")
    return 0


def prikaz_oprav(args) -> int:
    subor = _over_cestu(args.subor)
    if not subor:
        return 2
    args.subor = subor
    db, tb = HeaderDB(), Toolbox()
    strategia = args.strategia
    if not strategia:
        rep = analyze(args.subor, db, tb)
        strategie = rep.get("strategie") or []
        if not strategie:
            print("Pre tento súbor sa nenašiel žiadny vhodný postup.", file=sys.stderr)
            return 2
        strategia = strategie[0]["id"]
        print(f"Zvolený postup: {strategia}")
    vystup = args.vystup or os.path.join(os.path.dirname(os.path.abspath(args.subor)), "opravene")
    volby = {"sirka": args.sirka, "vyska": args.vyska, "fps": args.fps,
             "vzor": args.vzor, "verzie": args.verzie.replace("-", "_"),
             "rychle_hladanie": not args.dokladne}
    try:
        over_vystup(vystup)
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    ctx = Ctx(args.subor, vystup, tb, db, volby, log=lambda m: print(m, flush=True))
    vysledok = spusti(strategia, ctx)
    print("\n" + "=" * 66)
    print(vysledok.get("zhrnutie", ""))
    for v in vysledok.get("vystupy", []):
        print(f"  -> {v['cesta']}\n     {v['popis']}")
    k = vysledok.get("kontrola") or {}
    if k.get("ok"):
        print("Kontrola prehrateľnosti: v poriadku, výsledok sa dekóduje bez chýb.")
    else:
        print(f"Kontrola prehrateľnosti: dekodér hlási {k.get('error_count', '?')} chýb "
              f"(časť dát bola naozaj zničená a zachrániť sa nedá).")
    return 0 if vysledok.get("ok") else 1


def prikaz_davka(args) -> int:
    priecinok = _over_cestu(args.priecinok, priecinok=True)
    if not priecinok:
        return 2
    args.priecinok = priecinok
    db, tb = HeaderDB(), Toolbox()
    subory = najdi_videa(priecinok)
    if not subory:
        print("V priecinku sa nenasli ziadne videa.", file=sys.stderr)
        return 2
    print(f"Najdenych {len(subory)} videi.")
    vystup = args.vystup or os.path.join(os.path.abspath(args.priecinok), "opravene")
    volby = {"sirka": args.sirka, "vyska": args.vyska, "fps": args.fps,
             "vzor": args.vzor, "verzie": args.verzie.replace("-", "_"),
             "rychle_hladanie": not args.dokladne}
    try:
        v = spusti_davku(subory, vystup, tb, db, volby,
                         log=lambda m: print(m, flush=True),
                         strategia=args.strategia)
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print("\n" + "=" * 66)
    for r in v["vysledky"]:
        znak = "OK   " if r["ok"] else "CHYBA"
        print(f"  {znak} {r['subor']:36s} {r.get('strategia', '') or r.get('chyba', '')}")
    print(v["zhrnutie"])
    return 0 if v["ok"] else 1


def prikaz_prehlad(args) -> int:
    priecinok = _over_cestu(args.priecinok, priecinok=True)
    if not priecinok:
        return 2
    subory = najdi_videa(priecinok, rekurzivne=not args.bez_podpriecinkov)
    if not subory:
        print("V priecinku sa nenasli ziadne videa.", file=sys.stderr)
        return 2
    print(f"Preveruje sa {len(subory)} suborov"
          f"{'' if args.bez_podpriecinkov else ' (vratane podpriecinkov)'}…")
    v = prehlad_priecinka(subory, log=lambda m: print(m, flush=True))
    znaky = {"dobre": "OK   ", "ciastocne": "CAST ", "nezachranitelne": "NIE  ",
             "chyba": "?    "}
    print()
    for p in v["polozky"]:
        rel = os.path.relpath(p.get("cesta", p["subor"]), priecinok)
        print(f"  {znaky.get(p.get('verdikt'), '?    ')} "
              f"{rel[-58:]:60s} {p.get('velkost_citatelne', ''):>10s}  "
              f"{p.get('poznamka', '')}")
    print()
    print(f"Zachranitelnych uplne:   {v['pocty'].get('dobre', 0)}")
    print(f"Zachranitelnych ciastocne: {v['pocty'].get('ciastocne', 0)}")
    print(f"Nezachranitelnych:       {v['pocty'].get('nezachranitelne', 0)}")
    return 0


def prikaz_rozbor(args) -> int:
    subor = _over_cestu(args.subor)
    if not subor:
        return 2
    rozbor(subor, HeaderDB(), Toolbox(), log=lambda m: print(m, flush=True))
    return 0


def prikaz_kontrola(args) -> int:
    """Zmeria hotové video: beží plynule, alebo v ňom chýbajú snímky?"""
    tb = Toolbox()
    if not tb.path("ffmpeg"):
        print("Na kontrolu je potrebný ffmpeg.", file=sys.stderr)
        return 2
    zle = 0
    for zadane in args.subor:
        cesta = _over_cestu(zadane)
        if not cesta:
            zle += 1
            continue
        print("\n" + "=" * 66)
        print(os.path.basename(cesta))
        v = skontroluj_vysledok(cesta, tb)
        if not v.get("ok"):
            print(f"  CHYBA: {v.get('chyba')}")
            zle += 1
            continue
        if v.get("stopa"):
            print(f"  {v['stopa'].strip()}")
        print(f"  snímkov: {v['snimky']}   trvanie: {v['trvanie']} s   "
              f"frekvencia: {v['fps']}/s")
        print(f"  chyby dekódovania: {v['chyby_dekodovania']}")
        if v["plynule"]:
            print("  PLYNULÉ — rozostupy medzi snímkami sú všade rovnaké.")
            if v.get("koniec_mimo_poradia"):
                print("  (posledný snímok je mimo poradia — záznam je useknutý "
                      "uprostred skupiny snímkov; v prehrávači to vidieť nie je)")
        else:
            print(f"  TRHÁ SA: {v['trhnutia']} nepravidelných rozostupov, "
                  f"chýbajúcich snímkov približne {v['chybajuce_snimky']}")
            for t in v["kde_trha"][:10]:
                print(f"    snímok {t['snimok']} (sekunda {t['sekunda']}): "
                      f"rozostup {t['rozostup']} namiesto {v['bezny_rozostup']}")
            zle += 1
        for u in v.get("ukazky_chyb", [])[:3]:
            print(f"    {u.strip()}")
    return 1 if zle else 0


def prikaz_uprac(args) -> int:
    priecinok = _over_cestu(args.priecinok, priecinok=True)
    if not priecinok:
        return 2
    najdene = najdi_medzikroky(priecinok)
    if not najdene:
        print("Ziadne medzisubory na zmazanie. Vsetko je uz upratane.")
        return 0
    celkom = sum(v for _c, v in najdene)
    for cesta, velkost in najdene:
        print(f"  {human(velkost):>10}  {os.path.relpath(cesta, priecinok)}")
    print(f"\nSpolu {len(najdene)} suborov, {human(celkom)}.")
    if not args.naozaj:
        print("\nToto je len vypis — nic sa nezmazalo.")
        print("Na skutocne zmazanie pridaj na koniec prikazu:  --naozaj")
        return 0
    zmazane = 0
    usetrene = 0
    for cesta, velkost in najdene:
        try:
            os.remove(cesta)
            zmazane += 1
            usetrene += velkost
        except OSError as exc:
            print(f"  nepodarilo sa zmazat {cesta}: {exc}", file=sys.stderr)
    print(f"\nZmazanych {zmazane} suborov, uvolnenych {human(usetrene)}.")
    return 0


def prikaz_nastroje(args) -> int:
    tb = Toolbox()
    for meno, info in tb.status().items():
        print(f"{meno:9s} {'OK ' if info['available'] else '-- '} "
              f"{info['path'] or 'nenájdený'}")
        if info["version"]:
            print(f"          {info['version']}")
    db = HeaderDB()
    s = db.summary()
    print(f"\nDatabáza hlavičiek: verzia {s['verzia']}, {s['pocet_hlaviciek']} hlavičiek "
          f"({s['pocet_vlastnych']} vlastných), {s['pocet_kontajnerov']} kontajnerov")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="vidfix", description="Oprava videa poškodeného ransomvérom.")
    p.add_argument("--verzia", action="version", version=f"vidfix {__version__}")
    pod = p.add_subparsers(dest="prikaz")

    ps = pod.add_parser("server", help="spustí grafické rozhranie (predvolené)")
    ps.add_argument("--port", type=int, default=8765)
    ps.add_argument("--bez-prehliadaca", action="store_true")

    pa = pod.add_parser("analyza", help="diagnostika poškodeného súboru")
    pa.add_argument("subor")
    pa.add_argument("--json", action="store_true")

    po = pod.add_parser("oprav", help="oprava súboru")
    po.add_argument("subor")
    po.add_argument("-o", "--vystup", help="priečinok pre výsledky")
    po.add_argument("-s", "--strategia", choices=sorted(STRATEGIE),
                    help="postup (bez neho sa zvolí najvhodnejší)")
    po.add_argument("--sirka", type=int, help="rozlíšenie pôvodného videa")
    po.add_argument("--vyska", type=int)
    po.add_argument("--fps", type=int, default=None,
                    help="snímková frekvencia; bez nej sa zistí z indexu videa")
    po.add_argument("--vzor", help="zdravý súbor z rovnakého zariadenia")
    po.add_argument("--verzie", default="obidve",
                    choices=["obidve", "len-orezany", "len-opraveny"],
                    help="ktoré verzie uložiť (predvolene obidve)")
    po.add_argument("--dokladne", action="store_true",
                    help="dôkladnejšie (a pomalšie) hľadanie parametrov")

    pd = pod.add_parser("davka", help="oprava vsetkych videi v priecinku")
    pd.add_argument("priecinok")
    pd.add_argument("-o", "--vystup", help="priečinok pre výsledky")
    pd.add_argument("-s", "--strategia", choices=sorted(STRATEGIE),
                    help="vnútiť jeden postup (inak sa volí pre každý súbor zvlášť)")
    pd.add_argument("--sirka", type=int)
    pd.add_argument("--vyska", type=int)
    pd.add_argument("--fps", type=int, default=None,
                    help="snímková frekvencia; bez nej sa zistí z indexu videa")
    pd.add_argument("--vzor", help="iné video z tej istej kamery (aj poškodené)")
    pd.add_argument("--verzie", default="obidve",
                    choices=["obidve", "len-orezany", "len-opraveny"])
    pd.add_argument("--podpriecinky", action="store_true",
                    help="opravit aj videa v podpriecinkoch")
    pd.add_argument("--dokladne", action="store_true")

    pp = pod.add_parser("prehlad", help="pretriedi priecinok na zachranitelne a nie")
    pp.add_argument("priecinok")
    pp.add_argument("--bez-podpriecinkov", action="store_true",
                    help="nehladat v podpriecinkoch")

    pr = pod.add_parser("rozbor", help="podrobna diagnostika jedneho suboru")
    pr.add_argument("subor")

    pk = pod.add_parser("kontrola",
                        help="zmeria hotovy vysledok - plynulost a pocet snimkov")
    pk.add_argument("subor", nargs="+")

    pu = pod.add_parser("uprac", help="zmaze medzisubory, ku ktorym uz je hotovy vysledok")
    pu.add_argument("priecinok")
    pu.add_argument("--naozaj", action="store_true",
                    help="skutocne mazat (bez toho sa len vypise, co by sa zmazalo)")

    pod.add_parser("nastroje", help="zobrazí nájdené nástroje a stav databázy")

    args = p.parse_args(argv)
    if args.prikaz == "analyza":
        return prikaz_analyza(args)
    if args.prikaz == "oprav":
        return prikaz_oprav(args)
    if args.prikaz == "davka":
        return prikaz_davka(args)
    if args.prikaz == "prehlad":
        return prikaz_prehlad(args)
    if args.prikaz == "rozbor":
        return prikaz_rozbor(args)
    if args.prikaz == "kontrola":
        return prikaz_kontrola(args)
    if args.prikaz == "uprac":
        return prikaz_uprac(args)
    if args.prikaz == "nastroje":
        return prikaz_nastroje(args)
    from vidfix.server import spusti_server
    spusti_server(port=getattr(args, "port", 8765),
                  otvorit=not getattr(args, "bez_prehliadaca", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
