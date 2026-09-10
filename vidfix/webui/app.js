"use strict";
const TOKEN = new URLSearchParams(location.search).get("token") || "";
let stavAplikacie = { subor: null, analyza: null, strategia: null, uloha: null,
                      domov: "", vystup: "", priecinokVidea: "",
                      maFfmpeg: true, vystupOk: true };

/* Postupy, ktoré sa bez ffmpeg nedajú dokončiť. „Nahradenie hlavičky“ medzi ne
   nepatrí — to je čisté prepísanie hlavičky, ktoré ffmpeg nepotrebuje. */
const VYZADUJU_FFMPEG = ["mp4_carve", "ts_resync", "untrunc", "ffmpeg_remux"];

/* Prednastavené rozlíšenia — najčastejšie zdroje domáceho aj dronového videa.
   Slúžia na rekonštrukciu parametrov, keď index videa neprežil. */
const ROZLISENIA = [
  { nazov: "4K UHD", w: 3840, h: 2160 },
  { nazov: "4K DCI", w: 4096, h: 2160 },
  { nazov: "5.4K dron", w: 5312, h: 2988 },
  { nazov: "2.7K dron/GoPro", w: 2704, h: 1520 },
  { nazov: "Full HD", w: 1920, h: 1080 },
  { nazov: "HD", w: 1280, h: 720 },
  { nazov: "4K zvislé", w: 2160, h: 3840 },
  { nazov: "Full HD zvislé", w: 1080, h: 1920 },
];

/* Snímková frekvencia sa dá z poškodeného súboru zistiť len zriedka, preto sa
   pri vyrezávaní zadáva ručne — určuje rýchlosť prehrávania výsledku. */
const FPS_PREDVOLBY = [24, 25, 30, 50, 60, 120];

/* Ktoré nastavenia ktorá stratégia naozaj používa. Ostatné sa skryjú, aby
   používateľ nevypĺňal niečo, čo na výsledok nemá vplyv. */
const VOLBY_STRATEGIE = {
  mp4_graft: ["orezanie"],
  mp4_carve: ["rozlisenie", "fps", "vzor", "hladanie"],
  untrunc: ["rozlisenie", "fps", "vzor"],
  ts_resync: [],
  ffmpeg_remux: [],
};

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
  stavAplikacie.priecinokVidea = priecinok;
  stavAplikacie.vystup = priecinok + "/opravene";
  el.innerHTML = `<b>Vybraný súbor:</b> <code>${esc(cesta)}</code>
    <div class="riadok" style="margin-top:12px">
      <button id="btnAnalyza">Analyzovať súbor</button>
    </div>
    <p class="popis" style="margin:0">Pôvodný súbor sa nikdy nemení — výsledky idú do
      samostatného priečinka, ktorý si vyberieš v kroku 3.</p>`;
  document.getElementById("btnAnalyza").onclick = spustiAnalyzu;
}
document.getElementById("btnPrejst").onclick = () =>
  prejst(document.getElementById("cestaVstup").value);
document.getElementById("cestaVstup").addEventListener("keydown", e => {
  if (e.key === "Enter") prejst(e.target.value);
});

/* ---------- dialóg na výber súboru alebo priečinka ----------
   Používa ten istý serverový prehliadač ako krok 1, aby používateľ nemusel
   nikde vypisovať cesty ručne. */
let dialogHotovo = null;

