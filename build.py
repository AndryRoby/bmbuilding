#!/usr/bin/env python3
"""Generátor statického webu BM Building (bmbuilding.sk).

Číta obsah/ (firma.json, realizacie/*.json, obrazky/, logo.png) a sablona/ (texty, štýl, presmerovania,
písmo pre OG) a zapíše hotový web do web/. Python 3 so štandardnou knižnicou a Pillow, bez siete,
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
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

KOREN = Path(__file__).resolve().parent
SABLONA = KOREN / 'sablona'
VERZIA_OBR = 'obr-1|webp-q80-m6|lanczos'
VERZIA_OG = 'og-1|jpeg-q82'
# 720: telefóny s DPR do 1,8 (Lighthouse mobil 412 px x 1,75) brali 960 a LCP realizácie s fotkami bolo 2,6 s (kolo 1)
SIRKY = (480, 720, 960, 1440)
MAX_VSTUP = 2000
OG_W, OG_H = 1200, 630
MANIFEST = '.zostava.json'
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
DATUM_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
POMLCKY = '\u2012\u2013\u2014\u2015\u2212'

FARBA_BM = (0x19, 0x44, 0x71)
FARBA_NA_BM = (0xF6, 0xF5, 0xF2)
FARBA_NA_BM_2 = (0xC9, 0xD4, 0xE3)

SVG_TEL = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" '
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 '
           '6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2"/></svg>')
SVG_SIPKA = ('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
             'aria-hidden="true"><path d="M2 8h11M9 4l4 4-4 4"/></svg>')
FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#194471"/>'
               '<text x="32" y="34" font-family="Arial,Helvetica,sans-serif" font-size="28" font-weight="700" '
               'fill="#fff" text-anchor="middle">BM</text><rect x="14" y="41" width="36" height="2.5" fill="#fff"/></svg>\n')

CISLA = ' class="cisla"'
# oddeľovač „·“ s pevnou medzerou pred sebou: pri zalomení ostane na konci riadka, nezačne nový
# (brána dizajnu pokus 2, nález 3)
BODKA = '\u00a0· '

VAROVANIA = []


def varuj(sprava):
    VAROVANIA.append(sprava)
    print('VAROVANIE: ' + sprava, file=sys.stderr)


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
    s = re.sub(r'\b(č\.|ev\.|Ing\.|arch\.|§|ods\.|písm\.) ', '\\1\u00a0', s)
    s = re.sub(r'(§) ', '\\1\u00a0', s)
    return s


def T(s):
    """Text do HTML (escapovaný a so sadzbou)."""
    return sadzba(esc(s))


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


def sirky_pre(w):
    out = [s for s in SIRKY if s < w]
    if w <= SIRKY[-1] and w not in out:
        out.append(w)
    if not out:
        out = [SIRKY[-1]]
    return sorted(out)


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
        z = d.get('zverejnene', True)
        if z is False or (isinstance(z, str) and z.strip().lower() in ('false', 'nie', '0', '')) or z == 0 or z is None:
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
        fotky = []
        zoznam = d.get('fotky') if isinstance(d.get('fotky'), list) else []
        for f in zoznam:
            if isinstance(f, str):
                f = {'subor': f}
            if not isinstance(f, dict):
                continue
            p = cesta_fotky(obsah, f.get('subor'))
            if p is None or not p.is_file():
                varuj(f'{slug}: fotka {f.get("subor")!r} chýba alebo je mimo obsah/obrazky, vynechaná')
                continue
            try:
                w, h = rozmery_zdroja(p)
            except Exception:
                varuj(f'{slug}: fotka {f.get("subor")!r} sa nedá otvoriť, vynechaná')
                continue
            fotky.append({'zdroj': p, 'w': w, 'h': h, 'alt': jeden_riadok(f.get('alt'))})
        for i, f in enumerate(fotky, 1):
            f['nn'] = f'{i:02d}'
            f['sirky'] = sirky_pre(f['w'])
            f['max_w'] = f['sirky'][-1]
            f['max_h'] = max(1, round(f['h'] * f['max_w'] / f['w']))
            if not f['alt']:
                f['alt'] = ', '.join(x for x in (nazov, jeden_riadok(d.get('miesto'))) if x)
        r = {
            'slug': slug,
            'nazov': nazov,
            'miesto': jeden_riadok(d.get('miesto')),
            'rola': zaciatok_velky(jeden_riadok(d.get('rola'))),
            'projektant': jeden_riadok(d.get('projektant')),
            'roky': jeden_riadok(d.get('roky')),
            'investor': jeden_riadok(d.get('investor')),
            'zhotovitel': jeden_riadok(d.get('zhotovitel')),
            'text': cisty(d.get('text')),
            'zdroj': jeden_riadok(d.get('zdroj')),
            'fb_url': bezpecna_url(d.get('fb_url'), ['facebook.com', 'fb.com']),
            'datum': datum,
            'upravene': upravene,
            # titulok (nepovinné): vlastný title stránky realizácie, ak sa názov nezmestí do 60 znakov
            'titulok': jeden_riadok(d.get('titulok')),
            'poradie': poradie,
            'fotky': fotky,
        }
        if len(r['titulok']) > 60:
            # title nad 60 znakov Google skráti; vlastný titulok sa vtedy nepoužije (brána pokus 2, správnosť 1)
            varuj(f'{subor.name}: titulok má {len(r["titulok"])} znakov, najviac 60, nepoužitý')
            r['titulok'] = ''
        slugy.add(slug)
        vsetky.append(r)
    # poradie vzostupne, potom datum zostupne, potom slug
    vsetky.sort(key=lambda r: r['slug'])
    vsetky.sort(key=lambda r: r['datum'], reverse=True)
    vsetky.sort(key=lambda r: r['poradie'])
    return vsetky


# ---------------------------------------------------------------- obrázky

def otvor_rgb(p):
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGBA')
            podklad = Image.new('RGB', im.size, (0xEC, 0xEB, 0xE6))
            podklad.paste(im, mask=im.split()[-1])
            im = podklad
        else:
            im = im.convert('RGB')
        if max(im.size) > MAX_VSTUP:
            k = MAX_VSTUP / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        return im.copy()


def webp_bajty(p, sirka, vyska):
    im = otvor_rgb(p)
    if im.size != (sirka, vyska):
        im = im.resize((sirka, vyska), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=80, method=6)
    return buf.getvalue()


def odtlacok_suboru(p, *dalsie):
    h = hashlib.sha256()
    h.update(Path(p).read_bytes())
    for x in dalsie:
        h.update(b'|' + str(x).encode('utf-8'))
    return h.hexdigest()


def pismo(vaha, velkost):
    cesta = SABLONA / f'archivo-{vaha}.ttf'
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
        while posledny and draw.textlength(posledny + ' \u2026', font=font) > sirka:
            posledny = posledny.rsplit(' ', 1)[0] if ' ' in posledny else posledny[:-1]
        riadky[-1] = posledny + ' \u2026'
    return riadky


def kresli_znak(draw, x, y, velkost, font_znak):
    """Znak podľa loga: biele BM, pod ním biela čiara (na modrej ploche)."""
    draw.text((x, y), 'BM', font=font_znak, fill=FARBA_NA_BM)
    bb = draw.textbbox((x, y), 'BM', font=font_znak)
    draw.rectangle((x, bb[3] + velkost // 10, x + (bb[2] - x), bb[3] + velkost // 10 + max(2, velkost // 24)), fill=FARBA_NA_BM)
    return bb[3] + velkost // 10 + max(2, velkost // 24)


def og_bajty(foto, nadpis, riadok2, riadok3):
    platno = Image.new('RGB', (OG_W, OG_H), FARBA_BM)
    draw = ImageDraw.Draw(platno)
    f600 = pismo(600, 46)
    if foto is not None:
        im = otvor_rgb(foto)
        im = ImageOps.fit(im, (720, OG_H), Image.LANCZOS, centering=(0.5, 0.5))
        platno.paste(im, (0, 0))
        x0, sirka, pad = 720 + 48, OG_W - 720 - 96, 48
    else:
        x0, sirka, pad = 80, OG_W - 160, 72
    if f600 is None:
        # bez písma: len fotka a znak z obdĺžnikov
        draw.rectangle((x0, pad, x0 + 120, pad + 4), fill=FARBA_NA_BM)
        buf = io.BytesIO()
        platno.save(buf, 'JPEG', quality=82, optimize=True)
        return buf.getvalue()
    f_znak = pismo(600, 56 if foto is not None else 72)
    f400 = pismo(400, 24 if foto is not None else 30)
    y = kresli_znak(draw, x0, pad - 8, 56 if foto is not None else 72, f_znak)
    draw.text((x0, y + 10), 'BUILDING', font=pismo(600, 16 if foto is not None else 20), fill=FARBA_NA_BM_2)
    velkost = 46 if foto is not None else 64
    f_nadpis = pismo(600, velkost)
    riadky = zalom(draw, nadpis, f_nadpis, sirka, 5 if foto is not None else 3)
    if foto is not None and len(riadky) >= 5:
        f_nadpis = pismo(600, 38)
        riadky = zalom(draw, nadpis, f_nadpis, sirka, 6)
    vyska_r = int(f_nadpis.size * 1.12)
    y = 200 if foto is not None else 230
    for r in riadky:
        draw.text((x0, y), r, font=f_nadpis, fill=FARBA_NA_BM)
        y += vyska_r
    y += 16
    for r in zalom(draw, riadok2, f400, sirka, 2) if riadok2 else []:
        draw.text((x0, y), r, font=f400, fill=FARBA_NA_BM_2)
        y += int(f400.size * 1.3)
    if riadok3:
        f_spodok = pismo(400, 22 if foto is not None else 26)
        draw.line((x0, OG_H - pad - 44, x0 + sirka, OG_H - pad - 44), fill=FARBA_NA_BM, width=2)
        draw.text((x0, OG_H - pad - 30), riadok3, font=f_spodok, fill=FARBA_NA_BM)
    buf = io.BytesIO()
    platno.save(buf, 'JPEG', quality=82, optimize=True)
    return buf.getvalue()


def favicon_ico_bajty():
    im = Image.new('RGB', (64, 64), FARBA_BM)
    d = ImageDraw.Draw(im)
    f = pismo(600, 27)
    if f is not None:
        d.text((32, 30), 'BM', font=f, fill=(255, 255, 255), anchor='ms')
    d.rectangle((14, 40, 49, 42), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, 'ICO', sizes=[(32, 32), (16, 16)])
    return buf.getvalue()


# ---------------------------------------------------------------- HTML

class Web:
    def __init__(self, obsah, nahlad):
        self.obsah = obsah
        self.nahlad = nahlad
        self.nastavenia = json.loads((SABLONA / 'nastavenia.json').read_text(encoding='utf-8'))
        self.texty = json.loads((SABLONA / 'texty.json').read_text(encoding='utf-8'))
        self.css_sablona = (SABLONA / 'styl.css').read_text(encoding='utf-8').replace('\r\n', '\n')
        self.domena = self.nastavenia.get('domena', 'https://bmbuilding.sk').rstrip('/')
        self.firma = nacitaj_firmu(obsah)
        self.realizacie = nacitaj_realizacie(obsah)
        self.subory = {}      # relatívna cesta -> bajty
        self.obrazky = {}     # relatívna cesta -> (odtlačok, funkcia)
        self.mapa_stranok = []  # (cesta, lastmod)
        # lastmod (brána pokus 1, správnosť 6): vydanie = deň, keď sa naposledy zmenila šablóna alebo texty stránok
        # (nastavenia.json); realizácia = najnovší z vydania, dátumu príspevku a poľa upravene
        self.vydanie = platny_datum(self.nastavenia.get('vydanie'))
        if self.nastavenia.get('vydanie') and not self.vydanie:
            varuj('sablona/nastavenia.json: vydanie nie je platný dátum RRRR-MM-DD')
        datumy = sorted(self.zmena(r) for r in self.realizacie if self.zmena(r))
        self.najnovsi = max([d for d in (datumy[-1] if datumy else '', self.vydanie) if d], default='')
        # poradie v mriežkach a v navigácii: najprv s fotkou, potom fotky len s pôvodným stavom, potom bez fotky;
        # vnútri skupiny stabilne podľa poradie (brána dizajnu pokus 1, nálezy 1 a 3)
        self.zobrazenie = sorted(self.realizacie, key=self.skupina)
        self.logo = (obsah / 'logo.png') if (obsah / 'logo.png').is_file() else None

    def zmena(self, r):
        return max((d for d in (r['datum'], r['upravene'], self.vydanie) if d), default='')

    @staticmethod
    def povodny_stav(f):
        return 'pôvodný stav' in f['alt'].lower()

    def skupina(self, r):
        if not r['fotky']:
            return 2
        return 1 if self.povodny_stav(r['fotky'][0]) else 0

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

    def obrazok_cesty(self, r, f):
        return {s: f'obrazky/{r["slug"]}/{f["nn"]}-{s}.webp' for s in f['sirky']}

    def registruj_fotku(self, r, f):
        for s, cesta in self.obrazok_cesty(r, f).items():
            if cesta in self.obrazky:
                continue
            v = max(1, round(f['h'] * s / f['w']))
            odt = odtlacok_suboru(f['zdroj'], VERZIA_OBR, s, v)
            self.obrazky[cesta] = (odt, (lambda p=f['zdroj'], s=s, v=v: webp_bajty(p, s, v)))

    def img(self, pre, r, f, sizes, lcp=False, trieda=''):
        self.registruj_fotku(r, f)
        cesty = self.obrazok_cesty(r, f)
        srcset = ', '.join(f'{esc(pre + c)} {s}w' for s, c in cesty.items())
        stred = [s for s in f['sirky'] if s <= 960]
        src = cesty[stred[-1] if stred else f['sirky'][0]]
        nacit = 'fetchpriority="high"' if lcp else 'loading="lazy" decoding="async"'
        t = f' class="{trieda}"' if trieda else ''
        return (f'<img{t} src="{esc(pre + src)}" srcset="{srcset}" sizes="{sizes}" width="{f["max_w"]}" '
                f'height="{f["max_h"]}" alt="{esc(f["alt"])}" {nacit}>')

    def og_cesta_realizacie(self, r):
        return f'og/{r["slug"]}.jpg'

    def registruj_og(self, cesta, foto, nadpis, riadok2, riadok3):
        pismo_odt = ''
        for v in (400, 600):
            p = SABLONA / f'archivo-{v}.ttf'
            pismo_odt += hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else 'x'
        if foto is not None:
            odt = odtlacok_suboru(foto, VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)
        else:
            odt = hashlib.sha256('|'.join((VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)).encode()).hexdigest()
        self.obrazky[cesta] = (odt, (lambda: og_bajty(foto, nadpis, riadok2, riadok3)))

    def mesto(self, r):
        return r['miesto'].split(',')[0].strip() if r['miesto'] else ''

    def nazov_s_miestom(self, r):
        m = self.mesto(r)
        if m and m.lower() not in r['nazov'].lower():
            return f'{r["nazov"]}, {m}'
        return r['nazov']

    # --- kostra stránky
    def menu(self, pre, aktivne):
        def a(ciel, text, kluc):
            cur = ''
            if aktivne == kluc:
                cur = ' aria-current="page"'
            elif aktivne and kluc and aktivne.startswith(kluc + '/'):
                cur = ' aria-current="true"'
            return f'<a href="{esc(self.rel(pre, ciel))}"{cur}>{T(text)}</a>'
        sluzby_cur = aktivne in self.texty['sluzby']
        d = [f'<a href="{esc(self.rel(pre, "#sluzby"))}"' + (' aria-current="true"' if sluzby_cur else '') + '>Služby</a>',
             a('realizacie/', 'Realizácie', 'realizacie'), a('opravnenia/', 'Oprávnenia', 'opravnenia'),
             a('kontakt/', 'Kontakt', 'kontakt')]
        m = [a(f'{k}/', v['menu'], k) for k, v in self.texty['sluzby'].items()]
        m += [a('realizacie/', 'Realizácie', 'realizacie'), a('opravnenia/', 'Oprávnenia', 'opravnenia'),
              a('kontakt/', 'Kontakt', 'kontakt')]
        f = self.firma
        tel = ''
        if f['tel_href']:
            kratke = f['tel'].replace('+421 ', '0') if f['tel'].startswith('+421 ') else f['tel']
            # prístupné meno = „Zavolať“ + viditeľné číslo (aria-label s iným tvarom čísla nesedel s viditeľným textom,
            # Lighthouse label-content-name-mismatch, WCAG 2.5.3); skrytý span s display:none sa do mena nepočíta
            tel = (f'<a class="tel" href="tel:{esc(f["tel_href"])}">{SVG_TEL}<span class="skryte">Zavolať </span>'
                   f'<span class="tel-kratke">{T(kratke)}</span><span class="tel-dlhe">{T(f["tel"])}</span></a>')
        return ('<header class="hlavicka">\n<div class="obsah hlavicka-riadok">\n'
                f'<a class="znak" href="{esc(self.rel(pre, ""))}" aria-label="BM Building, úvod"><span class="znak-stvorec" aria-hidden="true">BM</span><span class="znak-meno" aria-hidden="true">BM Building</span></a>\n'
                f'<nav class="menu-d" aria-label="Hlavná navigácia">{"".join(d)}</nav>\n'
                f'{tel}\n'
                '<details class="menu-m"><summary><span class="menu-ikona" aria-hidden="true"><i></i><i></i><i></i></span>Menu</summary>'
                f'<nav class="menu-panel" aria-label="Hlavná navigácia v menu"><div class="obsah">{"".join(m)}</div></nav></details>\n'
                '</div>\n</header>\n')

    def pata(self, pre, stlpec_firma=True):
        """stlpec_firma=False na /kontakt/, kde je tá istá tabuľka firmy v tele (brána dizajnu pokus 1, nález 12)."""
        f = self.firma
        adresa = ', '.join(x for x in (f['ulica'], ' '.join(y for y in (f['psc'], f['mesto']) if y)) if x)
        kontakt = []
        if f['tel_href']:
            kontakt.append(f'<li><a href="tel:{esc(f["tel_href"])}">{T(f["tel"])}</a></li>')
        if f['email']:
            kontakt.append(f'<li><a href="mailto:{esc(f["email"])}">{T(f["email"])}</a></li>')
        stranky = [(f'{k}/', v['menu']) for k, v in self.texty['sluzby'].items()]
        # Ochrana osobných údajov je v spodnom riadku päty; v zozname stránok bola druhýkrát (snímky kolo 1)
        stranky += [('realizacie/', 'Realizácie'), ('opravnenia/', 'Oprávnenia'), ('kontakt/', 'Kontakt')]
        pole = [('Obchodné meno', f['meno']), ('Sídlo', adresa), ('IČO', f['ico']), ('IČ DPH', f['ic_dph']),
                ('Zápis', f['zapis'])]
        pole_html = ''.join(f'<dt>{T(k)}</dt><dd{CISLA if k in ("IČO", "IČ DPH") else ""}>{T(v)}</dd>'
                            for k, v in pole if v)
        rok = self.najnovsi[:4]
        spodok = [f'<span>© {T(rok + " " if rok else "")}{T(f["meno"])}</span>']
        if f['facebook']:
            spodok.append(f'<a href="{esc(f["facebook"])}">Facebook</a>')
        spodok.append(f'<a href="{esc(self.rel(pre, "ochrana-udajov/"))}">Ochrana osobných údajov</a>')
        return ('<footer class="pata">\n<div class="obsah">\n<div class="pata-stlpce">\n'
                f'<div><h2>Kontakt</h2><ul>{"".join(kontakt)}</ul><p>{T(adresa)}</p></div>\n'
                '<nav aria-label="Stránky v päte"><h2>Stránky</h2><ul>'
                + ''.join(f'<li><a href="{esc(self.rel(pre, c))}">{T(t)}</a></li>' for c, t in stranky)
                + '</ul></nav>\n'
                + (f'<div><h2>Firma</h2><dl class="pole">{pole_html}</dl></div>\n' if stlpec_firma else '')
                + f'</div>\n<div class="pata-spodok">{"".join(spodok)}</div>\n</div>\n</footer>\n')

    def blok_kontakt(self, pre, nadpis_h1=None):
        f = self.firma
        adresa = ', '.join(x for x in (f['ulica'], ' '.join(y for y in (f['psc'], f['mesto']) if y)) if x)
        dotaz = urllib.parse.quote_plus(adresa)
        gmaps = f'https://www.google.com/maps/search/?api=1&query={dotaz}'
        mapy = f'https://mapy.cz/zakladni?q={urllib.parse.quote(adresa)}'
        casti = []
        if nadpis_h1 is None:
            casti.append('<h2 class="h2" id="kontakt-nadpis">Kontakt</h2>')
        if f['tel_href']:
            casti.append(f'<a class="kontakt-tel" href="tel:{esc(f["tel_href"])}">{T(f["tel"])}</a><br>')
        if f['email']:
            casti.append(f'<a class="kontakt-mail" href="mailto:{esc(f["email"])}">{T(f["email"])}</a>')
        akcie = []
        if f['tel_href']:
            akcie.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">{SVG_TEL}Zavolať</a>')
        if f['email']:
            akcie.append(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{esc(f["email"])}">Napísať e-mail</a>')
        if akcie:
            casti.append(f'<div class="kontakt-akcie">{"".join(akcie)}</div>')
        kto = f['meno'] + (f', {f["konatel"]}, konateľ' if f['konatel'] else '')
        casti.append(f'<address class="kontakt-adresa">{T(kto)}<br>{T(adresa)}<br>'
                     f'<span class="kontakt-mapy"><a class="odkaz" href="{esc(gmaps)}">Otvoriť v Mapách Google</a>'
                     f'<a class="odkaz" href="{esc(mapy)}">Otvoriť na Mapy.cz</a></span></address>')
        return '\n'.join(casti)

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
            'image': self.abs_url('og/bmbuilding.jpg'),
            'address': {'@type': 'PostalAddress', 'streetAddress': f['ulica'], 'postalCode': f['psc'],
                        'addressLocality': f['mesto'], 'addressCountry': 'SK'},
            'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'},
                           {'@type': 'AdministrativeArea', 'name': f['posobnost']}],
            'knowsAbout': [v['menu'] for v in self.texty['sluzby'].values()] + ['Technický dozor investora (TDI)'],
        }
        if self.logo:
            uzol['logo'] = self.abs_url('logo.png')
        if f['tel']:
            uzol['telephone'] = f['tel']
        if f['email']:
            uzol['email'] = f['email']
        if f['facebook']:
            uzol['sameAs'] = [f['facebook']]
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
                og_typ='website', do_mapy=True, lastmod=None, zaklad=None, pata_firma=True):
        # zaklad (len 404): absolútne cesty od koreňa webu („/“ alebo „/bmbuilding/“) namiesto <base>; s <base> by
        # „Preskočiť na obsah“ (#obsah) viedlo na úvod (brána pokus 2, správnosť 8)
        pre = zaklad if zaklad else self.pre(cesta)
        css = self.css_sablona.replace('@PRE@', pre)
        ld = ld_json({'@context': 'https://schema.org', '@graph': ld_uzly}) if ld_uzly else ''
        # connect-src 'self': Lighthouse a PageSpeed Insights čítajú robots.txt cez stránku; pri default-src 'none'
        # ho CSP zablokovala a SEO audit robots-txt padal (92 namiesto 100, kontrola kolo 1). Web nemá spustiteľný skript.
        csp = ["default-src 'none'", "img-src 'self'", "font-src 'self'", "connect-src 'self'", 'style-src ' + sha(css)]
        if ld:
            csp.append('script-src ' + sha(ld))
        csp += ["base-uri 'none'", "form-action 'none'"]
        canon = self.abs_url(cesta or '')
        h = ['<!doctype html>', '<html lang="sk">', '<head>', '<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             # CSP s doslovnými apostrofmi (len pevné kľúčové slová a base64 hashe, žiadny obsah z JSON)
             f'<meta http-equiv="Content-Security-Policy" content="{"; ".join(csp)}">']
        if self.nahlad:
            h.append('<meta name="robots" content="noindex, nofollow">')
        h += ['<meta name="color-scheme" content="light">', '<meta name="theme-color" content="#F6F5F2">',
              f'<title>{esc(title)}</title>']
        if description:
            h.append(f'<meta name="description" content="{esc(description)}">')
        if cesta is not None and not zaklad:
            h.append(f'<link rel="canonical" href="{esc(canon)}">')
        h += [f'<link rel="icon" href="{esc(pre)}favicon.svg" type="image/svg+xml">',
              f'<link rel="icon" href="{esc(pre)}favicon.ico" sizes="32x32">',
              f'<link rel="preload" href="{esc(pre)}fonts/archivo-400-700.woff2" as="font" type="font/woff2" crossorigin>']
        og_w, og_h = OG_W, OG_H
        h += ['<meta property="og:locale" content="sk_SK">', f'<meta property="og:type" content="{og_typ}">',
              '<meta property="og:site_name" content="BM Building">',
              f'<meta property="og:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta property="og:description" content="{esc(description)}">')
        if not zaklad:
            h.append(f'<meta property="og:url" content="{esc(canon)}">')
        h += [f'<meta property="og:image" content="{esc(self.abs_url(og_obr))}">',
              f'<meta property="og:image:width" content="{og_w}">', f'<meta property="og:image:height" content="{og_h}">',
              '<meta name="twitter:card" content="summary_large_image">',
              f'<meta name="twitter:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta name="twitter:description" content="{esc(description)}">')
        h.append(f'<meta name="twitter:image" content="{esc(self.abs_url(og_obr))}">')
        h.append(f'<style>{css}</style>')
        if ld:
            h.append(f'<script type="application/ld+json">{ld}</script>')
        h += ['</head>', '<body>', '<a class="preskocit" href="#obsah">Preskočiť na obsah</a>',
              self.menu(pre, aktivne) + f'<main id="obsah">\n{telo}\n</main>\n' + self.pata(pre, pata_firma) + '</body>', '</html>', '']
        subor = (cesta + 'index.html') if cesta is not None else '404.html'
        self.subory[subor] = '\n'.join(h).encode('utf-8')
        if do_mapy and cesta is not None:
            self.mapa_stranok.append((cesta, platny_datum(lastmod) or self.najnovsi))

    # --- časti
    def karta(self, pre, r, nadpis='h3', lcp=False):
        """Karta s fotkou do mriežky. Realizácie bez fotky idú do zoznam_dalsich, nie na modrú dosku
        (brána dizajnu pokus 1, nálezy 1 a 2); riadok s počtom fotiek v karte nie je (nález 9)."""
        url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
        f = r['fotky'][0]
        trieda = 'foto na-vysku' if f['h'] > f['w'] else 'foto'
        stitok = ''
        if self.povodny_stav(f):
            # fotky len pôvodného stavu (Okánikova): viditeľne, nielen v alt (brána dizajnu pokus 1, nález 3)
            stitok = '<span class="stitok">Pôvodný stav pred rekonštrukciou</span>'
        plocha = (f'<div class="{trieda}">' + self.img(
            pre, r, f, '(min-width:1024px) 411px, (min-width:640px) calc(50vw - 44px), calc(100vw - 32px)', lcp=lcp) + f'{stitok}</div>')
        pole = []
        if r['miesto']:
            pole.append(('Miesto', r['miesto'], ''))
        if r['rola']:
            pole.append(('BM Building', r['rola'], ''))
        if r['projektant']:
            pole.append(('Projektant', r['projektant'], ''))
        if r['roky']:
            pole.append(('Termín', r['roky'], ' class="cisla"'))
        dl = ''.join(f'<dt>{T(k)}</dt><dd{c}>{T(v)}</dd>' for k, v, c in pole)
        dl = f'<dl class="pole">{dl}</dl>' if dl else ''
        return f'<li class="karta">{plocha}<{nadpis} class="karta-nazov"><a href="{url}">{T(r["nazov"])}</a></{nadpis}>{dl}</li>'

    def udaje_riadok(self, r):
        """Miesto · rola · roky · príspevok na Facebooku (bez pomlčiek; prázdne časti vynechá)."""
        casti = [T(x) for x in (r['miesto'], r['rola']) if x]
        if r['roky']:
            casti.append(f'<span class="cisla">{T(r["roky"])}</span>')
        if r['zdroj'] == 'fb' and r['datum']:
            casti.append(T(f'príspevok na Facebooku {datum_text(r["datum"])}'))
        return BODKA.join(casti)

    def mriezka(self, pre, zoznam, nadpis='h3', dalsie_nadpis='h3', max_kariet=None, max_dalsich=None, prva_lcp=False,
                zvysok_do_zoznamu=False):
        """Mriežka kariet s fotkou a pod ňou kompaktný zoznam realizácií bez fotky.

        zvysok_do_zoznamu: karty s fotkou nad max_kariet idú na začiatok zoznamu „Ďalšie realizácie“, aby stránka
        služby odkazovala na každú realizáciu s danou rolou (RD Pezinok na /stavebny-dozor/; brána pokus 2, správnosť 2)."""
        s_fotkou = [r for r in zoznam if r['fotky']]
        bez = [r for r in zoznam if not r['fotky']]
        if max_kariet is not None:
            if zvysok_do_zoznamu:
                bez = s_fotkou[max_kariet:] + bez
            s_fotkou = s_fotkou[:max_kariet]
        if max_dalsich is not None:
            bez = bez[:max_dalsich]
        out = ''
        if s_fotkou:
            # prvá karta na /realizacie/ je na mobile v prvej obrazovke a je LCP: bez lazy, s fetchpriority (kolo 2)
            out += '<ul class="karty">' + ''.join(self.karta(pre, r, nadpis, lcp=prva_lcp and i == 0)
                                                  for i, r in enumerate(s_fotkou)) + '</ul>'
        if bez:
            polozky = []
            for r in bez:
                url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
                riadok = self.udaje_riadok(r)
                polozky.append(f'<li><a class="dalsie-nazov" href="{url}">{T(r["nazov"])}</a>'
                               + (f'<span class="dalsie-udaje">{riadok}</span>' if riadok else '') + '</li>')
            nad = (f'<{dalsie_nadpis} class="h3 dalsie-nadpis">Ďalšie realizácie</{dalsie_nadpis}>' if s_fotkou else '')
            out += f'<div class="dalsie">{nad}<ul class="zoznam-dalsie">{"".join(polozky)}</ul></div>'
        return out

    def doklady(self, pre, cinnosti=None, nadpis='h3'):
        out = []
        for o in self.firma['opravnenia']:
            if cinnosti and o['cinnost'] not in cinnosti:
                continue
            # nadpis = činnosť a zameranie, aby sa dva doklady SKSI dali rozlíšiť (brána dizajnu pokus 1, nález 10)
            hlavicka = ', '.join(x for x in (zaciatok_velky(o['cinnost']), o['zameranie']) if x) or o['nazov'] or 'Osvedčenie'
            riadky = [('Vydala', o['organizacia']), ('Doklad', o['nazov']), ('Meno', o['osoba']), ('Činnosť', o['cinnost']),
                      ('Odborné zameranie', o['zameranie']), ('Evidenčné číslo', o['cislo']), ('Vydané', o['datum'])]
            dl = ''.join(f'<dt>{T(k)}</dt><dd{CISLA if k in ("Evidenčné číslo", "Vydané") else ""}>{T(v)}</dd>'
                         for k, v in riadky if v)
            out.append(f'<div class="doklad"><{nadpis}>{T(hlavicka)}</{nadpis}><dl class="pole">{dl}</dl></div>')
        return out

    def otazky_html(self, kluce):
        z = self.texty['zakon']
        out = []
        for k in kluce:
            q = self.texty['otazky'].get(k)
            if not q:
                continue
            out.append(f'<div><h3>{T(q["otazka"])}</h3><p>{T(q["odpoved"])}</p>'
                       f'<p class="zdroj">Zdroj: <a href="{esc(z["url"])}">{T(z["nazov"])}, {T(q["zdroj"])}</a></p></div>')
        return ''.join(out)

    # --- stránky
    def uvod(self):
        t = self.texty['uvod']
        pre = ''
        f = self.firma
        # úvodná fotka: nastavená realizácia, inak prvá zverejnená s fotkou na šírku aspoň 960 px
        hrdina = None
        def vhodna(r):
            for fo in r['fotky']:
                if fo['w'] >= fo['h'] and fo['w'] >= 960:
                    return fo
            return None
        poradie = [r for r in self.realizacie if r['slug'] == self.nastavenia.get('uvod_realizacia')] + self.zobrazenie
        for r in poradie:
            fo = vhodna(r)
            if fo:
                hrdina = (r, fo)
                break
        if hrdina:
            r, fo = hrdina
            popis = [f'<a href="realizacie/{esc(r["slug"])}/">{T(self.nazov_s_miestom(r))}</a>']
            # rola ako v obsahu a nad H1 detailu (veľké písmeno, „·“ pred rokmi; brána dizajnu pokus 2, nález 7)
            udaje = [T(r['rola'])] if r['rola'] else []
            if r['roky']:
                udaje.append(f'<span class="cisla">{T(r["roky"])}</span>')
            if udaje:
                popis.append(f'<span><span>{BODKA.join(udaje)}</span></span>')
            foto = (f'<figure class="uvod-foto"><div class="foto">'
                    + self.img(pre, r, fo, '(min-width:1024px) min(845px, 66vw), 100vw', lcp=True)
                    + f'</div><figcaption class="pole-riadok">{"".join(popis)}</figcaption></figure>')
            og_foto = fo['zdroj']
        else:
            foto = ('<figure class="uvod-foto"><div class="doska" aria-hidden="true"><span class="doska-udaj">od roku 2003</span>'
                    '<span class="doska-rola">osvedčenie SKSI, stavbyvedúci</span></div></figure>')
            og_foto = None
        self.registruj_og('og/bmbuilding.jpg', og_foto, 'Stavebný dozor, stavbyvedúci, stavebný manažment',
                          'Pezinok, Bratislava a okolie', 'bmbuilding.sk')
        n = len(self.realizacie)
        # lišta dôvery (brána dizajnu pokus 1, nález 5): roky osvedčení a zápisu do registra (FAKTY-WEB, ORSR)
        # podľa roku, potom počet realizácií od najstaršieho roku v poli roky; „sídlo Pezinok“ a „na webe“ nie
        roky_dovery = []
        for o in f['opravnenia']:
            if re.fullmatch(r'\d{4}', o['rok']):
                popis = 'osvedčenie SKSI, ' + o['cinnost'] + (', ' + o['zameranie'] if o['zameranie'] else '')
                roky_dovery.append((o['rok'], 'opravnenia/', popis))
        if platny_datum(f['den_zapisu']):
            roky_dovery.append((f['den_zapisu'][:4], 'opravnenia/#obchodny-register', 'spoločnosť v obchodnom registri'))
        dovera = [f'<div><a href="{c}"><b>{T(rok)}</b><span>{T(popis)}</span></a></div>'
                  for rok, c, popis in sorted(roky_dovery, key=lambda x: x[0])]
        if n:
            roky = [int(x) for r in self.realizacie for x in re.findall(r'\b(?:19|20)\d{2}\b', r['roky'])]
            popis = tvar(n, 'realizácia', 'realizácie', 'realizácií').split(' ', 1)[1] + (f' od roku {min(roky)}' if roky else '')
            dovera.append(f'<div><a href="realizacie/"><b>{n}</b><span>{T(popis)}</span></a></div>')
        akcie = []
        if f['tel_href']:
            akcie.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">{SVG_TEL}Zavolať <span class="cisla">{T(f["tel"])}</span></a>')
        if f['email']:
            akcie.append(f'<a class="odkaz" href="mailto:{esc(f["email"])}">Napísať e-mail</a>')
        sluzby = []
        for k, s in self.texty['sluzby'].items():
            sluzby.append(f'<li><h3><a href="{k}/">{T(s["menu"])}</a></h3><div class="sluzba-text"><p>{T(s["veta"])}</p>'
                          f'<p class="priklady">{T(s["priklady"])}</p></div>'
                          f'<span class="sipka" aria-hidden="true">Rozsah činností {SVG_SIPKA}</span></li>')
        pocet = int(self.nastavenia.get('uvod_pocet_realizacii', 6))
        # na úvode 6 kariet s fotkou bez realizácie z hlavnej fotky (brána dizajnu pokus 1, nálezy 1 a 4),
        # pod nimi najnovšie realizácie bez fotky ako kompaktný zoznam
        hrdina_slug = hrdina[0]['slug'] if hrdina else None
        zoznam = [r for r in self.zobrazenie if r['slug'] != hrdina_slug]
        karty = self.mriezka(pre, zoznam, max_kariet=pocet,
                             max_dalsich=int(self.nastavenia.get('uvod_pocet_dalsich', 3)))
        viac = ''
        if n > pocet:
            viac = f'<p class="viac"><a class="sipka" href="realizacie/">Všetky realizácie ({n}) {SVG_SIPKA}</a></p>'
        realizacie = ''
        if n:
            realizacie = (f'<section class="sekcia" aria-labelledby="realizacie-nadpis"><div class="obsah">'
                          f'<h2 class="h2" id="realizacie-nadpis">Realizácie</h2>{karty}{viac}</div></section>')
        doklady = self.doklady(pre)
        opr = ''
        if doklady:
            opr = (f'<section class="sekcia" aria-labelledby="opravnenia-nadpis"><div class="obsah">'
                   f'<h2 class="h2" id="opravnenia-nadpis">Oprávnenia</h2><div class="doklady">{"".join(doklady)}</div>'
                   f'<p class="viac"><a class="sipka" href="opravnenia/">Oprávnenia a osvedčenia {SVG_SIPKA}</a></p></div></section>')
        telo = (
            '<section class="uvod" aria-labelledby="uvod-nadpis"><div class="obsah mriezka">'
            f'<h1 id="uvod-nadpis">{T(t["h1"])} <span>{T(t["h1_miesto"])}</span></h1>'
            f'<div class="uvod-text"><p class="uvod-veta">{T(t["veta"])}</p><div class="uvod-akcie">{"".join(akcie)}</div></div>'
            f'{foto}<div class="dovera">{"".join(dovera)}</div></div></section>\n'
            f'<section class="sekcia" id="sluzby" aria-labelledby="sluzby-nadpis"><div class="obsah">'
            f'<h2 class="h2" id="sluzby-nadpis">Služby</h2><ul class="sluzby-zoznam">{"".join(sluzby)}</ul></div></section>\n'
            f'<section class="sekcia" aria-labelledby="firma-nadpis"><div class="obsah"><h2 class="h2" id="firma-nadpis">{T(t["o_firme_nadpis"])}</h2>'
            f'<div class="text">{"".join(f"<p>{T(p)}</p>" for p in t["o_firme"])}</div></div></section>\n'
            f'{realizacie}\n{opr}\n'
            f'<section class="sekcia" aria-labelledby="kontakt-nadpis"><div class="obsah blok-kontakt">{self.blok_kontakt(pre)}</div></section>'
        )
        uzly = [self.ld_firma(), {'@type': 'WebSite', '@id': self.abs_url('#web'), 'url': self.abs_url(''),
                                  'name': 'BM Building', 'inLanguage': 'sk', 'publisher': {'@id': self.abs_url('#firma')}},
                {'@type': 'WebPage', '@id': self.abs_url(''), 'url': self.abs_url(''), 'name': t['title'],
                 'isPartOf': {'@id': self.abs_url('#web')}, 'about': {'@id': self.abs_url('#firma')}, 'inLanguage': 'sk'}]
        self.stranka('', t['title'], t['description'], telo, '', uzly)

    def podstranka(self, cesta, kroky, h1, veta, telo, aktivne, title, description, uzly_navyse, og='og/bmbuilding.jpg',
                   og_typ='website', lastmod=None, kontakt=True, nad_h1='', pata_firma=True, typ_stranky='WebPage',
                   stranka_navyse=None):
        pre = self.pre(cesta)
        nad = f'<p class="nad-h1">{nad_h1}</p>' if nad_h1 else ''
        hlava = (f'<div class="obsah hlava">{self.omrvinky(pre, kroky)}{nad}<h1>{T(h1)}</h1>'
                 + (f'<p class="uvod-veta">{T(veta)}</p>' if veta else '') + '</div>')
        kont = (f'<section class="sekcia" aria-labelledby="kontakt-nadpis"><div class="obsah blok-kontakt">{self.blok_kontakt(pre)}</div></section>'
                if kontakt else '')
        uzly = [self.ld_firma(), self.ld_breadcrumb(kroky),
                {'@type': typ_stranky, '@id': self.abs_url(cesta), 'url': self.abs_url(cesta), 'name': title,
                 'inLanguage': 'sk', 'isPartOf': {'@id': self.abs_url('#web')}}]
        # jeden WebSite v grafe (uzol s @id #web je na úvode; brána pokus 2, správnosť 7)
        if stranka_navyse:
            uzly[-1].update(stranka_navyse)
        uzly += uzly_navyse
        self.stranka(cesta, title, description, hlava + '\n' + telo + '\n' + kont, aktivne, uzly, og_obr=og,
                     og_typ=og_typ, lastmod=lastmod, pata_firma=pata_firma)

    def stranky_sluzieb(self):
        for k, t in self.texty['sluzby'].items():
            cesta = f'{k}/'
            pre = self.pre(cesta)
            s = self.firma['sluzby'].get(k, {'nazov': t['menu'], 'popis': '', 'cinnosti': []})
            odseky = ''.join(f'<p>{T(p)}</p>' for p in t['odseky'])
            cinnosti = ''
            if s['cinnosti']:
                cinnosti = (f'<h2 class="h3">{T(t["cinnosti_nadpis"])}</h2><ul class="zoznam-cinnosti">'
                            + ''.join(f'<li>{T(c)}</li>' for c in s['cinnosti']) + '</ul>')
            # poradie (brána dizajnu pokus 2, nález 1): na mobile hneď pod H1 osvedčenie s tlačidlom Zavolať
            # (od 1024 px mriežkou vpravo), potom text a činnosti, realizácie a až za nimi otázky o zákone
            otazky = self.otazky_html(t['otazky'])
            if otazky:
                otazky = (f'<section class="sekcia" aria-labelledby="zakon-nadpis"><div class="obsah">'
                          f'<h2 class="h2" id="zakon-nadpis">Čo hovorí stavebný zákon</h2><div class="otazky">{otazky}</div></div></section>')
            # doklady v boku sú h2: bok je v DOM pred prvým h2 stránky, h3 by bol skok úrovne po h1
            doklady = self.doklady(pre, t['opravnenie'], nadpis='h2')
            bok = ''
            if doklady or self.firma['tel_href']:
                bok = '<aside class="sluzba-bok" aria-label="Oprávnenie">' + ''.join(doklady)
                if self.firma['tel_href']:
                    bok += f'<p class="bok-tlacidlo"><a class="tlacidlo" href="tel:{esc(self.firma["tel_href"])}">{SVG_TEL}Zavolať</a></p>'
                bok += '</aside>'
            suvisiace = [r for r in self.zobrazenie if any(kl in r['rola'].lower() for kl in t['kluce'])]
            real = ''
            if suvisiace:
                real = (f'<section class="sekcia" aria-labelledby="real-nadpis"><div class="obsah">'
                        f'<h2 class="h2" id="real-nadpis">{T(t["realizacie_nadpis"])}</h2>'
                        + self.mriezka(pre, suvisiace, max_kariet=6, zvysok_do_zoznamu=True)
                        + f'<p class="viac"><a class="sipka" href="../realizacie/">Všetky realizácie {SVG_SIPKA}</a></p></div></section>')
            telo = (f'<div class="obsah sluzba">{bok}<div class="sluzba-hlavne"><div class="text">{odseky}</div>{cinnosti}</div></div>\n'
                    f'{real}\n{otazky}')
            kroky = [('', 'Úvod'), (cesta, t['menu'])]
            sluzba = {'@type': 'Service', '@id': self.abs_url(cesta + '#sluzba'), 'name': t['h1'], 'serviceType': t['typ_sluzby'],
                      'description': t['description'], 'provider': {'@id': self.abs_url('#firma')},
                      'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'}],
                      'url': self.abs_url(cesta)}
            self.podstranka(cesta, kroky, t['h1'], t['veta'], telo, k, t['title'], t['description'], [sluzba])

    def stranka_realizacii(self):
        t = self.texty['realizacie']
        cesta = 'realizacie/'
        pre = self.pre(cesta)
        if self.realizacie:
            telo = ('<section class="sekcia" aria-label="Zoznam realizácií"><div class="obsah">'
                    + self.mriezka(pre, self.zobrazenie, nadpis='h2', dalsie_nadpis='h2', prva_lcp=True) + '</div></section>')
        else:
            telo = '<div class="obsah text"><p>Realizácie pripravujeme.</p></div>'
        kroky = [('', 'Úvod'), (cesta, 'Realizácie')]
        zoznam = {'@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'url': self.abs_url(f'realizacie/{r["slug"]}/'), 'name': r['nazov']}
            for i, r in enumerate(self.zobrazenie, 1)]}
        self.podstranka(cesta, kroky, t['h1'], t['veta'], telo, 'realizacie', t['title'], t['description'], [zoznam])

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

    def popis_fotky(self, r, f, predosly):
        """Viditeľný popis fotky z alt, len keď povie niečo nad názov a miesto stavby (napríklad „pôvodný stav“,
        „Vstup do Kina Lumière“); bez „fotka N z M“ a bez opakovania toho istého popisu pod susednou fotkou,
        okrem pôvodného stavu, ktorý stojí pod každou fotkou (brána dizajnu pokus 1, nález 3)."""
        s = re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')
        if not s:
            return ''
        zname = {w[:4] for w in re.findall(r'\w+', f'{r["nazov"]} {r["miesto"]}'.lower()) if len(w) >= 3}
        if not any(len(w) >= 3 and w[:4] not in zname for w in re.findall(r'\w+', s.lower())):
            return ''
        if s == predosly and not self.povodny_stav(f):
            return ''
        return s

    def foto_v_galerii(self, pre, r, f, sizes, popis, lcp=False):
        obr = self.img(pre, r, f, sizes, lcp=lcp)
        # odkaz len na väčšiu verziu, než je na stránke (zdroje zo starého webu majú najviac 960 px; nález 13)
        if f['max_w'] > 960:
            obr = f'<a href="{esc(pre + self.obrazok_cesty(r, f)[f["max_w"]])}">{obr}</a>'
        cap = f'<figcaption>{T(popis)}</figcaption>' if popis else ''
        return f'<figure>{obr}{cap}</figure>'

    def stranky_realizacie(self):
        poradie = self.zobrazenie
        for i, r in enumerate(poradie):
            cesta = f'realizacie/{r["slug"]}/'
            pre = self.pre(cesta)
            nm = self.nazov_s_miestom(r)
            # H1 je stavba s miestom; rola a roky v malom riadku nad ním (brána dizajnu pokus 1, nález 6)
            h1 = nm
            nad = [T(x) for x in (r['rola'],) if x] + ([f'<span class="cisla">{T(r["roky"])}</span>'] if r['roky'] else [])
            nad_h1 = BODKA.join(nad)
            title = self.titulok_realizacie(r)
            vety = [s_bodkou(nm)]
            if r['rola']:
                vety.append(s_bodkou(f'Rola BM Building: {male(r["rola"])}'))
            if r['projektant']:
                vety.append(s_bodkou(f'Projektant: {r["projektant"]}'))
            if r['roky']:
                vety.append(s_bodkou(f'Termín: {r["roky"]}'))
            desc = ''
            for v in vety:
                skus = (desc + ' ' + v).strip()
                if len(skus) <= 155:
                    desc = skus
                elif not desc:
                    desc = s_bodkou(skrat(v, 154))
            pole = [('Miesto', r['miesto']), ('BM Building', r['rola']), ('Projektant', r['projektant']),
                    ('Termín', r['roky']), ('Investor', r['investor']), ('Zhotoviteľ', r['zhotovitel'])]
            # dlhá hodnota (projektanti Okánikovej) ide pod 1024 px cez celý riadok, nie do polovičného stĺpca
            # so 7 riadkami (brána dizajnu pokus 2, nález 4)
            # krátka položka v ľavom stĺpci, za ktorou ide celý riadok alebo koniec, ide tiež cez celý riadok,
            # aby vedľa nej neostala prázdna polovica
            polozky = [[k, v, len(v) > 28] for k, v in pole if v]
            vlavo = True
            for j, polozka in enumerate(polozky):
                if polozka[2]:
                    vlavo = True
                elif vlavo and (j + 1 == len(polozky) or polozky[j + 1][2]):
                    polozka[2] = True
                else:
                    vlavo = not vlavo
            cele = ' class="cele"'
            pole_html = ''.join(f'<div{cele if c else ""}><dt>{T(k)}</dt>'
                                f'<dd{CISLA if k == "Termín" else ""}>{T(v)}</dd></div>'
                                for k, v, c in polozky)
            pole_html = f'<dl class="pole-siroke">{pole_html}</dl>' if pole_html else ''
            odseky = [p for p in r['text'].split('\n') if p.strip()]
            text = ''.join(f'<p>{T(p)}</p>' for p in odseky)
            # realizácia z Facebooku: dátum príspevku a odkaz (príspevok, inak stránka firmy), bez nových faktov
            # (brána dizajnu pokus 1, nález 11)
            fb = r['fb_url'] or self.firma['facebook']
            if r['zdroj'] == 'fb' and fb:
                kedy = f' {datum_text(r["datum"])}' if r['datum'] else ''
                text += (f'<p>{T("Príspevok o tejto stavbe zverejnil BM Building" + kedy + " na")} '
                         f'<a href="{esc(fb)}">Facebooku</a>.</p>')
            elif r['fb_url']:
                text += f'<p><a href="{esc(r["fb_url"])}">Príspevok na Facebooku</a></p>'
            text = f'<div class="text realizacia-text">{text}</div>' if text else ''
            galeria = ''
            og_foto = None
            if r['fotky']:
                # galéria: figure s viditeľným popisom, kde alt hovorí niečo navyše (nález 3), bez odkazu na
                # rovnako veľký webp (nález 13), section s menom namiesto div (nález 14), šírka 960 px (nález 8)
                prva = r['fotky'][0]
                popis = self.popis_fotky(r, prva, '')
                # od 1024 px je galéria v ľavých 8 z 12 stĺpcov (obsah 1280 px: 845 px, ďalšie fotky po 411 px),
                # vpravo tabuľka údajov (brána dizajnu pokus 2, nález 5)
                galeria = self.foto_v_galerii(pre, r, prva, '(min-width:1376px) 845px, (min-width:1024px) calc(66.67vw - 72px), '
                                              '(min-width:640px) calc(100vw - 64px), calc(100vw - 32px)', popis, lcp=True)
                dalsie = ''
                for f in r['fotky'][1:]:
                    popis = self.popis_fotky(r, f, popis)
                    dalsie += self.foto_v_galerii(pre, r, f, '(min-width:1376px) 411px, (min-width:1024px) calc(33.33vw - 48px), '
                                                  '(min-width:640px) calc(50vw - 44px), calc(100vw - 32px)', popis)
                    popis = re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')
                if dalsie:
                    galeria += f'<div class="galeria-dalsie">{dalsie}</div>'
                galeria = f'<section class="galeria" aria-label="Fotky">{galeria}</section>'
                og_foto = prva['zdroj']
            # bez fotky: žiadna modrá doska, ktorá by tretí raz opakovala roky a rolu (brána dizajnu pokus 1, nález 2)
            og = self.og_cesta_realizacie(r)
            self.registruj_og(og, og_foto, nm, ' · '.join(x for x in (r['rola'], r['roky']) if x),
                              'BM Building, s.r.o. · bmbuilding.sk')
            susedia = []
            if i > 0:
                p = poradie[i - 1]
                susedia.append(f'<a class="predch" href="../{esc(p["slug"])}/"><span>Predchádzajúca<span class="skryte"> realizácia:</span></span>{T(p["nazov"])}</a>')
            if i + 1 < len(poradie):
                p = poradie[i + 1]
                susedia.append(f'<a class="dalsia" href="../{esc(p["slug"])}/"><span>Ďalšia<span class="skryte"> realizácia:</span></span>{T(p["nazov"])}</a>')
            susedia_html = ''
            if susedia:
                susedia_html = f'<nav class="susedia" aria-label="Ďalšie realizácie">{"".join(susedia)}</nav>'
            spat = f'<p class="viac"><a class="sipka" href="../">Späť na všetky realizácie {SVG_SIPKA}</a></p>'
            hlavne = f'<div class="detail-hlavne">{text}{galeria}</div>' if text or galeria else ''
            telo = f'<div class="obsah"><div class="detail">{pole_html}{hlavne}</div>{susedia_html}{spat}</div>'
            kroky = [('', 'Úvod'), ('realizacie/', 'Realizácie'), (cesta, r['nazov'])]
            obrazky = [self.abs_url(self.obrazok_cesty(r, f)[f['max_w']]) for f in r['fotky']]
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
            # datePublished len pri príspevku (fb, sprava); dátum článku starého webu nie je dátum stavby ani tejto
            # stránky (brána pokus 1, správnosť 6)
            if r['datum'] and r['zdroj'] != 'web':
                dielo['datePublished'] = r['datum']
            if r['upravene']:
                dielo['dateModified'] = r['upravene']
            uzly = [dielo]
            self.podstranka(cesta, kroky, h1, '', telo, 'realizacie/' + r['slug'], title, desc, uzly, og=og,
                            og_typ='article', lastmod=self.zmena(r), nad_h1=nad_h1)

    def stranka_opravnenia(self):
        t = self.texty['opravnenia']
        cesta = 'opravnenia/'
        pre = self.pre(cesta)
        doklady = ''.join(self.doklady(pre, nadpis='h2'))
        telo = (f'<div class="obsah"><div class="doklady">{doklady}</div><p class="poznamka">{T(t["poznamka"])}</p>'
                f'<h2 class="h3 realizacia-text" id="obchodny-register">{T(t["register_nadpis"])}</h2><div class="text"><p>{T(t["register"])}</p>'
                f'<p class="poznamka">Zdroj: <a href="{esc(t["register_zdroj"])}">výpis z Obchodného registra SR</a></p></div></div>')
        kroky = [('', 'Úvod'), (cesta, 'Oprávnenia')]
        self.podstranka(cesta, kroky, t['h1'], t['veta'], telo, 'opravnenia', t['title'], t['description'], [],
                        lastmod=self.vydanie)

    def stranka_kontakt(self):
        t = self.texty['kontakt']
        cesta = 'kontakt/'
        pre = self.pre(cesta)
        f = self.firma
        pole = [('Obchodné meno', f['meno']), ('Konateľ', f['konatel']), ('IČO', f['ico']), ('IČ DPH', f['ic_dph']),
                ('Zápis', f['zapis'])]
        dl = ''.join(f'<dt>{T(k)}</dt><dd>{T(v)}</dd>' for k, v in pole if v)
        fb = f'<p class="viac"><a class="sipka" href="{esc(f["facebook"])}">BM Building na Facebooku {SVG_SIPKA}</a></p>' if f['facebook'] else ''
        telo = (f'<section class="sekcia" aria-label="Kontaktné údaje"><div class="obsah blok-kontakt">{self.blok_kontakt(pre, nadpis_h1=True)}'
                f'<dl class="pole">{dl}</dl>{fb}</div></section>')
        kroky = [('', 'Úvod'), (cesta, 'Kontakt')]
        # jeden uzol WebPage a ContactPage s @id namiesto dvoch uzlov pre tú istú URL (brána pokus 2, správnosť 7)
        self.podstranka(cesta, kroky, t['h1'], t['veta'], telo, 'kontakt', t['title'], t['description'], [],
                        kontakt=False, lastmod=self.vydanie, pata_firma=False, typ_stranky=['WebPage', 'ContactPage'],
                        stranka_navyse={'about': {'@id': self.abs_url('#firma')}})

    def stranka_ochrana(self):
        t = self.texty['ochrana']
        cesta = 'ochrana-udajov/'
        pre = self.pre(cesta)
        f = self.firma
        adresa = ', '.join(x for x in (f['ulica'], ' '.join(y for y in (f['psc'], f['mesto']) if y)) if x)
        kontakt = ' alebo '.join(x for x in (f['email'] and f'e-mailom na {f["email"]}', f['tel'] and f'telefonicky na {f["tel"]}') if x)
        prev = f'{f["meno"]}, {adresa}' + (f', IČO {f["ico"]}' if f['ico'] else '') + '.'
        if f['zapis']:
            prev += ' ' + f['zapis']
        casti = [
            ('Prevádzkovateľ', [prev, f'Kontakt na prevádzkovateľa: {kontakt}.' if kontakt else '']),
            ('Čo tento web nerobí', [
                'Web nepoužíva cookies, analytiku ani iné sledovacie nástroje a nemá žiadne formuláre. Nevkladá obsah tretích strán: mapy, Facebook ani písma z cudzích serverov sa nenačítavajú. Písmo je uložené priamo na tomto webe.',
                'Odkazy na Mapy Google, Mapy.cz a Facebook otvoria stránku tretej strany až po kliknutí. Na tej stránke platia pravidlá jej prevádzkovateľa.']),
            # čl. 13 nariadenia 2016/679: právny základ, doba uchovávania a prenos mimo EÚ (brána pokus 1, správnosť 10);
            # čl. 6 ods. 1 písm. b) a f) podľa ops/notes/fakty.md F639 (EUR-Lex), prenos podľa GitHub General Privacy
            # Statement, časť International data transfers (GET 21. 11. 2026)
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
            html_casti.append(f'<h2 class="h3 realizacia-text">{T(nadpis)}</h2>' + ''.join(f'<p>{T(p)}</p>' for p in odseky if p))
        odkazy = (f'<p class="poznamka">Zdroje: <a href="{esc(t["github_url"])}">GitHub Pages, Data collection</a>, '
                  f'<a href="{esc(t["github_privacy_url"])}">GitHub General Privacy Statement</a>, '
                  f'<a href="{esc(t["uoou_url"])}">Úrad na ochranu osobných údajov SR</a>. '
                  f'Písmo Archivo (The Archivo Project Authors), licencia SIL Open Font License 1.1: '
                  f'<a href="{esc(pre)}fonts/OFL.txt">fonts/OFL.txt</a>.</p>')
        telo = f'<div class="obsah"><div class="text">{"".join(html_casti)}{odkazy}</div></div>'
        kroky = [('', 'Úvod'), (cesta, 'Ochrana osobných údajov')]
        self.podstranka(cesta, kroky, t['h1'], '', telo, 'ochrana-udajov', t['title'], t['description'], [],
                        lastmod=self.vydanie)

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
        tel = (f'<a class="tlacidlo tlacidlo-obrys" href="tel:{esc(f["tel_href"])}">{SVG_TEL}Zavolať '
               f'<span class="cisla">{T(f["tel"])}</span></a>') if f['tel_href'] else ''
        telo = (f'<div class="obsah hlava"><h1>{T(t["h1"])}</h1><p class="uvod-veta">{T(t["veta"])}</p>'
                f'<div class="uvod-akcie"><a class="tlacidlo" href="{z}">Na úvod</a>{tel}<a class="odkaz" href="{z}realizacie/">Realizácie</a>'
                f'<a class="odkaz" href="{z}kontakt/">Kontakt</a></div></div>')
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
            obsah = ('<!doctype html>\n<html lang="sk">\n<head>\n<meta charset="utf-8">\n'
                     '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action \'none\'">\n'
                     f'{robots}<title>Stránka sa presunula | BM Building</title>\n'
                     f'<meta http-equiv="refresh" content="0; url={esc(rel)}">\n'
                     f'<link rel="canonical" href="{esc(canon)}">\n'
                     '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
                     f'<p>Stránka sa presunula na novú adresu: <a href="{esc(rel)}">{esc(canon)}</a></p>\n</body>\n</html>\n')
            self.subory[subor] = obsah.encode('utf-8')

    def staticke(self):
        f = self.firma
        self.subory['favicon.svg'] = FAVICON_SVG.encode('utf-8')
        self.obrazky['favicon.ico'] = (hashlib.sha256((VERZIA_OG + 'ico').encode()).hexdigest() + self.pismo_odt(),
                                       favicon_ico_bajty)
        pismo_zdroj = SABLONA / 'archivo-400-700.woff2'
        if pismo_zdroj.is_file():
            self.subory['fonts/archivo-400-700.woff2'] = pismo_zdroj.read_bytes()
        else:
            varuj('sablona/archivo-400-700.woff2 chýba, web pôjde s náhradným písmom')
        if (SABLONA / 'OFL.txt').is_file():
            self.subory['fonts/OFL.txt'] = (SABLONA / 'OFL.txt').read_bytes()
        if self.logo:
            self.subory['logo.png'] = self.logo.read_bytes()
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

    def pismo_odt(self):
        p = SABLONA / 'archivo-600.ttf'
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else 'x'

    def postav(self):
        self.uvod()
        self.stranky_sluzieb()
        self.stranka_realizacii()
        self.stranky_realizacie()
        self.stranka_opravnenia()
        self.stranka_kontakt()
        self.stranka_ochrana()
        self.stranka_404()
        self.presmerovania()
        self.staticke()


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
        if not cesta.endswith(('.html', '.xml', '.txt', '.svg')) or cesta == 'fonts/OFL.txt':
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
    for rel, (odt, funkcia) in sorted(web.obrazky.items()):
        if stary.get(rel) == odt and (vystup / rel).is_file():
            continue
        try:
            obrazky[rel] = funkcia()
        except Exception as e:
            raise ChybaZostavy(f'obrázok {rel} sa nedá vyrobiť ({e.__class__.__name__}: {e}); '
                               f'predchádzajúci výstup ostal bez zmeny')
    # zmaž vlastný predchádzajúci výstup, ktorý v novom nie je alebo sa zmenil (obrázky s rovnakým odtlačkom ostanú)
    for rel, odt in stary.items():
        p = vystup / rel
        if rel in web.obrazky and odt == novy.get(rel) and p.is_file():
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
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    # prázdne priečinky po vlastných súboroch
    for rel in sorted(stary, key=lambda x: -x.count('/')):
        rodic = (vystup / rel).parent
        while rodic != vystup and rodic.is_dir() and not any(rodic.iterdir()):
            rodic.rmdir()
            rodic = rodic.parent
    (vystup / MANIFEST).write_text(json.dumps({'verzia': 1, 'subory': dict(sorted(novy.items()))}, ensure_ascii=False,
                                              indent=0) + '\n', encoding='utf-8', newline='\n')
    return kodovanych


def main(argv=None):
    ap = argparse.ArgumentParser(description='Zostava statického webu BM Building z obsah/.')
    ap.add_argument('--nahlad', action='store_true', help='náhľad: noindex, robots Disallow, bez sitemap, CNAME a .nojekyll')
    ap.add_argument('--vystup', type=Path, default=KOREN / 'web', help='výstupný priečinok (predvolený web/)')
    ap.add_argument('--obsah', type=Path, default=KOREN / 'obsah', help='priečinok obsahu (predvolený obsah/)')
    a = ap.parse_args(argv)
    for prud in (sys.stdout, sys.stderr):
        try:
            prud.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    VAROVANIA.clear()
    nahlad = a.nahlad
    # GitHub Actions (workflow linky bmb-obsah volá „python build.py“): ostrý beh len s BMB_OSTRY=ano, inak náhľad
    # s noindex, Disallow a 404 pod /bmbuilding/, kým doména nie je prepnutá (brána pokus 1, správnosť 1)
    if not nahlad and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('BMB_OSTRY', '').strip().lower() != 'ano':
        nahlad = True
        print('GitHub Actions bez BMB_OSTRY=ano: zostavujem náhľad (noindex). Ostrý web po prepnutí domény: BMB_OSTRY=ano.')
    vystup = a.vystup.resolve()
    obsah = a.obsah.resolve()
    if vystup in (KOREN, obsah, SABLONA) or vystup in obsah.parents or obsah in vystup.parents or vystup == Path(vystup.anchor):
        print(f'CHYBA: výstup {vystup} nesmie byť koreň, obsah ani šablóna', file=sys.stderr)
        return 2
    try:
        web = Web(obsah, nahlad)
        web.postav()
        skontroluj_text(web.subory)
        kodovanych = zapis(web, vystup)
    except ChybaZostavy as e:
        print('CHYBA: ' + str(e), file=sys.stderr)
        return 1
    stranky = sum(1 for k in web.subory if k.endswith('.html'))
    print(f'hotovo: {vystup} ({"náhľad" if nahlad else "ostrý"}), {stranky} HTML, {len(web.realizacie)} realizácií, '
          f'{len(web.obrazky)} obrázkov (nových {kodovanych}), varovaní {len(VAROVANIA)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
