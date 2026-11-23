#!/usr/bin/env python3
"""Stiahne z verejného API arling-obsah realizácie a novinky, ktoré vznikli v D1, do priečinka obsah/ webu klienta.

Použitie: python .github/obsah/stiahni.py --api https://arling-obsah.<subdoména>.workers.dev/api/bmbuilding --obsah obsah [--sucho]

Verzia 2, doplňovanie (fb-ostro, 23. 11. 2026): repozitár je zdrojom pravdy pre staré položky, D1 len pre položky, ktoré
v nej vznikli. Spravované sú len id z Facebooku (fb-<číslice>[-<číslice>]) a zo správy obsahu (tvar id D1, nezačína
web-, s- ani fb-). Položky prvého naplnenia (web-…) sťahovač nečíta, nepíše ani nemaže.
- Spravovaná položka sa zapíše celá podľa API do obsah/realizacie/<slug>.json alebo obsah/novinky/<slug>.json, fotky do
  obsah/obrazky/<slug>/NN.<jpg|webp|png> alebo obsah/obrazky/novinky/<slug>/NN.<…>. Stav je obsah/.obsah-stav.json
  (verzia 2, podľa id). firma.json a súbory iných položiek nemení.
- Mazanie len výslovné: položka zmizne, len keď je jej id v zozname "stiahnute" z API (v D1 zmazaná alebo Len koncept),
  je v stave a jej súbor nesie to isté id. To, že položka v "polozky" chýba, nikdy nič nezmaže.
- Kolízia: cieľový súbor alebo priečinok fotiek patrí inej položke = koniec s kódom 1 bez zápisu.
- Zlyhanie zatvára: chyba siete, kód iný ako 200, zlý JSON alebo zlý tvar = koniec s kódom 1 bez zápisu. Novinka s
  kódom 404 znamená, že klient typ novinka nemá: novinky sa vtedy nemenia. Len štandardná knižnica Pythonu 3.

Príznak vizualizácie (linka bmb-dokoncenie): fotka z API smie mať "druh" (len "vizualizacia") a "autor" (text do 300
znakov, len spolu s druh); do súboru ide ako { subor, alt, druh, autor }. Iná hodnota = zlý tvar.
Kľúče, ktoré API vôbec nepozná (rok_prispevku, titulok, realizacia…), sa pri prepise vlastného súboru zachovajú
(ZACHOVAJ_NEZNAME_KLUCE); API ich nevie zmeniť ani zmazať, menia sa len v repozitári.
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
# Spravované id podľa products/arling-obsah/src/spolocne.mjs: RE_ID_FB (položky z Facebooku, smie ich vytvoriť len
# stroj), RE_ID (tvar id v D1) a RE_ID_NASTROJA (predpony nástrojov, správa ich klientovi nedá).
ID_FB = re.compile(r'^fb-[0-9]{1,30}(?:-[0-9]{1,30})?$')
ID_D1 = re.compile(r'^[a-z0-9-]{8,64}$')
ID_NASTROJA = re.compile(r'^(?:web|s|fb)-')
# Id, ktoré dáva správa obsahu (noveId v products/arling-obsah/ui/app.js, UUID v4). Staré súbory repozitára majú id
# rovné slugu, takže súbor bez záznamu v stave sa podľa id prevezme len pri fb-<číslice> alebo UUID.
ID_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
DATUM = re.compile(r'^\d{4}-\d{2}-\d{2}$')
CESTA_FOTKY = re.compile(r'^/f/[a-z0-9-]{2,40}/[A-Za-z0-9_-]{1,100}\.(jpg|webp|png)$')
SUBOR_FOTKY = re.compile(r'^\d{2}\.(jpg|webp|png)$')
ZDROJE = {'web', 'fb', 'sprava'}
TEXTY = ('miesto', 'rola', 'projektant', 'roky', 'text', 'investor', 'zhotovitel', 'fb_url')
MAX_JSON = 10 * 1024 * 1024
MAX_FOTKA = 6291456
MAX_FOTIEK = 60
MAX_STIAHNUTE = 10000
STAV = '.obsah-stav.json'
VERZIA_STAVU = 2
DOCASNY = '.obsah-tmp-'
DRUHY_FOTKY = {'vizualizacia'}
MAX_AUTOR = 300
# Zmena b) návrhu bmb-fb-samo: pri prepise vlastného súboru zachovať kľúče, ktoré API nepozná. False = súbor presne podľa API.
ZACHOVAJ_NEZNAME_KLUCE = True
# Typy: priečinok JSON, priečinok fotiek, či je typ povinný (404 = chyba), slugy, ktoré typ mať nesmie (realizácia
# „novinky“ by mala fotky v obsah/obrazky/novinky/, kde sú fotky noviniek), a kľúče z API, ktoré sa zo starého súboru
# nikdy nepreberajú.
TYPY = {
    'realizacia': {
        'json': 'realizacie', 'fotky': 'obrazky', 'povinny': True, 'zakazane_slugy': frozenset({'novinky'}),
        'kluce_api': frozenset({'id', 'slug', 'typ', 'nazov', 'fotky', 'zdroj', 'datum', 'poradie', 'zverejnene',
                                'upravene', *TEXTY}),
    },
    'novinka': {
        'json': 'novinky', 'fotky': 'obrazky/novinky', 'povinny': False, 'zakazane_slugy': frozenset(),
        'kluce_api': frozenset({'id', 'slug', 'typ', 'datum', 'nadpis', 'text', 'fotky', 'zdroj', 'poradie',
                                'zverejnene', 'upravene'}),
    },
}


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


def stiahni(url, limit, timeout, povol_404=False):
    """GET s hlavičkou User-Agent (worker bez nej vráti 403). Vráti bajty; pri povol_404 a kóde 404 None."""
    poziadavka = urllib.request.Request(url, headers={'User-Agent': 'arling-obsah-stiahni/2', 'Accept': '*/*'})
    try:
        with OTVARAC.open(poziadavka, timeout=timeout) as odpoved:
            if odpoved.status != 200:
                raise Chyba(f'Server vrátil kód {odpoved.status} pre {url}.')
            telo = odpoved.read(limit + 1)
    except urllib.error.HTTPError as e:
        if povol_404 and e.code == 404:
            return None
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


def je_spravovane(pid):
    """Id z Facebooku (fb-<číslice>[-<číslice>]) alebo zo správy obsahu (tvar id D1 bez predpony web-, s- a fb-)."""
    return isinstance(pid, str) and (bool(ID_FB.match(pid)) or (bool(ID_D1.match(pid)) and not ID_NASTROJA.match(pid)))


def priecinok_fotiek(typ, slug):
    return f'{TYPY[typ]["fotky"]}/{slug}'


def je_fotka_polozky(typ, slug, rel):
    """rel je obrazky/<slug>/NN.<…> (pri novinke obrazky/novinky/<slug>/NN.<…>), nič hlbšie ani inde."""
    predpona = priecinok_fotiek(typ, slug) + '/'
    return isinstance(rel, str) and rel.startswith(predpona) and SUBOR_FOTKY.match(rel[len(predpona):]) is not None


def text_alebo_none(hodnota, kde):
    if hodnota is None:
        return None
    if not isinstance(hodnota, str):
        raise Chyba(f'{kde} musí byť text.')
    return hodnota


def priznak_fotky(f, kde):
    """Príznak vizualizácie z API: {} alebo {'druh': 'vizualizacia'[, 'autor': text]}. Iná hodnota = Chyba (zlý tvar)."""
    druh = f.get('druh')
    autor = f.get('autor')
    if druh is None:
        if autor is not None:
            raise Chyba(f'{kde}: autor fotky je len spolu s druh.')
        return {}
    if not isinstance(druh, str) or druh not in DRUHY_FOTKY:
        raise Chyba(f'{kde}: neznámy druh fotky (povolené len vizualizacia).')
    vysledok = {'druh': druh}
    if autor is not None:
        if not isinstance(autor, str):
            raise Chyba(f'{kde}: autor fotky musí byť text.')
        if len(autor) > MAX_AUTOR:
            raise Chyba(f'{kde}: autor fotky je dlhší ako {MAX_AUTOR} znakov.')
        if autor.strip():
            vysledok['autor'] = autor
    return vysledok


def over_fotky(p, kde, typ, slug, povod):
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
        rel = f'{priecinok_fotiek(typ, slug)}/{j:02d}.{pripona}'
        zaznam = {'subor': f'obsah/{rel}', 'alt': alt}
        zaznam.update(priznak_fotky(f, kdef))
        fotky.append({'rel': rel, 'url': url, 'sha256': sha, 'pripona': pripona, 'zaznam': zaznam})
    return fotky


def obsah_polozky(p, kde, typ, slug, pid, fotky):
    """Obsah súboru v tvare, ktorý číta build.py (nacitaj_realizacie, nacitaj_novinky)."""
    datum = text_alebo_none(p.get('datum'), f'{kde}.datum')
    if datum is not None and not DATUM.match(datum):
        raise Chyba(f'{kde}: dátum nemá tvar RRRR-MM-DD.')
    poradie = p.get('poradie', 0)
    if not isinstance(poradie, int) or isinstance(poradie, bool):
        raise Chyba(f'{kde}: poradie musí byť celé číslo.')
    zdroj = p.get('zdroj', 'sprava')
    if zdroj not in ZDROJE:
        raise Chyba(f'{kde}: neznámy zdroj.')
    zaznamy = [f['zaznam'] for f in fotky]
    if typ == 'novinka':
        nadpis = p.get('nadpis')
        if not isinstance(nadpis, str) or not nadpis.strip():
            raise Chyba(f'{kde}: chýba nadpis.')
        if datum is None:
            raise Chyba(f'{kde}: novinka musí mať dátum RRRR-MM-DD.')
        return {'id': pid, 'slug': slug, 'datum': datum, 'nadpis': nadpis, 'text': text_alebo_none(p.get('text'), f'{kde}.text'),
                'fotky': zaznamy, 'zdroj': zdroj, 'zverejnene': True}
    nazov = p.get('nazov')
    if not isinstance(nazov, str) or not nazov.strip():
        raise Chyba(f'{kde}: chýba názov.')
    polia = {k: text_alebo_none(p.get(k), f'{kde}.{k}') for k in TEXTY}
    if polia['fb_url'] is not None and not polia['fb_url'].startswith('https://'):
        raise Chyba(f'{kde}: fb_url musí začínať https://.')
    return {
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
        'fotky': zaznamy,
        'zdroj': zdroj,
        'fb_url': polia['fb_url'],
        'datum': datum,
        'poradie': poradie,
        'zverejnene': True,
    }


def over_stiahnute(data, typ):
    """Zoznam id položiek, ktoré D1 stiahla z webu (zmazané alebo Len koncept). Bez kľúča (starý worker) None."""
    s = data.get('stiahnute')
    if s is None:
        return None
    if not isinstance(s, list) or len(s) > MAX_STIAHNUTE:
        raise Chyba(f'Odpoveď API pre {typ}: stiahnute musí byť zoznam s najviac {MAX_STIAHNUTE} id.')
    for i, pid in enumerate(s):
        if not isinstance(pid, str) or not ID.match(pid):
            raise Chyba(f'Odpoveď API pre {typ}: stiahnute[{i}] nie je platné id.')
    return set(s)


def over_odpoved(data, typ, povod):
    """Overí tvar odpovede API. Vráti (spravované položky v tvare pre zápis, počet ostatných, stiahnute alebo None).
    Ostatné položky (prvé naplnenie web-…) sa okrem id neoverujú ani nepoužijú. Zlý tvar = Chyba."""
    if not isinstance(data, dict) or not isinstance(data.get('polozky'), list):
        raise Chyba(f'Odpoveď API pre {typ} nemá tvar {{ "polozky": [ … ] }}.')
    if data.get('typ') not in (None, typ):
        raise Chyba(f'Odpoveď API nie je pre typ {typ}.')
    vysledok, slugy, idcka, ostatne = [], set(), set(), 0
    for i, p in enumerate(data['polozky']):
        kde = f'{typ}: polozky[{i}]'
        if not isinstance(p, dict):
            raise Chyba(f'{kde} nie je objekt.')
        pid = p.get('id')
        if not isinstance(pid, str) or not ID.match(pid):
            raise Chyba(f'{kde}: neplatné id.')
        if pid in idcka:
            raise Chyba(f'{kde}: id je v odpovedi dvakrát.')
        idcka.add(pid)
        if not je_spravovane(pid):
            ostatne += 1
            continue
        slug = p.get('slug')
        if not isinstance(slug, str) or not SLUG.match(slug):
            raise Chyba(f'{kde}: neplatný slug (povolené a až z, 0 až 9 a pomlčka, najviac 80 znakov).')
        if slug in slugy:
            raise Chyba(f'{kde}: slug je v odpovedi dvakrát.')
        slugy.add(slug)
        fotky = over_fotky(p, kde, typ, slug, povod)
        obsah = obsah_polozky(p, kde, typ, slug, pid, fotky)
        vysledok.append({'typ': typ, 'slug': slug, 'id': pid, 'json': obsah, 'fotky': fotky})
    return vysledok, ostatne, over_stiahnute(data, typ)


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


def citaj_subor(cesta):
    """(bajty, objekt JSON) existujúceho súboru; nečitateľný JSON = (bajty, None); chýbajúci súbor = (None, None)."""
    try:
        with open(cesta, 'rb') as f:
            bajty = f.read()
    except FileNotFoundError:
        return None, None
    try:
        return bajty, json.loads(bajty.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return bajty, None


def zaznam_stavu(typ, slug, fotky):
    """Platný záznam stavu {typ, slug, fotky} alebo None; fotky len z vlastného priečinka položky."""
    if typ not in TYPY or not isinstance(slug, str) or not SLUG.match(slug):
        return None
    fotky = fotky if isinstance(fotky, dict) else {}
    return {'typ': typ, 'slug': slug,
            'fotky': {r: s for r, s in fotky.items() if je_fotka_polozky(typ, slug, r) and isinstance(s, str) and SHA.match(s)}}


def nacitaj_stav(koren):
    """Stav verzie 2 {"verzia": 2, "polozky": {id: {typ, slug, fotky}}}. Verzia 1 ({"realizacie": {slug: {id, fotky}}})
    sa prevedie: spravované id ostanú, položky prvého naplnenia (web-…) vypadnú a ich súbory ostanú repozitáru.
    Vráti (stav, veta o prevode alebo None). Poškodený stav = prázdny (nič sa preto nezmaže)."""
    try:
        with open(os.path.join(koren, STAV), encoding='utf-8') as f:
            stav = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError):
        stav = None
    if isinstance(stav, dict) and stav.get('verzia') == VERZIA_STAVU and isinstance(stav.get('polozky'), dict):
        vysledok = {}
        for pid, z in stav['polozky'].items():
            zaznam = zaznam_stavu(z.get('typ'), z.get('slug'), z.get('fotky')) if isinstance(z, dict) else None
            if je_spravovane(pid) and zaznam:
                vysledok[pid] = zaznam
        return vysledok, None
    if isinstance(stav, dict) and stav.get('verzia') in (None, 1) and isinstance(stav.get('realizacie'), dict):
        vysledok, odovzdane = {}, 0
        for slug, v in stav['realizacie'].items():
            pid = v.get('id') if isinstance(v, dict) else None
            zaznam = zaznam_stavu('realizacia', slug, v.get('fotky')) if je_spravovane(pid) else None
            if zaznam:
                vysledok[pid] = zaznam
            else:
                odovzdane += 1
        return vysledok, (f'Stav sťahovača verzie 1 prevádzam na verziu 2: spravovaných položiek {len(vysledok)}, '
                          f'položiek prvého naplnenia a iných {odovzdane} odovzdávam repozitáru (ich súbory ostávajú).')
    print(f'Upozornenie: {STAV} je poškodený, beriem ho ako prázdny (nič sa preto nezmaže).')
    return {}, None


def json_text(obsah):
    return json.dumps(obsah, ensure_ascii=False, indent=2) + '\n'


def stav_bajty(novy_stav):
    stav = {'verzia': VERZIA_STAVU, 'polozky': novy_stav}
    return (json.dumps(stav, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


def zapis_atomovo(cesta, bajty, docasny):
    os.makedirs(os.path.dirname(cesta), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=docasny)
    with os.fdopen(fd, 'wb') as f:
        f.write(bajty)
    os.replace(tmp, cesta)


def fotky_zo_suboru(typ, slug, d):
    """Fotky, na ktoré odkazuje súbor položky, len z jej vlastného priečinka (obsah/obrazky/<slug>/NN.<…>)."""
    rel = set()
    fotky = d.get('fotky') if isinstance(d, dict) else None
    for f in fotky if isinstance(fotky, list) else []:
        subor = f.get('subor') if isinstance(f, dict) else None
        if isinstance(subor, str) and subor.startswith('obsah/') and je_fotka_polozky(typ, slug, subor[len('obsah/'):]):
            rel.add(subor[len('obsah/'):])
    return rel


def zmaz_polozku(koren, pid, zaznam, plan, dovod):
    """Naplánuje zmazanie súborov položky, ktorú vytvoril sťahovač. JSON len keď nesie to isté id, potom fotky zo stavu
    a z JSON, len vo vlastnom priečinku. Bez JSON len fotky zo stavu so zhodným sha256. Iné súbory v priečinku ostanú."""
    typ, slug = zaznam['typ'], zaznam['slug']
    rel_json = f'{TYPY[typ]["json"]}/{slug}.json'
    bajty, d = citaj_subor(bezpecna_cesta(koren, rel_json))
    if bajty is not None:
        if not (isinstance(d, dict) and d.get('id') == pid):
            plan['upozornenia'].append(f'{rel_json} nenesie id {pid}, nechávam ho aj jeho fotky ({dovod}).')
            return
        plan['zmazat'].add(rel_json)
        rel = set(zaznam['fotky']) | fotky_zo_suboru(typ, slug, d)
        plan['zmazat'].update(r for r in rel if os.path.isfile(bezpecna_cesta(koren, r)))
        return
    for r, sha in zaznam['fotky'].items():
        cesta = bezpecna_cesta(koren, r)
        if os.path.isfile(cesta) and sha_suboru(cesta) == sha:
            plan['zmazat'].add(r)


def priprav_plan(koren, odpovede, stav):
    """odpovede: {typ: {'polozky': [spravované], 'stiahnute': set alebo None}} pre typy, ktoré API vrátilo.
    Vráti plán (stiahnuť, zapísať, zmazať, nový stav, upozornenia); nič nemení. Kolízia = Chyba."""
    plan = {'stiahnut': [], 'zapisat': [], 'zmazat': set(), 'upozornenia': []}
    novy_stav = dict(stav)
    api = {}
    for typ, o in odpovede.items():
        for p in o['polozky']:
            if p['id'] in api:
                raise Chyba(f'Id {p["id"]} vracia API pri dvoch typoch.')
            api[p['id']] = p
            z = stav.get(p['id'])
            if z and z['typ'] != typ:
                raise Chyba(f'Položka {p["id"]} je v stave ako {z["typ"]}, API ju vracia ako {typ}. Treba ju opraviť ručne.')
    # 1. Mazanie: id zo zoznamu stiahnute, ktoré vytvoril sťahovač, a staré miesto položky so zmeneným slugom.
    for typ, o in odpovede.items():
        if o['stiahnute'] is None:
            plan['upozornenia'].append(f'API {typ} nemá zoznam stiahnute (starý worker), nič preto nemažem.')
            continue
        for pid in sorted(o['stiahnute']):
            if pid in api:
                plan['upozornenia'].append(f'{pid} je v API medzi zverejnenými aj v zozname stiahnute, nechávam ho.')
                continue
            z = stav.get(pid)
            if not je_spravovane(pid) or not z or z['typ'] != typ:
                continue
            zmaz_polozku(koren, pid, z, plan, 'stiahnutá v D1')
            novy_stav.pop(pid, None)
    for pid, p in api.items():
        z = stav.get(pid)
        if z and z['slug'] != p['slug']:
            zmaz_polozku(koren, pid, z, plan, f'nový slug {p["slug"]}')
    # 2. Zápis spravovaných položiek. Súbor alebo fotka, ktorá sa v tomto behu maže, sa berie ako voľná.
    ciele = set()
    for pid, p in sorted(api.items()):
        typ, slug = p['typ'], p['slug']
        z = stav.get(pid)
        if slug in TYPY[typ]['zakazane_slugy']:
            raise Chyba(f'{typ} {pid}: slug {slug} je vyhradený (obsah/{priecinok_fotiek(typ, slug)}/ patrí inému typu). '
                        'Treba zmeniť názov v správe obsahu.')
        rel_json = f'{TYPY[typ]["json"]}/{slug}.json'
        bajty, d = citaj_subor(bezpecna_cesta(koren, rel_json))
        if bajty is None or rel_json in plan['zmazat']:
            vlastny, d = False, None
        elif isinstance(d, dict) and d.get('id') == pid and ((z and z['slug'] == slug) or ID_FB.match(pid) or ID_UUID.match(pid)):
            # Vlastný súbor podľa stavu, alebo prevzatý podľa id po strate stavu či prerušenom behu.
            vlastny = True
        elif not isinstance(d, dict) and z and z['slug'] == slug:
            plan['upozornenia'].append(f'{rel_json} sa nedá prečítať, prepíšem ho podľa API bez ďalších kľúčov.')
            vlastny, d = True, None
        else:
            ine = d.get('id') if isinstance(d, dict) else None
            raise Chyba(f'Kolízia: obsah/{rel_json} patrí inej položke (id {ine or "chýba alebo nečitateľné"}), {typ} {pid} '
                        'z D1 by ho prepísala. Treba zmeniť slug v správe obsahu alebo súbor v repozitári.')
        fotky = {f['rel']: f['sha256'] for f in p['fotky']}
        priecinok = priecinok_fotiek(typ, slug)
        cesta_priecinka = bezpecna_cesta(koren, priecinok)
        if os.path.exists(cesta_priecinka) and not os.path.isdir(cesta_priecinka):
            raise Chyba(f'Kolízia: obsah/{priecinok} nie je priečinok.')
        if vlastny:
            # Fotku, ktorú položka v D1 už nemá, zmaže len keď ju sťahovač vytvoril (stav alebo jej súbor JSON).
            zname = (set(z['fotky']) if z and z['slug'] == slug else set()) | fotky_zo_suboru(typ, slug, d)
            plan['zmazat'].update(r for r in zname - set(fotky) if os.path.isfile(bezpecna_cesta(koren, r)))
        elif os.path.isdir(cesta_priecinka):
            # Nová položka: v priečinku smú byť len jej fotky zo skoršieho prerušeného behu (zhodné sha256) a súbory,
            # ktoré sa v tomto behu mažú.
            for n in sorted(os.listdir(cesta_priecinka)):
                rel = f'{priecinok}/{n}'
                cesta = os.path.join(cesta_priecinka, n)
                if rel in plan['zmazat']:
                    continue
                if not (os.path.isfile(cesta) and rel in fotky and sha_suboru(cesta) == fotky[rel]):
                    raise Chyba(f'Kolízia: obsah/{priecinok}/ už má súbor {n}, ktorý nepatrí {typ} {pid} z D1. '
                                'Treba zmeniť slug v správe obsahu alebo priečinok v repozitári.')
        for f in p['fotky']:
            cesta = bezpecna_cesta(koren, f['rel'])
            if not (os.path.isfile(cesta) and sha_suboru(cesta) == f['sha256']):
                plan['stiahnut'].append(f)
            ciele.add(f['rel'])
        obsah = p['json']
        if ZACHOVAJ_NEZNAME_KLUCE and vlastny and isinstance(d, dict):
            obsah = {**obsah, **{k: v for k, v in d.items() if k not in TYPY[typ]['kluce_api']}}
        novy = json_text(obsah).encode('utf-8')
        if bajty != novy:
            plan['zapisat'].append((rel_json, novy, not vlastny))
        ciele.add(rel_json)
        novy_stav[pid] = {'typ': typ, 'slug': slug, 'fotky': fotky}
    # Cieľ tohto behu sa nikdy nemaže (súbor alebo fotku inej položky prevezme nová).
    plan['zmazat'] = sorted(plan['zmazat'] - ciele)
    for rel in plan['zmazat']:
        bezpecna_cesta(koren, rel)
    plan['stav'] = stav_bajty(novy_stav)
    stary_stav, _ = citaj_subor(os.path.join(koren, STAV))
    plan['stav_zmena'] = stary_stav != plan['stav']
    return plan


def pocty(plan):
    return {
        'pridane': sum(1 for _, _, novy in plan['zapisat'] if novy),
        'zmenene': sum(1 for _, _, novy in plan['zapisat'] if not novy),
        'zmazane': len(plan['zmazat']),
        'zmazane_json': sum(1 for r in plan['zmazat'] if r.endswith('.json')),
        'stiahnute': len(plan['stiahnut']),
    }


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
        hotove = []
        for i, f in enumerate(plan['stiahnut']):
            bajty = stiahni(f['url'], MAX_FOTKA, timeout)
            if hashlib.sha256(bajty).hexdigest() != f['sha256']:
                raise Chyba(f'Fotka {f["url"]} nemá očakávané sha256.')
            if typ_obrazka(bajty) != f['pripona']:
                raise Chyba(f'Fotka {f["url"]} nie je obrázok typu {f["pripona"]}.')
            tmp = os.path.join(docasny, f'f{i}')
            with open(tmp, 'wb') as subor:
                subor.write(bajty)
            hotove.append((tmp, f['rel']))
        # Všetko stiahnuté a overené: až teraz sa mení obsah/.
        for tmp, rel in hotove:
            cesta = bezpecna_cesta(koren, rel)
            os.makedirs(os.path.dirname(cesta), exist_ok=True)
            os.replace(tmp, cesta)
        for rel, bajty, _ in plan['zapisat']:
            zapis_atomovo(bezpecna_cesta(koren, rel), bajty, docasny)
        for rel in plan['zmazat']:
            cesta = bezpecna_cesta(koren, rel)
            if os.path.isfile(cesta):
                os.remove(cesta)
        for rel in plan['zmazat']:
            priecinok = os.path.dirname(bezpecna_cesta(koren, rel))
            if rel.startswith('obrazky/') and os.path.isdir(priecinok) and not os.listdir(priecinok):
                os.rmdir(priecinok)
        if plan['stav_zmena']:
            zapis_atomovo(os.path.join(koren, STAV), plan['stav'], docasny)
    finally:
        shutil.rmtree(docasny, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Stiahne položky z Facebooku a zo správy (arling-obsah) do priečinka obsah/.')
    ap.add_argument('--api', default=os.environ.get('OBSAH_API', ''))
    ap.add_argument('--obsah', default='obsah')
    ap.add_argument('--sucho', action='store_true', help='len vypíše, čo by zmenil')
    ap.add_argument('--povol-prazdne', action='store_true',
                    help='od verzie 2 bez účinku (podľa chýbania v API sa nič nemaže), ostáva kvôli workflowu Obsah')
    ap.add_argument('--timeout', type=float, default=30.0)
    a = ap.parse_args(argv)
    try:
        api, povod = over_api(a.api)
        odpovede = {}
        for typ, definicia in TYPY.items():
            telo = stiahni(f'{api}/{typ}', MAX_JSON, a.timeout, povol_404=not definicia['povinny'])
            if telo is None:
                print(f'API {typ}: typ klient nemá (404), obsah/{definicia["json"]}/ nechávam bez zmeny.')
                continue
            try:
                data = json.loads(telo.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                raise Chyba(f'Odpoveď API pre {typ} nie je platný JSON.') from None
            polozky, ostatne, stiahnute = over_odpoved(data, typ, povod)
            odpovede[typ] = {'polozky': polozky, 'stiahnute': stiahnute}
            print(f'API {typ}: položiek {len(polozky) + ostatne}, z Facebooku a zo správy {len(polozky)}, '
                  f'prvé naplnenie a iné (nechávam repozitáru) {ostatne}, zoznam stiahnute '
                  f'{"chýba (starý worker)" if stiahnute is None else f"má {len(stiahnute)} id"}.')
        if a.povol_prazdne:
            print('Poznámka: --povol-prazdne od verzie 2 nič nemení, podľa chýbania v API sa nemaže.')
        koren = os.path.abspath(a.obsah)
        stav, prevod = nacitaj_stav(koren)
        if prevod:
            print(prevod)
        plan = priprav_plan(koren, odpovede, stav)
        for veta in plan['upozornenia']:
            print(f'Upozornenie: {veta}')
        for f in plan['stiahnut']:
            print(f'{"stiahol by som" if a.sucho else "sťahujem"}: {f["rel"]}')
        for rel, _, novy in plan['zapisat']:
            print(f'{"zapísal by som" if a.sucho else "zapisujem"}: {rel} ({"nový" if novy else "zmena"})')
        for rel in plan['zmazat']:
            print(f'{"zmazal by som" if a.sucho else "mažem"}: {rel}')
        c = pocty(plan)
        suhrn = (f'pridaní {c["pridane"]}, zmien {c["zmenene"]}, mazaní {c["zmazane"]} '
                 f'(JSON {c["zmazane_json"]}, fotky {c["zmazane"] - c["zmazane_json"]})')
        if a.sucho:
            print(f'Suchý beh: {suhrn}, fotiek na stiahnutie {c["stiahnute"]}, '
                  f'stav {"by som prepísal" if plan["stav_zmena"] else "bez zmeny"}. Nič som nezapísal.')
            return 0
        vykonaj(koren, plan, a.timeout)
        print(f'Hotovo: {suhrn}, stiahnutých fotiek {c["stiahnute"]}, stav {"prepísaný" if plan["stav_zmena"] else "bez zmeny"}.')
        return 0
    except Chyba as e:
        print(f'Chyba: {e} Nič som nezapísal ako hotové.', file=sys.stderr)
        return 1
    except OSError as e:
        # Stav sa zapisuje až na konci, takže ďalší beh všetko prepočíta podľa sha256 súborov a id v nich.
        print(f'Chyba: čítanie alebo zápis na disk zlyhal ({type(e).__name__}). Stav som neuložil, ďalší beh to zopakuje.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