function postavDialog() {
  if (document.getElementById("prekryv")) return;
  const e = document.createElement("div");
  e.className = "prekryv";
  e.id = "prekryv";
  e.hidden = true;
  e.innerHTML = `<div class="dialog">
    <h3 id="dlgNadpis">Vyber súbor</h3>
    <div class="riadok">
      <input type="text" id="dlgCesta" spellcheck="false">
      <button id="dlgPrejst">Prejsť</button>
    </div>
    <div class="disky" id="dlgDisky"></div>
    <div class="prehliadac" id="dlgZoznam"></div>
    <p class="popis" id="dlgPomoc" style="font-size:12px"></p>
    <div class="riadok" style="margin:12px 0 0">
      <button id="dlgVybrat">Vybrať tento priečinok</button>
      <button class="tichy" id="dlgZrusit">Zrušiť</button>
    </div></div>`;
  document.body.appendChild(e);
  e.onclick = ev => { if (ev.target === e) zavriDialog(null); };
  document.getElementById("dlgZrusit").onclick = () => zavriDialog(null);
  document.getElementById("dlgPrejst").onclick = () =>
    dlgPrejst(document.getElementById("dlgCesta").value);
  document.getElementById("dlgCesta").addEventListener("keydown", ev => {
    if (ev.key === "Enter") dlgPrejst(ev.target.value);
  });
  document.addEventListener("keydown", ev => {
    if (ev.key === "Escape" && !e.hidden) zavriDialog(null);
  });
}

function zavriDialog(vysledok) {
  const e = document.getElementById("prekryv");
  if (e) e.hidden = true;
  if (dialogHotovo) { dialogHotovo(vysledok); dialogHotovo = null; }
}

async function dlgPrejst(cesta) {
  try {
    const v = await api("/api/prehliadac", { cesta });
    document.getElementById("dlgCesta").value = v.cesta;
    document.getElementById("dlgVybrat").dataset.cesta = v.cesta;
    document.getElementById("dlgDisky").innerHTML = (v.disky || [])
      .map(d => `<button data-cesta="${esc(d.cesta)}">${esc(d.nazov)}</button>`).join("");
    document.querySelectorAll("#dlgDisky button").forEach(b =>
      b.onclick = () => dlgPrejst(b.dataset.cesta));
    const iba = document.getElementById("prekryv").dataset.rezim === "priecinok";
    let html = "";
    if (v.rodic) html += `<div class="polozka" data-dir="${esc(v.rodic)}">
      <span class="ikona">↰</span><span>.. (o úroveň vyššie)</span></div>`;
    for (const p of v.polozky) {
      if (!p.priecinok && iba) continue;
      const video = !p.priecinok && jeVideo(p.nazov);
      html += `<div class="polozka ${video ? "video" : ""}" ${p.priecinok
        ? `data-dir="${esc(p.cesta)}"` : `data-file="${esc(p.cesta)}"`}>
        <span class="ikona">${p.priecinok ? "📁" : (video ? "🎬" : "📄")}</span>
        <span>${esc(p.nazov)}</span>
        <span class="velkost">${esc(p.velkost_citatelne)}</span></div>`;
    }
    const zoznam = document.getElementById("dlgZoznam");
    zoznam.innerHTML = html || '<div class="polozka">Priečinok je prázdny.</div>';
    zoznam.querySelectorAll("[data-dir]").forEach(d => d.onclick = () => dlgPrejst(d.dataset.dir));
    zoznam.querySelectorAll("[data-file]").forEach(d =>
      d.onclick = () => zavriDialog(d.dataset.file));
  } catch (e) { hlaska(e.message, true); }
}

