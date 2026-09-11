"""Opravne strategie.

Kazda strategia je funkcia `strategia(ctx) -> dict`. `ctx` obsahuje cestu k
poskodenemu suboru, vystupny priecinok, nastroje, databazu hlaviciek,
pouzivatelske volby a funkciu na zapis do priebehu (`log`).

Ziadna strategia NIKDY nezapisuje do povodneho suboru - vsetko ide do noveho.
"""
from __future__ import annotations

import errno
import os
import shutil

from . import carve, h264_params, mp4
from .analyze import analyze
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


# Pripony, podla ktorych sa hlada video. Hlada sa kdekolvek v nazve, aby
# presli aj subory premenovane ransomverom (napr. "dovolenka.MP4.locked").
VIDEO_PRIPONY = (".mp4", ".mov", ".m4v", ".3gp", ".mts", ".m2ts", ".ts", ".m2t")


# Pripony, ktore videom urcite nie su. Velke subory sa inak beru ako mozne
# video (ransomver pripony menuje), ale dokument ci archiv nema zmysel skusat.
NIE_VIDEO_PRIPONY = (
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".dmg", ".iso", ".pkg",
    ".psd", ".ai", ".indd", ".prproj", ".aep", ".fcpbundle", ".pptx", ".docx",
    ".xlsx", ".numbers", ".pages", ".key", ".sketch", ".blend", ".exe", ".app",
    ".wav", ".aiff", ".mp3", ".flac", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raw", ".heic",
)


def _mozne_video(cesta: str) -> bool:
    nazov = os.path.basename(cesta).lower()
    if any(p in nazov for p in VIDEO_PRIPONY):
        return True
    if any(p in nazov for p in NIE_VIDEO_PRIPONY):
        return False
    try:
        return os.path.getsize(cesta) > (20 << 20)
    except OSError:
        return False


# Rozlíšenia, ktoré sa skúšajú, keď vzorový súbor nakrútila tá istá kamera,
# ale v inom režime. Ostatné nastavenia kodeku sú rovnaké, mení sa len rozmer.
ROZLISENIA_NA_SKUSANIE = [
    (3840, 2160), (1920, 1080), (2704, 1520), (1280, 720),
    (4096, 2160), (2560, 1440), (1080, 1920), (2160, 3840),
]


def _varianty_rozlisenia(ctx: Ctx, ps: bytes, hevc: bool, zdroj: str,
                         videne: set) -> list:
    """Z hotovej hlavičky vyrobí varianty s iným rozlíšením.

    Rieši presne ten prípad, keď má používateľ zdravé video z tej istej kamery,
    ale nakrútené v inom režime: profil, spôsob kódovania aj ostatné nastavenia
    sú rovnaké, nesedí jedine rozlíšenie — a to je jediné pole, ktoré treba
    v hlavičke prepísať.
    """
    sps = (carve.find_nal_in_annexb(ps, 33, hevc=True) if hevc
           else carve.find_sps_in_annexb(ps))
    if not sps:
        return []
    povodne = carve.plausible_hevc_sps(sps) if hevc else carve.plausible_sps(sps)
    if not povodne:
        return []

    ziadane = []
    if ctx.options.get("sirka") and ctx.options.get("vyska"):
        ziadane.append((int(ctx.options["sirka"]), int(ctx.options["vyska"])))
    ziadane += [r for r in ROZLISENIA_NA_SKUSANIE
                if r != (povodne["sirka"], povodne["vyska"])]

    out = []
    for sirka, vyska in ziadane[:9]:
        novy = carve.patch_sps_rozlisenie(sps, sirka, vyska, hevc=hevc)
        if not novy:
            continue
        upravene = ps.replace(carve.START_CODE + sps, carve.START_CODE + novy)
        if upravene in videne:
            continue
        videne.add(upravene)
        out.append({"popis": f"parametre z {zdroj} prepísané na {sirka}×{vyska}",
                    "blob": upravene, "kandidat": None})
    return out


