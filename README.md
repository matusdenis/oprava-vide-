# Oprava videa poškodeného ransomvérom

Program na záchranu videí, ktorým ransomvér prepísal začiatok súboru.
Beží lokálne, má jednoduché grafické rozhranie v prehliadači a **nepotrebuje
žiadny zdravý vzorový súbor** — chýbajúce hlavičky si berie z vlastnej databázy
zostavenej podľa verejných špecifikácií, prípadne si ich dopočíta zo samotných dát.

```
python3 vidfix.py
```

---

## Prečo sa dá video zachrániť

Ransomvér pri veľkých súboroch takmer nikdy nešifruje celý obsah — bolo by to
príliš pomalé. Prepíše len začiatok (typicky 64 KiB až niekoľko MiB) a zvyšok
nechá tak. A práve to je príležitosť:

**Kamery, telefóny a drony zapisujú index videa (`moov`) až na KONIEC súboru.**
Ak ransomvér zašifroval len začiatok, index s kompletnými tabuľkami vzoriek
prežil. Vtedy sa dá video obnoviť celé, okrem tej časti, ktorú naozaj prepísal.

Háčik: offsety v tabuľkách `stco`/`co64` sú **absolútne pozície v súbore**.
Preto sa poškodený začiatok nesmie odstrániť — musí sa nahradiť **rovnako dlhou**
platnou hlavičkou, aby všetko ostatné zostalo na svojom mieste. Presne to
program robí.

```
PÔVODNÝ SÚBOR      [ftyp][mdat: obrazové a zvukové dáta .......][moov: index]
PO RANSOMVÉRI      [######## šifrované ########][ ...dáta... ...][moov: index]  ← index prežil
PO OPRAVE          [ftyp][mdat(64-bit) ........][ ...dáta... ...][moov: index]
                   └─ presne rovnako dlhé, offsety v indexe naďalej sedia ─┘
```

## Čo program vie

| Situácia | Postup | Výsledok |
|---|---|---|
| MP4/MOV, index `moov` na konci prežil | nahradenie hlavičky z databázy | **obraz aj zvuk**, chýba len skutočne prepísaný začiatok |
| MP4/MOV typu *faststart*, index zničený | vyrezanie obrazu + rekonštrukcia parametrov skúšaním | obraz (zvuk sa bez indexu priradiť nedá) |
| MTS/M2TS/TS (AVCHD, kamery) | znovunájdenie paketov + doplnenie parametrov | obraz aj zvuk |
| Ľahké poškodenie | priame prebalenie cez ffmpeg | celý súbor |
| Máš ďalšie videá z tej istej kamery (**aj poškodené**) | prevzatie parametrov z ich hlavičiek | najlepšia možná presnosť |

Program navyše:

* **zmeria, kde presne končí zašifrovaná časť** — nehádže sa to podľa tabuliek,
  ale overuje sa priamo v dátach (platnosť NAL jednotiek podľa tabuliek
  `stsc`/`stsz`, plus štatistika podielu nulových bajtov);
* **odhalí „skákavé“ (intermitentné) šifrovanie**, keď poškodenie nie je súvislé,
  a ukáže mapu poškodených úsekov;
* **nájde údaje pripojené ransomvérom na koniec súboru** a pri oprave ich ignoruje;
* **nikdy nemení pôvodný súbor** — všetko zapisuje do nového;
* na záver **overí výsledok skutočným dekódovaním** cez ffmpeg a ukáže náhľad snímku.

## Inštalácia

Potrebný je Python 3.10+ a **ffmpeg**. Nič iné sa neinštaluje — program používa
iba štandardnú knižnicu Pythonu.