/* Otvorí dialóg a vráti vybranú cestu (alebo null pri zrušení). */
function vyberCestu({ nadpis, start, rezim = "oboje", pomoc = "" }) {
  postavDialog();
  const e = document.getElementById("prekryv");
  e.dataset.rezim = rezim;
  e.hidden = false;
  document.getElementById("dlgNadpis").textContent = nadpis;
  document.getElementById("dlgPomoc").textContent = pomoc;
  document.getElementById("dlgVybrat").onclick = () =>
    zavriDialog(document.getElementById("dlgVybrat").dataset.cesta);
  dlgPrejst(start || stavAplikacie.domov);
  return new Promise(res => { dialogHotovo = res; });
}

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

  html += `<div class="karta"><h3 style="margin-top:0">Kam uložiť výsledok</h3>
    <div class="riadok">
      <input type="text" id="vystupPriecinok" value="${esc(stavAplikacie.vystup)}"
             spellcheck="false">
      <button class="tichy" id="btnVyberVystup">Prehľadať…</button>
    </div>
    <div class="disky" id="rychleVystupy"></div>
    <p class="popis" id="stavVystupu" style="margin:6px 0 0"></p></div>

    <div class="karta"><h3 style="margin-top:0">Voliteľné nastavenia</h3>
    <div class="volby">
      <div class="volba siroka" data-volba="rozlisenie"><label>Rozlíšenie pôvodného videa
        (ak ho poznáš — hľadanie parametrov sa tým rádovo zrýchli)</label>
        <div class="riadok" style="margin:0 0 8px">
          <input type="number" id="volbaSirka" placeholder="šírka" style="min-width:70px">
          <input type="number" id="volbaVyska" placeholder="výška" style="min-width:70px"></div>
        <div class="disky" id="predvolbyRozlisenia"></div></div>
      <div class="volba siroka" data-volba="fps"><label>Snímková frekvencia výsledku</label>
        <input type="number" id="volbaFps" value="30" style="max-width:120px">
        <div class="disky" id="predvolbyFps" style="margin-top:8px"></div></div>
      <div class="volba" data-volba="vzor"><label>Iné video z tej istej kamery
        (nepovinné) — <b>stačí aj poškodené</b></label>
        <div class="riadok" style="margin:0">
          <input type="text" id="volbaVzor" placeholder="nevybrané — prehľadá sa okolie">
          <button class="tichy" id="btnVyberVzor">Prehľadať…</button>
        </div>
        <p class="popis" style="margin:6px 0 0;font-size:11.5px">Program automaticky
        prehľadá aj priečinok, v ktorom leží opravovaný súbor. Parametre kamery sa
        dajú vytiahnuť aj zo zašifrovaných videí — ransomvér poškodí len začiatok,
        takže hlavička na konci súboru väčšinou prežije.</p></div>
      <div class="volba" data-volba="orezanie"><label><input type="checkbox" id="volbaOrezat" checked>
        Vyrobiť aj čistú verziu bez poškodeného začiatku</label></div>
      <div class="volba" data-volba="hladanie"><label><input type="checkbox" id="volbaRychle" checked>
        Rýchle hľadanie parametrov (menej kombinácií)</label></div>
    </div>
    <button id="btnSpustit">Spustiť opravu</button>
    <p class="popis" id="poznamkaSpustit" style="margin:8px 0 0"></p></div>
    <div id="priebehOpravy"></div>`;
  el.innerHTML = html;

  el.querySelectorAll(".strategia").forEach(s => s.onclick = () => {
    el.querySelectorAll(".strategia").forEach(x => x.classList.remove("vybrana"));
    s.classList.add("vybrana");
    stavAplikacie.strategia = s.dataset.id;
    zobrazVolby();
  });

  // prednastavené rozlíšenia
  document.getElementById("predvolbyRozlisenia").innerHTML = ROZLISENIA
    .map((r, i) => `<button data-i="${i}">${esc(r.nazov)} · ${r.w}×${r.h}</button>`).join("")
    + `<button data-i="-1">Neviem — skúsiť všetky</button>`;
  document.querySelectorAll("#predvolbyRozlisenia button").forEach(b => b.onclick = () => {
    const r = ROZLISENIA[+b.dataset.i];
    document.getElementById("volbaSirka").value = r ? r.w : "";
    document.getElementById("volbaVyska").value = r ? r.h : "";
    document.querySelectorAll("#predvolbyRozlisenia button")
      .forEach(x => x.classList.toggle("vybrany", x === b));
  });

  // prednastavené snímkové frekvencie
  document.getElementById("predvolbyFps").innerHTML = FPS_PREDVOLBY
    .map(f => `<button data-f="${f}">${f}</button>`).join("");
  document.querySelectorAll("#predvolbyFps button").forEach(b => b.onclick = () => {
    document.getElementById("volbaFps").value = b.dataset.f;
    document.querySelectorAll("#predvolbyFps button")
      .forEach(x => x.classList.toggle("vybrany", x === b));
  });

  // rýchla voľba výstupného priečinka
  const d = stavAplikacie.domov;
  const miesta = [
    { nazov: "Vedľa videa", cesta: stavAplikacie.priecinokVidea + "/opravene" },
    { nazov: "Plocha", cesta: d + "/Desktop/opravene-videa" },
    { nazov: "Dokumenty", cesta: d + "/Documents/opravene-videa" },
    { nazov: "Domovský priečinok", cesta: d + "/opravene-videa" },
  ];
  document.getElementById("rychleVystupy").innerHTML = miesta
    .map((m, i) => `<button data-i="${i}">${esc(m.nazov)}</button>`).join("");
  document.querySelectorAll("#rychleVystupy button").forEach(b => b.onclick = () => {
    document.getElementById("vystupPriecinok").value = miesta[+b.dataset.i].cesta;
    overVystup();
  });
  document.getElementById("vystupPriecinok").addEventListener("change", overVystup);
  document.getElementById("btnSpustit").onclick = spustiOpravu;

  document.getElementById("btnVyberVzor").onclick = async () => {
    const c = await vyberCestu({
      nadpis: "Vyber iné video z tej istej kamery — alebo priečinok, kde ich máš",
      start: stavAplikacie.priecinokVidea || stavAplikacie.domov,
      rezim: "oboje",
      pomoc: "Klikni na video, alebo tlačidlom nižšie vyber celý priečinok. "
           + "Poškodené súbory sú v poriadku — parametre kamery sa dajú vytiahnuť aj z nich.",
    });
    if (c) document.getElementById("volbaVzor").value = c;
  };
  document.getElementById("btnVyberVystup").onclick = async () => {
    const c = await vyberCestu({
      nadpis: "Vyber priečinok, kam sa uloží výsledok",
      start: document.getElementById("vystupPriecinok").value || stavAplikacie.domov,
      rezim: "priecinok",
      pomoc: "Prejdi do priečinka a potvrď tlačidlom nižšie.",
    });
    if (c) {
      document.getElementById("vystupPriecinok").value = c;
      overVystup();
    }
  };
  zobrazVolby();
  overVystup();
}