def _zbieraj_vzory(ctx: Ctx, hevc: bool, max_suborov: int = 60) -> list:
    """Pozbiera parametre obrazu z ostatných videí v okolí.

    Kľúčové je, že sa hľadá aj v POŠKODENÝCH súboroch: ransomvér šifruje len
    začiatok, takže index `moov` na konci väčšinou prežije a je v ňom hlavička
    `avcC`/`hvcC` s presnými parametrami kamery. Používateľ tak nepotrebuje ani
    jeden zdravý súbor — stačí, že má z tej istej kamery ešte nejaké iné video,
    hoci rovnako zašifrované.
    """
    miesta = []
    vzor = ctx.options.get("vzor")
    if vzor and os.path.exists(vzor):
        miesta.append(vzor)
    miesta.append(os.path.dirname(ctx.path))

    subory = []
    for miesto in miesta:
        if os.path.isfile(miesto):
            subory.append(miesto)
        elif os.path.isdir(miesto):
            try:
                with os.scandir(miesto) as it:
                    subory += [e.path for e in it if e.is_file() and _mozne_video(e.path)]
            except OSError:
                continue
    subory = [s for s in dict.fromkeys(subory) if os.path.abspath(s) != ctx.path]
    if not subory:
        return []

    # Funkcia sa vola dvakrat (H.264 a H.265); hlasku staci vypisat raz.
    if not getattr(ctx, "_vzory_ohlasene", False):
        ctx.log(f"Hľadám parametre kamery v ostatných videách v okolí "
                f"({len(subory)} súborov, prehľadávajú sa aj poškodené)…")
        ctx._vzory_ohlasene = True
    kandidati = []
    videne = set()
    for cesta in subory[:max_suborov]:
        try:
            surove = mp4.codec_parameter_sets(cesta)
        except (OSError, ValueError):
            continue
        ps = carve.only_parameter_sets(surove, hevc)
        if not ps or ps in videne:
            continue
        videne.add(ps)
        sps = (carve.find_nal_in_annexb(ps, 33, hevc=True) if hevc
               else carve.find_sps_in_annexb(ps))
        info = ((carve.plausible_hevc_sps(sps) if hevc else carve.plausible_sps(sps))
                if sps else None)
        nazov = os.path.basename(cesta)
        popis = (f"parametre z {nazov} ({info['sirka']}×{info['vyska']}, {info['profil']})"
                 if info else f"parametre z {nazov}")
        ctx.log(f"  našiel som {popis}")
        # Nezmenena hlavicka ide vzdy prva: je to presne to, co kamera zapisala.
        # Az za nou nasleduju varianty s inym rozlisenim pre pripad, ze zdravy
        # subor vznikol v inom rezime.
        kandidati.append({"popis": popis, "blob": ps, "kandidat": None,
                          "povodne": True})
        kandidati += _varianty_rozlisenia(ctx, ps, hevc, nazov, videne)
    return kandidati


def _skus_kandidata(ctx: Ctx, mm, size: int, kand: dict, blob: bytes,
                    vzorka: int = 2 << 20) -> dict:
    """Vyreze z daneho miesta kratku vzorku a skusi ju naozaj dekodovat.

    Toto je jediny spolahlivy sposob, ako rozoznat skutocny zaciatok streamu od
    nahodnej zhody v zasifrovanych datach: zhoda moze prejst akoukolvek
    kontrolou struktury, ale dekodovat sa z nej nedá nic.
    """
    docasny = os.path.join(ctx.outdir, "_skuska_start.h264")
    try:
        carve.extract_annexb(mm, kand["offset"], min(size, kand["offset"] + vzorka * 3),
                             docasny, hevc=kand["hevc"], max_len=kand["max_len"])
        with open(docasny, "rb") as f:
            telo = f.read(vzorka)
        if not telo:
            return {"snimky": 0, "kvalita": 0.0, "skore": 0.0}
        return h264_params.score_headers(ctx.toolbox, blob, telo, ctx.outdir,
                                         hevc=kand["hevc"])
    except (OSError, RuntimeError):
        return {"snimky": 0, "kvalita": 0.0, "skore": 0.0}
    finally:
        try:
            os.remove(docasny)
        except OSError:
            pass


