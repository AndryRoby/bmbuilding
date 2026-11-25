"""Kontrola bmbuilding.sk PO nahratí ostrého webu cez FTP na WebHouse (audit naplno 25. 11. 2026, položka 1).

Spúšťať až po nahratí (pred ním by sa pýtala starého webu Joomla). Len požiadavky GET a HEAD, presmerovania nesleduje
sama, nič neposiela ani nemení. Očakávania berie z lokálneho výstupu build.py --hosting apache, ktorý sa nahral
(.htaccess, sitemap.xml, súbory a veľkosti), takže overí, že hosting robí presne to, čo nahratý .htaccess sľubuje:
https a holá doména jedným skokom, 32 starých adries Joomly, stránky zo sitemap.xml s canonical a bez noindex, typy
a veľkosti súborov, vlastná 404, zvyšky Joomly a súbory zostavy nie sú verejné, bezpečnostné hlavičky, cache a gzip.
Kód 1 pri chybe. Postup: ops/weby-na-mieru/klienti/bmbuilding/kolo-1/SPUSTENIE-FTP.md.

  python nastroje/kontrola_spustenia.py --web D:/ARLing-media/klienti/bmbuilding/web-ftp
  python nastroje/kontrola_spustenia.py --web DIR --vsetko          # navyše HEAD na každý súbor webu (asi 1 900)
  python nastroje/kontrola_spustenia.py --web DIR --lokalne PORT    # tá istá kontrola proti serveru simulácie
                                    # (port vypíše python nastroje/htaccess_simulacia.py --web DIR --server)
"""
import argparse
import gzip
import http.client
import json
import re
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

DOMENA = 'https://bmbuilding.sk'
# zvyšky Joomly a súbory zostavy pre GitHub Pages, ktoré po nahratí nesmú byť verejné (403 alebo 404); priečinky
# Joomly s prázdnym index.html aj bez lomky (/images, /cache): hneď 404, nie 301 na adresu s lomkou (oprava po overení)
NEVEREJNE = ('/administrator/', '/configuration.php', '/README.txt', '/LICENSE.txt', '/htaccess.txt', '/web.config.txt',
             '/images/', '/media/', '/components/', '/templates/', '/cache/', '/images', '/cache',
             '/api/index.php/v1/content/articles', '/.zostava.json', '/CNAME', '/.nojekyll', '/.htaccess', '/obrazky/', '/fonts/')
HLAVICKY = {'x-content-type-options': 'nosniff', 'referrer-policy': 'strict-origin-when-cross-origin',
            'x-frame-options': 'SAMEORIGIN', 'content-security-policy': "frame-ancestors 'self'"}


class Klient:
    """GET a HEAD bez sledovania presmerovaní. Ostro na skutočnú adresu (overený certifikát), lokálne na 127.0.0.1:PORT
    s hlavičkou Host a schémou v X-Simulacia-Schema (server simulácie); bmbuilding.sk sa vtedy vôbec nevolá."""

    def __init__(self, lokalne=None, pauza=0.05):
        self.lokalne = lokalne
        self.pauza = 0 if lokalne else pauza
        self.pocet = 0

    def ziadost(self, url, metoda='GET', gzip_ok=False):
        p = urllib.parse.urlsplit(url)
        cesta = (p.path or '/') + (f'?{p.query}' if p.query else '')
        hlavicky = {'User-Agent': 'Mozilla/5.0 (kontrola spustenia webu)', 'Accept-Encoding': 'gzip' if gzip_ok else 'identity'}
        if self.lokalne:
            spojenie = http.client.HTTPConnection('127.0.0.1', self.lokalne, timeout=30)
            hlavicky.update({'Host': p.netloc, 'X-Simulacia-Schema': p.scheme})
        elif p.scheme == 'https':
            spojenie = http.client.HTTPSConnection(p.hostname, p.port or 443, timeout=30, context=ssl.create_default_context())
        else:
            spojenie = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=30)
        if self.pocet and self.pauza:
            time.sleep(self.pauza)
        self.pocet += 1
        try:
            spojenie.request(metoda, cesta, headers=hlavicky)
            r = spojenie.getresponse()
            telo = r.read()
            h = {k.lower(): v for k, v in r.getheaders()}
            if h.get('content-encoding') == 'gzip' and telo:
                telo = gzip.decompress(telo)
            return r.status, h, telo
        finally:
            spojenie.close()


