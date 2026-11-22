"""Testy generátora build.py (unittest, bez siete), štýl Čiara podľa ukážky Ateliér Olovnica (linka bmb-olovnica).

Spustenie: python -m unittest -v test_build   (v priečinku products/bmbuilding)

Skutočný obsah (obsah/, dopĺňa ho linka bmb-fb) sa zostaví raz pre celý beh (ostrý a náhľad) s rýchlou náhradou
kódovania webp; očakávania sa počítajú z obsahu, nie z pevných počtov, aby testy platili aj po pridaní realizácií.
"""
import atexit
import base64
import colorsys
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
        self.assertIn('class="hrdina-foto"', lcp[0])
        self.assertNotIn('loading="lazy"', lcp[0])

    def test_presmerovania(self):
        s = (self.ostry / 'index.php' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('content="0; url=../"', s)
        self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/">', s)
        s = (self.ostry / 'index.php' / 'cinnost' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('url=../../#sluzby', s)
        self.assertIn('id="sluzby"', (self.ostry / 'index.html').read_text(encoding='utf-8'))
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
        maju = (KOREN / 'obsah' / 'novinky').is_dir() and any((KOREN / 'obsah' / 'novinky').glob('*.json'))
        self.assertEqual((self.ostry / 'novinky' / 'index.html').is_file(), bool(maju))
        self.assertEqual('href="novinky/"' in (self.ostry / 'index.html').read_text(encoding='utf-8'), bool(maju))

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
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            hlavicka = s.split('<header class="hlava">', 1)[1].split('</header>', 1)[0]
            m = re.search(r'<a class="znak" href="[^"]*"><img src="([^"]*)" width="52" height="52" alt="([^"]+)"></a>', hlavicka)
            self.assertIsNotNone(m, subor)
            self.assertTrue(m.group(1).endswith('logo.svg'), subor)
            self.assertIn('BM Building', m.group(2))
            self.assertIn('<meta name="theme-color" content="#17476f">', s)

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
                        '36965/B', 'Tatra banka, a.s.', '262 180 9127/1100', 'Navigovať', 'Mapy.cz', 'Facebook'):
                self.assertIn(kus, text, (cesta, kus))
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
                    '--modra-svetla:#7fb0dc', '.prace{position:relative;z-index:1;background:var(--grafit)'):
            self.assertIn(kus, self.css)
        # tmavý pás ostáva grafit ako v ukážke (zadanie), modrá z loga nie je plocha pásu
        prace = re.search(r'\.prace\{([^}]*)\}', self.css).group(1)
        self.assertNotIn('--modra', prace)
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
        # úvod má navyše preload fotky pohľadu A (LCP) pre mobil (výrez 5 : 4) a od 768 px (celá fotka)
        obr = re.findall(r'<link rel="preload" as="image"[^>]*>', s)
        self.assertEqual(len(obr), 2)
        # linka bmb-fotky: preload len formátu, ktorý picture vyberie (AVIF, bez kodeku WebP), nie oba
        self.assertRegex(obr[0], r'-54\.(avif|webp) 480w')
        self.assertIn('media="(max-width: 767px)"', obr[0])
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
        # vlasové čiary stĺpcov, mriežka 4, 8 a 12 stĺpcov, nadpis podstránky a olovnica v mene ako v ukážke
        for kus in ('--stlpce:4', '--stlpce:8', '--stlpce:12', '.ciary-vnutro span{border-left:1px solid var(--ciara)}',
                    '--krivka:cubic-bezier(.65,0,.35,1)', '.olovnica::after', 'border-radius:2px',
                    # komponenty ukážky Olovnica (PLAN-1-1.md časť 1)
                    '.hlava .obsah{', '.prace-lepidlo{', '.prace.pripnute .prace-posuvne{', '.fazy{', '.faza::before{',
                    '.cennik{', '.sluzba{', '.atelier-foto{', '.atelier-text{', '.kontakt{', '.mapa-karta{', '.pata-odkazy{',
                    '.uvod-mriezka::before{', '--f:min(15svh,(5 * var(--k) + 4 * var(--medzera)) * .196)'):
            self.assertIn(kus, self.css)
        for subor in stranky(self.ostry):
            s = subor.read_text(encoding='utf-8')
            self.assertEqual(s.count(build.CIARY_HTML), 1 + (1 if subor == self.ostry / 'index.html' else 0), subor)
            if subor.parent == self.ostry:
                continue
            telo = s.split('<main', 1)[1]
            self.assertRegex(telo, r'<div class="hlava-stranky[^"]*"><div class="obsah">', subor)
            self.assertRegex(telo, r'<h1 class="nadpis-stranky" id="nadpis">', subor)
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<span class="meno-velky" aria-hidden="true">BM Bu<span class="meno-i">ı<span class="olovnica"></span></span>lding</span>', uvod)
        self.assertIn('<span class="vh">BM Building</span>', uvod)

    def test_pohyb_len_bez_obmedzenia(self):
        css = self.css
        pohyb = css.split('@media (prefers-reduced-motion:no-preference){', 1)
        self.assertEqual(len(pohyb), 2)
        vnutri, za = pohyb[1].split('\n}\n', 1)
        mimo = pohyb[0] + za
        self.assertNotRegex(mimo, r'animation:')
        for kus in ('.olovnica{animation:kyv 1.6s', '.pohlad-ramec img{--odkryt-od:inset(0 0 12% 0);animation:odkryt 640ms', '.pohlad-ramec img{--odkryt-od:inset(0 0 0 100%)',
                    '.v-obrys{animation:kreslit-zdola', '.v-koty{animation:kreslit-zlava',
                    '.plan-kota .stlpec{transition:none;animation:pretocit', '@supports (animation-timeline:view())',
                    'animation:pas-posun linear both'):
            self.assertIn(kus, vnutri)
        # pri obmedzenom pohybe je pretočené číslo schémy hneď v konečnom stave a prechody sú vypnuté
        self.assertIn('.plan-kota .stlpec{transform:translateY(calc((var(--c) + 10) * -1.2em))}', mimo)
        self.assertIn('@media (prefers-reduced-motion:reduce)', mimo)
        # animuje sa len transform, opacity a clip-path, nie rozmery
        for kf in re.findall(r'@keyframes [a-z-]+\{(.*?)\}\}', css):
            self.assertNotRegex(kf, r'(^|[{;])(width|height|top|left|margin|padding)\s*:', kf)
        # čiary sa kreslia cez clip-path a scaleX, nie SVG pathLength s non-scaling-stroke (Firefox)
        for subor in [self.ostry / 'index.html', self.ostry / 'realizacie' / 'index.html']:
            s = subor.read_text(encoding='utf-8')
            self.assertNotIn('pathLength=', s)
            self.assertNotIn('non-scaling-stroke', s)
        self.assertNotIn('non-scaling-stroke', css)
        # obsah sa neskrýva natrvalo: trieda js zmizne, keď app.js nepribehne; schéma čaká (caka) len s app.js
        self.assertIn("if(window.BMB==null)d.classList.remove('js')", build.SKRIPT_JS)
        self.assertIn('.uvod-plan.caka *', mimo)
        # číslice kót bez atribútu style (CSP): poloha v triedach c0 až c9 a i0 až i9
        self.assertIn('.c9{--c:9}', css)
        self.assertIn('.i9{--i:9}', css)