1. **ffmpeg**: stiahni z [ffmpeg.org/download](https://ffmpeg.org/download.html),
   rozbaľ a buď ho pridaj do systémovej cesty `PATH`, alebo súbory `ffmpeg`
   a `ffprobe` (na Windows `ffmpeg.exe`, `ffprobe.exe`) skopíruj do priečinka
   `bin/` vedľa programu. Cestu sa dá zadať aj priamo v rozhraní v záložke
   *Nástroje*.
2. **untrunc** (nepovinný, len pre jednu zo stratégií):
   [github.com/anthwlock/untrunc](https://github.com/anthwlock/untrunc).

## Použitie

### Grafické rozhranie

```
python3 vidfix.py
```

Otvorí sa prehliadač s rozhraním na `http://127.0.0.1:8765`. Server počúva len
na `127.0.0.1` a každú požiadavku chráni náhodný token vygenerovaný pri štarte,
takže sa k nemu z iného počítača pripojiť nedá.

Postup je v troch krokoch: **1 · Súbor** → **2 · Analýza** → **3 · Oprava**.
Analýza ukáže, čo v súbore prežilo, kde končí zašifrovaná časť a ktorý postup
sa na daný súbor hodí najlepšie. Cesty nikde nemusíš vypisovať — súbor,
výstupný priečinok aj vzorové video sa vyberajú klikaním.

### Dávková oprava

Keď prvé video dopadne dobre, tlačidlom **„Opraviť rovnako aj ostatné videá…“**
sa tie isté nastavenia použijú na celý priečinok. Postup sa pritom pre každý
súbor volí zvlášť — v jednom priečinku bývajú aj súbory s prežitým indexom
(stačí nahradiť hlavičku), aj také, ktorým index neprežil (treba vyrezávať).
Priebeh vidno po jednotlivých súboroch, dávka sa dá kedykoľvek prerušiť
a súbor, ktorý zlyhá, ju nezastaví — pokračuje sa ďalším.

Z príkazového riadka to isté urobí:

```bash
python3 vidfix.py davka ~/videa -o ~/opravene
```

### Príkazový riadok

```bash
python3 vidfix.py analyza  dovolenka.mp4.locked          # diagnostika
python3 vidfix.py analyza  dovolenka.mp4.locked --json   # strojovo čitateľný výstup
python3 vidfix.py oprav    dovolenka.mp4.locked          # zvolí najvhodnejší postup
python3 vidfix.py oprav    video.mp4 -s mp4_carve --sirka 1920 --vyska 1080
python3 vidfix.py nastroje                               # čo sa našlo v systéme
```

Užitočné prepínače príkazu `oprav`:

| Prepínač | Význam |
|---|---|
| `-o PRIEČINOK` | kam uložiť výsledky (predvolene `opravene/` vedľa súboru) |
| `-s STRATÉGIA` | `mp4_graft`, `mp4_carve`, `ts_resync`, `untrunc`, `ffmpeg_remux` |
| `--sirka`, `--vyska` | rozlíšenie pôvodného videa — ak ho poznáš, hľadanie parametrov je rádovo rýchlejšie (v rozhraní sú na to prednastavené tlačidlá: 4K, 2.7K, Full HD, zvislé…) |
| `--vzor SÚBOR` | zdravý súbor z rovnakého zariadenia (aj úplne iné, krátke video) |
| `--dokladne` | dôkladnejšie (a pomalšie) hľadanie parametrov obrazu |
| `--bez-orezania` | nevyrábať čistú verziu bez poškodeného začiatku |

## Databáza hlavičiek

Súbor `data/headers.json` obsahuje:

* **podpisy kontajnerov** — MP4/MOV, MPEG-TS/M2TS, AVI, Matroska, ASF/WMV, FLV, MPEG-PS;
* **register značiek `ftyp`** podľa [MP4RA](https://mp4ra.org/registered-types/brands);
* **hotové hlavičky na graftovanie** — bajty boxov `ftyp` sú poskladané
  programovo podľa ISO/IEC 14496-12, takže sú zaručene platné;
* **orientačné profily zariadení** (telefón, GoPro, DJI, Sony, Canon, AVCHD kamery);
* **známe spôsoby šifrovania** a záložné kandidátske dĺžky zašifrovaného začiatku.

Databáza sa dá znovu vygenerovať:

```bash
python3 nastroje/generuj_databazu.py
```

**Vlastné hlavičky.** Ak máš k dispozícii hoci len jeden zdravý súbor z toho
istého zariadenia — pokojne iné, krátke video — pridaj si jeho hlavičku
v záložke *Databáza hlavičiek*. Uloží sa do `data/headers_user.json` a bude
najpresnejšou možnou náhradou. Pri stratégii vyrezávania sa z takého súboru dajú
prevziať aj parametre obrazu (`avcC`), čím sa rekonštrukcia zmení z hádania na istotu.

### Zdroje

Databáza je zostavená z verejne dostupných špecifikácií a registrov:
MP4RA (register značiek), ISO/IEC 14496-12 (ISO Base Media File Format),
Apple QuickTime File Format Specification, Matroska/WebM specifikácia,
OpenDML AVI/RIFF referencia, ISO/IEC 13818-1 (MPEG-2 TS), verejné zoznamy
signatúr súborov, No More Ransom a ID Ransomware. Úplný zoznam s odkazmi je
v rozhraní v záložke *Databáza hlavičiek*.

## Ako to funguje vnútri

```
vidfix.py              spúšťač: grafické rozhranie alebo príkazový riadok
vidfix/
  mp4.py               parser boxov ISO BMFF, tabuľky vzoriek, stavba novej hlavičky,
                       meranie hranice poškodenia a mapa poškodených úsekov
  carve.py             vyrezávanie surových dát: MPEG-TS, rozbaľovanie TS paketov,
                       prevod vzoriek na Annex-B, štatistický odhad šifrovanej časti
  h264_params.py       skladanie SPS/PPS a hľadanie správnych parametrov skúšaním
  headerdb.py          databáza hlavičiek vrátane vlastných
  analyze.py           diagnostika a návrh postupov
  repair.py            jednotlivé opravné stratégie
  tools.py             hľadanie a spúšťanie ffmpeg / ffprobe / untrunc
  jobs.py, server.py   úlohy na pozadí a lokálne webové rozhranie
  webui/               rozhranie (HTML, CSS, JavaScript — bez externých knižníc)
data/headers.json      databáza hlavičiek
nastroje/              generátor databázy
tests/                 testy (vyrobia si vlastné vzorky a poškodia ich)
```

### Ako sa hľadá hranica poškodenia

Nie odhadom, ale meraním — a to dvoma nezávislými spôsobmi:

1. **Podľa indexu (presné).** Ak `moov` prežil, poznáme presné pozície a veľkosti
   všetkých vzoriek. Pre každý úsek sa overí, či ho NAL jednotky presne
   „vydláždia“ — súčet ich dĺžok musí presne sedieť s veľkosťou vzorky
   v tabuľke `stsz`. Zašifrované dáta to nesplnia prakticky nikdy. Hranica sa
   nájde polením intervalu a potvrdí sa na niekoľkých nasledujúcich úsekoch.
2. **Štatisticky (aj bez indexu).** Šifrované dáta sú dokonale rovnomerné —
   každá hodnota bajtu je rovnako pravdepodobná. Chí-kvadrát test rovnomernosti
   na 256 KiB bloku preto vyjde okolo 255 (± 23) bez ohľadu na veľkosť bloku,
   kým komprimované video rovnomerné nie je nikdy a jeho odchýlka s veľkosťou
   bloku rastie:

   | | chí-kvadrát na 256 KiB |
   |---|---|
   | zašifrované dáta | 230 – 275 |
   | video H.265 (najhustejší prípad) | 400 – 1 000 |
   | video H.264 | 50 000 a viac |

   Koniec zašifrovanej časti sa tak dá určiť spoľahlivo aj vtedy, keď v súbore
   neprežilo vôbec nič zo štruktúry.

### Ako sa rekonštruujú stratené parametre obrazu

Keď je zničený index, chýbajú aj SPS/PPS — bez nich dekodér nevie ani to, aké
je video veľké. Program postupuje od najistejšieho k najmenej istému:

Kodek pritom nie je vopred známy (bol zapísaný v zničenom indexe), preto sa
súbor prehľadá pre **H.264 aj H.265** a vyhrá ten, ktorý dá dlhší a súvislejší
stream. Potom sa postupuje od najistejšieho zdroja parametrov k najmenej istému:

1. parametre nájdené **priamo v tele streamu** — takto ich nesú toky MTS/M2TS
   a AVCHD, kde sa opakujú pri každom kľúčovom snímku, takže stačí skopírovať
   ich na začiatok;
2. parametre z **`avcC`/`hvcC` iného videa z tej istej kamery** — a to
   **vrátane poškodených súborov**. Toto je najdôležitejšia cesta pre H.265 v MP4:
   kamery aj ffmpeg pri zápise do MP4 parametre z tela streamu odoberú a uložia
   ich len do hlavičky, takže po jej zašifrovaní v súbore nezostanú. Zdravý
   súbor však na to netreba: ransomvér šifruje len začiatok, takže v ostatných
   zašifrovaných videách z tej istej kamery index `moov` na konci väčšinou
   prežije aj s parametrami. Program preto sám prehľadá priečinok s opravovaným
   súborom (a prípadne ďalší, ktorý zadáš) a všetky nájdené sady parametrov
   vyskúša;
3. **skúšanie kombinácií** (len H.264): program poskladá SPS/PPS pre mriežku možností
   (rozlíšenie × profil × CABAC/CAVLC × spôsob číslovania snímkov), každú predradí
   vzorke streamu a nechá ffmpeg dekódovať. Vyhráva tá s najlepším pomerom
   dekódovaných snímkov k chybám — pri nesprávnych parametroch sa dekodér zahltí
   chybami, pri správnych ide takmer čisto.

Ak rozlíšenie pôvodného videa poznáš a zadáš ho, počet kombinácií klesne rádovo.

## Kam sa ukladajú výsledky

Výstupný priečinok si vyberieš v kroku **3 · Oprava**. Predvolene je to podpriečinok
`opravene/` vedľa pôvodného videa, ale jedným klikom sa dá prepnúť na *Plochu*,
*Dokumenty* alebo *domovský priečinok*.

Program ešte pred spustením overí, či sa do zvoleného miesta dá naozaj zapisovať,
a ak nie, povie prečo. Najčastejší prípad na macOS: **externý disk naformátovaný
ako NTFS je pripojený len na čítanie** — video sa z neho dá načítať, ale výsledok
naň zapísať nie. Vtedy stačí zvoliť priečinok na internom disku.

## Obmedzenia — na rovinu

* **Zašifrovaná časť je nenávratne preč.** Program obnoví štruktúru súboru, nie
  prepísané dáta. Prvé sekundy videa budú chýbať alebo budú rušené; program
  preto vyrobí aj čistú verziu orezanú po prvý neporušený kľúčový snímok
  (bez straty kvality, bez prekódovania).
* **Ak je súbor zašifrovaný celý, zachrániť sa nedá.** Žiadnym nástrojom.
  V takom prípade skús priponu a text výkupného na
  [No More Ransom](https://www.nomoreransom.org/) — pre viaceré rodiny
  ransomvéru existujú bezplatné dešifrovače.
* **Pri zničenom indexe sa zvuk zachrániť nedá.** Zvukové vzorky sú v súbore
  premiešané s obrazovými a bez tabuliek ich nemožno spoľahlivo odlíšiť.
* **Ak máš z kamery len jediné video a to má zničený index**, pri H.265 sa
  parametre vziať odkiaľ nedá. Oplatí sa prehľadať zálohy, telefón, staré karty
  aj kôš — postačí akýkoľvek iný záznam z tej istej kamery, hoci zašifrovaný.
* **Orezanie ide len po kľúčový snímok.** Pri kamerách býva každú 1 – 2 sekundy,
  takže strata je malá. Ak sú kľúčové snímky ďaleko od seba, orezanie zoberie
  viac — plná verzia zostáva k dispozícii tiež.
* Podporované sú **H.264 a H.265**, kodek program rozpozná sám. Pri H.265 bez
  zdravého vzoru sa parametre poskladať nedajú (majú príliš veľa kombinácií),
  takže vtedy je vzorový súbor z tej istej kamery nutný. Staršie kodeky
  (MPEG-4 Part 2, MJPEG) sa vyrezať nedajú, ale stratégia s prežitým indexom
  funguje aj pre ne.
* Keď sa vyrezaný stream nedá prebaliť bez straty kvality (chýbajúca pôvodná
  hlavička to často znemožní), program ho **prekóduje do H.264**, aby výsledok
  šiel otvoriť v bežnom prehrávači. Je to stratové, ale lepšie než surový
  súbor, ktorý neotvorí nič.
* Profily zariadení v databáze sú **orientačné**. Obsah boxu `ftyp` je pre
  prehrateľnosť zameniteľný, takže na výsledok nemajú vplyv — slúžia len na
  lepšie pomenovanie a odporučenie.

## Skôr než začneš

1. **Urob si kópiu poškodených súborov** na iný disk. Program originál nemení,
   ale pri záchrane dát je záloha vždy na mieste.
2. **Nepreformátuj a nemaž nič**, kým nemáš výsledky — pôvodné súbory môžu byť
   ešte obnoviteľné aj inými nástrojmi.
3. **Skús viacero postupov.** Ak jeden nezaberie, druhý môže. Analýza ich zoradí
   podľa vhodnosti.

## Testy

```bash
python3 -m unittest discover -s tests -t tests -v
```

Testy si samy vyrobia video, napodobnia naň útok ransomvéru (šifrovanie začiatku,
skákavé šifrovanie, značka pripojená na koniec) a overia, že sa dá zachrániť.
Testy, ktoré potrebujú ffmpeg, sa bez neho preskočia.
