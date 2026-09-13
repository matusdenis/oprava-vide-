"""Testy programu na opravu videa.

Spustenie:  python3 -m unittest discover -s tests -v
Testy, ktore potrebuju ffmpeg, sa bez neho automaticky preskocia.
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vidfix import carve, h264_params, mp4                       # noqa: E402
from vidfix.analyze import (analyze, rychly_verdikt,             # noqa: E402
                            skontroluj_vysledok, _je_v_tele_video)
from vidfix.headerdb import HeaderDB                             # noqa: E402
from vidfix.repair import (Ctx, najdi_medzikroky, najdi_videa,   # noqa: E402
                           hotovy_vystup, over_vystup, zisti_fps,
                           _VZORY_CACHE, _zbieraj_vzory,
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

    def test_prepis_rozlisenia_zachova_ostatne_nastavenia(self):
        # zdravý súbor z tej istej kamery, ale iné rozlíšenie: všetko ostatné
        # v hlavičke musí ostať nedotknuté
        for prof in (66, 77, 100):
            sps = h264_params.build_sps(1920, 1080, profile_idc=prof,
                                        log2_max_frame_num_minus4=2,
                                        max_num_ref_frames=3)
            povodne = carve.plausible_sps(sps)
            novy = carve.patch_sps_rozlisenie(sps, 1280, 720)
            self.assertIsNotNone(novy, f"prepis zlyhal pre profil {prof}")
            upravene = carve.plausible_sps(novy)
            self.assertEqual((upravene["sirka"], upravene["vyska"]), (1280, 720))
            self.assertEqual(upravene["profile_idc"], povodne["profile_idc"])
            self.assertEqual(upravene["level_idc"], povodne["level_idc"])

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

    def test_prepis_rozlisenia_h265(self):
        ps = mp4.codec_parameter_sets(self.zdroj_h265)
        sps = carve.find_nal_in_annexb(ps, 33, hevc=True)
        povodne = carve.plausible_hevc_sps(sps)
        novy = carve.patch_sps_rozlisenie(sps, 3840, 2160, hevc=True)
        self.assertIsNotNone(novy)
        upravene = carve.plausible_hevc_sps(novy)
        self.assertEqual((upravene["sirka"], upravene["vyska"]), (3840, 2160))
        self.assertEqual(upravene["profile_idc"], povodne["profile_idc"])
        self.assertEqual(upravene["level_idc"], povodne["level_idc"])

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
        """Skúša sa viac vzoriek — jedna náhodná zhoda nesmie stačiť.

        Krátky sled NAL jednotiek sa v šifrovaných dátach z času na čas podarí;
        práve preto sa nález overuje hlbšou prechádzkou. Bez nej hlásil tento
        test obraz asi v tretine pokusov.
        """
        for i in range(6):
            nahodny = self.out(f"nahodne{i}.bin")
            with open(nahodny, "wb") as f:
                f.write(random.Random(1000 + i).randbytes(6 << 20))
            f, mm, size = mp4.open_mm(nahodny)
            try:
                self.assertFalse(_je_v_tele_video(mm, size),
                                 f"náhodné dáta #{i} sa vyhlásili za video")
            finally:
                mm.close()
                f.close()
            os.remove(nahodny)

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
class TestPokracovanieDavky(ZakladVzorky):
    """Dávka cez stovky videí beží hodiny — musí sa dať dokončiť.

    Uspanie počítača, odpojenie disku či zavretie terminálu ju prerušia;
    pri opätovnom spustení nemá zmysel robiť znova to, čo už hotové je.
    """

    def setUp(self):
        # vlastný priečinok pre každý test — inak by výsledky jedného testu
        # rozhodovali o tom, čo vidí ďalší
        meno = self.id().rsplit(".", 1)[-1]
        self.vstup = self.out(f"davka_{meno}")
        self.vystup = self.out(f"davka_{meno}_von")
        os.makedirs(self.vstup, exist_ok=True)
        os.makedirs(self.vystup, exist_ok=True)
        self.subory = [
            posifruj_zaciatok(self.zdroj, os.path.join(self.vstup, f"v{i}.mp4"),
                              256 * 1024)
            for i in range(2)
        ]

    def test_hotovy_vysledok_sa_najde(self):
        hotovy = os.path.join(self.vystup, "v0_zachraneny.mp4")
        with open(hotovy, "wb") as f:
            f.write(b"x" * (128 << 10))
        self.assertEqual(hotovy_vystup(self.subory[0], self.vystup), hotovy)
        self.assertIsNone(hotovy_vystup(self.subory[1], self.vystup))

    def test_medzikrok_sa_za_vysledok_nepovazuje(self):
        with open(os.path.join(self.vystup, "v0_vyrezany.mp4"), "wb") as f:
            f.write(b"x" * (128 << 10))
        self.assertIsNone(hotovy_vystup(self.subory[0], self.vystup))

    def test_druhy_beh_hotove_preskoci(self):
        prvy = spusti_davku(self.subory, self.vystup, TB, HeaderDB(), {},
                            log=lambda m: None)
        self.assertEqual(prvy["hotove"], 2)
        self.assertEqual(prvy["uz_hotove"], 0)

        zaznam = []
        druhy = spusti_davku(self.subory, self.vystup, TB, HeaderDB(), {},
                             log=zaznam.append)
        self.assertEqual(druhy["uz_hotove"], 2, "\n".join(zaznam))
        self.assertEqual(druhy["hotove"], 0)
        self.assertTrue(druhy["ok"])

    def test_volba_prerobit_hotove(self):
        spusti_davku(self.subory, self.vystup, TB, HeaderDB(), {},
                     log=lambda m: None)
        znova = spusti_davku(self.subory, self.vystup, TB, HeaderDB(),
                             {"prerobit_hotove": True}, log=lambda m: None)
        self.assertEqual(znova["uz_hotove"], 0)
        self.assertEqual(znova["hotove"], 2)


@potrebuje_ffmpeg
class TestPamatNaVzory(ZakladVzorky):
    """Parametre okolitých videí sú pre celý priečinok rovnaké.

    Bez zapamätania sa to isté prehľadávanie zopakuje pri každom z niekoľkých
    sto súborov — a na veľkých záznamoch to je hodina navyše.
    """

    def setUp(self):
        self.priecinok = self.out("pamat_vzory")
        os.makedirs(self.priecinok, exist_ok=True)
        self.poskodeny = posifruj_zaciatok(
            self.zdroj, os.path.join(self.priecinok, "a.mp4"), 256 * 1024)
        shutil.copy(self.zdroj, os.path.join(self.priecinok, "sused.mp4"))
        _VZORY_CACHE.clear()

    def test_druhe_volanie_uz_neprehladava(self):
        ctx = Ctx(self.poskodeny, self.out("pamat_von"), TB, HeaderDB(), {})
        prve = _zbieraj_vzory(ctx, False)
        self.assertTrue(prve, "parametre suseda sa mali nájsť")

        volane = []
        povodne = mp4.codec_parameter_sets

        def sledovane(cesta, *a, **kw):
            volane.append(cesta)
            return povodne(cesta, *a, **kw)

        mp4.codec_parameter_sets = sledovane
        try:
            druhe = _zbieraj_vzory(ctx, False)
        finally:
            mp4.codec_parameter_sets = povodne
        self.assertEqual(druhe, prve)
        self.assertEqual(volane, [], "druhé volanie nemá znova prehľadávať")

    def test_iny_vzor_ma_vlastny_zaznam(self):
        ctx = Ctx(self.poskodeny, self.out("pamat_von2"), TB, HeaderDB(), {})
        _zbieraj_vzory(ctx, False)
        iny = Ctx(self.poskodeny, self.out("pamat_von2"), TB, HeaderDB(),
                  {"vzor": self.zdroj})
        self.assertTrue(_zbieraj_vzory(iny, False))
        self.assertGreaterEqual(len(_VZORY_CACHE), 2)


@potrebuje_ffmpeg
class TestKontrolaVysledku(unittest.TestCase):
    """Hotový výsledok sa musí dať zmerať — beží plynule, alebo sa trhá?"""

    @classmethod
    def setUpClass(cls):
        if not MA_FFMPEG:
            raise unittest.SkipTest("ffmpeg nie je k dispozicii")
        cls.dir = tempfile.mkdtemp(prefix="vidfix-kontrola-")
        cls.zdravy = vyrob_video(os.path.join(cls.dir, "zdrave.mp4"), sekundy=4)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_zdrave_video_je_plynule(self):
        v = skontroluj_vysledok(self.zdravy, TB)
        self.assertTrue(v["ok"])
        self.assertTrue(v["plynule"], v.get("kde_trha"))
        self.assertEqual(v["trhnutia"], 0)
        self.assertAlmostEqual(v["fps"], 25, delta=0.5)

    def test_vypadok_v_strede_sa_odhali(self):
        """Keď v strede chýbajú snímky, kontrola to musí povedať."""
        deravy = os.path.join(self.dir, "deravy.mp4")
        r = TB.run("ffmpeg", ["-y", "-v", "error", "-i", self.zdravy,
                              "-vf", "select='not(between(n,40,70))'",
                              "-vsync", "0", "-an", deravy], log=None)
        self.assertEqual(r["code"], 0, r.get("vystup"))
        v = skontroluj_vysledok(deravy, TB)
        self.assertTrue(v["ok"])
        self.assertFalse(v["plynule"], "výpadok snímkov mal byť odhalený")
        self.assertGreater(v["trhnutia"], 0)

    def test_chybajuci_subor(self):
        v = skontroluj_vysledok(os.path.join(self.dir, "niet.mp4"), TB)
        self.assertFalse(v["ok"])


@potrebuje_ffmpeg
class TestSnimkovaFrekvencia(unittest.TestCase):
    """Vyrezaný stream časovanie neobsahuje — musí sa vziať z indexu.

    Keď sa frekvencia odhadne zle, video sa prehráva privoľna alebo prirýchlo
    a vyzerá to ako sekanie, hoci sú všetky snímky na mieste.
    """

    @classmethod
    def setUpClass(cls):
        if not MA_FFMPEG:
            raise unittest.SkipTest("ffmpeg nie je k dispozicii")
        cls.dir = tempfile.mkdtemp(prefix="vidfix-fps-")
        cls.zdravy = vyrob_video(os.path.join(cls.dir, "zdrave50.mp4"),
                                 sekundy=3, fps=50)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_frekvencia_sa_precita_z_indexu(self):
        self.assertEqual(mp4.zaokruhli_fps(mp4.snimkova_frekvencia(self.zdravy)), 50)

    def test_ntsc_sa_zaokruhli(self):
        self.assertEqual(mp4.zaokruhli_fps(29.97), 30)
        self.assertEqual(mp4.zaokruhli_fps(59.94), 60)
        self.assertIsNone(mp4.zaokruhli_fps(None))

    def test_poskodenemu_suboru_pomoze_vzor(self):
        """Súbor bez indexu si frekvenciu vezme zo vzoru z tej istej kamery."""
        poskodeny = posifruj_zaciatok(
            vyrob_video(os.path.join(self.dir, "fs50.mp4"), sekundy=3, fps=50,
                        faststart=True),
            os.path.join(self.dir, "fs50_poskodene.mp4"), 256 * 1024)
        ctx = Ctx(poskodeny, os.path.join(self.dir, "von"), TB, HeaderDB(),
                  {"vzor": self.zdravy})
        self.assertEqual(zisti_fps(ctx), 50)

    def test_volba_pouzivatela_ma_prednost(self):
        ctx = Ctx(self.zdravy, os.path.join(self.dir, "von2"), TB, HeaderDB(),
                  {"fps": 24, "vzor": self.zdravy})
        self.assertEqual(zisti_fps(ctx), 24)


class TestVystupnyPriecinok(unittest.TestCase):
    """Disk pripojený len na čítanie sa musí ohlásiť hneď, nie 339-krát."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vidfix-vystup-")

    def tearDown(self):
        os.chmod(self.dir, 0o755)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_zapisovatelny_priecinok_prejde(self):
        over_vystup(os.path.join(self.dir, "opravene"))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "opravene")))

    @unittest.skipIf(os.geteuid() == 0, "root zapíše aj do chráneného priečinka")
    def test_chraneny_priecinok_sa_ohlasi(self):
        chraneny = os.path.join(self.dir, "bez_prav")
        os.makedirs(chraneny)
        os.chmod(chraneny, 0o500)
        with self.assertRaises(RuntimeError) as ctx:
            over_vystup(chraneny)
        self.assertIn("nedá zapisovať", str(ctx.exception))

    def test_malo_miesta_sa_ohlasi(self):
        with self.assertRaises(RuntimeError) as ctx:
            over_vystup(self.dir, potrebne=1 << 60)
        self.assertIn("voľných", str(ctx.exception))

    def test_davka_sa_nespusti_ked_sa_neda_zapisovat(self):
        """Dávka musí spadnúť pred prvým súborom, nie po každom zvlášť."""
        volane = []

        def log(m):
            volane.append(m)

        # priečinok sa nedá vytvoriť: v ceste je obyčajný súbor
        prekazka = os.path.join(self.dir, "toto_je_subor")
        with open(prekazka, "wb") as f:
            f.write(b"x")
        with self.assertRaises(RuntimeError):
            spusti_davku(["/neexistuje/a.mov"], os.path.join(prekazka, "von"),
                         Toolbox(), HeaderDB(), {}, log)
        self.assertEqual(volane, [], "dávka nesmie začať spracovávať súbory")


