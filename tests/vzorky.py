"""Vyroba testovacich vzoriek: zdrave video a jeho poskodene varianty."""
from __future__ import annotations

import os
import random
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vidfix.tools import Toolbox  # noqa: E402


def vyrob_video(cesta: str, sekundy: int = 12, sirka: int = 640, vyska: int = 360,
                faststart: bool = False, kontajner: str = "mp4",
                keyint: int = 30, kodek: str = "h264", fps: int = 25) -> str:
    """Vyrobi zdrave testovacie video pomocou ffmpeg."""
    tb = Toolbox()
    if not tb.path("ffmpeg"):
        raise RuntimeError("ffmpeg nie je k dispozicii")
    args = ["-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={sirka}x{vyska}:rate={fps}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", str(sekundy), "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if kodek == "h265":
        args += ["-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
                 "-x265-params", f"keyint={keyint}:log-level=none"]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(keyint)]
    if kontajner == "mp4" and faststart:
        args += ["-movflags", "+faststart"]
    args += [cesta]
    r = subprocess.run([tb.path("ffmpeg")] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg zlyhal: " + r.stderr[-500:])
    return cesta


def posifruj_zaciatok(zdroj: str, ciel: str, dlzka: int = 512 * 1024,
                      znacka: bytes = b"", seed: int = 42) -> str:
    """Napodobni ransomver: prepise zaciatok suboru nahodnymi datami."""
    with open(zdroj, "rb") as f:
        data = bytearray(f.read())
    n = min(dlzka, len(data) // 2)
    rnd = random.Random(seed)
    data[:n] = bytes(rnd.getrandbits(8) for _ in range(n))
    if znacka:
        data += znacka
    with open(ciel, "wb") as f:
        f.write(bytes(data))
    return ciel


def posifruj_skakavo(zdroj: str, ciel: str, blok: int = 64 * 1024,
                     kazdy: int = 4, seed: int = 7) -> str:
    """Napodobni "skakave" (intermitentne) sifrovanie - kazdy N-ty blok."""
    with open(zdroj, "rb") as f:
        data = bytearray(f.read())
    rnd = random.Random(seed)
    i = 0
    while i * blok * kazdy < len(data) - blok:
        zac = i * blok * kazdy
        data[zac:zac + blok] = bytes(rnd.getrandbits(8) for _ in range(blok))
        i += 1
    with open(ciel, "wb") as f:
        f.write(bytes(data))
    return ciel
