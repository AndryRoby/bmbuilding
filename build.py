#!/usr/bin/env python3
"""Generátor statického webu BM Building (bmbuilding.sk).

Číta obsah/ (firma.json, realizacie/*.json, novinky/*.json, obrazky/) a sablona/ (texty, štýl, app.js, logo,
pravidlá fotiek, presmerovania, písmo) a zapíše hotový web do web/. Python 3 so štandardnou knižnicou a Pillow, bez siete,
deterministicky (rovnaký vstup = rovnaké bajty, žiadny čas behu vo výstupe).

  python build.py               ostrý beh: index, sitemap.xml, robots.txt so Sitemap, CNAME, .nojekyll
  python build.py --nahlad      náhľad: noindex na každej stránke, robots.txt Disallow: /, bez sitemap
  python build.py --vystup DIR  iný výstupný priečinok (predvolený web/ vedľa build.py)
  python build.py --obsah DIR   iný priečinok obsahu (predvolený obsah/ vedľa build.py)

V GitHub Actions (GITHUB_ACTIONS=true) ide ostrý beh len s premennou prostredia BMB_OSTRY=ano, inak náhľad:
náhľad na github.io nesmie ísť do indexu, kým doména bmbuilding.sk nie je prepnutá (brána pokus 1, správnosť 1).

Pred zápisom zmaže len súbory, ktoré zapísal predchádzajúci beh (zoznam vo výstupe .zostava.json), a to až keď
sú všetky nové obrázky vyrobené. Obrázky s rovnakým odtlačkom vstupu ostanú a znova sa nekódujú.
"""
import argparse
import base64
import datetime
import hashlib
import html
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageMath, ImageOps, features

KOREN = Path(__file__).resolve().parent
SABLONA = KOREN / 'sablona'
# hotové webové verzie fotiek (nastroje/predlohy_web.py): manifest.json podľa sha256 zdrojovej fotky z obsah/
FOTKY_WEB = KOREN / 'fotky-web'
# obr-2 (linka bmb-fotky): sRGB cez ImageCms, exif_transpose, bez metadát, AVIF a WebP s kvalitou z nastavení
VERZIA_OBR = 'obr-2|srgb|bez-metadat|lanczos|avif-s6-t1|webp-m6'
VERZIA_OG = 'og-3|znacka|jpeg-q82'
# 720: telefóny s DPR do 1,8 (Lighthouse mobil 412 px x 1,75) brali 960 a LCP realizácie s fotkami bolo 2,6 s (kolo 1).
# 1920 a 2400 (linka bmb-fotky): lightbox na 1440 px s hustotou 2 potrebuje okolo 2235 px, prvá veľká bunka galérie
# 1754 px (fotky-kvalita/supis.md); len AVIF, WebP záloha končí na WEBP_MAX (prehliadač bez AVIF dostane to čo dnes)
SIRKY = (480, 720, 960, 1440, 1920, 2400)
WEBP_MAX = 1440
# štandardná šírka sa vynechá, keď je predloha od nej širšia menej ako o 10 % (1989 px: 1920 by bola takmer tá istá)
SIRKA_BLIZKO = 1.1
MAX_VSTUP = 2000
# kvalita kódovania z obsahu (nová fotka bez hotovej webovej verzie, napríklad v GitHub Actions); nastavenia.json
# kvalita_fotiek ju prepíše. Kalibrácia: ops/weby-na-mieru/klienti/bmbuilding/fotky-kvalita/KODOVANIE.md
KVALITA_PREDVOLENA = {'avif': 60, 'webp': 80}
KVALITA = dict(KVALITA_PREDVOLENA)
AVIF_RYCHLOST = 6
FORMATY = ('avif', 'webp')
MIME = {'avif': 'image/avif', 'webp': 'image/webp'}
OG_W, OG_H = 1200, 630
MANIFEST = '.zostava.json'
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
DATUM_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
POMLCKY = '\u2012\u2013\u2014\u2015\u2212'

# značka (dizajn 3, ops/weby-na-mieru/klienti/bmbuilding/dizajn-3/logo/LOGO.md): modrá nameraná v logu 2026
FARBA_BM = (0x17, 0x47, 0x6F)
FARBA_BIELA = (0xFF, 0xFF, 0xFF)
FARBA_BM_200 = (0xB9, 0xCD, 0xDD)

# šípky a krížik z ukážky Ateliér Olovnica (štýl Čiara): vlasová čiara 1,5 px, smer, nie ozdoba
SVG_SIPKA = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
             'aria-hidden="true"><path d="M2 9h13M10 4l5 5-5 5"/></svg>')
SVG_SIPKA_SPAT = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
                  'aria-hidden="true"><path d="M16 9H3M8 4 3 9l5 5"/></svg>')
SVG_VON = ('<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" '
           'aria-hidden="true"><path d="M4 10 10 4M5 3.5h5.5V9"/></svg>')
SVG_KRIZIK = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
              'aria-hidden="true"><path d="M4 4l10 10M14 4 4 14"/></svg>')
# vlasové čiary 12 stĺpcov cez celú stránku (najrozpoznateľnejší prvok štýlu, OLOVNICA-ROZBOR.md 6a)
CIARY_HTML = '<div class="ciary" aria-hidden="true"><div class="ciary-vnutro">' + '<span></span>' * 12 + '</div></div>'
SVG_SIPKA_DOLE = ('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
                  'aria-hidden="true"><path d="M8 2v11M3.5 8.5 8 13l4.5-4.5"/></svg>')
# ikona rámu fotky pri tlačidle Fotky v páse (ukážka: ikona pôdorysu pri tlačidle Pôdorys)
SVG_FOTKY = ('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" '
             'aria-hidden="true"><path d="M1.5 3.5h13v9h-13zM1.5 10.5l3.5-3.5 3 3 2-2 4.5 4.5"/></svg>')
# značka pohľadu A pri fotke úvodu (ukážka U:730)
SVG_POHLAD_ZNAK = ('<svg class="pohlad-znak" width="34" height="22" viewBox="-11 -11 34 22" aria-hidden="true">'
                   '<path d="M5.5-6.5L17 0L5.5 6.5Z"/><circle r="9.5"/><text y="4">A</text></svg>')
# písma s preloadom (Geist latinka a latinka s diakritikou); Geist Mono bez preloadu: na mobile je až pod prvou
# obrazovkou a na počítači vo výkrese, ktorý sa kreslí 1,4 s (PLAN-1-1.md časť 8 bod 3)
PRELOAD_PISMA = ('geist-latin.woff2', 'geist-latin-ext.woff2')
PISMA = ('geist-latin.woff2', 'geist-latin-ext.woff2', 'geist-mono-latin.woff2', 'geist-mono-latin-ext.woff2',
         'OFL-Geist.txt')
# favicon (variant A, ops/weby-na-mieru/klienti/bmbuilding/favicon/VYBER.md): hotové súbory zo sablona/favicon/ do koreňa
# webu; favicon.svg je sablona/logo-favicon.svg. Cesty v site.webmanifest sú relatívne k manifestu (náhľad aj ostrý web).
FAVICON_SUBORY = ('favicon.ico', 'favicon-32.png', 'favicon-48.png', 'apple-touch-icon.png', 'icon-192.png', 'icon-512.png',
                  'site.webmanifest')
# jediný vložený skript (hash v CSP): trieda js pre pohyb, ktorý čaká na vstup do obrazu; keď app.js do 4 s nepribehne,
# trieda zmizne a všetko ostane v konečnom stave (obsah sa nikdy neskrýva natrvalo)
SKRIPT_JS = ("(function(d){d.classList.add('js');setTimeout(function(){if(window.BMB==null)d.classList.remove('js')},4000)})"
             "(document.documentElement)")
# bez JS spodná lišta Zavolať a Napísať e-mail na mobile stojí stále (päta má pod sebou miesto, nič nezakryje)
# a fotky kariet pásu z noscript namiesto prázdnych (ukážka U:17)
NOSCRIPT_CSS = 'img[data-src]{display:none}@media (max-width:767px){.lista{transform:none;visibility:visible}}'
# typy fotiek (sablona/fotky-pravidla.json): ktoré nikdy nie sú obal dlaždice
TYPY_FOTIEK = ('HE', 'LH', 'LR', 'R', 'D', 'I', 'P')
NIE_OBAL = ('D', 'P')

# kategórie realizácií (SMER.md 2.7): odvodené z názvu, potom z textu; pole kategoria v obsahu má prednosť
KATEGORIE = (('bytove', 'Bytové domy'), ('rodinne', 'Rodinné domy'), ('rekonstrukcie', 'Rekonštrukcie'), ('ine', 'Iné'))
KAT_VZORY = {
    'rodinne': re.compile(r'^RD\b|rodinn|dvojdom|\bvil[ay]\b', re.I),
    'rekonstrukcie': re.compile(r'rekonštrukc|stavebné úpravy|nadstavb|\bobnov[ay]\b', re.I),
    'bytove': re.compile(r'bytov|rezidenci|polyfunkčn|apartmán', re.I),
}
KAT_PORADIE_NAZOV = ('rodinne', 'rekonstrukcie', 'bytove')
KAT_PORADIE_TEXT = ('rekonstrukcie', 'rodinne', 'bytove')
MAX_FOTIEK_NOVINKY = 10
# pás úvodu ukazuje najlepšie stavby, nikdy všetky (zadanie bmb-fb-uplnost bod 4): najviac 10 kariet
PAS_MAX = 10
# zoznam realizácií: JS ukáže prvých REALIZACIE_NARAZ kariet výberu a tlačidlo Zobraziť ďalšie (nastavenia
# realizacie_naraz); bez JS sú v HTML a viditeľné všetky karty (SEO, odkaz na každú realizáciu)
REALIZACIE_NARAZ = 12

CISLA = ' class="cisla"'
# oddeľovač „·“ s pevnou medzerou pred sebou: pri zalomení ostane na konci riadka, nezačne nový
# (brána dizajnu pokus 2, nález 3)
BODKA = '\u00a0· '

# stred značky pohľadu A v jednotkách výkresu (široká a úzka kompozícia): vľavo pred fasádou, mimo rezu a popisov;
# styl.css .pohlad-odkaz počíta polohu odkazu z tých istých čísel (viewBox -150 -30 1480 910 a -200 -30 890 910)
POHLAD_ZNACKA = {'s': (-75, 330), 'u': (-100, 330)}
# oddeľovač častí popisu fotky v lightboxe (ten istý reťazec delí a skladá app.js vo funkcii ukaz)
LB_ODDELOVAC = ' · '

VAROVANIA = []
# Prísne varovania (fb-ostro A4, OPONENT.md S2): fotka sekcie z nastavenia.json (uvod_foto, firma_foto, postup_foto)
# alebo realizácia z pas_vyber, ktorá v obsahu chýba. Lokálne je to varovanie a sekcia sa zostaví bez nej; v GitHub
# Actions (GITHUB_ACTIONS=true) z nich main urobí chybu zostavy (kód 1), nasadenie sa nespustí a ostane posledný dobrý web.
PRISNE_VAROVANIA = []


def varuj(sprava):
    VAROVANIA.append(sprava)
    print('VAROVANIE: ' + sprava, file=sys.stderr)


def varuj_prisne(sprava):
    """Varovanie, ktoré je v GitHub Actions chybou zostavy (PRISNE_VAROVANIA, main)."""
    if sprava not in PRISNE_VAROVANIA:
        PRISNE_VAROVANIA.append(sprava)
    varuj(sprava)


class ChybaZostavy(Exception):
    pass


# ---------------------------------------------------------------- texty a escapovanie

def cisty(hodnota):
    """Bezpečný reťazec z ľubovoľnej hodnoty JSON: bez riadiacich znakov, bez pomlčiek em a en, bez výkričníkov."""
    if hodnota is None or isinstance(hodnota, bool):
        return ''
    if isinstance(hodnota, (int, float)):
        hodnota = str(hodnota)
    if not isinstance(hodnota, str):
        return ''
    s = ''.join(ch for ch in hodnota if ch in '\n' or (ord(ch) >= 32 and not 0x7F <= ord(ch) < 0xA0))
    s = s.replace('\r', '').replace('\t', ' ')
    # rozsah rokov alebo mesiacov: 2021-2022, 2021 – 2022, 06/2013–11/2013 dá „až“
    s = re.sub(r'(?<=\d)\s*[' + POMLCKY + r']\s*(?=\d)', ' až ', s)
    s = re.sub(r'(\b\d{4}|\b\d{1,2}/\d{4})\s*-\s*(\d{4}\b|\d{1,2}/\d{4}\b)', r'\1 až \2', s)
    s = re.sub(r'\s+[' + POMLCKY + r']\s+', ', ', s)
    s = re.sub(r'[' + POMLCKY + r']', '-', s)
    s = re.sub(r'([.?])?!+', lambda m: m.group(1) or '.', s)
    s = re.sub(r'[ \u00a0]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    return s.strip()


def jeden_riadok(hodnota):
    return re.sub(r'\s+', ' ', cisty(hodnota)).strip()


def esc(s):
    return html.escape(s, quote=True)


JEDNOPISMENOVE = re.compile(r'(?<![\w&])([aikosuvzAIKOSUVZ]) ')


def sadzba(s):
    """Slovenská sadzba nad už escapovaným textom: nezalomiteľné medzery."""
    s = JEDNOPISMENOVE.sub('\\1\u00a0', s)
    s = re.sub(r'(?<=\d)\. (?=\d)', '.\u00a0', s)
    s = re.sub(r'(?<=\d) (?=\d)', '\u00a0', s)
    s = re.sub(r'(?<=\d) (?=(x|m|m²|km|€|%|ks|EUR)\b)', '\u00a0', s)
    # „6 x 4-izbové“: násobok sa nezalomí ani za x (snímka 390 px v kole 1 mala „6 x“ na konci riadka)
    s = re.sub(r'(?<=\d\u00a0x) (?=\d)', '\u00a0', s)
    # „3-izbové“: spojovník za číslicou sa nezalomí (snímka detailu Vtáčnik v2 mala „6 x 3-“ na konci riadka);
    # Geist nem\u00e1 nezalomite\u013en\u00fd spojovn\u00edk U+2011, preto cel\u00e9 slovo v span.nz s white-space:nowrap
    s = re.sub(r'(?<![\w-])(\d+-\w+)', r'<span class="nz">\1</span>', s)
    s = re.sub(r'\b(č\.|ev\.|Ing\.|arch\.|§|ods\.|písm\.) ', '\\1\u00a0', s)
    s = re.sub(r'(§) ', '\\1\u00a0', s)
    return s


def T(s):
    """Text do HTML (escapovaný a so sadzbou)."""
    return sadzba(esc(s))


def kluc_mesta(mesto):
    """Kľúč mesta do adresy a atribútu data-mesto: malé písmená bez diakritiky, [a-z0-9-] (Bratislava -> bratislava)."""
    s = unicodedata.normalize('NFKD', mesto or '').encode('ascii', 'ignore').decode('ascii').lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def ld_json(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=False)
    return s.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def sha(s):
    return "'sha256-" + base64.b64encode(hashlib.sha256(s.encode('utf-8')).digest()).decode() + "'"


def tvar(n, jedno, dve_styri, pat):
    if n == 1:
        return f'{n} {jedno}'
    if 2 <= n <= 4:
        return f'{n} {dve_styri}'
    return f'{n} {pat}'


def platny_datum(s):
    """RRRR-MM-DD, ktorý existuje v kalendári (2026-13-45 nie), inak ''."""
    s = s if isinstance(s, str) else ''
    if not DATUM_RE.match(s):
        return ''
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return ''
    return s


def datum_text(iso):
    m = DATUM_RE.match(platny_datum(iso or ''))
    if not m:
        return ''
    return f'{int(m.group(3))}. {int(m.group(2))}. {m.group(1)}'


def s_bodkou(v):
    """Veta s bodkou na konci, bez zdvojenia („Kappa 1 s.r.o.“ nedá „s.r.o..“)."""
    v = v.rstrip()
    return v if v.endswith(('.', '?')) else v + '.'


def skrat(s, limit):
    if len(s) <= limit:
        return s
    kus = s[:limit + 1]
    if ' ' in kus:
        kus = kus[:kus.rindex(' ')]
    else:
        kus = kus[:limit]
    return kus.rstrip(' ,.;:')


def zaciatok_velky(s):
    return s[:1].upper() + s[1:] if s else s


def male(s):
    """Rola do vety: prvé písmeno malé, ak druhé nie je veľké (TDI ostane)."""
    if len(s) > 1 and s[1].isupper():
        return s
    return s[:1].lower() + s[1:]


def bezpecna_url(u, hostitelia=None):
    u = jeden_riadok(u)
    if not u:
        return ''
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return ''
    if p.scheme != 'https' or not p.netloc:
        return ''
    if hostitelia and not any(p.hostname == h or (p.hostname or '').endswith('.' + h) for h in hostitelia):
        return ''
    cesta = urllib.parse.quote(urllib.parse.unquote(p.path), safe='/%:@-._~')
    dotaz = urllib.parse.quote(urllib.parse.unquote(p.query), safe='=&%:@-._~+,')
    return urllib.parse.urlunsplit(('https', p.netloc, cesta, dotaz, ''))


# ---------------------------------------------------------------- načítanie obsahu

def nacitaj_json(cesta):
    # utf-8-sig: súbor uložený v Poznámkovom bloku so znakom BOM sa načíta tiež (brána pokus 2, správnosť 3)
    try:
        return json.loads(Path(cesta).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as e:
        varuj(f'{cesta}: nedá sa načítať ({e.__class__.__name__})')
        return None


def nacitaj_firmu(obsah):
    d = nacitaj_json(obsah / 'firma.json')
    if not isinstance(d, dict):
        raise ChybaZostavy('obsah/firma.json chýba alebo nie je objekt JSON')
    adresa = d.get('adresa') if isinstance(d.get('adresa'), dict) else {}
    tel = jeden_riadok(d.get('telefon'))
    tel_href = re.sub(r'[^\d+]', '', jeden_riadok(d.get('telefon_href')) or tel)
    email = jeden_riadok(d.get('email'))
    if not re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email):
        if email:
            varuj('firma.json: e-mail nemá platný tvar, vynechaný')
        email = ''
    f = {
        'meno': jeden_riadok(d.get('meno')) or 'BM Building, s.r.o.',
        'konatel': jeden_riadok(d.get('konatel')),
        'ulica': jeden_riadok(adresa.get('ulica')),
        'psc': jeden_riadok(adresa.get('psc')),
        'mesto': jeden_riadok(adresa.get('mesto')),
        'ico': jeden_riadok(d.get('ico')),
        'ic_dph': jeden_riadok(d.get('ic_dph')),
        'zapis': jeden_riadok(d.get('zapis_or')) if d.get('zapis_or_overit') is not True else '',
        'den_zapisu': jeden_riadok(d.get('den_zapisu')),
        'tel': tel,
        'tel_href': tel_href,
        'email': email,
        'facebook': bezpecna_url(d.get('facebook'), ['facebook.com', 'fb.com']),
        'posobnost': jeden_riadok(d.get('posobnost')) or 'Bratislava a okolie',
        'sluzby': {},
        'opravnenia': [],
    }
    # zlý typ poľa (napr. "sluzby": 5) dá varovanie, nie traceback (brána pokus 2, správnosť 4)
    def zoznam(hodnota, meno):
        if hodnota is None or isinstance(hodnota, list):
            return hodnota or []
        varuj(f'firma.json: {meno} nie je zoznam, vynechané')
        return []
    for s in zoznam(d.get('sluzby'), 'sluzby'):
        if isinstance(s, dict) and SLUG_RE.match(str(s.get('slug') or '')):
            f['sluzby'][s['slug']] = {
                'nazov': jeden_riadok(s.get('nazov')),
                'popis': jeden_riadok(s.get('kratky_popis')),
                'cinnosti': [jeden_riadok(c) for c in zoznam(s.get('cinnosti'), f'sluzby.{s["slug"]}.cinnosti')
                             if jeden_riadok(c)],
            }
    for o in zoznam(d.get('opravnenia'), 'opravnenia'):
        if not isinstance(o, dict):
            continue
        f['opravnenia'].append({
            'organizacia': jeden_riadok(o.get('organizacia')),
            'nazov': jeden_riadok(o.get('nazov')),
            'osoba': jeden_riadok(o.get('osoba')),
            'cinnost': jeden_riadok(o.get('cinnost')),
            'zameranie': jeden_riadok(o.get('odborne_zameranie')),
            'cislo': jeden_riadok(o.get('evidencne_cislo')),
            'datum': jeden_riadok(o.get('datum_vydania_text')) or datum_text(jeden_riadok(o.get('datum_vydania'))),
            'rok': (jeden_riadok(o.get('datum_vydania')) or '')[:4],
        })
    return f


def cesta_fotky(obsah, subor):
    """Cesta k fotke len vnútri obsah/obrazky (žiadne ../, žiadne absolútne cesty)."""
    s = jeden_riadok(subor).replace('\\', '/')
    if not s or s.startswith('/') or re.match(r'^[A-Za-z]:', s):
        return None
    if s.startswith('obsah/'):
        s = s[len('obsah/'):]
    kusy = s.split('/')
    if any(k in ('', '.', '..') for k in kusy) or kusy[0] != 'obrazky':
        return None
    p = (obsah / s).resolve()
    koren = (obsah / 'obrazky').resolve()
    if koren not in p.parents:
        return None
    if p.suffix.lower() not in ('.jpg', '.jpeg', '.webp', '.png'):
        return None
    return p


def rozmery_zdroja(p):
    with Image.open(p) as im:
        # plné dekódovanie: fotka s platnou hlavičkou a useknutými dátami (prerušené nahratie v systéme obsahu)
        # tu vyhodí OSError a vynechá sa, inak by zhodila build až pri zápise (brána pokus 1, správnosť 2)
        im.load()
        w, h = im.size
        try:
            orient = im.getexif().get(274, 1)
        except Exception:
            orient = 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    if max(w, h) > MAX_VSTUP:
        k = MAX_VSTUP / max(w, h)
        w, h = max(1, round(w * k)), max(1, round(h * k))
    return w, h


_OSTROST = {}


def ostrost_fotky(p):
    """Ostrosť fotky rovnakým postupom ako audit fotiek (dizajn-3/fotky/audit.py): šedá verzia zmenšená na šírku 960 px,
    rozptyl Laplaciánu so 4-susedným jadrom. Len Pillow (CI nemá numpy): Laplacián cez ImageMath v režime F, priemery
    cez zmenšenie na 1 x 1 filtrom BOX. Cache podľa odtlačku súboru (stovky realizácií, viac behov v testoch)."""
    data = Path(p).read_bytes()
    kluc = hashlib.sha256(data).hexdigest()
    if kluc in _OSTROST:
        return _OSTROST[kluc]
    with Image.open(io.BytesIO(data)) as im:
        g = ImageOps.exif_transpose(im).convert('L')
    w, h = g.size
    if w != 960:
        g = g.resize((960, max(1, round(h * 960 / w))), Image.LANCZOS)
    f = g.convert('F')
    w, h = f.size
    if w < 3 or h < 3:
        _OSTROST[kluc] = 0.0
        return 0.0
    kus = {'c': f.crop((1, 1, w - 1, h - 1)), 'u': f.crop((1, 0, w - 1, h - 2)), 'd': f.crop((1, 2, w - 1, h)),
           'l': f.crop((0, 1, w - 2, h - 1)), 'r': f.crop((2, 1, w, h - 1))}
    lap = ImageMath.lambda_eval(lambda a: a['u'] + a['d'] + a['l'] + a['r'] - a['c'] * 4, **kus)
    stv = ImageMath.lambda_eval(lambda a: a['x'] * a['x'], x=lap)
    m = lap.resize((1, 1), Image.BOX).getpixel((0, 0))
    m2 = stv.resize((1, 1), Image.BOX).getpixel((0, 0))
    v = round(max(0.0, m2 - m * m), 1)
    _OSTROST[kluc] = v
    return v


def sirky_pre(w, strop=None):
    """Šírky súborov fotky širokej w px: štandardné šírky menšie než w a samotná w (najviac strop, predvolene
    SIRKY[-1]); štandardná šírka, od ktorej je w širšia menej ako o SIRKA_BLIZKO, sa vynechá (takmer rovnaký súbor)."""
    strop = SIRKY[-1] if strop is None else strop
    w = min(w, strop)
    out = [s for s in SIRKY if s < w and s * SIRKA_BLIZKO <= w]
    if w not in out:
        out.append(w)
    return sorted(out)


def vyska_pre(w, h, s):
    """Výška súboru so šírkou s pre fotku w x h (rovnaký výpočet v build.py aj v nastroje/predlohy_web.py)."""
    return max(1, round(h * s / w))


def nastav_sirky(f, w, h, formaty):
    """Šírky súborov fotky z predlohy w x h (hotová webová verzia z fotky-web/, inak zdroj z obsah/): sirky = WebP
    (záloha a plná fotka odkazu, pri AVIF najviac WEBP_MAX), sirky_avif = AVIF (prázdne bez AVIF), max_w a max_h
    najväčší súbor vôbec (lightbox, bunky galérie), plna_w najväčší WebP (odkaz bez JS)."""
    f['zaklad'] = (w, h)
    f['sirky_avif'] = sirky_pre(w) if 'avif' in formaty else []
    f['sirky'] = sirky_pre(w, WEBP_MAX) if f['sirky_avif'] else sirky_pre(w)
    f['plna_w'] = f['sirky'][-1]
    f['plna_h'] = vyska_pre(w, h, f['plna_w'])
    f['max_w'] = max(f['sirky'][-1], f['sirky_avif'][-1] if f['sirky_avif'] else 0)
    f['max_h'] = vyska_pre(w, h, f['max_w'])


def avif_dostupny():
    """Pillow s kodekom AVIF (PC aj koleso pillow 11.3.0 pre manylinux, fotky-kvalita/ci-avif.md)."""
    try:
        return bool(features.check('avif'))
    except Exception:
        return False


def kategorie_realizacie(nazov, text, vlastna=None):
    """Zoznam kľúčov kategórií, prvý je hlavný. Hlavná podľa názvu (rodinné, rekonštrukcie, bytové), potom podľa
    textu (rekonštrukcie, rodinné, bytové), inak „ine“; ďalšie sú všetky iné zhody v názve a texte."""
    kluce = [k for k, _ in KATEGORIE]
    hlavna = None
    if isinstance(vlastna, str):
        vlastna = [vlastna]
    if isinstance(vlastna, list):
        mena = {v.lower(): k for k, v in KATEGORIE}
        vybrane = []
        for v in vlastna:
            v = jeden_riadok(v).lower()
            k = v if v in kluce else mena.get(v)
            if k and k not in vybrane:
                vybrane.append(k)
        if vybrane:
            return vybrane
    for k in KAT_PORADIE_NAZOV:
        if KAT_VZORY[k].search(nazov):
            hlavna = k
            break
    if hlavna is None:
        for k in KAT_PORADIE_TEXT:
            if KAT_VZORY[k].search(text):
                hlavna = k
                break
    if hlavna is None:
        return ['ine']
    dalsie = [k for k in ('bytove', 'rodinne', 'rekonstrukcie')
              if k != hlavna and (KAT_VZORY[k].search(nazov) or KAT_VZORY[k].search(text))]
    return [hlavna] + dalsie


def roky_realizacie(roky, datum):
    """(rok, rok_od, rok_je_z_datumu): rok = najvyšší rok z poľa roky; bez neho rok z dátumu príspevku (karta ho
    potom nikdy nepíše ako rok stavby); bez oboch 0 (skupina Bez roku na konci)."""
    ys = [int(x) for x in re.findall(r'\b(?:19|20)\d{2}\b', roky)]
    if ys:
        return max(ys), min(ys), False
    if datum:
        return int(datum[:4]), int(datum[:4]), True
    return 0, 0, False


def zorad_chronologicky(zoznam):
    """rok zostupne, rok začiatku zostupne, poradie vzostupne, datum zostupne, slug (SMER.md 2.7)."""
    zoznam = sorted(zoznam, key=lambda r: r['slug'])
    zoznam.sort(key=lambda r: r['datum'], reverse=True)
    zoznam.sort(key=lambda r: r['poradie'])
    zoznam.sort(key=lambda r: r['rok_od'], reverse=True)
    zoznam.sort(key=lambda r: r['rok'], reverse=True)
    return zoznam


def nacitaj_fotky(obsah, zoznam, meno, nazov, miesto, max_pocet=None):
    fotky = []
    zoznam = zoznam if isinstance(zoznam, list) else []
    if max_pocet is not None and len(zoznam) > max_pocet:
        varuj(f'{meno}: {len(zoznam)} fotiek, použitých prvých {max_pocet}')
        zoznam = zoznam[:max_pocet]
    for f in zoznam:
        if isinstance(f, str):
            f = {'subor': f}
        if not isinstance(f, dict):
            continue
        p = cesta_fotky(obsah, f.get('subor'))
        if p is None or not p.is_file():
            varuj(f'{meno}: fotka {f.get("subor")!r} chýba alebo je mimo obsah/obrazky, vynechaná')
            continue
        try:
            w, h = rozmery_zdroja(p)
            ostrost = ostrost_fotky(p)
        except Exception:
            varuj(f'{meno}: fotka {f.get("subor")!r} sa nedá otvoriť, vynechaná')
            continue
        typ = jeden_riadok(f.get('typ')).upper()
        # vizualizácia (zadanie bmb-dokoncenie, Obsah 3): pole druh a meno autora z príspevku; nikdy obal ani hrdina
        viz = jeden_riadok(f.get('druh')).lower() == 'vizualizacia'
        fotky.append({'zdroj': p, 'w': w, 'h': h, 'alt': jeden_riadok(f.get('alt')), 'ostrost': ostrost,
                      'rel': p.relative_to((obsah / 'obrazky').resolve()).as_posix(),
                      'typ': typ if typ in TYPY_FOTIEK else '', 'viz': viz,
                      'autor': jeden_riadok(f.get('autor')) if viz else '',
                      # sha256 zdroja: kľúč hotovej webovej verzie vo fotky-web/manifest.json (Web.nastav_sirky)
                      'sha': hashlib.sha256(p.read_bytes()).hexdigest()})
    for i, f in enumerate(fotky, 1):
        f['nn'] = f'{i:02d}'
        nastav_sirky(f, f['w'], f['h'], ['avif', 'webp'])
        if not f['alt']:
            f['alt'] = ', '.join(x for x in (nazov, miesto) if x)
        if f['viz'] and not f['alt'].lower().startswith('vizualizácia'):
            f['alt'] = 'Vizualizácia, ' + f['alt'] + (f', autor {f["autor"]}' if f['autor'] else '')
    return fotky


def zverejnene(d):
    z = d.get('zverejnene', True)
    return not (z is False or (isinstance(z, str) and z.strip().lower() in ('false', 'nie', '0', '')) or z == 0 or z is None)


def nacitaj_novinky(obsah):
    """obsah/novinky/<slug>.json od linky bmb-fb: datum, nadpis, text, fotky (najviac 10), fb_url, slug. Zlý JSON,
    slug, dátum alebo cesta k fotke sa preskočí s varovaním; bez priečinka je zoznam prázdny."""
    priecinok = obsah / 'novinky'
    subory = sorted(priecinok.glob('*.json')) if priecinok.is_dir() else []
    vsetky, slugy = [], set()
    for subor in subory:
        d = nacitaj_json(subor)
        if not isinstance(d, dict):
            varuj(f'novinky/{subor.name}: nie je objekt JSON, preskočené')
            continue
        slug = d.get('slug')
        if slug is None or slug == '':
            slug = subor.stem
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            varuj(f'novinky/{subor.name}: slug {slug!r} nie je [a-z0-9-], preskočené')
            continue
        if slug in slugy:
            varuj(f'novinky/{subor.name}: slug {slug} je duplicitný, preskočené')
            continue
        if not zverejnene(d):
            continue
        text = cisty(d.get('text'))
        nadpis = jeden_riadok(d.get('nadpis'))
        if not nadpis and text:
            nadpis = skrat(text.split('\n', 1)[0].strip(), 100)
        if not nadpis:
            varuj(f'novinky/{subor.name}: chýba nadpis aj text, preskočené')
            continue
        datum = jeden_riadok(d.get('datum'))
        if not platny_datum(datum):
            varuj(f'novinky/{subor.name}: datum {datum!r} nie je platný dátum RRRR-MM-DD, preskočené')
            continue
        upravene = jeden_riadok(d.get('upravene'))
        if upravene and not platny_datum(upravene):
            upravene = ''
        fotky = nacitaj_fotky(obsah, d.get('fotky'), f'novinky/{slug}', nadpis, '', MAX_FOTIEK_NOVINKY)
        slugy.add(slug)
        vsetky.append({'slug': slug, 'nadpis': nadpis, 'text': text, 'datum': datum, 'upravene': upravene,
                       'fb_url': bezpecna_url(d.get('fb_url'), ['facebook.com', 'fb.com']), 'fotky': fotky,
                       'obr_dir': f'obrazky-novinky/{slug}',
                       # realizacia (obsah-zmeny.md): slug realizácie tej istej stavby, roky stavby sa berú z nej
                       'realizacia': d.get('realizacia') if isinstance(d.get('realizacia'), str)
                       and SLUG_RE.match(d.get('realizacia') or '') else ''})
    vsetky.sort(key=lambda n: n['slug'])
    vsetky.sort(key=lambda n: n['datum'], reverse=True)
    return vsetky


def nacitaj_realizacie(obsah):
    priecinok = obsah / 'realizacie'
    subory = sorted(priecinok.glob('*.json')) if priecinok.is_dir() else []
    vsetky, slugy = [], set()
    for subor in subory:
        d = nacitaj_json(subor)
        if not isinstance(d, dict):
            varuj(f'{subor.name}: nie je objekt JSON, preskočené')
            continue
        slug = d.get('slug')
        if slug is None or slug == '':
            slug = subor.stem
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            varuj(f'{subor.name}: slug {slug!r} nie je [a-z0-9-], preskočené')
            continue
        if slug in slugy:
            varuj(f'{subor.name}: slug {slug} je duplicitný, preskočené')
            continue
        if not zverejnene(d):
            continue
        nazov = jeden_riadok(d.get('nazov'))
        if not nazov:
            varuj(f'{subor.name}: chýba názov, preskočené')
            continue
        # dátum musí existovať v kalendári, inak by 2026-13-45 išlo do sitemap aj JSON-LD (brána pokus 1, správnosť 5)
        datum = jeden_riadok(d.get('datum'))
        if datum and not platny_datum(datum):
            varuj(f'{subor.name}: datum {datum!r} nie je platný dátum RRRR-MM-DD, vynechaný')
            datum = ''
        # upravene (nepovinné): posledná zmena realizácie, ide do lastmod a dateModified
        upravene = jeden_riadok(d.get('upravene'))
        if upravene and not platny_datum(upravene):
            varuj(f'{subor.name}: upravene {upravene!r} nie je platný dátum RRRR-MM-DD, vynechané')
            upravene = ''
        try:
            poradie = int(d.get('poradie'))
        except (TypeError, ValueError):
            poradie = 10 ** 6
        fotky = nacitaj_fotky(obsah, d.get('fotky'), slug, nazov, jeden_riadok(d.get('miesto')))
        roky = jeden_riadok(d.get('roky'))
        rok, rok_od, rok_z_datumu = roky_realizacie(roky, datum)
        text = cisty(d.get('text'))
        r = {
            'obr_dir': f'obrazky/{slug}',
            'kategorie': kategorie_realizacie(nazov, text, d.get('kategoria')),
            'rok': rok,
            'rok_od': rok_od,
            'rok_z_datumu': rok_z_datumu,
            'slug': slug,
            'nazov': nazov,
            'miesto': jeden_riadok(d.get('miesto')),
            'rola': zaciatok_velky(jeden_riadok(d.get('rola'))),
            'projektant': jeden_riadok(d.get('projektant')),
            # autor (nepovinné): zdroj uvádza „Autor“, nie „Projektant“ (Vila Holubyho, brána bmb-fb-uplnost nález 3)
            'autor': jeden_riadok(d.get('autor')),
            'roky': roky,
            'investor': jeden_riadok(d.get('investor')),
            'zhotovitel': jeden_riadok(d.get('zhotovitel')),
            'text': text,
            'zdroj': jeden_riadok(d.get('zdroj')),
            'fb_url': bezpecna_url(d.get('fb_url'), ['facebook.com', 'fb.com']),
            'datum': datum,
            'upravene': upravene,
            # titulok (nepovinné): vlastný title stránky realizácie, ak sa názov nezmestí do 60 znakov
            'titulok': jeden_riadok(d.get('titulok')),
            'poradie': poradie,
            'fotky': fotky,
            # rok_prispevku (obsah-zmeny.md): rok najnovšieho príspevku, len keď roky stavby chýbajú; karta ho
            # ukáže ako rok bez slova Facebook. obal_vizualizacia: realizácia bez skutočnej fotky (Devín)
            'rok_prispevku': (jeden_riadok(d.get('rok_prispevku'))
                              if re.match(r'^(19|20)\d{2}$', jeden_riadok(d.get('rok_prispevku'))) and not roky else ''),
            'obal_vizualizacia': d.get('obal_vizualizacia') is True,
        }
        if len(r['titulok']) > 60:
            # title nad 60 znakov Google skráti; vlastný titulok sa vtedy nepoužije (brána pokus 2, správnosť 1)
            varuj(f'{subor.name}: titulok má {len(r["titulok"])} znakov, najviac 60, nepoužitý')
            r['titulok'] = ''
        slugy.add(slug)
        vsetky.append(r)
    # chronologicky podľa rokov stavby (pole datum zo starého webu je dátum zverejnenia, rozhoduje až nakoniec)
    return zorad_chronologicky(vsetky)


# ---------------------------------------------------------------- obrázky

def do_srgb(im):
    """Obrázok s vloženým profilom ICC prevedený do sRGB (ImageCms, percepčný zámer); bez profilu sa berie ako sRGB
    (fotky z Facebooku a starého webu). Chyba profilu nezhodí build, fotka ostane bez prevodu."""
    icc = im.info.get('icc_profile')
    if not icc:
        return im
    try:
        from PIL import ImageCms
        zdroj = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        popis = (ImageCms.getProfileDescription(zdroj) or '').lower()
        if 'srgb' in popis.replace(' ', '') and im.mode == 'RGB':
            return im
        rezim = 'RGBA' if im.mode in ('RGBA', 'LA', 'PA') else 'RGB'
        if im.mode not in ('RGB', 'RGBA', 'CMYK', 'L'):
            im = im.convert(rezim)
        return ImageCms.profileToProfile(im, zdroj, ImageCms.createProfile('sRGB'), outputMode=rezim)
    except Exception as e:
        varuj(f'profil ICC sa nedá previesť do sRGB ({e.__class__.__name__}), fotka bez prevodu')
        return im


def otvor_rgb(p, max_vstup=MAX_VSTUP):
    """Fotka ako RGB v sRGB: otočenie podľa EXIF, prevod profilu ICC do sRGB, priehľadnosť na farbu podkladu,
    nad max_vstup zmenšená. Výsledok je bez metadát (prázdne info: žiadne EXIF, XMP ani ICC do výstupu)."""
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        im = do_srgb(im)
        if im.mode in ('RGBA', 'LA', 'P', 'PA'):
            im = im.convert('RGBA')
            podklad = Image.new('RGB', im.size, (0xEC, 0xEB, 0xE6))
            podklad.paste(im, mask=im.split()[-1])
            im = podklad
        else:
            im = im.convert('RGB')
        if max_vstup and max(im.size) > max_vstup:
            k = max_vstup / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        out = im.copy()
    out.info = {}
    return out


def box_px(box, w, h):
    """Rámik (x0, y0, x1, y1 v podieloch fotky 0 až 1) v px fotky w x h."""
    return (round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h))


