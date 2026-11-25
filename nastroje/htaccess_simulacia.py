"""Simulácia .htaccess ostrého webu BM Building pre Apache 2.4 (build.py --hosting apache) bez Apache.

Audit naplno 25. 11. 2026, položka 1 (FTP na WebHouse): skutočný Apache na PC nie je, pravidlá sa overujú touto
simuláciou podmnožiny Apache 2.4 v kontexte priečinka (.htaccess v koreni webu):
- mod_rewrite (https://httpd.apache.org/docs/2.4/mod/mod_rewrite.html, príznaky
  https://httpd.apache.org/docs/2.4/rewrite/flags.html): RewriteEngine; RewriteCond s %{HTTP_HOST}, %{HTTPS},
  %{REQUEST_FILENAME}, %{REQUEST_URI} a $N, vzory -d, -f, =reťazec a regulárny výraz, ! a príznaky NC a OR (reťaz
  OR ako v apply_rewrite_list); RewriteRule proti ceste bez úvodnej lomky (v priečinku sa predpona odreže a PATH_INFO
  pripojí), náhrada '-' alebo absolútna adresa s $N a %{...}, príznaky R=kód, L, QSD, NE. Externé presmerovanie bez
  QSD pripojí pôvodnú query, bez NE sa znaky mimo bezpečných znakov cesty (napríklad #) kódujú ako %xx. Kód mimo 3xx
  náhradu zahodí a skončí ako L.
- prechod cesty ako jadro Apache (ap_directory_walk): segment, ktorý neexistuje, alebo súbor uprostred cesty ukončí
  REQUEST_FILENAME, zvyšok je PATH_INFO (index.php/kontakt pri súbore index.php); statický súbor s PATH_INFO je 404.
- súbory .ht* zakáže hlavný konfiguračný súbor Apache (Require all denied v predvolenom httpd.conf), teda 403.
- mod_dir (https://httpd.apache.org/docs/2.4/mod/mod_dir.html): DirectorySlash (301 na adresu s lomkou) a DirectoryIndex
  (podpožiadavka, ktorá znova prejde pravidlami bez pravidiel s príznakom R, aj R=404, ako mod_rewrite.c
  apply_rewrite_list pri r->main; presmerovanie z nej ide klientovi); priečinok bez indexu je výpis obsahu alebo 403,
  čo test nikdy nesmie vidieť.
- ErrorDocument 404 (interné presmerovanie GET, ktoré znova prejde pravidlami, https://httpd.apache.org/docs/2.4/mod/core.html#errordocument).
- odpoveď: Content-Type z prípony (základ staršieho mime.types bez AVIF, WebP, woff2 a webmanifest; AddType, AddCharset,
  AddDefaultCharset pre text/plain a text/html), Header always set, ExpiresByType (Cache-Control max-age pri 200) a gzip
  podľa AddOutputFilterByType.
Čo .htaccess zo zostavy nepoužíva (vnútorný prepis, RewriteBase, iné príznaky alebo premenné, Options, iné direktívy),
simulácia odmietne výnimkou, aby nové pravidlo nezostalo neoverené.

  python nastroje/htaccess_simulacia.py --web DIR    # adresy a výsledky pre výstup build.py --hosting apache, kód 1 pri chybe
  python nastroje/htaccess_simulacia.py --web DIR --server   # server simulácie pre kontrola_spustenia.py --lokalne PORT
"""
import argparse
import gzip
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KOREN = Path(__file__).resolve().parents[1]
DOMENA = 'https://bmbuilding.sk'
# koreň webu v simulovanom súborovom systéme (hodnota %{REQUEST_FILENAME})
KOREN_FS = '/srv/www_root/'
# základ typov staršieho mime.types (AVIF, WebP, woff2 a webmanifest v ňom nie sú, pridá ich AddType)
ZAKLAD_TYPOV = {'html': 'text/html', 'htm': 'text/html', 'css': 'text/css', 'js': 'application/javascript',
                'json': 'application/json', 'txt': 'text/plain', 'xml': 'application/xml', 'svg': 'image/svg+xml',
                'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'ico': 'image/x-icon',
                'pdf': 'application/pdf', 'ini': 'text/plain'}
# bezpečné znaky cesty pri presmerovaní bez NE (ap_escape_uri: písmená, číslice a $-_.+!*'(),:@&=/~)
BEZPECNE = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$-_.+!*\'(),:@&=/~')
JEDNOTKY = {'second': 1, 'seconds': 1, 'minute': 60, 'minutes': 60, 'hour': 3600, 'hours': 3600, 'day': 86400,
            'days': 86400, 'week': 604800, 'weeks': 604800, 'month': 2592000, 'months': 2592000, 'year': 31536000,
            'years': 31536000}
