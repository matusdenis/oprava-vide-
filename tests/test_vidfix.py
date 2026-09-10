"""Testy programu na opravu videa.

Spustenie:  python3 -m unittest discover -s tests -v
Testy, ktore potrebuju ffmpeg, sa bez neho automaticky preskocia.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vidfix import carve, h264_params, mp4                       # noqa: E402
from vidfix.analyze import (analyze, rychly_verdikt,             # noqa: E402
                            _je_v_tele_video)
from vidfix.headerdb import HeaderDB                             # noqa: E402
from vidfix.repair import (Ctx, najdi_medzikroky, najdi_videa,   # noqa: E402
                           spusti, spusti_davku)
from vidfix.tools import Toolbox                                 # noqa: E402
from vidfix.util import entropy, human                           # noqa: E402
from vzorky import posifruj_skakavo, posifruj_zaciatok, vyrob_video  # noqa: E402

TB = Toolbox()
MA_FFMPEG = bool(TB.path("ffmpeg"))
potrebuje_ffmpeg = unittest.skipUnless(MA_FFMPEG, "ffmpeg nie je k dispozicii")


class TestPomocne(unittest.TestCase):
    def test_human(self):
        self.assertEqual(human(512), "512 B")
        self.assertEqual(human(1536), "1.50 KiB")

    def test_entropia(self):
        self.assertAlmostEqual(entropy(b"\0" * 1000), 0.0)
        self.assertGreater(entropy(bytes(range(256)) * 4), 7.9)


class TestBoxy(unittest.TestCase):
    def test_stavba_ftyp(self):
        b = mp4.ftyp_box(b"isom", 512, (b"isom", b"avc1"))
        self.assertEqual(len(b), 24)
        self.assertEqual(b[4:8], b"ftyp")
        self.assertEqual(int.from_bytes(b[0:4], "big"), 24)

    def test_hlavicka_graftu(self):
        h = mp4.build_graft_header(5_000_000)
        self.assertEqual(h[4:8], b"ftyp")
        self.assertEqual(h[len(h) - 12:len(h) - 8], b"mdat")
        # 64-bitova velkost mdat musi pokryvat vsetko az po index
        velkost = int.from_bytes(h[-8:], "big")
        self.assertEqual(velkost + 32, 5_000_000)

    def test_prefix_ma_presnu_dlzku(self):
        p = mp4.build_prefix(1 << 20, 9_000_000)
        self.assertEqual(len(p), 1 << 20)
        self.assertEqual(p[4:8], b"ftyp")

    def test_prilis_kratky_prefix(self):
        with self.assertRaises(ValueError):
            mp4.build_prefix(16, 9_000_000)


class TestParametreH264(unittest.TestCase):
    def test_sps_tam_a_spat(self):
        for (w, h, prof) in [(1920, 1080, 100), (1280, 720, 77), (640, 360, 66),
                             (3840, 2160, 100), (1080, 1920, 100)]:
            sps = h264_params.build_sps(w, h, profile_idc=prof)
            info = carve.parse_h264_sps(sps)
            self.assertIsNotNone(info, f"SPS {w}x{h} sa neda rozparsovat")
            self.assertEqual((info["sirka"], info["vyska"]), (w, h))
            self.assertEqual(info["profile_idc"], prof)

    def test_pps_je_platny(self):
        pps = h264_params.build_pps()
        self.assertEqual(pps[0] & 0x1F, 8)

    def test_nezmysel_nie_je_sps(self):
        self.assertIsNone(carve.plausible_sps(b"\x67\x27\x00\x60\xac\x11"))

    def test_mriezka_ma_rozumnu_velkost(self):
        self.assertGreater(len(h264_params.candidate_grid(quick=True)), 100)
        self.assertLess(len(h264_params.candidate_grid(quick=True)), 500)


class TestDatabaza(unittest.TestCase):
    def setUp(self):
        self.db = HeaderDB()

    def test_hlavicky_su_platne_boxy(self):
        for h in self.db.data["hlavicky"]:
            raw = self.db.header_bytes(h["id"])
            self.assertEqual(raw[4:8], b"ftyp", h["id"])
            self.assertEqual(int.from_bytes(raw[0:4], "big"), len(raw), h["id"])

    def test_rozpoznanie_podla_pripony(self):
        v = self.db.identify(b"\x00" * 32, filename="dovolenka.mp4.locked")
        self.assertTrue(any(k["id"] == "mp4" for k in v))

    def test_odporucanie_hlavicky(self):
        self.assertEqual(self.db.suggest_header("mp4", "hvc1"), "mp4_hevc")
        self.assertEqual(self.db.suggest_header("mov", "avc1"), "mov_quicktime")

    def test_kandidatske_dlzky(self):
        self.assertIn(1048576, self.db.candidate_lengths())


class ZakladVzorky(unittest.TestCase):
    """Spolocny zaklad pre testy, ktore potrebuju skutocne video."""

    @classmethod
    def setUpClass(cls):
        if not MA_FFMPEG:
            raise unittest.SkipTest("ffmpeg nie je k dispozicii")
        cls.dir = tempfile.mkdtemp(prefix="vidfix-test-")
        cls.zdroj = vyrob_video(os.path.join(cls.dir, "zdrave.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def out(self, meno: str) -> str:
        return os.path.join(self.dir, meno)


@potrebuje_ffmpeg
class TestIndexPrezil(ZakladVzorky):
    """Najbeznejsi pripad: moov je na konci suboru a prezil."""

    def setUp(self):
        self.poskodeny = posifruj_zaciatok(
            self.zdroj, self.out("poskodene.mp4"), 512 * 1024,
            znacka=b"RANSOM_ID_TEST_0001")

    def test_analyza_najde_index_aj_hranicu(self):
        rep = analyze(self.poskodeny, HeaderDB(), TB)
        d = rep["detail"]
        self.assertEqual(d["typ"], "mp4")
        self.assertIsNotNone(d["index_moov"])
        self.assertEqual(d["stav"], "index moov prežil")
        # zmerana hranica musi byt blizko skutocnej (jeden usek tolerancie)
        self.assertGreaterEqual(d["poskodenie"]["damage_end"], 512 * 1024)
        self.assertLess(d["poskodenie"]["damage_end"], 512 * 1024 + 300_000)
        self.assertEqual(rep["strategie"][0]["id"], "mp4_graft")

    def test_najde_pripojenu_znacku_na_konci(self):
        rep = analyze(self.poskodeny, HeaderDB(), TB)
        self.assertEqual(rep["detail"]["zvysne_bajty_na_konci"], len(b"RANSOM_ID_TEST_0001"))

    def test_oprava_vyrobi_prehratelny_subor(self):
        ctx = Ctx(self.poskodeny, self.out("vysledok"), TB, HeaderDB(), {})
        v = spusti("mp4_graft", ctx)
        self.assertTrue(v["ok"])
        self.assertGreaterEqual(len(v["vystupy"]), 1)
        for o in v["vystupy"]:
            self.assertTrue(os.path.exists(o["cesta"]))
        # cisty orezany subor sa musi dekodovat uplne bez chyb
        cisty = [o for o in v["vystupy"] if "orezany" in o["cesta"]]
        self.assertTrue(cisty, "ocakaval som aj orezanu verziu")
        self.assertTrue(TB.playable(cisty[0]["cesta"], seconds=30)["ok"])

    def test_povodny_subor_ostal_nedotknuty(self):
        with open(self.poskodeny, "rb") as f:
            pred = f.read()
        ctx = Ctx(self.poskodeny, self.out("vysledok2"), TB, HeaderDB(), {})
        spusti("mp4_graft", ctx)
        with open(self.poskodeny, "rb") as f:
            self.assertEqual(pred, f.read())

    def test_opraveny_ma_rovnaku_velkost(self):
        ctx = Ctx(self.poskodeny, self.out("vysledok3"), TB, HeaderDB(), {})
        v = spusti("mp4_graft", ctx)
        plny = [o for o in v["vystupy"] if "opraveny" in o["cesta"]][0]["cesta"]
        self.assertEqual(os.path.getsize(plny), os.path.getsize(self.poskodeny))


@potrebuje_ffmpeg
class TestIndexZniceny(ZakladVzorky):
    """Subor typu faststart - index bol na zaciatku a je prec."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zdroj_fs = vyrob_video(os.path.join(cls.dir, "faststart.mp4"),
                                   faststart=True)

    def setUp(self):
        self.poskodeny = posifruj_zaciatok(self.zdroj_fs,
                                           self.out("faststart_poskodene.mp4"),
                                           512 * 1024)

    def test_analyza_odporuci_vyrezanie(self):
        rep = analyze(self.poskodeny, HeaderDB(), TB)
        self.assertIsNone(rep["detail"].get("index_moov"))
        self.assertEqual(rep["strategie"][0]["id"], "mp4_carve")

    def test_statisticky_odhad_hranice(self):
        f, mm, size = mp4.open_mm(self.poskodeny)
        try:
            est = carve.estimate_encrypted_prefix(mm, size)
        finally:
            mm.close()
            f.close()
        self.assertIsNotNone(est["koniec"])
        self.assertLessEqual(abs(est["koniec"] - 512 * 1024), 128 * 1024)

    def test_medzisubory_sa_nenechavaju(self):
        # surový stream zaberá toľko ako samotné video — po vyrobení MP4 je zbytočný
        vystup = self.out("bez_medzikrokov")
        ctx = Ctx(self.poskodeny, vystup, TB, HeaderDB(),
                  {"sirka": 640, "vyska": 360, "fps": 25})
        spusti("mp4_carve", ctx)
        zvysne = os.listdir(vystup)
        self.assertTrue(any(f.endswith(".mp4") for f in zvysne))
        self.assertFalse([f for f in zvysne if f.endswith((".h264", ".h265"))],
                         f"medzisúbory mali byť zmazané, ostalo: {zvysne}")

    def test_medzisubory_sa_daju_ponechat(self):
        vystup = self.out("s_medzikrokmi")
        ctx = Ctx(self.poskodeny, vystup, TB, HeaderDB(),
                  {"sirka": 640, "vyska": 360, "fps": 25,
                   "ponechat_medzisubory": True})
        spusti("mp4_carve", ctx)
        zvysne = os.listdir(vystup)
        self.assertTrue([f for f in zvysne if f.endswith((".h264", ".h265"))])

    def test_uprac_maze_len_medzikroky_s_hotovym_vysledkom(self):
        d = self.out("upratovanie")
        os.makedirs(d, exist_ok=True)
        hotovy = os.path.join(d, "A_zachraneny.mp4")
        medzikrok = os.path.join(d, "A_vyrezany.h264")
        osirely = os.path.join(d, "B_vyrezany.h264")   # bez hotového výsledku
        dolezity = os.path.join(d, "moje_video.mp4")
        for c in (hotovy, medzikrok, osirely, dolezity):
            with open(c, "wb") as f:
                f.write(b"x" * 2048)
        najdene = [c for c, _v in najdi_medzikroky(d)]
        self.assertIn(medzikrok, najdene)
        self.assertNotIn(osirely, najdene, "medzikrok bez výsledku sa mazať nesmie")
        self.assertNotIn(dolezity, najdene)
        self.assertNotIn(hotovy, najdene)

    def test_vyrezanie_zachrani_obraz(self):
        ctx = Ctx(self.poskodeny, self.out("vysledok_carve"), TB, HeaderDB(),
                  {"sirka": 640, "vyska": 360, "fps": 25})
        v = spusti("mp4_carve", ctx)
        self.assertTrue(v["ok"])
        mp4y = [o for o in v["vystupy"] if o["cesta"].endswith(".mp4")]
        self.assertTrue(mp4y, "ocakaval som zachranene MP4")
        # rozlisenie sa musi trafit
        self.assertEqual((v["parametre"]["sirka"], v["parametre"]["vyska"]), (640, 360))


