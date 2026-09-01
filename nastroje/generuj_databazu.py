#!/usr/bin/env python3
"""Vygeneruje data/headers.json - databazu hlaviciek kontajnerov.

Bajty boxov `ftyp` sa skladaju programovo podla ISO/IEC 14496-12, takze su
zarucene platne. Zoznam znaciek (brands) pochadza z verejneho registra MP4RA,
podpisy kontajnerov z verejnych zoznamov signatur suborov.

Spustenie:  python3 nastroje/generuj_databazu.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vidfix.mp4 import ftyp_box  # noqa: E402

ZDROJE = [
    {"id": "mp4ra", "nazov": "MP4RA - register znaciek a typov boxov",
     "url": "https://mp4ra.org/registered-types/brands",
     "popis": "Oficialny register znaciek (brands) a typov boxov pre ISO BMFF."},
    {"id": "iso14496-12", "nazov": "ISO/IEC 14496-12 (ISO Base Media File Format)",
     "url": "https://www.iso.org/standard/83102.html",
     "popis": "Norma popisujuca strukturu boxov MP4/MOV vratane ftyp, moov, mdat."},
    {"id": "qtff", "nazov": "Apple QuickTime File Format Specification",
     "url": "https://developer.apple.com/library/archive/documentation/QuickTime/QTFF/",
     "popis": "Popis atomov QuickTime (.mov), z ktorych MP4 vychadza."},
    {"id": "matroska", "nazov": "Matroska / WebM specifikacia",
     "url": "https://www.matroska.org/technical/elements.html",
     "popis": "EBML elementy kontajnera MKV/WebM."},
    {"id": "riff-avi", "nazov": "OpenDML AVI / RIFF specifikacia",
     "url": "https://learn.microsoft.com/en-us/windows/win32/directshow/avi-riff-file-reference",
     "popis": "Struktura RIFF/AVI vratane hdrl, movi a idx1."},
    {"id": "mpegts", "nazov": "ISO/IEC 13818-1 (MPEG-2 Transport Stream)",
     "url": "https://www.iso.org/standard/75928.html",
     "popis": "Paketovy format TS/M2TS - 188 B pakety so synchronizacnym bajtom 0x47."},
    {"id": "sigs", "nazov": "Verejne zoznamy signatur suborov",
     "url": "https://en.wikipedia.org/wiki/List_of_file_signatures",
     "popis": "Prehlad magickych bajtov na zaciatku suborov (aj Gary Kessler File Signature Table)."},
    {"id": "nomoreransom", "nazov": "No More Ransom",
     "url": "https://www.nomoreransom.org/",
     "popis": "Verejny projekt europolu - identifikacia ransomveru a dostupne desifrovace."},
    {"id": "idransomware", "nazov": "ID Ransomware",
     "url": "https://id-ransomware.malwarehunterteam.com/",
     "popis": "Sluzba na urcenie rodiny ransomveru podla pripony a vykupneho listu."},
]

# ---------------------------------------------------------------------------
# Znacky ftyp (MP4RA). "popis" hovori, kde sa znacka bezne vyskytuje.
# ---------------------------------------------------------------------------
ZNACKY = [
    ("isom", "Zakladna znacka ISO Base Media File Format (najvseobecnejsia).", "mp4ra"),
    ("iso2", "ISO BMFF druhej generacie, bezna vo vystupe ffmpeg.", "mp4ra"),
    ("iso4", "ISO BMFF, verzia 4.", "mp4ra"),
    ("iso5", "ISO BMFF, verzia 5.", "mp4ra"),
    ("iso6", "ISO BMFF, verzia 6 (casto fragmentovane MP4).", "mp4ra"),
    ("mp41", "MPEG-4 verzia 1.", "mp4ra"),
    ("mp42", "MPEG-4 verzia 2 - najbeznejsia pre kamery a telefony.", "mp4ra"),
    ("avc1", "MP4 s videom H.264/AVC.", "mp4ra"),
    ("mmp4", "Mobilny MP4 profil.", "mp4ra"),
    ("M4V ", "Video pre Apple iTunes/QuickTime.", "mp4ra"),
    ("M4A ", "Zvuk pre Apple iTunes.", "mp4ra"),
    ("qt  ", "Apple QuickTime (.mov) - pouzivaju aj mnohe fotoaparaty.", "qtff"),
    ("3gp4", "3GPP, verzia 4 (starsie telefony).", "mp4ra"),
    ("3gp5", "3GPP, verzia 5.", "mp4ra"),
    ("3gp6", "3GPP, verzia 6.", "mp4ra"),
    ("3g2a", "3GPP2.", "mp4ra"),
    ("MSNV", "Profil pouzivany zariadeniami Sony (napr. Handycam, PSP).", "mp4ra"),
    ("dash", "Fragmentovane MP4 pre streamovanie MPEG-DASH.", "mp4ra"),
    ("msdh", "Fragmentovane MP4, segment DASH.", "mp4ra"),
    ("hvc1", "MP4 s videom H.265/HEVC (uzavrete GOP).", "mp4ra"),
    ("heic", "Obrazovy format HEIF (rovnaka struktura boxov).", "mp4ra"),
]

# Sablony hlaviciek - poskladane programovo, teda zarucene platne.
SABLONY = []


def pridaj_sablonu(sid, major, minor, compat, kontajner, popis, pouzitie, zdroj="iso14496-12"):
    data = ftyp_box(major.encode("latin-1"), minor,
                    tuple(c.encode("latin-1") for c in compat))
    SABLONY.append({
        "id": sid,
        "kontajner": kontajner,
        "popis": popis,
        "hex": data.hex(),
        "dlzka": len(data),
        "major_brand": major,
        "minor_version": minor,
        "compatible_brands": list(compat),
        "pouzitie": pouzitie,
        "zdroj": zdroj,
    })


pridaj_sablonu("mp4_isom_univerzalny", "isom", 512,
               ["isom", "iso2", "avc1", "mp41"], "mp4",
               "Univerzalna hlavicka MP4 - presne taku zapisuje ffmpeg.",
               "Prvá volba pre H.264 vo vacsine pripadov.")
pridaj_sablonu("mp4_mp42_kamery", "mp42", 0,
               ["mp42", "isom", "avc1"], "mp4",
               "Hlavicka bezna pre videa z telefonov a fotoaparatov.",
               "Ked bol povodny subor z mobilu alebo fotoaparatu.")
pridaj_sablonu("mp4_hevc", "isom", 512,
               ["isom", "iso2", "hvc1", "mp41"], "mp4",
               "MP4 s videom H.265/HEVC.",
               "Ked stopa pouziva kodek hvc1/hev1 (novsie iPhony, drony, 4K kamery).")
pridaj_sablonu("mov_quicktime", "qt  ", 512,
               ["qt  "], "mov",
               "QuickTime .mov - Apple, Canon, Nikon, Blackmagic.",
               "Ked povodny subor mal priponu .mov.")
pridaj_sablonu("mp4_3gp5_telefon", "3gp5", 512,
               ["3gp5", "isom", "3gp4"], "3gp",
               "Starsie mobilne video 3GPP.",
               "Subory .3gp zo starsich telefonov.")
pridaj_sablonu("mp4_sony_msnv", "MSNV", 289,
               ["MSNV", "mp42", "isom"], "mp4",
               "Profil zariadeni Sony.",
               "Videa z kamier a telefonov Sony.")
pridaj_sablonu("mp4_dash_fragmentovany", "iso6", 512,
               ["iso6", "isom", "iso2", "avc1", "mp41", "dash"], "mp4",
               "Fragmentovane MP4 (moof/mdat).",
               "Ked subor obsahuje boxy moof namiesto jedneho moov.")
pridaj_sablonu("m4v_apple", "M4V ", 1,
               ["M4V ", "M4A ", "mp42", "isom"], "m4v",
               "Apple M4V.", "Subory .m4v.")

KONTAJNERY = [
    {"id": "mp4", "nazov": "MP4 / MOV (ISO Base Media)",
     "pripony": [".mp4", ".mov", ".m4v", ".3gp", ".m4a", ".mp4v"],
     "podpisy": [{"offset": 4, "hex": "66747970", "popis": "'ftyp' na offsete 4"},
                 {"offset": 4, "hex": "6d6f6f76", "popis": "'moov' - index"},
                 {"offset": 4, "hex": "6d646174", "popis": "'mdat' - media data"}],
     "index": "moov",
     "index_na_konci": True,
     "poznamka": ("Kamery, telefony a drony zapisuju index moov az na koniec suboru. "
                  "Ak ransomver zasifroval len zaciatok, index prezije a subor sa da "
                  "opravit uplne bez zdraveho vzoru."),
     "zdroj": "iso14496-12"},
    {"id": "mpegts", "nazov": "MPEG-TS / M2TS / AVCHD",
     "pripony": [".ts", ".m2ts", ".mts", ".m2t"],
     "podpisy": [{"offset": 0, "hex": "47", "popis": "synchronizacny bajt 0x47 kazdych 188 B"},
                 {"offset": 4, "hex": "47", "popis": "M2TS - 4 B casova znacka + 188 B paket"}],
     "index": "ziadny",
     "index_na_konci": False,
     "poznamka": ("Format sa synchronizuje sam - staci zahodit poskodeny zaciatok a "
                  "nacitat od prveho platneho paketu. Uspesnost byva takmer 100 %."),
     "zdroj": "mpegts"},
    {"id": "avi", "nazov": "AVI (RIFF)",
     "pripony": [".avi"],
     "podpisy": [{"offset": 0, "hex": "52494646", "popis": "'RIFF'"},
                 {"offset": 8, "hex": "41564920", "popis": "'AVI '"}],
     "index": "idx1",
     "index_na_konci": True,
     "poznamka": ("Hlavicka hdrl je na zaciatku (byva zasifrovana), ale index idx1 na konci "
                  "prezije a offsety v nom su relativne voci zoznamu movi."),
     "zdroj": "riff-avi"},
    {"id": "matroska", "nazov": "Matroska / WebM",
     "pripony": [".mkv", ".webm"],
     "podpisy": [{"offset": 0, "hex": "1a45dfa3", "popis": "EBML hlavicka"}],
     "index": "Cues",
     "index_na_konci": True,
     "poznamka": ("Zaciatok obsahuje EBML hlavicku a popis stop (CodecPrivate). Ak je "
                  "zasifrovany, treba vyrezat surovy stream z Clusterov."),
     "zdroj": "matroska"},
    {"id": "asf", "nazov": "ASF / WMV",
     "pripony": [".wmv", ".asf"],
     "podpisy": [{"offset": 0, "hex": "3026b2758e66cf11a6d900aa0062ce6c",
                  "popis": "ASF Header Object GUID"}],
     "index": "Index Object",
     "index_na_konci": True,
     "poznamka": "Hlavicka na zaciatku, index na konci.",
     "zdroj": "sigs"},
    {"id": "flv", "nazov": "Flash Video",
     "pripony": [".flv"],
     "podpisy": [{"offset": 0, "hex": "464c5601", "popis": "'FLV' + verzia"}],
     "index": "onMetaData",
     "index_na_konci": False,
     "poznamka": "Samosynchronizujuce tagy - da sa nacitat od prveho platneho tagu.",
     "zdroj": "sigs"},
    {"id": "mpegps", "nazov": "MPEG Program Stream (VOB, MPG)",
     "pripony": [".mpg", ".mpeg", ".vob"],
     "podpisy": [{"offset": 0, "hex": "000001ba", "popis": "Pack header"}],
     "index": "ziadny",
     "index_na_konci": False,
     "poznamka": "Samosynchronizujuci sa format - staci najst prvy platny pack header.",
     "zdroj": "sigs"},
]

# Orientacne profily zariadeni. Obsah ftyp je pre prehratelnost zamenitelny,
# preto ide o odporucanie, nie o presnu identifikaciu zariadenia.
ZARIADENIA = [
    {"id": "iphone_mov", "vyrobca": "Apple", "popis": "iPhone / iPad, subory .MOV",
     "kontajner": "mov", "sablona": "mov_quicktime", "kodek": "h264 alebo hevc",
     "spolahlivost": "orientacne"},
    {"id": "android_mp4", "vyrobca": "Android (vseobecne)", "popis": "Telefony s Androidom",
     "kontajner": "mp4", "sablona": "mp4_mp42_kamery", "kodek": "h264",
     "spolahlivost": "orientacne"},
    {"id": "gopro_mp4", "vyrobca": "GoPro", "popis": "Akcne kamery GoPro",
     "kontajner": "mp4", "sablona": "mp4_isom_univerzalny", "kodek": "h264 alebo hevc",
     "spolahlivost": "orientacne"},
    {"id": "dji_mp4", "vyrobca": "DJI", "popis": "Drony a gimbaly DJI",
     "kontajner": "mp4", "sablona": "mp4_isom_univerzalny", "kodek": "h264 alebo hevc",
     "spolahlivost": "orientacne"},
    {"id": "sony_mp4", "vyrobca": "Sony", "popis": "Fotoaparaty a kamery Sony",
     "kontajner": "mp4", "sablona": "mp4_sony_msnv", "kodek": "h264",
     "spolahlivost": "orientacne"},
    {"id": "canon_mov", "vyrobca": "Canon", "popis": "Zrkadlovky a bezzrkadlovky Canon",
     "kontajner": "mov", "sablona": "mov_quicktime", "kodek": "h264 alebo hevc",
     "spolahlivost": "orientacne"},
    {"id": "avchd_kamera", "vyrobca": "Panasonic / Sony / Canon", "popis": "AVCHD kamery (.MTS/.M2TS)",
     "kontajner": "mpegts", "sablona": None, "kodek": "h264",
     "spolahlivost": "orientacne"},
]

# Kandidatske dlzky zasifrovaneho zaciatku. Program ich pouziva LEN ako
# zalozny plan, ked sa hranicu nepodari zmerat priamo z dat.
RANSOMVER = {
    "poznamka": ("Program hranicu poskodenia najprv MERIA priamo v subore (overuje "
                 "platnost NAL jednotiek podla tabuliek stsc/stsz). Tieto hodnoty su "
                 "len zalozne kandidatske dlzky, ked sa merat neda."),
    "kandidatske_dlzky": [
        4096, 8192, 16384, 32768, 65536, 131072, 150000, 262144, 393216,
        524288, 1048576, 1572864, 2097152, 3145728, 4194304, 5242880,
        8388608, 10485760,
    ],
    "vzory": [
        {"id": "prefix", "nazov": "Sifrovanie zaciatku suboru",
         "popis": ("Najbeznejsi pristup pri velkych suboroch - zasifruje sa len prvych "
                   "N bajtov (typicky 64 KiB az niekolko MiB), aby bolo sifrovanie rychle."),
         "dosledok": "Index moov na konci suboru prezije - oprava byva uplna.",
         "zdroj": "nomoreransom"},
        {"id": "intermitentne", "nazov": "Skakave (intermitentne) sifrovanie",
         "popis": "Sifruje sa kazdy N-ty blok, medzi nimi ostavaju povodne data.",
         "dosledok": ("Poskodenie nie je suvisle. Program zostavi mapu poskodenych usekov "
                      "a oznaci casy, ktore sa neda zachranit."),
         "zdroj": "idransomware"},
        {"id": "percentualne", "nazov": "Percentualne sifrovanie",
         "popis": "Zasifruje sa urcite percento suboru od zaciatku (napr. 10 %).",
         "dosledok": "Rovnaky postup ako pri sifrovani zaciatku, len je hranica vacsia.",
         "zdroj": "idransomware"},
        {"id": "cely_subor", "nazov": "Sifrovanie celeho suboru",
         "popis": "Cely obsah je zasifrovany, subor nema ziadnu neporusenu cast.",
         "dosledok": ("Data sa bez desifrovacieho kluca zachranit neda. Skus vyhladat "
                      "priponu na No More Ransom - pre niektore rodiny existuje desifrovac."),
         "zdroj": "nomoreransom"},
        {"id": "znacka_na_konci", "nazov": "Pripojena znacka na konci suboru",
         "popis": ("Mnohy ransomver pripaja na koniec suboru vlastnu hlavicku s ID obete "
                   "alebo zasifrovanym klucom."),
         "dosledok": "Program tieto bajty najde a pri oprave ich ignoruje.",
         "zdroj": "idransomware"},
    ],
}


def main():
    db = {
        "verzia": "1.0",
        "aktualizovane": date.today().isoformat(),
        "popis": ("Databaza hlaviciek kontajnerov zostavena z verejnych specifikacii a "
                  "registrov. Bajty boxov ftyp su poskladane programovo podla "
                  "ISO/IEC 14496-12, takze su zarucene platne."),
        "zdroje": ZDROJE,
        "kontajnery": KONTAJNERY,
        "ftyp_znacky": [{"znacka": z, "popis": p, "zdroj": s} for z, p, s in ZNACKY],
        "hlavicky": SABLONY,
        "zariadenia": ZARIADENIA,
        "ransomver": RANSOMVER,
    }
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "data", "headers.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"Zapisane: {out}  ({len(SABLONY)} hlaviciek, {len(KONTAJNERY)} kontajnerov)")


if __name__ == "__main__":
    main()