# súbory a priečinky starej Joomly (Joomla 3 a 4) a omylom nahraté súbory zostavy pre GitHub Pages: na FTP ostávajú
# vedľa nového webu, kým Andrej výslovne nepovie inak, a web ich nesmie ukázať
JOOMLA = ('index.php', 'configuration.php', 'htaccess.txt', 'web.config.txt', 'LICENSE.txt', 'README.txt',
          'robots.txt.dist', 'administrator/index.php', 'administrator/components/com_login/login.php',
          'api/index.php', 'bin/index.html', 'cache/index.html', 'images/index.html', 'cli/joomla.php', 'components/com_content/content.php',
          'images/joomla_black.png', 'images/stories/foto.jpg', 'includes/defines.php', 'language/sk-SK/sk-SK.ini',
          'layouts/joomla/content/info_block.php', 'libraries/import.php', 'logs/index.html', 'media/system/js/core.js',
          'modules/mod_menu/mod_menu.php', 'plugins/system/sef/sef.php', 'templates/protostar/index.php',
          'tmp/index.html', 'zaloha-2019.zip', '.zostava.json', 'CNAME', '.nojekyll')


def koduj_cestu(s):
    return ''.join(c if c in BEZPECNE else ''.join(f'%{b:02x}' for b in c.encode('utf-8')) for c in s)


def koduj_url(url):
    m = re.match(r'^(https?://[^/]*)(.*)$', url)
    return m.group(1) + koduj_cestu(m.group(2)) if m else koduj_cestu(url)


def slova(riadok):
    """Argumenty direktívy ako ap_getword_conf: úvodzovky ' alebo " ohraničia slovo, \\\\ je jedna spätná lomka."""
    v, i, n = [], 0, len(riadok)
    while i < n:
        while i < n and riadok[i] in ' \t':
            i += 1
        if i >= n:
            break
        q = riadok[i] if riadok[i] in '"\'' else ''
        j = i + 1 if q else i
        s = []
        while j < n and ((riadok[j] != q) if q else riadok[j] not in ' \t'):
            if riadok[j] == '\\' and j + 1 < n and (riadok[j + 1] == '\\' or (q and riadok[j + 1] == q)):
                j += 1
            s.append(riadok[j])
            j += 1
        v.append(''.join(s))
        i = j + 1 if q else j
    return v


def sekundy(text):
    kusy = text.split()
    if len(kusy) < 4 or kusy[0] not in ('access', 'now') or kusy[1] != 'plus' or len(kusy[2:]) % 2:
        raise ValueError(f'ExpiresByType {text!r}: simulácia pozná len „access plus N jednotka“')
    return sum(int(kusy[k]) * JEDNOTKY[kusy[k + 1]] for k in range(2, len(kusy), 2))


class Subory:
    """Súborový systém koreňa webu: súbory výstupu zostavy a navyše (napríklad JOOMLA, „.well-known/…“)."""

    def __init__(self, koren=None, navyse=()):
        self.koren = Path(koren) if koren else None
        self.subory, self.priecinky = set(), {''}
        if self.koren:
            for p in self.koren.rglob('*'):
                rel = p.relative_to(self.koren).as_posix()
                (self.subory if p.is_file() else self.priecinky).add(rel)
        for x in navyse:
            self.pridaj(x)

    def pridaj(self, rel):
        rel = rel.strip('/')
        self.subory.add(rel)
        casti = rel.split('/')
        for k in range(1, len(casti)):
            self.priecinky.add('/'.join(casti[:k]))

    def typ(self, rel):
        rel = re.sub(r'/+', '/', rel).strip('/')
        if rel in self.subory:
            return 'subor'
        if rel in self.priecinky:
            return 'priecinok'
        return None

    def bajty(self, rel):
        if self.koren and (self.koren / rel).is_file():
            return (self.koren / rel).read_bytes()
        return b'<?php /* simulovany subor starej Joomly */'


class Pravidlo:
    def __init__(self, riadok, vzor, nahrada, priznaky, podmienky):
        self.riadok = riadok
        self.neguj = vzor.startswith('!')
        self.vzor = re.compile(vzor[1:] if self.neguj else vzor)
        self.nahrada = nahrada
        self.podmienky = podmienky
        self.kod, self.L, self.QSD, self.NE = None, False, False, False
        for p in priznaky:
            k, _, h = p.partition('=')
            if k == 'R':
                self.kod = int(h or 302)
            elif k in ('L', 'QSD', 'NE') and not h:
                setattr(self, k, True)
            else:
                raise ValueError(f'{riadok}: príznak {p} simulácia nepozná')