def _vyber_start(ctx: Ctx, mm, size: int, hlavicky: list, hint: int = 0,
                 prvy_kandidat=None, pokusov: int = 12,
                 hevc: bool | None = None) -> dict | None:
    """Najde miesto, od ktoreho sa video naozaj dekoduje.

    Struktura sama o sebe nestaci: aj v zasifrovanych datach sa obcas nahodou
    najde miesto, ktore prejde prechadzkou. Spolahliva je az dvojica
    - kotva (oddelovac snimku, ktory kamera zapisuje pred kazdy snimok) a
    skusobne dekodovanie s parametrami z vzoru. Kotvy sa preveruju od zaciatku
    suboru, takze vyhra ta najskorsia, z ktorej sa obraz naozaj poskladá.
    """
    # Na overenie zaciatku staci nezmenena hlavicka z rovnakej kamery. Varianty
    # s prepisanym rozlisenim sa tu neskusaju - obraz by sa z nich nedekodoval
    # a spravny zaciatok by sa zahodil.
    overovacie = [h for h in hlavicky if h.get("povodne")] or hlavicky
    overovacie = overovacie[:3]

    def over(k: dict) -> bool:
        for h in overovacie:
            res = _skus_kandidata(ctx, mm, size, k, h["blob"])
            if res["kvalita"] >= 0.5 and res["snimky"] >= 5:
                return True
        return False

    kotvy = carve.najdi_kotvy(mm, size, hint=max(0, hint), pocet=pokusov,
                              hevc=hevc, log=ctx.log)

    def zaloha() -> list:
        """Pomalsie hladanie podla struktury - az ked kotvy nic nedali.

        Nie kazda kamera zapisuje pred snimky oddelovace; vtedy neostava nic
        ine, nez prejst miesta, ktore prejdu kontrolou struktury, a kazde
        skusit dekodovat.
        """
        zvysok = carve.najdi_kandidatov(mm, size, hint=max(0, hint),
                                        pocet=pokusov * 2)
        if prvy_kandidat is not None:
            zvysok = [prvy_kandidat] + [k for k in zvysok
                                        if k["offset"] != prvy_kandidat["offset"]]
        return zvysok

    if not hlavicky:
        if kotvy:
            return kotvy[0]
        z = zaloha()
        return z[0] if z else None

    for i, k in enumerate(kotvy, 1):
        if over(k):
            if i > 1:
                ctx.log(f"  prvých {i - 1} miest sa dekódovať nedá — "
                        f"začínam na offsete {k['offset']}")
            return k
    zvysne = zaloha()
    for k in zvysne:
        if over(k):
            ctx.log(f"  skúšobným dekódovaním nájdený začiatok na offsete "
                    f"{k['offset']}")
            return k
    if kotvy:
        ctx.log("  skúšobné dekódovanie nikde neprešlo — beriem prvý "
                "oddeľovač snímku")
        return kotvy[0]
    if zvysne:
        ctx.log("  žiadne miesto v súbore sa nedá dekódovať — beriem prvý "
                "nájdený začiatok")
        return zvysne[0]
    return None


