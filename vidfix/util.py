"""Drobne pomocne funkcie."""
from __future__ import annotations

import math
import os
from collections import Counter


def human(n: float) -> str:
    """Velkost v bajtoch ako citatelny retazec."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TiB"


def hhmmss(seconds: float | None) -> str:
    """Sekundy ako HH:MM:SS.mmm."""
    if seconds is None:
        return "?"
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def entropy(data: bytes) -> float:
    """Shannonova entropia bloku v bitoch na bajt (0 az 8)."""
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def entropy_profile(path: str, blocks: int = 160, block_size: int = 4096) -> list[dict]:
    """Vzorkovany entropicky profil suboru pre graf v UI.

    Vracia zoznam {offset, entropy} - `blocks` rovnomerne rozlozenych vzoriek.
    Sifrovane data maju entropiu takmer presne 8.0, komprimovane video
    typicky 7.4-7.95, hlavicky a tabulky vyrazne menej.
    """
    size = os.path.getsize(path)
    if size == 0:
        return []
    step = max(block_size, size // max(1, blocks))
    out = []
    with open(path, "rb") as f:
        off = 0
        while off < size and len(out) < blocks:
            f.seek(off)
            chunk = f.read(min(block_size, size - off))
            if not chunk:
                break
            out.append({"offset": off, "entropy": round(entropy(chunk), 4)})
            off += step
    return out


def hexdump(data: bytes, base: int = 0, limit: int = 256) -> str:
    """Klasicky hexdump pre zobrazenie v UI."""
    data = data[:limit]
    lines = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        hexpart = " ".join(f"{b:02x}" for b in row)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{base + i:08x}  {hexpart:<47}  {asciipart}")
    return "\n".join(lines)


def printable_type(t: bytes) -> bool:
    """Je stvorica bajtov rozumny nazov boxu/atomu?"""
    if len(t) != 4:
        return False
    for b in t:
        # povolene su ASCII pismena, cislice, medzera a niekolko znakov
        if not (0x20 <= b <= 0x7E):
            return False
    return True


def safe_name(name: str) -> str:
    """Nazov suboru bez nebezpecnych znakov."""
    bad = '<>:"/\\|?*\0'
    out = "".join("_" if c in bad else c for c in name)
    return out.strip() or "vystup"
