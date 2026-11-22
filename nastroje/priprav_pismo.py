"""Písmo OG obrázkov BM Building: Arimo (OFL 1.1), metricky zhodné s Arial Bold v logu a na banneri.

Web od štýlu Olovnica (21. 11. 2026) píše textom Geist (sablona/geist-*.woff2, zoznam znakov
nastroje/znaky_geist.py); Arimo ostáva len pre text v OG obrázkoch, ktoré kreslí Pillow.

Spustenie raz pri zmene písma (potrebuje fontTools a brotli, build.py ich nepotrebuje):
  python nastroje/priprav_pismo.py <cesta k Arimo[wght].ttf>

Zdroj: Google Fonts, ofl/arimo/Arimo[wght].ttf (sha256 e43898b1...; kópia v
ops/weby-na-mieru/klienti/bmbuilding/dizajn-3/logo/pismo/Arimo-wght.ttf).
Výstup do sablona/:
  arimo-400.ttf, arimo-700.ttf  statické inštancie tej istej podmnožiny pre OG obrázky (Pillow)
"""
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

SABLONA = Path(__file__).resolve().parents[1] / 'sablona'

# základná latinka, Latin-1 a Latin Extended-A (slovenčina, čeština, maďarčina, poľština, nemčina) a sadzba:
# pevná medzera, nezalomiteľný spojovník (sadzba() v build.py dáva U+2011 za číslicu), úvodzovky, tri bodky,
# znak krát, stredná bodka, stupeň, euro, paragraf, mínus, horné indexy (m², m³), šípky
ZNAKY = (''.join(chr(c) for c in range(0x20, 0x7F)) + ''.join(chr(c) for c in range(0xA0, 0x180))
         + '‑‘’‚“”„…−€←→')


def podmnozina(zdroj):
    f = TTFont(zdroj)
    o = subset.Options()
    o.layout_features = ['kern', 'liga', 'tnum', 'lnum', 'case']
    o.name_IDs = ['*']
    o.notdef_outline = True
    s = subset.Subsetter(o)
    s.populate(text=ZNAKY)
    s.subset(f)
    return f


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    zdroj = Path(argv[1])
    for vaha in (400, 700):
        g = instancer.instantiateVariableFont(podmnozina(zdroj), {'wght': vaha})
        g.save(SABLONA / f'arimo-{vaha}.ttf')
    for meno in ('arimo-400.ttf', 'arimo-700.ttf'):
        print(meno, (SABLONA / meno).stat().st_size, 'B')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