def _do_mp4(ctx: Ctx, raw: str, dst: str, hevc: bool, fps: int) -> dict | None:
    """Zabalí surový obrazový stream do MP4, aby sa dal normálne prehrať.

    Najprv sa skúsi prebalenie bez straty kvality. To však pri streame bez
    pôvodnej hlavičky často zlyhá — ffmpeg nevie odvodiť parametre a zapíše
    prázdny súbor. Vtedy sa video prekóduje: je to stratové a pomalšie, ale
    výsledok sa dá otvoriť v akomkoľvek prehrávači, čo je pri záchrane dát
    podstatnejšie než dokonalá kvalita.
    """
    vstup = ["-r", str(fps), "-f", "hevc" if hevc else "h264", "-i", raw]
    res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error"] + vstup
                          + ["-c", "copy", "-movflags", "+faststart", dst], log=None)
    if res["code"] == 0 and os.path.exists(dst) and os.path.getsize(dst) > 65536:
        return {"cesta": dst, "popis": f"Zachránené video ({fps} snímkov/s, "
                                       f"bez straty kvality)"}
    if os.path.exists(dst):
        os.remove(dst)
    ctx.log("Prebalenie bez straty kvality neuspelo (stream nemá pôvodnú hlavičku) "
            "— prekódovávam, aby sa výsledok dal otvoriť v bežnom prehrávači…")
    res = ctx.toolbox.run("ffmpeg", ["-y", "-v", "error"] + vstup
                          + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst],
                          log=None)
    if res["code"] == 0 and os.path.exists(dst) and os.path.getsize(dst) > 65536:
        return {"cesta": dst, "popis": f"Zachránené video ({fps} snímkov/s, "
                                       f"prekódované do H.264)"}
    if os.path.exists(dst):
        os.remove(dst)
    return None


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

        # Samotná oprava je hotová aj bez ffmpeg — ten slúži už len na overenie
        # a na voliteľné orezanie. Keď chýba, výsledok sa aj tak odovzdá.
        ma_ffmpeg = bool(ctx.toolbox.path("ffmpeg"))
        if not ma_ffmpeg:
            ctx.log("ffmpeg nie je k dispozícii — opravený súbor je hotový, ale "
                    "neviem ho overiť ani vyrobiť orezanú verziu. Skús ho prehrať "
                    "napríklad vo VLC, alebo doinštaluj ffmpeg podľa záložky Nástroje.")
        else:
            stav = ctx.toolbox.probe(dst)
            if stav.get("_ok"):
                ctx.log("ffprobe/ffmpeg súbor úspešne prečítal.")

        # Ktoré verzie si používateľ praje. Pri viacgigabajtových videách sa
        # oplatí nechať len jednu — dve zaberú dvojnásobok miesta.
        rezim = ctx.options.get("verzie", "obidve")
        if rezim not in ("obidve", "len_orezany", "len_opraveny"):
            rezim = "obidve"
        chce_orezanie = rezim in ("obidve", "len_orezany")

        # orezanie poskodeneho zaciatku po prvy klucovy snimok
        if ma_ffmpeg and chce_orezanie and video is not None and damage_end:
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
                if res["code"] == 0 and os.path.exists(trimmed) \
                        and os.path.getsize(trimmed) > 65536:
                    vystupy.append({"cesta": trimmed,
                                    "popis": f"Čistý súbor bez poškodeného začiatku "
                                             f"(začína v čase {cas:.1f} s)"})
                    if rezim == "len_orezany":
                        # celú verziu zmažeme až teraz, keď je orezaná overene hotová
                        os.remove(dst)
                        vystupy = [v for v in vystupy if v["cesta"] != dst]
                        ctx.log("Verziu v plnej dĺžke mažem — praješ si len orezanú.")
                else:
                    ctx.log("Orezanie sa nepodarilo — ostáva aspoň súbor v plnej dĺžke.")
            else:
                ctx.log("Nenašiel sa kľúčový snímok za poškodenou časťou.")

        kontrola = (ctx.toolbox.playable(vystupy[-1]["cesta"], seconds=8) if ma_ffmpeg
                    else {"ok": None, "reason": "bez ffmpeg sa výsledok nedá overiť"})
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
        elif not ctx.options.get("ponechat_medzisubory"):
            # MP4 je hotové, medzikroky (.ts, .h264) sú zbytočné a zaberajú
            # toľko miesta ako samotné video
            zostava = []
            for v in vystupy:
                if v["cesta"].endswith(".mp4"):
                    zostava.append(v)
                    continue
                try:
                    os.remove(v["cesta"])
                except OSError:
                    zostava.append(v)
            if len(zostava) != len(vystupy):
                ctx.log("Medzisúbory zmazané (v nastaveniach sa dajú ponechať).")
            vystupy = zostava
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
            # Odhad podľa rovnomernosti dát sa nedá brať ako hranica hľadania.
            # Záznam z kamery s vysokým dátovým tokom (napríklad 1080p50 pri
            # 46 Mbit/s) má rozloženie bajtov takmer rovnomerné, takže test
            # označí za zašifrované aj neporušené video a hľadanie by začalo
            # priďaleko — až medzi náhodnými zhodami. Preto sa prechádza od
            # začiatku; zašifrovaná časť sa aj tak preskočí sama, lebo v nej
            # žiadny platný sled NAL jednotiek nie je.
            hint = 0
            if est.get("koniec"):
                ctx.log(f"  (odhad hranice {est['koniec']} B beriem len ako "
                        f"orientačný — hľadám od začiatku súboru)")
        # Kodek vopred nepoznáme — index, v ktorom bol zapísaný, je zničený.
        # Preto sa zbierajú kandidáti pre H.264 aj H.265 a rozhodne sa medzi
        # nimi skúšobným dekódovaním: náhodná zhoda v zašifrovaných dátach
        # prejde akoukoľvek kontrolou štruktúry, ale dekódovať sa z nej nedá.
        # Keď používateľ kodek pozná, obmedzí sa hľadanie len naň.
        volba_hevc = ctx.options.get("hevc")
        ctx.log("Hľadám začiatok obrazového streamu…")
        # Hotové parametre z okolitých videí — potrebné už teraz, lebo práve
        # nimi sa jednotlivé miesta preverujú skúšobným dekódovaním.
        hlavicky = _zbieraj_vzory(ctx, False) + _zbieraj_vzory(ctx, True)
        found = _vyber_start(ctx, mm, size, hlavicky, hint=hint,
                             hevc=volba_hevc)
        if found is None:
            raise RuntimeError(
                "V súbore sa nenašiel žiadny súvislý obrazový stream. Súbor je "
                "pravdepodobne zašifrovaný celý, alebo používa iný kodek ako "
                "H.264/H.265.")

        hevc = found["hevc"]
        ctx.log(f"Rozpoznaný kodek: {'H.265 / HEVC' if hevc else 'H.264 / AVC'}, "
                f"začiatok na offsete {found['offset']}")
        raw = ctx.out("vyrezany", ".h265" if hevc else ".h264")
        params = carve.collect_parameter_sets(mm, found["offset"], size, hevc=hevc)
        if params:
            ctx.log(f"Parametre SPS/PPS sa našli priamo v tele streamu ({len(params)} B).")
        # Zoznam zaciatkov vsetkych snimkov. Pri vyrezavani slúži ako pevný
        # bod: stream sa po každom kúsku zvuku chytí presne tam, kde začína
        # ďalší snímok, namiesto hádania.
        kotvy = carve.kotvy_offsety(mm, size, hint=found["offset"], hevc=hevc)
        if kotvy:
            ctx.log(f"Nájdených {len(kotvy)} začiatkov snímkov — použijem ich "
                    f"ako pevné body pri vyrezávaní.")
        stat = carve.extract_annexb(mm, found["offset"], size, raw, hevc=hevc,
                                    sps_pps=params, max_len=found["max_len"],
                                    kotvy=kotvy, log=ctx.log)
        ctx.log(f"Vyrezaných {stat['nals']} NAL jednotiek, {human(stat['bytes'])}, "
                f"preskočených medzier: {stat['gaps']}")
    finally:
        mm.close()
        f.close()

    if not stat["nals"]:
        raise RuntimeError("Nepodarilo sa vyrezať žiadne obrazové dáta.")

    # Hotove parametre, ktore stoja za vyskusanie ako prve: najdene priamo v
    # tele streamu a vytiahnute zo zdraveho suboru z rovnakeho zariadenia.
    extra = []
    # Parametre sa v streame opakujú pri každom kľúčovom snímku, takže aj keď
    # prvý výskyt padol za obeť šifrovaniu, ďalší býva o kus ďalej.
    vlastne = carve.parameter_sets_anywhere(raw, hevc=hevc)
    if vlastne:
        sps = (carve.find_sps_in_annexb(vlastne) if not hevc
               else carve.find_nal_in_annexb(vlastne, 33, hevc=True))
        info = ((carve.plausible_hevc_sps(sps) if hevc else carve.plausible_sps(sps))
                if sps else None)
        popis = (f"parametre z tela streamu ({info['sirka']}×{info['vyska']}, "
                 f"{info['profil']})" if info else "parametre z tela streamu")
        ctx.log(f"V tele streamu sa našli {popis} — použijem ich.")
        extra.append({"popis": popis, "blob": vlastne, "kandidat": None})
    extra += [h for h in hlavicky]

    hlavicka = b""
    najdene = None
    if hevc and not extra:
        ctx.log("Stream je H.265, ale parametre kamery sa nenašli ani v ňom, ani "
                "v okolitých videách. Poskladať ich naslepo sa pri H.265 nedá — "
                "je ich príliš veľa kombinácií. Skús do poľa „Zdravý vzorový súbor“ "
                "zadať priečinok s ďalšími videami z tej istej kamery; stačia aj "
                "poškodené, parametre sa dajú vytiahnuť aj z nich.")
    if extra or not hevc:
        rozlisenia = None
        if ctx.options.get("sirka") and ctx.options.get("vyska"):
            rozlisenia = [(int(ctx.options["sirka"]), int(ctx.options["vyska"]))]
            ctx.log(f"Používam zadané rozlíšenie {rozlisenia[0][0]}×{rozlisenia[0][1]}.")
        najdene = h264_params.find_best_headers(
            ctx.toolbox, raw, ctx.outdir, resolutions=rozlisenia,
            quick=bool(ctx.options.get("rychle_hladanie", True)),
            log=ctx.log, extra=extra, hevc=hevc)
        if najdene and najdene["vysledok"]["skore"] > 0:
            hlavicka = najdene["hlavicka"]
            parametre_ok = True
        else:
            parametre_ok = False
            ctx.log("Žiadne parametre nezabrali. Skús zadať presné rozlíšenie "
                    "pôvodného videa, alebo dodaj zdravý súbor z rovnakého zariadenia.")
    else:
        parametre_ok = bool(params)

    # Hlavičku pripíšeme na začiatok toho istého súboru namiesto vyrobenia
    # druhej kópie — pri veľkých videách to je rozdiel celej veľkosti súboru.
    final_raw = raw
    if hlavicka:
        docasny = raw + ".tmp"
        with open(docasny, "wb") as fo:
            fo.write(hlavicka)
            with open(raw, "rb") as fi:
                shutil.copyfileobj(fi, fo, COPY_CHUNK)
        os.replace(docasny, raw)

    fps = ctx.options.get("fps") or 30
    dst = ctx.out("zachraneny", ".mp4")
    hotove = _do_mp4(ctx, final_raw, dst, hevc, fps)
    if hotove:
        vystupy.append(hotove)
        # Surový stream je len medzikrok. Keď MP4 vzniklo, je zbytočný a zaberá
        # rovnako veľa miesta ako samotné video, preto ho zmažeme — ak si ho
        # používateľ výslovne nepraje ponechať.
        if ctx.options.get("ponechat_medzisubory"):
            vystupy.insert(0, {"cesta": final_raw,
                               "popis": "Surový obrazový stream (medzikrok)"})
        else:
            try:
                os.remove(final_raw)
                ctx.log("Medzisúbor so surovým streamom zmazaný "
                        "(v nastaveniach sa dá ponechať).")
            except OSError:
                pass
    else:
        ctx.log("Ani prekódovanie neuspelo — z tohto streamu sa obraz poskladať "
                "nedá. Bez správnych parametrov ho neprehrá žiadny prehrávač.")
        vystupy.append({"cesta": final_raw,
                        "popis": "Surový obrazový stream (MP4 sa vyrobiť nepodarilo)"})

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


