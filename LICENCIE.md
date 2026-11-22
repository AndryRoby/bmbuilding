# Licencie

## Písmo Geist a Geist Mono (web, štýl Olovnica)
- Geist a Geist Mono, Copyright 2024 The Geist Project Authors (https://github.com/vercel/geist-font), SIL Open Font License 1.1. Plné znenie: `sablona/OFL-Geist.txt` (na webe `fonts/OFL-Geist.txt`, odkaz na stránke Ochrana osobných údajov).
- Súbory (nezmenené kópie podmnožín z ukážky ARLing `products/arling-sk/web/ukazky/architekti/fonts/`): `sablona/geist-latin.woff2`, `sablona/geist-latin-ext.woff2` (text, v CSS rodina „BMB Sans“), `sablona/geist-mono-latin.woff2`, `sablona/geist-mono-latin-ext.woff2` (čísla a údaje, rodina „BMB Mono“). Variabilná os wght 100 až 900. Na webe v `fonts/`.
- `sablona/pismo-znaky.txt`: zoznam znakov textových súborov Geist pre kontrolu v `build.py`, vyrába ho `nastroje/znaky_geist.py` (fontTools, len raz).
- Licencia nemá vyhradené meno písma, podmnožiny sú dovolené pod tou istou licenciou.

## Písmo Arimo (len OG obrázky)
- Arimo, Copyright 2026 The Arimo Project Authors (https://github.com/googlefonts/arimo), SIL Open Font License 1.1. Plné znenie: `sablona/OFL-Arimo.txt`.
- Zdroj: Google Fonts, `ofl/arimo/Arimo[wght].ttf` (kópia v `ops/weby-na-mieru/klienti/bmbuilding/dizajn-3/logo/pismo/`). Súbory: `sablona/arimo-400.ttf` a `sablona/arimo-700.ttf` (statické inštancie podmnožiny pre text v OG obrázkoch, ktoré kreslí Pillow), vyrába ich `nastroje/priprav_pismo.py` (fontTools). Na web sa Arimo nekopíruje.
- Prečo Arimo v OG: logo 2026 aj banner na plote sú vysádzané Arial Bold, Arimo je s ním metricky zhodné.

## Logo
- `sablona/logo.svg` a `sablona/logo-favicon.svg`: logo BM Building, s.r.o. (dielo klienta, logo 2026) prekreslené do SVG z obrázka `znacka/logo-2026.jpg`; obrysy písmen postavené na Arime Bold (OFL 1.1 sa na dokumenty vytvorené písmom nevzťahuje), postup v `ops/weby-na-mieru/klienti/bmbuilding/dizajn-3/logo/LOGO.md`. `sablona/logo-favicon.svg` (favicon variant A, 2026) je nakreslený znova na mriežke 32 jednotiek z obrysov písmen BM tohto loga; rastre v `sablona/favicon/` (ICO, PNG, `site.webmanifest`) vznikli generátorom `ops/weby-na-mieru/klienti/bmbuilding/favicon/nastroje/favicon.py`. `logo.png` a OG obrázky renderuje `build.py` z ciest loga.

## Obsah
- Fotky realizácií a logo patria BM Building, s.r.o. (zo starého webu bmbuilding.sk a z jeho Facebooku). Obrázky osvedčení SKSI sa na web nedávajú (obsahujú rodné číslo).

## Kód a štýl
- `build.py`, `test_build.py`, `sablona/styl.css`, `sablona/app.js` a nástroje: ARLing s. r. o., pre klienta BM Building, s.r.o. Štýl Čiara (kostra, rytmus, typografia, kóty, mierka, olovnica v mene, pohyb) je prevzatý z vlastnej ukážky ARLing Ateliér Olovnica (`products/arling-sk/web/ukazky/architekti/`); fotky a texty ukážky sa nepreberajú.