class TestKotvySnimkov(unittest.TestCase):
    """Kamery zapisujú pred každý snímok krátky oddeľovač.

    Je to najspoľahlivejší znak skutočného začiatku obrazu: v zašifrovaných
    dátach je šanca na náhodnú zhodu zanedbateľná, kým kontrolu štruktúry
    náhodné dáta občas prejdú.
    """

    def _mm(self, data: bytes):
        cesta = os.path.join(self.dir, "vzorka.bin")
        with open(cesta, "wb") as f:
            f.write(data)
        return mp4.open_mm(cesta)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vidfix-kotvy-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_oddelovac_snimku_sa_najde(self):
        # [dĺžka 2][AUD] a za ním normálny rez — presne ako to píše kamera
        rez = b"\x65" + b"\x88" * 4000
        data = (os.urandom(1 << 20)
                + b"\x00\x00\x00\x02\x09\x10"
                + len(rez).to_bytes(4, "big") + rez) * 1
        data += (b"\x00\x00\x00\x02\x09\x30"
                 + len(rez).to_bytes(4, "big") + rez) * 8
        f, mm, size = self._mm(data)
        try:
            kotvy = carve.kotvy_offsety(mm, size)
            self.assertEqual(kotvy[0], 1 << 20)
            self.assertEqual(len(kotvy), 9)
        finally:
            mm.close()
            f.close()

    def test_klucovy_snimok_sa_rozpozna(self):
        """Vyrezávanie musí začať kľúčovým snímkom, nie hocijakým."""
        def jednotka(typ, dlzka=3000):
            telo = bytes([typ]) + b"\x88" * dlzka
            return (b"\x00\x00\x00\x02\x09\x10"
                    + len(telo).to_bytes(4, "big") + telo)

        medzi = jednotka(0x65)      # IDR (typ 5)
        bezny = jednotka(0x41)      # bežný rez (typ 1)
        f, mm, size = self._mm(bezny + medzi + bezny)
        try:
            self.assertFalse(carve.je_klucovy(mm, size, 0, False))
            self.assertTrue(carve.je_klucovy(mm, size, len(bezny), False))
            self.assertFalse(carve.je_klucovy(mm, size, len(bezny) * 2, False))
        finally:
            mm.close()
            f.close()

    def test_v_tichom_zvuku_takmer_ziadne_kotvy(self):
        """Nekódovaný zvuk je plný núl — samotný vzor bajtov nestačí.

        Pri niekoľkogigabajtovom zázname sa päťbajtová zhoda v tichom PCM
        trafí tisíckrát. Falošná kotva by orezala skutočný snímok a zanechala
        v obraze chybu, preto sa overuje aj obsah jednotky.
        """
        import re as _re
        r = random.Random(4)
        pcm = bytearray()
        while len(pcm) < (24 << 20):
            pcm += b"\x00" if r.random() < 0.75 else bytes([r.getrandbits(8)])
        f, mm, size = self._mm(bytes(pcm))
        try:
            hrube = sum(len(_re.findall(v, mm, _re.S)) for _h, v, _p in carve.KOTVY)
            self.assertGreater(hrube, 100, "vzorka má obsahovať dosť zhôd")
            kotvy = carve.kotvy_offsety(mm, size)
            self.assertLess(len(kotvy), hrube / 50,
                            f"overenie obsahu má zhody odfiltrovať "
                            f"({len(kotvy)} z {hrube})")
        finally:
            mm.close()
            f.close()

    def test_v_nahodnych_datach_ziadne_kotvy(self):
        f, mm, size = self._mm(os.urandom(8 << 20))
        try:
            self.assertEqual(carve.kotvy_offsety(mm, size), [])
        finally:
            mm.close()
            f.close()

    def test_vyrezavanie_sa_chyti_dalsieho_snimku(self):
        """Za obrazom býva kus zvuku — bez kotiev sa stream po ňom rozpadne."""
        rez = b"\x65" + b"\x88" * 4000
        snimok = b"\x00\x00\x00\x02\x09\x10" + len(rez).to_bytes(4, "big") + rez
        zvuk = os.urandom(3000)          # PCM medzi snímkami, nie NAL jednotka
        data = b"".join(snimok + zvuk for _ in range(12))
        f, mm, size = self._mm(data)
        try:
            kotvy = carve.kotvy_offsety(mm, size)
            self.assertEqual(len(kotvy), 12)
            dst = os.path.join(self.dir, "von.h264")
            stat = carve.extract_annexb(mm, 0, size, dst, max_len=4 << 20,
                                        kotvy=kotvy)
            # Dva NAL na snímok (oddeľovač + rez). Posledný snímok sa zahadzuje
            # zámerne — na konci súboru býva useknutý a v prehrávači po ňom
            # ostane trhnutie. Ostatných jedenásť musí prejsť celých.
            self.assertEqual(stat["nals"], 22)
            self.assertEqual(stat["snimky"], 11)
        finally:
            mm.close()
            f.close()


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

    def test_davka_preskoci_nezachranitelny_subor(self):
        # súbor bez čohokoľvek použiteľného nesmie dávku zhodiť ani sa hlásiť
        # ako chyba — jednoducho sa preskočí
        zly = os.path.join(self.davka_dir, "zasifrovany.MOV.locked")
        with open(zly, "wb") as f:
            f.write(os.urandom(3 << 20))
        try:
            v = spusti_davku(najdi_videa(self.davka_dir), self.out("davka_skip"),
                             TB, HeaderDB(), {"fps": 25}, log=lambda m: None)
            preskocene = [r for r in v["vysledky"] if r.get("preskocene")]
            self.assertEqual(len(preskocene), 1)
            self.assertEqual(v["zlyhane"], 0, "preskočenie nie je chyba")
            self.assertEqual(v["hotove"], 2)
        finally:
            os.remove(zly)

    def test_plna_analyza_suhlasi_s_rychlym_triedenim(self):
        # obe cesty musia dať rovnaký záver, inak dávka pýta postup, ktorý
        # rýchle triedenie vôbec nenavrhlo
        for cesta in najdi_videa(self.davka_dir):
            rychly = rychly_verdikt(cesta)
            plna = analyze(cesta, HeaderDB(), TB)
            navrh = (plna.get("strategie") or [{}])[0].get("id")
            if rychly["verdikt"] == "nezachranitelne":
                self.assertEqual(navrh, "nezachranitelne", cesta)
            else:
                self.assertNotEqual(navrh, "nezachranitelne", cesta)

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