class Kontrola:
    def __init__(self, web, klient):
        self.web, self.k = Path(web), klient
        self.vysledky = []

    def zapis(self, popis, ok, detail='', zavazne=True):
        """zavazne=False: rýchlosť a hlavičky (gzip, cache, mod_headers); keď chýbajú, web funguje, vypíše sa POZOR
        a do chýb sa to neráta."""
        self.vysledky.append((popis, bool(ok), detail, zavazne))
        znak = 'OK    ' if ok else ('CHYBA ' if zavazne else 'POZOR ')
        print(znak + popis + (f' ({detail})' if detail and not ok else ''))

    def ziskaj(self, url, metoda='GET', gzip_ok=False):
        try:
            return self.k.ziadost(url, metoda, gzip_ok)
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            return 0, {}, f'{e.__class__.__name__}: {e}'.encode('utf-8')

    def presmerovanie(self, url, ciel):
        s, h, telo = self.ziskaj(url)
        loc = h.get('location', '')
        self.zapis(f'{url} -> 301 {ciel}', s == 301 and loc == ciel, f'{s} {loc} {telo[:120]!r}' if not s else f'{s} {loc}')
        return s == 301 and loc == ciel

    def stare_adresy(self):
        """Staré adresy z nahratého .htaccess (riadky RewriteRule ^index\\.php…)."""
        v = []
        for r in (self.web / '.htaccess').read_text(encoding='utf-8').splitlines():
            m = re.match(r'^RewriteRule \^(index\\\.php[^ ]*?)/\?\$ (https://\S+) \[R=301', r)
            if m:
                v.append(('/' + m.group(1).replace('\\.', '.'), m.group(2)))
        return v

    def spusti(self, vsetko=False):
        sitemap = (self.web / 'sitemap.xml').read_text(encoding='utf-8')
        stranky = re.findall(r'<loc>([^<]+)</loc>', sitemap)
        # 1. https a holá doména jedným skokom (query bežnej stránky ostane)
        for url, ciel in (('http://bmbuilding.sk/', '/'), ('http://www.bmbuilding.sk/', '/'), ('https://www.bmbuilding.sk/', '/'),
                          ('https://www.bmbuilding.sk/kontakt/', '/kontakt/'), ('http://www.bmbuilding.sk/kontakt', '/kontakt/'),
                          ('https://bmbuilding.sk/kontakt', '/kontakt/'),
                          ('https://bmbuilding.sk/realizacie?druh=rodinne', '/realizacie/?druh=rodinne')):
            self.presmerovanie(url, DOMENA + ciel)
        # 2. staré adresy Joomly: 301 rovno na cieľ, cieľ je stránka
        stare = self.stare_adresy()
        self.zapis(f'staré adresy v .htaccess: {len(stare)}', len(stare) >= 30, str(len(stare)))
        for stara, ciel in stare:
            if self.presmerovanie(DOMENA + stara, ciel):
                s, h, _ = self.ziskaj(ciel.split('#')[0])
                self.zapis(f'  cieľ {ciel} je stránka', s == 200 and 'text/html' in h.get('content-type', ''), str(s))
        self.presmerovanie(DOMENA + '/index.php?option=com_content&view=article&id=12', DOMENA + '/')
        self.presmerovanie('http://www.bmbuilding.sk/index.php/kontakt?start=5', DOMENA + '/kontakt/')
        # 3. stránky zo sitemap.xml: 200, HTML, canonical na seba, bez noindex
        self.zapis(f'sitemap.xml má {len(stranky)} adries', len(stranky) >= 30, str(len(stranky)))
        for loc in stranky:
            s, h, telo = self.ziskaj(loc)
            t = telo.decode('utf-8', 'replace')
            ok = (s == 200 and 'text/html' in h.get('content-type', '') and f'<link rel="canonical" href="{loc}">' in t
                  and 'noindex' not in t and 'noindex' not in h.get('x-robots-tag', ''))
            self.zapis(f'stránka {loc}', ok, f'{s} {h.get("content-type", "")}')
        # 4. súbory: typ a veľkosť ako v nahratom priečinku (HEAD bez gzip)
        vzorky = ['robots.txt', 'sitemap.xml', 'llms.txt', 'site.webmanifest', 'favicon.ico', 'app.js', 'logo.png',
                  'og/bmbuilding.jpg', 'fonts/geist-latin.woff2']
        for koncovka in ('.avif', '.webp'):
            prvy = next(iter(sorted(self.web.glob(f'obrazky/*/*{koncovka}'))), None)
            if prvy:
                vzorky.append(prvy.relative_to(self.web).as_posix())
        subory = sorted(p.relative_to(self.web).as_posix() for p in self.web.rglob('*') if p.is_file() and p.name != '.htaccess')
        typy = {'.txt': 'text/plain', '.xml': 'xml', '.webmanifest': 'application/manifest+json', '.ico': 'icon',
                '.js': 'javascript', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.avif': 'image/avif',
                '.webp': 'image/webp', '.svg': 'image/svg+xml', '.html': 'text/html'}
        for rel in (subory if vsetko else vzorky):
            if not (self.web / rel).is_file():
                continue
            s, h, _ = self.ziskaj(f'{DOMENA}/{urllib.parse.quote(rel)}', 'HEAD')
            typ = typy.get(Path(rel).suffix, '')
            dlzka = str((self.web / rel).stat().st_size)
            ok = s == 200 and typ in h.get('content-type', '') and h.get('content-length') == dlzka
            self.zapis(f'súbor /{rel}', ok, f'{s} {h.get("content-type", "")} {h.get("content-length")} (lokálne {dlzka})')
        s, _, telo = self.ziskaj(DOMENA + '/robots.txt')
        self.zapis('robots.txt bez Disallow: / a so Sitemap', s == 200 and b'Disallow: /\n' not in telo
                   and f'Sitemap: {DOMENA}/sitemap.xml'.encode() in telo)
        # 5. vlastná 404 a súbory, ktoré nesmú byť verejné
        titul = re.search(r'<title>([^<]+)</title>', (self.web / '404.html').read_text(encoding='utf-8')).group(1)
        for cesta in ('/neexistuje-kontrola-404/', '/realizacie/neexistuje-kontrola-404/'):
            s, _, telo = self.ziskaj(DOMENA + cesta)
            self.zapis(f'{cesta} vráti 404 so stránkou webu', s == 404 and titul.encode('utf-8') in telo, str(s))
        for cesta in NEVEREJNE:
            s, h, _ = self.ziskaj(DOMENA + cesta)
            self.zapis(f'{cesta} nie je verejný', s in (403, 404), f'{s} {h.get("location", "")}')
        # 6. hlavičky, cache a gzip
        s, h, _ = self.ziskaj(DOMENA + '/', gzip_ok=True)
        for k, hodnota in HLAVICKY.items():
            self.zapis(f'hlavička {k}', h.get(k) == hodnota, h.get(k, 'chýba'), zavazne=False)
        self.zapis('HTML s Cache-Control max-age=0', 'max-age=0' in h.get('cache-control', ''), h.get('cache-control', 'chýba'),
                   zavazne=False)
        self.zapis('HTML s gzip', h.get('content-encoding') == 'gzip', h.get('content-encoding', 'bez'), zavazne=False)
        appjs = re.search(r'src="(app\.js\?v=[0-9a-f]+)"', (self.web / 'index.html').read_text(encoding='utf-8'))
        if appjs:
            s, h, _ = self.ziskaj(f'{DOMENA}/{appjs.group(1)}', 'HEAD')
            self.zapis('app.js s cache na rok', s == 200 and 'max-age=31536000' in h.get('cache-control', ''),
                       f'{s} {h.get("cache-control", "chýba")}', zavazne=False)
        chyb = sum(1 for _, ok, _, zavazne in self.vysledky if not ok and zavazne)
        pozor = sum(1 for _, ok, _, zavazne in self.vysledky if not ok and not zavazne)
        print(json.dumps({'kontrol': len(self.vysledky), 'chyb': chyb, 'pozor': pozor, 'poziadaviek': self.k.pocet},
                         ensure_ascii=False))
        return chyb


def main(argv=None):
    ap = argparse.ArgumentParser(description='Kontrola bmbuilding.sk po nahratí cez FTP (len GET a HEAD).')
    ap.add_argument('--web', type=Path, default=Path('D:/ARLing-media/klienti/bmbuilding/web-ftp'),
                    help='nahratý výstup build.py --hosting apache (predvolený D:/ARLing-media/klienti/bmbuilding/web-ftp)')
    ap.add_argument('--vsetko', action='store_true', help='HEAD na každý súbor webu, nielen vzorky')
    ap.add_argument('--lokalne', type=int, metavar='PORT', help='server simulácie na 127.0.0.1:PORT namiesto bmbuilding.sk')
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    if not (a.web / '.htaccess').is_file() or not (a.web / 'sitemap.xml').is_file():
        print(f'CHYBA: {a.web} nie je výstup build.py --hosting apache (chýba .htaccess alebo sitemap.xml)', file=sys.stderr)
        return 2
    return 1 if Kontrola(a.web, Klient(a.lokalne)).spusti(a.vsetko) else 0


if __name__ == '__main__':
    sys.exit(main())
