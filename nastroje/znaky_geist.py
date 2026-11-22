"""Zoznam znakov písma Geist pre kontrolu v build.py (sablona/pismo-znaky.txt).

Web píše text písmom „BMB Sans“ = Geist (sablona/geist-latin.woff2 a geist-latin-ext.woff2, kópie z ukážky
products/arling-sk/web/ukazky/architekti/fonts, OFL 1.1). Znak mimo týchto dvoch súborov by prehliadač vykreslil
náhradným písmom, preto build varuje (Web.skontroluj_znaky_pisma). Tento nástroj zoznam vyrobí z tabuliek cmap.

Spustenie (raz, po zmene písma; potrebuje fontTools a brotli):
  python -B -X utf8 nastroje/znaky_geist.py
"""
from pathlib import Path

from fontTools.ttLib import TTFont

SABLONA = Path(__file__).resolve().parent.parent / 'sablona'


def main():
    znaky = set()
    for meno in ('geist-latin.woff2', 'geist-latin-ext.woff2'):
        f = TTFont(str(SABLONA / meno))
        znaky |= {chr(k) for k in f.getBestCmap()}
    # medzery a riadiace znaky kontrola aj tak preskakuje, nezalomiteľná medzera ide do zoznamu
    znaky = sorted(z for z in znaky if z.isprintable() or z == ' ')
    (SABLONA / 'pismo-znaky.txt').write_text(''.join(znaky) + '\n', encoding='utf-8', newline='\n')
    print(f'pismo-znaky.txt: {len(znaky)} znakov')


if __name__ == '__main__':
    main()
