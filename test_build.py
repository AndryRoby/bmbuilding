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


def postav(vystup, obsah=None, nahlad=False, prostredie=None, rychle=False, fotky_web=None, hosting=None):
    argv = ['--vystup', str(vystup)]
    if obsah:
        argv += ['--obsah', str(obsah)]
    if nahlad:
        argv.append('--nahlad')
    if fotky_web:
        argv += ['--fotky-web', str(fotky_web)]
    if hosting:
        argv += ['--hosting', hosting]
    out, err = io.StringIO(), io.StringIO()
    # testy bežia rovnako lokálne aj v GitHub Actions: premenné CI a hostingu nastaví len test, ktorý ich skúša
    env = {k: v for k, v in os.environ.items() if k not in ('GITHUB_ACTIONS', 'BMB_OSTRY', 'BMB_HOSTING')}
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
                  'fonts/geist-mono-cisla.woff2',
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
        # krasa-8 (Fotky slidera): imagesizes je sizes prvej fotky slidera podľa jej pomeru (sizes_slidera), ten istý
        # ako na obrázku, inak by prehliadač fotku stiahol dvakrát
        prva = re.search(r'<li class="snimka snimka-0 je"[^>]*>(.*?)</li>', s, re.S).group(1)
        sizes0 = re.search(r'<img [^>]*sizes="([^"]+)"', prva).group(1)
        # (od 2561 px najprv položky slidera bez presahu, oprava po overení širokého rozloženia)
        self.assertTrue(sizes0.startswith('(min-width: 2561px) and (max-height: 820px) max(1390px, '), sizes0)
        self.assertIn('(min-width: 1024px) and (max-height: 820px) max(', sizes0)
        self.assertIn(f'imagesizes="{sizes0}" fetchpriority="high"', obr[0])
        self.assertNotIn('-54.', obr[0])
        # preload Geist latinka a diakritika; z Geist Mono len podmnožina číslic na stránke, ktorá Mono kreslí (úvod:
        # valce kót), za fotkou a logom (audit naplno 25. 11. 2026, položka 44; celá latinka Mono bez preloadu)
        self.assertEqual(preload, [f'<link rel="preload" href="fonts/{p}" as="font" type="font/woff2" crossorigin>'
                                   for p in ('geist-latin.woff2', 'geist-latin-ext.woff2', build.PISMO_MONO_CISLA)])
        self.assertLess(s.index('logo-hlavicka.svg" as="image"'), s.index(f'fonts/{build.PISMO_MONO_CISLA}'))
        ukazka = KOREN.parent / 'arling-sk' / 'web' / 'ukazky' / 'architekti' / 'fonts'
        for meno in ('geist-latin.woff2', 'geist-latin-ext.woff2', 'geist-mono-latin.woff2', 'geist-mono-latin-ext.woff2'):
            self.assertEqual((self.ostry / 'fonts' / meno).read_bytes(), (KOREN / 'sablona' / meno).read_bytes())
            if ukazka.is_dir():
                self.assertEqual((KOREN / 'sablona' / meno).read_bytes(), (ukazka / meno).read_bytes(), meno)
        # podmnožina je naša (nastroje/pismo_mono_cisla.py), v ukážke nie je
        self.assertEqual((self.ostry / 'fonts' / build.PISMO_MONO_CISLA).read_bytes(),
                         (KOREN / 'sablona' / build.PISMO_MONO_CISLA).read_bytes())
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
        self.assertEqual(html.unescape(re.sub(r'<[^>]+>', '', nad.group(1))).replace(' ', ' ').split(' · '), self.texty['uvod']['nadtitulok'])
        # krasa-8: text v jednom span (jedna položka flexu vedľa čiarky), posledné slovo (miesto) aj s bodkou v span
        # uvod-nadtitulok-miesto, ktorý styl.css v užšom stĺpci skryje (test_krasa8_nadtitulok_bez_zlomu)
        self.assertRegex(nad.group(1), r'^<span>[^<]+<span class="uvod-nadtitulok-miesto"> · [^<]+</span></span>$')
        self.assertEqual(html.unescape(re.search(r'<span class="uvod-nadtitulok-miesto"> · ([^<]+)</span>', nad.group(1))
                                       .group(1)).replace(' ', ' '), self.texty['uvod']['nadtitulok'][-1])
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
                         ['1. Stavebný manažment:', '2. Stavebný dozor:', '3. Stavbyvedúci'])
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
        # dlhé zoznamy v dvoch stĺpcoch od tabletu (CSS mriežka so spoločnými riadkami, krasa-8), krátke nie
        self.assertEqual(re.findall(r'<ul class="cinnost-body cinnost-body-dlhe riadky-(\d+)">', cin), ['10', '4'])
        self.assertIn('.cinnost-body-dlhe{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));', self.css)
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
                          r' <span>(realizáci\w+)</span><span class="vh">: ([^<]+)</span></a>', hlava)
            if n:
                self.assertIsNotNone(m, slug)
                self.assertEqual(int(m.group(1)), n, slug)
                self.assertEqual(int(pocet_sluzby.group(1)), n, slug)
                self.assertEqual(m.group(2), 'realizácia' if n == 1 else 'realizácie' if n < 5 else 'realizácií')
                # služba v prístupnom mene odkazu (audit naplno 25. 11. 2026, položka 30), viditeľný text bez zmeny
                self.assertEqual(m.group(3).replace(chr(160), ' '), build.male(self.texty['sluzby'][slug]['menu']), slug)
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
        self.assertIn('.cinnost-hlava{grid-column:1/5;align-self:start;position:sticky;top:calc(var(--hore) + var(--m-24));'
                      'margin-bottom:var(--m-24)}', self.css)
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
        # ním, aspoň 48 px), nie údaje odtlačené na spodok; vysoké okno má pod hrdinom riadok onad (krasa-8: nadpis
        # O firme a druhý odsek, test_krasa8_vysoke_okno_o_firme)
        self.assertIn('grid-template-areas:"slider slider slider slider slider slider slider . . . . ." '
                      '"slider slider slider slider slider slider slider hlava hlava hlava hlava hlava" '
                      '"slider slider slider slider slider slider slider text text text text text" '
                      '"slider slider slider slider slider slider slider udaje udaje udaje udaje udaje" '
                      '"slider slider slider slider slider slider slider . . . . ." '
                      '"onad onad onad onad onad onad onad onad onad onad onad onad";'
                      'grid-template-rows:minmax(var(--m-48),1fr) auto auto auto minmax(var(--m-48),1fr) auto}', self.css)
        self.assertNotIn('.firma-udaje{align-self:end', self.css)
        for sel in ('.slider{grid-area:slider}', '.firma-udaje{grid-area:udaje;', '.firma-text{grid-area:text;', '.firma-hlava{grid-area:hlava}'):
            self.assertIn(sel, self.css)
        # H1 veľký: podiel šírky stĺpca, najviac --h1-strop (4rem = 64 px; od 1921 px rastie s oknom, úsek ŠIROKÉ OKNO,
        # test_siroke_okno_css), výška slidera na počítači podľa okna pod hlavičkou
        self.assertIn('.uvod-h1{margin-top:var(--m-16);font-size:clamp(1.75rem,9.8cqi,var(--h1-strop));', self.css)
        self.assertIn(':root{--h1-strop:4rem}', self.css)
        # výška fotky: okno pod hlavičkou až po spodok okna (krasa-8), najviac 75 % šírky okna (vysoké úzke okno
        # 1255 x 1260 nedá rám príliš na výšku)
        self.assertIn('min-height:clamp(480px,calc(100svh - var(--hore)),75vw)', self.css)
        # hrdina na prvú obrazovku aj na notebookoch (overenie-1, nález blokuje: pri 1280 x 720 až 1536 x 864 boli
        # štítok, ovládanie slidera a tlačidlá pod okrajom okna). Pri okne do 1079 px výšky (doladenie-2, predtým 960 px)
        # ide blok O firme pod hrdinu, vpravo ostane H1, údaje, zdroj a tlačidlá; H1 najviac 6 % výšky okna; do 820 px
        # hustejšie údaje. Prehliadač overil oprava-1/meraj.mjs (1280 x 720, 1366 x 768, 1440 x 900, 1536 x 864,
        # 1920 x 1080, 1024 x 768: spodok štítka, ovládania a tlačidiel v okne) a doladenie-2 (test_doladenie2_*).
        nizke = ''.join(css_bloky(self.css, '@media (min-width:1024px) and (max-height:1079px)'))
        self.assertIn('"slider slider slider slider slider slider slider udaje udaje udaje udaje udaje" '
                      '"slider slider slider slider slider slider slider . . . . ." '
                      '"text text text text text text text text text text text text";'
                      'grid-template-rows:minmax(var(--m-16),1fr) auto auto minmax(var(--m-16),1fr) auto}', nizke)
        self.assertNotIn('slider text', nizke)
        # H1 najviac 6 % výšky okna len do 960 px (doladenie-2: pri 961 až 1079 px plné H1 ako predtým vo vysokom okne)
        self.assertNotIn('.uvod-h1{', nizke)
        self.assertEqual(''.join(css_bloky(re.sub(r'/\*.*?\*/', '', self.css, flags=re.S),
                                           '@media (min-width:1024px) and (max-height:960px)')).strip(),
                         '.uvod-h1{font-size:min(clamp(1.75rem,9.8cqi,var(--h1-strop)),6svh)}')
        # oprava-2 (nález vážne): blok O firme pod hrdinom ako jeden celok, vlasová čiara, viditeľný nadtitulok vľavo,
        # oba odseky vpravo rovnakou farbou, nie rozhodené pod fotku a pod stĺpec; krasa-8: na mriežke 12 stĺpcov
        # pozadia (nadtitulok stĺpce 1 až 4, odseky od čiary 5. stĺpca), nie 4 a 8 dielov s dvojitou medzerou
        self.assertIn('.firma-text{display:grid;grid-area:text;grid-template-columns:repeat(var(--stlpce),minmax(0,1fr));'
                      'column-gap:var(--medzera);', nizke)
        self.assertIn('margin-top:var(--m-48);padding:var(--m-32) 0 0;border-top:1px solid var(--beton)}', nizke)
        self.assertIn('.firma-text-nad{grid-area:auto;grid-column:1/5;grid-row:1/span 2;', nizke)
        self.assertIn('.firma-text>p,.firma-text>p+p{grid-area:auto;grid-column:5/-1;margin:0;padding:0;border:0;', nizke)
        self.assertIn('color:var(--grafit)}', nizke)
        self.assertNotIn('minmax(0,4fr)', nizke)
        self.assertNotIn('grid-column:8/-1', nizke)
        self.assertNotIn('grid-column:1/8', nizke)
        najnizsie = ''.join(css_bloky(self.css, '@media (min-width:1024px) and (max-height:820px)'))
        # krasa-8: aj v najnižších oknách fotka po spodok okna (žiadne vlastné min-height o 24 px nižšie)
        self.assertNotIn('.snimky{', najnizsie)
        self.assertIn('.udaj,.udaj-siroky{padding:8px 0 9px}', najnizsie)
        # nízke okná sú za pravidlom počítača (min-width:1024px), aby ho prepísali
        self.assertLess(self.css.index('@media (min-width:1024px){'), self.css.index('@media (min-width:1024px) and (max-height:1079px){'))

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
        # sizes každej fotky podľa jej pomeru a orez_dole (krasa-8, Fotky slidera: sizes_slidera)
        for (r, f, _), (_, _, slug, obr) in zip(fotky, snimky):
            ocak_sizes = build.sizes_slidera(f['max_w'], f['max_h'], self.w.slider_orez.get(f['rel'], 0.0))
            self.assertIn(f'sizes="{ocak_sizes}"', obr, slug)
        # bez JS: odložené fotky skryté (noscript img[data-src]), viditeľná len prvá (trieda je)
        self.assertIn('img[data-src]{display:none}', build.NOSCRIPT_CSS)
        # popisy: názov s odkazom na realizáciu, miesto, len roky (bez slova príspevok)
        popisy = re.findall(r'<li class="slider-popis( je)?"><a href="realizacie/([a-z0-9-]+)/">(.*?)</a>(.*?)</li>', slider)
        self.assertEqual([p[1] for p in popisy], ocak)
        self.assertEqual([bool(p[0]) for p in popisy], [True] + [False] * 10)
        # doladenie-1: dlhý názov má navyše kratšiu podobu pre mobil (nastavenia nazov_mobil, span.slider-nazov-kratky),
        # celý názov ostáva v span.slider-nazov (na počítači); krátky je len pri názvoch s nazov_mobil
        kratke = {p['slug']: p['nazov_mobil'] for p in nast['uvod_slider'] if p.get('nazov_mobil')}
        self.assertEqual(len(kratke), 5)
        for (_, slug, nazov, udaje), (r, _, _) in zip(popisy, fotky):
            dvojica = re.fullmatch(r'<span class="slider-nazov">(.*?)</span><span class="slider-nazov-kratky">(.*?)</span>', nazov)
            self.assertEqual(bool(dvojica), slug in kratke, slug)
            if dvojica:
                nazov = dvojica.group(1)
                self.assertEqual(html.unescape(dvojica.group(2)).replace(' ', ' '), kratke[slug])
                self.assertLessEqual(len(kratke[slug]), 40)
            self.assertEqual(html.unescape(nazov).replace(' ', ' '), r['nazov'])
            text = html.unescape(re.sub(r'<[^>]+>', '', udaje)).replace(' ', ' ')
            self.assertNotIn('príspevok', text.lower())
            self.assertNotRegex(text, r'Rok|Roky|Termín')
            if r['miesto']:
                self.assertTrue(text.startswith(r['miesto']), (slug, text))
            # roky pri každej stavbe slidera, bez rokov stavby rok príspevku (RD Ahoj a Vila Holubyho 2026, ROKY.md)
            self.assertTrue(build.Web.roky_karty(r), slug)
            self.assertTrue(text.endswith(build.Web.roky_karty(r)), (slug, text))
        self.assertIn('<ul class="slider-popisy" aria-live="off">', slider)
        # ovládanie: počítadlo ako kóta 01 / 11, Predchádzajúca, Pozastaviť (aria-pressed), Ďalšia; bez JS skryté cez
        # CSS html:not(.js), nie atribútom hidden (doladenie-1: miesto v rozložení od prvého vykreslenia, CLS na mobile)
        ovl = re.search(r'<div class="slider-ovladanie" role="group" aria-label="[^"]+">(.*?)</div>', slider).group(1)
        self.assertIn('html:not(.js) .slider-ovladanie{display:none}', re.sub(r'/\*.*?\*/', '', self.css, flags=re.S))
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
                    "b.setAttribute('aria-pressed', stoj ? 'true' : 'false')"):
            self.assertIn(kus, js)
        self.assertNotIn('ovladanie.hidden', js)
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
        päta): Facebook z firma.json, LinkedIn Mária a Instagram firmy z nastavení (len https na linkedin.com
        a instagram.com), v novej karte s rel noopener; jedna sada ikon. krasa-8 (brána 3): ikona v štvorcovom čipe
        (span.siet-cip), glyf 24 px; v Kontakte za čipom názov siete (span.siet-meno, CSS ho ukáže len na počítači),
        v päte len čipy."""
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertEqual(nast['linkedin'], 'https://www.linkedin.com/in/m%C3%A1rio-bulko-aa176ba0/')
        # Andrej 26. 11. 2026: „zly ig som dal, dajme bmbuilding ig“ a „je to ono“ s odkazom z Google na profil
        # firmy @bmbuilding; 24. 11. tu bol osobný profil konateľa (mariobulko), ten už nikde nie je
        self.assertEqual(nast['instagram'], 'https://www.instagram.com/bmbuilding/')
        nova = 'target="_blank" rel="noopener"'
        cip = '<span class="siet-cip"><svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        fb = f'<a class="siet" href="{self.firma["facebook"]}" {nova} aria-label="Facebook BM Building">{cip}'
        li = f'<a class="siet" href="{nast["linkedin"]}" {nova} aria-label="LinkedIn: {self.firma["konatel"]}">{cip}'
        ig = f'<a class="siet" href="https://www.instagram.com/bmbuilding/" {nova} aria-label="Instagram BM Building">{cip}'
        # JSON-LD: Facebook a Instagram sú profily firmy (sameAs firmy), LinkedIn je profil konateľa (employee)
        uzol = [u for u in self.info('')['ld'] if 'Organization' in u.get('@type', [])][0]
        self.assertEqual(uzol['sameAs'], [self.firma['facebook'], 'https://www.instagram.com/bmbuilding/'])
        self.assertEqual(uzol['employee']['sameAs'], [nast['linkedin']])
        for p in self.ostry.rglob('*.html'):
            self.assertNotIn('mariobulko', p.read_text(encoding='utf-8'), p)
        for cesta in ('', 'kontakt', 'realizacie'):
            s = self.info(cesta)['html']
            kontakt = s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
            pata = s.split('<footer class="pata"', 1)[1].split('</footer>', 1)[0]
            for blok, popisy in ((kontakt, True), (pata, False)):
                siete = re.search(r'<p class="siete [a-z-]+">(.*?)</p>', blok).group(1)
                self.assertEqual(siete.count('<a class="siet"'), 3, cesta)
                # poradie Facebook, LinkedIn, Instagram (Instagram vedľa Facebooku a LinkedInu)
                self.assertTrue(siete.index(fb) < siete.index(li) < siete.index(ig), cesta)
                # svg skryté pred čítačkou v čipe; viditeľný text odkazu je len názov siete v Kontakte a prístupné meno
                # (aria-label) ním začína (WCAG 2.5.3); v päte bez textu
                for a, meno in zip(re.findall(r'<a class="siet"[^>]*>(.*?)</a>', siete), ('Facebook', 'LinkedIn', 'Instagram')):
                    self.assertTrue(a.startswith(cip), a[:160])
                    self.assertEqual(re.sub(r'<[^>]+>', '', a), meno if popisy else '', (cesta, a[-80:]))
                    if popisy:
                        self.assertTrue(a.endswith(f'</svg></span><span class="siet-meno">{meno}</span>'), a[-80:])
                for a in re.findall(r'<a class="siet"[^>]*>', siete):
                    self.assertRegex(a, r'aria-label="(Facebook|LinkedIn|Instagram)\b')
            # ikony sú inline SVG, žiadny cudzí obrázok; názov siete ako text len v span.siet-meno v Kontakte
            self.assertNotIn('>Facebook', pata)
            self.assertNotIn('siet-meno', pata)
            self.assertEqual(s.count('<span class="siet-meno">Facebook</span>'), 1, cesta)
            self.assertEqual(s.count('<span class="siet-meno">'), 3, cesta)
        # pekné stavy: hover len s myšou, focus-visible s výplňou čipu, cieľ dotyku 48 px (päta 44 px)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.siet{display:inline-flex;align-items:center;gap:var(--m-12);min-height:48px;', css)
        self.assertIn('.siet:focus-visible .siet-cip{', css)

    def test_instagram_podla_nastavenia(self):
        """Instagram len s adresou v nastaveniach (kópia nastavení): null ikonu vynechá na všetkých troch miestach,
        http ani cudzí hostiteľ neprejde (build.bezpecna_url)."""
        w = self.w
        povodne = w.instagram
        try:
            for adresa, ma_byt in (('https://www.instagram.com/bmbuilding/', True), (None, False),
                                   ('http://www.instagram.com/bmbuilding/', False),
                                   ('https://instagram.com.zle.sk/bmbuilding/', False), ('javascript:alert(1)', False)):
                nast = copy.deepcopy(w.nastavenia)
                nast['instagram'] = adresa
                w.instagram = build.bezpecna_url(nast.get('instagram'), ['instagram.com'])
                soc = ''.join(w.socialne())
                self.assertEqual('aria-label="Instagram BM Building"' in soc, ma_byt, adresa)
                self.assertEqual(soc.count('<a class="siet"'), 3 if ma_byt else 2, adresa)
                for html_bloku in (w.siete('kontakt-siete', popisy=True), w.pata('', True), w.kontakt_sekcia('')):
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
        # doladenie-1: H1 na fotke 9,4 % šírky stĺpca (33,6 px pri 390 px), väčšie než nadpisy sekcií
        self.assertIn('.uvod-h1{margin-top:0;font-size:clamp(1.5rem,9.4cqi,3rem);', mobil)
        # krasa-8: popis stavby hneď pod fotkou, až pod ním počítadlo a šípky (bez order, poradie DOM)
        self.assertIn('.slider-popisy{position:static;', mobil)
        self.assertNotIn('order:2', mobil)
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
        # doladenie-2 (krasa-8 mal vo vysokom okne riadok pod hrdinom so štítkom O firme a jedinou vetou, overenie 2:
        # osirelý zvyšok): vo vysokom okne (od 1080 px) je nadpis len pre čítačku, prvý odsek je úvodník pod H1 a druhý
        # jeho pokračovanie pod pravým stĺpcom; v nižších oknách a na mobile nadpis viditeľný uvedie celý blok
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertNotIn('.firma-text-nad{position:absolute', pc)
        self.assertNotIn('.firma-text-nad,', pc)
        vysoke = ''.join(css_bloky(css, '@media (min-width:1024px) and (min-height:1080px)'))
        self.assertIn('.firma-text-nad{position:absolute;width:1px;height:1px;', vysoke)
        nizke = ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:1079px)'))
        self.assertIn('.firma-text-nad{grid-area:auto;grid-column:1/5;', nizke)
        self.assertIn('.firma-text-nad{display:flex;', self.css)

    def test_krasa8_mriezka_hrdina_a_cinnost(self):
        """krasa-8 (brána 3, drobné: text pravých stĺpcov 23 px vpravo od čiary mriežky): od 1024 px začína všetko
        v pravom stĺpci hrdinu na čiare 8. stĺpca (žiadny vnútorný okraj vľavo), údaje ležia na piatich stĺpcoch pravej
        časti (druhý stĺpec údajov na čiare 10. stĺpca), karta Činnosti, riadok zákona a blok O firme na tej istej
        mriežke 12 stĺpcov ako vlasové čiary pozadia (text od čiary 5. stĺpca), veta SKSI odsadená o 4 stĺpce
        a 4 medzery. Prehliadač zmeral krasa-8/mriezka/meraj.mjs pri 1024 až 2560 px s odchýlkou 0 px."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        # pozadie aj obsah: rovnaká šírka, počet stĺpcov a medzera
        self.assertIn('.ciary-vnutro{height:100%;width:min(var(--sirka),100% - 2*var(--okraj));margin-inline:auto;display:grid;'
                      'grid-template-columns:repeat(var(--stlpce),minmax(0,1fr));column-gap:var(--medzera)}', css)
        self.assertIn('.obsah{width:min(var(--sirka),100% - 2*var(--okraj));margin-inline:auto}', css)
        self.assertIn('.mriezka{display:grid;grid-template-columns:repeat(var(--stlpce),minmax(0,1fr));column-gap:var(--medzera)}', css)
        # pravý stĺpec hrdinu bez vnútorného okraja vľavo
        for sel in ('.firma-hlava', '.firma-text', '.firma-udaje'):
            for d in re.findall(re.escape(sel) + r'\{([^}]*)\}', css):
                self.assertNotIn('padding-left', d, sel)
        self.assertIn('.firma-hlava{grid-area:hlava}', pc)
        # údaje: 5 stĺpcov pravej časti, Sídlo a Konateľ 2, Na trhu a Oprávnenia 3, V médiách cez všetky
        self.assertIn('.firma-fakty{grid-template-columns:repeat(5,minmax(0,1fr));gap:0 var(--medzera);', pc)
        self.assertIn('.udaj,.udaj-siroky{grid-column:span 3;', pc)
        self.assertIn('.firma-fakty>.udaj:nth-child(odd){grid-column:span 2}', pc)
        self.assertIn('.firma-fakty.firma-media>.udaj{grid-column:1/-1}', pc)
        self.assertLess(pc.index('.firma-fakty>.udaj:nth-child(odd)'), pc.index('.firma-fakty.firma-media>.udaj'))
        # úzky počítač (1024 až 1119 px, o posuvník 17 px nad 1099 px; oprava po overení): hodnota pod ikonou bez
        # odsadenia (meno konateľa sa nezalomí)
        self.assertIn('.udaj dd{margin-left:0}', ''.join(css_bloky(css, '@media (min-width:1024px) and (max-width:1119px)')))
        self.assertNotIn('.udaj dd{margin-left:0}', ''.join(css_bloky(css, '@media (min-width:1024px) and (max-width:1099px)')))
        # Činnosť a zákon na mriežke 12 stĺpcov: hlava 1 až 4, text a otázky od 5. stĺpca; veta SKSI od 5. čiary
        self.assertIn('.cinnost-karta,.zakon-list{grid-template-columns:repeat(var(--stlpce),minmax(0,1fr));'
                      'column-gap:var(--medzera)}', pc)
        self.assertIn('.cinnost-text,.zakon-list .otazky{grid-column:5/-1}', pc)
        self.assertIn('.zakon-hlava{grid-column:1/5;', pc)
        self.assertIn('.cinnost-sksi{margin-left:calc((100% + var(--medzera)) / 3)}', pc)
        self.assertNotIn('calc(var(--medzera) * 2)', css)
        # HTML: údaje v poradí Sídlo, Na trhu, Konateľ, Oprávnenia (2 a 3 stĺpce sa striedajú), karty s hlavou a textom
        telo = self.info('')['telo']
        dl = re.search(r'<dl class="firma-fakty">(.*?)</dl>', telo, re.S).group(1)
        self.assertEqual([html.unescape(k) for k in re.findall(r'<dt><svg[^>]*>.*?</svg><span>([^<]+)</span></dt>', dl)],
                         ['Sídlo', 'Na trhu', 'Konateľ', 'Oprávnenia'])
        cin = telo.split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        self.assertEqual(len(re.findall(r'<article class="cinnost-karta" [^>]*><div class="cinnost-hlava">', cin)), 3)
        self.assertEqual(cin.count('<div class="cinnost-text">'), 3)
        self.assertIn('<div class="zakon-list" role="group" aria-labelledby="zakon-uvod"><div class="zakon-hlava">', cin)

    def test_krasa8_zoznamy_v_riadkoch(self):
        """krasa-8 (brána 3: v CSS columns čiary medzi bodmi v stĺpcoch nesedeli na seba): dlhé zoznamy Činnosti sú od
        768 px mriežka s dvoma stĺpcami a spoločnými riadkami, druhý stĺpec na čiare stĺpca mriežky (medzera mriežky
        medzi stĺpcami). Poradie po stĺpcoch ako v pôvodnom zozname (najprv celý ľavý stĺpec, čítačka číta to isté
        poradie podľa DOM); počet riadkov dá build triedou riadky-N (polovica bodov nahor)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        # CSS columns len v širokom okne (od 1921 px) pre kartu bez zoznamu (odseky bez čiar, test_siroke_okno_css),
        # nikdy pre zoznam
        siroke = ''.join(css_bloky(css, '@media (min-width:1921px)'))
        self.assertNotIn('columns:2', css.replace(siroke, ''))
        self.assertEqual([x.strip() for x in re.findall(r'([^{}]*)\{columns:2', siroke)],
                         ['.cinnost-karta>.cinnost-text:not(:has(>ul)):has(>p+p)'])
        self.assertNotIn('break-inside', css.split('.cinnost-body li{', 1)[1].split('}', 1)[0])
        tablet = ''.join(css_bloky(css, '@media (min-width:768px)'))
        self.assertIn('.cinnost-body-dlhe{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:var(--medzera)}', tablet)
        self.assertIn('.cinnost-body-dlhe[class*="riadky-"]{grid-auto-flow:column}', tablet)
        # počet riadkov pre každý zoznam s triedou riadky-N (dlhé zoznamy na úvode aj zoznam stránky služby, ktorý je
        # mriežkou až od 1921 px); mimo mriežky grid-template-rows nič nerobí
        for n in range(3, build.RIADKY_ZOZNAMU_MAX + 1):
            self.assertIn(f'.cinnost-body.riadky-{n}{{grid-template-rows:repeat({n},auto)}}', css)
        self.assertNotIn(f'riadky-{build.RIADKY_ZOZNAMU_MAX + 1}{{', css)
        # mobil: jeden stĺpec (mriežka až od 768 px)
        self.assertNotIn('.cinnost-body-dlhe{display:grid', css_bez_blokov(css))
        # HTML: riadky-N podľa počtu bodov, body v pôvodnom poradí
        cd = self.texty['cinnost_doslova']
        telo = self.info('')['telo']
        for body in (cd['dozor']['body'], cd['stavbyveduci']['body_2']):
            n = (len(body) + 1) // 2
            ul = re.search(rf'<ul class="cinnost-body cinnost-body-dlhe riadky-{n}">(.*?)</ul>', telo)
            self.assertIsNotNone(ul, n)
            self.assertEqual([html.unescape(re.sub(r'<[^>]+>', '', li)).replace(' ', ' ')
                              for li in re.findall(r'<li>(.*?)</li>', ul.group(1))], body)
        # stránky služieb majú do 1920 px zoznamy v jednom stĺpci: bez triedy cinnost-body-dlhe (mriežka od 768 px),
        # dlhý zoznam má len triedu riadky-N pre dva stĺpce od 1921 px (overenie širokého rozloženia, nález drobné);
        # jednobodový zoznam Stavbyvedúceho bez triedy (fakturačné údaje v Kontakte majú od krasa-8, agent Kontakt,
        # vlastnú triedu kontakt-riadky-N, tá sa zoznamov netýka)
        for slug, triedy in (('stavebny-dozor', ['cinnost-body riadky-10']), ('stavbyveduci', ['cinnost-body', 'cinnost-body riadky-4'])):
            telo = self.info(f'{slug}/')['telo']
            self.assertEqual(re.findall(r'<ul class="(cinnost-body[^"]*)">', telo), triedy, slug)
            self.assertNotIn('cinnost-body-dlhe', telo, slug)
        sluzba = self.w.doslovny_text('skuska', {'body': [f'bod {i}' for i in range(5)]}, stlpce=False, riadky=True)
        self.assertIn('<ul class="cinnost-body riadky-3">', sluzba)
        self.assertIn('<ul class="cinnost-body">', self.w.doslovny_text('skuska', {'body': ['a', 'b', 'c', 'd']}, stlpce=False, riadky=True))
        self.assertIn('<ul class="cinnost-body">', self.w.doslovny_text('skuska', {'body': [f'bod {i}' for i in range(5)]}, stlpce=False))
        # zoznam nad maximum tried ostane bez triedy (body idú po riadkoch, nie každý bod do nového stĺpca)
        dlhy = self.w.doslovny_text('skuska', {'body': [f'bod {i}' for i in range(2 * build.RIADKY_ZOZNAMU_MAX + 2)]})
        self.assertIn('<ul class="cinnost-body cinnost-body-dlhe">', dlhy)
        kratky = self.w.doslovny_text('skuska', {'body': [f'bod {i}' for i in range(5)]})
        self.assertIn('<ul class="cinnost-body cinnost-body-dlhe riadky-3">', kratky)

    def test_krasa8_koniec_karty_a_kratky_zoznam(self):
        """krasa-8 (brána 3): koniec kariet Stavebný dozor a Stavbyvedúci jednou čiarou (zoznam na konci karty bez
        spodnej čiary, karta bez spodného okraja, čiaru kreslí karta; pripnutá hlava 24 px nad ňou); jednobodový zoznam
        Stavbyvedúceho s mierou riadka ako odseky (najviac 40 em, zaokrúhlené nadol na celé stĺpce mriežky, aby čiary
        zoznamu končili na hrane stĺpca)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.cinnost-karta>.cinnost-text>.cinnost-body:last-child{border-bottom:0}', css)
        self.assertIn('.cinnost-karta:has(>.cinnost-text>.cinnost-body:last-child){padding-bottom:0}', css)
        # stránka služby (text tiež cinnost-text, bez karty) si spodnú čiaru zoznamu nechá
        self.assertNotRegex(css, r'(^|[\s,}])\.cinnost-text>\.cinnost-body:last-child')
        dozor = self.info('stavebny-dozor/')['telo']
        self.assertIn('class="sluzba-hlavne atelier-text cinnost-text sluzba-doslova"', dozor)
        self.assertNotIn('cinnost-karta', dozor)
        self.assertIn('.cinnost-karta{display:grid;gap:var(--m-24);padding:clamp(var(--m-32),4vw,var(--m-64)) 0;'
                      'border-bottom:1px solid var(--beton)}', css)
        telo = self.info('')['telo']
        karty = dict(re.findall(r'<article class="cinnost-karta" aria-labelledby="cinnost-([a-z-]+)">(.*?)</article>', telo, re.S))
        self.assertTrue(karty['stavebny-manazment'].endswith('</p></div>'))
        for slug in ('stavebny-dozor', 'stavbyveduci'):
            self.assertTrue(karty[slug].endswith('</li></ul></div>'), slug)
        self.assertIn('.cinnost-text p{max-width:40em}', css)
        self.assertIn('.cinnost-karta .cinnost-body:not(.cinnost-body-dlhe){max-width:40em;max-width:calc(round(down,40em + '
                      'var(--medzera) + 1px,(100% - 7 * var(--medzera)) / 8 + var(--medzera)) - var(--medzera))}', css)
        self.assertEqual(re.findall(r'<ul class="cinnost-body">', karty['stavbyveduci']), ['<ul class="cinnost-body">'])
        # značka bodu je rámik (zaokrúhli sa rovnako pri každej polohe), nie plocha 1,5 px (raz 1 px, raz 2 px)
        self.assertIn('.cinnost-body li::before{content:"";position:absolute;left:2px;top:calc(9px + .75em);width:12px;'
                      'height:0;border-top:1.5px solid var(--modra)}', css)

    def test_krasa8_nadtitulok_bez_zlomu(self):
        """krasa-8 (brána 3: riadok kľúčových slov sa medzi 1024 a 1400 px lámal a čiarka visela v strede): čiarka
        v strede verzálok prvého riadka, miesto (posledné slovo) sa v stĺpci užšom ako 500 px skryje. Prehliadač zmeral
        jeden riadok pri 1024, 1100, 1280, 1333, 1334, 1366, 1439, 1440, 1536, 1680, 1920 a 2560 px (celý text 456 px
        pri 13 px a čiarka s medzerou 36 px)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.uvod-nadtitulok::before{content:"";flex:none;width:24px;height:1px;background:currentColor;'
                      'align-self:flex-start;margin-top:calc(.725em - .5px)}', css)
        self.assertIn('@container (max-width:500px){.uvod-nadtitulok-miesto{display:none}}', css)
        self.assertIn('.firma-hlava{grid-area:slider;container-type:inline-size}', css)
        self.assertIn('.uvod-nadtitulok{display:none}', ''.join(css_bloky(css, '@media (max-width:1023px)')))
        self.assertEqual(self.w.nadtitulok_uvodu([]), '')
        self.assertEqual(self.w.nadtitulok_uvodu(['Bratislava']), '<p class="uvod-nadtitulok"><span>Bratislava</span></p>')
        self.assertEqual(self.w.nadtitulok_uvodu(['Stavebný dozor', 'Pezinok']),
                         '<p class="uvod-nadtitulok"><span>Stavebný dozor<span class="uvod-nadtitulok-miesto"> · Pezinok'
                         '</span></span></p>')

    def test_krasa8_slider_po_spodok_okna(self):
        """krasa-8 (brána 3: pri 1440 x 900 končil slider 48 px a pri 1366 x 768 24 px nad spodkom okna, pás pod ním
        prázdny): od 1024 px siaha fotka slidera od hlavičky po spodok okna, sizes slidera ráta s touto výškou.
        Prehliadač zmeral spodok fotky na spodku okna a tlačidlá Zavolať a Napísať e-mail v okne pri 1024 x 768,
        1280 x 720, 1366 x 768, 1440 x 900, 1536 x 864, 1920 x 1080 a 1280 x 1024."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.snimky{aspect-ratio:auto;flex:1 1 auto;min-height:clamp(480px,calc(100vh - var(--hore)),75vw);'
                      'min-height:clamp(480px,calc(100svh - var(--hore)),75vw)}', pc)
        for blok in ('@media (min-width:1024px) and (max-height:1079px)', '@media (min-width:1024px) and (max-height:820px)'):
            self.assertNotIn('.snimky{', ''.join(css_bloky(css, blok)), blok)
        # sizes slidera ráta s výškou poľa: 100vh mínus hlavička 84 px, pri výške okna do 820 px 64 px (fotka 4 : 3)
        s43 = build.sizes_slidera(4, 3)
        self.assertIn('(min-width: 1024px) and (max-height: 820px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), 133.33vh - 85px, 640px)', s43)
        self.assertIn('(min-width: 1024px) and (max-height: 1079px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(133.33vh - 112px, 100vw))', s43)
        self.assertIn('(min-width: 1024px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(133.33vh - 112px, 100vw), 1307px)', s43)
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        # od 2561 px slider bez presahu (pole 1390 px, test_siroke_okno_sizes), potom položky počítača ako predtým
        self.assertIn('imagesizes="(min-width: 2561px) and (max-height: 820px) max(1390px, ', uvod)
        self.assertIn('(min-width: 1024px) and (max-height: 820px) max(min(57.7vw, ', uvod)

    # okná merania krasa-8 (Fotky slidera): šírka, výška a hustota pixelov; pole slidera zmerané v Chrome 25. 11. 2026
    # (D:/Temp/.../krasa-8/fotky/meranie-pred.json, ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/OPRAVA-FOTKY.md);
    # široké okná (od 1921 px obsah min(vw - 160, 2400) px, SIROKE.md) premerané po zmene 25. 11. 2026
    # (krasa-8/siroke-wf/stavba/skripty/sweep.mjs): pri 2560 x 1440 predtým 1416,7 px, teraz 1470 px; od 2561 px slider
    # bez presahu, pole 1390 px (oprava po overení, krasa-8/siroke-wf/oprava/skripty/hrdina.mjs bez posuvníka, predtým
    # pri 3440 x 1440 1910 px, pri 3840 x 2160 2110 px)
    OKNA_SLIDERA = ((1280, 720, 1), (1366, 768, 1), (1440, 900, 1), (1440, 900, 2), (1536, 864, 1.25), (1920, 1080, 1),
                    (2048, 1152, 1), (2560, 1440, 1), (2752, 1152, 1.25), (3440, 1440, 1), (768, 1024, 2), (390, 844, 3),
                    (360, 780, 3))
    POLE_CHROME = {(1280, 720): (729.8, 656.0), (1366, 768): (778.8, 704.0), (1440, 900): (821.0, 816.0),
                   (1536, 864): (876.0, 780.0), (1920, 1080): (1096.7, 996.0), (2048, 1152): (1171.3, 1068.0),
                   (2560, 1440): (1470.0, 1356.0), (3440, 1440): (1390.0, 1356.0),
                   (768, 1024): (768.0, 768.0), (390, 844): (390.0, 487.5), (360, 780): (360.0, 450.0)}
    # ďalšie okná počítača (krasa-8/fotky/pole-okna*.json, doladenie-2 meraj.mjs pole 25. 11. 2026 po zmene prahu): od
    # 1080 px výšky okna rozhoduje pri úzkych oknách pravý stĺpec hrdinu s odsekom O firme (882 až 974 px), napr. iPad
    # Pro na výšku 1024 x 1366; pri 961 až 1079 px je odsek pod hrdinom a pole má výšku okna mínus hlavička (najviac 75vw)
    POLE_CHROME_DALSIE = {(1024, 768): (583.8, 704.0), (1180, 820): (672.8, 756.0), (1600, 900): (912.9, 816.0),
                          (1024, 970): (583.8, 768.0), (1024, 1366): (583.8, 905.4), (1080, 1920): (615.8, 889.1),
                          (1200, 970): (684.2, 886.0), (1280, 970): (729.8, 886.0), (1440, 970): (821.0, 886.0),
                          (1600, 970): (912.9, 886.0), (1728, 1000): (987.3, 916.0), (1760, 970): (1006.0, 886.0),
                          (1920, 970): (1096.7, 886.0), (1255, 1260): (715.5, 941.3), (1280, 1024): (729.8, 940.0),
                          (1920, 1200): (1096.7, 1116.0), (2560, 1080): (1470.0, 996.0), (3840, 2160): (1390.0, 2076.0),
                          (1921, 1080): (1097.3, 996.0), (2000, 1080): (1143.3, 996.0), (2304, 1296): (1320.7, 1212.0),
                          (2560, 1000): (1470.0, 916.0), (2880, 1620): (1390.0, 1536.0), (3440, 1080): (1390.0, 996.0),
                          (2561, 1440): (1390.0, 1356.0), (2561, 1080): (1390.0, 996.0), (2600, 1080): (1390.0, 996.0),
                          (2752, 1152): (1390.0, 1068.0), (3000, 1080): (1390.0, 996.0),
                          (1920, 976): (1096.7, 892.0), (1680, 980): (959.3, 896.0), (1536, 961): (876.0, 877.0),
                          (1440, 961): (821.0, 877.0), (1648, 1079): (940.7, 995.0), (1648, 1080): (940.7, 996.0),
                          (1728, 1080): (987.3, 996.0), (1728, 1117): (987.3, 1033.0)}

    def test_krasa8_sizes_slidera_podla_pomeru(self):
        """krasa-8, agent Fotky slidera (brána 3: jeden sizes pre všetky fotky rátal s 4 : 3, fotky 16 : 9 mali pri
        1366 x 768 hustotu 0,77 a pri 1920 x 1080 0,81): sizes každej fotky slidera je max(šírka poľa, výška poľa x
        pomer fotky), pri orez_dole delený (1 - orez), na tablete a mobile podľa rámu 1 : 1 a 4 : 5. Výraz nie je
        menší než fotka po object-fit: cover v poli, ktoré zmeral Chrome, o viac než 0,5 px pri žiadnom okne a pri
        oknách merania nie je väčší o viac než 2 % (pri ďalších oknách, kde rozhoduje pravý stĺpec hrdinu, o 12 %)."""
        self.assertEqual(build.sizes_slidera(2400, 1350),
                         '(min-width: 2561px) and (max-height: 820px) max(1390px, 177.78vh - 114px, 853px), '
                         '(min-width: 2561px) max(1390px, min(177.78vh - 149px, 133.33vw)), '
                         '(min-width: 1024px) and (max-height: 820px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), 177.78vh - 114px, 853px), '
                         '(min-width: 1024px) and (max-height: 1079px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(177.78vh - 149px, 133.33vw)), '
                         '(min-width: 1024px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(177.78vh - 149px, 133.33vw), 1742px), '
                         '(min-width: 640px) 177.78vw, 222.22vw')
        # fotka na výšku: na tablete a mobile šírka okna (cover ju kreslí na šírku poľa)
        self.assertTrue(build.sizes_slidera(1800, 2400).endswith('(min-width: 640px) 100vw, 100vw'))
        # RD Ahoj 01 s orez_dole 0,17: obrázok je o 1 / 0,83 vyšší než pole
        self.assertEqual(build.sizes_slidera(2000, 2657, 0.17),
                         '(min-width: 2561px) and (max-height: 820px) max(1390px, 90.69vh - 58px, 435px), '
                         '(min-width: 2561px) max(1390px, min(90.69vh - 76px, 68.02vw)), '
                         '(min-width: 1024px) and (max-height: 820px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), 90.69vh - 58px, 435px), '
                         '(min-width: 1024px) and (max-height: 1079px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(90.69vh - 76px, 68.02vw)), '
                         '(min-width: 1024px) max(min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px), min(90.69vh - 76px, 68.02vw), 889px), '
                         '(min-width: 640px) 100vw, 113.36vw')
        # rozloženie v styl.css, z ktorého vzorec vychádza (pri zmene treba zmeniť aj sizes_slidera)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for kus in ('--okraj:clamp(16px,3.9vw,64px);', '--medzera:clamp(12px,1.6vw,24px);', '--sirka:1760px;',
                    '.snimky{position:relative;list-style:none;margin:0;padding:0;aspect-ratio:4/5;',
                    '.snimka img{width:100%;height:100%;object-fit:cover;'):
            self.assertIn(kus, css)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn(':root{--stlpce:12;--hore:84px}', pc)
        self.assertIn('.slider{margin-left:calc(-1 * max(var(--okraj), (100vw - var(--sirka)) / 2));margin-left:calc(-1 * max(var(--okraj), (100cqw - var(--sirka)) / 2))}', pc)
        # od 2561 px bez presahu (SLIDER_BEZ_PRESAHU, pole 7 stĺpcov obsahu 2400 px)
        self.assertEqual([[r.strip() for r in b.split('\n') if r.strip()] for b in
                          css_bloky(css, f'@media (min-width:{build.SLIDER_BEZ_PRESAHU}px)')], [['.slider{margin-left:0}']])
        self.assertEqual(build.SLIDER_POLE_BEZ_PRESAHU, 1390)
        self.assertIn('"slider slider slider slider slider slider slider . . . . ."', pc)
        self.assertIn(':root{--hore:64px}', ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:820px)')))
        self.assertIn('.snimky,.firma-hlava{aspect-ratio:1/1}', ''.join(css_bloky(css, '@media (min-width:640px) and (max-width:1023px)')))
        # pole podľa vzorca sedí s Chrome do 0,5 px
        for (vw, vh), (pw, ph) in self.POLE_CHROME.items():
            w, h = build.pole_slidera_px(vw, vh)
            self.assertAlmostEqual(w, pw, delta=0.5, msg=(vw, vh))
            self.assertAlmostEqual(h, ph, delta=0.5, msg=(vw, vh))
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        okna = list(self.POLE_CHROME.items()) + list(self.POLE_CHROME_DALSIE.items())
        for r, f, _ in fotky:
            orez = self.w.slider_orez.get(f['rel'], 0.0)
            k = build.pomer_slidera(f['max_w'], f['max_h'], orez)
            for (vw, vh), (pw, ph) in okna:
                potreba = max(pw, k * ph)
                sizes = build.sizes_slidera_px(f['max_w'], f['max_h'], orez, vw, vh)
                self.assertGreaterEqual(sizes, potreba - 0.5, (r['slug'], vw, vh))
                self.assertLessEqual(sizes, potreba * (1.02 if (vw, vh) in self.POLE_CHROME else 1.12), (r['slug'], vw, vh))
            # mobil a tablet aj pri iných šírkach (pevný rám 4 : 5 a 1 : 1)
            for vw in (320, 360, 412, 600, 640, 800, 1023):
                potreba = build.sirka_slidera_px(f['max_w'], f['max_h'], orez, vw, 900)
                sizes = build.sizes_slidera_px(f['max_w'], f['max_h'], orez, vw, 900)
                self.assertGreaterEqual(sizes, potreba - 0.5, (r['slug'], vw))
                self.assertLessEqual(sizes, potreba * 1.001, (r['slug'], vw))

    def test_krasa8_hustota_slidera_aspon_1(self):
        """krasa-8, agent Fotky slidera: pri oknách merania (1280 x 720 až 2560 x 1440, tablet 768 x 1024 pri hustote 2,
        mobil 390 a 360 px pri hustote 3) vyberie prehliadač pre každú fotku slidera súbor aspoň taký široký, ako je
        fotka po cover na obrazovke (hustota aspoň 1), okrem okien, kde nestačí ani celá predloha (strop: najväčší
        súbor je plná šírka predlohy). Široké predlohy 16 : 9 (2844 px) majú v AVIF aj súbor v plnej šírke. Prvá fotka
        (LCP) pri 1440 x 900 a hustote 1 má najviac 400 kB."""
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        snimky = re.findall(r'<li class="snimka snimka-(\d+)[^"]*" data-slug="([a-z0-9-]+)">(.*?)</li>', uvod, re.S)
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        self.assertEqual(len(snimky), len(fotky))
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        stropy = []
        for (r, f, _), (_, slug, obr) in zip(fotky, snimky):
            avif = re.search(r'(?:<source type="image/avif" srcset|data-avif)="([^"]+)"', obr).group(1)
            kandidati = sorted(int(x.split(' ')[1][:-1]) for x in avif.split(', '))
            self.assertEqual(kandidati, self.w.sirky_slidera(f, 'avif'), slug)
            orez = self.w.slider_orez.get(f['rel'], 0.0)
            if f['zaklad'][0] > build.SIRKY[-1] * build.SIRKA_BLIZKO:
                # predloha 16 : 9 (Dvojdom S 01, Hrnčiarovce 01): súbor v plnej šírke predlohy, hotový vo fotky-web
                self.assertEqual(kandidati[-1], f['zaklad'][0], slug)
                self.assertIn(f'-{f["zaklad"][0]}.avif {f["zaklad"][0]}w', avif)
            for vw, vh, dpr in self.OKNA_SLIDERA:
                potreba = build.sirka_slidera_px(f['max_w'], f['max_h'], orez, vw, vh) * dpr
                ziadost = build.sizes_slidera_px(f['max_w'], f['max_h'], orez, vw, vh) * dpr
                vyber = next((s for s in kandidati if s >= ziadost), kandidati[-1])
                # hustota pod 0,999 (sizes sa od fotky po cover líši najviac o 0,5 px CSS)
                if vyber < potreba * 0.999:
                    # strop: najväčší súbor je celá predloha (zdroj alebo zväčšenie Upscaylom), väčší nie je
                    self.assertEqual(vyber, kandidati[-1], (slug, vw, vh, dpr))
                    self.assertGreaterEqual(vyber, f['zaklad'][0] * 0.99, (slug, vw, vh, dpr))
                    stropy.append((slug, f'{vw}x{vh}@{dpr:g}', round(vyber / potreba, 3)))
        # stropy sú len pri hustote 2 a viac (predloha 1978 až 2844 px), nikdy pri hustote 1 a 1,25
        self.assertTrue(all(o.split('@')[1] in ('2', '3') for _, o, _ in stropy), stropy)
        self.assertTrue(all(h >= 0.89 for _, _, h in stropy), stropy)
        # LCP: RD Ahoj 01 pri 1440 x 900 a hustote 1 (súbor z fotky-web, bajty z manifestu)
        r0, f0, _ = fotky[0]
        self.assertEqual(r0['slug'], 'rd-ahoj-bratislava-nove-mesto')
        ziadost = build.sizes_slidera_px(f0['max_w'], f0['max_h'], self.w.slider_orez.get(f0['rel'], 0.0), 1440, 900)
        vyber = next(s for s in self.w.sirky_slidera(f0, 'avif') if s >= ziadost)
        subor = man[f0['sha']]['subory'][f'|{vyber}|avif']
        self.assertLessEqual(subor['bajty'], 400_000, (vyber, subor['bajty']))

    def test_krasa8_sirky_slidera_len_z_predlohy(self):
        """Súbor slidera v plnej šírke predlohy (nad SIRKY[-1] x SIRKA_BLIZKO) len z predlohy: pri pláne predlôh
        (nastroje/predlohy_web.py ho vyrobí) alebo keď je hotový vo fotky-web; inak ostanú štandardné šírky (nikdy
        zväčšenie Lanczosom zo zdroja v obsah/). WebP ostáva do WEBP_MAX, predloha do 2640 px bez súboru navyše."""
        if not self.w.avif:
            self.skipTest('Pillow bez AVIF')
        f = {'zaklad': (2844, 1600), 'sirky_avif': build.sirky_pre(2844), 'sirky': build.sirky_pre(2844, build.WEBP_MAX),
             'fw': None, 'sha': '0' * 64}
        povodne = self.w.predlohy
        try:
            # bez predlohy ani súboru vo fotky-web ani plná šírka, ani 840 (audit naplno, položka 43)
            self.w.predlohy = None
            self.assertEqual(self.w.sirky_slidera(f, 'avif'), [480, 720, 960, 1440, 1920, 2400])
            self.w.predlohy = {}
            self.assertEqual(self.w.sirky_slidera(f, 'avif'), [480, 720, 840, 960, 1440, 1920, 2400, 2844])
            self.assertEqual(self.w.sirky_slidera(f, 'webp'), [480, 720, 960, 1440])
            for w in (2400, 2406, 2640):
                g = dict(f, zaklad=(w, 1600), sirky_avif=build.sirky_pre(w))
                self.assertEqual(self.w.sirky_slidera(g, 'avif'), sorted(build.sirky_pre(w) + [840]), w)
        finally:
            self.w.predlohy = povodne

    def test_krasa8_vysoke_okno_o_firme(self):
        """krasa-8 (brána 3: pri 1920 x 1080 pravý stĺpec s oboma odsekmi O firme plný na 947 z 995 px): vo vysokom okne
        (od 1080 px, doladenie-2) je v hrdinovi prvý odsek O firme a druhý ide pod fotku od čiary 8. stĺpca, priamo pod
        pravý stĺpec ako pokračovanie toho istého textu (doladenie-2, nález drobné: riadok so štítkom O firme a jedinou
        vetou cez celú šírku pôsobil ako osirelý zvyšok); nadpis O firme je tam len pre čítačku. Blok nemá vlastný box
        (display: contents), pohyb prevezmú nadpis a odseky. V nižších oknách celý blok pod hrdinom s viditeľným nadpisom.
        HTML ostáva jeden blok v poradí H1, O firme, údaje. Prehliadač (doladenie-2/meraj.mjs vysoke) pri 1920 x 1080:
        druhý odsek začína na ľavej hrane prvého odseku 32 px pod fotkou, nadpis 1 x 1 px."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.firma-text{display:contents}', pc)
        self.assertIn('.firma-text>p{grid-area:text;margin-top:var(--m-24);', pc)
        self.assertIn('.firma-text>p+p{grid-area:onad;align-self:start;margin:var(--m-32) 0 0 '
                      'calc((100% + var(--medzera)) * 7 / 12);color:var(--grafit);text-wrap:pretty}', pc)
        # riadok cez celú šírku s nadpisom a vlasovou čiarou je preč
        self.assertNotIn('.firma-text-nad,.firma-text>p+p{grid-area:onad', pc)
        self.assertNotIn('border-top-color:transparent', pc)
        vysoke = ''.join(css_bloky(css, '@media (min-width:1024px) and (min-height:1080px)'))
        self.assertIn('.firma-text-nad{position:absolute;width:1px;height:1px;margin:0;overflow:hidden;'
                      'clip-path:inset(50%);white-space:nowrap}', vysoke)
        self.assertIn('.uvod.bez-slidera .firma-text{display:block;grid-area:text}', pc)
        nizke = ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:1079px)'))
        self.assertIn('.firma-text{display:grid;', nizke)
        self.assertNotIn('position:absolute', nizke)
        vnutri = css.split('@media (prefers-reduced-motion:no-preference){', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('.firma-text>*{animation:inherit}', ''.join(css_bloky(vnutri, '@media (min-width:1024px)')))
        self.assertIn('.firma-text>*{animation:none}',
                      ''.join(css_bloky(vnutri, '@media (min-width:1024px) and (max-height:1079px)')))
        telo = self.info('')['telo']
        blok = re.search(r'<div class="firma-text">(.*?)</div>', telo).group(1)
        self.assertEqual(len(re.findall(r'<p>', blok)), 2)
        poradie = [telo.index(k) for k in ('<h1 class="uvod-h1"', '<div class="firma-text">', '<div class="firma-udaje">')]
        self.assertEqual(poradie, sorted(poradie))

    def test_krasa8_mobil_popis_pod_fotkou(self):
        """krasa-8 (brána 3: na mobile pod fotkou najprv ovládanie, až potom názov stavby; pole popisu malo výšku
        najdlhšieho popisu): do 1023 px je popis hneď pod fotkou a pod ním počítadlo a šípky (poradie DOM), popis má vždy
        dva riadky (názov, miesto s rokmi), dlhší text sa skráti tromi bodkami, takže krátky popis nenechá prázdny riadok
        a prepnutie fotky nič neposunie (prehliadač pri 390, 360 a 768 px: výška popisu 40 px pri všetkých 11 fotkách,
        CLS 0)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        mobil = ''.join(css_bloky(css, '@media (max-width:1023px)'))
        self.assertIn('.slider-popisy{position:static;grid-template-columns:minmax(0,1fr);justify-items:stretch;', mobil)
        self.assertIn('.slider-popis a,.slider-udaje{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}', mobil)
        self.assertTrue(mobil.split('.slider-popis{', 1)[1].split('}', 1)[0].endswith('min-width:0'))
        self.assertNotIn('order:', mobil.split('.slider-popisy{', 1)[1].split('}', 1)[0])
        self.assertIn('.slider-popis{grid-area:1/1;', css)
        telo = self.info('')['telo']
        spodok = telo.split('<div class="slider-spodok">', 1)[1]
        self.assertLess(spodok.index('<ul class="slider-popisy"'), spodok.index('<div class="slider-ovladanie"'))
        popisy = re.findall(r'<li class="slider-popis[^"]*">(.*?)</li>', telo)
        self.assertEqual(len(popisy), 11)
        for li in popisy:
            self.assertRegex(li, r'^<a href="[^"]+">.+?</a><span class="slider-udaje">.+</span>$')

    def test_krasa8_sidlo_pod_sebou(self):
        """krasa-8: Sídlo v údajoch hrdinu má ulicu s číslom a PSČ s mestom nezalomiteľné (span.nz), v stĺpci údajov
        (2 z 12 stĺpcov) idú pod seba ako na obálke, nie „Podhorská 9, 902 01“ a samotné „Pezinok“."""
        telo = self.info('')['telo']
        dl = re.search(r'<dl class="firma-fakty">(.*?)</dl>', telo, re.S).group(1)
        sidlo = re.search(r'<span>Sídlo</span></dt><dd>(.*?)</dd>', dl).group(1)
        self.assertEqual(sidlo.replace(' ', ' '), '<span class="nz">Podhorská 9,</span> <span class="nz">902 01 Pezinok</span>')
        self.assertIn('.nz{white-space:nowrap}', self.css)

    def test_krasa8_kontakt_kompaktny(self):
        """krasa-8, agent Kontakt (brána 3: vizitka pripnutá s top 24 px zachádzala pod pripnutú hlavičku 64 až 84 px,
        modrý blok mal odsadenie 158 až 176 px a vpravo dole 200 až 380 px prázdnej plochy, telefón v Mono pôsobil
        roztrhane, na mobile tlačidlá rôznej šírky). Karta nie je pripnutá: od 768 px stojí vpravo vedľa nadpisu
        (verzálka), fakturačné údaje idú pod oboma cez celú šírku, na počítači v dvoch stĺpcoch so spoločnými riadkami
        (trieda kontakt-riadky-N, poradie po stĺpcoch); siete hneď pod tlačidlami. Prehliadač zmeral krasa-8/kontakt/
        meraj.mjs pri 1366, 1440 a 1920 px: karta nikdy pripnutá, ani raz pod hlavičkou."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        zaklad = css_bez_blokov(css)
        tablet = ''.join(css_bloky(css, '@media (min-width:768px)'))
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        mobil = ''.join(css_bloky(css, '@media (max-width:767px)'))
        # modrý blok úvodu: odsadenie 64 až 120 px (predtým rytmus sekcie 88 až 176 px)
        self.assertIn('.stranka-uvod .kontakt{background:var(--modra);color:#fff;--tlmeny:rgba(255,255,255,.8);'
                      'padding-block:clamp(var(--m-64),7vw,120px)}', zaklad)
        # karta nikde pripnutá; od 768 px v riadku s textom, údaje pod oboma; od 1024 px text 1 až 6, karta od 7
        self.assertNotRegex(css, r'\.kontakt-mapa\{[^}]*(sticky|[{;]top:)')
        self.assertIn('.kontakt-text{grid-column:1/5;grid-row:1}', tablet)
        self.assertIn('.kontakt-mapa{grid-column:5/-1;grid-row:1;align-self:start}', tablet)
        self.assertIn('.kontakt-udaje{grid-column:1/-1;grid-row:2}', tablet)
        self.assertIn('.kontakt-text{grid-column:1/7}', pc)
        self.assertIn('.kontakt-mapa{grid-column:7/-1}', pc)
        self.assertIn('.kontakt-fakty{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-flow:column;'
                      'column-gap:var(--medzera)}', pc)
        for n in range(1, 7):
            self.assertIn(f'.kontakt-riadky-{n}{{grid-template-rows:repeat({n},auto)}}', pc)
        # telefón vizitky písmom webu s tabuľkovými číslicami, nie v Mono
        self.assertNotIn('.vizitka a.cisla{font-family:var(--mono)', css)
        self.assertIn('.cisla{font-variant-numeric:tabular-nums}', zaklad)
        # mobil: Zavolať a Napísať e-mail rovnako široké na celý riadok
        self.assertIn('.kontakt-akcie{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--m-8)}', mobil)
        for cesta in ('', 'kontakt', 'realizacie'):
            sek = self.info(cesta)['telo'].split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
            text = sek.split('<div class="kontakt-text">', 1)[1].split('<div class="kontakt-mapa">', 1)[0]
            # poradie: tlačidlá, hneď za nimi siete (v ľavom stĺpci), potom karta a fakturačné údaje
            self.assertRegex(text, r'<div class="kontakt-akcie"[^>]*>.*?</div><p class="siete kontakt-siete">.*</p></div>$')
            self.assertLess(sek.index('class="siete kontakt-siete"'), sek.index('<address class="vizitka'))
            self.assertLess(sek.index('<address class="vizitka'), sek.index('<dl class="fakty kontakt-fakty'))
            self.assertNotIn('class="siete', sek.split('<div class="kontakt-udaje">', 1)[1])
            dl = re.search(r'<dl class="fakty kontakt-fakty kontakt-riadky-(\d+)">(.*?)</dl>', sek)
            self.assertIsNotNone(dl, cesta)
            # bunky Sídlo a Zápis majú triedu fakt-siroky (na mobile cez oba stĺpce, doladenie-2), Bankové spojenie
            # a Číslo účtu fakt-ucet (cez oba stĺpce do 359 px, doladenie-3)
            n = len(re.findall(r'<div(?: class="fakt-(?:siroky|ucet)")?><dt>', dl.group(2)))
            self.assertEqual(n, 8, cesta)
            self.assertEqual(int(dl.group(1)), (n + 1) // 2, cesta)
            # telefón vo vizitke s tabuľkovými číslicami
            self.assertRegex(sek, r'<span class="vizitka-k">tel\.:</span> <a class="cisla" href="tel:')

    def test_krasa8_siete_cipy(self):
        """krasa-8, agent Kontakt (brána 3: ikony sietí drobné a osamotené, asi 6,5 z 10; Andrej: „nech to je pekné
        naozaj“): štvorcový čip 48 px s vlasovou čiarou (na papieri farba čiar webu, v modrom bloku biela 40 %), glyf
        24 px zjednodušený podľa oficiálnych značiek (Facebook plné f, LinkedIn plné in, Instagram fotoaparát čiarou
        1,75) v optickom strede; hover (len myš) a focus-visible: na papieri modrý podklad a biela ikona, v modrom bloku
        biely podklad a modrá ikona; prechod 160 ms, pri obmedzenom pohybe žiadny; v Kontakte od 1024 px názov siete
        15 px, v päte tiež čipy 48 px (audit naplno 25. 11. 2026, položka 33; predtým 44 px). Adresy len z nastavení
        a firma.json (nič napevno v build.py)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        zaklad = css_bez_blokov(css)
        mys = ''.join(css_bloky(css, '@media (hover:hover) and (pointer:fine)'))
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.siet{display:inline-flex;align-items:center;gap:var(--m-12);min-height:48px;color:var(--grafit);'
                      'text-decoration:none;--cip-ciara:var(--beton);--cip-ikona:var(--modra);--cip-plocha:var(--modra);'
                      '--cip-ikona-aktivna:#fff}', zaklad)
        self.assertIn('.siet-cip{display:grid;place-items:center;flex:none;width:48px;height:48px;border:1px solid '
                      'var(--cip-ciara);border-radius:2px;color:var(--cip-ikona);transition:background-color 160ms '
                      'var(--krivka),border-color 160ms var(--krivka),color 160ms var(--krivka)}', zaklad)
        self.assertIn('.siet svg{display:block;width:24px;height:24px}', zaklad)
        aktivny = '{background:var(--cip-plocha);border-color:var(--cip-plocha);color:var(--cip-ikona-aktivna)}'
        self.assertIn('.siet:focus-visible .siet-cip' + aktivny, zaklad)
        self.assertIn('.siet:hover .siet-cip' + aktivny, mys)
        self.assertNotIn('.siet:hover', zaklad)
        # modrý blok úvodu: biela vlasová čiara 40 %, pri hover a fokuse biely podklad a modrá ikona
        self.assertIn('.stranka-uvod .kontakt .siet{color:#fff;--cip-ciara:rgba(255,255,255,.4);--cip-ikona:#fff;'
                      '--cip-plocha:#fff;--cip-ikona-aktivna:var(--modra)}', zaklad)
        # názov siete len na počítači a len v Kontakte, 15 px; päta bez vlastnej menšej veľkosti (čipy 48 px)
        self.assertIn('.siet-meno{display:none;font-size:15px;', zaklad)
        self.assertIn('.kontakt .siet-meno{display:inline}', pc)
        self.assertNotIn('.pata-siete .siet-cip{', css)
        self.assertNotIn('.pata-siete .siet{', css)
        # obmedzený pohyb: čip bez prechodu
        reduce = css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('{transition:none}', 1)[0]
        for sel in ('.siet', '.siet-cip', '.siet-meno'):
            self.assertIn(sel, reduce.strip().split(','))
        # glyfy: 24 x 24, skryté pred čítačkou, Facebook a LinkedIn plné, Instagram čiarou 1,75, v optickom strede

        def hranice(d):
            xs, ys = [], []
            for prikaz, cisla in re.findall(r'([MLHVCZ])([^MLHVCZ]*)', d):
                c = [float(v) for v in re.findall(r'-?\d+(?:\.\d+)?', cisla)]
                if prikaz == 'H':
                    xs += c
                elif prikaz == 'V':
                    ys += c
                elif prikaz in 'MLC':
                    xs += c[0::2]
                    ys += c[1::2]
            return min(xs), min(ys), max(xs), max(ys)
        for svg, mena in ((build.SVG_FACEBOOK, 'Facebook'), (build.SVG_LINKEDIN, 'LinkedIn'), (build.SVG_INSTAGRAM, 'Instagram')):
            self.assertTrue(svg.startswith('<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" '
                                           'focusable="false">'), mena)
            self.assertNotIn('<rect x="1.75"', svg)
        x0, y0, x1, y1 = hranice(re.search(r' d="([^"]+)"', build.SVG_FACEBOOK).group(1))
        self.assertIn('fill="currentColor"', build.SVG_FACEBOOK)
        self.assertAlmostEqual((x0 + x1) / 2, 12, delta=0.25)
        self.assertAlmostEqual((y0 + y1) / 2, 12, delta=0.25)
        self.assertGreaterEqual(y1 - y0, 19)
        x0, y0, x1, y1 = hranice(re.search(r' d="([^"]+)"', build.SVG_LINKEDIN).group(1))
        bodka = re.search(r'<circle fill="currentColor" cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/>', build.SVG_LINKEDIN)
        y0 = min(y0, float(bodka.group(2)) - float(bodka.group(3)))
        self.assertAlmostEqual((x0 + x1) / 2, 12, delta=0.25)
        self.assertAlmostEqual((y0 + y1) / 2, 12, delta=0.25)
        self.assertIn('<g fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3.25" y="3.25" width="17.5" '
                      'height="17.5" rx="5"/><circle cx="12" cy="12" r="4.1"/></g>', build.SVG_INSTAGRAM)
        # adresy len z nastavení a firma.json, nič napevno v kóde
        kod = (KOREN / 'build.py').read_text(encoding='utf-8')
        for url in ('facebook.com/BmBuilding', 'instagram.com/bmbuilding', 'instagram.com/mariobulko', 'linkedin.com/in/m'):
            self.assertNotIn(url, kod)
        w = self.w
        soc = ''.join(w.socialne(popisy=True))
        self.assertEqual(re.findall(r'href="([^"]+)"', soc), [w.firma['facebook'], w.linkedin, w.instagram])
        self.assertEqual(soc.count('target="_blank" rel="noopener"'), 3)

    def test_doladenie1_karty_cely_ram(self):
        """Doladenie-1 (overenie 1, nález vážne, krása asi 6 až 6,5 z 10): karta s malým zdrojom (výrez 4 : 5 pod
        KARTA_MIN_OREZ) už nie je zmenšená celá fotka 4 : 3 v strede rámu na rozmazanej kópii (foto-cela a foto-podklad
        z krasa-8 pôsobili ako pokazená alebo ešte sa načítavajúca fotka), ale výrez 4 : 5 cez celý rám ako ostatné
        karty: jeden obrázok, súbory -45, sizes SIZES_KARTA. Šírky výrezu idú z predlohy (fotky-web), nikdy nad ňu.
        Prehliadač zmeral pri 1440 a 390 px všetkých 26 kariet: fotka vypĺňa celý rám (doladenie-1/meraj.mjs)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for kus in ('foto-cela', 'foto-podklad', '.cap-', '.projekt-foto{isolation'):
            self.assertNotIn(kus, css, kus)
        self.assertIn('.projekt-svetly:hover .projekt-foto img:not(.karta-znak){transform:scale(1.03)}', css)
        for meno in ('KARTA_CELA_MAX', 'KARTA_CELA_MIN', 'KARTA_CELA_ZVACSENIE', 'sizes_karty_cela'):
            self.assertFalse(hasattr(build, meno), meno)
        malych = 0
        for cesta in ['realizacie/'] + [f'{k}/' for k in self.texty['sluzby']]:
            for trieda, karta in self.karty_zoznamu(cesta):
                self.assertNotIn('foto-cela', karta, cesta)
                self.assertNotIn('foto-podklad', karta, cesta)
                obr = re.findall(r'<picture>(.*?)</picture>', karta)
                self.assertEqual(len(obr), 1, cesta)
                img = re.search(r'<img [^>]*>', obr[0]).group(0)
                self.assertRegex(img, r'^<img src="\.\./obrazky/[a-z0-9-]+/\d\d-\d+-45\.webp" srcset="[^"]*-45\.webp \d+w')
                self.assertIn(f'sizes="{build.SIZES_KARTA}"', img, cesta)
                self.assertNotIn('alt=""', img, cesta)
                if 'Vizualizácia' in img:
                    # štítok Vizualizácia nad fotkou, autor ostáva v alt
                    self.assertIn('</picture><span class="stitok-viz">Vizualizácia</span></a>', karta)
                    self.assertIn('autor ', re.search(r'alt="([^"]*)"', img).group(1))
        for r in self.w.realizacie:
            f = self.w.foto_karty(r)
            if not f or not self.w.mala_karta(f):
                continue
            malych += 1
            # najväčší súbor výrezu je výrez predlohy (fotky-web, inak zdroj), nikdy väčší
            sirky = self.w.sirky_variantu(f, 'webp', '45')
            self.assertEqual(sirky[-1], min(build.orez_rozmer(*f['zaklad'], (4, 5))[0], build.SIRKY_OREZU[-1]), r['slug'])
        # RD Sklenárska, RD Devín, Vila X, Devínske lúky, RD Kalvária a Rezidencia Grunty (overenie 1)
        self.assertGreaterEqual(malych, 6)

    def test_doladenie1_menu_cez_celu_obrazovku(self):
        """Doladenie-1 (overenie 1, nález vážne, 360 až 1024 px): otvorený panel mobilného menu má šírku a výšku okna pod
        hlavičkou (predtým šírku obsahu, pás stránky po stranách, spodná hrana presekla riadok H1 alebo údajov), text
        položiek na čiare mriežky (vnútorný okraj = okraj stránky), bez spodnej čiary a prázdneho pásu; lišta Zavolať
        pri otvorenom menu odíde; podmenu Činnosti v poradí kariet Činnosti. Prehliadač zmeral pri 360, 390, 768 a 1024
        px panel od 0 po šírku okna a po spodok okna (doladenie-1/meraj.mjs)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        panel = re.search(r'\.menu-panel\{([^}]*)\}', css).group(1)
        for kus in ('position:absolute', 'left:calc(-1 * var(--okraj))', 'right:calc(-1 * var(--okraj))', 'top:100%',
                    'height:calc(100dvh - var(--hore) + 1px)', 'overflow-y:auto', 'overscroll-behavior:contain',
                    'background:var(--vapno)', 'padding:var(--m-8) var(--okraj) var(--m-32)'):
            self.assertIn(kus, panel)
        self.assertNotIn('border-bottom', panel)
        self.assertNotIn('max-height', panel)
        # panel je len do 1099 px (od 1100 px menu v hlavičke), .hlava .obsah má tam šírku okna mínus 2 okraje
        self.assertIn('.menu-m{display:none}', ''.join(css_bloky(css, '@media (min-width:1100px)')))
        self.assertIn('.hlava .obsah{position:relative;', css)
        self.assertIn('.hlava:has(.menu-m[open])~.lista{transform:translateY(calc(100% + 2px));visibility:hidden}', css)
        # podmenu Činnosti: 1. Stavebný manažment, 2. Stavebný dozor, 3. Stavbyvedúci (karty Činnosti na úvode)
        self.assertEqual(self.w.poradie_sluzieb(), ['stavebny-manazment', 'stavebny-dozor', 'stavbyveduci'])
        poradie_kariet = [c['slug'] for k, c in self.texty['cinnost_doslova'].items() if not k.startswith('_')]
        self.assertEqual(self.w.poradie_sluzieb(), poradie_kariet)
        for cesta in ('', 'realizacie/', 'stavebny-dozor/'):
            panel_html = self.info(cesta)['html'].split('<nav class="menu-panel"', 1)[1].split('</nav>', 1)[0]
            vnutro = re.search(r'</a><ul>(.*?)</ul>', panel_html).group(1)
            podmenu = [html.unescape(x) for x in re.findall(r'>([^<>]+)</a>', vnutro)]
            self.assertEqual(podmenu, ['Stavebný manažment', 'Stavebný dozor', 'Stavbyvedúci'], cesta)

    def test_doladenie1_slider_popis_a_ovladanie(self):
        """Doladenie-1: štítok stavby na počítači najviac po 24 px pred ovládaním (16 + 261 + 16 + 24 px; pri 1024 až
        1320 px sa predtým prekrýval o 3 px), riadky bez osamelého slova, roky sa nezalomia; na mobile kratší názov
        (nazov_mobil) namiesto skrátenia tromi bodkami; pri obmedzenom pohybe popis a fotka bez prechodu, fotka sa
        prepne až po dekódovaní a susedné fotky sa načítajú hneď. Prehliadač zmeral pri 1024, 1100, 1280 a 1366 px
        medzeru 24 px pri najdlhších popisoch a pri 390 a 360 px žiadny skrátený názov (doladenie-1/meraj.mjs)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.slider-popisy{top:auto;right:auto;left:var(--m-16);bottom:var(--m-16);align-items:end;'
                      'max-width:calc(100% - 317px)}', pc)
        self.assertIn('.slider-popis{padding:10px 14px 11px;font-size:15px;text-wrap:pretty}', pc)
        # ovládanie 261 px: 12 + 4 vnútorný okraj, počítadlo 96 + 6 + 5, tri tlačidlá 44 px, medzery 3 x 2 px
        self.assertIn('.slider-ovladanie{position:absolute;z-index:4;right:var(--m-16);bottom:var(--m-16);flex:none;gap:2px;'
                      'padding:4px 4px 4px 12px;', pc)
        self.assertIn('.slider-pocet{flex:none;width:96px;margin:0 6px 0 5px;', pc)
        self.assertIn('.slider-tl{width:44px;height:44px;', pc)
        self.assertEqual(16 + (12 + 4 + 96 + 6 + 5 + 3 * 44 + 3 * 2) + 16 + 24, 317)
        # pri 1024 až 1099 px medzera 12 px a kratší názov (doladenie-2, test_doladenie2_kratky_nazov_do_1099)
        self.assertEqual(16 + (12 + 4 + 96 + 6 + 5 + 3 * 44 + 3 * 2) + 16 + 12, 305)
        telo = self.info('')['telo']
        popisy = re.findall(r'<li class="slider-popis[^"]*">(.*?)</li>', telo)
        self.assertEqual(len(popisy), 11)
        for li in popisy:
            udaje = re.search(r'<span class="slider-udaje">(.*)</span>$', li).group(1)
            if re.search(r'\d{4}', udaje):
                # rozsah s pevnými medzerami okolo „až“ (sadzba, audit naplno 25. 11. 2026, položka 16)
                self.assertRegex(udaje, r'<span class="cisla nz">\d{4}( až \d{4})?</span>$')
        # mobil: kratší názov do 1023 px (od doladenia-2 aj 1024 až 1099 px), od 1100 px celý
        mobil = ''.join(css_bloky(css, '@media (max-width:1023px)'))
        self.assertIn('.slider-nazov-kratky{display:none}', css_bez_blokov(css))
        self.assertIn('.slider-nazov{display:none}', mobil)
        self.assertIn('.slider-nazov-kratky{display:inline}', mobil)
        # neplatný nazov_mobil sa nepoužije (varovanie), platný áno
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        self.assertEqual(sorted(self.w.slider_nazov), sorted(f['rel'] for r, f, _ in fotky
                                                             if r['slug'] in ('rekonstrukcia-okanikova-bratislava',
                                                                              'novostavba-rd-nitrianske-hrnciarovce',
                                                                              'filmovy-klub-lumiere-spitalska-bratislava',
                                                                              'ufo-most-snp-bratislava',
                                                                              'rekonstrukcia-venturska-bratislava')))
        for kratky in self.w.slider_nazov.values():
            self.assertLessEqual(len(kratky), 40)
        povodne = (self.w.nastavenia['uvod_slider'], self.w._slider, self.w.slider_nazov, self.w.slider_orez)
        try:
            self.w.nastavenia['uvod_slider'] = [dict(povodne[0][0], nazov_mobil='x' * 41), dict(povodne[0][1], nazov_mobil=7)]
            self.w._slider = None
            err = io.StringIO()
            with redirect_stderr(err):
                self.w.slider_fotky()
            self.assertEqual(self.w.slider_nazov, {})
            self.assertEqual(err.getvalue().count('nazov_mobil'), 2)
        finally:
            (self.w.nastavenia['uvod_slider'], self.w._slider, self.w.slider_nazov, self.w.slider_orez) = povodne
        # obmedzený pohyb: aktívna fotka a popis bez prechodu (dve triedy prebijú .slider-popis{transition:none})
        reduce = ''.join(css_bloky(css, '@media (prefers-reduced-motion:reduce)'))
        self.assertIn('.snimka.je,.slider-popis.je{transition:none}', reduce)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("img.decode().then(raz, raz)", "img.addEventListener('load', dekoduj, { once: true })",
                    'if (!pohyb) {', 'priprav(1); priprav(snimky.length - 1);',
                    'priprav((i - 1 + snimky.length) % snimky.length)'):
            self.assertIn(kus, js)

    def test_doladenie1_nadpisy_a_medzery(self):
        """Doladenie-1, drobné nálezy: H1 úvodu je najväčší text stránky (nadpisy sekcií cez --nadpis: mobil a tablet
        28 až 44 px, počítač 34 až 56 px; prehliadač zmeral H1 väčšie alebo rovné pri 360, 390, 768, 1024 x 768,
        1280 x 720, 1366 x 768, 1440 x 900 a 1920 x 1080), karta Kontaktu na verzálke nadpisu cez tú istú premennú;
        Kontakt odsadený 64 až 120 px na každej stránke, na /kontakt/ pod hlavou 32 až 48 px; zoznam a galéria končia
        48 až 96 px; vizitka od 1680 px na 5 stĺpcoch; časová os realizácií až od 640 px; riadok O firme vo vysokom
        okne bližšie k Činnosti a druhý odsek O firme v miere prvého (pretty); bočné tlačidlo služby bez Mono."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        zaklad = css_bez_blokov(css)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        tablet = ''.join(css_bloky(css, '@media (min-width:768px)'))
        root = re.search(r':root\{([^}]*)\}', css).group(1)
        self.assertIn('--nadpis:clamp(1.75rem,8.2vw,2.75rem)', root)
        self.assertIn(':root{--nadpis:clamp(2rem,3.3vw,3.5rem)}', pc)
        self.assertIn('.nadpis{font-size:var(--nadpis);', zaklad)
        self.assertIn('.kontakt-mapa{margin-top:calc(.145 * var(--nadpis))}', tablet)
        self.assertNotIn('5.2vw,5rem', css)
        # H1 úvodu: mobil 9,4 % šírky stĺpca (najmenej 1,5 rem, najviac 3 rem), počítač 9,8 % (najviac 4 rem a 6 % výšky
        # nízkeho okna); nadpis sekcie pri tých istých šírkach menší (1440 px: 47,5 px proti 52,9 px)
        self.assertIn('.uvod-h1{margin-top:0;font-size:clamp(1.5rem,9.4cqi,3rem);', ''.join(css_bloky(css, '@media (max-width:1023px)')))
        self.assertLessEqual(16 * 2.75, 16 * 3)                    # tablet: nadpis najviac 44 px, H1 48 px
        self.assertLessEqual(1440 * 0.033, 52.89)                  # 1440 x 900
        self.assertLessEqual(1366 * 0.033, 768 * 0.06)             # 1366 x 768: H1 = 6 % výšky okna
        self.assertLessEqual(1280 * 0.033, 720 * 0.06)             # 1280 x 720
        # medzery
        self.assertIn('.kontakt{background:var(--vapno-2);--tlmeny:#5d5d58;padding-block:clamp(var(--m-64),7vw,120px)}', zaklad)
        self.assertIn('.hlava-kontakt+.kontakt{padding-top:clamp(var(--m-32),3vw,var(--m-48))}', zaklad)
        self.assertIn('<div class="hlava-stranky hlava-kontakt">', self.info('kontakt/')['telo'])
        self.assertIn('.obsah.zoznam{padding-bottom:clamp(var(--m-48),5vw,var(--m-96))}', zaklad)
        self.assertIn('.galeria-sekcia{padding-top:clamp(40px,6vw,88px);padding-bottom:clamp(var(--m-48),5vw,var(--m-96))}', zaklad)
        self.assertIn('.kontakt-mapa{grid-column:8/-1}', ''.join(css_bloky(css, '@media (min-width:1680px)')))
        self.assertIn('.casova-mierka{display:none}', ''.join(css_bloky(css, '@media (max-width:639px)')))
        # vysoké okno (od 1080 px, doladenie-2): hrdina bez spodného odsadenia, Činnosť s menším horným, kotva pristane
        # 24 px nad nadpisom
        vysoke = ''.join(css_bloky(css, '@media (min-width:1024px) and (min-height:1080px)'))
        self.assertIn('.uvod{padding-bottom:0}', vysoke)
        self.assertIn('.cinnost-sekcia{padding-top:clamp(56px,4vw,72px);scroll-margin-top:calc(var(--m-24) - clamp(56px,4vw,72px))}', vysoke)
        self.assertIn('text-wrap:pretty}', pc.split('.firma-text>p+p{grid-area:onad;', 1)[1].split('\n', 1)[0])
        nizke = ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:1079px)'))
        self.assertIn('.firma-text>p+p{margin-top:var(--m-12);text-wrap:pretty}', nizke)
        self.assertNotIn('.firma-text>p+p{margin-top:var(--m-12);text-wrap:balance}', css)
        # bočné tlačidlo služby: číslo písmom webu s tabuľkovými číslicami ako vizitka
        self.assertIn('.bok-tlacidlo .cisla{font-weight:400}', zaklad)
        self.assertNotRegex(css, r'\.bok-tlacidlo[^{]*\{[^}]*var\(--mono\)')
        self.assertIn('<span class="cisla">+421', self.info('stavebny-dozor/')['telo'].split('class="bok-tlacidlo"', 1)[1][:200])

    def test_doladenie1_sidlo_a_ico_nezalomene(self):
        """Doladenie-1, drobné: Sídlo vo fakturačných údajoch Kontaktu (úvod, /kontakt/, záver podstránok) a v päte
        po častiach nezalomiteľné ako v hrdinovi („Podhorská 9,“ a „902 01 Pezinok“), v päte „IČO“ s pevnou medzerou
        pred číslom. Prehliadač pri 360 a 390 px: PSČ a mesto na jednom riadku, IČO s číslom (doladenie-1/meraj.mjs)."""
        sidlo = '<span class="nz">Podhorská 9,</span> <span class="nz">902\u00a001 Pezinok</span>'
        for cesta in ('', 'kontakt/', 'realizacie/', 'stavebny-dozor/'):
            telo = self.info(cesta)['html']
            dl = telo.split('<dl class="fakty kontakt-fakty', 1)[1].split('</dl>', 1)[0]
            dd = re.search(r'<dt>Sídlo</dt><dd>(.*?)</dd>', dl).group(1)
            self.assertEqual(dd, sidlo, cesta)
            pata = telo.split('<p class="pata-veta">', 1)[1].split('</p>', 1)[0]
            self.assertEqual(pata, f'BM Building, s.r.o., {sidlo}. IČO\u00a035\u00a0947\u00a0519.', cesta)

    # pravý stĺpec hrdinu vo vysokom okne (nadtitulok až tlačidlá, s prvým odsekom O firme) podľa šírky okna v CSS px,
    # zmeraný v Chrome 25. 11. 2026 (doladenie-2, meraj.mjs sweep: okno 1400 px na výšku, každých 8 px šírky 1024 až
    # 1920 px; najvyšší 878 px pri 1648 px, kde je H1 takmer 64 px a odsek má 5 riadkov). Do 1920 px výška okna stĺpec
    # nemení. Od 1921 px (široké okno, SIROKE.md) rastie H1 s oknom až po 7svh, stĺpec je preto zmeraný pri výške 1080 px,
    # kde je voči oknu najvyšší (siroke-wf/stavba/skripty/sweep.mjs: 873,8 px od 2176 px; predtým 826 px pri 2560 px)
    STLPEC_VYSOKE = {1024: 809, 1200: 786, 1280: 798, 1368: 812, 1440: 824, 1536: 850, 1600: 866, 1648: 878,
                     1680: 866, 1728: 876, 1768: 876, 1800: 846, 1920: 826, 2048: 852.4, 2176: 873.8, 2560: 873.8,
                     3440: 873.8}
    # doladenie-3, Chrome po oprave (meraj.mjs prva, posun hore, slider pozastavený): okno -> (spodok fotky pod
    # spodkom okna px, najmenšia rezerva tlačidiel, štítka a ovládania nad spodkom okna px). Pred opravou napr.
    # 1366 x 610 (27,5; -11,5), 1600 x 600 (37,6; -21,6), 1600 x 560 (67,6; -51,6), 1600 x 648 (1,5; 14,5).
    # Sweep 891 okien (11 šírok 1024 až 1920 px, výšky 540 až 700 px po 2 px): chyba len pri 540 až 544 px, kde
    # nestačí ani najmenšia výška fotky 480 px s hlavičkou 64 px; 525 okien do 1400 px výšky bez chyby.
    DOLADENIE3_NIZKE = {'1024x560': (0, 10.5), '1024x600': (0, 16), '1024x640': (0, 16), '1024x660': (0, 16),
                        '1280x600': (0, 16), '1366x610': (0, 16), '1366x640': (0, 16), '1440x600': (0, 16),
                        '1536x620': (0, 16), '1600x560': (0, 9.2), '1600x600': (0, 16), '1600x648': (0, 16),
                        '1600x660': (0, 16), '1680x600': (0, 16), '1792x600': (0, 16), '1920x560': (0, 16)}
    # doladenie-3, Chrome po oprave (meraj.mjs medzery): (stránka, hranica, okno) -> od posledného textu bloku po
    # nadpis ďalšej sekcie px; pred opravou 302 až 389 px pri 1366 až 1920 px, pred Kontaktom 246 až 296 px
    DOLADENIE3_MEDZERY = {
        ('stavebny-manazment', 'karty', '1366x768'): 174, ('stavebny-manazment', 'karty', '1440x900'): 181,
        ('stavebny-manazment', 'karty', '1920x1080'): 189, ('stavebny-manazment', 'kontakt', '1366x768'): 164,
        ('stavebny-manazment', 'kontakt', '1440x900'): 173, ('stavebny-manazment', 'kontakt', '1920x1080'): 196,
        ('stavebny-dozor', 'karty', '1366x768'): 138, ('stavebny-dozor', 'karty', '1440x900'): 145,
        ('stavebny-dozor', 'karty', '1920x1080'): 153, ('stavebny-dozor', 'otazky', '1366x768'): 137,
        ('stavebny-dozor', 'otazky', '1440x900'): 144, ('stavebny-dozor', 'otazky', '1920x1080'): 152,
        ('stavebny-dozor', 'kontakt', '1440x900'): 173, ('stavebny-dozor', 'kontakt', '1920x1080'): 196,
        ('stavbyveduci', 'otazky', '1366x768'): 167, ('stavbyveduci', 'otazky', '1440x900'): 181,
        ('stavbyveduci', 'otazky', '1920x1080'): 172, ('stavbyveduci', 'kontakt', '1440x900'): 173,
        ('stavbyveduci', 'kontakt', '1920x1080'): 196}

    def test_doladenie2_prah_vysokeho_okna(self):
        """Doladenie-2, nález vážne (overenie 2: pri 961 až 1058 px výšky okna stĺpec s prvým odsekom O firme natiahol
        fotku pod spodok okna; štítok a ovládanie 10 až 50 px pod oknom, pri 1728 x 1000 aj tlačidlá Zavolať a Napísať
        e-mail): vysoké okno s odsekom v hrdinovi až od 1080 px výšky, ten istý prah v styl.css (max-height:1079px
        a min-height:1080px) aj v sizes slidera (build.SLIDER_VYSOKE_OKNO). Stĺpec s riadkami 48 px nad a pod ním
        a hlavičkou 84 px sa pri každej zmeranej šírke zmestí s rezervou aspoň 20 px (najviac 1058 px). Prehliadač
        po oprave (doladenie-2/meraj.mjs vysoke a sweep, výšky 961 až 1400 px pri šírke 1024 až 2560 px): spodok fotky
        na spodku okna, štítok, ovládanie aj tlačidlá v okne; 1920 x 1080 a 2560 x 1440 bez zmeny. Široké okno (od
        1921 px, SIROKE.md): H1 rastie s oknom najviac po 7svh, stĺpec pri prahu 1080 px najviac 873,8 px, s riadkami
        a hlavičkou 1053,8 px (rezerva 26,2 px), test_siroke_okno_prva_obrazovka."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertEqual(build.SLIDER_VYSOKE_OKNO, 1079)
        # prahy výšky okna na počítači: 660 (ešte hustejšie údaje, doladenie-3), 820 (hustejšie údaje), 960 (len H1
        # najviac 6 % výšky okna, ako pred doladením 2) a SLIDER_VYSOKE_OKNO (odsek O firme v hrdinovi až od 1080 px)
        prahy = sorted({int(x) for x in re.findall(r'@media \(min-width:1024px\) and \(max-height:(\d+)px\)', css)})
        self.assertEqual(prahy, [660, 820, 960, build.SLIDER_VYSOKE_OKNO])
        self.assertEqual(''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:960px)')).strip(),
                         '.uvod-h1{font-size:min(clamp(1.75rem,9.8cqi,var(--h1-strop)),6svh)}')
        self.assertEqual(re.findall(r'@media \(min-width:1024px\) and \(min-height:(\d+)px\)', css),
                         [str(build.SLIDER_VYSOKE_OKNO + 1)])
        self.assertNotIn('(min-height:961px)', css)
        for w, stlpec in self.STLPEC_VYSOKE.items():
            potreba = stlpec + 2 * 48 + build.SLIDER_HLAVICKA
            self.assertLessEqual(potreba + 20, build.SLIDER_VYSOKE_OKNO + 1, w)
            # sizes vo vysokom okne ráta s poľom aspoň takým vysokým ako stĺpec s riadkami (horný odhad)
            self.assertGreaterEqual(build.SLIDER_STLPEC, stlpec + 2 * 48, w)
        # okná z nálezu majú odsek pod hrdinom, 1920 x 1080 a 2560 x 1440 ostávajú vysoké
        for vh in (961, 969, 976, 980, 1000, 1030, 1058, 1061, 1079):
            self.assertLessEqual(vh, build.SLIDER_VYSOKE_OKNO)
        for vh in (1080, 1117, 1440):
            self.assertGreater(vh, build.SLIDER_VYSOKE_OKNO)
        uvod = self.info('')['html']
        self.assertIn(f'(min-width: 1024px) and (max-height: {build.SLIDER_VYSOKE_OKNO}px) max(', uvod)
        self.assertNotIn('max-height: 960px', uvod)

    def test_doladenie2_trieda_js_bez_casovaca(self):
        """Doladenie-2 (overenie 2, Slow 3G 50 kB/s s latenciou 2 s: trieda js zmizla 4 s po skripte v hlave, kým sa
        app.js ešte sťahoval; ovládanie slidera na 0,7 s zmizlo a údaje pod ním skočili o 48 px hore a späť, CLS 0,0263):
        vložený skript zruší triedu js len pri DOMContentLoaded a len keď app.js nepribehol. Odložený skript sa spustí
        vždy pred DOMContentLoaded, pri chybe načítania DOMContentLoaded príde tiež. Simulácia v node: app.js spustený
        až po 5 s (pred DOMContentLoaded) triedu nechá, app.js, ktorý nepribehol, ju zruší; žiadny časovač."""
        self.assertNotIn('setTimeout', build.SKRIPT_JS)
        self.assertIn("document.addEventListener('DOMContentLoaded',function(){if(window.BMB==null)d.classList.remove('js')})",
                      build.SKRIPT_JS)
        hlava = self.info('')['html'].split('</head>', 1)[0]
        self.assertIn(f'<script>{build.SKRIPT_JS}</script>', hlava)
        self.assertRegex(hlava, r'<script src="app\.js\?v=[0-9a-f]+" defer></script>')
        self.assertIn('window.BMB = 1;', (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8'))
        node = shutil.which('node')
        if not node:
            self.skipTest('node nie je k dispozícii')
        import subprocess
        sim = r"""
const vm = require('vm');
const skript = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function beh(app) {
  const triedy = new Set(); const udalosti = {}; const casovace = [];
  const document = { documentElement: { classList: { add: (c) => triedy.add(c), remove: (c) => triedy.delete(c) } },
    addEventListener: (t, f) => { (udalosti[t] = udalosti[t] || []).push(f); } };
  const ctx = { document, setTimeout: (f, ms) => { casovace.push(ms); }, setInterval: (f, ms) => { casovace.push(ms); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(skript, ctx);
  const poSkripte = triedy.has('js');
  // odložený app.js: na pomalej sieti až o 5 s, vždy pred DOMContentLoaded; pri chybe načítania sa nespustí
  if (app === 'neskoro') vm.runInContext("window.BMB = 1; document.documentElement.classList.add('js');", ctx);
  (udalosti.DOMContentLoaded || []).forEach((f) => f());
  return { poSkripte, poDOM: triedy.has('js'), casovace };
}
console.log(JSON.stringify({ neskoro: beh('neskoro'), chyba: beh('chyba') }));
"""
        p = subprocess.run([node, '-e', sim], input=json.dumps(build.SKRIPT_JS), capture_output=True, text=True,
                           encoding='utf-8', timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        v = json.loads(p.stdout)
        self.assertEqual(v['neskoro'], {'poSkripte': True, 'poDOM': True, 'casovace': []})
        self.assertEqual(v['chyba'], {'poSkripte': True, 'poDOM': False, 'casovace': []})

    def test_doladenie2_lightbox_bez_zastupnej_fotky(self):
        """Doladenie-2 (overenie 2: pri prvom otvorení lightboxu na detaile sa stiahla aj zástupná fotka dialógu, napr.
        Lumière 06-1440.webp, 211 882 B, ktorá sa nikdy neukázala): ukaz() v app.js prepne loading na eager až po novom
        zdroji (AVIF source, srcset, src), takže lenivý obrázok zatvoreného dialógu stiahne len vybraný súbor. ukaz()
        beží pred showModal. Prehliadač (doladenie-2/meraj.mjs lightbox, Lumière 1440 a 390 px, tabuľka ASB a 1. a 3.
        fotka galérie): zástupná fotka pred opravou v 6 zo 6 otvorení, po oprave v 0 zo 6."""
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        ukaz = js.split('var ukaz = function (i) {', 1)[1].split('var otvor = function', 1)[0]
        self.assertEqual(ukaz.count("setAttribute('loading'"), 1)
        eager = ukaz.index("foto.setAttribute('loading', 'eager')")
        for kus in ("lbAvif.setAttribute('srcset', av)", "foto.setAttribute('srcset', srcsetZ(a))",
                    "foto.setAttribute('src', plnaZ(a))"):
            self.assertLess(ukaz.index(kus), eager, kus)
        otvor = js.split('var otvor = function', 1)[1].split('};', 1)[0]
        self.assertLess(otvor.index('ukaz(i);'), otvor.index('dlg.showModal();'))
        img = re.search(r'<img class="lightbox-foto"[^>]*>',
                        self.info('realizacie/filmovy-klub-lumiere-spitalska-bratislava/')['telo']).group(0)
        self.assertIn('loading="lazy"', img)

    def test_doladenie2_karta_lumiere_cely_napis(self):
        """Doladenie-2, nález drobné (overenie 2: výrez 4 : 5 zo stredu Lumière 08 odrezal z nápisu KINO LUMIERE
        písmená LU, na karte sa čítalo „KINO MIERE“): karta_foto Lumière má rámik [0, 0, 0.7, 1], výrez 4 : 5 zo
        stredu rámika začína do 10 % šírky zdroja a nápis (14 % až 33 % šírky) je celý; výrez má aspoň 768 px zdroja.
        Súbory -45 karty sú z fotky-web s tým istým rámikom (nastroje/predlohy_web.py --len), karta na /realizacie/
        a na stránke služby berie súbor -45."""
        lum = 'filmovy-klub-lumiere-spitalska-bratislava'
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertEqual(nast['karta_foto'][lum], {'subor': '08.jpg', 'vyrez': [0, 0, 0.7, 1]})
        r = next(r for r in self.w.realizacie if r['slug'] == lum)
        f = r['karta']
        self.assertEqual(f['rel'], f'{lum}/08.jpg')
        self.assertEqual(f['vyrez45'], (0.0, 0.0, 0.7, 1.0))
        zw, zh = f['zaklad']
        x0, y0, x1, y1 = build.box_px(f['vyrez45'], zw, zh)
        cw, _ = build.orez_rozmer(x1 - x0, y1 - y0, build.OREZY['45'])
        zac = x0 + (x1 - x0 - cw) // 2
        self.assertLessEqual(zac / zw, 0.10)
        self.assertGreaterEqual((zac + cw) / zw, 0.33)
        bx0, by0, bx1, by1 = build.box_px(f['vyrez45'], f['w'], f['h'])
        self.assertGreaterEqual(build.orez_rozmer(bx1 - bx0, by1 - by0, build.OREZY['45'])[0], 768)
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        z = man[f['sha']]
        vyrezy = {k: s.get('vyrez') for k, s in z['subory'].items() if k.startswith('45|')}
        self.assertTrue(vyrezy)
        self.assertTrue(all(v == [0, 0, 0.7, 1] for v in vyrezy.values()), vyrezy)
        for k, s in z['subory'].items():
            if k.startswith('45|'):
                self.assertTrue((KOREN / 'fotky-web' / s['subor']).is_file(), k)
        # karta na /realizacie/ (a na stránke služby, ak tam Lumière kartu má) berie výrez -45 z tohto rámika
        odkaz = f'class="projekt-foto" href="../realizacie/{lum}/"'
        karty = 0
        for cesta in ('realizacie/', 'stavebny-dozor/', 'stavebny-manazment/', 'stavbyveduci/'):
            casti = self.info(cesta)['telo'].split(odkaz, 1)
            if len(casti) == 1:
                self.assertNotEqual(cesta, 'realizacie/')
                continue
            karty += 1
            obr = re.search(r'<img [^>]*>', casti[1]).group(0)
            self.assertRegex(obr, rf'{lum}/\d\d-\d+-45\.webp', cesta)
        self.assertGreaterEqual(karty, 1)

    def test_doladenie2_kratky_nazov_do_1099(self):
        """Doladenie-2, nález drobné (overenie 2: pri 1024 px mal štítok Okánikovej na fotke 5 riadkov, 118 px, Lumière,
        Na Križovatkách a Ventúrska 4 riadky, tmavý blok zakryl ľavý spodok fotky): kratší názov (nazov_mobil) aj pri
        1024 až 1099 px, riadok miesta a rokov vyvážený, text štítku o 16 px širší (medzera k ovládaniu 12 px, bočný
        okraj 12 px); od 1100 px celý názov a medzera 24 px. Prehliadač (meraj.mjs popis a sirky): pri 1024 px najvyšší
        štítok 118 na 80 px (najviac 3 riadky pri všetkých 11 stavbách), pri 1050 až 1099 px najviac 80 px."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        uzky = ''.join(css_bloky(css, '@media (min-width:1024px) and (max-width:1099px)'))
        for kus in ('.slider-nazov{display:none}', '.slider-nazov-kratky{display:inline}', '.slider-udaje{text-wrap:balance}',
                    '.slider-popisy{max-width:calc(100% - 305px)}', '.slider-popis{padding-inline:12px}'):
            self.assertIn(kus, uzky)
        self.assertIn('.slider-nazov-kratky{display:none}', css_bez_blokov(css))
        for blok in ('@media (min-width:1100px)', '@media (min-width:1024px)'):
            self.assertNotIn('.slider-nazov-kratky{display:inline}', ''.join(css_bloky(css, blok)), blok)
        # blok úzkeho počítača je za blokom počítača (prepíše ho)
        self.assertLess(css.index('@media (min-width:1024px){'), css.index('@media (min-width:1024px) and (max-width:1099px){'))

    def test_doladenie2_lista_na_hranach_obsahu(self):
        """Doladenie-2, nález drobné (overenie 2: spodná lišta mala okraj 12 px proti 16 px obsahu a pomer tlačidiel
        1,25 : 1, tie isté tlačidlá v Kontakte 1 : 1): lišta s okrajom stránky a dvoma rovnakými stĺpcami s medzerou 8 px
        ako v Kontakte. Prehliadač (meraj.mjs lista): pri 360, 390 a 412 px tlačidlá na hranách obsahu, rovnako široké,
        text nepretečie ani pri 320 px."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        lista = deklaracie(css_bez_blokov(css), '.lista')
        self.assertEqual(lista['grid-template-columns'], 'repeat(2,minmax(0,1fr))')
        self.assertEqual(lista['gap'], 'var(--m-8)')
        self.assertEqual(lista['padding'], '10px var(--okraj) calc(10px + env(safe-area-inset-bottom))')
        self.assertIn('.obsah{width:min(var(--sirka),100% - 2*var(--okraj));', css)
        self.assertIn('.kontakt-akcie{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--m-8)}',
                      ''.join(css_bloky(css, '@media (max-width:767px)')))

    def test_doladenie2_kontakt_kompaktnejsi(self):
        """Doladenie-2, nález drobné (overenie 2: modrý Kontakt pri 390 px 1393 px, z toho fakturačné údaje 565 px;
        na počítači pod riadkom sietí 119 až 157 px prázdnej modrej plochy vedľa vyššej vizitky): do 767 px fakturačné
        údaje v dvoch stĺpcoch so štítkom nad hodnotou, Sídlo a Zápis cez oba stĺpce (trieda fakt-siroky); od 768 px
        ľavý stĺpec ako flex a riadok sietí na jeho spodku v osi s tlačidlom Navigovať, pod tlačidlami aspoň 24 px.
        Prehliadač (meraj.mjs kontakt) po oprave: pozri DOLADENIE-2.md (výšky blokov a prázdna plocha pod sieťami)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        mobil = ''.join(css_bloky(css, '@media (max-width:767px)'))
        self.assertIn('.kontakt-fakty{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-flow:row dense;'
                      'column-gap:var(--m-16)}', mobil)
        self.assertIn('.kontakt-fakty>div{display:block;', mobil)
        self.assertIn('.kontakt-fakty>.fakt-siroky{grid-column:1/-1}', mobil)
        tablet = ''.join(css_bloky(css, '@media (min-width:768px)'))
        self.assertIn('.kontakt-text{display:flex;flex-direction:column;padding-bottom:calc(var(--m-24) + 3px)}', tablet)
        self.assertIn('.kontakt-akcie{margin-bottom:var(--m-24)}', tablet)
        self.assertIn('.kontakt-siete{margin-top:auto}', tablet)
        # počítač: dva stĺpce so spoločnými riadkami ostávajú, fakt-siroky tam nič nemení
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertNotIn('fakt-siroky', pc)
        self.assertNotIn('fakt-siroky', tablet)
        self.assertEqual(build.KONTAKT_SIROKE, ('Sídlo', 'Zápis'))
        for cesta in ('', 'kontakt/', 'realizacie/'):
            dl = self.info(cesta)['telo'].split('<dl class="fakty kontakt-fakty', 1)[1].split('</dl>', 1)[0]
            # fakt-ucet (Bankové spojenie a Číslo účtu cez oba stĺpce len do 359 px, doladenie-3) je pri 360 až 767 px
            # úzka bunka ako ostatné
            bunky = [(t == 'siroky', k) for t, k in re.findall(r'<div(?: class="fakt-(siroky|ucet)")?><dt>([^<]+)</dt>', dl)]
            self.assertEqual(len(bunky), 8, cesta)
            self.assertEqual([k for t, k in bunky if t], ['Sídlo', 'Zápis'], cesta)
            # v dvoch stĺpcoch bez diery: úzke bunky medzi širokými po pároch
            uzke = 0
            for t, k in bunky:
                if t:
                    self.assertEqual(uzke % 2, 0, (cesta, k))
                    uzke = 0
                else:
                    uzke += 1
            self.assertEqual(uzke % 2, 0, cesta)

    def test_doladenie2_logo_hlavicky_hned(self):
        """Doladenie-2, nález drobné (overenie 2: pri Slow 4G a CPU 4x sa logo v hlavičke ukázalo až asi 1,6 s po
        začiatku, dovtedy hlavička len s tlačidlami): každá stránka má v hlave preload loga hlavičky s vysokou
        prioritou (rovnaká adresa ako obrázok v hlavičke), LCP fotka ostáva jediný obrázok s fetchpriority."""
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            hlava = s.split('</head>', 1)[0]
            src = re.search(r'<img class="znak-logo" src="([^"]*logo-hlavicka\.svg)"', s).group(1)
            self.assertIn(f'<link rel="preload" href="{src}" as="image" type="image/svg+xml" fetchpriority="high">',
                          hlava, subor)
            self.assertEqual(hlava.count('logo-hlavicka.svg'), 1, subor)
            self.assertLessEqual(len(re.findall(r'<img[^>]*fetchpriority="high"[^>]*>', s)), 1, subor)

    def test_doladenie2_cinnost_proti_zadaniu(self):
        """Doladenie-2, nález drobné (overenie 2): doslovný text Činnosti na úvode a na troch stránkach služieb
        porovnaný priamo so zadaním (ZMENY-KOLO-1.md časť 4, blok textu starého webu), nie s texty.json: každý odsek
        a bod znak po znaku okrem medzier (zadanie dovoľuje meniť len medzery). Nadpisy kariet na úvode tiež znak po znaku
        okrem medzier, aj s dvojbodkou „1. Stavebný manažment:“ a „2. Stavebný dozor:“ (rozhodnutie Fable 25. 11. 2026
        po overení 2), skrytý text odkazu na stránku služby je bez dvojbodky. Bez zadania (repozitár webu bez ops/, napr. GitHub Actions) sa test preskočí."""
        zmeny = KOREN.parents[1] / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding' / 'kolo-1' / 'ZMENY-KOLO-1.md'
        if not zmeny.is_file():
            self.skipTest('zadanie kola (ZMENY-KOLO-1.md) nie je dostupné')
        cast4 = zmeny.read_text(encoding='utf-8').split('## 4. Služby doslova', 1)[1].split('\n## 5.', 1)[0]
        blok = re.search(r'```\n(.*?)```', cast4, re.S).group(1)
        casti = []
        for riadok in (r.rstrip() for r in blok.split('\n') if r.strip()):
            if re.match(r'^\d\. ', riadok):
                casti.append({'nadpis': riadok, 'polozky': []})
            else:
                casti[-1]['polozky'].append(('li', riadok[2:]) if riadok.startswith('* ') else ('p', riadok))
        self.assertEqual([len(c['polozky']) for c in casti], [4, 20, 10])

        def bez(s):
            return re.sub(r'\s+', '', s)

        def cisty(s):
            return html.unescape(re.sub(r'<[^>]+>', '', s))

        def polozky(h):
            return [(t, cisty(x)) for t, x in re.findall(r'<(p|li)(?: class="[^"]*")?>(.*?)</\1>', h, re.S)]

        def porovnaj(cakane, web, kde):
            self.assertEqual(len(web), len(cakane), kde)
            for (tc, c), (tw, w) in zip(cakane, web):
                self.assertEqual((tw, bez(w)), (tc, bez(c)), kde)
        cin = self.info('')['telo'].split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        karty = re.findall(r'<article class="cinnost-karta" aria-labelledby="cinnost-([a-z-]+)">(.*?)</article>', cin, re.S)
        self.assertEqual(len(karty), 3)
        nadpisy = []
        for c, (slug, karta) in zip(casti, karty):
            h3 = cisty(re.search(r'<h3 class="cinnost-nadpis"[^>]*>(.*?)</h3>', karta).group(1))
            nadpisy.append((c['nadpis'], h3))
            self.assertEqual(bez(h3), bez(c['nadpis']), slug)
            self.assertIn(f'<span class="vh">: {html.escape(c["nadpis"].rstrip(":"), quote=False)}</span>',
                          html.unescape(karta).replace(' ', ' '), slug)
            porovnaj(c['polozky'], polozky(karta.split('<div class="cinnost-text">', 1)[1]), f'úvod {slug}')
            doslova = self.info(f'{slug}/')['telo'].split('sluzba-doslova">', 1)[1].split('<aside', 1)[0]
            # odkaz Všetky realizácie pod doslovným textom služby bez realizácií (položka 29) nie je text zadania
            doslova, n = self.ODKAZ_VSETKY_REALIZACIE.subn('', doslova)
            self.assertEqual(n, 0 if self.w.realizacie_sluzby(slug) else 1, slug)
            porovnaj(c['polozky'], polozky(doslova), f'/{slug}/')
        self.assertEqual([bez(w) for _, w in nadpisy], [bez(z) for z, _ in nadpisy])

    # ---------------------------------------------------------------- doladenie 3 (overenie 3, krasa-8, 25. 11. 2026)

    def test_doladenie3_bok_sluzby_na_tablete(self):
        """Doladenie-3, overenie 3 nález vážne (/stavebny-manazment/, /stavebny-dozor/, /stavbyveduci/ pri 768 až 959 px:
        box s dokladom SKSI vedľa textu mal 258 až 302 px, tabuľka v ňom ostala 252 px, obsah vytŕčal cez rámik až o 18 px
        a slová sa lámali uprostred, stavebnýc|h, Osvedčeni|e, 00912*10|*, 5. 11. 200|3): na tablete text cez celú šírku
        a box pod ním na 5 z 8 stĺpcov, od 1024 px vedľa seba ako doteraz (1/7 a 8/-1). Geometria podľa styl.css (okraj,
        medzera, 8 a 12 stĺpcov, odsadenie boxu clamp(20px,3vw,36px), rámik 1 px, štítok 7,5 em, medzera 16 px
        a rezerva hodnoty 12 px): hodnota má pri každej šírke 360 až 1100 px aspoň 120 px; najdlhšie slovo dokladov má
        v Chrome 108,2 px pri 18 px a 102,2 px pri 17 px (stavbyvedúci). Do 359 px je štítok nad hodnotou (test
        test_doladenie3_najuzsie_okno_320). Prehliadač (doladenie/meraj.mjs bok, 18 šírok 320 až 1440 px a 3 stránky,
        po oprave 0 rozdelených slov a obsah vnútri rámika): DOLADENIE-3.md."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        tablet = ''.join(css_bloky(css, '@media (min-width:768px)'))
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.sluzba-obal .sluzba-hlavne{grid-column:1/-1}', tablet)
        self.assertIn('.sluzba-obal .sluzba-bok{grid-column:1/6}', tablet)
        self.assertIn('.sluzba-obal .sluzba-hlavne{grid-column:1/7}', pc)
        self.assertIn('.sluzba-obal .sluzba-bok{grid-column:8/-1}', pc)
        self.assertNotIn('.sluzba-obal .sluzba-bok{grid-column:6/-1}', css)
        self.assertIn('.kalkulacka{margin-top:clamp(40px,5vw,64px);padding:clamp(20px,3vw,36px);border:1px solid', css)
        self.assertIn('.fakty>div{display:grid;grid-template-columns:minmax(7.5em,32%) minmax(0,1fr);gap:var(--m-16);', css)
        self.assertIn('.fakty dd{margin:0;padding-right:var(--m-12);', css)

        def hodnota(vw):
            okraj = min(64.0, max(16.0, 0.039 * vw))
            medzera = min(24.0, max(12.0, 0.016 * vw))
            obsah = min(1760.0, vw - 2 * okraj)
            if vw >= 1024:
                bok = 5 * (obsah - 11 * medzera) / 12 + 4 * medzera
            elif vw >= 768:
                bok = 5 * (obsah - 7 * medzera) / 8 + 4 * medzera
            else:
                bok = obsah
            pismo = 18 if vw >= 768 else 17
            vnutro = bok - 2 * min(36.0, max(20.0, 0.03 * vw)) - 2
            return vnutro - max(7.5 * pismo, 0.32 * vnutro) - 16 - 12, bok
        for vw in range(360, 1101):
            self.assertGreaterEqual(hodnota(vw)[0], 120, vw)
        # Chrome (meraj.mjs bok): box pri 768 px 437 px (predtým 258), pri 1023 px 583 px, od 1024 px bez zmeny 384 px
        self.assertAlmostEqual(hodnota(768)[1], 437.5, delta=1.5)
        self.assertAlmostEqual(hodnota(1023)[1], 583, delta=1.5)
        self.assertAlmostEqual(hodnota(1024)[1], 384, delta=1.5)
        for k in self.texty['sluzby']:
            telo = self.info(f'{k}/')['telo']
            # box je v DOM za textom: na mobile a tablete pod ním
            self.assertLess(telo.index('<div class="sluzba-hlavne'), telo.index('<aside class="kalkulacka sluzba-bok"'), k)

    def test_doladenie3_nizke_okno_hrdina(self):
        """Doladenie-3, overenie 3 nález vážne (sweep 525 okien, šírka 1024 až 2560 px, výška 600 až 1400 px: pri výške
        600 px a šírke 1024 až 1600 px tlačidlá Zavolať a Napísať e-mail, štítok stavby aj ovládanie slidera 19 až 22 px
        pod spodkom okna a fotka 35 až 38 px pod ním; jemný sweep po 2 px: fotka pod oknom až do výšky 648 px, pri 560 px
        65 až 68 px): pri výške okna do 660 px hustejší pravý stĺpec hrdinu, riadky nad a pod ním najmenej 4 px (pri voľnom
        mieste rastú ako doteraz), údaje 4 a 5 px, menšie odstupy, spolu asi 78 px; obsah ani H1 (6 % výšky okna) sa
        nemenia a fotka ostáva po spodok okna (žiadne vlastné min-height). Prehliadač po oprave (doladenie/meraj.mjs prva,
        11 šírok 1024 až 1920 px a výšky 540 až 700 px po 2 px; 25 šírok a výšky 600 až 1400 px po 40 px; čerstvé
        načítanie 1344 x 600, 1366 x 610, 1280 x 640, 1024 x 600, 1600 x 600): pozri DOLADENIE3_NIZKE."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        blok = ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:660px)'))
        for kus in ('.uvod-mriezka{grid-template-rows:minmax(var(--m-4),1fr) auto auto minmax(var(--m-4),1fr) auto}',
                    '.uvod-h1{margin-top:var(--m-8)}', '.firma-udaje{margin-top:var(--m-8)}',
                    '.udaj,.udaj-siroky{padding:4px 0 5px}', '.udaj dd{margin-top:0}', '.firma-zdroj{margin-top:var(--m-4)}',
                    '.firma-media{margin-top:var(--m-4)}', '.uvod-akcie{margin-top:var(--m-12)}'):
            self.assertIn(kus, blok)
        # fotka po spodok okna ako vo vyšších oknách, H1 bez zmeny (len pravidlo do 960 px výšky)
        self.assertNotIn('.snimky{', blok)
        self.assertNotIn('font-size', blok)
        self.assertNotIn('display:none', blok)
        # blok je za oboma blokmi do 820 px výšky (prepíše ich hodnoty)
        self.assertLess(css.rindex('@media (min-width:1024px) and (max-height:820px){'),
                        css.index('@media (min-width:1024px) and (max-height:660px){'))
        # Chrome po oprave: okno -> (spodok fotky pod oknom px, najmenšia rezerva tlačidiel, štítka a ovládania px)
        for okno, (dole, rezerva) in self.DOLADENIE3_NIZKE.items():
            self.assertEqual(dole, 0, okno)
            self.assertGreaterEqual(rezerva, 4, okno)

    def test_doladenie3_rytmus_sluzieb(self):
        """Doladenie-3, overenie 3 nález drobný (na stránkach služieb sa na hranici blokov sčítal spodný a horný rytmus:
        od posledného textu po nadpis ďalšej sekcie 338 až 392 px pri 1366 až 1920 px, pri 1728 x 1000 po prvom posune asi
        80 % okna prázdnych; pred Kontaktom 296 px): od 1024 px text služby, karty realizácií a otázky zo zákona po 48 až
        76 px (5vw), Kontakt so svojím odsadením 64 až 120 px (doladenie-1); do 1023 px bez zmeny. Chrome po oprave
        (doladenie/meraj.mjs medzery): DOLADENIE3_MEDZERY, cieľ 150 až 190 px ako /realizacie/ (173 px pri 1440 px)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.sluzba-obal{padding-bottom:clamp(var(--m-48),5vw,76px)}', pc)
        self.assertIn('.sluzba-obal~.sekcia-karty,.sluzba-obal~.otazky-sekcia{padding-block:clamp(var(--m-48),5vw,76px)}', pc)
        zaklad = css_bez_blokov(css)
        self.assertIn('.sluzba-obal{row-gap:40px;padding-bottom:var(--rytmus)}', zaklad)
        self.assertIn('.sekcia{padding-block:var(--rytmus)}', zaklad)
        # Kontakt si odsadenie nechá (pravidlo služby ho nesmie zachytiť)
        self.assertIn('.kontakt{background:var(--vapno-2);--tlmeny:#5d5d58;padding-block:clamp(var(--m-64),7vw,120px)}', zaklad)
        self.assertNotIn('.sluzba-obal~.sekcia{', css)
        for k in self.texty['sluzby']:
            telo = self.info(f'{k}/')['telo']
            # za obalom textu služby karty realizácií alebo otázky zo zákona, potom Kontakt (súrodenci v main)
            self.assertRegex(telo, r'<div class="obsah mriezka sluzba-obal">.*?</aside></div>\s*'
                                   r'<section class="sekcia (?:sekcia-karty|otazky-sekcia)"', k)
            self.assertRegex(telo, r'</section>\s*<section class="sekcia kontakt"', k)
        for kluc, medzera in self.DOLADENIE3_MEDZERY.items():
            self.assertLessEqual(medzera, 200, kluc)
            self.assertGreaterEqual(medzera, 120, kluc)
            self.assertIn(kluc[0], self.texty['sluzby'], kluc)

    def test_doladenie3_register_do_639(self):
        """Doladenie-3, overenie 3 nález vážne (/opravnenia/ pri 320 a 360 px: stĺpec auto so zdrojom si vzal asi 245 px,
        nadpisu Obchodný register ostalo 20 a 60 px, písmená išli pod seba, pri 360 px Obcho|dný registe|r, pri 375 px
        Obchod|ný): do 639 px jeden stĺpec, nadpis cez celý riadok, zdroj pod textom ako v hrdinovi (poradie v DOM: nadpis,
        text, zdroj); od 640 px vedľa seba ako doteraz. Prehliadač (meraj.mjs register, 320 až 1440 px): DOLADENIE-3.md."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        mobil = ''.join(css_bloky(css, '@media (max-width:639px)'))
        self.assertIn('.register .sluzba{grid-template-columns:minmax(0,1fr)}', mobil)
        self.assertIn('.register .cena{grid-column:1;grid-row:auto}', mobil)
        zaklad = css_bez_blokov(css)
        self.assertIn('.sluzba{display:grid;grid-template-columns:minmax(0,1fr) auto;', zaklad)
        self.assertIn('.cena{grid-column:2;grid-row:1;', zaklad)
        self.assertIn('.register .cena{font-size:15px;white-space:normal;text-align:left}', zaklad)
        li = self.info('opravnenia/')['telo'].split('<ul class="cennik register">', 1)[1].split('</ul>', 1)[0]
        self.assertRegex(li, r'^<li class="sluzba"><h2 class="nadpis-maly" id="obchodny-register">[^<]+</h2>'
                             r'<p class="sluzba-opis">[^<]+</p><p class="cena"><small>Zdroj: <a ')

    def test_doladenie3_najuzsie_okno_320(self):
        """Doladenie-3, overenie 3 nález drobný (320 px): číslo účtu v Kontakte 9127/110|0 (bunka 136 px, pevné medzery
        čísla), v boku služby stavbyvedú|ci (hodnota 90 px vedľa štítka 7,5 em), v detaile Cakovandpartner|s, a
        www.bytyrosnicka.s|k. Do 359 px Bankové spojenie a Číslo účtu cez oba stĺpce (trieda fakt-ucet) a v tabuľkách
        údajov podstránok štítok nad hodnotou; od 360 px bez zmeny. Prehliadač (meraj.mjs ucet a slova): DOLADENIE-3.md."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        uzke = ''.join(css_bloky(css, '@media (max-width:359px)'))
        self.assertIn('.kontakt-fakty>.fakt-ucet{grid-column:1/-1}', uzke)
        self.assertIn('.fakty>div{grid-template-columns:minmax(0,1fr);row-gap:var(--m-4)}', uzke)
        self.assertNotIn('fakt-ucet', css_bez_blokov(css))
        for blok in ('@media (max-width:767px)', '@media (min-width:768px)', '@media (min-width:1024px)'):
            self.assertNotIn('fakt-ucet', ''.join(css_bloky(css, blok)), blok)
        self.assertEqual(build.KONTAKT_UCET, ('Bankové spojenie', 'Číslo účtu'))
        for cesta in ('', 'kontakt/', 'realizacie/', 'stavebny-dozor/', 'realizacie/vila-x-bratislava/'):
            t = self.info(cesta)
            dl = t['telo'].split('<dl class="fakty kontakt-fakty', 1)[1].split('</dl>', 1)[0]
            self.assertEqual(re.findall(r'<div class="fakt-ucet"><dt>([^<]+)</dt>', dl), list(build.KONTAKT_UCET), cesta)
            # pravidlo pre Kontakt je v spoločnej časti štýlu (úvod nemá úsek podstránok)
            self.assertIn('.kontakt-fakty>.fakt-ucet{grid-column:1/-1}', t['html'], cesta)

    def test_doladenie3_menu_zavrie_pri_odchode_fokusu(self):
        """Doladenie-3, overenie 3 nález drobný (WCAG 2.2, bod 2.4.11): Tab za posledným odkazom otvoreného mobilného menu
        presunul fokus na prvok pod panelom (na úvode odkaz štítka RD Ahoj), menu ostalo otvorené a fokus nebolo vidno.
        app.js časť 1: focusout s novým prvkom mimo menu menu zavrie (bez návratu fokusu na tlačidlo); bez nového prvku
        (relatedTarget null: ťuk na plochu panela, odchod z okna, odkaz na iOS bez fokusu) a s prvkom v menu ostane
        otvorené, ťuk na odkaz tak vždy naviguje. Simulácia časti 1 v node nad falošným DOM; prehliadač (meraj.mjs menu,
        360 až 1099 px): DOLADENIE-3.md."""
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("menu.addEventListener('focusout'", js)
        cast = '// 1. Mobilné menu' + js.split('// 1. Mobilné menu', 1)[1].split('// 2. Spodná lišta', 1)[0]
        node = shutil.which('node')
        if not node:
            self.skipTest('node nie je k dispozícii')
        import subprocess
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const fokus = [];
function prvok(meno) {
  return { meno, nodeType: 1, poslucha: {}, addEventListener(t, f) { (this.poslucha[t] = this.poslucha[t] || []).push(f); },
    getAttribute() { return this.href || null; }, focus() { fokus.push(meno); } };
}
const tl = prvok('summary'); tl.textContent = 'Menu';
const odkaz = prvok('odkaz'); odkaz.href = 'realizacie/';
const kotva = prvok('kotva'); kotva.href = '#cinnost';
const vonku = prvok('vonku');
const menu = prvok('menu'); menu.open = false;
menu.querySelector = (s) => (s === 'summary' ? tl : null);
menu.querySelectorAll = (s) => (s === 'a[href]' ? [odkaz, kotva] : []);
menu.contains = (x) => x === menu || x === tl || x === odkaz || x === kotva;
const dok = prvok('document');
dok.querySelectorAll = (s) => (s === 'details.menu-m' ? [menu] : []);
const ctx = { d: dok, vsetky: (sel, koren) => Array.prototype.slice.call((koren || dok).querySelectorAll(sel)) };
vm.createContext(ctx);
vm.runInContext(kod, ctx);
const fo = (kam) => { (menu.poslucha.focusout || []).forEach((f) => f({ relatedTarget: kam })); return menu.open; };
const v = {};
menu.open = true; v.vnutri = fo(odkaz);
menu.open = true; v.tlacidlo = fo(tl);
menu.open = true; v.bez_prvku = fo(null);
menu.open = true; v.vonku = fo(vonku);
v.fokus_po_vonku = fokus.slice();
menu.open = false; v.zatvorene = fo(vonku);
menu.open = true; v.nie_prvok = fo({ nodeType: 3 });
console.log(JSON.stringify(v));
"""
        p = subprocess.run([node, '-e', sim], input=json.dumps(cast), capture_output=True, text=True, encoding='utf-8',
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {'vnutri': True, 'tlacidlo': True, 'bez_prvku': True, 'vonku': False,
                                                'fokus_po_vonku': [], 'zatvorene': False, 'nie_prvok': True})

    def test_hlavna_slider_od_okraja_s_posuvnikom(self):
        """Kontrola 3 v prehliadači (nález drobné, 25. 11. 2026): odsadenie slidera k ľavej hrane okna počítalo so 100vw,
        ktoré vo Windows zahŕňa aj posuvník 17 px, kým .obsah sa centruje v šírke bez neho; od šírky 1889 px začínal
        slider 8,5 px za ľavým okrajom a štítok stavby bol 7,5 px od okraja. Od 1024 px je .uvod kontajner a slider
        ráta so 100cqw (šírka sekcie bez posuvníka); 100vw ostáva pred ním ako záloha pre prehliadač bez cqw."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        self.assertIn('.uvod{container-type:inline-size}', pc)
        self.assertIn('.slider{margin-left:calc(-1 * max(var(--okraj), (100vw - var(--sirka)) / 2));'
                      'margin-left:calc(-1 * max(var(--okraj), (100cqw - var(--sirka)) / 2))}', pc)
        # v .uvod nie je nič s position fixed ani sticky (kontajner by im zmenil obsahujúci blok)
        uvod = self.info('')['telo'].split('<section class="uvod"', 1)[1].split('</section>', 1)[0]
        for trieda in ('lista', 'lightbox', 'hlava', 'cinnost-hlava'):
            self.assertNotIn(f'class="{trieda}', uvod)

    def test_hlavna_menu_esc_mimo_menu(self):
        """Kontrola 3 v prehliadači (nález drobné): po ťuknutí na prázdnu plochu otvoreného mobilného menu prešiel fokus
        na body a Esc menu nezavrel (posluch bol len na menu). Esc teraz počúva dokument: pri otvorenom menu ho zavrie
        a vráti fokus na tlačidlo, pri zatvorenom nerobí nič. Simulácia časti 1 app.js v node nad falošným DOM."""
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        cast = '// 1. Mobilné menu' + js.split('// 1. Mobilné menu', 1)[1].split('// 2. Spodná lišta', 1)[0]
        self.assertIn("d.addEventListener('keydown'", cast)
        self.assertNotIn("menu.addEventListener('keydown'", cast)
        node = shutil.which('node')
        if not node:
            self.skipTest('node nie je k dispozícii')
        import subprocess
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const fokus = [];
function prvok(meno) {
  return { meno, nodeType: 1, poslucha: {}, addEventListener(t, f) { (this.poslucha[t] = this.poslucha[t] || []).push(f); },
    getAttribute() { return null; }, focus() { fokus.push(meno); } };
}
const tl = prvok('summary'); tl.textContent = 'Menu';
const menu = prvok('menu'); menu.open = false;
menu.querySelector = (s) => (s === 'summary' ? tl : null);
menu.querySelectorAll = () => [];
menu.contains = (x) => x === menu || x === tl;
const dok = prvok('document');
dok.querySelectorAll = (s) => (s === 'details.menu-m' ? [menu] : []);
const ctx = { d: dok, vsetky: (sel, koren) => Array.prototype.slice.call((koren || dok).querySelectorAll(sel)) };
vm.createContext(ctx);
vm.runInContext(kod, ctx);
const kl = (key) => { let p = false; (dok.poslucha.keydown || []).forEach((f) => f({ key, preventDefault() { p = true; } })); return p; };
const v = {};
menu.open = true; v.esc_zabranene = kl('Escape'); v.otvorene_po_esc = menu.open; v.fokus = fokus.slice();
menu.open = true; kl('Enter'); v.otvorene_po_enter = menu.open;
menu.open = false; fokus.length = 0; v.zatvorene_esc_zabranene = kl('Escape'); v.fokus_zatvorene = fokus.slice();
console.log(JSON.stringify(v));
"""
        p = subprocess.run([node, '-e', sim], input=json.dumps(cast), capture_output=True, text=True, encoding='utf-8',
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {'esc_zabranene': True, 'otvorene_po_esc': False, 'fokus': ['summary'],
                                                'otvorene_po_enter': True, 'zatvorene_esc_zabranene': False,
                                                'fokus_zatvorene': []})

    def test_hlavna_nezalomitelne_galeria_a_pismena(self):
        """Kontrola zadania 26. 11. 2026 (drobné): odkaz „Galéria, 8 fotiek“ v popise titulnej fotky detailu sa na
        telefóne lámal za čiarkou a výpočet písmen „a), c), e), h)“ v zdroji otázok zo zákona sa lámal pred „e), h)“.
        Medzera za čiarkou je v oboch prípadoch nezalomiteľná."""
        self.assertEqual(build.sadzba('písm. a), c), e), h) zákona'), 'písm.\u00a0a),\u00a0c),\u00a0e),\u00a0h) zákona')
        self.assertEqual(build.sadzba('(a), b) a c)'), '(a),\u00a0b) a\u00a0c)')
        lum = self.info('realizacie/filmovy-klub-lumiere-spitalska-bratislava/')['telo']
        self.assertIn('<a href="#galeria">Galéria,\u00a08\u00a0fotiek</a>', lum.replace('&nbsp;', '\u00a0'))

    def test_hlavna_kontakt_lavy_stlpec_ako_24_11(self):
        """Andrej 26. 11. 2026 (porovnanie s náhľadom c80df1f z 24. 11.): „tá ľavá strana bola plnšia, krajšia“, ikony
        ostávajú. Od 1024 px sú fakturačné údaje v ľavom stĺpci pod tlačidlami a sieťami v jednom stĺpci, siete hneď pod
        tlačidlami (nie na spodku stĺpca) a vizitka vpravo cez oba riadky; tablet a mobil bez zmeny."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        pc = ''.join(css_bloky(css, '@media (min-width:1024px)'))
        for kus in ('.kontakt-udaje{grid-column:1/7;grid-row:2}', '.kontakt-mapa{grid-row:1 / span 2}',
                    '.kontakt-siete{margin-top:var(--m-24)}', '.kontakt-fakty{display:block;max-width:none}'):
            self.assertIn(kus, pc)
        # nové pravidlá sú za pôvodnými pravidlami od 1024 px, takže ich prebijú (rovnaká špecifickosť)
        self.assertGreater(css.index('.kontakt-fakty{display:block;max-width:none}'),
                           css.index('.kontakt-fakty{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-flow:column'))
        # Kontakt na úvode aj na stránke Kontakt: údaje sú v DOM za vizitkou (mobil: text, vizitka, údaje)
        for cesta in ('', 'kontakt/'):
            sek = self.info(cesta)['telo'].split('id="kontakt"', 1)[1]
            self.assertLess(sek.index('class="kontakt-mapa"'), sek.index('class="kontakt-udaje"'), cesta)

    def test_hlavna_bok_tlacidlo_320(self):
        """Kontrola 3 v prehliadači (nález drobné): pri 320 px bolo tlačidlo Zavolať s číslom v boku služby (251,8 px)
        širšie ako vnútro boxu (246 px). Do 359 px sa číslo zalomí pod slovo."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.bok-tlacidlo .tlacidlo{flex-wrap:wrap;row-gap:2px;padding-block:10px}',
                      ''.join(css_bloky(css, '@media (max-width:359px)')))

    def test_doladenie3_realizacie_len_roky(self):
        """Doladenie-3, overenie 3 nálezy vážne (pri realizáciách len rok alebo roky, nikde mesiac ani deň; ZMENY-KOLO-1.md
        5.2): UFO „Kolaudácia v novembri 2025.“, Dom Betliarska „Skolaudované 12/2013.“ a RD Jarovce „Skolaudované
        09/2014.“ boli na stránke aj v description JSON-LD; CreativeWork mal datePublished (deň príspevku na Facebooku,
        Dvojdom S 2026-10-04 pri stavbe 2021 až 2022) a dateModified 2026-11-24. Build vety nahradí podľa nastavenia
        text_len_roky (obsah sa nemení) a dátumy do CreativeWork nedáva. Na žiadnej stránke realizácie nie je v texte,
        meta description ani v JSON-LD stavby názov mesiaca, MM/RRRR, D. M. RRRR ani dátum RRRR-MM; výnimka je blok
        V médiách pri Lumière (číslo časopisu ASB „február 2024“ a datePublished 2024-02 v subjectOf je dátum vydania
        časopisu, nie stavby) a foundingDate firmy."""
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        for slug, dvojice in nast['text_len_roky'].items():
            d = self.realizacie.get(slug)
            for stara, nova in dvojice:
                self.assertIsNotNone(build.MESIAC_V_TEXTE.search(stara), stara)
                self.assertIsNone(build.MESIAC_V_TEXTE.search(nova), nova)
                if d is not None:
                    # pôvodná veta je v obsahu (inak nahradenie nič nerobí a build varuje)
                    self.assertIn(stara, build.cisty(d.get('text')), slug)
        for slug, cakane in (('ufo-most-snp-bratislava', 'Kolaudácia v roku 2025.'),
                             ('dom-betliarska-bratislava-petrzalka', 'Skolaudované v roku 2013.'),
                             ('rd-jarovce-bratislava', 'Skolaudované v roku 2014.')):
            t = self.info(f'realizacie/{slug}/')
            self.assertIn(cakane, html.unescape(re.sub(r'<[^>]+>', '', t['telo'])).replace(' ', ' '), slug)
            dielo = [u for u in t['ld'] if u.get('@type') == 'CreativeWork'][0]
            self.assertIn(cakane, dielo['description'], slug)
        datum_ld = re.compile(r'\b(?:19|20)\d{2}-\d{2}')
        for slug in self.realizacie:
            t = self.info(f'realizacie/{slug}/')
            telo = re.sub(r'<section class="sekcia media-sekcia".*?</section>', '', t['telo'], flags=re.S)
            telo = re.sub(r'<script.*?</script>|<style.*?</style>', '', telo, flags=re.S)
            # viditeľný text aj atribúty (popisy fotiek v lightboxe)
            text = html.unescape(re.sub(r'<[^>]+?>', ' ', telo) + ' ' + ' '.join(re.findall(r'="([^"]*)"', telo)))
            m = build.MESIAC_V_TEXTE.search(text.replace(' ', ' '))
            self.assertIsNone(m, (slug, m and m.group(0)))
            hlava = t['html'].split('</head>', 1)[0]
            for obsah in [t['desc']] + re.findall(r'<meta (?:property|name)="(?:og|twitter):description" content="([^"]*)"', hlava):
                self.assertIsNone(build.MESIAC_V_TEXTE.search(html.unescape(obsah)), slug)
            for u in t['ld']:
                if u.get('@type') in ('CreativeWork', 'WebPage', 'BreadcrumbList'):
                    bez = {k: v for k, v in u.items() if k != 'subjectOf'}
                    s = json.dumps(bez, ensure_ascii=False)
                    self.assertIsNone(build.MESIAC_V_TEXTE.search(s), (slug, u.get('@type')))
                    self.assertIsNone(datum_ld.search(s), (slug, u.get('@type')))
            dielo = [u for u in t['ld'] if u.get('@type') == 'CreativeWork'][0]
            self.assertFalse({'datePublished', 'dateModified', 'dateCreated'} & set(dielo), slug)

    def test_krasa8_asb_vyrez(self):
        """krasa-8, agent Kontakt (brána 3: výrez tabuľky ASB pri Lumière mal pri 1440 px 765 x 720 px, písmená 30 až
        40 px, prevalcoval stránku): 5 z 12 stĺpcov (1440 px: 540 px), od 1536 px 4 z 12 (1920 px: 571 px), na tablete
        5 z 8, text od čiary stĺpca za jedným voľným stĺpcom; plná veľkosť ostáva v lightboxe; sizes podľa toho."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.media-tabulka{grid-column:1/6}', ''.join(css_bloky(css, '@media (min-width:768px)')))
        self.assertIn('.media-text{grid-column:7/-1}', ''.join(css_bloky(css, '@media (min-width:1024px)')))
        siroke = ''.join(css_bloky(css, '@media (min-width:1536px)'))
        self.assertIn('.media-tabulka{grid-column:1/5}', siroke)
        self.assertIn('.media-text{grid-column:6/12}', siroke)
        self.assertNotIn('.media-tabulka{grid-column:1/8}', css)
        # od 1921 px (široké okno) 3 stĺpce mriežky širšieho obsahu (pri 2560 px 582 px ako 571 px pri 1920 px), do
        # 1920 px ten istý sizes ako predtým
        self.assertEqual(build.SIZES_MEDIA_TABULKA, '(min-width:1921px) min(25vw - 58px, 582px), '
                                                    '(min-width:1536px) min(30vw, 580px), (min-width:1024px) 38vw, '
                                                    '(min-width:768px) 58vw, calc(100vw - 32px)')
        siroke_okno = ''.join(css_bloky(css, '@media (min-width:1921px)'))
        self.assertIn('.media-tabulka{grid-column:1/4}', siroke_okno)
        # text po pravý okraj obsahu ako čiara pod nadpisom sekcie, fotky strán na 6 z 8 stĺpcov textu (po 3 stĺpce,
        # SIZES_MEDIA_STRANA), overenie širokého rozloženia, nález drobné: pri 5/11 ostával vpravo prázdny pás 404 px
        self.assertIn('.media-text{grid-column:5/-1}', siroke_okno)
        self.assertIn('.media-strany{max-width:calc((100% + var(--medzera)) * 6 / 8 - var(--medzera))}', siroke_okno)
        self.assertNotIn('.media-strany{max-width', css.replace(siroke_okno, ''))
        lum = 'filmovy-klub-lumiere-spitalska-bratislava'
        tab = self.info(f'realizacie/{lum}/')['telo'].split('<figure class="media-tabulka">', 1)[1].split('</figure>', 1)[0]
        # lightbox s plnou veľkosťou: odkaz na najväčší súbor výrezu
        plna = re.search(r'<a class="media-odkaz" href="([^"]+)"', tab).group(1)
        sirky = [int(s) for s in re.findall(r'casopis-asb/01-(\d+)\.webp \d+w', tab)]
        self.assertTrue(plna.endswith(f'/01-{max(sirky)}.webp'), (plna, sirky))

    def test_krasa8_omrvinky_mobil(self):
        """krasa-8, agent Kontakt (brána 3: Lumière na mobile, omrvinky opakovali celý dlhý názov tesne nad rovnakým H1):
        do 767 px na detaile (hlava-detail) je posledná položka skrytá len vizuálne (čítačka ju prečíta, span ostáva
        s aria-current="page"), viditeľné ostane Úvod / Realizácie bez lomky na konci; inde a od 768 px bez zmeny."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        mobil = ''.join(css_bloky(css, '@media (max-width:767px)'))
        self.assertIn('.hlava-detail .omrvinky li:last-child{position:absolute;width:1px;height:1px;overflow:hidden;'
                      'clip-path:inset(50%);white-space:nowrap}', mobil)
        self.assertIn('.hlava-detail .omrvinky li:nth-last-child(2)::after{content:none}', mobil)
        self.assertNotIn('.omrvinky li:last-child{display:none', css)
        self.assertNotIn('.hlava-detail .omrvinky', css_bez_blokov(css))
        lum = 'filmovy-klub-lumiere-spitalska-bratislava'
        telo = self.info(f'realizacie/{lum}/')['telo']
        hlava = telo.split('<div class="hlava-stranky hlava-detail">', 1)[1]
        nav = re.search(r'<nav class="omrvinky" aria-label="Omrvinky"><ol>(.*?)</ol></nav>', hlava).group(1)
        li = re.findall(r'<li>(.*?)</li>', nav)
        self.assertEqual(len(li), 3)
        self.assertEqual(li[1], '<a href="../../realizacie/">Realizácie</a>')
        self.assertTrue(li[2].startswith('<span aria-current="page">Stavebné úpravy Filmového klubu Lumière'), li[2])

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
        # karta zoznamu ukáže roky stavby, bez nich rok príspevku bez slova príspevok (Andrej 26. 11. 2026: „roky
        # xxx tak ako predtým“, ROKY.md); prázdna kóta len pri realizácii bez akéhokoľvek roku (dnes žiadna); slovo
        # Facebook v karte nie je (zadanie, Obsah 2)
        zoznam = self.info('realizacie/')['telo']
        for r in self.w.realizacie:
            karta = zoznam.split(f'realizacie/{r["slug"]}/"', 1)[1].split('</li>', 1)[0]
            self.assertNotIn('Facebook', karta)
            roky = self.w.roky_karty(r)
            self.assertTrue(roky, r['slug'])
            # zoznam má statickú kótu (slovo „až“ v span.kota-slovo): porovnáva sa text bez značiek
            self.assertIn(roky, re.sub(r'<[^>]+>', '', karta).replace('\u00a0', ' '), r['slug'])
            self.assertNotIn('kota-prazdna', karta, r['slug'])

    def test_kazda_karta_ma_fotku_obal_a_velka(self):
        # žiadna karta zoznamu bez fotky; vizualizácia ako fotka karty alebo obal len pri obal_vizualizacia, veľká fotka
        # nikdy vizualizácia (doladenie-1: test mal to isté meno ako test nižšie, ten ho prepísal a nikdy sa nespustil)
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
                # aj malý zdroj výrez 4 : 5 cez celý rám (doladenie-1, predtým celá fotka foto-cela v strede rámu)
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
        # karty (zoznam, služby) a stránky realizácií ukazujú roky stavby, bez nich rok príspevku (rok_prispevku, inak
        # rok z poľa datum) bez slova príspevok (Andrej 26. 11. 2026, ROKY.md), nikdy slovo Facebook
        for cesta in ['realizacie/'] + [f'{k}/' for k in self.texty['sluzby']]:
            for _, karta in self.karty_zoznamu(cesta):
                slug = re.search(r'realizacie/([a-z0-9-]+)/', karta).group(1)
                r = [x for x in self.w.realizacie if x['slug'] == slug][0]
                self.assertNotIn('Facebook', karta)
                ocak = self.w.roky_karty(r)
                self.assertEqual(ocak, r['roky'] or r['rok_prispevku'] or r['datum'][:4], slug)
                self.assertRegex(ocak, r'^(19|20)\d{2}( až (19|20)\d{2})?$', slug)
                self.assertNotIn('kota-prazdna', karta, slug)
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
            self.assertIn(f'<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">{ocak}</span>', detail, r['slug'])
            # popisok vždy Roky, aj pri jednom roku (ROKY.md); Rok ani Rok príspevku nikde
            self.assertIn(f'<dt>Roky</dt><dd>{ocak}</dd>', telo, r['slug'])
            for zle in ('<dt>Rok</dt>', 'Rok príspevku'):
                self.assertNotIn(zle, telo, (r['slug'], zle))

    def test_rok_prispevku_bez_slova_prispevok(self):
        # Andrej 26. 11. 2026 pri snímke /realizacie/ (RD Ahoj, Vila Holubyho a Dom pod lesom bez roku): „veď tie roky
        # som tam chcel všade, ale len takto napísané, že roky xxx tak ako predtým“ (ROKY.md). Zo stretnutia 24. 11.
        # („slovo príspevok preč, rok príspevku preč, len roky“) platí len: slovo príspevok preč. Realizácia bez rokov
        # stavby má rok príspevku (rok_prispevku, inak rok z poľa datum) na karte, v detaile, v popise slidera a vo
        # filtri Roky (data-rok je ten istý rok, voľba sedí s kartou), bez slova príspevok, mesiaca a dňa
        s_prispevkom = [r for r in self.w.realizacie if not r['roky']]
        self.assertTrue(s_prispevkom)
        zoznam = self.info('realizacie/')['telo'].replace('\u00a0', ' ')
        uvod = self.info('')['telo'].replace('\u00a0', ' ')
        for r in s_prispevkom:
            rok = r['rok_prispevku'] or r['datum'][:4]
            self.assertRegex(rok, r'^(19|20)\d{2}$', r['slug'])
            self.assertEqual(self.w.roky_karty(r), rok, r['slug'])
            pred, za = zoznam.split(f'realizacie/{r["slug"]}/"', 1)
            li, karta = pred.rsplit('<li ', 1)[1], za.split('</li>', 1)[0]
            self.assertIn(f'<span class="kota"><span class="kota-text">{rok}</span></span>', karta, r['slug'])
            self.assertNotIn('kota-prazdna', karta, r['slug'])
            self.assertIn(f'data-rok="{rok}"', li, r['slug'])
            telo = self.info(f'realizacie/{r["slug"]}/')['telo'].replace('\u00a0', ' ')
            hlava = telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]
            self.assertIn(f'<dt>Roky</dt><dd>{rok}</dd>', hlava, r['slug'])
            # slider úvodu: miesto a rok (RD Ahoj a Vila Holubyho)
            popis = re.search(rf'<li class="slider-popis[^"]*"><a href="realizacie/{r["slug"]}/">.*?</li>', uvod)
            if popis:
                self.assertTrue(popis.group(0).endswith(f'<span class="cisla nz">{rok}</span></span></li>'), r['slug'])
        vyber = zoznam.split('<select data-roky>', 1)[1].split('</select>', 1)[0]
        # každá realizácia má rok: voľba „rok neuvedený“ nie je; voľby len z rokov, ktoré ukazujú karty
        self.assertNotIn('rok neuvedený', vyber)
        na_kartach = {y for r in self.w.realizacie for y in self.w.roky_cisla(r)}
        for text in re.findall(r'<option value="\d{4}">([^<]+)</option>', vyber):
            for rok in re.findall(r'\d{4}', text):
                self.assertIn(int(rok), na_kartach, text)
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
            # detail s údajmi pri fotke: fotka pri 1024 až 1279 px cez celú šírku (oprava po overení, SIZES_DETAIL_UDAJE)
            sizes = build.SIZES_DETAIL_UDAJE if 'detail-s-udajmi' in telo else build.SIZES_DETAIL
            self.assertIn(f'sizes="{build.sizes_pre_cover(sizes, r["karta"]["w"], r["karta"]["h"])}"', foto)
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
        # jednotný zápis tej istej roly (sablona/nastavenia.json rola_zapis, audit naplno 25. 11. 2026, položka 14):
        # „Projektový manažér“ zo starého webu je na stránke v zápise „Projektový manažment a stavebný dozor“
        rola_zapis = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8')).get('rola_zapis') or {}

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
            if u['typ'] == 'rola':
                m = re.match(r'realizacie/([a-z0-9-]+)/index\.html$', u['stranka'])
                povodna = build.zaciatok_velky(build.jeden_riadok((self.realizacie.get(m.group(1)) or {}).get('rola'))) if m else ''
                nova = rola_zapis.get(povodna, '')
                return (bool(nova) and all(self.norm_medzery(o).casefold() in povodna.casefold() for o in u['overenie'])
                        and self.norm_medzery(nova) in text)
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

    # služba bez realizácie s jej rolou (Stavbyvedúci) má pod doslovným textom odkaz Všetky realizácie ako ostatné
    # služby (audit naplno 25. 11. 2026, položka 29): navigácia na konci hlavného stĺpca, nie text; text pred ním
    # ostáva slovo od slova
    ODKAZ_VSETKY_REALIZACIE = re.compile(r'<p class="viac"><a class="odkaz-sipka" href="\.\./realizacie/">Všetky realizácie '
                                         r'<svg[^>]*>.*?</svg></a></p>(?=</div>)')

    def test_sluzby_doslova_zo_stareho_webu(self):
        # hlavný text stránky služby je slovo od slova text starého webu (zadanie časť 4) po normalizácii medzier;
        # nič iné v hlavnom stĺpci okrem odkazu Všetky realizácie pri službe bez realizácií (položka 29), ten istý
        # text ako Činnosť na úvode (jeden zdroj texty.json cinnost_doslova)
        uvod = self.info('')['telo']
        for k, ocak in self.DOSLOVNE_SLUZBY.items():
            telo = self.info(f'{k}/')['telo']
            hlavny = telo.split('<div class="sluzba-hlavne atelier-text cinnost-text sluzba-doslova">', 1)[1]
            hlavny = hlavny.split('<aside class="kalkulacka sluzba-bok"', 1)[0]
            hlavny, n = self.ODKAZ_VSETKY_REALIZACIE.subn('', hlavny)
            self.assertEqual(n, 0 if self.w.realizacie_sluzby(k) else 1, k)
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
            # bez rokov stavby rok príspevku ako pri ostatných kartách, nie prázdna kóta (Andrej 26. 11. 2026, ROKY.md)
            self.assertNotIn('kota-prazdna', karta, slug)
            self.assertIn(f'<span class="kota"><span class="kota-text">{self.w.roky_karty(r)}</span></span>', karta, slug)

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
            # pod 320 px „E-mail“ (audit naplno 25. 11. 2026, položka 35, styl.css lista-kratky)
            self.assertIn('<a class="tlacidlo tlacidlo-obrys" ' + mail + '><span class="lista-dlhy">Napísať e-mail</span>'
                          '<span class="lista-kratky">E-mail</span></a></nav>', s, cesta)
            self.assertIn('class="tlacidlo tlacidlo-male hlavicka-tel" ' + tel, s.split('</header>', 1)[0], cesta)
        # na /kontakt/ je sekcia Kontakt hlavným obsahom (raz, bez výzvy záveru)
        kontakt = self.info('kontakt/')['telo']
        self.assertEqual(kontakt.count('<section class="sekcia kontakt"'), 1)
        self.assertNotIn(self.texty['vyzvy']['zaver'], kontakt.replace(' ', ' '))
        self.assertIn('Zavolajte alebo napíšte e-mail.', kontakt)
        self.assertIn('<noscript><style>img[data-src]{display:none}@media (max-width:767px){.lista{transform:none;visibility:visible}}</style></noscript>',
                      self.info('')['html'])
        self.assertIn('.lista .tlacidlo{width:100%;min-height:50px', self.css)
        # pod pätou rezerva na lištu len bez JS (lišta stojí stále); so skriptom sa lišta pri päte skryje, takže 48 px
        # (doladenie-1: na mobile 124 px prázdneho pásu)
        self.assertIn('.pata{border-top:1px solid var(--grafit);padding:48px 0 calc(48px + env(safe-area-inset-bottom))', self.css)
        self.assertIn('html:not(.js) .pata{padding-bottom:calc(48px + 76px + env(safe-area-inset-bottom))}',
                      ''.join(css_bloky(self.css, '@media (max-width:767px)')))

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
        # doladenie-1 (CLS 0,155 pri 390 px a pomalej sieti): filter bez atribútu hidden, miesto v rozložení od prvého
        # vykreslenia; bez JS ho (aj riadok stavu) skryje CSS cez html:not(.js), zoznam ukazuje všetko
        self.assertNotIn('hidden', filtre[0])
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('html:not(.js) [data-filtre],html:not(.js) [data-stav]{display:none}', css)
        self.assertIn("d.classList.add('js')", build.SKRIPT_JS)
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
        # riadok stavu s textom už v HTML (ten istý, aký napíše app.js bez filtra), nič sa nedopĺňa neskôr
        self.assertGreater(len(self.realizacie), build.REALIZACIE_NARAZ)
        self.assertIn(f'<p class="filtre-stav" data-stav aria-live="polite">Zobrazené: {build.REALIZACIE_NARAZ} z '
                      f'{len(self.realizacie)}</p>', telo)
        self.assertEqual(self.w.stav_zoznamu(5), '')
        self.assertEqual(self.w.stav_zoznamu(build.REALIZACIE_NARAZ), '')
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
        for kus in ('[data-filtre]', '[data-zoznam]', 'data-druh', 'data-rok', 'data-poradie', 'aria-pressed',
                    '[data-mesto]', "param.get('mesto')", "'data-naraz'", "'[data-viac]'", "'[data-viac-obal]'",
                    "'[data-viac-pocet]'", "s.zoradit === 'nazov'", "Intl.Collator('sk'", "'.dlazdica-nazov a'",
                    "d.documentElement.classList.add('js')"):
            self.assertIn(kus, js)
        # filter už neodkrýva skript (atribút hidden nemá)
        self.assertNotIn('filtre.hidden', js)

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
                # roky pri každej realizácii, bez rokov stavby rok príspevku (ROKY.md)
                self.assertTrue(self.w.roky_karty(r), slug)
                self.assertIn(self.w.roky_karty(r), a['data-udaje'], slug)
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
        # realizácia bez datePublished a dateModified (doladenie-3, overenie 3 nález vážne: pri realizáciách len roky,
        # dátum príspevku na Facebooku nie je dátum stavby); lastmod v sitemap ostáva
        for slug, d in self.realizacie.items():
            dielo = [u for u in self.info(f'realizacie/{slug}/')['ld'] if u.get('@type') == 'CreativeWork'][0]
            self.assertNotIn('datePublished', dielo, slug)
            self.assertNotIn('dateModified', dielo, slug)

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
        # bez rokov stavby radí rok príspevku (rok_prispevku, inak rok dátumu), ten istý rok ako na karte (ROKY.md)
        self.assertEqual(build.roky_realizacie('', '2019-05-05', '2018'), (2018, 2018, True))
        self.assertEqual(build.roky_realizacie('', '2019-05-05'), (2019, 2019, True))
        self.assertEqual(build.roky_realizacie('2012 až 2014', '2019-05-05', ''), (2014, 2012, False))
        self.assertEqual(build.roky_realizacie('', ''), (0, 0, False))
        # realizácia bez rokov stavby: rok príspevku na karte, slovo Facebook ani príspevok nie (Andrej 26. 11. 2026)
        zoznam = self.info('realizacie/')['telo']
        for x in self.w.realizacie:
            if not x['roky'] and x['zdroj'] == 'fb' and x['datum']:
                karta = zoznam.split(f'realizacie/{x["slug"]}/"', 1)[1].split('</li>', 1)[0]
                self.assertNotIn('Facebook', karta)
                self.assertNotIn('príspev', karta)
                self.assertNotIn('kota-prazdna', karta, x['slug'])
                self.assertEqual(x['rok'], int(self.w.roky_karty(x)), x['slug'])

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
        # web dostáva app.js zhustený bez komentárov (audit naplno 25. 11. 2026, položka 45, test_45_app_js_zhusteny)
        self.assertEqual((self.ostry / 'app.js').read_text(encoding='utf-8'),
                         build.zhusti_js((KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')))
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
        # počítači od hlavičky po spodok okna (ZMENY-KOLO-1.md 3A.2; krasa-8: už bez 48 px, pás pod fotkou bol prázdny)
        root = re.search(r':root\{([^}]*)\}', re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)).group(1)
        self.assertIn('--hore:64px', root)
        self.assertIn(':root{--stlpce:12;--hore:84px}', self.css)
        self.assertIn(':root{--hore:64px}', self.css)
        self.assertIn('calc(100svh - var(--hore)),75vw)', self.css)
        self.assertNotIn('calc(100svh - var(--hore) - var(--m-', self.css)
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
            self.assertLess(sek.index('<address class="vizitka'), sek.index('class="fakty kontakt-fakty'), cesta)
        # krasa-8 (brána 3): karta už nie je pripnutá (s top 24 px zachádzala pod pripnutú hlavičku 64 až 84 px),
        # stojí vpravo vedľa nadpisu a fakturačné údaje idú pod oboma cez celú šírku
        self.assertIn('.kontakt-mapa{grid-column:5/-1;grid-row:1;align-self:start}', self.css)
        self.assertIn('.kontakt-udaje{grid-column:1/-1;grid-row:2}', self.css)
        self.assertNotRegex(re.sub(r'/\*.*?\*/', '', self.css, flags=re.S), r'\.kontakt-mapa\{[^}]*sticky')

    def test_udaje_lightboxu(self):
        # rola malým písmenom uprostred vety; projektant sa pri vizualizácii vynechá, ak je ním autor (app.js)
        r = {'rola': 'Stavebný dozor', 'projektant': 'Ing. arch. Ilja Skoček', 'roky': '2019 až 2020'}
        self.assertEqual(build.Web.udaje_lightboxu(r),
                         'rola BM Building: stavebný dozor · projektant Ing. arch. Ilja Skoček · 2019 až 2020')
        self.assertEqual(build.Web.udaje_lightboxu({'rola': 'TDI', 'projektant': '', 'roky': ''}), 'rola BM Building: TDI')
        # bez rokov stavby rok príspevku, inak rok z poľa datum, nikdy mesiac a deň (ROKY.md)
        self.assertEqual(build.Web.udaje_lightboxu({'rola': 'TDI', 'projektant': '', 'roky': '', 'rok_prispevku': '2026',
                                                    'datum': '2025-08-25'}), 'rola BM Building: TDI · 2026')
        self.assertEqual(build.Web.udaje_lightboxu({'rola': '', 'projektant': '', 'roky': '', 'datum': '2024-04-08'}), '2024')
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
        # § 36 ods. 1: do 31. 12. 2026 „je oprávnená najmä a) sledovať…“, od 1. 1. 2027 „je povinná najmä“ (novela
        # 243/2026 Z. z.); veta „najmä sleduje…“ platí pre obe znenia (audit naplno 25. 11. 2026, položka 3);
        # manažment: výber dodávateľa starý web neuvádza ako činnosť
        q = self.texty['otazky']['co-robi-dozor']['odpoved']
        self.assertIn('podľa stavebného zákona najmä sleduje postup stavebných prác', q)
        self.assertNotIn('oprávnená', q)
        self.assertNotIn('povinná', q)
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
        # výrez tabuľky: obrázok s popisom zdroja, z sablona/media (od krasa-8 5 z 12 stĺpcov, od 1536 px 4 z 12,
        # plná veľkosť v lightboxe; test_krasa8_asb_vyrez)
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
        # Kontakt na úvode bez content-visibility (audit naplno 25. 11. 2026, položka 40: mimo stromu prístupnosti)
        self.assertNotIn('content-visibility', css)
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

    # ŠIROKÉ OKNO (styl.css úsek ŠIROKÉ OKNO; Andrej 25. 11. 2026 pri monitore 2560 px: „ešte mi vadí to hluché miesto
    # vpravo, premysli to, ako stavbyvedúcemu mu to bude divné“). Chrome 25. 11. 2026 po zmene (krasa-8/siroke-wf/stavba,
    # skripty/sweep.mjs, okno bez posuvníka, obmedzený pohyb): okno -> (šírka obsahu, okraj obsahu vľavo aj vpravo,
    # H1 úvodu px). Pred zmenou mal obsah od 1888 px okna vždy 1760 px (pri 2560 px okraje 400 px, pri 3440 px 840 px)
    # a H1 najviac 64 px. Záznam: ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/SIROKE.md
    SIROKE_CHROME = {(1024, 768): (944.1, 39.9, 37.617), (1440, 900): (1327.7, 56.2, 52.897), (1920, 1080): (1760, 80, 64),
                     (1921, 1080): (1761, 80, 64.05), (2000, 1080): (1840, 80, 68), (2048, 1152): (1888, 80, 70.4),
                     (2200, 1080): (2040, 80, 75.6), (2304, 1296): (2144, 80, 83.2), (2560, 1000): (2400, 80, 70),
                     (2560, 1080): (2400, 80, 75.6), (2560, 1440): (2400, 80, 96), (2560, 1600): (2400, 80, 96),
                     (2600, 1080): (2400, 100, 75.6), (2880, 1620): (2400, 240, 96.628), (3440, 1080): (2400, 520, 75.6),
                     (3440, 1440): (2400, 520, 96.628), (3840, 2160): (2400, 720, 96.628)}
    # pravý stĺpec hrdinu vo vysokom okne (s prvým odsekom O firme) pri výške okna 1080 px (prah vysokého okna), Chrome
    # po zmene (sweep.mjs): od 1921 px rastie H1 s oknom až po 7svh (75,6 px pri 1080 px), stĺpec potom 873,8 px pri
    # každej širšej šírke; vyššie okno má H1 najviac 7svh, takže rezerva rastie (pri 1080 px najmenšia, 26,2 px)
    STLPEC_VYSOKE_SIROKE = {1921: 825.8, 1936: 829, 2000: 842.4, 2048: 852.4, 2112: 865.6, 2176: 873.8, 2200: 873.8,
                            2560: 873.8, 3000: 873.8, 3440: 873.8}
    # šírky obrázkov, ktoré Chrome kreslí bez posuvníka pri 2048 x 1152 a 2560 x 1440 (overenie širokého rozloženia,
    # krasa-8/siroke-wf/testy/sizes_chrome.mjs, sizes-chrome.json, najmenší rámik každého druhu na všetkých stránkach):
    # karta 4 : 5, titulná fotka detailu 3 : 2, bunka galérie, galéria do 3 fotiek, prvá veľká fotka galérie, výrez
    # tabuľky ASB (obrázok vnútri rámika 1 px) a fotka strany ASB. Po oprave V médiách (text 5/-1, strany na 6 z 8
    # stĺpcov) má strana tú istú šírku (oprava/oblast/meranie-po.json: 454 a 582 px).
    SIZES_CHROME = {'SIZES_KARTA': {2048: 454.4, 2560: 582.4}, 'SIZES_DETAIL': {2048: 1091.3, 2560: 1390.0},
                    'SIZES_GALERIA': {2048: 613.3, 2560: 784.0}, 'SIZES_GALERIA_MALO': {2048: 932.0, 2560: 1188.0},
                    'SIZES_GALERIA_PRVA': {2048: 1250.7, 2560: 1592.0}, 'SIZES_MEDIA_TABULKA': {2048: 452.0, 2560: 580.0},
                    'SIZES_MEDIA_STRANA': {2048: 454.0, 2560: 582.0}}

    @classmethod
    def h1_css(cls):
        """Koeficienty H1 úvodu zo styl.css (overenie širokého rozloženia, nález drobné: vzorec nemá byť len kópiou
        v teste): clamp(Arem, Bcqi, var(--h1-strop)), line-height, --h1-strop do 1920 px (Crem) a od 1921 px
        max(Drem, min(Evw - Fpx, Gsvh)), pri výške okna do 960 px navyše najviac Hsvh."""
        css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        h1 = re.search(r'\.uvod-h1\{[^}]*font-size:clamp\(([\d.]+)rem,([\d.]+)cqi,var\(--h1-strop\)\);line-height:([\d.]+);',
                       css_bez_blokov(css))
        zaklad = re.search(r':root\{--h1-strop:([\d.]+)rem\}', css_bez_blokov(css))
        siroke = re.search(r'--h1-strop:max\(([\d.]+)rem,min\(([\d.]+)vw - ([\d.]+)px,([\d.]+)svh\)\)',
                           ''.join(css_bloky(css, '@media (min-width:1921px)')))
        nizke = re.search(r'\.uvod-h1\{font-size:min\(clamp\([\d.]+rem,[\d.]+cqi,var\(--h1-strop\)\),([\d.]+)svh\)\}',
                          ''.join(css_bloky(css, '@media (min-width:1024px) and (max-height:960px)')))
        for m in (h1, zaklad, siroke, nizke):
            assert m is not None, 'pravidlo H1 úvodu v styl.css sa zmenilo'
        f = lambda x: float(x)
        return {'min_px': 16 * f(h1.group(1)), 'cqi': f(h1.group(2)) / 100, 'riadok': f(h1.group(3)),
                'strop_px': 16 * f(zaklad.group(1)), 'siroke_min_px': 16 * f(siroke.group(1)), 'siroke_vw': f(siroke.group(2)) / 100,
                'siroke_px': f(siroke.group(3)), 'siroke_svh': f(siroke.group(4)) / 100, 'nizke_svh': f(nizke.group(1)) / 100}

    @classmethod
    def h1_uvodu_px(cls, vw, vh):
        """H1 úvodu na počítači (od 1024 px, okno bez posuvníka) podľa koeficientov zo styl.css (h1_css): clamp(1.75rem,
        9.8cqi, --h1-strop), cqi je hlava v 5 z 12 stĺpcov mriežky; --h1-strop 4rem, od 1921 px max(4rem, min(5vw - 32px,
        7svh)); pri výške okna do 960 px navyše najviac 6svh."""
        k = cls.h1_css()
        obsah = build.sirka_obsahu_px(vw)
        medzera = min(24.0, max(12.0, 0.016 * vw))
        hlava = 5 * (obsah - 11 * medzera) / 12 + 4 * medzera
        strop = k['strop_px'] if vw < build.SIROKE_OKNO else max(k['siroke_min_px'], min(k['siroke_vw'] * vw - k['siroke_px'], k['siroke_svh'] * vh))
        h1 = max(k['min_px'], min(k['cqi'] * hlava, strop))
        return min(h1, k['nizke_svh'] * vh) if vh <= 960 else h1

    def test_siroke_okno_css(self):
        """Široké okno (od 1921 px, jeden blok @media (min-width:1921px) na konci štýlu): obsah min(100vw - 160px,
        2400px) namiesto 1760 px, strop H1 max(4rem, min(5vw - 32px, 7svh)), titulná fotka detailu bez stropu 1000 px,
        vizitka Kontaktu na 4 stĺpce, výrez tabuľky V médiách na 3 stĺpce s textom od 5. stĺpca po pravý okraj a fotkami
        strán na 6 z 8 stĺpcov textu, karta Činnosti bez zoznamov v dvoch stĺpcoch bez delenia odsekov a stránka služby
        s dlhým zoznamom (trieda riadky-N) so zoznamom v dvoch stĺpcoch so spoločnými riadkami, krátkym zoznamom na šírku
        stĺpca zoznamu a boxom s dokladom na 4 stĺpce; stránka služby bez zoznamu (Stavebný manažment) s textom cez 8
        stĺpcov a odsekmi v dvoch stĺpcoch a Ďalšie realizácie v štyroch stĺpcoch (audit naplno 25. 11. 2026, časť 2).
        Od 2561 px (obsah na strope) slider bez presahu k okraju okna,
        jediný ďalší prah. Do 1920 px platia tie isté hodnoty ako predtým (--sirka 1760 px, --h1-strop 4rem). Mriežka,
        vlasové čiary a hlavička idú cez tú istú --sirka, slider k ľavej hrane okna cez 100cqw (posuvník)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        bloky = css_bloky(css, '@media (min-width:1921px)')
        self.assertEqual(len(bloky), 1)
        self.assertEqual([r.strip() for r in bloky[0].split('\n') if r.strip()], [
            ':root{--sirka:min(100vw - 160px,2400px);--h1-strop:max(4rem,min(5vw - 32px,7svh))}',
            '.detail .detail-foto{max-width:none}',
            # oprava po overení: do 1920 px tabuľka a text detailu od 8. stĺpca, od 1921 px ako predtým od 9.
            '.detail-s-udajmi .detail-udaje,.detail-s-udajmi .detail-text{grid-column:9/-1}',
            '.kontakt-mapa{grid-column:9/-1}',
            '.media-tabulka{grid-column:1/4}',
            '.media-text{grid-column:5/-1}',
            '.media-strany{max-width:calc((100% + var(--medzera)) * 6 / 8 - var(--medzera))}',
            '.cinnost-karta>.cinnost-text:not(:has(>ul)):has(>p+p){columns:2;column-gap:var(--medzera)}',
            '.cinnost-karta>.cinnost-text:not(:has(>ul))>p{break-inside:avoid}',
            '.sluzba-obal:has(.cinnost-body[class*="riadky-"]) .sluzba-hlavne{grid-column:1/9}',
            '.sluzba-obal:has(.cinnost-body[class*="riadky-"]) .sluzba-bok{grid-column:9/-1}',
            '.sluzba-obal:has(.cinnost-body[class*="riadky-"]) .cinnost-body[class*="riadky-"]{display:grid;'
            'grid-template-columns:repeat(2,minmax(0,1fr));column-gap:var(--medzera);grid-auto-flow:column;max-width:none}',
            '.sluzba-obal:has(.cinnost-body[class*="riadky-"]) .cinnost-body:not([class*="riadky-"])'
            '{max-width:calc((100% - var(--medzera)) / 2)}',
            # audit naplno 25. 11. 2026, položka 25: Stavebný manažment (bez zoznamu) text cez 8 stĺpcov, odseky v dvoch
            # stĺpcoch, box na 4 stĺpce; kontrola širokého rozloženia: Ďalšie realizácie v štyroch stĺpcoch pod kartami
            '.sluzba-obal:not(:has(.cinnost-body)) .sluzba-hlavne{grid-column:1/9;display:grid;grid-template-columns:'
            'repeat(2,minmax(0,1fr));grid-template-rows:auto auto auto 1fr;column-gap:var(--medzera);align-content:start}',
            '.sluzba-obal:not(:has(.cinnost-body)) .sluzba-hlavne>:not(p),.sluzba-obal:not(:has(.cinnost-body)) '
            '.sluzba-hlavne>.uvodna{grid-column:1/-1}',
            '.sluzba-obal:not(:has(.cinnost-body)) .sluzba-hlavne>.uvodna+p{grid-row:span 2}',
            '.sluzba-obal:not(:has(.cinnost-body)) .sluzba-bok{grid-column:9/-1}',
            '.zoznam-dalsie{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));column-gap:var(--medzera)}',
            '.zoznam-dalsie li{align-content:start}'])
        self.assertEqual(css.count('1921px'), 1)
        self.assertNotIn('1920px', css)
        # ultraširoké okno: jediný ďalší prah, len slider bez presahu, za blokom širokého okna
        ultra = css_bloky(css, '@media (min-width:2561px)')
        self.assertEqual([[r.strip() for r in b.split('\n') if r.strip()] for b in ultra], [['.slider{margin-left:0}']])
        self.assertEqual(css.count('2561px'), 1)
        self.assertNotIn('2560px', css)
        self.assertGreater(css.index('@media (min-width:2561px){'), css.index('@media (min-width:1921px){'))
        self.assertEqual(build.SLIDER_BEZ_PRESAHU, 2561)
        self.assertIn('--sirka:1760px;', css)
        self.assertIn(':root{--h1-strop:4rem}', css)
        self.assertNotIn('9.8cqi,4rem', css)
        # blok je za všetkými blokmi počítača (rovnaká špecificita, prepíše ich)
        zaciatok = css.index('@media (min-width:1921px){')
        for blok in ('@media (min-width:1024px)', '@media (min-width:1536px)', '@media (min-width:1680px)'):
            self.assertGreater(zaciatok, css.rindex(blok), blok)
        for kus in ('.obsah{width:min(var(--sirka),100% - 2*var(--okraj));margin-inline:auto}',
                    '.ciary-vnutro{height:100%;width:min(var(--sirka),100% - 2*var(--okraj));',
                    'margin-left:calc(-1 * max(var(--okraj), (100cqw - var(--sirka)) / 2))'):
            self.assertIn(kus, css)
        # karta Činnosti v dvoch stĺpcoch je len 1. Stavebný manažment (odseky bez zoznamu), ostatné majú zoznamy
        cin = self.info('')['telo'].split('<section class="sekcia cinnost-sekcia"', 1)[1].split('</section>', 1)[0]
        texty = [k.split('<div class="cinnost-text">', 1)[1] for k in cin.split('<article class="cinnost-karta"')[1:]]
        self.assertEqual(len(texty), 3)
        stlpce = [('<ul' not in t.split('</article>', 1)[0] and t.split('</article>', 1)[0].count('<p>') >= 2) for t in texty]
        self.assertEqual(stlpce, [True, False, False])
        # stránka služby: dlhý zoznam (riadky-N) má Stavebný dozor a Stavbyvedúci, Stavebný manažment je bez zoznamu
        # (selektor :has ho vynechá, karta tam ostáva na 5 stĺpcoch)
        for slug, dlhy in (('stavebny-dozor', True), ('stavbyveduci', True), ('stavebny-manazment', False)):
            obal = self.info(f'{slug}/')['telo'].split('<div class="obsah mriezka sluzba-obal">', 1)[1].split('</aside>', 1)[0]
            self.assertEqual(bool(re.search(r'<ul class="cinnost-body riadky-\d+">', obal)), dlhy, slug)

    def test_siroke_okno_obsah_a_h1_ako_chrome(self):
        """Šírka obsahu (build.sirka_obsahu_px) a H1 úvodu podľa vzorca zo styl.css sedia s Chrome (SIROKE_CHROME, okná
        1024 x 768 až 3840 x 2160) do 0,1 px: obsah pri 1920 a 1921 px spojitý (1760 a 1761 px), od 1921 px okraj 80 px,
        strop 2400 px (pri 3440 px okraje 520 px); H1 64 px pri 1920, 70,4 pri 2048 x 1152, 96 pri 2560 x 1440 a 75,6
        pri 2560 x 1080. Konštanty build.py (OBSAH_MAX, OKRAJ_SIROKY, OBSAH_SIROKY_MAX, SIROKE_OKNO) sú tie isté ako
        hodnoty v styl.css (overenie širokého rozloženia, nález drobné: cyklus nad vzorcom z implementácie by zmenu len
        v štýle neodhalil)."""
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        zaklad = re.search(r'--sirka:([\d.]+)px;', css_bez_blokov(css))
        siroke = re.search(r':root\{--sirka:min\(100vw - ([\d.]+)px,([\d.]+)px\);',
                           ''.join(css_bloky(css, f'@media (min-width:{build.SIROKE_OKNO}px)')))
        self.assertIsNotNone(zaklad)
        self.assertIsNotNone(siroke)
        self.assertEqual(float(zaklad.group(1)), build.OBSAH_MAX)
        self.assertEqual(float(siroke.group(1)), 2 * build.OKRAJ_SIROKY)
        self.assertEqual(float(siroke.group(2)), build.OBSAH_SIROKY_MAX)
        for (vw, vh), (obsah, okraj, h1) in self.SIROKE_CHROME.items():
            self.assertAlmostEqual(build.sirka_obsahu_px(vw), obsah, delta=0.1, msg=(vw, vh))
            self.assertAlmostEqual((vw - build.sirka_obsahu_px(vw)) / 2, okraj, delta=0.1, msg=(vw, vh))
            self.assertAlmostEqual(self.h1_uvodu_px(vw, vh), h1, delta=0.01, msg=(vw, vh))
        # od prahu obsah podľa hodnôt zo styl.css (nie z build.py), do prahu ako pred zmenou
        dva_okraje, strop_css = float(siroke.group(1)), float(siroke.group(2))
        for vw in range(1024, 4000):
            okraj = min(64.0, max(16.0, 0.039 * vw))
            css_sirka = float(zaklad.group(1)) if vw < build.SIROKE_OKNO else min(vw - dva_okraje, strop_css)
            self.assertEqual(build.sirka_obsahu_px(vw), min(css_sirka, vw - 2 * okraj), vw)
        # H1 do 1920 px najviac 64 px (ako pred zmenou), od 1921 px nikdy menšie než pri 1920 px
        for vh in (768, 900, 1080, 1440):
            self.assertLessEqual(self.h1_uvodu_px(1920, vh), 64)
            self.assertGreaterEqual(self.h1_uvodu_px(1921, vh), self.h1_uvodu_px(1920, vh))

    def test_siroke_okno_prva_obrazovka(self):
        """Prvá obrazovka platí ako pred zmenou aj pri širokom okne. Chrome po zmene (sweep.mjs, 858 okien: šírky 1024 až
        3440 px po 64 px, výšky 600 až 1440 px po 40 px a navyše 2560 x 1080, 2560 x 1000, 3440 x 1440, 3840 x 2160,
        1921 až 3000 x 1080; bez posuvníka aj s klasickým posuvníkom 17 px): fotka slidera po spodok okna (okrem
        známeho stropu 75vw v úzkych vysokých oknách), štítok stavby, ovládanie slidera aj tlačidlá Zavolať a Napísať
        e-mail celé v okne, slider na ľavej hrane okna, štítok 16 px od nej, okraje obsahu súmerné, bez vodorovného
        posunu; chyba 0 (pred zmenou tiež 0). Od 2561 px slider bez presahu (oprava po overení, hrdina.mjs pri 2561 až
        3840 px: fotka začína na čiare loga, pravý okraj za údajmi rovnaký ako ľavý). Vzorcom so štýlom zo styl.css
        (h1_css): pravý stĺpec vo vysokom okne pri prahu 1080 px (STLPEC_VYSOKE_SIROKE, zmerané s H1 75,6 px) sa
        zmestí s riadkami 48 px a hlavičkou 84 px s rezervou aspoň 20 px a sizes slidera ráta s poľom aspoň takým
        vysokým (SLIDER_STLPEC); vyššie okno má H1 najviac 7svh, takže rezerva rastie."""
        k = self.h1_css()
        for w, stlpec in self.STLPEC_VYSOKE_SIROKE.items():
            potreba = stlpec + 2 * 48 + build.SLIDER_HLAVICKA
            self.assertLessEqual(potreba + 20, build.SLIDER_VYSOKE_OKNO + 1, w)
            self.assertGreaterEqual(build.SLIDER_STLPEC, stlpec + 2 * 48, w)
            # H1 pri prahu zo styl.css nie je väčšie než pri meraní stĺpca (75,6 px); väčší strop výškou by stĺpec
            # pri 1080 px natiahol (prepočet nižšie)
            self.assertLessEqual(self.h1_uvodu_px(w, 1080), 75.6 + 1e-6, w)
        # od 2561 px (slider bez presahu) je pole vo vysokom okne aspoň 1080 - 84 px, viac než SLIDER_STLPEC, takže
        # sizes_slidera tam stĺpec nepotrebuje
        self.assertGreater(build.SLIDER_VYSOKE_OKNO + 1 - build.SLIDER_HLAVICKA, build.SLIDER_STLPEC)
        # stĺpec bez H1 (nadtitulok, odsek, údaje, zdroj, tlačidlá) pri 2560 x 1080: zmeraný stĺpec - 4 riadky H1
        h1_prah = self.h1_uvodu_px(2560, 1080)
        bez_h1 = self.STLPEC_VYSOKE_SIROKE[2560] - 4 * k['riadok'] * h1_prah
        # vysoké okno od 1080 px s H1 zo styl.css: rezerva najmenšia pri 1080 px a nikdy pod 20 px
        for vh in range(1080, 2200, 20):
            for vw in (2560, 3440):
                h1 = self.h1_uvodu_px(vw, vh)
                self.assertGreaterEqual(vh - (bez_h1 + 4 * k['riadok'] * h1 + 2 * 48 + build.SLIDER_HLAVICKA), 20, (vw, vh))
        # pri vyššom H1 (5vw - 32px bez stropu výškou, 96 px pri 2560 px) by sa stĺpec pri 2560 x 1080 nezmestil: preto 7svh
        h1_bez_stropu = min(k['cqi'] * (5 * (2400 - 11 * 24) / 12 + 4 * 24), k['siroke_vw'] * 2560 - k['siroke_px'])
        self.assertLess(1080 - (bez_h1 + 4 * k['riadok'] * h1_bez_stropu + 2 * 48 + build.SLIDER_HLAVICKA), 0)

    def test_siroke_okno_sizes(self):
        """sizes obrázkov (build.py) majú pre široké okno vlastnú prvú položku (min-width: 1921px), ostatné položky sú
        tie isté ako pred zmenou, takže prehliadač pri okne do 1920 px vyberá ten istý súbor (Chrome: currentSrc pri
        1920 x 1080, 1440 x 900 pri hustote 2 a 390 x 844 pri hustote 3 bez zmeny, SIROKE.md). Vzorec širokého okna je
        šírka obrázka v n stĺpcoch mriežky obsahu min(vw - 160, 2400) px s medzerou 24 px (n x C / 12 - (12 - n) x 2),
        nikdy menšia a najviac o 1 px väčšia pri každej šírke okna 1921 až 3840 px, a sedí so šírkou, ktorú Chrome
        kreslí pri 2048 a 2560 px (SIZES_CHROME, -0,5 až +2,5 px). Slider: reťazec sizes_slidera vyhodnotený pri
        každom okne dáva to isté ako sizes_slidera_px; šírka poľa do 1920 px tá istá ako predtým, od 1921 do 2560 px
        horný odhad poľa (7 stĺpcov a presah k ľavému okraju okna) najviac o 0,5 px a od 2561 px presne 7 stĺpcov
        obsahu bez presahu (1390 px)."""
        stare = {'SIZES_KARTA': '(min-width: 1024px) min(23vw, 425px), (min-width: 640px) 31vw, (min-width: 480px) 47vw, '
                                'calc(100vw - 32px)',
                 'SIZES_DETAIL': '(min-width: 1024px) min(56vw, 1000px), calc(100vw - 32px)',
                 'SIZES_HRDINA': '(min-width:1024px) min(56vw, 1000px), (min-width:768px) 92vw, calc(100vw - 32px)',
                 'SIZES_GALERIA_PRVA': '(min-width:1024px) min(62vw, 1150px), calc(100vw - 32px)',
                 'SIZES_GALERIA_MALO': '(min-width:1024px) min(62vw, 1150px), calc(100vw - 32px)',
                 'SIZES_GALERIA': '(min-width:640px) min(30vw, 570px), 46vw',
                 'SIZES_MEDIA_TABULKA': '(min-width:1536px) min(30vw, 580px), (min-width:1024px) 38vw, (min-width:768px) 58vw, '
                                        'calc(100vw - 32px)',
                 'SIZES_MEDIA_STRANA': '(min-width:1024px) min(22vw, 430px), calc(50vw - 24px)'}
        stlpce = {'SIZES_KARTA': 3, 'SIZES_DETAIL': 7, 'SIZES_HRDINA': 7, 'SIZES_GALERIA_PRVA': 8, 'SIZES_GALERIA_MALO': 6,
                  'SIZES_GALERIA': 4, 'SIZES_MEDIA_TABULKA': 3, 'SIZES_MEDIA_STRANA': 3}
        for meno, n in stlpce.items():
            casti = build._casti_sizes(getattr(build, meno))
            self.assertEqual(', '.join(casti[1:]), stare[meno], meno)
            m = re.fullmatch(r'\(min-width: ?1921px\) min\(([\d.]+)vw - ([\d.]+)px, (\d+)px\)', casti[0])
            self.assertTrue(m, (meno, casti[0]))
            a, b, c = float(m.group(1)), float(m.group(2)), float(m.group(3))
            for vw in range(1921, 3841):
                obsah = build.sirka_obsahu_px(vw)
                obraz = n * obsah / 12 - (12 - n) * 2
                sizes = min(a * vw / 100 - b, c)
                self.assertGreaterEqual(sizes, obraz - 0.01, (meno, vw))
                self.assertLessEqual(sizes, obraz + 1.0, (meno, vw))
        # celý sizes vyhodnotený ako v prehliadači voči šírke, ktorú Chrome kreslí
        for meno, okna in self.SIZES_CHROME.items():
            for vw, chrome in okna.items():
                sizes = vyhodnot_sizes(getattr(build, meno), vw, vw * 9 / 16)
                self.assertGreaterEqual(sizes, chrome - 0.5, (meno, vw))
                self.assertLessEqual(sizes, chrome + 2.5, (meno, vw))
        # slider: reťazec a jeho prepis v Pythone pri oknách 1024 až 3840 px a výškach okolo prahov 820, 1079 a 1080 px
        for w, h, orez in ((4, 3, 0.0), (2400, 1350, 0.0), (2000, 2657, 0.17), (1, 10, 0.0)):
            s = build.sizes_slidera(w, h, orez)
            for vw in list(range(1024, 3841, 37)) + [1920, 1921, 2560, 2561, 3440, 3840]:
                for vh in (600, 820, 821, 900, 1079, 1080, 1440, 2160):
                    self.assertAlmostEqual(vyhodnot_sizes(s, vw, vh), build.sizes_slidera_px(w, h, orez, vw, vh), delta=0.01,
                                           msg=(w, h, orez, vw, vh))
        # šírka poľa zo sizes (fotka 1 : 10, výška poľa nerozhoduje) voči rozloženiu v styl.css (pole_slidera_px)
        for vw in range(1024, 3841):
            sirka = build.sizes_slidera_px(1, 10, 0.0, vw, 900)
            pole = build.pole_slidera_px(vw, 900)[0]
            if vw <= 1920:
                self.assertEqual(sirka, min(0.577 * vw, 0.5 * vw + 137), vw)
            elif vw < build.SLIDER_BEZ_PRESAHU:
                self.assertGreaterEqual(sirka, pole - 0.01, vw)
                self.assertLessEqual(sirka, pole + 0.5, vw)
            else:
                self.assertEqual(sirka, build.SLIDER_POLE_BEZ_PRESAHU, vw)
                self.assertAlmostEqual(pole, build.SLIDER_POLE_BEZ_PRESAHU, delta=1e-9, msg=vw)
        # stránky používajú tieto sizes: galéria do 3 fotiek (galeria-malo) má vlastný, prvá veľká a mriežka svoj
        malo = self.info('realizacie/bytovy-dom-haanova-bratislava/')['telo']
        self.assertIn('<ul class="galeria galeria-malo" data-galeria>', malo)
        img = re.search(r'<ul class="galeria galeria-malo" data-galeria>.*?<img [^>]*>', malo, re.S).group(0)
        self.assertIn('sizes="' + build.SIZES_GALERIA_MALO.split(',')[0], img.replace('calc(', ''))
        prva = self.info('realizacie/digitalizacne-pracovisko-sfu-spitalska-bratislava/')['telo']
        li = re.search(r'<li class="galeria-polozka prva">(.*?)</li>', prva, re.S).group(1)
        self.assertIn('(min-width:1921px) ', li)
        self.assertIn('66.67vw - 114px', li)
        # sizes slidera v HTML úvodu (11 fotiek, AVIF prvej a preload): od 2561 px nezávisí od šírky okna (pole bez
        # presahu, pred opravou pri 3440 px 1910 px, keď pole siahalo po ľavý okraj okna), fotka užšia než pole dostane
        # presne 1390 px
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        slider = [html.unescape(s) for s in re.findall(r' (?:image)?sizes="([^"]+)"', uvod) if 'max(min(57.7vw' in s]
        self.assertEqual(len(slider), 13)
        for s in slider:
            self.assertTrue(s.startswith('(min-width: 2561px) and (max-height: 820px) max(1390px, '), s)
            for vh in (800, 1080, 1440):
                self.assertEqual(vyhodnot_sizes(s, 3440, vh), vyhodnot_sizes(s, 2561, vh), (s, vh))
                self.assertGreaterEqual(vyhodnot_sizes(s, 3440, vh), build.SLIDER_POLE_BEZ_PRESAHU)
        self.assertIn(build.SLIDER_POLE_BEZ_PRESAHU, {vyhodnot_sizes(s, 3440, 1080) for s in slider})


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
        # JSON-LD realizácie bez dňa úpravy a dňa príspevku (doladenie-3: pri realizáciách len roky)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('dateModified', s)
        self.assertNotIn('datePublished', s)
        self.assertNotIn('2030-01-02', s)

    def test_rok_prispevku_z_obsahu_alebo_z_datumu(self):
        """ROKY.md (Andrej 26. 11. 2026): bez rokov stavby má realizácia rok príspevku, rok_prispevku z obsahu má
        prednosť pred rokom z poľa datum, vždy len rok bez mesiaca a dňa (karta, data-rok, detail pod popiskom Roky,
        description, poradie); realizácia bez rokov aj dátumu (hola) má len vlasovú čiaru (audit naplno položka 15),
        bez riadku Roky a filter Roky má voľbu „rok neuvedený“; časová os ide od najstaršieho po najnovší rok kariet."""
        self.uprav('plna', roky='', datum='2023-06-15')
        (self.obsah / 'realizacie' / 'rok.json').write_text(json.dumps(
            {'slug': 'rok', 'nazov': 'Rodinný dom Rok', 'datum': '2021-03-04', 'rok_prispevku': '2022', 'zverejnene': True},
            ensure_ascii=False), encoding='utf-8')
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah, rychle=True)
        self.assertEqual(kod, 0, err)
        telo = stranka_info(web / 'realizacie' / 'index.html')['telo']
        karty = {re.search(r'realizacie/([a-z0-9-]+)/', o).group(1): (a, o)
                 for a, o in re.findall(r'<li class="dlazdica[^"]*"([^>]*)><figure>(.*?)</figure></li>', telo, re.S)}
        for slug, rok in (('plna', '2023'), ('rok', '2022'), ('xss', '2020')):
            a, o = karty[slug]
            self.assertIn(f'<span class="kota"><span class="kota-text">{rok}</span></span>', o, slug)
            self.assertIn(f'data-rok="{rok}"', a, slug)
            info = stranka_info(web / 'realizacie' / slug / 'index.html')
            self.assertIn(f'<dt>Roky</dt><dd>{rok}</dd>', info['telo'], slug)
        # kóta nad textom detailu (realizácia rok nemá fotku ani text, dvojstrana s kótou sa nezostaví)
        plna = stranka_info(web / 'realizacie' / 'plna' / 'index.html')
        self.assertIn('<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">2023</span>', plna['telo'])
        self.assertIn('Roky: 2023.', plna['desc'])
        a, o = karty['hola']
        self.assertIn('<span class="kota kota-prazdna" aria-hidden="true"></span>', o)
        self.assertIn('data-rok="0"', a)
        self.assertNotIn('<dt>Roky</dt>', stranka_info(web / 'realizacie' / 'hola' / 'index.html')['telo'])
        vyber = telo.split('<select data-roky>', 1)[1].split('</select>', 1)[0]
        self.assertEqual(re.findall(r'<option value="(\d*)">([^<]+)</option>', vyber),
                         [('', 'všetky'), ('2020', '2020 až 2023'), ('0', 'rok neuvedený')])
        os_ = telo.split('<div class="casova-mierka">', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'<b>(\d{4})</b>', os_), ['2020', '2023'])
        self.assertEqual(len(re.findall(r'<span class="m-rok v[1-4]">', os_)), 3)
        # poradie podľa toho istého roku ako na karte (2023, 2022, 2020), realizácia bez roku na konci
        poradie = sorted(karty, key=lambda s: int(re.search(r'data-poradie="(\d+)"', karty[s][0]).group(1)))
        self.assertEqual(poradie, ['plna', 'rok', 'xss', 'hola'])
        for p in web.rglob('*.html'):
            s = p.read_text(encoding='utf-8')
            for d in ('2023-06-15', '2021-03-04'):
                self.assertNotIn(d, s, p)

    def test_doladenie3_text_len_roky(self):
        """Doladenie-3 (overenie 3: pri realizáciách len roky): veta s mesiacom sa pri zostave nahradí podľa nastavenia
        text_len_roky (obsah sa nemení), v texte stránky, description JSON-LD aj meta description; pôvodná veta, ktorá
        v texte nie je, zlá položka nastavenia a text zverejnenej realizácie s mesiacom bez nahradenia sú varovania,
        zostavu nezhodia; skrytá a neexistujúca realizácia v nastavení nič nerobia."""
        self.uprav('plna', text='Bytový dom. Skolaudované 12/2013.\nDruhý odsek z novembra 2020.')
        self.uprav('skryta', text='Skolaudované 01/2010.')
        sablona = sablona_s_nastaveniami(self.tmp, **NASTAVENIA_MALY, text_len_roky={
            'plna': [['Skolaudované 12/2013.', 'Skolaudované v roku 2013.'], ['Veta, ktorá nie je.', 'Nič.'], 'zlé'],
            'skryta': [['Skolaudované 01/2010.', 'Skolaudované v roku 2010.']], 'neexistuje': [['a', 'b']]})
        web = self.tmp / 'web'
        with mock.patch.object(build, 'SABLONA', sablona):
            kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('12/2013', s)
        self.assertIn('"description":"Bytový dom. Skolaudované v roku 2013. Druhý odsek z novembra 2020."', s)
        self.assertIn('Skolaudované v roku 2013.', s)
        self.assertIn("text_len_roky: veta 'Veta, ktorá nie je.' v texte realizácie plna nie je", err)
        self.assertIn("text_len_roky: plna 'zlé' nie je dvojica", err)
        # druhá veta s mesiacom v nastavení nie je: varovanie s nájdeným slovom
        self.assertIn("realizácia plna: text má mesiac alebo deň ('novembra')", err)
        self.assertEqual([r for r in err.splitlines() if 'skryta' in r or 'neexistuje' in r], [])
        self.assertFalse((web / 'realizacie' / 'skryta').exists())

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
        # jeden rok tiež pod popiskom Roky ako predtým (Andrej 26. 11. 2026: „roky xxx“, ROKY.md)
        self.assertIn('Projektant: Kappa 1 s.r.o. Roky: 2019.', d)
        self.assertNotIn('Rok:', d)
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
        # preload a prvá fotka slidera; preload loga hlavičky (doladenie-2) je obrázok mimo LCP
        self.assertEqual(s.count('fetchpriority="high"') - s.count('logo-hlavicka.svg" as="image" type="image/svg+xml" '
                                                                   'fetchpriority="high"'), 2)
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
            # Instagram je profil firmy (@bmbuilding, Andrej 26. 11. 2026): patrí s Facebookom do sameAs firmy, nie
            # k osobe konateľa (employee)
            self.assertEqual('https://www.instagram.com/bmbuilding/' in uzol.get('sameAs', []), ma_byt, adresa)
            self.assertNotIn('https://www.instagram.com/bmbuilding/', uzol['employee'].get('sameAs', []))

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
        # karta zo zvyšnej ostrej fotky: 1000 x 700 dá výrez 4 : 5 len 560 x 700 (pod build.KARTA_MIN_OREZ); od
        # doladenia-1 aj tak výrez cez celý rám (nie celá fotka foto-cela v strede rámu), najväčší súbor 560 px
        self.assertIn('src="../obrazky/mix/02-560-45.webp"', mix)
        self.assertNotIn('foto-cela', mix)
        self.assertNotIn('-43.webp', mix)
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
        # jedna fotka na kartu, bez rozmazaného podkladu (doladenie-1: foto-podklad z krasa-8 je preč)
        self.assertFalse([i for i in imgs if 'foto-podklad' in i])
        self.assertEqual(len(imgs), 120 - len(range(0, 120, 7)))
        # len fotka prvej karty (LCP) je bez loading=lazy a s fetchpriority=high (oprava 1)
        self.assertEqual(sum(1 for i in imgs if 'fetchpriority="high"' in i), 1)
        for img in imgs:
            if 'fetchpriority="high"' not in img:
                self.assertIn('loading="lazy"', img)
            # fotka 720 x 540: výrez 4 : 5 má len 432 px (pod build.KARTA_MIN_OREZ), od doladenia-1 aj tak výrez cez
            # celý rám ako ostatné karty (nie celá fotka foto-cela v strede rámu); väčšia šírka než výrez sa nevyrába
            self.assertRegex(img, r'srcset="[^"]*-45\.webp \d+w')
            self.assertNotIn('foto-cela', img)
            self.assertNotRegex(img, r'-(4[89]\d|[5-9]\d\d|\d{4,})-45\.webp')
            self.assertRegex(img, r'width="\d+" height="\d+"')
        self.assertLess(len(gzip.compress(s.encode('utf-8'), 9)), 40 * 1024)
        css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        # bez content-visibility (audit naplno 25. 11. 2026, položka 40); karty ju nemajú kvôli subgrid
        self.assertNotIn('content-visibility', css)
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

    stav = re.search(r'<p class="filtre-stav" data-stav aria-live="polite">([^<]*)</p>', telo)
    assert stav, 'riadok stavu zoznamu chýba'
    return {
        'naraz': int(re.search(r'data-zoznam data-naraz="(\d+)"', telo).group(1)),
        'stav': html.unescape(stav.group(1)),
        'karty': [{'poradie': int(a['data-poradie']), 'druh': a.get('data-druh', ''), 'rok': a.get('data-rok', ''),
                   'mesto': a.get('data-mesto', ''), 'nazov': html.unescape(n), 'neskor': 'data-neskor' in a}
                  for a, n in zip(li, mena)],
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
            # zvyšok najviac polovica dávky sa ukáže naraz s dávkou (audit naplno 25. 11. 2026, položka 17)
            m = n if not naraz or n - ukazat <= naraz // 2 else ukazat
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
            # data-neskor (karty za prvou dávkou do spustenia app.js, položka 46) app.js hneď zmaže
            if st.get('neskor'):
                chyby.append(f'{kde}: karty {st["neskor"]} majú ešte data-neskor')
            if k and k['akcia'] == 'viac' and s['zoradit'] != 'nazov':
                ocak_f = vyber[bolo]['poradie'] if bolo < n else None
                if st['fokus'] != ocak_f:
                    chyby.append(f'{kde}: fokus na karte {st["fokus"]}, čakaný {ocak_f} (prvá nová)')
            elif k and k['akcia'] != 'viac' and st['fokus'] is not None:
                chyby.append(f'{kde}: fokus sa presunul na kartu {st["fokus"]} bez Zobraziť ďalšie')
            # po Zrušiť filter (tlačidlo v prázdnom výsledku, ktorý sa skryje) fokus na tlačidle Všetky, nie na body
            # (oprava po overení, WCAG 2.4.3); iný krok fokus na tlačidlo filtra nepresúva
            ocak_d = '' if k and k['akcia'] == 'zrusit' else None
            if st.get('fokus_druh') != ocak_d:
                chyby.append(f'{kde}: fokus na tlačidle filtra {st.get("fokus_druh")!r}, čakané {ocak_d!r}')
            # karta s fokusom sa posunie do okna najkratšou cestou (položka 36), inak sa nič neposúva
            ocak_p = {'poradie': st['fokus'], 'block': 'nearest'} if st['fokus'] is not None else None
            if st['posun'] != ocak_p:
                chyby.append(f'{kde}: posun do okna {st["posun"]}, čakaný {ocak_p}')
            q = '&'.join(f'{a}={s[a]}' for a in ('druh', 'roky', 'mesto') if s[a]) + (
                ('&' if filtruje else '') + f'zoradit={s["zoradit"]}' if s['zoradit'] != 'najnovsie' else '')
            if k and st['search'] != (f'?{q}' if q else ''):
                chyby.append(f'{kde}: adresa {st["search"]}, čakaná ?{q}')

    # riadok stavu z HTML (build.py, doladenie-1) je ten istý text, aký app.js napíše bez filtra v adrese: pri pomalej
    # sieti sa po spustení skriptu nič nezmení ani neposunie
    m0 = n_vsetky if not naraz or n_vsetky - naraz <= naraz // 2 else naraz
    if d['stav'] != (f'Zobrazené: {m0} z {n_vsetky}' if m0 < n_vsetky else ''):
        chyby.append(f'riadok stavu v HTML „{d["stav"]}“ nie je stav po spustení app.js bez filtra')
    # data-neskor v HTML práve na kartách, ktoré app.js po spustení bez filtra neukáže (položka 46): do spustenia app.js
    # nie sú v rozložení, ich fotky sa nesťahujú
    neskor = [k['poradie'] for k in karty if k['neskor']]
    if neskor != [k['poradie'] for k in sorted(karty, key=lambda x: x['poradie'])[m0:]]:
        chyby.append(f'data-neskor v HTML na kartách {neskor}, čakané za prvými {m0}')
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

def vyhodnot_sizes(sizes, vw, vh):
    """Dĺžka v CSS px, ktorú prehliadač vezme z atribútu sizes pri okne vw x vh: prvá položka, ktorej podmienka
    ((min|max)-(width|height) v px, spojené and) platí; dĺžka z čísel s vw, vh a px, calc, min a max (overenie
    širokého rozloženia, nález drobné: test sizes slidera nemá byť len kópiou vzorca)."""
    for kus in build._casti_sizes(sizes):
        m = re.fullmatch(r'((?:\((?:min|max)-(?:width|height): ?[\d.]+px\)(?: and )?)*)\s*(.+)', kus)
        podmienky, dlzka = m.group(1), m.group(2)
        plati = all((vw if os == 'width' else vh) >= float(x) if druh == 'min' else (vw if os == 'width' else vh) <= float(x)
                    for druh, os, x in re.findall(r'\((min|max)-(width|height): ?([\d.]+)px\)', podmienky))
        if not plati:
            continue
        vyraz = dlzka.replace('calc(', '(')
        vyraz = re.sub(r'([\d.]+)vw', lambda z: f'({z.group(1)}*{vw}/100)', vyraz)
        vyraz = re.sub(r'([\d.]+)vh', lambda z: f'({z.group(1)}*{vh}/100)', vyraz)
        vyraz = re.sub(r'([\d.]+)px', r'\1', vyraz)
        if not re.fullmatch(r'[\d.+\-*/(), minax]+', vyraz):
            raise ValueError(dlzka)
        return eval(vyraz, {'__builtins__': {}}, {'min': min, 'max': max})
    raise ValueError(sizes)


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
            # preload loga hlavičky (doladenie-2, vždy rovnaký malý súbor v hlave) nie je fotka stránky
            logo = re.findall(r'<link rel="preload" href="[^"]*logo-hlavicka\.svg"[^>]*>', t)
            self.assertEqual(len(logo), 1, cesta)
            self.assertLessEqual(t.count('fetchpriority=') - logo[0].count('fetchpriority='), 1, cesta)
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

    def test_rd_ahoj_03_bez_rozmazanych_ploch(self):
        """Overenie 3 krasa-8 (nález drobné, 25. 11. 2026): tretia fotka galérie RD Ahoj (03, brána zblízka) mala tie isté
        dve rozmazané plochy na gabióne ako 01 (číslo domu a menovka, rozmazané už v zdroji z Facebooku). Retušovaná
        predloha 1440 x 1920 (bez zväčšenia, pole retus) ich zakrýva klonom gabiónu z okolia. Žiadny súbor webu nesmie
        mať v mieste plôch hladkú plochu: jemná kresba vnútri oboch plôch aspoň 0,4 kresby gabiónu 110 px pod nimi
        (zmerané 25. 11.: zdroj 0,016 a 0,013, retušovaná predloha 0,885 a 0,912)."""
        from PIL import ImageChops, ImageFilter, ImageStat
        man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']
        z = next(z for z in man.values() if z['rel'] == 'rd-ahoj-bratislava-nove-mesto/03.jpg')
        self.assertTrue(z['predloha'].get('retus'))
        pw_, ph_ = z['predloha']['w'], z['predloha']['h']
        self.assertEqual((pw_, ph_), (1440, 1920))
        plochy = [(1035, 1022, 1072, 1071), (1094, 1015, 1179, 1074)]  # vnútro plôch, px predlohy (= zdroja)
        self.assertTrue(z['subory'])
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
        # OG obrázky (doladenie-2, overenie 2: og/bmbuilding.jpg sa kreslil zo zdroja v obsah/ a mal obe plochy, pomer
        # kresby 0,169 a 0,191): OG každej stránky, ktorá RD Ahoj 01 použije (úvod a predvolený OG 9 stránok), sa kreslí
        # z najväčšieho WebP retušovanej predlohy vo fotky-web; fotka vpravo ako ImageOps.fit na 480 x 630 px (og_bajty)
        hrdina = self.w.hrdina_foto()[1]
        self.assertEqual(hrdina['rel'], 'rd-ahoj-bratislava-nove-mesto/01.jpg')
        zdroj_og = Path(self.w.og_foto(hrdina))
        self.assertEqual(zdroj_og.parent.parent, KOREN / 'fotky-web')
        self.assertEqual(zdroj_og.suffix, '.webp')
        self.assertEqual(hashlib.sha256(zdroj_og.read_bytes()).hexdigest(),
                         max(((int(k.split('|')[1]), s) for k, s in z['subory'].items() if k.startswith('|') and k.endswith('|webp')),
                             key=lambda x: x[0])[1]['sha256'])
        # (detail RD Ahoj má obal 06, takže dnes je to len og/bmbuilding.jpg; zoznam sa ráta, nie je napevno)
        og_subory = ['og/bmbuilding.jpg'] + [self.w.og_cesta_realizacie(r) for r in self.w.realizacie
                                             if (r['obal'] or (r['galeria'][0] if r['galeria'] else {})).get('rel')
                                             == 'rd-ahoj-bratislava-nove-mesto/01.jpg']
        with Image.open(zdroj_og) as im:
            zw, zh = im.size
        cil = 480 / 630
        ow, oh = (zw, zw / cil) if zw / zh < cil else (zh * cil, zh)
        ox, oy, sk = (zw - ow) / 2, (zh - oh) / 2, 480 / ow
        for og in og_subory:
            with Image.open(self.ostry / og) as im:
                g = im.convert('L')
            self.assertEqual(g.size, (build.OG_W, build.OG_H), og)
            jemne = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(1.5)))

            def kresba_og(b, posun=0):
                x0, y0 = 720 + (b[0] * zw / pw_ - ox) * sk, ((b[1] + posun) * zh / ph_ - oy) * sk
                x1, y1 = 720 + (b[2] * zw / pw_ - ox) * sk, ((b[3] + posun) * zh / ph_ - oy) * sk
                return ImageStat.Stat(jemne.crop((round(x0), round(y0), round(x1), round(y1)))).mean[0]
            for b in plochy:
                pomer = kresba_og(b) / max(kresba_og(b, 110), 1e-6)
                self.assertGreater(pomer, 0.4, (og, b, round(pomer, 3)))
        # zdroj bez retuše ostáva zdrojom OG (iná fotka než RD Ahoj 01)
        ina = next(r['karta'] for r in self.w.realizacie if r['slug'] == 'bytovy-dom-rosnicka-bratislava')
        self.assertEqual(self.w.og_foto(ina), ina['zdroj'])
        self.assertIsNone(self.w.og_foto(None))

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

    def test_karty_s_malym_zdrojom_cely_ram(self):
        """Brána bmb-presnost pokus 1, nález 1 a doladenie-1: karta mriežky, služby a novinky s výrezom 4 : 5 pod
        KARTA_MIN_OREZ px predlohy (mala_karta) je od doladenia-1 tiež výrez 4 : 5 cez celý rám (celá fotka foto-cela
        v strede rámu na rozmazanom podklade pôsobila ako pokazená fotka, overenie 1, krása asi 6 z 10); najväčší súbor
        výrezu je výrez predlohy, nikdy väčší (mäkkosť pri hustote 2 a 3 je vedomá, ostrí ju len nová predloha).
        Karty služieb majú najprv fotky dosť veľké na celý rám (Vila X nie je na karte Stavebného manažmentu)."""
        self.assertEqual(build.KARTA_MIN_OREZ, 676)               # 2 x 422 / 1,25 = 675,2
        kariet = 0
        for stranka in ('realizacie', 'novinky', 'stavebny-dozor', 'stavebny-manazment', 'stavbyveduci'):
            s = (self.ostry_n / stranka / 'index.html').read_text(encoding='utf-8')
            for li in re.findall(r'<li class="(?:dlazdica|novinka-karta)[^"]*"[^>]*>.*?</li>', s, re.S):
                img = re.search(r'<img [^>]*>', li)
                m = re.search(r'src="[^"]*/(obrazky(?:-novinky)?)/([a-z0-9-]+)/(\d\d)-\d+-(\d\d)\.webp"', img.group(0)) if img else None
                if not m:
                    continue
                kariet += 1
                self.assertEqual(m.group(4), '45', (stranka, li[:120]))
                self.assertNotIn('foto-cela', li, stranka)
                self.assertNotIn('foto-podklad', li, stranka)
                self.assertIn(f'sizes="{build.SIZES_KARTA}"', img.group(0), stranka)
        self.assertGreater(kariet, 20)
        malych = 0
        for r in self.w_n.realizacie + self.w_n.novinky:
            f = self.w_n.foto_karty(r) if r in self.w_n.realizacie else r.get('karta')
            if not f:
                continue
            # karta_foto cely_ram: meria sa predloha zväčšená Upscaylom (ZMENY-KOLO-1.md 5.3), inak zdroj
            cw = build.orez_rozmer(*(f['zaklad'] if f.get('cely_ram') else (f['w'], f['h'])), (4, 5))[0]
            self.assertEqual(self.w_n.mala_karta(f), cw < build.KARTA_MIN_OREZ, f['rel'])
            if self.w_n.mala_karta(f):
                malych += 1
                sirky = self.w_n.sirky_variantu(f, 'webp', '45')
                self.assertLessEqual(sirky[-1], build.orez_rozmer(*f['zaklad'], (4, 5))[0], f['rel'])
        # 8 kariet s malým zdrojom: Vila X, Kalvária, Grunty, Devín, Devínske Luky, Sklenárska, dve novinky
        self.assertGreaterEqual(malych, 7)
        css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        self.assertNotIn('foto-cela', css)
        self.assertNotIn('--cap:', css)
        # služby: prvé karty majú fotku dosť veľkú na celý rám, Vila X je v zozname pod nimi
        man = (self.ostry / 'stavebny-manazment' / 'index.html').read_text(encoding='utf-8')
        karty = man.split('<ul class="karty">', 1)[1].split('</ul>', 1)[0]
        self.assertNotIn('realizacie/vila-x-bratislava/', karty)
        self.assertIn('realizacie/vila-x-bratislava/', man)

    def test_vyrez_karty_z_ramika(self):
        """karta_foto vyrez (brána bmb-presnost pokus 1, nález 9) ostáva v build.py; od 1. kola pripomienok ho používa
        len Lumière 08 (doladenie-2: výrez zo stredu celej fotky odrezal nápis KINO LUMIERE, test
        test_doladenie2_karta_lumiere_cely_napis), Hrnčiarovce majú reprezentatívnu fotku 01.jpg, ktorú vybral klient
        (ZMENY-KOLO-1.md 5.3). Dobrý rámik (výrez aspoň 768 px) nastaví vyrez45, zlý sa nepoužije a build varuje."""
        self.assertEqual([k for k, v in self.w.nastavenia['karta_foto'].items() if isinstance(v, dict) and v.get('vyrez')],
                         ['filmovy-klub-lumiere-spitalska-bratislava'])
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


# ---------------------------------------------------------------------------------------------------------------
class TestAuditNaplno(unittest.TestCase):
    """Audit naplno živého náhľadu 25. 11. 2026, časť 1 (texty a dáta): položky 2, 3, 5, 7, 10, 12, 14, 16, 20, 21,
    29, 30, 41 a 42 zoznamu audit-naplno/synteza/ZOZNAM.md (záznam ops/weby-na-mieru/klienti/bmbuilding/kolo-1/
    brana-3/AUDIT-OPRAVY.md). Nad skutočným obsahom, ostrý web aj náhľad; obsah/ sa nemení."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.nahlad, cls.w, cls.obsah = s['ostry'], s['nahlad'], s['web'], s['realizacie']
        cls.texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        cls.nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        cls.firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))

    def stranka(self, cesta, web=None):
        web = web or self.ostry
        return stranka_info(web / cesta / 'index.html' if cesta else web / 'index.html')

    @staticmethod
    def udaje(telo):
        """Údaje v hlave detailu (dl.fakty.udaje): popisok -> text hodnoty, pevné medzery ako medzery."""
        dl = telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]
        return {html.unescape(re.sub(r'<[^>]+>', '', k)): html.unescape(re.sub(r'<[^>]+>', '', v)).replace('\u00a0', ' ')
                for k, v in re.findall(r'<dt>(.*?)</dt><dd>(.*?)</dd>', dl, re.S)}

    def test_2_druh_stavby_a_roky_v_detaile(self):
        """Položka 2: detail má druh stavby z karty v jednotnom čísle (nie názov filtra „Rodinné domy“); Apartmánový
        dom v DRUHY_KARTY mení len Ventúrsku, Betliarska ostáva Bytový dom; bez druhu z názvu a textu (Okánikova, Dom
        pod lesom) kategória filtra pod popiskom Kategória. Popisok Rok pri jednom roku z tej istej položky je vrátený:
        popisok je vždy Roky v detaile aj v description, aj pri jednom roku a pri roku príspevku (Andrej 26. 11. 2026:
        „roky xxx tak ako predtým“, ROKY.md)."""
        ocak = {'rd-ahoj-bratislava-nove-mesto': ('Druh stavby', 'Rodinný dom'),
                'rd-kalvaria-bratislava': ('Druh stavby', 'Rodinný dom'),
                'rekonstrukcia-venturska-bratislava': ('Druh stavby', 'Apartmánový dom'),
                'dom-betliarska-bratislava-petrzalka': ('Druh stavby', 'Bytový dom'),
                'bytovy-dom-haanova-bratislava': ('Druh stavby', 'Bytový dom'),
                'polyfunkcny-objekt-budatinska-bratislava': ('Druh stavby', 'Polyfunkčný objekt'),
                'rekonstrukcia-okanikova-bratislava': ('Kategória', 'Bytové domy'),
                'dom-pod-lesom': ('Kategória', 'Rodinné domy')}
        for slug, (popisok, hodnota) in ocak.items():
            self.assertEqual(self.udaje(self.stranka(f'realizacie/{slug}/')['telo']).get(popisok), hodnota, slug)
        zoznam = self.stranka('realizacie/')['telo']
        for r in self.w.realizacie:
            info = self.stranka(f'realizacie/{r["slug"]}/')
            u = self.udaje(info['telo'])
            druh = self.w.druh_karty(r)
            # ten istý druh ako na karte zoznamu
            self.assertEqual(u.get('Druh stavby', ''), druh, r['slug'])
            karta = zoznam.split(f'realizacie/{r["slug"]}/"', 2)[2].split('</li>', 1)[0]
            m = re.search(r'<p class="projekt-udaje">(.*?)</p>', karta)
            udaje_karty = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))) if m else ''
            if druh:
                self.assertTrue(udaje_karty == druh or udaje_karty.startswith(druh + '\u00a0· '), (r['slug'], udaje_karty))
                self.assertNotIn('Kategória', u, r['slug'])
            roky = self.w.roky_karty(r)
            self.assertTrue(roky, r['slug'])
            self.assertEqual(u.get('Roky'), roky, r['slug'])
            self.assertNotIn('Rok', u, r['slug'])
            if roky in info['desc']:
                self.assertIn(f'Roky: {roky}.', info['desc'], r['slug'])
            self.assertNotIn('Rok:', info['desc'], r['slug'])
        ufo = self.stranka('realizacie/ufo-most-snp-bratislava/')
        self.assertEqual(self.udaje(ufo['telo']).get('Roky'), '2025')
        self.assertTrue(ufo['desc'].endswith('Roky: 2025.'), ufo['desc'])
        self.assertEqual(self.udaje(self.stranka('realizacie/rd-kalvaria-bratislava/')['telo']).get('Roky'), '2009')
        # Iné (most, kino, SFÚ) bez druhu aj bez kategórie
        for k in ('Druh stavby', 'Kategória'):
            self.assertNotIn(k, self.udaje(ufo['telo']))
        nazvy = [n for n, _ in build.DRUHY_KARTY]
        self.assertLess(nazvy.index('Apartmánový dom'), nazvy.index('Bytový dom'))
        # funkcia popisok_rokov (Rok pri jednom roku) je preč, popisok rokov je jeden
        self.assertFalse(hasattr(build.Web, 'popisok_rokov'))

    def test_3_10_otazky_zo_zakona_v_oboch_zneniach(self):
        """Položky 3 a 10: odpoveď „Čo robí stavebný dozor?“ platí pre znenie § 36 ods. 1 do 31. 12. 2026 („je
        oprávnená najmä“) aj od 1. 1. 2027 („je povinná najmä“, novela 243/2026 Z. z.), slová podľa písm. a), c), e),
        h); zdroj vedie na stránku predpisu bez dátumu časovej verzie; rozdiel s výnimkou § 34 ods. 4 písm. b)."""
        z = self.texty['zakon']
        self.assertEqual(z['url'], 'https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2025/25/')
        self.assertNotRegex(z['url'], r'\d{8}|\.pdf')
        self.assertEqual(z['nazov'], 'Slov-Lex, zákon č. 25/2025 Z. z. (stavebný zákon) v znení neskorších predpisov')
        q = self.texty['otazky']['co-robi-dozor']['odpoved']
        for kus in ('najmä sleduje postup stavebných prác z hľadiska súladu s overeným projektom stavby a technickými predpismi',
                    'kontroluje vhodnosť stavebných výrobkov na zabudovanie do stavby',
                    'bezodkladne informuje stavebníka o všetkých závažných okolnostiach priebehu výstavby',
                    'kontroluje vedenie stavebného denníka'):
            self.assertIn(kus, q)
        r = self.texty['otazky']['rozdiel']
        self.assertNotIn('Oboje', r['odpoved'])
        self.assertIn('Stavebný dozor aj vedenie uskutočňovania stavebných prác sú podľa zákona vyhradené činnosti', r['odpoved'])
        self.assertIn('Výkon stavebného dozoru pri stavebných úpravách, drobných stavbách a stavbách, na ktoré sa '
                      'vyžaduje ohlásenie, sa však za vyhradenú činnosť nepovažuje.', r['odpoved'])
        self.assertTrue(r['zdroj'].endswith('§ 34 ods. 2 písm. b) a c) a ods. 4 písm. b)'), r['zdroj'])
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            self.assertNotIn('ZZ_2025_25_', s, subor)
            self.assertNotIn('oprávnená najmä', s.replace('\u00a0', ' '), subor)
        for cesta in ('', 'stavebny-dozor/', 'stavbyveduci/'):
            telo = self.stranka(cesta)['telo']
            zdroje = re.findall(r'<p class="zdroj">Zdroj: <a href="([^"]+)">(.*?)</a></p>', telo)
            self.assertTrue(zdroje, cesta)
            for url, text in zdroje:
                self.assertEqual(url, z['url'], cesta)
                self.assertIn('v znení neskorších predpisov', html.unescape(text).replace('\u00a0', ' '), cesta)

    def test_5_obchodny_register_bez_datumov(self):
        """Položka 5: veta Obchodný register na /opravnenia/ bez dátumov zápisu činností (pri starších stavbách
        s dozorom pôsobili ako rozpor), zdroj ORSR ostáva; roly realizácií sa nemenia."""
        veta = self.texty['opravnenia']['register']
        self.assertEqual(veta, 'V obchodnom registri má spoločnosť zapísaný výkon činnosti stavbyvedúceho aj stavebného dozoru.')
        li = self.stranka('opravnenia/')['telo'].split('<ul class="cennik register">', 1)[1].split('</ul>', 1)[0]
        self.assertIn(f'<p class="sluzba-opis">{veta}</p>', li.replace('\u00a0', ' '))
        self.assertNotRegex(li, r'2005|2012')
        self.assertIn('Zdroj: <a href="https://www.orsr.sk/vypis.asp?', li)

    def test_7_llms_txt_a_roboti_ai_v_ostrom_behu(self):
        """Položka 7 (ops/runbook.md, checklist zverejnenia): ostrý robots.txt menuje GPTBot, ClaudeBot, PerplexityBot,
        Google-Extended a Bingbot v jednej skupine s Allow (RFC 9309 2.1) pred skupinou pre všetkých; ostrý web má
        llms.txt (llmstxt.org) a každý jeho odkaz má stránku; náhľad nemá ani jedno."""
        robots = (self.ostry / 'robots.txt').read_text(encoding='utf-8')
        skupiny = [g.splitlines() for g in robots.strip().split('\n\n')]
        self.assertEqual(skupiny, [[f'User-agent: {b}' for b in ('GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'Bingbot')]
                                   + ['Allow: /'], ['User-agent: *', 'Allow: /'], ['Sitemap: https://bmbuilding.sk/sitemap.xml']])
        self.assertEqual((self.nahlad / 'robots.txt').read_text(encoding='utf-8'), 'User-agent: *\nDisallow: /\n')
        self.assertFalse((self.nahlad / 'llms.txt').exists())
        llms = (self.ostry / 'llms.txt').read_text(encoding='utf-8')
        riadky = llms.splitlines()
        self.assertEqual(riadky[0], f'# {self.firma["meno"]}')
        self.assertEqual(riadky[2], '> ' + self.texty['uvod']['description'])
        for nadpis in ('## Služby', '## Realizácie', '## Firma'):
            self.assertIn(nadpis, riadky)
        odkazy = re.findall(r'^- \[(.+?)\]\((\S+?)\)(?:: (.+))?$', llms, re.M)
        ciele = [u for _, u, _ in odkazy]
        self.assertEqual(len(ciele), len(set(ciele)))
        # každý odkaz v llms.txt má stránku ostrého webu
        for _, u, _ in odkazy:
            self.assertTrue(u.startswith('https://bmbuilding.sk/'), u)
            self.assertTrue((self.ostry / u[len('https://bmbuilding.sk/'):] / 'index.html').is_file(), u)
        ocak = ([f'https://bmbuilding.sk/{s}/' for s in self.texty['sluzby']]
                + [f'https://bmbuilding.sk/realizacie/{r["slug"]}/' for r in self.w.realizacie]
                + [f'https://bmbuilding.sk/{c}' for c in ('realizacie/', 'opravnenia/', 'kontakt/', 'ochrana-udajov/')])
        self.assertEqual(sorted(ciele), sorted(ocak))
        # údaje firmy z firma.json, popis realizácie z jej údajov (rola zo zápisu na webe, Roky aj pri jednom roku)
        for kus in (self.firma['telefon'], self.firma['email'], self.firma['ico'], self.firma['ic_dph'],
                    'Podhorská 9, 902 01 Pezinok', self.firma['zapis_or'], 'stavbyvedúci, vydané 5. 11. 2003'):
            self.assertIn(kus, llms)
        self.assertIn('(https://bmbuilding.sk/realizacie/ufo-most-snp-bratislava/): Rola BM Building: projektový manažment '
                      'a stavebný dozor.', llms)
        self.assertIn('Roky: 2025.', llms)
        self.assertNotIn('Rok:', llms)
        # realizácia bez rokov stavby má v llms.txt rok príspevku bez slova príspevok (ROKY.md)
        ahoj = [x for x in riadky if '(https://bmbuilding.sk/realizacie/rd-ahoj-bratislava-nove-mesto/)' in x]
        self.assertEqual(len(ahoj), 1)
        self.assertTrue(ahoj[0].endswith('Roky: 2026.'), ahoj[0])
        for zle in ('!', '\u2013', '\u2014', '\u00a0', 'manažér a stavebný dozor'):
            self.assertNotIn(zle, llms)
        skryte = set(self.obsah_vsetky()) - {r['slug'] for r in self.w.realizacie}
        self.assertTrue(skryte)
        for slug in skryte:
            self.assertNotIn(slug, llms)

    @staticmethod
    def obsah_vsetky():
        return [p.stem for p in (KOREN / 'obsah' / 'realizacie').glob('*.json')]

    def test_7_llms_txt_escapuje_udaje_z_obsahu(self):
        """Položka 7, vkladanie (AGENTS.md bod 12b): údaje z obsahu idú do markdownu llms.txt s escapovaním znakov
        odkazu a HTML; jediné ciele odkazov sú stránky webu."""
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-llms-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        obsah = maly_obsah(tmp)
        p = obsah / 'realizacie' / 'plna.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d['nazov'] = 'Bytový dom [Skúška](javascript:alert(9)) `kód`'
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        kod, out, err = postav(tmp / 'web', obsah)
        self.assertEqual(kod, 0, err)
        llms = (tmp / 'web' / 'llms.txt').read_text(encoding='utf-8')
        self.assertIn('- [Bytový dom \\[Skúška\\](javascript:alert(9)) \\`kód\\`, Bratislava](https://bmbuilding.sk/realizacie/plna/)', llms)
        self.assertIn('\\<script\\>alert(1)\\</script\\>', llms)
        self.assertNotIn('<script>', llms)
        ciele = re.findall(r'(?<!\\)\]\(([^)]*)\)', llms)
        self.assertTrue(ciele)
        for u in ciele:
            self.assertTrue(u.startswith('https://bmbuilding.sk/'), u)
        # náhľad toho istého obsahu llms.txt nemá (ani po ostrom behu v tom istom priečinku)
        self.assertEqual(postav(tmp / 'web', obsah, nahlad=True)[0], 0)
        self.assertFalse((tmp / 'web' / 'llms.txt').exists())

    def test_12_og_obrazok_nahladu_z_adresy_nahladu(self):
        """Položka 12: v náhľade og:image a twitter:image z adresy náhľadu (odkaz vo WhatsAppe s obrázkom), súbor
        v náhľade je; canonical a og:url ostávajú na bmbuilding.sk, ostrý web má og:image na doméne."""
        adresa = self.nast['adresa_nahladu']
        self.assertEqual(adresa, 'https://arling.sk/bmbuilding/')
        for cesta in ('', 'realizacie/rd-pezinok/', 'stavebny-dozor/', 'kontakt/'):
            n = self.stranka(cesta, self.nahlad)['html']
            o = self.stranka(cesta)['html']
            og_n = re.search(r'<meta property="og:image" content="([^"]+)">', n).group(1)
            self.assertEqual(re.search(r'<meta name="twitter:image" content="([^"]+)">', n).group(1), og_n)
            self.assertTrue(og_n.startswith(adresa + 'og/'), og_n)
            self.assertTrue((self.nahlad / og_n[len(adresa):]).is_file(), og_n)
            canon = 'https://bmbuilding.sk/' + cesta
            for web in (n, o):
                self.assertIn(f'<link rel="canonical" href="{canon}">', web)
                self.assertIn(f'<meta property="og:url" content="{canon}">', web)
            self.assertIn(f'<meta property="og:image" content="https://bmbuilding.sk/{og_n[len(adresa):]}">', o)
            self.assertIn(f'<meta name="twitter:image" content="https://bmbuilding.sk/{og_n[len(adresa):]}">', o)
        self.assertIn(f'<meta property="og:image" content="{adresa}og/bmbuilding.jpg">',
                      (self.nahlad / '404.html').read_text(encoding='utf-8'))
        # JSON-LD aj v náhľade na doméne
        for u in self.stranka('', self.nahlad)['ld']:
            for k in ('url', 'logo', 'image'):
                if isinstance(u.get(k), str):
                    self.assertTrue(u[k].startswith('https://bmbuilding.sk/'), (k, u[k]))
        # bez adresy náhľadu alebo s adresou bez https ide obrázok z domény
        stare = (self.w.nahlad, self.w.adresa_nahladu)
        try:
            self.w.nahlad = True
            self.assertEqual(self.w.og_abs_url('og/x.jpg'), 'https://arling.sk/bmbuilding/og/x.jpg')
            self.w.adresa_nahladu = build.bezpecna_url('http://arling.sk/bmbuilding/').rstrip('/')
            self.assertEqual(self.w.og_abs_url('og/x.jpg'), 'https://bmbuilding.sk/og/x.jpg')
            self.w.nahlad = False
            self.w.adresa_nahladu = stare[1]
            self.assertEqual(self.w.og_abs_url('og/x.jpg'), 'https://bmbuilding.sk/og/x.jpg')
        finally:
            self.w.nahlad, self.w.adresa_nahladu = stare

    def test_14_jednotny_zapis_roly(self):
        """Položka 14: „Projektový manažér a stavebný dozor“ (Vtáčnik, Devínske lúky, Na Križovatkách, Okánikova)
        má na webe zápis „Projektový manažment a stavebný dozor“ cez nastavenia.json rola_zapis; obsah sa nemení,
        počty realizácií služieb ostávajú, text realizácie („pôsobil ako projektový manažér“) tiež."""
        stara, nova = 'Projektový manažér a stavebný dozor', 'Projektový manažment a stavebný dozor'
        self.assertEqual(self.nast['rola_zapis'], {stara: nova})
        povodne = sorted(s for s, d in self.obsah.items() if d.get('rola') == stara)
        self.assertEqual(povodne, ['bytove-domy-vtacnik-bratislava', 'bytovy-dom-devinske-luky-bratislava',
                                   'bytovy-dom-na-krizovatkach-bratislava', 'rekonstrukcia-okanikova-bratislava'])
        zoznam = self.stranka('realizacie/')['telo'].replace('\u00a0', ' ')
        for slug in povodne:
            r = next(x for x in self.w.realizacie if x['slug'] == slug)
            self.assertEqual(r['rola'], nova, slug)
            self.assertTrue(self.udaje(self.stranka(f'realizacie/{slug}/')['telo'])['Rola BM Building'].startswith(nova), slug)
            karta = zoznam.split(f'realizacie/{slug}/"', 2)[2].split('</li>', 1)[0]
            self.assertIn(f'<p class="projekt-text">{nova}</p>', karta, slug)
        for subor in stranky(self.ostry):
            t = subor.read_text(encoding='utf-8').replace('\u00a0', ' ')
            # jediný výskyt starého zápisu je veta textu realizácie Okánikova (obsah, nie pole rola)
            self.assertEqual(t.count('manažér a stavebný dozor'), t.count('pôsobil ako projektový manažér a stavebný dozor'), subor)
        for slug, t in self.texty['sluzby'].items():
            n = sum(1 for d in self.obsah.values() if any(k in (d.get('rola') or '').lower() for k in t['kluce']))
            self.assertEqual(len(self.w.realizacie_sluzby(slug)), n, slug)

    def test_14_21_zapis_v_nastaveniach_bez_padu(self):
        """Položky 14 a 21: zlé položky rola_zapis, mena_zapis a text_zapis sú varovanie, nie pád; kus textu, ktorý
        v realizácii nie je, je varovanie; chýbajúca realizácia sa preskočí; nahradí sa aj alt fotky."""
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-zapis-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        obsah = maly_obsah(tmp)
        sablona = sablona_s_nastaveniami(
            tmp, rola_zapis={'Stavebný dozor': 'Stavebný dozor a TDI', '_poznamka': 'x', 'Zlá': 5},
            mena_zapis='nie je objekt',
            text_zapis={'plna': [['Skúška', 'skúška'], ['Nie je v texte', 'x'], 'zlé'], 'nie-je-v-obsahu': [['a', 'b']]})
        err = io.StringIO()
        with mock.patch.object(build, 'SABLONA', sablona), redirect_stderr(err):
            w = build.Web(obsah, False)
        plna = next(r for r in w.realizacie if r['slug'] == 'plna')
        self.assertEqual(plna['rola'], 'Stavebný dozor a TDI')
        self.assertEqual(plna['nazov'], 'Bytový dom skúška')
        self.assertEqual([f['alt'] for f in plna['fotky']], ['Pohľad z ulice', 'Bytový dom skúška, Bratislava, Skúšobná ulica'])
        e = err.getvalue()
        self.assertIn("nastavenia rola_zapis: 'Zlá' -> 5 nie je dvojica textov", e)
        self.assertIn('nastavenia mena_zapis nie je objekt', e)
        self.assertIn("nastavenia text_zapis: 'Nie je v texte' v realizácii plna nie je", e)
        self.assertIn("nastavenia text_zapis: plna 'zlé' nie je dvojica", e)
        self.assertNotIn('nie-je-v-obsahu', e)

    def test_16_pevne_medzery_roky_a_pocet(self):
        """Položka 16: „2021 až 2022“ a „8 fotiek“ sa nezalomia (pevné medzery v sadzba a tvar); zoznam na
        /stavebny-dozor/, údaje a text detailov bez rozsahu rokov s obyčajnou medzerou."""
        self.assertEqual(build.sadzba('2021 až 2022'), '2021\u00a0až\u00a02022')
        self.assertEqual(build.sadzba('v rokoch 2018 až 2021.'), 'v\u00a0rokoch 2018\u00a0až\u00a02021.')
        self.assertEqual(build.sadzba('2 až 6 obytných'), '2\u00a0až\u00a06 obytných')
        self.assertEqual(build.sadzba('až 2022'), 'až 2022')
        self.assertEqual([build.tvar(n, 'fotka', 'fotky', 'fotiek') for n in (1, 3, 8)],
                         ['1\u00a0fotka', '3\u00a0fotky', '8\u00a0fotiek'])
        for s in self.texty['sluzby']:
            telo = self.stranka(f'{s}/')['telo']
            if '<ul class="zoznam-dalsie">' in telo:
                zoznam = telo.split('<ul class="zoznam-dalsie">', 1)[1].split('</ul>', 1)[0]
                self.assertNotRegex(zoznam, r'\d až \d', s)
                self.assertRegex(zoznam, '\\d\u00a0až\u00a0\\d', s)
        for r in self.w.realizacie:
            telo = self.stranka(f'realizacie/{r["slug"]}/')['telo']
            kusy = [telo.split(z, 1)[1].split(k, 1)[0] for z, k in (('<dl class="fakty udaje">', '</dl>'),
                                                                   ('<div class="detail-text atelier-text">', '</div>'))
                    if z in telo]
            self.assertTrue(kusy, r['slug'])
            for kus in kusy:
                self.assertNotRegex(re.sub(r'<[^>]+>', '', kus), r'\d až \d', r['slug'])
            m = re.search(r'<a href="#galeria">(.*?)</a>', telo)
            if m:
                self.assertRegex(m.group(1), '^Galéria, \\d+ fot', r['slug'])

    def test_20_vseobecna_vyzva_pre_tri_sluzby(self):
        """Položka 20: všeobecná výzva v Kontakte (úvod, /realizacie/, /opravnenia/, /ochrana-udajov/) menuje všetky
        tri služby; stránky služieb majú vlastnú výzvu."""
        v = self.texty['vyzvy']['zaver']
        self.assertEqual(v, 'Potrebujete stavebný manažment, stavebný dozor alebo stavbyvedúceho? Ozvite sa.')

        def kontakt(cesta):
            s = self.stranka(cesta)['html'].replace('\u00a0', ' ')
            return s.split('<section class="sekcia kontakt" id="kontakt"', 1)[1].split('</section>', 1)[0]
        for cesta in ('', 'realizacie/', 'opravnenia/', 'ochrana-udajov/'):
            self.assertIn(f'<p class="uvodny">{v} {self.texty["kontakt"]["posobnost"]}</p>', kontakt(cesta), cesta)
        for s, t in self.texty['sluzby'].items():
            k = kontakt(f'{s}/')
            self.assertIn(t['zaver'], k, s)
            self.assertNotIn(v, k, s)

    def test_21_mena_firiem_a_pravopis(self):
        """Položka 21: mená firiem podľa ORSR (HANT BA, a.s.; DARK BLUE STUDIO), pracovisko SFÚ s malým d v názve,
        texte a popisoch fotiek; obsah sa nemení, pomenovanie Lumière ostáva."""
        self.assertEqual(self.nast['mena_zapis'], {'HANT BA': 'HANT BA, a.s.'})
        self.assertEqual(self.obsah['bytove-domy-vtacnik-bratislava']['zhotovitel'], 'HANT BA')
        vt = self.udaje(self.stranka('realizacie/bytove-domy-vtacnik-bratislava/')['telo'])
        self.assertTrue(vt['Zhotoviteľ'].startswith('HANT BA, a.s.'), vt['Zhotoviteľ'])
        sfu = 'digitalizacne-pracovisko-sfu-spitalska-bratislava'
        self.assertTrue(self.obsah[sfu]['nazov'].startswith('Stavebné úpravy Digitalizačného'))
        info = self.stranka(f'realizacie/{sfu}/')
        self.assertTrue(info['h1'].startswith('Stavebné úpravy digitalizačného pracoviska SFÚ'), info['h1'])
        self.assertIn('Stavebné úpravy digitalizačného pracoviska Slovenského filmového ústavu', info['telo'])
        r = next(x for x in self.w.realizacie if x['slug'] == sfu)
        self.assertTrue(r['fotky'])
        self.assertTrue(all(f['alt'].startswith('Stavebné úpravy digitalizačného pracoviska SFÚ') for f in r['fotky']))
        for subor in stranky(self.ostry) + [self.ostry / 'llms.txt']:
            t = subor.read_text(encoding='utf-8')
            self.assertNotIn('Digitalizačného', t, subor)
            self.assertNotRegex(t, r'HANT BA(?!, a\.s\.)', subor)
            # jediná výnimka: alt (a data-alt lightboxu) obrázka tabuľky ASB prepisuje obrázok, na ňom je „DARK BLUE
            # studio“ (oprava po overení, kontrola zadania nález drobné)
            bez_altu = re.sub(r'(?:data-)?alt="Tabuľka údajov o stavbe z časopisu ASB[^"]*"', '', t)
            self.assertNotIn('DARK BLUE studio', bez_altu, subor)
        lum = self.stranka('realizacie/filmovy-klub-lumiere-spitalska-bratislava/')
        self.assertIn('Filmového klubu Lumière', lum['h1'])
        self.assertTrue(self.udaje(lum['telo'])['Projektant'].startswith('DARK BLUE STUDIO'))
        # alt obrázka tabuľky ASB podľa obrázka, údaje lightboxu (pole Projektant) podľa ORSR
        self.assertRegex(lum['telo'], r'alt="Tabuľka údajov o stavbe z časopisu ASB[^"]*projektant DARK BLUE studio, architekti')
        self.assertIn('projektant DARK BLUE STUDIO, Ing. arch.', lum['telo'])

    def test_29_stavbyveduci_odkaz_vsetky_realizacie(self):
        """Položka 29: /stavbyveduci/ (žiadna rola v obsahu ho nemá) má pod textom odkaz Všetky realizácie ako
        ostatné služby; každá stránka služby ho má práve raz."""
        self.assertEqual(self.w.realizacie_sluzby('stavbyveduci'), [])
        telo = self.stranka('stavbyveduci/')['telo']
        hlavne = telo.split('<div class="sluzba-hlavne', 1)[1].split('<aside', 1)[0]
        self.assertRegex(hlavne, r'</ul><p class="viac"><a class="odkaz-sipka" href="\.\./realizacie/">Všetky realizácie <svg')
        for s in self.texty['sluzby']:
            self.assertEqual(self.stranka(f'{s}/')['telo'].count('>Všetky realizácie <svg'), 1, s)

    def test_30_pocet_realizacii_so_sluzbou_v_mene_odkazu(self):
        """Položka 30: odkazy „13 realizácií“ a „23 realizácií“ v Činnosti majú pre čítačku aj službu (span.vh),
        viditeľný text sa nemení."""
        cin = self.stranka('')['telo'].split('id="cinnost"', 1)[1].split('</section>', 1)[0]
        odkazy = re.findall(r'<a class="cinnost-pocet" href="([a-z-]+)/#real-nadpis">(.*?)</a>', cin)
        self.assertEqual([s for s, _ in odkazy], [s for s in self.w.poradie_sluzieb() if self.w.realizacie_sluzby(s)])
        for slug, a in odkazy:
            meno = html.unescape(re.sub(r'<[^>]+>', '', a)).replace('\u00a0', ' ')
            self.assertEqual(meno, f'{len(self.w.realizacie_sluzby(slug))} {self.w.slovo_realizacie(len(self.w.realizacie_sluzby(slug)))}: '
                                   f'{build.male(self.texty["sluzby"][slug]["menu"])}')
            self.assertEqual(re.sub(r'<span class="vh">.*?</span>', '', a).count('<span'), 2)

    def test_41_ico_v_json_ld_firmy(self):
        """Položka 41: JSON-LD firmy má IČO ako identifier (PropertyValue, https://schema.org/identifier)."""
        ico = re.sub(r'\s+', '', self.firma['ico'])
        self.assertEqual(ico, '35947519')
        for cesta in ('', 'kontakt/', 'realizacie/rd-pezinok/'):
            f = next(u for u in self.stranka(cesta)['ld'] if str(u.get('@id', '')).endswith('#firma'))
            self.assertEqual(f['identifier'], {'@type': 'PropertyValue', 'propertyID': 'IČO', 'value': ico}, cesta)
            self.assertEqual(f['vatID'], self.firma['ic_dph'])

    def test_42_zostava_json_nejde_na_web(self):
        """Položka 42: workflow Obsah zmaže web/.zostava.json (zoznam súborov pre ďalší beh build.py) po zostavení
        a pred nahratím artefaktu GitHub Pages."""
        yml = (KOREN / '.github' / 'workflows' / 'obsah.yml').read_text(encoding='utf-8')
        self.assertEqual(build.MANIFEST, '.zostava.json')
        krok = f'run: rm -f web/{build.MANIFEST}'
        self.assertEqual(yml.count(krok), 1)
        self.assertLess(yml.index('run: python build.py'), yml.index(krok))
        self.assertLess(yml.index(krok), yml.index('actions/upload-pages-artifact@'))
        self.assertTrue((self.ostry / build.MANIFEST).is_file())


def node_simulacia(test, kod_appjs, sim):
    """Spustí kus app.js v node nad falošným DOM (sim dostane kód ako JSON na stdin) a vráti výstup JSON."""
    import subprocess
    node = shutil.which('node')
    if not node:
        test.skipTest('node nie je k dispozícii')
    p = subprocess.run([node, '-e', sim], input=json.dumps(kod_appjs), capture_output=True, text=True, encoding='utf-8',
                       timeout=60)
    test.assertEqual(p.returncode, 0, p.stderr)
    return json.loads(p.stdout)


def cast_appjs(od, po):
    js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
    assert od in js and po in js, (od, po)
    return od + js.split(od, 1)[1].split(po, 1)[0]


class TestAuditRozhranie(unittest.TestCase):
    """Audit naplno živého náhľadu 25. 11. 2026, časť 2 (rozhranie): položky 4, 6, 9, 11, 13, 15, 17, 18, 19, 23, 24,
    25, 26, 27, 32, 33, 34, 35, 36, 37, 38, 39 a 40 zoznamu audit-naplno/synteza/ZOZNAM.md a dva drobné nálezy kontroly
    širokého rozloženia (Ďalšie realizácie a osamelé slovo v boxe s dokladom od 1921 px). Záznam a merania v Chrome:
    ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/AUDIT-OPRAVY.md, časť 2."""

    # šírky textu v Chrome 158 (Geist, merané 25. 11. 2026 cez canvas measureText v prehliadači, rozhranie/funkcie.mjs):
    # najdlhšie voľby roliet pri písme 14 px a texty tlačidla Menu pri 15 px. Nová voľba s dlhším textom sa musí premerať.
    # „2020 až 2026“ (roky príspevku RD Ahoj a Vila Holubyho vo filtri) premerané 26. 11. 2026 tým istým spôsobom pri
    # 390 px, ostatné voľby vtedy vyšli rovnako ako 25. 11. („2020 až 2025“ 93,5; ROKY.md, roky/snimky.mjs)
    VOLBY_PX = {'všetky': 42.3, '2020 až 2026': 93.0, '2012 až 2018': 85.3, '2004 až 2009': 93.7, 'rok neuvedený': 93.8,
                'Bratislava (22)': 91.3, 'Ivanka pri Dunaji (1)': 122.2, 'Nitrianske Hrnčiarovce (1)': 161.4,
                'Pezinok (1)': 67.8, 'najnovšie': 61.0, 'najstaršie': 62.8, 'abecedne': 63.0}
    ZAVRIET_PX = 49.9
    # hlavička od 1024 px (Chrome, rozhranie/prieskum.mjs): logo najviac 229,4 px (výška 48 px), menu 356,3 px,
    # Zavolať s číslom 227,9 px, Menu 84 px
    LOGO_PX, MENU_PX, TEL_PX, MENU_M_PX = 229.4, 356.3, 227.9, 84.0

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.nahlad, cls.w = s['ostry'], s['nahlad'], s['web']
        cls.css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        cls.zaklad = css_bez_blokov(cls.css)
        cls.firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    def blok(self, hlavicka):
        return ''.join(css_bloky(self.css, hlavicka))

    def test_4_roletky_v_dvoch_stlpcoch_a_poradie_fokusu(self):
        """Položka 4: do 767 px roletky v dvoch stĺpcoch, Miesto cez celý riadok ako prvé (v DOM aj na obrazovke, fokus
        ide podľa obrazovky), pod ním Roky a Zoradiť; pod 320 px pod sebou. Každá voľba sa zmestí pri 320, 360, 390 a
        411 px (šírky textu z Chrome, okraje roletky 4 px, rámik 1 px, šípka 24 px); v troch stĺpcoch sa orezávala."""
        telo = self.info('realizacie/')['telo']
        vyber = telo.split('<div class="filtre-vyber">', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'<label class="([^"]+)">(\w+)', vyber),
                         [('vyber vyber-miesto', 'Miesto'), ('vyber', 'Roky'), ('vyber', 'Zoradiť')])
        mobil = self.blok('@media (max-width:767px)')
        self.assertIn('.filtre-vyber{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;width:100%}', mobil)
        self.assertIn('.vyber-miesto{grid-column:1/-1}', mobil)
        self.assertIn('.vyber select{width:100%;font-size:14px;padding:0 4px}', mobil)
        self.assertIn('.filtre-vyber{grid-template-columns:minmax(0,1fr)}', self.blok('@media (max-width:319px)'))
        # voľby z HTML sú zmerané; žiadna sa neoreže
        volby = {sel: [html.unescape(x) for x in re.findall(r'<option value="[^"]*">([^<]+)</option>',
                                                              telo.split(f'<select {sel}>', 1)[1].split('</select>', 1)[0])]
                 for sel in ('data-mesto', 'data-roky', 'data-zoradit')}
        # nová voľba (nové mesto alebo desaťročie) nemá šírku z Chrome: test spadne, treba ju premerať (funkcie.mjs)
        self.assertEqual({t for x in volby.values() for t in x} - set(self.VOLBY_PX), set())
        dlzky = {sel: [self.VOLBY_PX[t] for t in texty] for sel, texty in volby.items()}
        for vw in (320, 360, 390, 411):
            obsah = vw - 32
            stlpec = (obsah - 8) / 2
            for sel, sirka in (('data-mesto', obsah), ('data-roky', stlpec), ('data-zoradit', stlpec)):
                miesto = sirka - 2 * 4 - 2 - 24
                self.assertLessEqual(max(dlzky[sel]), miesto, (vw, sel))
        # pred opravou (tri stĺpce) by sa pri 360 px orezalo
        self.assertGreater(self.VOLBY_PX['rok neuvedený'], (328 - 16) / 3 - 34)

    def test_6_fokus_odkryje_skryty_blok(self):
        """Položka 6: fokus klávesnice v bloku .vstup, ktorý pozorovateľ ešte neodkryl (spodných 8 % okna), blok odkryje
        hneď (focusin) a pozorovateľ ho prestane sledovať; fokus mimo .vstup nič nemení. Simulácia časti 0b app.js
        (Chrome pri 1440 x 900: zastávka Oprávnenia mala nepriehľadnosť 0, po oprave 1). Od položky 48 (časť 3) dáva
        polohu pri načítaní prvé hlásenie pozorovateľa ioPoloha (boundingClientRect a rootBounds), skript nečíta
        innerHeight ani getBoundingClientRect (prvky v simulácii ho nemajú, čítania okna sa rátajú)."""
        kod = cast_appjs('// 0b. Sekcie pod hrdinom', '// 1. Mobilné menu')
        self.assertIn("d.addEventListener('focusin'", kod)
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function triedy() { const s = new Set(); return { add: (x) => s.add(x), remove: (x) => s.delete(x), contains: (x) => s.has(x), list: () => [...s] }; }
const prvky = [100, 850, 900, 1500, 2000].map((top, i) => ({ i, top, classList: triedy() }));
const pozorovatelia = [];
class IO { constructor(fn, opt) { this.fn = fn; this.opt = opt || {}; this.sledovane = new Set(); this.zrusene = []; pozorovatelia.push(this); }
  observe(e) { this.sledovane.add(e.i); } unobserve(e) { this.sledovane.delete(e.i); this.zrusene.push(e.i); } }
const posluch = {};
let citania = 0;
const win = {};
Object.defineProperty(win, 'innerHeight', { get() { citania++; return 900; } });
const d = { querySelector: (s) => (s === '.stranka-uvod' ? {} : null), addEventListener: (t, f) => { posluch[t] = f; },
  documentElement: { get clientHeight() { citania++; return 900; } } };
const ctx = { d, pohyb: true, maIO: true, window: win, IntersectionObserver: IO, vsetky: () => prvky };
vm.createContext(ctx);
vm.runInContext(kod, ctx);
const vstup = pozorovatelia.find((o) => o.opt.rootMargin);
const poloha = pozorovatelia.find((o) => !o.opt.rootMargin);
const v = { pozorovatelov: pozorovatelia.length, poloha_sleduje: [...poloha.sledovane].sort(),
  vstup_pred_hlasenim: prvky.map((p) => p.classList.contains('vstup')) };
// prvé hlásenie polohy po rozložení prehliadača (okno 900 px): horný okraj na spodku okna alebo nižšie sa skryje
poloha.fn(prvky.map((p) => ({ target: p, isIntersecting: p.top < 900, boundingClientRect: { top: p.top }, rootBounds: { top: 0, bottom: 900 } })));
v.vstup = prvky.map((p) => p.classList.contains('vstup'));
v.sledovane = [...vstup.sledovane].sort();
v.poloha_po = [...poloha.sledovane];
const bez = { i: 9, classList: triedy() };
poloha.fn([{ target: bez, isIntersecting: false, boundingClientRect: { top: 5000 }, rootBounds: null }]);
v.bez_rootbounds = bez.classList.contains('vstup');
vstup.fn([{ target: prvky[3], isIntersecting: true }, { target: prvky[4], isIntersecting: false }]);
v.videne = prvky.map((p) => p.classList.contains('videne'));
const odkaz = (p) => ({ closest: (s) => (s === '.vstup:not(.videne)' && p && p.classList.contains('vstup') && !p.classList.contains('videne') ? p : null) });
posluch.focusin({ target: odkaz(prvky[2]) });
v.po_fokuse = prvky.map((p) => p.classList.contains('videne'));
v.zrusene = vstup.zrusene.slice();
posluch.focusin({ target: odkaz(null) });
posluch.focusin({ target: odkaz(prvky[2]) });
v.zrusene_znova = vstup.zrusene.slice();
posluch.focusin({ target: {} });
v.citania = citania;
console.log(JSON.stringify(v));
"""
        v = node_simulacia(self, kod, sim)
        self.assertEqual(v, {'pozorovatelov': 2, 'poloha_sleduje': [0, 1, 2, 3, 4],
                             'vstup_pred_hlasenim': [False] * 5, 'vstup': [False, False, True, True, True],
                             'sledovane': [2, 3, 4], 'poloha_po': [], 'bez_rootbounds': False,
                             'videne': [False, False, False, True, False], 'po_fokuse': [False, False, True, True, False],
                             'zrusene': [3, 2], 'zrusene_znova': [3, 2], 'citania': 0})

    def test_9_odkaz_so_sipkou_na_osi(self):
        """Položka 9: odkaz so šípkou bez ľavého vnútorného okraja (text na osi nadpisu, na 404 na hrane tlačidla),
        obrys fokusu 3 px pred ním; pravý okraj 4 px ostáva. Chrome: text 4 px vpravo od osi pri 390, 1366 a 1440 px,
        po oprave 0 px na úvode, /stavebny-dozor/ aj 404."""
        d = deklaracie(self.zaklad, '.odkaz-sipka')
        self.assertEqual(d.get('padding'), '0 4px 0 0')
        self.assertNotRegex(self.css, r'\.odkaz-sipka\{[^}]*padding-left')
        self.assertEqual(deklaracie(self.zaklad, ':focus-visible').get('outline-offset'), '3px')
        for cesta, kus in (('', 'Stránka služby'), ('stavebny-dozor/', 'Všetky realizácie')):
            self.assertRegex(self.info(cesta)['telo'], rf'<a class="odkaz-sipka" href="[^"]+">{kus}')

    def test_11_slider_potiahnutim_prsta(self):
        """Položka 11: vodorovné potiahnutie prstom alebo perom po fotke (aspoň 48 px a viac ako 1,5-násobok zvislého)
        prepne stavbu ako šípky a slider ostane stáť, kým sa človek nedotkne miesta mimo neho; zvislé potiahnutie, myš
        a priblížená stránka nič nemenia; .snimky má touch-action pan-y. Simulácia časti 3b app.js nad falošným DOM
        (Chrome pri 390 x 844 s dotykom: vľavo RD Ahoj na Dvojdom S, vpravo späť, po 9 s bez zmeny, zvislo posun stránky)."""
        self.assertEqual(deklaracie(self.zaklad, '.snimky').get('touch-action'), 'pan-y pinch-zoom')
        kod = cast_appjs('// Odložená fotka slidera', '// 4b. Kotva')
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function triedy() { const s = new Set(); return { add: (...x) => x.forEach((y) => s.add(y)), remove: (...x) => x.forEach((y) => s.delete(y)), contains: (x) => s.has(x) }; }
function prvok(extra) { const p = { poslucha: {}, classList: triedy(), atr: {}, addEventListener(t, f) { (this.poslucha[t] = this.poslucha[t] || []).push(f); },
  getAttribute(n) { return n in this.atr ? this.atr[n] : null; }, setAttribute(n, v) { this.atr[n] = String(v); }, hasAttribute(n) { return n in this.atr; },
  removeAttribute(n) { delete this.atr[n]; } }; return Object.assign(p, extra || {}); }
const img = () => prvok({ complete: true, naturalWidth: 800, decode: () => Promise.resolve() });
const snimky = [0, 1, 2].map((i) => { const li = prvok(); const im = img(); li.querySelector = (s) => (s === 'img' ? im : null); if (i === 0) li.classList.add('je'); return li; });
const popisy = [0, 1, 2].map((i) => { const li = prvok(); if (i === 0) li.classList.add('je'); return li; });
const tlacidla = ['-1', 'pauza', '1'].map((x) => prvok({ atr: { 'data-slider': x } }));
tlacidla[1].atr['aria-pressed'] = 'false';
const ovladanie = prvok({ querySelectorAll: (s) => (s === '[data-slider]' ? tlacidla : []) });
const plocha = prvok();
const zoznam = prvok();
const vnutri = new Set([plocha, ...snimky, ...tlacidla, ovladanie]);
const slider = prvok({ atr: { 'data-interval': '8000', 'data-prelinanie': '1200' }, style: { setProperty() {} },
  contains: (x) => vnutri.has(x),
  querySelector: (s) => ({ '.slider-ovladanie': ovladanie, '.slider-cislo': null, '.slider-popisy': zoznam, '[data-slider="pauza"]': tlacidla[1], '.snimky': plocha })[s] || null,
  querySelectorAll: (s) => (s === '.snimka' ? snimky : s === '.slider-popis' ? popisy : []) });
const dok = prvok({ hidden: false, querySelector: (s) => (s === '.slider' ? slider : null), createElement: () => prvok() });
const casovace = []; let id = 0;
const win = { setTimeout: (f, ms) => { casovace.push(ms); return ++id; }, clearTimeout() {}, visualViewport: { scale: 1 }, PointerEvent: function () {},
  addEventListener() {}, requestAnimationFrame() {} };
const ctx = { d: dok, window: win, pohyb: true, maIO: false, vsetky: (s, k) => Array.prototype.slice.call((k || dok).querySelectorAll(s)), Number, Promise };
vm.createContext(ctx);
vm.runInContext(kod, ctx);
const aktivna = () => snimky.findIndex((x) => x.classList.contains('je'));
const pusti = (prv, t, e) => (prv.poslucha[t] || []).forEach((f) => f(e));
const tah = (typ, dx, dy) => { pusti(plocha, 'pointerdown', { isPrimary: true, pointerType: typ, clientX: 300, clientY: 400, pointerId: 7 });
  pusti(plocha, 'pointerup', { pointerId: 7, clientX: 300 + dx, clientY: 400 + dy }); };
(async () => {
  const v = { na_zaciatku: casovace.filter((x) => x === 8000).length };
  casovace.length = 0;
  tah('touch', -120, 10);
  await new Promise((r) => setTimeout(r, 20));
  v.po_vlavo = aktivna();
  v.planovane_po_tahu = casovace.filter((x) => x === 8000).length;
  tah('touch', 120, -5);
  await new Promise((r) => setTimeout(r, 20));
  v.po_vpravo = aktivna();
  tah('touch', -60, 100);
  tah('mouse', -150, 0);
  tah('pen', -40, 0);
  win.visualViewport.scale = 1.6; tah('touch', -150, 0); win.visualViewport.scale = 1;
  await new Promise((r) => setTimeout(r, 20));
  v.bez_zmeny = aktivna();
  tah('pen', -150, 0);
  await new Promise((r) => setTimeout(r, 20));
  v.pero = aktivna();
  casovace.length = 0;
  (dok.poslucha.pointerdown || []).forEach((f) => f({ target: plocha }));
  v.dotyk_vnutri = casovace.filter((x) => x === 8000).length;
  (dok.poslucha.pointerdown || []).forEach((f) => f({ target: {} }));
  v.dotyk_mimo = casovace.filter((x) => x === 8000).length;
  v.zive_popisy = zoznam.getAttribute('aria-live');
  console.log(JSON.stringify(v));
})();
"""
        v = node_simulacia(self, kod, sim)
        self.assertEqual(v, {'na_zaciatku': 1, 'po_vlavo': 1, 'planovane_po_tahu': 0, 'po_vpravo': 0, 'bez_zmeny': 0,
                             'pero': 1, 'dotyk_vnutri': 0, 'dotyk_mimo': 1, 'zive_popisy': 'polite'})

    def test_13_detail_udaje_pri_fotke(self):
        """Položka 13 a oprava po overení: na detaile s titulnou fotkou je tabuľka údajov v dvojstrane za fotkou (hlava
        má len H1), fotka je pri každej šírke hneď pod H1: do 1279 px fotka, tabuľka a text pod sebou (24 px pod H1, od
        1024 px 32 px), od 1280 px fotka vľavo cez oba riadky, tabuľka a pod ňou kóta s textom vpravo (do 1920 px od
        čiary 8. stĺpca). Chrome: pri 1366 x 633 je z fotky v prvej obrazovke najmenej 51 % (SFÚ), priemer 70 % pri 26
        realizáciách; pri 360 až 412 px celá fotka pri všetkých 26 (predtým pri 390 x 844 priemer 38 %, 11 bez fotky)."""
        for r in self.w.realizacie:
            telo = self.info(f'realizacie/{r["slug"]}/')['telo']
            hlava = telo.split('<div class="hlava-stranky hlava-detail">', 1)[1].split('<div class="obsah mriezka detail', 1)[0]
            self.assertNotIn('hlava-bok', hlava, r['slug'])
            self.assertIn('<h1 class="nadpis-stranky" id="nadpis">', hlava, r['slug'])
            self.assertRegex(telo, r'(?s)<div class="obsah mriezka detail detail-s-udajmi"><figure class="pohlad detail-foto">'
                                   r'.*?</figure><div class="detail-udaje"><dl class="fakty udaje">.*?</dl></div>'
                                   r'<div class="detail-text atelier-text">', r['slug'])
            self.assertEqual(telo.count('<dl class="fakty udaje">'), 1, r['slug'])
        pc = self.blok('@media (min-width:1024px)')
        for kus in ('.detail-s-udajmi{margin-top:calc(var(--m-32) - clamp(36px,5vw,72px))}',
                    '.detail.detail-s-udajmi .detail-text>.kota:first-child{margin-top:0}'):
            self.assertIn(kus, pc)
        self.assertNotIn('.detail-s-udajmi .detail-udaje{', pc)
        uzky = self.blok('@media (min-width:1024px) and (max-width:1279px)')
        for kus in ('.detail-s-udajmi .detail-foto{grid-column:1/-1;max-width:none}', '.detail-s-udajmi .detail-text{grid-column:1/-1}'):
            self.assertIn(kus, uzky)
        siroky = self.blok('@media (min-width:1280px)')
        for kus in ('.detail-s-udajmi{grid-template-rows:auto 1fr}', '.detail-s-udajmi .detail-udaje{grid-column:8/-1;grid-row:1}',
                    '.detail-s-udajmi .detail-foto{grid-column:1/8;grid-row:1/span 2;align-self:start}',
                    '.detail-s-udajmi .detail-text{grid-column:8/-1;grid-row:2}'):
            self.assertIn(kus, siroky)
        # od 1921 px voľný stĺpec za fotkou ako pred opravou po overení (široké rozloženie, SIROKE.md)
        self.assertIn('.detail-s-udajmi .detail-udaje,.detail-s-udajmi .detail-text{grid-column:9/-1}',
                      self.blok('@media (min-width:1921px)'))
        # bloky v poradí: počítač, 1024 až 1279, od 1280, od 1921 (každý prepíše predchádzajúci)
        css = self.css
        self.assertLess(css.index('@media (min-width:1024px){'), css.index('@media (min-width:1024px) and (max-width:1279px){'))
        self.assertLess(css.index('@media (min-width:1024px) and (max-width:1279px){'), css.index('@media (min-width:1280px){'))
        self.assertLess(css.rindex('@media (min-width:1280px){'), css.index('@media (min-width:1921px){'))
        # do 1023 px: fotka 24 px pod H1 (spodok hlavy clamp(36px,5vw,72px) vyrovnaný), tabuľka a text po 32 px
        self.assertEqual(deklaracie(self.zaklad, '.hlava-stranky').get('padding'), '0 0 clamp(36px,5vw,72px)')
        self.assertEqual(deklaracie(self.zaklad, '.detail').get('row-gap'), '32px')
        self.assertEqual(deklaracie(self.zaklad, '.detail-s-udajmi').get('margin-top'), 'calc(var(--m-24) - clamp(36px,5vw,72px))')
        self.assertNotIn('.detail-s-udajmi .detail-foto{', self.zaklad)

    def test_15_karta_bez_rokov_len_vlasova_ciara(self):
        """Položka 15: karta bez akéhokoľvek roku má v riadku kóty len vlasovú čiaru farby čiar webu na celú šírku
        karty, bez šikmých koncov (karty v rade ostávajú v spoločných riadkoch); predtým prázdna kótovacia čiara. Od
        26. 11. 2026 (ROKY.md) má každá skutočná realizácia rok (roky stavby alebo rok príspevku), vlasovú čiaru má
        len realizácia bez rokov aj bez dátumu (malý obsah, realizácia hola, test_rok_prispevku_z_obsahu_alebo_z_datumu)."""
        self.assertEqual(deklaracie(self.zaklad, '.kota-prazdna'),
                         {'margin-inline': '0', 'background-image': 'linear-gradient(var(--beton),var(--beton))'})
        self.assertIn('.kota-prazdna::before,.kota-prazdna::after{content:none}', self.zaklad)
        self.assertEqual([r['slug'] for r in self.w.realizacie if not self.w.roky_karty(r)], [])
        self.assertNotIn('kota-prazdna', self.info('realizacie/')['telo'])

    def test_17_36_maly_zvysok_a_fokus_v_okne(self):
        """Položky 17 a 36: zvyšok najviac polovica dávky (6 pri 12) sa ukáže hneď s dávkou: pri 26 kartách prvé
        Zobraziť ďalšie ukáže všetkých 14 (predtým 12 a ostalo „(2)“), pri 13 až 18 kartách sú hneď všetky; fokus ide
        na nadpis prvej novej karty a karta sa posunie do okna (scrollIntoView block nearest). Chrome pri 390 x 844
        a 1440 x 900: po kliknutí 26 kariet, tlačidlo skryté, karta s fokusom celá v okne nad lištou."""
        self.assertEqual(build.REALIZACIE_NARAZ, 12)
        self.assertEqual([self.w.stav_zoznamu(n) for n in (12, 13, 18, 19, 26)],
                         ['', '', '', 'Zobrazené: 12 z 19', 'Zobrazené: 12 z 26'])
        n = len(self.w.realizacie)
        self.assertGreater(n - 12, 6)
        _, stavy = spravanie_zoznamu(self.ostry, [{'akcia': 'viac'}])
        self.assertEqual(len(stavy[0]['viditelne']), 12)
        self.assertEqual(stavy[0]['viac_pocet'], f'({n - 12})')
        self.assertEqual(len(stavy[1]['viditelne']), n)
        self.assertTrue(stavy[1]['viac_skryte'])
        self.assertEqual(stavy[1]['viac_pocet'], '')
        self.assertEqual(stavy[1]['fokus'], 13)
        self.assertEqual(stavy[1]['posun'], {'poradie': 13, 'block': 'nearest'})
        self.assertIsNone(stavy[0]['posun'])
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('Math.floor(naraz / 2)', js)
        self.assertIn("prvaNova.scrollIntoView({ block: 'nearest' })", js)
        # karta nad spodnou lištou: do 767 px scroll-padding-bottom html
        self.assertIn('html{scroll-padding-bottom:calc(76px + env(safe-area-inset-bottom))}', self.blok('@media (max-width:767px)'))

    def test_18_details_bez_summary_neviditelne(self):
        """Položka 18: kým parser nedočíta summary, details.menu-m je neviditeľné (inak predvolené „▶ Details“); pravidlo je
        v spoločnej časti štýlu, teda aj vo vloženom štýle úvodu, a summary je prvé dieťa details."""
        self.assertIn('details.menu-m:not(:has(>summary)){visibility:hidden}', self.zaklad)
        for cesta in ('', 'kontakt/'):
            s = self.info(cesta)['html']
            self.assertIn('details.menu-m:not(:has(>summary)){visibility:hidden}', s.split('</style>', 1)[0], cesta)
            self.assertIn('<details class="menu-m"><summary class="tlacidlo tlacidlo-obrys tlacidlo-male">Menu</summary>', s)

    def test_19_27_hlavicka_menu_na_osi_a_cislo(self):
        """Položky 19 a 27: od 1100 px hlavička v troch stĺpcoch 1fr auto 1fr, menu na osi obsahu (predtým 51 až 70 px
        vpravo); od 1024 px Zavolať s číslom (do 1023 px len slovo), prístupné meno obsahuje viditeľný text. Zmestí sa
        pri 1024 až 3440 px: pri 1100 px je medzi menu a tlačidlom aspoň 80 px (Chrome 92,5 px)."""
        w1100 = self.blok('@media (min-width:1100px)')
        for kus in ('.hlava .obsah{display:grid;grid-template-columns:1fr auto 1fr}', '.hlava .znak{justify-self:start}',
                    '.menu-d{grid-column:2}', '.hlava-akcie{grid-column:3;justify-self:end}'):
            self.assertIn(kus, w1100)
        self.assertIn('.tel-cislo{display:none}', self.zaklad)
        self.assertIn('.tel-cislo{display:inline;font-weight:400}', self.blok('@media (min-width:1024px)'))
        tel = self.firma['telefon']
        for cesta in ('', 'stavebny-dozor/', 'realizacie/rd-pezinok/'):
            hlava = self.info(cesta)['html'].split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
            a = re.search(r'<a class="tlacidlo tlacidlo-male hlavicka-tel" href="tel:[^"]+" aria-label="([^"]+)">(.*?)</a>', hlava)
            self.assertIsNotNone(a, cesta)
            self.assertEqual(a.group(1), f'Zavolať {tel}')
            viditelny = html.unescape(re.sub(r'<[^>]+>', ' ', a.group(2))).replace(' ', ' ')
            self.assertEqual(' '.join(viditelny.split()), f'Zavolať {tel}')
            self.assertIn('<span class="tel-cislo cisla">', a.group(2))
        # od 1100 px: bočné stĺpce majú (obsah - menu - 2 medzery) / 2 a pojmú logo aj tlačidlo s rezervou aspoň 80 px
        for vw in range(1100 - 17, 3440 - 17 + 1, 7):
            stlpec = (build.sirka_obsahu_px(vw) - self.MENU_PX - 2 * 12) / 2
            self.assertGreaterEqual(stlpec - self.TEL_PX, 80, vw)
            self.assertGreaterEqual(stlpec - self.LOGO_PX, 70, vw)
        # 1024 až 1099 px: logo, Zavolať s číslom a Menu sa zmestia vedľa seba
        for vw in range(1024 - 17, 1099 - 17 + 1):
            self.assertLess(self.LOGO_PX + 12 + self.TEL_PX + 8 + self.MENU_M_PX, build.sirka_obsahu_px(vw), vw)

    def test_23_stitok_nad_hodnotou_do_399(self):
        """Položka 23: do 399 px je štítok nad hodnotou aj v boxe s dokladom na stránkach služieb a v údajoch detailu
        (Chrome pri 360 až 399 px: najužšia hodnota 143 až 182 px a 2 až 9 riadkov s jedným slovom, po oprave 286 až
        325 px a žiadny); do 359 px ostáva pravidlo pre všetky tabuľky, od 400 px vedľa seba ako doteraz."""
        b399 = self.blok('@media (max-width:399px)')
        self.assertIn('.sluzba-bok .fakty>div,.fakty.udaje>div{grid-template-columns:minmax(0,1fr);row-gap:var(--m-4)}', b399)
        self.assertIn('.fakty>div{grid-template-columns:minmax(0,1fr);row-gap:var(--m-4)}', self.blok('@media (max-width:359px)'))
        self.assertEqual(deklaracie(self.zaklad, '.fakty>div').get('grid-template-columns'), 'minmax(7.5em,32%) minmax(0,1fr)')

    def test_24_osvedcenia_na_sirku_ramu(self):
        """Položka 24: na Oprávneniach tabuľka osvedčenia na šírku rámu obrázka (554 px), rám, popis a tabuľka majú jednu
        ľavú aj pravú hranu (Chrome pri 1366 až 3440 px, predtým tabuľka cez celý stĺpec, pri 1920 px 868 px)."""
        self.assertEqual(deklaracie(self.zaklad, '.doklady .fakty'), {'max-width': 'var(--doklad-max)'})
        self.assertEqual(deklaracie(self.zaklad, '.doklad-popis').get('max-width'), 'var(--doklad-max)')
        self.assertEqual(deklaracie(self.zaklad, '.doklad-odkaz').get('max-width'), 'var(--doklad-max)')

    def test_25_stavebny_manazment_kratky_box(self):
        """Položka 25: box Stavebného manažmentu (dve osvedčenia) skrátene: nadpis Osvedčenia SKSI, vydavateľ a meno raz
        a jeden riadok na osvedčenie s odborným zameraním, evidenčným číslom a dátumom (len z firma.json); stránky
        Stavebný dozor a Stavbyvedúci majú celú tabuľku jedného osvedčenia. Chrome: box pri 1366 px 1127 na 583 px,
        spodok boxu 28 px pod textom (predtým 571 px); od 1921 px text v dvoch stĺpcoch a spodky zarovno."""
        telo = self.info('stavebny-manazment/')['telo']
        box = telo.split('<aside class="kalkulacka sluzba-bok"', 1)[1].split('</aside>', 1)[0]
        self.assertEqual(box.count('<div class="doklad'), 1)
        self.assertIn('<div class="doklad doklad-kratky"><h2 class="doklad-h">Osvedčenia SKSI</h2><dl class="fakty">', box)
        riadky = [(html.unescape(k), html.unescape(re.sub(r'<[^>]+>', '', v)).replace(' ', ' '))
                  for k, v in re.findall(r'<div><dt>(.*?)</dt><dd>(.*?)</dd></div>', box)]
        o = self.firma['opravnenia']
        self.assertEqual(riadky, [
            ('Vydala', o[0]['organizacia']), ('Meno', o[0]['osoba']),
            ('Stavbyvedúci', f'{o[0]["odborne_zameranie"]}, evidenčné číslo {o[0]["evidencne_cislo"]}, vydané {o[0]["datum_vydania_text"]}'),
            ('Stavebný dozor', f'evidenčné číslo {o[1]["evidencne_cislo"]}, vydané {o[1]["datum_vydania_text"]}')])
        self.assertIn('href="tel:', box)
        self.assertIn('Oprávnenia a osvedčenia', box)
        for slug, pocet in (('stavebny-dozor', 6), ('stavbyveduci', 7)):
            b = self.info(f'{slug}/')['telo'].split('<aside class="kalkulacka sluzba-bok"', 1)[1].split('</aside>', 1)[0]
            self.assertNotIn('doklad-kratky', b, slug)
            self.assertEqual(b.count('<dt>'), pocet, slug)
        # rôzny vydavateľ alebo držiteľ: celé tabuľky ako doteraz
        stare = self.w.firma['opravnenia']
        try:
            self.w.firma['opravnenia'] = [dict(stare[0]), dict(stare[1], osoba='Iná osoba')]
            self.assertIsNone(self.w.doklady_kratko(['stavbyvedúci', 'stavebný dozor']))
            self.w.firma['opravnenia'] = [dict(stare[0])]
            self.assertIsNone(self.w.doklady_kratko(['stavbyvedúci']))
        finally:
            self.w.firma['opravnenia'] = stare

    def test_26_spat_v_androide_zavrie_menu(self):
        """Položka 26: pri otvorení menu CloseWatcher, systémové Späť (požiadavka na zavretie) menu zavrie a vráti fokus
        na tlačidlo, pri zatvorení menu sa strážca zruší; prehliadač bez CloseWatcher bez chyby ako doteraz. Simulácia
        časti 1 app.js (Chrome 158: po requestClose menu zavreté, fokus na tlačidle, stránka ostala)."""
        kod = cast_appjs('// 1. Mobilné menu', '// 2. Spodná lišta')
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const fokus = [];
function prvok(meno) { return { meno, nodeType: 1, poslucha: {}, addEventListener(t, f) { (this.poslucha[t] = this.poslucha[t] || []).push(f); },
  getAttribute() { return null; }, focus() { fokus.push(meno); } }; }
function spusti(sCloseWatcher) {
  const tl = prvok('summary'); tl.textContent = 'Menu';
  const menu = prvok('menu'); menu.open = false;
  menu.querySelector = (s) => (s === 'summary' ? tl : null);
  menu.querySelectorAll = () => [];
  menu.contains = (x) => x === menu || x === tl;
  const dok = prvok('document');
  dok.querySelectorAll = (s) => (s === 'details.menu-m' ? [menu] : []);
  const strazcovia = [];
  const ctx = { d: dok, vsetky: (sel, koren) => Array.prototype.slice.call((koren || dok).querySelectorAll(sel)) };
  if (sCloseWatcher) ctx.CloseWatcher = class { constructor() { this.zruseny = false; strazcovia.push(this); } destroy() { this.zruseny = true; } };
  vm.createContext(ctx);
  vm.runInContext(kod, ctx);
  const prepni = (o) => { menu.open = o; (menu.poslucha.toggle || []).forEach((f) => f({})); };
  return { tl, menu, strazcovia, prepni };
}
const v = {};
let s = spusti(true);
s.prepni(true);
v.otvorene = { strazcov: s.strazcovia.length, text: s.tl.textContent };
fokus.length = 0;
s.strazcovia[0].onclose();
v.po_spat = { open: s.menu.open, fokus: fokus.slice() };
s.prepni(false);
v.po_toggle = { text: s.tl.textContent, zruseny_prvy: s.strazcovia[0].zruseny };
s.prepni(true);
s.prepni(false);
v.tlacidlom = { strazcov: s.strazcovia.length, zruseny_druhy: s.strazcovia[1].zruseny };
s = spusti(false);
s.prepni(true); s.prepni(false);
v.bez_closewatcher = s.tl.textContent;
console.log(JSON.stringify(v));
"""
        v = node_simulacia(self, kod, sim)
        self.assertEqual(v, {'otvorene': {'strazcov': 1, 'text': 'Zavrieť'}, 'po_spat': {'open': False, 'fokus': ['summary']},
                             'po_toggle': {'text': 'Menu', 'zruseny_prvy': False},
                             'tlacidlom': {'strazcov': 2, 'zruseny_druhy': True}, 'bez_closewatcher': 'Menu'})

    def test_32_telefon_na_sirku(self):
        """Položka 32: telefón na šírku (do 1023 px a najviac 600 px výšky) má hrdinu na výšku okna pod hlavičkou a H1
        podľa výšky okna, dva riadky v tmavej spodnej časti fotky. Chrome pri 914 x 307: hrdina 64 až 307 px, H1 na
        231 až 283 px (predtým hrdina 914 px a H1 na 750 px), pri 844 x 390, 740 x 360, 915 x 412 a 640 x 360 tiež."""
        b = self.blok('@media (max-width:1023px) and (orientation:landscape) and (max-height:600px)')
        self.assertIn('.snimky,.firma-hlava{aspect-ratio:auto;height:calc(100vh - var(--hore));height:calc(100svh - var(--hore))}', b)
        self.assertIn('.uvod-h1{font-size:clamp(1.25rem,8svh,2.5rem);max-width:22em}', b)
        # za blokom 640 až 1023 px (pomer 1 : 1), inak by ho prepísal
        self.assertGreater(self.css.index('@media (max-width:1023px) and (orientation:landscape)'),
                           self.css.index('@media (min-width:640px) and (max-width:1023px){'))

    def test_33_ciele_v_pate(self):
        """Položka 33: v päte logo s odkazom aspoň 44 px široké, „Web: ARLing“ 44,4 px vysoké (riadok 14 px x 1,6
        a 2 x 11 px, záporný okraj drží riadok textu), siete 48 px ako v Kontakte (Chrome: 44 x 44, 81,9 x 44,4, 48 x 48)."""
        self.assertEqual(deklaracie(self.zaklad, '.pata-znak'), {'min-width': '44px'})
        a = deklaracie(self.zaklad, '.pata-arling')
        self.assertEqual((a.get('display'), a.get('padding-block'), a.get('margin-block')), ('inline-block', '11px', '-11px'))
        self.assertEqual(deklaracie(self.zaklad, '.pata .male').get('font-size'), '14px')
        self.assertGreaterEqual(14 * 1.6 + 2 * 11, 44)
        self.assertEqual(deklaracie(self.zaklad, '.siet-cip').get('width'), '48px')
        self.assertNotIn('.pata-siete .siet', self.css)

    def test_34_menu_a_zavriet_rovnako_siroke(self):
        """Položka 34: tlačidlo Menu má 5,6 em (84 px) a okraje 16 px, „Zavrieť“ (49,9 px) sa do 84 px zmestí, takže
        otvorenie menu neposunie Zavolať (Chrome pri 390, 411, 768 a 1024 px: 1,9 px, po oprave 0); do 389 px okraje
        12 px."""
        self.assertEqual(deklaracie(self.zaklad, '.menu-m>summary').get('min-width'), '5.6em')
        self.assertEqual(deklaracie(self.zaklad, '.menu-m>summary').get('padding-inline'), '16px')
        self.assertLessEqual(self.ZAVRIET_PX + 2 * 16, 5.6 * 15)
        self.assertIn('.hlava-akcie .tlacidlo-male{padding:0 12px}', self.blok('@media (max-width:389px)'))
        self.assertIn('.tlacidlo-male{min-height:44px;padding:0 18px;font-size:15px}', self.zaklad)

    def test_35_pod_320_lista_a_roletky(self):
        """Položka 35: pod 320 px (priblíženie 150 % pri 411 px dá 274 px) v lište „E-mail“ (text 34,8 px od rámika,
        predtým 4,2 px), roletky pod sebou, dvojriadkový druh vľavo."""
        b319 = self.blok('@media (max-width:319px)')
        for kus in ('.filtre-vyber{grid-template-columns:minmax(0,1fr)}', '.lista-dlhy{display:none}', '.lista-kratky{display:inline}'):
            self.assertIn(kus, b319)
        self.assertIn('.lista-kratky{display:none}', self.zaklad)
        self.assertEqual(deklaracie(self.blok('@media (max-width:767px)'), '.filter').get('text-align'), 'left')
        s = self.info('')['html']
        self.assertIn('<span class="lista-dlhy">Napísať e-mail</span><span class="lista-kratky">E-mail</span></a></nav>', s)

    def test_37_lista_pri_obmedzenom_pohybe(self):
        """Položka 37: pri obmedzenom pohybe lišta vychádza bez prechodu aj s triedou vidno (Chrome: transition none)."""
        reduce = self.css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('{transition:none}', 1)[0]
        self.assertIn('.lista.vidno', reduce.strip().split(','))
        self.assertIn('.lista', reduce.strip().split(','))

    def test_38_39_rozostupy_textu(self):
        """Položky 38 a 39 (WCAG 1.4.12): valec kóty je vysoký jeden riadok (1lh, záloha 1.2em) a stĺpec 20 číslic sa
        posúva po 5 % svojej výšky, takže pri riadkovaní 1,5 ostane v okne cieľová číslica; „stavebno-manažérska“ v H1
        je inline-block, ktorý sa pri väčších medzerách zalomí za spojovníkom. Chrome s rozostupmi pri 390 a 1440 px:
        zlých číslic 10 a 8 na 0, vodorovný posun úvodu 47 a 64 px na 0."""
        self.assertEqual(deklaracie(self.zaklad, '.valec').get('height'), '1lh')
        self.assertIn('height:1.2em;height:1lh;', self.zaklad)
        self.assertIn('.stlpec{display:block;white-space:pre;transform:translateY(calc(var(--c) * -5%));', self.zaklad)
        self.assertIn('.otocene .stlpec{transform:translateY(calc((var(--c) + 10) * -5%))}', self.zaklad)
        obsah = re.search(r'\.stlpec::before\{content:"([^"]*)"\}', self.zaklad).group(1)
        self.assertEqual(len(re.findall(r'\d', obsah)), 20)
        self.assertNotIn('* -1.2em', self.css)
        self.assertEqual(deklaracie(self.zaklad, '.uvod-h1 .nz'), {'display': 'inline-block', 'max-width': '100%', 'white-space': 'normal'})
        self.assertIn('<span class="nz">stavebno-manažérska</span>', self.info('')['telo'])

    def test_40_kontakt_bez_content_visibility(self):
        """Položka 40: Kontakt na úvode bez content-visibility (CDP pri 390 a 1440 px: nadpis Kontakt v strome prístupnosti,
        nie ignored); komentár app.js už o content-visibility nehovorí."""
        self.assertNotIn('content-visibility', self.css)
        self.assertNotIn('contain-intrinsic-size', self.css)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('content-visibility', js)

    def test_siroke_osamele_slovo_v_boxe(self):
        """Kontrola širokého rozloženia (drobné): hodnoty v boxe s dokladom bez osamelého slova na poslednom riadku
        (text-wrap pretty). Chrome po 8 px pri 1024 až 2576 px: /stavebny-dozor/ 195 hodnôt so sirotou na 27,
        /stavbyveduci/ 122 na 4 (zvyšok len pri 1024 až 1280 px, kde sa dve posledné slová nezmestia na riadok),
        od 1288 px 0; riadky sa zmenili len v hodnotách, ktoré sirotu mali."""
        self.assertEqual(deklaracie(self.zaklad, '.sluzba-bok .fakty dd'), {'text-wrap': 'pretty'})


class TestAuditRychlost(unittest.TestCase):
    """Audit naplno živého náhľadu 25. 11. 2026, časť 3 (rýchlosť): položky 43, 44, 45, 46 a 48 zoznamu
    audit-naplno/synteza/ZOZNAM.md. Merania pred a po v Chrome 158 (prenos, LCP pri Slow 4G a CPU 4x, písma, vynútené
    prepočty zo stopy, fotky skrytých kariet, správanie, snímky): ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/
    AUDIT-OPRAVY.md, časť 3."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.w = s['ostry'], s['web']
        cls.css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        cls.zaklad = css_bez_blokov(cls.css)
        cls.sablona_js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        cls.man = json.loads((KOREN / 'fotky-web' / 'manifest.json').read_text(encoding='utf-8'))['fotky']

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    def test_43_slider_840(self):
        """Položka 43: každá z 11 fotiek slidera má v AVIF navyše 840w, súbor je z fotky-web (vyrobil ho
        nastroje/predlohy_web.py --len pre fotky slidera), preload prvej fotky ho má tiež. Pri 390 a 412 px s hustotou
        1,75 (Lighthouse mobil) prehliadač vezme pre RD Ahoj 01 840w namiesto 960w (181 219 B na 137 580 B); WebP,
        karty a galéria bez zmeny, súbor 840 len na úvode."""
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        snimky = re.findall(r'<li class="snimka snimka-(\d+)[^"]*" data-slug="([a-z0-9-]+)">(.*?)</li>', uvod, re.S)
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        self.assertEqual(len(fotky), len(self.w.nastavenia['uvod_slider']))
        self.assertEqual(len(snimky), len(fotky))
        for (r, f, _), (_, slug, obr) in zip(fotky, snimky):
            avif = re.search(r'(?:<source type="image/avif" srcset|data-avif)="([^"]+)"', obr).group(1)
            kandidati = {int(x.split(' ')[1][:-1]): x.split(' ')[0] for x in avif.split(', ')}
            self.assertIn(840, kandidati, slug)
            # súbor na webe je ten istý ako vo fotky-web (sha256 z manifestu), široký 840 px
            z = self.man[f['sha']]['subory']['|840|avif']
            self.assertEqual(z['w'], 840)
            self.assertEqual(hashlib.sha256((self.ostry / kandidati[840]).read_bytes()).hexdigest(), z['sha256'], slug)
            webp = re.search(r'<img [^>]*?(?:data-srcset|srcset)="([^"]+)"', obr).group(1)
            self.assertNotIn(' 840w', webp, slug)
        pre = re.search(r'<link rel="preload" as="image" type="image/avif" imagesrcset="([^"]+)" imagesizes="([^"]+)"', uvod)
        self.assertIn('-840.avif 840w', pre.group(1))
        # výber prehliadača zo sizes vyhodnotených nezávisle od sizes_slidera (vyhodnot_sizes)
        kand = sorted(int(x.split(' ')[1][:-1]) for x in pre.group(1).split(', '))
        for vw, vh in ((390, 844), (412, 823)):
            ziadost = vyhodnot_sizes(pre.group(2), vw, vh) * 1.75
            self.assertEqual(next(s for s in kand if s >= ziadost), 840, (vw, ziadost))
            self.assertEqual(next(s for s in kand if s != 840 and s >= ziadost), 960, (vw, ziadost))
        r0, f0, _ = fotky[0]
        self.assertEqual(r0['slug'], 'rd-ahoj-bratislava-nove-mesto')
        self.assertLess(self.man[f0['sha']]['subory']['|840|avif']['bajty'], self.man[f0['sha']]['subory']['|960|avif']['bajty'])
        for p in self.ostry.rglob('*.html'):
            if p != self.ostry / 'index.html':
                self.assertNotIn('-840.', p.read_text(encoding='utf-8'), p)
        self.assertEqual(build.SIRKY_OREZU, (480, 720, 960))

    def test_43_840_len_zo_suboru_predlohy(self):
        """840 sa pridá len so súborom vo fotky-web alebo pri pláne predlôh (nikdy zakódované zo zdroja v obsah/, RD Ahoj
        01 má retuš len v predlohe), len keď najbližšia väčšia šírka je od nej širšia aspoň o SIRKA_BLIZKO a len v AVIF."""
        if not self.w.avif:
            self.skipTest('Pillow bez AVIF')
        f = {'zaklad': (2000, 2657), 'sirky_avif': build.sirky_pre(2000), 'sirky': build.sirky_pre(2000, build.WEBP_MAX),
             'fw': True, 'sha': 'f' * 64}
        povodne = self.w.predlohy
        try:
            self.w.predlohy = None
            self.assertEqual(self.w.sirky_slidera(f, 'avif'), [480, 720, 960, 1440, 2000])
            self.w.predlohy = {}
            self.assertEqual(self.w.sirky_slidera(f, 'avif'), [480, 720, 840, 960, 1440, 2000])
            self.assertEqual(self.w.sirky_slidera(f, 'webp'), [480, 720, 960, 1440])
            for w, ocak in ((900, [480, 720, 900]), (800, [480, 720, 800]), (930, [480, 720, 840, 930])):
                g = dict(f, zaklad=(w, round(w * 4 / 3)), sirky_avif=build.sirky_pre(w))
                self.assertEqual(self.w.sirky_slidera(g, 'avif'), ocak, w)
            g = dict(f, zaklad=(1000, 1333), sirky_avif=build.sirky_pre(1000))
            self.assertEqual(self.w.sirky_slidera(g, 'avif'), [480, 720, 840, 1000])
        finally:
            self.w.predlohy = povodne

    def test_44_mono_podmnozina_cisel(self):
        """Položka 44: podmnožina Geist Mono pre číslice (sablona/geist-mono-cisla.woff2 z nastroje/pismo_mono_cisla.py)
        je tretí @font-face rodiny BMB Mono, po latinke Mono s tými istými deskriptormi a s unicode-range z nástroja.
        Preload má len stránka, ktorá Mono kreslí (na úvode v hlave za prednačítanou fotkou LCP, inde na konci tela:
        pri HTTP/1.1 preload v hlave zdržal fotku LCP /realizacie/, LCP pri 390 px a Slow 4G 3 676 proti 2 940 ms), a
        každý znak, ktorý stránky pri načítaní kreslia v Mono, je v podmnožine, takže sa latinka Mono nesťahuje.
        .nadpis-pocet („8 fotiek“) a skrytý text kóty (.vh) písmom webu. Chrome: úvod, /realizacie/, detaily a dve
        služby stiahnu 5 588 B namiesto 23 128 B, Kontakt nič."""
        sub = KOREN / 'sablona' / build.PISMO_MONO_CISLA
        self.assertTrue(sub.is_file())
        self.assertLess(sub.stat().st_size, 8000)
        self.assertIn(build.PISMO_MONO_CISLA, build.PISMA)
        nastroj = (KOREN / 'nastroje' / 'pismo_mono_cisla.py').read_text(encoding='utf-8')
        rozsah = re.search(r"^UNICODE_RANGE = '([^']+)'", nastroj, re.M).group(1)
        tvare = re.findall(r'@font-face\{font-family:"BMB Mono";src:url\(@PRE@fonts/([^)]+)\) format\("woff2"\);([^}]*)\}', self.css)
        self.assertEqual([t for t, _ in tvare], ['geist-mono-latin-ext.woff2', 'geist-mono-latin.woff2', build.PISMO_MONO_CISLA])
        deskriptory = lambda s: re.sub(r';?unicode-range:[^;]*', '', s)  # noqa: E731
        self.assertEqual(deskriptory(tvare[2][1]), deskriptory(tvare[1][1]))
        self.assertIn('font-weight:100 900', tvare[2][1])
        self.assertEqual(re.search(r'unicode-range:([^;}]+)', tvare[2][1]).group(1), rozsah)
        kody = set()
        for kus in rozsah.split(','):
            od, _, do = kus[2:].partition('-')
            kody.update(range(int(od, 16), int(do or od, 16) + 1))
        self.assertTrue({ord(c) for c in '0123456789 ()/.,:'} <= kody)
        self.assertEqual(deklaracie(self.zaklad, '.nadpis-pocet').get('font-family'), 'var(--pismo)')
        self.assertEqual(deklaracie(self.zaklad, '.vh').get('font-family'), 'var(--pismo)')
        # MONO_* v build.py zodpovedá selektorom styl.css s var(--mono)
        selektory = {sel.strip() for m in re.finditer(r'([^{}]+)\{([^{}]*var\(--mono\)[^{}]*)\}', self.css)
                     for sel in m.group(1).split(',')}
        self.assertEqual(selektory, {'.' + t for t in build.MONO_TRIEDY} | {f'.{a} {b}' for a, b in build.MONO_POTOMOK})
        for t in build.MONO_SANS:
            self.assertEqual(deklaracie(self.zaklad, '.' + t).get('font-family'), 'var(--pismo)', t)
        self.assertRegex(self.zaklad, r'\.stlpec::before\{content:"0\\A 1\\A 2')
        # preload práve na stránkach s Mono; znaky Mono pri načítaní len z podmnožiny
        s_mono, bez_mono, znaky_spolu = [], [], set()
        odkaz = f'href="[^"]*fonts/{re.escape(build.PISMO_MONO_CISLA)}" as="font" type="font/woff2" crossorigin>'
        for p in sorted(self.ostry.rglob('*.html')):
            t = p.read_text(encoding='utf-8')
            znaky = build.znaky_mono(t.split('<body', 1)[1]) if '<body' in t else set()
            znaky_spolu |= znaky
            self.assertEqual(len(re.findall('<link rel="preload" ' + odkaz, t)), 1 if znaky else 0, p)
            if znaky:
                hlava = t.split('</head>', 1)[0]
                if '<link rel="preload" as="image"' in hlava:
                    self.assertRegex(hlava, re.compile('<link rel="preload" as="image"[^>]*>.*<link rel="preload" ' + odkaz, re.S))
                else:
                    self.assertRegex(t, '<link rel="preload" ' + odkaz + '</body>')
                    self.assertNotRegex(hlava, '<link rel="preload" ' + odkaz)
            (s_mono if znaky else bez_mono).append(p.relative_to(self.ostry).as_posix())
        self.assertEqual({ord(c) for c in znaky_spolu} - kody, set(), sorted(znaky_spolu))
        for c in ('index.html', 'realizacie/index.html', 'stavebny-dozor/index.html', 'stavebny-manazment/index.html',
                  'realizacie/filmovy-klub-lumiere-spitalska-bratislava/index.html', 'realizacie/rd-ahoj-bratislava-nove-mesto/index.html'):
            self.assertIn(c, s_mono)
        for c in ('kontakt/index.html', 'ochrana-udajov/index.html', 'opravnenia/index.html', 'stavbyveduci/index.html', '404.html'):
            self.assertIn(c, bez_mono)
        # detekcia: valce kóty (CSS content), statická kóta, počet filtra; písmo webu vo vnútri Mono sa nepočíta
        self.assertEqual(build.znaky_mono('<span class="kota"><span class="kota-text otoc"><span class="vh">2013 až 2014</span>'
                                          '<span class="valce" aria-hidden="true"><span class="valec"><span class="stlpec c2 i0">'
                                          '</span></span> <span class="kota-slovo">až</span></span></span></span>'), set('0123456789'))
        self.assertEqual(build.znaky_mono('<button class="filter">Všetky<span>26</span></button>'), {'2', '6'})
        self.assertEqual(build.znaky_mono('<h2>Galéria <span class="nadpis-pocet cisla">8 fotiek</span></h2>'), set())
        self.assertEqual(build.znaky_mono('<span class="kota kota-prazdna" aria-hidden="true"></span><p>2025</p>'), set())

    def test_44_podmnozina_zhodna_s_latinkou(self):
        """Podmnožina má presne znaky z unicode-range, celú os váhy 100 až 900 (kóta Činnosti má váhu 300), tie isté
        zvislé metriky a šírky znakov ako latinka Mono (riadky a kóty sa nepohnú) a nástroj ju vyrobí bajtovo rovnakú."""
        try:
            from fontTools.ttLib import TTFont
            import brotli  # noqa: F401
        except ImportError:
            self.skipTest('fontTools alebo brotli nie je')
        import importlib.util
        spec = importlib.util.spec_from_file_location('pismo_mono_cisla', KOREN / 'nastroje' / 'pismo_mono_cisla.py')
        nastroj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(nastroj)
        sub = KOREN / 'sablona' / build.PISMO_MONO_CISLA
        f, g = TTFont(str(sub)), TTFont(str(KOREN / 'sablona' / 'geist-mono-latin.woff2'))
        self.assertEqual(set(f.getBestCmap()), nastroj.kody())
        self.assertEqual([(a.axisTag, a.minValue, a.maxValue) for a in f['fvar'].axes], [('wght', 100.0, 900.0)])
        for tab, polia in (('hhea', ('ascent', 'descent', 'lineGap')), ('head', ('unitsPerEm',)),
                           ('OS/2', ('sTypoAscender', 'sTypoDescender', 'sTypoLineGap', 'usWinAscent', 'usWinDescent'))):
            for p in polia:
                self.assertEqual(getattr(f[tab], p), getattr(g[tab], p), (tab, p))
        gc = g.getBestCmap()
        for k, meno in f.getBestCmap().items():
            self.assertEqual(f['hmtx'][meno][0], g['hmtx'][gc[k]][0], hex(k))
        buf = io.BytesIO()
        nastroj.podmnozina().save(buf)
        self.assertEqual(buf.getvalue(), sub.read_bytes())

    def test_45_app_js_zhusteny(self):
        """Položka 45: web dostáva app.js zhustený pri zostave (build.zhusti_js, bez novej závislosti): bez úvodného
        komentára, celých riadkov s // a odsadenia; riadky kódu tie isté a v tom istom poradí. Zhustený súbor má
        25 145 B namiesto 40 003 B (gzip úrovne 6 7 348 namiesto 13 009 B), prejde node --check a v Chrome sa správa rovnako
        (hlavička, menu, slider, lišta, objavenie, filter, Zobraziť ďalšie, lightbox); časť 5 skúša test správania
        zoznamu priamo na zhustenom súbore."""
        web = (self.ostry / 'app.js').read_text(encoding='utf-8')
        self.assertEqual(web, build.zhusti_js(self.sablona_js))
        self.assertLess(len(web.encode('utf-8')), 0.7 * len(self.sablona_js.encode('utf-8')))
        # predpoklady v šablóne: žiadny šablónový reťazec, pokračovanie riadka, iný blokový komentár ani komentár za kódom
        self.assertNotIn('`', self.sablona_js)
        self.assertIsNone(re.search(r'\\\n', self.sablona_js))
        kod = re.sub(r'\A\s*/\*.*?\*/', '', self.sablona_js, flags=re.S)
        self.assertNotIn('/*', kod)
        for r in kod.split('\n'):
            if not r.strip().startswith('//'):
                self.assertNotIn('//', r, r)
        # výstup: riadky kódu šablóny bez odsadenia v tom istom poradí, vynechané len prázdne riadky a komentáre
        ostavaju = [r.strip() for r in kod.split('\n') if r.strip() and not r.strip().startswith('//')]
        self.assertEqual(web.rstrip('\n').split('\n'), ostavaju)
        self.assertTrue(web.startswith('(function () {\n'))
        # verzia v adrese je odtlačok zhusteného súboru
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        self.assertIn(f'app.js?v={hashlib.sha256(web.encode("utf-8")).hexdigest()[:10]}"', uvod)
        # šablónový reťazec alebo pokračovanie riadka: súbor ide bez zhustenia a s varovaním
        for zly in ('var a = `x\n  // y`;\n', "var a = 'x\\\n  // y';\n"):
            pred = len(build.VAROVANIA)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(build.zhusti_js(zly), zly)
            self.assertEqual(len(build.VAROVANIA), pred + 1)
            del build.VAROVANIA[pred:]
        node = shutil.which('node')
        if not node:
            self.skipTest('node nie je k dispozícii')
        import subprocess
        p = subprocess.run([node, '--check', str(self.ostry / 'app.js')], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_46_karty_za_prvou_davkou_mimo_rozlozenia(self):
        """Položka 46: na /realizacie/ majú karty za prvou dávkou (13 až 26) data-neskor a styl.css ich pri triede js
        necháva mimo rozloženia, kým app.js atribút nezmaže (hneď na začiatku časti 5; hidden nastaví obnov v tej istej
        úlohe); bez JS ich nič neskryje. Chrome pri 1440 x 900 a Slow 4G: fotky kariet 13 až 20 pred spustením app.js
        8 (356 910 B), po oprave 0; bez JS a pri app.js 404 viditeľných 26."""
        r = Rozbor()
        r.feed(self.info('realizacie/')['telo'])
        li = [a for t, a in r.prvky if t == 'li' and 'dlazdica' in a.get('class', '').split()]
        n = len(li)
        prvych = self.w.prvych_zoznamu(n)
        self.assertEqual(prvych, build.REALIZACIE_NARAZ)
        self.assertEqual([int(a['data-poradie']) for a in li if 'data-neskor' in a], list(range(prvych + 1, n + 1)))
        self.assertTrue(all('hidden' not in a for a in li))
        self.assertEqual([self.w.prvych_zoznamu(x) for x in (5, 12, 13, 18, 19, 26)], [5, 12, 13, 18, 12, 12])
        self.assertEqual([self.w.stav_zoznamu(x) for x in (12, 18, 19)], ['', '', 'Zobrazené: 12 z 19'])
        # pravidlo v spoločnej časti štýlu za .dlazdica[hidden] (rovnaká váha ako .karty>.projekt, neskoršie vyhrá)
        self.assertIn('.js [data-neskor]{display:none}', self.zaklad)
        self.assertLess(self.zaklad.index('.dlazdica[hidden]'), self.zaklad.index('.js [data-neskor]{display:none}'))
        self.assertIn("if(window.BMB==null)d.classList.remove('js')", build.SKRIPT_JS)
        # app.js: zmazanie hneď na začiatku časti 5, pred čímkoľvek, čo môže zlyhať, a pred obnov(false)
        cast5 = self.sablona_js.split("var filtre = d.querySelector('[data-filtre]')", 1)[1].split(
            "var dlg = d.getElementById('lightbox')", 1)[0]
        i = cast5.index("removeAttribute('data-neskor')")
        self.assertLess(i, cast5.index('new URLSearchParams'))
        self.assertLess(i, cast5.index('obnov(false);'))
        # správanie nad zhusteným app.js webu: po spustení žiadna karta s data-neskor, viditeľných prvých 12
        _, stavy = spravanie_zoznamu(self.ostry, [{'akcia': 'viac'}])
        self.assertEqual([s['neskor'] for s in stavy], [[], []])
        self.assertEqual(len(stavy[0]['viditelne']), prvych)
        self.assertEqual(len(stavy[1]['viditelne']), n)
        # sabotáž: app.js bez zmazania atribútu test správania zoznamu zachytí
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-neskor-'))
        try:
            dobry = (self.ostry / 'app.js').read_text(encoding='utf-8')
            zly = dobry.replace("dlazdice.forEach(function (li) { li.removeAttribute('data-neskor'); });", '')
            self.assertNotEqual(zly, dobry)
            (tmp / 'app.js').write_text(zly, encoding='utf-8')
            chyby = chyby_zoznamu(self.ostry, app_js=tmp / 'app.js')
            self.assertTrue(any('data-neskor' in c for c in chyby), chyby[:3])
            self.assertEqual(chyby_zoznamu(self.ostry), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_48_bez_vynuteneho_prepoctu_pri_spusteni(self):
        """Položka 48: app.js pri spustení nečíta rozloženie. Čiaru pod hlavičkou prepína pozorovateľ značky 4 px na
        vrchu stránky (prvé hlásenie dá stav aj po príchode na kotvu), bez IntersectionObserver prvé čítanie až
        v requestAnimationFrame; časť 0b berie polohu z prvého hlásenia pozorovateľa (test_6). Chrome so stopou pri
        CPU 4x: vynútené prepočty z app.js pri spustení 270 až 313 ms (úvod), 61 až 235 ms (/realizacie/) a 84 až
        265 ms (detail Lumière), po oprave 0."""
        # kód bez komentárov (zhustený app.js webu): žiadne čítanie polohy ani výšky okna
        web = (self.ostry / 'app.js').read_text(encoding='utf-8')
        for citanie in ('getBoundingClientRect', 'innerHeight', 'clientHeight', 'offsetTop', 'getComputedStyle'):
            self.assertNotIn(citanie, web)
        self.assertEqual(web.count('pageYOffset'), 1)
        # blok pod oknom dostane vstup až po prvom hlásení (už vykreslený): skrytie bez prechodu, prechod len pri objavení
        # (Chrome pri 390 x 844 bez tohto pravidla: bloky pod oknom 0,8 s po načítaní na nepriehľadnosti 0,57, nie 0)
        pohyb = self.css.split('@media (prefers-reduced-motion:no-preference){', 1)[1].split('\n}\n', 1)[0]
        self.assertLess(pohyb.index('.js .vstup{transition:opacity 700ms'), pohyb.index('.js .vstup:not(.videne){transition:none}'))
        kod = cast_appjs('// 3. Pripnutá hlavička', '// Odložená fotka slidera')
        self.assertNotIn('obnov();', kod)
        sim = r"""
const vm = require('vm');
const kod = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function triedy() { const s = new Set(); return { add: (x) => s.add(x), remove: (x) => s.delete(x), contains: (x) => s.has(x),
  toggle: (x, b) => { if (b) s.add(x); else s.delete(x); return b; } }; }
function beh(maIO) {
  const hlava = { classList: triedy() };
  const vlozene = [];
  const pozorovatelia = [];
  class IO { constructor(fn) { this.fn = fn; this.sledovane = []; pozorovatelia.push(this); } observe(e) { this.sledovane.push(e); } }
  const body = { firstChild: { meno: 'prvy' }, insertBefore(e, pred) { vlozene.push({ e, pred }); } };
  const d = { body, querySelector: (s) => (s === '.hlava' ? hlava : null),
    createElement: (t) => ({ tag: t, atr: {}, style: {}, setAttribute(n, v) { this.atr[n] = String(v); } }) };
  let citania = 0; let posun = 0;
  const raf = [];
  const win = { addEventListener() {}, requestAnimationFrame: (f) => raf.push(f) };
  Object.defineProperty(win, 'pageYOffset', { get() { citania++; return posun; } });
  vm.runInContext(kod, vm.createContext({ d, window: win, maIO, IntersectionObserver: IO }));
  const v = { citania_pri_spusteni: citania, pozorovatelov: pozorovatelia.length, vlozene: vlozene.length, raf: raf.length };
  if (maIO) {
    const z = vlozene[0];
    v.znacka = { prva: z.pred === body.firstChild, tag: z.e.tag, aria: z.e.atr['aria-hidden'], styl: z.e.style,
      sleduje: pozorovatelia[0].sledovane[0] === z.e };
    pozorovatelia[0].fn([{ isIntersecting: true }]);
    v.hore = hlava.classList.contains('posunute');
    pozorovatelia[0].fn([{ isIntersecting: true }, { isIntersecting: false }]);
    v.posunute = hlava.classList.contains('posunute');
    pozorovatelia[0].fn([{ isIntersecting: true }]);
    v.spat = hlava.classList.contains('posunute');
  } else {
    posun = 10;
    raf.forEach((f) => f());
    v.po_raf = hlava.classList.contains('posunute');
  }
  v.citania = citania;
  return v;
}
console.log(JSON.stringify({ io: beh(true), bez_io: beh(false) }));
"""
        v = node_simulacia(self, kod, sim)
        self.assertEqual(v['io'], {'citania_pri_spusteni': 0, 'pozorovatelov': 1, 'vlozene': 1, 'raf': 0,
                                   'znacka': {'prva': True, 'tag': 'div', 'aria': 'true', 'sleduje': True,
                                              'styl': {'position': 'absolute', 'top': '0', 'left': '0', 'width': '1px',
                                                       'height': '4px', 'pointerEvents': 'none', 'visibility': 'hidden'}},
                                   'hore': False, 'posunute': True, 'spat': False, 'citania': 0})
        self.assertEqual(v['bez_io'], {'citania_pri_spusteni': 0, 'pozorovatelov': 0, 'vlozene': 0, 'raf': 1, 'po_raf': True,
                                       'citania': 1})


_FTP = {}


def ftp_zostava():
    """Ostrá zostava skutočného obsahu pre FTP na WebHouse (premenná BMB_HOSTING=apache) do prázdneho dočasného
    priečinka, raz za beh; zoznam zostavy je vedľa výstupu. Kódovanie rýchlou náhradou."""
    if not _FTP:
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-ftp-'))
        atexit.register(shutil.rmtree, tmp, True)
        vystup = tmp / 'web-ftp'
        _FTP['o'] = postav(vystup, rychle=True, prostredie={'BMB_HOSTING': 'apache'})
        _FTP.update(tmp=tmp, vystup=vystup)
    return _FTP


def nastroj(meno):
    """Modul z nastroje/ (htaccess_simulacia, kontrola_spustenia)."""
    import importlib
    if str(KOREN / 'nastroje') not in sys.path:
        sys.path.insert(0, str(KOREN / 'nastroje'))
    return importlib.import_module(meno)


def bez(text, kus, nahrada=''):
    """Sabotáž .htaccess: kus musí v texte byť (inak by test sabotáže nič neskúšal)."""
    assert kus in text, kus
    return text.replace(kus, nahrada, 1)


class TestHostingApache(unittest.TestCase):
    """Audit naplno živého náhľadu 25. 11. 2026, časť 4 (spustenie cez FTP na WebHouse): položka 1 zoznamu
    audit-naplno/synteza/ZOZNAM.md (prepínač hostingu, .htaccess, 32 presmerovaní 301, bez CNAME, .nojekyll,
    .zostava.json a index.php/, ochrana údajov s WebHouse) a položka 49 (README o BMB_OSTRY). Skutočný Apache na PC nie
    je: pravidlá overuje simulácia nastroje/htaccess_simulacia.py (sama overená na pravidlách z dokumentácie Apache)
    a kontrola po nahratí nastroje/kontrola_spustenia.py proti serveru simulácie. Záznam: ops/weby-na-mieru/klienti/
    bmbuilding/kolo-1/brana-3/AUDIT-OPRAVY.md, časť 4; postup: kolo-1/SPUSTENIE-FTP.md."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        f = ftp_zostava()
        cls.ostry, cls.realizacie, cls.w = s['ostry'], s['realizacie'], s['web']
        cls.ftp, cls.tmp = f['vystup'], f['tmp']
        cls.kod, cls.out, cls.err = f['o']
        cls.ht = (cls.ftp / '.htaccess').read_text(encoding='utf-8')
        cls.texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        cls.presm = json.loads((KOREN / 'sablona' / 'presmerovania.json').read_text(encoding='utf-8'))['presmerovania']
        cls.sim = nastroj('htaccess_simulacia')

    def ocakavane_ciele(self):
        """Cieľ každej starej adresy nezávisle od build.py: #sluzby je od zmeny 3A #cinnost, nezverejnená realizácia
        (zámerne skrytá, ZMENY-KOLO-1.md 5.6) ide na zoznam."""
        v = []
        for z in self.presm:
            ciel = {'/#sluzby': '/#cinnost'}.get(z['ciel'], z['ciel'])
            m = re.match(r'^/realizacie/([a-z0-9-]+)/$', ciel)
            if m and m.group(1) not in self.realizacie:
                ciel = '/realizacie/'
            v.append((z['stara'], ciel))
        return v

    def test_1_ftp_zostava_bez_suborov_pages(self):
        """Ostrá zostava pre FTP: kód 0, presne súbory ostrého webu pre GitHub Pages bez CNAME, .nojekyll,
        .zostava.json a 32 stránok v index.php/ (priečinok by sa na hostingu zrazil so súborom index.php Joomly),
        navyše .htaccess; inak sa líši len /ochrana-udajov/ (hosting). Zoznam zostavy je vedľa výstupu, canonical,
        sitemap, robots a llms.txt sú na https://bmbuilding.sk, nikde noindex, 404 s cestami od koreňa."""
        self.assertEqual(self.kod, 0, self.err)
        self.assertIn('(ostrý, hosting apache)', self.out)
        for meno in ('CNAME', '.nojekyll', build.MANIFEST, 'index.php'):
            self.assertFalse((self.ftp / meno).exists(), meno)
        manifest = self.tmp / ('web-ftp' + build.MANIFEST)
        self.assertTrue(manifest.is_file())
        zoznam = json.loads(manifest.read_text(encoding='utf-8'))['subory']
        self.assertIn('.htaccess', zoznam)
        self.assertEqual(set(zoznam), set(odtlacky(self.ftp)))
        o, f = odtlacky(self.ostry), odtlacky(self.ftp)
        len_pages = set(o) - set(f)
        self.assertEqual({x for x in len_pages if not x.startswith('index.php/')}, {'CNAME', '.nojekyll', build.MANIFEST})
        self.assertEqual(len([x for x in len_pages if x.startswith('index.php/')]), len(self.presm))
        self.assertEqual(set(f) - set(o), {'.htaccess'})
        self.assertEqual({k for k in set(o) & set(f) if o[k] != f[k]}, {'ochrana-udajov/index.html'})
        for p in stranky(self.ftp):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('noindex', s, p)
            if p.name == 'index.html':
                cesta = p.parent.relative_to(self.ftp).as_posix()
                self.assertIn(f'<link rel="canonical" href="https://bmbuilding.sk/{"" if cesta == "." else cesta + "/"}">', s, p)
        locs = re.findall(r'<loc>([^<]+)</loc>', (self.ftp / 'sitemap.xml').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(locs), 8 + len(self.realizacie))
        self.assertTrue(all(u.startswith('https://bmbuilding.sk/') for u in locs))
        for u in locs:
            self.assertTrue((self.ftp / u[len('https://bmbuilding.sk/'):] / 'index.html').is_file(), u)
        robots = (self.ftp / 'robots.txt').read_text(encoding='utf-8')
        self.assertIn('Sitemap: https://bmbuilding.sk/sitemap.xml', robots)
        self.assertNotIn('Disallow: /', robots)
        llms = (self.ftp / 'llms.txt').read_text(encoding='utf-8')
        self.assertTrue(all(u.startswith('https://bmbuilding.sk/') for u in re.findall(r'\]\((\S+?)\)', llms)))
        s404 = (self.ftp / '404.html').read_text(encoding='utf-8')
        self.assertIn('href="/realizacie/"', s404)
        self.assertIn('src="/app.js?v=', s404)
        self.assertEqual(interne_odkazy(self.ftp, '/'), [])

    def test_1_htaccess_pravidla(self):
        """.htaccess: DirectoryIndex index.html pred index.php, ErrorDocument 404, RewriteEngine mimo IfModule, pravidlá
        len pre doménu a www, výnimka /.well-known/, každá z 32 starých adries práve jedným pravidlom 301 s QSD (a NE pri
        kotve) na cieľ, ktorý vo výstupe je, potom ostatné index.php na úvod, priečinok bez lomky, www, https, 404 pre
        každé meno v koreni mimo zostavy (zoznam presne podľa výstupu), priečinok bez index.html, PHP; hlavičky,
        cache podľa CACHE_APACHE a gzip v mod_filter. HSTS zámerne nie."""
        r = self.ht.splitlines()
        self.assertIn('DirectoryIndex index.html index.php', r)
        self.assertIn('ErrorDocument 404 /404.html', r)
        hlbka, mimo = 0, []
        for x in r:
            hlbka += x.strip().startswith('<IfModule') - (x.strip() == '</IfModule>')
            if x.startswith(('RewriteEngine', 'RewriteRule', 'RewriteCond')):
                mimo.append(hlbka)
        self.assertEqual(set(mimo), {0})
        stare = re.findall(r'^RewriteRule \^(index\\\.php[^ ]*?)/\?\$ (\S+) \[([A-Z=0-9,]+)\]$', self.ht, re.M)
        ocak = [('/' + s.strip('/'), 'https://bmbuilding.sk' + c, 'R=301,L,QSD' + (',NE' if '#' in c else ''))
                for s, c in self.ocakavane_ciele()]
        self.assertEqual([('/' + s.replace('\\.', '.'), c, p) for s, c, p in stare], ocak)
        self.assertEqual(len(stare), len(self.presm))
        self.assertEqual(len({s for s, _, _ in stare}), len(self.presm))
        for _, ciel, _ in ocak:
            cesta = ciel[len('https://bmbuilding.sk/'):].split('#')[0]
            self.assertTrue((self.ftp / cesta / 'index.html').is_file(), ciel)
        poradie = ['RewriteCond %{HTTP_HOST} !^(?:www\\.)?bmbuilding\\.sk\\.?(?::[0-9]+)?$ [NC]', 'RewriteRule ^ - [L]',
                   'RewriteRule ^\\.well-known/ - [L]', 'RewriteRule ^index\\.php/?$ https://bmbuilding.sk/ [R=301,L,QSD]',
                   'RewriteRule ^index\\.php/ https://bmbuilding.sk/ [R=301,L,QSD]',
                   'RewriteRule ^(.+[^/])$ https://bmbuilding.sk/$1/ [R=301,L]', 'RewriteCond %{HTTP_HOST} ^www\\. [NC]',
                   'RewriteCond %{HTTPS} !=on', 'RewriteRule ^([^/]+) - [R=404,L]', 'RewriteRule ^. - [R=404,L]',
                   'RewriteRule \\.ph(?:p[0-9]?|tml|ar)(?:/|$) - [R=404,L]']
        indexy = [r.index(x) for x in poradie]
        self.assertEqual(indexy, sorted(indexy))
        self.assertGreater(r.index(poradie[4]), max(r.index(f'RewriteRule ^{s}/?$ {c} [{p}]') for s, c, p in stare))
        mena = re.search(r'^RewriteCond \$1 !\^\(\?:(.*)\)\$$', self.ht, re.M).group(1).split('|')
        self.assertEqual({m.replace('\\.', '.') for m in mena}, {p.name for p in self.ftp.iterdir()} - {'.htaccess'})
        for h in ('X-Content-Type-Options "nosniff"', 'Referrer-Policy "strict-origin-when-cross-origin"',
                  'X-Frame-Options "SAMEORIGIN"', 'Content-Security-Policy "frame-ancestors \'self\'"'):
            self.assertIn(f'  Header always set {h}', r)
        self.assertNotIn('Strict-Transport-Security', self.ht)
        for typ, cas in build.CACHE_APACHE:
            self.assertIn(f'  ExpiresByType {typ} "{cas}"', r)
        self.assertIn('<IfModule mod_deflate.c>\n  <IfModule mod_filter.c>\n    AddOutputFilterByType DEFLATE text/html', self.ht)
        for typ in ('image/avif .avif', 'image/webp .webp', 'font/woff2 .woff2', 'application/manifest+json .webmanifest'):
            self.assertIn(f'  AddType {typ}', r)
        self.assertFalse(any(c in self.ht for c in '\u2013\u2014'))
        self.assertNotIn('\r', self.ht)

    def test_1_simulacia_htaccess(self):
        """Simulácia Apache nad výstupom (nastroje/htaccess_simulacia.py over): každá stará adresa v 5 variantoch
        (bez lomky, s lomkou, s query, z http://www, z http s query) jedným skokom 301 na https://bmbuilding.sk a cieľ
        je stránka (kotva #cinnost je na úvode), iné index.php na úvod, https a holá doména jedným skokom (query
        bežnej stránky ostane), každý súbor zostavy 200 so svojím súborom, zvyšky Joomly, súbory zostavy pre Pages,
        priečinky bez index.html a PHP 404 s vlastnou stránkou, /.well-known/ bez presmerovania, iná doména bez
        pravidiel, typy, cache a hlavičky. Sabotáž každého pravidla simulácia zachytí."""
        v = self.sim.over(self.ftp)
        zle = [x for x in v if not x['ok']]
        self.assertEqual(zle, [], zle[:3])
        self.assertEqual(sum(1 for x in v if x['popis'] == 'stará adresa'), 5 * len(self.presm))
        self.assertGreater(len(v), len(odtlacky(self.ftp)) + 5 * len(self.presm))
        for meno in self.sim.JOOMLA:
            if meno != 'index.php':
                self.assertTrue(any(x['url'] == f'https://bmbuilding.sk/{meno}' and x['ok'] for x in v), meno)
        https = 'RewriteCond %{HTTPS} !=on\nRewriteRule ^ https://bmbuilding.sk%{REQUEST_URI} [R=301,L]\n'
        straz = 'RewriteCond %{HTTP_HOST} !^(?:www\\.)?bmbuilding\\.sk\\.?(?::[0-9]+)?$ [NC]\nRewriteRule ^ - [L]\n'
        sabotaze = {
            'kotva bez NE': lambda t: bez(t, '#cinnost [R=301,L,QSD,NE]', '#cinnost [R=301,L,QSD]'),
            'bez QSD': lambda t: t.replace(',QSD', ''),
            'https pred starými adresami': lambda t: bez(bez(t, https), 'RewriteRule ^\\.well-known/ - [L]\n',
                                                         'RewriteRule ^\\.well-known/ - [L]\n' + https),
            'bez 404 pre mená mimo zostavy': lambda t: re.sub(r'RewriteCond \$1 [^\n]*\nRewriteRule \^\(\[\^/\]\+\) - \[R=404,L\]\n', '', t),
            'výpis priečinka': lambda t: bez(t, 'RewriteCond %{REQUEST_FILENAME} -d\nRewriteCond %{REQUEST_FILENAME}/index.html !-f\n'
                                                'RewriteRule ^. - [R=404,L]\n'),
            'index.php pred index.html': lambda t: bez(t, 'DirectoryIndex index.html index.php', 'DirectoryIndex index.php index.html'),
            'bez ochrany inej domény': lambda t: bez(t, straz),
            'bez výnimky .well-known': lambda t: bez(t, 'RewriteRule ^\\.well-known/ - [L]\n'),
            'bez typu AVIF': lambda t: bez(t, '  AddType image/avif .avif\n'),
            'zlý cieľ starej adresy': lambda t: bez(t, 'https://bmbuilding.sk/realizacie/rd-pezinok/ [', 'https://bmbuilding.sk/realizacie/ ['),
            'bez nosniff': lambda t: bez(t, '  Header always set X-Content-Type-Options "nosniff"\n'),
            # celé pravidlo 3 aj s podmienkou na mená zostavy (oprava po overení), inak by podmienky ostali pri pravidle 4
            'bez priečinka s lomkou': lambda t: re.sub(r'RewriteCond \$1 \^\(\?:[^\n]*\)\(\?:/\|\$\)\n'
                                                       r'RewriteCond %\{REQUEST_FILENAME\} -d\n'
                                                       r'RewriteCond %\{REQUEST_FILENAME\}/index\.html -f\n'
                                                       r'RewriteRule \^\(\.\+\[\^/\]\)\$ https://bmbuilding\.sk/\$1/ \[R=301,L\]\n', '', t),
        }
        for meno, sab in sabotaze.items():
            text = sab(self.ht)
            self.assertNotEqual(text, self.ht, meno)
            self.assertTrue([x for x in self.sim.over(self.ftp, text=text) if not x['ok']], meno)

    def test_1_simulator_podla_apache(self):
        """Simulácia sama robí to, čo opisuje dokumentácia Apache 2.4: PATH_INFO za súborom index.php sa pripojí
        k ceste pravidla (mod_rewrite v priečinku), bez QSD sa query pripojí a s QSD zahodí, # bez NE sa zakóduje na %23
        (flags.html, NE), reťaz [OR], R=404 s ErrorDocument, DirectorySlash a DirectoryIndex, statický súbor
        s PATH_INFO 404, .ht* 403; neznámu direktívu, príznak alebo vnútorný prepis odmietne."""
        sim = self.sim
        fs = sim.Subory(navyse=['index.php', 'kontakt/index.html', '404.html', 'a.html'])
        ht = sim.Htaccess('\n'.join([
            'ErrorDocument 404 /404.html', 'DirectoryIndex index.html', 'RewriteEngine On',
            'RewriteRule ^index\\.php/kontakt$ https://x.sk/kontakt/ [R=301,L]',
            'RewriteRule ^kotva$ https://x.sk/#c [R=301,L]', 'RewriteRule ^kotva-ne$ https://x.sk/#c [R=301,L,NE]',
            'RewriteRule ^bez-q$ https://x.sk/a [R=301,L,QSD]', 'RewriteRule ^s-q$ https://x.sk/a [R=301,L]',
            'RewriteCond %{HTTP_HOST} ^a\\. [OR]', 'RewriteCond %{HTTP_HOST} ^b\\.', 'RewriteCond %{HTTPS} =on',
            'RewriteRule ^alebo$ https://x.sk/ok [R=301,L]', 'RewriteRule ^skry$ - [R=404,L]', '<IfModule mod_headers.c>',
            '  Header always set X-A "b \'c\'"', '</IfModule>']))
        p = lambda url: ht.poziadavka(url, fs)  # noqa: E731
        self.assertEqual(p('https://x.sk/index.php/kontakt')['location'], 'https://x.sk/kontakt/')
        self.assertEqual(p('https://x.sk/kotva')['location'], 'https://x.sk/%23c')
        self.assertEqual(p('https://x.sk/kotva-ne')['location'], 'https://x.sk/#c')
        self.assertEqual(p('https://x.sk/bez-q?x=1')['location'], 'https://x.sk/a')
        self.assertEqual(p('https://x.sk/s-q?x=1&y=2')['location'], 'https://x.sk/a?x=1&y=2')
        self.assertEqual([p(f'{s}://{h}/alebo')['stav'] for s, h in (('https', 'a.x.sk'), ('https', 'b.x.sk'),
                                                                       ('https', 'c.x.sk'), ('http', 'a.x.sk'))], [301, 301, 404, 404])
        v = p('https://x.sk/skry')
        self.assertEqual((v['stav'], v['subor']), (404, '404.html'))
        self.assertEqual(p('https://x.sk/kontakt')['location'], 'https://x.sk/kontakt/')
        self.assertEqual(p('https://x.sk/kontakt/')['subor'], 'kontakt/index.html')
        self.assertEqual((p('https://x.sk/')['stav'], p('https://x.sk/').get('vypis')), (403, True))
        self.assertTrue(p('https://x.sk/index.php/ine')['php'])
        self.assertEqual(p('https://x.sk/a.html/x')['stav'], 404)
        self.assertEqual(p('https://x.sk/.htaccess')['stav'], 403)
        self.assertIn(('X-A', "b 'c'"), p('https://x.sk/a.html')['hlavicky'])
        self.assertEqual(sim.Htaccess('<IfModule mod_headers.c>\nHeader always set X-A b\n</IfModule>',
                                      chyba_moduly={'mod_headers.c'}).hlavicky, [])
        for zly in ('Options -Indexes', 'RewriteRule ^a$ https://x.sk/ [R=301,QSA]', 'RewriteBase /', 'Redirect 301 / https://x.sk/'):
            with self.assertRaises(ValueError, msg=zly):
                sim.Htaccess(zly)
        with self.assertRaises(ValueError):
            sim.Htaccess('RewriteEngine On\nRewriteRule ^a$ /b [L]').poziadavka('https://x.sk/a', fs)

    def test_1_kontrola_po_nahrati_proti_simulacii(self):
        """nastroje/kontrola_spustenia.py (kontrola bmbuilding.sk po nahratí) proti serveru simulácie: 0 chýb pri
        správnom .htaccess, chyba pri pokazenom (https alebo QSD). Spojenia idú len na 127.0.0.1, bmbuilding.sk sa
        nevolá."""
        import socket
        kontrola = nastroj('kontrola_spustenia')
        povodne = socket.create_connection

        def len_lokalne(adresa, *a, **k):
            self.assertEqual(adresa[0], '127.0.0.1', adresa)
            return povodne(adresa, *a, **k)

        def beh(text=None):
            srv = self.sim.spusti_server(self.ftp, text)
            out = io.StringIO()
            try:
                with mock.patch.object(socket, 'create_connection', len_lokalne), redirect_stdout(out):
                    kod = kontrola.main(['--web', str(self.ftp), '--lokalne', str(srv.server_address[1])])
            finally:
                srv.shutdown()
                srv.server_close()
            return kod, out.getvalue()

        kod, out = beh()
        self.assertEqual(kod, 0, [r for r in out.splitlines() if not r.startswith('OK')][:5])
        suhrn = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((suhrn['chyb'], suhrn['pozor']), (0, 0))
        self.assertGreater(suhrn['kontrol'], 2 * len(self.presm) + 8 + len(self.realizacie))
        self.assertIn(f'OK    staré adresy v .htaccess: {len(self.presm)}', out)
        for sab in (lambda t: bez(t, 'RewriteCond %{HTTPS} !=on\n', 'RewriteCond %{HTTPS} =nikdy\n'),
                    lambda t: t.replace(',QSD', '')):
            kod, out = beh(sab(self.ht))
            self.assertEqual(kod, 1)
            self.assertIn('CHYBA ', out)
        self.assertEqual(kontrola.main(['--web', str(self.ostry), '--lokalne', '1']), 2)

    def test_1_ochrana_udajov_webhouse(self):
        """Čl. 13 GDPR podľa hostingu: pri FTP na WebHouse časť Hosting uvádza WebHouse, s. r. o. (meno, sídlo a IČO z jeho
        informácií o ochrane osobných údajov), čo zaznamenáva, účel a právny základ čl. 6 ods. 1 písm. f), príjemcu
        a sprostredkovateľa (DPA čl. 1 a 45), uchovávanie (bod 7) a miesto spracúvania s prenosom (DPA čl. 29 a 30),
        zdroje na helpdesk.webhouse.sk, bez GitHubu a USA; pri GitHub Pages ostáva text ako doteraz. Bez overených
        údajov o WebHouse zostava pre FTP zlyhá."""
        w = self.texty['ochrana']['webhouse']
        self.assertEqual((w['meno'], w['adresa'], w['ico']), ('WebHouse, s. r. o.', 'Paulínska 20, 917 01 Trnava', '36 743 852'))
        self.assertTrue(w['dpa_url'].startswith('https://helpdesk.webhouse.sk/742599-'))
        self.assertTrue(w['ochrana_url'].startswith('https://helpdesk.webhouse.sk/333948-'))
        stranka = (self.ftp / 'ochrana-udajov' / 'index.html').read_text(encoding='utf-8')
        hosting = stranka.split('<h2 class="nadpis-maly">Hosting</h2>', 1)[1].split('<h2', 1)[0]
        text = html.unescape(re.sub(r'<[^>]+>', ' ', hosting)).replace('\u00a0', ' ')
        for kus in ('beží na webhostingu spoločnosti WebHouse, s. r. o., Paulínska 20, 917 01 Trnava, IČO 36 743 852',
                    'IP adresu návštevníka', 'čl. 6 ods. 1 písm. f) nariadenia (EÚ) 2016/679',
                    'Príjemcom týchto údajov je WebHouse, s. r. o., ako poskytovateľ webhostingu',
                    'ako sprostredkovateľ podľa čl. 28 nariadenia', 'samostatný prevádzkovateľ',
                    'počas obdobia primeraného ich účelu', 'v Európskom hospodárskom priestore',
                    'podmienok kapitoly V nariadenia'):
            self.assertIn(kus, text)
        for nie in ('GitHub', 'USA'):
            self.assertNotIn(nie, stranka)
        zdroje = stranka.split('<p class="poznamka">', 1)[1].split('</p>', 1)[0]
        self.assertIn(f'<a href="{w["dpa_url"]}">', zdroje)
        self.assertIn(f'<a href="{w["ochrana_url"]}">', zdroje)
        self.assertIn('<a href="https://dataprotection.gov.sk/">', zdroje)
        pages = (self.ostry / 'ochrana-udajov' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('beží na službe GitHub Pages spoločnosti GitHub, Inc.', pages)
        self.assertNotIn('WebHouse', pages)
        # bez údajov o WebHouse (alebo s adresou mimo webhouse.sk) zostava pre FTP zlyhá, nič sa nevymýšľa
        povodne = self.w.hosting
        try:
            self.w.hosting = 'apache'
            for zle in ({}, dict(w, ico=''), dict(w, dpa_url='https://example.com/dpa'), dict(w, ochrana_url='http://helpdesk.webhouse.sk/x')):
                with self.assertRaises(build.ChybaZostavy):
                    self.w.ochrana_hosting(dict(self.texty['ochrana'], webhouse=zle))
        finally:
            self.w.hosting = povodne

    def test_1_prepinac_hostingu_a_poistky(self):
        """Prepínač --hosting má prednosť pred premennou BMB_HOSTING; neznámy hosting, apache s náhľadom (aj v GitHub
        Actions bez BMB_OSTRY=ano), výstup v repozitári, zostava pre GitHub Pages alebo cudzí súbor v priečinku pre FTP
        skončia kódom 2 ešte pred zostavou, nič sa nezapíše. Opakovaná FTP zostava do toho istého priečinka nič
        nezmení a cudzí súbor v ňom zastaví ďalší beh."""
        tmp = Path(tempfile.mkdtemp(prefix='bmb-test-hosting-'))
        try:
            for argv, prostredie, hosting, hlaska in (
                    ({'nahlad': True}, None, 'apache', 'len pre ostrý web'),
                    ({}, {'BMB_HOSTING': 'ftp'}, None, 'nepoznám'),
                    ({}, {'BMB_HOSTING': 'apache', 'GITHUB_ACTIONS': 'true'}, None, 'len pre ostrý web')):
                kod, _, err = postav(tmp / 'x', prostredie=prostredie, hosting=hosting, **argv)
                self.assertEqual(kod, 2, err)
                self.assertIn(hlaska, err)
                self.assertFalse((tmp / 'x').exists())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build.main(['--hosting', 'nginx', '--vystup', str(tmp / 'x')])
            v_repo = KOREN / 'test-ftp-nesmie-vzniknut'
            kod, _, err = postav(v_repo, hosting='apache')
            self.assertEqual(kod, 2)
            self.assertIn('mimo repozitára', err)
            self.assertFalse(v_repo.exists())
            pages = tmp / 'pages'
            pages.mkdir()
            (pages / build.MANIFEST).write_text('{"verzia": 1, "subory": {}}', encoding='utf-8')
            kod, _, err = postav(pages, prostredie={'BMB_HOSTING': 'apache'})
            self.assertEqual(kod, 2)
            self.assertIn('GitHub Pages', err)
            cudzi = tmp / 'cudzi'
            cudzi.mkdir()
            (cudzi / 'stary-web.html').write_text('x', encoding='utf-8')
            kod, _, err = postav(cudzi, hosting='apache')
            self.assertEqual(kod, 2)
            self.assertIn('stary-web.html', err)
            self.assertEqual(build.manifest_ftp((tmp / 'novy').resolve()), ((tmp / ('novy' + build.MANIFEST)).resolve(), ''))
            for chyba in ({'hosting': 'nginx'}, {'hosting': 'apache', 'nahlad': True}):
                with self.assertRaises(build.ChybaZostavy):
                    build.Web(KOREN / 'obsah', chyba.get('nahlad', False), hosting=chyba['hosting'])
            # opakovaná zostava do kópie FTP výstupu so zoznamom vedľa: nič nové, rovnaké bajty; cudzí súbor ju zastaví
            kopia = tmp / 'web-ftp'
            shutil.copytree(self.ftp, kopia)
            shutil.copyfile(self.tmp / ('web-ftp' + build.MANIFEST), tmp / ('web-ftp' + build.MANIFEST))
            kod, out, err = postav(kopia, rychle=True, hosting='apache', prostredie={'BMB_HOSTING': 'pages'})
            self.assertEqual(kod, 0, err)
            self.assertIn('(ostrý, hosting apache)', out)
            self.assertIn('nových 0', out)
            self.assertEqual(odtlacky(kopia), odtlacky(self.ftp))
            (kopia / 'obrazky' / 'pridane-rucne.jpg').write_bytes(b'x')
            kod, _, err = postav(kopia, hosting='apache')
            self.assertEqual(kod, 2)
            self.assertIn('obrazky/pridane-rucne.jpg', err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_49_readme_a_obsah_yml_bmb_ostry(self):
        """Položka 49: README netvrdí, že krok Zostaviť web nemá env BMB_OSTRY (obsah.yml ho má); pri GitHub Pages stačí
        premenná repozitára, pri FTP ju nenastavovať. README opisuje --hosting apache, simuláciu a kontrolu po nahratí."""
        readme = (KOREN / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('treba v kroku „Zostaviť web“ workflowu pridať', readme)
        self.assertIn('Krok „Zostaviť web“ v `obsah.yml` už má `env: BMB_OSTRY: ${{ vars.BMB_OSTRY }}`', readme)
        self.assertIn('pri ceste FTP na WebHouse ju nenastavovať', readme)
        for kus in ('--hosting apache', 'BMB_HOSTING=apache', 'nastroje/htaccess_simulacia.py', 'nastroje/kontrola_spustenia.py',
                    'SPUSTENIE-FTP.md', 'TestHostingApache'):
            self.assertIn(kus, readme)
        yml = (KOREN / '.github' / 'workflows' / 'obsah.yml').read_text(encoding='utf-8')
        krok = yml.split('- name: Zostaviť web', 1)[1].split('- name:', 1)[0]
        self.assertIn('env:\n          BMB_OSTRY: ${{ vars.BMB_OSTRY }}\n        run: python build.py\n', krok)
        self.assertNotIn('BMB_HOSTING', yml)


class TestOpravaPoOvereni(unittest.TestCase):
    """Oprava po overení opráv auditu naplno (25. 11. 2026, kontroly dizajnu, prehliadača, zadania a spustenia): box
    s dokladom na stránkach služieb, titulná fotka detailu hneď pod H1 pri každej šírke, rytmus a karty stránok služieb,
    text-wrap, fokus po Zrušiť filter, údaje hrdinu pri 1100 až 1119 px s posuvníkom, alt tabuľky ASB, a pri ceste FTP
    commit v zozname zostavy, pravidlo 3 .htaccess len pre mená zostavy, podpožiadavka DirectoryIndex v simulácii,
    server simulácie a skúška .htaccess. Merania v Chrome: ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/
    AUDIT-OPRAVY.md, časť Oprava po overení."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.w = s['ostry'], s['web']
        cls.css = re.sub(r'/\*.*?\*/', '', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'), flags=re.S)
        cls.zaklad = css_bez_blokov(cls.css)
        cls.firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    def blok(self, hlavicka):
        return ''.join(css_bloky(self.css, hlavicka))

    def test_box_ciarka_v_celku_a_cislo_bez_zalomenia(self):
        """Nález vážne (dizajn): v boxe Stavebného manažmentu bol span.nz okolo celého „evidenčné číslo …“ a čiarka mimo
        neho; pri 1024 až 1064 px stála čiarka sama na riadku, pri 1104 až 1248 px ňou riadok začínal a pri 1024 až
        1096 px išlo číslo cez pravý rámik boxu (o 3 až 28 px), pri 400 px vodorovný posun stránky 7 px. Teraz je
        nezalomiteľné len „číslo 00912*10*,“ aj s čiarkou a dátum (pevné medzery zo sadzby). Chrome po 8 až 16 px pri
        320 až 1920 px: pretečení 51 na 0, riadkov s čiarkou 67 na 0."""
        telo = self.info('stavebny-manazment/')['telo']
        box = telo.split('<aside class="kalkulacka sluzba-bok"', 1)[1].split('</aside>', 1)[0]
        dd = re.findall(r'<div><dt>([^<]+)</dt><dd>(.*?)</dd></div>', box)
        o = self.firma['opravnenia']
        riadky = {k: v for k, v in dd}
        # dátum má pevné medzery zo sadzby (T), v testoch bez spätnej lomky vo výraze f-reťazca (Python pred 3.12)
        datumy = [x['datum_vydania_text'].replace(' ', '\u00a0') for x in o[:2]]
        self.assertEqual(riadky['Stavbyvedúci'],
                         f'{o[0]["odborne_zameranie"]}, evidenčné <span class="nz">číslo {o[0]["evidencne_cislo"]},</span> '
                         f'vydané {datumy[0]}')
        self.assertEqual(riadky['Stavebný dozor'],
                         f'evidenčné <span class="nz">číslo {o[1]["evidencne_cislo"]},</span> vydané {datumy[1]}')
        for k, v in dd:
            self.assertNotIn('</span>,', v, k)
            self.assertNotRegex(v, r'^\s*,', k)
            self.assertNotIn('<span class="nz">evidenčné', v, k)
            for nz in re.findall(r'<span class="nz">(.*?)</span>', v):
                self.assertRegex(nz, r'^číslo \S+,?$', k)
        # bez čiarky za posledným kusom, žiadne dve čiarky
        self.assertFalse(any(re.sub(r'<[^>]+>', '', v).rstrip().endswith(',') or ',,' in v for _, v in dd))

    def test_box_stitok_nad_hodnotou_do_1279(self):
        """Nález vážne (dizajn): vedľa štítka mala hodnota v boxe s dokladom pri 1024 až 1279 px (box 5 z 12 stĺpcov)
        147 až 199 px a lámala sa po slove („Slovenská / komora / stavebných / inžinierov (SKSI)“), pri 400 až 1023 px
        166 až 236 px. Do 1279 px je štítok nad hodnotou ako do 399 px (položka 23), od 1280 px vedľa seba. Chrome po 8
        až 16 px: riadkov s jedným slovom vo viacriadkovej hodnote pri 400 až 1279 px 186 na 0, najužšia hodnota
        313 px (predtým 162 px)."""
        self.assertIn('.sluzba-bok .fakty>div{grid-template-columns:minmax(0,1fr);row-gap:var(--m-4)}',
                      self.blok('@media (max-width:1279px)'))
        for hlavicka in ('@media (min-width:1280px)', '@media (min-width:1921px)'):
            self.assertNotIn('.sluzba-bok .fakty>div{', self.blok(hlavicka), hlavicka)
        self.assertEqual(deklaracie(self.zaklad, '.fakty>div').get('grid-template-columns'), 'minmax(7.5em,32%) minmax(0,1fr)')
        # položka 23 ostáva (údaje detailu do 399 px)
        self.assertIn('.sluzba-bok .fakty>div,.fakty.udaje>div{grid-template-columns:minmax(0,1fr);row-gap:var(--m-4)}',
                      self.blok('@media (max-width:399px)'))

    def test_detail_fotka_pod_h1_a_sizes(self):
        """Nálezy vážne (dizajn): titulná fotka detailu je hneď pod H1 pri každej šírke; pri 1024 až 1279 px cez celú
        šírku a sizes to vie (prehliadač nevyberie súbor pre 7 stĺpcov): sizes pri 1024 až 1279 px nikdy menší ako
        šírka obsahu, od 1280 px 7 stĺpcov ako SIZES_DETAIL. Chrome nad 26 detailmi: prázdne miesto pod fotkou pri 1024
        až 1279 px 0 (predtým až 595 px), pri 1366 px najviac 173 (predtým 264), riadkov s jedným slovom v hodnotách
        pri 1024 px 74 na 2; pri 360 až 412 px celá fotka v prvej obrazovke pri všetkých 26 (predtým pri 390 x 844
        priemer 38 % a 11 detailov bez fotky)."""
        n = 0
        for r in self.w.realizacie:
            telo = self.info(f'realizacie/{r["slug"]}/')['telo']
            if 'detail-s-udajmi' not in telo:
                continue
            n += 1
            dvoj = telo.split('<div class="obsah mriezka detail detail-s-udajmi">', 1)[1]
            self.assertLess(dvoj.index('<figure class="pohlad detail-foto">'), dvoj.index('<div class="detail-udaje">'), r['slug'])
            self.assertLess(dvoj.index('<div class="detail-udaje">'), dvoj.index('<div class="detail-text'), r['slug'])
        self.assertEqual(n, len([r for r in self.w.realizacie if r.get('karta')]))
        casti = build._casti_sizes(build.SIZES_DETAIL_UDAJE)
        self.assertEqual(casti[0], build._casti_sizes(build.SIZES_DETAIL)[0])
        for vw in range(1024, 1280):
            obsah = build.sirka_obsahu_px(vw)
            self.assertGreaterEqual(vyhodnot_sizes(build.SIZES_DETAIL_UDAJE, vw, 800), obsah, vw)
            self.assertLessEqual(vyhodnot_sizes(build.SIZES_DETAIL_UDAJE, vw, 800), obsah + 40, vw)
        for vw in (1280, 1366, 1440, 1920, 2560, 400, 800):
            self.assertEqual(vyhodnot_sizes(build.SIZES_DETAIL_UDAJE, vw, 800), vyhodnot_sizes(build.SIZES_DETAIL, vw, 800), vw)

    def test_rytmus_a_karty_sluzieb_do_1023(self):
        """Nálezy drobné (dizajn): do 1023 px bolo medzi boxom alebo odkazom Všetky realizácie a ďalším nadpisom 176 až
        197 px, teraz 96 px ako na úvode (Chrome pri 390 a 768 px: /stavebny-dozor/ 176 na 96 px); karty v jednom
        stĺpci (do 479 px) bez rezervy druhého riadka názvu (RD Ahoj 44 na 22 px ako na /realizacie/)."""
        mobil = self.blok('@media (max-width:1023px)')
        self.assertIn('.sluzba-obal{padding-bottom:var(--m-48)}', mobil)
        self.assertIn('.sluzba-obal~.sekcia-karty,.sluzba-obal~.otazky-sekcia{padding-block:var(--m-48)}', mobil)
        self.assertIn('.karty>.projekt h3{min-height:0}', self.blok('@media (max-width:479px)'))
        self.assertEqual(deklaracie(self.zaklad, '.projekt h3').get('min-height'), 'calc(2 * 1.15em)')

    def test_text_wrap_bez_osameleho_slova(self):
        """Nálezy drobné (dizajn, prehliadač, zadanie): úvodný odsek stránok, riadok Zdroj pod otázkami zo zákona
        a pri Obchodnom registri text-wrap:pretty („známe.“, „Bulkovi.“, „h)“, „písm. b)“, „SR“ samé na riadku),
        hodnoty v tabuľke údajov detailu text-wrap:balance (pri 1440 px 32 hodnôt s jedným slovom na poslednom riadku,
        Chrome po oprave 2 pri každej šírke 1024 až 1920 px)."""
        self.assertEqual(deklaracie(self.zaklad, '.uvodny').get('text-wrap'), 'pretty')
        self.assertEqual(deklaracie(self.zaklad, '.otazka .zdroj').get('text-wrap'), 'pretty')
        self.assertEqual(deklaracie(self.zaklad, '.register .cena small').get('text-wrap'), 'pretty')
        self.assertEqual(deklaracie(self.zaklad, '.udaje dd').get('text-wrap'), 'balance')
        # vložený štýl úvodu má pravidlá úvodného odseku aj zdroja (otázky zo zákona sú aj na úvode)
        uvod = self.info('')['html'].split('</style>', 1)[0]
        self.assertIn('.otazka .zdroj{font-size:14px;color:var(--tlmeny);padding-bottom:20px;text-wrap:pretty}', uvod)

    def test_zrusit_filter_fokus_na_vsetky(self):
        """Nález drobné (prehliadač, WCAG 2.4.3): po Zrušiť filter (tlačidlo v prázdnom výsledku, ktorý sa skryje) padol
        fokus na body a ďalší Tab išiel až na Kontakt; teraz ide na tlačidlo Všetky. Falošný DOM (zoznam_spravanie.cjs)
        nad zhusteným app.js webu: po zrušení fokus na data-druh="" a výber zrušený; sabotáž bez presunu fokusu
        chyby_zoznamu zachytí."""
        d = data_zoznamu(self.ostry)
        druh = next(x for x in d['druhy'] if x)
        _, stavy = spravanie_zoznamu(self.ostry, [{'akcia': 'druh', 'hodnota': druh}, {'akcia': 'zrusit'}])
        self.assertIsNone(stavy[1]['fokus_druh'])
        self.assertEqual(stavy[2]['fokus_druh'], '')
        self.assertEqual(chyby_zoznamu(self.ostry), [])
        js = (self.ostry / 'app.js').read_text(encoding='utf-8')
        kus = "var vsetkyDruhy = filtre.querySelector('[data-druh=\"\"]');\nif (vsetkyDruhy) vsetkyDruhy.focus();\n"
        self.assertIn(kus, js)
        with tempfile.TemporaryDirectory() as t:
            zly = Path(t) / 'app.js'
            zly.write_text(js.replace(kus, ''), encoding='utf-8')
            self.assertTrue(any('fokus na tlačidle filtra' in c for c in chyby_zoznamu(self.ostry, app_js=zly)))

    def test_hrdina_udaje_bez_odsadenia_do_1119(self):
        """Nález drobné (prehliadač): okno 1100 až 1114 px s klasickým posuvníkom 17 px má obsah ako 1083 až 1097 px
        bez neho, hodnota údajov hrdinu s odsadením 34 px mala tri riadky a pri výške 556 až 568 px boli tlačidlá
        Zavolať a Napísať e-mail 4 až 7 px pod oknom. Hodnota bez odsadenia platí do 1119 px (o posuvník nad 1099 px),
        ostatné pravidlá úzkeho počítača ostávajú do 1099 px."""
        self.assertIn('.udaj dd{margin-left:0}', self.blok('@media (min-width:1024px) and (max-width:1119px)'))
        self.assertNotIn('.udaj dd{margin-left:0}', self.blok('@media (min-width:1024px) and (max-width:1099px)'))
        self.assertLess(self.css.index('@media (min-width:1024px){'), self.css.index('@media (min-width:1024px) and (max-width:1119px){'))
        self.assertIn('.udaj dd{margin-left:0}', self.info('')['html'].split('</style>', 1)[0])

    def test_ftp_commit_v_zozname_zostavy(self):
        """Nález vážne (spustenie): postup FTP stavia z disku, ktorý nemusí byť verzia schváleného náhľadu. build.py pri
        hostingu apache zapíše commit (len čítanie .git, git sa nespúšťa) do zoznamu zostavy vedľa výstupu a vypíše
        ho; zoznam zostavy pre GitHub Pages ho nemá. commit_repozitara: referencia, packed-refs, odpojený HEAD,
        neplatný HEAD a repozitár bez .git."""
        f = ftp_zostava()
        kod, out, err = f['o']
        self.assertEqual(kod, 0, err)
        zoznam = json.loads((f['tmp'] / ('web-ftp' + build.MANIFEST)).read_text(encoding='utf-8'))
        commit = build.commit_repozitara()
        self.assertEqual(zoznam['commit'], commit or 'neznámy')
        self.assertIn(f'commit: {commit}' if commit else 'commit: neznámy', out)
        self.assertNotIn('commit', json.loads((self.ostry / build.MANIFEST).read_text(encoding='utf-8')))
        sha1, sha2 = 'a' * 40, '0123456789abcdef' * 2 + '01234567'
        with tempfile.TemporaryDirectory() as t:
            k = Path(t)
            self.assertEqual(build.commit_repozitara(k), '')
            g = k / '.git'
            (g / 'refs' / 'heads').mkdir(parents=True)
            (g / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
            self.assertEqual(build.commit_repozitara(k), '')
            (g / 'packed-refs').write_text(f'# pack-refs with: peeled\n{sha2} refs/heads/main\n', encoding='utf-8')
            self.assertEqual(build.commit_repozitara(k), sha2)
            (g / 'refs' / 'heads' / 'main').write_text(sha1 + '\n', encoding='utf-8')
            self.assertEqual(build.commit_repozitara(k), sha1)
            (g / 'HEAD').write_text(sha2 + '\n', encoding='utf-8')
            self.assertEqual(build.commit_repozitara(k), sha2)
            for zly in ('ref: refs/../../x\n', 'nieco\n', 'ref: refs/heads/ina\n'):
                (g / 'HEAD').write_text(zly, encoding='utf-8')
                self.assertEqual(build.commit_repozitara(k), '', zly)

    def test_ftp_pravidlo3_len_mena_zostavy(self):
        """Nález drobné (spustenie): priečinok Joomly s prázdnym index.html bez lomky (/images, /cache) dostal 301 na
        adresu s lomkou a až potom 404 (prezrádza Joomlu, skok navyše). Pravidlo 3 má podmienku na mená v koreni
        zostavy, tie isté ako pravidlo 5; simulácia: /images a /cache hneď 404, stránka bez lomky ďalej 301."""
        f = ftp_zostava()
        ht = (f['vystup'] / '.htaccess').read_text(encoding='utf-8')
        sim = nastroj('htaccess_simulacia')
        r = ht.splitlines()
        i = r.index('RewriteRule ^(.+[^/])$ https://bmbuilding.sk/$1/ [R=301,L]')
        self.assertEqual(r[i - 2:i], ['RewriteCond %{REQUEST_FILENAME} -d', 'RewriteCond %{REQUEST_FILENAME}/index.html -f'])
        m3 = re.fullmatch(r'RewriteCond \$1 \^\(\?:(.*)\)\(\?:/\|\$\)', r[i - 3])
        m5 = re.search(r'^RewriteCond \$1 !\^\(\?:(.*)\)\$$', ht, re.M)
        self.assertTrue(m3 and m5, r[i - 3])
        self.assertEqual(m3.group(1), m5.group(1))
        fs = sim.Subory(f['vystup'], list(sim.JOOMLA))
        h = sim.Htaccess(ht)
        for cesta in ('images', 'cache'):
            v = h.poziadavka(f'https://bmbuilding.sk/{cesta}', fs)
            self.assertEqual((v['stav'], v.get('location')), (404, None), cesta)
        v = h.poziadavka('https://bmbuilding.sk/kontakt', fs)
        self.assertEqual((v['stav'], v['location']), (301, 'https://bmbuilding.sk/kontakt/'))
        # sabotáž: bez podmienky na mená zostavy simulácia aj kontrola po nahratí /images zachytia
        zle = ht.replace(r[i - 3] + '\n', '')
        self.assertEqual(sim.Htaccess(zle).poziadavka('https://bmbuilding.sk/images', fs)['stav'], 301)
        self.assertTrue(any(not x['ok'] and x['url'].endswith('/images') for x in sim.over(f['vystup'], zle)))
        kontrola = nastroj('kontrola_spustenia')
        self.assertIn('/images', kontrola.NEVEREJNE)
        self.assertIn('/cache', kontrola.NEVEREJNE)

    def test_simulacia_podpoziadavka_bez_pravidiel_r(self):
        """Nález drobné (spustenie): Apache 2.4 v podpožiadavke DirectoryIndex vynechá každé pravidlo s príznakom R, aj
        R=404 (mod_rewrite.c apply_rewrite_list: r->main a RULEFLAG_FORCEREDIRECT). Bez index.html v koreni by teda
        na https://bmbuilding.sk/ bežal index.php Joomly; simulácia to teraz ukáže (predtým 301), s index.html úvod.
        Postup preto nahráva index.html pred .htaccess."""
        sim = nastroj('htaccess_simulacia')
        h = sim.Htaccess('DirectoryIndex index.html index.php\nRewriteEngine On\nRewriteRule \\.php$ - [R=404,L]\n')
        fs = sim.Subory(navyse=['index.php'])
        self.assertTrue(h.poziadavka('https://x.sk/', fs)['php'])
        self.assertEqual(h.poziadavka('https://x.sk/index.php', fs)['stav'], 404)
        fs.pridaj('index.html')
        v = h.poziadavka('https://x.sk/', fs)
        self.assertEqual((v['stav'], v['subor'], v['php']), (200, 'index.html', False))
        f = ftp_zostava()
        ht = (f['vystup'] / '.htaccess').read_text(encoding='utf-8')
        fs = sim.Subory(f['vystup'], list(sim.JOOMLA))
        self.assertEqual(sim.Htaccess(ht).poziadavka('https://bmbuilding.sk/', fs)['subor'], 'index.html')
        fs.subory.discard('index.html')
        v = sim.Htaccess(ht).poziadavka('https://bmbuilding.sk/', fs)
        self.assertTrue(v['php'], v)
        postup = (KOREN.parents[1] / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding' / 'kolo-1' / 'SPUSTENIE-FTP.md')
        if postup.is_file():
            t = postup.read_text(encoding='utf-8')
            self.assertLess(t.index('1. `index.html`,'), t.index('3. nakoniec `.htaccess`'))

    def test_simulacia_server_z_prikazoveho_riadka(self):
        """Nález drobné (spustenie): kontrola na sucho s --lokalne PORT potrebovala server simulácie, ktorý sa z príkazového
        riadka spustiť nedal. htaccess_simulacia.py --server vypíše port a príkaz kontroly a odpovedá len na 127.0.0.1."""
        import http.client
        import subprocess
        f = ftp_zostava()
        proc = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(KOREN / 'nastroje' / 'htaccess_simulacia.py'),
                                 '--web', str(f['vystup']), '--server'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8')
        try:
            riadok = json.loads(proc.stdout.readline())
            self.assertIn('--lokalne', riadok['prikaz'])
            c = http.client.HTTPConnection('127.0.0.1', riadok['port'], timeout=20)
            c.request('GET', '/', headers={'Host': 'bmbuilding.sk', 'X-Simulacia-Schema': 'https'})
            o = c.getresponse()
            self.assertEqual(o.status, 200)
            self.assertIn(b'<html', o.read())
            c.request('GET', '/images', headers={'Host': 'bmbuilding.sk', 'X-Simulacia-Schema': 'https'})
            o = c.getresponse()
            o.read()
            self.assertEqual(o.status, 404)
            c.close()
        finally:
            proc.terminate()
            proc.wait(timeout=20)
            proc.stdout.close()
            proc.stderr.close()

    def test_skuska_htaccess(self):
        """Nález drobné (spustenie): či WebHouse v .htaccess dovolí DirectoryIndex a ExpiresByType (AllowOverride
        Indexes) a ostatné direktívy (FileInfo), sa inak ukáže až pri prepnutí chybou 500. nastroje/htaccess_skuska.py
        vyrobí priečinok skuska-htaccess: .htaccess s tými istými direktívami okrem pravidiel prepisu, DirectoryIndex
        skuska.html a skuska.html; simulácia ho prijme a v koreni skúšky ukáže skuska.html. Nástroj odmietne iné meno
        priečinka, výstup v repozitári alebo cudzie súbory."""
        f = ftp_zostava()
        skuska = nastroj('htaccess_skuska')
        sim = nastroj('htaccess_simulacia')
        ht = (f['vystup'] / '.htaccess').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as t, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            v = Path(t) / 'skuska-htaccess'
            self.assertEqual(skuska.main(['--web', str(f['vystup']), '--vystup', str(v)]), 0)
            self.assertEqual(sorted(p.name for p in v.iterdir()), ['.htaccess', 'skuska.html'])
            text = (v / '.htaccess').read_text(encoding='utf-8')
            self.assertIn('DirectoryIndex skuska.html', text)
            self.assertNotIn('RewriteCond', text)
            self.assertEqual(re.findall(r'^RewriteRule .*$', text, re.M), ['RewriteRule ^nikdy-nepouzite-pravidlo-skusky$ - [R=404,L]'])
            direktivy = lambda x: {r.split()[0] for r in x.splitlines() if r.strip() and not r.strip().startswith('#')}  # noqa: E731
            self.assertEqual(direktivy(ht) - {'RewriteCond'}, direktivy(text))
            for riadok in ht.splitlines():
                if riadok.strip() and not riadok.lstrip().startswith(('#', 'RewriteCond', 'RewriteRule', 'DirectoryIndex')):
                    self.assertIn(riadok, text.splitlines(), riadok)
            fs = sim.Subory(v, ['404.html'])
            o = sim.Htaccess(text).poziadavka('https://bmbuilding.sk/', fs)
            self.assertEqual((o['stav'], o['subor']), (200, 'skuska.html'))
            self.assertIn(('Content-Type', 'text/html; charset=UTF-8'), o['hlavicky'])
            self.assertIn('v poriadku', (v / 'skuska.html').read_text(encoding='utf-8'))
            # druhý beh do toho istého priečinka prejde, iné meno, cudzí súbor a výstup v repozitári nie
            self.assertEqual(skuska.main(['--web', str(f['vystup']), '--vystup', str(v)]), 0)
            self.assertEqual(skuska.main(['--web', str(f['vystup']), '--vystup', str(Path(t) / 'ine-meno')]), 2)
            (v / 'cudzi.txt').write_text('x', encoding='utf-8')
            self.assertEqual(skuska.main(['--web', str(f['vystup']), '--vystup', str(v)]), 2)
            self.assertEqual(skuska.main(['--web', str(f['vystup']), '--vystup', str(KOREN / 'skuska-htaccess')]), 2)
            self.assertFalse((KOREN / 'skuska-htaccess').exists())

    def test_dokumenty_po_overeni(self):
        """Nálezy (zadanie, spustenie): README r. 7 netvrdí „prázdnu kótu“ (oprava položky 15) ani popisok Rok pri jednom
        roku (od 26. 11. 2026 vždy Roky, roky všade a bez nich rok príspevku, ROKY.md),
        README opisuje commit v zozname zostavy, server simulácie a skúšku .htaccess; postup SPUSTENIE-FTP.md (len keď je
        repozitár ARLing vedľa, v GitHub Actions nie je) má bránu commitu, zálohu mimo priečinkov AI, skúšku .htaccess,
        poradie prepnutia, návrat s robots.txt, sitemap.xml a llms.txt a správu Viliamovi pre cestu FTP."""
        readme = (KOREN / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('karta bez rokov má prázdnu kótu', readme)
        self.assertNotIn('(popisok Roky, bez slova príspevok', readme)
        self.assertNotIn('popisok Rok pri jednom roku a Roky pri rozsahu', readme)
        self.assertNotIn('sa od opravy 1 neukazuje vôbec', readme)
        for kus in ('pod popiskom Roky aj pri jednom roku', 'bez nich rok príspevku', 'len vlasovú čiaru', 'commit_repozitara',
                    'htaccess_simulacia.py --web DIR --server', 'nastroje/htaccess_skuska.py', 'SPUSTENIE-FTP.md časť 1.1'):
            self.assertIn(kus, readme)
        postup = KOREN.parents[1] / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding' / 'kolo-1' / 'SPUSTENIE-FTP.md'
        if not postup.is_file():
            self.skipTest('postup FTP je v repozitári ARLing, nie v products/bmbuilding')
        t = postup.read_text(encoding='utf-8')
        for kus in ('git status --porcelain', 'git rev-parse HEAD origin/main', '**Commit schváleného náhľadu:**',
                    'D:/zalohy-klientov/bmbuilding/RRRR-MM-DD-CCCCCCC', 'nastroje/htaccess_skuska.py', 'súkromnom okne',
                    'Zmaž aj nové `sitemap.xml` a `llms.txt`', '## Správa Viliamovi',
                    '## Rozhodnutie Andreja pred spustením (blokátor)', 'http://ftp.bmbuilding.sk/administrator/',
                    'google1a2b3c4d5e6f.html', '`{"kontrol": 150, "chyb": 0, "pozor": 0, ...}`'):
            self.assertIn(kus, t)
        self.assertNotIn('zaloha-stareho-webu', t)
        self.assertFalse(any(z in t for z in ('\u2013', '\u2014')))

    def test_ftp_ochrana_zdroj_body_1_a_7(self):
        """Nález drobné (spustenie): meno, adresa a IČO WebHouse sú z bodu 1 jeho stránky Ochrana osobných údajov, zdroj
        pod textom uvádzal len bod 7."""
        f = ftp_zostava()
        t = (f['vystup'] / 'ochrana-udajov' / 'index.html').read_text(encoding='utf-8').replace('\u00a0', ' ')
        self.assertIn('WebHouse, Ochrana osobných údajov, body 1 a 7', t)
        self.assertNotIn('Ochrana osobných údajov, bod 7<', t)


class TestRokyAkoPredtym(unittest.TestCase):
    """Andrej 26. 11. 2026 pri snímke /realizacie/, kde RD Ahoj, Vila Holubyho a Dom pod lesom nemali rok a len UFO
    mal na kóte 2025: „prečo tam nie su tie roky? veď tie roky som tam chcel všade, ale len takto napísané, že roky
    xxx tak ako predtým. aj tuto aj na stranke projektov“. Každá realizácia má rok všade, kde web roky ukazuje
    (build.py roky_karty): roky stavby, bez nich rok príspevku (rok_prispevku, inak rok z poľa datum), bez slova
    príspevok, mesiaca a dňa; popisok v detaile vždy Roky. Záznam ops/weby-na-mieru/klienti/bmbuilding/kolo-1/
    brana-3/ROKY.md."""

    # realizácie bez rokov stavby v obsahu 26. 11. 2026 a ich rok príspevku (rok_prispevku, rovnaký ako rok poľa datum)
    BEZ_ROKOV_STAVBY = {'digitalizacne-pracovisko-sfu-spitalska-bratislava': '2024', 'dom-pod-lesom': '2024',
                        'rd-ahoj-bratislava-nove-mesto': '2026', 'rd-devin-bratislava': '2024',
                        'rd-sklenarska-bratislava': '2024', 'rekonstrukcia-vila-holubyho-bratislava': '2026'}
    ROKY_RE = re.compile(r'^(?:19|20)\d{2}(?: až (?:19|20)\d{2})?$')
    DATUM_RE = re.compile(r'\b(?:19|20)\d{2}-\d{2}(?:-\d{2})?\b')

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry, cls.w, cls.obsah = s['ostry'], s['web'], s['realizacie']
        cls.sluzby = list(json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))['sluzby'])

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    @staticmethod
    def text(s):
        return html.unescape(re.sub(r'<[^>]+>', '', s)).replace('\u00a0', ' ').strip()

    def karty(self, cesta):
        """slug -> (atribúty li, obsah figure) kariet realizácií na stránke cesta."""
        out = {}
        for atr, obsah in re.findall(r'<li class="dlazdica[^"]*"([^>]*)><figure>(.*?)</figure></li>',
                                     self.info(cesta)['telo'], re.S):
            out[re.search(r'realizacie/([a-z0-9-]+)/', obsah).group(1)] = (atr, obsah)
        return out

    def kota_karty(self, obsah):
        """Text kóty karty: statická kóta zoznamu alebo skrytý text kóty s valcami (stránky služieb); None bez kóty."""
        m = (re.search(r'<span class="kota"><span class="kota-text">(.*?)</span></span>', obsah)
             or re.search(r'<span class="kota"><span class="kota-text otoc"><span class="vh">(.*?)</span>', obsah))
        return self.text(m.group(1)) if m else None

    def udaje_detailu(self, telo):
        dl = telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]
        return {self.text(k): self.text(v) for k, v in re.findall(r'<dt>(.*?)</dt><dd>(.*?)</dd>', dl, re.S)}

    def dalsie(self, cesta):
        """slug -> text údajov v zozname Ďalšie realizácie na stránke služby."""
        telo = self.info(cesta)['telo']
        if '<ul class="zoznam-dalsie">' not in telo:
            return {}
        z = telo.split('<ul class="zoznam-dalsie">', 1)[1].split('</ul>', 1)[0]
        return {s: self.text(u) for s, u in
                re.findall(r'<li><a href="[^"]*realizacie/([a-z0-9-]+)/">.*?</a><span>(.*?)</span></li>', z)}

    def test_kazda_realizacia_ma_rok_na_karte_a_v_detaile(self):
        """Každá zverejnená realizácia (26 v obsahu 26. 11. 2026) má rok na karte /realizacie/ (kóta, data-rok) aj
        v detaile: kóta nad textom a tabuľka údajov pod popiskom Roky aj pri jednom roku, nikde Rok ani Rok
        príspevku; description má vetu Roky, keď sa zmestí do 155 znakov (rovnako ako pri rokoch stavby)."""
        self.assertEqual(sorted(r['slug'] for r in self.w.realizacie), sorted(self.obsah))
        karty = self.karty('realizacie/')
        self.assertEqual(sorted(karty), sorted(self.obsah))
        for r in self.w.realizacie:
            slug, roky = r['slug'], self.w.roky_karty(r)
            self.assertRegex(roky, self.ROKY_RE, slug)
            atr, obsah = karty[slug]
            self.assertEqual(self.kota_karty(obsah), roky, slug)
            self.assertNotIn('kota-prazdna', obsah, slug)
            najnovsi = max(int(y) for y in re.findall(r'\d{4}', roky))
            self.assertIn(f'data-rok="{najnovsi}"', atr, slug)
            info = self.info(f'realizacie/{slug}/')
            self.assertIn(f'<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">{roky}</span>',
                          info['telo'].replace('\u00a0', ' '), slug)
            u = self.udaje_detailu(info['telo'])
            self.assertEqual(u.get('Roky'), roky, slug)
            for zle in ('Rok', 'Rok príspevku', 'Roky stavby'):
                self.assertNotIn(zle, u, (slug, zle))
            veta = f'Roky: {roky}.'
            self.assertIn(veta, self.w.vety_realizacie(r), slug)
            self.assertEqual(info['desc'], self.w.popis_realizacie(r), slug)
            if veta not in info['desc']:
                self.assertGreater(len(info['desc']) + 1 + len(veta), 155, (slug, info['desc']))
            self.assertNotIn('Rok:', info['desc'], slug)

    def test_sest_bez_rokov_stavby_ma_rok_prispevku(self):
        """Šesť realizácií bez rokov stavby má rok príspevku (rok_prispevku, inak rok z poľa datum) bez slova príspevok
        na karte zoznamu aj stránok služieb, v zozname Ďalšie realizácie, v detaile (kóta, Roky, description, keď sa
        zmestí), v lightboxe, v popise slidera a v llms.txt; po oprave 1 kola 1 tam rok nebol vôbec."""
        bez = {r['slug']: r for r in self.w.realizacie if not r['roky']}
        self.assertEqual(sorted(bez), sorted(self.BEZ_ROKOV_STAVBY))
        karty = self.karty('realizacie/')
        sluzby = {c: (self.karty(f'{c}/'), self.dalsie(f'{c}/')) for c in self.sluzby}
        uvod = self.info('')['telo'].replace('\u00a0', ' ')
        llms = (self.ostry / 'llms.txt').read_text(encoding='utf-8').splitlines()
        na_sluzbach, v_lightboxe, v_slideri = 0, 0, []
        for slug, rok in self.BEZ_ROKOV_STAVBY.items():
            r = bez[slug]
            self.assertEqual((r['rok_prispevku'], r['datum'][:4], self.w.roky_karty(r), r['rok'], self.w.rok_filtra(r)),
                             (rok, rok, rok, int(rok), int(rok)), slug)
            atr, obsah = karty[slug]
            self.assertEqual(self.kota_karty(obsah), rok, slug)
            self.assertIn(f'data-rok="{rok}"', atr, slug)
            for c, (kk, dd) in sluzby.items():
                if slug in kk:
                    self.assertEqual(self.kota_karty(kk[slug][1]), rok, (c, slug))
                    na_sluzbach += 1
                if slug in dd:
                    self.assertTrue(dd[slug].endswith(f' · {rok}'), (c, slug, dd[slug]))
                    na_sluzbach += 1
            info = self.info(f'realizacie/{slug}/')
            self.assertIn(f'<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">{rok}</span>',
                          info['telo'], slug)
            self.assertEqual(self.udaje_detailu(info['telo']).get('Roky'), rok, slug)
            veta = f'Roky: {rok}.'
            if veta not in info['desc']:
                self.assertGreater(len(info['desc']) + 1 + len(veta), 155, (slug, info['desc']))
            for u in re.findall(r'data-udaje="([^"]*)"', info['telo']):
                self.assertTrue(html.unescape(u).endswith(rok), (slug, u))
                v_lightboxe += 1
            popis = re.search(rf'<li class="slider-popis[^"]*"><a href="realizacie/{slug}/">.*?</li>', uvod)
            if popis:
                self.assertTrue(popis.group(0).endswith(f'<span class="cisla nz">{rok}</span></span></li>'), slug)
                v_slideri.append(slug)
            riadok = [x for x in llms if f'(https://bmbuilding.sk/realizacie/{slug}/)' in x]
            self.assertEqual(len(riadok), 1, slug)
            self.assertTrue(riadok[0].endswith(veta), riadok[0])
            for t in (obsah, info['telo']):
                self.assertNotIn('príspev', html.unescape(re.sub(r'<[^>]+>', ' ', t)).lower(), slug)
        self.assertGreater(na_sluzbach, 0)
        self.assertGreater(v_lightboxe, 0)
        self.assertEqual(sorted(v_slideri), ['rd-ahoj-bratislava-nove-mesto', 'rekonstrukcia-vila-holubyho-bratislava'])
        self.assertIn('Roky: 2026.', self.info('realizacie/rd-ahoj-bratislava-nove-mesto/')['desc'])

    def test_nikde_mesiac_ani_den(self):
        """Pri realizáciách len rok alebo roky, nikde mesiac ani deň (ZMENY-KOLO-1.md 5.2, doladenie-3): text kariet
        (zoznam, služby), zoznam Ďalšie realizácie, popis slidera, tabuľka údajov, description a lightbox detailu
        (bez bloku V médiách s číslom časopisu), riadky realizácií v llms.txt; dátum príspevku (pole datum) nie je
        v žiadnom HTML ani v llms.txt (lastmod v sitemap nie je text stránky)."""
        kusy = []
        for cesta in ['realizacie/'] + [f'{c}/' for c in self.sluzby]:
            for slug, (_, obsah) in self.karty(cesta).items():
                self.assertRegex(self.kota_karty(obsah) or '', self.ROKY_RE, (cesta, slug))
                kusy.append((cesta, slug, self.text(obsah.split('<figcaption>', 1)[1])))
            for slug, u in self.dalsie(cesta).items():
                self.assertRegex(u.rsplit(' · ', 1)[1], self.ROKY_RE, (cesta, slug))
                kusy.append((cesta, slug, u))
        uvod = self.info('')['telo']
        for slug, u in re.findall(r'<li class="slider-popis[^"]*"><a href="realizacie/([a-z0-9-]+)/">.*?</a>(.*?)</li>', uvod):
            kusy.append(('uvod', slug, self.text(u)))
        for r in self.w.realizacie:
            info = self.info(f'realizacie/{r["slug"]}/')
            telo = re.sub(r'<section class="sekcia media-sekcia".*?</section>', '', info['telo'], flags=re.S)
            u = self.udaje_detailu(telo)
            self.assertRegex(u['Roky'], self.ROKY_RE, r['slug'])
            kusy += [(r['slug'], 'udaje', ' '.join(f'{k} {v}' for k, v in u.items())), (r['slug'], 'desc', info['desc'])]
            kusy += [(r['slug'], 'lightbox', html.unescape(x)) for x in re.findall(r'data-udaje="([^"]*)"', telo)]
        llms = (self.ostry / 'llms.txt').read_text(encoding='utf-8')
        kusy += [('llms', '', x) for x in llms.splitlines() if '/realizacie/' in x]
        self.assertGreater(len(kusy), 3 * len(self.w.realizacie))
        for kde, slug, t in kusy:
            m = build.MESIAC_V_TEXTE.search(t) or self.DATUM_RE.search(t)
            self.assertIsNone(m, (kde, slug, t, m and m.group(0)))
        datumy = {r['datum'] for r in self.w.realizacie if r['datum']}
        self.assertTrue(datumy)
        for subor in sorted(self.ostry.rglob('*.html')) + [self.ostry / 'llms.txt']:
            s = subor.read_text(encoding='utf-8')
            for d in datumy:
                self.assertNotIn(d, s, (subor, d))

    def test_filter_roky_a_casova_os(self):
        """Filter Roky a časová os na /realizacie/ sedia so zobrazenými rokmi: voľby desaťročí od najnovšieho, text od
        najmenšieho po najväčší rok kariet v desaťročí (2020 až 2026 s rokom príspevku RD Ahoj a Vila Holubyho),
        „rok neuvedený“ len pri realizácii bez roku (dnes žiadna); os od najstaršieho roku kariet po najnovší, dielik
        na posledný rok každej karty, roky šiestich realizácií bez rokov stavby na osi."""
        telo = self.info('realizacie/')['telo']
        vyber = telo.split('<select data-roky>', 1)[1].split('</select>', 1)[0]
        volby = re.findall(r'<option value="(\d*)">([^<]+)</option>', vyber)
        roky = [self.w.rok_filtra(r) for r in self.w.realizacie]
        self.assertNotIn(0, roky)
        dekady = {}
        for y in roky:
            dekady.setdefault(y - y % 10, []).append(y)
        ocak = [('', 'všetky')] + [(str(d), f'{min(v)} až {max(v)}' if min(v) != max(v) else str(v[0]))
                                   for d, v in sorted(dekady.items(), reverse=True)]
        self.assertEqual(volby, ocak)
        self.assertNotIn('rok neuvedený', vyber)
        self.assertIn(('2020', '2020 až 2026'), volby)
        for slug, rok in self.BEZ_ROKOV_STAVBY.items():
            self.assertIn(str(int(rok) - int(rok) % 10), dict(volby), slug)
        m = re.search(r'<div class="casova-mierka"><span class="mierka-os" aria-hidden="true">(.*?)</span>'
                      r'<span class="mierka-pozn">(.*?)</span></div>', telo, re.S)
        self.assertIsNotNone(m)
        bunky = re.findall(r'<span class="m-rok v(\d)">(?:<b>(\d{4})</b>)?</span>', m.group(1))
        cisla = [self.w.roky_cisla(r) for r in self.w.realizacie]
        od, do = min(min(c) for c in cisla), max(max(c) for c in cisla)
        self.assertGreaterEqual(do, 2026)
        self.assertEqual(len(bunky), do - od + 1)
        self.assertEqual((bunky[0][1], bunky[-1][1]), (str(od), str(do)))
        pocty = {}
        for c in cisla:
            pocty[max(c)] = pocty.get(max(c), 0) + 1
        mx = max(pocty.values())
        for i, (v, _) in enumerate(bunky):
            n = pocty.get(od + i, 0)
            self.assertEqual(int(v), max(1, min(4, round(n / mx * 4))) if n else 0, od + i)
        for rok in set(self.BEZ_ROKOV_STAVBY.values()):
            self.assertNotEqual(bunky[int(rok) - od][0], '0', rok)
        self.assertEqual(self.text(m.group(2)), f'Realizácie podľa posledného roku na karte, {od} až {do}. '
                                                'Modrý dielik: počet stavieb v tom roku.')

    def test_slider_roky_pri_kazdej_stavbe(self):
        """Popis slidera na úvode má pri každej stavbe miesto a roky (span.cisla.nz na konci), RD Ahoj a Vila Holubyho
        rok príspevku 2026 (predtým len miesto)."""
        uvod = self.info('')['telo']
        popisy = re.findall(r'<li class="slider-popis[^"]*"><a href="realizacie/([a-z0-9-]+)/">.*?</a>'
                            r'<span class="slider-udaje">(.*?)</span></li>', uvod)
        with redirect_stderr(io.StringIO()):
            fotky = self.w.slider_fotky()
        self.assertEqual([s for s, _ in popisy], [r['slug'] for r, _, _ in fotky])
        self.assertEqual(len(popisy), 11)
        podla = {r['slug']: r for r in self.w.realizacie}
        for slug, udaje in popisy:
            roky = self.w.roky_karty(podla[slug])
            self.assertRegex(roky, self.ROKY_RE, slug)
            self.assertTrue(udaje.replace('\u00a0', ' ').endswith(f'<span class="cisla nz">{roky}</span>'), (slug, udaje))
            self.assertNotIn('príspev', udaje)
        d = dict(popisy)
        for slug in ('rd-ahoj-bratislava-nove-mesto', 'rekonstrukcia-vila-holubyho-bratislava'):
            self.assertTrue(d[slug].endswith('<span class="cisla nz">2026</span>'), d[slug])


def copy_realizacie(r):
    """Plytká kópia realizácie na skúšku nastavenia karta_foto (fotky ostávajú tie isté slovníky)."""
    return dict(r)


if __name__ == '__main__':
    unittest.main()