# Stratégie, ktoré sa bez ffmpeg nedajú dokončiť. Kontrolujú sa vopred, aby
# používateľ nečakal minúty na prácu, ktorá aj tak nemôže skončiť výsledkom.
VYZADUJU_FFMPEG = {"mp4_carve", "ts_resync", "untrunc", "ffmpeg_remux"}

STRATEGIE = {
    "mp4_graft": strategia_mp4_graft,
    "mp4_carve": strategia_mp4_carve,
    "ts_resync": strategia_ts_resync,
    "untrunc": strategia_untrunc,
    "ffmpeg_remux": strategia_ffmpeg_remux,
}


def spusti(strategy_id: str, ctx: Ctx) -> dict:
    if strategy_id == "nezachranitelne":
        raise RuntimeError("Tento súbor sa opraviť nedá — nezostalo v ňom nič, "
                           "z čoho by sa dal obraz poskladať.")
    fn = STRATEGIE.get(strategy_id)
    if fn is None:
        raise ValueError(f"Neznáma stratégia: {strategy_id}")
    if strategy_id in VYZADUJU_FFMPEG and not ctx.toolbox.path("ffmpeg"):
        raise RuntimeError(
            "Tento postup potrebuje ffmpeg, ktorý sa v počítači nenašiel. "
            "Nainštaluj ho (v záložke Nástroje je návod), alebo použi postup "
            "„Nahradenie hlavičky“, ktorý ffmpeg nepotrebuje.")
    return fn(ctx)


