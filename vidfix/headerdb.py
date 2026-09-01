"""Databaza hlaviciek kontajnerov z verejnych zdrojov.

Databaza (data/headers.json) obsahuje:
  * podpisy kontajnerov (MP4/MOV, MPEG-TS, AVI, MKV, ASF, FLV, MPEG-PS)
  * register znaciek ftyp podla MP4RA
  * hotove, programovo poskladane sablony hlaviciek na graftovanie
  * orientacne profily zariadeni (telefon, GoPro, DJI, Sony, Canon...)
  * zname vzory spravania ransomveru a kandidatske dlzky zasifrovaneho zaciatku

Pouzivatel si moze pridat vlastne hlavicky (napr. vytiahnute z ineho suboru
z toho isteho zariadenia) - ukladaju sa do data/headers_user.json, aby ich
prepis dodavanej databazy neprepisal.
"""
from __future__ import annotations

import binascii
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "headers.json")
USER_DB_PATH = os.path.join(ROOT, "data", "headers_user.json")


class HeaderDB:
    def __init__(self, path: str = DB_PATH, user_path: str = USER_DB_PATH):
        self.path = path
        self.user_path = user_path
        self.data = {}
        self.user = {"hlavicky": []}
        self.load()

    # ------------------------------------------------------------------
    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        if os.path.exists(self.user_path):
            try:
                with open(self.user_path, "r", encoding="utf-8") as f:
                    self.user = json.load(f)
            except (OSError, json.JSONDecodeError):
                self.user = {"hlavicky": []}
        self.user.setdefault("hlavicky", [])

    def save_user(self):
        os.makedirs(os.path.dirname(self.user_path), exist_ok=True)
        with open(self.user_path, "w", encoding="utf-8") as f:
            json.dump(self.user, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    @property
    def headers(self) -> list:
        """Dodavane aj pouzivatelske hlavicky dokopy."""
        return list(self.data.get("hlavicky", [])) + list(self.user.get("hlavicky", []))

    def header(self, header_id: str) -> dict | None:
        for h in self.headers:
            if h.get("id") == header_id:
                return h
        return None

    def header_bytes(self, header_id: str) -> bytes | None:
        h = self.header(header_id)
        if not h:
            return None
        return binascii.unhexlify(h["hex"])

    def containers(self) -> list:
        return self.data.get("containers", self.data.get("kontajnery", []))

    def container(self, cid: str) -> dict | None:
        for c in self.containers():
            if c.get("id") == cid:
                return c
        return None

    def ransomware(self) -> dict:
        return self.data.get("ransomver", {})

    def candidate_lengths(self) -> list:
        return self.ransomware().get("kandidatske_dlzky", [])

    def devices(self) -> list:
        return self.data.get("zariadenia", [])

    def sources(self) -> list:
        return self.data.get("zdroje", [])

    # ------------------------------------------------------------------
    def identify(self, head: bytes, tail: bytes = b"", filename: str = "") -> list:
        """Urci mozne kontajnery podla magickych bajtov a pripony.

        Vracia zoznam kandidatov zoradeny podla skore. Pri suboroch poskodenych
        ransomverom byva zaciatok zasifrovany, takze sa skore opiera aj o
        priponu z povodneho nazvu (napr. "dovolenka.mp4.locked").
        """
        out = []
        lower = filename.lower()
        for c in self.containers():
            score = 0.0
            reasons = []
            for sig in c.get("podpisy", []):
                off = sig.get("offset", 0)
                raw = binascii.unhexlify(sig["hex"])
                if head[off:off + len(raw)] == raw:
                    score += 1.0
                    reasons.append(f"podpis: {sig['popis']}")
            for ext in c.get("pripony", []):
                if ext in lower:
                    score += 0.4
                    reasons.append(f"pripona {ext} v nazve suboru")
                    break
            if score > 0:
                out.append({"id": c["id"], "nazov": c["nazov"], "skore": round(score, 2),
                            "dovody": reasons, "poznamka": c.get("poznamka", ""),
                            "index_na_konci": c.get("index_na_konci", False)})
        out.sort(key=lambda x: -x["skore"])
        return out

    def suggest_header(self, container: str = "mp4", codec: str = "",
                       fragmented: bool = False) -> str:
        """Odporuci id sablony hlavicky podla kontajnera a kodeku."""
        codec = (codec or "").lower()
        if fragmented:
            return "mp4_dash_fragmentovany"
        if container in ("mov", "qt"):
            return "mov_quicktime"
        if container == "3gp":
            return "mp4_3gp5_telefon"
        if codec in ("hvc1", "hev1", "hevc", "h265", "dvh1"):
            return "mp4_hevc"
        return "mp4_isom_univerzalny"

    # ------------------------------------------------------------------
    def add_header_from_file(self, path: str, name: str, note: str = "",
                             max_len: int = 4096) -> dict:
        """Vytiahne box ftyp zo zdraveho suboru a ulozi ho ako novu sablonu.

        Hodi sa, ked ma pouzivatel k dispozicii hoci len JEDEN nepoškodeny subor
        z toho isteho zariadenia (aj kratke, aj uplne ine video) - jeho ftyp je
        potom najpresnejsia mozna nahrada.
        """
        with open(path, "rb") as f:
            head = f.read(max_len)
        if head[4:8] != b"ftyp":
            raise ValueError("Súbor nezačína boxom „ftyp“ — nie je to zdravý MP4/MOV.")
        size = int.from_bytes(head[0:4], "big")
        if size < 8 or size > len(head):
            raise ValueError("Box ftyp má nezmyselnú veľkosť.")
        raw = head[:size]
        entry = {
            "id": f"user_{len(self.user['hlavicky']) + 1}_{os.path.basename(path)[:24]}",
            "kontajner": "mov" if raw[8:12] == b"qt  " else "mp4",
            "popis": name or f"Vlastná hlavička z {os.path.basename(path)}",
            "hex": raw.hex(),
            "dlzka": len(raw),
            "major_brand": raw[8:12].decode("latin-1", "replace"),
            "minor_version": int.from_bytes(raw[12:16], "big"),
            "compatible_brands": [raw[i:i + 4].decode("latin-1", "replace")
                                  for i in range(16, len(raw), 4)],
            "pouzitie": note or "Vytiahnutá zo vzorového súboru používateľa.",
            "zdroj": "pouzivatel",
            "pridane": datetime.now().isoformat(timespec="seconds"),
            "zdrojovy_subor": os.path.basename(path),
        }
        self.user["hlavicky"].append(entry)
        self.save_user()
        return entry

    def remove_user_header(self, header_id: str) -> bool:
        before = len(self.user["hlavicky"])
        self.user["hlavicky"] = [h for h in self.user["hlavicky"] if h.get("id") != header_id]
        if len(self.user["hlavicky"]) != before:
            self.save_user()
            return True
        return False

    def summary(self) -> dict:
        return {
            "verzia": self.data.get("verzia"),
            "aktualizovane": self.data.get("aktualizovane"),
            "pocet_hlaviciek": len(self.headers),
            "pocet_vlastnych": len(self.user["hlavicky"]),
            "pocet_kontajnerov": len(self.containers()),
            "pocet_znaciek": len(self.data.get("ftyp_znacky", [])),
            "zdroje": self.sources(),
        }
