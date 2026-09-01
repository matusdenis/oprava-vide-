"use strict";
const TOKEN = new URLSearchParams(location.search).get("token") || "";
let stavAplikacie = { subor: null, analyza: null, strategia: null, uloha: null };

/* ---------- pomocne ---------- */
async function api(cesta, data) {
  const url = cesta + (cesta.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
  const nastavenia = data
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }
    : {};
  const r = await fetch(url, nastavenia);
  const j = await r.json().catch(() => ({ chyba: "Neplatná odpoveď servera." }));
  if (!r.ok || j.chyba) throw new Error(j.chyba || ("HTTP " + r.status));
  return j;
}
function hlaska(text, zla) {
  const e = document.getElementById("hlaska");
  e.textContent = text;
  e.className = "hlaska" + (zla ? " zla" : "");
  clearTimeout(e._t);
  e._t = setTimeout(() => e.classList.add("skryty"), 6000);
}
function esc(s) {
  return String(s === undefined || s === null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function bajty(n) {
  if (n === null || n === undefined) return "?";
  const j = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0; n = Number(n);
  while (n >= 1024 && i < j.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(2)) + " " + j[i];
}
function prepniPanel(id) {
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("aktivny", p.id === id));
  document.querySelectorAll(".zalozka").forEach(z => z.classList.toggle("aktivna", z.dataset.panel === id));
}
document.querySelectorAll(".zalozka").forEach(z =>
  z.onclick = () => prepniPanel(z.dataset.panel));

/* ---------- prehliadač súborov ---------- */
const VIDEO_PRIPONY = [".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts",
  ".m2ts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".vob"];
function jeVideo(nazov) {
  const n = nazov.toLowerCase();
  return VIDEO_PRIPONY.some(p => n.includes(p));
}
async function prejst(cesta) {
  try {
    const v = await api("/api/prehliadac", { cesta });
    document.getElementById("cestaVstup").value = v.cesta;
    document.getElementById("disky").innerHTML = (v.disky || [])
      .map(d => `<button data-cesta="${esc(d.cesta)}">${esc(d.nazov)}</button>`).join("");
    document.querySelectorAll("#disky button").forEach(b =>
      b.onclick = () => prejst(b.dataset.cesta));
    let html = "";
    if (v.rodic) html += `<div class="polozka" data-dir="${esc(v.rodic)}">
      <span class="ikona">↰</span><span>.. (o úroveň vyššie)</span></div>`;
    for (const p of v.polozky) {
      const video = !p.priecinok && jeVideo(p.nazov);
      html += `<div class="polozka ${video ? "video" : ""}" ${p.priecinok
        ? `data-dir="${esc(p.cesta)}"` : `data-file="${esc(p.cesta)}"`}>
        <span class="ikona">${p.priecinok ? "📁" : (video ? "🎬" : "📄")}</span>
        <span>${esc(p.nazov)}</span>
        <span class="velkost">${esc(p.velkost_citatelne)}</span></div>`;
    }
    const el = document.getElementById("prehliadac");
    el.innerHTML = html || '<div class="polozka">Priečinok je prázdny.</div>';
    el.querySelectorAll("[data-dir]").forEach(d => d.onclick = () => prejst(d.dataset.dir));
    el.querySelectorAll("[data-file]").forEach(d => d.onclick = () => vyberSubor(d.dataset.file));
  } catch (e) { hlaska(e.message, true); }
}
function vyberSubor(cesta) {
  stavAplikacie.subor = cesta;
  stavAplikacie.analyza = null;
  const nazov = cesta.split(/[\\/]/).pop();
  const priecinok = cesta.substring(0, cesta.length - nazov.length - 1);
  const el = document.getElementById("vybranySubor");
  el.classList.remove("skryty");
  el.innerHTML = `<b>Vybraný súbor:</b> <code>${esc(cesta)}</code>
    <div class="riadok" style="margin-top:12px">
      <button id="btnAnalyza">Analyzovať súbor</button>
      <input type="text" id="vystupPriecinok" value="${esc(priecinok + "/opravene")}">
    </div>
    <p class="popis" style="margin:0">Výsledky sa uložia do priečinka vyššie. Pôvodný súbor sa nemení.</p>`;
  document.getElementById("btnAnalyza").onclick = spustiAnalyzu;
}
document.getElementById("btnPrejst").onclick = () =>
  prejst(document.getElementById("cestaVstup").value);
document.getElementById("cestaVstup").addEventListener("keydown", e => {
  if (e.key === "Enter") prejst(e.target.value);
});

/* ---------- sledovanie úlohy ---------- */
async function sledujUlohu(id, naRiadok, naKoniec) {
  let od = 0;
  const tik = async () => {
    try {
      const s = await api("/api/uloha?id=" + id + "&od=" + od);
      od = s.pocet_riadkov;
      if (s.riadky.length) naRiadok(s.riadky);
      if (s.stav === "hotovo" || s.stav === "chyba") { naKoniec(s); return; }
      setTimeout(tik, 500);
    } catch (e) { naKoniec({ stav: "chyba", chyba: e.message }); }
  };
  tik();
}

/* ---------- analýza ---------- */
async function spustiAnalyzu() {
  prepniPanel("panel-analyza");
  const el = document.getElementById("analyzaObsah");
  el.innerHTML = '<div class="karta"><span class="spinner"></span>Analyzujem súbor…</div>';
  try {
    const { uloha } = await api("/api/analyza", { cesta: stavAplikacie.subor });
    sledujUlohu(uloha, () => { }, s => {
      if (s.stav === "chyba") {
        el.innerHTML = `<div class="vysledok zly"><b>Analýza zlyhala:</b> ${esc(s.chyba)}</div>`;
        return;
      }
      stavAplikacie.analyza = s.vysledok;
      vykresliAnalyzu(s.vysledok);
      pripravOpravu(s.vysledok);
    });
  } catch (e) { el.innerHTML = `<div class="vysledok zly">${esc(e.message)}</div>`; }
}

function vykresliAnalyzu(a) {
  const d = a.detail || {};
  let html = "";

  const kont = (a.kandidati_kontajnera || [])[0];
  html += `<div class="karta"><h3 style="margin-top:0">Súbor</h3><div class="mriezka">
    <div class="udaj"><b>Názov</b><span>${esc(a.subor)}</span></div>
    <div class="udaj"><b>Veľkosť</b><span>${esc(a.velkost_citatelne)}</span></div>
    <div class="udaj"><b>Rozpoznaný formát</b><span>${kont ? esc(kont.nazov) : "neurčený"}</span></div>
    <div class="udaj"><b>Šifrovaný začiatok</b><span>${a.odhad_sifrovanej_casti && a.odhad_sifrovanej_casti.koniec !== null
      ? bajty(a.odhad_sifrovanej_casti.koniec) : "neurčený"}</span></div>
  </div></div>`;

  // stav indexu
  if (d.typ === "mp4") {
    const maIndex = !!d.index_moov;
    html += `<div class="karta ${maIndex ? "zvyraznena" : ""}">
      <h3 style="margin-top:0">Stav vnútornej štruktúry
        <span class="znacka ${maIndex ? "dobra" : "pozor"}">${maIndex ? "index moov prežil" : "index moov zničený"}</span>
      </h3>`;
    if (maIndex) {
      const p = d.poskodenie || {};
      html += `<p class="popis">Index s tabuľkami vzoriek je na konci súboru a je neporušený.
        To je najlepší možný prípad — video sa dá obnoviť celé okrem skutočne prepísaného začiatku,
        a nie je na to potrebný žiadny zdravý vzorový súbor.</p>
      <div class="mriezka">
        <div class="udaj"><b>Index moov na offsete</b><span>${d.index_moov.offset}</span></div>
        <div class="udaj"><b>Koniec šifrovanej časti</b><span>${p.damage_end !== null && p.damage_end !== undefined ? bajty(p.damage_end) : "neurčený"}</span></div>
        <div class="udaj"><b>Poškodené úseky</b><span>${p.damaged_chunks ?? "?"} z ${p.total_chunks ?? "?"}</span></div>
        <div class="udaj"><b>Pripojené dáta na konci</b><span>${bajty(d.zvysne_bajty_na_konci || 0)}</span></div>
      </div>`;
      const m = d.mapa_poskodenia;
      if (m && !m.contiguous_prefix) {
        html += `<p class="popis" style="color:var(--pozor)">Pozor: poškodenie nie je súvislé
          (ransomvér šifroval po blokoch). Poškodených je ${(m.ratio_bad * 100).toFixed(1)} %
          kontrolovaných úsekov, takže časť videa bude rušená aj neskôr.</p>`;
      }
      if (d.stopy && d.stopy.length) {
        html += `<table><tr><th>Stopa</th><th>Kodek</th><th>Rozlíšenie</th><th>Trvanie</th>
          <th>Stratené úseky</th><th>Prvý zdravý kľúčový snímok</th></tr>`;
        for (const s of d.stopy) {
          html += `<tr><td>${s.typ === "vide" ? "obraz" : (s.typ === "soun" ? "zvuk" : esc(s.typ))}</td>
            <td>${esc(s.kodek)}</td>
            <td>${s.sirka ? s.sirka + "×" + s.vyska : (s.vzorkovacia_frekvencia ? s.vzorkovacia_frekvencia + " Hz, " + s.kanaly + " kan." : "—")}</td>
            <td>${s.trvanie_s} s</td>
            <td>${s.stratene_chunky ?? "?"} z ${s.pocet_chunkov}</td>
            <td>${s.prvy_klucovy_snimok_s !== null && s.prvy_klucovy_snimok_s !== undefined ? s.prvy_klucovy_snimok_s + " s" : "—"}</td></tr>`;
        }
        html += `</table>`;
      }
    } else {
      html += `<p class="popis">Index bol uložený na začiatku súboru a ransomvér ho prepísal.
        Obrazové dáta v tele súboru však väčšinou prežijú — dajú sa vyrezať a chýbajúce
        parametre obrazu program nájde skúšaním kombinácií z databázy.</p>`;
    }
    html += `</div>`;
  } else if (d.typ === "mpegts") {
    html += `<div class="karta zvyraznena"><h3 style="margin-top:0">MPEG-TS tok
      <span class="znacka dobra">samosynchronizujúci sa formát</span></h3>
      <p class="popis">Tento formát nemá centrálny index — stačí zahodiť zašifrovaný začiatok
      a čítať od prvého platného paketu.</p>
      ${d.synchronizacia ? `<div class="mriezka">
        <div class="udaj"><b>Prvý platný paket</b><span>${bajty(d.synchronizacia.offset)}</span></div>
        <div class="udaj"><b>Veľkosť paketu</b><span>${d.synchronizacia.packet_size} B</span></div></div>` : ""}
      </div>`;
  }

  // graf entropie
  if (a.entropia && a.entropia.length) {
    const hranica = (a.odhad_sifrovanej_casti || {}).koniec;
    html += `<div class="karta"><h3 style="margin-top:0">Mapa súboru</h3>
      <div class="graf">` +
      a.entropia.map(b => {
        const v = Math.max(2, Math.round((b.entropy / 8) * 100));
        const sif = hranica !== null && hranica !== undefined && b.offset < hranica;
        return `<div class="${sif ? "sifrovane" : ""}" style="height:${v}%" title="${bajty(b.offset)} · entropia ${b.entropy}"></div>`;
      }).join("") + `</div>
      <div class="legenda"><span><i style="background:var(--zle)"></i>zašifrovaná časť</span>
        <span><i style="background:var(--akcent)"></i>pôvodné dáta</span>
        <span>výška stĺpca = entropia bloku</span></div></div>`;
  }

  html += `<details class="karta"><summary>Prvých 160 bajtov súboru</summary>
    <pre>${esc(a.hexdump_zaciatku)}</pre></details>`;

  html += `<div class="riadok"><button onclick="document.querySelector('[data-panel=panel-oprava]').click()">
    Pokračovať na opravu →</button></div>`;
  document.getElementById("analyzaObsah").innerHTML = html;
}

/* ---------- oprava ---------- */
function pripravOpravu(a) {
  const el = document.getElementById("opravaObsah");
  const strategie = a.strategie || [];
  let html = `<p class="popis">Vyber postup. Poradie je zoradené od najlepšie
    vyhovujúceho pre tento konkrétny súbor.</p>`;
  strategie.forEach((s, i) => {
    const trieda = s.vhodnost.startsWith("vyborn") ? "dobra"
      : (s.vhodnost.startsWith("stredn") ? "pozor" : "");
    html += `<div class="strategia ${i === 0 ? "vybrana" : ""}" data-id="${esc(s.id)}">
      <h4>${esc(s.nazov)} <span class="znacka ${trieda}">${esc(s.vhodnost)}</span></h4>
      <p>${esc(s.popis)}</p>
      <p><b>Očakávaný výsledok:</b> ${esc(s.ocakavany_vysledok)}</p></div>`;
  });
  stavAplikacie.strategia = strategie.length ? strategie[0].id : null;

  html += `<div class="karta"><h3 style="margin-top:0">Voliteľné nastavenia</h3>
    <div class="volby">
      <div class="volba"><label>Rozlíšenie pôvodného videa (ak ho poznáš)</label>
        <div class="riadok" style="margin:0">
          <input type="number" id="volbaSirka" placeholder="šírka" style="min-width:70px">
          <input type="number" id="volbaVyska" placeholder="výška" style="min-width:70px"></div></div>
      <div class="volba"><label>Snímková frekvencia</label>
        <input type="number" id="volbaFps" value="30"></div>
      <div class="volba"><label>Zdravý vzorový súbor z rovnakého zariadenia (nepovinné)</label>
        <input type="text" id="volbaVzor" placeholder="cesta k zdravému súboru"></div>
      <div class="volba"><label><input type="checkbox" id="volbaOrezat" checked>
        Vyrobiť aj čistú verziu bez poškodeného začiatku</label>
        <label style="margin-top:6px"><input type="checkbox" id="volbaRychle" checked>
        Rýchle hľadanie parametrov (menej kombinácií)</label></div>
    </div>
    <button id="btnSpustit">Spustiť opravu</button></div>
    <div id="priebehOpravy"></div>`;
  el.innerHTML = html;

  el.querySelectorAll(".strategia").forEach(s => s.onclick = () => {
    el.querySelectorAll(".strategia").forEach(x => x.classList.remove("vybrana"));
    s.classList.add("vybrana");
    stavAplikacie.strategia = s.dataset.id;
  });
  document.getElementById("btnSpustit").onclick = spustiOpravu;
}

async function spustiOpravu() {
  const btn = document.getElementById("btnSpustit");
  btn.disabled = true;
  const priebeh = document.getElementById("priebehOpravy");
  priebeh.innerHTML = `<div class="karta"><h3 style="margin-top:0">
    <span class="spinner"></span>Prebieha oprava…</h3><pre class="log" id="logOpravy"></pre></div>`;
  const log = document.getElementById("logOpravy");
  const volby = {
    sirka: +document.getElementById("volbaSirka").value || null,
    vyska: +document.getElementById("volbaVyska").value || null,
    fps: +document.getElementById("volbaFps").value || 30,
    vzor: document.getElementById("volbaVzor").value || null,
    orezat: document.getElementById("volbaOrezat").checked,
    rychle_hladanie: document.getElementById("volbaRychle").checked,
  };
  try {
    const { uloha } = await api("/api/oprava", {
      cesta: stavAplikacie.subor,
      vystup: document.getElementById("vystupPriecinok").value,
      strategia: stavAplikacie.strategia,
      volby,
    });
    sledujUlohu(uloha, riadky => {
      log.textContent += riadky.map(r => r.text).join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
    }, s => {
      btn.disabled = false;
      vykresliVysledok(s, priebeh, log);
    });
  } catch (e) {
    btn.disabled = false;
    priebeh.innerHTML = `<div class="vysledok zly">${esc(e.message)}</div>`;
  }
}

function vykresliVysledok(s, priebeh, log) {
  const karta = priebeh.querySelector(".karta");
  karta.querySelector("h3").innerHTML = s.stav === "hotovo"
    ? "Hotovo" : "Oprava zlyhala";
  if (s.stav === "chyba") {
    priebeh.insertAdjacentHTML("beforeend",
      `<div class="vysledok zly"><b>Chyba:</b> ${esc(s.chyba)}</div>`);
    return;
  }
  const v = s.vysledok || {};
  const k = v.kontrola || {};
  let html = `<div class="vysledok ${v.ok ? "" : "zly"}">
    <b>${esc(v.zhrnutie || "")}</b><br>
    Kontrola prehrateľnosti: ${k.ok
      ? '<span class="znacka dobra">výsledok sa prehráva bez chýb</span>'
      : `<span class="znacka pozor">dekodér hlási ${k.error_count ?? "?"} chýb</span>`}</div>`;
  html += `<div class="karta"><h3 style="margin-top:0">Výsledné súbory</h3>`;
  for (const s2 of (v.vystupy || [])) {
    html += `<div class="subor-vystup"><span>🎬</span><div>
      <code>${esc(s2.cesta)}</code><br><span class="popis">${esc(s2.popis)}</span></div></div>`;
  }
  if ((v.vystupy || []).length) {
    const posledny = v.vystupy[v.vystupy.length - 1].cesta;
    html += `<div class="riadok" style="margin-top:12px">
      <button class="tichy" onclick="otvorPriecinok('${esc(posledny).replace(/'/g, "\\'")}')">
        Otvoriť priečinok s výsledkom</button></div>
      <img class="nahlad" alt="náhľad prvého snímku"
        src="/api/nahlad?token=${encodeURIComponent(TOKEN)}&cesta=${encodeURIComponent(posledny)}"
        onerror="this.style.display='none'">`;
  }
  html += `</div>`;
  priebeh.insertAdjacentHTML("beforeend", html);
}

async function otvorPriecinok(cesta) {
  try { await api("/api/otvorit-priecinok", { cesta }); }
  catch (e) { hlaska("Priečinok sa nepodarilo otvoriť: " + e.message, true); }
}

/* ---------- databáza ---------- */
async function nacitajDatabazu() {
  try {
    const d = await api("/api/databaza");
    let html = `<div class="karta"><div class="mriezka">
      <div class="udaj"><b>Verzia databázy</b><span>${esc(d.suhrn.verzia)}</span></div>
      <div class="udaj"><b>Hlavičiek</b><span>${d.suhrn.pocet_hlaviciek}</span></div>
      <div class="udaj"><b>Z toho vlastných</b><span>${d.suhrn.pocet_vlastnych}</span></div>
      <div class="udaj"><b>Kontajnerov</b><span>${d.suhrn.pocet_kontajnerov}</span></div>
    </div></div>`;

    html += `<div class="karta"><h3 style="margin-top:0">Hlavičky na graftovanie</h3>
      <table><tr><th>Názov</th><th>Značka</th><th>Dĺžka</th><th>Použitie</th><th></th></tr>`;
    for (const h of d.hlavicky) {
      html += `<tr><td>${esc(h.popis)}</td><td><code>${esc(h.major_brand)}</code></td>
        <td>${h.dlzka} B</td><td class="popis" style="margin:0">${esc(h.pouzitie)}</td>
        <td>${h.zdroj === "pouzivatel"
          ? `<button class="tichy" onclick="zmazHlavicku('${esc(h.id)}')">zmazať</button>` : ""}</td></tr>`;
    }
    html += `</table></div>`;

    html += `<div class="karta"><h3 style="margin-top:0">Podporované kontajnery</h3>
      <table><tr><th>Formát</th><th>Prípony</th><th>Index</th><th>Poznámka</th></tr>`;
    for (const c of d.kontajnery) {
      html += `<tr><td>${esc(c.nazov)}</td><td><code>${esc((c.pripony || []).join(" "))}</code></td>
        <td>${esc(c.index)}${c.index_na_konci ? " (na konci)" : ""}</td>
        <td class="popis" style="margin:0">${esc(c.poznamka)}</td></tr>`;
    }
    html += `</table></div>`;

    const r = d.ransomver || {};
    html += `<div class="karta"><h3 style="margin-top:0">Známe spôsoby šifrovania</h3>
      <p class="popis">${esc(r.poznamka || "")}</p><table>
      <tr><th>Vzor</th><th>Popis</th><th>Dôsledok pre záchranu</th></tr>`;
    for (const v of (r.vzory || [])) {
      html += `<tr><td>${esc(v.nazov)}</td><td class="popis" style="margin:0">${esc(v.popis)}</td>
        <td class="popis" style="margin:0">${esc(v.dosledok)}</td></tr>`;
    }
    html += `</table></div>`;

    html += `<div class="karta"><h3 style="margin-top:0">Zdroje</h3><table>`;
    for (const z of (d.suhrn.zdroje || [])) {
      html += `<tr><td><a href="${esc(z.url)}" target="_blank" rel="noopener">${esc(z.nazov)}</a></td>
        <td class="popis" style="margin:0">${esc(z.popis)}</td></tr>`;
    }
    html += `</table></div>`;
    document.getElementById("databazaObsah").innerHTML = html;
  } catch (e) { hlaska(e.message, true); }
}
async function zmazHlavicku(id) {
  try { await api("/api/databaza/zmazat", { id }); nacitajDatabazu(); hlaska("Hlavička zmazaná."); }
  catch (e) { hlaska(e.message, true); }
}
document.getElementById("btnPridatHlavicku").onclick = async () => {
  try {
    await api("/api/databaza/pridat", {
      cesta: document.getElementById("vzorHlavicka").value,
      nazov: document.getElementById("nazovHlavicka").value,
    });
    hlaska("Hlavička pridaná do databázy.");
    nacitajDatabazu();
  } catch (e) { hlaska(e.message, true); }
};

/* ---------- nástroje ---------- */
async function nacitajNastroje() {
  const s = await api("/api/stav");
  const n = s.nastroje;
  const povinne = { ffmpeg: true, ffprobe: false, untrunc: false };
  document.getElementById("stavNastroje").innerHTML =
    Object.entries(n).map(([k, v]) => {
      const trieda = v.available ? "dobra" : (povinne[k] ? "zla" : "pozor");
      const text = v.available ? "áno" : (povinne[k] ? "chýba" : "nepovinný");
      return `${esc(k)}: <span class="znacka ${trieda}">${text}</span>`;
    }).join(" &nbsp; ");
  let html = "";
  for (const [meno, v] of Object.entries(n)) {
    const nepovinny = meno !== "ffmpeg";
    html += `<div class="karta"><h3 style="margin-top:0">${esc(meno)}
      <span class="znacka ${v.available ? "dobra" : (nepovinny ? "pozor" : "zla")}">${v.available
        ? "nájdený" : (nepovinny ? "nenájdený (nepovinný)" : "nenájdený — je potrebný")}</span></h3>
      <div class="riadok"><input type="text" id="cesta_${meno}" value="${esc(v.path || "")}"
        placeholder="cesta k programu ${esc(meno)}">
        <button onclick="ulozNastroj('${meno}')">Uložiť</button></div>
      ${v.version ? `<p class="popis" style="margin:0">${esc(v.version)}</p>` : ""}</div>`;
  }
  html += `<div class="karta"><h3 style="margin-top:0">Kde ich získať</h3>
    <p class="popis">ffmpeg a ffprobe: <a href="https://ffmpeg.org/download.html" target="_blank" rel="noopener">ffmpeg.org/download</a>
    — stiahni balík pre svoj systém a rozbaľ ho, potom sem zadaj cestu k súboru <code>ffmpeg</code>
    (na Windows <code>ffmpeg.exe</code>). Program ich nájde aj sám, ak sú v priečinku
    <code>bin/</code> vedľa programu alebo v systémovej ceste PATH.<br>
    untrunc (nepovinný): <a href="https://github.com/anthwlock/untrunc" target="_blank" rel="noopener">github.com/anthwlock/untrunc</a></p></div>`;
  document.getElementById("nastrojeObsah").innerHTML = html;
  return s;
}
async function ulozNastroj(meno) {
  try {
    await api("/api/nastroje", { [meno]: document.getElementById("cesta_" + meno).value });
    hlaska("Cesta uložená.");
    nacitajNastroje();
  } catch (e) { hlaska(e.message, true); }
}

/* ---------- štart ---------- */
(async () => {
  try {
    const s = await nacitajNastroje();
    await prejst(s.domov);
    await nacitajDatabazu();
    if (!s.nastroje.ffmpeg.available) {
      hlaska("ffmpeg sa nenašiel — nastav k nemu cestu v záložke Nástroje.", true);
      prepniPanel("panel-nastroje");
    }
  } catch (e) { hlaska("Nepodarilo sa načítať stav: " + e.message, true); }
})();
