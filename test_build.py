"""Testy generátora build.py (unittest, bez siete).

Spustenie: python -m unittest -v test_build   (v priečinku products/bmbuilding)
"""
import base64
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
NEPOVINNE = {'li', 'p', 'dt', 'dd'}


class Rozbor(HTMLParser):
    """Prísny rozbor: párovanie značiek, odkazy, h1, meta, štýl a JSON-LD."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.zasobnik, self.chyby = [], []
        self.odkazy, self.h1, self.meta, self.base = [], 0, [], None
        self.styl, self.ldjson, self._v = [], [], None
        self.texty = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID:
            self.zasobnik.append(tag)
        if tag == 'h1':
            self.h1 += 1
        if tag == 'meta':
            self.meta.append(a)
        if tag == 'base':
            self.base = a.get('href')
        for k in ('href', 'src'):
            if a.get(k) and tag != 'base':
                self.odkazy.append(a[k])
        if a.get('srcset'):
            for kus in a['srcset'].split(','):
                self.odkazy.append(kus.strip().split(' ')[0])
        if tag == 'style':
            self._v = 'style'
        if tag == 'script':
            self._v = 'ld' if a.get('type') == 'application/ld+json' else 'script'
            if self._v == 'script':
                self.chyby.append('spustiteľný skript')

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
        elif self._v is None:
            self.texty.append(data)


def rozober(cesta):
    r = Rozbor()
    r.feed(cesta.read_text(encoding='utf-8'))
    r.close()
    if r.zasobnik:
        r.chyby.append(f'neuzavreté značky {r.zasobnik}')
    return r


def postav(vystup, obsah=None, nahlad=False, prostredie=None):
    argv = ['--vystup', str(vystup)]
    if obsah:
        argv += ['--obsah', str(obsah)]
    if nahlad:
        argv.append('--nahlad')
    out, err = io.StringIO(), io.StringIO()
    # testy bežia rovnako lokálne aj v GitHub Actions: premenné CI nastaví len test, ktorý ich skúša
    env = {k: v for k, v in os.environ.items() if k not in ('GITHUB_ACTIONS', 'BMB_OSTRY')}
    env.update(prostredie or {})
    with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
        kod = build.main(argv)
    return kod, out.getvalue(), err.getvalue()


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
    im = Image.new('RGB', (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = ((x * 7 + odtien) % 256, (y * 5 + odtien) % 256, ((x + y) * 3) % 256)
    cesta.parent.mkdir(parents=True, exist_ok=True)
    im.save(cesta, 'JPEG', quality=85)


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
                'zverejnene': True},
        'zly1': {'slug': 'Zlý Slug', 'nazov': 'Zlý slug'},
        'zly2': {'slug': '../von', 'nazov': 'Únik z priečinka'},
        'skryta': {'slug': 'skryta', 'nazov': 'Nezverejnená', 'zverejnene': False},
        'zz-dupl': {'slug': 'plna', 'nazov': 'Duplicitný slug'},
    }
    for meno, d in zaznamy.items():
        (obsah / 'realizacie' / f'{meno}.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    (obsah / 'realizacie' / 'pokazeny.json').write_text('{toto nie je json', encoding='utf-8')
    return obsah


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
    s = subor.read_text(encoding='utf-8')
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', s)
    if not m:
        return 'chýba CSP'
    politika = m.group(1)
    for blok, smernica in (('style', 'style-src'), ('script', 'script-src')):
        for obsah in re.findall(rf'<{blok}[^>]*>(.*?)</{blok}>', s, re.S):
            h = "'sha256-" + base64.b64encode(hashlib.sha256(obsah.encode('utf-8')).digest()).decode() + "'"
            casti = [c.strip() for c in politika.split(';')]
            if not any(c.startswith(smernica) and h in c for c in casti):
                return f'{smernica} nemá hash bloku {blok}'
    if 'unsafe-inline' in politika:
        return 'unsafe-inline'
    return ''


class TestSkutocnyObsah(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        cls.ostry = cls.tmp / 'ostry'
        cls.nahlad = cls.tmp / 'nahlad'
        cls.kod_o, cls.out_o, cls.err_o = postav(cls.ostry)
        cls.kod_n, cls.out_n, cls.err_n = postav(cls.nahlad, nahlad=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_beh_prejde(self):
        self.assertEqual(self.kod_o, 0, self.err_o)
        self.assertEqual(self.kod_n, 0, self.err_n)
        self.assertEqual(self.err_o.strip(), '', 'skutočný obsah nemá mať varovania')

    def test_stranky_existuju(self):
        for c in ('index.html', 'stavebny-dozor/index.html', 'stavbyveduci/index.html', 'stavebny-manazment/index.html',
                  'realizacie/index.html', 'opravnenia/index.html', 'kontakt/index.html', 'ochrana-udajov/index.html',
                  '404.html', 'index.php/index.html', 'index.php/cinnost/index.html', 'fonts/archivo-400-700.woff2',
                  'fonts/OFL.txt', 'og/bmbuilding.jpg', 'favicon.svg', 'favicon.ico'):
            self.assertTrue((self.ostry / c).is_file(), c)
        for p in sorted((KOREN / 'obsah' / 'realizacie').glob('*.json')):
            d = json.loads(p.read_text(encoding='utf-8'))
            if d.get('zverejnene') is not False:
                self.assertTrue((self.ostry / 'realizacie' / d['slug'] / 'index.html').is_file(), d['slug'])
                self.assertTrue((self.ostry / 'og' / f'{d["slug"]}.jpg').is_file(), d['slug'])

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
        for subor in sorted(self.ostry.rglob('*.html')):
            if 'index.php' in subor.relative_to(self.ostry).parts:
                continue
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
        self.assertGreaterEqual(len(urls), 28)
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

    def test_canonical_og_a_relativne_cesty(self):
        s = (self.ostry / 'realizacie' / 'bytovy-dom-rosnicka-bratislava' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/realizacie/bytovy-dom-rosnicka-bratislava/">', s)
        self.assertIn('content="https://bmbuilding.sk/og/bytovy-dom-rosnicka-bratislava.jpg"', s)
        # brána dizajnu pokus 1, nález 6: H1 je stavba s miestom, rola a roky sú v malom riadku nad ním
        t = s.replace(' ', ' ')
        self.assertIn('<h1>Bytový dom Rosnička, Bratislava</h1>', t)
        self.assertIn('<p class="nad-h1">Stavebný dozor, TDI · <span class="cisla">2015 až 2016</span></p>', t)
        self.assertIn('href="../../fonts/archivo-400-700.woff2"', s)
        self.assertNotRegex(s, r'(href|src)="/(?!/)')
        with Image.open(self.ostry / 'og' / 'bytovy-dom-rosnicka-bratislava.jpg') as im:
            self.assertEqual((im.size, im.format), ((1200, 630), 'JPEG'))

    def test_obrazky_nie_su_vacsie_ako_original(self):
        for p in sorted((self.ostry / 'obrazky').rglob('*.webp')):
            sirka = int(p.stem.split('-')[1])
            with Image.open(p) as im:
                self.assertEqual(im.width, sirka, p)
                self.assertLessEqual(im.width, 1440)
        s = (self.ostry / 'index.html').read_text(encoding='utf-8')
        lcp = re.findall(r'<img[^>]*fetchpriority="high"[^>]*>', s)
        self.assertEqual(len(lcp), 1)
        self.assertNotIn('loading="lazy"', lcp[0])
        for img in re.findall(r'<img[^>]*>', s):
            self.assertRegex(img, r'width="\d+" height="\d+"')
            self.assertRegex(img, r'alt="[^"]+"')

    def test_presmerovania(self):
        s = (self.ostry / 'index.php' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('content="0; url=../"', s)
        self.assertIn('<link rel="canonical" href="https://bmbuilding.sk/">', s)
        s = (self.ostry / 'index.php' / 'cinnost' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('url=../../#sluzby', s)
        s = (self.ostry / 'index.php/referencie/realizovane-projekty/5-rezidencia-grunty-bratislava-karlova-ves/index.html').read_text(encoding='utf-8')
        self.assertIn('https://bmbuilding.sk/realizacie/rezidencia-grunty-bratislava/', s)

    def test_bez_pomlciek_a_vykricnikov(self):
        for p in sorted(self.ostry.rglob('*')):
            if p.suffix in ('.html', '.xml', '.txt') and p.name != 'OFL.txt':
                s = p.read_text(encoding='utf-8')
                self.assertNotIn('–', s, p)
                self.assertNotIn('—', s, p)
                self.assertNotIn('!', s.replace('<!doctype html>', '', 1), p)

    def test_druhy_beh_rovnake_bajty_a_bez_kodovania(self):
        pred = odtlacky(self.ostry)
        kod, out, err = postav(self.ostry)
        self.assertEqual(kod, 0, err)
        self.assertIn('(nových 0)', out)
        self.assertEqual(odtlacky(self.ostry), pred)


class TestDocasnyObsah(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        self.obsah = maly_obsah(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_chybajuce_polia_zle_slugy_a_xss(self):
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        realizacie = sorted(p.name for p in (web / 'realizacie').iterdir() if p.is_dir())
        self.assertEqual(realizacie, ['hola', 'plna', 'xss'])
        self.assertIn('Zlý Slug', err)
        self.assertIn('../von', err)
        self.assertIn('duplicitný', err)
        self.assertIn('99.jpg', err)
        self.assertNotIn('nezverejnen', (web / 'realizacie' / 'index.html').read_text(encoding='utf-8').lower())
        # holá realizácia: bez fotiek, rokov, projektanta; bez modrej dosky (brána dizajnu pokus 1, nález 2)
        s = (web / 'realizacie' / 'hola' / 'index.html').read_text(encoding='utf-8')
        telo = s.split('<main', 1)[1].split('<footer', 1)[0]
        self.assertNotIn('class="doska"', telo)
        self.assertNotIn('Projektant', telo)
        # plná: pomlčka medzi rokmi dá „až“, výkričník bodku, 2 fotky (chýbajúca a mimo priečinka vynechané)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('2019 až 2021', s)
        self.assertEqual(len(re.findall(r'<img ', s)), 2)
        self.assertTrue((web / 'obrazky' / 'plna' / '02-500.webp').is_file())
        self.assertFalse((web / 'obrazky' / 'plna' / '02-960.webp').exists())
        # XSS: nič spustiteľné, všetko escapované, JSON-LD bez </script> vo vnútri
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
        s = (web / 'realizacie' / 'xss' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', s)
        self.assertIn('&quot;onerror=alert(1)', s)
        self.assertIn('&lt;b&gt;tučné&lt;/b&gt;', s)
        self.assertEqual(interne_odkazy(web, '/'), [])

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
        kod, _, _ = postav(self.obsah, self.obsah)
        self.assertEqual(kod, 2)
        kod, _, _ = postav(self.obsah / 'realizacie', self.obsah)
        self.assertEqual(kod, 2)

    def test_prazdny_obsah(self):
        for p in (self.obsah / 'realizacie').glob('*.json'):
            p.unlink()
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertTrue((web / 'index.html').is_file())
        self.assertEqual(interne_odkazy(web, '/'), [])


ZLY_KONIEC = re.compile(r'(\s(na|č\.?|a|i|v|vo|s|so|z|zo|k|ku|o|pre|pri|do|od|po|za|pod|nad)|\d|[,;:.])( \| BM Building)?$')


class TestOpravyPokus1(unittest.TestCase):
    """Nálezy brány pokus 1 (kontrola/brana-pokus-1-spravnost.md a brana-pokus-1-dizajn-ux.md) nad skutočným obsahom."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        cls.ostry = cls.tmp / 'ostry'
        cls.kod, cls.out, cls.err = postav(cls.ostry)
        cls.realizacie = {}
        for p in sorted((KOREN / 'obsah' / 'realizacie').glob('*.json')):
            d = json.loads(p.read_text(encoding='utf-8'))
            cls.realizacie[d['slug']] = d

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def info(self, cesta):
        return stranka_info(self.ostry / cesta / 'index.html' if cesta else self.ostry / 'index.html')

    # --- správnosť 3: titulky realizácií nie sú useknuté a majú miesto
    def test_titulky_realizacii(self):
        self.assertEqual(self.kod, 0, self.err)
        titulky = {}
        for slug in self.realizacie:
            t = self.info(f'realizacie/{slug}/')['title']
            titulky[slug] = t
            self.assertLessEqual(len(t), 60, t)
            self.assertNotRegex(t, ZLY_KONIEC, t)
        self.assertIn('Okánikova', titulky['rekonstrukcia-okanikova-bratislava'])
        self.assertIn('Ventúrska č. 3', titulky['rekonstrukcia-venturska-bratislava'])
        self.assertIn('Špitálska', titulky['filmovy-klub-spitalska-bratislava'])
        self.assertIn('5 ambulancií', titulky['rekonstrukcia-5-ambulancii-pre-lekarov'])
        self.assertIn('Moste SNP', titulky['ufo-most-snp-bratislava'])
        self.assertEqual(len(set(titulky.values())), len(titulky))

    # --- správnosť 4: description bez „s.r.o..“
    def test_description_bez_dvojitej_bodky(self):
        for p in sorted(self.ostry.rglob('*.html')):
            if 'index.php' in p.relative_to(self.ostry).parts:
                continue
            self.assertNotIn('..', stranka_info(p)['desc'], p)

    # --- správnosť 7: UFO bez role, CreativeWork bez creator a about
    def test_rola_nevymyslena(self):
        veta = self.info('realizacie/')['telo']
        self.assertNotIn('pôsobil ako stavebný dozor, projektový manažér alebo', veta)
        for slug, d in self.realizacie.items():
            dielo = [u for u in self.info(f'realizacie/{slug}/')['ld'] if u.get('@type') == 'CreativeWork'][0]
            self.assertNotIn('creator', dielo, slug)
            self.assertNotIn('about', dielo, slug)
            if d.get('rola'):
                self.assertEqual(dielo.get('contributor'), {'@id': 'https://bmbuilding.sk/#firma'}, slug)
            else:
                self.assertNotIn('contributor', dielo, slug)

    # --- správnosť 8 a 9, dizajn 15: texty bez zdroja a budúci čas
    def test_texty_bez_zdroja(self):
        ufo = self.info('realizacie/ufo-most-snp-bratislava/')
        self.assertNotIn('Termín', ufo['telo'])
        self.assertNotIn('Termín', ufo['desc'])
        self.assertIn('Kolaudácia v novembri 2025', ufo['telo'].replace(' ', ' '))
        for p in sorted(self.ostry.rglob('*.html')):
            self.assertNotIn('Najrýchlejšie', p.read_text(encoding='utf-8'), p)
        self.assertIn('Zavolajte alebo napíšte e-mail.', self.info('kontakt/')['telo'])
        vt = self.info('realizacie/bytove-domy-vtacnik-bratislava/')['telo']
        self.assertNotIn('bude sedem', vt)
        self.assertIn('Projekt tvorí sedem', vt.replace(' ', ' '))

    # --- správnosť 10: ochrana údajov podľa čl. 13
    def test_ochrana_udajov_uplna(self):
        t = self.info('ochrana-udajov/')['telo'].replace(' ', ' ')
        for kus in ('čl. 6 ods. 1 písm. b)', 'čl. 6 ods. 1 písm. f)', 'uchovávame', 'USA', 'GitHub, Inc.',
                    'https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement'):
            self.assertIn(kus, t)

    # --- správnosť 6: lastmod a datePublished
    def test_lastmod_a_datum_publikovania(self):
        vydanie = json.loads((KOREN / 'sablona' / 'nastavenia.json').read_text(encoding='utf-8'))['vydanie']
        ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
        for u in ET.parse(self.ostry / 'sitemap.xml').getroot().findall(f'{ns}url'):
            self.assertGreaterEqual(u.find(f'{ns}lastmod').text, vydanie, u.find(f'{ns}loc').text)
        vt = [u for u in self.info('realizacie/bytove-domy-vtacnik-bratislava/')['ld'] if u.get('@type') == 'CreativeWork'][0]
        self.assertNotIn('datePublished', vt)
        dv = [u for u in self.info('realizacie/dvojdom-s-bratislava/')['ld'] if u.get('@type') == 'CreativeWork'][0]
        self.assertEqual(dv.get('datePublished'), '2026-10-04')

    # --- správnosť 12: pojmy nového stavebného zákona
    def test_stavebny_dozor_pojmy(self):
        t = self.info('stavebny-dozor/')['telo'].replace(' ', ' ')
        self.assertIn('rozhodnutia o stavebnom zámere', t)

    # --- dizajn 1, 4: mriežky najprv s fotkou, na úvode bez hlavnej fotky, bez fotky ako kompaktný zoznam
    def test_mriezky_s_fotkou_a_zoznam_bez_fotky(self):
        for cesta in ('', 'realizacie/', 'stavebny-dozor/', 'stavbyveduci/', 'stavebny-manazment/'):
            telo = self.info(cesta)['telo']
            self.assertNotIn('class="doska"', telo, cesta)
            for karta in re.findall(r'<li class="karta">(.*?)</li>', telo, re.S):
                self.assertIn('<img', karta, cesta)
        uvod = self.info('')['telo']
        mriezka = uvod.split('<ul class="karty">', 1)[1].split('</ul>', 1)[0]
        self.assertEqual(mriezka.count('<li class="karta">'), 6)
        self.assertNotIn('realizacie/bytove-domy-vtacnik-bratislava/', mriezka)
        self.assertIn('Ďalšie realizácie', uvod)
        dalsie = uvod.split('class="zoznam-dalsie"', 1)[1].split('</ul>', 1)[0]
        self.assertIn('realizacie/dvojdom-s-bratislava/', dalsie)
        vsetky = self.info('realizacie/')['telo']
        dalsie = vsetky.split('class="zoznam-dalsie"', 1)[1].split('</ul>', 1)[0]
        bez_fotky = sorted(s for s, d in self.realizacie.items() if not d.get('fotky'))
        self.assertEqual(sorted(re.findall(r'realizacie/([a-z0-9-]+)/', dalsie)), bez_fotky)
        self.assertIn('príspevok na Facebooku 4. 10. 2026', dalsie.replace(' ', ' '))

    # --- dizajn 2 a 11: detail bez fotky bez dosky, s odkazom na Facebook
    def test_detail_bez_fotky(self):
        t = self.info('realizacie/dvojdom-s-bratislava/')['telo']
        self.assertNotIn('class="doska"', t)
        self.assertIn('href="https://www.facebook.com/BmBuilding"', t)
        self.assertIn('4. 10. 2026', t.replace(' ', ' '))

    # --- dizajn 3: pôvodný stav viditeľne
    def test_povodny_stav_viditelne(self):
        vsetky = self.info('realizacie/')['telo']
        karta = vsetky.split('realizacie/rekonstrukcia-okanikova-bratislava/', 1)[0].rsplit('<li class="karta">', 1)[1]
        self.assertIn('Pôvodný stav pred rekonštrukciou', karta)
        ok = self.info('realizacie/rekonstrukcia-okanikova-bratislava/')['telo']
        popisy = re.findall(r'<figcaption>(.*?)</figcaption>', ok, re.S)
        self.assertEqual(len(popisy), 3)
        self.assertTrue(all('pôvodný stav' in p.replace(' ', ' ') for p in popisy))
        vt = self.info('realizacie/bytove-domy-vtacnik-bratislava/')['telo']
        self.assertNotIn('<figcaption>', vt)
        # karta s pôvodným stavom nie je medzi prvými kartami úvodu
        uvod = self.info('')['telo'].split('<ul class="karty">', 1)[1].split('</ul>', 1)[0]
        self.assertNotIn('rekonstrukcia-okanikova-bratislava', uvod)

    # --- dizajn 5: lišta dôvery
    def test_lista_dovery(self):
        t = self.info('')['telo'].replace(' ', ' ')
        dovera = t.split('class="dovera"', 1)[1].split('</section>', 1)[0]
        self.assertIn('spoločnosť v obchodnom registri', dovera)
        self.assertIn('<b>2005</b>', dovera)
        self.assertIn(f'<b>{len(self.realizacie)}</b><span>realizácií od roku 2003</span>', dovera)
        self.assertNotIn('na webe', dovera)
        self.assertNotIn('sídlo', dovera)

    # --- kolo 2 (Lighthouse lcp-discovery): prvá karta na /realizacie/ bez lazy, ostatné lazy
    def test_prva_karta_realizacii_bez_lazy(self):
        imgs = re.findall(r'<img[^>]*>', self.info('realizacie/')['telo'])
        self.assertIn('fetchpriority="high"', imgs[0])
        self.assertNotIn('loading="lazy"', imgs[0])
        self.assertTrue(all('loading="lazy"' in i for i in imgs[1:]))
        self.assertEqual(sum('fetchpriority' in i for i in imgs), 1)

    # --- dizajn 9: karta bez riadku Fotky
    def test_karta_bez_poctu_fotiek(self):
        self.assertNotIn('<dt>Fotky</dt>', self.info('realizacie/')['telo'])

    # --- dizajn 10: rozlíšiteľné nadpisy dokladov
    def test_nadpisy_dokladov(self):
        # na stránkach služieb sú doklady h2 (bok je pred prvým h2; brána dizajnu pokus 2, nález 1)
        for cesta, tag in (('', 'h3'), ('opravnenia/', 'h2'), ('stavebny-manazment/', 'h2')):
            t = self.info(cesta)['telo']
            nadpisy = re.findall(rf'<div class="doklad"><{tag}>(.*?)</{tag}>', t)
            self.assertGreaterEqual(len(nadpisy), 1, cesta)
            self.assertEqual(len(nadpisy), len(set(nadpisy)), cesta)
            self.assertFalse(any('Slovenská komora' in n for n in nadpisy), cesta)

    # --- dizajn 12: /kontakt/ bez druhej tabuľky firmy v päte
    def test_kontakt_bez_dvojitej_tabulky(self):
        self.assertNotIn('<h2>Firma</h2>', self.info('kontakt/')['html'].split('<footer', 1)[1])
        self.assertIn('<h2>Firma</h2>', self.info('opravnenia/')['html'].split('<footer', 1)[1])

    # --- dizajn 13, 14: galéria bez odkazu na rovnako veľký webp, section namiesto div s aria-label
    def test_galeria(self):
        t = self.info('realizacie/bytove-domy-vtacnik-bratislava/')['telo']
        self.assertNotIn('<a href="../../obrazky/', t)
        self.assertNotIn('<div class="galeria" aria-label', t)
        self.assertIn('<section class="galeria" aria-label="Fotky">', t)

    # --- dizajn 7: CSS mesta pod H1 (šírku galérie 960 px z nálezu 8 nahradilo rozloženie detailu v pokuse 2, nález 5)
    def test_css(self):
        css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        self.assertIn('.uvod h1 span{display:block;margin-top:16px;font-size:clamp(1.125rem,1rem + .6vw,1.5rem)', css)


