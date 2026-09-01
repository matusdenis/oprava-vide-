"""Lokalne webove rozhranie programu.

Bezi len na 127.0.0.1, takze sa k nemu z ineho pocitaca neda pripojit. Kazdu
poziadavku navyse chrani nahodny token vygenerovany pri starte.
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .analyze import analyze
from .headerdb import HeaderDB
from .jobs import JobManager
from .repair import Ctx, spusti as spusti_strategiu
from .tools import Toolbox
from .util import human

WEBUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
NASTAVENIA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "nastavenia.json")

VIDEO_PRIPONY = (".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts",
                 ".m2ts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".vob", ".m2t")


class Stav:
    """Zdielany stav aplikacie."""

    def __init__(self):
        self.db = HeaderDB()
        self.toolbox = Toolbox(self._nacitaj_cesty())
        self.jobs = JobManager()
        self.token = secrets.token_urlsafe(16)

    def _nacitaj_cesty(self) -> dict:
        try:
            with open(NASTAVENIA, "r", encoding="utf-8") as f:
                return json.load(f).get("nastroje", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def uloz_cesty(self):
        os.makedirs(os.path.dirname(NASTAVENIA), exist_ok=True)
        with open(NASTAVENIA, "w", encoding="utf-8") as f:
            json.dump({"nastroje": self.toolbox.overrides}, f, ensure_ascii=False, indent=2)


def _vypis_priecinok(cesta: str) -> dict:
    cesta = os.path.abspath(os.path.expanduser(cesta or os.path.expanduser("~")))
    if not os.path.isdir(cesta):
        cesta = os.path.dirname(cesta) or os.path.expanduser("~")
    polozky = []
    try:
        with os.scandir(cesta) as it:
            for e in it:
                try:
                    je_dir = e.is_dir()
                    velkost = 0 if je_dir else e.stat().st_size
                except OSError:
                    continue
                if e.name.startswith("."):
                    continue
                polozky.append({"nazov": e.name, "priecinok": je_dir,
                                "velkost": velkost,
                                "velkost_citatelne": "" if je_dir else human(velkost),
                                "cesta": os.path.join(cesta, e.name)})
    except PermissionError:
        return {"cesta": cesta, "polozky": [], "chyba": "Do priečinka nie je prístup."}
    polozky.sort(key=lambda p: (not p["priecinok"], p["nazov"].lower()))
    rodic = os.path.dirname(cesta)
    return {"cesta": cesta, "rodic": rodic if rodic != cesta else None,
            "polozky": polozky,
            "disky": _disky()}


def _disky() -> list:
    if os.name != "nt":
        return [{"nazov": "/", "cesta": "/"},
                {"nazov": "Domov", "cesta": os.path.expanduser("~")}]
    out = []
    import string
    for pismeno in string.ascii_uppercase:
        d = f"{pismeno}:\\"
        if os.path.exists(d):
            out.append({"nazov": d, "cesta": d})
    out.append({"nazov": "Domov", "cesta": os.path.expanduser("~")})
    return out


class Handler(BaseHTTPRequestHandler):
    stav: Stav = None
    server_version = f"VidFix/{__version__}"

    def log_message(self, fmt, *args):      # tiche logovanie
        pass

    # ------------------------------------------------------------------
    def _json(self, data, code: int = 200):
        telo = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(telo)))
        self.end_headers()
        self.wfile.write(telo)

    def _subor(self, cesta: str, typ: str | None = None):
        if not os.path.isfile(cesta):
            self.send_error(404)
            return
        typ = typ or mimetypes.guess_type(cesta)[0] or "application/octet-stream"
        with open(cesta, "rb") as f:
            telo = f.read()
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(telo)))
        self.end_headers()
        self.wfile.write(telo)

    def _telo(self) -> dict:
        dlzka = int(self.headers.get("Content-Length") or 0)
        if not dlzka:
            return {}
        try:
            return json.loads(self.rfile.read(dlzka).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _overeny(self, dotaz: dict) -> bool:
        token = (dotaz.get("token", [None])[0]
                 or self.headers.get("X-VidFix-Token"))
        return token == self.stav.token

    # ------------------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        dotaz = urllib.parse.parse_qs(url.query)
        cesta = url.path

        if cesta in ("/", "/index.html"):
            self._subor(os.path.join(WEBUI, "index.html"), "text/html; charset=utf-8")
            return
        if cesta.startswith("/static/"):
            nazov = os.path.basename(cesta)
            self._subor(os.path.join(WEBUI, nazov))
            return
        if cesta == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if cesta == "/api/token-check":
            self._json({"ok": self._overeny(dotaz)})
            return

        if not self._overeny(dotaz):
            self._json({"chyba": "Neplatný token."}, 403)
            return

        if cesta == "/api/stav":
            self._json({"verzia": __version__,
                        "nastroje": self.stav.toolbox.status(),
                        "databaza": self.stav.db.summary(),
                        "domov": os.path.expanduser("~")})
        elif cesta == "/api/databaza":
            self._json({"suhrn": self.stav.db.summary(),
                        "hlavicky": self.stav.db.headers,
                        "kontajnery": self.stav.db.containers(),
                        "znacky": self.stav.db.data.get("ftyp_znacky", []),
                        "zariadenia": self.stav.db.devices(),
                        "ransomver": self.stav.db.ransomware()})
        elif cesta == "/api/uloha":
            job = self.stav.jobs.get(dotaz.get("id", [""])[0])
            if not job:
                self._json({"chyba": "Úloha neexistuje."}, 404)
                return
            od = int(dotaz.get("od", ["0"])[0])
            self._json(job.snapshot(od))
        elif cesta == "/api/nahlad":
            self._nahlad(dotaz.get("cesta", [""])[0])
        else:
            self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        dotaz = urllib.parse.parse_qs(url.query)
        if not self._overeny(dotaz):
            self._json({"chyba": "Neplatný token."}, 403)
            return
        telo = self._telo()
        cesta = url.path
        try:
            if cesta == "/api/prehliadac":
                self._json(_vypis_priecinok(telo.get("cesta") or ""))
            elif cesta == "/api/analyza":
                self._analyza(telo)
            elif cesta == "/api/oprava":
                self._oprava(telo)
            elif cesta == "/api/nastroje":
                for meno in ("ffmpeg", "ffprobe", "untrunc"):
                    if meno in telo:
                        self.stav.toolbox.set_path(meno, telo[meno] or None)
                self.stav.uloz_cesty()
                self._json({"nastroje": self.stav.toolbox.status()})
            elif cesta == "/api/databaza/pridat":
                zaznam = self.stav.db.add_header_from_file(
                    telo.get("cesta", ""), telo.get("nazov", ""), telo.get("poznamka", ""))
                self._json({"ok": True, "zaznam": zaznam})
            elif cesta == "/api/databaza/zmazat":
                ok = self.stav.db.remove_user_header(telo.get("id", ""))
                self._json({"ok": ok})
            elif cesta == "/api/otvorit-priecinok":
                self._otvorit(telo.get("cesta", ""))
            else:
                self.send_error(404)
        except Exception as exc:                # noqa: BLE001
            self._json({"chyba": str(exc) or exc.__class__.__name__}, 500)

    # ------------------------------------------------------------------
    def _analyza(self, telo: dict):
        cesta = telo.get("cesta", "")
        if not os.path.isfile(cesta):
            self._json({"chyba": "Súbor neexistuje."}, 400)
            return
        st = self.stav
        job = st.jobs.spusti(f"Analyza: {os.path.basename(cesta)}",
                             lambda log: analyze(cesta, st.db, st.toolbox, log=log))
        self._json({"uloha": job.id})

    def _oprava(self, telo: dict):
        cesta = telo.get("cesta", "")
        vystup = telo.get("vystup") or os.path.join(os.path.dirname(cesta), "opravene")
        strategia = telo.get("strategia", "")
        volby = telo.get("volby") or {}
        if not os.path.isfile(cesta):
            self._json({"chyba": "Súbor neexistuje."}, 400)
            return
        st = self.stav

        def uloha(log):
            ctx = Ctx(cesta, vystup, st.toolbox, st.db, volby, log=log)
            return spusti_strategiu(strategia, ctx)

        job = st.jobs.spusti(f"{strategia}: {os.path.basename(cesta)}", uloha)
        self._json({"uloha": job.id})

    def _nahlad(self, cesta: str):
        """Vyrobi nahladovy obrazok z vysledneho suboru."""
        if not os.path.isfile(cesta):
            self.send_error(404)
            return
        ffmpeg = self.stav.toolbox.path("ffmpeg")
        if not ffmpeg:
            self.send_error(503)
            return
        try:
            r = subprocess.run(
                [ffmpeg, "-v", "error", "-i", cesta, "-frames:v", "1",
                 "-vf", "scale=480:-1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=120)
            if r.returncode != 0 or not r.stdout:
                self.send_error(422)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(r.stdout)))
            self.end_headers()
            self.wfile.write(r.stdout)
        except (subprocess.SubprocessError, OSError):
            self.send_error(500)

    def _otvorit(self, cesta: str):
        """Otvori priecinok s vysledkami v spravcovi suborov."""
        if not os.path.isdir(cesta):
            cesta = os.path.dirname(cesta)
        if not os.path.isdir(cesta):
            self._json({"chyba": "Priečinok neexistuje."}, 400)
            return
        try:
            if os.name == "nt":
                os.startfile(cesta)                                  # noqa: S606
            elif os.uname().sysname == "Darwin":
                subprocess.Popen(["open", cesta])
            else:
                subprocess.Popen(["xdg-open", cesta])
            self._json({"ok": True})
        except (OSError, AttributeError) as exc:
            self._json({"chyba": str(exc)}, 500)


def spusti_server(host: str = "127.0.0.1", port: int = 8765, otvorit: bool = True):
    stav = Stav()
    Handler.stav = stav
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{server.server_port}/?token={stav.token}"
    print("=" * 70)
    print(f"  Oprava videa {__version__} — rozhranie beží na:")
    print(f"  {url}")
    print("=" * 70)
    print("  Ukončenie: Ctrl+C")
    n = stav.toolbox.status()
    if not n["ffmpeg"]["available"]:
        print("  POZOR: ffmpeg sa nenašiel. Nastav k nemu cestu v záložke Nástroje.")
    if otvorit:
        def otvor():
            import webbrowser
            webbrowser.open(url)
        threading.Timer(1.0, otvor).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKoniec.")
    finally:
        server.server_close()
