#!/usr/bin/env python3
"""Hotové webové verzie fotiek BM Building do fotky-web/ (linka bmb-fotky, agent Kódovanie, 22. 11. 2026).

  python -B -X utf8 nastroje/predlohy_web.py [--limit-min 8] [--procesy 12] [--len slug/01.jpg,...] [--vycisti]

Postup pre každú fotku, ktorú web naozaj použije (plán z build.Web s rozmermi predlôh, nič sa nehádže):
  1. Predloha: PNG z D:/ARLing-media/klienti/bmbuilding/web-predlohy podľa predlohy.json (zväčšená alebo zmenšená
     z rozmazanej verzie v obsah/, sRGB, bez metadát), ak sedí sha256 zdroja v obsah/ aj sha256 súboru predlohy;
     inak zdroj z obsah/ (build.otvor_rgb: exif_transpose, sRGB, najviac build.MAX_VSTUP).
  2. Kvalita pre každý formát (AVIF, WebP): najmenšia, pri ktorej MS-SSIM jasu zakódovanej fotky voči predlohe
     zmenšenej na tú istú šírku drží prah PRAH (binárne hľadanie na jednej referenčnej šírke REF_SIRKA, potom
     kontrola na najmenšej šírke; ak tam prah nedrží, kvalita sa zvýši). Metóda a prah: ops/weby-na-mieru/klienti/
     bmbuilding/fotky-kvalita/KODOVANIE.md; titulné fotky PRAH_TITULNE a AI predloha len nad 1,25 x zdroj:
     ops/weby-na-mieru/klienti/bmbuilding/presnost/oprava-fotky.md.
  3. Všetky potrebné súbory (šírky, výrezy 45 a 54, AVIF aj WebP) tou kvalitou cez build.kodovany (rovnaký kóder
     ako build.py), uložené do fotky-web/<sha256 zdroja[:16]>/<šírka>[-<orez>].<formát>, záznam do manifest.json.

Idempotentné: fotka s rovnakou predlohou, verziou a prahom a so všetkými súbormi na disku sa preskočí; chýbajúce
súbory sa dorobia s uloženou kvalitou. Manifest sa zapisuje po každej fotke (atomicky). --limit-min: po tomto čase
sa nové fotky nespúšťajú, rozbehnuté sa dokončia (dávky pod 9 min). Procesy so zníženou prioritou.
Potrebuje numpy a scipy (len PC, nie web ani CI). Spúšťať pod zámkom PC (hromadné kódovanie).
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import redirect_stderr
from pathlib import Path

KOREN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KOREN))
import build  # noqa: E402

PREDLOHY = Path('D:/ARLing-media/klienti/bmbuilding/web-predlohy')
VERZIA = 'fw-1|ms-ssim-jas|' + build.VERZIA_OBR
# prah MS-SSIM jasu (0 až 1) a rozsah hľadanej kvality; kalibrácia v KODOVANIE.md
PRAH = {'avif': 0.995, 'webp': 0.995}
# titulné fotky (hrdina úvodu, fotka karty v páse, mriežke a na titulke detailu, ilustračné fotky noviniek, fotky
# sekcií Postup a O firme) majú prísnejší prah (linka bmb-presnost, kalibrácia v
# ops/weby-na-mieru/klienti/bmbuilding/presnost/oprava-fotky.md časť 2)
PRAH_TITULNE = {'avif': 0.997, 'webp': 0.997}
ROZSAH = {'avif': (45, 90), 'webp': (60, 92)}
REF_SIRKA = 1440
# predloha z AI zväčšenia (Upscayl) slúži len na šírky nad ZDROJ_LANCZOS_MAX x šírka zdroja; šírky a výrezy do
# tejto šírky sa robia zo zdroja v obsah/ Lanczosom (aj mierne zväčšenie), lebo AI textúru zmäkčuje
# (presnost/meranie/fotky-pred.md časť 10: medián ostrosti 0,64 zdroja proti 0,97 bez AI). 1,25 je ten istý prah
# ako pri výbere predlohy (fotky-kvalita/zvacsenie.md: pod 1,25 x zdroj len Lanczos) a v pravidle mäkkej fotky (A).
# Všetky modely Upscaylu z PC (C:/Program Files/Upscayl/resources/models), nielen high-fidelity: 1. kolo pripomienok
# (kolo-1/fotky/UPSCAYL.md) vybralo pre slider ultrasharp-4x a upscayl-standard-4x; predloha z hociktorého modelu
# je zväčšená AI a pravidlo „zdroj do 1,25 x (titulné 1,6 x) šírky zdroja“ platí pre ňu rovnako (agent ÚVOD v3).
MODELY_AI = ('high-fidelity-4x', 'ultrasharp-4x', 'remacri-4x', 'ultramix-balanced-4x', 'upscayl-standard-4x',
             'upscayl-lite-4x', 'digital-art-4x')
ZDROJ_LANCZOS_MAX = 1.25
ZDROJ_DO_SIRKY = 'zdroj-do-1.25-sirky-2'
# titulné fotky (PRAH_TITULNE) berú zdroj Lanczosom až do 1,6 x šírky zdroja: karta novinky Vianočný pozdrav
# (Rosnička 14, výrez 734 px) brala pri 1920 px a hustote 2 súbor 960 px z AI s ostrosťou 0,39 zdroja (brána
# bmb-presnost pokus 1, nález 4, návrh do 1,5 x); 1,6 pokryje aj súbor 720 px karty Kalvárie 01 (zdroj 450 px), ktorý
# prehliadač berie na 528 px obrazovky. Nad 1,25 x je fotka hraničná alebo mäkká podľa pravidla A, AI tam nepomáha
ZDROJ_LANCZOS_MAX_TITULNE = 1.6
ZDROJ_DO_SIRKY_TITULNE = 'zdroj-do-1.6-sirky-titulne-1'


# ---------------------------------------------------------------- miera zhody (numpy, scipy)

def jas(im):
    """Jas (luma BT.709 z hodnôt sRGB) ako float64 matica."""
    import numpy as np
    a = np.asarray(im.convert('RGB'), dtype=np.float64)
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _ssim_casti(x, y):
    from scipy.ndimage import gaussian_filter
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = lambda z: gaussian_filter(z, 1.5, truncate=3.5)  # noqa: E731 (okno 11 x 11 ako Wang 2004)
    mx, my = g(x), g(y)
    sxx, syy, sxy = g(x * x) - mx * mx, g(y * y) - my * my, g(x * y) - mx * my
    cs = (2 * sxy + c2) / (sxx + syy + c2)
    l = (2 * mx * my + c1) / (mx * mx + my * my + c1)  # noqa: E741
    return l, cs


def _polovica(z):
    h, w = z.shape[0] // 2 * 2, z.shape[1] // 2 * 2
    z = z[:h, :w]
    return (z[0::2, 0::2] + z[1::2, 0::2] + z[0::2, 1::2] + z[1::2, 1::2]) / 4


VAHY_MS = (0.0448, 0.2856, 0.3001, 0.2363, 0.1333)


def ms_ssim(a, b):
    """MS-SSIM (Wang, Simoncelli, Bovik 2003) jasu dvoch obrázkov rovnakého rozmeru; pri malom obrázku menej mierok."""
    import numpy as np
    x, y = jas(a), jas(b)
    mierky = 5
    while mierky > 1 and min(x.shape) / 2 ** (mierky - 1) < 16:
        mierky -= 1
    vahy = np.array(VAHY_MS[:mierky])
    vahy = vahy / vahy.sum()
    hodnoty = []
    for i in range(mierky):
        l, cs = _ssim_casti(x, y)  # noqa: E741
        hodnoty.append(float((l * cs).mean()) if i == mierky - 1 else float(cs.mean()))
        if i < mierky - 1:
            x, y = _polovica(x), _polovica(y)
    return float(np.prod([max(v, 1e-9) ** w for v, w in zip(hodnoty, vahy)]))


def ssim(a, b):
    """Jednoduché SSIM jasu (jedna mierka), len pre záznam a kalibráciu."""
    l, cs = _ssim_casti(jas(a), jas(b))  # noqa: E741
    return float((l * cs).mean())


def dekoduj(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.convert('RGB')


def zhoda(ref, fmt, q):
    data = build.kodovany(ref, fmt, q)
    return ms_ssim(ref, dekoduj(data)), len(data)


def najmensia_kvalita(ref, fmt, prah, rozsah=None):
    """Najmenšia celá kvalita z rozsahu, pri ktorej MS-SSIM jasu drží prah (binárne hľadanie; zhoda s kvalitou
    rastie takmer monotónne). Vráti (kvalita, zhoda, bajty, počet kódovaní); keď prah nedrží ani horná hranica, hornú."""
    lo, hi = rozsah or ROZSAH[fmt]
    pokusy = {}

    def skus(q):
        if q not in pokusy:
            pokusy[q] = zhoda(ref, fmt, q)
        return pokusy[q]
    if skus(hi)[0] < prah:
        return hi, pokusy[hi][0], pokusy[hi][1], len(pokusy)
    while lo < hi:
        m = (lo + hi) // 2
        if skus(m)[0] >= prah:
            hi = m
        else:
            lo = m + 1
    z, b = skus(hi)
    return hi, z, b, len(pokusy)


# ---------------------------------------------------------------- predlohy a plán

def sha_suboru(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for kus in iter(lambda: f.read(1 << 20), b''):
            h.update(kus)
    return h.hexdigest()


def nacitaj_predlohy(priecinok):
    """predlohy.json agenta Zväčšenie: {sha256 zdroja: záznam} len s existujúcim súborom a rozumným rozmerom."""
    m = Path(priecinok) / 'predlohy.json'
    if not m.is_file():
        return {}
    d = json.loads(m.read_text(encoding='utf-8'))
    out = {}
    for z in d.get('predlohy') or []:
        try:
            sha, w, h, subor = z['sha256_zdroja'], int(z['w']), int(z['h']), Path(z['subor'])
        except (KeyError, TypeError, ValueError):
            continue
        if subor.is_file() and 0 < w <= 4000 and 0 < h <= 4000 and len(str(sha)) == 64:
            out[sha] = dict(z, w=w, h=h, subor=str(subor))
    return out


def plan(obsah, predlohy):
    """Súbory, ktoré web naozaj použije, s rozmermi z predlôh: {sha: {kľúč: (šírka, výška, orez, formát)}}, zdroje."""
    with redirect_stderr(io.StringIO()):
        web = build.Web(Path(obsah), False, predlohy={k: (v['w'], v['h']) for k, v in predlohy.items()})
        web.postav()
    rel = {}
    for r in web.realizacie + web.novinky:
        for f in r['fotky']:
            rel[f['sha']] = f['rel']
    return web.potreba, web.zdroje, rel, titulne_fotky(web), dict(web.vyrezy)


def titulne_fotky(web):
    """sha256 zdrojov titulných fotiek (prísnejší prah PRAH_TITULNE): hrdina úvodu, fotka karty každej realizácie
    a novinky (pás, mriežka, titulná fotka detailu), ilustračné fotky noviniek, fotky sekcií Postup a O firme."""
    out = set()
    with redirect_stderr(io.StringIO()):
        _, f = web.hrdina_foto()
        if f:
            out.add(f['sha'])
        for r in web.realizacie + web.novinky:
            for kluc in ('karta', 'karta_len', 'ilustracna_foto'):
                if r.get(kluc):
                    out.add(r[kluc]['sha'])
        for kluc in ('postup_foto', 'firma_foto'):
            _, f = web.foto_nastavenia(kluc)
            if f:
                out.add(f['sha'])
    return out


# ---------------------------------------------------------------- práca na jednej fotke (proces)

def znizena_priorita():
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
    except Exception:
        pass


def otvor_predlohu(uloha):
    from PIL import Image
    if uloha.get('predloha'):
        with Image.open(uloha['predloha']) as im:
            im = im.convert('RGB')
        im.info = {}
        return im
    return build.otvor_rgb(Path(uloha['zdroj']))


def zaklad_variantu(master, zdroj_im, limit=ZDROJ_LANCZOS_MAX):
    """Funkcia (šírka, orez) -> obrázok, z ktorého sa variant zmenší: zdroj z obsah/, keď je predloha z AI a variant
    nie je širší než limit x zdroj (pri výreze x výrez zdroja; ZDROJ_LANCZOS_MAX, titulné ZDROJ_LANCZOS_MAX_TITULNE),
    inak predloha."""
    def zaklad(s, orez=''):
        if zdroj_im is None:
            return master
        zw, zh = zdroj_im.size
        cw = build.orez_rozmer(zw, zh, build.OREZY[orez])[0] if orez else zw
        return zdroj_im if s <= cw * limit else master
    return zaklad


def spracuj(uloha):
    """Jedna fotka: kvalita (ak nie je známa) a súbory variantov. Vráti záznam manifestu a čas."""
    t0 = time.time()
    master = otvor_predlohu(uloha)
    if master.size != tuple(uloha['rozmer']):
        raise RuntimeError(f'{uloha["rel"]}: predloha má {master.size}, plán čaká {tuple(uloha["rozmer"])}')
    w, h = master.size
    zdroj_im = build.otvor_rgb(Path(uloha['zdroj'])) if uloha.get('zo_zdroja') else None
    limit = ZDROJ_LANCZOS_MAX_TITULNE if uloha.get('titulna') else ZDROJ_LANCZOS_MAX
    zaklad = zaklad_variantu(master, zdroj_im, limit)
    kvalita = dict(uloha.get('kvalita') or {})
    hladanie = {}
    for fmt in ('avif', 'webp'):
        if fmt in kvalita or not any(v[3] == fmt for v in uloha['varianty'].values()):
            continue
        sirky = sorted({v[0] for v in uloha['varianty'].values() if v[3] == fmt and not v[2]}) or [min(w, REF_SIRKA)]
        sr = max([s for s in sirky if s <= REF_SIRKA] or [sirky[0]])
        ref = build.zmensi(zaklad(sr).copy(), sr, build.vyska_pre(w, h, sr))
        q, z, b, n = najmensia_kvalita(ref, fmt, uloha['prah'][fmt])
        # kontrola na najmenšej šírke (viac detailu na pixel): ak tam prah nedrží, hľadá sa ďalej od q nahor
        sm = sirky[0]
        kontrola = None
        if sm != sr:
            ref_m = build.zmensi(zaklad(sm).copy(), sm, build.vyska_pre(w, h, sm))
            zm, _ = zhoda(ref_m, fmt, q)
            if zm < uloha['prah'][fmt]:
                q2, zm, _, n2 = najmensia_kvalita(ref_m, fmt, uloha['prah'][fmt], (q, ROZSAH[fmt][1]))
                n += n2
                q = q2
            kontrola = {'sirka': sm, 'zhoda': round(zm, 5)}
        kvalita[fmt] = q
        hladanie[fmt] = {'ref_sirka': sr, 'zhoda': round(z, 5), 'bajty_ref': b, 'kodovani': n, 'kontrola': kontrola}
    ciel = Path(uloha['ciel'])
    priecinok = ciel / uloha['sha'][:16]
    priecinok.mkdir(parents=True, exist_ok=True)
    subory = dict(uloha.get('hotove') or {})
    vyrez = tuple(uloha['vyrez']) if uloha.get('vyrez') else None
    for kluc, (s, v, orez, fmt) in sorted(uloha['varianty'].items()):
        if kluc in subory:
            continue
        # výrez 4 : 5 karty z rámika (nastavenia karta_foto vyrez), build.py ho berie len so zhodným záznamom vyrez
        box = vyrez if orez == '45' else None
        im = build.zmensi(zaklad(s, orez).copy(), s, v, build.OREZY[orez] if orez else None, box)
        data = build.kodovany(im, fmt, kvalita[fmt])
        rel = f'{uloha["sha"][:16]}/{s}{"-" + orez if orez else ""}.{fmt}'
        tmp = ciel / (rel + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, ciel / rel)
        subory[kluc] = {'subor': rel, 'w': s, 'h': v, 'bajty': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        if box:
            subory[kluc]['vyrez'] = list(box)
    zaznam = {'rel': uloha['rel'], 'kluc': uloha['kluc'],
              'predloha': {'w': w, 'h': h, 'model': uloha.get('model') or 'obsah', 'sha256': uloha.get('sha_predlohy') or ''},
              'kvalita': kvalita, 'zhoda': uloha['prah'], 'subory': dict(sorted(subory.items()))}
    if uloha.get('titulna'):
        zaznam['titulna'] = True
    if uloha.get('retus'):
        # všetky šírky z retušovanej predlohy (zdroj v obsah/ má chybu, ktorú opravuje len predloha)
        zaznam['predloha']['retus'] = True
    if zdroj_im is not None:
        # šírky do tejto šírky (pri výreze do 1,25 x šírky výrezu zdroja) sú zo zdroja v obsah/, nie z AI predlohy
        zaznam['zo_zdroja_do'] = int(zdroj_im.width * limit)
    if hladanie:
        zaznam['hladanie'] = hladanie
    elif uloha.get('hladanie'):
        zaznam['hladanie'] = uloha['hladanie']
    return uloha['sha'], zaznam, round(time.time() - t0, 1)


# ---------------------------------------------------------------- manifest

def nacitaj_manifest(ciel):
    m = Path(ciel) / 'manifest.json'
    if m.is_file():
        d = json.loads(m.read_text(encoding='utf-8'))
        if isinstance(d, dict) and isinstance(d.get('fotky'), dict):
            return d
    return {'verzia': 1, 'fotky': {}}


def zapis_manifest(ciel, d):
    d['fotky'] = dict(sorted(d['fotky'].items()))
    d['_poznamka'] = ('Hotové webové verzie fotiek (nastroje/predlohy_web.py, linka bmb-fotky). Kľúč = sha256 zdrojovej '
                      'fotky v obsah/; build.py záznam použije len pri zhode sha256 zdroja, inak fotku zakóduje sám. '
                      'Metóda a prah: ops/weby-na-mieru/klienti/bmbuilding/fotky-kvalita/KODOVANIE.md.')
    d['metoda'] = {'verzia': VERZIA, 'miera': 'MS-SSIM jasu voči predlohe zmenšenej na tú istú šírku', 'prah': PRAH,
                   'prah_titulne': PRAH_TITULNE, 'rozsah': ROZSAH, 'ref_sirka': REF_SIRKA,
                   'ai_predloha': f'{", ".join(MODELY_AI)} len nad {ZDROJ_LANCZOS_MAX:g} x šírku zdroja, do nej zdroj '
                                  f'z obsah/ Lanczosom ({ZDROJ_DO_SIRKY}); titulné fotky do {ZDROJ_LANCZOS_MAX_TITULNE:g} x '
                                  f'({ZDROJ_DO_SIRKY_TITULNE})'}
    m = Path(ciel) / 'manifest.json'
    tmp = m.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=False) + '\n', encoding='utf-8', newline='\n')
    os.replace(tmp, m)


def hotove_subory(ciel, zaznam):
    """Súbory záznamu, ktoré sú na disku so zapísanou veľkosťou."""
    out = {}
    for k, s in (zaznam.get('subory') or {}).items():
        p = Path(ciel) / s.get('subor', '')
        if p.is_file() and p.stat().st_size == s.get('bajty'):
            out[k] = s
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Webové verzie fotiek BM Building do fotky-web/ (AVIF a WebP).')
    ap.add_argument('--predlohy', type=Path, default=PREDLOHY)
    ap.add_argument('--obsah', type=Path, default=KOREN / 'obsah')
    ap.add_argument('--ciel', type=Path, default=build.FOTKY_WEB)
    ap.add_argument('--limit-min', type=float, default=8.0)
    ap.add_argument('--procesy', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument('--len', default='', help='len tieto fotky (rel z obsah/obrazky, čiarkou), na skúšku')
    ap.add_argument('--vycisti', action='store_true', help='zmazať z fotky-web záznamy a súbory, ktoré web nepoužije')
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')
    t0 = time.time()
    predlohy = nacitaj_predlohy(a.predlohy)
    # predloha len so sha256 súboru podľa predlohy.json (súbor prepísaný po zápise manifestu = zdroj z obsahu)
    stare_predlohy = 0
    for sha, p in list(predlohy.items()):
        p['sha_suboru'] = sha_suboru(p['subor'])
        if p.get('sha256_predlohy') and p['sha_suboru'] != p['sha256_predlohy']:
            print(f'VAROVANIE: predloha {p["subor"]} má iný sha256 než v predlohy.json, beriem zdroj z obsahu')
            del predlohy[sha]
            stare_predlohy += 1
    potreba, zdroje, rel, titulne, vyrezy = plan(a.obsah, predlohy)
    ciel = a.ciel.resolve()
    ciel.mkdir(parents=True, exist_ok=True)
    man = nacitaj_manifest(ciel)
    vyber = {x.strip() for x in a.len.split(',') if x.strip()}
    ulohy, preskocene = [], 0
    for sha in sorted(potreba):
        if vyber and rel.get(sha) not in vyber:
            continue
        p = predlohy.get(sha)
        if p:
            rozmer, sha_pred = (p['w'], p['h']), p['sha_suboru']
        else:
            rozmer, sha_pred = build.rozmery_zdroja(Path(zdroje[sha])), ''
        prah = PRAH_TITULNE if sha in titulne else PRAH
        # retušovaná predloha (pole retus v predlohy.json, napr. RD Ahoj 01: rozmazané plochy na gabióne zakryté
        # klonom gabiónu, kolo 1 24. 11. 2026): zdroj v obsah/ má chybu, ktorú opravuje len predloha, preto sú všetky
        # šírky z predlohy, nie do 1,25 x (titulné 1,6 x) zo zdroja
        retus = bool(p and p.get('retus'))
        zo_zdroja = bool(p and p.get('model') in MODELY_AI and not retus)
        # kľúč bez zmeny pre fotky s pôvodným prahom a bez AI predlohy (nič sa nekóduje znova)
        kluc_text = f'{VERZIA}|{sha_pred or sha}|{rozmer}|{json.dumps(prah, sort_keys=True)}'
        if zo_zdroja:
            kluc_text += '|' + (ZDROJ_DO_SIRKY_TITULNE if sha in titulne else ZDROJ_DO_SIRKY)
        if retus:
            kluc_text += '|retus'
        if vyrezy.get(sha):
            kluc_text += '|vyrez45=' + json.dumps(list(vyrezy[sha]))
        kluc = hashlib.sha256(kluc_text.encode()).hexdigest()[:20]
        stary = man['fotky'].get(sha)
        hotove = hotove_subory(ciel, stary) if stary and stary.get('kluc') == kluc else {}
        varianty = potreba[sha]
        if stary and stary.get('kluc') == kluc and all(k in hotove for k in varianty):
            preskocene += 1
            continue
        ulohy.append({'sha': sha, 'rel': rel.get(sha, ''), 'zdroj': str(zdroje[sha]), 'predloha': p['subor'] if p else '',
                      'model': p.get('model') if p else '', 'sha_predlohy': sha_pred, 'rozmer': list(rozmer),
                      'varianty': varianty, 'prah': prah, 'kluc': kluc, 'ciel': str(ciel),
                      'titulna': sha in titulne, 'zo_zdroja': zo_zdroja, 'retus': retus,
                      'vyrez': list(vyrezy[sha]) if vyrezy.get(sha) else None,
                      'kvalita': stary.get('kvalita') if hotove else None, 'hladanie': stary.get('hladanie') if hotove else None,
                      'hotove': {k: v for k, v in hotove.items() if k in varianty}})
    print(f'plán: {len(potreba)} fotiek na webe, titulných {len(titulne & set(potreba))}, '
          f'z predlôh {sum(1 for s in potreba if s in predlohy)}, '
          f'hotových {preskocene}, na prácu {len(ulohy)}, procesy {a.procesy}, limit {a.limit_min} min')
    koniec = t0 + a.limit_min * 60
    hotovo = chyby = 0
    with ProcessPoolExecutor(max_workers=a.procesy, initializer=znizena_priorita) as ex:
        bezi = set()
        fronta = list(ulohy)
        while fronta or bezi:
            while fronta and len(bezi) < a.procesy and time.time() < koniec:
                bezi.add(ex.submit(spracuj, fronta.pop(0)))
            if not bezi:
                break
            done, bezi = wait(bezi, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    sha, zaznam, trvanie = fut.result()
                except Exception as e:
                    chyby += 1
                    print(f'CHYBA: {e.__class__.__name__}: {e}')
                    continue
                man['fotky'][sha] = zaznam
                zapis_manifest(ciel, man)
                hotovo += 1
                print(f'  {zaznam["rel"]}: kvalita {zaznam["kvalita"]}, {len(zaznam["subory"])} súborov, {trvanie} s', flush=True)
    if a.vycisti and not vyber:
        pouzite = set(potreba)
        for sha in [s for s in man['fotky'] if s not in pouzite]:
            del man['fotky'][sha]
        # aj varianty, ktoré web už nepoužije (napríklad výrez karty po zmene fotky karty v nastavenia.json karta_foto)
        for sha, z in man['fotky'].items():
            z['subory'] = {k: v for k, v in z['subory'].items() if k in potreba[sha]}
        drzat = {s['subor'] for z in man['fotky'].values() for s in z['subory'].values()}
        zmazanych = 0
        for p in sorted(ciel.rglob('*')):
            r = p.relative_to(ciel).as_posix()
            if p.is_file() and p.suffix in ('.avif', '.webp', '.tmp') and r not in drzat:
                p.unlink()
                zmazanych += 1
        for p in sorted(ciel.iterdir()):
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        zapis_manifest(ciel, man)
        print(f'vyčistené: {zmazanych} nepoužitých súborov')
    zostava = len(ulohy) - hotovo - chyby
    velkost = sum(p.stat().st_size for p in ciel.rglob('*') if p.is_file())
    print(f'hotovo {hotovo}, chýb {chyby}, ostáva {zostava}, stare predlohy {stare_predlohy}, fotky-web {velkost / 1e6:.1f} MB, '
          f'{time.time() - t0:.0f} s')
    return 1 if chyby else (3 if zostava else 0)


if __name__ == '__main__':
    sys.exit(main())