class TestOpravyPokus1Docasne(unittest.TestCase):
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

    # --- správnosť 1: v GitHub Actions bez BMB_OSTRY=ano ide náhľad
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

    # --- správnosť 2: useknutá fotka sa vynechá, web ostane
    def test_poskodena_fotka_sa_vynecha(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        f = self.obsah / 'obrazky' / 'plna' / '01.jpg'
        f.write_bytes(f.read_bytes()[:3000])
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('01.jpg', err)
        self.assertTrue((web / 'index.html').is_file())
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(len(re.findall(r'<img ', s)), 1)

    # --- správnosť 2: chyba pri kódovaní obrázka nezmaže predchádzajúci výstup
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

    # --- správnosť 5: neplatný kalendárny dátum
    def test_neplatny_datum(self):
        self.uprav('plna', datum='2026-13-45')
        web = self.tmp / 'web'
        kod, out, err = postav(web, self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('2026-13-45', err)
        for p in web.rglob('*'):
            if p.is_file() and p.suffix in ('.html', '.xml'):
                self.assertNotIn('2026-13-45', p.read_text(encoding='utf-8'), p)

    # --- správnosť 6: pole upravene ide do lastmod a dateModified
    def test_upravene_v_lastmod(self):
        self.uprav('plna', upravene='2030-01-02')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        mapa = (web / 'sitemap.xml').read_text(encoding='utf-8')
        self.assertIn('<url><loc>https://bmbuilding.sk/realizacie/plna/</loc><lastmod>2030-01-02</lastmod></url>', mapa)
        self.assertIn('"dateModified":"2030-01-02"', (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8'))

    # --- správnosť 3: dlhý názov sa neseká uprostred vety; bez čiarky ostane celý (Google ho skráti sám),
    # s čiarkou ide začiatok po čiarku s mestom (pokus 2, správnosť 1 zmenila skrátenie po slovách)
    def test_skratenie_titulku(self):
        dlhy = 'Rekonštrukcia a nadstavba bytového domu na rohu ulíc pri starom moste č. 15 a 17 pre investora'
        self.uprav('plna', nazov=dlhy, miesto='Bratislava, Staré Mesto')
        self.uprav('xss', nazov='Rekonštrukcia a nadstavba bytového domu, na rohu ulíc pri starom moste č. 15 a 17',
                   miesto='Bratislava, Staré Mesto')
        self.uprav('holá', nazov='Dom', titulok='Vlastný titulok realizácie')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        t = stranka_info(web / 'realizacie' / 'plna' / 'index.html')['title']
        self.assertEqual(t, dlhy)
        t = stranka_info(web / 'realizacie' / 'xss' / 'index.html')['title']
        self.assertEqual(t, 'Rekonštrukcia a nadstavba bytového domu | BM Building')
        self.assertNotRegex(t, ZLY_KONIEC, t)
        self.assertEqual(stranka_info(web / 'realizacie' / 'hola' / 'index.html')['title'],
                         'Vlastný titulok realizácie | BM Building')

    # --- správnosť 4: bodka sa nezdvojí
    def test_description_bez_dvoch_bodiek(self):
        self.uprav('plna', projektant='Kappa 1 s.r.o.', roky='2019')
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        d = stranka_info(web / 'realizacie' / 'plna' / 'index.html')['desc']
        self.assertIn('Projektant: Kappa 1 s.r.o. Termín', d)
        self.assertNotIn('..', d)

    # --- dizajn 2: bez fotky ani dosky s prvým slovom názvu alebo náhradnou rolou
    def test_bez_fotky_bez_prveho_slova(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        for cesta in ('index.html', 'realizacie/index.html', 'realizacie/hola/index.html'):
            s = (web / cesta).read_text(encoding='utf-8')
            self.assertNotIn('class="doska"', s.split('<main', 1)[1], cesta)
            self.assertNotIn('>Holá</span>', s, cesta)
            self.assertNotIn('>Realizácia</span>', s, cesta)

    # --- dizajn 13: odkaz na väčšiu fotku len ak existuje väčšia ako 960 px
    def test_odkaz_na_vacsiu_fotku(self):
        web = self.tmp / 'web'
        self.assertEqual(postav(web, self.obsah)[0], 0)
        s = (web / 'realizacie' / 'plna' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<a href="../../obrazky/plna/01-1100.webp">', s)
        self.assertNotIn('<a href="../../obrazky/plna/02-', s)


class TestOpravyPokus2(unittest.TestCase):
    """Odporúčania brány pokus 2 (kontrola/brana-pokus-2-spravnost.md a brana-pokus-2-dizajn-ux.md), skutočný obsah."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        cls.ostry = cls.tmp / 'ostry'
        cls.nahlad = cls.tmp / 'nahlad'
        cls.kod, cls.out, cls.err = postav(cls.ostry)
        cls.kod_n, _, cls.err_n = postav(cls.nahlad, nahlad=True)
        cls.texty = json.loads((KOREN / 'sablona' / 'texty.json').read_text(encoding='utf-8'))
        cls.css = (KOREN / 'sablona' / 'styl.css').read_text(encoding='utf-8')
        cls.realizacie = {}
        for p in sorted((KOREN / 'obsah' / 'realizacie').glob('*.json')):
            d = json.loads(p.read_text(encoding='utf-8'))
            cls.realizacie[d['slug']] = d

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def info(self, cesta, koren=None):
        koren = koren or self.ostry
        return stranka_info(koren / cesta / 'index.html' if cesta else koren / 'index.html')

    # --- správnosť 2: stránka služby odkazuje na každú realizáciu s touto rolou (RD Pezinok na /stavebny-dozor/)
    def test_sluzby_odkazuju_na_vsetky_realizacie(self):
        self.assertEqual(self.kod, 0, self.err)
        for k, t in self.texty['sluzby'].items():
            telo = self.info(f'{k}/')['telo']
            for slug, d in self.realizacie.items():
                if any(kl in (d.get('rola') or '').lower() for kl in t['kluce']):
                    self.assertIn(f'realizacie/{slug}/', telo, f'{k}: {slug}')
        self.assertIn('realizacie/rd-pezinok/', self.info('stavebny-dozor/')['telo'])

    # --- dizajn 1: na stránke služby je osvedčenie s tlačidlom Zavolať a realizácie pred textom zákona
    def test_sluzby_poradie_obsahu(self):
        for k in self.texty['sluzby']:
            telo = self.info(f'{k}/')['telo']
            bok = telo.index('class="sluzba-bok"')
            self.assertLess(bok, telo.index('class="sluzba-hlavne"'), k)
            self.assertIn('href="tel:', telo[bok:telo.index('</aside>')], k)
            zakon = telo.find('Čo hovorí stavebný zákon')
            if zakon >= 0:
                if '<li class="karta">' in telo:   # stavbyvedúci zatiaľ nemá realizáciu s touto rolou
                    self.assertLess(telo.index('<li class="karta">'), zakon, k)
                self.assertLess(telo.index('href="tel:'), zakon, k)
        # bez skoku úrovne nadpisu: doklady v boku pred prvým h2 sú h2
        r = rozober(self.ostry / 'stavebny-dozor' / 'index.html')
        self.assertEqual(r.chyby, [])
        urovne = [int(m) for m in re.findall(r'<h([1-6])[ >]', self.info('stavebny-dozor/')['html'])]
        self.assertTrue(all(b <= a + 1 for a, b in zip(urovne, urovne[1:])), urovne)
        self.assertIn('@media (min-width:1024px){.sluzba{', self.css)
        self.assertIn('.sluzba-bok{grid-column:9/13;grid-row:1;', self.css)

    # --- dizajn 2: pod 640 px sa aktuálna položka omrviniek pri 3 a viac krokoch skryje, „/“ neostane sama
    def test_omrvinky_mobil(self):
        self.assertIn('@media (max-width:639px){.omrvinky li:last-child:nth-child(n+3){display:none}}', self.css)

    # --- dizajn 3: „·“ ostane na konci riadka (pevná medzera pred ním)
    def test_bodka_s_pevnou_medzerou(self):
        t = self.info('realizacie/dvojdom-s-bratislava/')['html']
        nad = re.search(r'<p class="nad-h1">(.*?)</p>', t).group(1)
        self.assertIn('\u00a0· ', nad)
        self.assertNotIn(' · ', nad.replace('\u00a0· ', ''))
        for udaje in re.findall(r'<span class="dalsie-udaje">(.*?)</span></li>', self.info('realizacie/')['telo']):
            self.assertNotIn(' · ', udaje.replace('\u00a0· ', ''))

    # --- dizajn 4: dlhá hodnota v tabuľke detailu ide cez celý riadok
    def test_dlha_hodnota_cez_celu_sirku(self):
        t = self.info('realizacie/rekonstrukcia-okanikova-bratislava/')['telo']
        self.assertRegex(t, r'<div class="cele"><dt>Projektant</dt>')
        self.assertRegex(t, r'<div><dt>Termín</dt>')
        # krátke Miesto pred celým riadkom by nechalo prázdnu polovicu, ide tiež cez celý riadok
        self.assertRegex(t, r'<div class="cele"><dt>Miesto</dt>')
        self.assertIn('.pole-siroke>.cele{grid-column:1/-1}', self.css)

    # --- dizajn 5: na desktope je tabuľka údajov vpravo vedľa textu a galérie, bez prázdneho pruhu
    def test_detail_dva_stlpce(self):
        t = self.info('realizacie/bytove-domy-vtacnik-bratislava/')['telo']
        self.assertIn('<div class="detail"><dl class="pole-siroke">', t)
        self.assertLess(t.index('<div class="detail-hlavne">'), t.index('<section class="galeria"'))
        self.assertIn('.detail>.pole-siroke{grid-column:9/13;grid-row:1;', self.css)
        self.assertNotIn('.galeria{max-width:960px}', self.css)
        self.assertIn('(min-width:1376px) 845px', t)

    # --- poistka k nálezu 5: predchádzajúca a ďalšia realizácia sú susedia v poradí zoznamu /realizacie/
    def test_susedia_v_poradi(self):
        zoznam = [u for u in self.info('realizacie/')['ld'] if u.get('@type') == 'ItemList'][0]['itemListElement']
        slugy = [u['url'].rstrip('/').rsplit('/', 1)[1] for u in zoznam]
        for i, slug in enumerate(slugy):
            t = self.info(f'realizacie/{slug}/')['telo']
            dalsia = re.search(r'<a class="dalsia" href="\.\./([a-z0-9-]+)/"', t)
            predch = re.search(r'<a class="predch" href="\.\./([a-z0-9-]+)/"', t)
            self.assertEqual(dalsia.group(1) if dalsia else None, slugy[i + 1] if i + 1 < len(slugy) else None, slug)
            self.assertEqual(predch.group(1) if predch else None, slugy[i - 1] if i else None, slug)

    # --- dizajn 6 a správnosť 8: 404 má tlačidlo Zavolať, nemá <base> a skok na obsah ostane na stránke
    def test_404(self):
        for koren, zaklad in ((self.ostry, '/'), (self.nahlad, '/bmbuilding/')):
            s = (koren / '404.html').read_text(encoding='utf-8')
            self.assertNotIn('<base', s)
            self.assertIn('<a class="preskocit" href="#obsah">', s)
            self.assertIn("base-uri 'none'", s)
            telo = s.split('<main', 1)[1].split('</main>', 1)[0]
            self.assertRegex(telo, r'<a class="tlacidlo tlacidlo-obrys" href="tel:\+421903794738">.*?Zavolať')
            self.assertIn(f'href="{zaklad}realizacie/"', telo)
            self.assertIn(f'url({zaklad}fonts/archivo-400-700.woff2)', s)
            for u in re.findall(r'(?:href|src)="([^"]+)"', s):
                if not re.match(r'^(https?:|tel:|mailto:|#)', u):
                    self.assertTrue(u.startswith(zaklad), u)

    # --- dizajn 7: rola pod hlavnou fotkou úvodu s veľkým písmenom a s „·“ ako nad H1
    def test_uvod_rola_ako_nad_h1(self):
        t = self.info('')['telo']
        cap = re.search(r'<figcaption class="pole-riadok">(.*?)</figcaption>', t).group(1)
        self.assertIn('Projektový manažér\u00a0· <span class="cisla">2016', cap)
        self.assertNotIn('>projektový manažér', cap)

    # --- správnosť 7: jeden WebSite s @id, kontakt ako jeden uzol, fotky v image
    def test_jsonld_graf(self):
        for p in sorted(self.ostry.rglob('index.html')):
            if 'index.php' in p.relative_to(self.ostry).parts:
                continue
            uzly = stranka_info(p)['ld']
            for u in uzly:
                if u.get('@type') == 'WebSite':
                    self.assertIn('@id', u, p)
                if u.get('isPartOf'):
                    self.assertEqual(u['isPartOf'], {'@id': 'https://bmbuilding.sk/#web'}, p)
            stranky = [u for u in uzly if 'WebPage' in (u['@type'] if isinstance(u['@type'], list) else [u['@type']])
                       or 'ContactPage' == u['@type']]
            self.assertEqual(len(stranky), 1, p)
            self.assertIn('@id', stranky[0], p)
        k = [u for u in self.info('kontakt/')['ld'] if 'ContactPage' in str(u.get('@type'))]
        self.assertEqual(len(k), 1)
        self.assertEqual(k[0]['@type'], ['WebPage', 'ContactPage'])
        self.assertEqual(k[0]['@id'], 'https://bmbuilding.sk/kontakt/')
        vt = [u for u in self.info('realizacie/bytove-domy-vtacnik-bratislava/')['ld'] if u['@type'] == 'CreativeWork'][0]
        self.assertTrue(all('/obrazky/' in i for i in vt['image']))


class TestOpravyPokus2Docasne(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bmb-test-'))
        self.obsah = maly_obsah(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- správnosť 1: dlhý názov z CMS nedá title končiaci na prídavnom mene ani bez názvu
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
            moznosti = {nazov, prva, f'{nazov}, {mesto}', f'{prva}, {mesto}'}
            self.assertIn(bez, moznosti, t)
            self.assertTrue(t.startswith(nazov.split(' ')[0]), t)
        self.assertEqual(w.titulok_realizacie({'nazov': pripady[0][0], 'miesto': pripady[0][1], 'titulok': ''}),
                         'Rekonštrukcia bytového domu a výmena výťahov | BM Building')
        self.assertEqual(w.titulok_realizacie({'nazov': pripady[1][0], 'miesto': 'Bratislava', 'titulok': ''}), pripady[1][0])

    # --- správnosť 1: pole titulok dlhšie než 60 znakov nezmizne ticho
    def test_dlhy_titulok_varuje(self):
        p = self.obsah / 'realizacie' / 'holá.json'
        d = json.loads(p.read_text(encoding='utf-8'))
        d['titulok'] = 'Titulok ' + 'veľmi dlhý ' * 7
        p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('titulok', err)
        self.assertIn('60', err)

    # --- správnosť 3: JSON s BOM (Poznámkový blok) sa zostaví
    def test_json_s_bom(self):
        (self.obsah / 'realizacie' / 'h.json').write_text(
            '\ufeff' + json.dumps({'slug': 'bom', 'nazov': 'S BOM'}), encoding='utf-8')
        f = self.obsah / 'firma.json'
        f.write_text('\ufeff' + f.read_text(encoding='utf-8'), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertTrue((self.tmp / 'web' / 'realizacie' / 'bom' / 'index.html').is_file(), err)
        self.assertNotIn('h.json', err)

    # --- správnosť 4: zlý typ poľa vo firma.json dá varovanie, nie traceback
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
        d['sluzby'] = [{'slug': 'stavebny-dozor', 'nazov': 'Dozor', 'cinnosti': 7}]
        d['opravnenia'] = [None, 3]
        f.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        kod, out, err = postav(self.tmp / 'web2', self.obsah)
        self.assertEqual(kod, 0, err)
        self.assertIn('cinnosti', err)


if __name__ == '__main__':
    unittest.main()
