"""Testy generátora build.py (unittest, bez siete), štýl Čiara podľa ukážky Ateliér Olovnica (linka bmb-olovnica).

Spustenie: python -m unittest -v test_build   (v priečinku products/bmbuilding)

Skutočný obsah (obsah/, dopĺňa ho linka bmb-fb) sa zostaví raz pre celý beh (ostrý a náhľad) s rýchlou náhradou
kódovania webp; očakávania sa počítajú z obsahu, nie z pevných počtov, aby testy platili aj po pridaní realizácií.
"""
import atexit
import base64
import colorsys
import copy
import gzip
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from PIL import Image

KOREN = Path(__file__).resolve().parent
sys.path.insert(0, str(KOREN))
import build  # noqa: E402

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
# prvky, ktorých koncovú značku HTML dovoľuje vynechať (my ich vždy zatvárame, ale parser ich toleruje)
NEPOVINNE = {'li', 'p', 'dt', 'dd', 'option'}
KLUCE_KATEGORII = {k for k, _ in build.KATEGORIE}
MODRA = (0x17, 0x47, 0x6F)


class Rozbor(HTMLParser):
    """Prísny rozbor: párovanie značiek, odkazy, h1, meta, štýl, JSON-LD a skripty (len vlastný app.js s defer,
    jeden vložený skript build.SKRIPT_JS a JSON-LD; žiadne inline handlery on* ani atribút style)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.zasobnik, self.chyby = [], []
        self.odkazy, self.h1, self.meta, self.base = [], 0, [], None
        self.styl, self.ldjson, self._v = [], [], None
        self.texty, self.inline, self.prvky = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.prvky.append((tag, a))
        if tag not in VOID:
            self.zasobnik.append(tag)
        if tag == 'h1':
            self.h1 += 1
        if tag == 'meta':
            self.meta.append(a)
        if tag == 'base':
            self.base = a.get('href')
        for k in a:
            if k.lower().startswith('on'):
                self.chyby.append(f'inline handler {k}')
            if k.lower() == 'style':
                self.chyby.append('atribút style (CSP ho nedovolí)')
        for k in ('href', 'src', 'data-plna'):
            if a.get(k) and tag != 'base':
                self.odkazy.append(a[k])
        for k in ('srcset', 'data-srcset'):
            if a.get(k):
                for kus in a[k].split(','):
                    self.odkazy.append(kus.strip().split(' ')[0])
        if tag == 'img' and not a.get('src'):
            self.chyby.append('img bez src')
        if tag == 'style':
            self._v = 'style'
        if tag == 'script':
            if a.get('type') == 'application/ld+json':
                self._v = 'ld'
            elif a.get('src'):
                self._v = 'src'
                if not re.search(r'(^|/)app\.js\?v=[0-9a-f]{10}$', a['src']) or 'defer' not in a:
                    self.chyby.append(f'cudzí alebo neodložený skript {a["src"]}')
            else:
                self._v = 'inline'

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.zasobnik and self.zasobnik[-1] == tag:
            self.zasobnik.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        while self.zasobnik and self.zasobnik[-1] != tag and self.zasobnik[-1] in NEPOVINNE:
            self.zasobnik.pop()
        if not self.zasobnik or self.zasobnik[-1] != tag:
            self.chyby.append(f'neočakávané </{tag}> pri {self.zasobnik[-3:]}')
            return
        self.zasobnik.pop()
        if tag in ('style', 'script'):
            self._v = None

    def handle_data(self, data):
        if self._v == 'style':
            self.styl.append(data)
        elif self._v == 'ld':
            self.ldjson.append(data)
        elif self._v == 'inline':
            self.inline.append(data)
            if data != build.SKRIPT_JS:
                self.chyby.append('neznámy vložený skript')
        elif self._v == 'src':
            if data.strip():
                self.chyby.append('skript so src má obsah')
        elif self._v is None:
            self.texty.append(data)


def rozober(cesta):
    r = Rozbor()
    r.feed(cesta.read_text(encoding='utf-8'))
    r.close()
    if r.zasobnik:
        r.chyby.append(f'neuzavreté značky {r.zasobnik}')
    return r


def lb_zdroje(prvky, k):
    """Srcset WebP a AVIF lightboxu pre odkaz prvky[k] (a[data-lightbox]), ako ich vyberie app.js (srcsetZ, avifZ):
    data-srcset a data-avif odkazu, inak srcset náhľadu (img a source type=image/avif) v odkaze (build.py lb_data
    nahlad, overenie zostavy bmb-presnost). Vráti (webp, avif, z_nahladu)."""
    a = prvky[k][1]
    if 'data-srcset' in a:
        return a['data-srcset'], a.get('data-avif', ''), False
    img = src = None
    for t, x in prvky[k + 1:]:
        if t == 'source' and x.get('type') == 'image/avif' and src is None:
            src = x
        if t == 'img':
            img = x
            break
        if t == 'a':
            break
    return (img or {}).get('srcset', ''), (src or {}).get('srcset', ''), True


def rychla_webp(p, s, v, pomer=None, box=None):
    """Náhrada kódovania webp pre skutočný obsah (stovky fotiek, aj výrezy kariet): správne rozmery, žiadna kvalita."""
    buf = io.BytesIO()
    Image.new('RGB', (s, v), MODRA).save(buf, 'WEBP', quality=1, method=0)
    return buf.getvalue()


# Linka bmb-fb-uplnost, oprava 2: ten istý záber s iným orezom (dHash ho nechytí, napríklad Dvojdom S 02 a 05,
# Vila Holubyho 07 a 10). Obe fotky v odtieňoch sivej na šírku 64 px a zvlášť na výšku 48 px; kratšiu posúvame po
# dlhšej a berieme najmenší priemerný rozdiel jasu (0 až 255). Postup podľa fb/uplnost/overenie2_orezy.py, rozdelenie
# hodnôt vo všetkých galériách fb/uplnost/oprava2_orezy.py a oprava2-orezy-pred.json: päť zhodných záberov 0,88 až
# 8,4, najbližšia iná dvojica 21,97 (Ventúrska 02 a 12, iná fotka z toho istého miesta). Tú istú fotku s pridaným
# nápisom cez časť záberu (Vtáčnik 13 a 10, 21,82) test nechytí; tá je vyradená ručne (zapis_obsah.py zhodne_fotky).
PRAH_OREZ = 13.0


def nahlad_orezu(cesta):
    """Dve malé sivé matice fotky na porovnanie orezu: (šírka 64 px, výška 48 px transponovaná)."""
    import numpy as np
    from PIL import ImageOps
    with Image.open(cesta) as im:
        im.draft('L', (256, 256))
        im = ImageOps.exif_transpose(im).convert('L')
    a = im.resize((64, max(1, round(im.height * 64 / im.width))), Image.BILINEAR)
    b = im.resize((max(1, round(im.width * 48 / im.height)), 48), Image.BILINEAR)
    return np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32).T


def _posun_orezu(a, b):
    """a, b s rovnakým počtom stĺpcov: kratšiu posúvaj po dlhšej, vráť najmenší priemerný rozdiel."""
    if a.shape[0] > b.shape[0]:
        a, b = b, a
    ha, hb = a.shape[0], b.shape[0]
    if ha < hb * 0.5:
        return 999.0
    return min(float(abs(b[y:y + ha] - a).mean()) for y in range(hb - ha + 1))


def rozdiel_orezu(na, nb):
    """Najmenší rozdiel dvoch fotiek pri posune orezu zvisle (rovnaká šírka) aj vodorovne (rovnaká výška)."""
    return min(_posun_orezu(na[0], nb[0]), _posun_orezu(na[1], nb[1]))


def zhodne_orezy(obsah, prah=None):
    """Dvojice fotiek v galérii každej zverejnenej realizácie (obsah/realizacie/*.json) s rozdielom orezu pod prahom."""
    prah = PRAH_OREZ if prah is None else prah
    zhodne, pocet = [], 0
    for p in sorted((Path(obsah) / 'realizacie').glob('*.json')):
        r = json.loads(p.read_text(encoding='utf-8-sig'))
        if r.get('zverejnene') is False:
            continue
        fotky = [str(f['subor']) for f in r.get('fotky') or []
                 if isinstance(f, dict) and (Path(obsah).parent / str(f.get('subor'))).is_file()]
        n = [nahlad_orezu(Path(obsah).parent / f) for f in fotky]
        pocet += len(n)
        for i in range(len(n)):
            for j in range(i + 1, len(n)):
                d = rozdiel_orezu(n[i], n[j])
                if d < prah:
                    zhodne.append((p.stem, fotky[i].split('/')[-1], fotky[j].split('/')[-1], round(d, 2)))
    return zhodne, pocet


def rychla_avif(p, s, v, pomer=None, box=None):
    """Náhrada kódovania AVIF pre skutočný obsah (linka bmb-fotky): správne rozmery, žiadna kvalita."""
    buf = io.BytesIO()
    Image.new('RGB', (s, v), MODRA).save(buf, 'AVIF', quality=1, speed=10)
    return buf.getvalue()


def postav(vystup, obsah=None, nahlad=False, prostredie=None, rychle=False, fotky_web=None):
    argv = ['--vystup', str(vystup)]
    if obsah:
        argv += ['--obsah', str(obsah)]
    if nahlad:
        argv.append('--nahlad')
    if fotky_web:
        argv += ['--fotky-web', str(fotky_web)]
    out, err = io.StringIO(), io.StringIO()
    # testy bežia rovnako lokálne aj v GitHub Actions: premenné CI nastaví len test, ktorý ich skúša
    env = {k: v for k, v in os.environ.items() if k not in ('GITHUB_ACTIONS', 'BMB_OSTRY')}
    env.update(prostredie or {})
    with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
        if rychle:
            with mock.patch.object(build, 'webp_bajty', rychla_webp), mock.patch.object(build, 'avif_bajty', rychla_avif):
                kod = build.main(argv)
        else:
            kod = build.main(argv)
    return kod, out.getvalue(), err.getvalue()


_SKUTOCNY = {}


def skutocny():
    """Skutočný obsah zostavený raz: ostrý web a náhľad (náhľad z kópie ostrého, obrázky sa znova nekódujú)."""
    if not _SKUTOCNY:
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-skutocny-'))
        atexit.register(shutil.rmtree, tmp, True)
        ostry, nahlad = tmp / 'ostry', tmp / 'nahlad'
        _SKUTOCNY['o'] = postav(ostry, rychle=True)
        # prísne varovania celej zostavy (fb-ostro A4): v GitHub Actions by z nich bola chyba zostavy
        _SKUTOCNY['prisne'] = list(build.PRISNE_VAROVANIA)
        shutil.copytree(ostry, nahlad)
        _SKUTOCNY['n'] = postav(nahlad, nahlad=True, rychle=True)
        _SKUTOCNY.update(tmp=tmp, ostry=ostry, nahlad=nahlad)
        realizacie = {}
        for p in sorted((KOREN / 'obsah' / 'realizacie').glob('*.json')):
            d = json.loads(p.read_text(encoding='utf-8-sig'))
            if d.get('zverejnene') is not False:
                realizacie[d['slug']] = d
        _SKUTOCNY['realizacie'] = realizacie
        with redirect_stderr(io.StringIO()):
            _SKUTOCNY['web'] = build.Web(KOREN / 'obsah', False)
    return _SKUTOCNY


_SKUTOCNY_N = {}


def skutocny_novinky():
    """Skutočný obsah s Novinkami zapnutými: kópia šablóny s "novinky": true (vratné nastavenie, ZMENY-KOLO-1.md 5.7,
    Andrej 24. 11. 2026 večer). Novinky musia fungovať ako pred 1. kolom; testy noviniek bežia nad touto zostavou.
    Výstup je kópia ostrého webu (fotky realizácií sa neskódujú znova), kódovanie rýchlou náhradou."""
    if not _SKUTOCNY_N:
        s = skutocny()
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-novinky-'))
        atexit.register(shutil.rmtree, tmp, True)
        sablona = sablona_s_nastaveniami(tmp, novinky=True)
        ostry = tmp / 'ostry'
        shutil.copytree(s['ostry'], ostry)
        with mock.patch.object(build, 'SABLONA', sablona):
            _SKUTOCNY_N['o'] = postav(ostry, rychle=True)
            with redirect_stderr(io.StringIO()):
                _SKUTOCNY_N['web'] = build.Web(KOREN / 'obsah', False)
        _SKUTOCNY_N.update(tmp=tmp, ostry=ostry, sablona=sablona)
    return _SKUTOCNY_N


def stranka_info(cesta):
    """title, description, h1, JSON-LD (zoznam uzlov @graph) a telo medzi <main> a <footer>."""
    s = cesta.read_text(encoding='utf-8')
    title = html.unescape(re.search(r'<title>(.*?)</title>', s).group(1))
    m = re.search(r'<meta name="description" content="([^"]*)">', s)
    desc = html.unescape(m.group(1)) if m else ''
    m = re.search(r'<h1[^>]*>(.*?)</h1>', s, re.S)
    h1 = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).replace(' ', ' ') if m else ''
    uzly = []
    for ld in re.findall(r'<script type="application/ld\+json">(.*?)</script>', s, re.S):
        uzly += json.loads(ld).get('@graph', [])
    telo = s.split('<main', 1)[1].split('<footer', 1)[0] if '<main' in s else s
    return {'title': title, 'desc': desc, 'h1': h1, 'ld': uzly, 'telo': telo, 'html': s}


def odtlacky(priecinok):
    return {str(p.relative_to(priecinok)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(priecinok.rglob('*')) if p.is_file()}


def fotka(cesta, w, h, odtien):
    """Ostrá syntetická fotka: prechod farieb a šachovnica 4 px (ostrosť vysoko nad prahmi obalu)."""
    im = Image.new('RGB', (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            k = 90 * ((x // 4 + y // 4) % 2)
            px[x, y] = ((x * 7 + odtien + k) % 256, (y * 5 + odtien + k) % 256, ((x + y) * 3 + k) % 256)
    cesta.parent.mkdir(parents=True, exist_ok=True)
    im.save(cesta, 'JPEG', quality=90)


def rozmazana(cesta, w, h):
    """Rozmazaná fotka: len pozvoľný prechod, rozptyl Laplaciánu takmer nula."""
    im = Image.new('RGB', (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = (90 + x * 60 // w, 110 + y * 40 // h, 130)
    cesta.parent.mkdir(parents=True, exist_ok=True)
    im.save(cesta, 'JPEG', quality=90)


def maly_obsah(koren):
    """Dočasný obsah: plná realizácia, realizácia bez polí, XSS, zlé slugy, nezverejnená, chýbajúca fotka."""
    obsah = koren / 'obsah'
    (obsah / 'realizacie').mkdir(parents=True)
    shutil.copyfile(KOREN / 'obsah' / 'firma.json', obsah / 'firma.json')
    fotka(obsah / 'obrazky' / 'plna' / '01.jpg', 1100, 700, 10)
    fotka(obsah / 'obrazky' / 'plna' / '02.jpg', 500, 800, 90)
    zaznamy = {
        'plna': {'id': 'plna', 'slug': 'plna', 'nazov': 'Bytový dom Skúška', 'miesto': 'Bratislava, Skúšobná ulica',
                 'rola': 'Stavebný dozor', 'projektant': 'Ing. arch. Test', 'roky': '2019–2021',
                 'text': 'Prvý odsek – s pomlčkou! A druhý.\nDruhý odsek.', 'investor': 'Investor s.r.o.',
                 'zhotovitel': '', 'fotky': [{'subor': 'obsah/obrazky/plna/01.jpg', 'alt': 'Pohľad z ulice'},
                                             {'subor': 'obsah/obrazky/plna/02.jpg', 'alt': ''},
                                             {'subor': 'obsah/obrazky/plna/99.jpg', 'alt': 'chýba'},
                                             {'subor': '../../../build.py', 'alt': 'mimo'}],
                 'zdroj': 'web', 'fb_url': None, 'datum': '2024-05-01', 'poradie': 1, 'zverejnene': True},
        'holá': {'slug': 'hola', 'nazov': 'Holá realizácia'},
        'xss': {'slug': 'xss', 'nazov': '<script>alert(1)</script> "onerror=alert(1)',
                'miesto': '"><img src=x onerror=alert(1)>', 'rola': 'Dozor</script><script>alert(2)</script>',
                'projektant': None, 'roky': 2020, 'text': '</script><script>alert(3)</script> & <b>tučné</b>',
                'fotky': 'nie je zoznam', 'fb_url': 'javascript:alert(4)', 'datum': 'zajtra', 'poradie': 'x',
                'kategoria': '"><script>alert(5)</script>', 'zverejnene': True},
        'zly1': {'slug': 'Zlý Slug', 'nazov': 'Zlý slug'},
        'zly2': {'slug': '../von', 'nazov': 'Únik z priečinka'},
        'skryta': {'slug': 'skryta', 'nazov': 'Nezverejnená', 'zverejnene': False},
        'zz-dupl': {'slug': 'plna', 'nazov': 'Duplicitný slug'},
    }
    for meno, d in zaznamy.items():
        (obsah / 'realizacie' / f'{meno}.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    (obsah / 'realizacie' / 'pokazeny.json').write_text('{toto nie je json', encoding='utf-8')
    return obsah


# slider úvodu, ktorý v malom obsahu (maly_obsah) je: obe fotky realizácie plna v slideri (02 menšia než prah
# velka, slider ju smie mať); šablóna webu má slugy skutočného obsahu (fb-ostro A4). Pás je od ÚVOD v3 preč.
NASTAVENIA_MALY = {'uvod_slider': [{'slug': 'plna', 'subor': '01.jpg', 'stred': [0.5, 0.5]},
                                   {'slug': 'plna', 'subor': '02.jpg', 'stred': [0.5, 0.3]}]}


def sablona_s_nastaveniami(koren, **zmeny):
    """Kópia sablona/ v koren/sablona s upraveným nastavenia.json (None kľúč odoberie), pre mock build.SABLONA."""
    sablona = koren / 'sablona'
    if not sablona.is_dir():
        shutil.copytree(KOREN / 'sablona', sablona)
    p = sablona / 'nastavenia.json'
    d = json.loads(p.read_text(encoding='utf-8'))
    for kluc, hodnota in zmeny.items():
        if hodnota is None:
            d.pop(kluc, None)
        else:
            d[kluc] = hodnota
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    return sablona


def pridaj_novinky(obsah):
    """Novinky od linky bmb-fb: dobrá s fotkami, XSS, zlý JSON, slug, dátum, cesta k fotke, 12 fotiek, nezverejnená."""
    n = obsah / 'novinky'
    n.mkdir()
    for i in range(1, 13):
        fotka(obsah / 'obrazky' / 'novinky' / 'vela' / f'{i:02d}.jpg', 480, 360, i * 9)
    fotka(obsah / 'obrazky' / 'novinky' / 'dobra' / '01.jpg', 1000, 700, 40)
    zaznamy = {
        'dobra': {'slug': 'dobra', 'datum': '2026-09-10', 'nadpis': 'Betonáž stropu na stavbe v Pezinku',
                  'text': 'Dnes sme betónovali strop!\nDruhý odsek – s pomlčkou.',
                  'fotky': [{'subor': 'obsah/obrazky/novinky/dobra/01.jpg', 'alt': 'Betonáž'},
                            {'subor': '../../build.py'}, 'obsah/obrazky/novinky/dobra/99.jpg'],
                  'fb_url': 'https://www.facebook.com/BmBuilding/posts/123'},
        'bez-nadpisu': {'datum': '2026-08-01', 'text': 'Prvý riadok je nadpis\nďalší text príspevku'},
        'xss-n': {'slug': 'xss-n', 'datum': '2026-07-01', 'nadpis': '<script>alert(6)</script>',
                  'text': '"><img src=x onerror=alert(7)>', 'fb_url': 'javascript:alert(8)'},
        'vela': {'slug': 'vela', 'datum': '2026-06-01', 'nadpis': 'Dvanásť fotiek',
                 'fotky': [f'obsah/obrazky/novinky/vela/{i:02d}.jpg' for i in range(1, 13)]},
        'zly-slug': {'slug': 'Zlý slug', 'datum': '2026-01-01', 'nadpis': 'Zlý slug'},
        'zly-datum': {'slug': 'zly-datum', 'datum': '2026-02-30', 'nadpis': 'Zlý dátum'},
        'skryta-n': {'slug': 'skryta-n', 'datum': '2026-03-01', 'nadpis': 'Skrytá', 'zverejnene': False},
        'prazdna': {'slug': 'prazdna', 'datum': '2026-03-02'},
    }
    for meno, d in zaznamy.items():
        (n / f'{meno}.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    (n / 'pokazena.json').write_text('[nie je to objekt', encoding='utf-8')


def interne_odkazy(vystup, zaklad_404):
    chyby = []
    for subor in sorted(vystup.rglob('*.html')):
        r = rozober(subor)
        for u in r.odkazy:
            if re.match(r'^(https?:|tel:|mailto:|data:)', u) or u.startswith('#'):
                continue
            cesta = u.split('#')[0].split('?')[0]
            if subor.name == '404.html' and subor.parent == vystup:
                # 404 nemá <base>, cesty sú absolútne od základu webu („/“ alebo „/bmbuilding/“)
                if not cesta.startswith(zaklad_404):
                    chyby.append(f'404.html: {u} nezačína {zaklad_404}')
                    continue
                cesta = cesta[len(zaklad_404):]
                ciel = (vystup / cesta) if cesta else vystup
            else:
                ciel = (subor.parent / cesta) if cesta else subor.parent
            ciel = ciel.resolve()
            if ciel.is_dir():
                ciel = ciel / 'index.html'
            if vystup.resolve() not in ciel.parents and ciel != vystup.resolve():
                chyby.append(f'{subor}: {u} mimo výstupu')
            elif not ciel.is_file():
                chyby.append(f'{subor.relative_to(vystup)}: {u} neexistuje')
        for m in r.meta:
            if m.get('http-equiv') == 'refresh':
                url = m['content'].split('url=', 1)[1].split('#')[0]
                ciel = (subor.parent / url).resolve()
                if ciel.is_dir():
                    ciel = ciel / 'index.html'
                if not ciel.is_file():
                    chyby.append(f'{subor.relative_to(vystup)}: presmerovanie {url} neexistuje')
    return chyby


def csp_sedi(subor):
    """Každý vložený blok <style> a <script> (bez src) má sha256 v CSP; script-src len 'self' a hashe."""
    s = subor.read_text(encoding='utf-8')
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', s)
    if not m:
        return 'chýba CSP'
    politika = m.group(1)
    casti = [c.strip() for c in politika.split(';')]
    for blok, smernica in (('style', 'style-src'), ('script', 'script-src')):
        for atributy, obsah in re.findall(rf'<{blok}([^>]*)>(.*?)</{blok}>', s, re.S):
            if 'src=' in atributy:
                continue
            h = "'sha256-" + base64.b64encode(hashlib.sha256(obsah.encode('utf-8')).digest()).decode() + "'"
            if not any(c.startswith(smernica) and h in c for c in casti):
                return f'{smernica} nemá hash bloku {blok}'
    skript = [c for c in casti if c.startswith('script-src')]
    if skript:
        for kus in skript[0].split()[1:]:
            if kus != "'self'" and not kus.startswith("'sha256-"):
                return f'script-src obsahuje {kus}'
    if 'unsafe-inline' in politika or 'unsafe-eval' in politika or 'http' in politika:
        return 'unsafe alebo cudzí zdroj'
    return ''


def stranky(web):
    """HTML stránky webu bez presmerovaní starých adries."""
    return [p for p in sorted(web.rglob('*.html')) if 'index.php' not in p.relative_to(web).parts]


def farby_css(css):
    """Všetky farby v CSS ako (r, g, b): #rgb, #rrggbb, rgb() a rgba()."""
    out = []
    for h in re.findall(r'#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b', css):
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        out.append(tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)))
    for r, g, b in re.findall(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', css):
        out.append((int(r), int(g), int(b)))
    return out


def zlta_alebo_bezova(rgb):
    """Teplá farba: odtieň 15 až 75 stupňov (oranžová, žltá, béžová, krémová) so sýtosťou (rozdiel najväčšej
    a najmenšej zložky) nad 6 z 255. Sýtosť HLS by pri takmer bielej nafúkla aj vápno ukážky #f4f4f1 (rozdiel 3 z 255,
    okom neutrálne, zadanie ho menuje) na 12 %; béžová zamietnutého smeru #eceae4 má rozdiel 8 a ostáva zachytená."""
    h, l, _ = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return 15 <= h * 360 <= 75 and max(rgb) - min(rgb) > 6 and 0.05 < l < 0.98


def bliz(a, b, tol=8):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------------------------------------------
class TestSkutocnyObsah(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.nahlad = s['ostry'], s['nahlad']
        cls.kod_o, cls.out_o, cls.err_o = s['o']
        cls.kod_n, cls.out_n, cls.err_n = s['n']
        cls.realizacie = s['realizacie']

    def test_beh_prejde(self):
        self.assertEqual(self.kod_o, 0, self.err_o)
        self.assertEqual(self.kod_n, 0, self.err_n)
        # skutočný obsah má len varovania o rozmazaných fotkách (FOTKY.md: build ich vypíše)
        ine = [r for r in self.err_o.splitlines() if r.strip() and 'je rozmazaná' not in r]
        self.assertEqual(ine, [])

    def test_v_actions_bez_chyby_sekcii(self):
        # fb-ostro A4: uvod_slider zo šablóny je v dnešnom obsahu, takže zostava v GitHub Actions prejde
        # (filter vyššie by varovanie „… je rozmazaná …“ skryl); Postup a O firme ako sekcia už fotky nemajú
        self.assertEqual(skutocny()['prisne'], [])
        w = skutocny()['web']
        with redirect_stderr(io.StringIO()):
            self.assertIsNotNone(w.hrdina_foto()[1])
            self.assertEqual(len(w.slider_fotky()), len(w.nastavenia['uvod_slider']))
            for kluc in ('firma_foto', 'postup_foto', 'uvod_foto'):
                self.assertNotIn(kluc, w.nastavenia)
            # tmavý pás je z úvodu preč aj s nastaveniami (ZMENY-KOLO-1.md 3A.6)
            for kluc in ('pas_vyber', 'uvod_pocet_realizacii'):
                self.assertNotIn(kluc, w.nastavenia)

    def test_stranky_existuju(self):
        for c in ('index.html', 'stavebny-dozor/index.html', 'stavbyveduci/index.html', 'stavebny-manazment/index.html',
                  'realizacie/index.html', 'opravnenia/index.html', 'kontakt/index.html', 'ochrana-udajov/index.html',
                  '404.html', 'index.php/index.html', 'index.php/cinnost/index.html', 'fonts/geist-latin.woff2',
                  'fonts/geist-latin-ext.woff2', 'fonts/geist-mono-latin.woff2', 'fonts/geist-mono-latin-ext.woff2',
                  'fonts/OFL-Geist.txt', 'og/bmbuilding.jpg', 'favicon.svg', 'favicon.ico', 'apple-touch-icon.png',
                  'logo.svg', 'logo.png', 'app.js'):
            self.assertTrue((self.ostry / c).is_file(), c)
        # staré písma (Archivo, Anybody, DM Mono, Arimo na webe) a znak z obsah/logo.png už web nemá
        for c in ('fonts/archivo-400-700.woff2', 'fonts/bmb-nadpis.woff2', 'fonts/bmb-mono.woff2', 'logo-znak.webp',
                  'fonts/arimo-400-700.woff2', 'fonts/OFL-Arimo.txt'):
            self.assertFalse((self.ostry / c).exists(), c)
        for slug in self.realizacie:
            self.assertTrue((self.ostry / 'realizacie' / slug / 'index.html').is_file(), slug)
            self.assertTrue((self.ostry / 'og' / f'{slug}.jpg').is_file(), slug)

    def test_html_sa_da_rozparsovat_a_ma_jeden_h1(self):
        for subor in sorted(self.ostry.rglob('*.html')):
            r = rozober(subor)
            self.assertEqual(r.chyby, [], subor)
            je_presmerovanie = 'index.php' in subor.relative_to(self.ostry).parts
            self.assertEqual(r.h1, 0 if je_presmerovanie else 1, subor)
            for ld in r.ldjson:
                json.loads(ld)

    def test_interne_odkazy_vedu_na_subory(self):
        self.assertEqual(interne_odkazy(self.ostry, '/'), [])
        self.assertEqual(interne_odkazy(self.nahlad, '/bmbuilding/'), [])

    def test_csp_hash(self):
        for subor in stranky(self.ostry):
            self.assertEqual(csp_sedi(subor), '', subor)

    def test_ostry_bez_noindex_a_so_sitemap(self):
        for subor in sorted(self.ostry.rglob('*.html')):
            self.assertNotIn('noindex', subor.read_text(encoding='utf-8'), subor)
        robots = (self.ostry / 'robots.txt').read_text(encoding='utf-8')
        self.assertIn('Sitemap: https://bmbuilding.sk/sitemap.xml', robots)
        self.assertNotIn('Disallow: /', robots)
        strom = ET.parse(self.ostry / 'sitemap.xml')
        ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        urls = strom.getroot().findall(f'{ns}url')
        self.assertGreaterEqual(len(urls), 8 + len(self.realizacie))
        for u in urls:
            self.assertTrue(u.find(f'{ns}loc').text.startswith('https://bmbuilding.sk/'))
            self.assertRegex(u.find(f'{ns}lastmod').text, r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual((self.ostry / 'CNAME').read_text(encoding='utf-8').strip(), 'bmbuilding.sk')
        self.assertTrue((self.ostry / '.nojekyll').is_file())

    def test_nahlad_noindex_a_disallow(self):
        for subor in sorted(self.nahlad.rglob('*.html')):
            self.assertIn('<meta name="robots" content="noindex, nofollow">', subor.read_text(encoding='utf-8'), subor)
        self.assertEqual((self.nahlad / 'robots.txt').read_text(encoding='utf-8'), 'User-agent: *\nDisallow: /\n')
        self.assertFalse((self.nahlad / 'sitemap.xml').exists())
        self.assertFalse((self.nahlad / 'CNAME').exists())
        # 404 bez <base>, cesty absolútne od /bmbuilding/ (brána pokus 2, správnosť 8)
        s404 = (self.nahlad / '404.html').read_text(encoding='utf-8')
        self.assertNotIn('<base', s404)
        self.assertIn('href="/bmbuilding/realizacie/"', s404)
        self.assertIn('src="/bmbuilding/app.js?v=', s404)
        self.assertIn('src="/bmbuilding/logo.svg"', s404)

    def test_canonical_og_a_relativne_cesty(self):
        s = (self.ostry / 'realizacie' / 'bytovy-dom-rosnicka-bratislava' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/realizacie/bytovy-dom-rosnicka-bratislava/">', s)
        self.assertIn('content="https://bmbuilding.sk/og/bytovy-dom-rosnicka-bratislava.jpg"', s)
        self.assertEqual(stranka_info(self.ostry / 'realizacie' / 'bytovy-dom-rosnicka-bratislava' / 'index.html')['h1'],
                         'Bytový dom Rosnička, Bratislava')
        self.assertIn('href="../../fonts/geist-latin.woff2"', s)
        self.assertIn('url(../../fonts/geist-latin-ext.woff2)', s)
        self.assertIn('src="../../app.js?v=', s)
        self.assertNotRegex(s, r'(href|src)="/(?!/)')
        with Image.open(self.ostry / 'og' / 'bytovy-dom-rosnicka-bratislava.jpg') as im:
            self.assertEqual((im.size, im.format), ((1200, 630), 'JPEG'))

    def test_obrazky_nie_su_vacsie_ako_original(self):
        for p in sorted((self.ostry / 'obrazky').rglob('*.webp')):
            kusy = p.stem.split('-')
            sirka = int(kusy[1])
            with Image.open(p) as im:
                self.assertEqual(im.width, sirka, p)
                self.assertLessEqual(im.width, 1440)
                # výrezy NN-<šírka>-45 (karta pásu 4 : 5) a -54 (mobilný rám pohľadu A 5 : 4), tolerancia 1 px
                if len(kusy) == 3:
                    a, b = build.OREZY[kusy[2]]
                    self.assertLessEqual(abs(im.height - im.width * b / a), 1, p)
                    self.assertLessEqual(im.width, 960, p)
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            self.assertLessEqual(len(re.findall(r'<img[^>]*fetchpriority="high"[^>]*>', s)), 1, subor)
            for img in re.findall(r'<img[^>]*>', s):
                self.assertRegex(img, r'width="\d+" height="\d+"')
                self.assertRegex(img, r'alt="[^"]*"')
        s = (self.ostry / 'index.html').read_text(encoding='utf-8')
        lcp = re.findall(r'<img[^>]*fetchpriority="high"[^>]*>', s)
        self.assertEqual(len(lcp), 1)
        # LCP je prvá fotka slidera úvodu (1. kolo pripomienok, 3.3)
        prva = s.split('<li class="snimka snimka-0 je"', 1)[1].split('</li>', 1)[0]
        self.assertIn(lcp[0], prva)
        self.assertNotIn('loading="lazy"', lcp[0])

    def test_presmerovania(self):
        s = (self.ostry / 'index.php' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('content="0; url=../"', s)
        self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/">', s)
        s = (self.ostry / 'index.php' / 'cinnost' / 'index.html').read_text(encoding='utf-8')
        # sekcia Činnosť má od zmeny 3A kotvu #cinnost; stará adresa /index.php/cinnost vedie na ňu (build.py KOTVY_PREMENOVANE)
        self.assertIn('url=../../#cinnost', s)
        self.assertIn('id="cinnost"', (self.ostry / 'index.html').read_text(encoding='utf-8'))
        self.assertNotIn('#sluzby', s)
        s = (self.ostry / 'index.php/referencie/realizovane-projekty/5-rezidencia-grunty-bratislava-karlova-ves/index.html').read_text(encoding='utf-8')
        self.assertIn('https://bmbuilding.sk/realizacie/rezidencia-grunty-bratislava/', s)
        pocet = len(json.loads((KOREN / 'sablona' / 'presmerovania.json').read_text(encoding='utf-8'))['presmerovania'])
        self.assertEqual(len(list((self.ostry / 'index.php').rglob('index.html'))), pocet)

    def test_bez_pomlciek_a_vykricnikov(self):
        for p in sorted(self.ostry.rglob('*')):
            if p.suffix in ('.html', '.xml', '.txt', '.svg', '.js', '.css') and 'fonts' not in p.relative_to(self.ostry).parts:
                s = p.read_text(encoding='utf-8')
                self.assertNotIn('–', s, p)
                self.assertNotIn('—', s, p)
                if p.suffix in ('.html', '.xml', '.txt'):
                    self.assertNotIn('!', s.replace('<!doctype html>', '', 1), p)

    def test_druhy_beh_rovnake_bajty_a_bez_kodovania(self):
        pred = odtlacky(self.ostry)
        kod, out, err = postav(self.ostry, rychle=True)
        self.assertEqual(kod, 0, err)
        self.assertIn('(nových 0)', out)
        self.assertEqual(odtlacky(self.ostry), pred)

    def test_novinky_len_ked_su(self):
        # Novinky z webu preč (ZMENY-KOLO-1.md 5.7, Andrej 24. 11. 2026 večer): nastavenie novinky false, súbory
        # noviniek v obsahu ostávajú; nezostaví sa /novinky/ ani detail, nikde odkaz, nie sú v sitemap
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertIs(nast['novinky'], False)
        self.assertTrue(any((KOREN / 'obsah' / 'novinky').glob('*.json')))
        self.assertTrue((KOREN / 'obsah' / 'novinky' / 'casopis-asb-2024-02.json').is_file())
        for web in (self.ostry, self.nahlad):
            self.assertFalse((web / 'novinky').exists(), web)
            self.assertFalse((web / 'obrazky-novinky').exists(), web)
            self.assertFalse(list((web / 'og').glob('novinka-*')), web)
            for subor in sorted(web.rglob('*')):
                if subor.is_file() and subor.suffix in ('.html', '.xml', '.txt', '.json', '.webmanifest'):
                    t = subor.read_text(encoding='utf-8', errors='replace')
                    self.assertNotIn('novinky/', t, subor)
                    self.assertNotIn('obrazky-novinky', t, subor)
                    self.assertNotIn('>Novinky<', t, subor)
                    self.assertNotIn('class="novinka-karta', t, subor)
        self.assertNotIn('novink', (self.ostry / 'sitemap.xml').read_text(encoding='utf-8'))
        # s "novinky": true ako predtým: stránka, detail každej novinky, menu na každej stránke a sitemap
        n = skutocny_novinky()
        self.assertEqual(n['o'][0], 0, n['o'][2])
        maju = sorted(p.stem for p in (KOREN / 'obsah' / 'novinky').glob('*.json'))
        self.assertTrue((n['ostry'] / 'novinky' / 'index.html').is_file())
        self.assertEqual(sorted(x['slug'] for x in n['web'].novinky), maju)
        for slug in maju:
            self.assertTrue((n['ostry'] / 'novinky' / slug / 'index.html').is_file(), slug)
        self.assertIn('href="novinky/"', (n['ostry'] / 'index.html').read_text(encoding='utf-8'))
        self.assertIn('<loc>https://bmbuilding.sk/novinky/</loc>', (n['ostry'] / 'sitemap.xml').read_text(encoding='utf-8'))
        # bez kľúča novinky (staršia šablóna) tiež zapnuté
        with mock.patch.object(build, 'SABLONA', sablona_s_nastaveniami(n['tmp'] / 'bez-kluca', novinky=None)), \
                redirect_stderr(io.StringIO()):
            self.assertTrue(build.Web(KOREN / 'obsah', False).novinky)

    def test_galeria_bez_zhodnych_fotiek(self):
        """Linka bmb-fb-uplnost, oprava 1: v jednej realizácii nie je dvakrát tá istá fotka (zo starého webu aj
        z Facebooku, v inom rozlíšení). dHash 12 x 12 = 144 bitov, zhodné páry mali 0 až 5 bitov, prah 6 ako
        PRAH_ZHODA v fb/nastroje/zapis_obsah.py."""
        def dhash(cesta, n=12):
            with Image.open(cesta) as im:
                g = im.convert('L').resize((n + 1, n), Image.LANCZOS)
            px = list(g.getdata())
            b = 0
            for y in range(n):
                for x in range(n):
                    b = (b << 1) | (px[y * (n + 1) + x] > px[y * (n + 1) + x + 1])
            return b

        zhodne = []
        pocet = 0
        for p in sorted((KOREN / 'obsah' / 'realizacie').glob('*.json')):
            r = json.loads(p.read_text(encoding='utf-8-sig'))
            if r.get('zverejnene') is False:
                continue
            h = [(f['subor'], dhash(KOREN / f['subor'])) for f in r.get('fotky') or []
                 if isinstance(f, dict) and (KOREN / str(f.get('subor'))).is_file()]
            pocet += len(h)
            for i in range(len(h)):
                for j in range(i + 1, len(h)):
                    d = bin(h[i][1] ^ h[j][1]).count('1')
                    if d <= 6:
                        zhodne.append((p.stem, h[i][0].split('/')[-1], h[j][0].split('/')[-1], d))
        self.assertGreater(pocet, 0)
        self.assertEqual(zhodne, [])

    def test_galeria_bez_zhodnych_orezov(self):
        """Linka bmb-fb-uplnost, oprava 2: v jednej realizácii nie je ten istý záber s iným orezom (posun orezu,
        PRAH_OREZ). Sabotáž fb/uplnost/oprava2_sabotaz_orezy.py (obsah pred opravou 2) musí zlyhať."""
        zhodne, pocet = zhodne_orezy(KOREN / 'obsah')
        self.assertGreater(pocet, 0)
        self.assertEqual(zhodne, [])


class TestZnacka(unittest.TestCase):
    """Dizajn 3: logo, favicon, OG, farby a písmo zo značky BM Building (logo 2026 a banner na plote)."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.nahlad = s['ostry'], s['nahlad']
        cls.css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')

    def test_logo_v_hlavicke_a_platne_svg(self):
        svg = (self.ostry / 'logo.svg').read_bytes()
        self.assertEqual(svg, (KOREN / 'sablona' / 'logo.svg').read_bytes())
        koren = ET.fromstring(svg)
        self.assertEqual(koren.tag, '{http://www.w3.org/2000/svg}svg')
        self.assertEqual(koren.get('viewBox'), '0 0 936 936')
        self.assertEqual(koren.get('role'), 'img')
        self.assertEqual(koren.find('{http://www.w3.org/2000/svg}title').text, 'BM Building')
        rect = koren.find('{http://www.w3.org/2000/svg}rect')
        self.assertEqual(rect.get('fill').lower(), '#17476f')
        # SVG sa dá vykresliť: biela cesta loga pokrýva podiel plochy ako v origináli (BM, linka, BUILDING)
        im = build.svg_obraz(svg.decode('utf-8'), 200)
        biele = sum(1 for p in im.getdata() if min(p) > 200) / (200 * 200)
        self.assertTrue(0.2 < biele < 0.5, biele)
        self.assertTrue(bliz(im.getpixel((2, 2)), MODRA))
        # biela linka loga: y 611,3 až 626,8 z 936 je pri 200 px riadok 130,6 až 133,9, stred 132 (LOGO.md bod 1);
        # riadok 129 (603,7 z 936) je ešte modrá medzera pod BM, preto test predtým padal na zlom mieste
        self.assertTrue(min(im.getpixel((100, 132))) > 200)
        self.assertTrue(bliz(im.getpixel((100, 127)), MODRA, 12))  # medzera nad linkou je modrá
        # hlavička každej stránky: logo v podobe starého webu (logo-hlavicka.svg) s alt textom (1. kolo pripomienok, 3.1)
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            hlavicka = s.split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
            m = re.search(r'<a class="znak" href="[^"]*"><img class="znak-logo" src="([^"]*)" width="(\d+)" height="(\d+)" '
                          r'alt="([^"]+)"></a>', hlavicka)
            self.assertIsNotNone(m, subor)
            self.assertTrue(m.group(1).endswith('logo-hlavicka.svg'), subor)
            self.assertEqual(m.group(4), 'BM Building, spoločnosť s ručením obmedzeným, úvod')
            self.assertIn('<meta name="theme-color" content="#17476f">', s)

    def test_logo_hlavicky_na_krivkach(self):
        """1. kolo pripomienok 3.1: logo hlavičky (štvorec BM BUILDING, „BM Building“, „spoločnosť s ručením
        obmedzeným“) s textom na krivkách, bez elementu text, nezávislé od písma; modrá loga, štvorec zo sablona/logo.svg."""
        svg = (self.ostry / 'logo-hlavicka.svg').read_bytes()
        self.assertEqual(svg, (KOREN / 'sablona' / 'logo-hlavicka.svg').read_bytes())
        koren = ET.fromstring(svg)
        ns = '{http://www.w3.org/2000/svg}'
        self.assertEqual(koren.tag, ns + 'svg')
        self.assertEqual([e for e in koren.iter() if e.tag in (ns + 'text', ns + 'tspan', ns + 'textPath', ns + 'image',
                                                                ns + 'use', ns + 'style', ns + 'script')], [])
        self.assertNotIn(b'<text', svg)
        self.assertNotIn(b'font-family', svg)
        self.assertNotIn(b'href', svg)
        _, _, vw, vh = (float(x) for x in koren.get('viewBox').split())
        self.assertGreater(vw / vh, 4)
        self.assertLess(vw / vh, 6.5)
        rect = koren.find(ns + 'rect')
        self.assertEqual((rect.get('fill').lower(), rect.get('width'), rect.get('height')), ('#17476f', str(round(vh)), str(round(vh))))
        cesty = koren.findall(ns + 'path')
        self.assertEqual([c.get('fill').lower() for c in cesty], ['#fff', '#17476f', '#17476f'])
        # vykreslenie: vľavo modrý štvorec s bielym BM, vpravo modrý text na bielom (žiadna iná farba)
        im = build.svg_obraz(svg.decode('utf-8'), 600)
        sirka, vyska = im.size
        stvorec = vyska
        self.assertTrue(bliz(im.getpixel((2, 2)), MODRA))
        biele_v_stvorci = sum(1 for x in range(0, stvorec, 3) for y in range(0, vyska, 3)
                              if min(im.getpixel((x, y))) > 200) / ((stvorec // 3 + 1) ** 2)
        self.assertTrue(0.2 < biele_v_stvorci < 0.5, biele_v_stvorci)
        text = [im.getpixel((x, y)) for x in range(stvorec + 10, sirka, 2) for y in range(vyska)]
        modre = sum(1 for p in text if bliz(p, MODRA, 30)) / len(text)
        self.assertTrue(0.08 < modre < 0.45, modre)

    def test_kontaktny_blok_z_banneru(self):
        # kontaktný blok v poradí banneru na plote (BM Building, s.r.o., Podhorská 9, 902 01 Pezinok, tel., e-mail, web)
        # ako poznávací prvok v závere stránky aj na Kontakte; logo pri ňom; dve tlačidlá Zavolať a Napísať e-mail
        firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))
        for cesta in ('index.html', 'kontakt/index.html', 'realizacie/rd-pezinok/index.html'):
            s = (self.ostry / cesta).read_text(encoding='utf-8')
            blok = s.split('<address class="vizitka', 1)[1].split('>', 1)[1].split('</address>', 1)[0]
            self.assertIn('logo.svg', blok, cesta)
            text = html.unescape(re.sub(r'<[^>]+>', '|', blok)).replace(' ', ' ')
            kusy = [k.strip() for k in text.split('|') if k.strip()]
            self.assertEqual(kusy, [firma['meno'], 'Podhorská 9', '902 01 Pezinok', 'tel.:', firma['telefon'], 'e-mail:',
                                    firma['email'], 'www.bmbuilding.sk'], cesta)
            self.assertIn(f'<a class="tlacidlo" href="tel:{firma["telefon_href"]}">Zavolať', s, cesta)
            self.assertIn(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{firma["email"]}">Napísať e-mail</a>', s, cesta)
            ids = re.findall(r'\sid="([^"]+)"', s)
            self.assertEqual(len(ids), len(set(ids)), cesta)
            # blok je v karte sekcie Kontakt z ukážky (namiesto ilustračnej mapy), sekcia má všetky údaje starého webu
            kontakt = s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
            self.assertIn('<figure class="mapa-karta"><address class="vizitka', kontakt, cesta)
            text = html.unescape(re.sub(r'<[^>]+>', ' ', kontakt)).replace(' ', ' ')
            for kus in (firma['meno'], firma['konatel'], 'Podhorská 9', '902 01 Pezinok', firma['ico'], firma['ic_dph'],
                        '36965/B', 'Tatra banka, a.s.', '262 180 9127/1100', 'Navigovať', 'Mapy.cz'):
                self.assertIn(kus, text, (cesta, kus))
            # Facebook ako ikona s aria-label (1. kolo pripomienok 3.7), nie text
            self.assertIn(f'<a class="siet" href="{firma["facebook"]}" target="_blank" rel="noopener" '
                          'aria-label="Facebook BM Building">', kontakt, cesta)
        # plachta, plot a lockup zamietnutého smeru aj starý záver a sekcia Kde už nikde nie sú
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            for stare in ('class="plachta', 'class="plot"', 'class="lockup', 'class="ocko', 'logo-linka', 'class="sekcia zaver',
                          'class="kde', 'class="hlavicka'):
                self.assertNotIn(stare, s, (subor, stare))

    def test_favicon_a_og_zo_znacky(self):
        self.assertEqual((self.ostry / 'favicon.svg').read_bytes(), (KOREN / 'sablona' / 'logo-favicon.svg').read_bytes())
        self.assertNotIn('#194471', (self.ostry / 'favicon.svg').read_text(encoding='utf-8'))
        with Image.open(self.ostry / 'favicon.ico') as im:
            self.assertIn((32, 32), im.info.get('sizes', {(im.width, im.height)}))
            im = im.convert('RGB')
            self.assertTrue(bliz(im.getpixel((1, 1)), MODRA), im.getpixel((1, 1)))
        with Image.open(self.ostry / 'apple-touch-icon.png') as im:
            self.assertEqual(im.size, (180, 180))
        with Image.open(self.ostry / 'logo.png') as im:
            self.assertEqual(im.size, (512, 512))
            im = im.convert('RGB')
            self.assertTrue(bliz(im.getpixel((5, 5)), MODRA))
            self.assertTrue(min(im.getpixel((256, 340))) > 200)  # linka loga
        for og in ('og/bmbuilding.jpg', 'og/bytovy-dom-rosnicka-bratislava.jpg', 'og/rd-ahoj-bratislava-nove-mesto.jpg'):
            if not (self.ostry / og).is_file():
                continue
            with Image.open(self.ostry / og) as im:
                self.assertEqual(im.size, (1200, 630))
                im = im.convert('RGB')
                self.assertTrue(bliz(im.getpixel((20, 20)), MODRA, 10), (og, im.getpixel((20, 20))))
                # biely rám loga vľavo hore a biela linka pod nadpisom
                self.assertTrue(min(im.getpixel((61, 100))) > 225, og)
                riadky_biele = [y for y in range(240, 560) if sum(1 for x in range(70, 330) if min(im.getpixel((x, y))) > 225) > 240]
                self.assertTrue(riadky_biele, og)
        uzol = [u for u in stranka_info(self.ostry / 'index.html')['ld'] if 'Organization' in u.get('@type', [])][0]
        self.assertEqual(uzol['logo'], 'https://bmbuilding.sk/logo.png')
        self.assertEqual(uzol['image'], 'https://bmbuilding.sk/logo.png')
        # logo.png je raster zo sablona/logo.svg, nie stará kópia obsah/logo.png
        with Image.open(self.ostry / 'logo.png') as im:
            self.assertEqual(im.convert('RGB').tobytes(), build.svg_obraz(build.SABLONA.joinpath('logo.svg').read_text(encoding='utf-8'), 512).tobytes())
        # stará modrá #194471 nikde vo výstupe ani v šablóne
        for p in list(self.ostry.rglob('*.html')) + list(self.ostry.rglob('*.svg')) + [self.ostry / 'app.js']:
            self.assertNotIn('194471', p.read_text(encoding='utf-8').lower(), p)
        for p in (KOREN / 'sablona').glob('*.*'):
            if p.suffix in ('.css', '.js', '.json', '.svg'):
                self.assertNotIn('194471', p.read_text(encoding='utf-8').lower(), p)
        # apple-touch-icon 180 je kópia hotového rastra zo sablona/favicon/ (variant A), od SVG sa líši len hranami písmen
        fav = build.SABLONA.joinpath('logo-favicon.svg').read_text(encoding='utf-8')
        with Image.open(self.ostry / 'apple-touch-icon.png') as im:
            z_svg = build.svg_obraz(fav, 180)
            rozdiel = sum(abs(a - b) for a, b in zip(im.convert('RGB').tobytes(), z_svg.tobytes())) / (180 * 180 * 3 * 255)
            self.assertLess(rozdiel, 0.02)

    def test_favicon_variant_a_subory_a_hlavicka(self):
        """Favicon variantu A (ops/.../favicon/VYBER.md): súbory sú po bajtoch kópie zo sablona/favicon/, rastre majú
        správne rozmery, ICO obsahuje 16, 32 a 48 px s modrým rohom, manifest je platný a ikony v ňom existujú."""
        sab = KOREN / 'sablona' / 'favicon'
        for meno in build.FAVICON_SUBORY:
            self.assertEqual((self.ostry / meno).read_bytes(), (sab / meno).read_bytes(), meno)
        for meno, n in (('favicon-32.png', 32), ('favicon-48.png', 48), ('apple-touch-icon.png', 180), ('icon-192.png', 192),
                        ('icon-512.png', 512)):
            with Image.open(self.ostry / meno) as im:
                self.assertEqual(im.size, (n, n), meno)
                self.assertEqual(im.format, 'PNG')
                self.assertTrue(bliz(im.convert('RGB').getpixel((1, 1)), MODRA), meno)
                if meno == 'apple-touch-icon.png':
                    self.assertEqual(im.mode, 'RGB')  # bez priehľadnosti
        with Image.open(self.ostry / 'favicon.ico') as ico:
            self.assertEqual(ico.format, 'ICO')
            self.assertEqual(sorted(ico.info['sizes']), [(16, 16), (32, 32), (48, 48)])
            for n in (16, 32, 48):
                ico.size = (n, n)
                ico.load()
                self.assertTrue(bliz(ico.convert('RGB').getpixel((0, 0)), MODRA), n)
        # favicon.svg je platné SVG zo sablona/logo-favicon.svg s modrým štvorcom a bielou kresbou, bez textu a štýlov
        svg = (self.ostry / 'favicon.svg').read_text(encoding='utf-8')
        koren = ET.fromstring(svg)
        self.assertEqual(koren.get('viewBox'), '0 0 32 32')
        self.assertNotIn('<text', svg)
        self.assertNotIn('style', svg)
        # manifest: platný JSON, názov firmy, farba ako meta theme-color, ikony existujú a majú deklarovaný rozmer
        man = json.loads((self.ostry / 'site.webmanifest').read_text(encoding='utf-8'))
        self.assertEqual(man['name'], 'BM Building, s.r.o.')
        self.assertEqual(man['theme_color'], '#17476f')
        self.assertEqual(man['lang'], 'sk')
        self.assertFalse(man['start_url'].startswith('/'), 'start_url musí byť relatívny (náhľad na podadrese)')
        self.assertEqual(sorted(i['sizes'] for i in man['icons']), ['192x192', '512x512'])
        for i in man['icons']:
            self.assertFalse(i['src'].startswith(('/', 'http')), i)
            with Image.open(self.ostry / i['src']) as im:
                self.assertEqual('%dx%d' % im.size, i['sizes'])
        # hlavička každej stránky (ostrý web aj náhľad): SVG, PNG 32 a 48 so sizes, ICO, apple-touch-icon, manifest,
        # theme-color a manifest-src v CSP; 404 má cesty od základu webu
        for web in (self.ostry, self.nahlad):
            for subor in stranky(web):
                s = subor.read_text(encoding='utf-8')
                if subor.name == '404.html' and subor.parent == web:
                    pre = '/' if web == self.ostry else '/bmbuilding/'
                else:
                    pre = '../' * (len(subor.relative_to(web).parts) - 1)
                for kus in (f'<link rel="icon" href="{pre}favicon.svg" type="image/svg+xml">',
                            f'<link rel="icon" href="{pre}favicon-32.png" type="image/png" sizes="32x32">',
                            f'<link rel="icon" href="{pre}favicon-48.png" type="image/png" sizes="48x48">',
                            f'<link rel="icon" href="{pre}favicon.ico" sizes="16x16 32x32 48x48">',
                            f'<link rel="apple-touch-icon" href="{pre}apple-touch-icon.png" sizes="180x180">',
                            f'<link rel="manifest" href="{pre}site.webmanifest">',
                            '<meta name="theme-color" content="#17476f">'):
                    self.assertIn(kus, s, (subor.relative_to(web), kus))
                self.assertIn("manifest-src 'self'", s, subor)
        n404 = (self.nahlad / '404.html').read_text(encoding='utf-8')
        for kus in ('href="/bmbuilding/favicon.svg"', 'href="/bmbuilding/favicon-32.png"', 'href="/bmbuilding/favicon-48.png"',
                    'href="/bmbuilding/apple-touch-icon.png"', 'href="/bmbuilding/site.webmanifest"'):
            self.assertIn(kus, n404)
        # presmerovacie stránky starých adries ostávajú s prázdnou ikonou (data:,)
        for p in (self.ostry / 'index.php').rglob('*.html'):
            self.assertIn('<link rel="icon" href="data:,">', p.read_text(encoding='utf-8'), p)
            self.assertNotIn('favicon', p.read_text(encoding='utf-8'), p)

    def test_ziadna_zlta_ani_bezova(self):
        farby = farby_css(self.css)
        self.assertGreater(len(farby), 10)
        zle = [f for f in farby if zlta_alebo_bezova(f)]
        self.assertEqual(zle, [])
        # farby ukážky Olovnica zo zadania; bronz #a8784a nahradila modrá z loga a jej odtiene
        for kus in ('--modra:#17476f', '--vapno:#f4f4f1', '--grafit:#1e1f1f', '--beton:#c9c8c3', '--modra-ciara:#3b7ab3',
                    '--modra-svetla:#7fb0dc'):
            self.assertIn(kus, self.css)
        # tmavý pás Vybrané realizácie je z úvodu preč (ZMENY-KOLO-1.md 3A.6)
        self.assertNotIn('.prace{', self.css)
        for bronz in ('a8784a', '8a5f36', 'c99b6d'):
            self.assertNotIn(bronz, self.css.lower())
        # žiadne prechody farieb: linear-gradient len z jednej farby (vlasová čiara, kóta, uhlopriečky rámu)
        for g in re.findall(r'(?:linear|radial|conic)-gradient\(([^;{}]*?)\)\s*(?:[;}\s,0-9])', self.css):
            farby_g = set(re.findall(r'var\(--[a-z0-9-]+\)|#[0-9a-fA-F]{3,6}', g)) - {'transparent'}
            self.assertLessEqual(len(farby_g), 1, g)
        # žiadne tiene kariet
        self.assertNotIn('box-shadow', self.css)
        # kontrola sama zachytí žltú a béžovú zamietnutého smeru, nie takmer bielu vápno ukážky
        self.assertTrue(zlta_alebo_bezova((0xEC, 0xEA, 0xE4)))
        self.assertTrue(zlta_alebo_bezova((0xF2, 0xC1, 0x2E)))
        self.assertTrue(zlta_alebo_bezova((0xA8, 0x78, 0x4A)))
        self.assertFalse(zlta_alebo_bezova((0xE4, 0xE8, 0xEB)))
        self.assertFalse(zlta_alebo_bezova((0xF4, 0xF4, 0xF1)))
        for subor in (self.ostry / 'favicon.svg', self.ostry / 'logo.svg'):
            self.assertEqual([f for f in farby_css(subor.read_text(encoding='utf-8')) if zlta_alebo_bezova(f)], [])

    def test_pismo_geist(self):
        # Geist a Geist Mono z ukážky (OFL 1.1) lokálne; Arimo ostáva len pre text v OG obrázkoch
        hlava = self.css.split(':root', 1)[0]
        self.assertEqual(sorted(set(re.findall(r'font-family:"([^"]+)"', hlava))), ['BMB Mono', 'BMB Sans'])
        for meno in ('Archivo', 'Anybody', 'DM Mono', 'bmb-nadpis', 'bmb-mono', 'Arimo', 'Olovnica Sans'):
            self.assertNotIn(meno, self.css)
        s = (self.ostry / 'index.html').read_text(encoding='utf-8')
        preload = re.findall(r'<link rel="preload"[^>]*as="font"[^>]*>', s)
        # úvod má navyše preload prvej fotky slidera (LCP, 1. kolo pripomienok 3.3), jeden pre všetky šírky
        obr = re.findall(r'<link rel="preload" as="image"[^>]*>', s)
        self.assertEqual(len(obr), 1)
        # linka bmb-fotky: preload len formátu, ktorý picture vyberie (AVIF, bez kodeku WebP), nie oba
        self.assertRegex(obr[0], r'type="image/(avif|webp)" imagesrcset="obrazky/[a-z0-9-]+/\d\d-\d+\.(avif|webp) \d+w')
        self.assertIn(f'imagesizes="{build.SIZES_SLIDER}" fetchpriority="high"', obr[0])
        self.assertNotIn('-54.', obr[0])
        # preload len Geist latinka a diakritika; Geist Mono bez preloadu (PLAN-1-1.md časť 8 bod 3, LCP na mobile)
        self.assertEqual(preload, [f'<link rel="preload" href="fonts/{p}" as="font" type="font/woff2" crossorigin>'
                                   for p in ('geist-latin.woff2', 'geist-latin-ext.woff2')])
        ukazka = KOREN.parent / 'arling-sk' / 'web' / 'ukazky' / 'architekti' / 'fonts'
        for meno in ('geist-latin.woff2', 'geist-latin-ext.woff2', 'geist-mono-latin.woff2', 'geist-mono-latin-ext.woff2'):
            self.assertEqual((self.ostry / 'fonts' / meno).read_bytes(), (KOREN / 'sablona' / meno).read_bytes())
            if ukazka.is_dir():
                self.assertEqual((KOREN / 'sablona' / meno).read_bytes(), (ukazka / meno).read_bytes(), meno)
        self.assertIn('SIL Open Font License', (self.ostry / 'fonts' / 'OFL-Geist.txt').read_text(encoding='utf-8'))
        lic = (KOREN / 'LICENCIE.md').read_text(encoding='utf-8')
        for kus in ('Geist', 'OFL-Geist.txt', 'Arimo'):
            self.assertIn(kus, lic)
        self.assertIn('fonts/OFL-Geist.txt', (self.ostry / 'ochrana-udajov' / 'index.html').read_text(encoding='utf-8'))
        znaky = set((KOREN / 'sablona' / 'pismo-znaky.txt').read_text(encoding='utf-8').rstrip('\n'))
        for ch in 'áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ „“…€·ı':
            self.assertIn(ch, znaky)
        # Geist nemá U+2011, sadzba preto drží „3-izbové“ spolu cez span.nz
        self.assertNotIn('‑', znaky)
        self.assertEqual(build.sadzba('6 x 3-izbové byty'), '6 x <span class="nz">3-izbové</span> byty')
        self.assertIn('.nz{white-space:nowrap}', self.css)

    def test_styl_ciara_kostra(self):
        # vlasové čiary stĺpcov, mriežka 4, 8 a 12 stĺpcov a nadpis podstránky ako v ukážke; olovnica v mene, schéma,
        # Postup a O firme ako sekcia sú od 1. kola pripomienok preč (ZMENY-KOLO-1.md 3.3 a 3.6)
        for kus in ('--stlpce:4', '--stlpce:8', '--stlpce:12', '.ciary-vnutro span{border-left:1px solid var(--ciara)}',
                    '--krivka:cubic-bezier(.65,0,.35,1)', 'border-radius:2px',
                    # komponenty ukážky Olovnica (PLAN-1-1.md časť 1)
                    '.hlava .obsah{',
                    '.cennik{', '.sluzba{', '.atelier-text{', '.kontakt{', '.mapa-karta{', '.pata-odkazy{',
                    # hrdina 1. kola: slider a O firme v oblastiach mriežky, pripnutá hlavička
                    '.uvod-mriezka{grid-template-areas:', '.hlava{position:sticky;top:0;', '.snimka.je{'):
            self.assertIn(kus, self.css)
        for stare in ('.olovnica', '.meno-velky', '.meno-maly', '.plan-ramec', '.vrstva', '.uvod-plan', '.fazy', '.faza',
                      '.postup', '.atelier-foto', '.atelier-mriezka', '.stav-bod', '.pohlad-odkaz', '@container plan',
                      '.prace', '.pas{', '.pas>', '.pas-ovladanie', '.sipka', '.navod-mys', '.projekt-dalsi', '.prepinac'):
            self.assertNotIn(stare, self.css, stare)
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            # čiary stĺpcov raz na stránku (úvod mal druhé v tmavom páse, ten je od ÚVOD v3 preč)
            self.assertEqual(s.count(build.CIARY_HTML), 1, subor)
            if subor.parent == self.ostry:
                continue
            telo = s.split('<main', 1)[1]
            self.assertRegex(telo, r'<div class="hlava-stranky[^"]*"><div class="obsah">', subor)
            self.assertRegex(telo, r'<h1 class="nadpis-stranky" id="nadpis">', subor)
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        for stare in ('class="olovnica"', 'class="meno-velky"', 'class="meno-maly"', 'ı', 'id="schema"', 'class="pohlad"',
                      'id="postup"', 'Pohľad A', 'class="stav', 'id="prace"'):
            self.assertNotIn(stare, uvod, stare)

    def test_pohyb_len_bez_obmedzenia(self):
        css = self.css
        pohyb = css.split('@media (prefers-reduced-motion:no-preference){', 1)
        self.assertEqual(len(pohyb), 2)
        vnutri, za = pohyb[1].split('\n}\n', 1)
        mimo = pohyb[0] + za
        # mimo bloku len animation:none (konečný stav pri obmedzenom pohybe, ÚVOD v3)
        self.assertNotRegex(mimo, r'animation:(?!none;)')
        for kus in ('.pohlad-ramec img{--odkryt-od:inset(0 0 12% 0);animation:odkryt 640ms', '.pohlad-ramec img{--odkryt-od:inset(0 0 0 100%)'):
            self.assertIn(kus, vnutri)
        # pás s časovou osou posunu je preč (ZMENY-KOLO-1.md 3A.6)
        for stare in ('pas-posun', 'animation-timeline', 'view-timeline'):
            self.assertNotIn(stare, css, stare)
        # olovnica a výkres sú preč; kreslit-zlava je späť pre riadok kľúčových slov úvodu (ÚVOD v3)
        for stare in ('kyv', 'kreslit-zdola', '.v-obrys', '.plan-kota'):
            self.assertNotIn(stare, css, stare)
        # pri obmedzenom pohybe sú prechody vypnuté, aj prelínanie fotiek slidera (app.js navyše bez automatického prechodu)
        self.assertIn('@media (prefers-reduced-motion:reduce)', mimo)
        obmedzeny = css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('\n}\n', 1)[0]
        self.assertRegex(obmedzeny, r'[,{]\.snimka,\.slider-popis,[^{]*\{transition:none\}')
        # animuje sa len transform, opacity a clip-path, nie rozmery
        for kf in re.findall(r'@keyframes [a-z-]+\{(.*?)\}\}', css):
            self.assertNotRegex(kf, r'(^|[{;])(width|height|top|left|margin|padding)\s*:', kf)
        # čiary sa kreslia cez clip-path a scaleX, nie SVG pathLength s non-scaling-stroke (Firefox)
        for subor in [self.ostry / 'index.html', self.ostry / 'realizacie' / 'index.html']:
            s = subor.read_text(encoding='utf-8')
            self.assertNotIn('pathLength=', s)
            self.assertNotIn('non-scaling-stroke', s)
        self.assertNotIn('non-scaling-stroke', css)
        # obsah sa neskrýva natrvalo: trieda js zmizne, keď app.js nepribehne
        self.assertIn("if(window.BMB==null)d.classList.remove('js')", build.SKRIPT_JS)
        # číslice kót bez atribútu style (CSP): poloha v triedach c0 až c9 a i0 až i9
        self.assertIn('.c9{--c:9}', css)
        self.assertIn('.i9{--i:9}', css)

    def test_uvod_pohyb_ako_predtym(self):
        """ÚVOD v3 (Andrej: „aj tie animácie prvkov nech tam sú, aké tam predtým boli“): pohybový jazyk pôvodného webu.
        Prvá fotka slidera sa odkryje (clip-path a zmenšenie z 1,04, 640 ms), kľúčové slová sa nakreslia zľava, H1,
        odseky, údaje, zdroj a tlačidlá sa objavia postupne s posunom, číslice kót sa raz pretočia, karty Činnosti
        sa objavia pri vstupe do obrazu; len transform, opacity a clip-path, krivka --krivka; pri obmedzenom pohybe
        hneď konečný stav; bez JS všetko viditeľné."""
        css = self.css
        vnutri = css.split('@media (prefers-reduced-motion:no-preference){', 1)[1].split('\n}\n', 1)[0]
        for kf in ('@keyframes zjavit{from{opacity:0}to{opacity:1}}',
                   '@keyframes zjavit-posun{from{opacity:0;transform:translate3d(0,14px,0)}to{opacity:1;transform:none}}',
                   '@keyframes kreslit-zlava{from{clip-path:inset(-4% 100% -4% -4%)}to{clip-path:inset(-4% -4% -4% -4%)}}',
                   '@keyframes odkryt{from{clip-path:var(--odkryt-od);transform:scale(1.04)}to{clip-path:inset(0 0 0 0);transform:none}}'):
            self.assertIn(kf, vnutri)
        oneskorenia = []
        for sel, anim in (('.uvod-nadtitulok', 'kreslit-zlava 520ms'), ('.uvod-h1', 'zjavit-posun 560ms'),
                          ('.firma-text', 'zjavit-posun 560ms'), ('.firma-fakty .udaj', 'zjavit-posun 480ms'),
                          ('.firma-zdroj', 'zjavit 400ms'), ('.uvod-akcie', 'zjavit-posun 480ms')):
            m = re.search(re.escape(sel) + r'\{animation:' + re.escape(anim) + r' var\(--krivka\) (\d+)ms both\}', vnutri)
            self.assertIsNotNone(m, sel)
            oneskorenia.append(int(m.group(1)))
        # v poradí čítania, kroky 40 až 120 ms (údaje medzi sebou po 60 ms)
        self.assertEqual(oneskorenia, sorted(oneskorenia))
        for a, b in zip(oneskorenia, oneskorenia[1:]):
            self.assertTrue(40 <= b - a <= 260, oneskorenia)
        self.assertIn('.snimka-0 img{--odkryt-od:inset(0 0 12% 0);animation:odkryt 640ms var(--krivka) 0ms both}', vnutri)
        self.assertIn('.firma-fakty .udaj:nth-child(2){animation-delay:420ms}', vnutri)
        # sekcie pod hrdinom: skryté len s triedou js a bez obmedzeného pohybu (bez JS všetko viditeľné)
        self.assertIn('.js .vstup:not(.videne){opacity:0;transform:translate3d(0,28px,0)}', vnutri)
        self.assertNotIn('.vstup:not(.videne)', css.replace(vnutri, ''))
        self.assertIn("if(window.BMB==null)d.classList.remove('js')", build.SKRIPT_JS)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("vsetky('.cinnost-sekcia .nadpis, .cinnost-karta, .cinnost-sksi, .zakon-list')", js)
        self.assertIn("z.target.classList.add('videne')", js)
        # vysoká karta Činnosti (na mobile aj 2 000 px) sa objaví hneď pri vstupe horného okraja, nie po podiele výšky
        self.assertRegex(js, r"var ioVstup = new IntersectionObserver\(function \(zaznamy\) \{[^}]*\{[^}]*\}\);\s*"
                             r"\}, \{ threshold: 0, rootMargin: '0px 0px -8% 0px' \}\);")
        self.assertIn("vsetky('.otoc')", js)
        # číslice kót sa pretočia: počítadlo slidera a čísla Činnosti majú triedu otoc
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<span class="kota slider-pocet"><span class="kota-text cisla otoc">', uvod)
        cinnost = uvod.split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(cinnost.count('<span class="kota kota-velka"><span class="kota-text otoc">'), 3)
        # pri obmedzenom pohybe konečný stav (žiadna animácia, plná nepriehľadnosť, bez posunu a orezu)
        rm = css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('{animation:none;opacity:1;transform:none;clip-path:none}', rm)
        for sel in ('.snimka-0 img', '.uvod-nadtitulok', '.uvod-h1', '.firma-text', '.firma-fakty .udaj', '.firma-zdroj',
                    '.uvod-akcie', '.vstup'):
            self.assertIn(sel, rm.split('{animation:none;opacity:1;transform:none;clip-path:none}', 1)[0])


class TestStranky(unittest.TestCase):
    """Úvod, služby, zoznam a stránky realizácií nad skutočným obsahom."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry = s['ostry']
        cls.realizacie = s['realizacie']
        cls.w = s['web']
        # Novinky sú v šablóne vypnuté (ZMENY-KOLO-1.md 5.7); testy noviniek bežia nad zostavou s "novinky": true
        n = skutocny_novinky()
        cls.ostry_n = n['ostry']
        cls.w_n = n['web']
        cls.texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        cls.firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))
        cls.css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    def info_n(self, cesta):
        return stranka_info(self.ostry_n / cesta / 'index.html' if cesta else self.ostry_n / 'index.html')

    def test_uvod_poradie_a_h1(self):
        """ZMENY-KOLO-1.md 3A.1 až 3A.3: H1 presne Andrejova veta, nad ním nadtitulok s kľúčovými slovami, veta
        o 20 rokoch nikde v úvode; poradie hrdina (H1, slider, údaje), hneď pod ním Činnosť a za ňou hneď Kontakt
        (tmavý pás Vybrané realizácie je preč, 3A.6)."""
        t = self.info('')
        self.assertEqual(t['h1'], 'BM Building, s.r.o. je stavebno-manažérska spoločnosť so sídlom v Pezinku.')
        self.assertEqual(self.texty['uvod']['h1'], t['h1'])
        telo = t['telo']
        # „stavebno-manažérska“ sa nezalomí za spojovníkom (Geist nemá U+2011)
        self.assertIn('<span class="nz">stavebno-manažérska</span>', telo.split('<h1', 1)[1].split('</h1>', 1)[0])
        # 20 rokov nikde v úvode (novinka o 20 rokoch ostáva v Novinkách), ani v textoch úvodu
        for kus in ('20 rokov', 'oslávila', 'pôsobenia na trhu'):
            self.assertNotIn(kus, telo, kus)
            self.assertNotIn(kus, json.dumps(self.texty['uvod'], ensure_ascii=False), kus)
        self.assertNotIn('firma-veta', telo)
        nov = json.loads((KOREN / 'obsah' / 'novinky' / '20-rokov-na-trhu-2025-08.json').read_text(encoding='utf-8'))
        self.assertIn('20 rokov pôsobenia na trhu', nov['text'])
        # nadtitulok nad H1: kľúčové slová z texty.json spojené bodkou v strede
        hlava = telo.split('<div class="firma-hlava" id="firma" data-vyzva-hore>', 1)[1].split('</div>', 1)[0]
        nad = re.search(r'<p class="uvod-nadtitulok">(.*?)</p>', hlava)
        self.assertIsNotNone(nad)
        self.assertLess(hlava.index('uvod-nadtitulok'), hlava.index('<h1 class="uvod-h1" id="uvod-nadpis">'))
        self.assertEqual(html.unescape(nad.group(1)).replace(' ', ' ').split(' · '), self.texty['uvod']['nadtitulok'])
        poradie = [telo.index(k) for k in ('<section class="uvod"', '<div class="firma-hlava" id="firma"', '<div class="slider"',
                                             '<div class="firma-udaje">', 'id="cinnost"',
                                             '<section class="sekcia kontakt" id="kontakt"')]
        self.assertEqual(poradie, sorted(poradie))
        self.assertEqual(telo.count('id="firma"'), 1)
        for stare in ('id="postup"', '#postup', 'class="meno', 'class="olovnica"', 'id="schema"', 'class="pohlad"',
                      'class="atelier-foto"', 'Na trhu od roku 2005', 'stav-text', 'Pohľad A', 'id="sluzby"', '#sluzby'):
            self.assertNotIn(stare, t['html'], stare)
        self.assertNotIn('pas-ukazky', t['html'])
        self.assertNotIn('Ukážka webu', t['html'])
        # Činnosť hneď pod hrdinom: medzi hrdinom a Činnosťou nič iné
        medzi = telo.split('</section>', 1)[1].split('<section', 1)[0]
        self.assertEqual(medzi.strip(), '')
        self.assertTrue(telo.split('</section>', 1)[1].lstrip().startswith('<section class="sekcia cinnost-sekcia" id="cinnost"'))
        # za Činnosťou hneď Kontakt, nič medzi nimi (žiadna prázdna medzera po páse)
        za_cinnostou = telo.split('id="cinnost"', 1)[1].split('</section>', 1)[1]
        self.assertTrue(za_cinnostou.lstrip().startswith('<section class="sekcia kontakt" id="kontakt"'), za_cinnostou[:120])
        # žiadne pred a po na úvode ani pás čísel zamietnutého smeru, žiadne novinky ani sekcia Kde na úvode
        self.assertNotIn('data-pred-po', telo)
        self.assertNotIn('data-porovnanie', telo)
        self.assertNotIn('class="dokazy"', telo)
        self.assertNotIn('novinka-karta', telo)
        self.assertNotIn('id="kde"', telo)

    def test_uvod_cinnost_doslova(self):
        """ZMENY-KOLO-1.md 3A.3 a časť 4: Činnosť na úvode je celý doslovný text starej stránky z texty.json
        cinnost_doslova (všetky odseky a body, slovo od slova, len medzery opravené), tri karty s ikonou, veľkým číslom
        ako kótou a nadpisom 1. až 3.; žiadna naša veta navyše, žiadne „všetkých N činností“, nič skryté."""
        cd = self.texty['cinnost_doslova']
        self.assertEqual([k for k in cd if not k.startswith('_')], ['manazment', 'dozor', 'stavbyveduci'])
        self.assertEqual([cd[k]['nadpis'] for k in ('manazment', 'dozor', 'stavbyveduci')],
                         ['1. Stavebný manažment', '2. Stavebný dozor', '3. Stavbyvedúci'])
        self.assertEqual([cd[k]['slug'] for k in ('manazment', 'dozor', 'stavbyveduci')],
                         ['stavebny-manazment', 'stavebny-dozor', 'stavbyveduci'])
        self.assertEqual(len(cd['manazment']['odseky']), 4)
        self.assertEqual(len(cd['dozor']['body']), 19)
        self.assertEqual((len(cd['stavbyveduci']['body']), len(cd['stavbyveduci']['body_2'])), (1, 7))
        # doslovné vety starého webu (výber, celý text porovná nižšie s HTML), opravené len medzery
        self.assertTrue(cd['manazment']['odseky'][0].startswith('BM Building, s.r.o je stavebno-manažérska spoločnosť, ktorá'))
        self.assertTrue(cd['manazment']['odseky'][3].startswith('Za nášho klienta prevezmeme riadenie a koordináciu procesu '
                                                               'zadania stavebných prác. Po výbere'))
        self.assertEqual(cd['dozor']['uvod'], 'V rámci výkonu stavebný dozor ponúkame tieto činnosti:')
        self.assertEqual(cd['dozor']['body'][0], 'oboznámenie sa s podkladmi (projekt, zmluvy, stavebné povolenia, územné rozhodnutie)')
        self.assertEqual(cd['dozor']['body'][-1], 'Účasť na kolaudačnom konaní')
        self.assertEqual(cd['stavbyveduci']['uvod_2'], 'V zmysle ustanovení Stavebného zákona je stavbyvedúci oprávnený:')
        self.assertEqual(cd['stavbyveduci']['body_2'][-1], 'vykázať cudzie osoby zo staveniska a zo stavby.')
        vsetky = json.dumps({k: v for k, v in cd.items() if not k.startswith('_')}, ensure_ascii=False)
        for zle in (' .', '( ', ' ,', '  '):
            self.assertNotIn(zle, vsetky, zle)
        telo = self.info('')['telo']
        cin = telo.split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        self.assertIn('<h2 class="nadpis" id="cinnost-nadpis">Činnosť</h2>', cin)
        karty = re.findall(r'<article class="cinnost-karta" aria-labelledby="cinnost-([a-z-]+)">(.*?)</article>', cin, re.S)
        self.assertEqual([k for k, _ in karty], ['stavebny-manazment', 'stavebny-dozor', 'stavbyveduci'])

        def cisty(s):
            return html.unescape(re.sub(r'<[^>]+>', '', s)).replace(' ', ' ')
        for (slug, karta), kluc, i in zip(karty, ('manazment', 'dozor', 'stavbyveduci'), (1, 2, 3)):
            c = cd[kluc]
            # ikona, veľké číslo ako kóta (skryté pred čítačkou, nadpis nesie číslo) a nadpis s odkazom na stránku služby
            self.assertIn('<span class="cinnost-ikona"><svg class="ikona"', karta)
            self.assertIn(f'<div class="cinnost-cislo" aria-hidden="true"><span class="kota kota-velka">', karta)
            self.assertIn(f'<span class="vh">{i:02d}</span>', karta)
            # nadpis bez odkazu (odkaz na stránku služby je zvlášť v hlave, 3A.7)
            h3 = re.search(r'<h3 class="cinnost-nadpis" id="cinnost-' + slug + r'">(.*?)</h3>', karta)
            self.assertIn(f'<a class="odkaz-sipka" href="{slug}/">Stránka služby<span class="vh">: ', karta)
            self.assertIsNotNone(h3, slug)
            self.assertEqual(cisty(h3.group(1)), c['nadpis'])
            text = karta.split('<div class="cinnost-text">', 1)[1]
            odseky = [cisty(p) for p in re.findall(r'<p>(.*?)</p>', text)]
            self.assertEqual(odseky, c.get('odseky', []))
            uvody = [cisty(p) for p in re.findall(r'<p class="cinnost-uvod">(.*?)</p>', text)]
            self.assertEqual(uvody, [c[u] for u in ('uvod', 'uvod_2') if c.get(u)])
            zoznamy = [[cisty(li) for li in re.findall(r'<li>(.*?)</li>', ul)]
                       for ul in re.findall(r'<ul class="cinnost-body[^"]*">(.*?)</ul>', text)]
            self.assertEqual(zoznamy, [c[b] for b in ('body', 'body_2') if c.get(b)])
            # nič skryté (bez details, hidden, rozbaľovania)
            for skryte in ('<details', ' hidden', 'aria-expanded'):
                self.assertNotIn(skryte, karta)
        # dlhé zoznamy v dvoch stĺpcoch od tabletu (CSS), krátke nie
        self.assertEqual(cin.count('<ul class="cinnost-body cinnost-body-dlhe">'), 2)
        self.assertIn('.cinnost-body-dlhe{columns:2;', self.css)
        # žiadna naša veta navyše: žiadny odsek mimo doslovného textu, žiadna SEO veta, žiadne odkazy na počet činností
        texty_kariet = ''.join(k.split('<div class="cinnost-text">', 1)[1] for _, k in karty)
        vety = re.findall(r'<p(?: class="[^"]*")?>(.*?)</p>', texty_kariet)
        doslovne = {cisty(x) for c in cd.values() if isinstance(c, dict) for k in ('odseky',) for x in c.get(k, [])}
        doslovne |= {c[u] for c in cd.values() if isinstance(c, dict) for u in ('uvod', 'uvod_2') if c.get(u)}
        self.assertEqual({cisty(v) for v in vety}, doslovne)
        text_cin = cisty(cin)
        self.assertNotRegex(text_cin, r'(?i)všetk\w*\s+\d+\s+činnost')
        self.assertNotRegex(telo, r'(?i)všetk\w*\s+(\d+|\w+)\s+činnost')
        for zle in ('stavebná firma z Pezinka', 'Kto je pri kolaudácii', 'sluzba-viac', '€'):
            self.assertNotIn(zle, cin, zle)
        # otázky zo zákona sú pod kartami (3A.7), nie v nich
        for _, karta in karty:
            self.assertNotIn('Čo hovorí stavebný zákon', karta)
        self.assertNotIn('uvodny', self.texty['cinnost'])
        # menu Činnosť vedie sem: na úvode #cinnost, z podstránky ../#cinnost; hlavička nadpis nezakryje
        uvod = self.info('')['html']
        self.assertIn('<a href="#cinnost">Činnosť</a>', uvod.split('<nav class="menu-d"', 1)[1].split('</nav>', 1)[0])
        for cesta in ('realizacie', 'kontakt', 'stavebny-dozor'):
            hl = self.info(cesta)['html'].split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
            self.assertIn('href="../#cinnost"', hl, cesta)
            self.assertNotIn('#sluzby', hl, cesta)
        self.assertIn('scroll-padding-top:calc(var(--hore) + 16px)', self.css)
        self.assertIn('.cinnost-sekcia{', self.css)
        self.assertIn('scroll-margin-top:', self.css.split('.cinnost-sekcia{', 1)[1].split('}', 1)[0])

    def test_uvod_cinnost_hlava_a_zakon(self):
        """ZMENY-KOLO-1.md 3A.7 (Andrej 24. 11. 2026 o 19:53, náhľad pred 1. kolom sa Máriovi páčil): v hlave karty
        Činnosti počet realizácií služby z rolí (ten istý ako na stránke služby) s odkazom na realizácie služby,
        osvedčenie SKSI z roku vydania s odkazom na Oprávnenia, odkaz na stránku služby; pod kartami veta o osvedčeniach
        SKSI a „Čo hovorí stavebný zákon“ s dvomi rozbaľovacími otázkami, zdrojom Slov-Lex a prvou otvorenou."""
        telo = self.info('')['telo']
        cin = telo.split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        karty = dict(re.findall(r'<article class="cinnost-karta" aria-labelledby="cinnost-([a-z-]+)">(.*?)</article>', cin, re.S))
        self.assertEqual(list(karty), ['stavebny-manazment', 'stavebny-dozor', 'stavbyveduci'])
        kluce = {k: v['kluce'] for k, v in self.texty['sluzby'].items()}
        realizacie = [json.loads(p.read_text(encoding='utf-8')) for p in (KOREN / 'obsah' / 'realizacie').glob('*.json')]
        zverejnene = [r for r in realizacie if r.get('zverejnene', True) is not False]
        rok = {o['cinnost']: o['datum_vydania'][:4] for o in self.firma['opravnenia']}
        for slug, karta in karty.items():
            hlava = karta.split('<div class="cinnost-text">', 1)[0]
            pocet_sluzby = re.search(r'id="real-nadpis">[^<]*<span class="nadpis-pocet cisla">(\d+)</span>',
                                     self.info(f'{slug}/')['telo'])
            n = sum(1 for r in zverejnene if any(kl in (r.get('rola') or '').lower() for kl in kluce[slug]))
            m = re.search(r'<a class="cinnost-pocet" href="' + slug + r'/#real-nadpis"><span class="cinnost-pocet-c cisla">(\d+)</span>'
                          r' <span>(realizáci\w+)</span></a>', hlava)
            if n:
                self.assertIsNotNone(m, slug)
                self.assertEqual(int(m.group(1)), n, slug)
                self.assertEqual(int(pocet_sluzby.group(1)), n, slug)
                self.assertEqual(m.group(2), 'realizácia' if n == 1 else 'realizácie' if n < 5 else 'realizácií')
            else:
                self.assertIsNone(m, slug)
            # nenápadný odkaz na stránku služby, nadpis bez odkazu
            self.assertEqual(hlava.count(f'href="{slug}/"'), 1, slug)
            self.assertNotIn('<details', karta)
        # počty z rolí: manažment a dozor majú realizácie (1. kolo: role dozoru doplnené pri Vtáčniku, Devínskych lúkach ...)
        self.assertIn('<small>ako projektový alebo stavebný manažment</small>', karty['stavebny-manazment'])
        self.assertIn('cinnost-pocet', karty['stavebny-dozor'])
        # osvedčenie SKSI z roku vydania s odkazom na Oprávnenia: dozor a stavbyvedúci, manažment nie (ako náhľad)
        for slug, cinnost in (('stavebny-dozor', 'stavebný dozor'), ('stavbyveduci', 'stavbyvedúci')):
            self.assertIn(f'<li class="cinnost-fakt"><a href="opravnenia/">osvedčenie SKSI z{chr(160)}roku {rok[cinnost]}</a></li>'
                          .replace(chr(160), ' '), karty[slug].replace(chr(160), ' '), slug)
        self.assertEqual((rok['stavebný dozor'], rok['stavbyvedúci']), ('2010', '2003'))
        self.assertNotIn('osvedčenie SKSI', karty['stavebny-manazment'])
        # žiadne „Všetkých N činností“ (všetky činnosti sú vypísané)
        cisty = html.unescape(re.sub(r'<[^>]+>', '', cin)).replace(chr(160), ' ')
        self.assertNotRegex(cisty, r'(?i)všetk\w*\s+\d+\s+činnost')
        # veta o osvedčeniach SKSI pod kartami s dátumami z firma.json a odkazom Oprávnenia
        za = cin.split('</article></div>', 1)[1]
        sksi = re.search(r'<p class="cinnost-sksi">(.*?)</p>', za)
        self.assertIsNotNone(sksi)
        self.assertEqual(html.unescape(re.sub(r'<[^>]+>', '', sksi.group(1))).replace(chr(160), ' '),
                         'Osvedčenia Slovenskej komory stavebných inžinierov: stavbyvedúci, vydané 5. 11. 2003; '
                         'stavebný dozor, vydané 10. 6. 2010. Oprávnenia')
        self.assertIn('<a href="opravnenia/">Oprávnenia</a></p>', sksi.group(0))
        # Čo hovorí stavebný zákon: dve otázky v details a summary, zdroj Slov-Lex pri každej, prvá otvorená
        zakon = za.split('<div class="zakon-list" role="group" aria-labelledby="zakon-uvod">', 1)[1]
        self.assertIn('<h3 class="cinnost-nadpis zakon-nadpis" id="zakon-uvod">Čo hovorí stavebný zákon</h3>', zakon)
        otazky = re.findall(r'<details class="otazka"( open)?><summary><h4>(.*?)</h4></summary><p>(.*?)</p>'
                            r'<p class="zdroj">Zdroj: <a href="([^"]+)">(.*?)</a></p></details>', zakon)
        self.assertEqual([html.unescape(q[1]).replace(chr(160), ' ') for q in otazky],
                         ['Čo robí stavebný dozor?', 'Aký je rozdiel medzi stavebným dozorom a stavbyvedúcim?'])
        self.assertEqual([q[0] for q in otazky], [' open', ''])
        for q in otazky:
            self.assertEqual(q[3], self.texty['zakon']['url'])
            self.assertTrue(q[3].startswith('https://www.slov-lex.sk/'))
            self.assertIn('zákon č.', html.unescape(q[4]).replace(chr(160), ' '))
        self.assertNotIn('Kto je pri kolaudácii', zakon)
        # na stránkach služieb ostávajú otázky zatvorené
        self.assertNotIn('<details class="otazka" open>', self.info('stavebny-dozor/')['telo'])
        # technický list: na počítači hlava pripnutá, zoznamy v dvoch stĺpcoch; body s tou istou značkou ako na
        # stránkach služieb, bez čísel 01, 02 (overenie-1: rovnaký doslovný text má vyzerať rovnako)
        self.assertIn('.cinnost-hlava{align-self:start;position:sticky;top:calc(var(--hore) + var(--m-24))}', self.css)
        self.assertNotIn('counter(bod', self.css)
        self.assertNotIn('.cinnost-karta .cinnost-body li', self.css)
        self.assertNotIn('columns:3', self.css)

    def test_uvod_o_firme_v_hrdinovi(self):
        """3A.2: údaje v hrdinovi ako rad s ikonami v poradí Sídlo, Na trhu, Konateľ, Oprávnenia (odkaz na stránku
        Oprávnenia), malým písmom zdroj s odkazom na výpis z ORSR, Zavolať a Napísať e-mail; kotva #firma; riadok
        Pôsobí nie je. Mobil: fotka a H1 na nej (jedna oblasť mriežky), pod ňou údaje; počítač slider vľavo 7 z 12."""
        telo = self.info('')['telo']
        hlava = telo.split('<div class="firma-hlava" id="firma" data-vyzva-hore>', 1)[1].split('</div>', 1)[0]
        self.assertIn('<h1 class="uvod-h1" id="uvod-nadpis">', hlava)
        udaje = telo.split('<div class="firma-udaje">', 1)[1].split('</section>', 1)[0]
        dl = re.search(r'<dl class="firma-fakty">(.*?)</dl>', udaje, re.S).group(1)
        polozky = re.findall(r'<div class="(udaj(?: udaj-siroky)?)"><dt>(<svg class="ikona"[^>]*aria-hidden="true"[^>]*>.*?</svg>)'
                             r'<span>(.*?)</span></dt><dd>(.*?)</dd></div>', dl)
        self.assertEqual([html.unescape(k) for _, _, k, _ in polozky], ['Sídlo', 'Na trhu', 'Konateľ', 'Oprávnenia'])
        self.assertEqual([t for t, _, _, _ in polozky], ['udaj udaj-siroky', 'udaj', 'udaj', 'udaj udaj-siroky'])
        for _, svg, k, _ in polozky:
            self.assertEqual(svg, build.IKONY_UDAJOV[html.unescape(k)])
        self.assertEqual(len({svg for _, svg, _, _ in polozky}), 4)
        riadky = [(html.unescape(k), v) for _, _, k, v in polozky]
        text = {k: html.unescape(re.sub(r'<[^>]+>', '', v)).replace(' ', ' ') for k, v in riadky}
        self.assertEqual(text['Sídlo'], 'Podhorská 9, 902 01 Pezinok')
        self.assertEqual(text['Na trhu'], 'od roku 2005')
        self.assertEqual(self.firma['den_zapisu'][:4], '2005')
        self.assertEqual(text['Konateľ'], self.firma['konatel'])
        self.assertEqual(text['Oprávnenia'], 'osvedčenia SKSI pre stavebný dozor a stavbyvedúceho')
        self.assertRegex(dict(riadky)['Oprávnenia'], r'^<a href="opravnenia/">')
        self.assertNotIn('Pôsobí', udaje)
        # zdroj pod údajmi: výpis z obchodného registra, len adresa orsr.sk z obsah/firma.json
        zdroj = re.search(r'<p class="firma-zdroj">Zdroj: <a href="([^"]+)">([^<]+)</a></p>', udaje)
        self.assertIsNotNone(zdroj)
        self.assertEqual(html.unescape(zdroj.group(2)).replace(' ', ' '), 'výpis z Obchodného registra SR')
        self.assertEqual(urllib.parse.urlsplit(html.unescape(zdroj.group(1))).hostname, 'www.orsr.sk')
        self.assertTrue(self.firma['zapis_or_zdroj'].startswith(html.unescape(zdroj.group(1))))
        self.assertLess(udaje.index('</dl>'), udaje.index('class="firma-zdroj"'))
        # tlačidlá Zavolať a Napísať e-mail pod údajmi (spodná lišta na mobile sa skryje, keď sú v obraze)
        akcie = udaje.split('<div class="uvod-akcie" data-vyzva-hore>', 1)[1]
        self.assertTrue(akcie.startswith(f'<a class="tlacidlo" href="tel:{self.firma["telefon_href"]}">Zavolať'))
        self.assertIn(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{self.firma["email"]}">Napísať e-mail</a>', akcie)
        # mobil a tablet: H1 bielym písmom na fotke (hlava a slider v jednej oblasti), pod ňou údaje; počítač slider
        # vľavo 7 z 12 stĺpcov cez oba riadky, vpravo hlava a údaje
        # oprava-2 (nález vážne): na mobile a tablete hneď pod fotkou údaje a tlačidlá, blok O firme až za nimi
        self.assertIn('.uvod-mriezka{grid-template-areas:"slider slider slider slider" "udaje udaje udaje udaje" "text text text text"', self.css)
        self.assertIn('.uvod-mriezka{grid-template-areas:"slider slider slider slider slider slider slider slider" '
                      '"udaje udaje udaje udaje udaje udaje udaje udaje" "text text text text text text text text"}', self.css)
        self.assertIn('.firma-hlava{grid-area:slider;container-type:inline-size}', self.css)
        mobil = self.css.split('@media (max-width:1023px){', 1)[1].split('\n}\n', 1)[0]
        hlava_m = mobil.split('\n  .firma-hlava{', 1)[1].split('}', 1)[0]
        self.assertTrue(hlava_m.startswith('z-index:3;'), hlava_m)
        self.assertIn('color:#fff', hlava_m)
        self.assertIn('linear-gradient(', hlava_m)
        self.assertIn('.snimky{position:relative;list-style:none;margin:0;padding:0;aspect-ratio:4/5;', self.css)
        # počítač (ÚVOD v3): pravý stĺpec hlava, text a údaje zvislo v strede vedľa fotky (prázdne riadky 1fr nad a pod
        # ním), nie údaje odtlačené na spodok
        self.assertIn('grid-template-areas:"slider slider slider slider slider slider slider . . . . ." '
                      '"slider slider slider slider slider slider slider hlava hlava hlava hlava hlava" '
                      '"slider slider slider slider slider slider slider text text text text text" '
                      '"slider slider slider slider slider slider slider udaje udaje udaje udaje udaje" '
                      '"slider slider slider slider slider slider slider . . . . .";'
                      'grid-template-rows:minmax(var(--m-24),1fr) auto auto auto minmax(var(--m-24),1fr)}', self.css)
        self.assertNotIn('.firma-udaje{align-self:end', self.css)
        for sel in ('.slider{grid-area:slider}', '.firma-udaje{grid-area:udaje;', '.firma-text{grid-area:text;', '.firma-hlava{grid-area:hlava;'):
            self.assertIn(sel, self.css)
        # H1 veľký: podiel šírky stĺpca, najviac 4rem (64 px), výška slidera na počítači podľa okna pod hlavičkou
        self.assertIn('.uvod-h1{margin-top:var(--m-16);font-size:clamp(1.75rem,9.8cqi,4rem);', self.css)
        # výška fotky: okno pod hlavičkou, najviac 58 % šírky okna (vysoké okno 1255 x 1260 nedá rám na výšku)
        self.assertIn('min-height:clamp(480px,calc(100svh - var(--hore) - var(--m-48)),58vw)', self.css)
        # hrdina na prvú obrazovku aj na notebookoch (overenie-1, nález blokuje: pri 1280 x 720 až 1536 x 864 boli
        # štítok, ovládanie slidera a tlačidlá pod okrajom okna). Pri okne do 960 px výšky idú odseky O firme pod
        # hrdinu (prvý pod fotku, druhý pod pravý stĺpec), vpravo ostane H1, údaje, zdroj a tlačidlá; H1 najviac
        # 6 % výšky okna; do 820 px hustejšie údaje. Prehliadač overil oprava-1/meraj.mjs (1280 x 720, 1366 x 768,
        # 1440 x 900, 1536 x 864, 1920 x 1080, 1024 x 768: spodok štítka, ovládania a tlačidiel v okne).
        nizke = ''.join(css_bloky(self.css, '@media (min-width:1024px) and (max-height:960px)'))
        self.assertIn('"slider slider slider slider slider slider slider udaje udaje udaje udaje udaje" '
                      '"slider slider slider slider slider slider slider . . . . ." '
                      '"text text text text text text text text text text text text";'
                      'grid-template-rows:minmax(var(--m-16),1fr) auto auto minmax(var(--m-16),1fr) auto}', nizke)
        self.assertNotIn('slider text', nizke)
        self.assertIn('.uvod-h1{font-size:min(clamp(1.75rem,9.8cqi,4rem),6svh)}', nizke)
        # oprava-2 (nález vážne): blok O firme pod hrdinom ako jeden celok (4 a 8 dielov ako list Činnosti), vlasová
        # čiara, viditeľný nadtitulok vľavo, oba odseky vpravo rovnakou farbou, nie rozhodené pod fotku a pod stĺpec
        self.assertIn('.firma-text{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);', nizke)
        self.assertIn('margin-top:var(--m-48);padding:var(--m-32) 0 0;border-top:1px solid var(--beton)}', nizke)
        self.assertIn('.firma-text p{grid-column:2;', nizke)
        self.assertIn('.firma-text p+p{margin-top:var(--m-12);padding-left:0;color:var(--grafit)}', nizke)
        self.assertIn('.firma-text-nad{position:static;', nizke)
        self.assertNotIn('grid-column:8/-1', nizke)
        self.assertNotIn('grid-column:1/8', nizke)
        najnizsie = ''.join(css_bloky(self.css, '@media (min-width:1024px) and (max-height:820px)'))
        self.assertIn('.snimky{min-height:clamp(480px,calc(100svh - var(--hore) - var(--m-24)),58vw)}', najnizsie)
        self.assertIn('.udaj,.udaj-siroky{padding:8px 0 9px}', najnizsie)
        # nízke okná sú za pravidlom počítača (min-width:1024px), aby ho prepísali
        self.assertLess(self.css.index('@media (min-width:1024px){'), self.css.index('@media (min-width:1024px) and (max-height:960px){'))

    def test_uvod_slider(self):
        """1. kolo pripomienok 3.3: slider 11 stavieb v poradí nastavenia uvod_slider (poradie zo zadania), prvá fotka
        eager s fetchpriority high, ostatné len v data-src, data-srcset a data-avif (app.js ich načíta tesne pred
        prechodom), popis s odkazom na realizáciu, miestom a len rokmi, ovládanie s aria; bez JS prvá fotka a popis."""
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        ocak = ['rd-ahoj-bratislava-nove-mesto', 'dvojdom-s-bratislava', 'rekonstrukcia-okanikova-bratislava',
                'novostavba-rd-nitrianske-hrnciarovce', 'filmovy-klub-lumiere-spitalska-bratislava', 'ufo-most-snp-bratislava',
                'bytove-domy-vtacnik-bratislava', 'bytovy-dom-na-krizovatkach-bratislava',
                'rekonstrukcia-vila-holubyho-bratislava', 'rekonstrukcia-venturska-bratislava', 'rd-kalvaria-bratislava']
        self.assertEqual([p['slug'] for p in nast['uvod_slider']], ocak)
        self.assertNotIn('uvod_foto', nast)
        telo = self.info('')['telo']
        slider = telo.split('<div class="slider"', 1)[1].split('<div class="firma-udaje">', 1)[0]
        snimky = re.findall(r'<li class="snimka snimka-(\d+)( je)?" data-slug="([a-z0-9-]+)">(.*?)</li>', slider, re.S)
        self.assertEqual([s[2] for s in snimky], ocak)
        self.assertEqual([int(s[0]) for s in snimky], list(range(11)))
        self.assertEqual([bool(s[1]) for s in snimky], [True] + [False] * 10)
        # fotky sú tie zo zoznamu (súbor z galérie realizácie), nie vizualizácie ani rozmazané
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        self.assertEqual([(r['slug'], f['rel'].split('/')[-1]) for r, f, _ in fotky],
                         [(p['slug'], p['subor']) for p in nast['uvod_slider']])
        for (r, f, _), (_, _, slug, obr) in zip(fotky, snimky):
            self.assertFalse(f.get('viz'), slug)
            self.assertFalse(f['rozmazana'], slug)
            self.assertTrue(any(f is g for g in r['galeria']), slug)
            self.assertIn(f'obrazky/{slug}/{f["nn"]}-', obr)
        # prvá fotka: picture s AVIF, eager (bez loading=lazy) a fetchpriority high; ostatné odložené
        prva = snimky[0][3]
        self.assertTrue(prva.startswith('<picture><source type="image/avif" srcset="'))
        self.assertIn('fetchpriority="high"', prva)
        self.assertNotIn('loading="lazy"', prva)
        self.assertNotIn('data-src', prva)
        for _, _, slug, obr in snimky[1:]:
            self.assertRegex(obr, r'^<img src="prazdna-45\.webp" data-src="obrazky/' + slug + r'/\d\d-\d+\.webp" '
                                  r'data-srcset="[^"]+ \d+w" data-avif="[^"]+\.avif \d+w" sizes="')
            self.assertNotIn('fetchpriority', obr)
            self.assertNotIn(' srcset=', obr.replace('data-srcset=', ''))
            self.assertIn(f'sizes="{build.SIZES_SLIDER}"', obr)
        # bez JS: odložené fotky skryté (noscript img[data-src]), viditeľná len prvá (trieda je)
        self.assertIn('img[data-src]{display:none}', build.NOSCRIPT_CSS)
        # popisy: názov s odkazom na realizáciu, miesto, len roky (bez slova príspevok)
        popisy = re.findall(r'<li class="slider-popis( je)?"><a href="realizacie/([a-z0-9-]+)/">(.*?)</a>(.*?)</li>', slider)
        self.assertEqual([p[1] for p in popisy], ocak)
        self.assertEqual([bool(p[0]) for p in popisy], [True] + [False] * 10)
        for (_, slug, nazov, udaje), (r, _, _) in zip(popisy, fotky):
            self.assertEqual(html.unescape(nazov).replace(' ', ' '), r['nazov'])
            text = html.unescape(re.sub(r'<[^>]+>', '', udaje)).replace(' ', ' ')
            self.assertNotIn('príspevok', text.lower())
            self.assertNotRegex(text, r'Rok|Roky|Termín')
            if r['miesto']:
                self.assertTrue(text.startswith(r['miesto']), (slug, text))
            if build.Web.roky_karty(r):
                self.assertTrue(text.endswith(build.Web.roky_karty(r)), (slug, text))
        self.assertIn('<ul class="slider-popisy" aria-live="off">', slider)
        # ovládanie: počítadlo ako kóta 01 / 11, Predchádzajúca, Pozastaviť (aria-pressed), Ďalšia; bez JS skryté
        ovl = re.search(r'<div class="slider-ovladanie" role="group" aria-label="[^"]+" hidden>(.*?)</div>', slider).group(1)
        # číslice sa pri vstupe do obrazu raz pretočia (otoc, valce ako pri kótach), čítačka číta 01 / 11
        self.assertIn('<span class="kota slider-pocet"><span class="kota-text cisla otoc"><span class="slider-cislo">'
                      '<span class="vh">01</span><span class="valce" aria-hidden="true"><span class="valec"><span class="stlpec c0 i0">'
                      '</span></span><span class="valec"><span class="stlpec c1 i1"></span></span></span></span>'
                      '<span class="kota-slovo"> / </span><span class="slider-spolu"><span class="vh">11</span>'
                      '<span class="valce" aria-hidden="true"><span class="valec"><span class="stlpec c1 i2"></span></span>'
                      '<span class="valec"><span class="stlpec c1 i3"></span></span></span></span></span></span>', ovl)
        tl = re.findall(r'<button class="slider-tl[^"]*" type="button" data-slider="([^"]+)" aria-controls="slider-snimky" '
                        r'aria-label="([^"]+)"( aria-pressed="false")?>', ovl)
        self.assertEqual(tl, [('-1', 'Predchádzajúca', ''), ('pauza', 'Pozastaviť', ' aria-pressed="false"'),
                              ('1', 'Ďalšia', '')])
        self.assertIn('<ul class="snimky" id="slider-snimky">', slider)
        self.assertIn(f'data-interval="{build.SLIDER_INTERVAL_MS}" data-prelinanie="{build.SLIDER_PRELINANIE_MS}"', slider)
        self.assertEqual((build.SLIDER_INTERVAL_MS, build.SLIDER_PRELINANIE_MS), (8000, 1200))
        # app.js: zastavenie pri myši, fokuse, skrytej karte a mimo obrazu, bez automatického prechodu pri obmedzenom
        # pohybe, načítanie ďalšej fotky pred prechodom
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("slider.addEventListener('mouseenter'", "slider.addEventListener('focusin'", "'visibilitychange'",
                    "var dovody = { tlacidlo: !pohyb }", "priprav(dalsia); }, Math.max(0, interval - NAPRED))",
                    "b.setAttribute('aria-pressed', stoj ? 'true' : 'false')", 'ovladanie.hidden = false'):
            self.assertIn(kus, js)
        # stred výrezu z nastavení ako pravidlo v štýle stránky (CSP nedovolí atribút style)
        self.assertNotIn(' style=', slider)

    def test_menu_bez_postupu(self):
        """1. kolo pripomienok 3.2 a 3.6: menu Činnosť, Realizácie, Oprávnenia, Kontakt (Novinky preč, ZMENY-KOLO-1.md
        5.7; s "novinky": true medzi Realizáciami a Oprávneniami ako predtým); Postup nikde (odkazy, kotvy,
        presmerovania, texty, nastavenia)."""
        for web, ocak in ((self.ostry, ['Činnosť', 'Realizácie', 'Oprávnenia', 'Kontakt']),
                          (self.ostry_n, ['Činnosť', 'Realizácie', 'Novinky', 'Oprávnenia', 'Kontakt'])):
            for subor in stranky(web):
                s = subor.read_text(encoding='utf-8')
                hlavicka = s.split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
                menu = re.search(r'<ul class="menu">(.*?)</ul>', hlavicka).group(1)
                self.assertEqual(re.findall(r'<a [^>]*>([^<]+)</a>', menu), ocak, subor)
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            for stare in ('id="postup"', '#postup', 'class="postup', 'class="faza', 'class="fazy', 'Ako prebieha stavebný dozor'):
                self.assertNotIn(stare, s, (subor, stare))
        for p in (KOREN / 'sablona' / 'presmerovania.json', KOREN / 'sablona' / 'texty.json', KOREN / 'sablona' / 'nastavenia.json'):
            d = json.loads(p.read_text(encoding='utf-8'))
            self.assertNotIn('#postup', json.dumps(d, ensure_ascii=False), p)
        texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        for kluc in ('postup', 'schema', 'firma'):
            self.assertNotIn(kluc, texty)
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertNotIn('postup_foto', nast)
        self.assertNotIn('firma_foto', nast)

    def test_socialne_siete_ikony(self):
        """1. kolo pripomienok 3.7 a 3A.6: siete ako vlastné SVG ikony s aria-label (kontakt na úvode, stránka Kontakt,
        päta): Facebook z firma.json, LinkedIn a Instagram Mária z nastavení (len https na linkedin.com a instagram.com),
        v novej karte s rel noopener; jedna sada ikon (rovnaký rám, veľkosť a hrúbka čiary)."""
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertEqual(nast['linkedin'], 'https://www.linkedin.com/in/m%C3%A1rio-bulko-aa176ba0/')
        # Andrej 24. 11. 2026 večer: „a ten instagram si tam prečo nedal? https://www.instagram.com/mariobulko“
        self.assertEqual(nast['instagram'], 'https://www.instagram.com/mariobulko/')
        nova = 'target="_blank" rel="noopener"'
        fb = f'<a class="siet" href="{self.firma["facebook"]}" {nova} aria-label="Facebook BM Building"><svg '
        li = f'<a class="siet" href="{nast["linkedin"]}" {nova} aria-label="LinkedIn: {self.firma["konatel"]}"><svg '
        ig = f'<a class="siet" href="https://www.instagram.com/mariobulko/" {nova} aria-label="Instagram: {self.firma["konatel"]}"><svg '
        for cesta in ('', 'kontakt', 'realizacie'):
            s = self.info(cesta)['html']
            kontakt = s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
            pata = s.split('<footer class="pata"', 1)[1].split('</footer>', 1)[0]
            for blok in (kontakt, pata):
                siete = re.search(r'<p class="siete [a-z-]+">(.*?)</p>', blok).group(1)
                self.assertEqual(siete.count('<a class="siet"'), 3, cesta)
                # poradie Facebook, LinkedIn, Instagram (Instagram vedľa Facebooku a LinkedInu)
                self.assertTrue(siete.index(fb) < siete.index(li) < siete.index(ig), cesta)
                # ikona bez textu: názov siete len v aria-label, svg skryté pred čítačkou, jeden rám pre všetky
                ramy = set()
                for a in re.findall(r'<a class="siet"[^>]*>(.*?)</a>', siete):
                    self.assertEqual(re.sub(r'<[^>]+>', '', a), '')
                    self.assertIn('aria-hidden="true"', a)
                    self.assertTrue(a.startswith('<svg width="22" height="22" viewBox="0 0 22 22" fill="none" '
                                                 'stroke="currentColor" stroke-width="1.5" '), a[:120])
                    ramy.add(re.search(r'<rect [^>]*/>', a).group(0))
                self.assertEqual(ramy, {'<rect x="1.75" y="1.75" width="18.5" height="18.5" rx="4.5"/>'})
            # ikony sú inline SVG, žiadny cudzí obrázok ani text „Facebook“ ako odkaz
            self.assertNotIn('>Facebook', s)
            self.assertNotIn('>LinkedIn', s)
            self.assertNotIn('>Instagram', s)
        # pekné stavy: hover len s myšou, focus-visible s obrysom, rovnaký cieľ dotyku pre všetky ikony
        self.assertIn('.siet{display:inline-grid;place-items:center;width:44px;height:44px;', self.css)
        self.assertIn('.siet:focus-visible{', self.css)

    def test_instagram_podla_nastavenia(self):
        """Instagram len s adresou v nastaveniach (kópia nastavení): null ikonu vynechá na všetkých troch miestach,
        http ani cudzí hostiteľ neprejde (build.bezpecna_url)."""
        w = self.w
        povodne = w.instagram
        try:
            for adresa, ma_byt in (('https://www.instagram.com/mariobulko/', True), (None, False),
                                   ('http://www.instagram.com/mariobulko/', False),
                                   ('https://instagram.com.zle.sk/mariobulko/', False), ('javascript:alert(1)', False)):
                nast = copy.deepcopy(w.nastavenia)
                nast['instagram'] = adresa
                w.instagram = build.bezpecna_url(nast.get('instagram'), ['instagram.com'])
                soc = ''.join(w.socialne())
                self.assertEqual('aria-label="Instagram:' in soc, ma_byt, adresa)
                self.assertEqual(soc.count('<a class="siet"'), 3 if ma_byt else 2, adresa)
                for html_bloku in (w.siete('kontakt-pozn'), w.pata('', True), w.kontakt_sekcia('')):
                    self.assertEqual('instagram.com' in html_bloku, ma_byt, adresa)
        finally:
            w.instagram = povodne

    def test_linkedin_len_https_linkedin(self):
        """LinkedIn len https na linkedin.com (build.bezpecna_url), inak sa ikona nezobrazí."""
        w = self.w
        povodne = w.linkedin
        try:
            for adresa, ma_byt in (('https://www.linkedin.com/in/m%C3%A1rio-bulko-aa176ba0/', True),
                                   ('http://www.linkedin.com/in/x/', False), ('https://linkedin.com.zle.sk/in/x/', False),
                                   ('javascript:alert(1)', False), ('https://evil.example/linkedin.com/', False), (None, False)):
                w.linkedin = build.bezpecna_url(adresa, ['linkedin.com'])
                soc = ''.join(w.socialne())
                self.assertEqual('aria-label="LinkedIn:' in soc, ma_byt, adresa)
                if ma_byt:
                    self.assertIn('href="https://www.linkedin.com/in/', soc)
        finally:
            w.linkedin = povodne

    def test_uvod_bez_pasu_realizacii(self):
        """ZMENY-KOLO-1.md 3A.6 (Andrej 24. 11. 2026 večer: „pás vybrané realizácie sme chceli dať preč a nechať len
        slideshow, chápeš“): na úvode je hrdina so slideshow, Činnosť a Kontakt; tmavý pás, jeho odkaz v hrdinovi,
        skript, štýl, nastavenia a texty sú preč. Zoznam /realizacie/ a menu Realizácie ostávajú."""
        t = self.info('')
        for stare in ('id="prace"', 'class="prace', 'class="pas"', 'pas-prac', 'Vybrané realizácie', 'odkaz-prace',
                      'class="projekt"', 'class="projekt-dalsi"', 'class="prepinac"', 'class="lb-zoznam"', 'preskocit-pas',
                      'class="sipky"', 'navod-mobil', 'id="lightbox"', '#prace'):
            self.assertNotIn(stare, t['html'], stare)
        sekcie = re.findall(r'<section class="([^"]+)"', t['telo'])
        self.assertEqual(sekcie, ['uvod', 'sekcia cinnost-sekcia', 'sekcia kontakt'])
        # slideshow ostáva (11 fotiek), menu Realizácie a stránka zoznamu tiež
        self.assertEqual(t['telo'].count('<li class="snimka snimka-'), 11)
        menu = t['html'].split('<nav class="menu-d"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('<a href="realizacie/"', menu)
        self.assertTrue(self.info('realizacie/')['h1'].startswith('Realizácie'))
        self.assertEqual(self.info('realizacie/')['telo'].count('<li class="dlazdica'), len(self.w.realizacie))
        # nastavenia, texty, build, štýl a skript bez pásu
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        for kluc in ('pas_vyber', '_pas_vyber', 'uvod_pocet_realizacii'):
            self.assertNotIn(kluc, nast)
        self.assertNotIn('pas', self.texty)
        self.assertNotIn('odkaz_prace', self.texty['uvod'])
        for meno in ('pas_vyber', 'pas_prac', 'karta_pasu'):
            self.assertFalse(hasattr(build.Web, meno), meno)
        for meno in ('PAS_MAX', 'SIZES_PAS', 'POCET_STAVIEB', 'SVG_FOTKY', 'SVG_SIPKA_DOLE'):
            self.assertFalse(hasattr(build, meno), meno)
        for kus in ('.prace', '.pas{', '.pas>', '.pas ', 'pas-posun', '.pas-ovladanie', '.sipky', '.sipka', '.navod-mys',
                    '.projekt-dalsi', '.prepinac', '.odkaz-prace', '.pripnute'):
            self.assertNotIn(kus, self.css, kus)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("querySelector('.prace')", 'prace-lepidlo', 'ioPas', "setProperty('--posun'", 'pripnute', 'vKlude',
                    "pas.addEventListener('focusin'", '.prace-hlava'):
            self.assertNotIn(kus, js, kus)

    def test_uvod_o_firme_text_presne(self):
        """ÚVOD v3: v hrdinovi pod H1 dva odseky pôvodnej sekcie O firme presne (bez 20 rokov, Andrej: „tých 20 rokov
        nemáme písať“) z texty.json uvod.o_firme_hrdina; na počítači vpravo medzi H1 a údajmi."""
        ocak = ['Investorom a developerom poskytuje podporné služby pri realizácii ich projektov a zabezpečí '
                'bezproblémový prechod z projektovej do realizačnej fázy. Stavebný manažment chápe ako ucelený proces: '
                'koordináciu všetkých profesií, riadenie a kontrolu stavby počas prípravy, realizácie až po kolaudáciu.',
                'Firmu vedie konateľ Ing. Mário Bulko. V obchodnom registri je spoločnosť zapísaná od 27. 7. 2005.']
        self.assertEqual(self.texty['uvod']['o_firme_hrdina'], ocak)
        telo = self.info('')['telo']
        blok = re.search(r'<div class="firma-text">(.*?)</div>', telo).group(1)
        odseky = [html.unescape(re.sub(r'<[^>]+>', '', p)).replace('\u00a0', ' ')
                  for p in re.findall(r'<p>(.*?)</p>', blok)]
        self.assertEqual(odseky, ocak)
        # poradie v hrdinovi: kľúčové slová, H1, odseky, údaje s ikonami, zdroj ORSR, tlačidlá
        poradie = [telo.index(k) for k in ('class="uvod-nadtitulok"', '<h1 class="uvod-h1"', '<div class="firma-text">',
                                             '<dl class="firma-fakty">', 'class="firma-zdroj"', '<div class="uvod-akcie"')]
        self.assertEqual(poradie, sorted(poradie))
        # žiadne „20 rokov“ ani rok výročia na úvode, ani v texte O firme
        uvod = html.unescape(re.sub(r'<[^>]+>', ' ', telo)).replace('\u00a0', ' ')
        for kus in ('20 rokov', '20 rokoch', 'oslávila', 'výročie'):
            self.assertNotIn(kus, uvod, kus)

    def test_oprava2_hrdina_mobil(self):
        """oprava-2, nález vážne (390 px, známka 7/10): tmavý prechod len v spodnej časti fotky (horná polovica čistá),
        H1 menší, riadok kľúčových slov na fotke nie je (lámal sa so samotnou bodkou), štítok stavby nie na fotke
        (zakrýval strechy), ale pod počítadlom grafitom na papieri; údaje a tlačidlá hneď pod fotkou, O firme za nimi."""
        mobil = self.css.split('@media (max-width:1023px){', 1)[1].split('\n}\n', 1)[0]
        hlava_m = mobil.split('\n  .firma-hlava{', 1)[1].split('}', 1)[0]
        prechod = re.search(r'linear-gradient\(180deg,rgba\(14,15,15,0\) (\d+)%,rgba\(14,15,15,\.(\d+)\) (\d+)%,'
                            r'rgba\(14,15,15,\.(\d+)\) 100%\)', hlava_m)
        self.assertIsNotNone(prechod, hlava_m)
        self.assertGreaterEqual(int(prechod.group(1)), 45)        # horná polovica fotky bez zatmenia
        self.assertGreaterEqual(int(prechod.group(2)), 50)        # pod H1 aspoň polovica tmavej
        self.assertGreaterEqual(int(prechod.group(4)), 85)
        self.assertIn('.uvod-nadtitulok{display:none}', mobil)
        self.assertIn('.uvod-h1{margin-top:0;font-size:clamp(1.5rem,8.2cqi,3rem);', mobil)
        self.assertIn('.slider-popisy{position:static;order:2;', mobil)
        self.assertIn('.slider-popis{padding:0;background:none;color:var(--grafit);', mobil)
        self.assertIn('.slider-spodok{flex-direction:column;', mobil)
        self.assertIn('.firma-text{margin-top:var(--m-32);padding-top:var(--m-24);border-top:1px solid var(--beton)}', mobil)
        # HTML: nadtitulok ostáva v hlave (počítač), štítok a ovládanie v spodku slidera
        telo = self.info('')['telo']
        self.assertIn('<div class="slider-spodok"><ul class="slider-popisy" aria-live="off">', telo)

    def test_oprava2_o_firme_nadtitulok(self):
        """oprava-2: blok O firme má nadtitulok O firme (h2, text z texty.json uvod.o_firme_nadpis); vo vysokom okne
        počítača je len pre čítačku, na mobile a v nižších oknách viditeľný; odseky ostávajú presne dva."""
        telo = self.info('')['telo']
        blok = re.search(r'<div class="firma-text">(.*?)</div>', telo).group(1)
        # sadzba dá za jednopísmenovú predložku pevnú medzeru (O firme)
        self.assertTrue(blok.replace(' ', ' ').startswith('<h2 class="firma-text-nad">O firme</h2><p>'), blok[:80])
        self.assertEqual(self.texty['uvod']['o_firme_nadpis'], 'O firme')
        self.assertEqual(len(re.findall(r'<p>', blok)), 2)
        # nadpisy úvodu v poradí: H1, O firme, Činnosť
        poradie = [telo.index(k) for k in ('<h1 class="uvod-h1"', '<h2 class="firma-text-nad">', 'id="cinnost-nadpis"')]
        self.assertEqual(poradie, sorted(poradie))
        pc = ''.join(css_bloky(self.css, '@media (min-width:1024px)'))
        self.assertIn('.firma-text-nad{position:absolute;width:1px;height:1px;', pc)
        self.assertIn('.firma-text-nad{display:flex;', self.css)

    def test_oprava2_drobne(self):
        """oprava-2, drobné: ciele dotyku odkazov osvedčení SKSI a vizitky 44 px, druhy realizácií na mobile v mriežke
        2 x 2 (nie posúvací rad), odstup nadpisu zákona od čiary otázok, rezerva hodnôt tabuľky údajov, úvod zoznamu
        realizácií bez slov roky realizácie, verejný skript bez interných ciest a mien, kóty sa kreslia."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.cinnost-fakt>a{display:inline-flex;align-items:center;align-self:flex-start;min-height:44px}', css)
        self.assertIn('.vizitka-riadky a{display:inline-flex;align-items:center;min-height:44px;', css)
        mobil = ''.join(css_bloky(css, '@media (max-width:767px)'))
        self.assertIn('.filtre-druh{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));', mobil)
        self.assertNotIn('overflow-x:auto', mobil)
        self.assertIn('.otazky-nadpis{padding-bottom:var(--m-16)}', ''.join(css_bloky(css, '@media (max-width:1023px)')))
        self.assertIn('.fakty dd{margin:0;padding-right:var(--m-12);', css)
        veta = self.texty['realizacie']['veta']
        self.assertNotIn('roky realizácie', veta.lower())
        self.assertIn('roky, ak sú známe', veta)
        zoznam = html.unescape(self.info('realizacie/')['telo'])
        self.assertNotIn('roky realizácie', zoznam.lower())
        # verejný app.js a komentáre štýlu: bez interných ciest, mien a slova olovnica (štýl build aj tak bez komentárov)
        js = (self.ostry / 'app.js').read_text(encoding='utf-8')
        for kus in ('ops/', 'Olovnica', 'olovnica', 'D:/', 'Andrej', 'Fable'):
            self.assertNotIn(kus, js, kus)
        self.assertNotIn('Olovnica', self.css.split('*/', 1)[0])
        self.assertNotIn('ops/', self.css.split('*/', 1)[0])
        self.assertNotIn('Olovnica', self.info('')['html'])
        # kóty s pretáčanými číslicami sa kreslia zo stredu len pri pohybe a s triedou js (bez JS celé)
        pohyb = ''.join(css_bloky(css, '@media (prefers-reduced-motion:no-preference)'))
        self.assertIn('.js .kota:has(>.otoc:not(.otocene)){background-size:0 1px}', pohyb)
        self.assertIn('.js .kota:has(>.otoc){background-position:50% 50%;transition:background-size .9s', pohyb)
        self.assertEqual(css.count(':has(>.otoc'), pohyb.count(':has(>.otoc'))

    def test_roky_na_kartach(self):
        # karta zoznamu ukáže len roky stavby; bez nich prázdnu kótu, rok príspevku nie (Mário 24. 11. 2026: „rok
        # príspevku preč, len roky“, overenie-1); slovo Facebook v karte nie je (zadanie, Obsah 2)
        zoznam = self.info('realizacie/')['telo']
        for r in self.w.realizacie:
            karta = zoznam.split(f'realizacie/{r["slug"]}/"', 1)[1].split('</li>', 1)[0]
            self.assertNotIn('Facebook', karta)
            if r['roky']:
                # zoznam má statickú kótu (slovo „až“ v span.kota-slovo): porovnáva sa text bez značiek
                self.assertIn(r['roky'], re.sub(r'<[^>]+>', '', karta).replace('\u00a0', ' '), r['slug'])
            else:
                self.assertIn('<span class="kota kota-prazdna" aria-hidden="true"></span>', karta, r['slug'])

    def test_kazda_karta_ma_fotku(self):
        # žiadna karta zoznamu bez fotky; vizualizácia ako fotka karty alebo obal len pri obal_vizualizacia
        zoznam = self.info('realizacie/')['telo']
        karty = re.findall(r'<li class="dlazdica.*?</li>', zoznam, re.S)
        self.assertEqual(len(karty), len(self.w.realizacie))
        for karta in karty:
            self.assertIn('<img', karta)
            self.assertNotIn('bez-foto', karta)
        for r in self.w.realizacie:
            if r['galeria']:
                self.assertIsNotNone(r['karta'], r['slug'])
            if r['karta'] is not None and r['karta'].get('viz'):
                self.assertTrue(r['obal_vizualizacia'], r['slug'])
            if r['obal'] is not None and r['obal'].get('viz'):
                self.assertTrue(r['obal_vizualizacia'], r['slug'])
            if r['velka'] is not None:
                self.assertFalse(r['velka'].get('viz'), r['slug'])

    def test_vizualizacie_so_stitkom(self):
        # vizualizácie z obsahu (druh vizualizacia): v galérii za skutočnými fotkami, štítok na náhľade, data-viz
        # a autor pre lightbox, alt začína „Vizualizácia“ (zadanie bmb-dokoncenie, Obsah 3)
        viditelne = 0
        for r in self.w.realizacie:
            viz = [f for f in r['galeria'] if f.get('viz')]
            if not viz:
                continue
            poradie = [bool(f.get('viz')) for f in r['galeria']]
            self.assertEqual(poradie, sorted(poradie), r['slug'])
            s = (self.ostry / 'realizacie' / r['slug'] / 'index.html').read_text(encoding='utf-8')
            # štítok na každom náhľade vizualizácie v galérii, navyše na fotke dvojstrany, keď je ňou vizualizácia
            # (len obal_vizualizacia, Devín)
            karta_viz = 1 if r['karta'] is not None and r['karta'].get('viz') else 0
            self.assertEqual(s.count('<span class="stitok-viz">Vizualizácia</span>'), len(viz) + karta_viz, r['slug'])
            self.assertEqual(s.count('data-viz="1"'), len(viz), r['slug'])
            alty = re.findall(r'data-viz="1" data-autor="[^"]*">(?:<picture><source [^>]*>)?<img [^>]*alt="([^"]*)"', s)
            self.assertEqual(len(alty), len(viz), r['slug'])
            for alt in alty:
                self.assertTrue(alt.startswith('Vizualizácia'), alt)
            # meno autora pod náhľadom (ak je známe) a v alt texte
            for f in viz:
                if f.get('autor'):
                    self.assertIn(html.escape(f'Vizualizácia, autor: {f["autor"]}', quote=False).replace('\u00a0', ' ')[:30],
                                  s.replace('\u00a0', ' '), r['slug'])
            viditelne += len(viz)
        # karta zo zoznamu realizácií so štítkom, keď je jej fotka vizualizácia (Devín)
        zoznam = self.info('realizacie/')['telo']
        for r in self.w.realizacie:
            karta = zoznam.split(f'href="../realizacie/{r["slug"]}/"', 1)[1].split('</li>', 1)[0]
            ma = r['karta'] is not None and r['karta'].get('viz')
            self.assertEqual('<span class="stitok-viz">Vizualizácia</span>' in karta, bool(ma), r['slug'])
            if ma:
                self.assertRegex(karta, r'alt="Vizualizácia')
        self.assertGreaterEqual(viditelne, 4)
        skl = next(r for r in self.w.realizacie if r['slug'] == 'rd-sklenarska-bratislava')
        self.assertTrue(skl['karta'].get('viz'))
        self.assertIs(skl['karta'], skl['galeria'][0])
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("'data-viz'", "'data-autor'", "'.lightbox-stitok'", "'data-alt'", "'a.prepinac[data-galeria]'",
                    "var skupinaZ = function (a)", "'data-nn'", "'data-cesta'", 'spustac.focus()'):
            self.assertIn(kus, js)

    def karty_zoznamu(self, cesta):
        """Svetlé karty (li.dlazdica alebo li.novinka-karta) na stránke: zoznam (trieda li, HTML karty)."""
        telo = self.info(cesta)['telo']
        return re.findall(r'<li class="((?:dlazdica|novinka-karta) projekt projekt-svetly[^"]*)"[^>]*>(.*?)</li>', telo, re.S)

    def test_kazda_karta_ma_fotku(self):
        # zadanie bmb-dokoncenie (Obsah 2): žiadna karta realizácie bez fotky, nikde rám bez fotky; karta je výrez
        # 4 : 5 skutočnej ostrej fotky (vizualizácia len pri obal_vizualizacia so štítkom)
        cesty = ['realizacie/'] + [f'{k}/' for k in self.texty['sluzby']]
        spolu = 0
        for cesta in cesty:
            karty = self.karty_zoznamu(cesta)
            # služba bez realizácie v tej role (stavbyvedúci) karty nemá
            self.assertTrue(karty or cesta != 'realizacie/', cesta)
            for i, (trieda, karta) in enumerate(karty):
                spolu += 1
                self.assertNotIn('bez-fotky', trieda, cesta)
                self.assertNotIn('bez-foto', karta, cesta)
                self.assertNotIn('karta-znak', karta, cesta)
                if 'foto-cela' in karta:
                    # malý zdroj: celá fotka 4 : 3 alebo 3 : 4 v strede rámu (brána bmb-presnost pokus 1, nález 1)
                    self.assertRegex(karta, r'<img class="foto-cela[^"]*" src="\.\./obrazky/[a-z0-9-]+/\d\d-\d+-(43|34)\.webp" '
                                     r'srcset="[^"]*-(43|34)\.webp \d+w', cesta)
                    self.assertIn('sizes="(min-width: 1024px) min(23vw - 48px, ', karta, cesta)
                else:
                    self.assertRegex(karta, r'<img src="\.\./obrazky/[a-z0-9-]+/\d\d-\d+-45\.webp" srcset="[^"]*-45\.webp \d+w', cesta)
                    self.assertIn(f'sizes="{build.SIZES_KARTA}"', karta, cesta)
                # prvá karta prehľadu je LCP (oprava 1, test_prva_karta_prehladu_bez_lazy), ostatné lenivé
                if not (cesta == 'realizacie/' and i == 0):
                    self.assertIn('loading="lazy"', karta)
        self.assertEqual(len(self.karty_zoznamu('realizacie/')), len(self.realizacie))
        self.assertGreater(spolu, len(self.realizacie))
        for r in self.w.realizacie:
            self.assertIsNotNone(r['karta'], r['slug'])
            if r['karta'].get('viz'):
                self.assertTrue(r['obal_vizualizacia'], r['slug'])
        # novinky: fotka 4 : 5 (aj z fotky na výšku); bez vlastnej použiteľnej fotky ilustračná fotka stavby
        # (oprava po bráne, bod 2), nikdy logo ani prázdny rám
        self.assertEqual(self.w.novinky, [])
        telo_n = self.info_n('novinky/')['telo']
        novinky = re.findall(r'<li class="((?:dlazdica|novinka-karta) projekt projekt-svetly[^"]*)"[^>]*>(.*?)</li>', telo_n, re.S)
        self.assertEqual(len(novinky), len(self.w_n.novinky))
        self.assertTrue(novinky)
        for (trieda, karta), n in zip(novinky, self.w_n.novinky):
            self.assertIn('<img', karta, n['slug'])
            self.assertRegex(karta, r'-(45|43|34)\.webp', n['slug'])
            self.assertNotIn('karta-znak', karta, n['slug'])
            self.assertNotIn('bez-fotky', trieda, n['slug'])
            self.assertEqual('Ilustračná fotka' in karta, not n['galeria'], n['slug'])

    def test_karty_mriezka_nie_pas(self):
        # prehľad realizácií je mriežka (nie pás) s filtrom druhu, rokov a miesta a časovou mierkou rokov
        telo = self.info('realizacie/')['telo']
        self.assertNotIn('class="pas"', telo)
        self.assertNotIn('class="prace"', telo)
        self.assertIn('<ul class="karty karty-zoznam" id="zoznam-realizacii" data-zoznam data-naraz=', telo)
        for kus in ('<select data-roky>', '<select data-mesto>', 'data-druh=""', 'class="casova-mierka"'):
            self.assertIn(kus, telo)
        for sirka, stlpce in (('480px', 2), ('640px', 3), ('1024px', 4)):
            self.assertRegex(self.css, rf'@media \(min-width:{sirka}\)\{{[^@]*\.karty\{{grid-template-columns:repeat\({stlpce},')
        # karty bez content-visibility (linka bmb-presnost): layout containment by zrušil subgrid kariet za tlačidlom
        # Zobraziť ďalšie; zobrazuje sa naraz len realizacie_naraz kariet (TestZarovnanie)
        self.assertNotIn('.dlazdica:nth-child(n+17){content-visibility:auto', self.css)

    def test_filter_miesta(self):
        # filter miesta: každé mesto z obsahu s počtom; kľúč mesta je data-mesto kariet; app.js filtruje a píše do adresy
        telo = self.info('realizacie/')['telo']
        vyber = telo.split('<select data-mesto>', 1)[1].split('</select>', 1)[0]
        mesta = re.findall(r'<option value="([a-z0-9-]+)">([^<]+) \((\d+)\)</option>', vyber)
        self.assertGreaterEqual(len(mesta), 2)
        karty = dict(re.findall(r'data-mesto="([a-z0-9-]*)" data-poradie="(\d+)"', telo))
        for kluc, meno, pocet in mesta:
            self.assertEqual(telo.count(f'data-mesto="{kluc}"'), int(pocet), meno)
            self.assertEqual(build.kluc_mesta(html.unescape(meno).replace('\u00a0', ' ')), kluc)
        self.assertTrue(karty)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("li.getAttribute('data-mesto') === s.mesto", "p.set('mesto', s.mesto)", "mesto.addEventListener('change'"):
            self.assertIn(kus, js)

    def test_roky_na_kartach_a_strankach(self):
        # karty (zoznam, služby) a stránky realizácií ukazujú len roky stavby; bez nich nič (rok príspevku preč,
        # Mário 24. 11. 2026, overenie-1), nikdy slovo Facebook
        for cesta in ['realizacie/'] + [f'{k}/' for k in self.texty['sluzby']]:
            for _, karta in self.karty_zoznamu(cesta):
                slug = re.search(r'realizacie/([a-z0-9-]+)/', karta).group(1)
                r = [x for x in self.w.realizacie if x['slug'] == slug][0]
                self.assertNotIn('Facebook', karta)
                ocak = self.w.roky_karty(r)
                self.assertEqual(ocak, r['roky'], slug)
                if not ocak:
                    self.assertIn('kota-prazdna', karta, slug)
                    continue
                if cesta == 'realizacie/':
                    # zoznam realizácií: statická kóta bez pretáčania (kratšie HTML pri desiatkach kariet, bmb-fb-uplnost)
                    kota = re.search(r'<span class="kota"><span class="kota-text">(.*?)</span></span>', karta).group(1)
                    self.assertEqual(re.sub(r'<[^>]+>', '', kota).replace('\u00a0', ' '), ocak, slug)
                    self.assertNotIn('class="valce"', karta)
                else:
                    self.assertIn(f'<span class="vh">{ocak}</span>', karta.replace('\u00a0', ' '), slug)
        for r in self.w.realizacie:
            telo = self.info(f'realizacie/{r["slug"]}/')['telo'].replace('\u00a0', ' ')
            ocak = self.w.roky_karty(r)
            detail = telo.split('class="obsah mriezka detail', 1)[1].split('</div></div>', 1)[0]
            if r['roky']:
                self.assertIn(f'<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">{ocak}</span>', detail, r['slug'])
            else:
                self.assertNotIn('kota-detail', detail, r['slug'])
                for zle in ('<dt>Rok</dt>', 'Rok príspevku', '<dt>Roky</dt>'):
                    self.assertNotIn(zle, telo, (r['slug'], zle))

    def test_rok_prispevku_nie_je_rok_realizacie(self):
        # 1. kolo pripomienok (ZMENY-KOLO-1.md 5.2 a Mário na stretnutí: „slovo príspevok preč, rok príspevku preč,
        # len roky“; overenie-1, nález vážne): rok príspevku nie je na karte, v detaile ani v popise slidera, karta
        # s ním má prázdnu kótu a filter Roky ju radí do „rok neuvedený“ (data-rok 0), takže voľba a karta sa
        # nebijú; voľby filtra len zo skutočných rokov
        s_prispevkom = [r for r in self.w.realizacie if not r['roky'] and (r['rok_prispevku'] or r['zdroj'] == 'fb')]
        self.assertTrue(s_prispevkom)
        zoznam = self.info('realizacie/')['telo'].replace('\u00a0', ' ')
        uvod = self.info('')['telo'].replace('\u00a0', ' ')
        for r in s_prispevkom:
            self.assertEqual(self.w.roky_karty(r), '', r['slug'])
            pred, za = zoznam.split(f'realizacie/{r["slug"]}/"', 1)
            li, karta = pred.rsplit('<li ', 1)[1], za.split('</li>', 1)[0]
            self.assertIn('<span class="kota kota-prazdna" aria-hidden="true"></span>', karta, r['slug'])
            self.assertIsNone(re.search(r'<span class="kota"><span class="kota-text">', karta), r['slug'])
            self.assertIn('data-rok="0"', li, r['slug'])
            rok = r['rok_prispevku'] or r['datum'][:4]
            telo = self.info(f'realizacie/{r["slug"]}/')['telo'].replace('\u00a0', ' ')
            hlava = telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]
            self.assertNotIn(f'<dd>{rok}</dd>', hlava, r['slug'])
            self.assertNotIn('<dt>Rok</dt>', hlava, r['slug'])
            # slider úvodu: popis len s miestom (RD Ahoj a Vila Holubyho)
            popis = re.search(rf'<li class="slider-popis[^"]*"><a href="realizacie/{r["slug"]}/">.*?</li>', uvod)
            if popis:
                self.assertNotIn(rok, re.sub(r'<[^>]+>', '', popis.group(0)), r['slug'])
                self.assertNotIn('class="cisla"', popis.group(0), r['slug'])
        vyber = zoznam.split('<select data-roky>', 1)[1].split('</select>', 1)[0]
        self.assertIn('<option value="0">rok neuvedený</option>', vyber)
        skutocne = {r['rok'] for r in self.w.realizacie if r['roky'] and not r['rok_z_datumu']}
        for text in re.findall(r'<option value="\d{4}">([^<]+)</option>', vyber):
            for rok in re.findall(r'\d{4}', text):
                self.assertIn(int(rok), skutocne, text)
        # nikde pri realizáciách slovo príspevok ani Termín výstavby: úvod (pás), zoznam, služby, každý detail
        stranky = ['', 'realizacie/'] + [f'{k}/' for k in self.texty['sluzby']] + [f'realizacie/{r["slug"]}/' for r in self.w.realizacie]
        for cesta in stranky:
            text = html.unescape(re.sub(r'<[^>]+>', ' ', self.info(cesta)['telo'])).lower()
            for zle in ('príspev', 'termín výstavby', 'rok príspevku', 'roky stavby'):
                self.assertNotIn(zle, text, (cesta, zle))
        # odkaz na Facebook pri realizácii bez slova príspevok
        for r in self.w.realizacie:
            if r['fb_url'] or r['zdroj'] == 'fb':
                self.assertIn('Pozrieť na Facebooku', self.info(f'realizacie/{r["slug"]}/')['telo'], r['slug'])

    def test_detail_dvojstrana(self):
        # stránka realizácie ako dvojstrana úvodu: fotka karty (pohľad A, LCP) vľavo, kóta a text vpravo; galéria
        for r in self.w.realizacie:
            telo = self.info(f'realizacie/{r["slug"]}/')['telo']
            foto = telo.split('<figure class="pohlad detail-foto">', 1)[1].split('</figure>', 1)[0]
            self.assertIn('fetchpriority="high"', foto, r['slug'])
            self.assertNotIn('loading="lazy"', foto, r['slug'])
            self.assertIn(f'obrazky/{r["slug"]}/{r["karta"]["nn"]}-', foto, r['slug'])
            self.assertIn(f'sizes="{build.sizes_pre_cover(build.SIZES_DETAIL, r["karta"]["w"], r["karta"]["h"])}"', foto)
            self.assertIn('<strong>Pohľad A:</strong>', foto)
            if len(r['galeria']) > 1:
                self.assertIn('<a href="#galeria">Galéria', foto)
            self.assertEqual('stitok-viz' in foto, bool(r['karta'].get('viz')), r['slug'])
            self.assertIn('<div class="detail-text atelier-text">', telo)
            self.assertIn('id="galeria"', telo)
        self.assertIn('.detail .pohlad-ramec{aspect-ratio:3/2;height:auto;background:var(--vapno-2)}', self.css)
        self.assertRegex(self.css, r'\.detail-foto \.pohlad-ramec img\{animation-delay:\d+ms\}')

    def test_strany_stavby_zo_stareho_webu(self):
        # adresy a weby investora, projektanta a zhotoviteľa, termín s mesiacmi a etapy zo starého webu
        # (sablona/stary-web.json, nástroj stary_web_strany.py) sú v údajoch stránky realizácie pod menom strany
        sw = json.loads((KOREN / 'sablona' / 'stary-web.json').read_text(encoding='utf-8'))['realizacie']
        self.assertGreaterEqual(len(sw), 10)
        zverejnene = {r['slug'] for r in self.w.realizacie}
        for slug, d in sw.items():
            if slug not in zverejnene:
                continue
            telo = self.info(f'realizacie/{slug}/')['telo']
            udaje = html.unescape(telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]).replace('\u00a0', ' ')
            for strana in ('investor', 'projektant', 'zhotovitel'):
                for x in d.get(strana, []):
                    for k in ('adresa', 'web'):
                        if x.get(k):
                            self.assertIn(x[k], udaje, (slug, strana, k))
            # Termín výstavby s mesiacmi je od 1. kola pripomienok preč (ZMENY-KOLO-1.md 5.2, len roky)
            self.assertNotIn('Termín výstavby', udaje, slug)
            for e in d.get('etapy', []):
                self.assertIn(e, udaje, slug)
            if d.get('rola_osoba'):
                self.assertIn(d['rola_osoba'], udaje, slug)
            # weby strán sú text, nie odkazy (cudzie domény sa mohli zmeniť)
            self.assertNotIn('<a href="http://www.', telo)
            for x in d.get('investor', []) + d.get('projektant', []) + d.get('zhotovitel', []):
                if x.get('web'):
                    self.assertNotIn(f'href="https://{x["web"]}', telo, slug)

    def test_pokrytie_stareho_webu(self):
        # každý údaj starého webu (dokoncenie/udaje-stareho-webu.json) je na svojej stránke zostaveného webu
        # (nástroj ops/weby-na-mieru/klienti/bmbuilding/nastroje/over_pokrytie.py, kód 0 = nič nechýba)
        klient = KOREN.parents[1] / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding'
        udaje = klient / 'dokoncenie' / 'udaje-stareho-webu.json'
        nastroj = klient / 'nastroje' / 'over_pokrytie.py'
        if not udaje.is_file() or not nastroj.is_file():
            self.skipTest('údaje starého webu alebo over_pokrytie.py nie sú v repozitári')
        import importlib.util
        spec = importlib.util.spec_from_file_location('over_pokrytie', nastroj)
        modul = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modul)
        out = io.StringIO()
        with redirect_stdout(out):
            kod = modul.main([str(self.ostry), '--udaje', str(udaje)])
        # zámerne chýba: skryté realizácie (zverejnene false, ZMENY-KOLO-1.md 1) a text adresy Facebooku, ktorý
        # nahradila ikona s odkazom (3.7); všetko ostatné zo starého webu musí byť na svojej stránke
        skryte = {p.stem for p in (KOREN / 'obsah' / 'realizacie').glob('*.json')} - {r['slug'] for r in self.w.realizacie}
        # 1. kolo pripomienok (ZMENY-KOLO-1.md 4 a 5.2): termín výstavby s mesiacmi je preč, na stránke ostávajú roky
        # (všetky roky z termínu musia na stránke byť); texty služieb sú doslovne starý web, takže údaj služby, ktorého
        # overenie bolo podľa nášho prepísaného znenia, je pokrytý, keď je na stránke celé pôvodné znenie (povodne)
        podla_id = {u['id']: u for u in json.loads(udaje.read_text(encoding='utf-8'))}

        def pokryte(riadok):
            u = podla_id.get(riadok.split()[1])
            if not u or not u.get('stranka'):
                return False
            text = html.unescape(re.sub(r'<[^>]+>', ' ', (self.ostry / u['stranka']).read_text(encoding='utf-8')))
            text = self.norm_medzery(text)
            if u['typ'] == 'termin':
                roky = re.findall(r'(?:19|20)\d{2}', ' '.join(u['overenie']))
                return bool(roky) and all(r in text for r in roky)
            if u['typ'] in ('sluzba', 'sluzba-bod'):
                return self.norm_medzery(u['povodne']) in text
            return False
        # referencie úvodu starého webu boli v tmavom páse; ten je preč (ZMENY-KOLO-1.md 3A.6, „nechať len
        # slideshow“), stavba je pokrytá, keď je v zozname realizácií (/realizacie/, menu Realizácie)
        def v_zozname(riadok):
            return riadok.startswith('CHÝBA uvod-referencie na index.html:') and 'realizacie/index.html' in riadok
        chyba = [r for r in out.getvalue().splitlines() if r.startswith('CHÝBA ')
                 and not any(f'realizacie/{s}/' in r for s in skryte) and 'facebook-ikona' not in r and not pokryte(r)
                 and not v_zozname(r)]
        self.assertEqual(chyba, [], out.getvalue())
        self.assertTrue(skryte)
        self.assertIn(f'href="{self.firma["facebook"]}"', self.info('kontakt')['html'])

    def test_novinky_v_komponentoch(self):
        # novinka (zostava s "novinky": true): hlava s dátumom a stavbou (pole realizacia) s rokmi stavby, dvojstrana,
        # galéria s lightboxom
        self.assertTrue(self.w_n.novinky)
        for n in self.w_n.novinky:
            telo = self.info_n(f'novinky/{n["slug"]}/')['telo'].replace('\u00a0', ' ')
            self.assertIn('<dt>Dátum</dt>', telo)
            r = self.w_n.realizacia_novinky(n)
            if r:
                self.assertIn(f'href="../../realizacie/{r["slug"]}/"', telo, n['slug'])
                self.assertIn(self.w_n.roky_karty(r), telo, n['slug'])
            if n['galeria']:
                self.assertIn('<dialog class="lightbox" id="lightbox"', telo)
                self.assertIn('<figure class="pohlad detail-foto">', telo)

    def test_sluzby_v_komponentoch(self):
        # služba: doslovný text vľavo, box kalkulačky s dokladom SKSI a Zavolať, karty realizácií v tej role;
        # očíslovaný zoznam činností (náš) je od 1. kola pripomienok preč, text je celý v hlavnom stĺpci
        for k, t in self.texty['sluzby'].items():
            telo = self.info(f'{k}/')['telo'].replace(chr(160), ' ')
            self.assertIn('<aside class="kalkulacka sluzba-bok"', telo)
            self.assertIn('<div class="sluzba-hlavne atelier-text cinnost-text sluzba-doslova">', telo, k)
            self.assertNotIn('cinnost-c cisla', telo, k)
            self.assertNotIn('<ol class="cinnosti">', telo, k)
            # fázy Postupu sú od 1. kola pripomienok preč aj zo stránky Stavebný dozor (ZMENY-KOLO-1.md 3.6)
            self.assertNotIn('<ol class="fazy">', telo, k)
            # H1, title a description pre vyhľadávače ostávajú (ZMENY-KOLO-1.md časť 4)
            self.assertEqual(self.info(f'{k}/')['h1'], t['h1'], k)
            for zle in ('veta', 'odseky', 'priklady', 'cinnosti_nadpis', 'cinnosti_uvod', 'pocet_popis'):
                self.assertNotIn(zle, t, k)

    # doslovný text služieb zo zadania 1. kola (ZMENY-KOLO-1.md časť 4, starý web), zapísaný tu nezávisle od
    # texty.json; povolené sú len opravy medzier „prác .“ na „prác.“ a „( projekt“ na „(projekt“
    DOSLOVNE_SLUZBY = {
        'stavebny-manazment': (
            'BM Building, s.r.o je stavebno-manažérska spoločnosť, ktorá poskytuje podporné služby investorom a developerom pri realizácii ich projektov. '
            'Naša spoločnosť zabezpečí bezproblémový prechod z projektovej do realizačnej fázy. Stavebný manažment chápeme ako ucelený proces, ktorý zahŕňa koordináciu všetkých profesií, riadenie a kontrolu stavby počas prípravy, realizácie až po jej kolaudáciu na základe definovaných právnych, ekonomických a technických podmienok tak, aby sme včas odhalili a obmedzili riziká, našli riešenia a na druhej strane využili všetky poskytnuté príležitosti. '
            'Ciele investora, resp. užívateľa splníme v požadovanej kvalite, v určenom termíne a za vopred určené náklady. '
            'Za nášho klienta prevezmeme riadenie a koordináciu procesu zadania stavebných prác . Po výbere dodávateľa stavby zabezpečíme uzatvorenie zmlúv, pričom koordinujeme aj ich právnu kontrolu.'),
        'stavebny-dozor': (
            'V rámci výkonu stavebný dozor ponúkame tieto činnosti: '
            'oboznámenie sa s podkladmi ( projekt, zmluvy, stavebné povolenia, územné rozhodnutie) '
            'Odovzdanie staveniska a zabezpečenie zápisu o tom do stavebného denníka '
            'Protokolárne odovzdanie základného smerového a výškového vytýčenia stavby zhotoviteľovi '
            'Kontrola dodržiavania podmienok vydaných na uskutočnenie stavby najmä stavebného povolenia a územného rozhodnutia '
            'Odsúhlasovanie dodatkov a zmien projektu '
            'Bezodkladné informovanie investora o všetkých závažných okolnostiach '
            'Kontrola vecnej a cenovej správnosti a úplnosti oceňovaných podkladov a platobných dokladov '
            'Kontrola vhodnosti stavebných materiálov a prevádzky technického vybavenia na stavbe '
            'Kontrola odborného ukladania stavebných materiálov a stavebných hmôt '
            'Kontrola tých častí diela, ktoré boli v ďalšom postupe zakryté alebo sa stali neprístupnými a zapísanie výsledkov kontroly do stavebného denníka '
            'Preberanie vykonaných stavebných prác zo strany dodávateľa '
            'Spolupráca s projektantom '
            'Sledovanie, či zhotoviteľ vykonáva predpísané a dohodnuté skúšky materiálov, konštrukcií a prác '
            'Sledovanie vedenia stavebného denníka '
            'Uplatňovanie námetov, smerujúcich k zhospodárneniu výstavby '
            'Kontrola postupu prác podľa časového plánu výstavby a zmlúv '
            'Kontrola odstránenia vád a nedorobkov '
            'Kontrola dodržiavania BOZP '
            'Účasť na kolaudačnom konaní'),
        'stavbyveduci': (
            'V rámci výkonu stavbyvedúci ponúkame činnosti podľa stavebného zákona: '
            'Stavebný zákon ukladá stavbyvedúcemu riadiť a koordinovať stavebné práce a iné činnosti na stavenisku a na stavbe a viesť o nich evidenciu v stavebnom denníku. '
            'V zmysle ustanovení Stavebného zákona je stavbyvedúci oprávnený: '
            'určovať začatie a ukončenie jednotlivých činností na stavenisku a na stavbe, '
            'riadiť vykonávanie stavebných prác a iných činností na stavenisku a na stavbe, '
            'organizovať pohyb činnosti osôb na stavenisku a na stavbe, '
            'kontrolovať výrobky a materiály pre stavbu, zisťovať ich kvalitu a vhodnosť na zabudovanie do stavby, určovať spôsob ich skladovania na stavenisku, '
            'dávať príkazy na okamžité zastavenie stavebných prác a iných činností na stavenisku a na stavbe, ak sa vyskytla prekážka, pre ktorú je ich ďalšie vykonávanie neprípustné, '
            'koordinovať poradie stavebných prác, '
            'vykázať cudzie osoby zo staveniska a zo stavby.'),
    }

    @staticmethod
    def norm_medzery(s):
        """Normalizácia medzier pre porovnanie so zadaním: biele znaky na jednu medzeru, bez medzery pred bodkou
        a čiarkou a po otváracej zátvorke (jediné povolené úpravy zápisu, ZMENY-KOLO-1.md časť 4)."""
        s = re.sub(r'\s+', ' ', s.replace(chr(160), ' ')).strip()
        return re.sub(r'\(\s+', '(', re.sub(r'\s+([.,:])', r'\1', s))

    def test_sluzby_doslova_zo_stareho_webu(self):
        # hlavný text stránky služby je slovo od slova text starého webu (zadanie časť 4) po normalizácii medzier;
        # nič iné v hlavnom stĺpci, ten istý text ako Činnosť na úvode (jeden zdroj texty.json cinnost_doslova)
        uvod = self.info('')['telo']
        for k, ocak in self.DOSLOVNE_SLUZBY.items():
            telo = self.info(f'{k}/')['telo']
            hlavny = telo.split('<div class="sluzba-hlavne atelier-text cinnost-text sluzba-doslova">', 1)[1]
            hlavny = hlavny.split('<aside class="kalkulacka sluzba-bok"', 1)[0]
            text = html.unescape(re.sub(r'<[^>]+>', ' ', hlavny))
            self.assertEqual(self.norm_medzery(text), self.norm_medzery(ocak), k)
            # zápis na webe: opravené len medzery, inak presne (bez medzery pred bodkou, bez medzery po zátvorke)
            cisty = html.unescape(re.sub(r'<[^>]+>', '', hlavny)).replace(chr(160), ' ')
            self.assertNotIn('prác .', cisty, k)
            self.assertNotIn('( projekt', cisty, k)
            # karta Činnosti na úvode má ten istý text
            karta = uvod.split(f'aria-labelledby="cinnost-{k}">', 1)[1].split('</article>', 1)[0]
            karta = karta.split('<div class="cinnost-text">', 1)[1]
            self.assertEqual(self.norm_medzery(html.unescape(re.sub(r'<[^>]+>', ' ', karta))), self.norm_medzery(ocak), k)
        # „s.r.o je“ v prvej vete služby ostáva bez bodky (slovo od slova)
        self.assertIn('BM Building, s.r.o je stavebno-manažérska', self.info('stavebny-manazment/')['telo'].replace(chr(160), ' '))

    def test_firma_sluzby_doslova(self):
        # firma.json sluzby: cinnosti doslovne zo starého webu, kratky_popis len doslovná veta (zadanie časť 4)
        cd = self.texty['cinnost_doslova']
        podla = {c['slug']: c for k, c in cd.items() if not k.startswith('_')}
        vety = {self.norm_medzery(x) for c in podla.values()
                for x in list(c.get('odseky', [])) + list(c.get('body', [])) + list(c.get('body_2', []))
                + [c.get('uvod', ''), c.get('uvod_2', '')] if x}
        self.assertEqual({s['slug'] for s in self.firma['sluzby']}, set(podla))
        for s in self.firma['sluzby']:
            c = podla[s['slug']]
            self.assertEqual(s.get('cinnosti', []), list(c.get('body', [])) + list(c.get('body_2', [])), s['slug'])
            self.assertIn(self.norm_medzery(s['kratky_popis']), vety, s['slug'])

    def test_bez_otazky_kolaudacia(self):
        # „Kto je pri kolaudácii?“ preč všade (stránky aj texty), ostatné otázky zo zákona ostávajú
        self.assertNotIn('kolaudacia', self.texty['otazky'])
        for p in self.ostry.rglob('*.html'):
            self.assertNotIn('Kto je pri kolaudácii', p.read_text(encoding='utf-8'), str(p))
        dozor = self.info('stavebny-dozor/')['telo'].replace(chr(160), ' ')
        for k in self.texty['sluzby']['stavebny-dozor']['otazky']:
            self.assertIn(html.escape(self.texty['otazky'][k]['otazka'], quote=False), dozor)
        self.assertEqual(self.texty['sluzby']['stavebny-dozor']['otazky'], ['co-robi-dozor', 'rozdiel'])

    # tabuľka zadania 1. kola, časť 1 (ZMENY-KOLO-1.md): súbory preč zo zoznamu fotky, súbory, ktoré ostávajú,
    # zobrazené fotky galérie (len kde ich tabuľka vymenúva) a rola BM Building
    TABULKA_KOLO1 = {
        'rd-ahoj-bratislava-nove-mesto': {'prec': '08 09', 'ostava': '01 02 03 04 05 06 07', 'galeria': '01 02 03 04 05 06 07'},
        'rekonstrukcia-vila-holubyho-bratislava': {'prec': '05', 'ostava': '01 02 03 04 06 09 10', 'galeria': '01 02 03 04 06 09 10'},
        'filmovy-klub-lumiere-spitalska-bratislava': {'prec': '01 03', 'ostava': '02 04 05 06 07 08 09 10',
                                                       'galeria': '08 02 04 05 06 07 09 10'},
        'rd-sklenarska-bratislava': {'prec': '01 02 03 04', 'ostava': '05 06 07 08', 'galeria': '05 06 07 08', 'viz': True},
        'dvojdom-s-bratislava': {'prec': '', 'ostava': '01 03 04 05', 'galeria': '01 03 04 05'},
        'rekonstrukcia-okanikova-bratislava': {'prec': '20 21 22 23',
                                               'ostava': '01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19'},
        'novostavba-rd-nitrianske-hrnciarovce': {'prec': '', 'ostava': '01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16'},
        'bytove-domy-vtacnik-bratislava': {'rola': 'Projektový manažér a stavebný dozor'},
        'bytovy-dom-devinske-luky-bratislava': {'prec': '20 21 22 23', 'rola': 'Projektový manažér a stavebný dozor'},
        'bytovy-dom-na-krizovatkach-bratislava': {'rola': 'Projektový manažér a stavebný dozor'},
        'bytovy-dom-rosnicka-bratislava': {'prec': '03 06 07 09 10 11 17', 'ostava': '01 02 12 13 14 15 16 18 19'},
        'rekonstrukcia-venturska-bratislava': {'prec': '02 03 07 08 09 11 14 19', 'galeria': '06 04 05 12 13 15'},
        'rd-jarovce-bratislava': {'prec': '03 04 05', 'ostava': '01 06', 'galeria': '01 06'},
        'dom-betliarska-bratislava-petrzalka': {'prec': '02 03 06', 'ostava': '01 05', 'galeria': '01 05',
                                                'rola': 'Projektový manažment a stavebný dozor'},
        'rd-pezinok': {'prec': '03', 'ostava': '01', 'galeria': '01'},
        'rezidencia-grunty-bratislava': {'prec': '08', 'ostava': '01 02 03', 'galeria': '01 02 03',
                                         'rola': 'Projektový manažment a stavebný dozor'},
    }
    SKRYTE_KOLO1 = ('filmovy-klub-spitalska-bratislava', 'rekonstrukcia-5-ambulancii-pre-lekarov')

    def test_galerie_podla_tabulky_kolo1(self):
        # časť 1 na úrovni zdrojových súborov (obsah/realizacie/*.json) aj zobrazenej galérie (poradie po 5.3 a 5.5)
        podla = {r['slug']: r for r in self.w.realizacie}
        for slug, t in self.TABULKA_KOLO1.items():
            d = json.loads((KOREN / 'obsah' / 'realizacie' / f'{slug}.json').read_text(encoding='utf-8'))
            subory = [Path(f['subor']).stem for f in d['fotky']]
            self.assertEqual(len(subory), len(set(subory)), slug)
            self.assertEqual(d.get('upravene'), '2026-11-24', slug)
            for nn in t.get('prec', '').split():
                self.assertNotIn(nn, subory, (slug, nn))
            if t.get('ostava'):
                self.assertTrue(set(t['ostava'].split()) <= set(subory), (slug, subory))
            if t.get('galeria'):
                self.assertEqual([Path(f['rel']).stem for f in podla[slug]['galeria']], t['galeria'].split(), slug)
            if t.get('viz'):
                self.assertTrue(all(f.get('druh') == 'vizualizacia' for f in d['fotky']), slug)
            if t.get('rola'):
                self.assertEqual(d['rola'], t['rola'], slug)
        for slug in self.SKRYTE_KOLO1:
            d = json.loads((KOREN / 'obsah' / 'realizacie' / f'{slug}.json').read_text(encoding='utf-8'))
            self.assertIs(d['zverejnene'], False, slug)
            self.assertNotIn(slug, podla)

    def test_rosnicka_02_hned_za_01(self):
        # ZMENY-KOLO-1.md 1 a 5.5: zobrazená fotka 02 (pole typ HE) je v galérii hneď za reprezentatívnou 01
        d = json.loads((KOREN / 'obsah' / 'realizacie' / 'bytovy-dom-rosnicka-bratislava.json').read_text(encoding='utf-8'))
        typ = {Path(f['subor']).stem: f.get('typ') for f in d['fotky']}
        self.assertEqual(typ['02'], 'HE')
        r = next(x for x in self.w.realizacie if x['slug'] == 'bytovy-dom-rosnicka-bratislava')
        self.assertEqual([Path(f['rel']).stem for f in r['galeria'][:2]], ['01', '02'])
        galeria = self.info('realizacie/bytovy-dom-rosnicka-bratislava/')['telo'].split('<ul class="galeria', 1)[1]
        polozky = galeria.split('</ul>', 1)[0].split('<li class="galeria-polozka')[1:]
        self.assertIn(f'obrazky/bytovy-dom-rosnicka-bratislava/{r["galeria"][1]["nn"]}-', polozky[1])

    def test_druhy_bez_rekonstrukcii(self):
        # ZMENY-KOLO-1.md 5.1: filter Bytové domy, Rodinné domy, Iné; druh podľa poľa kategoria; rekonštrukcie nikde
        telo = self.info('realizacie/')['telo']
        druhy = re.findall(r'<button class="filter" type="button" data-druh="([a-z]*)" aria-pressed="[a-z]+">([^<]+)<span>', telo)
        self.assertEqual(druhy, [('', 'Všetky'), ('bytove', 'Bytové domy'), ('rodinne', 'Rodinné domy'), ('ine', 'Iné')])
        self.assertNotIn('rekonstrukcie', telo)
        self.assertNotIn('Rekonštrukcie', html.unescape(re.sub(r'<[^>]+>', ' ', telo)))
        for r in self.w.realizacie:
            d = json.loads((KOREN / 'obsah' / 'realizacie' / f'{r["slug"]}.json').read_text(encoding='utf-8'))
            self.assertIn(d.get('kategoria'), ('bytove', 'rodinne', 'ine'), r['slug'])
            self.assertEqual(r['kategorie'], [d['kategoria']], r['slug'])
            self.assertIn(f'data-druh="{d["kategoria"]}"', telo.split(f'realizacie/{r["slug"]}/"', 1)[0].rsplit('<li ', 1)[1])
            udaje = self.info(f'realizacie/{r["slug"]}/')['telo']
            self.assertNotIn('Rekonštrukcie', udaje.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0])
        # kino, most, SFÚ a polyfunkčné objekty sú Iné (stretnutie 24. 11.)
        ine = {r['slug'] for r in self.w.realizacie if r['kategorie'] == ['ine']}
        self.assertTrue({'filmovy-klub-lumiere-spitalska-bratislava', 'ufo-most-snp-bratislava',
                         'digitalizacne-pracovisko-sfu-spitalska-bratislava'} <= ine)

    def test_skryte_realizacie_bez_odkazov(self):
        # ZMENY-KOLO-1.md 5.6: skryté realizácie nemajú stránku ani odkaz nikde (pás, služby, novinky, zoznam, sitemap);
        # staré adresy ich stránok presmerujú na /realizacie/
        for slug in self.SKRYTE_KOLO1:
            self.assertFalse((self.ostry / 'realizacie' / slug).exists(), slug)
            for p in self.ostry.rglob('*.html'):
                s = p.read_text(encoding='utf-8')
                self.assertNotIn(f'realizacie/{slug}', s, (slug, str(p)))
                self.assertNotIn(f'obrazky/{slug}/', s, (slug, str(p)))
            self.assertNotIn(slug, (self.ostry / 'sitemap.xml').read_text(encoding='utf-8'))
            self.assertNotIn(slug, json.dumps(self.w.nastavenia.get('pas_vyber')))
            self.assertNotIn(slug, json.dumps(self.w.nastavenia.get('uvod_slider')))
        presm = json.loads((KOREN / 'sablona' / 'presmerovania.json').read_text(encoding='utf-8'))['presmerovania']
        stare = [z['stara'] for z in presm if any(f'/realizacie/{s}/' == z.get('ciel') for s in self.SKRYTE_KOLO1)]
        self.assertEqual(len(stare), 2)
        for stara in stare:
            s = (self.ostry / stara.strip('/') / 'index.html').read_text(encoding='utf-8')
            self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/realizacie/">', s, stara)

    def test_karta_len_s_vizualizaciami(self):
        # ZMENY-KOLO-1.md 5.4: realizácia len s vizualizáciami (RD Sklenárska, RD Devín) má na karte prvú vizualizáciu
        # so štítkom Vizualizácia, nie prázdny rám
        mriezka = self.info('realizacie/')['telo']
        for slug in ('rd-sklenarska-bratislava', 'rd-devin-bratislava'):
            r = next(x for x in self.w.realizacie if x['slug'] == slug)
            self.assertTrue(all(f.get('viz') for f in r['galeria']), slug)
            self.assertIs(r['karta'], r['galeria'][0], slug)
            karta = mriezka.split(f'href="../realizacie/{slug}/"', 1)[1].split('</li>', 1)[0]
            self.assertIn(f'obrazky/{slug}/{r["karta"]["nn"]}-', karta)
            self.assertIn('<span class="stitok-viz">Vizualizácia</span>', karta)
            # bez rokov stavby prázdna kóta: rok príspevku na web nejde (Mário 24. 11. 2026, overenie-1)
            self.assertEqual('kota-prazdna' in karta, not r['roky'], slug)

    def test_novinka_asb(self):
        # ZMENY-KOLO-1.md 2.4: novinka ASB (časopis, nie Facebook) bez id, 3 obrázky, pri stavbe Lumière; stránka bez
        # odkazu „Pôvodný príspevok na Facebooku“. Od 5.7 (24. 11. večer) sú Novinky vypnuté a časopis je pri Lumière
        # (test_casopis_asb_pri_lumiere); súbor novinky ostáva a so zapnutými Novinkami sa zostaví ako predtým
        d = json.loads((KOREN / 'obsah' / 'novinky' / 'casopis-asb-2024-02.json').read_text(encoding='utf-8'))
        self.assertNotIn('id', d)
        self.assertEqual(d['realizacia'], 'filmovy-klub-lumiere-spitalska-bratislava')
        self.assertFalse(any(x['slug'] == 'casopis-asb-2024-02' for x in self.w.novinky))
        n = next(x for x in self.w_n.novinky if x['slug'] == 'casopis-asb-2024-02')
        self.assertEqual(len(n['galeria']), 3)
        telo = self.info_n('novinky/casopis-asb-2024-02/')['telo']
        text = telo.split('<section class="sekcia kontakt"', 1)[0]
        self.assertNotIn('facebook', text.lower())
        self.assertNotIn('príspevok na Facebooku', text)
        self.assertIn('href="../../realizacie/filmovy-klub-lumiere-spitalska-bratislava/"', text)
        self.assertIn('novinky/casopis-asb-2024-02/"', self.info_n('novinky/')['telo'])
        # ostatné novinky (z Facebooku) odkaz na príspevok majú
        for x in self.w_n.novinky:
            if x['slug'] != 'casopis-asb-2024-02':
                self.assertIn('Pôvodný príspevok na Facebooku', self.info_n(f'novinky/{x["slug"]}/')['telo'], x['slug'])
        self.assertIn('Novinky', self.info_n('')['html'].split('<nav class="menu-d"', 1)[1].split('</nav>', 1)[0])
        self.assertNotIn('Novinky', self.info('')['html'].split('<nav class="menu-d"', 1)[1].split('</nav>', 1)[0])

    def test_slider_finalne_predlohy(self):
        # nastavenia uvod_slider: pevné poradie a finálne súbory a stredy podľa agentov FOTKY a UPSCAYL; každá fotka
        # slidera je z fotky-web, a kde jej výrez 5 : 4 zo zdroja nestačí, z predlohy zväčšenej Upscaylom
        # RD Ahoj: stred vyššie a orez_dole 0,17 (ÚVOD v3: dom so strechou a fasádou, nie cesta s kanálom a obrubníkom)
        ocak = [('rd-ahoj-bratislava-nove-mesto', '01.jpg', [0.5, 0.2]), ('dvojdom-s-bratislava', '01.jpg', [0.45, 0.5]),
                ('rekonstrukcia-okanikova-bratislava', '05.jpg', [0.57, 0.5]),
                ('novostavba-rd-nitrianske-hrnciarovce', '01.jpg', [0.52, 0.5]),
                ('filmovy-klub-lumiere-spitalska-bratislava', '08.jpg', [0.2, 0.5]), ('ufo-most-snp-bratislava', '10.jpg', [0.5, 0.68]),
                ('bytove-domy-vtacnik-bratislava', '09.jpg', [0.54, 0.5]),
                ('bytovy-dom-na-krizovatkach-bratislava', '01.jpg', [0.47, 0.5]),
                ('rekonstrukcia-vila-holubyho-bratislava', '01.jpg', [0.5, 0.45]),
                ('rekonstrukcia-venturska-bratislava', '06.jpg', [0.55, 0.5]), ('rd-kalvaria-bratislava', '01.jpg', [0.5, 0.46])]
        self.assertEqual([(x['slug'], x['subor'], x['stred']) for x in self.w.nastavenia['uvod_slider']], ocak)
        fotky = self.w.slider_fotky()
        self.assertEqual([f['rel'] for _, f, _ in fotky], [f'{s}/{f}' for s, f, _ in ocak])
        upscayl = ('ultrasharp-4x', 'upscayl-standard-4x')
        for r, f, _ in fotky:
            self.assertTrue(f.get('fw'), f['rel'])
            vyrez = build.orez_rozmer(f['w'], f['h'], (5, 4))[0]
            if vyrez < 1800:
                self.assertIn(f['fw']['predloha']['model'], upscayl, f['rel'])
                self.assertGreaterEqual(build.orez_rozmer(*f['zaklad'], (5, 4))[0], 1800, f['rel'])
        # Hrnčiarovce 01 zväčšená Upscaylom (časť 2.2), súbory slidera v HTML úvodu sú jej šírky
        hrn = next(f for _, f, _ in fotky if f['rel'] == 'novostavba-rd-nitrianske-hrnciarovce/01.jpg')
        self.assertEqual(hrn['fw']['predloha']['model'], 'upscayl-standard-4x')
        self.assertEqual(hrn['zaklad'], (2844, 1600))

    def test_reduced_motion(self):
        # obmedzený pohyb: mimo bloku no-preference žiadna animácia; prechody tlačidiel, ikon sietí, filtra, lišty,
        # lightboxu a fotiek kariet a galérie vypnuté; prvky hrdinu a sekcie úvodu hneď v konečnom stave (ÚVOD v3)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        blok = css.split('@media (prefers-reduced-motion:reduce){', 1)[1]
        hlbka, koniec = 1, 0
        for i, ch in enumerate(blok):
            hlbka += ch == '{'
            hlbka -= ch == '}'
            if hlbka == 0:
                koniec = i
                break
        blok = blok[:koniec]
        self.assertNotIn('.pas', blok)
        prechody = blok.split('{transition:none}', 1)[0]
        for sel in ('.tlacidlo', '.siet', '.filter', '.lista', '.lightbox-tl', '.projekt-foto img', '.galeria-odkaz img',
                    '.snimka', '.vstup'):
            self.assertIn(sel, prechody)
        # konečný stav: bez animácie, plná nepriehľadnosť, bez posunu a orezu; číslice kót bez pretáčania
        self.assertIn('.snimka-0 img,.uvod-nadtitulok,.uvod-h1,.firma-text,.firma-fakty .udaj,.firma-zdroj,.uvod-akcie,'
                      '.slider-popisy,.slider-ovladanie,.vstup{animation:none;opacity:1;transform:none;clip-path:none}', blok)
        self.assertIn('.stlpec{transition:none}', blok)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)')", js)

    def test_mobilne_menu(self):
        s = self.info('')['html']
        panel = s.split('<details class="menu-m">', 1)[1].split('</details>', 1)[0]
        for ciel in ('#cinnost', 'realizacie/', 'opravnenia/', 'kontakt/'):
            self.assertIn(f'href="{ciel}"', panel)
        self.assertNotIn('#postup', panel)
        for k in self.texty['sluzby']:
            self.assertIn(f'href="{k}/"', panel)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("'details.menu-m'", "e.key === 'Escape'", "'Zavrieť'", 'tl.focus()'):
            self.assertIn(kus, js)

    def test_kontrast_tokenov(self):
        # dvojice farieb z PLAN-1-1.md časť 6 vzorcom WCAG: text aspoň 4,5, čiary aspoň 3
        koren = self.css.split(':root{', 1)[1].split('}', 1)[0]
        t = dict(re.findall(r'--([a-z0-9-]+):(#[0-9a-f]{6})', koren))

        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

        def k(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)
        for a, b, prah in (('modra', 'vapno', 4.5), ('modra', 'vapno-2', 4.5), ('tlmeny', 'vapno', 4.5),
                           ('modra-svetla', 'grafit', 4.5), ('na-tmavom-2', 'grafit', 4.5), ('vapno', 'grafit', 4.5),
                           ('modra-ciara', 'vapno', 3), ('grafit', 'vapno-2', 4.5)):
            self.assertGreaterEqual(k(t[a], t[b]), prah, (a, b))
        self.assertGreaterEqual(k('#5d5d58', t['vapno-2']), 4.5)
        self.assertGreaterEqual(k('#ffffff', t['modra']), 4.5)

    def test_vyzvy_a_zaver(self):
        f = self.firma
        tel, mail = f'href="tel:{f["telefon_href"]}"', f'href="mailto:{f["email"]}"'
        for cesta, druhy in (('', self.texty['vyzvy']['zaver']), ('realizacie/', self.texty['vyzvy']['zaver']),
                             ('stavbyveduci/', self.texty['sluzby']['stavbyveduci']['zaver']),
                             ('realizacie/rd-pezinok/', self.texty['vyzvy']['realizacia'])):
            s = self.info(cesta)['html'].replace(' ', ' ')
            # záver = sekcia Kontakt z ukážky s výzvou stránky (kontakt_sekcia)
            zaver = s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
            self.assertIn(druhy, zaver, cesta)
            self.assertIn(tel, zaver, cesta)
            self.assertIn(mail, zaver, cesta)
            self.assertIn('data-vyzva-dole', s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1][:80], cesta)
            # spodná lišta na mobile ako v ukážke: Zavolať a Napísať e-mail
            self.assertIn('<nav class="lista" aria-label="Rýchly kontakt"><a class="tlacidlo" ' + tel, s, cesta)
            self.assertIn('<a class="tlacidlo tlacidlo-obrys" ' + mail + '>Napísať e-mail</a></nav>', s, cesta)
            self.assertIn('class="tlacidlo tlacidlo-male hlavicka-tel" ' + tel, s.split('</header>', 1)[0], cesta)
        # na /kontakt/ je sekcia Kontakt hlavným obsahom (raz, bez výzvy záveru)
        kontakt = self.info('kontakt/')['telo']
        self.assertEqual(kontakt.count('<section class="sekcia kontakt"'), 1)
        self.assertNotIn(self.texty['vyzvy']['zaver'], kontakt.replace(' ', ' '))
        self.assertIn('Zavolajte alebo napíšte e-mail.', kontakt)
        self.assertIn('<noscript><style>img[data-src]{display:none}@media (max-width:767px){.lista{transform:none;visibility:visible}}</style></noscript>',
                      self.info('')['html'])
        self.assertIn('.lista .tlacidlo{width:100%;min-height:50px', self.css)
        self.assertIn('.pata{border-top:1px solid var(--grafit);padding:48px 0 calc(48px + 76px + env(safe-area-inset-bottom))', self.css)

    def test_menu_ako_stary_web_a_pata(self):
        # hlavička ukážky (trieda hlava): menu podľa starého webu (Činnosť, Realizácie, Postup, Novinky, Oprávnenia,
        # Kontakt), Zavolať; päta ukážky s Facebookom a nenápadne „Web: ARLing“
        s = self.info('realizacie/')['html']
        hlavicka = s.split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
        menu_d = hlavicka.split('<nav class="menu-d"', 1)[1].split('</nav>', 1)[0]
        polozky = [html.unescape(x) for x in re.findall(r'<a [^>]*>([^<]+)</a>', menu_d)]
        ocakavane = ['Činnosť', 'Realizácie'] + (['Novinky'] if self.w.novinky else []) + ['Oprávnenia', 'Kontakt']
        self.assertEqual(polozky, ocakavane)
        self.assertIn('<a href="../realizacie/" aria-current="page">Realizácie</a>', hlavicka)
        self.assertIn('href="../#cinnost"', hlavicka)
        self.assertNotIn('#postup', hlavicka)
        self.assertNotIn('#kde', hlavicka)
        # mobilné menu funguje bez JS (details) a má aj tri služby
        panel = hlavicka.split('<details class="menu-m">', 1)[1]
        for k in self.texty['sluzby']:
            self.assertIn(f'href="../{k}/"', panel)
        pata = s.split('<footer class="pata"', 1)[1]
        self.assertIn('<a class="pata-arling" href="https://arling.sk/web/">Web: ARLing</a>', pata)
        self.assertIn(f'href="{self.firma["facebook"]}"', pata)
        self.assertIn('<a class="znak pata-znak" href="../">', pata)
        self.assertIn('IČO 35 947 519.', pata.replace(' ', ' '))
        for kus in ('class="pata-zaver"', 'class="pata-odkazy"', 'class="male"', 'href="../opravnenia/"',
                    'href="../ochrana-udajov/"'):
            self.assertIn(kus, pata)
        # Instagram len s istou adresou v sablona/nastavenia.json (INSTAGRAM.md: neistá, skrytá)
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        if not nast.get('instagram'):
            for p in stranky(self.ostry):
                self.assertNotIn('instagram.com', p.read_text(encoding='utf-8'), p)

    def test_kde_nie_je_na_uvode_mesta_v_filtri(self):
        # sekcia Kde ukážka nemá (PLAN-1-1.md 1.4): z úvodu preč; mestá s počtom ostávajú vo filtri na /realizacie/
        self.assertNotIn('id="kde"', self.info('')['telo'])
        pocty = {}
        for r in self.w.realizacie:
            m = r['miesto'].split(',')[0].strip()
            if m:
                pocty[build.kluc_mesta(m)] = pocty.get(build.kluc_mesta(m), 0) + 1
        telo = self.info('realizacie/')['telo']
        vyber = telo.split('<select data-mesto>', 1)[1].split('</select>', 1)[0]
        mesta = dict(re.findall(r'<option value="([a-z0-9-]+)">[^<]+ \((\d+)\)</option>', vyber))
        self.assertEqual({k: int(v) for k, v in mesta.items()}, pocty)

    def test_sluzby_odkazuju_na_vsetky_realizacie(self):
        for k, t in self.texty['sluzby'].items():
            telo = self.info(f'{k}/')['telo']
            for slug, d in self.realizacie.items():
                if any(kl in (d.get('rola') or '').lower() for kl in t['kluce']):
                    self.assertIn(f'realizacie/{slug}/', telo, f'{k}: {slug}')
            self.assertIn('href="tel:', telo.split('class="kalkulacka sluzba-bok"', 1)[1].split('</aside>', 1)[0], k)
            urovne = [int(m) for m in re.findall(r'<h([1-6])[ >]', self.info(f'{k}/')['html'])]
            self.assertTrue(all(b <= a + 1 for a, b in zip(urovne, urovne[1:])), (k, urovne))

    def test_filter_a_data_atributy_zoznamu(self):
        telo = self.info('realizacie/')['telo']
        r = Rozbor()
        r.feed(telo)
        filtre = [a for t, a in r.prvky if 'data-filtre' in a]
        self.assertEqual(len(filtre), 1)
        self.assertIn('hidden', filtre[0])  # bez JS skrytý, zoznam ukazuje všetko
        tlacidla = [a for t, a in r.prvky if t == 'button' and 'data-druh' in a]
        self.assertEqual(tlacidla[0]['data-druh'], '')
        self.assertEqual(tlacidla[0]['aria-pressed'], 'true')
        self.assertTrue({a['data-druh'] for a in tlacidla[1:]} <= KLUCE_KATEGORII)
        self.assertTrue(all(a['aria-pressed'] == 'false' and a['type'] == 'button' for a in tlacidla[1:]))
        pocty = self.w.pocty_druhov()
        for a in tlacidla[1:]:
            self.assertIn(f'data-druh="{a["data-druh"]}" aria-pressed="false">', telo)
            self.assertRegex(telo, rf'data-druh="{a["data-druh"]}" aria-pressed="false">[^<]*<span>{pocty[a["data-druh"]]}</span>')
        self.assertRegex(telo, r'<select data-roky><option value="">všetky</option>(<option value="(\d{4}|0)">[^<]+</option>)+</select>')
        self.assertIn('<select data-zoradit><option value="najnovsie">najnovšie</option><option value="najstarsie">najstaršie</option>'
                      '<option value="nazov">abecedne</option></select>', telo)
        # postupné načítanie: všetky karty v HTML, app.js ukáže prvých data-naraz a tlačidlo Zobraziť ďalšie (bez JS skryté)
        self.assertIn(f'<ul class="karty karty-zoznam" id="zoznam-realizacii" data-zoznam data-naraz="{build.REALIZACIE_NARAZ}">', telo)
        self.assertIn('<p class="viac-obal" data-viac-obal hidden><button class="tlacidlo tlacidlo-obrys" type="button" data-viac '
                      'aria-controls="zoznam-realizacii">Zobraziť ďalšie <span class="viac-pocet" data-viac-pocet></span></button></p>', telo)
        # miesto: mestá z poľa miesto s počtom, kľúč [a-z0-9-] zhodný s data-mesto kariet (odkazy zo sekcie Kde)
        mesta = re.findall(r'<option value="([a-z0-9-]+)">[^<]+ \((\d+)\)</option>', telo.split('<select data-mesto>', 1)[1].split('</select>', 1)[0])
        self.assertTrue(mesta)
        for kluc, pocet in mesta:
            self.assertEqual(telo.count(f'data-mesto="{kluc}"'), int(pocet), kluc)
        self.assertIn('<p class="filtre-stav" data-stav aria-live="polite"></p>', telo)
        self.assertIn('<div class="prazdne" data-prazdne hidden>', telo)
        dlazdice = [a for t, a in r.prvky if t == 'li' and 'dlazdica' in a.get('class', '')]
        self.assertEqual(len(dlazdice), len(self.realizacie))
        self.assertEqual([int(a['data-poradie']) for a in dlazdice], list(range(1, len(dlazdice) + 1)))
        for a in dlazdice:
            self.assertNotIn('hidden', a)
            self.assertTrue(set(a['data-druh'].split()) <= KLUCE_KATEGORII, a)
            self.assertRegex(a['data-rok'], r'^(0|\d{4})$')
            self.assertRegex(a['data-mesto'], r'^[a-z0-9-]*$')
        # poradie dlaždíc = poradie od najnovších (ItemList, build.zorad_chronologicky)
        slugy = re.findall(r'<li class="dlazdica[^"]*"[^>]*><figure><a class="projekt-foto" href="\.\./realizacie/([a-z0-9-]+)/"', telo)
        zoznam = [u for u in self.info('realizacie/')['ld'] if u.get('@type') == 'ItemList'][0]['itemListElement']
        self.assertEqual(slugy, [u['url'].rstrip('/').rsplit('/', 1)[1] for u in zoznam])
        self.assertEqual(slugy, [x['slug'] for x in self.w.realizacie])
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ('[data-filtre]', '[data-zoznam]', 'data-druh', 'data-rok', 'data-poradie', 'aria-pressed', 'filtre.hidden = false',
                    '[data-mesto]', "param.get('mesto')", "'data-naraz'", "'[data-viac]'", "'[data-viac-obal]'",
                    "'[data-viac-pocet]'", "s.zoradit === 'nazov'", "Intl.Collator('sk'", "'.dlazdica-nazov a'"):
            self.assertIn(kus, js)

    def test_galeria_data_atributy_pre_lightbox(self):
        for slug, r in [(x['slug'], x) for x in self.w.realizacie if x['galeria']][:6]:
            cesta = self.ostry / 'realizacie' / slug / 'index.html'
            rz = rozober(cesta)
            indexy = [k for k, (t, a) in enumerate(rz.prvky) if t == 'a' and 'data-lightbox' in a]
            odkazy = [rz.prvky[k][1] for k in indexy]
            self.assertEqual(len(odkazy), len(r['galeria']), slug)
            for i, (k, a) in enumerate(zip(indexy, odkazy), 1):
                self.assertEqual(a['href'], a['data-plna'])
                self.assertEqual(a['data-index'], str(i))
                plna = (cesta.parent / a['data-plna']).resolve()
                with Image.open(plna) as im:
                    self.assertEqual((str(im.width), str(im.height)), (a['data-sirka'], a['data-vyska']), slug)
                # srcset lightboxu: data-srcset, pri náhľade so srcset (nie malá fotka) srcset náhľadu v odkaze
                webp, _avif, z_nahladu = lb_zdroje(rz.prvky, k)
                self.assertEqual(z_nahladu, 'mala' not in a.get('class', ''), (slug, i))
                self.assertTrue(webp, (slug, i))
                for kus in webp.split(', '):
                    u, w = kus.split(' ')
                    self.assertTrue((cesta.parent / u).resolve().is_file(), u)
                    self.assertRegex(w, r'^\d+w$')
                self.assertEqual(a['data-nazov'], self.w.nazov_s_miestom(r))
                # rola malým písmenom uprostred vety „rola BM Building: stavebný dozor“ (oprava 1)
                if r['rola']:
                    self.assertIn('rola BM Building: ' + build.male(r['rola']), a['data-udaje'])
                if r['roky']:
                    self.assertIn(r['roky'], a['data-udaje'])
                if r['projektant']:
                    self.assertIn(r['projektant'], a['data-udaje'])
            dialog = [a for t, a in rz.prvky if t == 'dialog']
            self.assertEqual(len(dialog), 1)
            self.assertEqual(dialog[0]['id'], 'lightbox')
            for tlacidlo in ('data-predosla', 'data-dalsia', 'data-zavriet'):
                self.assertEqual(sum(1 for t, a in rz.prvky if t == 'button' and tlacidlo in a), 1)
            # alt náhľadu čísluje podľa poradia galérie (to isté číslo ukáže počítadlo lightboxu)
            alty = re.findall(r'data-lightbox [^>]*>(?:<picture><source [^>]*>)?<img [^>]*alt="([^"]*)"', cesta.read_text(encoding='utf-8'))
            self.assertEqual(len(alty), len(odkazy), slug)
            for i, alt in enumerate(alty, 1):
                self.assertTrue(alt, slug)
                if len(alty) > 1:
                    self.assertTrue(alt.endswith(f', fotka {i} z {len(alty)}'), (slug, alt))
            s = cesta.read_text(encoding='utf-8')
            for trieda in ('lightbox-figura', 'lightbox-foto', 'lightbox-nazov', 'lightbox-udaje', 'lightbox-pocet'):
                self.assertIn(f'class="{trieda}"', s, slug)
        # app.js ovláda presne tento markup (prehliadačová skúška: dizajn-olovnica/nastroje/test-lightbox.mjs)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("getElementById('lightbox')", "'a[data-lightbox]'", "'[data-predosla]'", "'[data-dalsia]'",
                    "'[data-zavriet]'", "'.lightbox-foto'", "'.lightbox-figura'", "'.lightbox-nazov'", "'.lightbox-udaje'",
                    "'.lightbox-pocet'", 'showModal', "'close'", 'ArrowRight', 'ArrowLeft', "'Home'", "'End'",
                    "'pointerup'", 'odkazy[akt].focus()', "'data-plna'", "'data-srcset'", "'data-udaje'", "'data-popis'"):
            self.assertIn(kus, js)
        self.assertIn('.lb-otvorene{overflow:hidden}', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'))

    def test_rozmazane_nikdy_v_galerii_ani_na_obale(self):
        rozmazane = [(r, f) for r in self.w.realizacie for f in r['fotky'] if f['rozmazana']]
        self.assertTrue(rozmazane)  # skutočný obsah ich má (FOTKY.md: ambulancie, Ventúrska 01)
        texty = {p: p.read_text(encoding='utf-8') for p in stranky(self.ostry)}
        for r, f in rozmazane:
            predpona = f'obrazky/{r["slug"]}/{f["nn"]}-'
            for p, s in texty.items():
                self.assertNotIn(predpona, s, (p, f['rel']))
            self.assertIsNot(r['obal'], f)
        for r in self.w.realizacie:
            if r['obal']:
                o = r['obal']
                self.assertTrue(o['w'] >= o['h'] and o['w'] >= 640 and o['ostrost'] >= 350, (r['slug'], o['rel']))
                self.assertNotIn(o['typ'], ('D', 'P'))

    def test_povodny_stav_nie_je_obal(self):
        ok = [r for r in self.w.realizacie if r['slug'] == 'rekonstrukcia-okanikova-bratislava']
        if not ok:
            self.skipTest('Okánikova nie je v obsahu')
        r = ok[0]
        if r['obal']:
            self.assertNotEqual(r['obal']['typ'], 'P')
            self.assertNotIn('pôvodný stav', r['obal']['alt'].lower())
        # fotka karty (aj bez obalu) nikdy nie je pôvodný stav, kým je iná skutočná fotka
        self.assertIsNotNone(r['karta'])
        self.assertFalse(self.w.povodny_stav(r['karta']))

    def test_pred_po_nie_je(self):
        # zadanie bmb-olovnica 21. 11.: pred a po len pri vysokej istote; jediný pár (Rosnička 03/01) má strednú,
        # ostáva len ako návrh, ktorý build nečíta
        d = json.loads((KOREN / 'sablona' / 'fotky-pravidla.json').read_text(encoding='utf-8'))
        self.assertTrue(all(str(p.get('istota')).lower() == 'vysoká' for p in d['pary_pred_po']), d['pary_pred_po'])
        self.assertEqual({p['slug'] for p in d.get('navrhy_pred_po', [])}, {'bytovy-dom-rosnicka-bratislava'})
        if not d['pary_pred_po']:
            for p in stranky(self.ostry):
                s = p.read_text(encoding='utf-8')
                self.assertNotIn('data-pred-po', s, p)
                self.assertNotIn('pred-po-sekcia', s, p)

    def test_stranka_realizacie_udaje_a_susedia(self):
        slugy = [x['slug'] for x in self.w.realizacie]
        for i, slug in enumerate(slugy):
            t = self.info(f'realizacie/{slug}/')
            r = self.w.realizacie[i]
            self.assertEqual(t['h1'], self.w.nazov_s_miestom(r))
            dalsia = re.search(r'<a class="dalsia" href="\.\./([a-z0-9-]+)/"', t['telo'])
            predch = re.search(r'<a class="predch" href="\.\./([a-z0-9-]+)/"', t['telo'])
            self.assertEqual(dalsia.group(1) if dalsia else None, slugy[i + 1] if i + 1 < len(slugy) else None, slug)
            self.assertEqual(predch.group(1) if predch else None, slugy[i - 1] if i else None, slug)
            if r['rola']:
                self.assertIn('<dt>Rola BM Building</dt>', t['telo'])
            if not r['projektant']:
                self.assertNotIn('<dt>Projektant</dt>', t['telo'])
            dielo = [u for u in t['ld'] if u.get('@type') == 'CreativeWork'][0]
            self.assertNotIn('creator', dielo)
            self.assertEqual('contributor' in dielo, bool(r['rola']), slug)
            if r['galeria']:
                self.assertTrue(all('/obrazky/' in x for x in dielo['image']))

    def test_titulky_realizacii(self):
        titulky = {}
        for slug in self.realizacie:
            t = self.info(f'realizacie/{slug}/')['title']
            titulky[slug] = t
            self.assertLessEqual(len(t), 60, t)
        self.assertEqual(len(set(titulky.values())), len(titulky))

    def test_description_bez_dvojitej_bodky(self):
        for p in stranky(self.ostry):
            self.assertNotIn('..', stranka_info(p)['desc'], p)

    def test_ochrana_udajov_uplna(self):
        t = self.info('ochrana-udajov/')['telo'].replace(' ', ' ')
        for kus in ('čl. 6 ods. 1 písm. b)', 'čl. 6 ods. 1 písm. f)', 'uchovávame', 'USA', 'GitHub, Inc.',
                    'https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement',
                    'fonts/OFL-Geist.txt', 'nepoužíva cookies'):
            self.assertIn(kus, t)

    def test_lastmod_a_datum_publikovania(self):
        vydanie = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))['vydanie']
        ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        for u in ET.parse(self.ostry / 'sitemap.xml').getroot().findall(f'{ns}url'):
            self.assertGreaterEqual(u.find(f'{ns}lastmod').text, vydanie, u.find(f'{ns}loc').text)
        for slug, d in self.realizacie.items():
            dielo = [u for u in self.info(f'realizacie/{slug}/')['ld'] if u.get('@type') == 'CreativeWork'][0]
            if d.get('zdroj') == 'web' or not d.get('datum'):
                self.assertNotIn('datePublished', dielo, slug)
            elif build.platny_datum(d.get('datum')):
                self.assertEqual(dielo.get('datePublished'), d['datum'], slug)

    def test_jsonld_graf(self):
        for p in stranky(self.ostry):
            if p.name == '404.html' and p.parent == self.ostry:
                continue
            uzly = stranka_info(p)['ld']
            stranky_ld = [u for u in uzly if 'WebPage' in (u['@type'] if isinstance(u['@type'], list) else [u['@type']])
                          or u['@type'] in ('ContactPage', 'CollectionPage')]
            self.assertEqual(len(stranky_ld), 1, p)
            for u in uzly:
                if u.get('isPartOf'):
                    self.assertEqual(u['isPartOf'], {'@id': 'https://bmbuilding.sk/#web'}, p)

    def test_kategorie_pravidla(self):
        # 1. kolo pripomienok (ZMENY-KOLO-1.md 5.1): druhy Bytové domy, Rodinné domy, Iné; rekonštrukcie nie sú druh
        self.assertEqual(build.KATEGORIE, (('bytove', 'Bytové domy'), ('rodinne', 'Rodinné domy'), ('ine', 'Iné')))
        k = build.kategorie_realizacie
        self.assertEqual(k('RD Záhorie', ''), ['rodinne'])
        self.assertEqual(k('Rekonštrukcia rodinného domu', ''), ['rodinne'])
        self.assertEqual(k('Polyfunkčný dom', 'Stavebné úpravy'), ['ine'])
        self.assertEqual(k('Sklad', 'Hala pre logistiku'), ['ine'])
        self.assertEqual(k('Objekt', 'Nadstavba administratívnej budovy'), ['ine'])
        self.assertEqual(k('Rekonštrukcia bytového domu', ''), ['bytove'])
        # pole kategoria má prednosť; stará hodnota rekonstrukcie sa nepoužije (odvodí sa bez rekonštrukcií)
        self.assertEqual(k('Sklad', '', 'Rodinné domy'), ['rodinne'])
        self.assertEqual(k('Sklad', '', ['bytove', 'ine', 'nezmysel']), ['bytove', 'ine'])
        self.assertEqual(k('RD Jarovce', '', 'nezmysel'), ['rodinne'])
        self.assertEqual(k('Rekonštrukcia Okánikova', '', 'rekonstrukcie'), ['ine'])
        self.assertEqual(k('Rekonštrukcia Okánikova', '', 'bytove'), ['bytove'])

    def test_zoradenie_podla_rokov(self):
        r = lambda slug, roky, datum, poradie=5: {'slug': slug, 'roky': roky, 'datum': datum, 'poradie': poradie,
                                                  **dict(zip(('rok', 'rok_od', 'rok_z_datumu'), build.roky_realizacie(roky, datum)))}
        z = build.zorad_chronologicky([r('a', '2010 až 2012', '2025-01-01'), r('b', '2011 až 2012', '2013-01-09'),
                                       r('c', '', '2019-05-05'), r('d', '', ''), r('e', '2012', '2001-01-01', 1)])
        self.assertEqual([x['slug'] for x in z], ['c', 'e', 'b', 'a', 'd'])
        # realizácia bez rokov stavby: prázdna kóta, rok príspevku ani slovo Facebook nie (Mário 24. 11. 2026, overenie-1)
        for x in self.w.realizacie:
            if not x['roky'] and x['zdroj'] == 'fb' and x['datum']:
                karta = self.info('realizacie/')['telo'].split(f'realizacie/{x["slug"]}/"', 1)[1].split('</li>', 1)[0]
                self.assertNotIn('Facebook', karta)
                self.assertIn('kota-prazdna', karta, x['slug'])

    def test_404(self):
        s = (self.ostry / '404.html').read_text(encoding='utf-8')
        self.assertNotIn('<base', s)
        self.assertIn('<a class="preskocit" href="#obsah">', s)
        self.assertIn("base-uri 'none'", s)
        telo = s.split('<main', 1)[1].split('</main>', 1)[0]
        # akcie úvodu ukážky (Stavba 2): plné Zavolať a odkazy so šípkou s absolútnymi cestami od koreňa
        akcie = telo.split('<div class="uvod-akcie akcie-vlavo">', 1)[1].split('</div>', 1)[0]
        self.assertRegex(akcie, r'^<a class="tlacidlo" href="tel:\+421903794738">Zavolať <svg')
        for ciel, text in (('/', 'Na úvod'), ('/realizacie/', 'Realizácie'), ('/kontakt/', 'Kontakt')):
            self.assertIn(f'<a class="odkaz-sipka" href="{ciel}">{text} <svg', akcie)
        css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        self.assertIn('.chyba .uvod-akcie .odkaz-sipka{display:inline-flex}', css)  # na mobile úvod odkazy skrýva
        self.assertIn('url(/fonts/geist-latin.woff2)', s)
        self.assertIn('<h1 class="nadpis-stranky" id="nadpis">Stránka sa nenašla</h1>', telo)

    def test_csp_a_skripty(self):
        s = self.info('')['html']
        csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', s).group(1)
        skript = [c.strip() for c in csp.split(';') if c.strip().startswith('script-src')][0]
        self.assertTrue(skript.startswith("script-src 'self' 'sha256-"), skript)
        self.assertEqual(len(re.findall(r'<script src=', s)), 1)
        self.assertRegex(s, r'<script src="app\.js\?v=[0-9a-f]{10}" defer></script>')
        self.assertEqual((self.ostry / 'app.js').read_bytes(), (KOREN / 'sablona' / 'app.js').read_bytes())
        js = (self.ostry / 'app.js').read_text(encoding='utf-8')
        for zakazane in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function',
                         'setTimeout("', "setTimeout('", 'fetch(', 'XMLHttpRequest', 'localStorage', 'cookie'):
            self.assertNotIn(zakazane, js)

    def test_znaky_pisma(self):
        # skutočný obsah má len znaky písma Geist (inak by build varoval, test_beh_prejde)
        self.assertNotIn('písmo Geist nemá znaky', skutocny()['o'][2])

    # --- oprava 1 (ops/weby-na-mieru/klienti/bmbuilding/dokoncenie/oprava-1.md): každý test bez opravy zlyhá
    def test_pata_bez_dvojitej_bodky(self):
        # meno „BM Building, s.r.o.“ končí bodkou: v © päty nesmie byť „s.r.o..“ (bolo na 47 stránkach vrátane 404)
        self.assertEqual(build.s_bodkou('BM Building, s.r.o.'), 'BM Building, s.r.o.')
        for p in stranky(self.ostry):
            pata = p.read_text(encoding='utf-8').split('<footer', 1)[1]
            text = html.unescape(re.sub(r'<[^>]+>', ' ', pata))
            self.assertIn('©', text, p)
            self.assertNotIn('..', text, p)

    def test_hlavicka_nad_obsahom(self):
        # otvorené mobilné menu (.menu-panel v hlavičke) musí ležať nad main: z-index pripnutej hlavičky (sticky,
        # 1. kolo pripomienok 3.1) väčší ako main a päta
        zi = {}
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for sel, telo in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
            m = re.search(r'z-index:(\d+)', telo)
            if m and re.search(r'position:(relative|sticky)', telo):
                for s in sel.split(','):
                    zi.setdefault(s.strip(), int(m.group(1)))
        self.assertIn('.hlava{position:sticky;top:0;', self.css)
        self.assertGreater(zi['.hlava'], zi['main'])
        self.assertGreater(zi['.hlava'], zi['.pata'])
    def test_uvod_vyska_okna_a_veta(self):
        # --hore je výška pripnutej hlavičky: 64 px, od 1024 px 84 px, v nižších oknách 64 px; slider hrdinu siaha na
        # počítači od hlavičky po spodok okna bez 48 px (ZMENY-KOLO-1.md 3A.2)
        root = re.search(r':root\{([^}]*)\}', re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)).group(1)
        self.assertIn('--hore:64px', root)
        self.assertIn(':root{--stlpce:12;--hore:84px}', self.css)
        self.assertIn(':root{--hore:64px}', self.css)
        self.assertIn('calc(100svh - var(--hore) - var(--m-48))', self.css)
        for stare in ('--veta-f', '.uvod-veta', '--r1'):
            self.assertNotIn(stare, self.css)
    def test_presmerovania_ikona_a_csp(self):
        # Firefox pýtal /favicon.ico mimo CSP presmerovacej stránky: prázdna ikona data:, povolená v img-src,
        # žiadna sieťová požiadavka (skutočnú ikonu by okamžité presmerovanie zrušilo)
        stuby = sorted((self.ostry / 'index.php').rglob('index.html'))
        self.assertGreater(len(stuby), 5)
        for p in stuby:
            s = p.read_text(encoding='utf-8')
            csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', s).group(1)
            self.assertIn('img-src data:', csp, p)
            self.assertIn("default-src 'none'", csp, p)
            self.assertIn('<link rel="icon" href="data:,">', s, p)

    def test_bez_skrytych_zoznamov_pasu(self):
        # skryté zoznamy fotiek pásu (lb-zoznam) boli len v páse úvodu; po ÚVOD v3 (3A.6) nie sú na žiadnej stránke
        for subor in stranky(self.ostry):
            self.assertNotIn('class="lb-zoznam"', subor.read_text(encoding='utf-8'), subor)

    def test_prva_karta_prehladu_bez_lazy(self):
        # LCP prehľadu realizácií je fotka prvej karty: fetchpriority=high a nie loading=lazy; ostatné lenivé
        telo = self.info('realizacie/')['telo']
        karty = re.findall(r'<li class="dlazdica.*?</li>', telo, re.S)
        self.assertEqual(len(karty), len(self.w.realizacie))
        obr = [re.search(r'<img [^>]*>', k).group(0) for k in karty]
        self.assertIn('fetchpriority="high"', obr[0])
        self.assertNotIn('loading="lazy"', obr[0])
        for o in obr[1:]:
            self.assertIn('loading="lazy"', o)
            self.assertNotIn('fetchpriority', o)

    def test_kontakt_vizitka_hned_za_tlacidlami(self):
        # na mobile telefón a e-mail (vizitka) hneď pod Zavolať a Napísať e-mail, fakturačné údaje až pod nimi;
        # na počítači vizitka vpravo pripnutá cez oba riadky (nie 360 px prázdnej plochy pod ňou)
        for cesta in ('', 'kontakt/'):
            sek = self.info(cesta)['telo'].split('id="kontakt"', 1)[1]
            self.assertLess(sek.index('class="kontakt-akcie"'), sek.index('<address class="vizitka'), cesta)
            self.assertLess(sek.index('<address class="vizitka'), sek.index('class="fakty kontakt-fakty"'), cesta)
        self.assertIn('.kontakt-mapa{grid-column:5/-1;grid-row:1/span 2;align-self:start;position:sticky;top:24px}', self.css)

    def test_udaje_lightboxu(self):
        # rola malým písmenom uprostred vety; projektant sa pri vizualizácii vynechá, ak je ním autor (app.js)
        r = {'rola': 'Stavebný dozor', 'projektant': 'Ing. arch. Ilja Skoček', 'roky': '2019 až 2020'}
        self.assertEqual(build.Web.udaje_lightboxu(r),
                         'rola BM Building: stavebný dozor · projektant Ing. arch. Ilja Skoček · 2019 až 2020')
        self.assertEqual(build.Web.udaje_lightboxu({'rola': 'TDI', 'projektant': '', 'roky': ''}), 'rola BM Building: TDI')
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn(f".split('{build.LB_ODDELOVAC}')", js)
        self.assertIn("x === 'projektant ' + autor", js)
        for u in re.findall(r'data-udaje="([^"]*)"', self.info('')['html']):
            self.assertTrue(html.unescape(u).startswith('rola BM Building: ') or not u, u)

    def test_kota_v_stlpci(self):
        # šikmé konce kóty siahajú 5 px za okraj: karta v mriežke (obmedzený pohyb) aj kóta detailu nepresahujú stĺpec
        self.assertIn('.kota{position:relative;display:flex;justify-content:center;margin:16px 5px 0;', self.css)
        self.assertIn('.detail-text .kota{margin:0 6px 4px}', self.css)
        obmedzeny = self.css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('.projekt{flex:none;min-width:0}', obmedzeny)

    def test_texty_podla_zdroja(self):
        # § 36 ods. 1: „je oprávnená najmä a) sledovať…“; manažment: výber dodávateľa starý web neuvádza ako činnosť;
        # počet realizácií manažmentu je s popisom, že zlučuje projektový a stavebný manažment
        q = self.texty['otazky']['co-robi-dozor']['odpoved']
        self.assertIn('je podľa stavebného zákona oprávnená najmä sledovať', q)
        self.assertNotIn('najmä sleduje', q)
        # naše prepísané texty služieb (priklady, odseky, veta) sú od 1. kola preč, text je doslovne starý web
        m = self.texty['sluzby']['stavebny-manazment']
        self.assertNotIn('priklady', m)
        self.assertNotIn('výber dodávateľa', json.dumps(self.texty['cinnost_doslova'], ensure_ascii=False))

    # --- Novinky preč, časopis ASB (ZMENY-KOLO-1.md 5.7, Andrej 24. 11. 2026 večer) -------------------------------
    def test_casopis_asb_pri_lumiere(self):
        """Blok „Časopis ASB č. 244: stavebný dozor BM Building“ na stránke Lumière: veľký výrez tabuľky údajov o stavbe
        zo strany 77 (Máriova fotka, bez riadku Rozpočet) s popisom zdroja, vlastný text, odkazy len https s rel noopener
        (online článok na asb.sk bez tvrdenia, že tam je BM Building, a LinkedIn) a dve fotky dvojstrán s lightboxom."""
        lum = 'filmovy-klub-lumiere-spitalska-bratislava'
        r = next(x for x in self.w.realizacie if x['slug'] == lum)
        self.assertEqual(r['roky'], '2022 až 2024')
        self.assertEqual(r['investor'], 'Slovenský filmový ústav')
        self.assertIn('DARK BLUE', r['projektant'].upper())
        self.assertIn('NTJ', r['zhotovitel'])
        info = self.info(f'realizacie/{lum}/')
        telo = info['telo'].replace('\u00a0', ' ')
        self.assertIn('<section class="sekcia media-sekcia" id="casopis-asb" aria-labelledby="casopis-asb-nadpis">', telo)
        blok = telo.split('id="casopis-asb"', 1)[1].split('</section>', 1)[0]
        # nadpis hovorí to podstatné (Andrej: „stavebný dozor BM Building, nie len Mário Bulko“)
        self.assertIn('<h2 class="nadpis" id="casopis-asb-nadpis">Časopis ASB č. 244: stavebný dozor BM Building</h2>', blok)
        self.assertIn('Článok v časopise ASB', blok)
        self.assertIn('Kino Lumière: od požiaru k veľkému návratu', blok)
        # blok je pred galériou, za textom realizácie
        self.assertLess(telo.index('id="casopis-asb"'), telo.index('id="galeria"'))
        self.assertGreater(telo.index('id="casopis-asb"'), telo.index('class="obsah mriezka detail'))
        # výrez tabuľky: veľký obrázok (7 z 12 stĺpcov, nie miniatúra) s popisom zdroja, z sablona/media
        tab = blok.split('<figure class="media-tabulka">', 1)[1].split('</figure>', 1)[0]
        self.assertIn('<figcaption>Zdroj: ASB č. 244, s. 77</figcaption>', tab)
        self.assertIn(f'sizes="{build.SIZES_MEDIA_TABULKA}"', tab)
        self.assertRegex(tab, r'src="\.\./\.\./obrazky/media/casopis-asb/01-\d+\.webp"')
        self.assertIn('type="image/avif"', tab)
        self.assertRegex(tab, r'alt="Tabuľka údajov o stavbe z časopisu ASB č\. 244[^"]*stavebný dozor BM Building')
        zdroj = KOREN / 'sablona' / 'media' / 'asb-244-s77-tabulka.jpg'
        with Image.open(zdroj) as im:
            # zväčšený Upscaylom a narovnaný: ostrý aj pri hustote 2 v stĺpci 760 CSS px
            self.assertGreaterEqual(im.width, 1500)
            self.assertLess(im.height, im.width * 1.0)
        # rozpočet nikde na stránke (ani v texte, alt, JSON-LD)
        cela = info['html'].replace('\u00a0', ' ')
        for zle in ('Rozpočet', 'rozpočet', 'ROZPOČET', '2,44', '2 438', 'mil. €'):
            self.assertNotIn(zle, cela, zle)
        # fotky strán: malá galéria s lightboxom, popis autora fotiek
        strany = re.findall(r'<li class="media-strana">(.*?)</li>', blok, re.S)
        self.assertEqual(len(strany), 2)
        self.assertIn('Foto strán: Mário Bulko, časopis ASB č. 244, február 2024', blok)
        for i, li in enumerate(strany, 2):
            self.assertRegex(li, rf'src="\.\./\.\./obrazky/media/casopis-asb/0{i}-\d+\.webp"')
        odkazy = re.findall(r'<a class="media-odkaz" href="([^"]+)" data-lightbox ([^>]*)>', blok)
        self.assertEqual(len(odkazy), 3)
        for href, atr in odkazy:
            self.assertTrue(href.startswith('../../obrazky/media/casopis-asb/'), href)
            self.assertIn('data-galeria="media-casopis-asb"', atr)
            self.assertIn('data-nazov="Časopis ASB č. 244, február 2024"', atr)
            self.assertTrue((self.ostry / 'realizacie' / lum / href).resolve().is_file(), href)
        self.assertEqual(telo.count('<dialog class="lightbox" id="lightbox"'), 1)
        # odkazy von: len https na asb.sk a linkedin.com, nová karta s rel noopener; snímky z webu asb.sk nie
        von = re.findall(r'<a class="odkaz-von" href="([^"]+)" target="_blank" rel="noopener">([^<]+)', blok)
        self.assertEqual([t.strip() for _, t in von], ['Článok o rekonštrukcii na asb.sk', 'Fotky strán na LinkedIne (Mário Bulko)'])
        for href, _ in von:
            u = urllib.parse.urlsplit(html.unescape(href))
            self.assertEqual(u.scheme, 'https')
            self.assertTrue(any(u.hostname == h or u.hostname.endswith('.' + h) for h in build.MEDIA_HOSTITELIA), href)
        self.assertEqual(len(re.findall(r'href="(?:https?:)?//', blok)), 2)
        self.assertNotIn('asb-kino-lumiere', cela)
        self.assertNotRegex(blok, r'<img [^>]*src="https?:')
        # vlastný text netvrdí, že online článok menuje BM Building; tvrdenie je pri tlačenej tabuľke
        self.assertIn('V tabuľke údajov o stavbe na strane 77 je BM Building uvedený ako stavebný dozor', blok)
        # JSON-LD: tlačený článok ako subjectOf realizácie
        dielo = next(u for u in info['ld'] if u.get('@type') == 'CreativeWork')
        clanok = dielo['subjectOf'][0]
        self.assertEqual(clanok['headline'], 'Kino Lumière: od požiaru k veľkému návratu')
        self.assertEqual(clanok['isPartOf']['issueNumber'], '244')
        self.assertEqual(clanok['isPartOf']['isPartOf']['name'], 'ASB')
        self.assertEqual((clanok['pageStart'], clanok['pageEnd']), ('74', '77'))

    def test_v_mediach_devinske_luky_a_inde_nic(self):
        """Devínske lúky: nenápadný blok V médiách s odkazom na Archinfo.sk a doslovnou vetou; iné realizácie blok nemajú."""
        dev = 'bytovy-dom-devinske-luky-bratislava'
        telo = self.info(f'realizacie/{dev}/')['telo'].replace('\u00a0', ' ')
        blok = telo.split('id="v-mediach"', 1)[1].split('</section>', 1)[0]
        self.assertIn('media-male', telo.split('id="v-mediach"', 1)[0].rsplit('<section', 1)[1])
        self.assertIn('<a class="odkaz-von" href="https://www.archinfo.sk/diela/bytovy-dom/bytovy-dom-devinske-luky.html" '
                      'target="_blank" rel="noopener">Bytový dom Devínske lúky na Archinfo.sk', blok)
        self.assertIn('„Hlavný manažér stavby: Ing. Mário Bulko / BM Building“', blok)
        self.assertIn('BM Building“', self.info(f'realizacie/{dev}/')['telo'])
        self.assertNotIn('<img', blok)
        for r in self.w.realizacie:
            if r['slug'] in (dev, 'filmovy-klub-lumiere-spitalska-bratislava'):
                continue
            self.assertNotIn('media-sekcia', self.info(f'realizacie/{r["slug"]}/')['telo'], r['slug'])

    def test_v_mediach_v_hrdinovi(self):
        """Hrdina úvodu: posledný údaj V médiách s ikonou cez celý riadok, text so „stavebný dozor BM Building“ a odkaz
        na kotvu bloku ASB na stránke Lumière (kotva na tej stránke existuje); ostatné údaje v poradí ako v úvode v3."""
        telo = self.info('')['telo'].replace('\u00a0', ' ')
        udaje = telo.split('<div class="firma-udaje">', 1)[1].split('<div class="uvod-akcie"', 1)[0]
        fakty = udaje.split('<dl class="firma-fakty">', 1)[1].split('</dl>', 1)[0]
        self.assertEqual(re.findall(r'<dt><svg[^>]*>.*?</svg><span>([^<]+)</span></dt>', fakty),
                         ['Sídlo', 'Na trhu', 'Konateľ', 'Oprávnenia'])
        # oprava-2: V médiách vo vlastnom rade až za zdrojom z ORSR (zdroj patrí len k údajom z registra), krátky text
        self.assertLess(udaje.index('class="firma-zdroj"'), udaje.index('<dl class="firma-fakty firma-media">'))
        fakty = udaje.split('<dl class="firma-fakty firma-media">', 1)[1].split('</dl>', 1)[0]
        self.assertEqual(re.findall(r'<dt><svg[^>]*>.*?</svg><span>([^<]+)</span></dt>', fakty), ['V médiách'])
        media = fakty.split('<div class="udaj udaj-siroky udaj-media">', 1)[1]
        a = re.search(r'<dd><a href="([^"]+)">([^<]+)</a></dd>', media)
        self.assertEqual(a.group(1), 'realizacie/filmovy-klub-lumiere-spitalska-bratislava/#casopis-asb')
        self.assertEqual(html.unescape(a.group(2)), 'Časopis ASB č. 244 (2024), kino Lumière')
        # štítok nad hodnotou ako ostatné údaje: osobitné rozloženie riadka V médiách nie je
        self.assertNotIn('.udaj-media{display:grid', self.css)
        self.assertNotIn('.udaj-media dd{margin:0}', self.css)
        ciel = (self.ostry / 'realizacie' / 'filmovy-klub-lumiere-spitalska-bratislava' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="casopis-asb"', ciel)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.firma-fakty .udaj-media{grid-column:1/-1}', css)
        self.assertIn(build.IKONY_UDAJOV['V médiách'], fakty)
        # na úvode nikde odkaz na Novinky
        self.assertNotIn('novinky/', self.info('')['html'])

    def test_description_noviniek_nie_kratka(self):
        # krátke príspevky (34 a 47 znakov) dostanú do description dátum a autora príspevku, nič iné (zostava s Novinkami)
        self.assertTrue(self.w_n.novinky)
        for n in self.w_n.novinky:
            desc = self.info_n(f'novinky/{n["slug"]}/')['desc']
            self.assertGreaterEqual(len(desc), 70, (n['slug'], desc))
            self.assertNotIn('..', desc)
        d = self.info_n('novinky/vianocny-pozdrav-2025-12/')['desc']
        self.assertEqual(d, 'Veselé Vianoce a šťastný nový rok. Príspevok BM Building z 24. 12. 2025.')

    def test_uvod_vizualne_ako_ukazka(self):
        # rám fotky úvodu bez sivého pozadia (počas kreslenia výkresu papier ako v ukážke), detail si pozadie nechá;
        # lišta fáz pod schémou len na mobile; sekcie pod pásom s content-visibility (TBT úvodu)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        ramec = re.search(r'\n\.pohlad-ramec\{([^}]*)\}', css).group(1)
        self.assertNotIn('background', ramec)
        self.assertIn('.detail .pohlad-ramec{aspect-ratio:3/2;height:auto;background:var(--vapno-2)}', css)
        self.assertRegex(css, r'\.stranka-uvod \.kontakt\{content-visibility:auto;contain-intrinsic-size:auto \d+px\}')
        self.assertIn('.projekt-riadok{display:flex;align-items:flex-start;', css)

    # --- oprava po bráne (pokus 1, 21. 11. 2026) ---------------------------------------------------------------
    def test_vizualizacie_ufo_v_galerii_nezvacsene(self):
        # bod 1: malé vizualizácie UFO (05.jpg a 06.jpg, 320 x 179 px) sú v galérii novinky so štítkom, nie vynechané
        # ako rozmazané; v galérii nikdy zväčšené nad 1,25 násobok súboru (malá: bez srcset, object-fit:scale-down)
        n = next(x for x in self.w_n.novinky if x['slug'] == 'odovzdanie-staveniska-ufo-2025-03')
        self.assertEqual([f['rel'].rsplit('/', 1)[1] for f in n['galeria'] if f.get('viz')], ['05.jpg', '06.jpg'])
        s = (self.ostry_n / 'novinky' / n['slug'] / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('data-viz="1"'), 2)
        self.assertEqual(s.count('<span class="stitok-viz">Vizualizácia</span>'), 2)
        for nn in ('05', '06'):
            a = re.search(rf'<a class="galeria-odkaz mala" href="(\.\./\.\./obrazky-novinky/{n["slug"]}/{nn}-320\.webp)"[^>]*>'
                          r'(<img [^>]*>)', s)
            self.assertIsNotNone(a, nn)
            self.assertNotIn('srcset', a.group(2))
            self.assertIn('width="320" height="179"', a.group(2))
            self.assertRegex(a.group(2), r'alt="Vizualizácia')
            self.assertTrue((self.ostry_n / 'novinky' / n['slug'] / a.group(1)).resolve().is_file())
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.galeria-odkaz.mala img{object-fit:scale-down}', css)
        # obal, karta a hrdina nikdy z vizualizácie
        self.assertFalse(n['karta'].get('viz'))

    def test_galeria_nikdy_nezvacsi_nad_125(self):
        # bod 1: na každej stránke s galériou: fotka, ktorú by cover v najväčšej bunke zväčšil nad 1,25 násobok, je
        # „malá“ (bez srcset); ostatné bunku vyplnia najviac s 1,25 násobkom (rozmery plného súboru v data-sirka)
        malych = 0
        for p in [self.ostry / 'realizacie' / r['slug'] / 'index.html' for r in self.w.realizacie] + \
                 [self.ostry_n / 'novinky' / n['slug'] / 'index.html' for n in self.w_n.novinky]:
            s = p.read_text(encoding='utf-8')
            if 'class="galeria' not in s:
                continue
            ul = re.search(r'<ul class="galeria( galeria-malo)?" data-galeria>(.*?)</ul>', s, re.S)
            malo = bool(ul.group(1))
            for li in re.findall(r'<li class="galeria-polozka( prva)?">(.*?)</li>', ul.group(2), re.S):
                prva, obsah = li
                w = int(re.search(r'data-sirka="(\d+)"', obsah).group(1))
                h = int(re.search(r'data-vyska="(\d+)"', obsah).group(1))
                bunka = build.GALERIA_BUNKY['prva' if prva else ('malo' if malo else 'mriezka')]
                vyplni = w * 1.25 >= bunka[0] and h * 1.25 >= bunka[1]
                mala = 'class="galeria-odkaz mala"' in obsah
                self.assertEqual(mala, not vyplni, (p, w, h))
                img = re.search(r'<img [^>]*>', obsah).group(0)
                self.assertEqual('srcset=' in img, not mala, (p, img))
                malych += mala
        self.assertGreaterEqual(malych, 2)

    def test_ziadna_karta_na_webe_bez_fotky(self):
        # bod 2 (zadanie, Obsah 2): žiadna karta na celom webe nemá znak (logo) namiesto fotky; novinky bez vlastnej
        # fotky majú ilustračnú fotku stavby zo sablona/fotky-pravidla.json s poctivým popisom v alte a na karte
        karty = 0
        for p in stranky(self.ostry_n):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('class="karta-znak"', s, p)
            self.assertNotIn(' karta-bez-fotky"', s, p)
            for trieda, karta in re.findall(r'<li class="((?:dlazdica|novinka-karta|projekt)[^"]*)"[^>]*>(.*?)</li>', s, re.S):
                if 'projekt-dalsi' in trieda:
                    continue
                karty += 1
                self.assertRegex(karta, r'<img [^>]*(?:src|data-src)="[^"]*\.webp"', (p, trieda))
        # aspoň zoznam realizácií, zoznam noviniek a pás úvodu
        self.assertGreaterEqual(karty, len(self.w_n.realizacie) + len(self.w_n.novinky) + 8)
        for p in stranky(self.ostry):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('class="karta-znak"', s, p)
            self.assertNotIn(' karta-bez-fotky"', s, p)
        pravidla = json.loads((KOREN / 'sablona' / 'fotky-pravidla.json').read_text(encoding='utf-8'))
        zoznam = self.info_n('novinky/')['telo']
        bez = [n for n in self.w_n.novinky if not n['galeria']]
        self.assertEqual(sorted(n['slug'] for n in bez), sorted(pravidla['ilustracne_fotky']))
        for n in bez:
            volba = pravidla['ilustracne_fotky'][n['slug']]
            self.assertTrue(volba.get('dovod'), n['slug'])
            r = next(x for x in self.w_n.realizacie if x['slug'] == volba['slug'])
            f = n['ilustracna_foto']
            self.assertEqual(f['rel'], f'{r["slug"]}/{volba["subor"]}')
            self.assertFalse(f.get('viz'))
            self.assertGreaterEqual(f['ostrost'], self.w_n.pravidla['obal']['min_ostrost'])
            # iná fotka než karta realizácie (v prehľade sa neopakuje rovnaký záber)
            self.assertIsNot(f, r['karta'], n['slug'])
            popis = f'Ilustračná fotka: {self.w_n.nazov_s_miestom(r)}'
            karta = zoznam.split(f'href="../novinky/{n["slug"]}/"', 1)[1].split('</li>', 1)[0]
            self.assertIn(f'alt="{html.escape(popis)}"', karta.replace(' ', ' '))
            self.assertIn(f'src="../obrazky/{r["slug"]}/{f["nn"]}-', karta)
            self.assertIn('<span class="stitok-viz stitok-ilustr">Ilustračná fotka</span>', karta)
            self.assertIn(f'<p class="projekt-ilustr">{html.escape(popis, quote=False)}</p>', karta.replace(' ', ' '))
            # stránka novinky ilustračnú fotku neukazuje ako fotku novinky
            detail = (self.ostry_n / 'novinky' / n['slug'] / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('Ilustračná', detail)

    def test_omrvinky_oddelovac_s_predchadzajucou(self):
        # bod 4: oddeľovač „/“ je ::after predchádzajúcej položky s nowrap, nie ::before nasledujúcej (pri zalomení
        # na 390 px by začínal riadok)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertNotIn('.omrvinky li+li::before', css)
        self.assertIn('.omrvinky li:not(:last-child)::after{content:"/"', css)
        self.assertIn('.omrvinky li:not(:last-child){white-space:nowrap}', css)
        s = self.info('realizacie/rekonstrukcia-okanikova-bratislava/')['telo']
        nav = re.search(r'<nav class="omrvinky" aria-label="Omrvinky"><ol>(.*?)</ol></nav>', s).group(1)
        self.assertNotIn('/', re.sub(r'<[^>]+>', '', nav))


# ---------------------------------------------------------------------------------------------------------------
class TestDocasnyObsah(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        self.obsah = maly_obsah(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def uprav(self, meno, **polia):
        p = self.obsah / 'realizacie' / f'{meno}.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d.update(polia)
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')

    def test_chybajuce_polia_zle_slugy_a_xss(self):
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        realizacie = sorted(p.name for p in (web / 'realizacie').iterdir() if p.is_dir())
        self.assertEqual(realizacie, ['hola', 'plna', 'xss'])
        for kus in ('Zlý Slug', '../von', 'duplicitný', '99.jpg'):
            self.assertIn(kus, err)
        self.assertNotIn('nezverejnen', (web / 'realizacie' / 'index.html').read_text(encoding='utf-8').lower())
        s = (web / 'realizacie' / 'hola' / 'index.html').read_text(encoding='utf-8')
        telo = s.split('<main', 1)[1].split('<footer', 1)[0]
        self.assertNotIn('Projektant', telo)
        self.assertNotIn('data-lightbox', telo)
        # plná: pomlčka medzi rokmi dá „až“, výkričník bodku, 2 fotky (chýbajúca a mimo priečinka vynechané)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('2019 až 2021', s)
        self.assertEqual(s.count('data-lightbox'), 2)
        self.assertTrue((web / 'obrazky' / 'plna' / '02-500.webp').is_file())
        self.assertFalse((web / 'obrazky' / 'plna' / '02-960.webp').exists())
        for subor in sorted(web.rglob('*.html')):
            t = subor.read_text(encoding='utf-8')
            self.assertNotIn('<script>alert', t, subor)
            self.assertNotIn('<img src=x', t, subor)
            self.assertNotIn('javascript:', t, subor)
            for ld in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S):
                self.assertNotIn('<', ld)
                json.loads(ld)
            r = rozober(subor)
            self.assertEqual(r.chyby, [], subor)
            for tag, a in r.prvky:
                if 'data-druh' in a and tag == 'li':
                    self.assertTrue(set(a['data-druh'].split()) <= KLUCE_KATEGORII, (subor, a))
        s = (web / 'realizacie' / 'xss' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', s)
        self.assertIn('&quot;onerror=alert(1)', s)
        self.assertIn('&lt;b&gt;tučné&lt;/b&gt;', s)
        self.assertNotIn('alert(5)', (web / 'realizacie' / 'index.html').read_text(encoding='utf-8').split('<script type="application/ld+json">')[0])
        self.assertEqual(interne_odkazy(web, '/'), [])
        for subor in stranky(web):
            self.assertEqual(csp_sedi(subor), '', subor)

    def test_determinizmus_dvoch_behov(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        self.assertEqual(postav(a, self.obsah)[0], 0)
        self.assertEqual(postav(b, self.obsah)[0], 0)
        self.assertEqual(odtlacky(a), odtlacky(b))
        a2, b2 = self.tmp / 'a2', self.tmp / 'b2'
        self.assertEqual(postav(a2, self.obsah, nahlad=True)[0], 0)
        self.assertEqual(postav(b2, self.obsah, nahlad=True)[0], 0)
        self.assertEqual(odtlacky(a2), odtlacky(b2))

    def test_maze_len_vlastny_vystup(self):
        web = self.tmp / 'web'
        web.mkdir()
        (web / 'cudzi.txt').write_text('nechaj ma', encoding='utf-8')
        self.assertEqual(postav(web, self.obsah)[0], 0)
        self.assertTrue((web / 'realizacie' / 'hola' / 'index.html').is_file())
        (self.obsah / 'realizacie' / 'holá.json').unlink()
        self.assertEqual(postav(web, self.obsah, nahlad=True)[0], 0)
        self.assertFalse((web / 'realizacie' / 'hola').exists())
        self.assertFalse((web / 'sitemap.xml').exists())
        self.assertFalse((web / 'CNAME').exists())
        self.assertEqual((web / 'cudzi.txt').read_text(encoding='utf-8'), 'nechaj ma')

    def test_vystup_nesmie_byt_obsah(self):
        self.assertEqual(postav(self.obsah, self.obsah)[0], 2)
        self.assertEqual(postav(self.obsah / 'realizacie', self.obsah)[0], 2)

    def test_prazdny_obsah(self):
        for p in (self.obsah / 'realizacie').glob('*.json'):
            p.unlink()
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertTrue((web / 'index.html').is_file())
        self.assertEqual(interne_odkazy(web, '/'), [])

    def test_ci_bez_premennej_stavia_nahlad(self):
        # šablóna s fotkami sekcií z malého obsahu: slugy skutočného obsahu by v Actions boli chyba zostavy (fb-ostro A4)
        sablona = sablona_s_nastaveniami(self.tmp, **NASTAVENIA_MALY)
        web = self.tmp / 'ci'
        with mock.patch.object(build, 'SABLONA', sablona):
            kod, out, err = postav(web, self.obsah, prostredie={'GITHUB_ACTIONS': 'true'})
        self.assertEqual(kod, 0, err)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', (web / 'index.html').read_text(encoding='utf-8'))
        self.assertIn('href="/bmbuilding/kontakt/"', (web / '404.html').read_text(encoding='utf-8'))
        self.assertFalse((web / 'CNAME').exists())
        web2 = self.tmp / 'ci-ostry'
        with mock.patch.object(build, 'SABLONA', sablona):
            kod, out, err = postav(web2, self.obsah, prostredie={'GITHUB_ACTIONS': 'true', 'BMB_OSTRY': 'ano'})
        self.assertEqual(kod, 0, err)
        self.assertNotIn('noindex', (web2 / 'index.html').read_text(encoding='utf-8'))
        self.assertTrue((web2 / 'CNAME').is_file())

    def test_poskodena_fotka_sa_vynecha(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        f = self.obsah / 'obrazky' / 'plna' / '01.jpg'
        f.write_bytes(f.read_bytes()[:3000])
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('01.jpg', err)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('data-lightbox'), 1)

    def test_chyba_obrazka_nezmaze_vystup(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        pred = odtlacky(web)
        fotka(self.obsah / 'obrazky' / 'plna' / '02.jpg', 500, 800, 33)

        def zlyhaj(*a, **k):
            raise OSError('image file is truncated')
        with mock.patch.object(build, 'webp_bajty', zlyhaj):
            kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 1)
        self.assertEqual(odtlacky(web), pred)

    def test_neplatny_datum(self):
        self.uprav('plna', datum='2026-13-45')
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('2026-13-45', err)
        for p in web.rglob('*'):
            if p.is_file() and p.suffix in ('.html', '.xml'):
                self.assertNotIn('2026-13-45', p.read_text(encoding='utf-8'), p)

    def test_upravene_v_lastmod(self):
        self.uprav('plna', upravene='2030-01-02')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        mapa = (web / 'sitemap.xml').read_text(encoding='utf-8')
        self.assertIn('<url><loc>https://bmbuilding.sk/realizacie/plna/</loc><lastmod>2030-01-02</lastmod></url>', mapa)
        self.assertIn('"dateModified":"2030-01-02"', (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8'))

    def test_skratenie_titulku(self):
        dlhy = 'Rekonštrukcia a nadstavba bytového domu na rohu ulíc pri starom moste č. 15 a 17 pre investora'
        self.uprav('plna', nazov=dlhy, miesto='Bratislava, Staré Mesto')
        self.uprav('xss', nazov='Rekonštrukcia a nadstavba bytového domu, na rohu ulíc pri starom moste č. 15 a 17',
                   miesto='Bratislava, Staré Mesto')
        self.uprav('holá', nazov='Dom', titulok='Vlastný titulok realizácie')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        self.assertEqual(stranka_info(web / 'realizacie' / 'plna' / 'index.html')['title'], dlhy)
        self.assertEqual(stranka_info(web / 'realizacie' / 'xss' / 'index.html')['title'],
                         'Rekonštrukcia a nadstavba bytového domu | BM Building')
        self.assertEqual(stranka_info(web / 'realizacie' / 'hola' / 'index.html')['title'],
                         'Vlastný titulok realizácie | BM Building')

    def test_description_bez_dvoch_bodiek(self):
        self.uprav('plna', projektant='Kappa 1 s.r.o.', roky='2019')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        d = stranka_info(web / 'realizacie' / 'plna' / 'index.html')['desc']
        self.assertIn('Projektant: Kappa 1 s.r.o. Roky: 2019.', d)
        self.assertNotIn('Termín', d)
        self.assertNotIn('..', d)

    def test_odkaz_na_plnu_fotku(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<a class="galeria-odkaz" href="../../obrazky/plna/01-1100.webp" data-lightbox', s)
        # 500 x 800 v galérii s dvomi fotkami (bunka až 868 x 579 px) by cover zväčšil nad 1,25 násobok: malá fotka
        # (oprava po bráne, bod 1, GALERIA_BUNKY)
        self.assertIn('<a class="galeria-odkaz mala" href="../../obrazky/plna/02-500.webp" data-lightbox', s)

    def test_uvod_bez_velkej_fotky(self):
        # maly_obsah nemá fotku aspoň 1280 px: slider úvodu smie mať menšiu fotku, ktorú vybral klient (uvod_slider),
        # nikdy rozmazanú; H1, údaje, tlačidlá a Činnosť ostávajú, Postup, meno s olovnicou a schéma nie sú
        web = self.tmp / 'web'
        sablona = sablona_s_nastaveniami(self.tmp, **NASTAVENIA_MALY)
        with mock.patch.object(build, 'SABLONA', sablona):
            kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        s = (web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<section class="uvod" aria-labelledby="uvod-nadpis">', s)
        self.assertEqual(s.count('fetchpriority="high"'), 2)  # preload a prvá fotka slidera
        for kus in ('class="uvod-akcie"', 'id="cinnost"', 'class="firma-fakty"', 'class="snimka snimka-0 je"'):
            self.assertIn(kus, s)
        for kus in ('class="olovnica"', 'class="plan uvod-plan"', 'id="postup"', 'class="pohlad"'):
            self.assertNotIn(kus, s)
    def test_znak_mimo_pisma_varuje(self):
        f = self.obsah / 'firma.json'
        d = json.loads(f.read_text(encoding='utf-8'))
        d['konatel'] = 'Ing. Mário Bulko 字'
        f.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn("písmo Geist nemá znaky '字'", err)

    def test_titulok_dlhych_nazvov(self):
        with redirect_stderr(io.StringIO()):
            w = build.Web(self.obsah, False)
        pripady = [
            ('Rekonštrukcia bytového domu a výmena výťahov, Námestie hraničiarov', 'Bratislava Petržalka'),
            ('Stavebné úpravy nástupného priestoru do reštaurácie UFO na Moste SNP', 'Bratislava'),
            ('Novostavba rodinného domu s garážou a bazénom v lokalite Dolné Rúbanisko', 'Ivanka pri Dunaji, Dolné Rúbanisko'),
            ('Prestavba administratívnej budovy na bytový dom s podzemnými garážami', 'Bratislava-Nové Mesto Koliba Kamzík'),
            ('Prestavba, garáže', 'Bratislava-Nové Mesto Koliba Kamzík pod lesom pri vysielači a ďalej až k Železnej studničke'),
            ('Rekonštrukcia historickej budovy, Ventúrska ulica', 'Bratislava, Staré Mesto'),
            ('Polyfunkčný objekt', ''),
        ]
        for nazov, miesto in pripady:
            t = w.titulok_realizacie({'nazov': nazov, 'miesto': miesto, 'titulok': ''})
            bez = t[:-len(' | BM Building')] if t.endswith(' | BM Building') else t
            prva = nazov.split(',')[0]
            mesto = miesto.split(',')[0]
            self.assertIn(bez, {nazov, prva, f'{nazov}, {mesto}', f'{prva}, {mesto}'}, t)
        self.assertEqual(w.titulok_realizacie({'nazov': pripady[0][0], 'miesto': pripady[0][1], 'titulok': ''}),
                         'Rekonštrukcia bytového domu a výmena výťahov | BM Building')

    def test_json_s_bom(self):
        bom = chr(0xFEFF)
        (self.obsah / 'realizacie' / 'h.json').write_text(bom + json.dumps({'slug': 'bom', 'nazov': 'S BOM'}), encoding='utf-8')
        f = self.obsah / 'firma.json'
        f.write_text(bom + f.read_text(encoding='utf-8'), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertTrue((self.tmp / 'web' / 'realizacie' / 'bom' / 'index.html').is_file(), err)

    def test_zle_typy_poli_firmy(self):
        f = self.obsah / 'firma.json'
        d = json.loads(f.read_text(encoding='utf-8'))
        d['sluzby'] = 5
        d['opravnenia'] = 'osvedčenie'
        f.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertIn('sluzby', err)
        self.assertIn('opravnenia', err)


ACTIONS = {'GITHUB_ACTIONS': 'true'}


class TestChybaVActions(unittest.TestCase):
    """fb-ostro A4 (OPONENT.md S2, POSTUP.md časť 4): v GitHub Actions je chýbajúca realizácia alebo fotka z uvod_foto,
    firma_foto alebo postup_foto a slug z pas_vyber chyba zostavy (kód 1): nič sa nezapíše, krok nasadenia sa nespustí
    a ostane posledný dobrý web. Lokálne ostáva varovanie a sekcia bez fotky."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        self.obsah = maly_obsah(self.tmp)
        self.web = self.tmp / 'web'
        self.nastav()
        # posledný dobrý web: zostava v Actions s fotkami sekcií, ktoré v obsahu sú
        kod, out, err = self.postav(ACTIONS)
        self.assertEqual(kod, 0, err)
        self.dobry = odtlacky(self.web)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def nastav(self, **zmeny):
        self.sablona = sablona_s_nastaveniami(self.tmp, **{**NASTAVENIA_MALY, **zmeny})

    def postav(self, prostredie=None, vystup=None):
        with mock.patch.object(build, 'SABLONA', self.sablona):
            return postav(vystup or self.web, self.obsah, prostredie=prostredie, rychle=True)

    def test_nastavenia_z_obsahu_prejdu(self):
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<li class="snimka snimka-0 je" data-slug="plna">', uvod)
        self.assertIn('realizacie/plna/', uvod.split('<div class="slider"', 1)[1].split('</section>', 1)[0])
        self.assertNotIn('id="prace"', uvod)
        for stare in ('<figure class="atelier-foto">', '<figure class="postup-foto">'):
            self.assertNotIn(stare, uvod)
        # nenastavený kľúč (slider záložnou fotkou karty) nie je chyba ani v Actions; starý kľúč pas_vyber sa ignoruje
        self.nastav(uvod_slider=None, pas_vyber=['plna'])
        kod, out, err = self.postav(ACTIONS)
        self.assertEqual(kod, 0, err)
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<li class="snimka snimka-0 je" data-slug="', uvod)
        self.assertNotIn('id="prace"', uvod)

    def test_chybajuce_v_actions_je_chyba_a_web_ostane(self):
        pripady = [
            ({'uvod_slider': [{'slug': 'plna', 'subor': '01.jpg'}, {'slug': 'zmazana-stavba', 'subor': '05.jpg'}]},
             "uvod_slider: realizácia 'zmazana-stavba' nie je v obsahu"),
            # 99.jpg je v JSON realizácie, ale súbor chýba
            ({'uvod_slider': [{'slug': 'plna', 'subor': '01.jpg'}, {'slug': 'plna', 'subor': '99.jpg'}]},
             'uvod_slider: fotka 99.jpg realizácie plna chýba'),
        ]
        for zmeny, sprava in pripady:
            with self.subTest(sprava):
                self.nastav(**zmeny)
                for prostredie in (ACTIONS, {**ACTIONS, 'BMB_OSTRY': 'ano'}):
                    kod, out, err = self.postav(prostredie)
                    self.assertEqual(kod, 1, err)
                    self.assertIn('CHYBA: v GitHub Actions chýba fotka sekcie alebo realizácia slidera', err)
                    self.assertIn(sprava, err.split('CHYBA:', 1)[1])
                    self.assertEqual(odtlacky(self.web), self.dobry)
                # lokálne len varovanie, zostava prejde ako doteraz
                kod, out, err = self.postav(vystup=self.tmp / 'lokalne')
                self.assertEqual(kod, 0, err)
                self.assertIn('VAROVANIE: ' + sprava, err)

    def test_zmazana_fotka_alebo_realizacia_v_obsahu(self):
        # OPONENT.md S2: sťahovanie alebo ručný zásah zmaže fotku slidera z obsah/, nastavenia ostanú
        (self.obsah / 'obrazky' / 'plna' / '01.jpg').unlink()
        kod, out, err = self.postav(ACTIONS)
        self.assertEqual(kod, 1, err)
        self.assertIn('uvod_slider: fotka 01.jpg realizácie plna chýba', err.split('CHYBA:', 1)[1])
        self.assertEqual(odtlacky(self.web), self.dobry)
        # zmazaná celá realizácia zo slidera (zz-dupl má ten istý slug, preč aj ona)
        (self.obsah / 'realizacie' / 'plna.json').unlink()
        (self.obsah / 'realizacie' / 'zz-dupl.json').unlink()
        kod, out, err = self.postav(ACTIONS)
        self.assertEqual(kod, 1, err)
        self.assertIn("uvod_slider: realizácia 'plna' nie je v obsahu", err.split('CHYBA:', 1)[1])
        self.assertNotIn('pas_vyber', err)
        self.assertEqual(odtlacky(self.web), self.dobry)
        # lokálne sa web zostaví bez nich (doterajšie správanie)
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)


class TestFotkyPravidla(unittest.TestCase):
    """Rozmazané fotky, obal a pred a po (FOTKY.md, Pravidlo pre build.py) na dočasnom obsahu a dočasnej šablóne."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        self.obsah = maly_obsah(self.tmp)
        self.sablona = self.tmp / 'sablona'
        shutil.copytree(KOREN / 'sablona', self.sablona)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def postav(self, nahlad=False):
        with mock.patch.object(build, 'SABLONA', self.sablona):
            return postav(self.tmp / 'web', self.obsah, nahlad=nahlad)

    def pravidla(self, **zmeny):
        p = self.sablona / 'fotky-pravidla.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d.update(zmeny)
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')

    def realizacia(self, slug, fotky, **polia):
        d = {'slug': slug, 'nazov': f'Bytový dom {slug}', 'miesto': 'Bratislava', 'rola': 'Stavebný dozor',
             'roky': '2020 až 2021', 'fotky': [{'subor': f'obsah/obrazky/{slug}/{f}', 'alt': ''} for f in fotky]}
        d.update(polia)
        (self.obsah / 'realizacie' / f'{slug}.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')

    def test_instagram_len_s_istou_adresou(self):
        # adresa Instagramu je v sablona/nastavenia.json (nie v obsah/); bez nej alebo s cudzou adresou odkaz nie je
        p = self.sablona / 'nastavenia.json'
        n = json.loads(p.read_text(encoding='utf-8'))
        for adresa, ma_byt in ((None, False), ('javascript:alert(1)', False), ('https://evil.example/bmbuilding', False),
                               ('http://www.instagram.com/bmbuilding/', False), ('https://instagram.com.zle.sk/bmbuilding/', False),
                               ('https://www.instagram.com/bmbuilding/', True)):
            n['instagram'] = adresa
            p.write_text(json.dumps(n, ensure_ascii=False), encoding='utf-8')
            kod, out, err = self.postav()
            self.assertEqual(kod, 0, err)
            s = (self.tmp / 'web' / 'index.html').read_text(encoding='utf-8')
            self.assertEqual('href="https://www.instagram.com/bmbuilding/"' in s, ma_byt, adresa)
            self.assertNotIn('javascript:', s)
            self.assertNotIn('evil.example', s)
            uzol = [u for u in stranka_info(self.tmp / 'web' / 'index.html')['ld'] if 'Organization' in u.get('@type', [])][0]
            # Instagram je profil konateľa (mariobulko, ÚVOD v3): patrí k osobe (employee), nie do sameAs firmy
            self.assertNotIn('https://www.instagram.com/bmbuilding/', uzol.get('sameAs', []))
            self.assertEqual('https://www.instagram.com/bmbuilding/' in uzol['employee'].get('sameAs', []), ma_byt, adresa)

    def test_ostrost_ako_audit(self):
        # rozptyl Laplaciánu (4-susedné jadro) na šedej verzii so šírkou 960: Pillow sa zhoduje s priamym výpočtom
        p = self.tmp / 'o.jpg'
        fotka(p, 960, 40, 5)
        with Image.open(p) as im:
            g = im.convert('L')
        w, h = g.size
        px = g.load()
        lap = [px[x, y - 1] + px[x, y + 1] + px[x - 1, y] + px[x + 1, y] - 4 * px[x, y]
               for y in range(1, h - 1) for x in range(1, w - 1)]
        m = sum(lap) / len(lap)
        var = sum((v - m) ** 2 for v in lap) / len(lap)
        self.assertAlmostEqual(build.ostrost_fotky(p), var, delta=var * 0.002 + 0.2)
        r = self.tmp / 'r.jpg'
        rozmazana(r, 1200, 800)
        self.assertLess(build.ostrost_fotky(r), 80)

    def test_rozmazana_fotka_nikdy_obal(self):
        rozmazana(self.obsah / 'obrazky' / 'mix' / '01.jpg', 1400, 900)
        fotka(self.obsah / 'obrazky' / 'mix' / '02.jpg', 1000, 700, 50)
        rozmazana(self.obsah / 'obrazky' / 'len-zla' / '01.jpg', 1400, 900)
        self.realizacia('mix', ['01.jpg', '02.jpg'], poradie=0)
        self.realizacia('len-zla', ['01.jpg'], poradie=0)
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertIn('mix: fotka mix/01.jpg je rozmazaná', err)
        web = self.tmp / 'web'
        for p in stranky(web):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('obrazky/mix/01-', s, p)
            self.assertNotIn('obrazky/len-zla/', s, p)
        zoznam = (web / 'realizacie' / 'index.html').read_text(encoding='utf-8')
        mix = zoznam.split('href="../realizacie/mix/"', 1)[1].split('</li>', 1)[0]
        # karta zo zvyšnej ostrej fotky: 1000 x 700 dá výrez 4 : 5 len 560 x 700 (pod build.KARTA_MIN_OREZ), preto celá
        # fotka 4 : 3 (933 x 700) v strede rámu (brána bmb-presnost pokus 1, nález 1)
        self.assertIn('src="../obrazky/mix/02-720-43.webp"', mix)
        self.assertIn('class="foto-cela"', mix)
        zla = zoznam.split('href="../realizacie/len-zla/"', 1)[1].split('</li>', 1)[0]
        # bez jedinej ostrej fotky: v ráme logo firmy, nikdy rozmazaná fotka
        self.assertIn('class="karta-znak"', zla)
        self.assertNotIn('obrazky/len-zla', zla)
        self.assertEqual((web / 'realizacie' / 'mix' / 'index.html').read_text(encoding='utf-8').count('data-lightbox'), 1)
        self.assertFalse((web / 'obrazky' / 'len-zla').exists())
        # OG realizácie len s rozmazanou fotkou je bez fotky: modrá aj vpravo
        with Image.open(web / 'og' / 'len-zla.jpg') as im:
            self.assertTrue(bliz(im.convert('RGB').getpixel((1150, 300)), MODRA, 10))

    def test_obal_podla_typu_a_velkosti(self):
        fotka(self.obsah / 'obrazky' / 'typy' / '01.jpg', 1200, 800, 1)   # detail podľa pravidiel
        fotka(self.obsah / 'obrazky' / 'typy' / '02.jpg', 600, 400, 2)    # malá
        fotka(self.obsah / 'obrazky' / 'typy' / '03.jpg', 700, 1000, 3)   # na výšku
        fotka(self.obsah / 'obrazky' / 'typy' / '04.jpg', 800, 600, 4)    # vyhovuje len základnému prahu
        fotka(self.obsah / 'obrazky' / 'typy' / '05.jpg', 1000, 700, 5)   # interiér
        self.realizacia('typy', ['01.jpg', '02.jpg', '03.jpg', '04.jpg', '05.jpg'])
        self.pravidla(typy={'typy/01.jpg': 'D', 'typy/05.jpg': 'I'})
        with mock.patch.object(build, 'SABLONA', self.sablona), redirect_stderr(io.StringIO()):
            w = build.Web(self.obsah, False)
        r = [x for x in w.realizacie if x['slug'] == 'typy'][0]
        self.assertEqual(r['obal']['rel'], 'typy/04.jpg')
        self.assertEqual([f['rel'] for f in r['galeria']][-2:], ['typy/01.jpg', 'typy/05.jpg'])  # detail a interiér na konci
        self.assertIsNone(r['velka'])

    def test_mala_rozmazana_vizualizacia_v_galerii(self):
        # oprava po bráne, bod 1: vizualizácia (malý render 320 x 179, ostrosť pod prah_rozmazana) ide do galérie, lebo
        # vizualizácie majú vlastný prah prah_rozmazana_vizualizacia (predvolene 0); rozmazaná skutočná fotka nie;
        # malá fotka v galérii bez srcset a s triedou mala (nikdy zväčšená nad 1,25 násobok)
        fotka(self.obsah / 'obrazky' / 'vr' / '01.jpg', 1400, 900, 11)
        rozmazana(self.obsah / 'obrazky' / 'vr' / '02.jpg', 1400, 900)
        rozmazana(self.obsah / 'obrazky' / 'vr' / '03.jpg', 320, 179)
        self.realizacia('vr', [])
        p = self.obsah / 'realizacie' / 'vr.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d['fotky'] = [{'subor': 'obsah/obrazky/vr/01.jpg', 'alt': ''}, {'subor': 'obsah/obrazky/vr/02.jpg', 'alt': ''},
                      {'subor': 'obsah/obrazky/vr/03.jpg', 'alt': '', 'druh': 'vizualizacia', 'autor': 'Ing. arch. Test'}]
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        with mock.patch.object(build, 'SABLONA', self.sablona), redirect_stderr(io.StringIO()):
            w = build.Web(self.obsah, False)
        r = next(x for x in w.realizacie if x['slug'] == 'vr')
        self.assertLess(r['fotky'][2]['ostrost'], w.pravidla['prah'])
        self.assertEqual([f['rel'] for f in r['galeria']], ['vr/01.jpg', 'vr/03.jpg'])
        self.assertEqual(r['karta']['rel'], 'vr/01.jpg')
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertIn('vr: fotka vr/02.jpg je rozmazaná', err)
        self.assertNotIn('vr/03.jpg je rozmazaná', err)
        s = (self.tmp / 'web' / 'realizacie' / 'vr' / 'index.html').read_text(encoding='utf-8')
        m = re.search(r'<a class="galeria-odkaz mala" href="\.\./\.\./obrazky/vr/03-320\.webp" data-lightbox [^>]*data-viz="1"[^>]*>(<img [^>]*>)', s)
        self.assertIsNotNone(m)
        self.assertNotIn('srcset', m.group(1))
        self.assertIn('<span class="stitok-viz">Vizualizácia</span>', s)
        # vlastný prah v šablóne platí: s prahom 80 je aj vizualizácia vynechaná
        self.pravidla(prah_rozmazana_vizualizacia=80)
        with mock.patch.object(build, 'SABLONA', self.sablona), redirect_stderr(io.StringIO()):
            w = build.Web(self.obsah, False)
        self.assertEqual([f['rel'] for f in next(x for x in w.realizacie if x['slug'] == 'vr')['galeria']], ['vr/01.jpg'])

    def test_ilustracna_fotka_novinky_bez_fotky(self):
        # oprava po bráne, bod 2: novinka bez fotky má na karte skutočnú ostrú fotku stavby zo sablona/fotky-pravidla.json
        # (ilustracne_fotky, inak ilustracna_predvolena) s popisom „Ilustračná fotka: …“; vizualizácia alebo
        # rozmazaná fotka sa nepoužije (varovanie a ďalšia voľba); logo len keď nie je nič
        fotka(self.obsah / 'obrazky' / 'ilu' / '01.jpg', 1400, 900, 21)
        fotka(self.obsah / 'obrazky' / 'ilu' / '02.jpg', 1400, 900, 42)
        fotka(self.obsah / 'obrazky' / 'ilu' / '03.jpg', 1400, 900, 63)
        self.realizacia('ilu', [])
        p = self.obsah / 'realizacie' / 'ilu.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d['fotky'] = [{'subor': 'obsah/obrazky/ilu/01.jpg', 'alt': ''}, {'subor': 'obsah/obrazky/ilu/02.jpg', 'alt': ''},
                      {'subor': 'obsah/obrazky/ilu/03.jpg', 'alt': '', 'druh': 'vizualizacia'}]
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        sablona_s_nastaveniami(self.tmp, novinky=True)
        (self.obsah / 'novinky').mkdir()
        for slug in ('jubileum', 'pozdrav', 'zla-volba'):
            (self.obsah / 'novinky' / f'{slug}.json').write_text(json.dumps(
                {'slug': slug, 'datum': '2025-08-27', 'nadpis': f'Novinka {slug}', 'text': 'Text novinky.', 'fotky': []},
                ensure_ascii=False), encoding='utf-8')
        self.pravidla(ilustracne_fotky={'jubileum': {'slug': 'ilu', 'subor': '02.jpg', 'dovod': 'test'},
                                        'zla-volba': {'slug': 'ilu', 'subor': '03.jpg', 'dovod': 'vizualizácia'}},
                      ilustracna_predvolena={'slug': 'ilu', 'subor': '01.jpg', 'dovod': 'test'})
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertIn('novinka zla-volba: ilustračná fotka zla-volba (ilu/03.jpg)', err)
        zoznam = (self.tmp / 'web' / 'novinky' / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('class="karta-znak"', zoznam)
        for slug, nn in (('jubileum', '02'), ('pozdrav', '01'), ('zla-volba', '01')):
            karta = zoznam.split(f'href="../novinky/{slug}/"', 1)[1].split('</li>', 1)[0]
            self.assertIn(f'src="../obrazky/ilu/{nn}-', karta, slug)
            self.assertIn('alt="Ilustračná fotka: Bytový dom ilu, Bratislava"', karta.replace('\xa0', ' '), slug)
            self.assertIn('<p class="projekt-ilustr">Ilustračná fotka: Bytový dom ilu, Bratislava</p>', karta.replace('\xa0', ' '), slug)
            self.assertIn('stitok-ilustr', karta, slug)
            detail = (self.tmp / 'web' / 'novinky' / slug / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('obrazky/ilu/', detail.split('<main', 1)[1].split('<footer', 1)[0])
        # bez ilustračných volieb v šablóne: logo a varovanie (nikdy cudzia fotka bez popisu)
        self.pravidla(ilustracne_fotky={}, ilustracna_predvolena=None)
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertIn('novinka pozdrav: bez fotky a bez ilustračnej fotky', err)
        zoznam = (self.tmp / 'web' / 'novinky' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(zoznam.count('class="karta-znak"'), 3)
        self.assertNotIn('Ilustračná', zoznam)

    def test_v_mediach_bezpecne(self):
        """v_mediach (ZMENY-KOLO-1.md 5.7): odkaz len https na asb.sk, linkedin.com a archinfo.sk, obrázok len zo
        sablona/media; zlé položky sa vynechajú s varovaním, skrytá realizácia blok nedostane; bez galérie realizácie
        dostane stránka dialóg lightboxu z obrázka bloku; údaj V médiách v hrdinovi vedie na kotvu."""
        shutil.copyfile(KOREN / 'sablona' / 'media' / 'asb-244-s77-tabulka.jpg', self.sablona / 'media' / 'skuska.jpg')
        self.realizacia('bez-fotiek', [])
        sablona_s_nastaveniami(self.tmp, v_mediach={
            'bez-fotiek': {'kotva': 'casopis', 'nadpis': 'Časopis: stavebný dozor BM Building', 'text': ['Vlastná veta.'],
                           'tabulka': {'subor': 'media/skuska.jpg', 'alt': 'Výrez tabuľky', 'popis': 'Zdroj: skúška'},
                           'strany': [{'subor': '../build.py', 'alt': 'mimo'}, {'subor': 'media/chyba.jpg', 'alt': 'chýba'}],
                           'odkazy': [{'text': 'Dobrý', 'url': 'https://www.asb.sk/clanok'},
                                      {'text': 'Http', 'url': 'http://www.asb.sk/clanok'},
                                      {'text': 'Cudzí', 'url': 'https://zly.example/asb.sk'},
                                      {'text': 'Skript', 'url': 'javascript:alert(1)'},
                                      {'text': '', 'url': 'https://www.linkedin.com/posts/x'}],
                           'uvod': 'Časopis: stavebný dozor BM Building'},
            'skryta': {'nadpis': 'Nie', 'odkazy': [{'text': 'x', 'url': 'https://www.asb.sk/x'}]},
            'plna': {'kotva': 'Zlá Kotva', 'odkazy': [{'text': 'Archinfo', 'url': 'https://www.archinfo.sk/x.html'}]}})
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertIn("v_mediach bez-fotiek: obrázok '../build.py' chýba alebo je mimo sablona/media", err)
        self.assertIn("v_mediach bez-fotiek: obrázok 'media/chyba.jpg' chýba", err)
        for zly in ('http://www.asb.sk/clanok', 'https://zly.example/asb.sk', 'javascript:alert(1)', 'https://www.linkedin.com/posts/x'):
            self.assertIn(f"odkaz '{zly}' nie je https", err)
        self.assertIn('v_mediach skryta: realizácia nie je zverejnená', err)
        self.assertIn("v_mediach plna: kotva 'Zlá Kotva' nie je [a-z0-9-]", err)
        web = self.tmp / 'web'
        s = (web / 'realizacie' / 'bez-fotiek' / 'index.html').read_text(encoding='utf-8')
        blok = s.split('id="casopis"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(re.findall(r'class="odkaz-von" href="([^"]+)"', blok), ['https://www.asb.sk/clanok'])
        self.assertEqual(blok.count('data-lightbox'), 1)
        self.assertNotIn('media-strana', blok)
        self.assertEqual(s.count('<dialog class="lightbox" id="lightbox"'), 1)
        self.assertTrue((web / 'obrazky' / 'media' / 'casopis').is_dir())
        p = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="v-mediach"', p)
        self.assertIn('href="https://www.archinfo.sk/x.html" target="_blank" rel="noopener"', p)
        self.assertFalse((web / 'realizacie' / 'skryta').exists())
        uvod = (web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<a href="realizacie/bez-fotiek/#casopis">', uvod)
        # bez v_mediach ani blok, ani údaj
        sablona_s_nastaveniami(self.tmp, v_mediach=None)
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        self.assertNotIn('<section class="sekcia media-sekcia', (web / 'realizacie' / 'bez-fotiek' / 'index.html').read_text(encoding='utf-8'))
        self.assertNotIn('class="udaj udaj-siroky udaj-media"', (web / 'index.html').read_text(encoding='utf-8'))

    def test_vizualizacia_nie_je_obal_ani_hrdina(self):
        # vizualizácia (druh vizualizacia): za skutočnými fotkami, nikdy obal ani hrdina úvodu, okrem obal_vizualizacia;
        # štítok, data-viz, autor a alt „Vizualizácia“ v galérii (zadanie bmb-dokoncenie, Obsah 3)
        for slug, w, h in (('viz', 1400, 900), ('len-viz', 1400, 900), ('len-viz-bez', 1400, 900)):
            fotka(self.obsah / 'obrazky' / slug / '01.jpg', w, h, 31)
        fotka(self.obsah / 'obrazky' / 'viz' / '02.jpg', 1000, 700, 62)
        viz = {'subor': '', 'alt': '', 'druh': 'vizualizacia', 'autor': 'Ing. arch. Test'}
        for slug, fotky, navyse in (('viz', [dict(viz, subor='obsah/obrazky/viz/01.jpg'), {'subor': 'obsah/obrazky/viz/02.jpg', 'alt': ''}], {}),
                                    ('len-viz', [dict(viz, subor='obsah/obrazky/len-viz/01.jpg')], {'obal_vizualizacia': True}),
                                    ('len-viz-bez', [dict(viz, subor='obsah/obrazky/len-viz-bez/01.jpg')], {})):
            self.realizacia(slug, [], **navyse)
            p = self.obsah / 'realizacie' / f'{slug}.json'
            d = json.loads(p.read_text(encoding='utf-8'))
            d['fotky'] = fotky
            p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        with mock.patch.object(build, 'SABLONA', self.sablona), redirect_stderr(io.StringIO()):
            w = build.Web(self.obsah, False)
        r = {x['slug']: x for x in w.realizacie}
        self.assertEqual(r['viz']['obal']['rel'], 'viz/02.jpg')
        self.assertEqual([f['viz'] for f in r['viz']['galeria']], [False, True])
        self.assertIsNone(r['viz']['velka'])
        self.assertEqual(r['viz']['karta']['rel'], 'viz/02.jpg')
        self.assertTrue(r['len-viz']['obal']['viz'])
        self.assertTrue(r['len-viz']['karta']['viz'])
        self.assertIsNone(r['len-viz']['velka'])
        # realizácia len s vizualizáciami: karta a obal je prvá vizualizácia so štítkom, nie prázdny rám
        # (ZMENY-KOLO-1.md 5.4); hrdina úvodu nikdy z vizualizácie
        self.assertTrue(r['len-viz-bez']['obal']['viz'])
        self.assertTrue(r['len-viz-bez']['karta']['viz'])
        self.assertIsNone(r['len-viz-bez']['velka'])
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        s = (self.tmp / 'web' / 'realizacie' / 'viz' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('<span class="stitok-viz">Vizualizácia</span>'), 1)
        self.assertRegex(s, r'data-viz="1" data-autor="Ing\. arch\. Test">(?:<picture><source [^>]*>)?<img')
        alt = re.search(r'data-viz="1" data-autor="[^"]*">(?:<picture><source [^>]*>)?<img [^>]*alt="([^"]*)"', s).group(1)
        self.assertTrue(alt.startswith('Vizualizácia, '), alt)
        self.assertIn('Ing. arch. Test', alt)

    def test_bez_paru_nic(self):
        self.pravidla(pary_pred_po=[])
        fotka(self.obsah / 'obrazky' / 'plna' / '03.jpg', 1100, 700, 77)
        self.realizacia('plna', ['01.jpg', '03.jpg'])
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        for p in stranky(self.tmp / 'web'):
            self.assertNotIn('data-pred-po', p.read_text(encoding='utf-8'), p)

    def test_par_len_z_overeneho_zoznamu(self):
        fotka(self.obsah / 'obrazky' / 'plna' / '03.jpg', 1100, 700, 77)
        fotka(self.obsah / 'obrazky' / 'plna' / '04.jpg', 1100, 900, 88)
        self.realizacia('plna', ['01.jpg', '03.jpg', '04.jpg'])
        par = {'slug': 'plna', 'pred': 'obrazky/plna/03.jpg', 'po': 'obrazky/plna/01.jpg', 'istota': 'vysoká',
               'popis_pred': 'Rozostavané', 'popis_po': 'Hotové'}
        self.pravidla(pary_pred_po=[par])
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        web = self.tmp / 'web'
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('data-pred-po'), 1)
        pp = s.split('data-pred-po', 1)[1].split('</div>', 1)[0]
        self.assertIn('obrazky/plna/02-', pp.split('pred-po-po', 1)[0])  # 03.jpg je druhá fotka obsahu
        self.assertIn('obrazky/plna/01-', pp.split('pred-po-po', 1)[1])
        for p in stranky(web):
            if p.parent.name != 'plna':
                self.assertNotIn('data-pred-po', p.read_text(encoding='utf-8'), p)
        # nízka istota, iný pomer strán, chýbajúci súbor, cudzia realizácia: nič a varovanie
        # návrh v navrhy_pred_po sa nikdy nevykreslí
        self.pravidla(pary_pred_po=[], navrhy_pred_po=[par])
        kod, out, err = self.postav()
        self.assertEqual(kod, 0, err)
        for p in stranky(web):
            self.assertNotIn('data-pred-po', p.read_text(encoding='utf-8'), p)
        for zly, hlaska in ((dict(par, istota='nízka'), 'istota'), (dict(par, istota='stredná'), 'istota'),
                            (dict(par, po='obrazky/plna/04.jpg'), 'pomer strán'),
                            (dict(par, pred='obrazky/plna/99.jpg'), 'chýba'), (dict(par, slug='hola'), None)):
            self.pravidla(pary_pred_po=[zly])
            kod, out, err = self.postav()
            self.assertEqual(kod, 0, err)
            for p in stranky(web):
                self.assertNotIn('data-pred-po', p.read_text(encoding='utf-8'), (zly, p))
            if hlaska:
                self.assertIn(hlaska, err)


class TestNovinky(unittest.TestCase):
    """Novinky z obsah/novinky (linka bmb-fb): bezpečne ako realizácie, a len keď nejaké sú."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        cls.obsah = maly_obsah(cls.tmp)
        pridaj_novinky(cls.obsah)
        cls.web = cls.tmp / 'web'
        # Novinky sú v šablóne vypnuté (ZMENY-KOLO-1.md 5.7); tu kópia šablóny s "novinky": true
        with mock.patch.object(build, 'SABLONA', sablona_s_nastaveniami(cls.tmp, novinky=True)):
            cls.kod, cls.out, cls.err = postav(cls.web, cls.obsah)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_zle_vstupy_preskocene_s_varovanim(self):
        self.assertEqual(self.kod, 0, self.err)
        stranky_n = sorted(p.name for p in (self.web / 'novinky').iterdir() if p.is_dir())
        self.assertEqual(stranky_n, ['bez-nadpisu', 'dobra', 'vela', 'xss-n'])
        for kus in ('pokazena.json', 'Zlý slug', '2026-02-30', 'prazdna.json', 'novinky/dobra: fotka', '99.jpg',
                    'novinky/vela: 12 fotiek, použitých prvých 10'):
            self.assertIn(kus, self.err)
        self.assertNotIn('Skrytá', (self.web / 'novinky' / 'index.html').read_text(encoding='utf-8'))
        vela = (self.web / 'novinky' / 'vela' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(vela.count('data-lightbox'), 10)

    def test_stranky_menu_sitemap_a_jsonld(self):
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        # novinky sú v menu (ako na starom webe ich ukážka na úvode nemá) a na vlastnej stránke
        self.assertIn('href="novinky/"', uvod.split('<main', 1)[0])
        self.assertNotIn('class="novinka-karta', uvod)
        zoznam = (self.web / 'novinky' / 'index.html').read_text(encoding='utf-8')
        poradie = re.findall(r'<a href="\.\./novinky/([a-z0-9-]+)/">', zoznam.split('class="karty novinky-karty"', 1)[1])
        self.assertEqual(poradie, ['dobra', 'bez-nadpisu', 'xss-n', 'vela'])
        # svetlé karty novinky: každá s obrázkom (fotka 4 : 5, bez fotky logo), kóta s dátumom
        karty = zoznam.split('class="karty novinky-karty"', 1)[1].split('</ul>', 1)[0].split('<li class="novinka-karta')[1:]
        self.assertEqual(len(karty), 4)
        for k in karty:
            self.assertIn('<img', k)
            self.assertIn('class="kota"', k)
            self.assertRegex(k.replace('\u00a0', ' '), r'<span class="vh">\d{1,2}\. \d{1,2}\. \d{4}</span>')
        mapa = (self.web / 'sitemap.xml').read_text(encoding='utf-8')
        self.assertIn('<loc>https://bmbuilding.sk/novinky/</loc>', mapa)
        info = stranka_info(self.web / 'novinky' / 'dobra' / 'index.html')
        clanok = [u for u in info['ld'] if u.get('@type') == 'Article'][0]
        self.assertEqual(clanok['datePublished'], '2026-09-10')
        self.assertEqual(clanok['headline'], 'Betonáž stropu na stavbe v Pezinku')
        self.assertTrue(all('/obrazky-novinky/dobra/' in i for i in clanok['image']))
        self.assertIn('href="https://www.facebook.com/BmBuilding/posts/123"', info['telo'])
        self.assertIn('Dnes sme betónovali strop.', info['telo'])
        self.assertEqual(stranka_info(self.web / 'novinky' / 'bez-nadpisu' / 'index.html')['h1'], 'Prvý riadok je nadpis, 1. 8. 2026')

    def test_xss_a_kontroly(self):
        for subor in sorted(self.web.rglob('*.html')):
            t = subor.read_text(encoding='utf-8')
            self.assertNotIn('<script>alert', t, subor)
            self.assertNotIn('<img src=x', t, subor)
            self.assertNotIn('javascript:', t, subor)
            self.assertEqual(rozober(subor).chyby, [], subor)
        for subor in stranky(self.web):
            self.assertEqual(csp_sedi(subor), '', subor)
        self.assertEqual(interne_odkazy(self.web, '/'), [])


class TestStovkyRealizacii(unittest.TestCase):
    """Stovky realizácií (SMER.md 8): do limitu jedna statická stránka, nad ním stránky po desaťročiach; každá
    realizácia má stránku a je v zozname práve raz; lazy obrázky so srcset a rozmermi; úvod len 8 kariet v páse."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        obsah = cls.tmp / 'obsah'
        (obsah / 'realizacie').mkdir(parents=True)
        shutil.copyfile(KOREN / 'obsah' / 'firma.json', obsah / 'firma.json')
        for i in range(4):
            fotka(obsah / 'obrazky' / 'spolocne' / f'{i}.jpg', 720, 540, i * 50)
        druhy = ['Bytový dom', 'RD', 'Rekonštrukcia objektu', 'Sklad', 'Polyfunkčný objekt']
        for i in range(120):
            d = {'slug': f'stavba-{i:03d}', 'nazov': f'{druhy[i % 5]} {i:03d}', 'miesto': 'Bratislava',
                 'rola': ['Stavebný dozor', 'Projektový manažér', 'Stavbyvedúci'][i % 3],
                 'roky': f'{1995 + i % 31} až {1996 + i % 31}' if i % 9 else '', 'datum': f'20{10 + i % 16}-0{1 + i % 9}-1{i % 9}',
                 'zdroj': 'fb' if i % 9 == 0 else 'web', 'poradie': i,
                 'fotky': [{'subor': f'obsah/obrazky/spolocne/{i % 4}.jpg', 'alt': ''}] if i % 7 else []}
            (obsah / 'realizacie' / f'{d["slug"]}.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        cls.obsah = obsah
        cls.web = cls.tmp / 'web'
        cls.kod, cls.out, cls.err = postav(cls.web, obsah)
        # nad limit (v teste 50): stránky po desaťročiach
        cls.sablona = cls.tmp / 'sablona'
        shutil.copytree(KOREN / 'sablona', cls.sablona)
        n = json.loads((cls.sablona / 'nastavenia.json').read_text(encoding='utf-8'))
        n['realizacie_na_stranu'] = 50
        (cls.sablona / 'nastavenia.json').write_text(json.dumps(n, ensure_ascii=False), encoding='utf-8')
        cls.web2 = cls.tmp / 'web2'
        with mock.patch.object(build, 'SABLONA', cls.sablona):
            cls.kod2, cls.out2, cls.err2 = postav(cls.web2, obsah)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_jedna_stranka_zoznamu(self):
        self.assertEqual(self.kod, 0, self.err)
        # slider šablóny (uvod_slider) nemá v tomto obsahu ani jednu realizáciu: lokálne varovania a záložná fotka,
        # nie chyba (v GitHub Actions chyba, TestChybaVActions); pás úvodu je preč, varovanie pas_vyber nie je
        # to isté pre v_mediach (ZMENY-KOLO-1.md 5.7: realizácie Lumière a Devínske lúky v tomto obsahu nie sú)
        sekcie = ('presmerovanie', 'uvod_slider', 'v_mediach')
        self.assertEqual([r for r in self.err.splitlines() if r.strip() and not any(s in r for s in sekcie)], [])
        self.assertNotIn('pas_vyber', self.err)
        self.assertEqual(len([p for p in (self.web / 'realizacie').iterdir() if p.is_dir()]), 120)
        s = (self.web / 'realizacie' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('<li class="dlazdica'), 120)
        self.assertEqual(len(set(re.findall(r'<a href="\.\./realizacie/(stavba-\d{3})/">', s))), 120)
        imgs = re.findall(r'<img[^>]*>', s.split('<main', 1)[1].split('<section class="sekcia kontakt"', 1)[0])
        # realizácia bez fotky má v ráme karty logo (karta-znak), ostatné výrez fotky 4 : 5
        self.assertEqual(sum(1 for i in imgs if 'karta-znak' in i), len(range(0, 120, 7)))
        imgs = [i for i in imgs if 'karta-znak' not in i]
        self.assertEqual(len(imgs), 120 - len(range(0, 120, 7)))
        # len fotka prvej karty (LCP) je bez loading=lazy a s fetchpriority=high (oprava 1)
        self.assertEqual(sum(1 for i in imgs if 'fetchpriority="high"' in i), 1)
        for img in imgs:
            if 'fetchpriority="high"' not in img:
                self.assertIn('loading="lazy"', img)
            # fotka 720 x 540: výrez 4 : 5 má len 432 px (pod build.KARTA_MIN_OREZ), karta ukáže celú fotku 4 : 3
            # v strede rámu (brána bmb-presnost pokus 1, nález 1); väčšia šírka sa nevyrába
            self.assertRegex(img, r'srcset="[^"]*-43\.webp \d+w')
            self.assertIn('foto-cela', img)
            self.assertRegex(img, r'width="\d+" height="\d+"')
        self.assertLess(len(gzip.compress(s.encode('utf-8'), 9)), 40 * 1024)
        css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        self.assertIn('content-visibility:auto', css)
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        # úvod bez pásu aj pri 120 realizáciách (ZMENY-KOLO-1.md 3A.6): len slider so záložnou fotkou
        self.assertNotIn('id="prace"', uvod)
        self.assertNotIn('<li class="projekt">', uvod)
        self.assertEqual(interne_odkazy(self.web, '/'), [])

    def test_nad_limit_stranky_po_desatrociach(self):
        self.assertEqual(self.kod2, 0, self.err2)
        zoznamy = [self.web2 / 'realizacie' / 'index.html'] + sorted((self.web2 / 'realizacie').glob('roky-*/index.html'))
        self.assertGreater(len(zoznamy), 2)
        vsetky = []
        for p in zoznamy:
            s = p.read_text(encoding='utf-8')
            self.assertIn('<nav class="dekady"', s)
            self.assertEqual(rozober(p).chyby, [], p)
            slugy = re.findall(r'<a href="(?:\.\./)+realizacie/(stavba-\d{3})/">', s)
            self.assertLessEqual(len(slugy), 50 if p.parent.name == 'realizacie' else 120)
            # na stránkach desaťročí niet výberu rokov (nahrádza ho navigácia), druh a zoradenie ostávajú
            self.assertNotIn('data-roky', s)
            self.assertIn('data-zoradit', s)
            vsetky += slugy
        self.assertEqual(sorted(vsetky), sorted(f'stavba-{i:03d}' for i in range(120)))
        self.assertEqual(len(vsetky), len(set(vsetky)))
        mapa = (self.web2 / 'sitemap.xml').read_text(encoding='utf-8')
        for p in zoznamy[1:]:
            self.assertIn(f'<loc>https://bmbuilding.sk/realizacie/{p.parent.name}/</loc>', mapa)
        self.assertEqual(interne_odkazy(self.web2, '/'), [])


def rozmnoz_obsah(ciel, pocet=60):
    """Kópia skutočného obsahu (obsah/ do ciel/obsah) rozmnožená na aspoň pocet zverejnených realizácií: kópie
    existujúcich realizácií so slugom <slug>-kopia-<k> a názvom „<názov> <k>“, fotky z tých istých súborov
    (zadanie bmb-fb-uplnost bod 4, test na 60 realizácií). Skutočný obsah/ sa nemení."""
    obsah = ciel / 'obsah'
    shutil.copytree(KOREN / 'obsah', obsah)
    zdroje = []
    for p in sorted((obsah / 'realizacie').glob('*.json')):
        d = json.loads(p.read_text(encoding='utf-8-sig'))
        if d.get('zverejnene') is not False:
            zdroje.append(d)
    n = len(zdroje)
    k = 2
    while n < pocet:
        for d in zdroje:
            if n >= pocet:
                break
            c = dict(d)
            c['slug'] = f'{d["slug"]}-kopia-{k}'
            c['id'] = c['slug']
            c['nazov'] = f'{d["nazov"]} {k}'
            c.pop('titulok', None)
            (obsah / 'realizacie' / f'{c["slug"]}.json').write_text(json.dumps(c, ensure_ascii=False), encoding='utf-8')
            n += 1
        k += 1
    return obsah, n


ZOZNAM_SPRAVANIE = KOREN / 'nastroje' / 'zoznam_spravanie.cjs'


def data_zoznamu(web):
    """Karty a ovládanie zoznamu z vykreslenej stránky web/realizacie/index.html (vstup pre zoznam_spravanie.cjs)."""
    telo = (Path(web) / 'realizacie' / 'index.html').read_text(encoding='utf-8').split('<main', 1)[1]
    r = Rozbor()
    r.feed(telo)
    li = [a for t, a in r.prvky if t == 'li' and 'dlazdica' in a.get('class', '').split()]
    mena = re.findall(r'<h2 class="dlazdica-nazov"><a href="[^"]*">([^<]+)</a></h2>', telo)
    assert len(li) == len(mena), (len(li), len(mena))

    def moznosti(atr):
        blok = telo.split(f'<select {atr}>', 1)
        return re.findall(r'<option value="([^"]*)"', blok[1].split('</select>', 1)[0]) if len(blok) == 2 else None

    return {
        'naraz': int(re.search(r'data-zoznam data-naraz="(\d+)"', telo).group(1)),
        'karty': [{'poradie': int(a['data-poradie']), 'druh': a.get('data-druh', ''), 'rok': a.get('data-rok', ''),
                   'mesto': a.get('data-mesto', ''), 'nazov': html.unescape(n)} for a, n in zip(li, mena)],
        'druhy': re.findall(r'<button class="filter" type="button" data-druh="([a-z]*)"', telo),
        'roky': moznosti('data-roky'), 'mesta': moznosti('data-mesto'), 'zoradit': moznosti('data-zoradit'),
    }


def spravanie_zoznamu(web, kroky, search='', app_js=None):
    """Spustí časť 5 app.js nad falošným DOM (nastroje/zoznam_spravanie.cjs) a vráti stavy po každom kroku."""
    import subprocess
    vstup = dict(data_zoznamu(web), app_js=str(app_js or Path(web) / 'app.js'), kroky=kroky, search=search)
    node = shutil.which('node')
    assert node, 'node nie je v PATH (test správania zoznamu ho potrebuje)'
    p = subprocess.run([node, str(ZOZNAM_SPRAVANIE)], input=json.dumps(vstup), capture_output=True, text=True,
                       encoding='utf-8', timeout=120)
    assert p.returncode == 0, p.stderr
    return vstup, json.loads(p.stdout)


def chyby_zoznamu(web, app_js=None):
    """Postupné načítanie, filter a zoradenie zoznamu realizácií proti nezávisle vypočítanému očakávaniu.
    Scenár: spustenie, 2x Zobraziť ďalšie, filter druhu, Zobraziť ďalšie, zoradenie najstaršie, zrušiť filter,
    miesto, zoradenie podľa názvu, Zobraziť ďalšie, roky s druhom bez výsledku, zrušiť; zvlášť spustenie s adresou.
    Vráti zoznam rozdielov (prázdny = správne). Linka bmb-fb-uplnost, oprava 2."""
    d = data_zoznamu(web)
    karty, naraz, n_vsetky = d['karty'], d['naraz'], len(d['karty'])
    druhy = [x for x in d['druhy'] if x]
    pocet = lambda f: sum(1 for k in karty if f(k))  # noqa: E731
    dek = lambda k: str(int(k['rok']) - int(k['rok']) % 10) if (k['rok'] or '0') != '0' else '0'  # noqa: E731
    druh_max = max(druhy, key=lambda x: pocet(lambda k: x in k['druh'].split()))
    mesto_max = max((m for m in d['mesta'] or [] if m), key=lambda m: pocet(lambda k: k['mesto'] == m), default='')
    kroky = [{'akcia': 'viac'}, {'akcia': 'viac'}, {'akcia': 'druh', 'hodnota': druh_max}, {'akcia': 'viac'},
             {'akcia': 'zoradit', 'hodnota': 'najstarsie'}, {'akcia': 'zrusit'}]
    if mesto_max:
        kroky += [{'akcia': 'mesto', 'hodnota': mesto_max}]
    kroky += [{'akcia': 'zoradit', 'hodnota': 'nazov'}, {'akcia': 'viac'}, {'akcia': 'zoradit', 'hodnota': 'najnovsie'},
              {'akcia': 'zrusit'}]
    prazdna = next(((x, r) for x in druhy for r in d['roky'] or [] if r
                    and not pocet(lambda k: x in k['druh'].split() and dek(k) == r)), None)
    if prazdna:
        kroky += [{'akcia': 'druh', 'hodnota': prazdna[0]}, {'akcia': 'roky', 'hodnota': prazdna[1]}, {'akcia': 'zrusit'}]
    chyby = []

    def porovnaj(stavy, kroky, s):
        ukazat = naraz
        for i, (st, k) in enumerate(zip(stavy, [None] + kroky)):
            bolo = ukazat
            if k and k['akcia'] == 'viac':
                ukazat += naraz
            elif k:
                ukazat = naraz
                if k['akcia'] == 'zrusit':
                    s.update(druh='', roky='', mesto='')
                else:
                    s[k['akcia']] = k['hodnota']
            vyber = [x for x in karty if (not s['druh'] or s['druh'] in x['druh'].split())
                     and (not s['roky'] or dek(x) == s['roky']) and (not s['mesto'] or x['mesto'] == s['mesto'])]
            if s['zoradit'] != 'nazov':
                vyber.sort(key=lambda x: x['poradie'], reverse=s['zoradit'] == 'najstarsie')
            n = len(vyber)
            m = min(n, ukazat) if naraz else n
            kde = f'krok {i} {k} pri {dict(s)}'
            if s['zoradit'] != 'nazov':
                ocak = [x['poradie'] for x in vyber[:m]]
                if st['viditelne'] != ocak:
                    chyby.append(f'{kde}: viditeľné {st["viditelne"]}, čakané {ocak}')
                dom = sorted((x['poradie'] for x in karty), reverse=s['zoradit'] == 'najstarsie')
                if st['poradie'] != dom:
                    chyby.append(f'{kde}: karty v DOM nie sú zoradené ({st["poradie"][:15]}…)')
            else:
                if len(st['viditelne']) != m or not set(st['viditelne']) <= {x['poradie'] for x in vyber}:
                    chyby.append(f'{kde}: pri zoradení podľa názvu {len(st["viditelne"])} viditeľných, čakané {m}')
            filtruje = s['druh'] or s['roky'] or s['mesto']
            text = (f'Vybrané: {n} z {n_vsetky}' + (f', zobrazené: {m}' if m < n else '')) if filtruje else (
                f'Zobrazené: {m} z {n}' if m < n else '')
            if st['stav'] != text:
                chyby.append(f'{kde}: stav „{st["stav"]}“, čakaný „{text}“')
            if st['viac_skryte'] != (not m < n) or st['viac_pocet'] != (f'({n - m})' if m < n else ''):
                chyby.append(f'{kde}: Zobraziť ďalšie skryté {st["viac_skryte"]} {st["viac_pocet"]}, čakané {n - m} zvyšných')
            if st['prazdne_skryte'] != (n > 0):
                chyby.append(f'{kde}: prázdny výsledok skrytý {st["prazdne_skryte"]} pri {n} kartách')
            if st['filtre_skryte']:
                chyby.append(f'{kde}: filtre ostali skryté')
            if k and k['akcia'] == 'viac' and s['zoradit'] != 'nazov':
                ocak_f = vyber[bolo]['poradie'] if bolo < n else None
                if st['fokus'] != ocak_f:
                    chyby.append(f'{kde}: fokus na karte {st["fokus"]}, čakaný {ocak_f} (prvá nová)')
            elif k and k['akcia'] != 'viac' and st['fokus'] is not None:
                chyby.append(f'{kde}: fokus sa presunul na kartu {st["fokus"]} bez Zobraziť ďalšie')
            q = '&'.join(f'{a}={s[a]}' for a in ('druh', 'roky', 'mesto') if s[a]) + (
                ('&' if filtruje else '') + f'zoradit={s["zoradit"]}' if s['zoradit'] != 'najnovsie' else '')
            if k and st['search'] != (f'?{q}' if q else ''):
                chyby.append(f'{kde}: adresa {st["search"]}, čakaná ?{q}')

    _, stavy = spravanie_zoznamu(web, kroky, app_js=app_js)
    porovnaj(stavy, kroky, {'druh': '', 'roky': '', 'mesto': '', 'zoradit': 'najnovsie'})
    # spustenie s adresou ?druh=…&zoradit=najstarsie (stav z adresy, bez zápisu)
    _, stavy = spravanie_zoznamu(web, [{'akcia': 'viac'}], search=f'?druh={druh_max}&zoradit=najstarsie', app_js=app_js)
    s = {'druh': druh_max, 'roky': '', 'mesto': '', 'zoradit': 'najstarsie'}
    porovnaj(stavy[:1], [], dict(s))
    porovnaj(stavy, [{'akcia': 'viac'}], dict(s))
    return chyby


class TestSestdesiatRealizacii(unittest.TestCase):
    """Web pri vyše 50 realizáciách (zadanie bmb-fb-uplnost bod 4) nad kópiou skutočného obsahu rozmnoženou na 60:
    stránka každej realizácie, prehľad s odkazom na každú, filter a zoradenie s dátami pre všetky, postupné
    načítanie (bez JS všetko viditeľné), pás najviac 10, sitemap, JSON-LD a presmerovania ostávajú."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-60-'))
        cls.obsah, cls.pocet = rozmnoz_obsah(cls.tmp, 60)
        cls.web = cls.tmp / 'web'
        cls.kod, cls.out, cls.err = postav(cls.web, cls.obsah, rychle=True)
        cls.slugy = sorted(json.loads(p.read_text(encoding='utf-8-sig'))['slug']
                           for p in (cls.obsah / 'realizacie').glob('*.json')
                           if json.loads(p.read_text(encoding='utf-8-sig')).get('zverejnene') is not False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_zostava_a_stranky_realizacii(self):
        self.assertEqual(self.kod, 0, self.err)
        self.assertGreaterEqual(self.pocet, 60)
        self.assertEqual(len(self.slugy), self.pocet)
        self.assertIn(f'{self.pocet} realizácií', self.out)
        for slug in self.slugy:
            self.assertTrue((self.web / 'realizacie' / slug / 'index.html').is_file(), slug)
        self.assertEqual(interne_odkazy(self.web, '/'), [])
        for p in (self.web / 'index.html', self.web / 'realizacie' / 'index.html'):
            self.assertEqual(csp_sedi(p), '', p)
            self.assertEqual(rozober(p).chyby, [], p)

    def test_prehlad_odkaz_na_kazdu_a_data_filtra(self):
        s = (self.web / 'realizacie' / 'index.html').read_text(encoding='utf-8')
        telo = s.split('<main', 1)[1]
        r = Rozbor()
        r.feed(telo)
        dlazdice = [a for t, a in r.prvky if t == 'li' and 'dlazdica' in a.get('class', '')]
        self.assertEqual(len(dlazdice), self.pocet)
        # bez JS sú všetky karty viditeľné (žiadna nemá hidden), poradie 1 až N pre zoradenie
        self.assertTrue(all('hidden' not in a for a in dlazdice))
        self.assertEqual(sorted(int(a['data-poradie']) for a in dlazdice), list(range(1, self.pocet + 1)))
        odkazy = re.findall(r'<h2 class="dlazdica-nazov"><a href="\.\./realizacie/([a-z0-9-]+)/">([^<]+)</a></h2>', telo)
        self.assertEqual(sorted(x for x, _ in odkazy), self.slugy)
        self.assertTrue(all(n.strip() for _, n in odkazy))
        # filter: každá karta má druh z tlačidiel, rok s desaťročím vo výbere rokov a mesto vo výbere miesta
        druhy_tl = set(re.findall(r'<button class="filter" type="button" data-druh="([a-z]*)"', telo))
        self.assertIn(f'data-druh="" aria-pressed="true">Všetky<span>{self.pocet}</span>', telo)
        dekady = set(re.findall(r'<option value="(\d{4}|0)">', telo.split('<select data-roky>', 1)[1].split('</select>', 1)[0]))
        mesta = dict(re.findall(r'<option value="([a-z0-9-]+)">[^<]+ \((\d+)\)</option>', telo.split('<select data-mesto>', 1)[1].split('</select>', 1)[0]))
        for a in dlazdice:
            self.assertTrue(a['data-druh'].split(), a)
            self.assertTrue(set(a['data-druh'].split()) <= druhy_tl, a)
            rok = int(a['data-rok'])
            self.assertIn(str(rok - rok % 10) if rok else '0', dekady, a)
            if a['data-mesto']:
                self.assertIn(a['data-mesto'], mesta, a)
        for kluc, n in mesta.items():
            self.assertEqual(sum(1 for a in dlazdice if a['data-mesto'] == kluc), int(n), kluc)
        # zoradenie: od najnovších, od najstarších a podľa názvu; postupné načítanie po data-naraz kartách
        self.assertIn('<option value="nazov">abecedne</option>', telo)
        self.assertIn(f'data-zoznam data-naraz="{build.REALIZACIE_NARAZ}">', telo)
        self.assertLess(build.REALIZACIE_NARAZ, self.pocet)
        self.assertIn('<p class="viac-obal" data-viac-obal hidden>', telo)
        self.assertIn('aria-live="polite"', telo.split('data-stav', 1)[1][:40])
        # obrázky kariet: len prvá (LCP) bez lazy, ostatné lazy so srcset a rozmermi
        imgs = re.findall(r'<img[^>]*>', telo.split('<ul class="karty karty-zoznam"', 1)[1].split('</ul>', 1)[0])
        self.assertEqual(sum(1 for i in imgs if 'fetchpriority="high"' in i), 1)
        self.assertTrue(all('loading="lazy"' in i for i in imgs if 'fetchpriority="high"' not in i))
        # JSON-LD ItemList so všetkými realizáciami
        zoznam = [u for u in stranka_info(self.web / 'realizacie' / 'index.html')['ld'] if u.get('@type') == 'ItemList'][0]
        self.assertEqual(len(zoznam['itemListElement']), self.pocet)

    def test_postupne_nacitanie_spravanie(self):
        """app.js časť 5 nad falošným DOM (nastroje/zoznam_spravanie.cjs): 12 viditeľných, po kliknutí 24 a fokus
        na 13. karte, pri filtri znova 12, zoradenie, prázdny výsledok, stav a adresa. Sabotáže ukazat += 0,
        vidno = ok a naraz = 0 musia zlyhať (fb/uplnost/oprava2_sabotaz_appjs.py, linka bmb-fb-uplnost, oprava 2)."""
        self.assertGreater(self.pocet, 2 * build.REALIZACIE_NARAZ)
        self.assertEqual(chyby_zoznamu(self.web), [])

    def test_postupne_nacitanie_spravanie_skutocny(self):
        """To isté nad skutočným obsahom (dnes 28 realizácií: 12, 24, 28)."""
        self.assertEqual(chyby_zoznamu(skutocny()['ostry']), [])

    def test_uvod_bez_pasu_pri_mnohych(self):
        # úvod nikdy neukazuje všetky stavby: od ÚVOD v3 (3A.6) žiadny pás, len slider; všetky sú v zozname
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="prace"', uvod)
        self.assertNotIn('<li class="projekt">', uvod)
        self.assertGreaterEqual(uvod.count('<li class="snimka snimka-'), 1)
        zoznam = (self.web / 'realizacie' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(zoznam.count('<li class="dlazdica'), self.pocet)

    def test_sitemap_presmerovania_a_ld(self):
        mapa = (self.web / 'sitemap.xml').read_text(encoding='utf-8')
        for slug in self.slugy:
            self.assertIn(f'<loc>https://bmbuilding.sk/realizacie/{slug}/</loc>', mapa)
        d = json.loads((KOREN / 'sablona' / 'presmerovania.json').read_text(encoding='utf-8'))
        stare = [z['stara'] for z in d.get('presmerovania', []) if re.match(r'^/[A-Za-z0-9._/-]+$', str(z.get('stara') or ''))]
        self.assertTrue(stare)
        for stara in stare:
            p = self.web / stara.strip('/') / 'index.html'
            self.assertTrue(p.is_file(), stara)
        for slug in self.slugy[::7]:
            info = stranka_info(self.web / 'realizacie' / slug / 'index.html')
            self.assertTrue(info['ld'], slug)
            self.assertIn(f'<link rel="canonical" href="https://bmbuilding.sk/realizacie/{slug}/">', info['html'])



# originály osvedčení s rodným číslom (len na D:, nikdy na web); test porovná hash, ak existujú
ORIGINALY_OSVEDCENI = [Path('D:/ARLing-media/klienti/bmbuilding/opravnenia'),
                       Path('D:/ARLing-media/klienti/bmbuilding/stary-web-obrazky/opravnenia')]
# pás zakrytia (rodné číslo, narodený/á) v súradniciach zdroja: jednofarebná plocha (fb/uplnost/web-50.md)
ZAKRYTIE = {'osvedcenie-sksi-stavbyveduci-2003': (218, 309, 312, 330),
            'osvedcenie-sksi-stavebny-dozor-2010': (225, 307, 300, 323)}


def zakrytie_v_oreze(meno):
    """ZAKRYTIE v súradniciach skenu orezaného na hranu papiera (build.OREZ_DOKLADU, brána bmb-presnost pokus 2)."""
    l, t = build.OREZ_DOKLADU.get(meno, (0, 0))[:2]
    x0, y0, x1, y1 = ZAKRYTIE[meno]
    return (x0 - l, y0 - t, x1 - l, y1 - t)


class TestOpravnenia(unittest.TestCase):
    """Stránka Oprávnenia so zakrytými osvedčeniami SKSI (zadanie bmb-fb-uplnost, Navyše; FAKTY-WEB.md)."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry = s['ostry']
        cls.info = stranka_info(cls.ostry / 'opravnenia' / 'index.html')

    def test_dva_obrazky_veta_a_lightbox(self):
        telo = self.info['telo']
        self.assertEqual(telo.count('<figure class="doklad-obr">'), 2)
        text = html.unescape(re.sub(r'<[^>]+>', ' ', telo)).replace('\u00a0', ' ')
        self.assertIn('Rodné číslo a dátum narodenia sú zakryté kvôli ochrane osobných údajov.', text)
        # veta je pod obrázkami (za mriežkou dokladov)
        poznamka = telo.index('<p class="poznamka">Rodné číslo')
        self.assertGreater(poznamka, telo.rindex('<figure class="doklad-obr">'))
        r = Rozbor()
        r.feed(telo)
        odkazy = [a for t, a in r.prvky if t == 'a' and 'data-lightbox' in a]
        self.assertEqual(len(odkazy), 2)
        for i, a in enumerate(odkazy, 1):
            self.assertEqual(a['data-galeria'], 'osvedcenia')
            self.assertEqual(a['data-index'], str(i))
            self.assertEqual(a['href'], a['data-plna'])
            self.assertTrue(a['data-alt'])
        dialog = [a for t, a in r.prvky if t == 'dialog']
        self.assertEqual(len(dialog), 1)
        self.assertEqual(dialog[0]['id'], 'lightbox')
        self.assertEqual(dialog[0]['data-slovo'], 'Osvedčenie')
        # popis každého podľa FAKTY-WEB.md: činnosť, odborné zameranie, evidenčné číslo, dátum vydania doslovne
        for kus in ('stavbyvedúci', 'pozemné stavby', '00912*10*', '5. 11. 2003', 'stavebný dozor', '09903', '10. 6. 2010',
                    'Osvedčenie o vykonaní odbornej skúšky', 'Osvedčenie o vykonaní skúšky odbornej spôsobilosti'):
            self.assertIn(kus, text)
        # doslovné popisy podľa zadania bmb-fotky (časť 3), pod obrázkom každého osvedčenia ako figcaption
        popisy = ('Osvedčenie SKSI o vykonaní odbornej skúšky: stavbyvedúci, pozemné stavby, evidenčné číslo 00912*10*, vydané 5. 11. 2003',
                  'Osvedčenie SKSI o vykonaní skúšky odbornej spôsobilosti: stavebný dozor, evidenčné číslo 09903, vydané 10. 6. 2010')
        figcaption = [html.unescape(re.sub(r'<[^>]+>', '', c)).replace(' ', ' ')
                      for c in re.findall(r'<figcaption class="doklad-popis">(.*?)</figcaption>', telo, re.S)]
        self.assertEqual(figcaption, list(popisy))
        for fig in telo.split('<figure class="doklad-obr">')[1:]:
            self.assertLess(fig.index('<img'), fig.index('<figcaption'))
            self.assertLess(fig.index('<figcaption'), fig.index('</figure>'))
        imgs = [a for t, a in r.prvky if t == 'img' and 'osvedcenie-sksi' in a.get('src', '') and 'lightbox-foto' not in a.get('class', '')]
        self.assertEqual(len(imgs), 2)
        for a in imgs:
            self.assertIn('SKSI', a['alt'])
            self.assertIn('osobné údaje zakryté', a['alt'])
        self.assertEqual(csp_sedi(self.ostry / 'opravnenia' / 'index.html'), '')
        # stránky služieb majú doklady bez obrázkov
        for k in ('stavebny-dozor', 'stavebny-manazment'):
            p = self.ostry / k / 'index.html'
            if p.is_file():
                self.assertNotIn('<figure class="doklad-obr"', p.read_text(encoding='utf-8'))

    def test_subory_len_zmensene_a_zakryte(self):
        vystup = sorted((self.ostry / 'opravnenia').glob('*.webp'))
        # 360, 720, sken a 2-násobok skenu pre každé z dvoch osvedčení (linka bmb-presnost)
        self.assertEqual(len(vystup), 8)
        self.assertFalse(list(self.ostry.rglob('osvedcenie-*.png')))
        for p in vystup:
            meno, sirka = p.stem.rsplit('-', 1)
            zdroj = KOREN / 'sablona' / 'opravnenia' / f'{meno}.png'
            # porovnáva sa so skenom orezaným na hranu papiera (build.OREZ_DOKLADU), zakrytie v jeho súradniciach
            z = build.sken_dokladu(zdroj)
            with Image.open(p) as o:
                # najviac 2-násobok skenu, pri užšom skene 2-násobok vnútra rámu 528 px (Lanczos a jemné doostrenie,
                # bez AI), pomer strán zachovaný
                self.assertLessEqual(o.width, 2 * max(z.width, 528))
                self.assertLessEqual(o.height, round(z.height * 2 * max(z.width, 528) / z.width) + 1)
                self.assertEqual(o.width, int(sirka))
                self.assertAlmostEqual(o.height / o.width, z.height / z.width, places=2)
                if o.width >= z.width:
                    # pás zakrytia ostal jednofarebný (žiadne čitateľné rodné číslo ani dátum narodenia), aj zväčšený
                    k = o.width / z.width
                    box = [round(x * k) for x in zakrytie_v_oreze(meno)]
                    m = round(3 * k)
                    vyrez = o.convert('L').crop((box[0] + m, box[1] + m, box[2] - m, box[3] - m))
                    self.assertLess(max(vyrez.getextrema()) - min(vyrez.getextrema()), 24, p)
            # zdroj v šablóne je zakrytá verzia z ops (rovnaké bajty), nie originál
            zakryta = KOREN.parent.parent / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding' / 'opravnenia' / f'{meno}.png'
            if zakryta.is_file():
                self.assertEqual(hashlib.sha256(zdroj.read_bytes()).digest(), hashlib.sha256(zakryta.read_bytes()).digest())

    def test_odkazy_na_register_sksi(self):
        """Odkaz „Overiť v registri SKSI“ pri každom osvedčení: len na verejnyportal.sksi.sk (Verejný portál SKSI, adresy
        otvorené 21. 11. 2026, opravnenia/NAVRH.md), s rel noopener a aria-label začínajúcim viditeľným textom."""
        r = Rozbor()
        r.feed(self.info['telo'])
        odkazy = [a for t, a in r.prvky if t == 'a' and urllib.parse.urlsplit(a.get('href', '')).hostname == 'verejnyportal.sksi.sk']
        self.assertEqual(len(odkazy), 2)
        for a in odkazy:
            self.assertTrue(a['href'].startswith('https://verejnyportal.sksi.sk/search/'))
            self.assertIn('noopener', a.get('rel', ''))
            self.assertTrue(a['aria-label'].startswith('Overiť v registri SKSI'), a)
        self.assertEqual(len({a['href'] for a in odkazy}), 2)
        self.assertEqual(len({a['aria-label'] for a in odkazy}), 2)
        text = html.unescape(re.sub(r'<[^>]+>', ' ', self.info['telo'])).replace(' ', ' ')
        self.assertEqual(text.count('Overiť v registri SKSI'), 2)
        # adresa nesie len priezvisko a evidenčné číslo, nikdy rodné číslo ani dátum narodenia
        for a in odkazy:
            kriteria = json.loads(base64.b64decode(a['href'].rsplit('/', 1)[1] + '=='))
            self.assertEqual(sorted(kriteria), sorted(['fo_last', 'front_evc', 'qual', 'codes', 'front_order', 'front_order_type']))
            self.assertEqual(kriteria['fo_last'], 'Bulko')
        evc = sorted(json.loads(base64.b64decode(a['href'].rsplit('/', 1)[1] + '=='))['front_evc'] for a in odkazy)
        self.assertEqual(evc, ['00912', '09903'])
        # odkaz je len na stránke Oprávnenia a len pri osvedčení s obrázkom (služby majú doklady bez obrázkov)
        for p in stranky(self.ostry):
            if p.relative_to(self.ostry).as_posix() != 'opravnenia/index.html':
                self.assertNotIn('verejnyportal.sksi.sk', p.read_text(encoding='utf-8'), p)
        self.assertEqual(csp_sedi(self.ostry / 'opravnenia' / 'index.html'), '')

    def test_register_len_na_hostitela_sksi(self):
        """Cudzí hostiteľ, adresa bez https alebo chýbajúci kľúč: odkaz sa nezobrazí (nastavenia.json opravnenia_register)."""
        w = build.Web(KOREN / 'obsah', False)
        w.nastavenia['opravnenia_register'] = {'00912*10*': 'https://example.com/search/abc', '09903': 'http://verejnyportal.sksi.sk/search/abc'}
        html_ = ''.join(w.doklady('', obrazky=True))
        self.assertNotIn('Overiť v registri', html_)
        w.nastavenia['opravnenia_register'] = {'09903': 'https://verejnyportal.sksi.sk/search/abc'}
        html_ = ''.join(w.doklady('', obrazky=True)).replace(' ', ' ')
        self.assertEqual(html_.count('Overiť v registri SKSI'), 2)  # viditeľný text a aria-label jedného odkazu
        self.assertEqual(html_.count('<a class="odkaz-von"'), 1)
        w.nastavenia.pop('opravnenia_register')
        self.assertNotIn('Overiť v registri', ''.join(w.doklady('', obrazky=True)))

    def test_na_webe_su_len_zakryte_osvedcenia_sha256(self):
        """Zdroje osvedčení v sablona/opravnenia/ majú pevné sha256 zakrytých verzií (rodné číslo a dátum narodenia
        zakryté); web má len ich WebP (zmenšené alebo bez AI zväčšené), nikdy PNG ani iný súbor osvedčenia."""
        zakryte = {'osvedcenie-sksi-stavbyveduci-2003.png': '2b200e6cc0b4d62113b707872a0684fb8104920de958d560c37ea099c77c2d61',
                   'osvedcenie-sksi-stavebny-dozor-2010.png': '366b8fc9cce86860f0e6ad7dedd61d6c2b858d9e9b25b3a97e290edfdf5c9696'}
        zdroje = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (KOREN / 'sablona' / 'opravnenia').iterdir()}
        self.assertEqual(zdroje, zakryte)
        for hodnota in zakryte.values():
            self.assertEqual(len(hodnota), 64)
        # obsah webu: iba opravnenia/<meno>-<šírka>.webp z týchto dvoch mien
        subory = sorted(p.name for p in (self.ostry / 'opravnenia').iterdir() if p.name != 'index.html')
        self.assertEqual(len(subory), 8)
        for n in subory:
            self.assertRegex(n, r'^osvedcenie-sksi-(stavbyveduci-2003|stavebny-dozor-2010)-\d+\.webp$')
        # nikde na webe nie je bajt po bajte súbor s týmto sha256 ani nezakrytý originál
        for p in self.ostry.rglob('*'):
            if p.is_file() and p.suffix.lower() in ('.png', '.jpg', '.webp', '.avif'):
                self.assertNotIn(hashlib.sha256(p.read_bytes()).hexdigest(), zakryte.values(), p)
        # na webe nie je rodné číslo (vzor šesť číslic, lomka) ani dátum narodenia mimo vety o zakrytí
        for p in stranky(self.ostry):
            s = html.unescape(re.sub(r'<[^>]+>', ' ', p.read_text(encoding='utf-8'))).replace(' ', ' ')
            self.assertIsNone(re.search(r'\b\d{6}\s*/\s*\d{3,4}\b', s), p)
            s_bez_vety = s.replace('Rodné číslo a dátum narodenia sú zakryté kvôli ochrane osobných údajov.', '')
            self.assertNotIn('narodenia', s_bez_vety, p)
            self.assertNotIn('Rodné číslo', s_bez_vety, p)

    def test_ziadny_subor_nie_je_original(self):
        originaly = set()
        for d in ORIGINALY_OSVEDCENI:
            if d.is_dir():
                originaly |= {hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir() if p.is_file()}
        if not originaly:
            self.skipTest('originály na D: nie sú dostupné')
        for d in (self.ostry, KOREN / 'sablona'):
            for p in d.rglob('*'):
                if p.is_file() and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
                    self.assertNotIn(hashlib.sha256(p.read_bytes()).hexdigest(), originaly, p)

    def test_orez_skenov_na_hranu_papiera(self):
        """Brána bmb-presnost pokus 2, nález 1: oba skeny orezané na hranu papiera s pomerom strán do 0,002 pod vnútrom
        rámu, takže pri object-fit cover vypĺňa papier oboch osvedčení celé vnútro rámu (rovnaká horná hrana aj výška
        pri každej šírke). Orez odstráni len biely okraj skenera a prázdny papier (bez ornamentu a písma)."""
        import numpy as np
        mapa = build.Web(KOREN / 'obsah', False).nastavenia['opravnenia_obrazky']
        mena = sorted(Path(v).stem for v in mapa.values())
        self.assertEqual(sorted(build.OREZ_DOKLADU), mena)
        pomery = []
        for meno in mena:
            zdroj = KOREN / 'sablona' / 'opravnenia' / f'{meno}.png'
            l, t, r, b = build.OREZ_DOKLADU[meno]
            z = build.sken_dokladu(zdroj)
            self.assertEqual(z.size, (r - l, b - t))
            pomer = z.width / z.height
            pomery.append(pomer)
            self.assertLessEqual(pomer, build.POMER_RAMU_DOKLADU)
            self.assertGreaterEqual(pomer, build.POMER_RAMU_DOKLADU - 0.002)
            # cover pri vnútri rámu 322 (768 px), 332 (390 px), 438 (1024 px) až 528 x 757,51 CSS px (1440 a 1920 px):
            # odreže najviac 1,5 % papiera
            for ram_w in (322, 332, 438, 528):
                ram_h = (ram_w + 26) * 297 / 210 - 26
                s = max(ram_w / z.width, ram_h / z.height)
                self.assertLessEqual(1 - ram_w / (z.width * s), 0.015, (meno, ram_w))
                self.assertLessEqual(1 - ram_h / (z.height * s), 0.015, (meno, ram_w))
            # obvod výrezu je papier, nie biely okraj skenera
            rgb = np.asarray(z, dtype=np.int32).sum(2)
            obvod = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
            self.assertLess(float((obvod >= 735).mean()), 0.01, meno)
            # odstránená plocha je biela alebo prázdny papier: najtmavší px zeleného ornamentu pri hranách výrezu má
            # jas 85 až 139 (presnost/meranie/oprava2-orez.json), odstránená plocha 192 a 210
            jas = np.asarray(Image.open(zdroj).convert('L'), dtype=np.float64)
            maska = np.ones_like(jas, dtype=bool)
            maska[t:b, l:r] = False
            self.assertGreaterEqual(float(jas[maska].min()), 180, meno)
            self.assertGreaterEqual(float(np.percentile(jas[maska], 1)), 200, meno)
            # atribúty img a data-sirka sú rozmer výrezu
            telo = self.info['telo']
            self.assertIn(f'width="{z.width}" height="{z.height}"', telo)
            self.assertIn(f'data-sirka="{z.width}" data-vyska="{z.height}"', telo)
        self.assertLess(abs(pomery[0] - pomery[1]), 0.002)
        # sabotáž: bez orezu má pravý sken pomer 0,716, mimo tolerancie (contain by dal biely pás 9 px)
        with Image.open(KOREN / 'sablona' / 'opravnenia' / 'osvedcenie-sksi-stavebny-dozor-2010.png') as im:
            self.assertGreater(im.width / im.height - build.POMER_RAMU_DOKLADU, 0.002)
        povodne = dict(build.OREZ_DOKLADU)
        try:
            build.OREZ_DOKLADU['osvedcenie-sksi-stavebny-dozor-2010'] = (2, 3, 517, 725)
            with self.assertRaises(build.ChybaZostavy):
                build.sken_dokladu(KOREN / 'sablona' / 'opravnenia' / 'osvedcenie-sksi-stavebny-dozor-2010.png')
        finally:
            build.OREZ_DOKLADU.clear()
            build.OREZ_DOKLADU.update(povodne)


# ---------------------------------------------------------------- linka bmb-presnost: zarovnanie s nulovou toleranciou

def css_bloky(css, hlavicka):
    """Telá všetkých blokov „<hlavicka>{...}“ (napr. @supports (grid-template-rows:subgrid)) aj s vnorenými blokmi."""
    out = []
    i = 0
    while True:
        j = css.find(hlavicka + '{', i)
        if j < 0:
            return out
        k = j + len(hlavicka) + 1
        hlbka = 1
        while hlbka:
            hlbka += {'{': 1, '}': -1}.get(css[k], 0)
            k += 1
        out.append(css[j + len(hlavicka) + 1:k - 1])
        i = k


def css_bez_blokov(css):
    """Štýl bez @media a @supports (len pravidlá, ktoré platia pri každej šírke a v každom prehliadači)."""
    out = []
    i = 0
    while True:
        m = re.compile(r'@(?:media|supports|container|keyframes)\b').search(css, i)
        if not m:
            out.append(css[i:])
            return ''.join(out)
        out.append(css[i:m.start()])
        k = css.index('{', m.start()) + 1
        hlbka = 1
        while hlbka:
            hlbka += {'{': 1, '}': -1}.get(css[k], 0)
            k += 1
        i = k


def deklaracie(css, selektor):
    """Deklarácie pravidiel s presne týmto selektorom (nie v zozname selektorov); neskoršie prepíše skoršie."""
    out = {}
    for m in re.finditer(r'(?:^|[{};\n])\s*' + re.escape(selektor) + r'\{([^{}]*)\}', css):
        for d in m.group(1).split(';'):
            if ':' in d:
                k, v = d.split(':', 1)
                out[k.strip()] = v.strip()
    return out


class CastiKariet(HTMLParser):
    """Triedy priamych detí figcaption v kartách (li.projekt): poradie častí karty, ktoré CSS kladie do riadkov subgrid."""

    def __init__(self, trieda_karty):
        super().__init__()
        self.trieda = trieda_karty
        self.zasobnik = []
        self.karty = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        triedy = (a.get('class') or '').split()
        if self.zasobnik and self.zasobnik[-1][0] == 'figcaption' and self.zasobnik[-1][2]:
            self.karty[-1].append(triedy[0] if triedy else tag)
        v_karte = tag == 'figcaption' and any(t == 'li' and self.trieda in tr for t, tr, _ in self.zasobnik)
        if tag == 'figcaption' and v_karte:
            self.karty.append([])
        if tag not in VOID:
            self.zasobnik.append((tag, triedy, v_karte))

    def handle_startendtag(self, tag, attrs):
        if self.zasobnik and self.zasobnik[-1][0] == 'figcaption' and self.zasobnik[-1][2]:
            triedy = (dict(attrs).get('class') or '').split()
            self.karty[-1].append(triedy[0] if triedy else tag)

    def handle_endtag(self, tag):
        for i in range(len(self.zasobnik) - 1, -1, -1):
            if self.zasobnik[i][0] == tag:
                del self.zasobnik[i:]
                break


class TestZarovnanie(unittest.TestCase):
    """Linka bmb-presnost, časť 1 Oprava (zadanie ops/druhy-ucet/gmail/zadanie-bmb-presnost.md): osvedčenia v rámoch
    A4 a v spoločných riadkoch, karty pásu a mriežok v spoločných riadkoch (subgrid), text vedľa textu na základnej
    čiare, jedna škála medzier. Meranie v prehliadači: ops/weby-na-mieru/klienti/bmbuilding/presnost/oprava-zarovnanie.md
    (0 rozdielov nad 1 px v Chrome aj Firefoxe pri 5 šírkach); tieto testy strážia štruktúru a pravidlá, z ktorých to
    vychádza, bez prehliadača."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry = s['ostry']
        cls.css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        cls.zaklad = css_bez_blokov(cls.css)
        cls.subgrid = '\n'.join(css_bloky(cls.css, '@supports (grid-template-rows:subgrid)'))

    def telo(self, cesta):
        return stranka_info(self.ostry / cesta)['telo']

    def test_osvedcenia_rovnake_riadky_tabuliek(self):
        telo = self.telo('opravnenia/index.html')
        doklady = re.findall(r'<div class="(doklad[^"]*)"><h2 class="doklad-h">(.*?)</dl>(.*?)</div>', telo, re.S)
        self.assertEqual(len(doklady), 2)
        cisty = lambda s: html.unescape(s).replace(' ', ' ')  # noqa: E731
        stitky = [[cisty(x) for x in re.findall(r'<dt>(.*?)</dt>', d)] for _, d, _ in doklady]
        hodnoty = [[cisty(re.sub(r'<[^>]+>', '', x)) for x in re.findall(r'<dd>(.*?)</dd>', d)] for _, d, _ in doklady]
        # rovnaké riadky v rovnakom poradí v oboch tabuľkách (bez toho mala ľavá 7 a pravá 6 riadkov)
        self.assertEqual(stitky[0], ['Vydala', 'Doklad', 'Meno', 'Činnosť', 'Odborné zameranie', 'Evidenčné číslo', 'Vydané'])
        self.assertEqual(stitky[0], stitky[1])
        self.assertEqual(hodnoty[0][4], 'pozemné stavby')
        # osvedčenie stavebného dozoru 2010 odborné zameranie neuvádza (zakrytý sken v sablona/opravnenia/)
        self.assertEqual(hodnoty[1][4], 'v osvedčení neuvedené')
        for trieda, d, za in doklady:
            self.assertEqual(trieda, f'doklad riadkov-{len(stitky[0])}')
            # poradie častí = poradie riadkov mriežky: nadpis, rám s obrázkom, popis, tabuľka, odkaz do registra
            self.assertRegex(d, r'(?s)^[^<]*</h2><figure class="doklad-obr"><a class="doklad-odkaz"[^>]*>(<picture>.*?</picture>|<img [^>]*>)</a>'
                                r'<figcaption class="doklad-popis">.*?</figcaption></figure><dl class="fakty">')
            self.assertTrue(za.startswith('<p class="doklad-register">'), za[:80])
        # stránka služby: jedno osvedčenie bez obrázka, bez triedy riadkov a bez doplneného riadka
        sluzba = self.telo('stavebny-dozor/index.html')
        self.assertIn('<div class="doklad"><h2 class="doklad-h">', sluzba)
        self.assertNotIn('riadkov-', sluzba)
        self.assertNotIn('neuvedené', html.unescape(sluzba).replace(' ', ' '))
        self.assertEqual(csp_sedi(self.ostry / 'opravnenia' / 'index.html'), '')

    def test_osvedcenia_ram_a4_a_spolocne_riadky(self):
        # rám s rovnakým pomerom strán (A4) a šírkou stĺpca; sken orezaný na hranu papiera vypĺňa vnútro rámu (cover,
        # brána pokus 2 nález 1: contain dal skenom s iným pomerom strán iný biely pás a papier v inej výške)
        odkaz = deklaracie(self.zaklad, '.doklad-odkaz')
        self.assertEqual(odkaz.get('aspect-ratio'), '210/297')
        self.assertEqual(odkaz.get('width'), '100%')
        self.assertEqual(odkaz.get('display'), 'block')
        self.assertEqual(odkaz.get('background'), '#fff')
        img = deklaracie(self.zaklad, '.doklad-odkaz img')
        self.assertEqual(img.get('object-fit'), 'cover')
        self.assertEqual(img.get('position'), 'absolute')
        self.assertEqual(img.get('height'), 'calc(100% - 2 * var(--m-12))')
        self.assertNotIn('auto', img.get('height', 'auto'))
        # rám najviac 554 px: 528 px skenu + 2 x 12 px okraj + 2 x 1 px čiara, sken 528 px sa zobrazí 1 : 1
        self.assertEqual(deklaracie(self.zaklad, '.doklady').get('--doklad-max'), '554px')
        # nadpis, rám, popis, každý riadok tabuľky a odkaz v spoločných riadkoch oboch stĺpcov (subgrid od 640 px)
        sg = ''.join(css_bloky(self.subgrid, '@media (min-width:640px)'))
        self.assertIn('.doklady>.doklad{display:grid;grid-template-rows:subgrid;', sg)
        for n in range(1, 8):
            self.assertIn(f'.doklady>.riadkov-{n}{{grid-row:span {n + 4}}}', sg)
        self.assertIn('.doklady>.doklad>.doklad-h{grid-row:1}', sg)
        self.assertIn('.doklady>.doklad>.doklad-obr{display:grid;grid-template-rows:subgrid;grid-row:2/span 2}', sg)
        self.assertIn('.doklady>.doklad>.fakty{display:grid;grid-template-rows:subgrid;grid-row:4/-2}', sg)
        self.assertIn('.doklady>.doklad>.doklad-register{grid-row:-2}', sg)
        # medzera medzi riadkami dokladov nie je row-gap (zdedil by sa medzi časti dokladu)
        self.assertIn('.doklady{row-gap:0;', sg)

    def test_karty_casti_v_poradi_riadkov(self):
        # časti karty idú v pevnom poradí; CSS ich kladie do riadkov subgrid (kóta 1, nadpis 2, údaje -3, rola -2)
        pripady = (('realizacie/index.html', 'dlazdica', ['kota', 'dlazdica-nazov'], ['projekt-udaje', 'projekt-text']),
                   ('stavebny-dozor/index.html', 'dlazdica', ['kota', 'dlazdica-nazov'], ['projekt-udaje', 'projekt-text']),
                   ('novinky/index.html', 'novinka-karta', ['kota', 'novinka-nazov'], ['projekt-ilustr', 'projekt-udaje', 'projekt-text']))
        ostry_n = skutocny_novinky()['ostry']
        for cesta, trieda, zaciatok, volitelne in pripady:
            r = CastiKariet(trieda)
            r.feed(stranka_info(ostry_n / cesta)['telo'])
            self.assertGreaterEqual(len(r.karty), 2, cesta)
            for casti in r.karty:
                self.assertEqual(casti[:2], zaciatok, (cesta, casti))
                zvysok = casti[2:]
                self.assertEqual(zvysok, [c for c in volitelne if c in zvysok], (cesta, casti))
                # riadkov subgrid je dosť pre všetky časti: fotka + kóta + nadpis + voliteľné
                self.assertLessEqual(len(casti), 2 + len(volitelne))
        # novinka s ilustračnou fotkou aj stavbou a úryvkom (taký pár skutočný obsah dnes nemá): všetkých 5 častí
        # v poradí riadkov, popis ilustračnej fotky v 3. riadku pred stavbou
        w = skutocny_novinky()['web']
        n = next((x for x in w.novinky if not x['karta'] and x.get('ilustracna_foto')), None)
        self.assertIsNotNone(n)
        r = CastiKariet('novinka-karta')
        with redirect_stderr(io.StringIO()):
            r.feed(w.novinka_karta('', dict(n, realizacia=w.realizacie[0]['slug'], text='Krátky úryvok novinky.')))
        self.assertEqual(r.karty, [['kota', 'novinka-nazov', 'projekt-ilustr', 'projekt-udaje', 'projekt-text']])

    def test_karty_v_spolocnych_riadkoch_subgrid(self):
        sg = self.subgrid
        # svetlé karty: 5 riadkov (fotka, kóta, nadpis, údaje, rola), novinky 6 (navyše popis ilustračnej fotky)
        for kus in ('.karty{row-gap:0;margin-bottom:calc(-1 * var(--m-48))}',
                    '.karty>.projekt{display:grid;grid-template-rows:subgrid;grid-row:span 5;margin-bottom:var(--m-48)}',
                    '.karty>.novinka-karta{grid-row:span 6}',
                    '.karty>.projekt>figure{display:grid;grid-template-rows:subgrid;grid-row:1/-1}',
                    '.karty>.projekt figcaption{display:grid;grid-template-rows:subgrid;grid-row:2/-1}',
                    '.karty figcaption>.projekt-ilustr{grid-row:3}', '.karty figcaption>.projekt-udaje{grid-row:-3}',
                    '.karty figcaption>.projekt-text{grid-row:-2}'):
            self.assertIn(kus, sg)
        # tmavý pás je z úvodu preč (ZMENY-KOLO-1.md 3A.6): žiadne pravidlá pásu ani pripnutého pásu
        self.assertNotIn('.pas', sg)
        self.assertNotIn('.prace', self.zaklad)
        # content-visibility zapína layout containment a karta by prestala byť subgrid
        self.assertNotRegex(self.css, r'\.dlazdica[^{}]*\{[^}]*content-visibility')

    def test_text_vedla_textu_na_zakladnej_ciare(self):
        # pravidlá pri každej šírke (mimo @media): štítok a hodnota, číslo činnosti a text, názov a cena
        self.assertEqual(deklaracie(self.zaklad, '.fakty>div').get('align-items'), 'baseline')
        self.assertEqual(deklaracie(self.zaklad, '.sluzba').get('align-items'), 'baseline')
        # kratší sused sa natiahne na výšku dlhšieho, voľné miesto ide pod text
        self.assertEqual(deklaracie(self.zaklad, '.susedia a').get('align-content'), 'start')
        # popis pod číslom v cenníku sa zalamuje (pri 1024 px pretiekol okraj stránky o 6 px)
        self.assertEqual(deklaracie(self.zaklad, '.cena small').get('white-space'), 'normal')

    def test_hlava_sluzby_a_detailu(self):
        # overenie-1 (drobné): stránka služby má menší H1 (2 riadky pri 1440 px, nie 3 až 4), titulok detailu
        # realizácie je dole v riadku hlavy, takže pri dlhých údajoch vpravo nie je diera medzi titulkom a fotkou
        for k in ('stavebny-dozor', 'stavbyveduci', 'stavebny-manazment'):
            self.assertIn('<div class="hlava-stranky hlava-sluzba">', self.telo(f'{k}/index.html'), k)
        self.assertNotIn('hlava-sluzba', self.telo('kontakt/index.html'))
        self.assertEqual(deklaracie(self.zaklad, '.hlava-sluzba .nadpis-stranky').get('font-size'), 'clamp(2.4rem,4.6vw,4.75rem)')
        pc = ''.join(css_bloky(self.css, '@media (min-width:1024px)'))
        self.assertIn('.hlava-detail .nadpis-stranky{grid-column:1/8;align-self:end}', pc)

    def test_kota_detailu_na_hrane_fotky(self):
        pc = ''.join(css_bloky(self.css, '@media (min-width:1024px)'))
        self.assertIn('.detail:not(.detail-bez-fotky) .detail-text>.kota:first-child{margin-top:calc(-1 * var(--m-8))}', pc)
        # údaje v hlave detailu a text pod nimi s jednou ľavou hranou (9. stĺpec ako bok ostatných podstránok)
        self.assertIn('.detail .detail-text{grid-column:9/-1}', pc)
        self.assertIn('.hlava-detail .hlava-bok{grid-column:9/-1;', pc)
        # kóta je 16 px vysoká s čiarou v polovici: posun o 8 px dá čiaru na hornú hranu fotky
        kota = deklaracie(self.zaklad, '.kota')
        self.assertEqual(kota.get('min-height'), '16px')
        self.assertIn('0 50%/100% 1px', kota.get('background', ''))
        telo = self.telo('realizacie/novostavba-rd-nitrianske-hrnciarovce/index.html')
        self.assertIn('<div class="detail-text atelier-text"><span class="kota', telo)

    def test_jedna_skala_medzier(self):
        root = deklaracie(self.zaklad, ':root')
        self.assertEqual({k: v for k, v in root.items() if k.startswith('--m-')},
                         {f'--m-{n}': f'{n}px' for n in (4, 8, 12, 16, 24, 32, 48, 64, 96)})
        # súčiastky, kde meranie našlo nerovnaké medzery: margin, padding a gap len zo škály (alebo 0, auto, %)
        selektory = ('.fakty>div', '.sluzba-doslova .sluzba-ikona', '.sluzba', '.cena small', '.susedia a', '.doklady', '.doklady .doklad-h',
                     '.doklad-obr', '.doklad-popis', '.doklad-register', '.projekt-riadok', '.projekt-udaje',
                     '.projekt-text', '.projekt-svetly .kota', '.projekt-svetly .dlazdica-nazov,.projekt-svetly .novinka-nazov',
                     '.projekt-svetly .projekt-text', '.projekt-ilustr', '.sekcia-karty .karty', '.cinnost-karty', '.cinnost-karta',
                     '.firma-fakty', '.mapa-spodok', '.vizitka')
        for sel in selektory:
            d = deklaracie(self.zaklad, sel)
            self.assertTrue(d, sel)
            for k, v in d.items():
                if k.split('-')[0] in ('margin', 'padding', 'gap', 'row', 'column') and k != 'grid-template-columns':
                    self.assertNotRegex(v, r'(?<![\w-])\d+(\.\d+)?px', (sel, k, v))
        # vizitka a spodok s tlačidlom Navigovať majú jeden vnútorný okraj (logo a tlačidlo na jednej ľavej hrane)
        self.assertEqual(deklaracie(self.zaklad, '.vizitka').get('padding'), 'var(--karta-okraj)')
        self.assertTrue(deklaracie(self.zaklad, '.mapa-spodok').get('padding', '').startswith('0 var(--karta-okraj) '))

    def test_karta_kontaktu_na_verzalke_nadpisu(self):
        # brána pokus 3 nález 1: od 768 px stojí karta s adresou vedľa nadpisu sekcie Kontakt; jej horná čiara bola na
        # hrane škatule h2, verzálka o 4 až 11 px nižšie (snímky presnost/oprava-po-prejde.md). Karta ide dole o odstup
        # verzálky od hrany škatule: Geist pri line-height 1 (ascent 1005, descent 295, capHeight 710 na 1000) 0,145 em.
        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            self.skipTest('fontTools nie je')
        f = TTFont(KOREN / 'sablona' / 'geist-latin.woff2')
        em, asc, desc, cap = f['head'].unitsPerEm, f['hhea'].ascent, -f['hhea'].descent, f['OS/2'].sCapHeight
        nadpis = deklaracie(self.zaklad, '.nadpis')
        self.assertEqual(nadpis.get('line-height'), '1')
        k = (1 - (asc + desc) / em) / 2 + asc / em - cap / em
        self.assertAlmostEqual(k, 0.145, places=3)
        tablet = '\n'.join(css_bloky(self.css, '@media (min-width:768px)'))
        self.assertEqual(deklaracie(tablet, '.kontakt-mapa').get('margin-top'), f'calc(.145 * {nadpis["font-size"]})')
        # na mobile je karta pod textom, bez posunu
        self.assertNotIn('margin-top', deklaracie(self.zaklad, '.kontakt-mapa'))
        for cesta in ('index.html', 'kontakt/index.html'):
            sek = self.telo(cesta).split('id="kontakt"', 1)[1]
            self.assertIn('<div class="kontakt-text"><h2 class="nadpis" id="kontakt-nadpis">', sek, cesta)
            self.assertIn('<div class="kontakt-mapa"><figure class="mapa-karta">', sek, cesta)


# ---------------------------------------------------------------- linka bmb-fotky: AVIF, WebP a fotky-web

def profil_nie_srgb():
    """Platný profil ICC iného priestoru RGB: vstavaný sRGB s vymenenými primármi červenej a modrej (rXYZ, bXYZ)
    a popisom „Test“. Čistá červená v ňom je v sRGB modrá; build ju musí previesť (ImageCms), nie ignorovať."""
    from PIL import ImageCms
    b = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    tagy = {}
    for i in range(int.from_bytes(b[128:132], 'big')):
        z = 132 + 12 * i
        tagy[bytes(b[z:z + 4])] = (int.from_bytes(b[z + 4:z + 8], 'big'), int.from_bytes(b[z + 8:z + 12], 'big'))
    (r0, rn), (b0, bn) = tagy[b'rXYZ'], tagy[b'bXYZ']
    b[r0:r0 + rn], b[b0:b0 + bn] = bytes(b[b0:b0 + bn]), bytes(b[r0:r0 + rn])
    return bytes(b).replace('sRGB'.encode('utf-16-be'), 'Test'.encode('utf-16-be'))


def fotka_s_metadatami(cesta):
    """JPEG 900 x 1400 s EXIF orientáciou 6 (zobrazí sa 1400 x 900), značkou fotoaparátu, XMP a profilom ICC iným než
    sRGB; šachovnica 16 px červená a biela (ostrá, v sRGB po prevode modrá a biela)."""
    im = Image.new('RGB', (900, 1400), (255, 255, 255))
    px = im.load()
    for x in range(900):
        for y in range(1400):
            if (x // 16 + y // 16) % 2:
                px[x, y] = (220, 30, 30)
    exif = Image.Exif()
    exif[274] = 6
    exif[271] = 'Fotoaparat Test'
    cesta.parent.mkdir(parents=True, exist_ok=True)
    im.save(cesta, 'JPEG', quality=92, exif=exif.tobytes(), icc_profile=profil_nie_srgb(),
            xmp=b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>')


def vyrob_fotky_web(obsah, ciel, predlohy, kvalita=50):
    """fotky-web/ ako ho vyrobí nastroje/predlohy_web.py, bez hľadania kvality (pevná kvalita): plán súborov z build.Web
    s rozmermi predlôh, súbory cez build.kodovany z predlohy (obrázok v pamäti), manifest.json. predlohy: {rel: Image}."""
    sha = {rel: hashlib.sha256((obsah / 'obrazky' / rel).read_bytes()).hexdigest() for rel in predlohy}
    with redirect_stderr(io.StringIO()):
        w = build.Web(obsah, False, fotky_web=ciel, predlohy={sha[r]: im.size for r, im in predlohy.items()})
        w.postav()
    man = {'verzia': 1, 'fotky': {}}
    for rel, im in predlohy.items():
        s = sha[rel]
        subory = {}
        for kluc, (sirka, vyska, orez, fmt) in sorted(w.potreba[s].items()):
            data = build.kodovany(build.zmensi(im.copy(), sirka, vyska, build.OREZY[orez] if orez else None), fmt, kvalita)
            meno = f'{s[:16]}/{sirka}{"-" + orez if orez else ""}.{fmt}'
            (ciel / meno).parent.mkdir(parents=True, exist_ok=True)
            (ciel / meno).write_bytes(data)
            subory[kluc] = {'subor': meno, 'w': sirka, 'h': vyska, 'bajty': len(data),
                            'sha256': hashlib.sha256(data).hexdigest()}
        man['fotky'][s] = {'rel': rel, 'predloha': {'w': im.width, 'h': im.height}, 'kvalita': {'avif': kvalita, 'webp': kvalita},
                           'subory': subory}
    (ciel / 'manifest.json').write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding='utf-8')
    return man, sha


def zdroje_picture(s):
    """Každý <picture> stránky: (zoznam atribútov source, atribúty img)."""
    out = []
    for kus in re.findall(r'<picture>(.*?)</picture>', s, re.S):
        r = Rozbor()
        r.feed(kus)
        out.append(([a for t, a in r.prvky if t == 'source'], [a for t, a in r.prvky if t == 'img'][0]))
    return out


def srcset_polozky(srcset):
    return [(k.strip().rsplit(' ', 1)[0], int(k.strip().rsplit(' ', 1)[1][:-1])) for k in srcset.split(',') if k.strip()]


# strop šírky záložného WebP (KODOVANIE.md časť 1); zámerne pevné číslo, nie build.WEBP_MAX
STROP_WEBP = 1440


class TestFotkyWeb(unittest.TestCase):
    """Linka bmb-fotky: picture so zdrojom AVIF a zálohou WebP, šírky podľa predlohy z fotky-web/ (nikdy väčšie),
    bez metadát a v sRGB, fetchpriority len na titulnej fotke úvodu, záloha bez kodeku AVIF, zmenený zdroj = nové
    kódovanie z obsahu, opakovaný build nič neprepíše. Každý test má sabotáž v ops/weby-na-mieru/klienti/bmbuilding/
    fotky-kvalita/nastroje/sabotaz_fotky.py (musí zlyhať)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-fw-'))
        cls.obsah = maly_obsah(cls.tmp)
        fotka_s_metadatami(cls.obsah / 'obrazky' / 'meta' / '01.jpg')
        (cls.obsah / 'realizacie' / 'meta.json').write_text(json.dumps({
            'slug': 'meta', 'nazov': 'Bytový dom Metadáta', 'miesto': 'Pezinok', 'roky': '2020 až 2021', 'poradie': 0,
            'fotky': [{'subor': 'obsah/obrazky/meta/01.jpg', 'alt': 'Fasáda'}]}, ensure_ascii=False), encoding='utf-8')
        # predloha plna/01.jpg (zdroj 1100 x 700) zväčšená na 2200 x 1400 ako z web-predlohy
        with Image.open(cls.obsah / 'obrazky' / 'plna' / '01.jpg') as im:
            cls.predloha = im.convert('RGB').resize((2200, 1400), Image.LANCZOS)
        cls.fw = cls.tmp / 'fotky-web'
        cls.man, cls.sha = vyrob_fotky_web(cls.obsah, cls.fw, {'plna/01.jpg': cls.predloha})
        cls.web = cls.tmp / 'web'
        cls.kod, cls.out, cls.err = postav(cls.web, cls.obsah, fotky_web=cls.fw)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def html(self, cesta):
        return (self.web / cesta).read_text(encoding='utf-8')

    def test_beh_a_pocty(self):
        self.assertEqual(self.kod, 0, self.err)
        pocet = len(self.man['fotky'][self.sha['plna/01.jpg']]['subory'])
        self.assertIn(f'fotky: z fotky-web {pocet} súborov', self.out)
        self.assertIn('AVIF áno', self.out)

    def test_picture_so_zdrojmi_avif_a_webp(self):
        for cesta in ('realizacie/plna/index.html', 'realizacie/meta/index.html', 'realizacie/index.html', 'index.html'):
            s = self.html(cesta)
            obrazky = zdroje_picture(s)
            self.assertTrue(obrazky, cesta)
            for zdroje, img in obrazky:
                avif = [z for z in zdroje if z.get('type') == 'image/avif' and 'media' not in z]
                self.assertEqual(len(avif), 1, (cesta, zdroje))
                self.assertTrue(avif[0].get('sizes'), cesta)
                self.assertEqual(avif[0]['sizes'], img['sizes'], cesta)
                for druh, srcset in (('AVIF', avif[0]['srcset']), ('WEBP', img['srcset'])):
                    for u, w in srcset_polozky(srcset):
                        self.assertTrue(u.endswith('.' + druh.lower()), u)
                        with Image.open((self.web / cesta).parent / u) as im:
                            self.assertEqual((im.format, im.width), (druh, w), u)
                self.assertTrue(img['src'].endswith('.webp'))
        # lightbox: AVIF v data-avif odkazov galérie (pri náhľade so srcset v zdroji AVIF náhľadu, lb_zdroje)
        # a v data-sirky-avif skrátenej zmluvy pásu úvodu
        s = self.html('realizacie/plna/index.html')
        prvky = rozober(self.web / 'realizacie/plna/index.html').prvky
        indexy = [k for k, (t, a) in enumerate(prvky) if t == 'a' and 'data-lightbox' in a]
        self.assertTrue(indexy)
        for k in indexy:
            _webp, avif, _z = lb_zdroje(prvky, k)
            self.assertTrue(avif, prvky[k][1].get('href'))
            for u, _w in srcset_polozky(avif):
                self.assertTrue((self.web / 'realizacie' / 'plna' / u).resolve().is_file(), u)
        uvod = self.html('index.html')
        # zoznamy fotiek pásu (lb-zoznam) na úvode nie sú (pás je preč, 3A.6); odložené fotky slidera majú AVIF v data-avif
        self.assertNotIn('class="lb-zoznam"', uvod)
        karty = re.findall(r'<img src="prazdna-45\.webp" data-src="[^"]*" data-srcset="[^"]*" data-avif="([^"]*)"', uvod)
        for avif in karty:
            for u, _w in srcset_polozky(avif):
                self.assertTrue((self.web / u).is_file(), u)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("'data-avif'", "'data-sirky-avif'", "'image/avif'", 'avifZdroj(foto', "createElement('picture')"):
            self.assertIn(kus, js)
        self.assertIn('picture{display:contents}', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'))

    def test_sirky_nie_vacsie_nez_predloha(self):
        # plna/01.jpg má predlohu 2200 px vo fotky-web: AVIF až do 2200 (viac než zdroj 1100), WebP najviac 1440
        s = self.html('realizacie/plna/index.html')
        avif = [u for zdroje, _i in zdroje_picture(s) for z in zdroje for u in srcset_polozky(z['srcset'])
                if '/plna/01-' in u[0] and z.get('type') == 'image/avif']
        webp = [u for _z, img in zdroje_picture(s) for u in srcset_polozky(img['srcset']) if '/plna/01-' in u[0]]
        self.assertEqual(max(w for _u, w in avif), 2200)
        # pevné číslo, nie build.WEBP_MAX: zmena konštanty (napríklad WEBP_MAX = 2400) musí test zhodiť (brána 22. 11., nález 4)
        self.assertLessEqual(max(w for _u, w in webp), STROP_WEBP)
        # bez predlohy (plna/02.jpg 500 px, meta/01.jpg 1400 px) nikdy nad šírku zdroja
        for p in sorted((self.web / 'obrazky').rglob('*.*')):
            kusy = p.stem.split('-')
            with Image.open(p) as im:
                self.assertEqual(im.width, int(kusy[1]), p)
                if len(kusy) == 2:
                    strop = {'plna/01': 2200, 'plna/02': 500, 'meta/01': 1400}[f'{p.parent.name}/{kusy[0]}']
                    self.assertLessEqual(im.width, strop if p.suffix == '.avif' else min(strop, STROP_WEBP), p)
        # súbory z fotky-web sú bajt po bajte tie z manifestu (kópia, nie nové kódovanie)
        z = self.man['fotky'][self.sha['plna/01.jpg']]['subory']['|2200|avif']
        self.assertEqual(hashlib.sha256((self.web / 'obrazky' / 'plna' / '01-2200.avif').read_bytes()).hexdigest(), z['sha256'])

    def test_bez_metadat_a_srgb(self):
        subory = sorted(p for p in (self.web / 'obrazky').rglob('*') if p.suffix in ('.avif', '.webp'))
        self.assertGreater(len([p for p in subory if p.suffix == '.avif']), 5)
        for p in subory:
            data = p.read_bytes()
            with Image.open(io.BytesIO(data)) as im:
                for k in ('exif', 'xmp', 'icc_profile', 'XML:com.adobe.xmp'):
                    self.assertFalse(im.info.get(k), (p, k))
                self.assertEqual(len(im.getexif()), 0, p)
            self.assertNotIn(b'Fotoaparat Test', data, p)
            self.assertNotIn(b'xmpmeta', data, p)
            if p.suffix == '.avif':
                # jediná farebná informácia: nclx 1/13/6 (BT.709 primáry, prenos sRGB), žiadny profil ICC (prof)
                i = data.find(b'colr')
                self.assertEqual(data[i + 4:i + 10], b'nclx\x00\x01', p)
                self.assertEqual(data[i + 10:i + 12], b'\x00\x0d', p)
                self.assertNotIn(b'colrprof', data, p)
            else:
                self.assertNotIn(b'ICCP', data[:64], p)
                self.assertNotIn(b'EXIF', data[:64], p)
        # meta/01.jpg: otočená podľa EXIF (1400 x 900) a prevedená z profilu Test do sRGB (červená je modrá)
        for fmt in ('avif', 'webp'):
            with Image.open(self.web / 'obrazky' / 'meta' / f'01-960.{fmt}') as im:
                self.assertEqual(im.size, (960, 617), fmt)
                r, g, b = [sum(x) / len(x) for x in zip(*im.convert('RGB').getdata())]
            self.assertGreater(b, r + 40, (fmt, r, g, b))

    def test_fetchpriority_len_na_titulnej_fotke_uvodu(self):
        s = self.html('index.html')
        # kópie v noscript (bez skriptu) sa nerátajú, prehliadač so skriptom ich nenačíta
        obr = re.findall(r'<img [^>]*>', re.sub(r'<noscript>.*?</noscript>', '', s, flags=re.S))
        fotky = [i for i in obr if 'obrazky/' in i]
        # malý obsah: slider so záložnou fotkou (pás s ďalšími fotkami je od ÚVOD v3 preč)
        self.assertGreaterEqual(len(fotky), 1)
        prve = [i for i in obr if 'fetchpriority' in i]
        self.assertEqual(len(prve), 1, prve)
        self.assertIn(prve[0], s.split('<li class="snimka snimka-0 je"', 1)[1].split('</li>', 1)[0])
        self.assertIn('fetchpriority="high"', prve[0])
        self.assertNotIn('loading="lazy"', prve[0])
        for i in fotky:
            if i is prve[0]:
                continue
            self.assertTrue('loading="lazy"' in i or 'data-src=' in i, i)
        # inde lenivo: galéria realizácie celá loading=lazy, na stránke najviac jedna fotka s fetchpriority (LCP
        # dvojstrany, pôvodné pravidlo Lighthouse lcp-discovery)
        for cesta in ('realizacie/plna/index.html', 'realizacie/meta/index.html', 'realizacie/index.html'):
            t = re.sub(r'<noscript>.*?</noscript>', '', self.html(cesta), flags=re.S)
            self.assertLessEqual(t.count('fetchpriority='), 1, cesta)
            galeria = re.findall(r'<a class="galeria-odkaz[^"]*"[^>]*>(?:<picture>(?:<source [^>]*>)+)?(<img [^>]*>)', t)
            for i in galeria:
                self.assertIn('loading="lazy"', i, cesta)
            ostatne = [i for i in re.findall(r'<img [^>]*>', t) if 'obrazky/' in i and 'fetchpriority' not in i]
            self.assertTrue(ostatne, cesta)
            for i in ostatne:
                self.assertIn('loading="lazy"', i, cesta)
        self.assertTrue(re.findall(r'<a class="galeria-odkaz', self.html('realizacie/plna/index.html')))
        # preload fotky úvodu len AVIF (picture vyberie AVIF), nie aj WebP
        pre = re.findall(r'<link rel="preload" as="image"[^>]*>', s)
        self.assertEqual(len(pre), 1)
        for p in pre:
            self.assertIn('type="image/avif"', p)
            self.assertIn('fetchpriority="high"', p)

    def test_zaloha_bez_avif(self):
        web = self.tmp / 'web-bez-avif'
        with mock.patch.object(build, 'avif_dostupny', lambda: False):
            kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('Pillow nemá kodek AVIF', err)
        self.assertIn('AVIF nie (len WebP)', out)
        self.assertEqual(list(web.rglob('*.avif')), [])
        for p in stranky(web):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('image/avif', s, p)
            self.assertNotIn('data-avif', s, p)
            self.assertNotIn('data-sirky-avif', s, p)
        self.assertTrue((web / 'obrazky' / 'plna' / '01-1100.webp').is_file())
        # s fotky-web: AVIF predlohy ide kópiou aj bez kodeku, ostatné fotky len WebP
        web2 = self.tmp / 'web-bez-avif-fw'
        with mock.patch.object(build, 'avif_dostupny', lambda: False):
            kod, out, err = postav(web2, self.obsah, fotky_web=self.fw)
        self.assertEqual(kod, 0, err)
        avif = sorted(p.relative_to(web2).as_posix() for p in web2.rglob('*.avif'))
        self.assertTrue(avif)
        self.assertTrue(all(a.startswith('obrazky/plna/01-') for a in avif), avif)

    def test_iny_sha256_zdroja_nove_kodovanie(self):
        obsah = self.tmp / 'zmeneny' / 'obsah'
        shutil.copytree(self.obsah, obsah)
        with Image.open(obsah / 'obrazky' / 'plna' / '01.jpg') as im:
            im.convert('RGB').save(obsah / 'obrazky' / 'plna' / '01.jpg', 'JPEG', quality=80)
        web = self.tmp / 'web-zmeneny'
        kod, out, err = postav(web, obsah, fotky_web=self.fw)
        self.assertEqual(kod, 0, err)
        self.assertIn('fotky: z fotky-web 0 súborov', out)
        self.assertFalse((web / 'obrazky' / 'plna' / '01-2200.avif').exists())
        self.assertTrue((web / 'obrazky' / 'plna' / '01-1100.avif').is_file())
        hotove = {z['sha256'] for z in self.man['fotky'][self.sha['plna/01.jpg']]['subory'].values()}
        for p in (web / 'obrazky' / 'plna').glob('01-*'):
            self.assertNotIn(hashlib.sha256(p.read_bytes()).hexdigest(), hotove, p)
            with Image.open(p) as im:
                self.assertLessEqual(im.width, 1100, p)

    def test_opakovany_build_nic_neprepise(self):
        web = self.tmp / 'web-dvakrat'
        kod, out, err = postav(web, self.obsah, fotky_web=self.fw)
        self.assertEqual(kod, 0, err)
        pred = {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in web.rglob('*') if p.is_file()}
        self.assertTrue(pred)
        kod, out, err = postav(web, self.obsah, fotky_web=self.fw)
        self.assertEqual(kod, 0, err)
        self.assertIn('(nových 0)', out)
        po = {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
              for p in web.rglob('*') if p.is_file()}
        self.assertEqual(po, pred)


# ---------------------------------------------------------------- linka bmb-presnost: ostré fotky kariet a osvedčenia

# karta pásu má pri 1920 px najviac 480 CSS px, pri hustote 2 teda 960 px zariadenia; viac než 1,25 px obrazovky na
# 1 px zdroja je mäkká fotka (presnost/meranie/fotky-pred.md, pravidlo A), výrez 4 : 5 zdroja preto aspoň 960 / 1,25
MIN_OREZ_PASU = 768


class TestPresnostFotky(unittest.TestCase):
    """Linka bmb-presnost (ops/weby-na-mieru/klienti/bmbuilding/presnost/oprava-fotky.md): fotka karty z nastavenia
    karta_foto, výrez 4 : 5 kariet pásu dosť veľký, prísnejší prah kódovania titulných fotiek, AI predloha len nad
    1,25 x šírku zdroja, sizes pri object-fit: cover a osvedčenia zväčšené bez AI. Sabotáže:
    presnost/nastroje/sabotaz_presnost.py (každý test musí bez opravy zlyhať)."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry = s['ostry']
        cls.w = s['web']
        # fotky-web/manifest.json (titulné fotky) a karty noviniek vznikli so zapnutými Novinkami; tie testy bežia
        # nad zostavou s "novinky": true (ZMENY-KOLO-1.md 5.7)
        n = skutocny_novinky()
        cls.ostry_n = n['ostry']
        cls.w_n = n['web']

    def test_karta_foto_z_nastaveni(self):
        # reprezentatívne fotky podľa zadania (ZMENY-KOLO-1.md 5.3) a fotky kariet stavieb slidera (Vtáčnik, Okánikova,
        # Na Križovatkách a Ventúrska podľa agenta FOTKY), Kalvária ostáva len na karte mriežky (detail false)
        nast = self.w.nastavenia.get('karta_foto')
        self.assertIsInstance(nast, dict)
        ocak = {'rd-ahoj-bratislava-nove-mesto': '01.jpg', 'rekonstrukcia-vila-holubyho-bratislava': '01.jpg',
                'filmovy-klub-lumiere-spitalska-bratislava': '08.jpg', 'dvojdom-s-bratislava': '01.jpg',
                'novostavba-rd-nitrianske-hrnciarovce': '01.jpg', 'bytovy-dom-rosnicka-bratislava': '01.jpg',
                'dom-betliarska-bratislava-petrzalka': '01.jpg', 'ufo-most-snp-bratislava': '10.jpg',
                'bytove-domy-vtacnik-bratislava': '09.jpg', 'rekonstrukcia-okanikova-bratislava': '05.jpg',
                'bytovy-dom-na-krizovatkach-bratislava': '01.jpg', 'rekonstrukcia-venturska-bratislava': '06.jpg',
                'rd-kalvaria-bratislava': '01.jpg'}
        self.assertEqual({k: (v['subor'] if isinstance(v, dict) else v) for k, v in nast.items()}, ocak)
        # skryté realizácie (Filmový klub 17, ambulancie 09) v karta_foto nie sú
        self.assertNotIn('filmovy-klub-spitalska-bratislava', nast)
        self.assertNotIn('rekonstrukcia-5-ambulancii-pre-lekarov', nast)
        podla = {r['slug']: r for r in self.w.realizacie}
        for slug, volba in nast.items():
            subor = volba['subor'] if isinstance(volba, dict) else volba
            # "detail": false mení len kartu mriežky a služieb (karta_len), titulná fotka detailu ostáva (brána 1, nález 2)
            kluc = 'karta_len' if isinstance(volba, dict) and volba.get('detail') is False else 'karta'
            self.assertEqual(podla[slug][kluc]['rel'], f'{slug}/{subor}', slug)
        kal = podla['rd-kalvaria-bratislava']
        self.assertEqual(kal['karta']['rel'], 'rd-kalvaria-bratislava/02.jpg')
        mriezka = (self.ostry / 'realizacie' / 'index.html').read_text(encoding='utf-8')
        kal_karta = mriezka.split('href="../realizacie/rd-kalvaria-bratislava/"', 1)[1].split('</li>', 1)[0]
        self.assertIn('rd-kalvaria-bratislava/01-', kal_karta)
        self.assertNotIn('rd-kalvaria-bratislava/02-', kal_karta)
        # titulná fotka detailu aj karta mriežky Hrnčiaroviec sú 01.jpg (predloha zväčšená Upscaylom)
        hrn_m = mriezka.split('href="../realizacie/novostavba-rd-nitrianske-hrnciarovce/"', 1)[1].split('</li>', 1)[0]
        self.assertIn('novostavba-rd-nitrianske-hrnciarovce/01-960-45.webp', hrn_m)
        self.assertNotIn('foto-cela', hrn_m)
        detail = (self.ostry / 'realizacie' / 'novostavba-rd-nitrianske-hrnciarovce' / 'index.html').read_text(encoding='utf-8')
        pohlad = detail.split('<figure class="pohlad detail-foto">', 1)[1].split('</figure>', 1)[0]
        self.assertIn('novostavba-rd-nitrianske-hrnciarovce/01-', pohlad)
        # zlá voľba (chýbajúci súbor, vizualizácia alebo pôvodný stav) nechá kartu podľa pravidiel a varuje
        r = copy_realizacie(podla['novostavba-rd-nitrianske-hrnciarovce'])
        povodna = r['karta']
        stare = self.w.nastavenia['karta_foto']
        try:
            self.w.nastavenia['karta_foto'] = {r['slug']: 'neexistuje.jpg'}
            r['karta'] = povodna
            err = io.StringIO()
            with redirect_stderr(err):
                self.w.karta_z_nastaveni(r)
            self.assertIs(r['karta'], povodna)
            self.assertIn('karta_foto', err.getvalue())
            self.w.nastavenia['karta_foto'] = {r['slug']: '16.jpg'}
            self.w.karta_z_nastaveni(r)
            self.assertEqual(r['karta']['rel'], f'{r["slug"]}/16.jpg')
            # reprezentatívna fotka ide v galérii na prvé miesto, ostatné ostávajú v poradí
            self.assertIs(r['galeria'][0], r['karta'])
            self.assertEqual([f['rel'] for f in r['galeria'][1:]],
                             [f['rel'] for f in podla[r['slug']]['galeria'] if f is not r['karta']])
        finally:
            self.w.nastavenia['karta_foto'] = stare

    def test_nahradena_karta_nie_je_prva_velka_v_galerii(self):
        """Reprezentatívna fotka (karta_foto) je v galérii prvá (ZMENY-KOLO-1.md 5.3) a v detaile nie je dvakrát veľká
        pod sebou (pohľad A nad galériou a prvá veľká galérie). Fotka karty nahradená nastavením karta_foto ostáva
        v galérii ako bežná fotka, nikdy prvá veľká (overenie zostavy bmb-presnost)."""
        podla = {r['slug']: r for r in self.w.realizacie}
        for slug in self.w.nastavenia['karta_foto']:
            r = podla[slug]
            volba = self.w.nastavenia['karta_foto'][slug]
            if isinstance(volba, dict) and volba.get('detail') is False:
                continue
            self.assertIs(r['galeria'][0], r['karta'], slug)
            detail = (self.ostry / 'realizacie' / slug / 'index.html').read_text(encoding='utf-8')
            galeria = detail.split('<ul class="galeria', 1)[1].split('</ul>', 1)[0]
            polozky = galeria.split('<li class="galeria-polozka')[1:]
            self.assertEqual(len(polozky), len(r['galeria']), slug)
            # prvá položka galérie je súbor reprezentatívnej fotky (<obr_dir>/<nn>-<šírka>), nie veľká
            self.assertIn(f'{r["obr_dir"]}/{r["karta"]["nn"]}-', polozky[0], slug)
            self.assertFalse(polozky[0].startswith(' prva'), slug)
            nahradena = r.get('karta_nahradena')
            if nahradena:
                s_nou = [p for p in polozky if f'{r["obr_dir"]}/{nahradena["nn"]}-' in p]
                self.assertEqual(len(s_nou), 1, slug)
                self.assertFalse(s_nou[0].startswith(' prva'), slug)

    def test_lightbox_zdroje_z_nahladu(self):
        """Odkaz galérie s náhľadom so srcset nenesie data-srcset ani data-avif (sú bajtovo rovnaké ako srcset náhľadu
        a jeho zdroja AVIF), app.js ich berie z náhľadu; malá fotka a doklady ich nesú ďalej (overenie zostavy
        bmb-presnost: HTML detailu Hrnčiaroviec 111 124 B s duplicitami, Lighthouse mobil 89, po zmene 91)."""
        odkazov = z_nahladu = 0
        for r in self.w.realizacie:
            if not r['galeria']:
                continue
            s = (self.ostry / 'realizacie' / r['slug'] / 'index.html').read_text(encoding='utf-8')
            galeria = s.split('<ul class="galeria', 1)[1].split('</ul>', 1)[0]
            for p in galeria.split('<li class="galeria-polozka')[1:]:
                a = re.search(r'<a class="galeria-odkaz( mala)?"[^>]*>', p)
                odkazov += 1
                if a.group(1):
                    self.assertIn(' data-srcset="', a.group(0), r['slug'])
                    continue
                z_nahladu += 1
                self.assertNotIn(' data-srcset="', a.group(0), r['slug'])
                self.assertNotIn(' data-avif="', a.group(0), r['slug'])
                # náhľad nesie to, čo by inak nieslo data-srcset a data-avif
                self.assertRegex(p, r'<img [^>]*srcset="[^"]+"', r['slug'])
                self.assertRegex(p, r'<source type="image/avif" srcset="[^"]+"', r['slug'])
        self.assertGreater(z_nahladu, 100)
        self.assertGreaterEqual(odkazov, z_nahladu)
        # doklady na Oprávneniach majú vlastný markup a srcset ďalej v data-srcset
        opr = (self.ostry / 'opravnenia' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(len(re.findall(r'data-galeria="osvedcenia"[^>]*', opr)), 2)
        self.assertEqual(opr.count(' data-srcset="'), 2)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("a.getAttribute('data-srcset') || nahladZ(a, 'img')", js)
        self.assertIn("""nahladZ(a, 'source[type="image/avif"]')""", js)

    def test_karty_s_dost_velkym_zdrojom(self):
        # reprezentatívne fotky kariet (nastavenia karta_foto) bez Kalvárie (detail false: karta mriežky ide cez
        # celú fotku, titulná je 02); pred ÚVOD v3 to boli karty tmavého pásu
        kf = self.w.nastavenia['karta_foto']
        pas = [r for r in self.w.realizacie if r['slug'] in kf and not (isinstance(kf[r['slug']], dict)
                                                                         and kf[r['slug']].get('detail') is False)]
        self.assertGreaterEqual(len(pas), 10)
        def vyrez_karty(f):
            w, h = f['w'], f['h']
            if f.get('vyrez45'):
                x0, y0, x1, y1 = build.box_px(f['vyrez45'], w, h)
                w, h = x1 - x0, y1 - y0
            return build.orez_rozmer(w, h, (4, 5))[0]
        # Rosnička: reprezentatívnu fotku 01 vybral klient (ZMENY-KOLO-1.md 5.3, karta_foto 09 je z galérie preč);
        # jej ostrosť na karte rieši predloha zväčšená Upscaylom (časť 2), prah zdroja tu pre ňu neplatí
        vybral_klient = {'bytovy-dom-rosnicka-bratislava/01.jpg'}
        for r in pas:
            f = r['karta']
            cw = vyrez_karty(f)
            if f['rel'] in vybral_klient:
                continue
            if f.get('cely_ram'):
                # reprezentatívna fotka s malým zdrojom (Dvojdom S 01, Hrnčiarovce 01): ostrosť nesie predloha zväčšená
                # Upscaylom (fotky-web), jej výrez musí stačiť (ZMENY-KOLO-1.md 2.2 a 3A.4)
                self.assertIn(f['fw']['predloha']['model'], ('ultrasharp-4x', 'upscayl-standard-4x', 'high-fidelity-4x'), f['rel'])
                cw = build.orez_rozmer(*f['zaklad'], (4, 5))[0]
            self.assertGreaterEqual(cw, MIN_OREZ_PASU, (r['slug'], f['rel'], cw))
        # Hrnčiarovce: reprezentatívna 01.jpg (zadanie 5.3), výrez predlohy zväčšenej Upscaylom aspoň 960 px, teda bez
        # zväčšenia aj pri 1920 px a hustote 2 (zadanie: karta musí byť ostrá)
        hrn = next(r for r in pas if r['slug'] == 'novostavba-rd-nitrianske-hrnciarovce')
        self.assertEqual(hrn['karta']['rel'], 'novostavba-rd-nitrianske-hrnciarovce/01.jpg')
        self.assertTrue(hrn['karta'].get('cely_ram'))
        self.assertEqual(hrn['karta']['fw']['predloha']['model'], 'upscayl-standard-4x')
        self.assertGreaterEqual(build.orez_rozmer(*hrn['karta']['zaklad'], (4, 5))[0], 960)

    def test_titulne_fotky_prisnejsi_prah(self):
        sys.path.insert(0, str(KOREN / 'nastroje'))
        import predlohy_web as pw
        self.assertGreaterEqual(pw.PRAH_TITULNE['avif'], 0.997)
        self.assertGreaterEqual(pw.PRAH_TITULNE['webp'], 0.997)
        self.assertGreater(pw.PRAH_TITULNE['avif'], pw.PRAH['avif'])
        titulne = pw.titulne_fotky(self.w_n)
        hrdina = self.w_n.hrdina_foto()[1]
        self.assertIn(hrdina['sha'], titulne)
        for r in self.w_n.realizacie:
            if r['karta']:
                self.assertIn(r['karta']['sha'], titulne, r['slug'])
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        for sha in titulne:
            z = man.get(sha)
            self.assertIsNotNone(z, sha)
            self.assertTrue(z.get('titulna'), z['rel'])
            self.assertEqual(z['zhoda'], pw.PRAH_TITULNE, z['rel'])
        ostatne = [z for sha, z in man.items() if sha not in titulne]
        self.assertTrue(ostatne)
        for z in ostatne:
            self.assertEqual(z['zhoda'], pw.PRAH, z['rel'])

    def test_ai_predloha_len_nad_zdroj(self):
        sys.path.insert(0, str(KOREN / 'nastroje'))
        import predlohy_web as pw
        master = Image.new('RGB', (2400, 1800), (255, 0, 0))
        zdroj = Image.new('RGB', (1440, 1080), (0, 0, 255))
        zaklad = pw.zaklad_variantu(master, zdroj)
        self.assertIs(zaklad(960), zdroj)
        self.assertIs(zaklad(1440), zdroj)
        self.assertIs(zaklad(1800), zdroj)       # 1,25 x 1440
        self.assertIs(zaklad(1920), master)
        self.assertIs(zaklad(960, '45'), zdroj)  # výrez 4 : 5 zdroja 864 px, 1,25 x 864 = 1080
        self.assertIs(zaklad(1200, '45'), master)
        self.assertIs(pw.zaklad_variantu(master, None)(480), master)
        # titulné fotky do 1,6 x zdroja (brána bmb-presnost pokus 1, nález 4: karta Rosnička 14 brala 960 px z AI)
        self.assertGreaterEqual(pw.ZDROJ_LANCZOS_MAX_TITULNE, 1.5)
        titulny = pw.zaklad_variantu(master, zdroj, pw.ZDROJ_LANCZOS_MAX_TITULNE)
        self.assertIs(titulny(2160), zdroj)      # 1,5 x 1440
        self.assertIs(titulny(1200, '45'), zdroj)
        self.assertIs(titulny(2400), master)
        # skutočné fotky-web: každá fotka s AI predlohou má šírky do 1,25 x zdroja zo zdroja (záznam zo_zdroja_do),
        # titulná do ZDROJ_LANCZOS_MAX_TITULNE x
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        ai = [z for z in man.values() if z['predloha'].get('model') in pw.MODELY_AI and not z['predloha'].get('retus')]
        self.assertTrue(ai)
        self.assertTrue(any(z.get('titulna') for z in ai))
        # retušovaná predloha (RD Ahoj 01): všetky šírky z predlohy, žiadna zo zdroja s chybou
        retus = [z for z in man.values() if z['predloha'].get('retus')]
        for z in retus:
            self.assertNotIn('zo_zdroja_do', z, z['rel'])
        for z in ai:
            w, _ = build.rozmery_zdroja(KOREN / 'obsah' / 'obrazky' / z['rel'])
            k = pw.ZDROJ_LANCZOS_MAX_TITULNE if z.get('titulna') else 1.25
            self.assertEqual(z.get('zo_zdroja_do'), int(w * k), z['rel'])
        # karta novinky Vianočný pozdrav (Rosnička 14, výrez 734 px): súbor 960 px výrezu je zo zdroja, nie z AI
        ros = next(z for z in man.values() if z['rel'] == 'bytovy-dom-rosnicka-bratislava/14.jpg')
        self.assertTrue(ros.get('titulna'))
        self.assertGreaterEqual(build.orez_rozmer(1224, 918, (4, 5))[0] * pw.ZDROJ_LANCZOS_MAX_TITULNE, 960)

    def test_rd_ahoj_bez_rozmazanych_ploch(self):
        # RD Ahoj 01 (prvá fotka slidera): dve rozmazané plochy na gabióne (číslo domu a menovka, rozmazané už v zdroji)
        # zakrýva retušovaná predloha (kolo 1, 24. 11. 2026). Žiadny súbor webu nesmie mať v mieste plôch hladkú
        # šedú plochu: jemná kresba vnútri oboch plôch musí byť podobná gabiónu 110 px pod nimi (zmerané 24. 11.: pred
        # retušou 0,006 až 0,16 na všetkých 15 súboroch, po retuši 0,71 až 1,05)
        from PIL import ImageChops, ImageFilter, ImageStat
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        z = next(z for z in man.values() if z['rel'] == 'rd-ahoj-bratislava-nove-mesto/01.jpg')
        self.assertTrue(z['predloha'].get('retus'))
        self.assertNotIn('zo_zdroja_do', z)
        pw_, ph_ = z['predloha']['w'], z['predloha']['h']
        plochy = [(1013, 1442, 1043, 1489), (1076, 1436, 1153, 1490)]  # vnútro plôch, px predlohy 2000 x 2657
        for kluc, s in z['subory'].items():
            orez = kluc.split('|')[0]
            with Image.open(KOREN / 'fotky-web' / s['subor']) as im:
                g = im.convert('L')
            dy = 0
            if orez:
                ch = build.orez_rozmer(pw_, ph_, build.OREZY[orez])[1]
                dy = (ph_ - ch) // 2
            k = g.width / pw_
            jemne = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(1.5)))

            def kresba(b, posun=0):
                box = (round(b[0] * k), round((b[1] + posun - dy) * k), round(b[2] * k), round((b[3] + posun - dy) * k))
                return ImageStat.Stat(jemne.crop(box)).mean[0]
            for b in plochy:
                pomer = kresba(b) / max(kresba(b, 110), 1e-6)
                self.assertGreater(pomer, 0.4, (kluc, b, round(pomer, 3)))

    def test_sizes_pri_cover_podla_pomeru_fotky(self):
        sd = '(min-width: 1024px) min(56vw, 1000px), calc(100vw - 32px)'
        self.assertEqual(build.sizes_pre_cover(sd, 2000, 1333), sd)
        self.assertEqual(build.sizes_pre_cover(sd, 1280, 960), sd)
        self.assertEqual(build.sizes_pre_cover(sd, 1496, 640),
                         '(min-width: 1024px) calc(min(56vw, 1000px) * 1.56), calc((100vw - 32px) * 1.56)')
        self.assertEqual(build.sizes_pre_cover('(min-width:640px) min(30vw, 570px), 46vw', 1440, 810),
                         '(min-width:640px) calc(min(30vw, 570px) * 1.19), calc(46vw * 1.19)')
        # web: titulná fotka detailu Devína (2,34 : 1 v ráme 3 : 2) a galéria s fotkou 16 : 9
        detail = (self.ostry / 'realizacie' / 'rd-devin-bratislava' / 'index.html').read_text(encoding='utf-8')
        pohlad = detail.split('<figure class="pohlad detail-foto">', 1)[1].split('</figure>', 1)[0]
        self.assertIn('* 1.56)', pohlad)
        siroke = 0
        for r in self.w.realizacie:
            telo = (self.ostry / 'realizacie' / r['slug'] / 'index.html').read_text(encoding='utf-8')
            gal = telo.split('id="galeria"', 1)[1] if 'id="galeria"' in telo else ''
            for f in r['galeria']:
                if f['w'] / f['h'] > 1.6:
                    kus = re.search(rf'<img src="[^"]*/{f["nn"]}-[^"]*" srcset="[^"]*" sizes="([^"]*)"', gal)
                    if kus:
                        siroke += 1
                        self.assertIn('calc(', kus.group(1), (r['slug'], f['rel']))
        self.assertGreater(siroke, 0)

    def test_osvedcenia_zvacsene_bez_ai_a_bez_zmeny_pisma(self):
        import numpy as np
        r = Rozbor()
        r.feed((self.ostry / 'opravnenia' / 'index.html').read_text(encoding='utf-8'))
        odkazy = [a for t, a in r.prvky if t == 'a' and a.get('data-galeria') == 'osvedcenia']
        self.assertEqual(len(odkazy), 2)
        for a in odkazy:
            sirky = dict((w, c) for c, w in srcset_polozky(a['data-srcset']))
            meno = a['data-plna'].rsplit('/', 1)[1].rsplit('-', 1)[0]
            zdroj = KOREN / 'sablona' / 'opravnenia' / f'{meno}.png'
            # sken orezaný na hranu papiera (build.OREZ_DOKLADU, brána pokus 2): výrez pixelov skenu (po prevode do sRGB
            # v otvor_rgb) bez prepočtu
            z = build.sken_dokladu(zdroj)
            self.assertEqual(z.tobytes(), build.otvor_rgb(zdroj).crop(build.OREZ_DOKLADU[meno]).tobytes())
            # data-sirka ostáva šírkou skenu (lightbox doklad nezväčší nad ňu v CSS px), súbor pre hustotu 2 je v srcset
            self.assertEqual(int(a['data-sirka']), z.width)
            hore = 2 * max(z.width, 528)
            self.assertIn(hore, sirky, a['data-srcset'])
            self.assertTrue(a['data-plna'].endswith(f'-{hore}.webp'))
            with Image.open(self.ostry / 'opravnenia' / f'{meno}-{hore}.webp') as o:
                o = o.convert('RGB')
            self.assertEqual(o.width, hore)
            # zmenšené späť na rozmer skenu: ani písmeno iné (rozdiel jasu ako pri čistom Lanczose a WebP q90)
            jas = lambda im: np.asarray(im.convert('L'), dtype=np.float64)  # noqa: E731
            d = np.abs(jas(o.resize(z.size, Image.LANCZOS)) - jas(z))
            self.assertLess(float(d.max()), 40, meno)
            self.assertLess(float(np.percentile(d, 99.9)), 16, meno)
            self.assertLess(float(d.mean()), 2.0, meno)
            # jemné doostrenie: viac vysokých frekvencií než čistý Lanczos, ale nie prehnane
            from PIL import ImageFilter

            def hf(im):
                y = jas(im)
                g = np.asarray(im.convert('L').filter(ImageFilter.GaussianBlur(1)), dtype=np.float64)
                return float(((y - g) ** 2).mean())
            pomer = hf(o) / hf(z.resize(o.size, Image.LANCZOS))
            self.assertGreater(pomer, 1.1, meno)
            self.assertLess(pomer, 1.8, meno)
            # zakryté miesto ostalo jednofarebné aj v zväčšenom súbore
            k = o.width / z.width
            box = [round(x * k) for x in zakrytie_v_oreze(meno)]
            m = round(3 * k)
            vyrez = o.convert('L').crop((box[0] + m, box[1] + m, box[2] - m, box[3] - m))
            self.assertLess(max(vyrez.getextrema()) - min(vyrez.getextrema()), 24)

    def test_karty_s_malym_zdrojom_cela_fotka(self):
        """Brána bmb-presnost pokus 1, nález 1: karta mriežky, služby a novinky s výrezom 4 : 5 pod KARTA_MIN_OREZ px
        zdroja ukáže celú fotku 4 : 3 (3 : 4) v strede rámu, najviac cap CSS px, takže pri hustote 2 pripadá na 1 px
        zdroja najviac 1,25 px obrazovky aj pri najširšej karte; ostatné karty výrez 4 : 5 aspoň KARTA_MIN_OREZ px.
        Karty služieb majú najprv fotky dosť veľké na celý rám (Vila X nie je na karte Stavebného manažmentu)."""
        self.assertEqual(build.KARTA_MIN_OREZ, 676)               # 2 x 422 / 1,25 = 675,2
        self.assertLessEqual(2 * build.KARTA_CELA_MAX / build.KARTA_MIN_OREZ, 1.25)
        malych = 0
        for stranka in ('realizacie', 'novinky', 'stavebny-dozor', 'stavebny-manazment', 'stavbyveduci'):
            s = (self.ostry_n / stranka / 'index.html').read_text(encoding='utf-8')
            for li in re.findall(r'<li class="(?:dlazdica|novinka-karta)[^"]*"[^>]*>.*?</li>', s, re.S):
                img = re.search(r'<img [^>]*>', li)
                m = re.search(r'src="[^"]*/(obrazky(?:-novinky)?)/([a-z0-9-]+)/(\d\d)-\d+-(\d\d)\.webp"', img.group(0)) if img else None
                if not m:
                    continue
                trieda = (re.search(r'class="([^"]*)"', img.group(0)) or [None, ''])[1]
                w_attr = int(re.search(r'width="(\d+)"', img.group(0)).group(1))
                if m.group(4) == '45':
                    self.assertNotIn('foto-cela', trieda, (stranka, li[:120]))
                    continue
                malych += 1
                self.assertIn(m.group(4), ('43', '34'), (stranka, li[:120]))
                self.assertIn('foto-cela', trieda)
                cap = int(re.search(r'cap-(\d+)', trieda).group(1)) if 'cap-' in trieda else build.KARTA_CELA_MAX
                self.assertIn(f'{cap}px)', re.search(r'sizes="([^"]*)"', img.group(0)).group(1))
                self.assertGreater(w_attr, 0)
        # 7 kariet s malým zdrojom z brány 1: Vila X, Kalvária, Grunty, Devín, Devínske Luky, dve novinky
        self.assertGreaterEqual(malych, 7)
        for r in self.w_n.realizacie + self.w_n.novinky:
            f = self.w_n.foto_karty(r) if r in self.w_n.realizacie else r.get('karta')
            if not f:
                continue
            # karta_foto cely_ram: meria sa predloha zväčšená Upscaylom (ZMENY-KOLO-1.md 5.3), inak zdroj
            cw = build.orez_rozmer(*(f['zaklad'] if f.get('cely_ram') else (f['w'], f['h'])), (4, 5))[0]
            self.assertEqual(self.w_n.mala_karta(f), cw < build.KARTA_MIN_OREZ, f['rel'])
            if self.w_n.mala_karta(f):
                kod = '43' if f['w'] >= f['h'] else '34'
                vw = build.orez_rozmer(f['w'], f['h'], build.OREZY[kod])[0]
                cap = max(build.KARTA_CELA_MIN, min(build.KARTA_CELA_MAX, int(vw * build.KARTA_CELA_ZVACSENIE / 2) // 8 * 8))
                self.assertLessEqual(2 * cap / vw, 1.25, f['rel'])
        css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        self.assertIn('.projekt-foto img.foto-cela{position:absolute;inset:0;margin:auto;'
                      f'width:min(100% - 2 * var(--m-24),var(--cap,{build.KARTA_CELA_MAX}px))', css)
        for n in range(build.KARTA_CELA_MIN, build.KARTA_CELA_MAX, 8):
            self.assertIn(f'.cap-{n}{{--cap:{n}px}}', css)
        # služby: prvé karty majú fotku dosť veľkú na celý rám, Vila X je v zozname pod nimi
        man = (self.ostry / 'stavebny-manazment' / 'index.html').read_text(encoding='utf-8')
        karty = man.split('<ul class="karty">', 1)[1].split('</ul>', 1)[0]
        self.assertNotIn('realizacie/vila-x-bratislava/', karty)
        self.assertIn('realizacie/vila-x-bratislava/', man)
        self.assertNotIn('foto-cela', karty)

    def test_vyrez_karty_z_ramika(self):
        """karta_foto vyrez (brána bmb-presnost pokus 1, nález 9) ostáva v build.py, ale od 1. kola pripomienok ho žiadna
        karta nepoužíva: Hrnčiarovce majú reprezentatívnu fotku 01.jpg, ktorú vybral klient (ZMENY-KOLO-1.md 5.3).
        Dobrý rámik (výrez aspoň 768 px) nastaví vyrez45, zlý sa nepoužije a build varuje."""
        self.assertFalse(any(isinstance(v, dict) and v.get('vyrez') for v in self.w.nastavenia['karta_foto'].values()))
        hrn = next(r for r in self.w.realizacie if r['slug'] == 'novostavba-rd-nitrianske-hrnciarovce')
        self.assertFalse(hrn['karta'].get('vyrez45'))
        stare = self.w.nastavenia['karta_foto']
        f9 = dict(next(x for x in hrn['galeria'] if x['rel'].endswith('/09.jpg')))
        f9.pop('vyrez45', None)
        try:
            for vyrez, ok in (([0.26, 0.067, 0.74, 0.868], True), ([0.4, 0.4, 0.6, 0.6], False)):
                f9.pop('vyrez45', None)
                r = copy_realizacie(hrn)
                r['galeria'] = [f9 if x['rel'] == f9['rel'] else x for x in hrn['galeria']]
                self.w.nastavenia['karta_foto'] = {r['slug']: {'subor': '09.jpg', 'vyrez': vyrez}}
                err = io.StringIO()
                with redirect_stderr(err):
                    self.w.karta_z_nastaveni(r)
                self.assertEqual('vyrez45' in f9, ok, vyrez)
                self.assertEqual('výrez' in err.getvalue(), not ok, vyrez)
                self.assertIs(r['karta'], f9)
                self.assertIs(r['galeria'][0], f9)
        finally:
            self.w.nastavenia['karta_foto'] = stare
            self.w.vyrezy.pop(f9['sha'], None)

    def test_css_nadpis_pasu_a_titulna_fotka_detailu(self):
        """Brána bmb-presnost pokus 1, nálezy 5 a 6: nadpis v riadku s tlačidlom Fotky na celú výšku riadka; pole
        titulnej fotky detailu najviac 1000 CSS px ako v sizes, súbor 2000 px sa pri 1920 px nezväčšuje."""
        css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        self.assertIn('.projekt-riadok>h3{align-self:stretch}', css)
        self.assertIn('.detail .detail-foto{grid-column:1/8;max-width:1000px}', css)
        self.assertIn('min(56vw, 1000px)', build.SIZES_DETAIL)


def copy_realizacie(r):
    """Plytká kópia realizácie na skúšku nastavenia karta_foto (fotky ostávajú tie isté slovníky)."""
    return dict(r)


if __name__ == '__main__':
    unittest.main()