@potrebuje_ffmpeg
class TestTokTS(ZakladVzorky):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zdroj_ts = vyrob_video(os.path.join(cls.dir, "zdrave.ts"),
                                   kontajner="ts", keyint=25)

    def setUp(self):
        self.poskodeny = posifruj_zaciatok(self.zdroj_ts, self.out("poskodene.ts"),
                                           256 * 1024)

    def test_analyza_rozpozna_ts(self):
        rep = analyze(self.poskodeny, HeaderDB(), TB)
        self.assertEqual(rep["detail"]["typ"], "mpegts")
        self.assertEqual(rep["strategie"][0]["id"], "ts_resync")

    def test_zachrana_toku(self):
        ctx = Ctx(self.poskodeny, self.out("vysledok_ts"), TB, HeaderDB(), {})
        v = spusti("ts_resync", ctx)
        self.assertTrue(v["ok"])
        self.assertTrue(v["vystupy"])
        self.assertTrue(os.path.exists(v["vystupy"][0]["cesta"]))


@potrebuje_ffmpeg
class TestSkakaveSifrovanie(ZakladVzorky):
    def test_mapa_poskodenia_odhali_nesuvisle_poskodenie(self):
        poskodeny = posifruj_skakavo(self.zdroj, self.out("skakave.mp4"))
        rep = analyze(poskodeny, HeaderDB(), TB)
        mapa = rep["detail"].get("mapa_poskodenia")
        self.assertIsNotNone(mapa)
        self.assertFalse(mapa["contiguous_prefix"],
                         "poskodenie ma byt vyhodnotene ako nesuvisle")
        self.assertGreater(mapa["bad"], 0)