class TestStranky(unittest.TestCase):
    """Úvod, služby, zoznam a stránky realizácií nad skutočným obsahom."""

    @classmethod
    def setUpClass(cls):
        s = skutocny()
        cls.ostry = s['ostry']
        cls.realizacie = s['realizacie']
        cls.w = s['web']
        cls.texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        cls.firma = json.loads((KOREN / 'obsah' / 'firma.json').read_text(encoding='utf-8'))
        cls.css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    def test_uvod_poradie_a_h1(self):
        t = self.info('')
        # meno ako v ukážke: malý riadok a veľké meno; čítačka a vyhľadávač čítajú skrytý text, nie ı s olovnicou
        self.assertTrue(t['h1'].startswith(self.texty['uvod']['meno_maly'] + ' BM Building'), t['h1'])
        telo = t['telo']
        # časti ukážky Olovnica v jej poradí (PLAN-1-1.md časť 1): úvod (meno, text, pohľad A, výkres), pás prác,
        # Postup, Činnosť (Služby a ceny), O firme (Ateliér), Kontakt; bez pásu „Ukážka webu od ARLing“
        poradie = [telo.index(k) for k in ('class="uvod', 'class="meno-blok"', 'class="uvod-text"', 'class="pohlad"',
                                             'class="plan uvod-plan"', 'id="prace"', 'id="postup"', 'id="sluzby"',
                                             'id="firma"', '<section class="sekcia kontakt" id="kontakt"')]
        self.assertEqual(poradie, sorted(poradie))
        self.assertNotIn('pas-ukazky', t['html'])
        self.assertNotIn('Ukážka webu', t['html'])
        # jedna jasná veta pre investorov a developerov, Zavolať a Pozrieť realizácie, stav len so skutočným údajom
        uvod = telo.split('class="uvod-text"', 1)[1].split('class="pohlad"', 1)[0].replace(' ', ' ')
        self.assertIn(self.texty['uvod']['veta'], html.unescape(re.sub(r'<[^>]+>', '', uvod)))
        self.assertIn('investorom a developerom', self.texty['uvod']['veta'].lower())
        self.assertIn(f'<a class="tlacidlo" href="tel:{self.firma["telefon_href"]}">Zavolať', uvod)
        self.assertIn('<a class="odkaz-sipka" href="#prace">Pozrieť realizácie', uvod)
        self.assertNotIn('mailto:', uvod)
        self.assertIn('data-vyzva-hore', uvod)
        stav = re.search(r'<span class="stav-text">([^<]+)</span>', uvod).group(1)
        self.assertEqual(stav, 'Na trhu od roku 2005')
        self.assertEqual(self.firma['den_zapisu'][:4], '2005')
        self.assertIn('ORSR', self.texty['uvod']['_stav_zdroj'])
        # pohľad A: skutočná fotka jeho stavby, mobilný výrez 5 : 4 v picture, popis z obsahu s odkazom na realizáciu
        ram = telo.split('class="pohlad"', 1)[1].split('</figure>', 1)[0]
        self.assertRegex(ram, r'<source media="\(max-width: 767px\)" type="image/webp" srcset="[^"]*-54\.webp 480w')
        self.assertIn('<strong>Pohľad A:</strong> <a href="realizacie/', ram)
        self.assertNotIn('kota-pohlad', ram)
        self.assertNotIn('kota-zvisla', ram)
        # Činnosť: každá služba s vlastnou stránkou, bez cien (žiadne €)
        cinnost = telo.split('id="sluzby"', 1)[1].split('</section>', 1)[0]
        for k in self.texty['sluzby']:
            self.assertIn(f'href="{k}/"', cinnost)
        self.assertNotIn('€', cinnost)
        # fotka v ráme úvodu je veľká a ostrá (nastavenia uvod_foto alebo prvá vyhovujúca), nie vizualizácia
        src = re.search(r'<img class="hrdina-foto" src="obrazky/([a-z0-9-]+)/(\d\d)-', telo)
        self.assertIsNotNone(src)
        r = [x for x in self.w.realizacie if x['slug'] == src.group(1)][0]
        f = [x for x in r['fotky'] if x['nn'] == src.group(2)][0]
        self.assertGreaterEqual(f['w'], 1280)
        self.assertGreaterEqual(f['ostrost'], 800)
        self.assertFalse(f['viz'])
        # žiadne pred a po na úvode ani pás čísel zamietnutého smeru, žiadne novinky ani sekcia Kde na úvode
        self.assertNotIn('data-pred-po', telo)
        self.assertNotIn('data-porovnanie', telo)
        self.assertNotIn('class="dokazy"', telo)
        self.assertNotIn('novinka-karta', telo)
        self.assertNotIn('id="kde"', telo)

    def karty_pasu(self):
        sekcia = self.info('')['telo'].split('<section class="prace" id="prace"', 1)[1].split('</section>', 1)[0]
        return sekcia, re.findall(r'<li class="projekt">(.*?)(?=<li class="projekt|</ul></div></div>)', sekcia, re.S)

    def test_uvod_realizacie(self):
        # tmavý pás vybraných realizácií z ukážky (U:801 až U:951): výber z nastavenia pas_vyber v poradí podľa rokov
        sekcia, karty = self.karty_pasu()
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        vyber = [r for r in self.w.realizacie if r['slug'] in nast['pas_vyber']]
        self.assertEqual(len(vyber), len(nast['pas_vyber']))
        self.assertEqual(len(karty), len(vyber))
        slugy = re.findall(r'<h3><a href="realizacie/([a-z0-9-]+)/">', sekcia)
        self.assertEqual(slugy, [r['slug'] for r in vyber])
        for karta, r in zip(karty, vyber):
            self.assertIn('<figure><div class="projekt-foto"><img', karta)
            self.assertIn('loading="lazy"', karta)
            self.assertIn('class="kota"', karta)
            # fotka karty odložená ako v ukážke (data-src, prázdny obrázok v src), bez skriptu kópia v noscript
            self.assertRegex(karta, r'<img src="prazdna-45\.webp" data-src="obrazky/[a-z0-9-]+/\d\d-\d+-45\.webp" data-srcset="')
            self.assertRegex(karta, r'<noscript><img src="obrazky/[a-z0-9-]+/\d\d-\d+-45\.webp"[^>]*loading="lazy"')
            # tlačidlo Fotky: odkaz na galériu stránky realizácie, app.js otvorí lightbox so skupinou tej realizácie
            self.assertIn(f'<a class="prepinac" href="realizacie/{r["slug"]}/#galeria" data-galeria="{r["slug"]}" aria-haspopup="dialog"', karta)
            zoznam = re.search(rf'<ul class="lb-zoznam" data-galeria="{r["slug"]}" data-cesta="obrazky/{r["slug"]}/" [^>]*hidden>(.*?)</ul>', karta, re.S).group(1)
            self.assertEqual(zoznam.count('data-lightbox'), len(r['galeria']), r['slug'])
            self.assertNotIn('<img', zoznam)
            if r['rola']:
                self.assertIn(f'<p class="projekt-text">{html.escape(r["rola"])}</p>', karta.replace(' ', ' '))
        self.assertIn('<ul class="pas" id="pas-prac" role="list" aria-label="Vybrané realizácie" tabindex="0">', sekcia)
        self.assertIn('<div class="sipky" role="group" aria-label="Posun pásu realizácií" hidden>', sekcia)
        self.assertIn(f'Všetky realizácie ({len(self.realizacie)})', sekcia)
        self.assertIn(f'Ďalších {len(self.realizacie) - len(vyber)} stavieb je v zozname realizácií.', sekcia.replace(' ', ' '))
        # jeden lightbox na stránku, preskočenie pásu pre klávesnicu
        self.assertEqual(self.info('')['html'].count('<dialog class="lightbox" id="lightbox"'), 1)
        self.assertIn('<a class="preskocit preskocit-pas" href="#postup">', sekcia)
        # úvodná veta pásu z dát: počet slovom a roky vybraných kariet
        roky = [r for r in vyber if r['roky']]
        veta = f'{build.POCET_STAVIEB[len(vyber)]} z rokov {min(r["rok_od"] for r in roky)} až {max(r["rok"] for r in roky)}.'
        self.assertIn(veta, sekcia.replace(' ', ' '))

    def test_pas_prac(self):
        # pripnutý vodorovný pás (ukážka U:657 až U:670, UJ:181 až UJ:340): CSS časová os len bez obmedzenia pohybu,
        # na mobile scroll-snap, pri obmedzenom pohybe mriežka; app.js zapína pripnutie a posúva pri fokuse
        css = self.css
        for kus in ('.prace.pripnute .prace-lepidlo{position:sticky;top:0', 'scroll-snap-type:x mandatory',
                    'view-timeline-name:--prace', 'animation-range:contain 0% contain 100%', '.projekt-foto{position:relative;aspect-ratio:4/5'):
            self.assertIn(kus, css)
        pohyb = css.split('@media (prefers-reduced-motion:no-preference){', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('@supports (animation-timeline:view())', pohyb)
        self.assertNotIn('@supports (animation-timeline:view())', css.replace(pohyb, ''))
        obmedzeny = css.split('@media (prefers-reduced-motion:reduce){', 1)[1].split('\n}\n', 1)[0]
        self.assertRegex(obmedzeny, r'\.pas\{display:grid;')
        self.assertIn('.pas-ovladanie{display:none}', obmedzeny)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        for kus in ("'(min-width: 1024px) and (min-height: 560px) and (prefers-reduced-motion: no-preference)'",
                    "CSS.supports('animation-timeline: view()')", "pas.addEventListener('focusin'", "setProperty('--posun'",
                    "sekcia.style.height", "'min(45vh, 480px)'", 'requestIdleCallback'):
            self.assertIn(kus, js)

    def test_vykres_schema(self):
        # animovaný technický výkres (PLAN-1-1.md časť 3): zjavne schéma, bez rozmerov, texty zo starého webu,
        # vrstvy kreslené cez clip-path do 1,8 s
        telo = self.info('')['telo']
        fig = telo.split('<figure class="plan uvod-plan" id="schema">', 1)[1].split('</figure>', 1)[0]
        self.assertIn('<strong>Schéma</strong>', fig)
        self.assertIn('role="img" aria-labelledby="schema-titul"', fig)
        titul = html.unescape(re.search(r'<span class="vh" id="schema-titul">([^<]+)</span>', fig).group(1))
        self.assertIn('Schéma, nie výkres konkrétnej stavby', titul.replace(' ', ' '))
        bez_znaciek = html.unescape(re.sub(r'<[^>]+>', ' ', fig)).replace(' ', ' ')
        self.assertNotRegex(bez_znaciek, r'\d\s*(mm|cm|m²|m)\b')
        for vrstva in ('v-teren', 'v-srafa', 'v-obrys', 'v-okna', 'v-schody', 'v-bod', 'v-koty', 'v-popisy'):
            self.assertEqual(fig.count(f'<div class="vrstva {vrstva}">'), 2, vrstva)  # široká a úzka kompozícia
        # každý popis a riadok pod ním je z texty.json schema a pochádza zo starého webu (stavebny-dozor.md)
        ts = self.texty['schema']
        povolene = {k['popis'] for k in ts['kontroly']} | {k['plocha'] for k in ts['kontroly']}
        povolene |= {'smerové', 'a výškové', ts['nie_v_mierke']}
        texty = [html.unescape(x).replace(' ', ' ')
                 for x in re.findall(r'<(?:text|tspan) class="(?:popis|plocha|poznamka-schemy)"[^>]*>([^<]+)<', fig)]
        self.assertTrue(texty)
        self.assertTrue(set(texty) <= povolene, set(texty) - povolene)
        dozor = KOREN.parent.parent / 'ops' / 'weby-na-mieru' / 'klienti' / 'bmbuilding' / 'stary-web' / 'stavebny-dozor.md'
        if dozor.is_file():
            stary = dozor.read_text(encoding='utf-8').lower()
            for k in ts['kontroly']:
                for slovo in re.findall(r'\w{5,}', (k['popis'] + ' ' + k['plocha']).lower()):
                    self.assertIn(slovo[:5], stary, (k, slovo))
            self.assertEqual(len(re.findall(r'^- ', dozor.read_text(encoding='utf-8'), re.M)), 19)
        # číslo pod rezom = počet činností stavebného dozoru (19)
        n = len([s for s in self.firma['sluzby'] if s['slug'] == 'stavebny-dozor'][0]['cinnosti'])
        self.assertEqual(n, 19)
        kota = re.search(r'<span class="plan-kota" aria-hidden="true">(.*?)<span class="kota-slovo">', fig).group(1)
        self.assertEqual(''.join(re.findall(r'stlpec c(\d)', kota)), str(n))
        self.assertIn(f'všetkých {n} činností', fig.replace(' ', ' '))
        # mierka bez atribútu style, polohy triedami
        self.assertIn('<span class="mierka-c mc-17">', fig)
        self.assertIn('.mc-17{left:16.667%}', self.css)
        # každá animácia výkresu skončí do 1,8 s (trvanie + oneskorenie)
        pohyb = self.css.split('@media (prefers-reduced-motion:no-preference){', 1)[1].split('\n}\n', 1)[0]
        casy = re.findall(r'(\.v-[a-z]+|\.plan-kota[^{]*)\{[^}]*?animation:[a-z-]+ (\d+)ms [^;}]*?(\d+)ms both', pohyb)
        self.assertGreaterEqual(len(casy), 10)
        for sel, trvanie, oneskorenie in casy:
            self.assertLessEqual(int(trvanie) + int(oneskorenie), 1800, sel)
        self.assertNotIn('pathLength', fig)

    def test_roky_na_kartach(self):
        # karta ukáže roky stavby, bez nich rok príspevku; slovo Facebook v karte nie je (zadanie, Obsah 2)
        _, karty = self.karty_pasu()
        for karta in karty:
            self.assertNotIn('Facebook', karta)
            self.assertRegex(karta.replace(' ', ' '), r'<span class="vh">(príspevok \d{4}|\d{4}( až \d{4})?)</span>')
        zoznam = self.info('realizacie/')['telo']
        for r in self.w.realizacie:
            karta = zoznam.split(f'realizacie/{r["slug"]}/"', 1)[1].split('</li>', 1)[0]
            self.assertNotIn('Facebook', karta)
            ocakavane = r['roky'] or r['rok_prispevku']
            if ocakavane:
                # zoznam má statickú kótu (slovo „až“ v span.kota-slovo): porovnáva sa text bez značiek
                self.assertIn(ocakavane, re.sub(r'<[^>]+>', '', karta).replace(' ', ' '), r['slug'])

    def test_kazda_karta_pasu_ma_fotku(self):
        # žiadna karta pásu bez fotky; vizualizácia ako fotka karty alebo obal len pri obal_vizualizacia
        _, karty = self.karty_pasu()
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
        self.assertGreaterEqual(viditelne, 20)
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
        novinky = self.karty_zoznamu('novinky/')
        self.assertEqual(len(novinky), len(self.w.novinky))
        for (trieda, karta), n in zip(novinky, self.w.novinky):
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
        # karty (zoznam, služby) a stránky realizácií ukazujú roky stavby, bez nich rok príspevku bez slova Facebook
        for cesta in ['realizacie/'] + [f'{k}/' for k in self.texty['sluzby']]:
            for _, karta in self.karty_zoznamu(cesta):
                slug = re.search(r'realizacie/([a-z0-9-]+)/', karta).group(1)
                r = [x for x in self.w.realizacie if x['slug'] == slug][0]
                self.assertNotIn('Facebook', karta)
                # bez rokov stavby „príspevok RRRR“, nie holý rok (nález 1 brány bmb-fb-uplnost)
                ocak = r['roky'] or (f'príspevok {r["rok_prispevku"]}' if r['rok_prispevku'] else '')
                self.assertTrue(ocak, slug)
                if cesta == 'realizacie/':
                    # zoznam realizácií: statická kóta bez pretáčania (kratšie HTML pri desiatkach kariet, bmb-fb-uplnost)
                    kota = re.search(r'<span class="kota"><span class="kota-text">(.*?)</span></span>', karta).group(1)
                    self.assertEqual(re.sub(r'<[^>]+>', '', kota).replace('\u00a0', ' '), ocak, slug)
                    self.assertNotIn('class="valce"', karta)
                else:
                    self.assertIn(f'<span class="vh">{ocak}</span>', karta.replace('\u00a0', ' '), slug)
        for r in self.w.realizacie:
            telo = self.info(f'realizacie/{r["slug"]}/')['telo'].replace('\u00a0', ' ')
            ocak = r['roky'] or f'príspevok {r["rok_prispevku"]}'
            detail = telo.split('class="obsah mriezka detail', 1)[1].split('</div></div>', 1)[0]
            self.assertIn(f'<span class="kota kota-detail"><span class="kota-text otoc"><span class="vh">{ocak}</span>', detail, r['slug'])
            if not r['roky']:
                self.assertIn(f'<dt>Rok príspevku</dt><dd>{r["rok_prispevku"]}</dd>', telo, r['slug'])
                self.assertNotIn('<dt>Roky</dt>', telo, r['slug'])

    def test_rok_prispevku_nie_je_rok_realizacie(self):
        # nález 1 brány bmb-fb-uplnost: realizácia bez známych rokov (rok_prispevku) nesmie na karte ani vo filtri
        # Roky vyzerať ako realizácia z toho roku. Karta „príspevok RRRR“, data-rok 0 (voľba „rok neuvedený“),
        # roky vo voľbách filtra len zo skutočných rokov realizácie; to isté pás úvodu, karty a zoznamy služieb
        s_prispevkom = [r for r in self.w.realizacie if r['rok_prispevku'] and not r['roky']]
        self.assertTrue(s_prispevkom)
        holy = re.compile(r'<span class="kota[^"]*"><span class="kota-text[^"]*">(?:<span class="vh">)?\d{4}</span>')
        zoznam = self.info('realizacie/')['telo'].replace('\u00a0', ' ')
        for r in s_prispevkom:
            pred, za = zoznam.split(f'realizacie/{r["slug"]}/"', 1)
            li, karta = pred.rsplit('<li ', 1)[1], za.split('</li>', 1)[0]
            self.assertIsNone(holy.search(karta), r['slug'])
            kota = re.search(r'<span class="kota"><span class="kota-text">(.*?)</span></span>', karta).group(1)
            self.assertEqual(re.sub(r'<[^>]+>', '', kota), f'príspevok {r["rok_prispevku"]}', r['slug'])
            self.assertIn('data-rok="0"', li, r['slug'])
        vyber = zoznam.split('<select data-roky>', 1)[1].split('</select>', 1)[0]
        self.assertIn('<option value="0">rok neuvedený</option>', vyber)
        skutocne = {r['rok'] for r in self.w.realizacie if r['roky'] and not r['rok_z_datumu']}
        for text in re.findall(r'<option value="\d{4}">([^<]+)</option>', vyber):
            for rok in re.findall(r'\d{4}', text):
                self.assertIn(int(rok), skutocne, text)
        # pás úvodu, karty služieb a zoznamy „Ďalšie realizácie“: rok príspevku nikdy holý
        for cesta in [''] + [f'{k}/' for k in self.texty['sluzby']]:
            telo = self.info(cesta)['telo'].replace('\u00a0', ' ')
            for r in s_prispevkom:
                for kus in telo.split(f'realizacie/{r["slug"]}/"')[1:]:
                    kus = kus.split('</li>', 1)[0]
                    self.assertIsNone(holy.search(kus), (cesta, r['slug']))
                    if re.search(rf'(?<!príspevok )\b{r["rok_prispevku"]}\b', re.sub(r'<[^>]+>', '', kus)):
                        self.fail(f'holý rok príspevku {r["rok_prispevku"]} na {cesta or "úvode"} pri {r["slug"]}')

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
        for slug, d in sw.items():
            telo = self.info(f'realizacie/{slug}/')['telo']
            udaje = html.unescape(telo.split('<dl class="fakty udaje">', 1)[1].split('</dl>', 1)[0]).replace('\u00a0', ' ')
            for strana in ('investor', 'projektant', 'zhotovitel'):
                for x in d.get(strana, []):
                    for k in ('adresa', 'web'):
                        if x.get(k):
                            self.assertIn(x[k], udaje, (slug, strana, k))
            if d.get('termin'):
                self.assertIn(f'<dt>Termín výstavby</dt><dd>{d["termin"]}</dd>', udaje, slug)
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
        self.assertEqual(kod, 0, out.getvalue())
        self.assertRegex(out.getvalue(), r'chýba 0,')

    def test_novinky_v_komponentoch(self):
        # novinka: hlava s dátumom a stavbou (pole realizacia) s rokmi stavby, dvojstrana, galéria s lightboxom
        for n in self.w.novinky:
            telo = self.info(f'novinky/{n["slug"]}/')['telo'].replace('\u00a0', ' ')
            self.assertIn('<dt>Dátum</dt>', telo)
            r = self.w.realizacia_novinky(n)
            if r:
                self.assertIn(f'href="../../realizacie/{r["slug"]}/"', telo, n['slug'])
                self.assertIn(self.w.roky_karty(r), telo, n['slug'])
            if n['galeria']:
                self.assertIn('<dialog class="lightbox" id="lightbox"', telo)
                self.assertIn('<figure class="pohlad detail-foto">', telo)

    def test_sluzby_v_komponentoch(self):
        # služba: text vľavo, box kalkulačky s dokladom SKSI a Zavolať, činnosti zo starého webu s číslom v Mono,
        # pri dozore štyri fázy postupu, karty realizácií v tej role
        for k, t in self.texty['sluzby'].items():
            telo = self.info(f'{k}/')['telo'].replace('\u00a0', ' ')
            self.assertIn('<aside class="kalkulacka sluzba-bok"', telo)
            cinnosti = self.firma_sluzby()[k]
            self.assertEqual(telo.count('<span class="cinnost-c cisla" aria-hidden="true">'), len(cinnosti), k)
            if t.get('cinnosti_uvod'):
                self.assertIn(t['cinnosti_uvod'], telo, k)
            self.assertEqual('<ol class="fazy">' in telo, k == 'stavebny-dozor', k)
        self.assertIn('stavebného zákona', self.texty['sluzby']['stavbyveduci']['cinnosti_uvod'])

    def firma_sluzby(self):
        return {s['slug']: s.get('cinnosti', []) for s in self.firma['sluzby']}

    def test_reduced_motion(self):
        # obmedzený pohyb: mimo bloku no-preference žiadna animácia; pás je obyčajný zoznam (mriežka), prechody
        # tlačidiel, filtra, lišty, lightboxu a fotiek kariet a galérie vypnuté; kóta výkresu má konečnú polohu
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
        self.assertRegex(blok, r'\.pas\{display:grid;grid-template-columns:repeat\(auto-fill')
        self.assertIn('.pas-ovladanie{display:none}', blok)
        prechody = blok.split('{transition:none}', 1)[0]
        for sel in ('.tlacidlo', '.filter', '.lista', '.lightbox-tl', '.projekt-foto img', '.galeria-odkaz img', '.prepinac'):
            self.assertIn(sel, prechody)
        self.assertIn('.plan-kota .stlpec{transform:translateY(calc((var(--c) + 10) * -1.2em))}', css)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)')", js)
        self.assertIn('(prefers-reduced-motion: no-preference)', js)

    def test_postup_cinnost_firma(self):
        telo = self.info('')['telo']
        postup = telo.split('id="postup"', 1)[1].split('</section>', 1)[0]
        fazy = re.findall(r'<li class="faza">(.*?)</li>', postup, re.S)
        self.assertEqual(len(fazy), len(self.texty['postup']['fazy']))
        for i, faza in enumerate(fazy, 1):
            self.assertIn('<span class="faza-dlzka otoc" aria-hidden="true">', faza)
            self.assertIn(f'stlpec c{i} i1', faza)
            self.assertIn('<p class="vystup">Činnosť: <a href=', faza)
        nast = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))
        self.assertRegex(postup, r'<figure class="postup-foto">(?:<picture><source [^>]*>)?<img src="obrazky/'
                         + re.escape(nast['postup_foto']['slug']) + '/')
        cinnost = telo.split('id="sluzby"', 1)[1].split('</section>', 1)[0]
        h3 = re.findall(r'<li class="sluzba"><h3><a href="([a-z-]+)/">', cinnost)
        self.assertEqual(h3, self.texty['cinnost']['poradie'])
        self.assertIn('Osvedčenia Slovenskej komory stavebných inžinierov', cinnost)
        self.assertIn('<div class="kalkulacka zakon-box"', cinnost)
        self.assertEqual(cinnost.count('<details class="otazka">'), len(self.texty['otazky']))
        firma = telo.split('id="firma"', 1)[1].split('</section>', 1)[0]
        text = html.unescape(re.sub(r'<[^>]+>', ' ', firma)).replace(' ', ' ')
        for kus in ('20 rokov', 'od roku 2005', self.firma['konatel'], 'Podhorská 9', 'Bratislava a okolie', 'stavbyvedúci 2003'):
            self.assertIn(kus, text)
        self.assertIn('href="novinky/20-rokov-na-trhu-2025-08/"', firma)
        # zdroj 20 rokov: novinka z Facebooku v obsahu
        nov = json.loads((KOREN / 'obsah' / 'novinky' / '20-rokov-na-trhu-2025-08.json').read_text(encoding='utf-8'))
        self.assertIn('20 rokov pôsobenia na trhu', nov['text'])

    def test_mobilne_menu(self):
        s = self.info('')['html']
        panel = s.split('<details class="menu-m">', 1)[1].split('</details>', 1)[0]
        for ciel in ('#sluzby', 'realizacie/', '#postup', 'opravnenia/', 'kontakt/'):
            self.assertIn(f'href="{ciel}"', panel)
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
        ocakavane = ['Činnosť', 'Realizácie', 'Postup'] + (['Novinky'] if self.w.novinky else []) + ['Oprávnenia', 'Kontakt']
        self.assertEqual(polozky, ocakavane)
        self.assertIn('<a href="../realizacie/" aria-current="page">Realizácie</a>', hlavicka)
        self.assertIn('href="../#sluzby"', hlavicka)
        self.assertIn('href="../#postup"', hlavicka)
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
        k = build.kategorie_realizacie
        self.assertEqual(k('RD Záhorie', ''), ['rodinne'])
        self.assertEqual(k('Rekonštrukcia rodinného domu', ''), ['rodinne', 'rekonstrukcie'])
        self.assertEqual(k('Polyfunkčný dom', 'Stavebné úpravy'), ['bytove', 'rekonstrukcie'])
        self.assertEqual(k('Sklad', 'Hala pre logistiku'), ['ine'])
        self.assertEqual(k('Objekt', 'Nadstavba administratívnej budovy'), ['rekonstrukcie'])
        self.assertEqual(k('Sklad', '', 'Rodinné domy'), ['rodinne'])
        self.assertEqual(k('Sklad', '', ['bytove', 'ine', 'nezmysel']), ['bytove', 'ine'])
        self.assertEqual(k('RD Jarovce', '', 'nezmysel'), ['rodinne'])

    def test_zoradenie_podla_rokov(self):
        r = lambda slug, roky, datum, poradie=5: {'slug': slug, 'roky': roky, 'datum': datum, 'poradie': poradie,
                                                  **dict(zip(('rok', 'rok_od', 'rok_z_datumu'), build.roky_realizacie(roky, datum)))}
        z = build.zorad_chronologicky([r('a', '2010 až 2012', '2025-01-01'), r('b', '2011 až 2012', '2013-01-09'),
                                       r('c', '', '2019-05-05'), r('d', '', ''), r('e', '2012', '2001-01-01', 1)])
        self.assertEqual([x['slug'] for x in z], ['c', 'e', 'b', 'a', 'd'])
        # realizácia bez rokov stavby nesie rok príspevku (rok_prispevku), bez slova Facebook (zadanie bmb-dokoncenie)
        for x in self.w.realizacie:
            if not x['roky'] and x['zdroj'] == 'fb' and x['datum']:
                karta = self.info('realizacie/')['telo'].split(f'realizacie/{x["slug"]}/"', 1)[1].split('</li>', 1)[0]
                self.assertNotIn('Facebook', karta)
                self.assertIn(x['rok_prispevku'] or x['datum'][:4], karta)

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
        # otvorené mobilné menu (.menu-panel v hlavičke) musí ležať nad main: z-index hlavičky väčší ako main
        zi = {}
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for sel, telo in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
            m = re.search(r'z-index:(\d+)', telo)
            if m and 'position:relative' in telo:
                for s in sel.split(','):
                    zi.setdefault(s.strip(), int(m.group(1)))
        self.assertGreater(zi['.hlava'], zi['main'])
        self.assertGreater(zi['.hlava'], zi['.pata'])

    def test_meno_maly_dva_riadky(self):
        # odbor v dvoch riadkoch, aby šnúra olovnice nad ı neprešla cez „spoločnosť“; olovnica siaha o riadok vyššie
        s = self.info('')['html']
        maly = re.search(r'<span class="meno-maly">(.*?)</span> <span class="vh">', s).group(1)
        self.assertEqual(re.findall(r'<span>([^<]+)</span>', maly), ['Stavebno-manažérska', 'spoločnosť'])
        for kus in ('--nad:calc(var(--nad-pad) + 2 * var(--maly-v))', '.meno-maly{display:block;height:calc(2 * var(--maly-v))',
                    '.meno-maly>span{display:block}', '--r1:max(var(--nad-pad) + 2 * var(--maly-v)'):
            self.assertIn(kus, self.css)

    def test_uvod_vyska_okna_a_veta(self):
        # --hore je len hlavička (84 px, nižšie okná 64 px), pás „Ukážka webu“ ukážky tu nie je; veta úvodu je druhá
        # úroveň (0,3 × --f) v dvoch riadkoch a jej výška ide do --r1
        root = re.search(r':root\{([^}]*)\}', re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)).group(1)
        self.assertIn('--hore:84px', root)
        self.assertIn(':root{--hore:64px}', self.css)
        self.assertIn('--veta-f:clamp(1.6rem,.3 * var(--f),2.4rem)', self.css)
        self.assertIn('.uvod-veta{margin-top:6px;font-size:var(--veta-f);line-height:1.08;letter-spacing:-.03em;max-width:21em;text-wrap:balance}', self.css)
        self.assertIn('2.16 * var(--veta-f)', self.css)
        self.assertIn('- var(--r1) -', self.css)

    def test_focusin_pasu_len_z_klavesnice(self):
        # klik myšou na Fotky v pripnutom páse: fokus pri stlačení nesmie posunúť stránku (lightbox by sa neotvoril)
        js = (KOREN / 'sablona' / 'app.js').read_text(encoding='utf-8')
        blok = js.split("pas.addEventListener('focusin'", 1)[1].split('\n    });', 1)[0]
        self.assertIn("matches(':focus-visible')", blok)
        self.assertLess(blok.index(':focus-visible'), blok.index('window.scrollTo'))
        self.assertLess(blok.index('rk.right <= window.innerWidth'), blok.index('window.scrollTo'))

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

    def test_skryte_odkazy_pasu_maju_href(self):
        # Lighthouse SEO crawlable-anchors: odkazy fotiek v skrytom zozname pásu majú href na plnú fotku
        s = self.info('')['html']
        zoznamy = re.findall(r'<ul class="lb-zoznam"[^>]*>(.*?)</ul>', s)
        self.assertGreaterEqual(len(zoznamy), 8)
        for z in zoznamy:
            odkazy = re.findall(r'<a ([^>]*)></a>', z)
            self.assertTrue(odkazy)
            for a in odkazy:
                m = re.search(r'href="([^"]+)"', a)
                self.assertTrue(m, a)
                self.assertTrue((self.ostry / html.unescape(m.group(1))).is_file(), m.group(1))

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
        self.assertIn('.projekt,.projekt-dalsi{flex:none;min-width:0}', obmedzeny)

    def test_texty_podla_zdroja(self):
        # § 36 ods. 1: „je oprávnená najmä a) sledovať…“; manažment: výber dodávateľa starý web neuvádza ako činnosť;
        # počet realizácií manažmentu je s popisom, že zlučuje projektový a stavebný manažment
        q = self.texty['otazky']['co-robi-dozor']['odpoved']
        self.assertIn('je podľa stavebného zákona oprávnená najmä sledovať', q)
        self.assertNotIn('najmä sleduje', q)
        m = self.texty['sluzby']['stavebny-manazment']
        self.assertNotIn('výber dodávateľa,', m['priklady'])
        riadok = self.info('')['telo'].split('<li class="sluzba"><h3><a href="stavebny-manazment/"', 1)[1].split('</li>', 1)[0]
        self.assertIn(m['pocet_popis'], html.unescape(riadok).replace('\xa0', ' '))

    def test_pohlad_a_ma_protikus_vo_vykrese(self):
        # „Pohľad A“ pri fotke má značku A vo výkrese (široká aj úzka kompozícia) s odkazom na fotku a vzájomné
        # zvýraznenie pri prechode myšou ako v ukážke (U:448 až U:456)
        fig = self.info('')['telo'].split('<figure class="plan uvod-plan" id="schema">', 1)[1].split('</figure>', 1)[0]
        self.assertEqual(fig.count('<g class="pohlad-znacka" transform="translate('), 2)
        self.assertEqual(fig.count('<text class="pohlad-pismeno" y="7.5">A</text>'), 2)
        self.assertEqual(fig.count('<a class="pohlad-odkaz" href="#pohlad-a" tabindex="-1" aria-hidden="true"></a>'), 2)
        self.assertIn('<figure class="pohlad" id="pohlad-a">', self.info('')['telo'])
        for s, (x, y) in build.POHLAD_ZNACKA.items():
            self.assertIn(f'translate({x} {y})', fig)
        self.assertIn('.pohlad-odkaz{position:absolute;z-index:2;left:calc(100% * 75 / 1480);top:calc(100% * 360 / 910)', self.css)
        self.assertIn('.plan-uzky .pohlad-odkaz{left:calc(100% * 100 / 890)', self.css)
        for kus in ('.uvod-mriezka:has(.pohlad-odkaz:hover) .pohlad-ramec::before{opacity:1}',
                    '.uvod-mriezka:has(.pohlad:hover) .plan .pohlad-kruh{stroke:var(--modra)}'):
            self.assertIn(kus, self.css)

    def test_description_noviniek_nie_kratka(self):
        # krátke príspevky (34 a 47 znakov) dostanú do description dátum a autora príspevku, nič iné
        for n in self.w.novinky:
            desc = self.info(f'novinky/{n["slug"]}/')['desc']
            self.assertGreaterEqual(len(desc), 70, (n['slug'], desc))
            self.assertNotIn('..', desc)
        d = self.info('novinky/vianocny-pozdrav-2025-12/')['desc']
        self.assertEqual(d, 'Veselé Vianoce a šťastný nový rok. Príspevok BM Building z 24. 12. 2025.')

    def test_uvod_vizualne_ako_ukazka(self):
        # rám fotky úvodu bez sivého pozadia (počas kreslenia výkresu papier ako v ukážke), detail si pozadie nechá;
        # lišta fáz pod schémou len na mobile; sekcie pod pásom s content-visibility (TBT úvodu)
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        ramec = re.search(r'\n\.pohlad-ramec\{([^}]*)\}', css).group(1)
        self.assertNotIn('background', ramec)
        self.assertIn('.detail .pohlad-ramec{aspect-ratio:3/2;height:auto;background:var(--vapno-2)}', css)
        tablet = css.split('@media (min-width:768px){\n  :root{--stlpce:8}', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('.uvod-plan .mierka{display:none}', tablet)
        self.assertRegex(css, r'\.stranka-uvod #postup,[^{]*\{content-visibility:auto;contain-intrinsic-size:auto \d+px\}')
        self.assertIn('.projekt-riadok{display:flex;align-items:flex-start;', css)

    # --- oprava po bráne (pokus 1, 21. 11. 2026) ---------------------------------------------------------------
    def test_vizualizacie_ufo_v_galerii_nezvacsene(self):
        # bod 1: malé vizualizácie UFO (05.jpg a 06.jpg, 320 x 179 px) sú v galérii novinky so štítkom, nie vynechané
        # ako rozmazané; v galérii nikdy zväčšené nad 1,25 násobok súboru (malá: bez srcset, object-fit:scale-down)
        n = next(x for x in self.w.novinky if x['slug'] == 'odovzdanie-staveniska-ufo-2025-03')
        self.assertEqual([f['rel'].rsplit('/', 1)[1] for f in n['galeria'] if f.get('viz')], ['05.jpg', '06.jpg'])
        s = (self.ostry / 'novinky' / n['slug'] / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(s.count('data-viz="1"'), 2)
        self.assertEqual(s.count('<span class="stitok-viz">Vizualizácia</span>'), 2)
        for nn in ('05', '06'):
            a = re.search(rf'<a class="galeria-odkaz mala" href="(\.\./\.\./obrazky-novinky/{n["slug"]}/{nn}-320\.webp)"[^>]*>'
                          r'(<img [^>]*>)', s)
            self.assertIsNotNone(a, nn)
            self.assertNotIn('srcset', a.group(2))
            self.assertIn('width="320" height="179"', a.group(2))
            self.assertRegex(a.group(2), r'alt="Vizualizácia')
            self.assertTrue((self.ostry / 'novinky' / n['slug'] / a.group(1)).resolve().is_file())
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        self.assertIn('.galeria-odkaz.mala img{object-fit:scale-down}', css)
        # obal, karta a hrdina nikdy z vizualizácie
        self.assertFalse(n['karta'].get('viz'))

    def test_galeria_nikdy_nezvacsi_nad_125(self):
        # bod 1: na každej stránke s galériou: fotka, ktorú by cover v najväčšej bunke zväčšil nad 1,25 násobok, je
        # „malá“ (bez srcset); ostatné bunku vyplnia najviac s 1,25 násobkom (rozmery plného súboru v data-sirka)
        malych = 0
        for p in [self.ostry / 'realizacie' / r['slug'] / 'index.html' for r in self.w.realizacie] + \
                 [self.ostry / 'novinky' / n['slug'] / 'index.html' for n in self.w.novinky]:
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
        for p in stranky(self.ostry):
            s = p.read_text(encoding='utf-8')
            self.assertNotIn('class="karta-znak"', s, p)
            self.assertNotIn(' karta-bez-fotky"', s, p)
            for trieda, karta in re.findall(r'<li class="((?:dlazdica|novinka-karta|projekt)[^"]*)"[^>]*>(.*?)</li>', s, re.S):
                if 'projekt-dalsi' in trieda:
                    continue
                karty += 1
                self.assertRegex(karta, r'<img [^>]*(?:src|data-src)="[^"]*\.webp"', (p, trieda))
        # aspoň zoznam realizácií, zoznam noviniek a pás úvodu
        self.assertGreaterEqual(karty, len(self.w.realizacie) + len(self.w.novinky) + 8)
        pravidla = json.loads((KOREN / 'sablona' / 'fotky-pravidla.json').read_text(encoding='utf-8'))
        zoznam = self.info('novinky/')['telo']
        bez = [n for n in self.w.novinky if not n['galeria']]
        self.assertEqual(sorted(n['slug'] for n in bez), sorted(pravidla['ilustracne_fotky']))
        for n in bez:
            volba = pravidla['ilustracne_fotky'][n['slug']]
            self.assertTrue(volba.get('dovod'), n['slug'])
            r = next(x for x in self.w.realizacie if x['slug'] == volba['slug'])
            f = n['ilustracna_foto']
            self.assertEqual(f['rel'], f'{r["slug"]}/{volba["subor"]}')
            self.assertFalse(f.get('viz'))
            self.assertGreaterEqual(f['ostrost'], self.w.pravidla['obal']['min_ostrost'])
            # iná fotka než karta realizácie (v prehľade sa neopakuje rovnaký záber)
            self.assertIsNot(f, r['karta'], n['slug'])
            popis = f'Ilustračná fotka: {self.w.nazov_s_miestom(r)}'
            karta = zoznam.split(f'href="../novinky/{n["slug"]}/"', 1)[1].split('</li>', 1)[0]
            self.assertIn(f'alt="{html.escape(popis)}"', karta.replace(' ', ' '))
            self.assertIn(f'src="../obrazky/{r["slug"]}/{f["nn"]}-', karta)
            self.assertIn('<span class="stitok-viz stitok-ilustr">Ilustračná fotka</span>', karta)
            self.assertIn(f'<p class="projekt-ilustr">{html.escape(popis, quote=False)}</p>', karta.replace(' ', ' '))
            # stránka novinky ilustračnú fotku neukazuje ako fotku novinky
            detail = (self.ostry / 'novinky' / n['slug'] / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('Ilustračná', detail)

    def test_navod_pasu_podla_zariadenia(self):
        # bod 3: dotyk (pointer coarse) radí prst, počítač s myšou koliesko a Shift; Shift nikdy pri coarse
        tp = self.texty['pas']
        self.assertIn('prst', tp['navod_mobil'])
        self.assertIn('Shift', tp['navod_mys'])
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        hrube = css.split('@media (pointer:coarse){', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('.prace:not(.pripnute) .navod-mys{display:none}', hrube)
        self.assertIn('.prace:not(.pripnute) .navod-mobil{display:inline}', hrube)
        # koliesko sa zobrazí len v bloku pre myš (hover a pointer fine), inak je skryté
        jemne = [b.split('\n}\n', 1)[0] for b in css.split('@media (hover:hover) and (pointer:fine){')[1:]]
        self.assertTrue(any('.prace:not(.pripnute) .navod-mys{display:inline}' in b for b in jemne))
        self.assertIn('.navod-pocitac,.navod-mys{display:none}', css)
        self.assertEqual(len(re.findall(r'\.navod-mys\{display:inline\}', css)), 1)
        # blok pre dotyk je za blokom pre myš (pri rovnakej špecifickosti rozhoduje poradie)
        self.assertLess(css.index('.prace:not(.pripnute) .navod-mys{display:inline}'), css.index('@media (pointer:coarse){'))
        uvod = self.info('')['telo']
        self.assertIn(f'<span class="navod-mobil">{build.T(tp["navod_mobil"])}</span>', uvod)

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
        web = self.tmp / 'ci'
        kod, out, err = postav(web, self.obsah, prostredie={'GITHUB_ACTIONS': 'true'})
        self.assertEqual(kod, 0, err)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', (web / 'index.html').read_text(encoding='utf-8'))
        self.assertIn('href="/bmbuilding/kontakt/"', (web / '404.html').read_text(encoding='utf-8'))
        self.assertFalse((web / 'CNAME').exists())
        web2 = self.tmp / 'ci-ostry'
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
        self.assertIn('Projektant: Kappa 1 s.r.o. Termín', d)
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
        # maly_obsah nemá fotku aspoň 1280 px: úvod bez pohľadu A (nikdy malá ani rozmazaná zväčšená fotka), schéma
        # cez celú šírku; meno s olovnicou, veta, tlačidlá a činnosť ostávajú
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        s = (web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<section class="uvod bez-fotky"', s)
        self.assertNotIn('fetchpriority', s)
        self.assertNotIn('class="pohlad"', s)
        for kus in ('class="olovnica"', 'class="uvod-akcie"', 'id="sluzby"', 'class="plan uvod-plan"', 'id="postup"'):
            self.assertIn(kus, s)
        self.assertIn('.uvod.bez-fotky .uvod-plan{grid-column:1/-1}', (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8'))

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
            self.assertEqual('https://www.instagram.com/bmbuilding/' in uzol.get('sameAs', []), ma_byt, adresa)

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
        self.assertIsNone(r['len-viz-bez']['obal'])
        self.assertIsNone(r['len-viz-bez']['karta'])
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
        # pas_vyber šablóny nemá v tomto obsahu ani jednu realizáciu: varovania a záložný výber, nie chyba
        self.assertEqual([r for r in self.err.splitlines() if r.strip() and 'presmerovanie' not in r and 'pas_vyber' not in r], [])
        self.assertIn('pas_vyber: doplnené záložným výberom', self.err)
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
        # bez zoznamu pas_vyber v obsahu: prvých uvod_pocet_realizacii (8) s fotkou karty
        self.assertEqual(uvod.split('id="prace"', 1)[1].split('</section>', 1)[0].count('<li class="projekt">'), 8)
        self.assertIn('Všetky realizácie (120)', uvod)
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

    def test_pas_najviac_desat(self):
        uvod = (self.web / 'index.html').read_text(encoding='utf-8')
        sekcia = uvod.split('id="prace"', 1)[1].split('</section>', 1)[0]
        karty = sekcia.count('<li class="projekt">')
        self.assertGreater(karty, 0)
        self.assertLessEqual(karty, build.PAS_MAX)
        self.assertEqual(build.PAS_MAX, 10)
        self.assertIn(f'Všetky realizácie ({self.pocet})', sekcia)

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

    def test_pas_vyber_zalozny_a_limit(self):
        w = skutocny()['web']
        povodne = dict(w.nastavenia)
        vsetky = [r['slug'] for r in w.realizacie if r['karta']]
        try:
            # viac ako 10 platných a jeden neexistujúci: najviac 10, varovanie, nie chyba
            w.nastavenia['pas_vyber'] = vsetky[:12] + ['neexistuje-xyz']
            with redirect_stderr(io.StringIO()) as e:
                build.VAROVANIA.clear()
                v = w.pas_vyber()
            self.assertEqual(len(v), build.PAS_MAX)
            self.assertIn("'neexistuje-xyz' nie je v obsahu", e.getvalue())
            self.assertIn(f'pás ukáže prvých {build.PAS_MAX}', e.getvalue())
            # chýbajúci slug v krátkom zozname: doplní záložný výber do pôvodného počtu
            w.nastavenia['pas_vyber'] = vsetky[:3] + ['neexistuje-xyz']
            with redirect_stderr(io.StringIO()) as e:
                v = w.pas_vyber()
            self.assertEqual(len(v), 4)
            self.assertTrue(set(vsetky[:3]) <= {r['slug'] for r in v})
            self.assertIn('doplnené záložným výberom', e.getvalue())
            # bez pas_vyber: záložný výber uvod_pocet_realizacii (najviac 10) s varovaním, poradie podľa zoznamu
            del w.nastavenia['pas_vyber']
            with redirect_stderr(io.StringIO()) as e:
                v = w.pas_vyber()
            self.assertEqual(len(v), min(int(povodne.get('uvod_pocet_realizacii', 8)), build.PAS_MAX))
            self.assertIn('pas_vyber chýba', e.getvalue())
            poradie = [r['slug'] for r in w.realizacie]
            self.assertEqual([r['slug'] for r in v], sorted((r['slug'] for r in v), key=poradie.index))
            self.assertTrue(all(r['karta'] for r in v))
            w.nastavenia['uvod_pocet_realizacii'] = 40
            with redirect_stderr(io.StringIO()):
                self.assertLessEqual(len(w.pas_vyber()), build.PAS_MAX)
        finally:
            w.nastavenia.clear()
            w.nastavenia.update(povodne)
            build.VAROVANIA.clear()


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
        pripady = (('index.html', 'projekt', ['kota', 'projekt-riadok'], ['projekt-udaje', 'projekt-text']),
                   ('realizacie/index.html', 'dlazdica', ['kota', 'dlazdica-nazov'], ['projekt-udaje', 'projekt-text']),
                   ('stavebny-dozor/index.html', 'dlazdica', ['kota', 'dlazdica-nazov'], ['projekt-udaje', 'projekt-text']),
                   ('novinky/index.html', 'novinka-karta', ['kota', 'novinka-nazov'], ['projekt-ilustr', 'projekt-udaje', 'projekt-text']))
        for cesta, trieda, zaciatok, volitelne in pripady:
            r = CastiKariet(trieda)
            r.feed(self.telo(cesta))
            self.assertGreaterEqual(len(r.karty), 2, cesta)
            for casti in r.karty:
                self.assertEqual(casti[:2], zaciatok, (cesta, casti))
                zvysok = casti[2:]
                self.assertEqual(zvysok, [c for c in volitelne if c in zvysok], (cesta, casti))
                # riadkov subgrid je dosť pre všetky časti: fotka + kóta + nadpis + voliteľné
                self.assertLessEqual(len(casti), 2 + len(volitelne))
        # novinka s ilustračnou fotkou aj stavbou a úryvkom (taký pár skutočný obsah dnes nemá): všetkých 5 častí
        # v poradí riadkov, popis ilustračnej fotky v 3. riadku pred stavbou
        w = skutocny()['web']
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
        # tmavý pás: mriežka so stĺpcom na kartu a 5 riadkami, karty a karta Ďalšie realizácie cez všetky riadky
        for kus in ('.pas{display:grid;grid-auto-flow:column;grid-auto-columns:var(--karta);grid-template-rows:repeat(5,auto);row-gap:0}',
                    '.pas>.projekt{display:grid;grid-template-rows:subgrid;grid-row:1/-1}',
                    '.pas>.projekt-dalsi{grid-row:1/-1;aspect-ratio:auto}',
                    '.pas>.projekt>figure{display:grid;grid-template-rows:subgrid;grid-row:1/-1}',
                    '.pas>.projekt figcaption{display:grid;grid-template-rows:subgrid;grid-row:2/-1}',
                    '.pas figcaption>.projekt-udaje{grid-row:-3}', '.pas figcaption>.projekt-text{grid-row:-2}'):
            self.assertIn(kus, sg)
        # obmedzený pohyb: pás ako mriežka po riadkoch, karta cez 5 riadkov
        rm = ''.join(css_bloky(sg, '@media (prefers-reduced-motion:reduce)'))
        self.assertIn('.pas>.projekt,.pas>.projekt-dalsi{grid-row:span 5;', rm)
        # pripnutý pás bez subgrid: karty natiahnuté na rovnakú výšku, nie zarovnané hore
        pripnuty = deklaracie(self.zaklad, '.prace.pripnute .pas')
        self.assertEqual(pripnuty.get('align-items'), 'stretch')
        self.assertIn('--karta', pripnuty)
        # content-visibility zapína layout containment a karta by prestala byť subgrid
        self.assertNotRegex(self.css, r'\.dlazdica[^{}]*\{[^}]*content-visibility')

    def test_fazy_v_spolocnych_riadkoch(self):
        pc = ''.join(b for b in css_bloky(self.css, '@media (min-width:1024px)'))
        fazy = ''.join(css_bloky(pc, '@supports (grid-template-rows:subgrid)'))
        self.assertIn('.fazy{grid-template-rows:repeat(4,auto)}', fazy)
        self.assertIn('.faza{display:grid;grid-template-rows:subgrid;grid-row:1/-1}', fazy)
        self.assertIn('.faza .vystup{grid-row:4}', fazy)
        telo = self.telo('index.html')
        for li in re.findall(r'<li class="faza">(.*?)</li>', telo, re.S):
            # číslo, nadpis, text a nanajvýš riadok Činnosť: 4 riadky subgrid
            self.assertRegex(li, r'^<span class="faza-dlzka[^"]*"[^>]*>.*?</span></span><h3>[^<]*</h3><p>.*?</p>(<p class="vystup">.*</p>)?$')

    def test_text_vedla_textu_na_zakladnej_ciare(self):
        # pravidlá pri každej šírke (mimo @media): štítok a hodnota, číslo činnosti a text, názov a cena
        self.assertEqual(deklaracie(self.zaklad, '.fakty>div').get('align-items'), 'baseline')
        self.assertEqual(deklaracie(self.zaklad, '.cinnosti li').get('align-items'), 'baseline')
        self.assertNotIn('padding-top', deklaracie(self.zaklad, '.cinnost-c'))
        self.assertEqual(deklaracie(self.zaklad, '.sluzba').get('align-items'), 'baseline')
        # kratší sused sa natiahne na výšku dlhšieho, voľné miesto ide pod text
        self.assertEqual(deklaracie(self.zaklad, '.susedia a').get('align-content'), 'start')
        # popis pod číslom v cenníku sa zalamuje (pri 1024 px pretiekol okraj stránky o 6 px)
        self.assertEqual(deklaracie(self.zaklad, '.cena small').get('white-space'), 'normal')

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
        selektory = ('.fakty>div', '.cinnosti li', '.sluzba', '.cena small', '.susedia a', '.doklady', '.doklady .doklad-h',
                     '.doklad-obr', '.doklad-popis', '.doklad-register', '.projekt-riadok', '.projekt-udaje',
                     '.projekt-text', '.projekt-svetly .kota', '.projekt-svetly .dlazdica-nazov,.projekt-svetly .novinka-nazov',
                     '.projekt-svetly .projekt-text', '.projekt-ilustr', '.sekcia-karty .karty', '.faza', '.faza h3',
                     '.faza p', '.faza .vystup', '.mapa-spodok', '.vizitka')
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
        pas = re.findall(r'<ul class="lb-zoznam" data-galeria="[a-z0-9-]+" data-cesta="([^"]*)"[^>]*>(.*?)</ul>', uvod, re.S)
        self.assertTrue(pas)
        for cesta_pasu, polozky in pas:
            for nn, sirky in re.findall(r'<a href="[^"]*" data-lightbox data-nn="(\d\d)" data-sirky="[^"]*" '
                                        r'data-vyska="\d+" data-sirky-avif="([^"]*)"', polozky):
                for w in sirky.split():
                    self.assertTrue((self.web / f'{cesta_pasu}{nn}-{w}.avif').is_file(), (cesta_pasu, nn, w))
        self.assertIn('data-sirky-avif="480 720 960 1440 1920 2200"', uvod)
        karty = re.findall(r'<img src="prazdna-45\.webp" data-src="[^"]*" data-srcset="[^"]*" data-avif="([^"]*)"', uvod)
        self.assertTrue(karty)
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
        self.assertGreater(len(fotky), 2)
        prve = [i for i in obr if 'fetchpriority' in i]
        self.assertEqual(len(prve), 1, prve)
        self.assertIn('class="hrdina-foto"', prve[0])
        self.assertIn('fetchpriority="high"', prve[0])
        self.assertNotIn('loading="lazy"', prve[0])
        for i in fotky:
            if i is prve[0] or 'class="hrdina-foto"' in i:
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
        self.assertEqual(len(pre), 2)
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

    def test_karta_foto_z_nastaveni(self):
        nast = self.w.nastavenia.get('karta_foto')
        self.assertIsInstance(nast, dict)
        self.assertEqual(nast.get('novostavba-rd-nitrianske-hrnciarovce', {}).get('subor'), '09.jpg')
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
        # karta pásu, titulná fotka detailu aj karta mriežky Hrnčiaroviec sú 09.jpg (výrez 4 : 5 a celá fotka)
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        hrn = uvod.split('href="realizacie/novostavba-rd-nitrianske-hrnciarovce/"', 1)[0].rsplit('<li class="projekt">', 1)[1]
        self.assertIn('novostavba-rd-nitrianske-hrnciarovce/09-960-45.webp', hrn)
        self.assertNotIn('/01-', hrn)
        detail = (self.ostry / 'realizacie' / 'novostavba-rd-nitrianske-hrnciarovce' / 'index.html').read_text(encoding='utf-8')
        pohlad = detail.split('<figure class="pohlad detail-foto">', 1)[1].split('</figure>', 1)[0]
        self.assertIn('novostavba-rd-nitrianske-hrnciarovce/09-', pohlad)
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
        finally:
            self.w.nastavenia['karta_foto'] = stare

    def test_nahradena_karta_nie_je_prva_velka_v_galerii(self):
        """Fotka karty nahradená nastavením karta_foto (malý zdroj) nesmie byť prvou veľkou fotkou galérie: inak by sa
        mäkká fotka ukázala cez celú šírku galérie a jej bajty by na mobile zdržali LCP (overenie zostavy bmb-presnost,
        Lighthouse detailu Hrnčiaroviec 89 pred opravou). V galérii ostáva ako bežná fotka."""
        podla = {r['slug']: r for r in self.w.realizacie}
        hrn = podla['novostavba-rd-nitrianske-hrnciarovce']
        self.assertEqual(hrn['karta_nahradena']['rel'], 'novostavba-rd-nitrianske-hrnciarovce/01.jpg')
        # predpoklad: nahradená fotka je prvá v galérii a inak by prvou veľkou fotkou bola
        self.assertIs(hrn['galeria'][0], hrn['karta_nahradena'])
        self.assertTrue(self.w.nezvacsena(hrn['galeria'][0], build.GALERIA_BUNKY['prva']))
        for slug in self.w.nastavenia['karta_foto']:
            r = podla[slug]
            detail = (self.ostry / 'realizacie' / slug / 'index.html').read_text(encoding='utf-8')
            galeria = detail.split('<ul class="galeria', 1)[1].split('</ul>', 1)[0]
            polozky = galeria.split('<li class="galeria-polozka')[1:]
            self.assertEqual(len(polozky), len(r['galeria']), slug)
            nahradena = r.get('karta_nahradena')
            if not nahradena:
                continue
            # súbor na webe: <obr_dir>/<nn>-<šírka>.<formát> (nn je poradie fotky, nie meno v obsah/)
            s_nou = [p for p in polozky if f'{r["obr_dir"]}/{nahradena["nn"]}-' in p]
            self.assertEqual(len(s_nou), 1, slug)
            self.assertFalse(s_nou[0].startswith(' prva'), slug)
        hrn_detail = (self.ostry / 'realizacie' / 'novostavba-rd-nitrianske-hrnciarovce' / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('galeria-polozka prva', hrn_detail)

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

    def test_karty_pasu_s_dost_velkym_zdrojom(self):
        with redirect_stderr(io.StringIO()):
            pas = self.w.pas_vyber()
        self.assertTrue(pas)
        def vyrez_karty(f):
            w, h = f['w'], f['h']
            if f.get('vyrez45'):
                x0, y0, x1, y1 = build.box_px(f['vyrez45'], w, h)
                w, h = x1 - x0, y1 - y0
            return build.orez_rozmer(w, h, (4, 5))[0]
        for r in pas:
            f = r['karta']
            cw = vyrez_karty(f)
            self.assertGreaterEqual(cw, MIN_OREZ_PASU, (r['slug'], f['rel'], cw))
        # Hrnčiarovce: výrez aspoň 960 px, teda bez zväčšenia aj pri 1920 px a hustote 2 (zadanie: karta musí byť ostrá),
        # od brány 1 (nález 9) z rámika s viac domom a menej hliny
        hrn = next(r for r in pas if r['slug'] == 'novostavba-rd-nitrianske-hrnciarovce')
        self.assertTrue(hrn['karta'].get('vyrez45'))
        self.assertGreaterEqual(vyrez_karty(hrn['karta']), 960)

    def test_titulne_fotky_prisnejsi_prah(self):
        sys.path.insert(0, str(KOREN / 'nastroje'))
        import predlohy_web as pw
        self.assertGreaterEqual(pw.PRAH_TITULNE['avif'], 0.997)
        self.assertGreaterEqual(pw.PRAH_TITULNE['webp'], 0.997)
        self.assertGreater(pw.PRAH_TITULNE['avif'], pw.PRAH['avif'])
        titulne = pw.titulne_fotky(self.w)
        hrdina = self.w.hrdina_foto()[1]
        self.assertIn(hrdina['sha'], titulne)
        for r in self.w.realizacie:
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
        ai = [z for z in man.values() if z['predloha'].get('model') in pw.MODELY_AI]
        self.assertTrue(ai)
        self.assertTrue(any(z.get('titulna') for z in ai))
        for z in ai:
            w, _ = build.rozmery_zdroja(KOREN / 'obsah' / 'obrazky' / z['rel'])
            k = pw.ZDROJ_LANCZOS_MAX_TITULNE if z.get('titulna') else 1.25
            self.assertEqual(z.get('zo_zdroja_do'), int(w * k), z['rel'])
        # karta novinky Vianočný pozdrav (Rosnička 14, výrez 734 px): súbor 960 px výrezu je zo zdroja, nie z AI
        ros = next(z for z in man.values() if z['rel'] == 'bytovy-dom-rosnicka-bratislava/14.jpg')
        self.assertTrue(ros.get('titulna'))
        self.assertGreaterEqual(build.orez_rozmer(1224, 918, (4, 5))[0] * pw.ZDROJ_LANCZOS_MAX_TITULNE, 960)

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
            s = (self.ostry / stranka / 'index.html').read_text(encoding='utf-8')
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
        for r in self.w.realizacie + self.w.novinky:
            f = self.w.foto_karty(r) if r in self.w.realizacie else r.get('karta')
            if not f:
                continue
            cw = build.orez_rozmer(f['w'], f['h'], (4, 5))[0]
            self.assertEqual(self.w.mala_karta(f), cw < build.KARTA_MIN_OREZ, f['rel'])
            if self.w.mala_karta(f):
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

    def test_postup_a_firma_s_dost_velkym_zdrojom(self):
        """Brána bmb-presnost pokus 1, nález 3: fotky Postupu a O firme majú pole 868 CSS px pri 1920 px (1736 px
        obrazovky pri hustote 2); výrez 3 : 2 zdroja musí mať aspoň 1736 / 1,25 = 1389 px."""
        for kluc in ('postup_foto', 'firma_foto'):
            with redirect_stderr(io.StringIO()):
                r, f = self.w.foto_nastavenia(kluc)
            self.assertIsNotNone(f, kluc)
            self.assertGreaterEqual(build.orez_rozmer(f['w'], f['h'], (3, 2))[0], 1389, (kluc, f['rel']))
        uvod = (self.ostry / 'index.html').read_text(encoding='utf-8')
        postup = uvod.split('<figure class="postup-foto">', 1)[1].split('</figure>', 1)[0]
        self.assertIn(self.w.texty['postup']['foto_popis'], postup)
        r, f = self.w.foto_nastavenia('postup_foto')
        self.assertTrue(self.w.texty['postup']['foto_popis'].startswith(r['nazov']))

    def test_vyrez_karty_z_ramika(self):
        """Brána bmb-presnost pokus 1, nález 9: výrez 4 : 5 karty Hrnčiaroviec je z rámika karta_foto vyrez (viac domu,
        menej hliny), súbor fotky-web ho nesie v zázname vyrez a zhoduje sa s výrezom z rámika, nie zo stredu."""
        import numpy as np
        hrn = next(r for r in self.w.realizacie if r['slug'] == 'novostavba-rd-nitrianske-hrnciarovce')
        f = hrn['karta']
        box = f.get('vyrez45')
        self.assertTrue(box)
        with Image.open(self.ostry / 'obrazky' / hrn['slug'] / f'{f["nn"]}-480-45.webp') as im:
            web = np.asarray(im.convert('L'), dtype=np.float64)
        zdroj = build.otvor_rgb(Path(f['zdroj']))
        z_ramika = np.asarray(build.zmensi(zdroj.copy(), 480, 600, (4, 5), box).convert('L'), dtype=np.float64)
        zo_stredu = np.asarray(build.zmensi(zdroj.copy(), 480, 600, (4, 5)).convert('L'), dtype=np.float64)
        d_ram, d_str = float(np.abs(web - z_ramika).mean()), float(np.abs(web - zo_stredu).mean())
        self.assertLess(d_ram, 4, d_ram)
        self.assertGreater(d_str, 2 * d_ram, (d_ram, d_str))
        # zlý rámik (výrez pod 768 px) sa nepoužije a build varuje
        r = copy_realizacie(hrn)
        stare = self.w.nastavenia['karta_foto']
        f2 = dict(f)
        f2.pop('vyrez45', None)
        r['galeria'] = [f2 if x is f else x for x in r['galeria']]
        try:
            self.w.nastavenia['karta_foto'] = {r['slug']: {'subor': f['rel'].rsplit('/', 1)[1], 'vyrez': [0.4, 0.4, 0.6, 0.6]}}
            err = io.StringIO()
            with redirect_stderr(err):
                self.w.karta_z_nastaveni(r)
            self.assertNotIn('vyrez45', f2)
            self.assertIn('výrez', err.getvalue())
        finally:
            self.w.nastavenia['karta_foto'] = stare

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