/* Ukáže len tie nastavenia, ktoré zvolená stratégia naozaj používa. */
function zobrazVolby() {
  const pouzite = VOLBY_STRATEGIE[stavAplikacie.strategia] || [];
  document.querySelectorAll("[data-volba]").forEach(v => {
    v.hidden = !pouzite.includes(v.dataset.volba);
  });
  const karta = document.querySelector("[data-volba]")?.closest(".karta");
  if (karta) karta.hidden = pouzite.length === 0;
  aktualizujSpustit();
}

/* Tlačidlo Spustiť opravu je aktívne, len keď sa dá kam zapisovať a keď je
   k dispozícii všetko, čo zvolený postup potrebuje. */
function aktualizujSpustit() {
  const btn = document.getElementById("btnSpustit");
  const poznamka = document.getElementById("poznamkaSpustit");
  if (!btn) return;
  const chybaFfmpeg = !stavAplikacie.maFfmpeg
    && VYZADUJU_FFMPEG.includes(stavAplikacie.strategia);
  btn.disabled = chybaFfmpeg || !stavAplikacie.vystupOk;
  if (poznamka) {
    poznamka.innerHTML = chybaFfmpeg
      ? `<span class="znacka zla">chýba ffmpeg</span> Tento postup ho potrebuje.
         Nainštaluj ho podľa záložky <b>Nástroje</b>, alebo zvoľ postup
         „Nahradenie hlavičky“, ktorý ffmpeg nepotrebuje.`
      : (!stavAplikacie.maFfmpeg
         ? `<span class="znacka pozor">ffmpeg chýba</span> Oprava prebehne,
            ale výsledok sa nedá overiť ani orezať.` : "");
  }
}

/* Overí, či sa do zvoleného priečinka dá zapisovať. Externé disky (NTFS) býva
   macOS pripojený len na čítanie — vtedy sa to má používateľ dozvedieť hneď,
   nie až keď oprava po niekoľkých minútach spadne. */
