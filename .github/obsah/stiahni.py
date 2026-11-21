#!/usr/bin/env python3
"""Stiahne realizácie z verejného API arling-obsah do priečinka obsah/ webu klienta.

Použitie: python .github/obsah/stiahni.py --api https://arling-obsah.<subdoména>.workers.dev/api/bmbuilding --obsah obsah [--sucho] [--povol-prazdne]

Zapisuje len obsah/realizacie/<slug>.json, obsah/obrazky/<slug>/NN.<jpg|webp|png> a obsah/.obsah-stav.json.
firma.json a súbory, ktoré API nikdy nemalo (zdroj web), nemení. Zlyhanie zatvára: chyba siete, kód iný ako 200,
zlý JSON alebo zlý tvar = koniec s kódom 1 bez zápisu. Len štandardná knižnica Pythonu 3.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SLUG = re.compile(r'^(?=.*[a-z0-9])[a-z0-9-]{1,80}$')
ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
DATUM = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CESTA_FOTKY = re.compile(r'^/f/[a-z0-9-]{2,40}/[A-Za-z0-9_-]{1,100}\.(jpg|webp|png)$')
SUBOR_FOTKY = re.compile(r'^\d{2}\.(jpg|webp|png)$')
ZDROJE = {'web', 'fb', 'sprava'}
TEXTY = ('miesto', 'rola', 'projektant', 'roky', 'text', 'investor', 'zhotovitel', 'fb_url')
MAX_JSON = 10 * 1024 * 1024
MAX_FOTKA = 6291456
MAX_FOTIEK = 60
STAV = '.obsah-stav.json'
DOCASNY = '.obsah-tmp-'


class Chyba(Exception):
    """Chyba s vetou pre človeka; vedie ku koncu s kódom 1 bez zápisu."""


class BezPresmerovania(urllib.request.HTTPRedirectHandler):
    """Presmerovanie sa neprijíma (urllib potom vráti chybu HTTP s kódom 3xx)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OTVARAC = urllib.request.build_opener(BezPresmerovania)


def over_api(api):
    if not api:
        raise Chyba('Chýba adresa API (--api alebo premenná repozitára OBSAH_API).')
    u = urllib.parse.urlsplit(api)
    lokalny = u.hostname in ('127.0.0.1', 'localhost')
    if u.scheme != 'https' and not (u.scheme == 'http' and lokalny):
        raise Chyba('Adresa API musí začínať https:// (http:// len pre 127.0.0.1 a localhost).')
    if u.query or u.fragment or u.username or u.password or not u.netloc:
        raise Chyba('Adresa API nesmie mať otázku, fragment ani meno a heslo.')
    return api.rstrip('/'), (u.scheme, u.netloc)


def stiahni(url, limit, timeout):
    poziadavka = urllib.request.Request(url, headers={'User-Agent': 'arling-obsah-stiahni/1', 'Accept': '*/*'})
    try:
        with OTVARAC.open(poziadavka, timeout=timeout) as odpoved:
            if odpoved.status != 200:
                raise Chyba(f'Server vrátil kód {odpoved.status} pre {url}.')
            telo = odpoved.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise Chyba(f'Server vrátil kód {e.code} pre {url}.') from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Chyba(f'Sieť zlyhala pre {url} ({type(e).__name__}).') from None
    if len(telo) > limit:
        raise Chyba(f'Odpoveď z {url} je väčšia ako {limit} bajtov.')
    return telo


