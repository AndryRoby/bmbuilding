"""Testy stiahni.py (verzia 2, doplňovanie): lokálny http.server v tom istom procese, bez internetu.

Spúšťanie: python -B -m unittest discover -s products/bmbuilding/.github/obsah
"""
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import unittest

import stiahni

JPG = b'\xff\xd8\xff\xe0' + b'jpeg-ukazka-1' + b'\xff\xd9'
JPG2 = b'\xff\xd8\xff\xe0' + b'jpeg-ukazka-2' + b'\xff\xd9'
JPG3 = b'\xff\xd8\xff\xe0' + b'jpeg-ukazka-3-nova' + b'\xff\xd9'
JPG4 = b'\xff\xd8\xff\xe0' + b'jpeg-ukazka-4-novinka' + b'\xff\xd9'
PNG = b'\x89PNG\r\n\x1a\n' + b'png-ukazka'
# Id z Facebooku v tvare idPolozkyFb (products/arling-obsah/src/fb.mjs): fb-<číslice> alebo fb-<číslice>-<číslice>.
FB = 'fb-123-456'
# Id zo správy obsahu v tvare noveId (products/arling-obsah/ui/app.js): UUID v4.
SPRAVA = '0b6f1c2e-5d4a-4c3b-9a8e-7f6d5c4b3a21'
BEZ_ZMIEN = 'pridaní 0, zmien 0, mazaní 0 (JSON 0, fotky 0), stiahnutých fotiek 0, stav bez zmeny'


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Server:
    """Trasy: cesta → (kód, telo). Zaznamená cesty aj metódy požiadaviek (sťahovač smie len GET)."""

    def __init__(self):
        self.trasy = {}
        self.poziadavky = []
        self.metody = []
        server = self

        class Obsluha(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.poziadavky.append(self.path)
                server.metody.append(self.command)
                kod, telo = server.trasy.get(self.path, (404, b'{"chyba":"Nena\xc5\xa1lo sa."}'))
                self.send_response(kod)
                if kod in (301, 302):
                    self.send_header('Location', telo.decode())
                    telo = b''
                self.send_header('Content-Length', str(len(telo)))
                self.end_headers()
                self.wfile.write(telo)

            def ina_metoda(self):
                server.poziadavky.append(self.path)
                server.metody.append(self.command)
                self.send_response(405)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = ina_metoda

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Obsluha)
        self.vlakno = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.vlakno.start()
        self.zaklad = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def zastav(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def fotky(self):
        return [p for p in self.poziadavky if p.startswith('/f/')]


class TestStiahni(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.zastav)
        self.dir = tempfile.mkdtemp(prefix='stiahni-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.obsah = os.path.join(self.dir, 'obsah')
        os.makedirs(os.path.join(self.obsah, 'realizacie'))
        os.makedirs(os.path.join(self.obsah, 'obrazky', 'stara-web'))
        with open(os.path.join(self.obsah, 'firma.json'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('{"nazov": "BM Building s.r.o."}\n')
        with open(os.path.join(self.obsah, 'realizacie', 'stara-web.json'), 'w', encoding='utf-8') as f:
            json.dump({'id': 'web-stara-web', 'slug': 'stara-web', 'nazov': 'Stará', 'zdroj': 'web',
                       'fotky': [{'subor': 'obsah/obrazky/stara-web/01.jpg', 'alt': 'x'}]}, f)
        with open(os.path.join(self.obsah, 'obrazky', 'stara-web', '01.jpg'), 'wb') as f:
            f.write(JPG)
        self.subory = {'a1': JPG, 'a2': JPG2, 'b1': PNG}
        self.polozky = [self.polozka('dvojdom', SPRAVA, [('a1', 'jpg'), ('a2', 'jpg')], poradie=1),
                        self.polozka('rd-ahoj', FB, [('b1', 'png')], poradie=2)]
        # stiahnute: None = kľúč chýba (starý worker), inak zoznam id. novinky: None = trasa vráti 404.
        self.stiahnute = []
        self.novinky = None
        self.stiahnute_novinky = []
        self.nastav_api()

    def fotky_api(self, fotky):
        return [{'id': fid, 'url': f'{self.server.zaklad}/f/bmbuilding/{fid}.{ext}', 'alt': f'Fotka {fid}',
                 'sirka': 10, 'vyska': 10, 'sha256': sha(self.subory[fid])} for fid, ext in fotky]

    def polozka(self, slug, pid, fotky, poradie=0):
        return {
            'id': pid, 'slug': slug, 'typ': 'realizacia', 'poradie': poradie, 'zdroj': 'sprava',
            'upravene': '2026-11-21T05:00:00.000Z', 'nazov': f'Stavba {slug}', 'miesto': 'Bratislava',
            'rola': 'Stavebný dozor', 'text': 'Riadok 1\n<script>alert(1)</script>', 'datum': '2026-10-04',
            'investor': 'Mesto Bratislava', 'fotky': self.fotky_api(fotky),
        }

    def novinka(self, slug, pid, fotky, datum='2026-11-20'):
        return {
            'id': pid, 'slug': slug, 'typ': 'novinka', 'poradie': 0, 'zdroj': 'fb', 'upravene': '2026-11-22T03:00:00.000Z',
            'datum': datum, 'nadpis': f'Novinka {slug}', 'text': 'Kolaudujeme.', 'fotky': self.fotky_api(fotky),
        }

    def nastav_api(self, telo=None, kod=200):
        if telo is None:
            d = {'klient': 'bmbuilding', 'typ': 'realizacia', 'aktualizovane': None, 'polozky': self.polozky}
            if self.stiahnute is not None:
                d['stiahnute'] = self.stiahnute
            telo = json.dumps(d).encode()
        self.server.trasy['/api/bmbuilding/realizacia'] = (kod, telo)
        if self.novinky is None:
            self.server.trasy.pop('/api/bmbuilding/novinka', None)
        else:
            d = {'klient': 'bmbuilding', 'typ': 'novinka', 'aktualizovane': None, 'polozky': self.novinky}
            if self.stiahnute_novinky is not None:
                d['stiahnute'] = self.stiahnute_novinky
            self.server.trasy['/api/bmbuilding/novinka'] = (200, json.dumps(d).encode())
        for p in self.polozky + (self.novinky or []):
            fotky = p.get('fotky') if isinstance(p, dict) else None
            for f in fotky if isinstance(fotky, list) else []:
                if isinstance(f, dict) and f.get('id') in self.subory:
                    self.server.trasy[f['url'][len(self.server.zaklad):]] = (200, self.subory[f['id']])

    def spusti(self, *navyse):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            kod = stiahni.main(['--api', f'{self.server.zaklad}/api/bmbuilding', '--obsah', self.obsah, '--timeout', '5', *navyse])
        self.vystup = out.getvalue() + err.getvalue()
        return kod

    def snimka(self):
        vysledok = {}
        for koren, _, subory in os.walk(self.dir):
            for n in subory:
                cesta = os.path.join(koren, n)
                with open(cesta, 'rb') as f:
                    vysledok[os.path.relpath(cesta, self.dir).replace(os.sep, '/')] = f.read()
        return vysledok

    def citaj(self, rel):
        with open(os.path.join(self.obsah, *rel.split('/')), encoding='utf-8') as f:
            return json.load(f)

    def stav(self):
        d = self.citaj('.obsah-stav.json')
        self.assertEqual(d['verzia'], 2)
        return d['polozky']

    def zapis(self, rel, bajty):
        cesta = os.path.join(self.obsah, *rel.split('/'))
        os.makedirs(os.path.dirname(cesta), exist_ok=True)
        with open(cesta, 'wb') as f:
            f.write(bajty)

    def zapis_json(self, rel, d):
        self.zapis(rel, (json.dumps(d, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def test_prve_stiahnutie_zapise_presny_tvar_dohody(self):
        stara = self.snimka()['obsah/realizacie/stara-web.json']
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('realizacie/dvojdom.json')
        self.assertEqual(list(d), ['id', 'slug', 'nazov', 'miesto', 'rola', 'projektant', 'roky', 'text', 'investor',
                                   'zhotovitel', 'fotky', 'zdroj', 'fb_url', 'datum', 'poradie', 'zverejnene'])
        # investor a zhotovitel (doplnok dohody z linky bmb-web, build.py ich zobrazuje) prejdú zo servera do súboru.
        self.assertEqual(d['investor'], 'Mesto Bratislava')
        self.assertIsNone(d['zhotovitel'])
        self.assertEqual(d['id'], SPRAVA)
        self.assertEqual(d['fotky'], [{'subor': 'obsah/obrazky/dvojdom/01.jpg', 'alt': 'Fotka a1'},
                                      {'subor': 'obsah/obrazky/dvojdom/02.jpg', 'alt': 'Fotka a2'}])
        self.assertIsNone(d['projektant'])
        self.assertIsNone(d['fb_url'])
        self.assertIs(d['zverejnene'], True)
        self.assertEqual(d['text'], 'Riadok 1\n<script>alert(1)</script>')
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/dvojdom/01.jpg'], JPG)
        self.assertEqual(s['obsah/obrazky/dvojdom/02.jpg'], JPG2)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/01.png'], PNG)
        self.assertEqual(self.citaj('realizacie/rd-ahoj.json')['fotky'][0]['subor'], 'obsah/obrazky/rd-ahoj/01.png')
        self.assertEqual(self.stav()[SPRAVA], {'typ': 'realizacia', 'slug': 'dvojdom', 'fotky': {
            'obrazky/dvojdom/01.jpg': sha(JPG), 'obrazky/dvojdom/02.jpg': sha(JPG2)}})
        self.assertEqual(self.stav()[FB]['slug'], 'rd-ahoj')
        self.assertEqual(s['obsah/firma.json'], b'{"nazov": "BM Building s.r.o."}\n')
        self.assertEqual(s['obsah/realizacie/stara-web.json'], stara)
        self.assertIn('pridaní 2, zmien 0, mazaní 0 (JSON 0, fotky 0), stiahnutých fotiek 3, stav prepísaný', self.vystup)
        self.assertFalse(any('.obsah-tmp-' in k for k in s), 'dočasný priečinok nezostal')

    def test_druhe_spustenie_bez_zmeny_nic_nestiahne_ani_nezapise(self):
        self.assertEqual(self.spusti(), 0)
        pred = self.snimka()
        casy = {k: os.stat(os.path.join(self.dir, k)).st_mtime_ns for k in pred}
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.server.fotky(), [])
        self.assertIn(BEZ_ZMIEN, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual({k: os.stat(os.path.join(self.dir, k)).st_mtime_ns for k in pred}, casy)

    def test_zmena_fotky_stiahne_len_tu_jednu(self):
        self.assertEqual(self.spusti(), 0)
        self.subory['a2'] = JPG3
        self.polozky[0]['fotky'][1]['sha256'] = sha(JPG3)
        self.nastav_api()
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.server.fotky(), ['/f/bmbuilding/a2.jpg'])
        self.assertEqual(self.snimka()['obsah/obrazky/dvojdom/02.jpg'], JPG3)
        self.assertEqual(self.stav()[SPRAVA]['fotky']['obrazky/dvojdom/02.jpg'], sha(JPG3))

    def test_menej_fotiek_a_ina_pripona_zmaze_stare_subory(self):
        self.assertEqual(self.spusti(), 0)
        self.polozky[0] = self.polozka('dvojdom', SPRAVA, [('b1', 'png')], poradie=1)
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/dvojdom/01.png'], PNG)
        self.assertNotIn('obsah/obrazky/dvojdom/01.jpg', s)
        self.assertNotIn('obsah/obrazky/dvojdom/02.jpg', s)

    def test_zmazana_so_znackou_zmizne_aj_s_fotkami_a_cudzie_subory_ostanu(self):
        self.assertEqual(self.spusti(), 0)
        with open(os.path.join(self.obsah, 'realizacie', 'z-facebooku.json'), 'w', encoding='utf-8') as f:
            json.dump({'slug': 'z-facebooku', 'nazov': 'FB', 'zdroj': 'fb', 'fotky': []}, f)
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertFalse(os.path.exists(os.path.join(self.obsah, 'obrazky', 'rd-ahoj')))
        self.assertIn('obsah/realizacie/z-facebooku.json', s, 'zdroj fb bez id sťahovača ostane (verzia 1 ho mazala)')
        self.assertIn('obsah/realizacie/stara-web.json', s, 'zdroj web ostane')
        self.assertEqual(s['obsah/obrazky/stara-web/01.jpg'], JPG)
        self.assertIn('obsah/firma.json', s)
        self.assertNotIn(FB, self.stav())
        self.assertIn('mazaní 2 (JSON 1, fotky 1)', self.vystup)

    def test_web_polozka_z_api_sa_nezapise_ani_nezmaze(self):
        pred = self.snimka()
        self.polozky.append(self.polozka('stara-web', 'web-stara-web', [('a1', 'jpg'), ('a2', 'jpg')], poradie=3))
        self.polozky[-1]['nazov'] = 'Iný názov z D1'
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(s['obsah/realizacie/stara-web.json'], pred['obsah/realizacie/stara-web.json'])
        self.assertNotIn('obsah/obrazky/stara-web/02.jpg', s)
        self.assertNotIn('web-stara-web', self.stav())
        self.assertIn('prvé naplnenie a iné (nechávam repozitáru) 1', self.vystup)
        self.polozky.pop()
        self.stiahnute = ['web-stara-web']
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(s['obsah/realizacie/stara-web.json'], pred['obsah/realizacie/stara-web.json'])
        self.assertEqual(s['obsah/obrazky/stara-web/01.jpg'], JPG)

    def over_bez_zapisu(self, kod_ocakavany=1):
        pred = self.snimka()
        self.assertEqual(self.spusti(), kod_ocakavany, self.vystup)
        self.assertEqual(self.snimka(), pred)

    def test_zly_slug_s_cestou_von_zlyha_bez_zapisu(self):
        for slug in ('../von', '..', 'a/b', 'Velke', 'a.b', '', 'x' * 81):
            self.polozky[0]['slug'] = slug
            self.nastav_api()
            self.over_bez_zapisu()
            self.assertIn('slug', self.vystup)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'von.json')))

    def test_zla_pripona_alebo_cudzi_server_fotky_zlyha_bez_zapisu(self):
        zaklad = self.server.zaklad
        for url in (f'{zaklad}/f/bmbuilding/a1.svg', f'{zaklad}/f/bmbuilding/a1.jpg.exe', f'{zaklad}/f/bmbuilding/../../a1.jpg',
                    'http://127.0.0.2:9/f/bmbuilding/a1.jpg', 'file:///etc/passwd', f'{zaklad}/f/bmbuilding/a1.jpg?x=1'):
            self.polozky[0]['fotky'][0]['url'] = url
            self.nastav_api()
            self.over_bez_zapisu()

    def test_obsah_fotky_ineho_typu_ako_pripona_zlyha(self):
        self.subory['a1'] = PNG
        self.polozky[0]['fotky'][0]['sha256'] = sha(PNG)
        self.nastav_api()
        self.over_bez_zapisu()

    def test_500_zo_servera_nezapise_nic(self):
        self.nastav_api(telo=b'{"chyba":"x"}', kod=500)
        self.over_bez_zapisu()
        self.assertIn('500', self.vystup)

    def test_500_pri_fotke_nezapise_nic_ani_ostatne(self):
        self.nastav_api()
        self.server.trasy['/f/bmbuilding/b1.png'] = (500, b'')
        self.over_bez_zapisu()

    def test_500_pri_novinke_nezapise_nic(self):
        self.nastav_api()
        self.server.trasy['/api/bmbuilding/novinka'] = (500, b'{"chyba":"x"}')
        self.over_bez_zapisu()
        self.assertIn('500', self.vystup)

    def test_zle_sha256_fotky_nezapise_nic(self):
        self.polozky[1]['fotky'][0]['sha256'] = sha(b'ine')
        self.nastav_api()
        self.over_bez_zapisu()

    def test_zly_json_a_zly_tvar_zlyhaju(self):
        for telo in (b'<html>chyba</html>', b'', b'[]', b'{"polozky": {}}', json.dumps({'polozky': [{'id': 'fb-1', 'slug': 'a'}]}).encode(),
                     json.dumps({'polozky': [{'slug': 'a'}]}).encode(), json.dumps({'polozky': [{'id': '../x'}]}).encode(),
                     json.dumps({'typ': 'novinka', 'polozky': []}).encode(), json.dumps({'polozky': ['x']}).encode()):
            with self.subTest(telo=telo):
                self.nastav_api(telo=telo)
                self.over_bez_zapisu()

    def test_presmerovanie_sa_neprijme(self):
        self.server.trasy['/api/bmbuilding/realizacia'] = (302, b'http://127.0.0.1:9/ine')
        self.over_bez_zapisu()

    def test_prazdna_odpoved_nic_nezmaze_ani_s_povol_prazdne(self):
        self.assertEqual(self.spusti(), 0)
        pred = self.snimka()
        self.polozky = []
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual(self.spusti('--povol-prazdne'), 0, self.vystup)
        self.assertIn('--povol-prazdne od verzie 2 nič nemení', self.vystup)
        self.assertEqual(self.snimka(), pred)

    def test_sucho_nic_nezapise_ani_nestiahne_fotky(self):
        pred = self.snimka()
        self.assertEqual(self.spusti('--sucho'), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual(self.server.fotky(), [])
        self.assertIn('stiahol by som: obrazky/dvojdom/01.jpg', self.vystup)
        self.assertIn('zapísal by som: realizacie/rd-ahoj.json (nový)', self.vystup)
        self.assertIn('Suchý beh: pridaní 2, zmien 0, mazaní 0 (JSON 0, fotky 0), fotiek na stiahnutie 3, stav by som prepísal.',
                      self.vystup)

    def test_chybajuca_alebo_zla_adresa_api(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(stiahni.main(['--api', '', '--obsah', self.obsah]), 1)
            self.assertEqual(stiahni.main(['--api', 'http://example.com/api/bmbuilding', '--obsah', self.obsah]), 1)
            self.assertEqual(stiahni.main(['--api', 'file:///etc', '--obsah', self.obsah]), 1)
        self.assertIn('OBSAH_API', err.getvalue())

    def test_poskodeny_stav_nic_nezmaze_a_vlastne_subory_prevezme_podla_id(self):
        self.assertEqual(self.spusti(), 0)
        with open(os.path.join(self.obsah, '.obsah-stav.json'), 'w', encoding='utf-8') as f:
            f.write('{zly')
        with open(os.path.join(self.obsah, 'realizacie', 'z-webu.json'), 'w', encoding='utf-8') as f:
            json.dump({'slug': 'z-webu', 'nazov': 'Web', 'zdroj': 'web'}, f)
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertIn('poškodený', self.vystup)
        self.assertIn('obsah/realizacie/z-webu.json', s)
        self.assertIn('obsah/realizacie/rd-ahoj.json', s, 'bez záznamu v stave sa nemaže ani so značkou')
        self.assertIn(SPRAVA, self.stav(), 'vlastný súbor s tým istým id sa prevezme')

    def test_bezpecna_cesta_odmietne_cestu_von(self):
        for rel in ('../x', 'obrazky/../../x', '/etc/passwd', '\\x', ''):
            with self.assertRaises(stiahni.Chyba):
                stiahni.bezpecna_cesta(self.obsah, rel)

    # Zmena a) návrhu bmb-fb-samo: príznak vizualizácie z API (druh, autor) ide do súboru v tvare dokončenia.
    def test_vizualizacia_z_api_ide_do_suboru_v_tvare_dokoncenia(self):
        self.polozky[0]['fotky'][1]['druh'] = 'vizualizacia'
        self.polozky[0]['fotky'][1]['autor'] = 'Ing. arch. <b>Rezník</b>, architekti.sk'
        self.polozky[1]['fotky'][0]['druh'] = 'vizualizacia'
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('realizacie/dvojdom.json')
        self.assertEqual(d['fotky'][0], {'subor': 'obsah/obrazky/dvojdom/01.jpg', 'alt': 'Fotka a1'})
        self.assertEqual(list(d['fotky'][1]), ['subor', 'alt', 'druh', 'autor'])
        self.assertEqual(d['fotky'][1], {'subor': 'obsah/obrazky/dvojdom/02.jpg', 'alt': 'Fotka a2', 'druh': 'vizualizacia',
                                         'autor': 'Ing. arch. <b>Rezník</b>, architekti.sk'})
        self.assertEqual(self.citaj('realizacie/rd-ahoj.json')['fotky'][0],
                         {'subor': 'obsah/obrazky/rd-ahoj/01.png', 'alt': 'Fotka b1', 'druh': 'vizualizacia'})
        # Druhý beh s tým istým API nič nezmení (idempotencia).
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn(BEZ_ZMIEN, self.vystup)

    def test_vizualizacia_zrusena_v_api_zmizne_zo_suboru(self):
        self.polozky[0]['fotky'][1].update({'druh': 'vizualizacia', 'autor': 'Autor'})
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.citaj('realizacie/dvojdom.json')['fotky'][1]['druh'], 'vizualizacia')
        del self.polozky[0]['fotky'][1]['druh']
        del self.polozky[0]['fotky'][1]['autor']
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.citaj('realizacie/dvojdom.json')['fotky'][1], {'subor': 'obsah/obrazky/dvojdom/02.jpg', 'alt': 'Fotka a2'})

    def test_prazdny_autor_sa_nezapise_a_null_je_ako_bez_priznaku(self):
        self.polozky[0]['fotky'][0].update({'druh': None, 'autor': None})
        self.polozky[0]['fotky'][1].update({'druh': 'vizualizacia', 'autor': '  '})
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('realizacie/dvojdom.json')
        self.assertEqual(d['fotky'][0], {'subor': 'obsah/obrazky/dvojdom/01.jpg', 'alt': 'Fotka a1'})
        self.assertEqual(d['fotky'][1], {'subor': 'obsah/obrazky/dvojdom/02.jpg', 'alt': 'Fotka a2', 'druh': 'vizualizacia'})

    def test_zly_druh_alebo_autor_fotky_zlyha_bez_zapisu(self):
        pripady = [
            ({'druh': 'foto'}, 'druh'),
            ({'druh': 'Vizualizacia'}, 'druh'),
            ({'druh': ''}, 'druh'),
            ({'druh': 1}, 'druh'),
            ({'druh': ['vizualizacia']}, 'druh'),
            ({'autor': 'Bez druhu'}, 'autor'),
            ({'druh': 'vizualizacia', 'autor': 5}, 'autor'),
            ({'druh': 'vizualizacia', 'autor': {'meno': 'x'}}, 'autor'),
            ({'druh': 'vizualizacia', 'autor': 'x' * 301}, 'autor'),
        ]
        for navyse, slovo in pripady:
            with self.subTest(navyse=navyse):
                fotka = self.polozky[0]['fotky'][0]
                fotka.pop('druh', None)
                fotka.pop('autor', None)
                fotka.update(navyse)
                self.nastav_api()
                self.over_bez_zapisu()
                self.assertIn(slovo, self.vystup)
                self.assertIn('polozky[0].fotky[0]', self.vystup)

    def test_autor_presne_300_znakov_prejde(self):
        self.polozky[0]['fotky'][0].update({'druh': 'vizualizacia', 'autor': 'á' * 300})
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.citaj('realizacie/dvojdom.json')['fotky'][0]['autor'], 'á' * 300)

    def test_vizualizacia_na_novej_fb_polozke_a_novinke(self):
        self.subory.update({'c1': JPG3, 'n1': JPG4})
        nova = self.polozka('rd-vizualizacia', 'fb-1872670187025847', [('c1', 'jpg')])
        nova['fotky'][0].update({'druh': 'vizualizacia', 'autor': 'Ing. arch. Novák'})
        self.polozky.append(nova)
        self.novinky = [self.novinka('navrh-domu', 'fb-1872670187025848', [('n1', 'jpg')])]
        self.novinky[0]['fotky'][0]['druh'] = 'vizualizacia'
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.citaj('realizacie/rd-vizualizacia.json')['fotky'],
                         [{'subor': 'obsah/obrazky/rd-vizualizacia/01.jpg', 'alt': 'Fotka c1', 'druh': 'vizualizacia',
                           'autor': 'Ing. arch. Novák'}])
        self.assertEqual(self.citaj('novinky/navrh-domu.json')['fotky'],
                         [{'subor': 'obsah/obrazky/novinky/navrh-domu/01.jpg', 'alt': 'Fotka n1', 'druh': 'vizualizacia'}])

    # Zmena b) návrhu bmb-fb-samo: kľúče, ktoré API nepozná, ostanú pri prepise vlastného súboru.
    def dopln_lokalne(self, rel, **navyse):
        cesta = os.path.join(self.obsah, *rel.split('/'))
        with open(cesta, encoding='utf-8') as f:
            d = json.load(f)
        d.update(navyse)
        with open(cesta, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2) + '\n')

    def test_nezname_kluce_ostanu_pri_prepise_a_api_kluce_vyhraju(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/dvojdom.json', rok_prispevku='2024', obal_vizualizacia=True,
                           autor='Ing. arch. Skoček', upravene='2026-11-21', typ='iny', nazov='Lokálny názov',
                           fotky=[{'subor': 'obsah/obrazky/dvojdom/01.jpg', 'alt': 'x', 'druh': 'vizualizacia'}])
        self.polozky[0]['text'] = 'Nový text zo správy'
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('realizacie/dvojdom.json')
        self.assertEqual(list(d), ['id', 'slug', 'nazov', 'miesto', 'rola', 'projektant', 'roky', 'text', 'investor',
                                   'zhotovitel', 'fotky', 'zdroj', 'fb_url', 'datum', 'poradie', 'zverejnene',
                                   'rok_prispevku', 'obal_vizualizacia', 'autor'])
        self.assertEqual(d['text'], 'Nový text zo správy')
        self.assertEqual(d['nazov'], 'Stavba dvojdom', 'kľúč, ktorý API pozná, berie sa z API')
        self.assertEqual(d['fotky'][0], {'subor': 'obsah/obrazky/dvojdom/01.jpg', 'alt': 'Fotka a1'},
                         'príznak fotky sa zo starého súboru nepreberá, rozhoduje API')
        self.assertEqual((d['rok_prispevku'], d['obal_vizualizacia'], d['autor']), ('2024', True, 'Ing. arch. Skoček'))
        self.assertNotIn('upravene', d)
        self.assertNotIn('typ', d)
        # Druhý beh nič nezmení ani nezapíše (idempotencia aj so zachovanými kľúčmi).
        pred = self.snimka()
        casy = {k: os.stat(os.path.join(self.dir, k)).st_mtime_ns for k in pred}
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn(BEZ_ZMIEN, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual({k: os.stat(os.path.join(self.dir, k)).st_mtime_ns for k in pred}, casy)

    def test_nezname_kluce_sucho_nic_nezapise(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/dvojdom.json', rok_prispevku='2024')
        self.polozky[0]['text'] = 'Iný'
        self.nastav_api()
        pred = self.snimka()
        self.assertEqual(self.spusti('--sucho'), 0, self.vystup)
        self.assertIn('zapísal by som: realizacie/dvojdom.json (zmena)', self.vystup)
        self.assertEqual(self.snimka(), pred)

    def test_bez_prepinaca_b_povodne_spravanie(self):
        self.addCleanup(setattr, stiahni, 'ZACHOVAJ_NEZNAME_KLUCE', stiahni.ZACHOVAJ_NEZNAME_KLUCE)
        stiahni.ZACHOVAJ_NEZNAME_KLUCE = False
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/dvojdom.json', rok_prispevku='2024')
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertNotIn('rok_prispevku', self.citaj('realizacie/dvojdom.json'))

    def test_nezname_kluce_nezachrania_polozku_stiahnutu_v_d1(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/rd-ahoj.json', rok_prispevku='2024', obal_vizualizacia=True)
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', self.snimka())

    def test_necitatelny_vlastny_subor_sa_prepise_podla_api_s_upozornenim(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        for zly in ('{zly', '[1, 2]'):
            with self.subTest(zly=zly):
                with open(os.path.join(self.obsah, 'realizacie', 'dvojdom.json'), 'w', encoding='utf-8') as f:
                    f.write(zly)
                self.assertEqual(self.spusti(), 0, self.vystup)
                self.assertIn('Upozornenie: realizacie/dvojdom.json', self.vystup)
                self.assertEqual(self.citaj('realizacie/dvojdom.json')['nazov'], 'Stavba dvojdom')

    # Verzia 2 (fb-ostro, 23. 11. 2026): doplňovanie namiesto zrkadla.
    def test_dnesny_stav_api_len_s_polozkami_web_nic_nezmeni(self):
        # Ako živé API 23. 11. 2026: len položky web-bmbuilding-…, Dvojdom S so zdrojom fb a 0 fotkami, bez stiahnute,
        # novinka 404. Lokálne viac fotiek (01 a 03, nie za sebou), novinka bez id a stav verzie 1.
        self.zapis_json('realizacie/dvojdom-s-bratislava.json', {
            'id': 'dvojdom-s-bratislava', 'slug': 'dvojdom-s-bratislava', 'nazov': 'Dvojdom S', 'zdroj': 'fb',
            'fotky': [{'subor': 'obsah/obrazky/dvojdom-s-bratislava/01.jpg', 'alt': 'a'},
                      {'subor': 'obsah/obrazky/dvojdom-s-bratislava/03.jpg', 'alt': 'b', 'druh': 'vizualizacia'}],
            'upravene': '2026-11-21'})
        self.zapis('obrazky/dvojdom-s-bratislava/01.jpg', JPG)
        self.zapis('obrazky/dvojdom-s-bratislava/03.jpg', JPG2)
        self.zapis_json('novinky/kolaudacia-ufo-2025-11.json', {
            'slug': 'kolaudacia-ufo-2025-11', 'datum': '2025-11-14', 'nadpis': 'Kolaudácia UFO', 'zdroj': 'fb', 'zverejnene': True,
            'fotky': [{'subor': 'obsah/obrazky/novinky/kolaudacia-ufo-2025-11/01.jpg', 'alt': 'x'}]})
        self.zapis('obrazky/novinky/kolaudacia-ufo-2025-11/01.jpg', JPG3)
        self.zapis_json('.obsah-stav.json', {'verzia': 1, 'realizacie': {
            'stara-web': {'id': 'web-bmbuilding-stara-web', 'fotky': {'obrazky/stara-web/01.jpg': sha(JPG2)}},
            'dvojdom-s-bratislava': {'id': 'web-bmbuilding-dvojdom-s-bratislava', 'fotky': {}}}})
        self.polozky = [self.polozka('dvojdom-s-bratislava', 'web-bmbuilding-dvojdom-s-bratislava', [], poradie=1),
                        self.polozka('stara-web', 'web-bmbuilding-stara-web', [('a2', 'jpg')], poradie=2)]
        self.polozky[0]['zdroj'] = 'fb'
        self.stiahnute = None
        self.nastav_api()
        pred = self.snimka()
        self.assertEqual(self.spusti('--sucho'), 0, self.vystup)
        self.assertIn('Suchý beh: pridaní 0, zmien 0, mazaní 0 (JSON 0, fotky 0), fotiek na stiahnutie 0, stav by som prepísal.',
                      self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual(self.spusti(), 0, self.vystup)
        po = self.snimka()
        bez_stavu = lambda s: {k: v for k, v in s.items() if k != 'obsah/.obsah-stav.json'}
        self.assertEqual(bez_stavu(po), bez_stavu(pred))
        self.assertEqual(self.citaj('.obsah-stav.json'), {'verzia': 2, 'polozky': {}})
        self.assertEqual(self.server.fotky(), [])
        self.assertIn('spravovaných položiek 0, položiek prvého naplnenia a iných 2 odovzdávam repozitáru', self.vystup)
        self.assertIn('nemá zoznam stiahnute (starý worker)', self.vystup)
        self.assertIn('API novinka: typ klient nemá (404)', self.vystup)
        self.assertIn('pridaní 0, zmien 0, mazaní 0 (JSON 0, fotky 0), stiahnutých fotiek 0, stav prepísaný', self.vystup)
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn(BEZ_ZMIEN, self.vystup)
        self.assertEqual(self.snimka(), po)

    def test_polozka_chybajuca_v_d1_sa_nezmaze(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        pred = self.snimka()
        self.polozky = [self.polozky[0]]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred, 'chýba v polozky, nie je v stiahnute: ostane')
        self.assertIn(FB, self.stav())
        self.stiahnute = None
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('starý worker', self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.polozky = []
        self.stiahnute = []
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred, 'prázdna D1 (napríklad obnova staršej databázy) nič nezmaže')

    def test_nova_fb_polozka_sa_prida(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        pred = self.snimka()
        self.subory.update({'c1': JPG3, 'c2': PNG})
        nova = self.polozka('rodinny-dom-koliba', 'fb-1872670187025847', [('c1', 'jpg'), ('c2', 'png')])
        nova.update({'zdroj': 'fb', 'fb_url': 'https://www.facebook.com/bmbuilding/posts/1872670187025847', 'roky': '2025 až 2026'})
        self.polozky.append(nova)
        self.nastav_api()
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('pridaní 1, zmien 0, mazaní 0 (JSON 0, fotky 0), stiahnutých fotiek 2', self.vystup)
        self.assertEqual(sorted(self.server.fotky()), ['/f/bmbuilding/c1.jpg', '/f/bmbuilding/c2.png'])
        d = self.citaj('realizacie/rodinny-dom-koliba.json')
        self.assertEqual((d['id'], d['zdroj'], d['roky']), ('fb-1872670187025847', 'fb', '2025 až 2026'))
        self.assertEqual(d['fb_url'], 'https://www.facebook.com/bmbuilding/posts/1872670187025847')
        self.assertEqual(d['fotky'], [{'subor': 'obsah/obrazky/rodinny-dom-koliba/01.jpg', 'alt': 'Fotka c1'},
                                      {'subor': 'obsah/obrazky/rodinny-dom-koliba/02.png', 'alt': 'Fotka c2'}])
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/rodinny-dom-koliba/01.jpg'], JPG3)
        self.assertEqual(s['obsah/obrazky/rodinny-dom-koliba/02.png'], PNG)
        self.assertEqual(self.stav()['fb-1872670187025847']['slug'], 'rodinny-dom-koliba')
        for k, v in pred.items():
            if k != 'obsah/.obsah-stav.json':
                self.assertEqual(s[k], v, k)

    def test_zmenena_sa_prepise(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/rd-ahoj.json', titulok='RD Ahoj', upravene='2026-11-01')
        self.subory['b2'] = JPG3
        self.polozky[1] = self.polozka('rd-ahoj', FB, [('b2', 'jpg'), ('b1', 'png')], poradie=2)
        self.polozky[1]['text'] = 'Nový text z Facebooku'
        self.nastav_api()
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('pridaní 0, zmien 1, mazaní 1 (JSON 0, fotky 1), stiahnutých fotiek 2', self.vystup)
        d = self.citaj('realizacie/rd-ahoj.json')
        self.assertEqual(d['text'], 'Nový text z Facebooku')
        self.assertEqual(d['titulok'], 'RD Ahoj', 'kľúč, ktorý API nepozná, ostane')
        self.assertNotIn('upravene', d)
        self.assertEqual(d['fotky'], [{'subor': 'obsah/obrazky/rd-ahoj/01.jpg', 'alt': 'Fotka b2'},
                                      {'subor': 'obsah/obrazky/rd-ahoj/02.png', 'alt': 'Fotka b1'}])
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/rd-ahoj/01.jpg'], JPG3)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/02.png'], PNG)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertEqual(sorted(self.server.fotky()), ['/f/bmbuilding/b1.png', '/f/bmbuilding/b2.jpg'])

    def test_zmazana_len_so_znackou_a_len_vlastna(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        # Ručný súbor s id z Facebooku, ktorý sťahovač nevytvoril (nie je v stave), a cudzie id v zozname stiahnute.
        self.zapis_json('realizacie/rucna.json', {'id': 'fb-999', 'slug': 'rucna', 'nazov': 'Ručná', 'zdroj': 'fb',
                                                  'fotky': [{'subor': 'obsah/obrazky/rucna/01.jpg', 'alt': 'x'}]})
        self.zapis('obrazky/rucna/01.jpg', JPG)
        pred = self.snimka()
        self.stiahnute = ['fb-999', 'web-stara-web', 'uuid-neznama-0000']
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred, 'v stiahnute, ale nie v stave: nič')
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        for k in ('obsah/realizacie/rucna.json', 'obsah/obrazky/rucna/01.jpg', 'obsah/realizacie/dvojdom.json',
                  'obsah/realizacie/stara-web.json'):
            self.assertEqual(s[k], pred[k], k)

    def test_id_v_polozky_aj_v_stiahnute_ostane(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        pred = self.snimka()
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertIn('aj v zozname stiahnute, nechávam ho', self.vystup)

    def test_koncept_a_znova_zverejnena(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        rd = self.polozky.pop()
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', self.snimka())
        self.polozky.append(rd)
        self.stiahnute = []
        self.nastav_api()
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka()['obsah/obrazky/rd-ahoj/01.png'], PNG)
        self.assertEqual(self.citaj('realizacie/rd-ahoj.json')['id'], FB)
        self.assertEqual(self.server.fotky(), ['/f/bmbuilding/b1.png'])
        self.assertIn('pridaní 1', self.vystup)

    def test_fotky_starej_polozky_sa_nestratia_ked_d1_ma_menej(self):
        self.zapis_json('realizacie/ufo-most-snp.json', {
            'id': 'ufo-most-snp', 'slug': 'ufo-most-snp', 'nazov': 'UFO', 'zdroj': 'fb',
            'fotky': [{'subor': 'obsah/obrazky/ufo-most-snp/01.jpg', 'alt': 'a'}, {'subor': 'obsah/obrazky/ufo-most-snp/02.jpg', 'alt': 'b'}]})
        self.zapis('obrazky/ufo-most-snp/01.jpg', JPG)
        self.zapis('obrazky/ufo-most-snp/02.jpg', JPG2)
        self.polozky.append(self.polozka('ufo-most-snp', 'web-bmbuilding-ufo-most-snp-bratislava', [], poradie=3))
        self.nastav_api()
        pred = self.snimka()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        for k in ('obsah/realizacie/ufo-most-snp.json', 'obsah/obrazky/ufo-most-snp/01.jpg', 'obsah/obrazky/ufo-most-snp/02.jpg'):
            self.assertEqual(s[k], pred[k], k)

    def test_fotky_vlastnej_polozky_sa_nestratia_pri_preradeni_ani_pri_chybe(self):
        self.subory.update({'b2': JPG2, 'b3': JPG3, 'b4': JPG4})
        self.polozky[1] = self.polozka('rd-ahoj', FB, [('b1', 'png'), ('b2', 'jpg'), ('b3', 'jpg')], poradie=2)
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.zapis('obrazky/rd-ahoj/09.jpg', JPG)
        # D1 odobrala strednú fotku: tretia sa posunie na druhé miesto s tým istým obsahom.
        self.polozky[1] = self.polozka('rd-ahoj', FB, [('b1', 'png'), ('b3', 'jpg')], poradie=2)
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/rd-ahoj/01.png'], PNG)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/02.jpg'], JPG3)
        self.assertNotIn('obsah/obrazky/rd-ahoj/03.jpg', s)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/09.jpg'], JPG, 'súbor, ktorý sťahovač nevytvoril, ostane')
        # Novú fotku nejde stiahnuť (500): nič sa nezmení, staré fotky ostanú.
        self.polozky[1] = self.polozka('rd-ahoj', FB, [('b4', 'jpg')], poradie=2)
        self.nastav_api()
        self.server.trasy['/f/bmbuilding/b4.jpg'] = (500, b'')
        self.over_bez_zapisu()
        # Stiahnutá položka: zmizne JSON a jej fotky, cudzie súbory v priečinku ostanú.
        self.zapis('obrazky/rd-ahoj/poznamka.txt', b'poznamka')
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/02.jpg', s)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/09.jpg'], JPG)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/poznamka.txt'], b'poznamka')

    def test_novinka_z_fb_sa_prida_v_tvare_build_a_stara_ostane(self):
        self.subory['n1'] = JPG4
        self.zapis_json('novinky/stara-novinka.json', {'slug': 'stara-novinka', 'datum': '2025-01-01', 'nadpis': 'Stará',
                                                       'fotky': [], 'zdroj': 'fb', 'zverejnene': True})
        pred = self.snimka()
        self.novinky = [self.novinka('kolaudacia-nova-koliba', 'fb-1872670187025999', [('n1', 'jpg')])]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('novinky/kolaudacia-nova-koliba.json')
        self.assertEqual(list(d), ['id', 'slug', 'datum', 'nadpis', 'text', 'fotky', 'zdroj', 'zverejnene'])
        self.assertEqual((d['datum'], d['nadpis'], d['text']), ('2026-11-20', 'Novinka kolaudacia-nova-koliba', 'Kolaudujeme.'))
        self.assertEqual(d['fotky'], [{'subor': 'obsah/obrazky/novinky/kolaudacia-nova-koliba/01.jpg', 'alt': 'Fotka n1'}])
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/novinky/kolaudacia-nova-koliba/01.jpg'], JPG4)
        self.assertEqual(s['obsah/novinky/stara-novinka.json'], pred['obsah/novinky/stara-novinka.json'])
        self.assertEqual(self.stav()['fb-1872670187025999'], {'typ': 'novinka', 'slug': 'kolaudacia-nova-koliba', 'fotky': {
            'obrazky/novinky/kolaudacia-nova-koliba/01.jpg': sha(JPG4)}})
        self.novinky = []
        self.stiahnute_novinky = ['fb-1872670187025999']
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/novinky/kolaudacia-nova-koliba.json', s)
        self.assertNotIn('obsah/obrazky/novinky/kolaudacia-nova-koliba/01.jpg', s)
        self.assertEqual(s['obsah/novinky/stara-novinka.json'], pred['obsah/novinky/stara-novinka.json'])

    def test_zly_tvar_novinky_zlyha_bez_zapisu(self):
        for zmena in ({'nadpis': ''}, {'nadpis': None}, {'datum': None}, {'datum': '20. 11. 2026'}, {'text': 5}):
            with self.subTest(zmena=zmena):
                self.novinky = [self.novinka('nova', 'fb-77', [])]
                self.novinky[0].update(zmena)
                self.nastav_api()
                self.over_bez_zapisu()
                self.assertIn('novinka: polozky[0]', self.vystup)

    def test_novinka_404_nic_nezmaze(self):
        self.subory['n1'] = JPG4
        self.novinky = [self.novinka('nova', 'fb-77', [('n1', 'jpg')])]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        pred = self.snimka()
        self.novinky = None
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertIn('fb-77', self.stav())
        self.assertIn('API novinka: typ klient nemá (404)', self.vystup)

    def test_kolizia_slugu_skonci_kodom_1_bez_zapisu(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.zapis('obrazky/osirotena/01.jpg', JPG3)
        self.zapis('realizacie/zla.json', b'{zly')
        self.zapis_json('novinky/stara-novinka.json', {'slug': 'stara-novinka', 'datum': '2025-01-01', 'nadpis': 'Stará',
                                                       'fotky': [], 'zdroj': 'fb'})
        zaklad = [self.polozky[0], self.polozky[1]]
        pripady = [
            ([self.polozka('stara-web', 'fb-555', [('a1', 'jpg')])], None, 'obsah/realizacie/stara-web.json'),
            ([self.polozka('osirotena', 'fb-556', [('a2', 'jpg')])], None, 'obsah/obrazky/osirotena/'),
            ([self.polozka('zla', 'fb-557', [])], None, 'obsah/realizacie/zla.json'),
            ([self.polozka('novinky', 'fb-558', [])], None, 'vyhradený'),
            ([], [self.novinka('stara-novinka', 'fb-559', [])], 'obsah/novinky/stara-novinka.json'),
        ]
        for nove, novinky, slovo in pripady:
            with self.subTest(slovo=slovo):
                self.polozky = zaklad + nove
                self.novinky = novinky
                self.nastav_api()
                self.over_bez_zapisu()
                self.assertIn(slovo, self.vystup)
        # Iná spravovaná položka zo správy so slugom súboru, ktorý patrí položke z Facebooku (tá v API chýba).
        self.polozky = [zaklad[0], self.polozka('rd-ahoj', 'a1b2c3d4-0000-4000-8000-000000000001', [])]
        self.novinky = None
        self.nastav_api()
        self.over_bez_zapisu()
        self.assertIn(f'id {FB}', self.vystup)

    def test_stary_subor_s_id_rovnym_slugu_sa_neprevezme(self):
        # Staré realizácie majú id rovné slugu (dom-pod-lesom). Položka zo správy s takým id a slugom (API ho prijme, správa
        # ho sama nedá, tá dáva UUID) ich nesmie prepísať: kolízia, nič sa nezapíše.
        self.zapis_json('realizacie/dom-pod-lesom.json', {'id': 'dom-pod-lesom', 'slug': 'dom-pod-lesom', 'nazov': 'Dom pod lesom',
                                                          'zdroj': 'fb', 'fotky': [{'subor': 'obsah/obrazky/dom-pod-lesom/01.jpg', 'alt': 'x'}]})
        self.zapis('obrazky/dom-pod-lesom/01.jpg', JPG)
        self.polozky.append(self.polozka('dom-pod-lesom', 'dom-pod-lesom', [('a2', 'jpg')]))
        self.nastav_api()
        self.over_bez_zapisu()
        self.assertIn('Kolízia: obsah/realizacie/dom-pod-lesom.json', self.vystup)

    def test_zvysky_preruseneho_behu_sa_prevezmu(self):
        self.subory['c1'] = JPG3
        self.zapis('obrazky/nova/01.jpg', JPG3)
        self.polozky.append(self.polozka('nova', 'fb-4242', [('c1', 'jpg')]))
        self.nastav_api()
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertNotIn('/f/bmbuilding/c1.jpg', self.server.fotky())
        self.assertEqual(self.citaj('realizacie/nova.json')['id'], 'fb-4242')
        # JSON je zapísaný, stav nie (prerušenie pred zápisom stavu): ďalší beh súbor prevezme podľa id.
        stav = self.citaj('.obsah-stav.json')
        del stav['polozky']['fb-4242']
        self.zapis_json('.obsah-stav.json', stav)
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('pridaní 0, zmien 0, mazaní 0 (JSON 0, fotky 0), stiahnutých fotiek 0, stav prepísaný', self.vystup)
        self.assertIn('fb-4242', self.stav())

    def test_zmena_slugu_presunie_subory(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.polozky[1]['slug'] = 'rd-ahoj-nove-mesto'
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertFalse(os.path.exists(os.path.join(self.obsah, 'obrazky', 'rd-ahoj')))
        self.assertEqual(s['obsah/obrazky/rd-ahoj-nove-mesto/01.png'], PNG)
        self.assertEqual(self.citaj('realizacie/rd-ahoj-nove-mesto.json')['id'], FB)
        self.assertEqual(self.stav()[FB]['slug'], 'rd-ahoj-nove-mesto')
        self.assertIn('pridaní 1, zmien 0, mazaní 2 (JSON 1, fotky 1)', self.vystup)

    def test_slug_stiahnutej_polozky_prevezme_nova_v_tom_istom_behu(self):
        # Worker slug zmazanej položky znova nedá (volnySlug ráta aj zmazané), cez SQL sa to stať môže.
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.subory['c1'] = JPG3
        self.polozky[1] = self.polozka('rd-ahoj', 'fb-888', [('c1', 'jpg')], poradie=2)
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(self.citaj('realizacie/rd-ahoj.json')['id'], 'fb-888')
        self.assertEqual(s['obsah/obrazky/rd-ahoj/01.jpg'], JPG3)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertEqual(sorted(self.stav()), sorted(['fb-888', SPRAVA]))
        self.assertIn('pridaní 1, zmien 0, mazaní 1 (JSON 0, fotky 1)', self.vystup)

    def test_stav_verzie_1_so_spravovanym_id_sa_prevezme(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.zapis_json('.obsah-stav.json', {'verzia': 1, 'realizacie': {
            'dvojdom': {'id': SPRAVA, 'fotky': {'obrazky/dvojdom/01.jpg': sha(JPG), 'obrazky/dvojdom/02.jpg': sha(JPG2)}},
            'rd-ahoj': {'id': FB, 'fotky': {'obrazky/rd-ahoj/01.png': sha(PNG)}},
            'stara-web': {'id': 'web-stara-web', 'fotky': {'obrazky/stara-web/01.jpg': sha(JPG)}}}})
        del self.polozky[1]
        self.stiahnute = [FB, 'web-stara-web']
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertIn('obsah/realizacie/stara-web.json', s)
        self.assertEqual(s['obsah/obrazky/stara-web/01.jpg'], JPG)
        self.assertEqual(sorted(self.stav()), [SPRAVA])
        self.assertIn('spravovaných položiek 2, položiek prvého naplnenia a iných 1', self.vystup)

    def test_zly_tvar_stiahnute_zlyha_bez_zapisu(self):
        for zle in ('x', [1], ['../x'], {'id': FB}, [None], ['x' * 101]):
            with self.subTest(zle=zle):
                self.stiahnute = zle
                self.nastav_api()
                self.over_bez_zapisu()
                self.assertIn('stiahnute', self.vystup)

    def test_stiahnute_null_je_ako_stary_worker(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        pred = self.snimka()
        del self.polozky[1]
        d = {'klient': 'bmbuilding', 'typ': 'realizacia', 'aktualizovane': None, 'polozky': self.polozky, 'stiahnute': None}
        self.nastav_api(telo=json.dumps(d).encode())
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertIn('starý worker', self.vystup)

    def test_stiahnuta_bez_json_zmaze_len_fotky_so_zhodnym_sha(self):
        self.subory['b2'] = JPG2
        self.polozky[1] = self.polozka('rd-ahoj', FB, [('b1', 'png'), ('b2', 'jpg')], poradie=2)
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        os.remove(os.path.join(self.obsah, 'realizacie', 'rd-ahoj.json'))
        self.zapis('obrazky/rd-ahoj/02.jpg', JPG3)
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/02.jpg'], JPG3, 'fotku, ktorú medzitým niekto nahradil, nemaže')
        self.assertNotIn(FB, self.stav())

    def test_subor_stiahnutej_polozky_s_inym_id_sa_nezmaze(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.dopln_lokalne('realizacie/rd-ahoj.json', id='rucne-prepisane')
        del self.polozky[1]
        self.stiahnute = [FB]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertEqual(s['obsah/obrazky/rd-ahoj/01.png'], PNG)
        self.assertNotIn(FB, self.stav())
        self.assertIn(f'nenesie id {FB}', self.vystup)

    def test_zmena_typu_id_zlyha_bez_zapisu(self):
        self.subory['n1'] = JPG4
        self.novinky = [self.novinka('nova', 'fb-77', [('n1', 'jpg')])]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.novinky = []
        self.polozky.append(self.polozka('nova', 'fb-77', []))
        self.nastav_api()
        self.over_bez_zapisu()
        self.assertIn('fb-77', self.vystup)

    def test_nespravovane_polozky_sa_neoveruju_ani_nezapisu(self):
        self.polozky.append({'id': 'web-bmbuilding-zla', 'slug': '../zla', 'fotky': 'nie zoznam'})
        self.polozky.append({'id': 's-bmbuilding-0123456789abcdef', 'slug': 'fotka'})
        self.polozky.append({'id': 'fb-abc', 'slug': 'neplatne-fb'})
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('z Facebooku a zo správy 2, prvé naplnenie a iné (nechávam repozitáru) 3', self.vystup)
        self.assertEqual(sorted(self.stav()), sorted([FB, SPRAVA]))
        self.assertFalse(os.path.exists(os.path.join(self.obsah, 'realizacie', 'neplatne-fb.json')))

    def test_len_get_poziadavky(self):
        self.subory['n1'] = JPG4
        self.novinky = [self.novinka('nova', 'fb-77', [('n1', 'jpg')])]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(set(self.server.metody), {'GET'})
        self.assertIn('/api/bmbuilding/realizacia', self.server.poziadavky)
        self.assertIn('/api/bmbuilding/novinka', self.server.poziadavky)


if __name__ == '__main__':
    unittest.main()