# ---------------------------------------------------------------------------
# Davkova oprava
# ---------------------------------------------------------------------------

# Priecinky, do ktorych nema zmysel liezt: vysledky vlastnej prace a systemove
PRESKOCIT = {"opravene", "opravene-videa", ".Trashes", ".Spotlight-V100",
             ".fseventsd", "$RECYCLE.BIN", "System Volume Information"}


def najdi_videa(priecinok: str, vynechaj: set | None = None,
                rekurzivne: bool = False, max_hlbka: int = 6,
                max_suborov: int = 20000) -> list:
    """Zoznam videi v priecinku, vratane premenovanych ransomverom.

    S `rekurzivne=True` prejde aj podpriecinky - projekty byvaju rozdelene po
    priecinkoch podla kamier a dni, takze bez toho by prehlad nasiel len tie
    subory, ktore lezia volne navrchu.
    """
    vynechaj = {os.path.abspath(c) for c in (vynechaj or set())}
    koren = os.path.abspath(priecinok)
    out = []

    def pridaj(cesta: str):
        try:
            if os.path.getsize(cesta) > (64 << 10):
                out.append(cesta)
        except OSError:
            pass

    if not rekurzivne:
        try:
            with os.scandir(priecinok) as it:
                for e in it:
                    if e.is_file() and os.path.abspath(e.path) not in vynechaj \
                            and _mozne_video(e.path):
                        pridaj(e.path)
        except OSError:
            return []
    else:
        for korenovy, podpriecinky, subory in os.walk(priecinok):
            hlbka = korenovy[len(koren):].count(os.sep)
            if hlbka >= max_hlbka:
                podpriecinky[:] = []
            podpriecinky[:] = [d for d in podpriecinky
                               if not d.startswith(".") and d not in PRESKOCIT]
            for nazov in subory:
                cesta = os.path.join(korenovy, nazov)
                if os.path.abspath(cesta) in vynechaj or nazov.startswith("."):
                    continue
                if _mozne_video(cesta):
                    pridaj(cesta)
            if len(out) >= max_suborov:
                break
    out.sort(key=lambda c: c.lower())
    return out[:max_suborov]


