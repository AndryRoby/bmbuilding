# Web BM Building (bmbuilding.sk)

Statický web firmy BM Building, s.r.o. (stavebný dozor, stavbyvedúci, stavebný manažment, Pezinok). Generátor `build.py` číta obsah z `obsah/` a šablónu zo `sablona/` a zapíše hotový web do `web/`. Bez redakčného systému, bez JavaScriptu, bez cookies, bez cudzích zdrojov.

## Spustenie
Potrebuje Python 3 a Pillow (`pip install pillow`). Žiadna sieť.

```
python build.py               # ostrý beh pre https://bmbuilding.sk/
python build.py --nahlad      # náhľad (napr. https://<účet>.github.io/bmbuilding/)
python build.py --vystup DIR  # iný výstupný priečinok (predvolený web/)
python build.py --obsah DIR   # iný priečinok obsahu (predvolený obsah/)
python -m unittest -v test_build   # testy (asi 45 s)
```

- **Ostrý beh:** sitemap.xml s lastmod, robots.txt so Sitemap, CNAME (bmbuilding.sk), .nojekyll, žiadne noindex.
- **GitHub Actions:** pri `GITHUB_ACTIONS=true` ide ostrý beh len s premennou prostredia `BMB_OSTRY=ano`, inak build sám prepne na náhľad (náhľad na github.io nesmie ísť do indexu). Po prepnutí domény bmbuilding.sk treba v kroku „Zostaviť web“ workflowu pridať `env: BMB_OSTRY: ${{ vars.BMB_OSTRY }}` a premennú repozitára `BMB_OSTRY=ano` (krok pre linku bmb-obsah alebo Fable).
- **Náhľad:** na každej stránke `<meta name="robots" content="noindex, nofollow">`, robots.txt `Disallow: /`, bez sitemap, CNAME a .nojekyll; 404.html má cesty absolútne od `/bmbuilding/` bez `<base>`, aby „Preskočiť na obsah“ ostalo na 404 (`sablona/nastavenia.json`, zaklad_nahladu).
- Interné odkazy sú relatívne, ten istý výstup funguje na doméne aj v podpriečinku github.io. Absolútne (https://bmbuilding.sk) sú len canonical, og:url, og:image, JSON-LD a sitemap.
- Beh je deterministický: rovnaký vstup dá rovnaké bajty, v súboroch nie je čas behu. lastmod realizácie je najnovší z `datum`, `upravene` a `vydanie` (`sablona/nastavenia.json`, deň poslednej zmeny šablóny), ostatných stránok `vydanie`, úvodu, služieb a zoznamu realizácií najnovší z toho všetkého. Rok v päte je z toho istého najnovšieho dátumu. Neplatný kalendárny dátum (2026-13-45) sa vynechá s varovaním.
- Pred zápisom zmaže len súbory, ktoré zapísal predchádzajúci beh (zoznam `web/.zostava.json`), a to až keď sú všetky nové obrázky vyrobené; ak niektorý zlyhá, výstup ostane celý a build skončí kódom 1. Obrázky s rovnakým odtlačkom vstupu sa znova nekódujú.
- Výstup sa overí: bez pomlčiek em a en a bez výkričníkov, inak build zlyhá (kód 1). Texty z obsahu sa pred tým upravia: pomlčka medzi rokmi dá „až“, výkričník bodku.

## Čo vznikne
`/`, `/stavebny-dozor/`, `/stavbyveduci/`, `/stavebny-manazment/`, `/realizacie/`, `/realizacie/<slug>/` pre každú zverejnenú realizáciu, `/opravnenia/`, `/kontakt/`, `/ochrana-udajov/`, `404.html`, presmerovania starých adries Joomla (`/index.php/...`, statické stránky s meta refresh, canonical a odkazom; zoznam `sablona/presmerovania.json`), `obrazky/<slug>/<nn>-<šírka>.webp` (480, 960, 1440 a originál do 1440, nikdy väčšie než originál, bez EXIF), `og/<slug>.jpg` a `og/bmbuilding.jpg` (1200 x 630), `fonts/`, `favicon.svg`, `favicon.ico`, `logo.png`.

Bezpečnosť: CSP v meta značke (`default-src 'none'`, štýl a JSON-LD cez sha256 hash, ktorý build spočíta), každý údaj z JSON escapovaný podľa kontextu, JSON-LD s escapovaným `<`, `>` a `&`, slugy len `[a-z0-9-]`, cesty k fotkám len vnútri `obsah/obrazky`, odkaz na Facebook len https na facebook.com.

## Štruktúra obsahu (dohoda s linkou bmb-obsah, nemeniť)
- `obsah/realizacie/<slug>.json`: `id`, `slug`, `nazov`, `miesto`, `rola`, `projektant`, `roky`, `text`, `fotky` (zoznam `{"subor": "obsah/obrazky/<slug>/01.jpg", "alt": "..."}`), `zdroj` (`web`, `fb`, `sprava`), `fb_url`, `datum` (RRRR-MM-DD), `poradie`, `zverejnene`. Voliteľné `investor`, `zhotovitel`.
  - Voliteľné aj `upravene` (RRRR-MM-DD, posledná zmena, ide do lastmod a dateModified) a `titulok` (vlastný title do 60 znakov, ak sa názov nezmestí; dlhší build nepoužije a vypíše varovanie. Bez neho build skúsi názov s mestom, prvú a poslednú časť názvu, potom začiatok názvu po prvú čiarku; názov nikdy neseká uprostred vety a keď sa nič nezmestí, dá celý názov, Google ho skráti sám).
  - Chýbajúce alebo prázdne polia nespôsobia pád: riadok sa nevypíše. Realizácia bez fotiek nejde do mriežky kariet, ale do kompaktného zoznamu „Ďalšie realizácie“ pod ňou (miesto, rola, roky, pri `zdroj: fb` dátum príspevku), a dostane OG obrázok bez fotky. Fotka, ktorá sa nedá celá dekódovať (useknutý súbor), sa vynechá s varovaním.
  - `zverejnene: false` sa nezobrazí. Zlý alebo duplicitný slug, chýbajúci názov a pokazený JSON sa preskočia s varovaním. Chýbajúca fotka sa vynechá s varovaním.
  - Poradie: `poradie` vzostupne, potom `datum` zostupne, potom `slug`. V mriežkach a v navigácii najprv realizácie s fotkou, potom tie, ktorých prvá fotka má v alt „pôvodný stav“ (karta dostane štítok „Pôvodný stav pred rekonštrukciou“), potom bez fotky. Úvod vynechá v mriežke realizáciu z hlavnej fotky.
  - Fotky JPEG, PNG alebo WebP; nad 2000 px sa najprv zmenšia na 2000.
- `obsah/firma.json`: `meno`, `konatel`, `adresa` (`ulica`, `psc`, `mesto`), `ico`, `ic_dph`, `zapis_or` (do päty len ak `zapis_or_overit` nie je true), `den_zapisu`, `telefon`, `telefon_href`, `email`, `facebook`, `posobnost`, `sluzby` (slug, názov, krátky popis, činnosti), `opravnenia`, `hodiny` (nepoužité, nie sú vo faktoch).
- `obsah/logo.png`: logo pre JSON-LD.

## Šablóna (`sablona/`)
- `texty.json`: title, description a H1 stránok (podľa `ops/weby-na-mieru/klienti/bmbuilding/seo/DOPYT.md`), texty služieb zo starého webu, otázky so zdrojom v zákone č. 25/2025 Z. z. (odkaz na Slov-Lex treba po 1. 1. 2027 skontrolovať), text ochrany údajov.
- `nastavenia.json`: doména, realizácia pre úvodnú fotku, počet kariet na úvode, cesta náhľadu.
- `styl.css`: jediný štýl (vložený do každej stránky, `@PRE@` = relatívna cesta k písmu), podľa `ops/weby-na-mieru/klienti/bmbuilding/navrh/DIZAJN.md`.
- `presmerovania.json`: staré adresy a ciele (kópia zo `seo/PRESMEROVANIA.json` so zosúladenými slugmi, `nastroje/kopiruj_presmerovania.py`).
- písmo: `archivo-400-700.woff2` (web), `archivo-400.ttf`, `archivo-600.ttf` (OG), `OFL.txt`. Licencie v `LICENCIE.md`.

## Nástroje
- `nastroje/naplnenie_zo_stareho_webu.py`: jednorazové naplnenie `obsah/` zo starého webu (linka obsahu).
- `nastroje/priprav_pismo_og.py`: statické TTF pre OG (potrebuje fontTools, len raz).
- `nastroje/kopiruj_presmerovania.py`: obnova `sablona/presmerovania.json`.
- Snímky a merania: `ops/weby-na-mieru/klienti/bmbuilding/navrh/nastroje/snimky-webu.mjs` (len pod zámkom PC).
