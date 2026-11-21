"""Jednorazovo pripraví statické TTF písma pre OG obrázky do sablona/.

Zdroj: products/arling-sk/web/ukazky/trener/fonts/archivo-text.woff2 (Archivo, The Archivo Project Authors,
SIL OFL 1.1, variabilná os wght 400 až 700, podmnožina latinky so slovenčinou). Výstup: sablona/archivo-400.ttf
a sablona/archivo-600.ttf (inštancie osi wght) a sablona/OFL.txt. build.py potom nepotrebuje fontTools,
len Pillow, a funguje aj na Linuxe v GitHub Action.

Spustenie (raz, z koreňa repozitára ARLing): python products/bmbuilding/nastroje/priprav_pismo_og.py
Potrebuje fontTools a brotli (len tento nástroj, nie build.py).
"""
from pathlib import Path
import shutil

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

KOREN = Path(__file__).resolve().parents[3]
ZDROJ = KOREN / 'products/arling-sk/web/ukazky/trener/fonts/archivo-text.woff2'
OFL = KOREN / 'products/arling-sk/web/ukazky/trener/fonts/OFL.txt'
CIEL = Path(__file__).resolve().parents[1] / 'sablona'


def main():
    CIEL.mkdir(parents=True, exist_ok=True)
    for vaha in (400, 600):
        f = TTFont(str(ZDROJ))
        f.flavor = None
        st = instancer.instantiateVariableFont(f, {'wght': vaha})
        if 'head' in st:
            # pevný čas zmeny, aby bol súbor rovnaký pri každom spustení
            st['head'].modified = st['head'].created
        cesta = CIEL / f'archivo-{vaha}.ttf'
        st.save(str(cesta), reorderTables=True)
        print(cesta, cesta.stat().st_size)
    shutil.copyfile(OFL, CIEL / 'OFL.txt')


if __name__ == '__main__':
    main()