async function overVystup() {
  const pole = document.getElementById("vystupPriecinok");
  const stav = document.getElementById("stavVystupu");
  if (!pole || !stav) return;
  stavAplikacie.vystup = pole.value;
  try {
    const v = await api("/api/kontrola-vystupu", { cesta: pole.value });
    stavAplikacie.vystupOk = !!v.zapisovatelny;
    stav.innerHTML = v.zapisovatelny
      ? `<span class="znacka dobra">priečinok je v poriadku</span> ` + esc(v.popis || "")
      : `<span class="znacka zla">nedá sa doň zapisovať</span> ` + esc(v.popis || "")
        + ` — vyber iné miesto tlačidlom vyššie.`;
    aktualizujSpustit();
  } catch (e) { stav.textContent = e.message; }
}

function zozbierajVolby() {
  return {
    sirka: +document.getElementById("volbaSirka").value || null,
    vyska: +document.getElementById("volbaVyska").value || null,
    fps: +document.getElementById("volbaFps").value || 30,
    vzor: document.getElementById("volbaVzor").value || null,
    orezat: document.getElementById("volbaOrezat").checked,
    rychle_hladanie: document.getElementById("volbaRychle").checked,
  };
}

async function spustiOpravu() {
  const btn = document.getElementById("btnSpustit");
  btn.disabled = true;
  const priebeh = document.getElementById("priebehOpravy");
  priebeh.innerHTML = `<div class="karta"><h3 style="margin-top:0">
    <span class="spinner"></span>Prebieha oprava…</h3><pre class="log" id="logOpravy"></pre></div>`;
  const log = document.getElementById("logOpravy");
  const volby = zozbierajVolby();
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
    Kontrola prehrateľnosti: ${k.ok === true
      ? '<span class="znacka dobra">výsledok sa prehráva bez chýb</span>'
      : (k.ok === null
         ? '<span class="znacka pozor">nedala sa spraviť — chýba ffmpeg</span>'
         : `<span class="znacka pozor">dekodér hlási ${k.error_count ?? "?"} chýb</span>`)}</div>`;
  html += `<div class="karta"><h3 style="margin-top:0">Výsledné súbory</h3>`;
  for (const s2 of (v.vystupy || [])) {
    html += `<div class="subor-vystup"><span>🎬</span><div>
      <code>${esc(s2.cesta)}</code><br><span class="popis">${esc(s2.popis)}</span></div></div>`;
  }
  if ((v.vystupy || []).length) {
    const posledny = v.vystupy[v.vystupy.length - 1].cesta;
    html += `<div class="riadok" style="margin-top:12px">
      <button class="tichy" onclick="otvorPriecinok('${esc(posledny).replace(/'/g, "\\'")}')">
        Otvoriť priečinok s výsledkom</button>
      <button id="btnDavka">Opraviť rovnako aj ostatné videá…</button></div>
      <img class="nahlad" alt="náhľad prvého snímku"
        src="/api/nahlad?token=${encodeURIComponent(TOKEN)}&cesta=${encodeURIComponent(posledny)}"
        onerror="this.style.display='none'">`;
  }
  html += `</div>`;
  priebeh.insertAdjacentHTML("beforeend", html);
  pripojDavku();
}

/* ---------- dávková oprava ----------
   Nastavenia, ktoré na prvom videu zabrali, sa použijú na celý priečinok.
   Postup sa však volí pre každý súbor zvlášť: v jednom priečinku bývajú aj
   súbory s prežitým indexom, aj také, ktorým index neprežil. */
function pripojDavku() {
  const btn = document.getElementById("btnDavka");
  if (!btn) return;
  btn.onclick = async () => {
    const priecinok = await vyberCestu({
      nadpis: "Vyber priečinok s videami, ktoré sa majú opraviť rovnako",
      start: stavAplikacie.priecinokVidea || stavAplikacie.domov,
      rezim: "priecinok",
      pomoc: "Prejdi do priečinka s ostatnými poškodenými videami a potvrď ho. "
           + "Postup sa pre každý súbor zvolí automaticky podľa toho, čo v ňom prežilo.",
    });
    if (priecinok) spustiDavku(priecinok);
  };
}

async function spustiDavku(priecinok) {
  const priebeh = document.getElementById("priebehOpravy");
  try {
    const zoznam = await api("/api/zoznam-videi",
      { priecinok, vynechaj: [stavAplikacie.subor] });
    if (!zoznam.subory.length) {
      hlaska("V tomto priečinku sa nenašli žiadne ďalšie videá.", true);
      return;
    }
    const volby = zozbierajVolby();
    priebeh.insertAdjacentHTML("beforeend", `<div class="karta" id="kartaDavky">
      <h3 style="margin-top:0"><span class="spinner"></span>
        Dávková oprava — ${zoznam.subory.length} súborov</h3>
      <p class="popis">${zoznam.subory.map(s => esc(s.nazov)).join(", ")}</p>
      <div class="riadok"><button class="tichy" id="btnPrerusit">Prerušiť</button></div>
      <pre class="log" id="logDavky"></pre></div>`);
    const log = document.getElementById("logDavky");
    const { uloha } = await api("/api/davka", {
      priecinok,
      vynechaj: [stavAplikacie.subor],
      vystup: document.getElementById("vystupPriecinok").value,
      volby,
    });
    document.getElementById("btnPrerusit").onclick = async () => {
      try { await api("/api/uloha/zrusit", { id: uloha }); hlaska("Prerušujem po dobehnutí súboru…"); }
      catch (e) { hlaska(e.message, true); }
    };
    sledujUlohu(uloha, riadky => {
      log.textContent += riadky.map(r => r.text).join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
    }, s => vykresliDavku(s));
  } catch (e) { hlaska(e.message, true); }
}

function vykresliDavku(s) {
  const karta = document.getElementById("kartaDavky");
  if (!karta) return;
  const btn = document.getElementById("btnPrerusit");
  if (btn) btn.remove();
  if (s.stav === "chyba") {
    karta.querySelector("h3").textContent = "Dávka zlyhala";
    karta.insertAdjacentHTML("beforeend",
      `<div class="vysledok zly">${esc(s.chyba)}</div>`);
    return;
  }
  const v = s.vysledok || {};
  karta.querySelector("h3").textContent =
    `Dávková oprava — ${v.hotove || 0} z ${v.pocet || 0} hotových`;
  let html = `<div class="vysledok ${v.zlyhane ? "" : ""}"><b>${esc(v.zhrnutie || "")}</b></div>
    <table><tr><th>Súbor</th><th>Stav</th><th>Postup</th><th>Výsledok</th></tr>`;
  for (const r of (v.vysledky || [])) {
    html += `<tr><td>${esc(r.subor)}</td>
      <td>${r.ok ? '<span class="znacka dobra">opravené</span>'
                 : '<span class="znacka zla">zlyhalo</span>'}</td>
      <td>${esc(r.strategia || "—")}</td>
      <td class="popis" style="margin:0">${r.ok
        ? esc((r.vystupy || []).map(x => x.cesta.split(/[\\/]/).pop()).join(", "))
        : esc(r.chyba || "")}</td></tr>`;
  }
  html += `</table>`;
  karta.insertAdjacentHTML("beforeend", html);
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
  stavAplikacie.maFfmpeg = !!(n.ffmpeg && n.ffmpeg.available);
  aktualizujSpustit();
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
    stavAplikacie.domov = s.domov;
    await prejst(s.domov);
    await nacitajDatabazu();
    if (!s.nastroje.ffmpeg.available) {
      hlaska("ffmpeg sa nenašiel — nastav k nemu cestu v záložke Nástroje.", true);
      prepniPanel("panel-nastroje");
    }
  } catch (e) { hlaska("Nepodarilo sa načítať stav: " + e.message, true); }
})();