def typ_obrazka(bajty):
    if bajty[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if bajty[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if len(bajty) >= 12 and bajty[:4] == b'RIFF' and bajty[8:12] == b'WEBP':
        return 'webp'
    return None


def text_alebo_none(hodnota, kde):
    if hodnota is None:
        return None
    if not isinstance(hodnota, str):
        raise Chyba(f'{kde} musí byť text.')
    return hodnota


def over_polozky(data, povod):
    """Overí tvar odpovede API; vráti zoznam položiek v tvare pre zápis. Zlý tvar = Chyba."""
    if not isinstance(data, dict) or not isinstance(data.get('polozky'), list):
        raise Chyba('Odpoveď API nemá tvar { "polozky": [ … ] }.')
    if data.get('typ') not in (None, 'realizacia'):
        raise Chyba('Odpoveď API nie je pre typ realizacia.')
    vysledok = []
    slugy = set()
    idcka = set()
    for i, p in enumerate(data['polozky']):
        kde = f'polozky[{i}]'
        if not isinstance(p, dict):
            raise Chyba(f'{kde} nie je objekt.')
        pid, slug = p.get('id'), p.get('slug')
        if not isinstance(pid, str) or not ID.match(pid):
            raise Chyba(f'{kde}: neplatné id.')
        if not isinstance(slug, str) or not SLUG.match(slug):
            raise Chyba(f'{kde}: neplatný slug (povolené a až z, 0 až 9 a pomlčka, najviac 80 znakov).')
        if slug in slugy or pid in idcka:
            raise Chyba(f'{kde}: slug alebo id je v odpovedi dvakrát.')
        slugy.add(slug)
        idcka.add(pid)
        nazov = p.get('nazov')
        if not isinstance(nazov, str) or not nazov.strip():
            raise Chyba(f'{kde}: chýba názov.')
        polia = {k: text_alebo_none(p.get(k), f'{kde}.{k}') for k in TEXTY}
        if polia['fb_url'] is not None and not polia['fb_url'].startswith('https://'):
            raise Chyba(f'{kde}: fb_url musí začínať https://.')
        datum = text_alebo_none(p.get('datum'), f'{kde}.datum')
        if datum is not None and not DATUM.match(datum):
            raise Chyba(f'{kde}: dátum nemá tvar RRRR-MM-DD.')
        poradie = p.get('poradie', 0)
        if not isinstance(poradie, int) or isinstance(poradie, bool):
            raise Chyba(f'{kde}: poradie musí byť celé číslo.')
        zdroj = p.get('zdroj', 'sprava')
        if zdroj not in ZDROJE:
            raise Chyba(f'{kde}: neznámy zdroj.')
        fotky_api = p.get('fotky') or []
        if not isinstance(fotky_api, list) or len(fotky_api) > MAX_FOTIEK:
            raise Chyba(f'{kde}: fotky musia byť zoznam s najviac {MAX_FOTIEK} položkami.')
        fotky = []
        for j, f in enumerate(fotky_api, start=1):
            kdef = f'{kde}.fotky[{j - 1}]'
            if not isinstance(f, dict):
                raise Chyba(f'{kdef} nie je objekt.')
            url, sha, alt = f.get('url'), f.get('sha256'), f.get('alt', '')
            if not isinstance(url, str) or not isinstance(alt, str):
                raise Chyba(f'{kdef}: chýba url alebo alt.')
            if not isinstance(sha, str) or not SHA.match(sha):
                raise Chyba(f'{kdef}: neplatné sha256.')
            u = urllib.parse.urlsplit(url)
            zhoda = CESTA_FOTKY.match(u.path)
            if (u.scheme, u.netloc) != povod or not zhoda or u.query or u.fragment:
                raise Chyba(f'{kdef}: adresa fotky nie je z rovnakého servera ako API alebo má zlú príponu.')
            pripona = zhoda.group(1)
            rel = f'obrazky/{slug}/{j:02d}.{pripona}'
            fotky.append({'rel': rel, 'url': url, 'sha256': sha, 'pripona': pripona, 'alt': alt})
        obsah = {
            'id': pid,
            'slug': slug,
            'nazov': nazov,
            'miesto': polia['miesto'],
            'rola': polia['rola'],
            'projektant': polia['projektant'],
            'roky': polia['roky'],
            'text': polia['text'],
            'investor': polia['investor'],
            'zhotovitel': polia['zhotovitel'],
            'fotky': [{'subor': f'obsah/{f["rel"]}', 'alt': f['alt']} for f in fotky],
            'zdroj': zdroj,
            'fb_url': polia['fb_url'],
            'datum': datum,
            'poradie': poradie,
            'zverejnene': True,
        }
        vysledok.append({'slug': slug, 'id': pid, 'json': obsah, 'fotky': fotky})
    return vysledok


def bezpecna_cesta(koren, rel):
    """Cesta vnútri priečinka obsah; čokoľvek mimo = Chyba."""
    if not rel or rel.startswith(('/', '\\')) or '..' in re.split(r'[\\/]', rel):
        raise Chyba(f'Cesta {rel} nie je bezpečná.')
    cesta = os.path.join(koren, *rel.split('/'))
    skutocny_koren = os.path.realpath(koren)
    if os.path.commonpath([skutocny_koren, os.path.realpath(cesta)]) != skutocny_koren:
        raise Chyba(f'Cesta {rel} ide mimo priečinka obsah.')
    return cesta


def sha_suboru(cesta):
    h = hashlib.sha256()
    with open(cesta, 'rb') as f:
        for kus in iter(lambda: f.read(1 << 16), b''):
            h.update(kus)
    return h.hexdigest()


def nacitaj_stav(koren):
    cesta = os.path.join(koren, STAV)
    try:
        with open(cesta, encoding='utf-8') as f:
            stav = json.load(f)
        if isinstance(stav, dict) and isinstance(stav.get('realizacie'), dict):
            return {s: v for s, v in stav['realizacie'].items() if isinstance(s, str) and SLUG.match(s) and isinstance(v, dict)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    print(f'Upozornenie: {STAV} je poškodený, beriem ho ako prázdny (nič sa preto nezmaže).')
    return {}


def json_text(obsah):
    return json.dumps(obsah, ensure_ascii=False, indent=2) + '\n'


def zapis_atomovo(cesta, bajty, docasny):
    os.makedirs(os.path.dirname(cesta), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=docasny)
    with os.fdopen(fd, 'wb') as f:
        f.write(bajty)
    os.replace(tmp, cesta)


def lokalne_realizacie(koren):
    priecinok = os.path.join(koren, 'realizacie')
    if not os.path.isdir(priecinok):
        return []
    return sorted(n[:-5] for n in os.listdir(priecinok) if n.endswith('.json') and SLUG.match(n[:-5]))


def fotky_na_zmazanie(koren, slug, lokalny_json, stav_slugu):
    """Fotky starej realizácie: zo súboru (len obsah/obrazky/<slug>/…) a zo stavu."""
    rel = set()
    fotky = lokalny_json.get('fotky') if isinstance(lokalny_json, dict) else None
    for f in fotky if isinstance(fotky, list) else []:
        subor = f.get('subor') if isinstance(f, dict) else None
        predpona = f'obsah/obrazky/{slug}/'
        if isinstance(subor, str) and subor.startswith(predpona) and SUBOR_FOTKY.match(subor[len(predpona):]):
            rel.add(subor[len('obsah/'):])
    for r in (stav_slugu or {}).get('fotky', {}) or {}:
        if isinstance(r, str) and r.startswith(f'obrazky/{slug}/') and SUBOR_FOTKY.match(r.split('/')[-1]) and r.count('/') == 2:
            rel.add(r)
    return sorted(rel)


def priprav_plan(koren, polozky, stav, povol_prazdne):
    lokalne = lokalne_realizacie(koren)
    if not polozky and lokalne and not povol_prazdne:
        raise Chyba(f'API vrátilo 0 realizácií, ale lokálne ich je {len(lokalne)}. Ak je to správne, spustite s --povol-prazdne.')
    plan = {'stiahnut': [], 'zapisat': [], 'zmazat': [], 'novy_stav': {}}
    api_slugy = {p['slug'] for p in polozky}
    for p in polozky:
        slug = p['slug']
        ciele = set()
        for f in p['fotky']:
            cesta = bezpecna_cesta(koren, f['rel'])
            ciele.add(f['rel'])
            if not (os.path.isfile(cesta) and sha_suboru(cesta) == f['sha256']):
                plan['stiahnut'].append(f)
        priecinok = os.path.join(koren, 'obrazky', slug)
        if os.path.isdir(priecinok):
            for n in sorted(os.listdir(priecinok)):
                rel = f'obrazky/{slug}/{n}'
                if SUBOR_FOTKY.match(n) and rel not in ciele:
                    plan['zmazat'].append(rel)
        rel_json = f'realizacie/{slug}.json'
        cesta_json = bezpecna_cesta(koren, rel_json)
        novy = json_text(p['json']).encode('utf-8')
        try:
            with open(cesta_json, 'rb') as f:
                stary = f.read()
        except FileNotFoundError:
            stary = None
        if stary != novy:
            plan['zapisat'].append((rel_json, novy))
        plan['novy_stav'][slug] = {'id': p['id'], 'fotky': {f['rel']: f['sha256'] for f in p['fotky']}}
    for slug in lokalne:
        if slug in api_slugy:
            continue
        rel_json = f'realizacie/{slug}.json'
        try:
            with open(bezpecna_cesta(koren, rel_json), encoding='utf-8') as f:
                lokalny = json.load(f)
        except (OSError, ValueError):
            print(f'Upozornenie: {rel_json} sa nedá prečítať, nechávam ho.')
            continue
        zdroj = lokalny.get('zdroj') if isinstance(lokalny, dict) else None
        if slug in stav or zdroj != 'web':
            plan['zmazat'].append(rel_json)
            plan['zmazat'].extend(fotky_na_zmazanie(koren, slug, lokalny, stav.get(slug)))
    for rel in plan['zmazat']:
        bezpecna_cesta(koren, rel)
    plan['zmazat'] = sorted(set(plan['zmazat']))
    return plan


def vycisti_docasne(koren):
    if os.path.isdir(koren):
        for n in os.listdir(koren):
            if n.startswith(DOCASNY):
                shutil.rmtree(os.path.join(koren, n), ignore_errors=True)


def vykonaj(koren, plan, timeout):
    os.makedirs(koren, exist_ok=True)
    vycisti_docasne(koren)
    docasny = tempfile.mkdtemp(prefix=DOCASNY, dir=koren)
    try:
        stiahnute = []
        for i, f in enumerate(plan['stiahnut']):
            bajty = stiahni(f['url'], MAX_FOTKA, timeout)
            if hashlib.sha256(bajty).hexdigest() != f['sha256']:
                raise Chyba(f'Fotka {f["url"]} nemá očakávané sha256.')
            if typ_obrazka(bajty) != f['pripona']:
                raise Chyba(f'Fotka {f["url"]} nie je obrázok typu {f["pripona"]}.')
            tmp = os.path.join(docasny, f'f{i}')
            with open(tmp, 'wb') as subor:
                subor.write(bajty)
            stiahnute.append((tmp, f['rel']))
        # Všetko stiahnuté a overené: až teraz sa mení obsah/.
        for tmp, rel in stiahnute:
            cesta = bezpecna_cesta(koren, rel)
            os.makedirs(os.path.dirname(cesta), exist_ok=True)
            os.replace(tmp, cesta)
        for rel, bajty in plan['zapisat']:
            zapis_atomovo(bezpecna_cesta(koren, rel), bajty, docasny)
        for rel in plan['zmazat']:
            cesta = bezpecna_cesta(koren, rel)
            if os.path.isfile(cesta):
                os.remove(cesta)
        for rel in plan['zmazat']:
            priecinok = os.path.dirname(bezpecna_cesta(koren, rel))
            if rel.startswith('obrazky/') and os.path.isdir(priecinok) and not os.listdir(priecinok):
                os.rmdir(priecinok)
        stav = {'verzia': 1, 'realizacie': plan['novy_stav']}
        novy_stav = (json.dumps(stav, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
        cesta_stavu = os.path.join(koren, STAV)
        try:
            with open(cesta_stavu, 'rb') as f:
                stary_stav = f.read()
        except FileNotFoundError:
            stary_stav = None
        if stary_stav != novy_stav:
            zapis_atomovo(cesta_stavu, novy_stav, docasny)
    finally:
        shutil.rmtree(docasny, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Stiahne realizácie z arling-obsah do priečinka obsah/.')
    ap.add_argument('--api', default=os.environ.get('OBSAH_API', ''))
    ap.add_argument('--obsah', default='obsah')
    ap.add_argument('--sucho', action='store_true', help='len vypíše, čo by zmenil')
    ap.add_argument('--povol-prazdne', action='store_true', help='povolí 0 realizácií z API, aj keď lokálne nejaké sú')
    ap.add_argument('--timeout', type=float, default=30.0)
    a = ap.parse_args(argv)
    try:
        api, povod = over_api(a.api)
        telo = stiahni(f'{api}/realizacia', MAX_JSON, a.timeout)
        try:
            data = json.loads(telo.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise Chyba('Odpoveď API nie je platný JSON.') from None
        polozky = over_polozky(data, povod)
        koren = os.path.abspath(a.obsah)
        stav = nacitaj_stav(koren)
        plan = priprav_plan(koren, polozky, stav, a.povol_prazdne)
        for f in plan['stiahnut']:
            print(f'{"stiahol by som" if a.sucho else "sťahujem"}: {f["rel"]}')
        for rel, _ in plan['zapisat']:
            print(f'{"zapísal by som" if a.sucho else "zapisujem"}: {rel}')
        for rel in plan['zmazat']:
            print(f'{"zmazal by som" if a.sucho else "mažem"}: {rel}')
        if a.sucho:
            print(f'Suchý beh: realizácií v API {len(polozky)}, nič som nezapísal.')
            return 0
        vykonaj(koren, plan, a.timeout)
        zmeny = len(plan['stiahnut']) + len(plan['zapisat']) + len(plan['zmazat'])
        print(f'Hotovo: realizácií v API {len(polozky)}, zmien {zmeny}.')
        return 0
    except Chyba as e:
        print(f'Chyba: {e} Nič som nezapísal ako hotové.', file=sys.stderr)
        return 1
    except OSError as e:
        # Stav sa zapisuje až na konci, takže ďalší beh všetko prepočíta podľa sha256 súborov.
        print(f'Chyba: zápis na disk zlyhal ({type(e).__name__}). Stav som neuložil, ďalší beh to zopakuje.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