def over_vystup(outdir: str, potrebne: int = 0) -> None:
    """Overi, ze sa do vystupneho priecinka da naozaj zapisovat.

    Disky formatovane pre Windows (NTFS) pripaja macOS len na citanie a
    externy disk sa vie prepnut do rezimu len na citanie aj sam, ked na nom
    zacnu chyby. Bez tejto kontroly by dávka prebehla cez stovky suborov a
    pri kazdom zopakovala tu istu chybu.

    Vyhodi `RuntimeError` s vysvetlenim, co s tym.
    """
    try:
        os.makedirs(outdir, exist_ok=True)
    except (OSError, ValueError) as exc:
        dovod = getattr(exc, "strerror", None) or str(exc)
        raise RuntimeError(
            f"Do priečinka „{outdir}“ sa nedá zapisovať ({dovod}). "
            f"Vyber výstupný priečinok na disku, na ktorý sa dá zapisovať — "
            f"napríklad na internom disku počítača.") from exc

    skuska = os.path.join(outdir, ".vidfix_skuska")
    try:
        with open(skuska, "wb") as f:
            f.write(b"x")
        os.remove(skuska)
    except OSError as exc:
        if exc.errno == errno.EROFS:
            dovod = ("disk je pripojený len na čítanie. Býva to pri diskoch "
                     "formátovaných pre Windows (NTFS), ktoré macOS zapisovať "
                     "nevie, alebo keď sa disk kvôli chybám sám prepol do "
                     "režimu len na čítanie")
        elif exc.errno in (errno.EACCES, errno.EPERM):
            dovod = "priečinok je chránený a program doň nemá prístup"
        elif exc.errno == errno.ENOSPC:
            dovod = "na disku už nie je voľné miesto"
        else:
            dovod = exc.strerror or "zápis zlyhal"
        raise RuntimeError(
            f"Do priečinka „{outdir}“ sa nedá zapisovať: {dovod}. "
            f"Zvoľ výstupný priečinok na inom disku — zachránené videá musia "
            f"mať kam ísť. Pôvodné poškodené súbory sa nikdy neprepisujú, "
            f"takže disk s nimi stačí mať pripojený len na čítanie.") from exc

    if potrebne:
        try:
            volne = shutil.disk_usage(outdir).free
        except OSError:
            return
        if volne < potrebne:
            raise RuntimeError(
                f"Na výstupnom disku je {human(volne)} voľných, ale zachránené "
                f"videá zaberú približne {human(potrebne)}. Uvoľni miesto "
                f"alebo vyber priestrannejší disk.")