@potrebuje_ffmpeg
class TestZdravySubor(ZakladVzorky):
    def test_zdravy_subor_sa_vyhodnoti_ako_neporuseny(self):
        rep = analyze(self.zdroj, HeaderDB(), TB)
        self.assertEqual(rep["detail"]["hranica_struktury"], 0)
        self.assertEqual(rep["detail"]["poskodenie"]["damage_end"], 0)

    def test_hlavicku_zo_zdraveho_suboru_sa_da_pridat(self):
        db = HeaderDB(user_path=os.path.join(self.dir, "vlastne.json"))
        z = db.add_header_from_file(self.zdroj, "Testovacia kamera")
        self.assertEqual(z["major_brand"][:4], "isom")
        self.assertTrue(any(h["id"] == z["id"] for h in db.headers))

    def test_parametre_z_avcc(self):
        ps = mp4.avcc_parameter_sets(self.zdroj)
        self.assertTrue(ps.startswith(b"\x00\x00\x00\x01"))
        sps = carve.find_sps_in_annexb(ps)
        info = carve.plausible_sps(sps)
        self.assertEqual((info["sirka"], info["vyska"]), (640, 360))


@potrebuje_ffmpeg
class TestH265(ZakladVzorky):
    """Kamery a drony nakrúcajú aj v H.265 — kodek sa musí rozpoznať sám."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zdroj_h265 = vyrob_video(os.path.join(cls.dir, "zdrave_h265.mp4"),
                                     sekundy=8, sirka=1280, vyska=720,
                                     faststart=True, kodek="h265", keyint=25)

    def setUp(self):
        self.poskodeny = posifruj_zaciatok(self.zdroj_h265,
                                           self.out("h265_poskodene.mp4"), 256 * 1024)

    def test_sps_h265_sa_da_rozparsovat(self):
        ps = mp4.codec_parameter_sets(self.zdroj_h265)
        sps = carve.find_nal_in_annexb(ps, 33, hevc=True)
        info = carve.plausible_hevc_sps(sps)
        self.assertIsNotNone(info, "SPS pre H.265 sa nepodarilo rozparsovať")
        self.assertEqual((info["sirka"], info["vyska"]), (1280, 720))

    def test_odhad_hranice_funguje_aj_pri_h265(self):
        # H.265 má oveľa hustejšie dáta než H.264 — počítanie núl na to nestačilo
        f, mm, size = mp4.open_mm(self.poskodeny)
        try:
            est = carve.estimate_encrypted_prefix(mm, size)
        finally:
            mm.close()
            f.close()
        self.assertIsNotNone(est["koniec"], "zašifrovaná časť sa nenašla")
        self.assertLessEqual(abs(est["koniec"] - 256 * 1024), 256 * 1024)

    def test_kodek_sa_rozpozna_sam(self):
        f, mm, size = mp4.open_mm(self.poskodeny)
        try:
            najdene = carve.find_nal_stream(mm, size, hint=256 * 1024)
        finally:
            mm.close()
            f.close()
        self.assertIsNotNone(najdene, "obrazový stream sa nenašiel")
        self.assertTrue(najdene["hevc"], "kodek mal byť rozpoznaný ako H.265")

    def test_zachrana_so_vzorom_da_prehratelny_subor(self):
        ctx = Ctx(self.poskodeny, self.out("vysledok_h265"), TB, HeaderDB(),
                  {"fps": 25, "vzor": self.zdroj_h265})
        v = spusti("mp4_carve", ctx)
        self.assertTrue(v["ok"])
        mp4y = [o for o in v["vystupy"] if o["cesta"].endswith(".mp4")]
        self.assertTrue(mp4y, "očakával som prehrateľné MP4 na výstupe")
        self.assertGreater(os.path.getsize(mp4y[0]["cesta"]), 65536)


@potrebuje_ffmpeg
class TestVysokyDatovyTok(ZakladVzorky):
    """Video s vysokým tokom sa od šifrovaných dát štatisticky takmer nelíši.

    Preto nesmie o zachrániteľnosti rozhodovať iba test rovnomernosti — musí
    existovať aj kontrola, ktorá sa opiera o štruktúru dát.
    """

    def test_v_tele_videa_sa_najde_obraz(self):
        f, mm, size = mp4.open_mm(self.zdroj)
        try:
            self.assertTrue(_je_v_tele_video(mm, size))
        finally:
            mm.close()
            f.close()

    def test_v_nahodnych_datach_sa_obraz_nenajde(self):
        nahodny = self.out("nahodne.bin")
        with open(nahodny, "wb") as f:
            f.write(os.urandom(6 << 20))
        f, mm, size = mp4.open_mm(nahodny)
        try:
            self.assertFalse(_je_v_tele_video(mm, size))
        finally:
            mm.close()
            f.close()

    def test_zle_meranie_entropie_nezhodi_verdikt(self):
        # naschvál znefunkčníme test rovnomernosti: všetko bude vyzerať šifrovane
        povodny = carve.PRAH_CHI2
        carve.PRAH_CHI2 = 1e9
        try:
            v = rychly_verdikt(self.zdroj)
            self.assertNotEqual(v["verdikt"], "nezachranitelne",
                                "štruktúrna kontrola mala súbor zachrániť")
        finally:
            carve.PRAH_CHI2 = povodny


@potrebuje_ffmpeg
class TestDavka(ZakladVzorky):
    """Dávka musí zvládnuť priečinok, kde sú rôzne poškodené súbory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.davka_dir = os.path.join(cls.dir, "davka")
        os.makedirs(cls.davka_dir, exist_ok=True)
        # jeden s prežitým indexom, jeden faststart (index zničený)
        bezny = vyrob_video(os.path.join(cls.dir, "bezny.mp4"), sekundy=6)
        fs = vyrob_video(os.path.join(cls.dir, "fs.mp4"), sekundy=6, faststart=True)
        # ransomvér súbory premenúva, preto aj tu iná prípona
        posifruj_zaciatok(bezny, os.path.join(cls.davka_dir, "A001.MP4.locked"), 256 * 1024)
        posifruj_zaciatok(fs, os.path.join(cls.davka_dir, "A002.MP4.locked"), 256 * 1024)

    def test_najdi_videa_zoberie_aj_premenovane(self):
        subory = najdi_videa(self.davka_dir)
        self.assertEqual(len(subory), 2)
        self.assertTrue(all(c.endswith(".locked") for c in subory))

    def test_najdi_videa_prehlada_podpriecinky(self):
        # projekty bývajú rozdelené po priečinkoch podľa kamier a dní
        hlbky = os.path.join(self.davka_dir, "kamera_2", "den2")
        os.makedirs(hlbky, exist_ok=True)
        vnoreny = os.path.join(hlbky, "B001.MP4.locked")
        shutil.copy(najdi_videa(self.davka_dir)[0], vnoreny)
        try:
            plytko = najdi_videa(self.davka_dir)
            hlboko = najdi_videa(self.davka_dir, rekurzivne=True)
            self.assertNotIn(vnoreny, plytko)
            self.assertIn(vnoreny, hlboko)
        finally:
            shutil.rmtree(os.path.join(self.davka_dir, "kamera_2"))

    def test_najdi_videa_preskoci_vlastne_vysledky(self):
        vystup = os.path.join(self.davka_dir, "opravene")
        os.makedirs(vystup, exist_ok=True)
        hotovy = os.path.join(vystup, "C001_opraveny.mp4")
        shutil.copy(najdi_videa(self.davka_dir)[0], hotovy)
        try:
            self.assertNotIn(hotovy, najdi_videa(self.davka_dir, rekurzivne=True))
        finally:
            shutil.rmtree(vystup)

    def test_najdi_videa_vynecha_zadane(self):
        vsetky = najdi_videa(self.davka_dir)
        zvysok = najdi_videa(self.davka_dir, vynechaj={vsetky[0]})
        self.assertEqual(len(zvysok), len(vsetky) - 1)

    def test_davka_zvoli_postup_pre_kazdy_subor_zvlast(self):
        subory = najdi_videa(self.davka_dir)
        v = spusti_davku(subory, self.out("davka_vystup"), TB, HeaderDB(),
                         {"fps": 25}, log=lambda m: None)
        self.assertEqual(v["pocet"], 2)
        self.assertEqual(v["hotove"], 2, "oba súbory sa mali podariť")
        postupy = {r["subor"]: r["strategia"] for r in v["vysledky"]}
        self.assertEqual(postupy["A001.MP4.locked"], "mp4_graft")
        self.assertEqual(postupy["A002.MP4.locked"], "mp4_carve")

    def test_davka_sa_da_prerusit(self):
        subory = najdi_videa(self.davka_dir)
        v = spusti_davku(subory, self.out("davka_stop"), TB, HeaderDB(), {},
                         log=lambda m: None, preruseny=lambda: True)
        self.assertEqual(v["hotove"], 0, "pri prerušení sa nemá spracovať nič")

    def test_davka_prezije_chybny_subor(self):
        zly = os.path.join(self.davka_dir, "pokazeny.mp4")
        with open(zly, "wb") as f:
            f.write(b"toto nie je video" * 5000)
        try:
            v = spusti_davku(najdi_videa(self.davka_dir), self.out("davka_mix"),
                             TB, HeaderDB(), {"fps": 25}, log=lambda m: None)
            self.assertEqual(v["hotove"], 2)
            self.assertEqual(v["zlyhane"], 1)
        finally:
            os.remove(zly)


if __name__ == "__main__":
    unittest.main(verbosity=2)
