"""Testy stiahni.py: lokálny http.server v tom istom procese, bez internetu.

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
PNG = b'\x89PNG\r\n\x1a\n' + b'png-ukazka'


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Server:
    """Trasy: cesta → (kód, telo). Počíta požiadavky na fotky."""

    def __init__(self):
        self.trasy = {}
        self.poziadavky = []
        server = self

        class Obsluha(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.poziadavky.append(self.path)
                kod, telo = server.trasy.get(self.path, (404, b'{"chyba":"Nena\xc5\xa1lo sa."}'))
                self.send_response(kod)
                if kod in (301, 302):
                    self.send_header('Location', telo.decode())
                    telo = b''
                self.send_header('Content-Length', str(len(telo)))
                self.end_headers()
                self.wfile.write(telo)

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
        self.polozky = [self.polozka('dvojdom', 'uuid-0000-dvojdom', [('a1', 'jpg'), ('a2', 'jpg')], poradie=1),
                        self.polozka('rd-ahoj', 'fb-123_456', [('b1', 'png')], poradie=2)]
        self.nastav_api()

    def polozka(self, slug, pid, fotky, poradie=0):
        return {
            'id': pid, 'slug': slug, 'typ': 'realizacia', 'poradie': poradie, 'zdroj': 'sprava',
            'upravene': '2026-11-21T05:00:00.000Z', 'nazov': f'Stavba {slug}', 'miesto': 'Bratislava',
            'rola': 'Stavebný dozor', 'text': 'Riadok 1\n<script>alert(1)</script>', 'datum': '2026-10-04',
            'investor': 'Mesto Bratislava',
            'fotky': [{'id': fid, 'url': f'{self.server.zaklad}/f/bmbuilding/{fid}.{ext}', 'alt': f'Fotka {fid}',
                       'sirka': 10, 'vyska': 10, 'sha256': sha(self.subory[fid])} for fid, ext in fotky],
        }

    def nastav_api(self, telo=None, kod=200):
        if telo is None:
            telo = json.dumps({'klient': 'bmbuilding', 'typ': 'realizacia', 'aktualizovane': None, 'polozky': self.polozky}).encode()
        self.server.trasy['/api/bmbuilding/realizacia'] = (kod, telo)
        for p in self.polozky:
            for f in p['fotky']:
                cesta = f['url'][len(self.server.zaklad):]
                self.server.trasy[cesta] = (200, self.subory[f['id']])

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

    def test_prve_stiahnutie_zapise_presny_tvar_dohody(self):
        self.assertEqual(self.spusti(), 0, self.vystup)
        d = self.citaj('realizacie/dvojdom.json')
        self.assertEqual(list(d), ['id', 'slug', 'nazov', 'miesto', 'rola', 'projektant', 'roky', 'text', 'investor',
                                   'zhotovitel', 'fotky', 'zdroj', 'fb_url', 'datum', 'poradie', 'zverejnene'])
        # investor a zhotovitel (doplnok dohody z linky bmb-web, build.py ich zobrazuje) prejdú zo servera do súboru.
        self.assertEqual(d['investor'], 'Mesto Bratislava')
        self.assertIsNone(d['zhotovitel'])
        self.assertEqual(d['id'], 'uuid-0000-dvojdom')
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
        stav = self.citaj('.obsah-stav.json')
        self.assertEqual(stav['realizacie']['dvojdom']['fotky']['obrazky/dvojdom/02.jpg'], sha(JPG2))
        self.assertEqual(s['obsah/firma.json'], b'{"nazov": "BM Building s.r.o."}\n')
        self.assertIn('obsah/realizacie/stara-web.json', s)
        self.assertFalse(any('.obsah-tmp-' in k for k in s), 'dočasný priečinok nezostal')

    def test_druhe_spustenie_bez_zmeny_nic_nestiahne_ani_nezapise(self):
        self.assertEqual(self.spusti(), 0)
        pred = self.snimka()
        casy = {k: os.stat(os.path.join(self.dir, k)).st_mtime_ns for k in pred}
        self.server.poziadavky.clear()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertEqual(self.server.fotky(), [])
        self.assertIn('zmien 0', self.vystup)
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
        self.assertEqual(self.citaj('.obsah-stav.json')['realizacie']['dvojdom']['fotky']['obrazky/dvojdom/02.jpg'], sha(JPG3))

    def test_menej_fotiek_a_ina_pripona_zmaze_stare_subory(self):
        self.assertEqual(self.spusti(), 0)
        self.polozky[0] = self.polozka('dvojdom', 'uuid-0000-dvojdom', [('b1', 'png')], poradie=1)
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertEqual(s['obsah/obrazky/dvojdom/01.png'], PNG)
        self.assertNotIn('obsah/obrazky/dvojdom/01.jpg', s)
        self.assertNotIn('obsah/obrazky/dvojdom/02.jpg', s)

    def test_zmazana_polozka_zmizne_aj_s_fotkami_a_web_subory_ostanu(self):
        self.assertEqual(self.spusti(), 0)
        with open(os.path.join(self.obsah, 'realizacie', 'z-facebooku.json'), 'w', encoding='utf-8') as f:
            json.dump({'slug': 'z-facebooku', 'nazov': 'FB', 'zdroj': 'fb', 'fotky': []}, f)
        del self.polozky[1]
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/rd-ahoj.json', s)
        self.assertNotIn('obsah/obrazky/rd-ahoj/01.png', s)
        self.assertFalse(os.path.exists(os.path.join(self.obsah, 'obrazky', 'rd-ahoj')))
        self.assertNotIn('obsah/realizacie/z-facebooku.json', s, 'zdroj fb, ktorý API nemá, sa zmaže')
        self.assertIn('obsah/realizacie/stara-web.json', s, 'zdroj web, ktorý API nikdy nemalo, ostane')
        self.assertEqual(s['obsah/obrazky/stara-web/01.jpg'], JPG)
        self.assertIn('obsah/firma.json', s)
        self.assertNotIn('rd-ahoj', self.citaj('.obsah-stav.json')['realizacie'])

    def test_web_polozka_prevzata_z_api_sa_po_zmazani_v_api_zmaze(self):
        self.polozky.append(self.polozka('stara-web', 'web-stara-web', [('a1', 'jpg')], poradie=3))
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.polozky.pop()
        self.nastav_api()
        self.assertEqual(self.spusti(), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/stara-web.json', s)
        self.assertNotIn('obsah/obrazky/stara-web/01.jpg', s)

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

    def test_zle_sha256_fotky_nezapise_nic(self):
        self.polozky[1]['fotky'][0]['sha256'] = sha(b'ine')
        self.nastav_api()
        self.over_bez_zapisu()

    def test_zly_json_a_zly_tvar_zlyhaju(self):
        for telo in (b'<html>chyba</html>', b'', b'[]', b'{"polozky": {}}', json.dumps({'polozky': [{'id': 'x', 'slug': 'a'}]}).encode()):
            self.nastav_api(telo=telo)
            self.over_bez_zapisu()

    def test_presmerovanie_sa_neprijme(self):
        self.server.trasy['/api/bmbuilding/realizacia'] = (302, b'http://127.0.0.1:9/ine')
        self.over_bez_zapisu()

    def test_prazdna_odpoved_bez_povolenia_zlyha_s_povolenim_prejde(self):
        self.assertEqual(self.spusti(), 0)
        self.polozky = []
        self.nastav_api()
        self.over_bez_zapisu()
        self.assertIn('--povol-prazdne', self.vystup)
        self.assertEqual(self.spusti('--povol-prazdne'), 0, self.vystup)
        s = self.snimka()
        self.assertNotIn('obsah/realizacie/dvojdom.json', s)
        self.assertIn('obsah/realizacie/stara-web.json', s)
        self.assertIn('obsah/firma.json', s)

    def test_sucho_nic_nezapise_ani_nestiahne_fotky(self):
        pred = self.snimka()
        self.assertEqual(self.spusti('--sucho'), 0, self.vystup)
        self.assertEqual(self.snimka(), pred)
        self.assertEqual(self.server.fotky(), [])
        self.assertIn('stiahol by som: obrazky/dvojdom/01.jpg', self.vystup)
        self.assertIn('zapísal by som: realizacie/rd-ahoj.json', self.vystup)

    def test_chybajuca_alebo_zla_adresa_api(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(stiahni.main(['--api', '', '--obsah', self.obsah]), 1)
            self.assertEqual(stiahni.main(['--api', 'http://example.com/api/bmbuilding', '--obsah', self.obsah]), 1)
            self.assertEqual(stiahni.main(['--api', 'file:///etc', '--obsah', self.obsah]), 1)
        self.assertIn('OBSAH_API', err.getvalue())

    def test_poskodeny_stav_nic_nezmaze(self):
        self.assertEqual(self.spusti(), 0)
        with open(os.path.join(self.obsah, '.obsah-stav.json'), 'w', encoding='utf-8') as f:
            f.write('{zly')
        with open(os.path.join(self.obsah, 'realizacie', 'z-webu.json'), 'w', encoding='utf-8') as f:
            json.dump({'slug': 'z-webu', 'nazov': 'Web', 'zdroj': 'web'}, f)
        self.assertEqual(self.spusti(), 0, self.vystup)
        self.assertIn('obsah/realizacie/z-webu.json', self.snimka())
        self.assertIn('dvojdom', self.citaj('.obsah-stav.json')['realizacie'])

    def test_bezpecna_cesta_odmietne_cestu_von(self):
        for rel in ('../x', 'obrazky/../../x', '/etc/passwd', '\\x', ''):
            with self.assertRaises(stiahni.Chyba):
                stiahni.bezpecna_cesta(self.obsah, rel)


if __name__ == '__main__':
    unittest.main()