def spusti_davku(subory: list, outdir: str, toolbox, db, volby: dict, log,
                 strategia: str | None = None, preruseny=None) -> dict:
    """Opravi cely zoznam suborov rovnakym postupom ako ten prvy.

    Postup sa pre kazdy subor volí zvlast: v jednom priecinku byvaju subory,
    ktorym index prezil (staci nahradit hlavicku), aj take, ktorym neprezil
    (treba vyrezavat). Nastavenia - vzorovy subor, rozlisenie, snimkova
    frekvencia - sa preberaju z prveho, uspesneho behu.
    """
    # Zapisovatelnost sa overi raz na zaciatku. Bez toho by sa tá istá chyba
    # zopakovala pri kazdom z niekolko sto suborov.
    potrebne = 0
    for cesta in subory:
        try:
            potrebne += os.path.getsize(cesta)
        except OSError:
            continue
    over_vystup(outdir, potrebne)

    vysledky = []
    hotove = zlyhane = preskocene = 0
    for i, cesta in enumerate(subory, 1):
        if preruseny and preruseny():
            log("Dávka prerušená používateľom.")
            break
        nazov = os.path.basename(cesta)
        log("")
        log(f"───── [{i}/{len(subory)}] {nazov} " + "─" * max(0, 40 - len(nazov)))
        try:
            rep = analyze(cesta, db, toolbox)
            zvolena = strategia
            if not zvolena:
                navrhy = [s["id"] for s in rep.get("strategie", [])]
                zvolena = navrhy[0] if navrhy else None
            if zvolena == "nezachranitelne":
                dovod = (rep.get("strategie") or [{}])[0].get("nazov", "nedá sa opraviť")
                log(f"— preskakujem: {dovod}")
                vysledky.append({"subor": nazov, "cesta": cesta, "ok": False,
                                 "preskocene": True, "chyba": dovod})
                preskocene += 1
                continue
            if not zvolena:
                raise RuntimeError("Pre tento súbor sa nenašiel vhodný postup.")
            if zvolena in VYZADUJU_FFMPEG and not toolbox.path("ffmpeg"):
                raise RuntimeError("Tento postup potrebuje ffmpeg.")
            log(f"Postup: {zvolena}")
            ctx = Ctx(cesta, outdir, toolbox, db, volby, log=log)
            v = spusti(zvolena, ctx)
            hotove += 1
            vysledky.append({"subor": nazov, "cesta": cesta, "ok": True,
                             "strategia": zvolena, "zhrnutie": v.get("zhrnutie", ""),
                             "vystupy": v.get("vystupy", []),
                             "kontrola": v.get("kontrola", {})})
            log(f"✓ hotovo — {len(v.get('vystupy', []))} výsledných súborov")
        except Exception as exc:            # noqa: BLE001 - chybu chceme ukazat a ist dalej
            zlyhane += 1
            vysledky.append({"subor": nazov, "cesta": cesta, "ok": False,
                             "chyba": str(exc) or exc.__class__.__name__})
            log(f"✗ nepodarilo sa: {exc}")

    log("")
    log("=" * 56)
    log(f"Hotovo: {hotove} z {len(subory)} súborov opravených, {zlyhane} zlyhalo"
        + (f", {preskocene} preskočených (nedajú sa opraviť)." if preskocene else "."))
    return {"ok": hotove > 0, "pocet": len(subory), "hotove": hotove,
            "zlyhane": zlyhane, "preskocene": preskocene, "vysledky": vysledky,
            "zhrnutie": f"Opravených {hotove} z {len(subory)} súborov."
                        + (f" {preskocene} sa opraviť nedá." if preskocene else "")}


# ---------------------------------------------------------------------------
# Upratanie medzisuborov
# ---------------------------------------------------------------------------

# Pripony a znacky, ktorymi program pomenuva medzikroky. Nic ine sa nemaze.
MEDZIKROKY = ("_vyrezany.h264", "_vyrezany.h265",
              "_vyrezany_s_hlavickou.h264", "_vyrezany_s_hlavickou.h265",
              "_obraz.h264", "_obraz.h265",
              "_zachraneny.ts", "_zachraneny_od_klucoveho_snimku.ts")


def najdi_medzikroky(priecinok: str, rekurzivne: bool = True) -> list:
    """Najde medzisubory, ku ktorym uz existuje hotovy vysledok.

    Maze sa len to, co program sam vyrobil ako medzikrok, a len vtedy, ked
    vedla lezi hotove MP4 - teda ked uz medzikrok netreba. Vsetko ostatne
    ostava nedotknute.
    """
    out = []
    chodza = os.walk(priecinok) if rekurzivne else [
        (priecinok, [], [e.name for e in os.scandir(priecinok) if e.is_file()])]
    for koren, _podpriecinky, subory in chodza:
        mena = set(subory)
        for nazov in subory:
            for znacka in MEDZIKROKY:
                if not nazov.endswith(znacka):
                    continue
                zaklad = nazov[:-len(znacka)]
                hotove = f"{zaklad}_zachraneny.mp4"
                if hotove in mena:
                    cesta = os.path.join(koren, nazov)
                    try:
                        out.append((cesta, os.path.getsize(cesta)))
                    except OSError:
                        pass
                break
    return out
