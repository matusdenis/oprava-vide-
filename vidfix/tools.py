"""Vyhladanie a spustanie externych nastrojov: ffmpeg, ffprobe, untrunc."""
from __future__ import annotations

import json
import os
import re
import platform
import shutil
import subprocess

# Miesta, kde sa nastroje casto nachadzaju, ked nie su v PATH
# Hlasenia dekodera o uz zniceny datach - do priebehu ich netreba vypisovat,
# len sa spocitaju. Pouzivatela zaujima postup opravy, nie tisic riadkov o tom,
# ze zasifrovana cast sa neda dekodovat.
SUM_DEKODERA = re.compile(
    r"^\s*(\[(h264|hevc|aac|mp3|ac3|mp4|mov|mpegts|matroska|avi|vist|aist|vost|aost|"
    r"in#|out#|fc#|dec:|enc:)[^\]]*\]|Last message repeated|frame=|\s*$)")

EXTRA_DIRS = [
    r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files\ffmpeg", r"C:\Tools\ffmpeg\bin",
    "/usr/local/bin", "/usr/bin", "/opt/homebrew/bin", "/snap/bin",
    os.path.expanduser("~/bin"), os.path.expanduser("~/.local/bin"),
]


def _candidates(name: str):
    exe = name + (".exe" if platform.system() == "Windows" else "")
    found = shutil.which(name)
    if found:
        yield found
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in [os.path.join(here, "bin"), os.path.join(here, "nastroje")] + EXTRA_DIRS:
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            yield p
    # ffmpeg z balicka imageio-ffmpeg, ak je nainstalovany
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            yield imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass


class Toolbox:
    """Drzi cesty k externym nastrojom a spusta ich."""

    def __init__(self, overrides: dict | None = None):
        self.overrides = dict(overrides or {})
        self._cache: dict[str, str | None] = {}

    def path(self, name: str) -> str | None:
        if name in self.overrides and self.overrides[name]:
            return self.overrides[name]
        if name not in self._cache:
            self._cache[name] = next(iter(_candidates(name)), None)
        return self._cache[name]

    def set_path(self, name: str, path: str | None):
        self.overrides[name] = path
        self._cache.pop(name, None)

    def version(self, name: str) -> str | None:
        p = self.path(name)
        if not p:
            return None
        try:
            r = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=20)
            out = (r.stdout or r.stderr).strip().splitlines()
            return out[0] if out else "?"
        except Exception:
            # untrunc nema -version a skonci s chybou, aj tak vieme, ze existuje
            return "prítomný"

    def status(self) -> dict:
        out = {}
        for name in ("ffmpeg", "ffprobe", "untrunc"):
            p = self.path(name)
            out[name] = {"path": p, "version": self.version(name) if p else None,
                         "available": bool(p)}
        return out

    # ------------------------------------------------------------------
    def run(self, name: str, args: list, log=None, timeout: int | None = None) -> dict:
        """Spusti nastroj, priebezne posiela vystup do `log` (callable)."""
        exe = self.path(name)
        if not exe:
            raise FileNotFoundError(f"Nástroj „{name}“ sa nenašiel. Nastav k nemu cestu v záložke Nástroje.")
        cmd = [exe] + [str(a) for a in args]
        if log:
            log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        lines = []
        potlacene = 0
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                if SUM_DEKODERA.match(line):
                    potlacene += 1
                    continue
                if log and (len(lines) < 400 or len(lines) % 25 == 0):
                    log(line)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        finally:
            if proc.stdout:
                proc.stdout.close()
        if log and potlacene:
            log(f"  ({potlacene} hlásení dekodéra o poškodenej časti — to je "
                f"pri poškodenom súbore očakávané)")
        return {"code": proc.returncode, "output": "\n".join(lines), "cmd": cmd,
                "potlacene": potlacene}

    # ------------------------------------------------------------------
    def probe(self, path: str) -> dict:
        """Zisti informacie o subore. Preferuje ffprobe, inak pouzije ffmpeg."""
        ffprobe = self.path("ffprobe")
        if ffprobe:
            try:
                r = subprocess.run(
                    [ffprobe, "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", path],
                    capture_output=True, text=True, timeout=180)
                if r.stdout.strip():
                    data = json.loads(r.stdout)
                    data["_ok"] = r.returncode == 0
                    return data
            except Exception as exc:
                return {"_ok": False, "_error": str(exc)}
        ffmpeg = self.path("ffmpeg")
        if ffmpeg:
            try:
                r = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                                   capture_output=True, text=True, timeout=180)
                return {"_ok": "Invalid data" not in r.stderr, "_raw": r.stderr}
            except Exception as exc:
                return {"_ok": False, "_error": str(exc)}
        return {"_ok": False, "_error": "ffmpeg ani ffprobe nie sú k dispozícii"}

    def playable(self, path: str, seconds: float = 5.0) -> dict:
        """Skusi subor naozaj dekodovat - konecny test uspesnosti opravy."""
        ffmpeg = self.path("ffmpeg")
        if not ffmpeg:
            return {"ok": False, "reason": "ffmpeg nie je k dispozícii"}
        try:
            r = subprocess.run([ffmpeg, "-v", "error", "-xerror",
                                "-probesize", "200M", "-analyzeduration", "200M",
                                "-t", str(seconds), "-i", path, "-f", "null", "-"],
                               capture_output=True, text=True, timeout=600)
            errs = [l for l in r.stderr.splitlines() if l.strip()]
            return {"ok": r.returncode == 0, "errors": errs[:20], "error_count": len(errs)}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
