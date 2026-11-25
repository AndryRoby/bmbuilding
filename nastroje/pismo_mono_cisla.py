"""Podmnožina písma Geist Mono pre číslice kót a počtov (sablona/geist-mono-cisla.woff2).

Audit naplno 25. 11. 2026, položka 44: web kreslí písmom BMB Mono (Geist Mono) na prvých obrazovkách len číslice,
medzery a pár znakov okolo nich (kóty rokov, valce počítadla slidera a čísel Činnosti, čísla fotiek v galérii, počty
vo filtri). Prehliadač pre ne sťahoval celú latinku Geist Mono (geist-mono-latin.woff2, 23 128 B) až z vloženého
štýlu, teda v kritickej reťazi LCP. Táto podmnožina má len znaky UNICODE_RANGE, styl.css ju deklaruje ako tretí
@font-face rodiny BMB Mono s tým istým unicode-range (posledné pravidlo sa pri prekryve skúša prvé, CSS Fonts 4,
https://www.w3.org/TR/css-fonts-4/#unicode-range-desc) a build.py ju prednačíta len na stránkach, ktoré Mono kreslia.
Písmená v Mono (napríklad počítadlo lightboxu „Fotka 3 z 12“) berie prehliadač naďalej z latinky, až keď ich treba.

Os váhy ostáva celá (100 až 900, ako ostatné tváre rodiny): kóta Činnosti má váhu 300, ostatné 400, a tváre s rovnakými
deskriptormi tvoria jedno zložené písmo, v ktorom rozhoduje len unicode-range.

Spustenie raz pri zmene písma (potrebuje fontTools a brotli, build.py ich nepotrebuje):
  python -B -X utf8 nastroje/pismo_mono_cisla.py
"""
import hashlib
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

SABLONA = Path(__file__).resolve().parents[1] / 'sablona'
ZDROJ = SABLONA / 'geist-mono-latin.woff2'
CIEL = SABLONA / 'geist-mono-cisla.woff2'
# medzera, interpunkcia a číslice (U+0020 až U+0040), pevná medzera, znak krát, pomlčka a mínus (rozsahy čísel);
# ten istý zápis má @font-face v sablona/styl.css (test_build.py to porovná s tabuľkou cmap súboru)
UNICODE_RANGE = 'U+0020-0040,U+00A0,U+00D7,U+2013,U+2212'


def kody(rozsah=UNICODE_RANGE):
    """Množina kódov z reťazca unicode-range (U+XXXX a U+XXXX-YYYY oddelené čiarkou)."""
    out = set()
    for kus in rozsah.split(','):
        kus = kus.strip().upper()
        if not kus.startswith('U+'):
            raise ValueError(kus)
        od, _, do = kus[2:].partition('-')
        out.update(range(int(od, 16), int(do or od, 16) + 1))
    return out


def podmnozina(zdroj=ZDROJ):
    # recalcTimestamp=False: čas zápisu by inak menil hlavičku head a súbor by nebol pri každom behu rovnaký
    f = TTFont(str(zdroj), recalcTimestamp=False)
    o = subset.Options()
    # číslice a interpunkcia nepotrebujú tvarovanie (Geist Mono má len ccmp, locl, zlomky a značky diakritiky);
    # bez funkcií sa do podmnožiny nedostanú glyfy zlomkov cez uzáver GSUB
    o.layout_features = []
    o.name_IDs = ['*']
    o.name_languages = ['*']
    o.notdef_outline = True
    o.flavor = 'woff2'
    s = subset.Subsetter(o)
    s.populate(unicodes=sorted(kody()))
    s.subset(f)
    f.flavor = 'woff2'
    return f


def main():
    f = podmnozina()
    f.save(str(CIEL))
    cmap = set(TTFont(str(CIEL)).getBestCmap())
    chyba = kody() - cmap
    print(f'{CIEL.name}: {CIEL.stat().st_size} B, znakov {len(cmap)}, sha256 {hashlib.sha256(CIEL.read_bytes()).hexdigest()[:16]}')
    if chyba:
        print('znaky, ktoré zdroj nemá:', ' '.join(f'U+{c:04X}' for c in sorted(chyba)))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