def zmensi(im, sirka, vyska, pomer=None, box=None):
    """Výrez zo stredu s pomerom (orezy kariet a mobilného rámu) a zmena na sirka x vyska filtrom Lanczos. box: najprv
    len táto časť fotky (rámik v podieloch, karta_foto vyrez), výrez s pomerom potom zo stredu rámika."""
    if box:
        im = im.crop(box_px(box, im.width, im.height))
    if pomer:
        cw, ch = orez_rozmer(im.width, im.height, pomer)
        x, y = (im.width - cw) // 2, (im.height - ch) // 2
        im = im.crop((x, y, x + cw, y + ch))
    if im.size != (sirka, vyska):
        im = im.resize((sirka, vyska), Image.LANCZOS)
    im.info = {}
    return im


def kodovany(im, fmt, kvalita):
    """Bajty WebP alebo AVIF bez metadát (žiadne exif, xmp ani icc_profile; AVIF nesie len nclx sRGB, ktoré libavif
    zapíše sám). AVIF v jednom vlákne: rovnaký vstup dá rovnaké bajty na každom stroji."""
    im = im.copy()
    im.info = {}
    buf = io.BytesIO()
    if fmt == 'avif':
        im.save(buf, 'AVIF', quality=int(kvalita), speed=AVIF_RYCHLOST, max_threads=1, subsampling='4:2:0',
                exif=b'', xmp=b'', icc_profile=b'')
    else:
        im.save(buf, 'WEBP', quality=int(kvalita), method=6, exif=b'', xmp=b'', icc_profile=b'')
    return buf.getvalue()


def webp_bajty(p, sirka, vyska, pomer=None, box=None):
    """Fotka z obsahu zmenšená na sirka x vyska ako WebP s kvalitou KVALITA['webp']; s pomerom najprv výrez zo stredu
    (s rámikom box zo stredu rámika)."""
    return kodovany(zmensi(otvor_rgb(p), sirka, vyska, pomer, box), 'webp', KVALITA['webp'])


def avif_bajty(p, sirka, vyska, pomer=None, box=None):
    """To isté ako webp_bajty vo formáte AVIF s kvalitou KVALITA['avif']."""
    return kodovany(zmensi(otvor_rgb(p), sirka, vyska, pomer, box), 'avif', KVALITA['avif'])


# orezy (PLAN-1-1.md časť 8): karta pásu 4 : 5 a mobilný rám pohľadu A 5 : 4; súbor NN-<šírka>-<kód>.webp
OREZY = {'45': (4, 5), '54': (5, 4), '43': (4, 3), '34': (3, 4)}
SIRKY_OREZU = (480, 720, 960)
# karta s malým zdrojom (brána bmb-presnost pokus 1, nález 1): výrez 4 : 5 karty mriežky, služby a novinky musí mať
# aspoň KARTA_MIN_OREZ px zdroja, aby pri najširšej karte (422 CSS px pri 1920 px, hustota 2: 844 px obrazovky) na 1 px
# zdroja nepripadalo viac než 1,25 px obrazovky (844 / 1,25 = 675,2). Menší zdroj nejde cez celý rám: celá fotka
# (výrez 4 : 3, fotka na výšku 3 : 4) stojí v strede rámu 4 : 5 na podklade, široká najviac KARTA_CELA_MAX CSS px
# a najviac KARTA_CELA_ZVACSENIE x šírku výrezu zdroja / 2 (trieda cap-N), takže zväčšenie ostáva do 1,2
KARTA_MIN_OREZ = 676
KARTA_CELA_MAX = 360
KARTA_CELA_ZVACSENIE = 1.2
KARTA_CELA_MIN = 160


def orez_rozmer(w, h, pomer):
    """Najväčší výrez s pomerom pomer (šírka, výška) zo stredu fotky w x h."""
    a, b = pomer
    if w * b > h * a:
        return max(1, round(h * a / b)), h
    return w, max(1, round(w * b / a))


def sirky_orezu(cw):
    out = [s for s in SIRKY_OREZU if s < cw]
    if cw <= SIRKY_OREZU[-1] and cw not in out:
        out.append(cw)
    return sorted(out) or [SIRKY_OREZU[-1]]


# osvedčenia SKSI (stránka Oprávnenia): text dokladu ostáva čitateľný (vyššia kvalita); súbor
# opravnenia/<meno>-<šírka>.webp. Sken má len 528 a 522 px, stránka a lightbox ho pri hustote 2 kreslia na 1056 px
# (presnost/meranie/fotky-pred.md časť 11), preto aj zväčšené súbory do ZVACSENIE_DOKLADU x zdroj, len bez AI:
# Lanczos a jemné doostrenie (UnsharpMask polomer 1,0, 40 %, prah 2). Kalibrácia v ops/weby-na-mieru/klienti/
# bmbuilding/presnost/oprava-fotky.md: zmenšené späť na rozmer skenu sa od skenu líšia rovnako ako čistý Lanczos
# (najväčší rozdiel jasu 17,6 a 26,6 z 255, priemer 0,45 a 0,66), zakryté miesta ostávajú jednofarebné. Ostrejší
# doklad dá len nový sken. Zdroj v sablona/opravnenia/ sa nemení.
SIRKY_DOKLADU = (360, 720)
ZVACSENIE_DOKLADU = 2
DOOSTRENIE_DOKLADU = (1.0, 40, 2)
# vnútro rámu dokladu v CSS px (styl.css .doklady --doklad-max 554 px = 528 + 2 x 12 px okraj + 2 x 1 px čiara):
# užší sken (522 px) sa v ráme zobrazí na 528 px (zmerané: box 528 x 757,5 CSS px pri 1440 a 1920), preto najväčší
# súbor má 2 x max(šírka skenu, 528), inak by prehliadač pri hustote 2 súbor 1044 px zväčšoval (z_súbor 1,011)
SIRKA_RAMU_DOKLADU = 528
# pomer strán vnútra rámu pri najväčšej šírke: 528 x (554 x 297 / 210 - 2 x 12 - 2 x 1) = 528 x 757,51 CSS px
POMER_RAMU_DOKLADU = SIRKA_RAMU_DOKLADU / (554 * 297 / 210 - 26)
# orez zakrytého skenu na hranu papiera (ľavý, horný, pravý, dolný okraj výrezu v px skenu; brána bmb-presnost pokus 2,
# nález 1): object-fit contain dal skenom s iným pomerom strán (528 x 755 a 522 x 729) iný biely pás v ráme, papier
# vedľa seba bol o 7 px inde a o 16 px inak vysoký (1440 px). Výrez odstráni biely okraj skenera (ľavý sken vľavo 3,
# hore 4, vpravo 4 a dole 4 px, k tomu 1 px papiera vpravo; pravý sken vľavo 2, hore 3 a dole 4 px, vpravo svetlý tieň
# skenera 4 px) a pri pravom skene ešte 6 px (vľavo) a 7 px (vpravo) prázdneho papiera, aby mali oba výrezy pomer strán
# do 0,002 pod vnútrom rámu (520 / 747 = 0,6961, 503 / 722 = 0,6967, rám 0,6970); styl.css ich kreslí cez object-fit cover, papier vypĺňa celé vnútro rámu v oboch. Odstránená plocha nemá
# ornament ani písmo (presnost/nastroje/oprava2_orez_kontrola.py, test_build.py TestOpravnenia), zdroj sa nemení.
OREZ_DOKLADU = {'osvedcenie-sksi-stavbyveduci-2003': (3, 4, 523, 751),
                'osvedcenie-sksi-stavebny-dozor-2010': (8, 3, 511, 725)}
VERZIA_DOKLADU = 'doklad-3|webp-q90-m6|lanczos|zvacsenie-2x-lanczos-usm-1.0-40-2|orez-papier'


def sken_dokladu(p):
    """Zakrytý sken osvedčenia ako RGB, orezaný na hranu papiera podľa OREZ_DOKLADU (bez orezu, keď meno nie je
    v zozname). Výrez musí ležať v skene a mať pomer strán najviac o 0,002 pod vnútrom rámu (nie nad ním, aby cover
    pri 528 px rámu kreslil súbor 1056 px na celú šírku)."""
    im = otvor_rgb(p)
    box = OREZ_DOKLADU.get(Path(p).stem)
    if not box:
        return im
    l, t, r, b = box
    if not (0 <= l < r <= im.width and 0 <= t < b <= im.height):
        raise ChybaZostavy(f'{Path(p).name}: orez {box} mimo skenu {im.width} x {im.height}')
    pomer = (r - l) / (b - t)
    if not (POMER_RAMU_DOKLADU - 0.002 <= pomer <= POMER_RAMU_DOKLADU):
        raise ChybaZostavy(f'{Path(p).name}: orez {box} má pomer strán {pomer:.4f}, rám {POMER_RAMU_DOKLADU:.4f}')
    return im.crop(box)


def sirky_dokladu(w):
    """Šírky súborov dokladu so skenom širokým w px: SIRKY_DOKLADU pod najväčšou šírkou, sken a najväčšia šírka
    ZVACSENIE_DOKLADU x max(w, SIRKA_RAMU_DOKLADU)."""
    hore = ZVACSENIE_DOKLADU * max(w, SIRKA_RAMU_DOKLADU)
    return sorted({s for s in SIRKY_DOKLADU if s != w and s < hore} | {w, hore})


def doklad_webp_bajty(p, sirka, vyska):
    """Zakrytý obrázok osvedčenia ako WebP: rovnaká veľkosť (len prevod formátu), zmenšenie Lanczos, alebo zväčšenie
    Lanczosom s jemným doostrením najviac na ZVACSENIE_DOKLADU x max(sken, SIRKA_RAMU_DOKLADU) (bez AI, nové
    detaily nevznikajú). Sken je najprv orezaný na hranu papiera (sken_dokladu)."""
    im = sken_dokladu(p)
    strop = ZVACSENIE_DOKLADU * max(im.width, SIRKA_RAMU_DOKLADU)
    if sirka > strop or vyska > round(im.height * strop / im.width) + 1:
        raise ChybaZostavy(f'{p.name}: doklad sa nezväčšuje nad {strop} px '
                           f'({im.width} x {im.height} -> {sirka} x {vyska})')
    if sirka > im.width:
        # vždy cez 2-násobok s doostrením (kalibrované), medzišírka (720) jeho zmenšením Lanczos
        polomer, percento, prah = DOOSTRENIE_DOKLADU
        im = im.resize((im.width * ZVACSENIE_DOKLADU, im.height * ZVACSENIE_DOKLADU), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=polomer, percent=percento, threshold=prah))
    if im.size != (sirka, vyska):
        im = im.resize((sirka, vyska), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=90, method=6)
    return buf.getvalue()


def odtlacok_suboru(p, *dalsie):
    h = hashlib.sha256()
    h.update(Path(p).read_bytes())
    for x in dalsie:
        h.update(b'|' + str(x).encode('utf-8'))
    return h.hexdigest()


def kluc_variantu(orez, sirka, fmt):
    """Kľúč súboru v manifeste fotky-web: „<orez>|<šírka>|<formát>“, orez prázdny (celá fotka), 45 alebo 54."""
    return f'{orez or ""}|{int(sirka)}|{fmt}'


