"""Skopíruje seo/PRESMEROVANIA.json linky bmb-web do sablona/presmerovania.json a zosúladí cieľové slugy
so slugmi v obsah/realizacie (20. 11. 2026). Spustiť z koreňa repozitára ARLing, keď sa zoznam v ops zmení."""
import json
from pathlib import Path

KOREN = Path(__file__).resolve().parents[3]
ZDROJ = KOREN / 'ops/weby-na-mieru/klienti/bmbuilding/seo/PRESMEROVANIA.json'
CIEL = Path(__file__).resolve().parents[1] / 'sablona' / 'presmerovania.json'

MAPA = {
    'rezidencia-grunty-bratislava-karlova-ves': 'rezidencia-grunty-bratislava',
    'bytove-domy-vtacnik-tupeho-bratislava': 'bytove-domy-vtacnik-bratislava',
    'rd-jarovce': 'rd-jarovce-bratislava',
    'ambulancie-rekonstrukcia-obchodnych-priestorov': 'rekonstrukcia-5-ambulancii-pre-lekarov',
}
NA_SLUZBY = {'/index.php/cinnost', '/index.php/cinnost/12-cinnost'}


def main():
    zdroj = json.loads(ZDROJ.read_text(encoding='utf-8'))
    out = []
    for z in zdroj:
        ciel = z['ciel']
        for stary, novy in MAPA.items():
            if ciel == '/realizacie/' + stary + '/':
                ciel = '/realizacie/' + novy + '/'
        if z['stara'] in NA_SLUZBY:
            ciel = '/#sluzby'
        out.append({'stara': z['stara'], 'ciel': ciel})
    doc = {
        '_poznamka': ('Kópia ops/weby-na-mieru/klienti/bmbuilding/seo/PRESMEROVANIA.json (20. 11. 2026) so zosúladenými '
                      'cieľmi podľa slugov v obsah/realizacie. Cieľ realizácie, ktorá neexistuje alebo nie je '
                      'zverejnená, build.py nahradí /realizacie/.'),
        'presmerovania': out,
    }
    CIEL.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(CIEL, len(out))


if __name__ == '__main__':
    main()
