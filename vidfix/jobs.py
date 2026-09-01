"""Spustanie dlhych uloh na pozadi, aby rozhranie neprestalo reagovat."""
from __future__ import annotations

import threading
import time
import traceback
import uuid


class Job:
    def __init__(self, nazov: str):
        self.id = uuid.uuid4().hex[:12]
        self.nazov = nazov
        self.stav = "caka"          # caka | bezi | hotovo | chyba
        self.riadky: list = []
        self.vysledok = None
        self.chyba = None
        self.zaciatok = time.time()
        self.koniec = None
        self._lock = threading.Lock()

    def log(self, sprava: str):
        with self._lock:
            self.riadky.append({"cas": round(time.time() - self.zaciatok, 1),
                                "text": str(sprava)})
            if len(self.riadky) > 4000:
                del self.riadky[:1000]

    def snapshot(self, od: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id, "nazov": self.nazov, "stav": self.stav,
                "riadky": self.riadky[od:], "pocet_riadkov": len(self.riadky),
                "vysledok": self.vysledok, "chyba": self.chyba,
                "trvanie": round((self.koniec or time.time()) - self.zaciatok, 1),
            }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def spusti(self, nazov: str, fn) -> Job:
        """`fn(log)` sa vykona vo vlastnom vlakne."""
        job = Job(nazov)
        with self._lock:
            self.jobs[job.id] = job

        def beh():
            job.stav = "bezi"
            try:
                job.vysledok = fn(job.log)
                job.stav = "hotovo"
            except Exception as exc:            # noqa: BLE001 - chybu ukazeme uzivatelovi
                job.chyba = str(exc) or exc.__class__.__name__
                job.log("CHYBA: " + job.chyba)
                job.log(traceback.format_exc().strip().splitlines()[-1])
                job.stav = "chyba"
            finally:
                job.koniec = time.time()

        threading.Thread(target=beh, daemon=True, name=f"uloha-{job.id}").start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def uprac(self, starsie_ako: float = 3600):
        teraz = time.time()
        with self._lock:
            for jid in [j for j, o in self.jobs.items()
                        if o.koniec and teraz - o.koniec > starsie_ako]:
                del self.jobs[jid]
