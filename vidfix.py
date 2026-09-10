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
                            rozbor)
from vidfix.headerdb import HeaderDB                # noqa: E402
from vidfix.repair import (Ctx, STRATEGIE, najdi_videa,      # noqa: E402
                           spusti, spusti_davku)
from vidfix.tools import Toolbox                    # noqa: E402


def prikaz_analyza(args) -> int:
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
             "vzor": args.vzor, "orezat": not args.bez_orezania,
             "rychle_hladanie": not args.dokladne}
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
    db, tb = HeaderDB(), Toolbox()
    if os.path.isdir(args.priecinok):
        subory = najdi_videa(args.priecinok)
    else:
        print(f"Priecinok neexistuje: {args.priecinok}", file=sys.stderr)
        return 2
    if not subory:
        print("V priecinku sa nenasli ziadne videa.", file=sys.stderr)
        return 2
    print(f"Najdenych {len(subory)} videi.")
    vystup = args.vystup or os.path.join(os.path.abspath(args.priecinok), "opravene")
    volby = {"sirka": args.sirka, "vyska": args.vyska, "fps": args.fps,
             "vzor": args.vzor, "orezat": not args.bez_orezania,
             "rychle_hladanie": not args.dokladne}
    v = spusti_davku(subory, vystup, tb, db, volby,
                     log=lambda m: print(m, flush=True), strategia=args.strategia)
    print("\n" + "=" * 66)
    for r in v["vysledky"]:
        znak = "OK   " if r["ok"] else "CHYBA"
        print(f"  {znak} {r['subor']:36s} {r.get('strategia', '') or r.get('chyba', '')}")
    print(v["zhrnutie"])
    return 0 if v["ok"] else 1


def prikaz_prehlad(args) -> int:
    if not os.path.isdir(args.priecinok):
        print(f"Priecinok neexistuje: {args.priecinok}", file=sys.stderr)
        return 2
    subory = najdi_videa(args.priecinok)
    if not subory:
        print("V priecinku sa nenasli ziadne videa.", file=sys.stderr)
        return 2
    print(f"Preveruje sa {len(subory)} suborov…")
    v = prehlad_priecinka(subory, log=lambda m: print(m, flush=True))
    znaky = {"dobre": "OK   ", "ciastocne": "CAST ", "nezachranitelne": "NIE  ",
             "chyba": "?    "}
    print()
    for p in v["polozky"]:
        print(f"  {znaky.get(p.get('verdikt'), '?    ')} "
              f"{p['subor'][:44]:46s} {p.get('velkost_citatelne', ''):>10s}  "
              f"{p.get('poznamka', '')}")
    print()
    print(f"Zachranitelnych uplne:   {v['pocty'].get('dobre', 0)}")
    print(f"Zachranitelnych ciastocne: {v['pocty'].get('ciastocne', 0)}")
    print(f"Nezachranitelnych:       {v['pocty'].get('nezachranitelne', 0)}")
    return 0


def prikaz_rozbor(args) -> int:
    rozbor(args.subor, HeaderDB(), Toolbox(), log=lambda m: print(m, flush=True))
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
    po.add_argument("--fps", type=int, default=30)
    po.add_argument("--vzor", help="zdravý súbor z rovnakého zariadenia")
    po.add_argument("--bez-orezania", action="store_true")
    po.add_argument("--dokladne", action="store_true",
                    help="dôkladnejšie (a pomalšie) hľadanie parametrov")

    pd = pod.add_parser("davka", help="oprava vsetkych videi v priecinku")
    pd.add_argument("priecinok")
    pd.add_argument("-o", "--vystup", help="priečinok pre výsledky")
    pd.add_argument("-s", "--strategia", choices=sorted(STRATEGIE),
                    help="vnútiť jeden postup (inak sa volí pre každý súbor zvlášť)")
    pd.add_argument("--sirka", type=int)
    pd.add_argument("--vyska", type=int)
    pd.add_argument("--fps", type=int, default=30)
    pd.add_argument("--vzor", help="iné video z tej istej kamery (aj poškodené)")
    pd.add_argument("--bez-orezania", action="store_true")
    pd.add_argument("--dokladne", action="store_true")

    pp = pod.add_parser("prehlad", help="pretriedi priecinok na zachranitelne a nie")
    pp.add_argument("priecinok")

    pr = pod.add_parser("rozbor", help="podrobna diagnostika jedneho suboru")
    pr.add_argument("subor")

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
    if args.prikaz == "nastroje":
        return prikaz_nastroje(args)
    from vidfix.server import spusti_server
    spusti_server(port=getattr(args, "port", 8765),
                  otvorit=not getattr(args, "bez_prehliadaca", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