class Podmienka:
    def __init__(self, riadok, test, vzor, priznaky):
        self.riadok, self.test = riadok, test
        self.NC = 'NC' in priznaky
        self.OR = 'OR' in priznaky
        if set(priznaky) - {'NC', 'OR'}:
            raise ValueError(f'{riadok}: príznaky {priznaky} simulácia nepozná')
        self.neguj = vzor.startswith('!')
        v = vzor[1:] if self.neguj else vzor
        if v in ('-d', '-f'):
            self.druh, self.hodnota = v, None
        elif v.startswith('='):
            self.druh, self.hodnota = '=', v[1:]
        elif v[:1] in '<>' or v.startswith('-'):
            raise ValueError(f'{riadok}: vzor {vzor} simulácia nepozná')
        else:
            self.druh, self.hodnota = 're', re.compile(v, re.I if self.NC else 0)


def priznaky(slovo):
    if not (slovo.startswith('[') and slovo.endswith(']')):
        raise ValueError(f'príznaky {slovo!r} nie sú v [ ]')
    return [p.strip() for p in slovo[1:-1].split(',') if p.strip()]


class Htaccess:
    def __init__(self, text, chyba_moduly=()):
        self.index, self.chybove, self.engine, self.pravidla = [], {}, False, []
        self.hlavicky, self.expires, self.expires_zapnute = [], {}, False
        self.typy, self.sady, self.predvolena_sada, self.gzip = {}, {}, None, set()
        zasobnik, podmienky = [], []
        for cislo, cely in enumerate(text.splitlines(), 1):
            r = cely.strip()
            if not r or r.startswith('#'):
                continue
            m = re.fullmatch(r'<IfModule\s+(!?)([\w.]+)>', r)
            if m:
                zasobnik.append((m.group(2) not in chyba_moduly) != bool(m.group(1)))
                continue
            if r == '</IfModule>':
                zasobnik.pop()
                continue
            if r.startswith('<'):
                raise ValueError(f'riadok {cislo}: blok {r} simulácia nepozná')
            if not all(zasobnik):
                continue
            meno, _, zvysok = r.partition(' ')
            a = slova(zvysok)
            popis = f'riadok {cislo}: {r}'
            if meno == 'AddDefaultCharset':
                self.predvolena_sada = None if a[0].lower() == 'off' else a[0]
            elif meno == 'AddType':
                for ext in a[1:]:
                    self.typy[ext.lstrip('.').lower()] = a[0]
            elif meno == 'AddCharset':
                for ext in a[1:]:
                    self.sady[ext.lstrip('.').lower()] = a[0]
            elif meno == 'DirectoryIndex':
                self.index = a
            elif meno == 'ErrorDocument':
                if not a[1].startswith('/'):
                    raise ValueError(f'{popis}: ErrorDocument simulácia pozná len miestnu cestu')
                self.chybove[int(a[0])] = a[1]
            elif meno == 'RewriteEngine':
                self.engine = a[0].lower() == 'on'
            elif meno == 'RewriteCond':
                podmienky.append(Podmienka(popis, a[0], a[1], priznaky(a[2]) if len(a) > 2 else []))
            elif meno == 'RewriteRule':
                if len(a) not in (2, 3):
                    raise ValueError(f'{popis}: RewriteRule potrebuje vzor, náhradu a príznaky')
                self.pravidla.append(Pravidlo(popis, a[0], a[1], priznaky(a[2]) if len(a) > 2 else [], podmienky))
                podmienky = []
            elif meno == 'Header':
                if a[:2] != ['always', 'set'] or len(a) != 4:
                    raise ValueError(f'{popis}: simulácia pozná len Header always set Meno "hodnota"')
                self.hlavicky.append((a[2], a[3]))
            elif meno == 'ExpiresActive':
                self.expires_zapnute = a[0].lower() == 'on'
            elif meno == 'ExpiresByType':
                self.expires[a[0]] = sekundy(a[1])
            elif meno == 'AddOutputFilterByType':
                if a[0] != 'DEFLATE':
                    raise ValueError(f'{popis}: simulácia pozná len filter DEFLATE')
                self.gzip.update(a[1:])
            else:
                raise ValueError(f'{popis}: direktívu {meno} simulácia nepozná')
        if zasobnik:
            raise ValueError('neuzavretý blok <IfModule>')
        if podmienky:
            raise ValueError('RewriteCond bez RewriteRule na konci súboru')

    # --- prechod cesty, premenné a podmienky
    @staticmethod
    def prechod(fs, rel):
        """(REQUEST_FILENAME relatívne ku koreňu, PATH_INFO, typ) ako ap_directory_walk."""
        lomka = rel.endswith('/')
        casti = [c for c in rel.split('/') if c]
        akt = ''
        for i, c in enumerate(casti):
            kand = f'{akt}/{c}' if akt else c
            t = fs.typ(kand)
            if t != 'priecinok':
                zvysok = casti[i + 1:]
                info = ('/' + '/'.join(zvysok) if zvysok else '') + ('/' if lomka and (zvysok or t == 'subor') else '')
                return kand, info, t
            akt = kand
        return akt + ('/' if lomka and akt else ''), '', 'priecinok'

    @staticmethod
    def rozvin(s, premenne, skupiny):
        def nahrad(m):
            if m.group(1) is not None:
                if m.group(1) not in premenne:
                    raise ValueError(f'premennú %{{{m.group(1)}}} simulácia nepozná')
                return premenne[m.group(1)]
            n = int(m.group(2))
            if n == 0 or n > len(skupiny):
                raise ValueError(f'${n} nemá skupinu vo vzore')
            return skupiny[n - 1] or ''
        if re.search(r'%\d', s):
            raise ValueError('spätné odkazy %N simulácia nepozná')
        return re.sub(r'%\{([A-Za-z_:-]+)\}|\$(\d)', nahrad, s)

    def podmienka(self, c, premenne, skupiny, fs):
        hodnota = self.rozvin(c.test, premenne, skupiny)
        if c.druh in ('-d', '-f'):
            if not hodnota.startswith(KOREN_FS.rstrip('/')):
                raise ValueError(f'{c.riadok}: {c.druh} len nad cestou v koreni webu')
            t = fs.typ(hodnota[len(KOREN_FS.rstrip('/')):])
            vysledok = t == ('priecinok' if c.druh == '-d' else 'subor')
        elif c.druh == '=':
            vysledok = hodnota.lower() == c.hodnota.lower() if c.NC else hodnota == c.hodnota
        else:
            vysledok = c.hodnota.search(hodnota) is not None
        return vysledok != c.neguj

    def podmienky(self, zoznam, premenne, skupiny, fs):
        """Reťaz podmienok ako apply_rewrite_list: [OR] spojí podmienku s nasledujúcou, inak všetky musia platiť."""
        i = 0
        while i < len(zoznam):
            c = zoznam[i]
            ok = self.podmienka(c, premenne, skupiny, fs)
            if c.OR:
                if not ok:
                    i += 1
                    continue
                while i < len(zoznam) and zoznam[i].OR:
                    i += 1
            elif not ok:
                return False
            i += 1
        return True

    # --- spracovanie požiadavky
    def prepis(self, z, kroky):
        """Pravidlá mod_rewrite v kontexte koreňa webu. Vráti None (bez zmeny) alebo {'stav': kód, 'location'}."""
        if not self.engine:
            return None
        rel = z['uri'].lstrip('/')
        subor, info, _ = self.prechod(z['fs'], rel)
        premenne = {'HTTP_HOST': z['host'], 'HTTPS': 'on' if z['schema'] == 'https' else 'off',
                    'REQUEST_FILENAME': KOREN_FS + subor, 'REQUEST_URI': z['uri']}
        for p in self.pravidla:
            # podpožiadavka (DirectoryIndex) pravidlo s príznakom R preskočí, aj R=404: mod_rewrite.c apply_rewrite_list
            # pri r->main != NULL a RULEFLAG_FORCEREDIRECT (oprava po overení, kontrola spustenia: bez index.html v koreni
            # Apache spustí index.php Joomly, simulácia predtým hlásila 301)
            if z.get('podpoziadavka') and p.kod is not None:
                continue
            m = p.vzor.search(rel)
            if (m is not None) == p.neguj:
                continue
            skupiny = () if p.neguj else m.groups()
            if not self.podmienky(p.podmienky, premenne, skupiny, z['fs']):
                continue
            kroky.append(p.riadok)
            if p.nahrada == '-':
                if p.kod is not None:
                    if 300 <= p.kod < 400:
                        raise ValueError(f'{p.riadok}: presmerovanie s náhradou - simulácia nepozná')
                    return {'stav': p.kod}
                if p.L:
                    return None
                continue
            ciel = self.rozvin(p.nahrada, premenne, skupiny)
            if p.kod is None or not re.match(r'^https?://', ciel):
                raise ValueError(f'{p.riadok}: vnútorný prepis simulácia nepozná')
            if not 300 <= p.kod < 400:
                return {'stav': p.kod}
            cesta, otaznik, nova = ciel.partition('?')
            if not p.NE:
                cesta = koduj_url(cesta)
            q = (nova or None) if otaznik else (None if p.QSD else z['query'])
            return {'stav': p.kod, 'location': cesta + (f'?{q}' if q else '')}
        return None

    def spracuj(self, z, kroky):
        posledny = z['uri'].rstrip('/').rsplit('/', 1)[-1]
        if posledny.startswith('.ht'):
            kroky.append('httpd.conf: <Files ".ht*"> Require all denied')
            return {'stav': 403}
        v = self.prepis(z, kroky)
        if v is not None:
            return v
        uri = z['uri']
        subor, info, typ = self.prechod(z['fs'], uri.lstrip('/'))
        if typ == 'priecinok':
            if not uri.endswith('/'):
                kroky.append('mod_dir: DirectorySlash')
                return {'stav': 301, 'location': f'{z["schema"]}://{z["host"]}{koduj_cestu(uri)}/'
                                                 + (f'?{z["query"]}' if z['query'] else '')}
            chyba = None
            for meno in self.index:
                pod = dict(z, uri=uri + meno, podpoziadavka=True)
                v = self.spracuj(pod, kroky)
                if v['stav'] == 200 and v.get('subor'):
                    kroky.append(f'mod_dir: DirectoryIndex {meno}')
                    return v
                if 300 <= v['stav'] < 400:
                    return v
                if v['stav'] not in (200, 404):
                    chyba = v['stav']
            if chyba:
                return {'stav': chyba}
            return {'stav': 403, 'vypis': True}
        if typ == 'subor':
            php = subor.endswith('.php')
            if info and not php:
                return {'stav': 404}
            return {'stav': 200, 'subor': subor, 'php': php}
        return {'stav': 404}

    def poziadavka(self, url, fs, gzip_ok=False):
        """Odpoveď na GET absolútnej adresy: {'stav', 'location', 'subor', 'php', 'vypis', 'typ', 'hlavicky', 'kroky'}.
        Pri 404 interné presmerovanie na ErrorDocument (subor je potom stránka chyby, stav ostáva 404)."""
        p = urllib.parse.urlsplit(url)
        z = {'schema': p.scheme, 'host': p.netloc, 'uri': urllib.parse.unquote(p.path or '/'),
             'query': p.query or None, 'fs': fs}
        kroky = []
        v = dict(self.spracuj(z, kroky))
        if v['stav'] in self.chybove:
            doc = self.chybove[v['stav']]
            kroky.append(f'ErrorDocument {v["stav"]} {doc}')
            d = self.spracuj(dict(z, uri=doc, query=None), kroky)
            if d['stav'] == 200 and d.get('subor') and not d.get('php'):
                v['subor'] = d['subor']
            else:
                v['chyba_dokumentu'] = d
        v['kroky'] = kroky
        v['typ'] = self.typ(v['subor']) if v.get('subor') else None
        v['hlavicky'] = self.hlavicky_odpovede(v, gzip_ok)
        return v

    def typ(self, subor):
        meno = subor.rsplit('/', 1)[-1]
        ext = meno.rsplit('.', 1)[-1].lower() if '.' in meno else ''
        t = self.typy.get(ext) or ZAKLAD_TYPOV.get(ext)
        if not t:
            return None
        sada = self.sady.get(ext) or (self.predvolena_sada if t in ('text/plain', 'text/html') else None)
        return f'{t}; charset={sada}' if sada else t

    def hlavicky_odpovede(self, v, gzip_ok):
        h = list(self.hlavicky)
        if v.get('location'):
            h.append(('Location', v['location']))
        typ = v.get('typ')
        if typ:
            h.append(('Content-Type', typ))
            zaklad = typ.split(';')[0]
            if v['stav'] == 200 and self.expires_zapnute and zaklad in self.expires:
                h.append(('Cache-Control', f'max-age={self.expires[zaklad]}'))
            if gzip_ok and zaklad in self.gzip:
                h += [('Content-Encoding', 'gzip'), ('Vary', 'Accept-Encoding')]
        return h