class FotkyWeb:
    """Hotové webové verzie fotiek (priečinok fotky-web/, nástroj nastroje/predlohy_web.py). manifest.json:
    {"verzia": 1, "fotky": {<sha256 zdroja z obsah/>: {"rel", "predloha": {"w", "h", ...}, "kvalita": {...},
    "subory": {"<orez>|<šírka>|<formát>": {"subor", "w", "h", "bajty", "sha256"}}}}}. Build použije záznam len vtedy,
    keď sedí sha256 zdroja; súbor len keď existuje a má zapísanú veľkosť (sha256 sa overí pri kopírovaní).
    Pokazený manifest alebo cesta mimo priečinka = záznam sa nepoužije (fotka sa zakóduje z obsahu)."""

    def __init__(self, koren):
        self.koren = Path(koren) if koren else None
        self.fotky = {}
        m = self.koren / 'manifest.json' if self.koren else None
        if m is None or not m.is_file():
            return
        try:
            d = json.loads(m.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            varuj(f'{m} sa nedá prečítať, fotky sa zakódujú z obsahu')
            return
        fotky = d.get('fotky') if isinstance(d, dict) else None
        if isinstance(fotky, dict):
            self.fotky = {k: v for k, v in fotky.items()
                          if isinstance(k, str) and re.match(r'^[0-9a-f]{64}$', k) and isinstance(v, dict)}

    def zaznam(self, sha):
        z = self.fotky.get(sha)
        if not z:
            return None
        p = z.get('predloha') if isinstance(z.get('predloha'), dict) else {}
        try:
            w, h = int(p.get('w')), int(p.get('h'))
        except (TypeError, ValueError):
            return None
        if not (0 < w <= 10000 and 0 < h <= 10000):
            return None
        return z

    def subor(self, sha, orez, sirka, fmt):
        """Záznam súboru {"subor", "w", "h", "bajty", "sha256", "cesta"} alebo None (chýba, mimo priečinka, iná veľkosť)."""
        z = self.zaznam(sha)
        if not z or not isinstance(z.get('subory'), dict):
            return None
        s = z['subory'].get(kluc_variantu(orez, sirka, fmt))
        if not isinstance(s, dict) or not isinstance(s.get('subor'), str):
            return None
        rel = s['subor'].replace('\\', '/')
        if not bezpecna_rel(rel) or not rel.endswith('.' + fmt) or not re.match(r'^[0-9a-f]{64}$', str(s.get('sha256', ''))):
            return None
        p = self.koren / rel
        try:
            if not p.is_file() or p.stat().st_size != int(s.get('bajty', -1)):
                return None
        except (OSError, TypeError, ValueError):
            return None
        return dict(s, cesta=p)

    @staticmethod
    def citaj(s):
        data = Path(s['cesta']).read_bytes()
        if hashlib.sha256(data).hexdigest() != s['sha256']:
            raise ChybaZostavy(f'fotky-web/{s["subor"]}: sha256 nesedí s manifest.json')
        return data


def pismo(vaha, velkost):
    """Arimo (statická inštancia podmnožiny, sablona/arimo-<váha>.ttf) pre OG obrázky; bez súboru None."""
    cesta = SABLONA / f'arimo-{vaha}.ttf'
    if cesta.is_file():
        return ImageFont.truetype(str(cesta), velkost)
    return None


def zalom(draw, text, font, sirka, max_riadkov):
    slova = text.split()
    riadky, akt = [], ''
    for slovo in slova:
        skus = (akt + ' ' + slovo).strip()
        if draw.textlength(skus, font=font) <= sirka or not akt:
            akt = skus
        else:
            riadky.append(akt)
            akt = slovo
    if akt:
        riadky.append(akt)
    if len(riadky) > max_riadkov:
        riadky = riadky[:max_riadkov]
        posledny = riadky[-1]
        while posledny and draw.textlength(posledny + ' …', font=font) > sirka:
            posledny = posledny.rsplit(' ', 1)[0] if ' ' in posledny else posledny[:-1]
        riadky[-1] = posledny + ' …'
    return riadky


# ---------------------------------------------------------------- logo SVG (sablona/logo.svg) na raster

SVG_TOKEN = re.compile(r'[MLHVQCZmlhvqcz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
SVG_ATRIBUT = re.compile(r'([a-zA-Z-]+)="([^"]*)"')


def svg_podcesty(d):
    """Podcesty cesty SVG ako zoznamy bodov. Logo z logo_svg.py má len absolútne M, L, H, V, Q, C a Z; krivky sa delia
    na 12 úsečiek (pri 4-násobnom nadvzorkovaní hladké aj pri 512 px)."""
    tok = SVG_TOKEN.findall(d)
    i, prikaz = 0, None
    cesty, akt = [], []
    x = y = sx = sy = 0.0

    def cislo():
        nonlocal i
        v = float(tok[i])
        i += 1
        return v
    while i < len(tok):
        if tok[i].isalpha():
            prikaz = tok[i]
            i += 1
            if prikaz in 'Zz':
                if akt:
                    cesty.append(akt)
                akt = []
                x, y = sx, sy
            continue
        if prikaz == 'M':
            x, y = cislo(), cislo()
            if akt:
                cesty.append(akt)
            akt = [(x, y)]
            sx, sy = x, y
            prikaz = 'L'
        elif prikaz == 'L':
            x, y = cislo(), cislo()
            akt.append((x, y))
        elif prikaz == 'H':
            x = cislo()
            akt.append((x, y))
        elif prikaz == 'V':
            y = cislo()
            akt.append((x, y))
        elif prikaz == 'Q':
            x1, y1, x2, y2 = cislo(), cislo(), cislo(), cislo()
            for k in range(1, 13):
                t = k / 12
                akt.append(((1 - t) ** 2 * x + 2 * (1 - t) * t * x1 + t * t * x2,
                            (1 - t) ** 2 * y + 2 * (1 - t) * t * y1 + t * t * y2))
            x, y = x2, y2
        elif prikaz == 'C':
            x1, y1, x2, y2, x3, y3 = (cislo() for _ in range(6))
            for k in range(1, 13):
                t = k / 12
                u = 1 - t
                akt.append((u ** 3 * x + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                            u ** 3 * y + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
            x, y = x3, y3
        else:
            raise ValueError(f'príkaz cesty SVG {prikaz!r} nie je podporený')
    if akt:
        cesty.append(akt)
    return cesty


def svg_obraz(svg, velkost):
    """Raster loga (štvorcový viewBox, prvky rect a path s výplňou) vo veľkosti velkost x velkost, RGB. Podcesty sa
    skladajú pravidlom párnosti (XOR), čo pri písmenách loga dáva to isté ako nonzero (obrysy sa neprekrývajú)."""
    vb = re.search(r'viewBox="([\d.\s-]+)"', svg)
    x0, y0, vw, vh = (float(v) for v in vb.group(1).split()) if vb else (0.0, 0.0, 936.0, 936.0)
    n = 4
    s = velkost * n / vw
    platno = Image.new('RGB', (velkost * n, round(vh * s)), FARBA_BIELA)
    for prvok, atributy in re.findall(r'<(rect|path)\s([^>]*?)/?>', svg):
        a = dict(SVG_ATRIBUT.findall(atributy))
        farba = ImageColor.getrgb(a.get('fill', '#000'))
        maska = Image.new('1', platno.size, 0)
        if prvok == 'rect':
            rx, ry = float(a.get('x', 0)) - x0, float(a.get('y', 0)) - y0
            ImageDraw.Draw(maska).rectangle((rx * s, ry * s, (rx + float(a['width'])) * s - 1,
                                             (ry + float(a['height'])) * s - 1), fill=1)
        else:
            for body in svg_podcesty(a.get('d', '')):
                if len(body) < 3:
                    continue
                m = Image.new('1', platno.size, 0)
                ImageDraw.Draw(m).polygon([((bx - x0) * s, (by - y0) * s) for bx, by in body], fill=1)
                maska = ImageChops.logical_xor(maska, m)
        platno.paste(farba, (0, 0), maska.convert('L'))
    return platno.resize((velkost, round(vh * velkost / vw)), Image.LANCZOS)


def og_bajty(foto, nadpis, riadok2, riadok3, logo_svg):
    """OG obrázok 1200 x 630 v značke: modrá #17476f, logo s bielym rámom (na modrej by splynulo, LOGO.md bod 5),
    nadpis tučne veľkými písmenami ako na banneri, pod ním biela linka z loga a menší riadok veľkými písmenami;
    s fotkou stavby vpravo (orez na výšku, nikdy nie zväčšená nad 1,25 násobok)."""
    platno = Image.new('RGB', (OG_W, OG_H), FARBA_BM)
    draw = ImageDraw.Draw(platno)
    x0, hore = 64, 56
    sirka = OG_W - 2 * x0
    if foto is not None:
        im = otvor_rgb(foto)
        k = max(480 / im.width, OG_H / im.height)
        if k <= 1.25:
            im = ImageOps.fit(im, (480, OG_H), Image.LANCZOS, centering=(0.5, 0.5))
            platno.paste(im, (OG_W - 480, 0))
            sirka = OG_W - 480 - x0 - 56
    logo_v = 132
    draw.rectangle((x0 - 4, hore - 4, x0 + logo_v + 3, hore + logo_v + 3), fill=FARBA_BIELA)
    platno.paste(svg_obraz(logo_svg, logo_v), (x0, hore))
    f_nadpis = pismo(700, 60)
    if f_nadpis is None:
        buf = io.BytesIO()
        platno.save(buf, 'JPEG', quality=82, optimize=True)
        return buf.getvalue()
    text = nadpis.upper()
    for velkost, riadkov in ((60, 3), (50, 4), (42, 5)):
        f_nadpis = pismo(700, velkost)
        riadky = zalom(draw, text, f_nadpis, sirka, 99)
        if len(riadky) <= riadkov:
            break
    riadky = zalom(draw, text, f_nadpis, sirka, riadkov)
    y = hore + logo_v + 52
    max_sirka = 0
    for r in riadky:
        draw.text((x0, y), r, font=f_nadpis, fill=FARBA_BIELA)
        max_sirka = max(max_sirka, draw.textlength(r, font=f_nadpis))
        y += round(f_nadpis.size * 1.06)
    # biela linka z loga: hrúbka 1,65 % strany loga, šírka po koniec najdlhšieho riadku (ako linka pod BM)
    y += 14
    draw.rectangle((x0, y, x0 + round(max_sirka), y + 6), fill=FARBA_BIELA)
    y += 6 + 18
    if riadok2:
        f2 = pismo(700, 22)
        for r in zalom(draw, riadok2.upper(), f2, sirka, 2):
            draw.text((x0, y), r, font=f2, fill=FARBA_BM_200)
            y += 30
    if riadok3:
        draw.text((x0, OG_H - hore - 26), riadok3, font=pismo(400, 26), fill=FARBA_BIELA)
    buf = io.BytesIO()
    platno.save(buf, 'JPEG', quality=82, optimize=True)
    return buf.getvalue()


def png_bajty(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- HTML

# sizes podľa rozloženia v styl.css (mriežka 4, 8 a 12 stĺpcov ako v ukážke, obsah najviac 1760 px):
# karty zoznamu 4 v rade od 1024 px, 3 od 640 px, pod tým riadok s náhľadom 38 % šírky; pás 30 vw; fotka úvodu
# 7 z 12 stĺpcov; galéria 3 v rade (prvá cez 2), na mobile 2 v rade
# svetlá karta 4 : 5 v mriežke (zoznam realizácií, služby, novinky; Stavba 2): 1 stĺpec do 479 px, 2 od 480, 3 od 640,
# 4 od 1024 px (obsah najviac 1760 px: (1760 - 3 x 24) / 4 = 422)
SIZES_KARTA = ('(min-width: 1024px) min(23vw, 425px), (min-width: 640px) 31vw, (min-width: 480px) 47vw, '
               'calc(100vw - 32px)')


def sizes_karty_cela(cap):
    """sizes celej fotky karty s malým zdrojom (KARTA_MIN_OREZ): šírka rámu karty mínus 2 x 24 px, najviac cap CSS px."""
    return (f'(min-width: 1024px) min(23vw - 48px, {cap}px), (min-width: 640px) min(31vw - 48px, {cap}px), '
            f'(min-width: 480px) min(47vw - 48px, {cap}px), min(100vw - 80px, {cap}px)')
# fotka v dvojstrane detailu realizácie a novinky: 7 z 12 stĺpcov od 1024 px, inak celá šírka
SIZES_DETAIL = '(min-width: 1024px) min(56vw, 1000px), calc(100vw - 32px)'
# pás, pohľad A, Postup a O firme podľa ukážky Olovnica (PLAN-1-1.md časť 8); pripnutý pás prepne app.js na min(45vh, 480px)
SIZES_PAS = '(min-width: 1024px) min(30vw, 440px), (min-width: 768px) 42vw, min(80vw, 420px)'
SIZES_POHLAD = '(min-width: 1024px) max(45vw, min(49vw, 133vh - 437px)), (min-width: 768px) 58vw, calc(100vw - 32px)'
SIZES_POSTUP = '(min-width: 1024px) min(45.5vw, 880px), (min-width: 768px) 52vw, calc(100vw - 32px)'
SIZES_FIRMA = '(min-width: 1024px) min(46vw, 880px), (min-width: 768px) 46vw, calc(100vw - 32px)'
# druh stavby na karte pásu v jednotnom čísle: prvá zhoda v názve, potom v texte realizácie
DRUHY_KARTY = (('Stavebné úpravy', r'stavebné úpravy'), ('Rekonštrukcia', r'rekonštrukc|nadstavb'),
               ('Polyfunkčný objekt', r'polyfunkčn'), ('Rezidencia', r'rezidenci'), ('Bytové domy', r'bytové domy'),
               ('Bytový dom', r'bytov'), ('Dvojdom', r'dvojdom'), ('Vila', r'\bvil[ay]\b'),
               ('Rodinný dom', r'(^|\s)RD\b|rodinn'))
POCET_STAVIEB = {1: 'Jedna stavba', 2: 'Dve stavby', 3: 'Tri stavby', 4: 'Štyri stavby', 5: 'Päť stavieb',
                 6: 'Šesť stavieb', 7: 'Sedem stavieb', 8: 'Osem stavieb', 9: 'Deväť stavieb', 10: 'Desať stavieb'}
SIZES_HRDINA = '(min-width:1024px) min(56vw, 1000px), (min-width:768px) 92vw, calc(100vw - 32px)'
SIZES_GALERIA_PRVA = '(min-width:1024px) min(62vw, 1150px), calc(100vw - 32px)'
SIZES_GALERIA = '(min-width:640px) min(30vw, 570px), 46vw'
# rám pohľadu A detailu a bunka galérie majú pomer 3 : 2 (styl.css aspect-ratio:3/2; zmerané v
# ops/weby-na-mieru/klienti/bmbuilding/presnost/meranie/fotky-pred.md: 358 x 239, 765 x 510, 1017 x 678 a 427 x 285
# CSS px); object-fit: cover fotky širšej než rám ukáže len časť jej šírky (sizes_pre_cover)
POMER_RAMU_COVER = 1.5


def _casti_sizes(sizes):
    """Položky atribútu sizes rozdelené čiarkami mimo zátvoriek (min(30vw, 570px) ostane celé)."""
    out, hlbka, kus = [], 0, ''
    for z in sizes:
        if z == ',' and hlbka == 0:
            out.append(kus.strip())
            kus = ''
            continue
        hlbka += (z == '(') - (z == ')')
        kus += z
    out.append(kus.strip())
    return [x for x in out if x]


def sizes_pre_cover(sizes, w, h, ram=POMER_RAMU_COVER):
    """sizes pre fotku w x h v ráme s pomerom ram (šírka / výška) pri object-fit: cover. Fotka širšia než rám je
    orezaná po stranách, takže prehliadač potrebuje súbor širší o k = (w / h) / ram; bez toho vyberie súbor podľa šírky
    boxu, ktorý je na výšku malý, a zväčší ho (fotky-pred.md časť 9: Devín 01 z_súbor 1,55 pri 390 px). Každá dĺžka sa
    vynásobí k (zaokrúhlené nahor na stotiny) cez calc(); užšia alebo rovnako široká fotka má sizes bez zmeny."""
    if not w or not h:
        return sizes
    k = (w / h) / ram
    if k <= 1.005:
        return sizes
    k = -(-round(k * 1000) // 10) / 100
    out = []
    for kus in _casti_sizes(sizes):
        media, dlzka = '', kus
        if kus.startswith('('):
            hlbka = 0
            for i, z in enumerate(kus):
                hlbka += (z == '(') - (z == ')')
                if hlbka == 0:
                    media, dlzka = kus[:i + 1] + ' ', kus[i + 1:].strip()
                    break
        vnutro = dlzka[5:-1] if dlzka.startswith('calc(') and dlzka.endswith(')') else dlzka
        out.append(f'{media}calc(({vnutro}) * {k:g})' if ' ' in vnutro.strip() and not vnutro.startswith(('min(', 'max('))
                   else f'{media}calc({vnutro} * {k:g})')
    return ', '.join(out)
# najväčšie bunky galérie v CSS px (styl.css: --sirka 1760 px, medzera 24 px, bunka 3 : 2): mriežka 3 stĺpce,
# galeria-malo 2 stĺpce, prvá veľká 2 stĺpce a 2 riadky. Fotka, ktorú by object-fit:cover v takej bunke zväčšil nad
# 1,25 násobok súboru, je v galérii „malá“: bez srcset a s object-fit:scale-down, nikdy väčšia než súbor
# (oprava po bráne, bod 1: vizualizácie UFO 320 x 179 px; rovnaké pravidlo ako lightbox v app.js a OG)
GALERIA_BUNKY = {'mriezka': (571, 381), 'malo': (868, 579), 'prva': (1165, 784)}
ZVACSENIE_MAX = 1.25
SIZES_PRED_PO = '(min-width:768px) 46vw, calc(100vw - 32px)'


def cislo_svg(v):
    return str(int(v)) if float(v).is_integer() else f'{v:g}'


def svg_obrys(obdlzniky):
    """Obrys zjednotenia osovo zarovnaných obdĺžnikov (x0, y0, x1, y1) ako cesta SVG z úsekov H a V, bez vnútorných
    čiar (rezané konštrukcie schémy: steny, dosky, základ). Slučky v smere hodín (plná plocha vpravo), deterministicky."""
    xs = sorted({x for o in obdlzniky for x in (o[0], o[2])})
    ys = sorted({y for o in obdlzniky for y in (o[1], o[3])})
    plne = set()
    for i in range(len(xs) - 1):
        cx = (xs[i] + xs[i + 1]) / 2
        for j in range(len(ys) - 1):
            cy = (ys[j] + ys[j + 1]) / 2
            if any(o[0] <= cx <= o[2] and o[1] <= cy <= o[3] for o in obdlzniky):
                plne.add((i, j))
    hrany = {}
    for i, j in sorted(plne):
        x0, x1, y0, y1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
        if (i, j - 1) not in plne:
            hrany.setdefault((x0, y0), []).append((x1, y0))
        if (i + 1, j) not in plne:
            hrany.setdefault((x1, y0), []).append((x1, y1))
        if (i, j + 1) not in plne:
            hrany.setdefault((x1, y1), []).append((x0, y1))
        if (i - 1, j) not in plne:
            hrany.setdefault((x0, y1), []).append((x0, y0))
    d = []
    while hrany:
        start = min(hrany)
        body, bod = [start], start
        while True:
            konce = hrany[bod]
            dalsi = konce.pop(0)
            if not konce:
                del hrany[bod]
            bod = dalsi
            if bod == start:
                break
            body.append(bod)
        n = len(body)
        rohy = [body[k] for k in range(n)
                if not (body[k - 1][0] == body[k][0] == body[(k + 1) % n][0] or body[k - 1][1] == body[k][1] == body[(k + 1) % n][1])]
        if len(rohy) < 3:
            continue
        kus = f'M{cislo_svg(rohy[0][0])} {cislo_svg(rohy[0][1])}'
        for a, b in zip(rohy, rohy[1:]):
            kus += f'H{cislo_svg(b[0])}' if a[1] == b[1] else f'V{cislo_svg(b[1])}'
        d.append(kus + 'Z')
    return ''.join(d)


def nacitaj_pravidla():
    """sablona/fotky-pravidla.json; chýbajúci alebo pokazený súbor dá predvolené prahy a žiadny pár pred a po."""
    p = SABLONA / 'fotky-pravidla.json'
    d = nacitaj_json(p) if p.is_file() else {}
    d = d if isinstance(d, dict) else {}

    def prahy(kluc, sirka, ostrost):
        x = d.get(kluc) if isinstance(d.get(kluc), dict) else {}
        try:
            return {'min_sirka': int(x.get('min_sirka', sirka)), 'min_ostrost': float(x.get('min_ostrost', ostrost))}
        except (TypeError, ValueError):
            return {'min_sirka': sirka, 'min_ostrost': ostrost}
    try:
        prah = float(d.get('prah_rozmazana', 80))
    except (TypeError, ValueError):
        prah = 80.0
    # vizualizácie (malé rendery z príspevkov) majú vlastný prah, predvolene 0: vždy v galérii (oprava po bráne, bod 1)
    try:
        prah_viz = float(d.get('prah_rozmazana_vizualizacia', 0))
    except (TypeError, ValueError):
        prah_viz = 0.0
    # ilustračné fotky kariet noviniek bez vlastnej fotky (oprava po bráne, bod 2): slug novinky -> slug a súbor
    ilustracne = {k: v for k, v in (d.get('ilustracne_fotky') or {}).items()
                  if isinstance(k, str) and isinstance(v, dict)} if isinstance(d.get('ilustracne_fotky'), dict) else {}
    predvolena = d.get('ilustracna_predvolena') if isinstance(d.get('ilustracna_predvolena'), dict) else None
    try:
        interier = re.compile(str(d.get('interier_nazov') or 'ambulanc|klub|interi[eé]r'), re.I)
    except re.error:
        interier = re.compile('ambulanc|klub|interi[eé]r', re.I)
    poradie = [t for t in (d.get('poradie_typov') or TYPY_FOTIEK) if t in TYPY_FOTIEK]
    typy = {k: v for k, v in (d.get('typy') or {}).items() if isinstance(k, str) and v in TYPY_FOTIEK}
    pary = [x for x in (d.get('pary_pred_po') or []) if isinstance(x, dict)]
    return {'prah': prah, 'prah_viz': prah_viz, 'obal': prahy('obal', 640, 350), 'odporucany': prahy('odporucany_obal', 960, 500),
            'velka': prahy('velka', 1280, 800), 'interier': interier, 'poradie': poradie or list(TYPY_FOTIEK),
            'typy': typy, 'pary': pary, 'ilustracne': ilustracne, 'ilustracna_predvolena': predvolena}


class _TextStranky(HTMLParser):
    """Viditeľný text stránky (mimo script a style) a texty alt, aria-label a title na kontrolu znakov písma."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texty, self._skryte = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skryte += 1
        for k, v in attrs:
            if v and k in ('alt', 'aria-label', 'title', 'data-nazov', 'data-popis', 'data-alt', 'data-autor'):
                self.texty.append(v)

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skryte:
            self._skryte -= 1

    def handle_data(self, data):
        if not self._skryte:
            self.texty.append(data)


class Web:
    def __init__(self, obsah, nahlad, fotky_web=None, predlohy=None):
        """fotky_web: priečinok hotových webových verzií (predvolený FOTKY_WEB). predlohy: {sha256 zdroja: (w, h)}
        namiesto manifestu, len pre nastroje/predlohy_web.py (plán súborov z predlôh, ktoré ešte nie sú zakódované)."""
        self.obsah = obsah
        self.nahlad = nahlad
        self.nastavenia = json.loads((SABLONA / 'nastavenia.json').read_text(encoding='utf-8'))
        kv = self.nastavenia.get('kvalita_fotiek')
        kv = kv if isinstance(kv, dict) else {}
        for fmt in FORMATY:
            KVALITA[fmt] = KVALITA_PREDVOLENA[fmt]
            try:
                q = int(kv.get(fmt, KVALITA_PREDVOLENA[fmt]))
            except (TypeError, ValueError):
                q = 0
            if 1 <= q <= 100:
                KVALITA[fmt] = q
            else:
                varuj(f'nastavenia kvalita_fotiek.{fmt} nie je číslo 1 až 100, ostáva {KVALITA[fmt]}')
        self.avif = avif_dostupny()
        self.fotky_web = FotkyWeb(FOTKY_WEB if fotky_web is None else fotky_web)
        self.predlohy = predlohy
        # súbory fotiek, ktoré stránky naozaj použijú: sha256 zdroja -> {kľúč variantu: (šírka, výška, orez, formát)}
        # (plán pre nastroje/predlohy_web.py) a počty: z fotky-web, zakódované z obsahu
        self.potreba = {}
        self.zdroje = {}
        # rámik výrezu 4 : 5 karty (karta_foto vyrez): sha256 zdroja -> (x0, y0, x1, y1) v podieloch fotky
        self.vyrezy = {}
        self.pocty_fotiek = {'fotky-web': 0, 'obsah': 0}
        self.texty = json.loads((SABLONA / 'texty.json').read_text(encoding='utf-8'))
        css = (SABLONA / 'styl.css').read_text(encoding='utf-8').replace('\r\n', '\n')

        def zhusti(c):
            # vložený štýl bez komentárov a odsadenia (asi 7 kB menej HTML na každej stránke, výkon na mobile)
            return re.sub(r'^[ \t]+', '', re.sub(r'\n\s*\n', '\n', re.sub(r'/\*.*?\*/', '', c, flags=re.S)), flags=re.M).strip() + '\n'
        self.css_sablona = zhusti(css)
        # úvod bez úseku PODSTRÁNKY (značky @podstranky-zaciatok a @podstranky-koniec v styl.css): menej HTML na mobile
        self.css_uvod = zhusti(re.sub(r'/\* @podstranky-zaciatok \*/.*?/\* @podstranky-koniec \*/', '', css, flags=re.S))
        self.js = (SABLONA / 'app.js').read_bytes() if (SABLONA / 'app.js').is_file() else b''
        self.js_verzia = hashlib.sha256(self.js).hexdigest()[:10]
        self.logo_svg = (SABLONA / 'logo.svg').read_text(encoding='utf-8')
        self.favicon_svg = (SABLONA / 'logo-favicon.svg').read_text(encoding='utf-8')
        znaky = SABLONA / 'pismo-znaky.txt'
        self.znaky_pisma = set(znaky.read_text(encoding='utf-8').rstrip('\n')) if znaky.is_file() else None
        self.pravidla = nacitaj_pravidla()
        self.domena = self.nastavenia.get('domena', 'https://bmbuilding.sk').rstrip('/')
        # Instagram len s istou adresou (dizajn-olovnica/INSTAGRAM.md); adresa je v šablóne, nie v obsahu
        self.instagram = bezpecna_url(self.nastavenia.get('instagram'), ['instagram.com'])
        self.firma = nacitaj_firmu(obsah)
        self.realizacie = nacitaj_realizacie(obsah)
        self.novinky = nacitaj_novinky(obsah)
        for r in self.realizacie + self.novinky:
            for f in r['fotky']:
                self.nastav_sirky(f)
        if not self.avif and any(not f.get('fw') for r in self.realizacie + self.novinky for f in r['fotky']):
            varuj('Pillow nemá kodek AVIF: fotky bez hotovej webovej verzie (fotky-web/) dostanú len WebP')
        # údaje strán stavby zo starého webu (sablona/stary-web.json, nástroj stary_web_strany.py): adresy a weby
        # investora, projektanta a zhotoviteľa, termín s mesiacmi, etapy, osoba pri role; obsah má prednosť
        sw = SABLONA / 'stary-web.json'
        sw = nacitaj_json(sw) if sw.is_file() else {}
        sw = (sw or {}).get('realizacie') if isinstance(sw, dict) else {}
        sw = sw if isinstance(sw, dict) else {}
        for r in self.realizacie:
            d = sw.get(r['slug']) if isinstance(sw.get(r['slug']), dict) else {}
            r['strany'] = {}
            for strana in ('investor', 'projektant', 'zhotovitel'):
                zoz = d.get(strana) if isinstance(d.get(strana), list) else []
                r['strany'][strana] = [{k: jeden_riadok(x.get(k)) for k in ('meno', 'adresa', 'web')}
                                       for x in zoz if isinstance(x, dict) and (x.get('adresa') or x.get('web'))]
            r['termin'] = jeden_riadok(d.get('termin'))
            r['etapy'] = [jeden_riadok(e) for e in (d.get('etapy') or []) if jeden_riadok(e)]
            r['rola_osoba'] = jeden_riadok(d.get('rola_osoba'))
        for r in self.realizacie:
            self.priprav_fotky(r, r['nazov'])
        for n in self.novinky:
            self.priprav_fotky(n, n['nadpis'])
            self.ilustracna_fotka(n)
        self.subory = {}      # relatívna cesta -> bajty
        self.obrazky = {}     # relatívna cesta -> (odtlačok, funkcia)
        self.mapa_stranok = []  # (cesta, lastmod)
        # lastmod (brána pokus 1, správnosť 6): vydanie = deň, keď sa naposledy zmenila šablóna alebo texty stránok
        # (nastavenia.json); realizácia a novinka = najnovší z vydania, dátumu príspevku a poľa upravene
        self.vydanie = platny_datum(self.nastavenia.get('vydanie'))
        if self.nastavenia.get('vydanie') and not self.vydanie:
            varuj('sablona/nastavenia.json: vydanie nie je platný dátum RRRR-MM-DD')
        datumy = sorted(self.zmena(r) for r in self.realizacie + self.novinky if self.zmena(r))
        self.najnovsi = max([d for d in (datumy[-1] if datumy else '', self.vydanie) if d], default='')

    def zmena(self, r):
        return max((d for d in (r['datum'], r['upravene'], self.vydanie) if d), default='')

    # --- fotky: rozmazané, typ, poradie galérie, obal (FOTKY.md, Pravidlo pre build.py)
    @staticmethod
    def povodny_stav(f):
        return 'pôvodný stav' in f['alt'].lower() or f.get('typ') == 'P'

    def priprav_fotky(self, r, nazov):
        p = self.pravidla
        for f in r['fotky']:
            f['typ'] = f['typ'] or p['typy'].get(f['rel'], '')
            if 'pôvodný stav' in f['alt'].lower() and not f['typ']:
                f['typ'] = 'P'
            # vizualizácia (malý render, nie fotka stavby) má vlastný prah prah_rozmazana_vizualizacia (predvolene 0)
            f['rozmazana'] = f['ostrost'] < (p['prah_viz'] if f.get('viz') else p['prah'])
        meno = r.get('slug', '')
        for f in r['fotky']:
            if f['rozmazana']:
                varuj(f'{meno}: fotka {f["rel"]} je rozmazaná (ostrosť {f["ostrost"]:g} pod '
                      f'{p["prah_viz"] if f.get("viz") else p["prah"]:g}), '
                      'nie je v galérii ani na obale')
        poradie = {t: i for i, t in enumerate(p['poradie'])}
        ostre = [(i, f) for i, f in enumerate(r['fotky']) if not f['rozmazana']]
        # galéria: podľa typu (hotová, letecká, rozostavaná, detail, interiér, pôvodný stav), v rámci typu podľa
        # ostrosti; fotky bez typu (budúci obsah) ostávajú v poradí obsahu na mieste hotovej stavby
        # vizualizácie vždy za skutočnými fotkami (obsah-zmeny.md, návrh pre stavbu bod 2)
        ostre.sort(key=lambda x: (1 if x[1].get('viz') else 0, poradie.get(x[1]['typ'] or 'HE', 0), 0 if x[1]['typ'] else 1,
                                  -x[1]['ostrost'] if x[1]['typ'] else x[0]))
        r['galeria'] = [f for _, f in ostre]
        interier_ok = bool(p['interier'].search(nazov))
        viz_obal = bool(r.get('obal_vizualizacia'))

        def mozny(f, prah, viz_smie=False):
            return (f['w'] >= f['h'] and f['w'] >= prah['min_sirka'] and f['ostrost'] >= prah['min_ostrost']
                    and f['typ'] not in NIE_OBAL and not self.povodny_stav(f) and (f['typ'] != 'I' or interier_ok)
                    and (not f.get('viz') or viz_smie))
        r['obal'] = (next((f for f in r['galeria'] if mozny(f, p['odporucany'])), None)
                     or next((f for f in r['galeria'] if mozny(f, p['obal'])), None)
                     or (next((f for f in r['galeria'] if mozny(f, p['obal'], True)), None) if viz_obal else None))
        # hrdina úvodu nikdy z vizualizácie
        r['velka'] = next((f for f in r['galeria'] if mozny(f, p['velka'])), None)
        # fotka karty (pás, zoznam): žiadna karta bez fotky (zadanie bmb-dokoncenie, Obsah 2). Poradie: obal, prvá
        # ostrá skutočná fotka z galérie (prednostne na šírku, nie pôvodný stav), vizualizácia len pri obal_vizualizacia
        skutocne = [f for f in r['galeria'] if not f.get('viz')]
        r['karta'] = (r['obal']
                      or next((f for f in skutocne if f['w'] >= f['h'] and not self.povodny_stav(f)), None)
                      or next((f for f in skutocne if not self.povodny_stav(f)), None)
                      or (skutocne[0] if skutocne else None)
                      or (r['galeria'][0] if viz_obal and r['galeria'] else None))
        self.karta_z_nastaveni(r)

    def karta_z_nastaveni(self, r):
        """Fotka karty realizácie (pás, mriežka, titulná fotka detailu) určená v sablona/nastavenia.json karta_foto
        (slug -> súbor, linka bmb-presnost: ostrejšia fotka s väčším zdrojom z vlastnej galérie, obsah sa nemení).
        Len skutočná fotka z galérie (nie rozmazaná, nie vizualizácia ani pôvodný stav); inak varovanie a karta podľa
        pravidiel vyššie. Novinky sa neriadia týmto nastavením (ich fotky sú v obsah/obrazky/novinky/)."""
        mapa = self.nastavenia.get('karta_foto')
        slug = r.get('slug', '')
        if not isinstance(mapa, dict) or slug not in mapa or any(f['rel'].startswith('novinky/') for f in r['fotky']):
            return
        volba = mapa.get(slug)
        # {"subor": ..., "detail": false}: len karta mriežky a služieb (karta_len), titulná fotka detailu a pás ostávajú
        # (brána bmb-presnost pokus 1, nález 2: Kalvária 02 má v rohu neostré listy, 01 bez nich je na detail malá)
        len_karta = isinstance(volba, dict) and volba.get('detail') is False
        subor = jeden_riadok(volba.get('subor') if isinstance(volba, dict) else volba)
        f = next((x for x in r['galeria'] if x['rel'] == f'{slug}/{subor}'), None)
        if f is None or f.get('viz') or self.povodny_stav(f):
            varuj(f'nastavenia karta_foto: fotka {subor} realizácie {slug} nie je v galérii, je rozmazaná, vizualizácia '
                  'alebo pôvodný stav, karta ostáva podľa pravidiel')
            return
        # {"subor": ..., "vyrez": [x0, y0, x1, y1]}: výrez 4 : 5 karty len z tohto rámika fotky (podiely 0 až 1), napr.
        # viac domu a menej holej hliny (brána bmb-presnost pokus 1, nález 9); výrez musí mať aspoň 768 px zdroja
        vyrez = volba.get('vyrez') if isinstance(volba, dict) else None
        if vyrez is not None:
            ok = (isinstance(vyrez, list) and len(vyrez) == 4 and all(isinstance(x, (int, float)) for x in vyrez)
                  and 0 <= vyrez[0] < vyrez[2] <= 1 and 0 <= vyrez[1] < vyrez[3] <= 1)
            if ok:
                x0, y0, x1, y1 = box_px(vyrez, f['w'], f['h'])
                ok = orez_rozmer(x1 - x0, y1 - y0, OREZY['45'])[0] >= 768
            if ok:
                f['vyrez45'] = tuple(float(x) for x in vyrez)
                self.vyrezy[f['sha']] = f['vyrez45']
            else:
                varuj(f'nastavenia karta_foto: výrez {vyrez!r} realizácie {slug} nie je rámik 0 až 1 s výrezom 4 : 5 '
                      'aspoň 768 px zdroja, karta ide zo stredu fotky')
        if len_karta:
            r['karta_len'] = f
            return
        # nahradená fotka karty (malý zdroj, preto ju karta_foto vymenilo) nesmie byť prvou veľkou fotkou galérie
        # (galeria(), overenie zostavy bmb-presnost: inak by sa mäkká fotka ukázala cez celú šírku galérie)
        if r.get('karta') is not f:
            r['karta_nahradena'] = r.get('karta')
        r['karta'] = f

    def ilustracna_fotka(self, n):
        """Novinka bez vlastnej použiteľnej fotky (karta None): skutočná ostrá fotka stavby z realizácie podľa
        sablona/fotky-pravidla.json (ilustracne_fotky[slug novinky], inak ilustracna_predvolena), s popisom
        „Ilustračná fotka: <realizácia>, <miesto>“ (oprava po bráne, bod 2: žiadna karta bez fotky, nikto nesmie
        myslieť, že fotka zobrazuje predmet novinky). Fotka musí byť v galérii realizácie (nie rozmazaná), ostrá aspoň
        ako obal, nie vizualizácia ani pôvodný stav; inak build varuje a karta ostane s logom. Ilustračná fotka je
        len na karte (n['ilustracna_foto'] a n['ilustracna_r']); n['karta'] ostáva None, takže stránka novinky ani
        jej OG obrázok ju neukážu ako fotku novinky."""
        n['ilustracna'] = ''
        n['ilustracna_foto'] = n['ilustracna_r'] = None
        if n.get('karta') is not None:
            return
        p = self.pravidla
        for kluc, volba in ((n['slug'], p['ilustracne'].get(n['slug'])), ('ilustracna_predvolena', p['ilustracna_predvolena'])):
            if not volba:
                continue
            r = next((x for x in self.realizacie if x['slug'] == volba.get('slug')), None)
            subor = jeden_riadok(volba.get('subor'))
            f = next((x for x in r['galeria'] if x['rel'] == f'{r["slug"]}/{subor}'), None) if r else None
            if (f is None or f.get('viz') or self.povodny_stav(f) or f['ostrost'] < p['obal']['min_ostrost']):
                varuj(f'novinka {n["slug"]}: ilustračná fotka {kluc} ({volba.get("slug")}/{subor}) chýba, nie je ostrá, '
                      'je vizualizácia alebo pôvodný stav, nepoužitá')
                continue
            n['ilustracna_foto'] = f
            n['ilustracna_r'] = r
            n['ilustracna'] = f'Ilustračná fotka: {self.nazov_s_miestom(r)}'
            return
        varuj(f'novinka {n["slug"]}: bez fotky a bez ilustračnej fotky v sablona/fotky-pravidla.json, karta s logom')

    # --- pomocné
    def abs_url(self, cesta):
        return self.domena + '/' + cesta

    @staticmethod
    def pre(cesta):
        return '../' * cesta.count('/')

    @staticmethod
    def rel(pre, ciel):
        r = pre + ciel
        return r if r else './'

    # --- súbory fotiek (linka bmb-fotky): AVIF a WebP v šírkach podľa predlohy, hotové z fotky-web/ alebo z obsahu
    def nastav_sirky(self, f):
        """Predloha fotky: hotová webová verzia (fotky-web/manifest.json, záznam so sha256 zdroja; môže byť zväčšená
        z rozmazanej verzie, takže širšia než zdroj) alebo zdroj z obsah/ (nová fotka zo systému obsahu, iný sha256).
        Pravidlá výberu fotky (obal, úvod, ostrosť) ostávajú podľa zdroja (f['w'], f['h'])."""
        f['fw'] = None
        if self.predlohy is not None:
            w, h = self.predlohy.get(f['sha']) or (f['w'], f['h'])
            ma_avif = self.avif
        else:
            z = self.fotky_web.zaznam(f['sha'])
            if z:
                f['fw'] = z
                w, h = int(z['predloha']['w']), int(z['predloha']['h'])
            else:
                w, h = f['w'], f['h']
            # AVIF z fotky-web/ sa len kopíruje, kodek treba len na súbory, ktoré tam chýbajú
            ma_avif = self.avif or bool(z and any(str(k).endswith('|avif') for k in (z.get('subory') or {})))
        nastav_sirky(f, w, h, FORMATY if ma_avif else ('webp',))

    def da_sa(self, f, s, fmt, orez=''):
        """Súbor sa dá dať na web: WebP vždy (z fotky-web alebo z obsahu), AVIF s kodekom alebo hotový vo fotky-web."""
        if fmt == 'webp' or self.avif:
            return True
        return bool(f.get('fw')) and self.fotky_web.subor(f['sha'], orez, s, fmt) is not None

    def sirky_variantu(self, f, fmt, orez=''):
        """Šírky súborov fotky vo formáte fmt (orez '' celá fotka, 45 alebo 54 výrez zo stredu predlohy)."""
        if orez:
            if fmt == 'avif' and not f['sirky_avif']:
                return []
            zw, zh = f['zaklad']
            if orez == '45' and f.get('vyrez45'):
                x0, y0, x1, y1 = box_px(f['vyrez45'], zw, zh)
                zw, zh = x1 - x0, y1 - y0
            cw, _ = orez_rozmer(zw, zh, OREZY[orez])
            sirky = sirky_orezu(cw)
        else:
            sirky = f['sirky_avif'] if fmt == 'avif' else f['sirky']
        return [s for s in sirky if self.da_sa(f, s, fmt, orez)]

    def variant(self, r, f, s, fmt, orez=''):
        """Cesta súboru <obr_dir>/<nn>-<šírka>[-<orez>].<formát> a jeho registrácia na výrobu: kópia z fotky-web/,
        keď sedí sha256 zdroja a rozmer, inak kódovanie z obsah/ s kvalitou KVALITA (rovnaký odtlačok = bez kódovania)."""
        cesta = f'{r["obr_dir"]}/{f["nn"]}-{s}{"-" + orez if orez else ""}.{fmt}'
        if cesta in self.obrazky:
            return cesta
        if orez:
            a, b = OREZY[orez]
            v = max(1, round(s * b / a))
        else:
            v = vyska_pre(f['zaklad'][0], f['zaklad'][1], s)
        self.potreba.setdefault(f['sha'], {})[kluc_variantu(orez, s, fmt)] = (s, v, orez, fmt)
        self.zdroje[f['sha']] = f['zdroj']
        box = f.get('vyrez45') if orez == '45' else None
        hotovy = self.fotky_web.subor(f['sha'], orez, s, fmt) if f.get('fw') else None
        # výrez z rámika (karta_foto vyrez) len zo súboru fotky-web s tým istým rámikom, inak z obsahu
        if hotovy and (hotovy.get('w'), hotovy.get('h')) == (s, v) and hotovy.get('vyrez') == (list(box) if box else None):
            odt = hashlib.sha256(f'fotky-web|{hotovy["sha256"]}'.encode('utf-8')).hexdigest()
            self.obrazky[cesta] = (odt, (lambda x=hotovy: FotkyWeb.citaj(x)))
            self.pocty_fotiek['fotky-web'] += 1
        else:
            odt = hashlib.sha256('|'.join((f['sha'], VERZIA_OBR, fmt, str(KVALITA[fmt]), str(s), str(v), orez)
                                          + ((repr(box),) if box else ())).encode('utf-8')).hexdigest()
            pm = OREZY[orez] if orez else None
            # rámik len keď je (testy a nástroje nahrádzajú kódovanie funkciou so 4 parametrami)
            if fmt == 'avif':
                self.obrazky[cesta] = (odt, (lambda p=f['zdroj'], s=s, v=v, pm=pm, box=box:
                                             avif_bajty(p, s, v, pm, box) if box else avif_bajty(p, s, v, pm)))
            else:
                self.obrazky[cesta] = (odt, (lambda p=f['zdroj'], s=s, v=v, pm=pm, box=box:
                                             webp_bajty(p, s, v, pm, box) if box else webp_bajty(p, s, v, pm)))
            self.pocty_fotiek['obsah'] += 1
        return cesta

    def obrazok_cesty(self, r, f):
        """Cesty WebP celej fotky {šírka: cesta} (záloha, plná fotka odkazu); registruje ich na výrobu."""
        return {s: self.variant(r, f, s, 'webp') for s in self.sirky_variantu(f, 'webp')}

    def registruj_fotku(self, r, f):
        """Všetky súbory celej fotky (WebP aj AVIF) pre lightbox zo skrátenej zmluvy pásu (data-sirky)."""
        for fmt in FORMATY:
            for s in self.sirky_variantu(f, fmt):
                self.variant(r, f, s, fmt)

    def srcset(self, pre, r, f, fmt='webp', orez=''):
        return ', '.join(f'{esc(pre + self.variant(r, f, s, fmt, orez))} {s}w' for s in self.sirky_variantu(f, fmt, orez))

    def orez_srcset(self, pre, r, f, kod, fmt='webp'):
        sirky = self.sirky_variantu(f, fmt, kod)
        a, b = OREZY[kod]
        cesty = {s: (self.variant(r, f, s, fmt, kod), max(1, round(s * b / a))) for s in sirky}
        return ', '.join(f'{esc(pre + c)} {s}w' for s, (c, _) in cesty.items()), cesty

    def img(self, pre, r, f, sizes, lcp=False, lazy=True, trieda='', alt=None, orez=None, odlozene=False, zdroje=''):
        """Fotka ako <picture>: <source type="image/avif"> so srcset a sizes, potom <img> so WebP (src, srcset,
        sizes, rozmery). zdroje: <source> s media pred AVIF (mobilný výrez pohľadu A). odlozene (karta pásu): v src
        prázdny obrázok, skutočné srcset v data-src, data-srcset a AVIF v data-avif; obal picture so zdrojom AVIF
        doplní až app.js (source bez srcset by bol neplatný HTML). lcp: fetchpriority=high bez loading=lazy."""
        orez = orez or ''
        webp = self.sirky_variantu(f, 'webp', orez)
        srcset = self.srcset(pre, r, f, 'webp', orez)
        if orez:
            stred = [s for s in webp if s <= 720]
            a, b = OREZY[orez]
            w, h = webp[-1], max(1, round(webp[-1] * b / a))
        else:
            stred = [s for s in webp if s <= 960]
            w, h = f['max_w'], f['max_h']
        src = self.variant(r, f, stred[-1] if stred else webp[0], 'webp', orez)
        avif = self.srcset(pre, r, f, 'avif', orez)
        if lcp:
            nacit = 'fetchpriority="high"'
        elif lazy and not odlozene:
            nacit = 'loading="lazy" decoding="async"'
        else:
            nacit = 'decoding="async"'
        t = f' class="{trieda}"' if trieda else ''
        alt = esc(alt if alt is not None else f['alt'])
        if odlozene:
            return (f'<img{t} src="{esc(pre)}prazdna-45.webp" data-src="{esc(pre + src)}" data-srcset="{srcset}"'
                    + (f' data-avif="{avif}"' if avif else '')
                    + f' sizes="{sizes}" width="{w}" height="{h}" alt="{alt}" {nacit}>')
        obr = (f'<img{t} src="{esc(pre + src)}" srcset="{srcset}" sizes="{sizes}" width="{w}" height="{h}" '
               f'alt="{alt}" {nacit}>')
        avif_zdroj = f'<source type="image/avif" srcset="{avif}" sizes="{sizes}">' if avif else ''
        if not avif_zdroj and not zdroje:
            return obr
        return f'<picture>{zdroje}{avif_zdroj}{obr}</picture>'

    def og_cesta_realizacie(self, r):
        return f'og/{r["slug"]}.jpg'

    def pismo_odt(self):
        odt = ''
        for v in (400, 700):
            p = SABLONA / f'arimo-{v}.ttf'
            odt += hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else 'x'
        return odt + hashlib.sha256(self.logo_svg.encode('utf-8')).hexdigest()[:16]

    def registruj_og(self, cesta, foto, nadpis, riadok2, riadok3):
        pismo_odt = self.pismo_odt()
        if foto is not None:
            odt = odtlacok_suboru(foto, VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)
        else:
            odt = hashlib.sha256('|'.join((VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)).encode()).hexdigest()
        logo = self.logo_svg
        self.obrazky[cesta] = (odt, (lambda: og_bajty(foto, nadpis, riadok2, riadok3, logo)))

    def mesto(self, r):
        return r['miesto'].split(',')[0].strip() if r['miesto'] else ''

    def nazov_s_miestom(self, r):
        m = self.mesto(r)
        if m and m.lower() not in r['nazov'].lower():
            return f'{r["nazov"]}, {m}'
        return r['nazov']

    def adresa(self):
        f = self.firma
        return ', '.join(x for x in (f['ulica'], ' '.join(y for y in (f['psc'], f['mesto']) if y)) if x)

    @staticmethod
    def roky_karty(r):
        """Rok alebo roky na karte (zadanie bmb-dokoncenie, Obsah 2): roky stavby; bez nich rok príspevku
        (rok_prispevku z obsahu, inak rok dátumu príspevku z Facebooku), bez slova Facebook."""
        if r.get('roky'):
            return r['roky']
        if r.get('rok_prispevku'):
            return r['rok_prispevku']
        if r.get('zdroj') == 'fb' and r.get('datum'):
            return r['datum'][:4]
        return ''

    @classmethod
    def roky_stitok(cls, r):
        """Text kóty a údajov karty: roky stavby; bez nich „príspevok RRRR“, aby sa rok príspevku nečítal ako rok
        realizácie (brána bmb-fb-uplnost, nález 1; detail má ten istý rok pod popiskom Rok príspevku)."""
        if r.get('roky'):
            return r['roky']
        rok = cls.roky_karty(r)
        return f'príspevok {rok}' if rok else ''

    @staticmethod
    def rok_filtra(r):
        """Rok pre filter Roky a stránky po desaťročiach (data-rok): len skutočný rok realizácie z poľa roky.
        Rok príspevku (rok_z_datumu) sa nemieša so skutočnými rokmi, ide do voľby „rok neuvedený“ (0)."""
        return 0 if r.get('rok_z_datumu') else int(r.get('rok') or 0)

    def kedy(self, r):
        """Roky stavby; bez nich „príspevok RRRR“ (roky_stitok), nikdy slovo Facebook na karte."""
        if r['roky']:
            return f'<span class="roky">{T(r["roky"])}</span>'
        return T(self.roky_stitok(r))

    def kat_nazov(self, k):
        return dict(KATEGORIE).get(k, k)

    def pocty_druhov(self, zoznam=None):
        zoznam = self.realizacie if zoznam is None else zoznam
        return {k: sum(1 for r in zoznam if k in r['kategorie']) for k, _ in KATEGORIE}

    # --- prvky štýlu Čiara (ukážka Ateliér Olovnica, dizajn-olovnica/OLOVNICA-ROZBOR.md)
    def kota(self, text, trieda='', staticka=False):
        """Kóta z ukážky: vlasová čiara so šikmými koncami a údajom v strede (roky). Číslice sú stĺpce, ktoré sa pri
        vstupe do obrazu raz pretočia (app.js pridá triedu otocene, CSS dodá číslice); čítačka číta skrytý text.
        Polohu číslice nesú triedy c0 až c9 a i0 až i9 (CSP nedovoľuje atribút style).
        staticka (karty zoznamu realizácií): tá istá kóta bez pretáčania, číslice ako text v Mono. Valce majú asi
        450 bajtov na kartu; pri 60 kartách by HTML zoznamu narástlo o 27 kB a Lighthouse mobil klesol (bmb-fb-uplnost)."""
        if staticka:
            kusy = ''.join(f'<span class="kota-slovo">{esc(k)}</span>' if re.search(r'[^\W\d_]', k) else esc(k)
                           for k in re.findall(r'\s+|[^\s]+', text))
            t = f' {trieda}' if trieda else ''
            return f'<span class="kota{t}"><span class="kota-text">{sadzba(kusy)}</span></span>'
        casti, i = [], 0
        # slová (až) písmom Sans ako v ukážke (.kota-slovo), číslice v Mono; medzery pevné
        for kus in re.findall(r'\d|\s+|[^\d\s]+', text):
            if kus.isdigit() and i < 10:
                casti.append(f'<span class="valec"><span class="stlpec c{kus} i{i}"></span></span>')
                i += 1
            elif kus.isspace():
                casti.append(' ')
            elif re.search(r'[^\W\d_]', kus):
                casti.append(f'<span class="kota-slovo">{esc(kus)}</span>')
            else:
                casti.append(esc(kus))
        t = f' {trieda}' if trieda else ''
        return (f'<span class="kota{t}"><span class="kota-text otoc"><span class="vh">{T(text)}</span>'
                f'<span class="valce" aria-hidden="true">{"".join(casti)}</span></span></span>')

    def akcie(self, trieda='akcie', vyzva=''):
        """Dve tlačidlá zo zadania: Zavolať (tel:) a Napísať e-mail (mailto:); jedna menovka na zámer."""
        f = self.firma
        a = []
        if f['tel_href']:
            a.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať {SVG_SIPKA}</a>')
        if f['email']:
            a.append(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{esc(f["email"])}">Napísať e-mail</a>')
        v = f' {vyzva}' if vyzva else ''
        return f'<div class="{trieda}"{v}>{"".join(a)}</div>' if a else ''

    def socialne(self):
        """Odkazy na Facebook a Instagram. Instagram len keď je jeho adresa v sablona/nastavenia.json (istá adresa,
        dizajn-olovnica/INSTAGRAM.md); inak sa odkaz nezobrazí."""
        out = []
        if self.firma['facebook']:
            out.append(f'<a class="odkaz-von" href="{esc(self.firma["facebook"])}">Facebook {SVG_VON}</a>')
        if self.instagram:
            out.append(f'<a class="odkaz-von" href="{esc(self.instagram)}">Instagram {SVG_VON}</a>')
        return out

    def vizitka(self, pre, trieda='', alt_loga=''):
        """Kontaktný blok v poradí banneru na plote: meno, ulica, mesto, tel., e-mail, web (poznávací prvok značky)."""
        f = self.firma
        riadky = [f'<span class="vizitka-meno">{T(f["meno"])}</span>']
        if f['ulica']:
            riadky.append(f'<span>{T(f["ulica"])}</span>')
        mesto = ' '.join(x for x in (f['psc'], f['mesto']) if x)
        if mesto:
            riadky.append(f'<span>{T(mesto)}</span>')
        if f['tel']:
            riadky.append(f'<span><span class="vizitka-k">tel.:</span> <a class="cisla" href="tel:{esc(f["tel_href"])}">'
                          f'{T(f["tel"])}</a></span>')
        if f['email']:
            riadky.append(f'<span><span class="vizitka-k">e-mail:</span> <a href="mailto:{esc(f["email"])}">'
                          f'{T(f["email"])}</a></span>')
        host = urllib.parse.urlsplit(self.domena).hostname or ''
        if host:
            riadky.append(f'<span>{T(host if host.startswith("www.") else "www." + host)}</span>')
        t = f' {trieda}' if trieda else ''
        return (f'<address class="vizitka{t}"><img class="vizitka-logo" src="{esc(pre)}logo.svg" width="64" height="64" '
                f'alt="{esc(alt_loga)}" loading="lazy" decoding="async"><span class="vizitka-riadky">{"".join(riadky)}</span></address>')

    def kontakt_sekcia(self, pre, hlavna=False, nadpis=None, veta=None):
        """Sekcia Kontakt z ukážky (U:1075 až U:1149) na svetlejšom podklade: vľavo nadpis, výzva, Zavolať a Napísať
        e-mail a fakturačné údaje so všetkým zo starého webu (stary-web/kontakt.md, aj bankové spojenie podľa
        FAKTY-WEB.md r. 14) a Facebook; vpravo karta namiesto ilustračnej mapy (ulica ukážky je vymyslená): kontaktný
        blok v poradí banneru na plote a pod ním Navigovať (Mapy Google) a Mapy.cz. Je to záver každej stránky okrem
        /kontakt/, kde je hlavným obsahom (hlavna=True: nadpis h2 Kontaktné údaje, bez výzvy)."""
        f = self.firma
        tk = self.texty['kontakt']
        nadpis = (tk.get('sekcia_nadpis') or 'Kontaktné údaje') if hlavna else (nadpis or 'Kontakt')
        veta = '' if hlavna else (veta or self.texty['vyzvy']['zaver'])
        adresa = self.adresa()
        gmaps = f'https://www.google.com/maps/search/?api=1&query={urllib.parse.quote_plus(adresa)}'
        mapy = f'https://mapy.cz/zakladni?q={urllib.parse.quote(adresa)}'
        pole = [('Obchodné meno', f['meno']), ('Konateľ', f['konatel']), ('Sídlo', adresa), ('IČO', f['ico']),
                ('IČ DPH', f['ic_dph']), ('Zápis', f['zapis']), ('Bankové spojenie', tk.get('banka', '')),
                ('Číslo účtu', tk.get('ucet', ''))]
        dl = ''.join(f'<div><dt>{T(k)}</dt><dd{CISLA if k in ("IČO", "IČ DPH", "Číslo účtu") else ""}>{T(v)}</dd></div>'
                     for k, v in pole if v)
        soc = self.socialne()
        if hlavna and f['facebook']:
            # na stránke Kontakt aj adresa profilu ako text (starý web mal ikonu Facebooku s odkazom, údaj uvod-facebook-ikona)
            p = urllib.parse.urlsplit(f['facebook'])
            adresa_fb = (p.hostname or '').removeprefix('www.') + p.path.rstrip('/')
            soc[0] = f'<a class="odkaz-von" href="{esc(f["facebook"])}">Facebook: {T(adresa_fb)} {SVG_VON}</a>'
        vyzva = '' if hlavna else ' data-vyzva-dole'
        posobnost = tk.get('posobnost', '')
        return (f'<section class="sekcia kontakt" id="kontakt" aria-labelledby="kontakt-nadpis"{vyzva}>'
                f'<div class="obsah mriezka kontakt-mriezka"><div class="kontakt-text">'
                f'<h2 class="nadpis" id="kontakt-nadpis">{T(nadpis)}</h2>'
                + (f'<p class="uvodny">{T(veta)} {T(posobnost)}</p>' if veta else '')
                + self.akcie('kontakt-akcie', 'data-vyzva-hore' if hlavna else '')
                # vizitka (telefón, e-mail) v DOM hneď za tlačidlami, na mobile nad fakturačnými údajmi; na počítači
                # vpravo a pripnutá, fakturačné údaje vľavo pod tlačidlami (overenie kolo 1)
                + '</div><div class="kontakt-mapa"><figure class="mapa-karta">'
                + self.vizitka(pre, alt_loga='Logo BM Building, s.r.o.' if hlavna else '')
                + f'<figcaption class="mapa-spodok"><a class="tlacidlo tlacidlo-obrys" href="{esc(gmaps)}">Navigovať {SVG_SIPKA}</a>'
                f'<a class="odkaz-von" href="{esc(mapy)}">Mapy.cz {SVG_VON}</a></figcaption></figure></div>'
                + f'<div class="kontakt-udaje"><h3 class="kontakt-h">{T(tk.get("udaje_nadpis") or "Fakturačné údaje")}</h3><dl class="fakty kontakt-fakty">{dl}</dl>'
                + (f'<p class="kontakt-pozn">{"".join(soc)}</p>' if soc else '')
                + '</div></div></section>')

    def zaver(self, pre='', hlavny=None, dalej=None):
        """Záver každej stránky okrem Kontaktu: sekcia Kontakt z ukážky (kontakt_sekcia) s výzvou stránky."""
        return self.kontakt_sekcia(pre, nadpis=hlavny, veta=dalej)

    def hlava_stranky(self, pre, kroky, h1, pod='', bok='', navyse='', trieda=''):
        """Hlava podstránky: omrvinky, vlasová čiara cez celú šírku, veľký ľahký nadpis vľavo a veta alebo údaje
        vpravo (dvojstrana z úvodu ukážky). pod: druhá časť nadpisu (mesto, dátum), oddelená skrytou čiarkou."""
        h1_html = T(h1) + (f'<span class="vh">, </span><span class="nadpis-pod">{T(pod)}</span>' if pod else '')
        t = f' {trieda}' if trieda else ''
        return (f'<div class="hlava-stranky{t}"><div class="obsah">{self.omrvinky(pre, kroky) if kroky else ""}'
                f'<div class="mriezka hlava-mriezka"><h1 class="nadpis-stranky" id="nadpis">{h1_html}</h1>'
                + (f'<div class="hlava-bok">{bok}</div>' if bok else '') + f'</div>{navyse}</div></div>')

    # --- kostra stránky
    def menu(self, pre, aktivne):
        """Hlavička ukážky (U:692 až U:709, trieda hlava): logo (sablona/logo.svg) namiesto znaku Olovnica, menu
        podľa starého webu (Činnosť, Realizácie, Postup, Novinky, Oprávnenia, Kontakt), tlačidlo Zavolať (tel:);
        do 1099 px menu v details (funguje bez JS, app.js dopĺňa Esc a zatvorenie)."""
        sluzby = list(self.texty['sluzby'])

        def a(ciel, text, kluc):
            cur = ''
            if aktivne and aktivne == kluc:
                cur = ' aria-current="page"'
            elif aktivne and kluc and (aktivne.startswith(kluc + '/') or (kluc == 'sluzby' and aktivne in sluzby)):
                cur = ' aria-current="true"'
            return f'<a href="{esc(self.rel(pre, ciel))}"{cur}>{T(text)}</a>'
        polozky = [('#sluzby', 'Činnosť', 'sluzby'), ('realizacie/', 'Realizácie', 'realizacie'), ('#postup', 'Postup', 'postup')]
        if self.novinky:
            polozky.append(('novinky/', 'Novinky', 'novinky'))
        polozky += [('opravnenia/', 'Oprávnenia', 'opravnenia'), ('kontakt/', 'Kontakt', 'kontakt')]
        d = ''.join(f'<li>{a(c, t, k)}</li>' for c, t, k in polozky)
        m = []
        for c, t, k in polozky:
            m.append(f'<li>{a(c, t, k)}')
            if k == 'sluzby':
                m.append('<ul>' + ''.join(f'<li>{a(s + "/", v["menu"], s)}</li>' for s, v in self.texty['sluzby'].items())
                         + '</ul>')
            m.append('</li>')
        f = self.firma
        tel = ''
        if f['tel_href']:
            tel = (f'<a class="tlacidlo tlacidlo-male hlavicka-tel" href="tel:{esc(f["tel_href"])}" '
                   f'aria-label="Zavolať {esc(f["tel"])}">Zavolať</a>')
        return ('<header class="hlava">\n<div class="obsah">'
                f'<a class="znak" href="{esc(self.rel(pre, ""))}"><img src="{esc(pre)}logo.svg" width="52" height="52" '
                f'alt="BM Building, úvod"></a>\n'
                f'<nav class="menu-d" aria-label="Hlavná"><ul class="menu">{d}</ul></nav>\n'
                f'<div class="hlava-akcie">{tel}<details class="menu-m"><summary class="tlacidlo tlacidlo-obrys tlacidlo-male">Menu</summary>'
                f'<nav class="menu-panel" aria-label="Hlavná, menu"><ul>{"".join(m)}</ul></nav></details></div>\n'
                '</div></header>\n')

    def pata(self, pre, stlpec_firma=True):
        """Päta ukážky (U:1152 až U:1161): logo, jedna veta s adresou a IČO, odkazy (Facebook, Instagram len s istou
        adresou, Oprávnenia, Ochrana osobných údajov), drobné písmo s © a zápisom v registri a nenápadne „Web: ARLing“.
        Stránky a fakturačné údaje sú v sekcii Kontakt (kontakt_sekcia); stlpec_firma ostáva len pre spätnú zhodu."""
        f = self.firma
        veta = f'{f["meno"]}, {self.adresa()}.' + (f' IČO {f["ico"]}.' if f['ico'] else '')
        odkazy = self.socialne()
        odkazy.append(f'<a href="{esc(self.rel(pre, "opravnenia/"))}">Oprávnenia</a>')
        odkazy.append(f'<a href="{esc(self.rel(pre, "ochrana-udajov/"))}">Ochrana osobných údajov</a>')
        rok = self.najnovsi[:4]
        # s_bodkou: meno „BM Building, s.r.o.“ už bodkou končí, „s.r.o..“ nie (overenie kolo 1)
        male = (f'<span>© {T(rok + " " if rok else "")}{T(s_bodkou(f["meno"]))}</span> '
                + (f'<span>{T(f["zapis"])}</span> ' if f['zapis'] else '')
                + '<a class="pata-arling" href="https://arling.sk/web/">Web: ARLing</a>')
        return ('<footer class="pata" data-vyzva-dole>\n<div class="obsah mriezka pata-mriezka">\n'
                f'<div class="pata-zaver"><a class="znak pata-znak" href="{esc(self.rel(pre, ""))}"><img src="{esc(pre)}logo.svg" '
                'width="40" height="40" alt="BM Building, úvod" loading="lazy"></a>'
                f'<p class="pata-veta">{T(veta)}</p><p class="pata-odkazy">{"".join(odkazy)}</p></div>\n'
                f'<p class="male">{male}</p>\n</div>\n</footer>\n')

    def lista(self):
        """Spodná lišta na mobile (ukážka 12): Zavolať a Napísať e-mail, vyjde až keď tlačidlá úvodu odídu z obrazu."""
        f = self.firma
        a = []
        if f['tel_href']:
            a.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať</a>')
        if f['email']:
            a.append(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{esc(f["email"])}">Napísať e-mail</a>')
        return f'<nav class="lista" aria-label="Rýchly kontakt">{"".join(a)}</nav>\n' if a else ''

    def omrvinky(self, pre, kroky):
        li = []
        for i, (ciel, text) in enumerate(kroky):
            if i == len(kroky) - 1:
                li.append(f'<li><span aria-current="page">{T(text)}</span></li>')
            else:
                li.append(f'<li><a href="{esc(self.rel(pre, ciel))}">{T(text)}</a></li>')
        return f'<nav class="omrvinky" aria-label="Omrvinky"><ol>{"".join(li)}</ol></nav>'

    def ld_firma(self):
        f = self.firma
        uzol = {
            '@type': ['Organization', 'ProfessionalService'],
            '@id': self.abs_url('#firma'),
            'name': 'BM Building',
            'legalName': f['meno'],
            'url': self.abs_url(''),
            # logo aj image: PNG 512 x 512 renderované zo sablona/logo.svg, absolútna URL (LOGO.md bod 5, Google žiada
            # aspoň 112 px); OG obrázok 1200 x 630 ostáva len v og:image a twitter:image
            'logo': self.abs_url('logo.png'),
            'image': self.abs_url('logo.png'),
            'address': {'@type': 'PostalAddress', 'streetAddress': f['ulica'], 'postalCode': f['psc'],
                        'addressLocality': f['mesto'], 'addressCountry': 'SK'},
            'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'},
                           {'@type': 'AdministrativeArea', 'name': f['posobnost']}],
            'knowsAbout': [v['menu'] for v in self.texty['sluzby'].values()] + ['Technický dozor investora (TDI)'],
        }
        if f['tel']:
            uzol['telephone'] = f['tel']
        if f['email']:
            uzol['email'] = f['email']
        same = [x for x in (f['facebook'], self.instagram) if x]
        if same:
            uzol['sameAs'] = same
        if f['ic_dph']:
            uzol['vatID'] = f['ic_dph']
        if platny_datum(f['den_zapisu']):
            uzol['foundingDate'] = f['den_zapisu']
        if f['konatel']:
            uzol['employee'] = {'@type': 'Person', 'name': f['konatel'], 'jobTitle': 'konateľ'}
        return uzol

    def ld_breadcrumb(self, kroky):
        return {'@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': t, 'item': self.abs_url(c)}
            for i, (c, t) in enumerate(kroky, 1)]}

    def stranka(self, cesta, title, description, telo, aktivne, ld_uzly, og_obr='og/bmbuilding.jpg',
                og_typ='website', do_mapy=True, lastmod=None, zaklad=None, pata_firma=True, trieda='', preload_obr=None):
        # zaklad (len 404): absolútne cesty od koreňa webu („/“ alebo „/bmbuilding/“) namiesto <base>; s <base> by
        # „Preskočiť na obsah“ (#obsah) viedlo na úvod (brána pokus 2, správnosť 8)
        pre = zaklad if zaklad else self.pre(cesta)
        css = (self.css_uvod if cesta == '' else self.css_sablona).replace('@PRE@', pre)
        ld = ld_json({'@context': 'https://schema.org', '@graph': ld_uzly}) if ld_uzly else ''
        # connect-src 'self': Lighthouse a PageSpeed Insights čítajú robots.txt cez stránku (kontrola kolo 1).
        # script-src: vlastný app.js ('self'), jeden vložený skript a JSON-LD cez sha256; žiadne cudzie zdroje,
        # žiadne inline handlery ani unsafe-inline (v2, SMER.md 2.12)
        skripty = ["'self'", sha(SKRIPT_JS)] + ([sha(ld)] if ld else [])
        csp = ["default-src 'none'", "img-src 'self'", "font-src 'self'", "connect-src 'self'", "manifest-src 'self'",
               'style-src ' + sha(css) + ' ' + sha(NOSCRIPT_CSS), 'script-src ' + ' '.join(skripty),
               "base-uri 'none'", "form-action 'none'"]
        canon = self.abs_url(cesta or '')
        h = ['<!doctype html>', '<html lang="sk">', '<head>', '<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             # CSP s doslovnými apostrofmi (len pevné kľúčové slová a base64 hashe, žiadny obsah z JSON)
             f'<meta http-equiv="Content-Security-Policy" content="{"; ".join(csp)}">']
        if self.nahlad:
            h.append('<meta name="robots" content="noindex, nofollow">')
        h += ['<meta name="color-scheme" content="light">', '<meta name="theme-color" content="#17476f">',
              f'<title>{esc(title)}</title>']
        if description:
            h.append(f'<meta name="description" content="{esc(description)}">')
        if cesta is not None and not zaklad:
            h.append(f'<link rel="canonical" href="{esc(canon)}">')
        h += [f'<link rel="icon" href="{esc(pre)}favicon.svg" type="image/svg+xml">',
              f'<link rel="icon" href="{esc(pre)}favicon-32.png" type="image/png" sizes="32x32">',
              f'<link rel="icon" href="{esc(pre)}favicon-48.png" type="image/png" sizes="48x48">',
              f'<link rel="icon" href="{esc(pre)}favicon.ico" sizes="16x16 32x32 48x48">',
              f'<link rel="apple-touch-icon" href="{esc(pre)}apple-touch-icon.png" sizes="180x180">',
              f'<link rel="manifest" href="{esc(pre)}site.webmanifest">']
        # Geist a Geist Mono (OFL 1.1, ukážka Olovnica): preload latinky textu, slovenskej diakritiky a čísel v Mono
        h += [f'<link rel="preload" href="{esc(pre)}fonts/{p}" as="font" type="font/woff2" crossorigin>' for p in PRELOAD_PISMA]
        # úvod: preload fotky pohľadu A (LCP) podľa šírky, aby sa sťahovala hneď s hlavou dokumentu (Lighthouse mobil)
        h += preload_obr or []
        h += ['<meta property="og:locale" content="sk_SK">', f'<meta property="og:type" content="{og_typ}">',
              '<meta property="og:site_name" content="BM Building">',
              f'<meta property="og:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta property="og:description" content="{esc(description)}">')
        if not zaklad:
            h.append(f'<meta property="og:url" content="{esc(canon)}">')
        h += [f'<meta property="og:image" content="{esc(self.abs_url(og_obr))}">',
              f'<meta property="og:image:width" content="{OG_W}">', f'<meta property="og:image:height" content="{OG_H}">',
              '<meta name="twitter:card" content="summary_large_image">',
              f'<meta name="twitter:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta name="twitter:description" content="{esc(description)}">')
        h.append(f'<meta name="twitter:image" content="{esc(self.abs_url(og_obr))}">')
        h.append(f'<style>{css}</style>')
        h.append(f'<noscript><style>{NOSCRIPT_CSS}</style></noscript>')
        h.append(f'<script>{SKRIPT_JS}</script>')
        if self.js:
            h.append(f'<script src="{esc(pre)}app.js?v={self.js_verzia}" defer></script>')
        if ld:
            h.append(f'<script type="application/ld+json">{ld}</script>')
        h += ['</head>', f'<body{f" class={chr(34)}{trieda}{chr(34)}" if trieda else ""}>',
              '<a class="preskocit" href="#obsah">Preskočiť na obsah</a>', CIARY_HTML,
              self.menu(pre, aktivne) + f'<main id="obsah">\n{telo}\n</main>\n' + self.pata(pre, pata_firma)
              + self.lista() + '</body>', '</html>', '']
        subor = (cesta + 'index.html') if cesta is not None else '404.html'
        self.subory[subor] = '\n'.join(h).encode('utf-8')
        if do_mapy and cesta is not None:
            self.mapa_stranok.append((cesta, platny_datum(lastmod) or self.najnovsi))

    # --- časti
    def znak_karty(self, pre):
        """Karta bez jedinej použiteľnej fotky (len novinky, ktorých obrázky sú cudzia grafika alebo osobné údaje,
        obsah-zmeny.md): logo firmy v ráme 4 : 5, nie cudzia fotka inej stavby (bola by zavádzajúca)."""
        return (f'<img class="karta-znak" src="{esc(pre)}logo.svg" width="160" height="160" alt="" loading="lazy" '
                'decoding="async">')

    @staticmethod
    def foto_karty(r):
        """Fotka svetlej karty realizácie (mriežka, služby): karta_len z nastavenia karta_foto s "detail": false,
        inak fotka karty (tá je aj titulnou fotkou detailu a kartou pásu)."""
        return r.get('karta_len') or r.get('karta')

    @staticmethod
    def mala_karta(f):
        """Výrez 4 : 5 zdroja je menší než KARTA_MIN_OREZ: na karte mriežky by bola fotka mäkká (brána bmb-presnost)."""
        return bool(f) and orez_rozmer(f['w'], f['h'], OREZY['45'])[0] < KARTA_MIN_OREZ

    def img_karty(self, pre, r, f, alt, lcp=False):
        """Fotka svetlej karty: výrez 4 : 5 cez celý rám, pri malom zdroji (mala_karta) celá fotka 4 : 3 (na výšku
        3 : 4) v strede rámu na podklade, najviac KARTA_CELA_MAX CSS px a zväčšenie do KARTA_CELA_ZVACSENIE (trieda cap-N).
        Rám karty ostáva 4 : 5, takže karty v riadku majú rovnakú výšku a polohu kóty, nadpisu a údajov."""
        if not self.mala_karta(f):
            return self.img(pre, r, f, SIZES_KARTA, lcp=lcp, alt=alt, orez='45')
        kod = '43' if f['w'] >= f['h'] else '34'
        vw, _ = orez_rozmer(f['w'], f['h'], OREZY[kod])
        cap = max(KARTA_CELA_MIN, min(KARTA_CELA_MAX, int(vw * KARTA_CELA_ZVACSENIE / 2) // 8 * 8))
        trieda = 'foto-cela' + (' na-vysku' if kod == '34' else '') + (f' cap-{cap}' if cap < KARTA_CELA_MAX else '')
        return self.img(pre, r, f, sizes_karty_cela(cap), lcp=lcp, alt=alt, orez=kod, trieda=trieda)

    def dlazdica(self, pre, r, nadpis='h3', data=False, poradie=0, lcp=False):
        """Svetlá karta realizácie v mriežke (karta pásu ukážky na svetlom podklade, PLAN-1-1.md časť 5): fotka 4 : 5
        (výrez zo stredu fotky karty: obal, inak prvá ostrá skutočná, vizualizácia len pri obal_vizualizacia so
        štítkom), kóta s rokmi stavby alebo rokom príspevku, nadpis s odkazom, druh a miesto, rola. data: atribúty
        pre filter a zoradenie v app.js (pevné kľúče a čísla)."""
        url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
        f = self.foto_karty(r)
        nm = self.nazov_s_miestom(r)
        stitok = ''
        if f:
            # lcp: prvá karta prehľadu je LCP, bez loading=lazy a s fetchpriority=high (Lighthouse lcp-discovery, kolo 1)
            foto = self.img_karty(pre, r, f, self.alt_bez_cisla(f) or nm, lcp=lcp)
            if f.get('viz'):
                stitok = '<span class="stitok-viz">Vizualizácia</span>'
        else:
            # realizácia bez jedinej ostrej fotky (skutočný obsah taký nemá, test_kazda_karta_ma_fotku)
            foto = self.znak_karty(pre)
        roky = self.roky_stitok(r)
        # zoznam realizácií (data): kóta bez pretáčania číslic, kratšie HTML pri desiatkach kariet
        kota = self.kota(roky, staticka=data) if roky else '<span class="kota kota-prazdna" aria-hidden="true"></span>'
        udaje = BODKA.join(T(x) for x in (self.druh_karty(r), r['miesto']) if x)
        atributy = ''
        if data:
            atributy = (f' data-druh="{" ".join(r["kategorie"])}" data-rok="{self.rok_filtra(r)}" '
                        f'data-mesto="{esc(kluc_mesta(self.mesto(r)))}" data-poradie="{int(poradie)}"')
        return (f'<li class="dlazdica projekt projekt-svetly{"" if f else " bez-fotky"}"{atributy}><figure>'
                f'<a class="projekt-foto" href="{url}" tabindex="-1" aria-hidden="true">{foto}{stitok}</a>'
                f'<figcaption>{kota}<{nadpis} class="dlazdica-nazov"><a href="{url}">{T(r["nazov"])}</a></{nadpis}>'
                + (f'<p class="projekt-udaje">{udaje}</p>' if udaje else '')
                + (f'<p class="projekt-text">{T(r["rola"])}</p>' if r['rola'] else '') + '</figcaption></figure></li>')

    def obrazok_dokladu(self, o):
        """Zakrytý obrázok osvedčenia SKSI pre stránku Oprávnenia (zadanie bmb-fb-uplnost, Navyše; FAKTY-WEB.md časť
        Oprávnenia: osvedčenia SKSI). Zdroj len zo šablóny: nastavenia.json opravnenia_obrazky (evidenčné číslo ->
        súbor v sablona/opravnenia/, kópia zakrytej verzie z ops/weby-na-mieru/klienti/bmbuilding/opravnenia/;
        originály s rodným číslom nikdy). Výstup opravnenia/<meno>-<šírka>.webp zmenšením, prevodom formátu alebo
        zväčšením najviac 2-krát Lanczosom s jemným doostrením (bez AI, linka bmb-presnost). None, keď obrázok nie
        je určený alebo chýba."""
        mapa = self.nastavenia.get('opravnenia_obrazky')
        rel = mapa.get(o['cislo']) if isinstance(mapa, dict) and o['cislo'] else None
        if not rel:
            return None
        rel = str(rel).replace('\\', '/')
        if not re.match(r'^opravnenia/[a-z0-9-]+\.(png|jpg|jpeg|webp)$', rel):
            varuj(f'nastavenia opravnenia_obrazky: neplatná cesta {rel!r}, obrázok vynechaný')
            return None
        p = SABLONA / rel
        if not p.is_file():
            varuj(f'sablona/{rel} chýba, osvedčenie {o["cislo"]} bez obrázka')
            return None
        w, h = sken_dokladu(p).size
        meno = Path(rel).stem
        # aj zväčšené súbory pre hustotu 2 (sirky_dokladu, doklad_webp_bajty: Lanczos a jemné doostrenie, bez AI);
        # w a h ostávajú rozmerom skenu orezaného na hranu papiera (atribúty img a data-sirka: lightbox doklad nezväčší
        # nad šírku skenu v CSS px)
        sirky = sirky_dokladu(w)
        cesty = {}
        for s in sirky:
            v = max(1, round(h * s / w))
            cesta = f'opravnenia/{meno}-{s}.webp'
            odt = odtlacok_suboru(p, VERZIA_DOKLADU, OREZ_DOKLADU.get(meno), s, v)
            self.obrazky[cesta] = (odt, (lambda p=p, s=s, v=v: doklad_webp_bajty(p, s, v)))
            cesty[s] = (cesta, v)
        return {'w': w, 'h': h, 'cesty': cesty}

    def doklady(self, pre, cinnosti=None, nadpis='h3', obrazky=False):
        out = []
        n_obr = 0

        def riadky_dokladu(o):
            return [('Vydala', o['organizacia']), ('Doklad', o['nazov']), ('Meno', o['osoba']), ('Činnosť', o['cinnost']),
                    ('Odborné zameranie', o['zameranie']), ('Evidenčné číslo', o['cislo']), ('Vydané', o['datum'])]
        vybrane = [o for o in self.firma['opravnenia'] if not (cinnosti and o['cinnost'] not in cinnosti)]
        # stránka Oprávnenia (obrazky): doklady stoja vedľa seba, tabuľky majú rovnaké riadky v rovnakom poradí, aby
        # riadky oboch tabuliek ležali v spoločných riadkoch mriežky (styl.css riadkov-N, linka bmb-presnost); riadok,
        # ktorý má aspoň jeden doklad, majú všetky, chýbajúca hodnota je „v osvedčení neuvedené“ (osvedčenie stavebného
        # dozoru 2010 odborné zameranie neuvádza, zakrytý sken sablona/opravnenia/osvedcenie-sksi-stavebny-dozor-2010.png)
        spolocne = None
        if obrazky and len(vybrane) > 1:
            spolocne = {k for o in vybrane for k, v in riadky_dokladu(o) if v}
        neuvedene = self.texty['opravnenia'].get('neuvedene', 'v osvedčení neuvedené')
        for o in vybrane:
            # nadpis = činnosť a zameranie, aby sa dva doklady SKSI dali rozlíšiť (brána dizajnu pokus 1, nález 10)
            hlavicka = ', '.join(x for x in (zaciatok_velky(o['cinnost']), o['zameranie']) if x) or o['nazov'] or 'Osvedčenie'
            if spolocne is None:
                riadky = [(k, v) for k, v in riadky_dokladu(o) if v]
            else:
                riadky = [(k, v or neuvedene) for k, v in riadky_dokladu(o) if k in spolocne]
            dl = ''.join(f'<div><dt>{T(k)}</dt><dd>{T(v)}</dd></div>' for k, v in riadky)
            trieda = f'doklad riadkov-{len(riadky)}' if obrazky and 1 <= len(riadky) <= 7 else 'doklad'
            figura = ''
            obr = self.obrazok_dokladu(o) if obrazky else None
            if obr:
                n_obr += 1
                sirky = sorted(obr['cesty'])
                srcset = ', '.join(f'{esc(pre + obr["cesty"][s][0])} {s}w' for s in sirky)
                plna = esc(pre + obr['cesty'][sirky[-1]][0])
                src = esc(pre + obr['cesty'][sirky[0]][0])
                popis = ', '.join(x for x in (o['cinnost'], o['zameranie'] and f'odborné zameranie {o["zameranie"]}',
                                              o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                                              o['datum'] and f'vydané {o["datum"]}') if x)
                alt = ', '.join(x for x in (o['nazov'] or 'Osvedčenie', o['organizacia'], o['osoba']) if x)
                alt += f': {popis}; osobné údaje zakryté'
                udaje = LB_ODDELOVAC.join(x for x in (o['nazov'], o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                                                      o['datum'] and f'vydané {o["datum"]}') if x)
                # odkaz na plný obrázok (bez JS sa otvorí priamo), app.js ho otvorí v lightboxe skupiny osvedcenia;
                # data-presne: lightbox obrázok nezväčší nad šírku súboru (doklad, nie fotka)
                figura = (f'<figure class="doklad-obr"><a class="doklad-odkaz" href="{plna}" data-lightbox data-galeria="osvedcenia" '
                          f'data-plna="{plna}" data-srcset="{srcset}" data-sirka="{obr["w"]}" data-vyska="{obr["h"]}" '
                          f'data-nazov="{esc("Osvedčenie SKSI: " + hlavicka)}" data-popis="" data-udaje="{esc(udaje)}" '
                          f'data-index="{n_obr}" data-alt="{esc(alt)}" data-presne="1">'
                          f'<img src="{src}" srcset="{srcset}" sizes="(min-width: 640px) min(528px, 45vw), calc(100vw - 32px)" '
                          f'width="{obr["w"]}" height="{obr["h"]}" alt="{esc(alt)}" decoding="async"></a>'
                          f'<figcaption class="doklad-popis">{T(self.popis_dokladu(o))}</figcaption></figure>')
            # odkaz do verejného zoznamu SKSI (Verejný portál SKSI): len pri osvedčení s obrázkom, len adresa zo šablóny
            # (nastavenia.json opravnenia_register, kľúč = evidenčné číslo) a len na hostiteľa verejnyportal.sksi.sk
            overit = ''
            reg = self.nastavenia.get('opravnenia_register')
            url = bezpecna_url(reg.get(o['cislo']), ['verejnyportal.sksi.sk']) if obrazky and isinstance(reg, dict) and o['cislo'] else ''
            if url:
                nap = self.texty['opravnenia'].get('register_overit', 'Overiť v registri SKSI')
                overit = (f'<p class="doklad-register"><a class="odkaz-von" href="{esc(url)}" rel="noopener" '
                          f'aria-label="{esc(nap + ": " + o["cinnost"])}">{T(nap)} {SVG_VON}</a></p>')
            out.append(f'<div class="{trieda}"><{nadpis} class="doklad-h">{T(hlavicka)}</{nadpis}>{figura}'
                       f'<dl class="fakty">{dl}</dl>{overit}</div>')
        return out

    @staticmethod
    def popis_dokladu(o):
        """Doslovný popis osvedčenia podľa zadania bmb-fotky (časť 3): „Osvedčenie SKSI o vykonaní ...: činnosť, odborné
        zameranie, evidenčné číslo X, vydané D“. Skladá sa len z polí obsah/firma.json, žiadne ručne písané číslo."""
        nazov = o['nazov'] or 'Osvedčenie'
        if 'SKSI' in o['organizacia'] and nazov.startswith('Osvedčenie ') and 'SKSI' not in nazov:
            nazov = 'Osvedčenie SKSI ' + nazov[len('Osvedčenie '):]
        casti = [x for x in (o['cinnost'], o['zameranie'], o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                             o['datum'] and f'vydané {o["datum"]}') if x]
        return nazov + (': ' + ', '.join(casti) if casti else '')

    def otazky_html(self, kluce, h='h3'):
        z = self.texty['zakon']
        out = []
        for k in kluce:
            q = self.texty['otazky'].get(k)
            if not q:
                continue
            out.append(f'<details class="otazka"><summary><{h}>{T(q["otazka"])}</{h}></summary><p>{T(q["odpoved"])}</p>'
                       f'<p class="zdroj">Zdroj: <a href="{esc(z["url"])}">{T(z["nazov"])}, {T(q["zdroj"])}</a></p></details>')
        return ''.join(out)

    def otazky_sekcia(self, kluce, ident):
        otazky = self.otazky_html(kluce)
        if not otazky:
            return ''
        return (f'<section class="sekcia otazky-sekcia" aria-labelledby="{ident}"><div class="obsah mriezka">'
                f'<h2 class="nadpis otazky-nadpis" id="{ident}">Čo hovorí stavebný zákon</h2>'
                f'<div class="otazky">{otazky}</div></div></section>')

    def realizacia_novinky(self, n):
        return next((r for r in self.realizacie if r['slug'] == n.get('realizacia')), None) if n.get('realizacia') else None

    def novinka_karta(self, pre, n, nadpis='h3'):
        """Svetlá karta novinky ako karta realizácie: fotka 4 : 5 (prvá ostrá skutočná, aj na výšku), kóta s dátumom
        príspevku, nadpis s odkazom, stavba s rokmi (pole realizacia), úryvok textu. Bez vlastnej použiteľnej fotky
        ilustračná fotka stavby so štítkom a popisom „Ilustračná fotka: …“ (ilustracna_fotka); logo len keď chýba aj tá."""
        url = esc(self.rel(pre, f'novinky/{n["slug"]}/'))
        f = n['karta']
        stitok = ilustr = ''
        if f:
            foto = self.img_karty(pre, n, f, self.alt_bez_cisla(f) or n['nadpis'])
            if f.get('viz'):
                stitok = '<span class="stitok-viz">Vizualizácia</span>'
        elif n.get('ilustracna_foto'):
            f = n['ilustracna_foto']
            foto = self.img_karty(pre, n['ilustracna_r'], f, n['ilustracna'])
            stitok = '<span class="stitok-viz stitok-ilustr">Ilustračná fotka</span>'
            ilustr = f'<p class="projekt-ilustr">{T(n["ilustracna"])}</p>'
        else:
            foto = self.znak_karty(pre)
        ukazka = skrat(re.sub(r'\s+', ' ', n['text']), 160) if n['text'] else ''
        if ukazka and ukazka.strip() == n['nadpis'].strip():
            ukazka = ''
        r = self.realizacia_novinky(n)
        stavba = ''
        if r:
            stavba = BODKA.join(T(x) for x in (r['nazov'], self.roky_stitok(r)) if x)
        return (f'<li class="novinka-karta projekt projekt-svetly{"" if f else " karta-bez-fotky"}"><figure>'
                f'<a class="projekt-foto{"" if f else " projekt-znak"}" href="{url}" tabindex="-1" aria-hidden="true">{foto}{stitok}</a>'
                f'<figcaption>{self.kota(datum_text(n["datum"]))}<{nadpis} class="novinka-nazov"><a href="{url}">{T(n["nadpis"])}</a></{nadpis}>'
                + ilustr + (f'<p class="projekt-udaje">{stavba}</p>' if stavba else '')
                + (f'<p class="projekt-text">{T(ukazka)}</p>' if ukazka else '') + '</figcaption></figure></li>')

    # --- úvod
    def hrdina_foto(self):
        """Fotka v ráme úvodu (nastavenia uvod_foto: slug a súbor alebo číslo fotky). Musí byť veľká a ostrá
        (FOTKY.md bod 3: šírka aspoň 1280 px, ostrosť aspoň 800); inak prvá taká fotka z realizácií; bez nej nič.
        Nastavená realizácia alebo fotka, ktorá chýba alebo nevyhovuje, je prísne varovanie (v Actions chyba zostavy)."""
        nast = self.nastavenia.get('uvod_foto') or {}
        r = next((x for x in self.realizacie if x['slug'] == nast.get('slug')), None)
        if r:
            subor = jeden_riadok(nast.get('subor'))
            nn = str(nast.get('fotka') or '').zfill(2)
            f = next((x for x in r['galeria'] if (subor and x['rel'] == f'{r["slug"]}/{subor}') or (not subor and x['nn'] == nn)), None)
            p = self.pravidla['velka']
            if f and f['w'] >= p['min_sirka'] and f['ostrost'] >= p['min_ostrost'] and f['w'] >= f['h']:
                return r, f
            varuj_prisne(f'uvod_foto: fotka {subor or nn} realizácie {r["slug"]} chýba alebo nie je dosť veľká a ostrá na úvod')
        elif nast.get('slug'):
            varuj_prisne(f'uvod_foto: realizácia {nast.get("slug")!r} nie je v obsahu')
        for x in self.realizacie:
            if x['velka']:
                return x, x['velka']
        return None, None

    def mierka(self, pre):
        """Časová mierka (grafická mierka z ukážky preložená na roky): os od najstaršieho po najnovší rok realizácie,
        dielik na každý rok, výška dielika podľa počtu realizácií ukončených v tom roku. Len realizácie so známymi
        rokmi (rok z dátumu príspevku nie je rok stavby). Rastie s obsahom, nič nie je natvrdo."""
        so_rokmi = [r for r in self.realizacie if r['rok'] and not r['rok_z_datumu']]
        if len(so_rokmi) < 2:
            return ''
        od, do = min(r['rok_od'] for r in so_rokmi), max(r['rok'] for r in so_rokmi)
        if do - od < 2:
            return ''
        pocty = {}
        for r in so_rokmi:
            pocty[r['rok']] = pocty.get(r['rok'], 0) + 1
        mx = max(pocty.values())
        popisy = {od, do} | {y for y in range(od, do + 1) if y % 10 == 0 and y - od >= 3 and do - y >= 3}
        bunky = []
        for y in range(od, do + 1):
            n = pocty.get(y, 0)
            v = 0 if not n else max(1, min(4, round(n / mx * 4)))
            bunky.append(f'<span class="m-rok v{v}">' + (f'<b>{y}</b>' if y in popisy else '') + '</span>')
        # na zozname realizácií (Stavba 2) je mierka pod filtrom ako popis, nie odkaz na tú istú stránku
        return (f'<div class="casova-mierka"><span class="mierka-os" aria-hidden="true">{"".join(bunky)}</span>'
                f'<span class="mierka-pozn">{T(f"Realizácie so známymi rokmi podľa roku ukončenia, {od} až {do}. Modrý dielik: počet stavieb v tom roku.")}</span></div>')

    # --- úvod v časti ukážky Olovnica (PLAN-1-1.md časť 1): úvod, pás prác, Postup, Činnosť, O firme, Kontakt
    def foto_nastavenia(self, kluc):
        """Fotka sekcie z nastavenia.json (slug a súbor), len skutočná ostrá fotka z galérie; inak (None, None).
        Nenastavený kľúč je sekcia bez fotky; nastavená realizácia alebo fotka, ktorá chýba, je prísne varovanie
        (v Actions chyba zostavy, fb-ostro A4)."""
        nast = self.nastavenia.get(kluc) or {}
        r = next((x for x in self.realizacie if x['slug'] == nast.get('slug')), None)
        if not r:
            if nast.get('slug'):
                varuj_prisne(f'{kluc}: realizácia {nast.get("slug")!r} nie je v obsahu')
            return None, None
        subor = jeden_riadok(nast.get('subor'))
        f = next((x for x in r['galeria'] if x['rel'] == f'{r["slug"]}/{subor}' and not x.get('viz')), None)
        if f is None:
            varuj_prisne(f'{kluc}: fotka {subor} realizácie {r["slug"]} chýba, je rozmazaná alebo je vizualizácia')
            return None, None
        return r, f

    @staticmethod
    def alt_bez_cisla(f):
        return re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')

    def druh_karty(self, r):
        """Druh stavby na karte (jednotné číslo) z vlastného názvu, potom z textu realizácie; nič nevymýšľa."""
        for zdroj in (r['nazov'], r['text']):
            for nazov, vzor in DRUHY_KARTY:
                if re.search(vzor, zdroj, re.I):
                    return nazov
        return ''

    def lb_data(self, pre, r, f, i, n, nazov, popis_udaje, skupina='', nahlad=False):
        """Odkaz na plnú fotku a jeho dáta pre lightbox (zmluva PRIPRAVA-BUILD.md bod 3, rozšírená o viac galérií
        na stránke: data-galeria, data-alt; vizualizácia data-viz a data-autor). nahlad: odkaz obsahuje náhľad
        self.img celej fotky (galéria, nie malá fotka), ktorého srcset WebP a zdroj AVIF sú bajtovo rovnaké ako
        data-srcset a data-avif; tie sa vtedy vynechajú a app.js ich vezme z náhľadu (overenie zostavy bmb-presnost,
        menšie HTML detailu s galériou, meranie v ops/weby-na-mieru/klienti/bmbuilding/presnost/overenie-zostava.md)."""
        cesty = self.obrazok_cesty(r, f)
        plna = esc(pre + cesty[f['plna_w']])
        alt = self.alt_bez_cisla(f)
        alt = f'{alt}, fotka {i} z {n}' if alt and n > 1 else alt
        # data-avif: srcset AVIF pre zdroj type=image/avif v lightboxe (app.js), WebP ostáva v data-srcset
        avif = self.srcset(pre, r, f, 'avif')
        data = {'data-plna': plna}
        if not nahlad:
            data['data-srcset'] = self.srcset(pre, r, f)
            if avif:
                data['data-avif'] = avif
        # data-sirka a data-vyska: plný súbor WebP (odkaz), lightbox fotku nezväčší nad jeho 1,25 násobok
        data.update({'data-sirka': str(f['plna_w']),
                'data-vyska': str(f['plna_h']), 'data-nazov': esc(nazov),
                'data-popis': '' if f.get('viz') else esc(self.popis_fotky(r, f)),
                'data-udaje': esc(popis_udaje), 'data-index': str(i), 'data-alt': esc(alt)})
        if skupina:
            data['data-galeria'] = esc(skupina)
        if f.get('viz'):
            data['data-viz'] = '1'
            data['data-autor'] = esc(f.get('autor', ''))
        return plna, alt, ' '.join(f'{k}="{v}"' for k, v in data.items())

    @staticmethod
    def udaje_lightboxu(r):
        """Údaje stavby pod fotkou v lightboxe, časti oddelené LB_ODDELOVAC: rola BM Building (malým písmenom uprostred
        vety), projektant, roky. app.js pri vizualizácii vynechá projektanta, ak je to ten istý človek ako autor
        (overenie kolo 1: „autor X, … projektant X“ dvakrát a „Stavebný dozor“ s veľkým písmenom uprostred vety)."""
        return LB_ODDELOVAC.join(x for x in (f'rola BM Building: {male(r["rola"])}' if r['rola'] else '',
                                             f'projektant {r["projektant"]}' if r['projektant'] else '',
                                             f'autor {r["autor"]}' if r.get('autor') else '', r['roky']) if x)

    def lightbox_dialog(self, pre, nazov, r, f):
        """Jeden dialog#lightbox na stránku (úvod: všetky galérie pásu, realizácia a novinka: jej galéria)."""
        return self.lightbox_html(f'Fotky: {nazov}', pre + self.obrazok_cesty(r, f)[f['plna_w']], f['max_w'], f['max_h'])

    @staticmethod
    def lightbox_html(nazov, src, w, h, slovo=''):
        """Markup dialog#lightbox (zmluva app.js časť 6) s prvým obrázkom src w x h; nazov ide do aria-label.
        slovo (doklady na stránke Oprávnenia): počítadlo „<slovo> 1 z 2“ a aria-label bez predpony Fotky."""
        navyse = f' data-predpona="" data-slovo="{esc(slovo)}"' if slovo else ''
        return (f'<dialog class="lightbox" id="lightbox" aria-label="{esc(nazov)}"{navyse}>'
                f'<figure class="lightbox-figura"><img class="lightbox-foto" src="{esc(src)}" '
                f'width="{w}" height="{h}" alt="" loading="lazy" decoding="async">'
                '<span class="lightbox-stitok" hidden>Vizualizácia</span>'
                '<figcaption class="lightbox-popis" aria-live="polite"><span class="lightbox-pocet"></span><b class="lightbox-nazov"></b>'
                '<span class="lightbox-udaje"></span></figcaption></figure>'
                f'<button class="lightbox-tl lightbox-predosla" type="button" data-predosla>{SVG_SIPKA_SPAT}<span>Predošlá</span></button>'
                f'<button class="lightbox-tl lightbox-dalsia" type="button" data-dalsia><span>Ďalšia</span>{SVG_SIPKA}</button>'
                f'<button class="lightbox-tl lightbox-zavriet" type="button" data-zavriet>{SVG_KRIZIK}<span>Zavrieť</span></button></dialog>')

    def zalozny_pas(self):
        """Záložné poradie pre pás (bez pas_vyber alebo na doplnenie chýbajúcich): len s fotkou karty, najprv so
        skutočnou fotkou (nie len vizualizácia), so známymi rokmi stavby a s najviac fotkami v galérii; pri zhode
        novšia podľa zoznamu realizácií. Nič sa nevymýšľa, len sa vyberá z obsahu."""
        poradie = {r['slug']: i for i, r in enumerate(self.realizacie)}
        return sorted((r for r in self.realizacie if r['karta']),
                      key=lambda r: (bool(r['karta'].get('viz')), not (r['roky'] and not r['rok_z_datumu']),
                                     -len(r['galeria']), poradie[r['slug']]))

    def pas_vyber(self):
        """Realizácie v páse úvodu (zadanie bmb-fb-uplnost bod 4: najlepšie stavby, nikdy všetky): nastavenia.json
        pas_vyber v poradí zoznamu realizácií (podľa rokov), najviac PAS_MAX. Chýbajúci slug alebo realizácia bez
        fotky sa vynechá s varovaním a miesto doplní záložný výber (zalozny_pas); bez pas_vyber záložný výber
        uvod_pocet_realizacii (tiež najviac PAS_MAX) s varovaním. Lokálne nikdy chyba zostavy; chýbajúci slug
        a realizácia bez fotky sú prísne varovania, v GitHub Actions chyba zostavy (fb-ostro A4)."""
        n = max(1, min(int(self.nastavenia.get('uvod_pocet_realizacii', 8)), PAS_MAX))
        vyber = self.nastavenia.get('pas_vyber')
        podla = {r['slug']: r for r in self.realizacie}
        poradie = {r['slug']: i for i, r in enumerate(self.realizacie)}
        out = []
        if isinstance(vyber, list) and vyber:
            ciel = min(len(vyber), PAS_MAX)
            for s in vyber:
                r = podla.get(s) if isinstance(s, str) else None
                if r is None:
                    varuj_prisne(f'nastavenia pas_vyber: realizácia {s!r} nie je v obsahu, vynechaná')
                elif not r['karta']:
                    varuj_prisne(f'nastavenia pas_vyber: realizácia {s} nemá fotku, vynechaná')
                elif r not in out:
                    out.append(r)
            if len(out) > PAS_MAX:
                varuj(f'nastavenia pas_vyber: {len(out)} realizácií, pás ukáže prvých {PAS_MAX} zo zoznamu')
                out = out[:PAS_MAX]
            if len(out) < ciel:
                doplnok = [r for r in self.zalozny_pas() if r not in out][:ciel - len(out)]
                if doplnok:
                    varuj('nastavenia pas_vyber: doplnené záložným výberom ' + ', '.join(r['slug'] for r in doplnok))
                out += doplnok
        else:
            if self.realizacie:
                varuj(f'nastavenia pas_vyber chýba alebo nie je zoznam: záložný výber {n} realizácií (najviac fotiek, známe roky)')
            out = self.zalozny_pas()[:n]
        return sorted(out, key=lambda r: poradie[r['slug']])

    def karta_pasu(self, pre, r):
        """Karta pásu z ukážky (U:817 až U:843): fotka 4 : 5 (výrez zo stredu), kóta s rokmi, nadpis s odkazom na
        stránku realizácie, tlačidlo Fotky (odkaz na galériu, app.js otvorí lightbox), druh a miesto, rola. Za kartou
        skrytý zoznam fotiek galérie pre lightbox (bez obrázkov, len odkazy s dátami)."""
        f = r['karta']
        nm = self.nazov_s_miestom(r)
        url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
        stitok = '<span class="stitok-viz">Vizualizácia</span>' if f.get('viz') else ''
        # fotka karty ako v ukážke (U:820 a U:821): skutočný zdroj v data-src a data-srcset, kým sa k pásu človek
        # nepriblíži (app.js), inak by lenivé fotky ôsmich kariet na mobile súperili s fotkou úvodu o LCP; v src je
        # spoločný prázdny obrázok 4 : 5 (prazdna-45.webp, CSP nedovolí data:), bez skriptu kópia v noscript
        # AVIF v data-avif: app.js obalí fotku do picture so zdrojom type=image/avif až pri načítaní; bez skriptu
        # kópia v noscript len ako WebP (img), lebo picture so zdrojom by sa načítal hneď aj s JS
        alt = self.alt_bez_cisla(f) or nm
        realna = self.img(pre, r, f, SIZES_PAS, alt=alt, orez='45')
        realna = realna[realna.index('<img'):].replace('</picture>', '')
        foto = self.img(pre, r, f, SIZES_PAS, alt=alt, orez='45', odlozene=True) + f'<noscript>{realna}</noscript>'
        roky = self.roky_stitok(r)
        kota = self.kota(roky) if roky else '<span class="kota kota-prazdna" aria-hidden="true"></span>'
        n = len(r['galeria'])
        tlacidlo = ''
        polozky = []
        udaje_lb = ''
        if n:
            tlacidlo = (f'<a class="prepinac" href="{url}#galeria" data-galeria="{esc(r["slug"])}" aria-haspopup="dialog" '
                        f'aria-label="Fotky: {esc(nm)}, {tvar(n, "fotka", "fotky", "fotiek")}">{SVG_FOTKY}Fotky</a>')
            udaje_lb = self.udaje_lightboxu(r)
            # skrátená zmluva pre skryté zoznamy (HTML úvodu, výkon na mobile): plná fotka v href, šírky v data-sirky
            # (srcset zloží app.js z href), názov a údaje na zozname, alt z názvu a poradia; vizualizácia má vlastný alt
            # zoznam je skrytý a bez JS sa nepoužije (tlačidlo Fotky vedie na galériu), preto odkazy bez href: cestu
            # k fotke zloží app.js z data-cesta zoznamu, čísla fotky a šírok (cesty registruje registruj_fotku)
            for i, g in enumerate(r['galeria'], 1):
                self.registruj_fotku(r, g)
                navyse = ''
                if g.get('viz'):
                    alt = self.alt_bez_cisla(g)
                    navyse = (f' data-viz="1" data-autor="{esc(g.get("autor", ""))}"'
                              f' data-alt="{esc(f"{alt}, fotka {i} z {n}" if n > 1 else alt)}"')
                # href na plnú fotku (Lighthouse SEO crawlable-anchors, overenie kolo 1); app.js ju berie ako plnú fotku
                plna = esc(pre + self.obrazok_cesty(r, g)[g['plna_w']])
                # data-sirky: šírky WebP, data-sirky-avif: šírky AVIF (app.js zloží <nn>-<šírka>.avif), data-sirka
                # výška plného WebP (lightbox fotku nezväčší nad 1,25 násobok jeho šírky)
                av = self.sirky_variantu(g, 'avif')
                navyse += f' data-sirky-avif="{" ".join(str(s) for s in av)}"' if av else ''
                polozky.append(f'<li><a href="{plna}" data-lightbox data-nn="{g["nn"]}" data-sirky="{" ".join(str(s) for s in g["sirky"])}" '
                               f'data-vyska="{g["plna_h"]}"{navyse}></a></li>')
        udaje = BODKA.join(T(x) for x in (self.druh_karty(r), r['miesto']) if x)
        return (f'<li class="projekt"><figure><div class="projekt-foto">{foto}{stitok}</div><figcaption>{kota}'
                f'<div class="projekt-riadok"><h3><a href="{url}">{T(r["nazov"])}</a></h3>{tlacidlo}</div>'
                + (f'<p class="projekt-udaje">{udaje}</p>' if udaje else '')
                + (f'<p class="projekt-text">{T(r["rola"])}</p>' if r['rola'] else '')
                + '</figcaption></figure>'
                + (f'<ul class="lb-zoznam" data-galeria="{esc(r["slug"])}" data-cesta="{esc(pre + r["obr_dir"])}/" '
                   f'data-nazov="{esc(nm)}" data-udaje="{esc(udaje_lb)}" hidden>'
                   f'{"".join(polozky)}</ul>' if polozky else '')
                + '</li>')

    def pas_prac(self, pre):
        """Tmavý pripnutý vodorovný pás prác z ukážky (U:801 až U:951, PLAN-1-1.md 1.4 a 2.4): na počítači sa pás
        pripne a zvislý posun ho posúva do strany (CSS animation-timeline, app.js zapne triedu pripnute), na mobile
        a tablete je to natívny posuvník so scroll-snap, pri obmedzenom pohybe obyčajný zoznam (mriežka)."""
        tp = self.texty['pas']
        vyber = self.pas_vyber()
        if not vyber:
            return ''
        so_rokmi = [r for r in vyber if r['roky'] and r['rok'] and not r['rok_z_datumu']]
        pocet = POCET_STAVIEB.get(len(vyber), f'{len(vyber)} stavieb')
        if so_rokmi:
            od, do = min(r['rok_od'] for r in so_rokmi), max(r['rok'] for r in so_rokmi)
            prva = f'{pocet} z rokov {od} až {do}.' if od != do else f'{pocet} z roku {od}.'
        else:
            prva = f'{pocet}.'
        uvodny = f'{prva} {tp["uvodny"]}'
        karty = [self.karta_pasu(pre, r) for r in vyber]
        n_vsetky = len(self.realizacie)
        dalsich = n_vsetky - len(vyber)
        if dalsich == 1:
            veta = 'Ďalšia 1 stavba je v zozname realizácií.'
        elif 2 <= dalsich <= 4:
            veta = f'Ďalšie {dalsich} stavby sú v zozname realizácií.'
        elif dalsich > 4:
            veta = f'Ďalších {dalsich} stavieb je v zozname realizácií.'
        else:
            veta = 'Všetky stavby sú aj v zozname realizácií s filtrom.'
        karty.append(f'<li class="projekt-dalsi"><p>{T(veta)}</p>'
                     f'<a class="tlacidlo" href="{esc(self.rel(pre, "realizacie/"))}">Všetky realizácie ({n_vsetky}) {SVG_SIPKA}</a></li>')
        prvy = next((r for r in vyber if r['galeria']), vyber[0])
        lb = self.lightbox_dialog(pre, self.nazov_s_miestom(prvy), prvy, (prvy['galeria'] or [prvy['karta']])[0])
        return (f'<section class="prace" id="prace" aria-labelledby="prace-nadpis">{CIARY_HTML}'
                f'<a class="preskocit preskocit-pas" href="#postup">{T(tp["preskocit"])}</a>'
                '<div class="prace-lepidlo"><div class="prace-posuvne"><div class="prace-hlava">'
                f'<h2 class="nadpis" id="prace-nadpis">{T(tp["nadpis"])}</h2><p class="uvodny">{T(uvodny)}</p>'
                f'<div class="pas-ovladanie"><p class="navod"><span class="navod-mobil">{T(tp["navod_mobil"])}</span>'
                f'<span class="navod-mys">{T(tp["navod_mys"])}</span><span class="navod-pocitac">{T(tp["navod_pocitac"])}</span></p>'
                '<div class="sipky" role="group" aria-label="Posun pásu realizácií" hidden>'
                f'<button class="sipka" type="button" data-smer="-1" aria-controls="pas-prac">{SVG_SIPKA_SPAT}Predošlá</button>'
                f'<button class="sipka" type="button" data-smer="1" aria-controls="pas-prac">Ďalšia{SVG_SIPKA}</button></div></div></div>'
                f'<ul class="pas" id="pas-prac" role="list" aria-label="{esc(tp["nadpis"])}" tabindex="0">{"".join(karty)}</ul>'
                f'</div></div>{lb}</section>')

    # --- výkres v úvode: schéma rezu bytovým domom (PLAN-1-1.md časť 3), vrstvy kreslené cez clip-path
    def pocet_cinnosti_dozoru(self):
        s = self.firma['sluzby'].get('stavebny-dozor') if isinstance(self.firma.get('sluzby'), dict) else None
        return len(s['cinnosti']) if s and s.get('cinnosti') else 0

    def schema_svg(self, siroka):
        """Vrstvy schémy (HTML obal s vlastným SVG, rovnaký viewBox) pre širokú (od 768 px) alebo úzku kompozíciu
        (mobil). Jednotky sú len jednotky výkresu, nič sa nevypisuje ako rozmer."""
        ts = self.texty['schema']
        W = 1200 if siroka else 560
        L, R = (-150, 1330) if siroka else (-200, 690)
        vb = f'{L} -30 {R - L} 910'
        s = 's' if siroka else 'u'
        NP = (240, 360, 480, 600)
        obd = [(-20, 720, W + 20, 760)]

        def stena(x0, x1, y0, y1, otvory):
            y = y0
            for a, b in sorted(otvory):
                if a > y:
                    obd.append((x0, y, x1, a))
                y = max(y, b)
            if y < y1:
                obd.append((x0, y, x1, y1))
        okna = [(y - 85, y - 30) for y in NP]
        stena(0, 30, 90, 720, okna)
        stena(W - 30, W, 90, 720, okna)
        if siroka:
            stena(585, 615, 140, 720, [(y - 70, y) for y in NP + (720,)])
        for y in (120,) + NP:
            if siroka and y != 120:
                obd += [(0, y, 900, y + 20), (1160, y, W, y + 20)]
            else:
                obd.append((0, y, W, y + 20))
        obrys = svg_obrys(obd)
        teren_x = -72 if siroka else -102
        zem = svg_obrys([(L + 10, 600, 0, 720), (L + 10, 720, -20, 760)]) + svg_obrys([(W, 600, R - 10, 720), (W + 20, 720, R - 10, 760)])
        teren = f'M{L + 10} 600H{teren_x}M{W} 600H{R - 10}'
        okna_d = ''.join(f'M{x} {y - 85}V{y - 30}' for y in NP for x in (0, 12, 16, 30, W - 30, W - 16, W - 12, W))
        schody = ''
        if siroka:
            for y in (720, 600, 480, 360):
                schody += f'M900 {y}' + ''.join(f'V{y - 15 * (k + 1)}H{cislo_svg(900 + 32.5 * (k + 1))}' for k in range(8))
                schody += f'M932.5 {y}L1160 {y - 105}'
        krok = 140 if siroka else 120
        stlpiky = [x for x in range(40, W - 39, krok)]
        schody += ''.join(f'M{x} 120V80' for x in stlpiky) + f'M{stlpiky[0]} 80H{stlpiky[-1]}'
        K = W + 70
        urovne = (120, 240, 360, 480, 600, 720)
        koty = ''.join(f'M{W + 15} {y}H{K + 12}' for y in urovne) + f'M{K} 120V720'
        znacky = ''.join(f'M{K - 9} {y + 9}L{K + 9} {y - 9}' for y in urovne)
        riadok2 = 860 if siroka else 830
        texty_kot = []
        # úrovne zdola: 600 až 720 je 1. PP, 480 až 600 1. NP, ... 120 až 240 4. NP
        useky = [(600, 720), (480, 600), (360, 480), (240, 360), (120, 240)]
        for (a, b), nazov in zip(useky, ts['urovne']):
            c = (a + b) // 2
            texty_kot.append(f'<text x="{K - 12}" y="{c}" transform="rotate(-90 {K - 12} {c})">{T(nazov)}</text>')
        if siroka:
            body = (0, 300, 900, W)
            koty += ''.join(f'M{x} 775V815' for x in body) + f'M0 815V870M{W} 815V870M0 805H{W}M0 {riadok2}H{W}'
            znacky += ''.join(f'M{x - 9} 814L{x + 9} 796' for x in body) + f'M-9 {riadok2 + 9}L9 {riadok2 - 9}M{W - 9} {riadok2 + 9}L{W + 9} {riadok2 - 9}'
            for (a, b), faza in zip(((0, 300), (300, 900), (900, W)), ts['fazy']):
                texty_kot.append(f'<text class="kh" x="{(a + b) // 2}" y="793">{T(faza)}</text>')
        else:
            koty += f'M0 775V840M{W} 775V840M0 {riadok2}H{W}'
            znacky += f'M-9 {riadok2 + 9}L9 {riadok2 - 9}M{W - 9} {riadok2 + 9}L{W + 9} {riadok2 - 9}'
        bod_x = teren_x + 12
        bod = f'M{teren_x} 584H{teren_x + 24}L{bod_x} 600Z'
        ciarka = f'M{teren_x + 24} 600H0'
        # popisy kontrol: podlažie podľa úrovne, strecha nad doskou, vytýčenie pri výškovom bode
        px = 307 if siroka else W // 2
        miesta = {'strecha': 30, '4. NP': 176, '3. NP': 296, '2. NP': 416, '1. NP': 536, '1. PP': 656}
        popisy = []
        for k in ts['kontroly']:
            if k['kde'] == 'teren':
                x = (L + 0) // 2
                prvy, _, druhy = k['plocha'].partition(' a ')
                riadky = [prvy, 'a ' + druhy] if druhy else [k['plocha']]
                tsp = ''.join(f'<tspan class="plocha" x="{x}" dy="{"1.25em" if j == 0 else "1.15em"}">{T(t)}</tspan>'
                              for j, t in enumerate(riadky))
                popisy.append(f'<text class="popis" x="{x}" y="500">{T(k["popis"])}{tsp}</text>')
            elif k['kde'] in miesta:
                y = miesta[k['kde']]
                popisy.append(f'<text class="popis" x="{px}" y="{y}">{T(k["popis"])}'
                              f'<tspan class="plocha" x="{px}" dy="1.25em">{T(k["plocha"])}</tspan></text>')
        popisy.append(f'<text class="poznamka-schemy" x="{L + 10}" y="0">{T(ts["nie_v_mierke"])}</text>')
        # značka pohľadu A pred fasádou mimo rezu (ukážka U:781 až U:786): hrot mieri na fasádu, pod ňou popis v Mono;
        # nad značkou je v HTML odkaz na fotku (pohlad-odkaz), prechod myšou zvýrazní fotku a fotka značku (overenie kolo 1)
        ax, ay = POHLAD_ZNACKA['s' if siroka else 'u']
        if getattr(self, 'ma_pohlad', False):
            popisy.append(f'<g class="pohlad-znacka" transform="translate({ax} {ay})"><path class="pohlad-hrot" d="M14-10L32 0L14 10Z"/>'
                          f'<circle class="pohlad-kruh" r="17"/><text class="pohlad-pismeno" y="7.5">A</text></g>'
                          f'<text class="pohlad-popis" x="{ax}" y="{ay + 52}">{T(ts.get("pohlad", "pohľad A"))}</text>')

        def vrstva(trieda, obsah, defs=''):
            return (f'<div class="vrstva {trieda}"><svg viewBox="{vb}" aria-hidden="true" focusable="false">'
                    + (f'<defs>{defs}</defs>' if defs else '') + f'{obsah}</svg></div>')
        return ''.join([
            vrstva('v-teren', f'<path class="zemina" d="{zem}"/><path class="teren" d="{teren}"/>',
                   f'<pattern id="zemina-{s}" patternUnits="userSpaceOnUse" width="18" height="18" patternTransform="rotate(45)">'
                   f'<path class="zemina-ciara" d="M9-1V19"/></pattern>'),
            vrstva('v-srafa', f'<path class="srafa-plocha" d="{obrys}"/>',
                   f'<pattern id="srafa-{s}" patternUnits="userSpaceOnUse" width="9" height="9" patternTransform="rotate(45)">'
                   f'<path class="srafa-ciara" d="M4.5-1V10"/></pattern>'),
            vrstva('v-obrys', f'<path class="obrys" d="{obrys}"/>'),
            vrstva('v-okna', f'<path class="okna" d="{okna_d}"/>'),
            vrstva('v-schody', f'<path class="nabytok" d="{schody}"/>'),
            vrstva('v-bod', f'<path class="bod-ciarka" d="{ciarka}"/><path class="bod-hrot" d="{bod}"/>'),
            vrstva('v-koty', f'<path class="koty-ciary" d="{koty}"/><path class="koty-znacky" d="{znacky}"/>'
                   f'<g class="koty-text">{"".join(texty_kot)}</g>'),
            vrstva('v-popisy', f'<g class="popisy">{"".join(popisy)}</g>'),
        ])

    def schema(self, pre):
        """Animovaný technický výkres úvodu (namiesto pôdorysu ukážky): zjavne schéma rezu bytovým domom, v každom
        podlaží jedna kontrola stavebného dozoru zo starého webu, pod rezom kóta postupu a počet činností dozoru,
        ktorý sa pretočí. Dve kompozície (široká od 768 px, úzka na mobile), prepína CSS."""
        ts = self.texty['schema']
        n = self.pocet_cinnosti_dozoru()
        miesto = {'strecha': 'strecha', 'teren': 'pri výškovom bode na teréne'}
        kontroly = '; '.join(f'{miesto.get(k["kde"], k["kde"])}: {k["popis"].lower() if k["popis"] != "BOZP" else "BOZP"}, '
                             f'{k["plocha"]}' for k in ts['kontroly'])
        titul = (f'{ts["titul"]} Kontroly: {kontroly}. Pod rezom kóta postupu: {", ".join(ts["fazy"])}.'
                 + (f' Stavebný dozor má {n} činností.' if n else ''))
        kota = ''
        if n:
            cisla = ''.join(f'<span class="valec"><span class="stlpec c{ch}"></span></span>' for ch in str(n))
            kota = f'<span class="plan-kota" aria-hidden="true">{cisla}<span class="kota-slovo"> {T(ts["kota"])}</span></span>'
        odkaz = ''
        if 'stavebny-dozor' in self.texty['sluzby'] and n:
            odkaz = f' <a href="{esc(self.rel(pre, "stavebny-dozor/"))}">{T(ts["odkaz"].replace("{n}", str(n)))}</a>'
        mierka = ('<span class="mierka" aria-hidden="true"><span class="mierka-pas"></span>'
                  + ''.join(f'<span class="mierka-c mc-{p}">{T(fz)}</span>' for p, fz in zip((17, 50, 83), ts['fazy']))
                  + '</span>')
        # odkaz nad značkou A len pre myš a dotyk (ako v ukážke U:789), fotka je hneď vedľa alebo nad schémou
        odkaz_a = '<a class="pohlad-odkaz" href="#pohlad-a" tabindex="-1" aria-hidden="true"></a>' if self.ma_pohlad else ''
        return (f'<figure class="plan uvod-plan" id="schema"><span class="vh" id="schema-titul">{T(titul)}</span>'
                f'<div class="plan-ramec plan-siroky" role="img" aria-labelledby="schema-titul">{self.schema_svg(True)}{kota}{odkaz_a}</div>'
                f'<div class="plan-ramec plan-uzky" role="img" aria-labelledby="schema-titul">{self.schema_svg(False)}{kota}{odkaz_a}</div>'
                f'<figcaption><span><strong>Schéma</strong>{BODKA}{T(ts["popis"])}<br>{T(ts["popis_2"])}{odkaz}</span>'
                f'{mierka}</figcaption></figure>')

    def pohlad_a(self, pre, r, f):
        """Pohľad A z ukážky (U:726 až U:731): skutočná fotka jeho stavby (LCP), na mobile výrez 5 : 4 vyrobený
        build.py (picture), od 768 px celá fotka; popis zo značkou A, názvom, mestom a rokmi z obsahu."""
        nm = self.nazov_s_miestom(r)
        alt = self.alt_bez_cisla(f) or nm
        srcset54, _ = self.orez_srcset(pre, r, f, '54')
        avif54, _ = self.orez_srcset(pre, r, f, '54', 'avif')
        mobil = '(max-width: 767px)'
        zdroje = ((f'<source media="{mobil}" type="image/avif" srcset="{avif54}" sizes="calc(100vw - 32px)">' if avif54 else '')
                  + f'<source media="{mobil}" type="image/webp" srcset="{srcset54}" sizes="calc(100vw - 32px)">')
        obr = self.img(pre, r, f, SIZES_POHLAD, lcp=True, trieda='hrdina-foto', alt=alt, zdroje=zdroje)
        # preload len formátu, ktorý picture naozaj vyberie: AVIF (prehliadač bez AVIF typ preskočí a vezme WebP bez
        # preloadu), bez AVIF WebP; dva preloady by v prehliadači s AVIF stiahli fotku dvakrát
        avif = self.srcset(pre, r, f, 'avif')
        typ, s54, plna = ('image/avif', avif54, avif) if avif54 and avif else ('image/webp', srcset54, self.srcset(pre, r, f))
        self.preload_hrdina = [
            f'<link rel="preload" as="image" type="{typ}" imagesrcset="{s54}" imagesizes="calc(100vw - 32px)" '
            f'media="{mobil}" fetchpriority="high">',
            f'<link rel="preload" as="image" type="{typ}" imagesrcset="{plna}" imagesizes="{SIZES_POHLAD}" '
            'media="(min-width: 768px)" fetchpriority="high">']
        casti = [f'<a href="{esc(self.rel(pre, "realizacie/" + r["slug"] + "/"))}">{T(r["nazov"])}</a>']
        m = self.mesto(r)
        if m and m.lower() not in r['nazov'].lower():
            casti.append(T(m))
        if r['roky']:
            casti.append(T(r['roky']))
        return (f'<figure class="pohlad" id="pohlad-a"><div class="pohlad-ramec">'
                f'{obr}</div><figcaption>{SVG_POHLAD_ZNAK}<span><strong>{T(self.texty["uvod"].get("pohlad", "Pohľad A"))}:</strong> '
                f'{BODKA.join(casti)}</span></figcaption></figure>')

    def postup(self, pre):
        """Postup z ukážky (U:953 až U:993): nadpis a veta vľavo, fotka rozostavanej stavby vpravo, štyri fázy
        s pretáčaným poradovým číslom (BM nemá doložené trvania), texty zo starého webu a činnosť, ktorá fázu pokrýva."""
        t = self.texty['postup']
        r, f = self.foto_nastavenia('postup_foto')
        foto = ''
        if f:
            foto = (f'<figure class="postup-foto">{self.img(pre, r, f, SIZES_POSTUP, alt=self.alt_bez_cisla(f))}'
                    f'<figcaption>{T(t["foto_popis"])}</figcaption></figure>')
        return (f'<section class="sekcia postup" id="postup" aria-labelledby="postup-nadpis"><div class="obsah">'
                f'<div class="mriezka postup-hlava"><div><h2 class="nadpis" id="postup-nadpis">{T(t["nadpis"])}</h2>'
                f'<p class="uvodny">{T(t["uvodny"])}</p></div>{foto}</div>'
                f'{self.fazy_html(pre)}</div></section>')

    def fazy_html(self, pre, h='h3'):
        """Štyri fázy postupu (úvod, stránka stavebného dozoru): pretáčané poradové číslo, názov, text zo starého
        webu a činnosť, ktorá fázu pokrýva."""
        fazy = []
        for i, fz in enumerate(self.texty['postup']['fazy'], 1):
            cislo = f'{i:02d}'
            dlzka = ('<span class="faza-dlzka otoc" aria-hidden="true"><span class="valce">'
                     + ''.join(f'<span class="valec"><span class="stlpec c{ch} i{j}"></span></span>' for j, ch in enumerate(cislo))
                     + '</span></span>')
            sl = [k for k in fz.get('sluzby', []) if k in self.texty['sluzby']]
            odkazy = ' a '.join(f'<a href="{esc(self.rel(pre, k + "/"))}">{T(male(self.texty["sluzby"][k]["menu"]))}</a>' for k in sl)
            fazy.append(f'<li class="faza">{dlzka}<{h}>{T(fz["nazov"])}</{h}><p>{T(fz["text"])}</p>'
                        + (f'<p class="vystup">Činnosť: {odkazy}</p>' if odkazy else '') + '</li>')
        return f'<ol class="fazy">{"".join(fazy)}</ol>'

    def cinnost(self, pre):
        """Činnosť v komponente Služby a ceny ukážky (U:995 až U:1051), bez cien: tri služby v poradí starého webu
        so stránkou, vetou a príkladmi; v stĺpci ceny počet realizácií v tej role (z obsahu) a rok osvedčenia SKSI;
        v boxe kalkulačky otázky zo stavebného zákona so zdrojom."""
        tc = self.texty['cinnost']
        poradie = [k for k in tc.get('poradie', []) if k in self.texty['sluzby']]
        poradie += [k for k in self.texty['sluzby'] if k not in poradie]
        riadky = []
        for k in poradie:
            t = self.texty['sluzby'][k]
            n = sum(1 for r in self.realizacie if any(kl in r['rola'].lower() for kl in t['kluce']))
            dokl = [o for o in self.firma['opravnenia'] if o['cinnost'] in t.get('opravnenie', [])]
            small = ''
            if len(t.get('opravnenie', [])) == 1 and dokl and dokl[0].get('rok'):
                small = f'<small>osvedčenie SKSI z roku {dokl[0]["rok"]}</small>'
            elif n and t.get('pocet_popis'):
                # počet zlučuje roly z referencií (projektový manažment aj stavebný manažment), overenie kolo 1
                small = f'<small>{T(t["pocet_popis"])}</small>'
            cena = (f'<p class="cena"><span class="cisla">{n}</span> {T(tvar(n, "realizácia", "realizácie", "realizácií").split(" ", 1)[1])}{small}</p>'
                    if n else (f'<p class="cena">{small}</p>' if small else ''))
            opis = ' '.join(x for x in (t.get('veta', ''), t.get('priklady', '')) if x)
            # Odkaz na celý zoznam činností zo starého webu na stránke služby (Andrej 23. 11. 2026: Mário „tam chce mať
            # všetky info“; zoznamy boli len na podstránkach a z úvodu ich nebolo vidno).
            cin = (self.firma['sluzby'].get(k) or {}).get('cinnosti') or []
            viac = ''
            if cin:
                pocet = len(cin)
                text_viac = ('Celý popis činnosti' if pocet == 1 else
                             f'Všetky {tvar(pocet, "činnosť", "činnosti", "činností")}' if pocet <= 4 else
                             f'Všetkých {tvar(pocet, "činnosť", "činnosti", "činností")}')
                viac = (f'<a class="odkaz-sipka sluzba-viac" href="{esc(self.rel(pre, k + "/"))}#cinnosti-nadpis">'
                        f'{T(text_viac)} {SVG_SIPKA}</a>')
            riadky.append(f'<li class="sluzba"><h3><a href="{esc(self.rel(pre, k + "/"))}">{T(t["menu"])}</a></h3>'
                          f'<p class="sluzba-opis">{T(opis)}{viac}</p>{cena}</li>')
        dokl = [f'{o["cinnost"]}, vydané {o["datum"]}' for o in self.firma['opravnenia'] if o['cinnost'] and o['datum']]
        pozn = ''
        if dokl:
            pozn = (f'<p class="cennik-pozn">{T("Osvedčenia Slovenskej komory stavebných inžinierov: " + "; ".join(dokl) + ".")} '
                    f'<a href="{esc(self.rel(pre, "opravnenia/"))}">Oprávnenia</a></p>')
        otazky = self.otazky_html(list(self.texty['otazky']), 'h4')
        box = (f'<div class="kalkulacka zakon-box" role="group" aria-labelledby="zakon-uvod">'
               f'<h3 id="zakon-uvod">{T(tc["zakon_nadpis"])}</h3><div class="otazky">{otazky}</div></div>') if otazky else ''
        return (f'<section class="sekcia" id="sluzby" aria-labelledby="sluzby-nadpis"><div class="obsah mriezka sluzby-obal">'
                f'<div class="sluzby-hlava"><h2 class="nadpis" id="sluzby-nadpis">{T(tc["nadpis"])}</h2>'
                f'<p class="uvodny">{T(tc["uvodny"])}</p></div>'
                f'<ul class="cennik">{"".join(riadky)}</ul>{pozn}{box}</div></section>')

    def o_firme(self, pre):
        """O firme v komponente Ateliér ukážky (U:1053 až U:1073): fotka hotovej stavby vľavo, text a fakty vpravo
        (na trhu od 2005 a 20 rokov z Facebooku, konateľ, osvedčenia SKSI, pôsobnosť, sídlo), odkaz na Oprávnenia."""
        tf = self.texty['firma']
        r, f = self.foto_nastavenia('firma_foto')
        foto = ''
        if f:
            popis = self.nazov_s_miestom(r) + (f', {r["roky"]}' if r['roky'] else '')
            foto = (f'<figure class="atelier-foto">{self.img(pre, r, f, SIZES_FIRMA, alt=self.alt_bez_cisla(f))}'
                    f'<figcaption><a href="{esc(self.rel(pre, "realizacie/" + r["slug"] + "/"))}">{T(popis)}</a></figcaption></figure>')
        na_trhu = T(tf['na_trhu'])
        nov = next((n for n in self.novinky if n['slug'] == tf.get('novinka_20')), None)
        if nov:
            na_trhu = f'<a href="{esc(self.rel(pre, "novinky/" + nov["slug"] + "/"))}">{na_trhu}</a>'
        osv = ', '.join(f'{o["cinnost"]} {o["rok"]}' for o in self.firma['opravnenia'] if o['cinnost'] and o['rok'])
        fakty = [('Na trhu', na_trhu), ('Konateľ', T(self.firma['konatel'])), ('Osvedčenia SKSI', T(osv)),
                 ('Pôsobí', T(self.firma['posobnost'])), ('Sídlo', T(self.adresa()))]
        dl = ''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in fakty if v)
        return (f'<section class="sekcia" id="firma" aria-labelledby="firma-nadpis"><div class="obsah mriezka atelier-mriezka">'
                f'{foto}<div class="atelier-text"><h2 class="nadpis" id="firma-nadpis">{T(tf["nadpis"])}</h2>'
                f'<p class="uvodna">{T(tf["uvodna"])}</p>' + ''.join(f'<p class="tlmene">{T(p)}</p>' for p in tf['odseky'])
                + f'<dl class="fakty">{dl}</dl><p class="firma-odkaz"><a class="odkaz-sipka" href="{esc(self.rel(pre, "opravnenia/"))}">'
                f'Oprávnenia a osvedčenia {SVG_SIPKA}</a></p></div></div></section>')

    def uvod(self):
        t = self.texty['uvod']
        pre = ''
        r, f = self.hrdina_foto()
        self.registruj_og('og/bmbuilding.jpg', f['zdroj'] if f else None, 'Stavebný dozor',
                          'Stavbyvedúci a stavebný manažment, Bratislava a okolie', 'bmbuilding.sk')
        # meno ako v ukážke: malý riadok a veľké ľahké meno, bodku nad ı v slove Building tvorí olovnica na šnúre
        # zavesenej na vlasovej čiare (zvislica stavbyvedúceho); čítačka číta skrytý text, nie ı
        # odbor v dvoch riadkoch („Stavebno-manažérska“ a „spoločnosť“): v jednom riadku by šnúra olovnice nad ı
        # prešla cez slovo (overenie kolo 1); prvý riadok končí pred ı pri 390 aj 1440 px
        maly = [x for x in jeden_riadok(t['meno_maly']).rsplit(' ', 1) if x]
        meno = ('<div class="meno-blok"><h1 class="meno" id="meno">'
                # riadky ako bloky s medzerou medzi nimi: textContent nadpisu ostane „Stavebno-manažérska spoločnosť“
                f'<span class="meno-maly">{" ".join(f"<span>{T(x)}</span>" for x in maly)}</span> <span class="vh">BM Building</span>'
                '<span class="meno-velky" aria-hidden="true">BM Bu<span class="meno-i">ı<span class="olovnica"></span></span>lding</span>'
                '</h1></div>')
        fi = self.firma
        akcie = []
        if fi['tel_href']:
            akcie.append(f'<a class="tlacidlo" href="tel:{esc(fi["tel_href"])}">Zavolať {SVG_SIPKA}</a>')
        if self.realizacie:
            akcie.append(f'<a class="odkaz-sipka" href="#prace">Pozrieť realizácie {SVG_SIPKA_DOLE}</a>')
        if t.get('stav'):
            akcie.append(f'<p class="stav"><span class="stav-bod" aria-hidden="true"></span><span class="stav-text">{T(t["stav"])}</span></p>')
        text = (f'<div class="uvod-text"><p class="meno-popis">{T(t["popis"])}</p><p class="uvod-veta">{T(t["veta"])}</p>'
                f'<div class="uvod-akcie" data-vyzva-hore>{"".join(akcie)}</div></div>')
        foto = self.pohlad_a(pre, r, f) if f else ''
        # značka pohľadu A vo výkrese len keď je fotka pohľadu A (schema_svg, schema)
        self.ma_pohlad = bool(f)
        casti = [f'<section class="uvod{"" if f else " bez-fotky"}" aria-labelledby="meno"><div class="obsah mriezka uvod-mriezka">'
                 f'{meno}{text}{foto}{self.schema(pre)}</div></section>']
        if self.realizacie:
            casti.append(self.pas_prac(pre))
        casti.append(self.postup(pre))
        casti.append(self.cinnost(pre))
        casti.append(self.o_firme(pre))
        casti.append(self.kontakt_sekcia(pre))
        uzly = [self.ld_firma(), {'@type': 'WebSite', '@id': self.abs_url('#web'), 'url': self.abs_url(''),
                                  'name': 'BM Building', 'inLanguage': 'sk', 'publisher': {'@id': self.abs_url('#firma')}},
                {'@type': 'WebPage', '@id': self.abs_url(''), 'url': self.abs_url(''), 'name': t['title'],
                 'isPartOf': {'@id': self.abs_url('#web')}, 'about': {'@id': self.abs_url('#firma')}, 'inLanguage': 'sk'}]
        self.stranka('', t['title'], t['description'], '\n'.join(c for c in casti if c), '', uzly, trieda='stranka-uvod',
                     preload_obr=self.preload_hrdina if f else None)

    # --- podstránky
    def podstranka(self, cesta, kroky, title, description, telo, aktivne, uzly_navyse, og='og/bmbuilding.jpg',
                   og_typ='website', lastmod=None, zaver=True, zaver_hlavny=None, zaver_dalej=None, pata_firma=True,
                   typ_stranky='WebPage', stranka_navyse=None):
        uzly = [self.ld_firma(), self.ld_breadcrumb(kroky),
                {'@type': typ_stranky, '@id': self.abs_url(cesta), 'url': self.abs_url(cesta), 'name': title,
                 'inLanguage': 'sk', 'isPartOf': {'@id': self.abs_url('#web')}}]
        # jeden WebSite v grafe (uzol s @id #web je na úvode; brána pokus 2, správnosť 7)
        if stranka_navyse:
            uzly[-1].update(stranka_navyse)
        uzly += uzly_navyse
        if zaver:
            telo += '\n' + self.zaver(self.pre(cesta), zaver_hlavny, zaver_dalej)
        self.stranka(cesta, title, description, telo, aktivne, uzly, og_obr=og, og_typ=og_typ, lastmod=lastmod,
                     pata_firma=pata_firma)

    def zoznam_dalsich(self, pre, zoznam, nadpis='h3'):
        polozky = []
        for r in zoznam:
            url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
            udaje = BODKA.join([T(x) for x in (r['miesto'], r['rola']) if x] + ([self.kedy(r)] if self.kedy(r) else []))
            polozky.append(f'<li><a href="{url}">{T(r["nazov"])}</a>'
                           + (f'<span>{udaje}</span>' if udaje else '') + '</li>')
        return (f'<div class="dalsie"><{nadpis} class="dalsie-nadpis">Ďalšie realizácie</{nadpis}>'
                f'<ul class="zoznam-dalsie">{"".join(polozky)}</ul></div>')

    def stranky_sluzieb(self):
        for k, t in self.texty['sluzby'].items():
            cesta = f'{k}/'
            pre = self.pre(cesta)
            s = self.firma['sluzby'].get(k, {'nazov': t['menu'], 'popis': '', 'cinnosti': []})
            # dvojstrana ukážky (O firme): text služby vľavo, vpravo box kalkulačky s dokladom SKSI a Zavolať
            odseky = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">{T(p)}</p>' for i, p in enumerate(t['odseky']))
            cinnosti = ''
            if s['cinnosti']:
                # zoznam činností zo starého webu ako riadky cenníka ukážky: poradové číslo v Mono, text činnosti
                fazy = self.fazy_html(pre, 'h3') if k == 'stavebny-dozor' else ''
                uvod_c = f'<p class="uvodny">{T(t["cinnosti_uvod"])}</p>' if t.get('cinnosti_uvod') else ''
                cinnosti = (f'<section class="sekcia cinnosti-sekcia" aria-labelledby="cinnosti-nadpis"><div class="obsah">'
                            f'<div class="sekcia-hlava"><h2 class="nadpis" id="cinnosti-nadpis">{T(t["cinnosti_nadpis"])}'
                            f' <span class="nadpis-pocet cisla">{len(s["cinnosti"])}</span></h2>{uvod_c}</div>{fazy}<ol class="cinnosti">'
                            + ''.join(f'<li><span class="cinnost-c cisla" aria-hidden="true">{i:02d}</span><span>{T(c)}</span></li>'
                                      for i, c in enumerate(s['cinnosti'], 1)) + '</ol></div></section>')
            # doklady v boku sú h2: bok je v DOM pred prvým h2 stránky, h3 by bol skok úrovne po h1
            doklady = self.doklady(pre, t['opravnenie'], nadpis='h2')
            bok = ''
            if doklady or self.firma['tel_href']:
                bok = '<aside class="kalkulacka sluzba-bok" aria-label="Oprávnenie a kontakt">' + ''.join(doklady)
                if self.firma['tel_href']:
                    bok += (f'<p class="bok-tlacidlo"><a class="tlacidlo" href="tel:{esc(self.firma["tel_href"])}">'
                            f'Zavolať <span class="cisla">{T(self.firma["tel"])}</span></a></p>')
                bok += (f'<p class="bok-odkaz"><a class="odkaz-sipka" href="{esc(self.rel(pre, "opravnenia/"))}">'
                        f'Oprávnenia a osvedčenia {SVG_SIPKA}</a></p></aside>')
            suvisiace = [r for r in self.realizacie if any(kl in r['rola'].lower() for kl in t['kluce'])]
            real = ''
            if suvisiace:
                naraz = int(self.nastavenia.get('sluzba_pocet_kariet', 4))
                # stránka služby odkazuje na každú realizáciu s danou rolou (brána pokus 2, správnosť 2): nad počet
                # kariet ostatné ako kompaktný zoznam; karta vždy s fotkou (r['karta'])
                # karty s fotkou dosť veľkou na celý rám 4 : 5 majú prednosť (brána bmb-presnost pokus 1, nález 1:
                # Vila X 640 x 480 px na karte Stavebného manažmentu); malé ostávajú v kompaktnom zozname nižšie
                s_obalom = sorted((r for r in suvisiace if self.foto_karty(r)),
                                  key=lambda r: self.mala_karta(self.foto_karty(r)))[:naraz]
                zvysok = [r for r in suvisiace if r not in s_obalom]
                real = (f'<section class="sekcia sekcia-karty" aria-labelledby="real-nadpis"><div class="obsah">'
                        f'<div class="sekcia-hlava"><h2 class="nadpis" id="real-nadpis">{T(t["realizacie_nadpis"])}'
                        f' <span class="nadpis-pocet cisla">{len(suvisiace)}</span></h2></div>'
                        f'<ul class="karty">{"".join(self.dlazdica(pre, r) for r in s_obalom)}</ul>'
                        + (self.zoznam_dalsich(pre, zvysok) if zvysok else '')
                        + f'<p class="viac"><a class="odkaz-sipka" href="../realizacie/">Všetky realizácie {SVG_SIPKA}</a></p></div></section>')
            kroky = [('', 'Úvod'), (cesta, t['menu'])]
            hlava = self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
            telo = (hlava + f'\n<div class="obsah mriezka sluzba-obal"><div class="sluzba-hlavne atelier-text">{odseky}</div>{bok}</div>\n'
                    f'{cinnosti}\n{real}\n{self.otazky_sekcia(t["otazky"], "zakon-nadpis")}')
            sluzba = {'@type': 'Service', '@id': self.abs_url(cesta + '#sluzba'), 'name': t['h1'], 'serviceType': t['typ_sluzby'],
                      'description': t['description'], 'provider': {'@id': self.abs_url('#firma')},
                      'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'}],
                      'url': self.abs_url(cesta)}
            self.podstranka(cesta, kroky, t['title'], t['description'], telo, k, [sluzba], zaver_dalej=t.get('zaver'))

    def filtre(self, zoznam, roky_vyber=True):
        """Ovládanie filtra a zoradenia pre app.js: bez JS skryté (hidden), zoznam ukazuje všetko v poradí od
        najnovších. Druh z kategórií build.py, roky po desaťročiach podľa najvyššieho roku v roky, mesto z poľa miesto."""
        pocty = self.pocty_druhov(zoznam)
        druhy = [f'<button class="filter" type="button" data-druh="" aria-pressed="true">Všetky<span>{len(zoznam)}</span></button>']
        for k, nazov in KATEGORIE:
            if pocty[k]:
                druhy.append(f'<button class="filter" type="button" data-druh="{k}" aria-pressed="false">{T(nazov)}'
                             f'<span>{pocty[k]}</span></button>')
        vyber = ''
        if roky_vyber:
            # len skutočné roky realizácie; rok príspevku a chýbajúci rok spolu ako „rok neuvedený“ (nález 1 brány)
            dekady = {}
            for r in zoznam:
                rok = self.rok_filtra(r)
                dekady.setdefault((rok // 10) * 10 if rok else 0, []).append(rok)
            moznosti = ['<option value="">všetky</option>']
            for d in sorted(dekady, reverse=True):
                if d:
                    od, do = min(dekady[d]), max(dekady[d])
                    moznosti.append(f'<option value="{d}">{od if od == do else f"{od} až {do}"}</option>')
                else:
                    moznosti.append('<option value="0">rok neuvedený</option>')
            vyber = f'<label class="vyber">Roky <select data-roky>{"".join(moznosti)}</select></label>'
        mesta = {}
        for r in zoznam:
            m = self.mesto(r)
            if m:
                mesta[kluc_mesta(m)] = (mesta.get(kluc_mesta(m), (m, 0))[0], mesta.get(kluc_mesta(m), (m, 0))[1] + 1)
        if len(mesta) > 1:
            moznosti = ['<option value="">všetky</option>'] + [
                f'<option value="{esc(k)}">{T(m)} ({n})</option>' for k, (m, n) in sorted(mesta.items(), key=lambda x: (-x[1][1], x[1][0]))]
            vyber += f'<label class="vyber">Miesto <select data-mesto>{"".join(moznosti)}</select></label>'
        # podľa názvu: app.js triedi text nadpisu karty slovenským poradím (Intl.Collator sk)
        # krátke popisky (oprava 1 linky bmb-fb-uplnost): pri 390 px má select v trojstĺpcovom riadku asi 110 px,
        # „od najnovších“ sa orezalo na „od najnovšíc“
        vyber += ('<label class="vyber">Zoradiť <select data-zoradit><option value="najnovsie">najnovšie</option>'
                  '<option value="najstarsie">najstaršie</option><option value="nazov">abecedne</option></select></label>')
        return (f'<div class="filtre" data-filtre hidden><div class="filtre-druh" role="group" aria-label="Druh stavby">'
                f'{"".join(druhy)}</div><div class="filtre-vyber">{vyber}</div></div>')

    def naraz(self):
        """Koľko kariet zoznamu ukáže app.js naraz (nastavenia realizacie_naraz, predvolene REALIZACIE_NARAZ);
        0 = všetky naraz bez tlačidla Zobraziť ďalšie."""
        try:
            n = int(self.nastavenia.get('realizacie_naraz', REALIZACIE_NARAZ))
        except (TypeError, ValueError):
            varuj('nastavenia realizacie_naraz nie je číslo, použitých ' + str(REALIZACIE_NARAZ))
            n = REALIZACIE_NARAZ
        return max(0, n)

    def stranka_realizacii(self):
        t = self.texty['realizacie']
        cesta = 'realizacie/'
        kroky = [('', 'Úvod'), (cesta, 'Realizácie')]
        limit = int(self.nastavenia.get('realizacie_na_stranu', 300))
        if len(self.realizacie) <= limit:
            stranky = [(cesta, self.realizacie, '')]
            nav = []
        else:
            # nad limit: stránky po desaťročiach, úvodná stránka zoznamu má najnovšie desaťročie
            dekady = {}
            for r in self.realizacie:
                rok = self.rok_filtra(r)
                dekady.setdefault((rok // 10) * 10 if rok else 0, []).append(r)
            poradie = sorted((d for d in dekady if d), reverse=True) + ([0] if 0 in dekady else [])
            stranky = []
            for i, d in enumerate(poradie):
                c = cesta if i == 0 else f'{cesta}roky-{d if d else "bez"}/'
                stranky.append((c, dekady[d], d))
            nav = []
            for c, zoz, d in stranky:
                text = (f'{d} až {d + 9}' if d else 'Rok neuvedený')
                nav.append((c, text, len(zoz)))
        for c, zoznam, dekada in stranky:
            pre_c = self.pre(c)
            kroky_c = kroky if c == cesta else kroky + [(c, nav[[x[0] for x in nav].index(c)][1])]
            navigacia = ''
            if len(stranky) > 1:
                polozky = []
                for c2, text, pocet in nav:
                    cur = ' aria-current="page"' if c2 == c else ''
                    polozky.append(f'<li><a href="{esc(self.rel(pre_c, c2))}"{cur}>{T(text)} <span>{pocet}</span></a></li>')
                navigacia = f'<nav class="dekady" aria-label="Realizácie podľa rokov"><ul>{"".join(polozky)}</ul></nav>'
            hlava = self.hlava_stranky(pre_c, kroky_c, 'Realizácie', bok=f'<p class="uvodny">{T(t["veta"])}</p>',
                                       navyse=navigacia + (self.filtre(zoznam, roky_vyber=len(stranky) == 1) if zoznam else '')
                                       + (self.mierka(pre_c) if zoznam and len(stranky) == 1 else ''))
            if zoznam:
                dlazdice = ''.join(self.dlazdica(pre_c, r, 'h2', data=True, poradie=i, lcp=(i == 1))
                                    for i, r in enumerate(zoznam, 1))
                # postupné načítanie (zadanie bmb-fb-uplnost bod 4): všetky karty sú v HTML (bez JS viditeľné, odkaz
                # na každú realizáciu pre vyhľadávače), app.js ukáže prvých data-naraz z výberu a tlačidlo Zobraziť
                # ďalšie s počtom zvyšných; filter a zoradenie pracujú nad všetkými kartami, nielen nad zobrazenými
                telo = (hlava + f'\n<div class="obsah zoznam"><p class="filtre-stav" data-stav aria-live="polite"></p>'
                        f'<ul class="karty karty-zoznam" id="zoznam-realizacii" data-zoznam data-naraz="{self.naraz()}">{dlazdice}</ul>'
                        '<p class="viac-obal" data-viac-obal hidden><button class="tlacidlo tlacidlo-obrys" type="button" '
                        'data-viac aria-controls="zoznam-realizacii">Zobraziť ďalšie <span class="viac-pocet" data-viac-pocet></span></button></p>'
                        '<div class="prazdne" data-prazdne hidden><p>Pre tento výber nie je žiadna realizácia.</p>'
                        '<button class="tlacidlo tlacidlo-obrys" type="button" data-zrusit>Zrušiť filter</button></div></div>')
            else:
                telo = hlava + '<div class="obsah text"><p>Realizácie pripravujeme.</p></div>'
            zoznam_ld = {'@type': 'ItemList', 'itemListElement': [
                {'@type': 'ListItem', 'position': i, 'url': self.abs_url(f'realizacie/{r["slug"]}/'), 'name': r['nazov']}
                for i, r in enumerate(zoznam, 1)]}
            title = t['title'] if c == cesta else f'Realizácie {kroky_c[-1][1]} | BM Building'
            self.podstranka(c, kroky_c, title, t['description'], telo, 'realizacie', [zoznam_ld])

    def titulok_realizacie(self, r):
        """Title stránky realizácie do 60 znakov bez useknutia na nezmysel (brána pokus 1, správnosť 3).

        Poradie: vlastné pole titulok; názov s mestom; názov; prvá a posledná časť názvu (ulica) s mestom alebo bez;
        názov s mestom alebo názov bez prípony značky; potom začiatok názvu po prvú čiarku s mestom alebo bez,
        s príponou alebo bez; nakoniec celý názov, hoci má viac ako 60 znakov (Google ho skráti sám). Názov sa
        nikdy neseká uprostred vety, lebo skrátenie po slovách dávalo „Rekonštrukcia bytového, …“ a pri dlhom
        mieste title bez názvu (brána pokus 2, správnosť 1). Prípona „ | BM Building“ ide všade, kde sa zmestí."""
        pripona, limit = ' | BM Building', 60
        if r['titulok']:
            if len(r['titulok']) + len(pripona) <= limit:
                return r['titulok'] + pripona
            if len(r['titulok']) <= limit:
                return r['titulok']
        nm, m = self.nazov_s_miestom(r), self.mesto(r)
        casti = [c.strip() for c in r['nazov'].split(',') if c.strip()]

        def s_mestom(t):
            return f'{t}, {m}' if m and m.lower() not in t.lower() else t
        kandidati = [nm, r['nazov']]
        if len(casti) > 2:
            kratsi = f'{casti[0]}, {casti[-1]}'
            kandidati += [s_mestom(kratsi), kratsi]
        for k in kandidati:
            if len(k) + len(pripona) <= limit:
                return k + pripona
        for k in (nm, r['nazov']):
            if len(k) <= limit:
                return k
        zaciatok = [s_mestom(casti[0]), casti[0]] if len(casti) > 1 else []
        for k in zaciatok:
            if len(k) + len(pripona) <= limit:
                return k + pripona
        for k in kandidati[2:] + zaciatok:
            if len(k) <= limit:
                return k
        return r['nazov']

    def popis_fotky(self, r, f):
        """Popis fotky z alt bez „fotka N z M“; prázdny, ak nehovorí nič nad názov a miesto stavby."""
        s = re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')
        zname = {w[:4] for w in re.findall(r'\w+', f'{r.get("nazov", r.get("nadpis", ""))} {r.get("miesto", "")}'.lower()) if len(w) >= 3}
        if not s or not any(len(w) >= 3 and w[:4] not in zname for w in re.findall(r'\w+', s.lower())):
            return ''
        return s

    @staticmethod
    def nezvacsena(f, bunka):
        """True, keď fotka vyplní bunku (object-fit:cover) najviac s 1,25 násobným zväčšením súboru."""
        return f['plna_w'] * ZVACSENIE_MAX >= bunka[0] and f['plna_h'] * ZVACSENIE_MAX >= bunka[1]

    def galeria(self, pre, r, nazov, popis_udaje):
        """Galéria: prvá fotka veľká, ostatné v mriežke 3 : 2; každá fotka je odkaz na plnú verziu (funguje bez JS)
        a nesie dáta pre lightbox v app.js (zmluva v dizajn-olovnica/PRIPRAVA-BUILD.md bod 3). Rozmazané fotky
        v nej nie sú (priprav_fotky)."""
        fotky = r['galeria']
        if not fotky:
            return ''
        polozky = []
        malo = len(fotky) <= 3
        for i, f in enumerate(fotky, 1):
            # vizualizácia: pod náhľadom meno autora (projektant alebo autor z príspevku, obsah-zmeny.md), štítok na fotke
            popis = (('Vizualizácia, autor: ' + f['autor']) if f.get('autor') else '') if f.get('viz') else self.popis_fotky(r, f)
            # prvá veľká len keď nie je tou istou fotkou ako pohľad A nad galériou (detail_dvojstrana), inak by
            # rovnaká fotka bola dvakrát veľká pod sebou; ani fotka karty nahradená nastavením karta_foto (malý zdroj),
            # galéria má potom tú istú stavbu ako pred zmenou karty (overenie zostavy bmb-presnost)
            prva = (i == 1 and len(fotky) >= 4 and f['w'] >= 960 and f['w'] >= f['h']
                    and f is not r.get('karta') and f is not r.get('karta_nahradena')
                    and self.nezvacsena(f, GALERIA_BUNKY['prva']))
            # dáta lightboxu (lb_data, Stavba 1): alt nesie číslo podľa poradia v galérii (to isté ukáže počítadlo),
            # data-galeria = slug, vizualizácia so štítkom na náhľade, v lightboxe aj v alt texte; pri náhľade so
            # srcset (nie malá fotka) berie app.js srcset a zdroj AVIF lightboxu z náhľadu (lb_data nahlad)
            sizes = SIZES_GALERIA_PRVA if prva or malo else SIZES_GALERIA
            mala = not prva and not self.nezvacsena(f, GALERIA_BUNKY['malo' if malo else 'mriezka'])
            plna, alt, atributy = self.lb_data(pre, r, f, i, len(fotky), nazov, popis_udaje, r.get('slug', ''),
                                               nahlad=not mala)
            if mala:
                # malá fotka (GALERIA_BUNKY): len plný súbor bez srcset (prirodzená veľkosť = súbor v CSS px), CSS
                # .galeria-odkaz.mala img object-fit:scale-down ju nikdy nezväčší. Len WebP: jeden malý súbor
                # (užší než 460 px) v prirodzenej veľkosti, AVIF by ušetril pár kB (fotky-kvalita/KODOVANIE.md)
                self.registruj_fotku(r, f)
                obr = (f'<img src="{plna}" width="{f["plna_w"]}" height="{f["plna_h"]}" '
                       f'alt="{esc(alt or f["alt"])}" loading="lazy" decoding="async">')
            else:
                # bunka 3 : 2 s object-fit: cover: širšia fotka potrebuje širší súbor (sizes_pre_cover)
                obr = self.img(pre, r, f, sizes_pre_cover(sizes, f['w'], f['h']), alt=alt or None)
            stitok = '<span class="stitok-viz">Vizualizácia</span>' if f.get('viz') else ''
            polozky.append(f'<li class="galeria-polozka{" prva" if prva else ""}"><a class="galeria-odkaz{" mala" if mala else ""}" href="{plna}" '
                           f'data-lightbox {atributy}>{obr}{stitok}<span class="galeria-c cisla" aria-hidden="true">{i:02d}</span></a>'
                           + (f'<span class="galeria-popis">{T(popis)}</span>' if popis else '') + '</li>')
        pocet = tvar(len(fotky), 'fotka', 'fotky', 'fotiek')
        return (f'<section class="sekcia galeria-sekcia" id="galeria" aria-labelledby="galeria-nadpis"><div class="obsah">'
                f'<div class="sekcia-hlava galeria-hlava"><h2 class="nadpis" id="galeria-nadpis">Galéria <span class="nadpis-pocet cisla">{T(pocet)}</span></h2>'
                f'<p class="navod">{T("Fotka sa otvorí cez celú obrazovku. Ďalšia: šípka alebo potiahnutie prstom.")}</p></div>'
                f'<ul class="galeria{" galeria-malo" if malo else ""}" data-galeria>{"".join(polozky)}</ul></div></section>'
                + self.lightbox_dialog(pre, nazov, r, fotky[0]))

    def pred_po(self, pre, r):
        """Pred a po len pre dvojicu zo sablona/fotky-pravidla.json (istota vysoká, oba súbory v tejto realizácii, nie
        rozmazané, rovnaký pomer strán do 2 %); inak nič (FOTKY.md bod 5). Stredná istota nestačí: zadanie
        bmb-olovnica 21. 11. 2026 „ak istota nie je vysoká, pred a po nedávaj“ (pár Rosnička je len v navrhy_pred_po)."""
        for par in self.pravidla['pary']:
            if par.get('slug') != r['slug']:
                continue
            if str(par.get('istota', '')).strip().lower() != 'vysoká':
                varuj(f'pred a po {r["slug"]}: istota {par.get("istota")!r} nestačí, nezobrazené')
                continue
            najdi = {('obrazky/' + f['rel']): f for f in r['galeria']}
            fp, fo = najdi.get(str(par.get('pred'))), najdi.get(str(par.get('po')))
            if not fp or not fo:
                varuj(f'pred a po {r["slug"]}: fotka chýba alebo je rozmazaná, nezobrazené')
                continue
            if abs(fp['w'] / fp['h'] - fo['w'] / fo['h']) > 0.02 * (fo['w'] / fo['h']):
                varuj(f'pred a po {r["slug"]}: fotky majú iný pomer strán, nezobrazené')
                continue
            popis_pred = jeden_riadok(par.get('popis_pred')) or 'Pred'
            popis_po = jeden_riadok(par.get('popis_po')) or 'Po'
            nm = self.nazov_s_miestom(r)
            return (f'<section class="sekcia pred-po-sekcia" aria-labelledby="pred-po-nadpis"><div class="obsah">'
                    f'<h2 class="nadpis" id="pred-po-nadpis">{T(popis_pred)} a {T(popis_po.lower())}</h2>'
                    f'<div class="pred-po" data-pred-po>'
                    f'<figure class="pred-po-pred">{self.img(pre, r, fp, SIZES_PRED_PO, alt=f"{nm}, {popis_pred.lower()}")}'
                    f'<figcaption>{T(popis_pred)}</figcaption></figure>'
                    f'<figure class="pred-po-po">{self.img(pre, r, fo, SIZES_PRED_PO, alt=f"{nm}, {popis_po.lower()}")}'
                    f'<figcaption>{T(popis_po)}</figcaption></figure></div>'
                    '<p class="poznamka">Obe fotky sú z toho istého miesta.</p></div></section>')
        return ''

    def susedia(self, zoznam, i, nazov_kluc, slovo):
        """Navigácia Novšia a Staršia pod detailom (realizácia, novinka)."""
        out = []
        if i > 0:
            p = zoznam[i - 1]
            out.append(f'<a class="predch" href="../{esc(p["slug"])}/"><span class="susedia-smer">{SVG_SIPKA_SPAT}Novšia'
                       f'<span class="vh"> {slovo}:</span></span><b>{T(p[nazov_kluc])}</b></a>')
        if i + 1 < len(zoznam):
            p = zoznam[i + 1]
            out.append(f'<a class="dalsia" href="../{esc(p["slug"])}/"><span class="susedia-smer">Staršia'
                       f'<span class="vh"> {slovo}:</span>{SVG_SIPKA}</span><b>{T(p[nazov_kluc])}</b></a>')
        return f'<nav class="susedia" aria-label="Ďalšie: {slovo}">{"".join(out)}</nav>' if out else ''

    def udaje_realizacie(self, r):
        """Údaje realizácie v hlave stránky (dl.fakty ukážky): miesto, druh, rola BM Building s osobou zo starého webu,
        projektant, investor a zhotoviteľ s adresou a webom pod menom (sablona/stary-web.json), roky stavby, termín
        výstavby s mesiacmi a etapy; bez rokov stavby rok príspevku. Weby strán ako text, nie odkaz (cudzie domény
        sa mohli od starého webu zmeniť)."""
        druh = ', '.join(self.kat_nazov(k) for k in r['kategorie'] if k != 'ine')
        strany = r.get('strany') or {}

        def strana(kluc, meno):
            zoz = strany.get(kluc) or []
            if not meno and zoz:
                meno = ', '.join(x['meno'] for x in zoz if x.get('meno'))
            if not meno:
                return ''
            riadky = []
            for x in zoz:
                kusy = [T(v) for v in (x.get('adresa'), x.get('web')) if v]
                if not kusy:
                    continue
                predpona = f'{T(x["meno"])}: ' if len(zoz) > 1 and x.get('meno') else ''
                riadky.append(f'<span class="fakty-pod">{predpona}{BODKA.join(kusy)}</span>')
            return T(meno) + ''.join(riadky)
        rola = T(r['rola']) + (f'<span class="fakty-pod">{T(r["rola_osoba"])}</span>' if r['rola'] and r.get('rola_osoba') else '')
        pole = [('Miesto', T(r['miesto'])), ('Druh stavby', T(druh)), ('Rola BM Building', rola),
                ('Projektant', strana('projektant', r['projektant'])), ('Autor', T(r.get('autor') or '')),
                ('Investor', strana('investor', r['investor'])),
                ('Zhotoviteľ', strana('zhotovitel', r['zhotovitel'])), ('Roky', T(r['roky']))]
        if r.get('termin'):
            pole.append(('Termín výstavby', T(r['termin'])))
        if r.get('etapy'):
            pole.append(('Etapy', ''.join(f'<span class="fakty-riadok">{T(e)}</span>' for e in r['etapy'])))
        if not r['roky'] and self.roky_karty(r):
            # rok príspevku len pod týmto popiskom, nikdy ako Roky (nález 1 brány bmb-fb-uplnost)
            pole.append(('Rok príspevku', T(self.roky_karty(r))))
        html_ = ''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in pole if v)
        return f'<dl class="fakty udaje">{html_}</dl>' if html_ else ''

    def detail_dvojstrana(self, pre, r, nazov, text, roky=''):
        """Dvojstrana detailu ako úvod ukážky: vľavo fotka (pohľad A, odkrytie sprava), vpravo kóta s rokmi a text
        (prvý odsek väčší, ďalšie tlmené). Fotka je fotka karty (nikdy vizualizácia, ak existuje skutočná)."""
        f = r.get('karta')
        foto = ''
        if f:
            n = len(r['galeria'])
            stitok = '<span class="stitok-viz">Vizualizácia</span>' if f.get('viz') else ''
            obr = self.img(pre, r, f, sizes_pre_cover(SIZES_DETAIL, f['w'], f['h']), lcp=True, alt=self.alt_bez_cisla(f) or nazov)
            popis = self.popis_fotky(r, f) if not f.get('viz') else (
                'Vizualizácia' + (f', autor: {f["autor"]}' if f.get('autor') else ''))
            casti = [T(popis or nazov)]
            if n > 1:
                casti.append(f'<a href="#galeria">{T("Galéria, " + tvar(n, "fotka", "fotky", "fotiek"))}</a>')
            foto = (f'<figure class="pohlad detail-foto"><div class="pohlad-ramec">{obr}{stitok}</div>'
                    f'<figcaption>{SVG_POHLAD_ZNAK}<span><strong>Pohľad A:</strong> {BODKA.join(casti)}</span></figcaption></figure>')
        if not text and not foto:
            return ''
        kota = self.kota(roky, 'kota-detail') if roky else ''
        return (f'<div class="obsah mriezka detail{"" if foto else " detail-bez-fotky"}">{foto}'
                f'<div class="detail-text atelier-text">{kota}{text}</div></div>')

    def stranky_realizacie(self):
        poradie = self.realizacie
        for i, r in enumerate(poradie):
            cesta = f'realizacie/{r["slug"]}/'
            pre = self.pre(cesta)
            nm = self.nazov_s_miestom(r)
            title = self.titulok_realizacie(r)
            vety = [s_bodkou(nm)]
            if r['rola']:
                vety.append(s_bodkou(f'Rola BM Building: {male(r["rola"])}'))
            if r['projektant']:
                vety.append(s_bodkou(f'Projektant: {r["projektant"]}'))
            if r.get('autor'):
                vety.append(s_bodkou(f'Autor: {r["autor"]}'))
            if r['roky']:
                vety.append(s_bodkou(f'Termín: {r["roky"]}'))
            desc = ''
            for v in vety:
                skus = (desc + ' ' + v).strip()
                if len(skus) <= 155:
                    desc = skus
                elif not desc:
                    desc = s_bodkou(skrat(v, 154))
            pole_html = self.udaje_realizacie(r)
            odseky = [p for p in r['text'].split('\n') if p.strip()]
            text = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">{T(p)}</p>' for i, p in enumerate(odseky))
            # realizácia z Facebooku: dátum príspevku a odkaz (príspevok, inak stránka firmy), bez nových faktov
            fb = r['fb_url'] or self.firma['facebook']
            if r['zdroj'] == 'fb' and fb:
                kedy = f' {datum_text(r["datum"])}' if r['datum'] else ''
                text += (f'<p class="tlmene">{T("Príspevok o tejto stavbe zverejnil BM Building" + kedy + " na")} '
                         f'<a href="{esc(fb)}">Facebooku</a>.</p>')
            elif r['fb_url']:
                text += f'<p class="tlmene"><a href="{esc(r["fb_url"])}">Príspevok na Facebooku</a></p>'
            kroky = [('', 'Úvod'), ('realizacie/', 'Realizácie'), (cesta, r['nazov'])]
            m = self.mesto(r) if self.mesto(r).lower() not in r['nazov'].lower() else ''
            hlava = self.hlava_stranky(pre, kroky, r['nazov'], m, bok=pole_html, trieda='hlava-detail')
            udaje_lb = self.udaje_lightboxu(r)
            galeria = self.galeria(pre, r, nm, udaje_lb)
            obal = r['obal'] or (r['galeria'][0] if r['galeria'] else None)
            og = self.og_cesta_realizacie(r)
            self.registruj_og(og, obal['zdroj'] if obal else None, r['nazov'],
                              ', '.join(x for x in (r['rola'], r['roky']) if x), 'BM Building, s.r.o., bmbuilding.sk')
            telo_detail = self.detail_dvojstrana(pre, r, nm, text, self.roky_stitok(r))
            telo = (hlava + '\n' + telo_detail + '\n' + self.pred_po(pre, r) + galeria + '\n'
                    + f'<div class="obsah susedia-obal">{self.susedia(poradie, i, "nazov", "realizácia")}'
                    f'<p class="viac"><a class="odkaz-sipka" href="../">Všetky realizácie {SVG_SIPKA}</a></p></div>')
            obrazky = [self.abs_url(self.obrazok_cesty(r, f)[f['plna_w']]) for f in r['galeria']]
            # CreativeWork bez creator a about: BM Building stavbu nevytvoril (dozor, manažment); contributor len pri
            # známej role (UFO rolu nemá, FAKTY-WEB: rolu nevymýšľať; brána pokus 1, správnosť 7)
            dielo = {'@type': 'CreativeWork', '@id': self.abs_url(cesta + '#realizacia'), 'name': nm,
                     'url': self.abs_url(cesta), 'image': obrazky or [self.abs_url(og)]}
            if r['rola']:
                dielo['contributor'] = {'@id': self.abs_url('#firma')}
            if r['miesto']:
                dielo['locationCreated'] = {'@type': 'Place', 'name': r['miesto']}
            if r['text']:
                dielo['description'] = r['text'].replace('\n', ' ')
            # datePublished len pri príspevku (fb, sprava); dátum článku starého webu nie je dátum stavby
            if r['datum'] and r['zdroj'] != 'web':
                dielo['datePublished'] = r['datum']
            if r['upravene']:
                dielo['dateModified'] = r['upravene']
            self.podstranka(cesta, kroky, title, desc, telo, 'realizacie/' + r['slug'], [dielo], og=og,
                            og_typ='article', lastmod=self.zmena(r), zaver_dalej=self.texty['vyzvy']['realizacia'])

    def stranky_noviniek(self):
        if not self.novinky:
            return
        t = self.texty['novinky']
        cesta = 'novinky/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Novinky')]
        karty = ''.join(self.novinka_karta(pre, n, nadpis='h2') for n in self.novinky)
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'\n<div class="obsah zoznam"><ul class="karty novinky-karty">{karty}</ul></div>')
        zoznam = {'@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'url': self.abs_url(f'novinky/{n["slug"]}/'), 'name': n['nadpis']}
            for i, n in enumerate(self.novinky, 1)]}
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'novinky', [zoznam], typ_stranky='CollectionPage',
                        lastmod=max(self.zmena(n) for n in self.novinky))
        for i, n in enumerate(self.novinky):
            cesta_n = f'novinky/{n["slug"]}/'
            pre_n = self.pre(cesta_n)
            kroky_n = kroky + [(cesta_n, n['nadpis'])]
            odseky = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">{T(p)}</p>'
                             for i, p in enumerate(p for p in n['text'].split('\n') if p.strip()))
            fb = n['fb_url'] or self.firma['facebook']
            if fb:
                odseky += f'<p class="tlmene"><a href="{esc(fb)}">{T("Pôvodný príspevok na Facebooku")}</a></p>'
            galeria = self.galeria(pre_n, n, n['nadpis'], datum_text(n['datum']))
            # údaje vpravo v hlave: dátum a stavba, ku ktorej novinka patrí, s rokmi stavby (pole realizacia)
            r_n = self.realizacia_novinky(n)
            fakty = [('Dátum', f'<span class="cisla">{T(datum_text(n["datum"]))}</span>')]
            if r_n:
                fakty.append(('Stavba', f'<a href="{esc(self.rel(pre_n, "realizacie/" + r_n["slug"] + "/"))}">{T(r_n["nazov"])}</a>'))
                if self.roky_karty(r_n):
                    fakty.append(('Roky stavby' if r_n['roky'] else 'Rok príspevku', T(self.roky_karty(r_n))))
            bok_n = '<dl class="fakty udaje">' + ''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in fakty) + '</dl>'
            dvoj = self.detail_dvojstrana(pre_n, n, n['nadpis'], odseky)
            telo_n = (self.hlava_stranky(pre_n, kroky_n, n['nadpis'], datum_text(n['datum']), bok=bok_n, trieda='hlava-detail')
                      + f'\n{dvoj}\n{galeria}'
                      + f'\n<div class="obsah susedia-obal">{self.susedia(self.novinky, i, "nadpis", "novinka")}'
                      f'<p class="viac"><a class="odkaz-sipka" href="../">Všetky novinky {SVG_SIPKA}</a></p></div>')
            og = f'og/novinka-{n["slug"]}.jpg'
            obal = n['obal'] or (n['galeria'][0] if n['galeria'] else None)
            self.registruj_og(og, obal['zdroj'] if obal else None, n['nadpis'],
                              datum_text(n['datum']), 'BM Building, s.r.o., bmbuilding.sk')
            clanok = {'@type': 'Article', '@id': self.abs_url(cesta_n + '#clanok'), 'headline': skrat(n['nadpis'], 110),
                      'url': self.abs_url(cesta_n), 'datePublished': n['datum'], 'inLanguage': 'sk',
                      'image': [self.abs_url(self.obrazok_cesty(n, f)[f['plna_w']]) for f in n['galeria']] or [self.abs_url(og)],
                      'author': {'@id': self.abs_url('#firma')}, 'publisher': {'@id': self.abs_url('#firma')},
                      'mainEntityOfPage': {'@id': self.abs_url(cesta_n)}}
            if n['upravene']:
                clanok['dateModified'] = n['upravene']
            if n['text']:
                clanok['articleBody'] = n['text'].replace('\n', ' ')
            desc = skrat(re.sub(r'\s+', ' ', n['text'] or n['nadpis']), 155)
            if len(desc) < 70 and datum_text(n['datum']):
                # krátky príspevok (34 a 47 znakov, overenie kolo 1): doplní sa len kto a kedy ho uverejnil
                desc = s_bodkou(desc) + f' Príspevok BM Building z {datum_text(n["datum"])}.'
            title = n['nadpis'] + ' | BM Building' if len(n['nadpis']) <= 46 else skrat(n['nadpis'], 60)
            self.podstranka(cesta_n, kroky_n, title, desc, telo_n, 'novinky/' + n['slug'], [clanok], og=og,
                            og_typ='article', lastmod=self.zmena(n))

    def stranka_opravnenia(self):
        t = self.texty['opravnenia']
        cesta = 'opravnenia/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Oprávnenia')]
        doklady = self.doklady(pre, nadpis='h2', obrazky=True)
        s_obrazkom = sum(1 for x in doklady if 'data-lightbox' in x)
        # veta o zakrytí pod obrázkami (FAKTY-WEB.md, Oprávnenia: osvedčenia SKSI); bez obrázkov pôvodná poznámka
        poznamka = t['poznamka'] if s_obrazkom else t.get('poznamka_bez_obrazkov', t['poznamka'])
        lb = ''
        if s_obrazkom:
            prvy = re.search(r'data-plna="([^"]*)" data-srcset="[^"]*" data-sirka="(\d+)" data-vyska="(\d+)"', ''.join(doklady))
            lb = self.lightbox_html('Osvedčenia SKSI', html.unescape(prvy.group(1)), prvy.group(2), prvy.group(3),
                                    slovo='Osvedčenie')
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'\n<div class="obsah stranka-text"><div class="doklady">{"".join(doklady)}</div><p class="poznamka">{T(poznamka)}</p>{lb}'
                # Obchodný register ako riadok cenníka ukážky: nadpis, text zápisu a zdroj v stĺpci ceny
                f'<ul class="cennik register"><li class="sluzba"><h2 class="nadpis-maly" id="obchodny-register">{T(t["register_nadpis"])}</h2>'
                f'<p class="sluzba-opis">{T(t["register"])}</p>'
                f'<p class="cena"><small>Zdroj: <a href="{esc(t["register_zdroj"])}">výpis z Obchodného registra SR</a></small></p></li></ul></div>')
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'opravnenia', [], lastmod=self.vydanie)

    def stranka_kontakt(self):
        """Kontakt: hlava stránky a sekcia Kontakt z ukážky ako hlavný obsah (kontakt_sekcia, hlavna=True): všetky
        kontaktné a fakturačné údaje zo starého webu, kontaktný blok v poradí banneru, Navigovať a Facebook."""
        t = self.texty['kontakt']
        cesta = 'kontakt/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Kontakt')]
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + '\n' + self.kontakt_sekcia(pre, hlavna=True))
        # jeden uzol WebPage a ContactPage s @id namiesto dvoch uzlov pre tú istú URL (brána pokus 2, správnosť 7)
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'kontakt', [], zaver=False,
                        lastmod=self.vydanie, pata_firma=False, typ_stranky=['WebPage', 'ContactPage'],
                        stranka_navyse={'about': {'@id': self.abs_url('#firma')}})

    def stranka_ochrana(self):
        t = self.texty['ochrana']
        cesta = 'ochrana-udajov/'
        pre = self.pre(cesta)
        f = self.firma
        adresa = self.adresa()
        kontakt = ' alebo '.join(x for x in (f['email'] and f'e-mailom na {f["email"]}', f['tel'] and f'telefonicky na {f["tel"]}') if x)
        prev = f'{f["meno"]}, {adresa}' + (f', IČO {f["ico"]}' if f['ico'] else '') + '.'
        if f['zapis']:
            prev += ' ' + f['zapis']
        casti = [
            ('Prevádzkovateľ', [prev, f'Kontakt na prevádzkovateľa: {kontakt}.' if kontakt else '']),
            ('Čo tento web nerobí', [
                'Web nepoužíva cookies, analytiku ani iné sledovacie nástroje a nemá žiadne formuláre. Nevkladá obsah tretích strán: mapy, Facebook ani písma z cudzích serverov sa nenačítavajú. Písmo a malý skript na pohyb a ovládanie stránky sú uložené priamo na tomto webe a nič neodosielajú.',
                'Odkazy na Mapy Google, Mapy.cz a Facebook otvoria stránku tretej strany až po kliknutí. Na tej stránke platia pravidlá jej prevádzkovateľa.']),
            # čl. 13 nariadenia 2016/679: právny základ, doba uchovávania a prenos mimo EÚ (brána pokus 1, správnosť 10)
            ('Hosting', [
                'Web je statický a beží na službe GitHub Pages spoločnosti GitHub, Inc. Pri návšteve webu GitHub podľa svojej dokumentácie zaznamenáva a ukladá IP adresu návštevníka na bezpečnostné účely. Tieto technické záznamy spracúva GitHub, nie BM Building.',
                'GitHub, Inc. sídli v USA. Podľa svojho vyhlásenia o ochrane osobných údajov spracúva osobné údaje aj v USA a pri prenose z Európskej únie do krajín, ktorým Európska komisia nepriznala primeranú úroveň ochrany, sa spravidla opiera o štandardné zmluvné doložky Európskej komisie.']),
            ('Ak nás kontaktujete', [
                'Ak nám napíšete e-mail alebo zavoláte, údaje, ktoré nám pritom poskytnete, použijeme len na vybavenie vašej požiadavky.',
                'Právnym základom je čl. 6 ods. 1 písm. b) nariadenia (EÚ) 2016/679, ak ide o opatrenia pred uzatvorením zmluvy na vašu žiadosť, napríklad dopyt na stavebný dozor. Pri ostatných otázkach je ním čl. 6 ods. 1 písm. f), teda náš oprávnený záujem odpovedať na správu, ktorú ste nám poslali.',
                'Údaje uchovávame len tak dlho, ako je potrebné na vybavenie požiadavky. Ak z nej vznikne zmluva, uchovávame ich po dobu, ktorú vyžadujú právne predpisy.']),
            ('Vaše práva', [
                'Podľa nariadenia (EÚ) 2016/679 (GDPR) máte právo na prístup k svojim osobným údajom, na ich opravu, vymazanie a obmedzenie spracúvania, právo namietať a právo na prenosnosť údajov. Uplatniť ich môžete ' + (kontakt + ' alebo ' if kontakt else '') + f'písomne na adrese {adresa}.',
                'Máte tiež právo podať sťažnosť Úradu na ochranu osobných údajov Slovenskej republiky.']),
        ]
        html_casti = []
        for nadpis, odseky in casti:
            html_casti.append(f'<h2 class="nadpis-maly">{T(nadpis)}</h2>' + ''.join(f'<p>{T(p)}</p>' for p in odseky if p))
        odkazy = (f'<p class="poznamka">Zdroje: <a href="{esc(t["github_url"])}">GitHub Pages, Data collection</a>, '
                  f'<a href="{esc(t["github_privacy_url"])}">GitHub General Privacy Statement</a>, '
                  f'<a href="{esc(t["uoou_url"])}">Úrad na ochranu osobných údajov SR</a>. '
                  'Písmo Geist a Geist Mono (The Geist Project Authors), licencia SIL Open Font License 1.1: '
                  f'<a href="{esc(pre)}fonts/OFL-Geist.txt">fonts/OFL-Geist.txt</a>.</p>')
        kroky = [('', 'Úvod'), (cesta, 'Ochrana osobných údajov')]
        telo = (self.hlava_stranky(pre, kroky, t['h1'])
                + f'\n<div class="obsah stranka-text"><div class="text">{"".join(html_casti)}{odkazy}</div></div>')
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'ochrana-udajov', [], lastmod=self.vydanie)

    def stranka_404(self):
        t = self.texty['chyba']
        zaklad = self.nastavenia.get('zaklad_nahladu', '/bmbuilding/') if self.nahlad else '/'
        if not re.match(r'^/[A-Za-z0-9._/-]*$', zaklad) or '..' in zaklad:
            zaklad = '/'
        if not zaklad.endswith('/'):
            zaklad += '/'
        z = esc(zaklad)
        # text sľubuje „alebo nám zavolajte“: tlačidlo Zavolať aj v tele, nielen v hlavičke (brána dizajnu pokus 2, nález 6)
        f = self.firma
        tel = f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať {SVG_SIPKA}</a>' if f['tel_href'] else ''
        # akcie ako v úvode ukážky: plné tlačidlo Zavolať a odkazy so šípkou
        telo = (self.hlava_stranky(zaklad, [], t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'<div class="obsah chyba"><div class="uvod-akcie akcie-vlavo">{tel}<a class="odkaz-sipka" href="{z}">Na úvod {SVG_SIPKA}</a>'
                f'<a class="odkaz-sipka" href="{z}realizacie/">Realizácie {SVG_SIPKA}</a><a class="odkaz-sipka" href="{z}kontakt/">Kontakt {SVG_SIPKA}</a></div></div>')
        self.stranka(None, t['title'], '', telo, '', [], do_mapy=False, zaklad=zaklad)

    def presmerovania(self):
        cesty_webu = set(self.subory)
        d = json.loads((SABLONA / 'presmerovania.json').read_text(encoding='utf-8'))
        slugy = {r['slug'] for r in self.realizacie}
        for z in d.get('presmerovania', []):
            stara = str(z.get('stara') or '')
            ciel = str(z.get('ciel') or '/')
            if not re.match(r'^/[A-Za-z0-9._/-]+$', stara) or '..' in stara or '//' in stara:
                varuj(f'presmerovanie {stara!r}: neplatná stará cesta, preskočené')
                continue
            if not re.match(r'^/[a-z0-9/-]*(#[a-z0-9-]+)?$', ciel):
                varuj(f'presmerovanie {stara!r}: neplatný cieľ {ciel!r}, použitý úvod')
                ciel = '/'
            m = re.match(r'^/realizacie/([a-z0-9-]+)/$', ciel)
            if m and m.group(1) not in slugy:
                varuj(f'presmerovanie {stara}: realizácia {m.group(1)} nie je zverejnená, cieľ /realizacie/')
                ciel = '/realizacie/'
            cesta = stara.strip('/') + '/'
            subor = cesta + 'index.html'
            if subor in cesty_webu:
                varuj(f'presmerovanie {stara}: koliduje so stránkou webu, preskočené')
                continue
            ciel_cesta, _, kotva = ciel.lstrip('/').partition('#')
            rel = self.rel(self.pre(cesta), ciel_cesta) + (('#' + kotva) if kotva else '')
            canon = self.abs_url(ciel_cesta)
            robots = '<meta name="robots" content="noindex, nofollow">\n' if self.nahlad else ''
            # prázdna ikona (data:,) a img-src data:: bez nej Firefox pýta /favicon.ico mimo CSP (porušenie, overenie
            # kolo 1); skutočná ikona by sa pri okamžitom presmerovaní aj tak zrušila (requestfailed, oprava 1)
            obsah = ('<!doctype html>\n<html lang="sk">\n<head>\n<meta charset="utf-8">\n'
                     '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; base-uri \'none\'; form-action \'none\'">\n'
                     f'{robots}<title>Stránka sa presunula | BM Building</title>\n'
                     '<link rel="icon" href="data:,">\n'
                     f'<meta http-equiv="refresh" content="0; url={esc(rel)}">\n'
                     f'<link rel="canonical" href="{esc(canon)}">\n'
                     '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
                     f'<p>Stránka sa presunula na novú adresu: <a href="{esc(rel)}">{esc(canon)}</a></p>\n</body>\n</html>\n')
            self.subory[subor] = obsah.encode('utf-8')

    def staticke(self):
        # favicon a logá zo značky (LOGO.md bod 5): SVG priamo zo sablona/, rastre renderované z tých istých ciest
        self.subory['favicon.svg'] = self.favicon_svg.encode('utf-8')
        self.subory['logo.svg'] = self.logo_svg.encode('utf-8')
        # prázdny obrázok 4 : 5 vo farbe podkladu karty pásu (src kariet, kým app.js nedosadí skutočnú fotku)
        buf = io.BytesIO()
        Image.new('RGB', (4, 5), (0x2A, 0x2B, 0x2B)).save(buf, 'WEBP', lossless=True)
        self.subory['prazdna-45.webp'] = buf.getvalue()
        # ručne kreslené rastre favicon (nastroje favicon.py v ops/.../favicon/): kopírujú sa po bajtoch, nič sa
        # nezmenšuje z SVG (Lanczos by zahodil ručné pixely 16 a 32 px)
        for meno in FAVICON_SUBORY:
            p = SABLONA / 'favicon' / meno
            if p.is_file():
                self.subory[meno] = p.read_bytes()
            else:
                varuj(f'sablona/favicon/{meno} chýba, web ho nebude mať')
        logo = self.logo_svg
        odt_logo = hashlib.sha256((VERZIA_OG + '|logo|' + logo).encode('utf-8')).hexdigest()
        self.obrazky['logo.png'] = (odt_logo + '-512', lambda: png_bajty(svg_obraz(logo, 512)))
        for zdroj in PISMA:
            p = SABLONA / zdroj
            if p.is_file():
                self.subory['fonts/' + zdroj] = p.read_bytes()
            elif zdroj.endswith('.woff2'):
                varuj(f'sablona/{zdroj} chýba, web pôjde s náhradným písmom')
        if self.js:
            self.subory['app.js'] = self.js
        if self.nahlad:
            self.subory['robots.txt'] = b'User-agent: *\nDisallow: /\n'
        else:
            self.subory['robots.txt'] = f'User-agent: *\nAllow: /\n\nSitemap: {self.domena}/sitemap.xml\n'.encode('utf-8')
            riadky = ['<?xml version="1.0" encoding="UTF-8"?>',
                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for cesta, lastmod in sorted(self.mapa_stranok):
                riadky.append(f'<url><loc>{esc(self.abs_url(cesta))}</loc>' + (f'<lastmod>{lastmod}</lastmod>' if lastmod else '') + '</url>')
            riadky.append('</urlset>')
            self.subory['sitemap.xml'] = ('\n'.join(riadky) + '\n').encode('utf-8')
            self.subory['CNAME'] = (urllib.parse.urlsplit(self.domena).hostname + '\n').encode('utf-8')
            self.subory['.nojekyll'] = b''

    def skontroluj_znaky_pisma(self):
        """Písmo Geist je podmnožina latinky (sablona/pismo-znaky.txt, nastroje/znaky_geist.py); znak mimo nej by
        sa vykreslil náhradným písmom, preto varovanie s prvou stránkou, kde je."""
        if self.znaky_pisma is None:
            return
        chybajuce = {}
        for cesta, data in sorted(self.subory.items()):
            if not cesta.endswith('.html') or cesta.startswith('index.php/'):
                continue
            p = _TextStranky()
            p.feed(data.decode('utf-8'))
            p.close()
            for ch in set(''.join(p.texty)) - self.znaky_pisma - {'\n', '\t', '\r'}:
                chybajuce.setdefault(ch, cesta)
        if chybajuce:
            varuj('písmo Geist nemá znaky ' + ' '.join(f'{c!r} ({p})' for c, p in sorted(chybajuce.items()))
                  + ' (sablona/pismo-znaky.txt)')

    def postav(self):
        self.uvod()
        self.stranky_sluzieb()
        self.stranka_realizacii()
        self.stranky_realizacie()
        self.stranky_noviniek()
        self.stranka_opravnenia()
        self.stranka_kontakt()
        self.stranka_ochrana()
        self.stranka_404()
        self.presmerovania()
        self.staticke()
        self.skontroluj_znaky_pisma()


# ---------------------------------------------------------------- kontrola výstupu

class _Zber(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texty = []

    def handle_data(self, data):
        self.texty.append(data)

    def handle_starttag(self, tag, attrs):
        for _, v in attrs:
            if v:
                self.texty.append(v)


def skontroluj_text(subory):
    chyby = []
    for cesta, data in subory.items():
        if not cesta.endswith(('.html', '.xml', '.txt', '.svg')) or cesta.startswith('fonts/'):
            continue
        s = data.decode('utf-8')
        if any(p in s for p in '\u2013\u2014'):
            chyby.append(f'{cesta}: pomlčka em alebo en')
        telo = s[len('<!doctype html>'):] if s.startswith('<!doctype html>') else s
        if '!' in telo:
            chyby.append(f'{cesta}: výkričník')
        if cesta.endswith('.html'):
            z = _Zber()
            z.feed(s)
            z.close()
    if chyby:
        raise ChybaZostavy('výstup neprešiel kontrolou textu: ' + '; '.join(chyby[:10]))


# ---------------------------------------------------------------- zápis

def bezpecna_rel(rel):
    kusy = rel.split('/')
    return rel and not rel.startswith('/') and all(k not in ('', '.', '..') for k in kusy) and '\\' not in rel


PAMAT_KODOVANIA = 64 * 1024 * 1024
_KODOVANE = {}


_KODOVANE_BAJTY = [0]


def _kluc_pamate(odt):
    # aj totožnosť kódovacích funkcií: testy ich nahrádzajú rýchlou náhradou, tá nesmie zostať v pamäti pre ostrý beh
    return f'{odt}|{id(webp_bajty)}|{id(avif_bajty)}'


def _zapamataj(odt, data):
    """Zakódované bajty podľa odtlačku (najstaršie von, keď súčet presiahne PAMAT_KODOVANIA)."""
    k = _kluc_pamate(odt)
    if k in _KODOVANE:
        return
    _KODOVANE[k] = data
    _KODOVANE_BAJTY[0] += len(data)
    while _KODOVANE and _KODOVANE_BAJTY[0] > PAMAT_KODOVANIA:
        _KODOVANE_BAJTY[0] -= len(_KODOVANE.pop(next(iter(_KODOVANE))))


def zapis(web, vystup):
    vystup.mkdir(parents=True, exist_ok=True)
    stary = {}
    m = vystup / MANIFEST
    if m.is_file():
        try:
            d = json.loads(m.read_text(encoding='utf-8'))
            if isinstance(d.get('subory'), dict):
                stary = {k: v for k, v in d['subory'].items() if isinstance(k, str) and bezpecna_rel(k)}
        except ValueError:
            varuj('výstupný .zostava.json je poškodený, nič sa nemaže')
    elif any(vystup.iterdir()):
        varuj(f'{vystup} nie je prázdny a nemá {MANIFEST}: cudzie súbory nechávam, prepíšem len vlastné')
    novy = {}
    plan = dict(web.subory)
    for rel, (odt, _) in web.obrazky.items():
        novy[rel] = odt
    for rel in plan:
        novy[rel] = hashlib.sha256(plan[rel]).hexdigest()
    # najprv vyrob všetky nové obrázky do pamäte; ak zlyhá čo i len jeden, predchádzajúci výstup ostane celý
    # (brána pokus 1, správnosť 2: useknutá fotka zmazala web a build spadol s OSError)
    obrazky = {}
    ulohy = {}
    for rel, (odt, funkcia) in sorted(web.obrazky.items()):
        if stary.get(rel) == odt and (vystup / rel).is_file():
            continue
        # rovnaký odtlačok = rovnaké bajty (tá istá fotka vo viacerých realizáciách, ilustračná fotka, ďalší beh
        # v tom istom procese): kóduje sa raz, pamäť najviac PAMAT_KODOVANIA bajtov
        if _kluc_pamate(odt) in _KODOVANE:
            obrazky[rel] = _KODOVANE[_kluc_pamate(odt)]
            continue
        ulohy.setdefault(odt, (funkcia, []))[1].append(rel)
    # kódovanie vo vláknach (Pillow pri kódovaní a zmene veľkosti uvoľní GIL, na PC asi 5-krát rýchlejšie);
    # každý súbor sa kóduje sám v jednom vlákne kodeku, takže bajty nezávisia od počtu vlákien
    with ThreadPoolExecutor(max_workers=max(1, min(16, os.cpu_count() or 1))) as ex:
        buduce = {ex.submit(funkcia): (odt, rely) for odt, (funkcia, rely) in ulohy.items()}
        for fut in as_completed(buduce):
            odt, rely = buduce[fut]
            try:
                data = fut.result()
            except Exception as e:
                for f in buduce:
                    f.cancel()
                raise ChybaZostavy(f'obrázok {rely[0]} sa nedá vyrobiť ({e.__class__.__name__}: {e}); '
                                   f'predchádzajúci výstup ostal bez zmeny')
            _zapamataj(odt, data)
            for rel in rely:
                obrazky[rel] = data
    # zmaž vlastný predchádzajúci výstup, ktorý v novom nie je alebo sa zmenil (obrázky s rovnakým odtlačkom ostanú)
    for rel, odt in stary.items():
        p = vystup / rel
        if (rel in web.obrazky or rel in plan) and odt == novy.get(rel) and p.is_file():
            continue
        if p.is_file():
            p.unlink()
    for rel, data in obrazky.items():
        p = vystup / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    kodovanych = len(obrazky)
    for rel, data in sorted(plan.items()):
        p = vystup / rel
        # rovnaké bajty sa neprepisujú (opakovaný build bez zmeny nič nezmení, ani čas súboru)
        if stary.get(rel) == novy[rel] and p.is_file() and p.stat().st_size == len(data) and p.read_bytes() == data:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    # prázdne priečinky po vlastných súboroch
    for rel in sorted(stary, key=lambda x: -x.count('/')):
        rodic = (vystup / rel).parent
        while rodic != vystup and rodic.is_dir() and not any(rodic.iterdir()):
            rodic.rmdir()
            rodic = rodic.parent
    zostava = (json.dumps({'verzia': 1, 'subory': dict(sorted(novy.items()))}, ensure_ascii=False, indent=0) + '\n').encode('utf-8')
    if not (m.is_file() and m.read_bytes() == zostava):
        m.write_bytes(zostava)
    return kodovanych


def main(argv=None):
    ap = argparse.ArgumentParser(description='Zostava statického webu BM Building z obsah/.')
    ap.add_argument('--nahlad', action='store_true', help='náhľad: noindex, robots Disallow, bez sitemap, CNAME a .nojekyll')
    ap.add_argument('--vystup', type=Path, default=KOREN / 'web', help='výstupný priečinok (predvolený web/)')
    ap.add_argument('--obsah', type=Path, default=KOREN / 'obsah', help='priečinok obsahu (predvolený obsah/)')
    ap.add_argument('--fotky-web', type=Path, default=FOTKY_WEB,
                    help='hotové webové verzie fotiek s manifest.json (predvolený fotky-web/)')
    a = ap.parse_args(argv)
    for prud in (sys.stdout, sys.stderr):
        try:
            prud.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    VAROVANIA.clear()
    PRISNE_VAROVANIA.clear()
    nahlad = a.nahlad
    v_actions = os.environ.get('GITHUB_ACTIONS') == 'true'
    # GitHub Actions (workflow linky bmb-obsah volá „python build.py“): ostrý beh len s BMB_OSTRY=ano, inak náhľad
    # s noindex, Disallow a 404 pod /bmbuilding/, kým doména nie je prepnutá (brána pokus 1, správnosť 1)
    if not nahlad and v_actions and os.environ.get('BMB_OSTRY', '').strip().lower() != 'ano':
        nahlad = True
        print('GitHub Actions bez BMB_OSTRY=ano: zostavujem náhľad (noindex). Ostrý web po prepnutí domény: BMB_OSTRY=ano.')
    vystup = a.vystup.resolve()
    obsah = a.obsah.resolve()
    if vystup in (KOREN, obsah, SABLONA) or vystup in obsah.parents or obsah in vystup.parents or vystup == Path(vystup.anchor):
        print(f'CHYBA: výstup {vystup} nesmie byť koreň, obsah ani šablóna', file=sys.stderr)
        return 2
    try:
        web = Web(obsah, nahlad, fotky_web=a.fotky_web.resolve())
        web.postav()
        skontroluj_text(web.subory)
        # fb-ostro A4 (OPONENT.md S2): v Actions bez fotky sekcie alebo realizácie pásu nič nezapísať a skončiť kódom 1;
        # krok nasadenia sa nespustí a na webe ostane posledná dobrá verzia
        if v_actions and PRISNE_VAROVANIA:
            raise ChybaZostavy('v GitHub Actions chýba fotka sekcie alebo realizácia pásu z nastavenia.json (nič som '
                               'nezapísal, nasadenie sa nespustí a ostane posledný dobrý web): '
                               + '; '.join(PRISNE_VAROVANIA[:10]))
        kodovanych = zapis(web, vystup)
    except ChybaZostavy as e:
        print('CHYBA: ' + str(e), file=sys.stderr)
        return 1
    stranky = sum(1 for k in web.subory if k.endswith('.html'))
    print(f'hotovo: {vystup} ({"náhľad" if nahlad else "ostrý"}), {stranky} HTML, {len(web.realizacie)} realizácií, '
          f'{len(web.obrazky)} obrázkov (nových {kodovanych}), varovaní {len(VAROVANIA)}')
    # súbory fotiek: hotové z fotky-web/ (kópia) a zakódované z obsah/ (nová alebo zmenená fotka, AVIF len s kodekom)
    print(f'fotky: z fotky-web {web.pocty_fotiek["fotky-web"]} súborov, z obsahu {web.pocty_fotiek["obsah"]} súborov, '
          f'AVIF {"áno" if web.avif else "nie (len WebP)"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
