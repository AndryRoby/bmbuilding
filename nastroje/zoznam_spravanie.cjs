/* Správanie zoznamu realizácií z app.js (časť 5: filter, zoradenie, postupné načítanie) nad malým falošným DOM,
   bez prehliadača a bez balíkov (linka bmb-fb-uplnost, oprava 2, 21. 11. 2026). Volá ho test_build.py
   (TestSestdesiatRealizacii.test_postupne_nacitanie_spravanie) a sabotáž ops/.../fb/uplnost/oprava2_sabotaz_appjs.py.

   node nastroje/zoznam_spravanie.cjs < vstup.json > vystup.json
   Vstup (JSON na stdin):
     app_js   cesta k app.js (z výstupu build.py),
     naraz    hodnota data-naraz zoznamu,
     karty    [{poradie, druh, rok, mesto, nazov}] z vykreslenej stránky /realizacie/ v poradí HTML,
     druhy    hodnoty data-druh tlačidiel filtra, roky, mesta a zoradit hodnoty option zo selectov,
     search   počiatočná časť adresy ?… (napríklad "?zoradit=najstarsie"),
     kroky    [{akcia: "viac" | "druh" | "roky" | "mesto" | "zoradit" | "zrusit", hodnota}].
   Výstup: zoznam stavov (po spustení a po každom kroku): viditelne (data-poradie neskrytých kariet v poradí DOM),
     poradie (všetky karty v poradí DOM), stav (text data-stav), viac_skryte, viac_pocet, prazdne_skryte,
     fokus (data-poradie karty, na ktorej odkaz v tomto kroku dostal fokus, inak null), search (adresa po kroku).
   Z app.js sa vezme presne časť medzi komentármi „// 5. Filter“ a „// 6. Lightbox“ a spustí sa vo vm s premennými
   d (dokument) a vsetky (ako v app.js). Ak sa značky v app.js nenájdu alebo kód siahne na nepodporovaný selektor,
   skript skončí chybou (kód 2), takže test nemôže potichu prejsť. */
'use strict';
const fs = require('fs');
const vm = require('vm');

const vstup = JSON.parse(fs.readFileSync(0, 'utf8'));
const zdroj = fs.readFileSync(vstup.app_js, 'utf8');
const od = zdroj.indexOf('// 5. Filter');
const po = zdroj.indexOf('// 6. Lightbox');
if (od < 0 || po < 0 || po < od) { process.stderr.write('app.js: značky časti 5 a 6 sa nenašli\n'); process.exit(2); }
const kod = zdroj.slice(od, po);

let aktivny = null;