# --- overenie výstupu zostavy

def ocakavane_presmerovania(web):
    """Staré adresy zo sablona/presmerovania.json s cieľom nezávisle od build.py: premenovaná kotva (#sluzby na
    #cinnost), cieľ realizácie len ak jej stránka vo výstupe je, inak zoznam realizácií."""
    if str(KOREN) not in sys.path:
        sys.path.insert(0, str(KOREN))
    import build  # noqa: E402
    d = json.loads((KOREN / 'sablona' / 'presmerovania.json').read_text(encoding='utf-8'))
    v = []
    for z in d['presmerovania']:
        ciel = build.KOTVY_PREMENOVANE.get(z['ciel'], z['ciel'])
        m = re.match(r'^/realizacie/([a-z0-9-]+)/$', ciel)
        if m and not (Path(web) / 'realizacie' / m.group(1) / 'index.html').is_file():
            ciel = '/realizacie/'
        v.append((z['stara'], ciel))
    return v


def over(web, text=None, navyse=JOOMLA):
    """Overí .htaccess výstupu build.py --hosting apache simuláciou: [{'popis', 'url', 'ocak', 'vysl', 'ok'}].
    text: iný .htaccess (sabotáž v teste), inak web/.htaccess."""
    web = Path(web)
    ht = Htaccess(text if text is not None else (web / '.htaccess').read_text(encoding='utf-8'))
    fs = Subory(web, list(navyse) + ['.well-known/acme-challenge/token-simulacia'])
    vysledky = []

    def zapis(popis, url, ocak, v, ok):
        vysledky.append({'popis': popis, 'url': url, 'ocak': ocak,
                         'vysl': {k: v.get(k) for k in ('stav', 'location', 'subor', 'php', 'vypis', 'typ')},
                         'kroky': v.get('kroky', []), 'ok': bool(ok)})

    def presm(popis, url, ciel):
        v = ht.poziadavka(url, fs)
        zapis(popis, url, f'301 {ciel}', v, v['stav'] == 301 and v.get('location') == ciel)

    def stranka(popis, url, subor, stav=200):
        v = ht.poziadavka(url, fs)
        zapis(popis, url, f'{stav} {subor}', v, v['stav'] == stav and v.get('subor') == subor and not v.get('php'))

    # 1. staré adresy Joomly: jeden skok 301 na https://bmbuilding.sk, z každého variantu
    for stara, ciel in ocakavane_presmerovania(web):
        for varianta in (f'{DOMENA}{stara}', f'{DOMENA}{stara}/', f'{DOMENA}{stara}?start=5',
                         f'http://www.bmbuilding.sk{stara}', f'http://bmbuilding.sk{stara}/?tmpl=component&format=feed'):
            presm('stará adresa', varianta, DOMENA + ciel)
        cesta, _, kotva = ciel.partition('#')
        subor = cesta.lstrip('/') + 'index.html'
        stranka('cieľ starej adresy', DOMENA + cesta, subor)
        if kotva:
            html_ciela = (web / subor).read_text(encoding='utf-8')
            vysledky.append({'popis': f'kotva #{kotva} v cieli', 'url': DOMENA + ciel, 'ocak': f'id="{kotva}"',
                             'vysl': {}, 'kroky': [], 'ok': f'id="{kotva}"' in html_ciela})
    for url in (f'{DOMENA}/index.php?option=com_content&view=article&id=12&Itemid=5',
                f'{DOMENA}/index.php/component/content/article/12', f'{DOMENA}/index.php/neznama-stranka',
                'http://www.bmbuilding.sk/index.php?option=com_contact'):
        presm('iná adresa Joomly', url, DOMENA + '/')
    # 2. https a holá doména jedným skokom, query sa pri bežných stránkach zachová
    for url, ciel in (('http://bmbuilding.sk/', '/'), ('http://www.bmbuilding.sk/', '/'), ('https://www.bmbuilding.sk/', '/'),
                      ('https://www.bmbuilding.sk/kontakt/', '/kontakt/'), ('http://www.bmbuilding.sk/kontakt', '/kontakt/'),
                      ('https://bmbuilding.sk/kontakt', '/kontakt/'), ('http://bmbuilding.sk/realizacie/rd-pezinok', '/realizacie/rd-pezinok/'),
                      ('https://bmbuilding.sk/realizacie?druh=rodinne', '/realizacie/?druh=rodinne'),
                      ('http://bmbuilding.sk/realizacie/?druh=rodinne&miesto=bratislava', '/realizacie/?druh=rodinne&miesto=bratislava'),
                      ('http://WWW.BMBUILDING.SK/opravnenia/', '/opravnenia/'), ('http://bmbuilding.sk/llms.txt', '/llms.txt'),
                      ('http://bmbuilding.sk/neexistuje/', '/neexistuje/'), ('http://bmbuilding.sk/administrator/', '/administrator/')):
        presm('https a holá doména', url, DOMENA + ciel)
    # 3. každý súbor zostavy je na https://bmbuilding.sk, priečinok stránky s lomkou dá index.html
    for rel in sorted(fs.subory):
        if not (web / rel).is_file() or rel == '.htaccess':
            continue
        stranka('súbor webu', f'{DOMENA}/{urllib.parse.quote(rel)}', rel)
        if rel.endswith('/index.html'):
            stranka('stránka webu', f'{DOMENA}/{rel[:-len("index.html")]}', rel)
    stranka('úvod', f'{DOMENA}/', 'index.html')
    # 4. 404 s vlastnou stránkou: neexistujúca adresa, priečinok bez index.html, Joomla, súbory zostavy, PHP
    for rel in ('neexistuje/', 'realizacie/neexistuje/', 'kontakt/neexistuje.html', 'obrazky/', 'fonts', 'og/',
                'obrazky/neexistuje/', 'api/index.php/v1/content/articles', 'administrator/index.php?option=com_login',
                'images/', 'media/', 'index.php5', 'Index.php',
                # priečinok Joomly s prázdnym index.html bez lomky: hneď 404, nie 301 na adresu s lomkou (oprava po overení)
                'images', 'cache') + tuple(x for x in navyse if x != 'index.php'):
        stranka('404', f'{DOMENA}/{rel}', '404.html', 404)
    v = ht.poziadavka(f'{DOMENA}/.htaccess', fs)
    zapis('.htaccess nie je verejný', f'{DOMENA}/.htaccess', '403 alebo 404', v, v['stav'] in (403, 404) and not v.get('subor') == '.htaccess')
    # 5. overenie certifikátu Let's Encrypt cez http aj www bez presmerovania
    for url in ('http://bmbuilding.sk/.well-known/acme-challenge/token-simulacia',
                'http://www.bmbuilding.sk/.well-known/acme-challenge/token-simulacia'):
        stranka('Let\'s Encrypt', url, '.well-known/acme-challenge/token-simulacia')
    # 6. iná doména na tom istom hostingu (koreň v podpriečinku) pravidlá nezdedí
    fs.pridaj('ina-domena/index.php')
    v = ht.poziadavka('http://ina-domena.sk/ina-domena/index.php', fs)
    zapis('iná doména bez pravidiel', 'http://ina-domena.sk/ina-domena/index.php', '200 php', v, v['stav'] == 200 and v.get('php'))
    v = ht.poziadavka('http://ina-domena.sk/ina-domena/', fs)
    zapis('iná doména: index.php ako index', 'http://ina-domena.sk/ina-domena/', '200 php', v, v['stav'] == 200 and v.get('php'))
    # 7. typy, cache a hlavičky
    hlavne = {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'strict-origin-when-cross-origin',
              'X-Frame-Options': 'SAMEORIGIN', 'Content-Security-Policy': "frame-ancestors 'self'"}
    priklady = {'index.html': ('text/html; charset=UTF-8', 0), 'app.js': ('application/javascript; charset=UTF-8', 31536000),
                'robots.txt': ('text/plain; charset=UTF-8', 3600), 'llms.txt': ('text/plain; charset=UTF-8', 3600),
                'sitemap.xml': ('application/xml; charset=UTF-8', 3600),
                'site.webmanifest': ('application/manifest+json; charset=UTF-8', 604800),
                'favicon.ico': ('image/x-icon', 2592000), 'logo.svg': ('image/svg+xml; charset=UTF-8', 2592000)}
    for koncovka, typ in (('.avif', 'image/avif'), ('.webp', 'image/webp'), ('.woff2', 'font/woff2'), ('.jpg', 'image/jpeg')):
        prvy = next((r for r in sorted(fs.subory) if r.endswith(koncovka) and (web / r).is_file()), None)
        if prvy:
            priklady[prvy] = (typ, 2592000)
    for rel, (typ, cache) in priklady.items():
        v = ht.poziadavka(f'{DOMENA}/{rel}', fs, gzip_ok=True)
        h = dict(v['hlavicky'])
        gz = typ.split(';')[0] in ('text/html', 'text/plain', 'application/xml', 'application/javascript',
                                   'application/manifest+json', 'image/svg+xml', 'image/x-icon')
        ok = (v['stav'] == 200 and h.get('Content-Type') == typ and h.get('Cache-Control') == f'max-age={cache}'
              and all(h.get(k) == x for k, x in hlavne.items()) and (h.get('Content-Encoding') == 'gzip') == gz)
        zapis('typ, cache a hlavičky', f'{DOMENA}/{rel}', f'{typ}, max-age={cache}, gzip {gz}', dict(v, typ=h.get('Content-Type')), ok)
    for url in (f'{DOMENA}/neexistuje/', 'http://bmbuilding.sk/'):
        v = ht.poziadavka(url, fs)
        h = dict(v['hlavicky'])
        zapis('hlavičky aj pri 404 a 301', url, 'nosniff a frame-ancestors', v,
              all(h.get(k) == x for k, x in hlavne.items()) and 'Cache-Control' not in h)
    return vysledky