class El {
  constructor(tag, attrs, text) {
    this.tagName = tag.toUpperCase();
    this.attrs = new Map(Object.entries(attrs || {}).map(([k, v]) => [k, String(v)]));
    this._hidden = this.attrs.has('hidden');
    this.attrs.delete('hidden');
    this.children = [];
    this.parent = null;
    this._text = text || '';
    this.listeners = {};
    this.value = this.attrs.has('value') ? this.attrs.get('value') : '';
  }
  get hidden() { return this._hidden; }
  set hidden(v) { this._hidden = !!v; }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  hasAttribute(n) { return this.attrs.has(n); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  appendChild(c) {
    if (c.parent) c.parent.children.splice(c.parent.children.indexOf(c), 1);
    c.parent = this;
    this.children.push(c);
    return c;
  }
  pridaj(...cs) { cs.forEach((c) => this.appendChild(c)); return this; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  vyvolaj(t) { (this.listeners[t] || []).forEach((f) => f.call(this, { type: t, target: this })); }
  focus() { aktivny = this; }
  potomkovia() { const o = []; const ch = (e) => e.children.forEach((c) => { o.push(c); ch(c); }); ch(this); return o; }
  querySelectorAll(sel) { const cast = rozober(sel); return this.potomkovia().filter((e) => sedi(e, cast, this)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

// selektory: zložené z tag, .trieda, [atribút] a [atribút="hodnota"], spojené medzerou (potomok)
function rozober(sel) {
  return sel.trim().split(/\s+/).map((c) => {
    const m = /^([a-z0-9]+)?((?:\.[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/.exec(c);
    if (!m) throw new Error('nepodporovaný selektor: ' + sel);
    const casti = m[2].match(/\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]/g) || [];
    return { tag: m[1] ? m[1].toUpperCase() : null, casti };
  });
}
function sediJeden(e, c) {
  if (c.tag && e.tagName !== c.tag) return false;
  return c.casti.every((p) => {
    if (p[0] === '.') return (e.getAttribute('class') || '').split(/\s+/).includes(p.slice(1));
    const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(p);
    if (m[1] === 'hidden') return e.hidden;
    return m[2] === undefined ? e.hasAttribute(m[1]) : e.getAttribute(m[1]) === m[2];
  });
}
function sedi(e, cast, koren) {
  if (!sediJeden(e, cast[cast.length - 1])) return false;
  let i = cast.length - 2;
  let p = e.parent;
  while (i >= 0 && p && p !== koren.parent) {
    if (sediJeden(p, cast[i])) i--;
    if (p === koren) break;
    p = p.parent;
  }
  return i < 0;
}

// dokument podľa markup build.py (Web.realizacie_zoznam): filtre, stav, zoznam, Zobraziť ďalšie, prázdny výsledok
const E = (tag, attrs, text) => new El(tag, attrs, text);
const html = E('html');
const filtre = E('div', { class: 'filtre', 'data-filtre': '', hidden: '' });
filtre.pridaj(...vstup.druhy.map((x) => E('button', { class: 'filter', 'data-druh': x, 'aria-pressed': x ? 'false' : 'true' }, x || 'Všetky')));
const sel = (atr, hodnoty) => E('select', { [atr]: '' }).pridaj(...hodnoty.map((v) => E('option', { value: v }, v)));
if (vstup.roky) filtre.appendChild(sel('data-roky', vstup.roky));
if (vstup.mesta) filtre.appendChild(sel('data-mesto', vstup.mesta));
if (vstup.zoradit) filtre.appendChild(sel('data-zoradit', vstup.zoradit));
const stav = E('p', { class: 'filtre-stav', 'data-stav': '', 'aria-live': 'polite' });
const zoznam = E('ul', { class: 'karty karty-zoznam', id: 'zoznam-realizacii', 'data-zoznam': '', 'data-naraz': vstup.naraz });
vstup.karty.forEach((k) => {
  const a = E('a', { href: '../realizacie/x/' }, k.nazov);
  zoznam.appendChild(E('li', { class: 'dlazdica', 'data-poradie': k.poradie, 'data-druh': k.druh, 'data-rok': k.rok, 'data-mesto': k.mesto })
    .pridaj(E('h2', { class: 'dlazdica-nazov' }).pridaj(a)));
});
const viacPocet = E('span', { class: 'viac-pocet', 'data-viac-pocet': '' });
const viac = E('button', { class: 'tlacidlo', 'data-viac': '' }, 'Zobraziť ďalšie ').pridaj(viacPocet);
const viacObal = E('p', { class: 'viac-obal', 'data-viac-obal': '', hidden: '' }).pridaj(viac);
const zrusit = E('button', { 'data-zrusit': '' }, 'Zrušiť filter');
const prazdne = E('div', { class: 'prazdne', 'data-prazdne': '', hidden: '' }).pridaj(E('p', {}, 'Pre tento výber nie je žiadna realizácia.'), zrusit);
html.pridaj(E('body').pridaj(filtre, stav, zoznam, viacObal, prazdne));

const doc = {
  querySelector: (s) => html.querySelector(s),
  querySelectorAll: (s) => html.querySelectorAll(s),
  getElementById: (id) => html.potomkovia().find((e) => e.getAttribute('id') === id) || null,
};
const win = {
  location: { pathname: '/realizacie/', search: vstup.search || '' },
  history: { replaceState: (_s, _t, url) => { const q = String(url).indexOf('?'); win.location.search = q >= 0 ? String(url).slice(q) : ''; } },
  Intl,
};
const vsetky = (s, koren) => Array.prototype.slice.call((koren || doc).querySelectorAll(s));

function snimka() {
  const lis = zoznam.children.filter((e) => e.tagName === 'LI');
  let fokus = null;
  for (let e = aktivny; e; e = e.parent) if (e.tagName === 'LI') { fokus = Number(e.getAttribute('data-poradie')); break; }
  return {
    viditelne: lis.filter((e) => !e.hidden).map((e) => Number(e.getAttribute('data-poradie'))),
    poradie: lis.map((e) => Number(e.getAttribute('data-poradie'))),
    stav: stav.textContent,
    viac_skryte: viacObal.hidden,
    viac_pocet: viacPocet.textContent,
    prazdne_skryte: prazdne.hidden,
    filtre_skryte: filtre.hidden,
    fokus,
    search: win.location.search,
  };
}

vm.runInNewContext('(function () {\n' + kod + '\n})();', { d: doc, vsetky, window: win, URLSearchParams, Intl }, { filename: 'app.js#5' });
const stavy = [snimka()];
for (const k of vstup.kroky) {
  aktivny = null;
  if (k.akcia === 'viac') viac.vyvolaj('click');
  else if (k.akcia === 'zrusit') zrusit.vyvolaj('click');
  else if (k.akcia === 'druh') {
    const b = filtre.children.find((e) => e.tagName === 'BUTTON' && e.getAttribute('data-druh') === k.hodnota);
    if (!b) throw new Error('tlačidlo druhu ' + k.hodnota + ' nie je');
    b.vyvolaj('click');
  } else if (['roky', 'mesto', 'zoradit'].includes(k.akcia)) {
    const s = filtre.querySelector('[data-' + k.akcia + ']');
    s.value = k.hodnota;
    s.vyvolaj('change');
  } else throw new Error('neznámy krok ' + k.akcia);
  stavy.push(snimka());
}
process.stdout.write(JSON.stringify(stavy));