# --- server simulácie pre nastroje/kontrola_spustenia.py --lokalne

class _Obsluha(BaseHTTPRequestHandler):
    server_version = 'Apache'
    sys_version = ''

    def log_message(self, *a):
        pass

    def _odpovedz(self, telo_poslat):
        ht, fs = self.server.ht, self.server.fs
        schema = self.headers.get('X-Simulacia-Schema', 'http')
        gz = 'gzip' in (self.headers.get('Accept-Encoding') or '')
        v = ht.poziadavka(f'{schema}://{self.headers.get("Host", "")}{self.path}', fs, gzip_ok=gz)
        telo = fs.bajty(v['subor']) if v.get('subor') else (b'<p>Moved</p>' if v.get('location') else b'<p>Chyba</p>')
        hlavicky = v['hlavicky']
        if any(k == 'Content-Encoding' for k, _ in hlavicky):
            telo = gzip.compress(telo, mtime=0)
        self.send_response(v['stav'])
        for k, x in hlavicky:
            self.send_header(k, x)
        self.send_header('Content-Length', str(len(telo)))
        self.end_headers()
        if telo_poslat:
            self.wfile.write(telo)

    def do_GET(self):
        self._odpovedz(True)

    def do_HEAD(self):
        self._odpovedz(False)


def spusti_server(web, text=None, navyse=JOOMLA):
    """Server simulácie na 127.0.0.1 a voľnom porte vo vlákne; schému (http alebo https) berie z hlavičky
    X-Simulacia-Schema, hostiteľa z Host. Vráti server (server.server_address[1] je port, zavrieť shutdown())."""
    web = Path(web)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Obsluha)
    srv.ht = Htaccess(text if text is not None else (web / '.htaccess').read_text(encoding='utf-8'))
    srv.fs = Subory(web, navyse)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description='Simulácia .htaccess výstupu build.py --hosting apache.')
    ap.add_argument('--web', type=Path, required=True, help='výstup build.py --hosting apache')
    ap.add_argument('--vsetko', action='store_true', help='vypísať aj vyhovujúce adresy')
    ap.add_argument('--server', action='store_true',
                    help='spustiť server simulácie na 127.0.0.1 (vypíše port pre kontrola_spustenia.py --lokalne PORT, '
                         'beží do Ctrl+C)')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')
    if a.server:
        # kontrola po nahratí na sucho (oprava po overení, kontrola spustenia: README a kontrola_spustenia.py ponúkali
        # --lokalne PORT, ale server sa z príkazového riadka spustiť nedal)
        srv = spusti_server(a.web)
        print(json.dumps({'port': srv.server_address[1], 'web': str(a.web),
                          'prikaz': f'python nastroje/kontrola_spustenia.py --web "{a.web}" --lokalne {srv.server_address[1]}'},
                         ensure_ascii=False), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
        return 0
    vysledky = over(a.web)
    zle = [v for v in vysledky if not v['ok']]
    for v in vysledky:
        if a.vsetko or not v['ok']:
            print(('OK    ' if v['ok'] else 'CHYBA ') + f'{v["popis"]}: {v["url"]} -> {v["vysl"]} (čakám {v["ocak"]})')
    starych = sum(1 for v in vysledky if v['popis'] == 'stará adresa')
    print(json.dumps({'kontrol': len(vysledky), 'chyb': len(zle), 'variantov_starych_adries': starych}, ensure_ascii=False))
    return 1 if zle else 0


if __name__ == '__main__':
    sys.exit(main())
