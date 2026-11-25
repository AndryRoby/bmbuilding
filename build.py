#!/usr/bin/env python3
"""Generátor statického webu BM Building (bmbuilding.sk).

Číta obsah/ (firma.json, realizacie/*.json, novinky/*.json, obrazky/) a sablona/ (texty, štýl, app.js, logo,
pravidlá fotiek, presmerovania, písmo) a zapíše hotový web do web/. Python 3 so štandardnou knižnicou a Pillow, bez siete,
deterministicky (rovnaký vstup = rovnaké bajty, žiadny čas behu vo výstupe).

  python build.py               ostrý beh: index, sitemap.xml, robots.txt so Sitemap a robotmi AI, llms.txt, CNAME, .nojekyll
  python build.py --nahlad      náhľad: noindex na každej stránke, robots.txt Disallow: /, bez sitemap a llms.txt,
                                og:image z adresy náhľadu (nastavenia.json adresa_nahladu)
  python build.py --vystup DIR  iný výstupný priečinok (predvolený web/ vedľa build.py)
  python build.py --obsah DIR   iný priečinok obsahu (predvolený obsah/ vedľa build.py)
  python build.py --hosting apache --vystup DIR
                                ostrý web na nahratie cez FTP na webhosting WebHouse (Apache): .htaccess s presmerovaniami
                                301 a pravidlami, bez CNAME, .nojekyll a priečinka index.php/; výstup mimo repozitára,
                                zoznam zostavy vedľa neho (DIR.zostava.json); prepínač aj premenná BMB_HOSTING=apache

V GitHub Actions (GITHUB_ACTIONS=true) ide ostrý beh len s premennou prostredia BMB_OSTRY=ano, inak náhľad:
náhľad na github.io nesmie ísť do indexu, kým doména bmbuilding.sk nie je prepnutá (brána pokus 1, správnosť 1).

Pred zápisom zmaže len súbory, ktoré zapísal predchádzajúci beh (zoznam vo výstupe .zostava.json), a to až keď
sú všetky nové obrázky vyrobené. Obrázky s rovnakým odtlačkom vstupu ostanú a znova sa nekódujú.
"""
import argparse
import base64
import datetime
import hashlib
import html
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageMath, ImageOps, features

KOREN = Path(__file__).resolve().parent
SABLONA = KOREN / 'sablona'
# hotové webové verzie fotiek (nastroje/predlohy_web.py): manifest.json podľa sha256 zdrojovej fotky z obsah/
FOTKY_WEB = KOREN / 'fotky-web'
# obr-2 (linka bmb-fotky): sRGB cez ImageCms, exif_transpose, bez metadát, AVIF a WebP s kvalitou z nastavení
VERZIA_OBR = 'obr-2|srgb|bez-metadat|lanczos|avif-s6-t1|webp-m6'
VERZIA_OG = 'og-3|znacka|jpeg-q82'
# 720: telefóny s DPR do 1,8 (Lighthouse mobil 412 px x 1,75) brali 960 a LCP realizácie s fotkami bolo 2,6 s (kolo 1).
# 1920 a 2400 (linka bmb-fotky): lightbox na 1440 px s hustotou 2 potrebuje okolo 2235 px, prvá veľká bunka galérie
# 1754 px (fotky-kvalita/supis.md); len AVIF, WebP záloha končí na WEBP_MAX (prehliadač bez AVIF dostane to čo dnes)
SIRKY = (480, 720, 960, 1440, 1920, 2400)
WEBP_MAX = 1440
# štandardná šírka sa vynechá, keď je predloha od nej širšia menej ako o 10 % (1989 px: 1920 by bola takmer tá istá)
SIRKA_BLIZKO = 1.1
MAX_VSTUP = 2000
# kvalita kódovania z obsahu (nová fotka bez hotovej webovej verzie, napríklad v GitHub Actions); nastavenia.json
# kvalita_fotiek ju prepíše. Kalibrácia: ops/weby-na-mieru/klienti/bmbuilding/fotky-kvalita/KODOVANIE.md
KVALITA_PREDVOLENA = {'avif': 60, 'webp': 80}
KVALITA = dict(KVALITA_PREDVOLENA)
AVIF_RYCHLOST = 6
FORMATY = ('avif', 'webp')
MIME = {'avif': 'image/avif', 'webp': 'image/webp'}
OG_W, OG_H = 1200, 630
MANIFEST = '.zostava.json'
# hosting ostrého webu (audit naplno 25. 11. 2026, položka 1): pages = GitHub Pages (CNAME, .nojekyll, staré adresy
# Joomly ako stránky s meta refresh v index.php/), apache = webhosting WebHouse cez FTP (.htaccess, staré adresy ako 301,
# bez CNAME, .nojekyll a priečinka index.php/, ktorý by sa na hostingu zrazil so súborom index.php Joomly; zoznam
# zostavy vedľa výstupu, aby sa nenahral na FTP). Prepínač --hosting alebo premenná BMB_HOSTING, predvolený pages.
HOSTINGY = ('pages', 'apache')
HTACCESS = '.htaccess'
# cache na Apache (mod_expires podľa typu bez parametrov): HTML vždy overiť (nová verzia hneď po nahratí), app.js
# má v adrese ?v=<odtlačok>, takže rok; písma a fotky nemajú odtlačok v mene (zmena fotky pod tým istým menom), preto
# mesiac; robots.txt, sitemap.xml a llms.txt hodina. GitHub Pages hlavičky nastaviť nedovolí (max-age 600).
CACHE_APACHE = (('text/html', 'access plus 0 seconds'), ('text/plain', 'access plus 1 hour'),
                ('application/xml', 'access plus 1 hour'), ('text/xml', 'access plus 1 hour'),
                ('application/manifest+json', 'access plus 1 week'), ('application/javascript', 'access plus 1 year'),
                ('text/javascript', 'access plus 1 year'), ('text/css', 'access plus 1 month'),
                ('font/woff2', 'access plus 1 month'), ('image/avif', 'access plus 1 month'),
                ('image/webp', 'access plus 1 month'), ('image/jpeg', 'access plus 1 month'),
                ('image/png', 'access plus 1 month'), ('image/svg+xml', 'access plus 1 month'),
                ('image/x-icon', 'access plus 1 month'), ('image/vnd.microsoft.icon', 'access plus 1 month'))
# gzip na Apache (mod_deflate cez mod_filter): textové typy, AVIF, WebP, JPEG, PNG a woff2 sú už skomprimované
GZIP_APACHE = ('text/html', 'text/plain', 'text/css', 'text/xml', 'application/xml', 'application/javascript',
               'text/javascript', 'application/json', 'application/manifest+json', 'image/svg+xml', 'image/x-icon',
               'image/vnd.microsoft.icon')
SLUG_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
DATUM_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
POMLCKY = '\u2012\u2013\u2014\u2015\u2212'

# značka (dizajn 3, ops/weby-na-mieru/klienti/bmbuilding/dizajn-3/logo/LOGO.md): modrá nameraná v logu 2026
FARBA_BM = (0x17, 0x47, 0x6F)
FARBA_BIELA = (0xFF, 0xFF, 0xFF)
FARBA_BM_200 = (0xB9, 0xCD, 0xDD)

# šípky a krížik z ukážky Ateliér Olovnica (štýl Čiara): vlasová čiara 1,5 px, smer, nie ozdoba
SVG_SIPKA = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
             'aria-hidden="true"><path d="M2 9h13M10 4l5 5-5 5"/></svg>')
SVG_SIPKA_SPAT = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
                  'aria-hidden="true"><path d="M16 9H3M8 4 3 9l5 5"/></svg>')
SVG_VON = ('<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" '
           'aria-hidden="true"><path d="M4 10 10 4M5 3.5h5.5V9"/></svg>')
SVG_KRIZIK = ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.5" '
              'aria-hidden="true"><path d="M4 4l10 10M14 4 4 14"/></svg>')
# vlasové čiary 12 stĺpcov cez celú stránku (najrozpoznateľnejší prvok štýlu, OLOVNICA-ROZBOR.md 6a)
CIARY_HTML = '<div class="ciary" aria-hidden="true"><div class="ciary-vnutro">' + '<span></span>' * 12 + '</div></div>'
# ikony sociálnych sietí (1. kolo pripomienok, ZMENY-KOLO-1.md 3.7): vlastné SVG, nie logá sietí na farebnom
# podklade; text nesie aria-label odkazu (CSP: inline SVG, nič cudzie). krasa-8 (brána 3: obrysy 22 px s čiarou 1,5
# boli drobné, „naozaj pekné“ nestačilo): glyf 24 x 24 zjednodušený podľa oficiálnych značiek, vlastné krivky, rám
# kreslí čip v CSS (.siet-cip, 48 px s vlasovou čiarou). Facebook: plné „f“ (driek 4, priečka a koniec oblúka 3,5,
# zošikmený koniec priečky ako v značke); LinkedIn: plné „in“ (kruhová bodka i, driek 3,5, oblúk n); Instagram:
# fotoaparát čiarou 1,75 (zaoblený štvorec, objektív, plná bodka blesku). Všetky tri v optickom strede (12, 12).
_SIET_SVG = '<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
SVG_FACEBOOK = (_SIET_SVG + '<path fill="currentColor" d="M9.25 21.75V12.75H6.5V9.25H9.25V7.75C9.25 4.15 11.35 2.25 '
                '14.9 2.25H17.5V5.75H15.6C13.95 5.75 13.25 6.45 13.25 8V9.25H16.9L16.4 12.75H13.25V21.75Z"/></svg>')
SVG_LINKEDIN = (_SIET_SVG + '<path fill="currentColor" d="M3.6 9H7.1V20.5H3.6ZM9.4 9H12.85V10.6C13.4 9.55 14.65 8.7 '
                '16.4 8.7C19.7 8.7 20.4 10.9 20.4 13.85V20.5H16.9V14.75C16.9 13.3 16.75 12 15.15 12C13.55 12 12.85 '
                '13.05 12.85 14.75V20.5H9.4Z"/><circle fill="currentColor" cx="5.35" cy="5.35" r="2.05"/></svg>')
SVG_INSTAGRAM = (_SIET_SVG + '<g fill="none" stroke="currentColor" stroke-width="1.75"><rect x="3.25" y="3.25" '
                 'width="17.5" height="17.5" rx="5"/><circle cx="12" cy="12" r="4.1"/></g>'
                 '<circle fill="currentColor" cx="17.15" cy="6.85" r="1.15"/></svg>')
SVG_PAUZA = ('<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
             'aria-hidden="true" focusable="false"><path class="ikona-pauza" d="M5.5 3v10M10.5 3v10"/>'
             '<path class="ikona-hraj" d="M4.5 2.8v10.4L13 8z"/></svg>')
# ikony údajov v hrdinovi úvodu a troch častí Činnosti (ZMENY-KOLO-1.md 3A.2 a 3A.3, skladba podľa vzorov Mária):
# vlastné obrysy vlasovou čiarou ako šípky a siete, nie plné ikony zo vzoru; význam nesie text vedľa nich
_IKONA = ('<svg class="ikona" width="{s}" height="{s}" viewBox="0 0 {s} {s}" fill="none" stroke="currentColor" '
          'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{p}</svg>')
IKONY_UDAJOV = {
    'Sídlo': _IKONA.format(s=24, p='<path d="M12 21.25s-6.75-6.1-6.75-11.25a6.75 6.75 0 0 1 13.5 0c0 5.15-6.75 11.25-6.75 11.25z"/>'
                                   '<circle cx="12" cy="10" r="2.4"/>'),
    'Na trhu': _IKONA.format(s=24, p='<path d="M3.75 5.75h16.5v14.5H3.75zM3.75 10.25h16.5M8 3.25v4.5M16 3.25v4.5M7.5 14.25h2.5"/>'),
    'Konateľ': _IKONA.format(s=24, p='<circle cx="12" cy="7.75" r="3.75"/><path d="M4.75 20.25c.7-4.1 3.5-6.5 7.25-6.5s6.55 2.4 7.25 6.5"/>'),
    'Oprávnenia': _IKONA.format(s=24, p='<path d="M12 3.25 4.75 6v5.25c0 4.5 3.05 8.2 7.25 9.5 4.2-1.3 7.25-5 7.25-9.5V6z"/>'
                                        '<path d="m8.75 12 2.25 2.25 4.25-4.5"/>'),
    # časopis (ZMENY-KOLO-1.md 5.7: údaj V médiách v hrdinovi), rovnaká mriežka 24 a hrúbka čiary ako ostatné ikony
    'V médiách': _IKONA.format(s=24, p='<path d="M4.75 5.25h11.5v13.5a1.5 1.5 0 0 0 1.5 1.5H6.25a1.5 1.5 0 0 1-1.5-1.5z"/>'
                                       '<path d="M16.25 9.25h3v9.5a1.5 1.5 0 0 1-3 0M7.75 9.25h5.5M7.75 12.75h5.5M7.75 16.25h3.5"/>'),
}
# zmienky v médiách pri realizácii (nastavenia.json v_mediach): odkazy len https na tieto weby, inak sa vynechajú
MEDIA_HOSTITELIA = ('asb.sk', 'linkedin.com', 'archinfo.sk')
IKONY_CINNOSTI = {
    'stavebny-manazment': _IKONA.format(s=32, p='<path d="M10 6.75H6.75v21.5h18.5V6.75H22"/><path d="M11.25 4.75h9.5v4h-9.5z"/>'
                                                '<path d="m11 16 2.75 2.75 5.5-5.5M11 23.75h10"/>'),
    'stavebny-dozor': _IKONA.format(s=32, p='<circle cx="14" cy="14" r="8.75"/><path d="m20.25 20.25 7.25 7.25"/>'
                                            '<path d="m10.25 14 2.6 2.6 4.9-4.9"/>'),
    'stavbyveduci': _IKONA.format(s=32, p='<path d="M3.75 24.25h24.5"/><path d="M6.75 24.25V20.5a9.25 9.25 0 0 1 18.5 0v3.75"/>'
                                          '<path d="M13 12.25V8.75h6v3.5M16 8.75v8"/>'),
}
# značka pohľadu A pri titulnej fotke detailu realizácie (ukážka U:730)
SVG_POHLAD_ZNAK = ('<svg class="pohlad-znak" width="34" height="22" viewBox="-11 -11 34 22" aria-hidden="true">'
                   '<path d="M5.5-6.5L17 0L5.5 6.5Z"/><circle r="9.5"/><text y="4">A</text></svg>')
# písma s preloadom na každej stránke (Geist latinka a latinka s diakritikou)
PRELOAD_PISMA = ('geist-latin.woff2', 'geist-latin-ext.woff2')
# Geist Mono kreslí na prvých obrazovkách len číslice kót a počtov (valce kóty, roky, čísla fotiek, počty filtra).
# Audit naplno 25. 11. 2026, položka 44: latinka Mono (23 128 B) sa pre ne sťahovala až z vloženého štýlu, v kritickej
# reťazi dokument, štýl, písmo. Podmnožina číslic (nastroje/pismo_mono_cisla.py, 5 588 B, tretí @font-face rodiny
# BMB Mono v styl.css s unicode-range) má preload len na stránkach, ktoré Mono naozaj kreslia (znaky_mono; inak by
# Chrome hlásil nepoužitý preload, napríklad Kontakt), na úvode v hlave za fotkou LCP, inde na konci tela
# (Web.stranka). Latinku Mono stiahne prehliadač až pri písmene v Mono (počítadlo lightboxu „Fotka 3 z 12“).
PISMO_MONO_CISLA = 'geist-mono-cisla.woff2'
PISMA = ('geist-latin.woff2', 'geist-latin-ext.woff2', 'geist-mono-latin.woff2', 'geist-mono-latin-ext.woff2',
         PISMO_MONO_CISLA, 'OFL-Geist.txt')
# text, ktorý kreslí písmo BMB Mono (styl.css var(--mono)): triedy prvkov, dvojice (trieda predka, značka), triedy
# s písmom webu vo vnútri Mono a stĺpec valca kóty, ktorý kreslí číslice 0 až 9 cez ::before (CSS content).
# test_build.py ich porovná so selektormi v styl.css
MONO_TRIEDY = ('kota', 'lightbox-pocet', 'viac-pocet', 'projekt-ilustr', 'galeria-c')
MONO_POTOMOK = (('dekady', 'span'), ('m-rok', 'b'), ('filter', 'span'))
MONO_SANS = ('kota-slovo', 'vh')
MONO_STLPEC = 'stlpec'
# favicon (variant A, ops/weby-na-mieru/klienti/bmbuilding/favicon/VYBER.md): hotové súbory zo sablona/favicon/ do koreňa
# webu; favicon.svg je sablona/logo-favicon.svg. Cesty v site.webmanifest sú relatívne k manifestu (náhľad aj ostrý web).
FAVICON_SUBORY = ('favicon.ico', 'favicon-32.png', 'favicon-48.png', 'apple-touch-icon.png', 'icon-192.png', 'icon-512.png',
                  'site.webmanifest')
# jediný vložený skript (hash v CSP): trieda js pre pohyb, ktorý čaká na vstup do obrazu, a pre filter a ovládanie
# slidera, ktoré bez JS nie sú. Trieda zmizne len vtedy, keď app.js naozaj nepribehol: odložený skript (defer) sa vždy
# spustí pred DOMContentLoaded, aj keď sa sťahuje dlho, a pri chybe načítania DOMContentLoaded príde tiež, takže obsah
# sa nikdy neskrýva natrvalo. Doladenie-2 (overenie 2, Slow 3G): predtým časovač 4 s od skriptu v hlave triedu zrušil,
# kým sa app.js ešte sťahoval, ovládanie slidera na 0,7 s zmizlo a údaje pod ním skočili o 48 px hore a späť.
SKRIPT_JS = ("(function(d){d.classList.add('js');document.addEventListener('DOMContentLoaded',function(){"
             "if(window.BMB==null)d.classList.remove('js')})})(document.documentElement)")
# bez JS spodná lišta Zavolať a Napísať e-mail na mobile stojí stále (päta má pod sebou miesto, nič nezakryje)
# a fotky kariet pásu z noscript namiesto prázdnych (ukážka U:17)
NOSCRIPT_CSS = 'img[data-src]{display:none}@media (max-width:767px){.lista{transform:none;visibility:visible}}'
# typy fotiek (sablona/fotky-pravidla.json): ktoré nikdy nie sú obal dlaždice
TYPY_FOTIEK = ('HE', 'LH', 'LR', 'R', 'D', 'I', 'P')
NIE_OBAL = ('D', 'P')

# kategórie realizácií (SMER.md 2.7): pole kategoria v obsahu má prednosť, inak odvodené z názvu, potom z textu.
# 1. kolo pripomienok (ZMENY-KOLO-1.md 5.1): len Bytové domy, Rodinné domy a Iné, rekonštrukcie ako druh nie sú
# (Mário: „bez rekonštrukcie“); polyfunkčné objekty idú do Iné (časť 1, Kategórie)
KATEGORIE = (('bytove', 'Bytové domy'), ('rodinne', 'Rodinné domy'), ('ine', 'Iné'))
KAT_VZORY = {
    'rodinne': re.compile(r'^RD\b|rodinn|dvojdom|\bvil[ay]\b', re.I),
    'bytove': re.compile(r'bytov|rezidenci|apartmán', re.I),
}
KAT_PORADIE_NAZOV = ('rodinne', 'bytove')
KAT_PORADIE_TEXT = ('rodinne', 'bytove')
MAX_FOTIEK_NOVINKY = 10
# zoznam realizácií: JS ukáže prvých REALIZACIE_NARAZ kariet výberu a tlačidlo Zobraziť ďalšie (nastavenia
# realizacie_naraz); bez JS sú v HTML a viditeľné všetky karty (SEO, odkaz na každú realizáciu)
REALIZACIE_NARAZ = 12

# roboti AI a vyhľadávačov, ktorých ostrý robots.txt menuje výslovne (firemný checklist zverejnenia ops/runbook.md;
# audit naplno 25. 11. 2026, položka 7): jedna skupina s viacerými riadkami User-agent podľa RFC 9309 časť 2.1
# (https://www.rfc-editor.org/rfc/rfc9309.html), pred skupinou pre všetkých
AI_ROBOTI = ('GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'Bingbot')

CISLA = ' class="cisla"'
# oddeľovač „·“ s pevnou medzerou pred sebou: pri zalomení ostane na konci riadka, nezačne nový
# (brána dizajnu pokus 2, nález 3)
BODKA = '\u00a0· '

# slider stavieb v úvode (1. kolo pripomienok 24. 11. 2026, ZMENY-KOLO-1.md 3.3): poradie a súbory v nastavenia.json
# uvod_slider; prelínanie a čas na fotku ovláda app.js (data-prelinanie, data-interval v ms)
SLIDER_PRELINANIE_MS = 1200
SLIDER_INTERVAL_MS = 8000
# dlhý zoznam Činnosti v dvoch stĺpcoch so spoločnými riadkami: styl.css má triedy riadky-3 až riadky-12 (krasa-8)
RIADKY_ZOZNAMU_MAX = 12
# kotvy úvodu premenované v 1. kole pripomienok (cieľ presmerovania starej adresy -> nový cieľ)
KOTVY_PREMENOVANE = {'/#sluzby': '/#cinnost'}
# oddeľovač častí popisu fotky v lightboxe (ten istý reťazec delí a skladá app.js vo funkcii ukaz)
LB_ODDELOVAC = ' · '
# fakturačné údaje Kontaktu s dlhou hodnotou: na mobile cez oba stĺpce (styl.css .fakt-siroky, doladenie-2)
KONTAKT_SIROKE = ('Sídlo', 'Zápis')
# bankové spojenie a číslo účtu cez oba stĺpce len v najužšom okne (styl.css .fakt-ucet do 359 px, doladenie-3:
# pri 320 px malo číslo účtu s pevnými medzerami 136 px a posledná číslica skočila na nový riadok, 9127/110|0)
KONTAKT_UCET = ('Bankové spojenie', 'Číslo účtu')
# mesiac alebo deň pri realizácii (ZMENY-KOLO-1.md 5.2: pri realizáciách len rok alebo roky): slovenský názov mesiaca
# v ľubovoľnom páde, MM/RRRR a D. M. RRRR. Build varuje pri texte zverejnenej realizácie, ktorý ho má (overenie 3)
MESIAC_V_TEXTE = re.compile(
    r'\b(?:janu[áa]r\w*|febru[áa]r\w*|mar(?:ec|ci|ca)|apr[íi]l\w*|m[áa](?:j|ji|ja)|j[úu](?:n|ni|na|l|li|la)|august\w*'
    r'|septemb\w*|okt[óo]b\w*|novemb\w*|decemb\w*)\b|\b(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}\b'
    r'|\b\d{1,2}\.\s?\d{1,2}\.\s?(?:19|20)\d{2}\b', re.I)

VAROVANIA = []
# Prísne varovania (fb-ostro A4, OPONENT.md S2): fotka slidera úvodu z nastavenia.json (uvod_slider)
# (realizácia alebo fotka), ktorá v obsahu chýba. Lokálne je to varovanie a sekcia sa zostaví bez nej; v GitHub
# Actions (GITHUB_ACTIONS=true) z nich main urobí chybu zostavy (kód 1), nasadenie sa nespustí a ostane posledný dobrý web.
PRISNE_VAROVANIA = []


def varuj(sprava):
    VAROVANIA.append(sprava)
    print('VAROVANIE: ' + sprava, file=sys.stderr)


def varuj_prisne(sprava):
    """Varovanie, ktoré je v GitHub Actions chybou zostavy (PRISNE_VAROVANIA, main)."""
    if sprava not in PRISNE_VAROVANIA:
        PRISNE_VAROVANIA.append(sprava)
    varuj(sprava)


class ChybaZostavy(Exception):
    pass


# ---------------------------------------------------------------- texty a escapovanie

def cisty(hodnota):
    """Bezpečný reťazec z ľubovoľnej hodnoty JSON: bez riadiacich znakov, bez pomlčiek em a en, bez výkričníkov."""
    if hodnota is None or isinstance(hodnota, bool):
        return ''
    if isinstance(hodnota, (int, float)):
        hodnota = str(hodnota)
    if not isinstance(hodnota, str):
        return ''
    s = ''.join(ch for ch in hodnota if ch in '\n' or (ord(ch) >= 32 and not 0x7F <= ord(ch) < 0xA0))
    s = s.replace('\r', '').replace('\t', ' ')
    # rozsah rokov alebo mesiacov: 2021-2022, 2021 – 2022, 06/2013–11/2013 dá „až“
    s = re.sub(r'(?<=\d)\s*[' + POMLCKY + r']\s*(?=\d)', ' až ', s)
    s = re.sub(r'(\b\d{4}|\b\d{1,2}/\d{4})\s*-\s*(\d{4}\b|\d{1,2}/\d{4}\b)', r'\1 až \2', s)
    s = re.sub(r'\s+[' + POMLCKY + r']\s+', ', ', s)
    s = re.sub(r'[' + POMLCKY + r']', '-', s)
    s = re.sub(r'([.?])?!+', lambda m: m.group(1) or '.', s)
    s = re.sub(r'[ \u00a0]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    return s.strip()


def jeden_riadok(hodnota):
    return re.sub(r'\s+', ' ', cisty(hodnota)).strip()


def esc(s):
    return html.escape(s, quote=True)


JEDNOPISMENOVE = re.compile(r'(?<![\w&])([aikosuvzAIKOSUVZ]) ')


def sadzba(s):
    """Slovenská sadzba nad už escapovaným textom: nezalomiteľné medzery."""
    s = JEDNOPISMENOVE.sub('\\1\u00a0', s)
    s = re.sub(r'(?<=\d)\. (?=\d)', '.\u00a0', s)
    s = re.sub(r'(?<=\d) (?=\d)', '\u00a0', s)
    s = re.sub(r'(?<=\d) (?=(x|m|m²|km|€|%|ks|EUR)\b)', '\u00a0', s)
    # „6 x 4-izbové“: násobok sa nezalomí ani za x (snímka 390 px v kole 1 mala „6 x“ na konci riadka)
    s = re.sub(r'(?<=\d\u00a0x) (?=\d)', '\u00a0', s)
    # rozsah \u201e2021 a\u017e 2022\u201c sa nezalom\u00ed (audit naplno 25. 11. 2026, polo\u017eka 16: pri 390 px \u201e2021 / a\u017e 2022\u201c a
    # \u201e2013 a\u017e / 2014\u201c v zozname na /stavebny-dozor/)
    s = re.sub(r'(?<=\d) a\u017e (?=\d)', '\u00a0a\u017e\u00a0', s)
    # „3-izbové“: spojovník za číslicou sa nezalomí (snímka detailu Vtáčnik v2 mala „6 x 3-“ na konci riadka);
    # Geist nem\u00e1 nezalomite\u013en\u00fd spojovn\u00edk U+2011, preto cel\u00e9 slovo v span.nz s white-space:nowrap
    s = re.sub(r'(?<![\w-])(\d+-\w+)', r'<span class="nz">\1</span>', s)
    s = re.sub(r'\b(č\.|ev\.|Ing\.|arch\.|§|ods\.|písm\.) ', '\\1\u00a0', s)
    # výpočet písmen „a), c), e), h)“ sa nezalomí (kontrola zadania 26. 11. 2026: pri 388 až 395 px ostalo „e), h)“
    # samo na poslednom riadku zdroja pod otázkami zo zákona)
    s = re.sub(r'(?<=\b[a-z]\)), (?=[a-z]\))', ',\u00a0', s)
    s = re.sub(r'(§) ', '\\1\u00a0', s)
    return s


def T(s):
    """Text do HTML (escapovaný a so sadzbou)."""
    return sadzba(esc(s))


def kluc_mesta(mesto):
    """Kľúč mesta do adresy a atribútu data-mesto: malé písmená bez diakritiky, [a-z0-9-] (Bratislava -> bratislava)."""
    s = unicodedata.normalize('NFKD', mesto or '').encode('ascii', 'ignore').decode('ascii').lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def ld_json(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=False)
    return s.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def sha(s):
    return "'sha256-" + base64.b64encode(hashlib.sha256(s.encode('utf-8')).digest()).decode() + "'"


def tvar(n, jedno, dve_styri, pat):
    """Počet so slovom v správnom tvare; medzi číslom a slovom pevná medzera (audit naplno 25. 11. 2026, položka 16:
    popis fotky Lumière „Galéria, 8 / fotiek“ sa zalomil medzi číslom a slovom)."""
    if n == 1:
        return f'{n} {jedno}'
    if 2 <= n <= 4:
        return f'{n} {dve_styri}'
    return f'{n} {pat}'


def platny_datum(s):
    """RRRR-MM-DD, ktorý existuje v kalendári (2026-13-45 nie), inak ''."""
    s = s if isinstance(s, str) else ''
    if not DATUM_RE.match(s):
        return ''
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return ''
    return s


def datum_text(iso):
    m = DATUM_RE.match(platny_datum(iso or ''))
    if not m:
        return ''
    return f'{int(m.group(3))}. {int(m.group(2))}. {m.group(1)}'


def s_bodkou(v):
    """Veta s bodkou na konci, bez zdvojenia („Kappa 1 s.r.o.“ nedá „s.r.o..“)."""
    v = v.rstrip()
    return v if v.endswith(('.', '?')) else v + '.'


def skrat(s, limit):
    if len(s) <= limit:
        return s
    kus = s[:limit + 1]
    if ' ' in kus:
        kus = kus[:kus.rindex(' ')]
    else:
        kus = kus[:limit]
    return kus.rstrip(' ,.;:')


def zaciatok_velky(s):
    return s[:1].upper() + s[1:] if s else s


def male(s):
    """Rola do vety: prvé písmeno malé, ak druhé nie je veľké (TDI ostane)."""
    if len(s) > 1 and s[1].isupper():
        return s
    return s[:1].lower() + s[1:]


def bezpecna_url(u, hostitelia=None):
    u = jeden_riadok(u)
    if not u:
        return ''
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return ''
    if p.scheme != 'https' or not p.netloc:
        return ''
    if hostitelia and not any(p.hostname == h or (p.hostname or '').endswith('.' + h) for h in hostitelia):
        return ''
    cesta = urllib.parse.quote(urllib.parse.unquote(p.path), safe='/%:@-._~')
    dotaz = urllib.parse.quote(urllib.parse.unquote(p.query), safe='=&%:@-._~+,')
    return urllib.parse.urlunsplit(('https', p.netloc, cesta, dotaz, ''))


# ---------------------------------------------------------------- načítanie obsahu

def nacitaj_json(cesta):
    # utf-8-sig: súbor uložený v Poznámkovom bloku so znakom BOM sa načíta tiež (brána pokus 2, správnosť 3)
    try:
        return json.loads(Path(cesta).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as e:
        varuj(f'{cesta}: nedá sa načítať ({e.__class__.__name__})')
        return None


def nacitaj_firmu(obsah):
    d = nacitaj_json(obsah / 'firma.json')
    if not isinstance(d, dict):
        raise ChybaZostavy('obsah/firma.json chýba alebo nie je objekt JSON')
    adresa = d.get('adresa') if isinstance(d.get('adresa'), dict) else {}
    tel = jeden_riadok(d.get('telefon'))
    tel_href = re.sub(r'[^\d+]', '', jeden_riadok(d.get('telefon_href')) or tel)
    email = jeden_riadok(d.get('email'))
    if not re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email):
        if email:
            varuj('firma.json: e-mail nemá platný tvar, vynechaný')
        email = ''
    f = {
        'meno': jeden_riadok(d.get('meno')) or 'BM Building, s.r.o.',
        'konatel': jeden_riadok(d.get('konatel')),
        'ulica': jeden_riadok(adresa.get('ulica')),
        'psc': jeden_riadok(adresa.get('psc')),
        'mesto': jeden_riadok(adresa.get('mesto')),
        'ico': jeden_riadok(d.get('ico')),
        'ic_dph': jeden_riadok(d.get('ic_dph')),
        'zapis': jeden_riadok(d.get('zapis_or')) if d.get('zapis_or_overit') is not True else '',
        'den_zapisu': jeden_riadok(d.get('den_zapisu')),
        # výpis z obchodného registra (zdroj údajov O firme v úvode): prvá adresa v poli zapis_or_zdroj, len https na orsr.sk
        'zapis_zdroj': bezpecna_url((re.findall(r'https://\S+', jeden_riadok(d.get('zapis_or_zdroj'))) or [''])[0], ['orsr.sk']),
        'tel': tel,
        'tel_href': tel_href,
        'email': email,
        'facebook': bezpecna_url(d.get('facebook'), ['facebook.com', 'fb.com']),
        'posobnost': jeden_riadok(d.get('posobnost')) or 'Bratislava a okolie',
        'sluzby': {},
        'opravnenia': [],
    }
    # zlý typ poľa (napr. "sluzby": 5) dá varovanie, nie traceback (brána pokus 2, správnosť 4)
    def zoznam(hodnota, meno):
        if hodnota is None or isinstance(hodnota, list):
            return hodnota or []
        varuj(f'firma.json: {meno} nie je zoznam, vynechané')
        return []
    for s in zoznam(d.get('sluzby'), 'sluzby'):
        if isinstance(s, dict) and SLUG_RE.match(str(s.get('slug') or '')):
            f['sluzby'][s['slug']] = {
                'nazov': jeden_riadok(s.get('nazov')),
                'popis': jeden_riadok(s.get('kratky_popis')),
                'cinnosti': [jeden_riadok(c) for c in zoznam(s.get('cinnosti'), f'sluzby.{s["slug"]}.cinnosti')
                             if jeden_riadok(c)],
            }
    for o in zoznam(d.get('opravnenia'), 'opravnenia'):
        if not isinstance(o, dict):
            continue
        f['opravnenia'].append({
            'organizacia': jeden_riadok(o.get('organizacia')),
            'nazov': jeden_riadok(o.get('nazov')),
            'osoba': jeden_riadok(o.get('osoba')),
            'cinnost': jeden_riadok(o.get('cinnost')),
            'zameranie': jeden_riadok(o.get('odborne_zameranie')),
            'cislo': jeden_riadok(o.get('evidencne_cislo')),
            'datum': jeden_riadok(o.get('datum_vydania_text')) or datum_text(jeden_riadok(o.get('datum_vydania'))),
            'rok': (jeden_riadok(o.get('datum_vydania')) or '')[:4],
        })
    return f


def cesta_fotky(obsah, subor):
    """Cesta k fotke len vnútri obsah/obrazky (žiadne ../, žiadne absolútne cesty)."""
    s = jeden_riadok(subor).replace('\\', '/')
    if not s or s.startswith('/') or re.match(r'^[A-Za-z]:', s):
        return None
    if s.startswith('obsah/'):
        s = s[len('obsah/'):]
    kusy = s.split('/')
    if any(k in ('', '.', '..') for k in kusy) or kusy[0] != 'obrazky':
        return None
    p = (obsah / s).resolve()
    koren = (obsah / 'obrazky').resolve()
    if koren not in p.parents:
        return None
    if p.suffix.lower() not in ('.jpg', '.jpeg', '.webp', '.png'):
        return None
    return p


def rozmery_zdroja(p):
    with Image.open(p) as im:
        # plné dekódovanie: fotka s platnou hlavičkou a useknutými dátami (prerušené nahratie v systéme obsahu)
        # tu vyhodí OSError a vynechá sa, inak by zhodila build až pri zápise (brána pokus 1, správnosť 2)
        im.load()
        w, h = im.size
        try:
            orient = im.getexif().get(274, 1)
        except Exception:
            orient = 1
    if orient in (5, 6, 7, 8):
        w, h = h, w
    if max(w, h) > MAX_VSTUP:
        k = MAX_VSTUP / max(w, h)
        w, h = max(1, round(w * k)), max(1, round(h * k))
    return w, h


_OSTROST = {}


def ostrost_fotky(p):
    """Ostrosť fotky rovnakým postupom ako audit fotiek (dizajn-3/fotky/audit.py): šedá verzia zmenšená na šírku 960 px,
    rozptyl Laplaciánu so 4-susedným jadrom. Len Pillow (CI nemá numpy): Laplacián cez ImageMath v režime F, priemery
    cez zmenšenie na 1 x 1 filtrom BOX. Cache podľa odtlačku súboru (stovky realizácií, viac behov v testoch)."""
    data = Path(p).read_bytes()
    kluc = hashlib.sha256(data).hexdigest()
    if kluc in _OSTROST:
        return _OSTROST[kluc]
    with Image.open(io.BytesIO(data)) as im:
        g = ImageOps.exif_transpose(im).convert('L')
    w, h = g.size
    if w != 960:
        g = g.resize((960, max(1, round(h * 960 / w))), Image.LANCZOS)
    f = g.convert('F')
    w, h = f.size
    if w < 3 or h < 3:
        _OSTROST[kluc] = 0.0
        return 0.0
    kus = {'c': f.crop((1, 1, w - 1, h - 1)), 'u': f.crop((1, 0, w - 1, h - 2)), 'd': f.crop((1, 2, w - 1, h)),
           'l': f.crop((0, 1, w - 2, h - 1)), 'r': f.crop((2, 1, w, h - 1))}
    lap = ImageMath.lambda_eval(lambda a: a['u'] + a['d'] + a['l'] + a['r'] - a['c'] * 4, **kus)
    stv = ImageMath.lambda_eval(lambda a: a['x'] * a['x'], x=lap)
    m = lap.resize((1, 1), Image.BOX).getpixel((0, 0))
    m2 = stv.resize((1, 1), Image.BOX).getpixel((0, 0))
    v = round(max(0.0, m2 - m * m), 1)
    _OSTROST[kluc] = v
    return v


def sirky_pre(w, strop=None):
    """Šírky súborov fotky širokej w px: štandardné šírky menšie než w a samotná w (najviac strop, predvolene
    SIRKY[-1]); štandardná šírka, od ktorej je w širšia menej ako o SIRKA_BLIZKO, sa vynechá (takmer rovnaký súbor)."""
    strop = SIRKY[-1] if strop is None else strop
    w = min(w, strop)
    out = [s for s in SIRKY if s < w and s * SIRKA_BLIZKO <= w]
    if w not in out:
        out.append(w)
    return sorted(out)


def vyska_pre(w, h, s):
    """Výška súboru so šírkou s pre fotku w x h (rovnaký výpočet v build.py aj v nastroje/predlohy_web.py)."""
    return max(1, round(h * s / w))


def nastav_sirky(f, w, h, formaty):
    """Šírky súborov fotky z predlohy w x h (hotová webová verzia z fotky-web/, inak zdroj z obsah/): sirky = WebP
    (záloha a plná fotka odkazu, pri AVIF najviac WEBP_MAX), sirky_avif = AVIF (prázdne bez AVIF), max_w a max_h
    najväčší súbor vôbec (lightbox, bunky galérie), plna_w najväčší WebP (odkaz bez JS)."""
    f['zaklad'] = (w, h)
    f['sirky_avif'] = sirky_pre(w) if 'avif' in formaty else []
    f['sirky'] = sirky_pre(w, WEBP_MAX) if f['sirky_avif'] else sirky_pre(w)
    f['plna_w'] = f['sirky'][-1]
    f['plna_h'] = vyska_pre(w, h, f['plna_w'])
    f['max_w'] = max(f['sirky'][-1], f['sirky_avif'][-1] if f['sirky_avif'] else 0)
    f['max_h'] = vyska_pre(w, h, f['max_w'])


def avif_dostupny():
    """Pillow s kodekom AVIF (PC aj koleso pillow 11.3.0 pre manylinux, fotky-kvalita/ci-avif.md)."""
    try:
        return bool(features.check('avif'))
    except Exception:
        return False


def kategorie_realizacie(nazov, text, vlastna=None):
    """Zoznam kľúčov kategórií, prvý je hlavný. Pole kategoria v obsahu (kľúč alebo názov) má prednosť; neznáma
    hodnota (aj stará „rekonstrukcie“) sa nepoužije. Inak hlavná podľa názvu (rodinné, bytové), potom podľa textu
    (rodinné, bytové), inak „ine“; ďalšie sú všetky iné zhody v názve a texte."""
    kluce = [k for k, _ in KATEGORIE]
    hlavna = None
    if isinstance(vlastna, str):
        vlastna = [vlastna]
    if isinstance(vlastna, list):
        mena = {v.lower(): k for k, v in KATEGORIE}
        vybrane = []
        for v in vlastna:
            v = jeden_riadok(v).lower()
            k = v if v in kluce else mena.get(v)
            if k and k not in vybrane:
                vybrane.append(k)
        if vybrane:
            return vybrane
    for k in KAT_PORADIE_NAZOV:
        if KAT_VZORY[k].search(nazov):
            hlavna = k
            break
    if hlavna is None:
        for k in KAT_PORADIE_TEXT:
            if KAT_VZORY[k].search(text):
                hlavna = k
                break
    if hlavna is None:
        return ['ine']
    dalsie = [k for k in ('bytove', 'rodinne')
              if k != hlavna and (KAT_VZORY[k].search(nazov) or KAT_VZORY[k].search(text))]
    return [hlavna] + dalsie


def roky_realizacie(roky, datum):
    """(rok, rok_od, rok_je_z_datumu): rok = najvyšší rok z poľa roky; bez neho rok z dátumu príspevku (karta ho
    potom nikdy nepíše ako rok stavby); bez oboch 0 (skupina Bez roku na konci)."""
    ys = [int(x) for x in re.findall(r'\b(?:19|20)\d{2}\b', roky)]
    if ys:
        return max(ys), min(ys), False
    if datum:
        return int(datum[:4]), int(datum[:4]), True
    return 0, 0, False


def zorad_chronologicky(zoznam):
    """rok zostupne, rok začiatku zostupne, poradie vzostupne, datum zostupne, slug (SMER.md 2.7)."""
    zoznam = sorted(zoznam, key=lambda r: r['slug'])
    zoznam.sort(key=lambda r: r['datum'], reverse=True)
    zoznam.sort(key=lambda r: r['poradie'])
    zoznam.sort(key=lambda r: r['rok_od'], reverse=True)
    zoznam.sort(key=lambda r: r['rok'], reverse=True)
    return zoznam


def nacitaj_fotky(obsah, zoznam, meno, nazov, miesto, max_pocet=None):
    fotky = []
    zoznam = zoznam if isinstance(zoznam, list) else []
    if max_pocet is not None and len(zoznam) > max_pocet:
        varuj(f'{meno}: {len(zoznam)} fotiek, použitých prvých {max_pocet}')
        zoznam = zoznam[:max_pocet]
    for f in zoznam:
        if isinstance(f, str):
            f = {'subor': f}
        if not isinstance(f, dict):
            continue
        p = cesta_fotky(obsah, f.get('subor'))
        if p is None or not p.is_file():
            varuj(f'{meno}: fotka {f.get("subor")!r} chýba alebo je mimo obsah/obrazky, vynechaná')
            continue
        try:
            w, h = rozmery_zdroja(p)
            ostrost = ostrost_fotky(p)
        except Exception:
            varuj(f'{meno}: fotka {f.get("subor")!r} sa nedá otvoriť, vynechaná')
            continue
        typ = jeden_riadok(f.get('typ')).upper()
        # vizualizácia (zadanie bmb-dokoncenie, Obsah 3): pole druh a meno autora z príspevku; nikdy obal ani hrdina
        viz = jeden_riadok(f.get('druh')).lower() == 'vizualizacia'
        fotky.append({'zdroj': p, 'w': w, 'h': h, 'alt': jeden_riadok(f.get('alt')), 'ostrost': ostrost,
                      'rel': p.relative_to((obsah / 'obrazky').resolve()).as_posix(),
                      'typ': typ if typ in TYPY_FOTIEK else '', 'viz': viz,
                      'autor': jeden_riadok(f.get('autor')) if viz else '',
                      # sha256 zdroja: kľúč hotovej webovej verzie vo fotky-web/manifest.json (Web.nastav_sirky)
                      'sha': hashlib.sha256(p.read_bytes()).hexdigest()})
    for i, f in enumerate(fotky, 1):
        f['nn'] = f'{i:02d}'
        nastav_sirky(f, f['w'], f['h'], ['avif', 'webp'])
        if not f['alt']:
            f['alt'] = ', '.join(x for x in (nazov, miesto) if x)
        if f['viz'] and not f['alt'].lower().startswith('vizualizácia'):
            f['alt'] = 'Vizualizácia, ' + f['alt'] + (f', autor {f["autor"]}' if f['autor'] else '')
    return fotky


def zverejnene(d):
    z = d.get('zverejnene', True)
    return not (z is False or (isinstance(z, str) and z.strip().lower() in ('false', 'nie', '0', '')) or z == 0 or z is None)


def nacitaj_novinky(obsah):
    """obsah/novinky/<slug>.json od linky bmb-fb: datum, nadpis, text, fotky (najviac 10), fb_url, slug. Zlý JSON,
    slug, dátum alebo cesta k fotke sa preskočí s varovaním; bez priečinka je zoznam prázdny."""
    priecinok = obsah / 'novinky'
    subory = sorted(priecinok.glob('*.json')) if priecinok.is_dir() else []
    vsetky, slugy = [], set()
    for subor in subory:
        d = nacitaj_json(subor)
        if not isinstance(d, dict):
            varuj(f'novinky/{subor.name}: nie je objekt JSON, preskočené')
            continue
        slug = d.get('slug')
        if slug is None or slug == '':
            slug = subor.stem
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            varuj(f'novinky/{subor.name}: slug {slug!r} nie je [a-z0-9-], preskočené')
            continue
        if slug in slugy:
            varuj(f'novinky/{subor.name}: slug {slug} je duplicitný, preskočené')
            continue
        if not zverejnene(d):
            continue
        text = cisty(d.get('text'))
        nadpis = jeden_riadok(d.get('nadpis'))
        if not nadpis and text:
            nadpis = skrat(text.split('\n', 1)[0].strip(), 100)
        if not nadpis:
            varuj(f'novinky/{subor.name}: chýba nadpis aj text, preskočené')
            continue
        datum = jeden_riadok(d.get('datum'))
        if not platny_datum(datum):
            varuj(f'novinky/{subor.name}: datum {datum!r} nie je platný dátum RRRR-MM-DD, preskočené')
            continue
        upravene = jeden_riadok(d.get('upravene'))
        if upravene and not platny_datum(upravene):
            upravene = ''
        fotky = nacitaj_fotky(obsah, d.get('fotky'), f'novinky/{slug}', nadpis, '', MAX_FOTIEK_NOVINKY)
        slugy.add(slug)
        vsetky.append({'slug': slug, 'nadpis': nadpis, 'text': text, 'datum': datum, 'upravene': upravene,
                       'fb_url': bezpecna_url(d.get('fb_url'), ['facebook.com', 'fb.com']), 'fotky': fotky,
                       # zdroj (fb, sprava): odkaz na Facebook firmy bez fb_url len pri príspevku z Facebooku
                       'zdroj': jeden_riadok(d.get('zdroj')) or 'fb',
                       'obr_dir': f'obrazky-novinky/{slug}',
                       # realizacia (obsah-zmeny.md): slug realizácie tej istej stavby, roky stavby sa berú z nej
                       'realizacia': d.get('realizacia') if isinstance(d.get('realizacia'), str)
                       and SLUG_RE.match(d.get('realizacia') or '') else ''})
    vsetky.sort(key=lambda n: n['slug'])
    vsetky.sort(key=lambda n: n['datum'], reverse=True)
    return vsetky


def nacitaj_realizacie(obsah):
    priecinok = obsah / 'realizacie'
    subory = sorted(priecinok.glob('*.json')) if priecinok.is_dir() else []
    vsetky, slugy = [], set()
    for subor in subory:
        d = nacitaj_json(subor)
        if not isinstance(d, dict):
            varuj(f'{subor.name}: nie je objekt JSON, preskočené')
            continue
        slug = d.get('slug')
        if slug is None or slug == '':
            slug = subor.stem
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            varuj(f'{subor.name}: slug {slug!r} nie je [a-z0-9-], preskočené')
            continue
        if slug in slugy:
            varuj(f'{subor.name}: slug {slug} je duplicitný, preskočené')
            continue
        if not zverejnene(d):
            continue
        nazov = jeden_riadok(d.get('nazov'))
        if not nazov:
            varuj(f'{subor.name}: chýba názov, preskočené')
            continue
        # dátum musí existovať v kalendári, inak by 2026-13-45 išlo do sitemap aj JSON-LD (brána pokus 1, správnosť 5)
        datum = jeden_riadok(d.get('datum'))
        if datum and not platny_datum(datum):
            varuj(f'{subor.name}: datum {datum!r} nie je platný dátum RRRR-MM-DD, vynechaný')
            datum = ''
        # upravene (nepovinné): posledná zmena realizácie, ide do lastmod a dateModified
        upravene = jeden_riadok(d.get('upravene'))
        if upravene and not platny_datum(upravene):
            varuj(f'{subor.name}: upravene {upravene!r} nie je platný dátum RRRR-MM-DD, vynechané')
            upravene = ''
        try:
            poradie = int(d.get('poradie'))
        except (TypeError, ValueError):
            poradie = 10 ** 6
        fotky = nacitaj_fotky(obsah, d.get('fotky'), slug, nazov, jeden_riadok(d.get('miesto')))
        roky = jeden_riadok(d.get('roky'))
        rok, rok_od, rok_z_datumu = roky_realizacie(roky, datum)
        text = cisty(d.get('text'))
        r = {
            'obr_dir': f'obrazky/{slug}',
            'kategorie': kategorie_realizacie(nazov, text, d.get('kategoria')),
            'rok': rok,
            'rok_od': rok_od,
            'rok_z_datumu': rok_z_datumu,
            'slug': slug,
            'nazov': nazov,
            'miesto': jeden_riadok(d.get('miesto')),
            'rola': zaciatok_velky(jeden_riadok(d.get('rola'))),
            'projektant': jeden_riadok(d.get('projektant')),
            # autor (nepovinné): zdroj uvádza „Autor“, nie „Projektant“ (Vila Holubyho, brána bmb-fb-uplnost nález 3)
            'autor': jeden_riadok(d.get('autor')),
            'roky': roky,
            'investor': jeden_riadok(d.get('investor')),
            'zhotovitel': jeden_riadok(d.get('zhotovitel')),
            'text': text,
            'zdroj': jeden_riadok(d.get('zdroj')),
            'fb_url': bezpecna_url(d.get('fb_url'), ['facebook.com', 'fb.com']),
            'datum': datum,
            'upravene': upravene,
            # titulok (nepovinné): vlastný title stránky realizácie, ak sa názov nezmestí do 60 znakov
            'titulok': jeden_riadok(d.get('titulok')),
            'poradie': poradie,
            'fotky': fotky,
            # rok_prispevku (obsah-zmeny.md): rok najnovšieho príspevku, len keď roky stavby chýbajú; od 1. kola
            # pripomienok sa na webe neukazuje (roky_karty). obal_vizualizacia: realizácia bez skutočnej fotky (Devín)
            'rok_prispevku': (jeden_riadok(d.get('rok_prispevku'))
                              if re.match(r'^(19|20)\d{2}$', jeden_riadok(d.get('rok_prispevku'))) and not roky else ''),
            'obal_vizualizacia': d.get('obal_vizualizacia') is True,
        }
        if len(r['titulok']) > 60:
            # title nad 60 znakov Google skráti; vlastný titulok sa vtedy nepoužije (brána pokus 2, správnosť 1)
            varuj(f'{subor.name}: titulok má {len(r["titulok"])} znakov, najviac 60, nepoužitý')
            r['titulok'] = ''
        slugy.add(slug)
        vsetky.append(r)
    # chronologicky podľa rokov stavby (pole datum zo starého webu je dátum zverejnenia, rozhoduje až nakoniec)
    return zorad_chronologicky(vsetky)


# ---------------------------------------------------------------- obrázky

def do_srgb(im):
    """Obrázok s vloženým profilom ICC prevedený do sRGB (ImageCms, percepčný zámer); bez profilu sa berie ako sRGB
    (fotky z Facebooku a starého webu). Chyba profilu nezhodí build, fotka ostane bez prevodu."""
    icc = im.info.get('icc_profile')
    if not icc:
        return im
    try:
        from PIL import ImageCms
        zdroj = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        popis = (ImageCms.getProfileDescription(zdroj) or '').lower()
        if 'srgb' in popis.replace(' ', '') and im.mode == 'RGB':
            return im
        rezim = 'RGBA' if im.mode in ('RGBA', 'LA', 'PA') else 'RGB'
        if im.mode not in ('RGB', 'RGBA', 'CMYK', 'L'):
            im = im.convert(rezim)
        return ImageCms.profileToProfile(im, zdroj, ImageCms.createProfile('sRGB'), outputMode=rezim)
    except Exception as e:
        varuj(f'profil ICC sa nedá previesť do sRGB ({e.__class__.__name__}), fotka bez prevodu')
        return im


def otvor_rgb(p, max_vstup=MAX_VSTUP):
    """Fotka ako RGB v sRGB: otočenie podľa EXIF, prevod profilu ICC do sRGB, priehľadnosť na farbu podkladu,
    nad max_vstup zmenšená. Výsledok je bez metadát (prázdne info: žiadne EXIF, XMP ani ICC do výstupu)."""
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        im = do_srgb(im)
        if im.mode in ('RGBA', 'LA', 'P', 'PA'):
            im = im.convert('RGBA')
            podklad = Image.new('RGB', im.size, (0xEC, 0xEB, 0xE6))
            podklad.paste(im, mask=im.split()[-1])
            im = podklad
        else:
            im = im.convert('RGB')
        if max_vstup and max(im.size) > max_vstup:
            k = max_vstup / max(im.size)
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        out = im.copy()
    out.info = {}
    return out


def box_px(box, w, h):
    """Rámik (x0, y0, x1, y1 v podieloch fotky 0 až 1) v px fotky w x h."""
    return (round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h))


def zmensi(im, sirka, vyska, pomer=None, box=None):
    """Výrez zo stredu s pomerom (orezy kariet a mobilného rámu) a zmena na sirka x vyska filtrom Lanczos. box: najprv
    len táto časť fotky (rámik v podieloch, karta_foto vyrez), výrez s pomerom potom zo stredu rámika."""
    if box:
        im = im.crop(box_px(box, im.width, im.height))
    if pomer:
        cw, ch = orez_rozmer(im.width, im.height, pomer)
        x, y = (im.width - cw) // 2, (im.height - ch) // 2
        im = im.crop((x, y, x + cw, y + ch))
    if im.size != (sirka, vyska):
        im = im.resize((sirka, vyska), Image.LANCZOS)
    im.info = {}
    return im


def kodovany(im, fmt, kvalita):
    """Bajty WebP alebo AVIF bez metadát (žiadne exif, xmp ani icc_profile; AVIF nesie len nclx sRGB, ktoré libavif
    zapíše sám). AVIF v jednom vlákne: rovnaký vstup dá rovnaké bajty na každom stroji."""
    im = im.copy()
    im.info = {}
    buf = io.BytesIO()
    if fmt == 'avif':
        im.save(buf, 'AVIF', quality=int(kvalita), speed=AVIF_RYCHLOST, max_threads=1, subsampling='4:2:0',
                exif=b'', xmp=b'', icc_profile=b'')
    else:
        im.save(buf, 'WEBP', quality=int(kvalita), method=6, exif=b'', xmp=b'', icc_profile=b'')
    return buf.getvalue()


def webp_bajty(p, sirka, vyska, pomer=None, box=None):
    """Fotka z obsahu zmenšená na sirka x vyska ako WebP s kvalitou KVALITA['webp']; s pomerom najprv výrez zo stredu
    (s rámikom box zo stredu rámika)."""
    return kodovany(zmensi(otvor_rgb(p), sirka, vyska, pomer, box), 'webp', KVALITA['webp'])


def avif_bajty(p, sirka, vyska, pomer=None, box=None):
    """To isté ako webp_bajty vo formáte AVIF s kvalitou KVALITA['avif']."""
    return kodovany(zmensi(otvor_rgb(p), sirka, vyska, pomer, box), 'avif', KVALITA['avif'])


# orezy (PLAN-1-1.md časť 8): karta pásu 4 : 5 a mobilný rám pohľadu A 5 : 4; súbor NN-<šírka>-<kód>.webp
OREZY = {'45': (4, 5), '54': (5, 4), '43': (4, 3), '34': (3, 4)}
SIRKY_OREZU = (480, 720, 960)
# karta s malým zdrojom (brána bmb-presnost pokus 1, nález 1): výrez 4 : 5 karty mriežky, služby a novinky s aspoň
# KARTA_MIN_OREZ px zdroja je ostrý aj pri najširšej karte (422 CSS px pri 1920 px, hustota 2: 844 px obrazovky, na 1 px
# zdroja najviac 1,25 px obrazovky, 844 / 1,25 = 675,2). Menší zdroj ide od doladenia-1 tiež cez celý rám (celá fotka
# v strede rámu na rozmazanom podklade pôsobila ako pokazená fotka); stránka služby ho dá až za karty s väčším zdrojom
KARTA_MIN_OREZ = 676


def orez_rozmer(w, h, pomer):
    """Najväčší výrez s pomerom pomer (šírka, výška) zo stredu fotky w x h."""
    a, b = pomer
    if w * b > h * a:
        return max(1, round(h * a / b)), h
    return w, max(1, round(w * b / a))


def sirky_orezu(cw):
    out = [s for s in SIRKY_OREZU if s < cw]
    if cw <= SIRKY_OREZU[-1] and cw not in out:
        out.append(cw)
    return sorted(out) or [SIRKY_OREZU[-1]]


# osvedčenia SKSI (stránka Oprávnenia): text dokladu ostáva čitateľný (vyššia kvalita); súbor
# opravnenia/<meno>-<šírka>.webp. Sken má len 528 a 522 px, stránka a lightbox ho pri hustote 2 kreslia na 1056 px
# (presnost/meranie/fotky-pred.md časť 11), preto aj zväčšené súbory do ZVACSENIE_DOKLADU x zdroj, len bez AI:
# Lanczos a jemné doostrenie (UnsharpMask polomer 1,0, 40 %, prah 2). Kalibrácia v ops/weby-na-mieru/klienti/
# bmbuilding/presnost/oprava-fotky.md: zmenšené späť na rozmer skenu sa od skenu líšia rovnako ako čistý Lanczos
# (najväčší rozdiel jasu 17,6 a 26,6 z 255, priemer 0,45 a 0,66), zakryté miesta ostávajú jednofarebné. Ostrejší
# doklad dá len nový sken. Zdroj v sablona/opravnenia/ sa nemení.
SIRKY_DOKLADU = (360, 720)
ZVACSENIE_DOKLADU = 2
DOOSTRENIE_DOKLADU = (1.0, 40, 2)
# vnútro rámu dokladu v CSS px (styl.css .doklady --doklad-max 554 px = 528 + 2 x 12 px okraj + 2 x 1 px čiara):
# užší sken (522 px) sa v ráme zobrazí na 528 px (zmerané: box 528 x 757,5 CSS px pri 1440 a 1920), preto najväčší
# súbor má 2 x max(šírka skenu, 528), inak by prehliadač pri hustote 2 súbor 1044 px zväčšoval (z_súbor 1,011)
SIRKA_RAMU_DOKLADU = 528
# pomer strán vnútra rámu pri najväčšej šírke: 528 x (554 x 297 / 210 - 2 x 12 - 2 x 1) = 528 x 757,51 CSS px
POMER_RAMU_DOKLADU = SIRKA_RAMU_DOKLADU / (554 * 297 / 210 - 26)
# orez zakrytého skenu na hranu papiera (ľavý, horný, pravý, dolný okraj výrezu v px skenu; brána bmb-presnost pokus 2,
# nález 1): object-fit contain dal skenom s iným pomerom strán (528 x 755 a 522 x 729) iný biely pás v ráme, papier
# vedľa seba bol o 7 px inde a o 16 px inak vysoký (1440 px). Výrez odstráni biely okraj skenera (ľavý sken vľavo 3,
# hore 4, vpravo 4 a dole 4 px, k tomu 1 px papiera vpravo; pravý sken vľavo 2, hore 3 a dole 4 px, vpravo svetlý tieň
# skenera 4 px) a pri pravom skene ešte 6 px (vľavo) a 7 px (vpravo) prázdneho papiera, aby mali oba výrezy pomer strán
# do 0,002 pod vnútrom rámu (520 / 747 = 0,6961, 503 / 722 = 0,6967, rám 0,6970); styl.css ich kreslí cez object-fit cover, papier vypĺňa celé vnútro rámu v oboch. Odstránená plocha nemá
# ornament ani písmo (presnost/nastroje/oprava2_orez_kontrola.py, test_build.py TestOpravnenia), zdroj sa nemení.
OREZ_DOKLADU = {'osvedcenie-sksi-stavbyveduci-2003': (3, 4, 523, 751),
                'osvedcenie-sksi-stavebny-dozor-2010': (8, 3, 511, 725)}
VERZIA_DOKLADU = 'doklad-3|webp-q90-m6|lanczos|zvacsenie-2x-lanczos-usm-1.0-40-2|orez-papier'


def sken_dokladu(p):
    """Zakrytý sken osvedčenia ako RGB, orezaný na hranu papiera podľa OREZ_DOKLADU (bez orezu, keď meno nie je
    v zozname). Výrez musí ležať v skene a mať pomer strán najviac o 0,002 pod vnútrom rámu (nie nad ním, aby cover
    pri 528 px rámu kreslil súbor 1056 px na celú šírku)."""
    im = otvor_rgb(p)
    box = OREZ_DOKLADU.get(Path(p).stem)
    if not box:
        return im
    l, t, r, b = box
    if not (0 <= l < r <= im.width and 0 <= t < b <= im.height):
        raise ChybaZostavy(f'{Path(p).name}: orez {box} mimo skenu {im.width} x {im.height}')
    pomer = (r - l) / (b - t)
    if not (POMER_RAMU_DOKLADU - 0.002 <= pomer <= POMER_RAMU_DOKLADU):
        raise ChybaZostavy(f'{Path(p).name}: orez {box} má pomer strán {pomer:.4f}, rám {POMER_RAMU_DOKLADU:.4f}')
    return im.crop(box)


def sirky_dokladu(w):
    """Šírky súborov dokladu so skenom širokým w px: SIRKY_DOKLADU pod najväčšou šírkou, sken a najväčšia šírka
    ZVACSENIE_DOKLADU x max(w, SIRKA_RAMU_DOKLADU)."""
    hore = ZVACSENIE_DOKLADU * max(w, SIRKA_RAMU_DOKLADU)
    return sorted({s for s in SIRKY_DOKLADU if s != w and s < hore} | {w, hore})


def doklad_webp_bajty(p, sirka, vyska):
    """Zakrytý obrázok osvedčenia ako WebP: rovnaká veľkosť (len prevod formátu), zmenšenie Lanczos, alebo zväčšenie
    Lanczosom s jemným doostrením najviac na ZVACSENIE_DOKLADU x max(sken, SIRKA_RAMU_DOKLADU) (bez AI, nové
    detaily nevznikajú). Sken je najprv orezaný na hranu papiera (sken_dokladu)."""
    im = sken_dokladu(p)
    strop = ZVACSENIE_DOKLADU * max(im.width, SIRKA_RAMU_DOKLADU)
    if sirka > strop or vyska > round(im.height * strop / im.width) + 1:
        raise ChybaZostavy(f'{p.name}: doklad sa nezväčšuje nad {strop} px '
                           f'({im.width} x {im.height} -> {sirka} x {vyska})')
    if sirka > im.width:
        # vždy cez 2-násobok s doostrením (kalibrované), medzišírka (720) jeho zmenšením Lanczos
        polomer, percento, prah = DOOSTRENIE_DOKLADU
        im = im.resize((im.width * ZVACSENIE_DOKLADU, im.height * ZVACSENIE_DOKLADU), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=polomer, percent=percento, threshold=prah))
    if im.size != (sirka, vyska):
        im = im.resize((sirka, vyska), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=90, method=6)
    return buf.getvalue()


def odtlacok_suboru(p, *dalsie):
    h = hashlib.sha256()
    h.update(Path(p).read_bytes())
    for x in dalsie:
        h.update(b'|' + str(x).encode('utf-8'))
    return h.hexdigest()


def kluc_variantu(orez, sirka, fmt):
    """Kľúč súboru v manifeste fotky-web: „<orez>|<šírka>|<formát>“, orez prázdny (celá fotka), 45 alebo 54."""
    return f'{orez or ""}|{int(sirka)}|{fmt}'


class FotkyWeb:
    """Hotové webové verzie fotiek (priečinok fotky-web/, nástroj nastroje/predlohy_web.py). manifest.json:
    {"verzia": 1, "fotky": {<sha256 zdroja z obsah/>: {"rel", "predloha": {"w", "h", ...}, "kvalita": {...},
    "subory": {"<orez>|<šírka>|<formát>": {"subor", "w", "h", "bajty", "sha256"}}}}}. Build použije záznam len vtedy,
    keď sedí sha256 zdroja; súbor len keď existuje a má zapísanú veľkosť (sha256 sa overí pri kopírovaní).
    Pokazený manifest alebo cesta mimo priečinka = záznam sa nepoužije (fotka sa zakóduje z obsahu)."""

    def __init__(self, koren):
        self.koren = Path(koren) if koren else None
        self.fotky = {}
        m = self.koren / 'manifest.json' if self.koren else None
        if m is None or not m.is_file():
            return
        try:
            d = json.loads(m.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            varuj(f'{m} sa nedá prečítať, fotky sa zakódujú z obsahu')
            return
        fotky = d.get('fotky') if isinstance(d, dict) else None
        if isinstance(fotky, dict):
            self.fotky = {k: v for k, v in fotky.items()
                          if isinstance(k, str) and re.match(r'^[0-9a-f]{64}$', k) and isinstance(v, dict)}

    def zaznam(self, sha):
        z = self.fotky.get(sha)
        if not z:
            return None
        p = z.get('predloha') if isinstance(z.get('predloha'), dict) else {}
        try:
            w, h = int(p.get('w')), int(p.get('h'))
        except (TypeError, ValueError):
            return None
        if not (0 < w <= 10000 and 0 < h <= 10000):
            return None
        return z

    def subor(self, sha, orez, sirka, fmt):
        """Záznam súboru {"subor", "w", "h", "bajty", "sha256", "cesta"} alebo None (chýba, mimo priečinka, iná veľkosť)."""
        z = self.zaznam(sha)
        if not z or not isinstance(z.get('subory'), dict):
            return None
        s = z['subory'].get(kluc_variantu(orez, sirka, fmt))
        if not isinstance(s, dict) or not isinstance(s.get('subor'), str):
            return None
        rel = s['subor'].replace('\\', '/')
        if not bezpecna_rel(rel) or not rel.endswith('.' + fmt) or not re.match(r'^[0-9a-f]{64}$', str(s.get('sha256', ''))):
            return None
        p = self.koren / rel
        try:
            if not p.is_file() or p.stat().st_size != int(s.get('bajty', -1)):
                return None
        except (OSError, TypeError, ValueError):
            return None
        return dict(s, cesta=p)

    @staticmethod
    def citaj(s):
        data = Path(s['cesta']).read_bytes()
        if hashlib.sha256(data).hexdigest() != s['sha256']:
            raise ChybaZostavy(f'fotky-web/{s["subor"]}: sha256 nesedí s manifest.json')
        return data


def pismo(vaha, velkost):
    """Arimo (statická inštancia podmnožiny, sablona/arimo-<váha>.ttf) pre OG obrázky; bez súboru None."""
    cesta = SABLONA / f'arimo-{vaha}.ttf'
    if cesta.is_file():
        return ImageFont.truetype(str(cesta), velkost)
    return None


def zalom(draw, text, font, sirka, max_riadkov):
    slova = text.split()
    riadky, akt = [], ''
    for slovo in slova:
        skus = (akt + ' ' + slovo).strip()
        if draw.textlength(skus, font=font) <= sirka or not akt:
            akt = skus
        else:
            riadky.append(akt)
            akt = slovo
    if akt:
        riadky.append(akt)
    if len(riadky) > max_riadkov:
        riadky = riadky[:max_riadkov]
        posledny = riadky[-1]
        while posledny and draw.textlength(posledny + ' …', font=font) > sirka:
            posledny = posledny.rsplit(' ', 1)[0] if ' ' in posledny else posledny[:-1]
        riadky[-1] = posledny + ' …'
    return riadky


# ---------------------------------------------------------------- logo SVG (sablona/logo.svg) na raster

SVG_TOKEN = re.compile(r'[MLHVQCZmlhvqcz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
SVG_ATRIBUT = re.compile(r'([a-zA-Z-]+)="([^"]*)"')


def svg_podcesty(d):
    """Podcesty cesty SVG ako zoznamy bodov. Logo z logo_svg.py má len absolútne M, L, H, V, Q, C a Z; krivky sa delia
    na 12 úsečiek (pri 4-násobnom nadvzorkovaní hladké aj pri 512 px)."""
    tok = SVG_TOKEN.findall(d)
    i, prikaz = 0, None
    cesty, akt = [], []
    x = y = sx = sy = 0.0

    def cislo():
        nonlocal i
        v = float(tok[i])
        i += 1
        return v
    while i < len(tok):
        if tok[i].isalpha():
            prikaz = tok[i]
            i += 1
            if prikaz in 'Zz':
                if akt:
                    cesty.append(akt)
                akt = []
                x, y = sx, sy
            continue
        if prikaz == 'M':
            x, y = cislo(), cislo()
            if akt:
                cesty.append(akt)
            akt = [(x, y)]
            sx, sy = x, y
            prikaz = 'L'
        elif prikaz == 'L':
            x, y = cislo(), cislo()
            akt.append((x, y))
        elif prikaz == 'H':
            x = cislo()
            akt.append((x, y))
        elif prikaz == 'V':
            y = cislo()
            akt.append((x, y))
        elif prikaz == 'Q':
            x1, y1, x2, y2 = cislo(), cislo(), cislo(), cislo()
            for k in range(1, 13):
                t = k / 12
                akt.append(((1 - t) ** 2 * x + 2 * (1 - t) * t * x1 + t * t * x2,
                            (1 - t) ** 2 * y + 2 * (1 - t) * t * y1 + t * t * y2))
            x, y = x2, y2
        elif prikaz == 'C':
            x1, y1, x2, y2, x3, y3 = (cislo() for _ in range(6))
            for k in range(1, 13):
                t = k / 12
                u = 1 - t
                akt.append((u ** 3 * x + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                            u ** 3 * y + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
            x, y = x3, y3
        else:
            raise ValueError(f'príkaz cesty SVG {prikaz!r} nie je podporený')
    if akt:
        cesty.append(akt)
    return cesty


def svg_obraz(svg, velkost):
    """Raster loga (štvorcový viewBox, prvky rect a path s výplňou) vo veľkosti velkost x velkost, RGB. Podcesty sa
    skladajú pravidlom párnosti (XOR), čo pri písmenách loga dáva to isté ako nonzero (obrysy sa neprekrývajú)."""
    vb = re.search(r'viewBox="([\d.\s-]+)"', svg)
    x0, y0, vw, vh = (float(v) for v in vb.group(1).split()) if vb else (0.0, 0.0, 936.0, 936.0)
    n = 4
    s = velkost * n / vw
    platno = Image.new('RGB', (velkost * n, round(vh * s)), FARBA_BIELA)
    for prvok, atributy in re.findall(r'<(rect|path)\s([^>]*?)/?>', svg):
        a = dict(SVG_ATRIBUT.findall(atributy))
        farba = ImageColor.getrgb(a.get('fill', '#000'))
        maska = Image.new('1', platno.size, 0)
        if prvok == 'rect':
            rx, ry = float(a.get('x', 0)) - x0, float(a.get('y', 0)) - y0
            ImageDraw.Draw(maska).rectangle((rx * s, ry * s, (rx + float(a['width'])) * s - 1,
                                             (ry + float(a['height'])) * s - 1), fill=1)
        else:
            for body in svg_podcesty(a.get('d', '')):
                if len(body) < 3:
                    continue
                m = Image.new('1', platno.size, 0)
                ImageDraw.Draw(m).polygon([((bx - x0) * s, (by - y0) * s) for bx, by in body], fill=1)
                maska = ImageChops.logical_xor(maska, m)
        platno.paste(farba, (0, 0), maska.convert('L'))
    return platno.resize((velkost, round(vh * velkost / vw)), Image.LANCZOS)


def og_bajty(foto, nadpis, riadok2, riadok3, logo_svg):
    """OG obrázok 1200 x 630 v značke: modrá #17476f, logo s bielym rámom (na modrej by splynulo, LOGO.md bod 5),
    nadpis tučne veľkými písmenami ako na banneri, pod ním biela linka z loga a menší riadok veľkými písmenami;
    s fotkou stavby vpravo (orez na výšku, nikdy nie zväčšená nad 1,25 násobok)."""
    platno = Image.new('RGB', (OG_W, OG_H), FARBA_BM)
    draw = ImageDraw.Draw(platno)
    x0, hore = 64, 56
    sirka = OG_W - 2 * x0
    if foto is not None:
        im = otvor_rgb(foto)
        k = max(480 / im.width, OG_H / im.height)
        if k <= 1.25:
            im = ImageOps.fit(im, (480, OG_H), Image.LANCZOS, centering=(0.5, 0.5))
            platno.paste(im, (OG_W - 480, 0))
            sirka = OG_W - 480 - x0 - 56
    logo_v = 132
    draw.rectangle((x0 - 4, hore - 4, x0 + logo_v + 3, hore + logo_v + 3), fill=FARBA_BIELA)
    platno.paste(svg_obraz(logo_svg, logo_v), (x0, hore))
    f_nadpis = pismo(700, 60)
    if f_nadpis is None:
        buf = io.BytesIO()
        platno.save(buf, 'JPEG', quality=82, optimize=True)
        return buf.getvalue()
    text = nadpis.upper()
    for velkost, riadkov in ((60, 3), (50, 4), (42, 5)):
        f_nadpis = pismo(700, velkost)
        riadky = zalom(draw, text, f_nadpis, sirka, 99)
        if len(riadky) <= riadkov:
            break
    riadky = zalom(draw, text, f_nadpis, sirka, riadkov)
    y = hore + logo_v + 52
    max_sirka = 0
    for r in riadky:
        draw.text((x0, y), r, font=f_nadpis, fill=FARBA_BIELA)
        max_sirka = max(max_sirka, draw.textlength(r, font=f_nadpis))
        y += round(f_nadpis.size * 1.06)
    # biela linka z loga: hrúbka 1,65 % strany loga, šírka po koniec najdlhšieho riadku (ako linka pod BM)
    y += 14
    draw.rectangle((x0, y, x0 + round(max_sirka), y + 6), fill=FARBA_BIELA)
    y += 6 + 18
    if riadok2:
        f2 = pismo(700, 22)
        for r in zalom(draw, riadok2.upper(), f2, sirka, 2):
            draw.text((x0, y), r, font=f2, fill=FARBA_BM_200)
            y += 30
    if riadok3:
        draw.text((x0, OG_H - hore - 26), riadok3, font=pismo(400, 26), fill=FARBA_BIELA)
    buf = io.BytesIO()
    platno.save(buf, 'JPEG', quality=82, optimize=True)
    return buf.getvalue()


def png_bajty(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- HTML

# sizes podľa rozloženia v styl.css (mriežka 4, 8 a 12 stĺpcov ako v ukážke): obsah najviac 1760 px do 1920 px okna,
# od 1921 px (úsek ŠIROKÉ OKNO, Andrej 25. 11. 2026: „hluché miesto vpravo“ pri 2560 px) rastie s oknom s okrajom
# 80 px, min(100vw - 160px, 2400px). Každý sizes má pre široké okno vlastnú prvú položku (min-width: 1921px), ostatné
# položky sú tie isté ako do 1920 px, takže pri užšom okne prehliadač vyberá rovnaký súbor ako predtým. Vzorce
# širokého okna sú horný odhad (posuvník 17 px ubere z obsahu 0 px, z okrajov 8,5 px): obsah C = vw - 160, stĺpec
# (C - 11 x 24) / 12, n stĺpcov n x C / 12 - (12 - n) x 2 px. Merania hustoty pred a po:
# ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/SIROKE.md
SIROKE_OKNO = 1921
OBSAH_MAX = 1760
OBSAH_SIROKY_MAX = 2400
OKRAJ_SIROKY = 80


def sirka_obsahu_px(vw):
    """Šírka obsahu (.obsah, mriežka 12 stĺpcov) v CSS px pri okne vw bez posuvníka podľa styl.css: --sirka 1760 px,
    od 1921 px min(100vw - 160px, 2400px); najviac okno mínus dva okraje clamp(16px, 3.9vw, 64px)."""
    okraj = min(64.0, max(16.0, 0.039 * vw))
    sirka = float(OBSAH_MAX) if vw < SIROKE_OKNO else min(vw - 2.0 * OKRAJ_SIROKY, float(OBSAH_SIROKY_MAX))
    return min(sirka, vw - 2 * okraj)


# karty zoznamu 4 v rade od 1024 px, 3 od 640 px, pod tým riadok s náhľadom 38 % šírky; pás 30 vw; fotka úvodu
# 7 z 12 stĺpcov; galéria 3 v rade (prvá cez 2), na mobile 2 v rade
# svetlá karta 4 : 5 v mriežke (zoznam realizácií, služby, novinky; Stavba 2): 1 stĺpec do 479 px, 2 od 480, 3 od 640,
# 4 od 1024 px (obsah 1760 px: (1760 - 3 x 24) / 4 = 422; od 1921 px (C - 72) / 4 = 25vw - 58px, pri 2560 px 582)
SIZES_KARTA = ('(min-width: 1921px) min(25vw - 58px, 582px), (min-width: 1024px) min(23vw, 425px), '
               '(min-width: 640px) 31vw, (min-width: 480px) 47vw, calc(100vw - 32px)')
# fotka v dvojstrane detailu realizácie a novinky: 7 z 12 stĺpcov od 1024 px (do 1920 px najviac 1000 px, styl.css
# max-width), od 1921 px celých 7 stĺpcov bez stropu: 7 x C / 12 - 10 = 58.34vw - 103px, pri 2560 px 1390 px
SIZES_DETAIL = '(min-width: 1921px) min(58.34vw - 103px, 1390px), (min-width: 1024px) min(56vw, 1000px), calc(100vw - 32px)'
# detail realizácie s údajmi pri fotke (trieda detail-s-udajmi): pri 1024 až 1279 px fotka cez celú šírku obsahu pod H1
# (obsah = okno mínus 2 x okraj 3,9vw, pri 1024 px 944,1 px, pri 1279 px 1179,2 px; sizes o 0,9 až 21,8 px viac, s posuvníkom o 17 px viac;
# oprava po overení, kontrola dizajnu nález vážne), od 1280 px 7 z 12 stĺpcov ako SIZES_DETAIL
SIZES_DETAIL_UDAJE = ('(min-width: 1921px) min(58.34vw - 103px, 1390px), (min-width: 1280px) min(56vw, 1000px), '
                      '(min-width: 1024px) calc(100vw - 78px), calc(100vw - 32px)')
# slider úvodu (3A.2): pole od 1024 px má 7 z 12 stĺpcov s presahom k ľavému okraju okna a výšku okna pod hlavičkou
# (krasa-8: 100vh mínus hlavička 84 px, pri výške okna do 820 px 64 px; najmenej 480 px, najviac 75vw), od 640 px rám
# 1 : 1 a mobil 4 : 5 na celú šírku okna. Šírka poľa je vw / 2 + obsah / 12 - 5 / 12 medzery mriežky, zmerané v Chrome
# 729,8 px pri 1280, 778,8 pri 1366, 821 pri 1440, 876 pri 1536 a 1096,7 pri 1920 px, od 1921 px (obsah vw - 160)
# 7 x vw / 12 - 23,3: 1171,3 pri 2048 a 1470 pri 2560 px. Zhora ju drží min(57.7vw, max(50vw + 137px, 58.34vw -
# 23.4px), 50vw + 190px): do 1920 px je max(...) vždy 50vw + 137px, teda ten istý výraz ako predtým (nad 1888 px má
# obsah 1760 px), 50vw + 190px drží odhad pri 2559 a 2560 px. Od 2561 px (SLIDER_BEZ_PRESAHU, styl.css) je obsah na
# strope 2400 px a slider začína na čiare 1. stĺpca obsahu bez presahu (overenie širokého rozloženia, nález vážne:
# pri 3440 px ostával vpravo prázdny pás 511,5 px), pole má 7 stĺpcov obsahu, 1390 px (SLIDER_POLE_BEZ_PRESAHU),
# a sizes má pre to vlastné položky (min-width: 2561px) pred ostatnými. object-fit: cover kreslí fotku širšiu než
# pole cez celú výšku poľa, takže fotka je široká max(šírka poľa, výška poľa x pomer fotky) a sizes ráta build pre
# každú fotku z jej pomeru (sizes_slidera). krasa-8, agent Fotky slidera (brána 3: jeden sizes pre všetky fotky rátal s 4 : 3,
# fotky 16 : 9 mali pri 1366 x 768 hustotu 0,77, pri 1920 x 1080 0,81, fotky na výšku brali zbytočne veľký súbor):
# ops/weby-na-mieru/klienti/bmbuilding/kolo-1/brana-3/OPRAVA-FOTKY.md
SLIDER_SIRKA_POLA = 'min(57.7vw, max(50vw + 137px, 58.34vw - 23.4px), 50vw + 190px)'
# od tejto šírky okna slider bez presahu k ľavému okraju okna (styl.css @media (min-width:2561px)); pole 7 stĺpcov
# obsahu 2400 px s medzerou mriežky 24 px: 7 x 2400 / 12 - 5 x 24 / 12 = 1390 px
SLIDER_BEZ_PRESAHU = 2561
SLIDER_POLE_BEZ_PRESAHU = round(7 * OBSAH_SIROKY_MAX / 12 - 5 * 24 / 12)
# výška poľa: hlavička nad 820 px výšky okna a pod ňou (styl.css --hore), najmenšia výška a strop vo vw
SLIDER_HLAVICKA = 84
SLIDER_HLAVICKA_NIZKE = 64
SLIDER_NIZKE_OKNO = 820
SLIDER_MIN_VYSKA = 480
SLIDER_MAX_VYSKA_VW = 75
# od 1080 px výšky okna je v pravom stĺpci hrdinu aj prvý odsek O firme a pole je najmenej také vysoké ako stĺpec
# s okrajmi mriežky: Chrome zmeral 882 až 974 px pri šírke okna 1024 až 2560 px (krasa-8, Fotky slidera,
# D:/Temp/.../krasa-8/fotky/pole-okna*.json, doladenie-2 meranie-sweep); sizes ráta s horným odhadom. Prah je ten
# istý ako v styl.css (max-height:1079px a min-height:1080px): pole so stĺpcom potrebuje okno najviac 974 + 84 =
# 1058 px (1648 px šírky), pri prahu 960 px stĺpec natiahol fotku pod spodok okna (doladenie-2, nález vážne);
# 1080 nechá rezervu 22 px a 1920 x 1080 ostáva vysoké okno. Pri zmene textov hrdinu premerať.
SLIDER_VYSOKE_OKNO = 1079
SLIDER_STLPEC = 980
# výška rámu na mobile (4 : 5, do 639 px) a tablete (1 : 1, 640 až 1023 px) ako násobok šírky okna
SLIDER_RAM_MOBIL = 1.25
SLIDER_RAM_TABLET = 1.0


def _cislo(x, des=2):
    """Násobok do sizes zaokrúhlený na des desatinných miest (chyba pod 0,01 %). Nie nahor: fotka 16 : 9 na mobile
    360 px pri hustote 3 je široká presne 2400 px obrazovky a zaokrúhlenie nahor by prehliadač poslalo po súbor
    2844 px (o 219 kB väčší pri Hrnčiarovciach) kvôli 0,1 px."""
    return round(x, des) + 0.0


def pomer_slidera(w, h, orez=0.0):
    """Šírka obrázka slidera k výške poľa: pomer fotky, pri orez_dole delený (1 - orez), lebo obrázok je o tú časť
    vyšší než pole (styl.css .snimka-N img height)."""
    return (w / h) / (1 - orez)


def _nasobky_slidera(w, h, orez=0.0):
    """Čísla do sizes_slidera: k x 100 (vh), k x hlavička 64 a 84 px, k x 480 px, k x 75 (vw), k x stĺpec hrdinu
    (px), tablet a mobil (vw)."""
    k = pomer_slidera(w, h, orez)
    return (_cislo(k * 100), round(k * SLIDER_HLAVICKA_NIZKE), round(k * SLIDER_HLAVICKA), round(k * SLIDER_MIN_VYSKA),
            _cislo(k * SLIDER_MAX_VYSKA_VW), round(k * SLIDER_STLPEC), _cislo(100 * max(1.0, k * SLIDER_RAM_TABLET)),
            _cislo(100 * max(1.0, k * SLIDER_RAM_MOBIL)))


def sizes_slidera(w, h, orez=0.0):
    """sizes fotky slidera w x h (orez = orez_dole z nastavení) pri object-fit: cover v poli slidera: na počítači
    max(šírka poľa, k x výška poľa), na tablete k x 100vw a na mobile 1,25 k x 100vw (k = pomer_slidera), najmenej
    šírka poľa. Výška poľa podľa výšky okna: do 820 px 100vh - 64 px (najmenej 480 px), do 1079 px 100vh - 84 px
    (najviac 75vw), od 1080 px navyše najmenej SLIDER_STLPEC (pravý stĺpec s odsekom O firme). Násobky na stotiny
    a px na celé (od fotky po cover sa líši najviac o 0,5 px, šírka poľa je horný odhad), takže prehliadač vyberie
    súbor aspoň taký široký, ako je fotka na obrazovke. Od SLIDER_BEZ_PRESAHU (2561 px) majú prednosť dve položky
    s pevnou šírkou poľa SLIDER_POLE_BEZ_PRESAHU (slider bez presahu, 1390 px): nízke okno ako na počítači a jedna
    pre ostatné výšky, lebo od 821 px výšky okna je pole vysoké 100vh - 84 px (najviac 75vw, pri tejto šírke aspoň
    1920 px) a od 1080 px už aspoň 996 px, viac než pravý stĺpec SLIDER_STLPEC, ktorý tu preto nerozhoduje."""
    vh, h64, h84, v480, vw75, stlpec, tablet, mobil = _nasobky_slidera(w, h, orez)
    bez, pole = SLIDER_BEZ_PRESAHU, SLIDER_POLE_BEZ_PRESAHU
    bez_nizke = f'(min-width: {bez}px) and (max-height: {SLIDER_NIZKE_OKNO}px) max({pole}px, {vh:g}vh - {h64}px, {v480}px)'
    bez_ostatne = f'(min-width: {bez}px) max({pole}px, min({vh:g}vh - {h84}px, {vw75:g}vw))'
    nizke = (f'(min-width: 1024px) and (max-height: {SLIDER_NIZKE_OKNO}px) max({SLIDER_SIRKA_POLA}, '
             f'{vh:g}vh - {h64}px, {v480}px)')
    stredne = (f'(min-width: 1024px) and (max-height: {SLIDER_VYSOKE_OKNO}px) max({SLIDER_SIRKA_POLA}, '
               f'min({vh:g}vh - {h84}px, {vw75:g}vw))')
    vysoke = f'(min-width: 1024px) max({SLIDER_SIRKA_POLA}, min({vh:g}vh - {h84}px, {vw75:g}vw), {stlpec}px)'
    return ', '.join((bez_nizke, bez_ostatne, nizke, stredne, vysoke, f'(min-width: 640px) {tablet:g}vw', f'{mobil:g}vw'))


def sizes_slidera_px(w, h, orez, vw, vh):
    """Dĺžka, ktorú prehliadač vypočíta zo sizes_slidera pri okne vw x vh CSS px (tie isté čísla ako v reťazci),
    pre testy a merania."""
    kvh, h64, h84, v480, vw75, stlpec, tablet, mobil = _nasobky_slidera(w, h, orez)
    if vw >= SLIDER_BEZ_PRESAHU:
        if vh <= SLIDER_NIZKE_OKNO:
            return max(SLIDER_POLE_BEZ_PRESAHU, kvh * vh / 100 - h64, v480)
        return max(SLIDER_POLE_BEZ_PRESAHU, min(kvh * vh / 100 - h84, vw75 * vw / 100))
    if vw >= 1024:
        sirka = min(0.577 * vw, max(0.5 * vw + 137, 0.5834 * vw - 23.4), 0.5 * vw + 190)
        if vh <= SLIDER_NIZKE_OKNO:
            return max(sirka, kvh * vh / 100 - h64, v480)
        vyska = min(kvh * vh / 100 - h84, vw75 * vw / 100)
        return max(sirka, vyska) if vh <= SLIDER_VYSOKE_OKNO else max(sirka, vyska, stlpec)
    return (tablet if vw >= 640 else mobil) * vw / 100


def pole_slidera_px(vw, vh):
    """Pole slidera (šírka, výška) v CSS px podľa rozloženia v styl.css pri okne vw x vh bez posuvníka: od 1024 px
    šírka 7 z 12 stĺpcov obsahu (7 x obsah / 12 - 5 / 12 medzery mriežky, obsah sirka_obsahu_px: 1760 px, od 1921 px
    vw - 160 px, najviac 2400 px) a presah k ľavému okraju okna (vw - obsah) / 2, spolu vw / 2 + obsah / 12 - 5 / 12
    medzery; od SLIDER_BEZ_PRESAHU (2561 px) bez presahu, 1390 px. Výška clamp(480px, 100vh - hlavička, 75vw),
    tablet 1 : 1, mobil 4 : 5 na celú šírku okna. Od 1080 px výšky okna je pole navyše najmenej také vysoké ako pravý
    stĺpec hrdinu (SLIDER_STLPEC), to tu nie je: rozhoduje pri úzkych vysokých oknách (1024 x 1366)."""
    if vw >= 1024:
        medzera = min(24.0, max(12.0, 0.016 * vw))
        obsah = sirka_obsahu_px(vw)
        hore = SLIDER_HLAVICKA_NIZKE if vh <= SLIDER_NIZKE_OKNO else SLIDER_HLAVICKA
        presah = 0.0 if vw >= SLIDER_BEZ_PRESAHU else (vw - obsah) / 2
        return presah + 7 * obsah / 12 - 5 * medzera / 12, max(float(SLIDER_MIN_VYSKA), min(vh - hore, SLIDER_MAX_VYSKA_VW * vw / 100))
    return float(vw), vw * (SLIDER_RAM_TABLET if vw >= 640 else SLIDER_RAM_MOBIL)


def sirka_slidera_px(w, h, orez, vw, vh):
    """Šírka fotky slidera po object-fit: cover v CSS px: max(šírka poľa, pomer_slidera x výška poľa)."""
    sirka, vyska = pole_slidera_px(vw, vh)
    return max(sirka, pomer_slidera(w, h, orez) * vyska)
# fotka slidera širšia než SIRKY[-1] x SIRKA_BLIZKO (predloha 16 : 9, 2844 px) má v AVIF navyše súbor v plnej šírke
# predlohy: pole na tablete a mobile je 1 : 1 a 4 : 5, takže fotka 16 : 9 je pri hustote 2 až 3 široká 2600 až 2900 px
# obrazovky (768 x 1024: 2731 px, 390 x 844: 2600 px, 1440 x 900 pri hustote 2: 2901 px, 2560 x 1440: 2411 px)
SLIDER_MAX_SIRKA = 4000
# fotka slidera má v AVIF navyše šírku 840 (audit naplno 25. 11. 2026, položka 43): telefón 390 až 412 px s hustotou
# 1,75 (Lighthouse mobil) kreslí prvú fotku (RD Ahoj 01, rám 4 : 5, 113,4vw) na 774 až 817 px obrazovky a medzi 720
# a 960 nebolo nič, bral 960w (181 219 B). Tablet 640 až 840 px s hustotou 1 rovnako. Len slider (LCP úvodu), karty
# a galéria ostávajú
SLIDER_SIRKA_NAVYSE = 840
# druh stavby na karte zoznamu v jednotnom čísle: prvá zhoda v názve, potom v texte realizácie. Rekonštrukcia
# a stavebné úpravy nie sú druh stavby (ZMENY-KOLO-1.md 5.1, „bez rekonštrukcie“), karta ukáže druh budovy.
# Ten istý druh má detail pod popiskom Druh stavby (audit naplno 25. 11. 2026, položka 2). Apartmánový dom pred
# Bytovým domom: Ventúrska („Apartmánový dom na Ventúrskej ulici“) bola bez druhu, Betliarska („18 bytov a 35
# apartmánov“) ostáva Bytový dom
DRUHY_KARTY = (('Polyfunkčný objekt', r'polyfunkčn'), ('Rezidencia', r'rezidenci'), ('Bytové domy', r'bytové domy'),
               ('Apartmánový dom', r'apartmánový dom'), ('Bytový dom', r'bytov'), ('Dvojdom', r'dvojdom'),
               ('Vila', r'\bvil[ay]\b'), ('Rodinný dom', r'(^|\s)RD\b|rodinn'))
# pohľad A cez 7 z 12 stĺpcov (rovnako ako SIZES_DETAIL; teraz bez použitia, úvod má slider)
SIZES_HRDINA = ('(min-width:1921px) min(58.34vw - 103px, 1390px), (min-width:1024px) min(56vw, 1000px), '
                '(min-width:768px) 92vw, calc(100vw - 32px)')
# galéria (bunka 4 z 12 stĺpcov, 3 v rade): prvá veľká cez 2 bunky (8 stĺpcov, od 1921 px 2 x C / 3 - 8 =
# 66.67vw - 114px, pri 2560 px 1592 px), bunka mriežky od 1921 px C / 3 - 16 = 33.34vw - 69px (pri 2560 px 784 px),
# galéria do 3 fotiek (galeria-malo) 2 v rade, od 1921 px C / 2 - 12 = 50vw - 92px (pri 2560 px 1188 px); do 1920 px
# má galeria-malo ten istý sizes ako prvá veľká
SIZES_GALERIA_PRVA = '(min-width:1921px) min(66.67vw - 114px, 1592px), (min-width:1024px) min(62vw, 1150px), calc(100vw - 32px)'
SIZES_GALERIA_MALO = '(min-width:1921px) min(50vw - 92px, 1188px), (min-width:1024px) min(62vw, 1150px), calc(100vw - 32px)'
SIZES_GALERIA = '(min-width:1921px) min(33.34vw - 69px, 784px), (min-width:640px) min(30vw, 570px), 46vw'
# rám pohľadu A detailu a bunka galérie majú pomer 3 : 2 (styl.css aspect-ratio:3/2; zmerané v
# ops/weby-na-mieru/klienti/bmbuilding/presnost/meranie/fotky-pred.md: 358 x 239, 765 x 510, 1017 x 678 a 427 x 285
# CSS px); object-fit: cover fotky širšej než rám ukáže len časť jej šírky (sizes_pre_cover)
POMER_RAMU_COVER = 1.5


def _casti_sizes(sizes):
    """Položky atribútu sizes rozdelené čiarkami mimo zátvoriek (min(30vw, 570px) ostane celé)."""
    out, hlbka, kus = [], 0, ''
    for z in sizes:
        if z == ',' and hlbka == 0:
            out.append(kus.strip())
            kus = ''
            continue
        hlbka += (z == '(') - (z == ')')
        kus += z
    out.append(kus.strip())
    return [x for x in out if x]


def sizes_pre_cover(sizes, w, h, ram=POMER_RAMU_COVER):
    """sizes pre fotku w x h v ráme s pomerom ram (šírka / výška) pri object-fit: cover. Fotka širšia než rám je
    orezaná po stranách, takže prehliadač potrebuje súbor širší o k = (w / h) / ram; bez toho vyberie súbor podľa šírky
    boxu, ktorý je na výšku malý, a zväčší ho (fotky-pred.md časť 9: Devín 01 z_súbor 1,55 pri 390 px). Každá dĺžka sa
    vynásobí k (zaokrúhlené nahor na stotiny) cez calc(); užšia alebo rovnako široká fotka má sizes bez zmeny."""
    if not w or not h:
        return sizes
    k = (w / h) / ram
    if k <= 1.005:
        return sizes
    k = -(-round(k * 1000) // 10) / 100
    out = []
    for kus in _casti_sizes(sizes):
        media, dlzka = '', kus
        if kus.startswith('('):
            hlbka = 0
            for i, z in enumerate(kus):
                hlbka += (z == '(') - (z == ')')
                if hlbka == 0:
                    media, dlzka = kus[:i + 1] + ' ', kus[i + 1:].strip()
                    break
        vnutro = dlzka[5:-1] if dlzka.startswith('calc(') and dlzka.endswith(')') else dlzka
        out.append(f'{media}calc(({vnutro}) * {k:g})' if ' ' in vnutro.strip() and not vnutro.startswith(('min(', 'max('))
                   else f'{media}calc({vnutro} * {k:g})')
    return ', '.join(out)
# najväčšie bunky galérie v CSS px pri obsahu 1760 px (okno do 1920 px; styl.css: --sirka 1760 px, medzera 24 px,
# bunka 3 : 2): mriežka 3 stĺpce, galeria-malo 2 stĺpce, prvá veľká 2 stĺpce a 2 riadky. Fotka, ktorú by
# object-fit:cover v takej bunke zväčšil nad 1,25 násobok súboru, je v galérii „malá“: bez srcset a s
# object-fit:scale-down, nikdy väčšia než súbor (oprava po bráne, bod 1: vizualizácie UFO 320 x 179 px; rovnaké pravidlo
# ako lightbox v app.js a OG). Od 1921 px bunky rastú s obsahom (pri 2560 px 784 x 523, 1188 x 792 a 1592 x 1069 px);
# prah ostáva pri 1760 px, aby sa rozloženie galérie do 1920 px nezmenilo: malá fotka sa ani na širokom okne
# nezväčší (scale-down), ostatné berú zo srcset súbor podľa sizes, najviac plnú šírku predlohy (SIROKE.md)
GALERIA_BUNKY = {'mriezka': (571, 381), 'malo': (868, 579), 'prva': (1165, 784)}
ZVACSENIE_MAX = 1.25
SIZES_PRED_PO = '(min-width:768px) 46vw, calc(100vw - 32px)'
# blok V médiách na stránke realizácie (ZMENY-KOLO-1.md 5.7): výrez tabuľky (krasa-8, brána 3: pri 1440 px 765 x
# 720 px, prevalcoval stránku) 5 z 12 stĺpcov od 1024 px (1440 px: 540 px), od 1536 px 4 z 12 (1920 px: 571 px),
# na tablete 5 z 8, na mobile celá šírka; plná veľkosť v lightboxe. Fotky strán po dvoch v pravom stĺpci (6 z 12
# stĺpcov: pri 1440 px 314 px, pri 1920 px 422 px, teda asi 22vw). Od 1921 px (styl.css úsek ŠIROKÉ OKNO) výrez
# 3 stĺpce C / 4 - 18 = 25vw - 58px (pri 2560 px 582 px, písmená tabuľky ako pri 1920 px) a text 6 stĺpcov od čiary
# 5. stĺpca, fotka strany (C / 2 - 36) / 2 = 25vw - 58px (pri 2560 px 582 px, nie väčšia než výrez).
SIZES_MEDIA_TABULKA = ('(min-width:1921px) min(25vw - 58px, 582px), (min-width:1536px) min(30vw, 580px), '
                       '(min-width:1024px) 38vw, (min-width:768px) 58vw, calc(100vw - 32px)')
SIZES_MEDIA_STRANA = '(min-width:1921px) min(25vw - 58px, 582px), (min-width:1024px) min(22vw, 430px), calc(50vw - 24px)'


def nacitaj_pravidla():
    """sablona/fotky-pravidla.json; chýbajúci alebo pokazený súbor dá predvolené prahy a žiadny pár pred a po."""
    p = SABLONA / 'fotky-pravidla.json'
    d = nacitaj_json(p) if p.is_file() else {}
    d = d if isinstance(d, dict) else {}

    def prahy(kluc, sirka, ostrost):
        x = d.get(kluc) if isinstance(d.get(kluc), dict) else {}
        try:
            return {'min_sirka': int(x.get('min_sirka', sirka)), 'min_ostrost': float(x.get('min_ostrost', ostrost))}
        except (TypeError, ValueError):
            return {'min_sirka': sirka, 'min_ostrost': ostrost}
    try:
        prah = float(d.get('prah_rozmazana', 80))
    except (TypeError, ValueError):
        prah = 80.0
    # vizualizácie (malé rendery z príspevkov) majú vlastný prah, predvolene 0: vždy v galérii (oprava po bráne, bod 1)
    try:
        prah_viz = float(d.get('prah_rozmazana_vizualizacia', 0))
    except (TypeError, ValueError):
        prah_viz = 0.0
    # ilustračné fotky kariet noviniek bez vlastnej fotky (oprava po bráne, bod 2): slug novinky -> slug a súbor
    ilustracne = {k: v for k, v in (d.get('ilustracne_fotky') or {}).items()
                  if isinstance(k, str) and isinstance(v, dict)} if isinstance(d.get('ilustracne_fotky'), dict) else {}
    predvolena = d.get('ilustracna_predvolena') if isinstance(d.get('ilustracna_predvolena'), dict) else None
    try:
        interier = re.compile(str(d.get('interier_nazov') or 'ambulanc|klub|interi[eé]r'), re.I)
    except re.error:
        interier = re.compile('ambulanc|klub|interi[eé]r', re.I)
    poradie = [t for t in (d.get('poradie_typov') or TYPY_FOTIEK) if t in TYPY_FOTIEK]
    typy = {k: v for k, v in (d.get('typy') or {}).items() if isinstance(k, str) and v in TYPY_FOTIEK}
    pary = [x for x in (d.get('pary_pred_po') or []) if isinstance(x, dict)]
    return {'prah': prah, 'prah_viz': prah_viz, 'obal': prahy('obal', 640, 350), 'odporucany': prahy('odporucany_obal', 960, 500),
            'velka': prahy('velka', 1280, 800), 'interier': interier, 'poradie': poradie or list(TYPY_FOTIEK),
            'typy': typy, 'pary': pary, 'ilustracne': ilustracne, 'ilustracna_predvolena': predvolena}


def zhusti_js(text):
    """app.js pre web bez komentárov a odsadenia (audit naplno 25. 11. 2026, položka 45: Lighthouse unminified-javascript,
    súbor sa kopíroval bez úpravy). Zmaže úvodný blokový komentár, celé riadky s komentárom //, odsadenie a prázdne
    riadky; riadky kódu ostanú v tom istom poradí, každý na svojom riadku (nič sa nespája, takže sa nemení ani
    automatické vkladanie bodkočiarky). Bez novej závislosti, deterministicky. Bezpečné, lebo app.js nemá šablónové
    reťazce ani pokračovanie reťazca cez koniec riadka (test_build.py to stráži); keby ich mal, súbor ide bez zmeny
    a s varovaním. Komentár za kódom na tom istom riadku ostane (app.js taký nemá)."""
    t = text.replace('\r\n', '\n').lstrip('﻿')
    if '`' in t or re.search(r'\\\n', t):
        varuj('app.js má šablónový reťazec alebo pokračovanie riadka, ide na web bez zhustenia')
        return text
    t = re.sub(r'\A\s*/\*.*?\*/', '', t, flags=re.S)
    riadky = (r.strip() for r in t.split('\n'))
    return '\n'.join(r for r in riadky if r and not r.startswith('//')) + '\n'


class _TextMono(HTMLParser):
    """Znaky, ktoré stránka kreslí písmom BMB Mono podľa MONO_TRIEDY, MONO_POTOMOK a MONO_SANS (bez script a style);
    stĺpec valca kóty (MONO_STLPEC) kreslí číslice 0 až 9 cez CSS content. Medzery sa nerátajú."""
    PRAZDNE = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.zasobnik, self.znaky, self._skryte = [], set(), 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template'):
            self._skryte += 1
        if tag in self.PRAZDNE:
            return
        triedy = set((dict(attrs).get('class') or '').split())
        mono = self.zasobnik[-1][2] if self.zasobnik else False
        if triedy & set(MONO_TRIEDY) or any(tag == z and any(t in x[1] for x in self.zasobnik) for t, z in MONO_POTOMOK):
            mono = True
        if triedy & set(MONO_SANS):
            mono = False
        if mono and MONO_STLPEC in triedy and not self._skryte:
            self.znaky.update('0123456789')
        self.zasobnik.append((tag, triedy, mono))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.PRAZDNE:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'template') and self._skryte:
            self._skryte -= 1
        for i in range(len(self.zasobnik) - 1, -1, -1):
            if self.zasobnik[i][0] == tag:
                del self.zasobnik[i:]
                break

    def handle_data(self, data):
        if not self._skryte and self.zasobnik and self.zasobnik[-1][2]:
            self.znaky.update(c for c in data if not c.isspace())


def znaky_mono(html_text):
    """Množina znakov (bez medzier), ktoré HTML stránky kreslí písmom BMB Mono (_TextMono)."""
    p = _TextMono()
    p.feed(html_text)
    p.close()
    return p.znaky


class _TextStranky(HTMLParser):
    """Viditeľný text stránky (mimo script a style) a texty alt, aria-label a title na kontrolu znakov písma."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texty, self._skryte = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skryte += 1
        for k, v in attrs:
            if v and k in ('alt', 'aria-label', 'title', 'data-nazov', 'data-popis', 'data-alt', 'data-autor'):
                self.texty.append(v)

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skryte:
            self._skryte -= 1

    def handle_data(self, data):
        if not self._skryte:
            self.texty.append(data)


class Web:
    def __init__(self, obsah, nahlad, fotky_web=None, predlohy=None, hosting='pages'):
        """fotky_web: priečinok hotových webových verzií (predvolený FOTKY_WEB). predlohy: {sha256 zdroja: (w, h)}
        namiesto manifestu, len pre nastroje/predlohy_web.py (plán súborov z predlôh, ktoré ešte nie sú zakódované).
        hosting: 'pages' (GitHub Pages) alebo 'apache' (WebHouse cez FTP, len ostrý web), HOSTINGY."""
        if hosting not in HOSTINGY:
            raise ChybaZostavy(f'hosting {hosting!r} nepoznám, povolené: {", ".join(HOSTINGY)}')
        if hosting == 'apache' and nahlad:
            raise ChybaZostavy('hosting apache je len pre ostrý web na doméne, nie pre náhľad')
        self.obsah = obsah
        self.nahlad = nahlad
        self.hosting = hosting
        self.nastavenia = json.loads((SABLONA / 'nastavenia.json').read_text(encoding='utf-8'))
        kv = self.nastavenia.get('kvalita_fotiek')
        kv = kv if isinstance(kv, dict) else {}
        for fmt in FORMATY:
            KVALITA[fmt] = KVALITA_PREDVOLENA[fmt]
            try:
                q = int(kv.get(fmt, KVALITA_PREDVOLENA[fmt]))
            except (TypeError, ValueError):
                q = 0
            if 1 <= q <= 100:
                KVALITA[fmt] = q
            else:
                varuj(f'nastavenia kvalita_fotiek.{fmt} nie je číslo 1 až 100, ostáva {KVALITA[fmt]}')
        self.avif = avif_dostupny()
        self.fotky_web = FotkyWeb(FOTKY_WEB if fotky_web is None else fotky_web)
        self.predlohy = predlohy
        # súbory fotiek, ktoré stránky naozaj použijú: sha256 zdroja -> {kľúč variantu: (šírka, výška, orez, formát)}
        # (plán pre nastroje/predlohy_web.py) a počty: z fotky-web, zakódované z obsahu
        self.potreba = {}
        self.zdroje = {}
        # rámik výrezu 4 : 5 karty (karta_foto vyrez): sha256 zdroja -> (x0, y0, x1, y1) v podieloch fotky
        self.vyrezy = {}
        self.pocty_fotiek = {'fotky-web': 0, 'obsah': 0}
        self.texty = json.loads((SABLONA / 'texty.json').read_text(encoding='utf-8'))
        css = (SABLONA / 'styl.css').read_text(encoding='utf-8').replace('\r\n', '\n')

        def zhusti(c):
            # vložený štýl bez komentárov a odsadenia (asi 7 kB menej HTML na každej stránke, výkon na mobile)
            return re.sub(r'^[ \t]+', '', re.sub(r'\n\s*\n', '\n', re.sub(r'/\*.*?\*/', '', c, flags=re.S)), flags=re.M).strip() + '\n'
        self.css_sablona = zhusti(css)
        # úvod bez úseku PODSTRÁNKY (značky @podstranky-zaciatok a @podstranky-koniec v styl.css): menej HTML na mobile
        self.css_uvod = zhusti(re.sub(r'/\* @podstranky-zaciatok \*/.*?/\* @podstranky-koniec \*/', '', css, flags=re.S))
        # app.js zhustený (zhusti_js, audit naplno položka 45); verzia v adrese ?v= je z odtlačku zhusteného súboru
        js = (SABLONA / 'app.js').read_text(encoding='utf-8') if (SABLONA / 'app.js').is_file() else ''
        self.js = zhusti_js(js).encode('utf-8') if js else b''
        self.js_verzia = hashlib.sha256(self.js).hexdigest()[:10]
        self.logo_svg = (SABLONA / 'logo.svg').read_text(encoding='utf-8')
        # logo hlavičky v podobe starého webu (štvorec, BM Building, spoločnosť s ručením obmedzeným), text na krivkách
        # (ops/weby-na-mieru/klienti/bmbuilding/kolo-1/uvod/logo_hlavicka.py); bez neho hlavička so štvorcom logo.svg
        lh = SABLONA / 'logo-hlavicka.svg'
        self.logo_hlavicka = lh.read_text(encoding='utf-8') if lh.is_file() else ''
        if not self.logo_hlavicka:
            varuj('sablona/logo-hlavicka.svg chýba, hlavička ukáže len štvorec loga')
        self.favicon_svg = (SABLONA / 'logo-favicon.svg').read_text(encoding='utf-8')
        znaky = SABLONA / 'pismo-znaky.txt'
        self.znaky_pisma = set(znaky.read_text(encoding='utf-8').rstrip('\n')) if znaky.is_file() else None
        self.pravidla = nacitaj_pravidla()
        self.domena = self.nastavenia.get('domena', 'https://bmbuilding.sk').rstrip('/')
        # adresa náhľadu (audit naplno 25. 11. 2026, položka 12): v náhľade idú og:image a twitter:image z nej, aby
        # odkaz na náhľad poslaný cez WhatsApp mal obrázok (na bmbuilding.sk do spustenia beží starý web a súbory
        # zostavy tam nie sú); canonical, og:url, JSON-LD a sitemap ostávajú na doméne. Len https, inak doména
        self.adresa_nahladu = bezpecna_url(self.nastavenia.get('adresa_nahladu')).rstrip('/')
        # Instagram len s istou adresou (dizajn-olovnica/INSTAGRAM.md); adresa je v šablóne, nie v obsahu
        self.instagram = bezpecna_url(self.nastavenia.get('instagram'), ['instagram.com'])
        # LinkedIn konateľa (1. kolo pripomienok, ZMENY-KOLO-1.md 3.7): len https na linkedin.com, inak sa ikona nezobrazí
        self.linkedin = bezpecna_url(self.nastavenia.get('linkedin'), ['linkedin.com'])
        self.firma = nacitaj_firmu(obsah)
        self.realizacie = nacitaj_realizacie(obsah)
        self.text_len_roky()
        # Novinky z webu preč (Andrej 24. 11. 2026 večer, ZMENY-KOLO-1.md 5.7: „pôvodne to chcel dať preč, ale chcel tam
        # dať ten časopis“): nastavenie "novinky": false v sablona/nastavenia.json. Bez noviniek sa nezostaví /novinky/
        # ani detail, nie sú v menu, v sitemap ani v odkazoch; súbory v obsah/novinky/ ostávajú (vratné, true = ako predtým)
        self.novinky_zapnute = self.nastavenia.get('novinky', True) is not False
        self.novinky = nacitaj_novinky(obsah) if self.novinky_zapnute else []
        for r in self.realizacie + self.novinky:
            for f in r['fotky']:
                self.nastav_sirky(f)
        if not self.avif and any(not f.get('fw') for r in self.realizacie + self.novinky for f in r['fotky']):
            varuj('Pillow nemá kodek AVIF: fotky bez hotovej webovej verzie (fotky-web/) dostanú len WebP')
        # údaje strán stavby zo starého webu (sablona/stary-web.json, nástroj stary_web_strany.py): adresy a weby
        # investora, projektanta a zhotoviteľa, termín s mesiacmi, etapy, osoba pri role; obsah má prednosť
        sw = SABLONA / 'stary-web.json'
        sw = nacitaj_json(sw) if sw.is_file() else {}
        sw = (sw or {}).get('realizacie') if isinstance(sw, dict) else {}
        sw = sw if isinstance(sw, dict) else {}
        for r in self.realizacie:
            d = sw.get(r['slug']) if isinstance(sw.get(r['slug']), dict) else {}
            r['strany'] = {}
            for strana in ('investor', 'projektant', 'zhotovitel'):
                zoz = d.get(strana) if isinstance(d.get(strana), list) else []
                r['strany'][strana] = [{k: jeden_riadok(x.get(k)) for k in ('meno', 'adresa', 'web')}
                                       for x in zoz if isinstance(x, dict) and (x.get('adresa') or x.get('web'))]
            r['termin'] = jeden_riadok(d.get('termin'))
            r['etapy'] = [jeden_riadok(e) for e in (d.get('etapy') or []) if jeden_riadok(e)]
            r['rola_osoba'] = jeden_riadok(d.get('rola_osoba'))
        # jednotný zápis roly, mien firiem a pravopis názvu pri zostave (audit naplno, položky 14 a 21)
        self.zapis_realizacii()
        for r in self.realizacie:
            self.priprav_fotky(r, r['nazov'])
        for n in self.novinky:
            self.priprav_fotky(n, n['nadpis'])
            self.ilustracna_fotka(n)
        self.media = self.nacitaj_media()
        self.subory = {}      # relatívna cesta -> bajty
        self.obrazky = {}     # relatívna cesta -> (odtlačok, funkcia)
        self.mapa_stranok = []  # (cesta, lastmod)
        # lastmod (brána pokus 1, správnosť 6): vydanie = deň, keď sa naposledy zmenila šablóna alebo texty stránok
        # (nastavenia.json); realizácia a novinka = najnovší z vydania, dátumu príspevku a poľa upravene
        self.vydanie = platny_datum(self.nastavenia.get('vydanie'))
        if self.nastavenia.get('vydanie') and not self.vydanie:
            varuj('sablona/nastavenia.json: vydanie nie je platný dátum RRRR-MM-DD')
        datumy = sorted(self.zmena(r) for r in self.realizacie + self.novinky if self.zmena(r))
        self.najnovsi = max([d for d in (datumy[-1] if datumy else '', self.vydanie) if d], default='')

    def zmena(self, r):
        return max((d for d in (r['datum'], r['upravene'], self.vydanie) if d), default='')

    def text_len_roky(self):
        """Text realizácie len s rokom (overenie 3, nález vážne; ZMENY-KOLO-1.md 5.2: pri realizáciách len rok alebo
        roky, nikde mesiac): sablona/nastavenia.json text_len_roky, slug -> [[pôvodná veta, veta len s rokom], ...].
        Obsah sa nemení (sťahovač ho prepisuje), veta sa nahradí pri zostave, takže text, description aj JSON-LD
        stránky ju majú len s rokom. Pôvodná veta, ktorá v texte nie je, je varovanie (obsah sa zmenil). Zverejnená
        realizácia, ktorej text má aj potom mesiac (MESIAC_V_TEXTE), je tiež varovanie."""
        mapa = self.nastavenia.get('text_len_roky')
        mapa = mapa if isinstance(mapa, dict) else {}
        podla_slugu = {r['slug']: r for r in self.realizacie}
        for slug, dvojice in mapa.items():
            r = podla_slugu.get(slug)
            if r is None:
                # skrytá (zverejnene false) alebo chýbajúca realizácia: nie je na webe, nič sa nenahrádza
                continue
            for d in dvojice if isinstance(dvojice, list) else [None]:
                if not (isinstance(d, list) and len(d) == 2 and all(isinstance(x, str) and x.strip() for x in d)):
                    varuj(f'nastavenia text_len_roky: {slug} {d!r} nie je dvojica [pôvodná veta, nová veta], vynechané')
                    continue
                stara, nova = (cisty(x) for x in d)
                if stara in r['text']:
                    r['text'] = r['text'].replace(stara, nova)
                else:
                    varuj(f'nastavenia text_len_roky: veta {stara!r} v texte realizácie {slug} nie je, text ostáva')
        for r in self.realizacie:
            m = MESIAC_V_TEXTE.search(r['text'])
            if m:
                varuj(f'realizácia {r["slug"]}: text má mesiac alebo deň ({m.group(0)!r}), na webe majú byť len roky '
                      '(sablona/nastavenia.json text_len_roky)')

    def mapa_zapisu(self, kluc, uprava=jeden_riadok):
        """Mapa {pôvodný text: nový text} zo sablona/nastavenia.json (rola_zapis, mena_zapis); kľúče s podčiarkovníkom
        sú poznámky. Obe strany prejdú úpravou ako obsah (bez pomlčiek em a en a výkričníkov), zlá položka je
        varovanie, nie pád zostavy."""
        m = self.nastavenia.get(kluc)
        if m is None:
            return {}
        if not isinstance(m, dict):
            varuj(f'nastavenia {kluc} nie je objekt, vynechané')
            return {}
        out = {}
        for k, v in m.items():
            if k.startswith('_'):
                continue
            stara, nova = uprava(k), (uprava(v) if isinstance(v, str) else '')
            if stara and nova:
                out[stara] = nova
            else:
                varuj(f'nastavenia {kluc}: {k!r} -> {v!r} nie je dvojica textov, vynechané')
        return out

    def zapis_realizacii(self):
        """Jednotný zápis v realizáciách pri zostave (audit naplno 25. 11. 2026, ZOZNAM.md položky 14 a 21). Obsah sa
        nemení (sťahovač ho prepisuje), nahradí sa ako text_len_roky, takže detail, karty, zoznamy, lightbox, OG,
        description, JSON-LD aj llms.txt majú ten istý zápis. sablona/nastavenia.json:
        rola_zapis: celé pole rola -> zápis tej istej roly (roly sú Máriove, mení sa len zápis, nie rola);
        mena_zapis: celá hodnota poľa projektant, investor, zhotovitel alebo autor a meno strany zo starého webu
        (stary-web.json) -> meno podľa ORSR;
        text_zapis: slug -> [[pôvodný kus, nový kus], ...] v názve, vlastnom titulku, texte a popisoch (alt) fotiek.
        Kus, ktorý v zverejnenej realizácii nie je, je varovanie (obsah sa zmenil); skrytá alebo chýbajúca realizácia
        sa preskočí bez varovania."""
        roly = self.mapa_zapisu('rola_zapis', uprava=lambda s: zaciatok_velky(jeden_riadok(s)))
        mena = self.mapa_zapisu('mena_zapis')
        for r in self.realizacie:
            r['rola'] = roly.get(r['rola'], r['rola'])
            for pole in ('projektant', 'investor', 'zhotovitel', 'autor'):
                r[pole] = mena.get(r.get(pole) or '', r.get(pole) or '')
            for zoz in (r.get('strany') or {}).values():
                for x in zoz:
                    x['meno'] = mena.get(x.get('meno') or '', x.get('meno') or '')
        mapa = self.nastavenia.get('text_zapis')
        if mapa is not None and not isinstance(mapa, dict):
            varuj('nastavenia text_zapis nie je objekt, vynechané')
        podla_slugu = {r['slug']: r for r in self.realizacie}
        for slug, dvojice in (mapa if isinstance(mapa, dict) else {}).items():
            r = podla_slugu.get(slug)
            if r is None:
                continue
            for d in dvojice if isinstance(dvojice, list) else [None]:
                if not (isinstance(d, list) and len(d) == 2 and all(isinstance(x, str) and x.strip() for x in d)):
                    varuj(f'nastavenia text_zapis: {slug} {d!r} nie je dvojica [pôvodný kus, nový kus], vynechané')
                    continue
                stara, nova = (jeden_riadok(x) for x in d)
                nasiel = False
                for pole in ('nazov', 'titulok', 'text'):
                    if stara in r[pole]:
                        r[pole] = r[pole].replace(stara, nova)
                        nasiel = True
                for f in r['fotky']:
                    if stara in f['alt']:
                        f['alt'] = f['alt'].replace(stara, nova)
                        nasiel = True
                if not nasiel:
                    varuj(f'nastavenia text_zapis: {stara!r} v realizácii {slug} nie je, text ostáva')

    # --- v médiách (ZMENY-KOLO-1.md 5.7, 24. 11. 2026 večer): časopis ASB pri Lumière, Archinfo pri Devínskych lúkach
    def media_fotka(self, d, meno):
        """Obrázok bloku V médiách zo sablona/media/ (výrez tabuľky, fotky strán): {subor, alt, popis}. Cesta len vnútri
        sablona/media; chýbajúci alebo pokazený súbor sa vynechá s varovaním. Kóduje sa ako ostatné fotky (AVIF, WebP)."""
        if not isinstance(d, dict):
            return None
        rel = jeden_riadok(d.get('subor'))
        koren = (SABLONA / 'media').resolve()
        try:
            p = (SABLONA / rel).resolve() if rel else None
        except (OSError, ValueError):
            p = None
        if p is None or koren not in p.parents or not p.is_file():
            varuj(f'v_mediach {meno}: obrázok {rel!r} chýba alebo je mimo sablona/media, vynechaný')
            return None
        try:
            w, h = rozmery_zdroja(p)
        except Exception:
            varuj(f'v_mediach {meno}: obrázok {rel!r} sa nedá otvoriť, vynechaný')
            return None
        f = {'zdroj': p, 'w': w, 'h': h, 'alt': jeden_riadok(d.get('alt')), 'popis': jeden_riadok(d.get('popis')),
             'ostrost': 0.0, 'rel': 'media/' + p.name, 'typ': '', 'viz': False, 'autor': '', 'rozmazana': False,
             'sha': hashlib.sha256(p.read_bytes()).hexdigest()}
        self.nastav_sirky(f)
        return f

    def nacitaj_media(self):
        """Zmienky v médiách pri realizácii (sablona/nastavenia.json v_mediach): slug zverejnenej realizácie -> kotva
        bloku, nadtitulok, nadpis, odseky vlastného textu, odkazy (len https na MEDIA_HOSTITELIA, s voliteľnou doslovnou
        citáciou), výrez tabuľky a fotky strán zo sablona/media/, údaje článku pre JSON-LD a text údaja V médiách
        v hrdinovi úvodu (uvod). Zlá položka sa vynechá s varovaním; skrytá realizácia nikdy (nemá stránku)."""
        nast = self.nastavenia.get('v_mediach')
        nast = nast if isinstance(nast, dict) else {}
        slugy = {r['slug'] for r in self.realizacie}
        media = {}
        for slug, d in nast.items():
            if slug.startswith('_'):
                continue
            if not isinstance(d, dict) or slug not in slugy:
                varuj(f'v_mediach {slug}: realizácia nie je zverejnená alebo položka nie je objekt, vynechané')
                continue
            kotva = jeden_riadok(d.get('kotva')) or 'v-mediach'
            if not SLUG_RE.match(kotva):
                varuj(f'v_mediach {slug}: kotva {kotva!r} nie je [a-z0-9-], použitá v-mediach')
                kotva = 'v-mediach'
            odkazy = []
            for o in d.get('odkazy') if isinstance(d.get('odkazy'), list) else []:
                o = o if isinstance(o, dict) else {}
                url, text = bezpecna_url(o.get('url'), MEDIA_HOSTITELIA), jeden_riadok(o.get('text'))
                if not url or not text:
                    varuj(f'v_mediach {slug}: odkaz {o.get("url")!r} nie je https na {", ".join(MEDIA_HOSTITELIA)} '
                          'alebo nemá text, vynechaný')
                    continue
                odkazy.append({'url': url, 'text': text, 'citat': jeden_riadok(o.get('citat'))})
            nadpis = jeden_riadok(d.get('nadpis')) or 'V médiách'
            m = {'slug': f'media-{kotva}', 'realizacia': slug, 'kotva': kotva, 'nadpis': nadpis, 'nazov': nadpis,
                 'miesto': '', 'obr_dir': f'obrazky/media/{kotva}', 'nadtitulok': jeden_riadok(d.get('nadtitulok')),
                 'text': [jeden_riadok(x) for x in (d.get('text') if isinstance(d.get('text'), list) else [d.get('text')])
                          if jeden_riadok(x)],
                 'odkazy': odkazy, 'uvod': jeden_riadok(d.get('uvod')),
                 'nazov_lightboxu': jeden_riadok(d.get('nazov_lightboxu')) or nadpis,
                 'strany_nadpis': jeden_riadok(d.get('strany_nadpis')), 'strany_popis': jeden_riadok(d.get('strany_popis')),
                 'clanok': d.get('clanok') if isinstance(d.get('clanok'), dict) else {}}
            m['tabulka'] = self.media_fotka(d.get('tabulka'), slug)
            m['strany'] = [f for f in (self.media_fotka(x, slug) for x in (d.get('strany') if isinstance(d.get('strany'), list) else []))
                           if f]
            for i, f in enumerate(([m['tabulka']] if m['tabulka'] else []) + m['strany'], 1):
                f['nn'] = f'{i:02d}'
            if not (m['text'] or m['odkazy'] or m['tabulka'] or m['strany']):
                varuj(f'v_mediach {slug}: bez textu, odkazu aj obrázka, vynechané')
                continue
            media[slug] = m
        return media

    def media_obrazky(self, m):
        return ([m['tabulka']] if m['tabulka'] else []) + m['strany']

    def v_mediach_blok(self, pre, r):
        """Blok V médiách na stránke realizácie (kotva z nastavení, napr. #casopis-asb): nadtitulok a nadpis s tým
        podstatným, veľký výrez tabuľky s popisom zdroja, vlastný text, odkazy von (target _blank, rel noopener) a malá
        galéria fotiek strán s lightboxom (skupina media-<kotva>, ten istý dialog ako galéria realizácie). Bez položky
        v nastaveniach prázdny reťazec."""
        m = self.media.get(r['slug'])
        if not m:
            return ''
        k = m['kotva']
        obrazky = self.media_obrazky(m)
        n = len(obrazky)

        def odkaz(f, sizes):
            plna, alt, atr = self.lb_data(pre, m, f, int(f['nn']), n, m['nazov_lightboxu'], f['popis'], m['slug'],
                                          nahlad=True, popis='')
            return (f'<a class="media-odkaz" href="{plna}" data-lightbox {atr}>'
                    f'{self.img(pre, m, f, sizes, alt=alt or f["alt"])}</a>')
        tab = ''
        if m['tabulka']:
            f = m['tabulka']
            tab = (f'<figure class="media-tabulka">{odkaz(f, SIZES_MEDIA_TABULKA)}'
                   + (f'<figcaption>{T(f["popis"])}</figcaption>' if f['popis'] else '') + '</figure>')
        # meno firmy a rozsah strán „74 až 77“ sa nezalomia
        text = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">'
                       f'{T(p).replace("BM Building", "BM" + chr(160) + "Building").replace(" až ", " až" + chr(160))}</p>'
                       for i, p in enumerate(m['text']))
        odkazy = ''
        if m['odkazy']:
            li = []
            for o in m['odkazy']:
                citat = (f'<span class="media-citat">„{T(o["citat"]).replace("BM Building", "BM" + chr(160) + "Building")}“</span>'
                         if o['citat'] else '')
                li.append(f'<li><a class="odkaz-von" href="{esc(o["url"])}" target="_blank" rel="noopener">'
                          f'{T(o["text"])} {SVG_VON}</a>{citat}</li>')
            odkazy = f'<ul class="media-odkazy">{"".join(li)}</ul>'
        strany = ''
        if m['strany']:
            li = ''.join(f'<li class="media-strana">{odkaz(f, SIZES_MEDIA_STRANA)}</li>' for f in m['strany'])
            strany = ('<div class="media-strany-obal">'
                      + (f'<h3 class="media-podnadpis">{T(m["strany_nadpis"])}</h3>' if m['strany_nadpis'] else '')
                      + f'<ul class="media-strany">{li}</ul>'
                      + (f'<p class="media-zdroj">{T(m["strany_popis"])}</p>' if m['strany_popis'] else '') + '</div>')
        nad = f'<p class="media-nad">{T(m["nadtitulok"])}</p>' if m['nadtitulok'] else ''
        return (f'<section class="sekcia media-sekcia{"" if obrazky else " media-male"}" id="{k}" aria-labelledby="{k}-nadpis">'
                '<div class="obsah">'
                f'<div class="media-hlava">{nad}<h2 class="nadpis" id="{k}-nadpis">{T(m["nadpis"])}</h2></div>'
                f'<div class="mriezka media-mriezka{"" if tab else " media-bez-tabulky"}">{tab}'
                f'<div class="media-text">{text}{odkazy}{strany}</div></div></div></section>')

    def ld_media(self, r):
        """subjectOf pre CreativeWork realizácie: tlačený článok z nastavení v_mediach (clanok), napr. ASB č. 244."""
        m = self.media.get(r['slug'])
        c = m['clanok'] if m else {}
        if not c.get('titulok') or not c.get('periodikum'):
            return []
        cislo = {'@type': 'PublicationIssue', 'isPartOf': {'@type': 'Periodical', 'name': jeden_riadok(c['periodikum'])}}
        if c.get('cislo'):
            cislo['issueNumber'] = jeden_riadok(c['cislo'])
        if c.get('datum'):
            cislo['datePublished'] = jeden_riadok(c['datum'])
        a = {'@type': 'Article', 'headline': jeden_riadok(c['titulok']), 'isPartOf': cislo}
        for k, kluc in (('strana_od', 'pageStart'), ('strana_do', 'pageEnd')):
            if c.get(k):
                a[kluc] = jeden_riadok(c[k])
        return [a]

    # --- fotky: rozmazané, typ, poradie galérie, obal (FOTKY.md, Pravidlo pre build.py)
    @staticmethod
    def povodny_stav(f):
        return 'pôvodný stav' in f['alt'].lower() or f.get('typ') == 'P'

    def priprav_fotky(self, r, nazov):
        p = self.pravidla
        for f in r['fotky']:
            f['typ'] = f['typ'] or p['typy'].get(f['rel'], '')
            if 'pôvodný stav' in f['alt'].lower() and not f['typ']:
                f['typ'] = 'P'
            # vizualizácia (malý render, nie fotka stavby) má vlastný prah prah_rozmazana_vizualizacia (predvolene 0)
            f['rozmazana'] = f['ostrost'] < (p['prah_viz'] if f.get('viz') else p['prah'])
        meno = r.get('slug', '')
        for f in r['fotky']:
            if f['rozmazana']:
                varuj(f'{meno}: fotka {f["rel"]} je rozmazaná (ostrosť {f["ostrost"]:g} pod '
                      f'{p["prah_viz"] if f.get("viz") else p["prah"]:g}), '
                      'nie je v galérii ani na obale')
        poradie = {t: i for i, t in enumerate(p['poradie'])}
        ostre = [(i, f) for i, f in enumerate(r['fotky']) if not f['rozmazana']]
        # galéria: podľa typu (hotová, letecká, rozostavaná, detail, interiér, pôvodný stav), v rámci typu podľa
        # ostrosti; fotky bez typu (budúci obsah) ostávajú v poradí obsahu na mieste hotovej stavby
        # vizualizácie vždy za skutočnými fotkami (obsah-zmeny.md, návrh pre stavbu bod 2)
        ostre.sort(key=lambda x: (1 if x[1].get('viz') else 0, poradie.get(x[1]['typ'] or 'HE', 0), 0 if x[1]['typ'] else 1,
                                  -x[1]['ostrost'] if x[1]['typ'] else x[0]))
        r['galeria'] = [f for _, f in ostre]
        interier_ok = bool(p['interier'].search(nazov))
        # realizácia, ktorá má len vizualizácie (RD Sklenárska, ZMENY-KOLO-1.md 5.4): karta a obal je prvá
        # vizualizácia so štítkom, nie prázdny rám (ako obal_vizualizacia v obsahu)
        if r['galeria'] and all(f.get('viz') for f in r['galeria']):
            r['obal_vizualizacia'] = True
        viz_obal = bool(r.get('obal_vizualizacia'))

        def mozny(f, prah, viz_smie=False):
            return (f['w'] >= f['h'] and f['w'] >= prah['min_sirka'] and f['ostrost'] >= prah['min_ostrost']
                    and f['typ'] not in NIE_OBAL and not self.povodny_stav(f) and (f['typ'] != 'I' or interier_ok)
                    and (not f.get('viz') or viz_smie))
        r['obal'] = (next((f for f in r['galeria'] if mozny(f, p['odporucany'])), None)
                     or next((f for f in r['galeria'] if mozny(f, p['obal'])), None)
                     or (next((f for f in r['galeria'] if mozny(f, p['obal'], True)), None) if viz_obal else None))
        # hrdina úvodu nikdy z vizualizácie
        r['velka'] = next((f for f in r['galeria'] if mozny(f, p['velka'])), None)
        # fotka karty (pás, zoznam): žiadna karta bez fotky (zadanie bmb-dokoncenie, Obsah 2). Poradie: obal, prvá
        # ostrá skutočná fotka z galérie (prednostne na šírku, nie pôvodný stav), vizualizácia len pri obal_vizualizacia
        skutocne = [f for f in r['galeria'] if not f.get('viz')]
        r['karta'] = (r['obal']
                      or next((f for f in skutocne if f['w'] >= f['h'] and not self.povodny_stav(f)), None)
                      or next((f for f in skutocne if not self.povodny_stav(f)), None)
                      or (skutocne[0] if skutocne else None)
                      or (r['galeria'][0] if viz_obal and r['galeria'] else None))
        self.karta_z_nastaveni(r)

    def karta_z_nastaveni(self, r):
        """Fotka karty realizácie (pás, mriežka, titulná fotka detailu) určená v sablona/nastavenia.json karta_foto
        (slug -> súbor, linka bmb-presnost: ostrejšia fotka s väčším zdrojom z vlastnej galérie, obsah sa nemení).
        Len skutočná fotka z galérie (nie rozmazaná, nie vizualizácia ani pôvodný stav); inak varovanie a karta podľa
        pravidiel vyššie. Novinky sa neriadia týmto nastavením (ich fotky sú v obsah/obrazky/novinky/)."""
        mapa = self.nastavenia.get('karta_foto')
        slug = r.get('slug', '')
        if not isinstance(mapa, dict) or slug not in mapa or any(f['rel'].startswith('novinky/') for f in r['fotky']):
            return
        volba = mapa.get(slug)
        # {"subor": ..., "detail": false}: len karta mriežky a služieb (karta_len), titulná fotka detailu a pás ostávajú
        # (brána bmb-presnost pokus 1, nález 2: Kalvária 02 má v rohu neostré listy, 01 bez nich je na detail malá)
        len_karta = isinstance(volba, dict) and volba.get('detail') is False
        subor = jeden_riadok(volba.get('subor') if isinstance(volba, dict) else volba)
        f = next((x for x in r['galeria'] if x['rel'] == f'{slug}/{subor}'), None)
        if f is None or f.get('viz') or self.povodny_stav(f):
            varuj(f'nastavenia karta_foto: fotka {subor} realizácie {slug} nie je v galérii, je rozmazaná, vizualizácia '
                  'alebo pôvodný stav, karta ostáva podľa pravidiel')
            return
        # {"subor": ..., "vyrez": [x0, y0, x1, y1]}: výrez 4 : 5 karty len z tohto rámika fotky (podiely 0 až 1), napr.
        # viac domu a menej holej hliny (brána bmb-presnost pokus 1, nález 9); výrez musí mať aspoň 768 px zdroja
        vyrez = volba.get('vyrez') if isinstance(volba, dict) else None
        if vyrez is not None:
            ok = (isinstance(vyrez, list) and len(vyrez) == 4 and all(isinstance(x, (int, float)) for x in vyrez)
                  and 0 <= vyrez[0] < vyrez[2] <= 1 and 0 <= vyrez[1] < vyrez[3] <= 1)
            if ok:
                x0, y0, x1, y1 = box_px(vyrez, f['w'], f['h'])
                ok = orez_rozmer(x1 - x0, y1 - y0, OREZY['45'])[0] >= 768
            if ok:
                f['vyrez45'] = tuple(float(x) for x in vyrez)
                self.vyrezy[f['sha']] = f['vyrez45']
            else:
                varuj(f'nastavenia karta_foto: výrez {vyrez!r} realizácie {slug} nie je rámik 0 až 1 s výrezom 4 : 5 '
                      'aspoň 768 px zdroja, karta ide zo stredu fotky')
        # {"subor": ..., "cely_ram": true}: karta mriežky a služieb ukáže výrez 4 : 5 cez celý rám aj pri malom zdroji,
        # keď predloha (fotky-web, zväčšené Upscaylom) má výrez aspoň KARTA_MIN_OREZ (Dvojdom S 01, Hrnčiarovce 01,
        # ZMENY-KOLO-1.md 5.3 a 3A.4); bez takej predlohy ostáva celá fotka v ráme (mala_karta)
        if isinstance(volba, dict) and volba.get('cely_ram') is True:
            f['cely_ram'] = True
        if len_karta:
            r['karta_len'] = f
            return
        # nahradená fotka karty (malý zdroj, preto ju karta_foto vymenilo) nesmie byť prvou veľkou fotkou galérie
        # (galeria(), overenie zostavy bmb-presnost: inak by sa mäkká fotka ukázala cez celú šírku galérie)
        if r.get('karta') is not f:
            r['karta_nahradena'] = r.get('karta')
        r['karta'] = f
        # reprezentatívna fotka (karta_foto) je v galérii prvá (ZMENY-KOLO-1.md 5.3), ostatné ostávajú v poradí
        r['galeria'] = [f] + [x for x in r['galeria'] if x is not f]

    def ilustracna_fotka(self, n):
        """Novinka bez vlastnej použiteľnej fotky (karta None): skutočná ostrá fotka stavby z realizácie podľa
        sablona/fotky-pravidla.json (ilustracne_fotky[slug novinky], inak ilustracna_predvolena), s popisom
        „Ilustračná fotka: <realizácia>, <miesto>“ (oprava po bráne, bod 2: žiadna karta bez fotky, nikto nesmie
        myslieť, že fotka zobrazuje predmet novinky). Fotka musí byť v galérii realizácie (nie rozmazaná), ostrá aspoň
        ako obal, nie vizualizácia ani pôvodný stav; inak build varuje a karta ostane s logom. Ilustračná fotka je
        len na karte (n['ilustracna_foto'] a n['ilustracna_r']); n['karta'] ostáva None, takže stránka novinky ani
        jej OG obrázok ju neukážu ako fotku novinky."""
        n['ilustracna'] = ''
        n['ilustracna_foto'] = n['ilustracna_r'] = None
        if n.get('karta') is not None:
            return
        p = self.pravidla
        for kluc, volba in ((n['slug'], p['ilustracne'].get(n['slug'])), ('ilustracna_predvolena', p['ilustracna_predvolena'])):
            if not volba:
                continue
            r = next((x for x in self.realizacie if x['slug'] == volba.get('slug')), None)
            subor = jeden_riadok(volba.get('subor'))
            f = next((x for x in r['galeria'] if x['rel'] == f'{r["slug"]}/{subor}'), None) if r else None
            if (f is None or f.get('viz') or self.povodny_stav(f) or f['ostrost'] < p['obal']['min_ostrost']):
                varuj(f'novinka {n["slug"]}: ilustračná fotka {kluc} ({volba.get("slug")}/{subor}) chýba, nie je ostrá, '
                      'je vizualizácia alebo pôvodný stav, nepoužitá')
                continue
            n['ilustracna_foto'] = f
            n['ilustracna_r'] = r
            n['ilustracna'] = f'Ilustračná fotka: {self.nazov_s_miestom(r)}'
            return
        varuj(f'novinka {n["slug"]}: bez fotky a bez ilustračnej fotky v sablona/fotky-pravidla.json, karta s logom')

    # --- pomocné
    def abs_url(self, cesta):
        return self.domena + '/' + cesta

    def og_abs_url(self, cesta):
        """Absolútna adresa obrázka Open Graph (og:image, twitter:image): v náhľade z adresy náhľadu (nastavenia.json
        adresa_nahladu, položka 12), v ostrom behu a bez adresy náhľadu z domény."""
        return (self.adresa_nahladu if self.nahlad and self.adresa_nahladu else self.domena) + '/' + cesta

    @staticmethod
    def pre(cesta):
        return '../' * cesta.count('/')

    @staticmethod
    def rel(pre, ciel):
        r = pre + ciel
        return r if r else './'

    # --- súbory fotiek (linka bmb-fotky): AVIF a WebP v šírkach podľa predlohy, hotové z fotky-web/ alebo z obsahu
    def nastav_sirky(self, f):
        """Predloha fotky: hotová webová verzia (fotky-web/manifest.json, záznam so sha256 zdroja; môže byť zväčšená
        z rozmazanej verzie, takže širšia než zdroj) alebo zdroj z obsah/ (nová fotka zo systému obsahu, iný sha256).
        Pravidlá výberu fotky (obal, úvod, ostrosť) ostávajú podľa zdroja (f['w'], f['h'])."""
        f['fw'] = None
        if self.predlohy is not None:
            w, h = self.predlohy.get(f['sha']) or (f['w'], f['h'])
            ma_avif = self.avif
        else:
            z = self.fotky_web.zaznam(f['sha'])
            if z:
                f['fw'] = z
                w, h = int(z['predloha']['w']), int(z['predloha']['h'])
            else:
                w, h = f['w'], f['h']
            # AVIF z fotky-web/ sa len kopíruje, kodek treba len na súbory, ktoré tam chýbajú
            ma_avif = self.avif or bool(z and any(str(k).endswith('|avif') for k in (z.get('subory') or {})))
        nastav_sirky(f, w, h, FORMATY if ma_avif else ('webp',))

    def da_sa(self, f, s, fmt, orez=''):
        """Súbor sa dá dať na web: WebP vždy (z fotky-web alebo z obsahu), AVIF s kodekom alebo hotový vo fotky-web."""
        if fmt == 'webp' or self.avif:
            return True
        return bool(f.get('fw')) and self.fotky_web.subor(f['sha'], orez, s, fmt) is not None

    def sirky_variantu(self, f, fmt, orez=''):
        """Šírky súborov fotky vo formáte fmt (orez '' celá fotka, 45 alebo 54 výrez zo stredu predlohy)."""
        if orez:
            if fmt == 'avif' and not f['sirky_avif']:
                return []
            zw, zh = f['zaklad']
            if orez == '45' and f.get('vyrez45'):
                x0, y0, x1, y1 = box_px(f['vyrez45'], zw, zh)
                zw, zh = x1 - x0, y1 - y0
            cw, _ = orez_rozmer(zw, zh, OREZY[orez])
            sirky = sirky_orezu(cw)
        else:
            sirky = f['sirky_avif'] if fmt == 'avif' else f['sirky']
        return [s for s in sirky if self.da_sa(f, s, fmt, orez)]

    def variant(self, r, f, s, fmt, orez=''):
        """Cesta súboru <obr_dir>/<nn>-<šírka>[-<orez>].<formát> a jeho registrácia na výrobu: kópia z fotky-web/,
        keď sedí sha256 zdroja a rozmer, inak kódovanie z obsah/ s kvalitou KVALITA (rovnaký odtlačok = bez kódovania)."""
        cesta = f'{r["obr_dir"]}/{f["nn"]}-{s}{"-" + orez if orez else ""}.{fmt}'
        if cesta in self.obrazky:
            return cesta
        if orez:
            a, b = OREZY[orez]
            v = max(1, round(s * b / a))
        else:
            v = vyska_pre(f['zaklad'][0], f['zaklad'][1], s)
        self.potreba.setdefault(f['sha'], {})[kluc_variantu(orez, s, fmt)] = (s, v, orez, fmt)
        self.zdroje[f['sha']] = f['zdroj']
        box = f.get('vyrez45') if orez == '45' else None
        hotovy = self.fotky_web.subor(f['sha'], orez, s, fmt) if f.get('fw') else None
        # výrez z rámika (karta_foto vyrez) len zo súboru fotky-web s tým istým rámikom, inak z obsahu
        if hotovy and (hotovy.get('w'), hotovy.get('h')) == (s, v) and hotovy.get('vyrez') == (list(box) if box else None):
            odt = hashlib.sha256(f'fotky-web|{hotovy["sha256"]}'.encode('utf-8')).hexdigest()
            self.obrazky[cesta] = (odt, (lambda x=hotovy: FotkyWeb.citaj(x)))
            self.pocty_fotiek['fotky-web'] += 1
        else:
            odt = hashlib.sha256('|'.join((f['sha'], VERZIA_OBR, fmt, str(KVALITA[fmt]), str(s), str(v), orez)
                                          + ((repr(box),) if box else ())).encode('utf-8')).hexdigest()
            pm = OREZY[orez] if orez else None
            # rámik len keď je (testy a nástroje nahrádzajú kódovanie funkciou so 4 parametrami)
            if fmt == 'avif':
                self.obrazky[cesta] = (odt, (lambda p=f['zdroj'], s=s, v=v, pm=pm, box=box:
                                             avif_bajty(p, s, v, pm, box) if box else avif_bajty(p, s, v, pm)))
            else:
                self.obrazky[cesta] = (odt, (lambda p=f['zdroj'], s=s, v=v, pm=pm, box=box:
                                             webp_bajty(p, s, v, pm, box) if box else webp_bajty(p, s, v, pm)))
            self.pocty_fotiek['obsah'] += 1
        return cesta

    def obrazok_cesty(self, r, f):
        """Cesty WebP celej fotky {šírka: cesta} (záloha, plná fotka odkazu); registruje ich na výrobu."""
        return {s: self.variant(r, f, s, 'webp') for s in self.sirky_variantu(f, 'webp')}

    def registruj_fotku(self, r, f):
        """Všetky súbory celej fotky (WebP aj AVIF) pre lightbox zo skrátenej zmluvy pásu (data-sirky)."""
        for fmt in FORMATY:
            for s in self.sirky_variantu(f, fmt):
                self.variant(r, f, s, fmt)

    def srcset(self, pre, r, f, fmt='webp', orez='', sirky=None):
        """srcset fotky vo formáte fmt; sirky (zoznam šírok) nahradí predvolené šírky (slider: sirky_slidera)."""
        sirky = self.sirky_variantu(f, fmt, orez) if sirky is None else sirky
        return ', '.join(f'{esc(pre + self.variant(r, f, s, fmt, orez))} {s}w' for s in sirky)

    def sirky_slidera(self, f, fmt):
        """Šírky súborov fotky slidera: šírky celej fotky a v AVIF navyše SLIDER_SIRKA_NAVYSE (840) medzi 720 a 960
        a plná šírka predlohy, keď je širšia než SIRKY[-1] x SIRKA_BLIZKO (predloha 16 : 9 má 2844 px, pole slidera
        je takmer štvorcové, krasa-8). Oba súbory navyše len z predlohy: hotové vo fotky-web (záznam so súborom) alebo
        pri pláne predlôh (nastroje/predlohy_web.py, ktorý ich vyrobí); nikdy zakódované zo zdroja v obsah/ (RD Ahoj 01
        má retuš len v predlohe). 840 len keď najbližšia väčšia šírka je od nej širšia aspoň o SIRKA_BLIZKO."""
        sirky = self.sirky_variantu(f, fmt)
        if fmt != 'avif' or not sirky:
            return sirky
        w = f['zaklad'][0]

        def z_predlohy(s):
            return self.predlohy is not None or bool(f.get('fw') and self.fotky_web.subor(f['sha'], '', s, 'avif') is not None)
        n = SLIDER_SIRKA_NAVYSE
        vacsie = [s for s in sirky if s > n]
        if n not in sirky and vacsie and n * SIRKA_BLIZKO <= vacsie[0] and z_predlohy(n):
            sirky = sorted(sirky + [n])
        if w <= sirky[-1] or w <= SIRKY[-1] * SIRKA_BLIZKO or w > SLIDER_MAX_SIRKA:
            return sirky
        return sirky + [w] if z_predlohy(w) else sirky

    def orez_srcset(self, pre, r, f, kod, fmt='webp'):
        sirky = self.sirky_variantu(f, fmt, kod)
        a, b = OREZY[kod]
        cesty = {s: (self.variant(r, f, s, fmt, kod), max(1, round(s * b / a))) for s in sirky}
        return ', '.join(f'{esc(pre + c)} {s}w' for s, (c, _) in cesty.items()), cesty

    def img(self, pre, r, f, sizes, lcp=False, lazy=True, trieda='', alt=None, orez=None, odlozene=False, zdroje='',
            sirky_avif=None):
        """Fotka ako <picture>: <source type="image/avif"> so srcset a sizes, potom <img> so WebP (src, srcset,
        sizes, rozmery). zdroje: <source> s media pred AVIF (mobilný výrez pohľadu A). odlozene (karta pásu): v src
        prázdny obrázok, skutočné srcset v data-src, data-srcset a AVIF v data-avif; obal picture so zdrojom AVIF
        doplní až app.js (source bez srcset by bol neplatný HTML). lcp: fetchpriority=high bez loading=lazy.
        sirky_avif: šírky AVIF namiesto predvolených (slider: sirky_slidera, navyše plná šírka predlohy)."""
        orez = orez or ''
        webp = self.sirky_variantu(f, 'webp', orez)
        srcset = self.srcset(pre, r, f, 'webp', orez)
        if orez:
            stred = [s for s in webp if s <= 720]
            a, b = OREZY[orez]
            w, h = webp[-1], max(1, round(webp[-1] * b / a))
        else:
            stred = [s for s in webp if s <= 960]
            w, h = f['max_w'], f['max_h']
        src = self.variant(r, f, stred[-1] if stred else webp[0], 'webp', orez)
        avif = self.srcset(pre, r, f, 'avif', orez, sirky_avif)
        if lcp:
            nacit = 'fetchpriority="high"'
        elif lazy and not odlozene:
            nacit = 'loading="lazy" decoding="async"'
        else:
            nacit = 'decoding="async"'
        t = f' class="{trieda}"' if trieda else ''
        alt = esc(alt if alt is not None else f['alt'])
        if odlozene:
            return (f'<img{t} src="{esc(pre)}prazdna-45.webp" data-src="{esc(pre + src)}" data-srcset="{srcset}"'
                    + (f' data-avif="{avif}"' if avif else '')
                    + f' sizes="{sizes}" width="{w}" height="{h}" alt="{alt}" {nacit}>')
        obr = (f'<img{t} src="{esc(pre + src)}" srcset="{srcset}" sizes="{sizes}" width="{w}" height="{h}" '
               f'alt="{alt}" {nacit}>')
        avif_zdroj = f'<source type="image/avif" srcset="{avif}" sizes="{sizes}">' if avif else ''
        if not avif_zdroj and not zdroje:
            return obr
        return f'<picture>{zdroje}{avif_zdroj}{obr}</picture>'

    def og_cesta_realizacie(self, r):
        return f'og/{r["slug"]}.jpg'

    def pismo_odt(self):
        odt = ''
        for v in (400, 700):
            p = SABLONA / f'arimo-{v}.ttf'
            odt += hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else 'x'
        return odt + hashlib.sha256(self.logo_svg.encode('utf-8')).hexdigest()[:16]

    def og_foto(self, f):
        """Fotka do OG obrázka (cesta k súboru alebo None): zdroj z obsah/, ale pri retušovanej predlohe (fotky-web,
        predloha.retus) najväčší celý súbor WebP z fotky-web, lebo retuš je len v predlohe a zdroj ju nemá (doladenie-2,
        overenie 2: og/bmbuilding.jpg z RD Ahoj 01 mal obe rozmazané plochy na gabióne, ktoré web inde zakrýva).
        Funguje aj v GitHub Actions, predlohy z D: netreba; súbor sa overí sha256 z manifestu. Retušovaná predloha bez
        použiteľného súboru dá OG bez fotky (zdroj s plochami nikdy)."""
        if not f:
            return None
        z = self.fotky_web.zaznam(f['sha']) if f.get('sha') else None
        if not z or z['predloha'].get('retus') is not True:
            return f['zdroj']
        sirky = sorted((int(k.split('|')[1]) for k in (z.get('subory') or {})
                        if re.match(r'^\|\d+\|webp$', str(k))), reverse=True)
        for s in sirky:
            hotovy = self.fotky_web.subor(f['sha'], '', s, 'webp')
            if hotovy and hashlib.sha256(Path(hotovy['cesta']).read_bytes()).hexdigest() == hotovy['sha256']:
                return hotovy['cesta']
        varuj(f'{f["rel"]}: retušovaná predloha bez súboru WebP vo fotky-web, OG obrázok bez fotky')
        return None

    def registruj_og(self, cesta, foto, nadpis, riadok2, riadok3):
        pismo_odt = self.pismo_odt()
        if foto is not None:
            odt = odtlacok_suboru(foto, VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)
        else:
            odt = hashlib.sha256('|'.join((VERZIA_OG, nadpis, riadok2, riadok3, pismo_odt)).encode()).hexdigest()
        logo = self.logo_svg
        self.obrazky[cesta] = (odt, (lambda: og_bajty(foto, nadpis, riadok2, riadok3, logo)))

    def mesto(self, r):
        return r['miesto'].split(',')[0].strip() if r['miesto'] else ''

    def nazov_s_miestom(self, r):
        m = self.mesto(r)
        if m and m.lower() not in r['nazov'].lower():
            return f'{r["nazov"]}, {m}'
        return r['nazov']

    def adresa(self):
        f = self.firma
        return ', '.join(x for x in (f['ulica'], ' '.join(y for y in (f['psc'], f['mesto']) if y)) if x)

    @staticmethod
    def roky_karty(r):
        """Roky na karte, v detaile a v popise slidera: len roky stavby z poľa roky. 1. kolo pripomienok (Mário na
        stretnutí 24. 11. 2026, STRETNUTIE-2026-11-24.md: „slovo príspevok preč, rok príspevku preč, len roky“;
        overenie-1, nález vážne): rok príspevku (rok_prispevku, rok dátumu z Facebooku) sa na webe neukazuje, lebo
        bez slova príspevok vyzeral ako rok stavby a filter Roky ho aj tak radil do „rok neuvedený“. Pole
        rok_prispevku ostáva v obsahu (sťahovač ho zachová), build ho už nezobrazuje."""
        return r.get('roky') or ''

    @classmethod
    def roky_stitok(cls, r):
        """Text kóty a údajov karty: len roky (1. kolo pripomienok, ZMENY-KOLO-1.md 5.2: slovo príspevok preč, napr.
        „príspevok 2026“ na „2026“): roky stavby, bez nich rok z roky_karty. Detail ho má pod popiskom Rok."""
        return cls.roky_karty(r)

    @staticmethod
    def rok_filtra(r):
        """Rok pre filter Roky a stránky po desaťročiach (data-rok): len skutočný rok realizácie z poľa roky.
        Rok príspevku (rok_z_datumu) sa nemieša so skutočnými rokmi, ide do voľby „rok neuvedený“ (0)."""
        return 0 if r.get('rok_z_datumu') else int(r.get('rok') or 0)

    def kedy(self, r):
        """Roky stavby; bez nich rok (roky_stitok), nikdy slovo príspevok ani Facebook na karte."""
        if r['roky']:
            return f'<span class="roky">{T(r["roky"])}</span>'
        return T(self.roky_stitok(r))

    def kat_nazov(self, k):
        return dict(KATEGORIE).get(k, k)

    def pocty_druhov(self, zoznam=None):
        zoznam = self.realizacie if zoznam is None else zoznam
        return {k: sum(1 for r in zoznam if k in r['kategorie']) for k, _ in KATEGORIE}

    # --- prvky štýlu Čiara (ukážka Ateliér Olovnica, dizajn-olovnica/OLOVNICA-ROZBOR.md)
    def kota(self, text, trieda='', staticka=False):
        """Kóta z ukážky: vlasová čiara so šikmými koncami a údajom v strede (roky). Číslice sú stĺpce, ktoré sa pri
        vstupe do obrazu raz pretočia (app.js pridá triedu otocene, CSS dodá číslice); čítačka číta skrytý text.
        Polohu číslice nesú triedy c0 až c9 a i0 až i9 (CSP nedovoľuje atribút style).
        staticka (karty zoznamu realizácií): tá istá kóta bez pretáčania, číslice ako text v Mono. Valce majú asi
        450 bajtov na kartu; pri 60 kartách by HTML zoznamu narástlo o 27 kB a Lighthouse mobil klesol (bmb-fb-uplnost)."""
        if staticka:
            kusy = ''.join(f'<span class="kota-slovo">{esc(k)}</span>' if re.search(r'[^\W\d_]', k) else esc(k)
                           for k in re.findall(r'\s+|[^\s]+', text))
            t = f' {trieda}' if trieda else ''
            return f'<span class="kota{t}"><span class="kota-text">{sadzba(kusy)}</span></span>'
        casti, i = [], 0
        # slová (až) písmom Sans ako v ukážke (.kota-slovo), číslice v Mono; medzery pevné
        for kus in re.findall(r'\d|\s+|[^\d\s]+', text):
            if kus.isdigit() and i < 10:
                casti.append(f'<span class="valec"><span class="stlpec c{kus} i{i}"></span></span>')
                i += 1
            elif kus.isspace():
                casti.append(' ')
            elif re.search(r'[^\W\d_]', kus):
                casti.append(f'<span class="kota-slovo">{esc(kus)}</span>')
            else:
                casti.append(esc(kus))
        t = f' {trieda}' if trieda else ''
        return (f'<span class="kota{t}"><span class="kota-text otoc"><span class="vh">{T(text)}</span>'
                f'<span class="valce" aria-hidden="true">{"".join(casti)}</span></span></span>')

    def akcie(self, trieda='akcie', vyzva='', navyse=''):
        """Dve tlačidlá zo zadania: Zavolať (tel:) a Napísať e-mail (mailto:); jedna menovka na zámer. navyse: HTML
        za tlačidlami."""
        f = self.firma
        a = []
        if f['tel_href']:
            a.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať {SVG_SIPKA}</a>')
        if f['email']:
            a.append(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{esc(f["email"])}">Napísať e-mail</a>')
        v = f' {vyzva}' if vyzva else ''
        return f'<div class="{trieda}"{v}>{"".join(a)}{navyse}</div>' if a else ''

    def socialne(self, popisy=False):
        """Sociálne siete ako ikony (1. kolo pripomienok, ZMENY-KOLO-1.md 3.7): Facebook z obsah/firma.json, LinkedIn
        konateľa z sablona/nastavenia.json (len https na linkedin.com), Instagram len keď je jeho adresa v nastaveniach
        (profil konateľa, Andrej 24. 11. 2026 večer, len https na instagram.com, inak sa ikona vynechá). Ikona je vlastné
        SVG z jednej sady v štvorcovom čipe (span.siet-cip, krasa-8), názov siete nesie aria-label odkazu; profil sa
        otvorí v novej karte (target _blank, rel noopener). popisy (Kontakt): za čipom názov siete (span.siet-meno), ktorý
        CSS ukáže len na počítači; aria-label ním začína, takže viditeľný popis je v prístupnom mene (WCAG 2.5.3)."""
        f = self.firma
        kto = f['konatel'] or 'konateľ'
        siete = [(f['facebook'], 'Facebook BM Building', 'Facebook', SVG_FACEBOOK),
                 (self.linkedin, f'LinkedIn: {kto}', 'LinkedIn', SVG_LINKEDIN),
                 (self.instagram, f'Instagram: {kto}', 'Instagram', SVG_INSTAGRAM)]
        return [f'<a class="siet" href="{esc(url)}" target="_blank" rel="noopener" aria-label="{esc(meno)}">'
                f'<span class="siet-cip">{svg}</span>' + (f'<span class="siet-meno">{T(kratke)}</span>' if popisy else '')
                + '</a>' for url, meno, kratke, svg in siete if url]

    def siete(self, trieda='', popisy=False):
        """Riadok ikon sociálnych sietí (Kontakt hneď pod tlačidlami s popismi, päta len čipy); bez sietí nič."""
        soc = self.socialne(popisy)
        t = f' {trieda}' if trieda else ''
        return f'<p class="siete{t}">{"".join(soc)}</p>' if soc else ''

    def vizitka(self, pre, trieda='', alt_loga=''):
        """Kontaktný blok v poradí banneru na plote: meno, ulica, mesto, tel., e-mail, web (poznávací prvok značky)."""
        f = self.firma
        riadky = [f'<span class="vizitka-meno">{T(f["meno"])}</span>']
        if f['ulica']:
            riadky.append(f'<span>{T(f["ulica"])}</span>')
        mesto = ' '.join(x for x in (f['psc'], f['mesto']) if x)
        if mesto:
            riadky.append(f'<span>{T(mesto)}</span>')
        if f['tel']:
            riadky.append(f'<span><span class="vizitka-k">tel.:</span> <a class="cisla" href="tel:{esc(f["tel_href"])}">'
                          f'{T(f["tel"])}</a></span>')
        if f['email']:
            riadky.append(f'<span><span class="vizitka-k">e-mail:</span> <a href="mailto:{esc(f["email"])}">'
                          f'{T(f["email"])}</a></span>')
        host = urllib.parse.urlsplit(self.domena).hostname or ''
        if host:
            riadky.append(f'<span>{T(host if host.startswith("www.") else "www." + host)}</span>')
        t = f' {trieda}' if trieda else ''
        return (f'<address class="vizitka{t}"><img class="vizitka-logo" src="{esc(pre)}logo.svg" width="64" height="64" '
                f'alt="{esc(alt_loga)}" loading="lazy" decoding="async"><span class="vizitka-riadky">{"".join(riadky)}</span></address>')

    def kontakt_sekcia(self, pre, hlavna=False, nadpis=None, veta=None):
        """Sekcia Kontakt z ukážky (U:1075 až U:1149) na svetlejšom podklade (na úvode modrý blok): vľavo nadpis, výzva,
        Zavolať a Napísať e-mail a hneď pod nimi riadok sietí (čipy, na počítači s popismi, krasa-8); vpravo karta
        namiesto ilustračnej mapy (ulica ukážky je vymyslená): kontaktný blok v poradí banneru na plote a pod ním
        Navigovať (Mapy Google) a Mapy.cz; pod oboma fakturačné údaje so všetkým zo starého webu (stary-web/kontakt.md,
        aj bankové spojenie podľa FAKTY-WEB.md r. 14), na počítači v dvoch stĺpcoch so spoločnými riadkami (trieda
        kontakt-riadky-N, polovica riadkov nahor; krasa-8, brána 3: pod pripnutou kartou ostávalo 200 až 380 px prázdnej
        plochy).
        Je to záver každej stránky okrem /kontakt/, kde je hlavným obsahom (hlavna=True: nadpis h2 Kontaktné údaje,
        bez výzvy)."""
        f = self.firma
        tk = self.texty['kontakt']
        nadpis = (tk.get('sekcia_nadpis') or 'Kontaktné údaje') if hlavna else (nadpis or 'Kontakt')
        veta = '' if hlavna else (veta or self.texty['vyzvy']['zaver'])
        adresa = self.adresa()
        gmaps = f'https://www.google.com/maps/search/?api=1&query={urllib.parse.quote_plus(adresa)}'
        mapy = f'https://mapy.cz/zakladni?q={urllib.parse.quote(adresa)}'
        pole = [('Obchodné meno', f['meno']), ('Konateľ', f['konatel']), ('Sídlo', adresa), ('IČO', f['ico']),
                ('IČ DPH', f['ic_dph']), ('Zápis', f['zapis']), ('Bankové spojenie', tk.get('banka', '')),
                ('Číslo účtu', tk.get('ucet', ''))]
        riadky = [(k, v) for k, v in pole if v]
        # Sídlo ako v údajoch hrdinu: „Podhorská 9,“ a „902 01 Pezinok“ sa vnútri nezalomia (doladenie-1, nález drobné:
        # pri 360 a 390 px sa PSČ oddelilo od mesta). Na mobile sú údaje v dvoch stĺpcoch a dlhé hodnoty (Sídlo, Zápis)
        # idú cez oba (trieda fakt-siroky, styl.css do 767 px; doladenie-2), na počítači nič nemení
        bunky = []
        for k, v in riadky:
            # bankové spojenie a číslo účtu cez oba stĺpce len do 359 px (trieda fakt-ucet, doladenie-3)
            trieda = (' class="fakt-siroky"' if k in KONTAKT_SIROKE
                      else ' class="fakt-ucet"' if k in KONTAKT_UCET else '')
            bunky.append(f'<div{trieda}><dt>{T(k)}</dt><dd{CISLA if k in ("IČO", "IČ DPH", "Číslo účtu") else ""}>'
                         f'{self.adresa_dvojriadok() if k == "Sídlo" else T(v)}</dd></div>')
        dl = ''.join(bunky)
        vyzva = '' if hlavna else ' data-vyzva-dole'
        posobnost = tk.get('posobnost', '')
        return (f'<section class="sekcia kontakt" id="kontakt" aria-labelledby="kontakt-nadpis"{vyzva}>'
                f'<div class="obsah mriezka kontakt-mriezka"><div class="kontakt-text">'
                f'<h2 class="nadpis" id="kontakt-nadpis">{T(nadpis)}</h2>'
                + (f'<p class="uvodny">{T(veta)} {T(posobnost)}</p>' if veta else '')
                + self.akcie('kontakt-akcie', 'data-vyzva-hore' if hlavna else '')
                # siete hneď pod tlačidlami (krasa-8, brána 3: ikony boli osamotené pod tabuľkou údajov)
                + self.siete('kontakt-siete', popisy=True)
                # vizitka (telefón, e-mail) v DOM hneď za tlačidlami a sieťami, na mobile nad fakturačnými údajmi; od 768 px
                # vpravo vedľa nadpisu, fakturačné údaje pod oboma cez celú šírku (krasa-8: karta už nie je pripnutá)
                + '</div><div class="kontakt-mapa"><figure class="mapa-karta">'
                + self.vizitka(pre, alt_loga='Logo BM Building, s.r.o.' if hlavna else '')
                + f'<figcaption class="mapa-spodok"><a class="tlacidlo tlacidlo-obrys" href="{esc(gmaps)}">Navigovať {SVG_SIPKA}</a>'
                f'<a class="odkaz-von" href="{esc(mapy)}">Mapy.cz {SVG_VON}</a></figcaption></figure></div>'
                + f'<div class="kontakt-udaje"><h3 class="kontakt-h">{T(tk.get("udaje_nadpis") or "Fakturačné údaje")}</h3>'
                f'<dl class="fakty kontakt-fakty kontakt-riadky-{(len(riadky) + 1) // 2}">{dl}</dl>'
                + '</div></div></section>')

    def zaver(self, pre='', hlavny=None, dalej=None):
        """Záver každej stránky okrem Kontaktu: sekcia Kontakt z ukážky (kontakt_sekcia) s výzvou stránky."""
        return self.kontakt_sekcia(pre, nadpis=hlavny, veta=dalej)

    def hlava_stranky(self, pre, kroky, h1, pod='', bok='', navyse='', trieda=''):
        """Hlava podstránky: omrvinky, vlasová čiara cez celú šírku, veľký ľahký nadpis vľavo a veta alebo údaje
        vpravo (dvojstrana z úvodu ukážky). pod: druhá časť nadpisu (mesto, dátum), oddelená skrytou čiarkou."""
        h1_html = T(h1) + (f'<span class="vh">, </span><span class="nadpis-pod">{T(pod)}</span>' if pod else '')
        t = f' {trieda}' if trieda else ''
        return (f'<div class="hlava-stranky{t}"><div class="obsah">{self.omrvinky(pre, kroky) if kroky else ""}'
                f'<div class="mriezka hlava-mriezka"><h1 class="nadpis-stranky" id="nadpis">{h1_html}</h1>'
                + (f'<div class="hlava-bok">{bok}</div>' if bok else '') + f'</div>{navyse}</div></div>')

    def poradie_sluzieb(self):
        """Slugy služieb (texty.json sluzby) v poradí kariet Činnosti na úvode (cinnost_doslova: 1. Stavebný manažment,
        2. Stavebný dozor, 3. Stavbyvedúci); služba bez karty ide na koniec v pôvodnom poradí. Podmenu Činnosti
        v mobilnom menu je v tomto poradí (doladenie-1, nález drobné: bolo Stavebný dozor, Stavbyvedúci, manažment)."""
        karty = [v.get('slug') for k, v in (self.texty.get('cinnost_doslova') or {}).items()
                 if not k.startswith('_') and isinstance(v, dict)]
        sluzby = list(self.texty['sluzby'])
        return sorted(sluzby, key=lambda s: (karty.index(s) if s in karty else len(karty), sluzby.index(s)))

    # --- kostra stránky
    def menu(self, pre, aktivne):
        """Pripnutá hlavička na každej stránke (1. kolo pripomienok, ZMENY-KOLO-1.md 3.1 a 3.2): vľavo logo v podobe
        starého webu (sablona/logo-hlavicka.svg, text na krivkách), menu podľa starého webu bez Postupu (Činnosť,
        Realizácie, Novinky, Oprávnenia, Kontakt), tlačidlo Zavolať (tel:); do 1099 px menu v details (funguje bez JS,
        app.js dopĺňa Esc a zatvorenie)."""
        sluzby = list(self.texty['sluzby'])

        def a(ciel, text, kluc):
            cur = ''
            if aktivne and aktivne == kluc:
                cur = ' aria-current="page"'
            elif aktivne and kluc and (aktivne.startswith(kluc + '/') or (kluc == 'sluzby' and aktivne in sluzby)):
                cur = ' aria-current="true"'
            return f'<a href="{esc(self.rel(pre, ciel))}"{cur}>{T(text)}</a>'
        # Činnosť vedie na doslovný text hneď pod hrdinom úvodu (ZMENY-KOLO-1.md 3A.3), z podstránky ../#cinnost
        polozky = [('#cinnost', 'Činnosť', 'sluzby'), ('realizacie/', 'Realizácie', 'realizacie')]
        if self.novinky:
            polozky.append(('novinky/', 'Novinky', 'novinky'))
        polozky += [('opravnenia/', 'Oprávnenia', 'opravnenia'), ('kontakt/', 'Kontakt', 'kontakt')]
        d = ''.join(f'<li>{a(c, t, k)}</li>' for c, t, k in polozky)
        m = []
        for c, t, k in polozky:
            m.append(f'<li>{a(c, t, k)}')
            if k == 'sluzby':
                m.append('<ul>' + ''.join(f'<li>{a(s + "/", self.texty["sluzby"][s]["menu"], s)}</li>'
                                          for s in self.poradie_sluzieb()) + '</ul>')
            m.append('</li>')
        f = self.firma
        tel = ''
        if f['tel_href']:
            # na počítači (od 1024 px) aj s číslom: tel: vo Windows otvorí len výber aplikácie a číslo nebolo pred
            # Kontaktom vidno (audit naplno 25. 11. 2026, položka 27); prístupné meno aria-label obsahuje viditeľný
            # text pri každej šírke (WCAG 2.5.3)
            cislo = f'<span class="tel-cislo cisla">{T(f["tel"])}</span>' if f['tel'] else ''
            tel = (f'<a class="tlacidlo tlacidlo-male hlavicka-tel" href="tel:{esc(f["tel_href"])}" '
                   f'aria-label="Zavolať {esc(f["tel"])}">Zavolať{cislo}</a>')
        if self.logo_hlavicka:
            vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', self.logo_hlavicka)
            w, h = (round(float(vb.group(1))), round(float(vb.group(2)))) if vb else (478, 100)
            logo = (f'<img class="znak-logo" src="{esc(pre)}logo-hlavicka.svg" width="{w}" height="{h}" '
                    'alt="BM Building, spoločnosť s ručením obmedzeným, úvod">')
        else:
            logo = f'<img src="{esc(pre)}logo.svg" width="52" height="52" alt="BM Building, úvod">'
        return ('<header class="hlava">\n<div class="obsah">'
                f'<a class="znak" href="{esc(self.rel(pre, ""))}">{logo}</a>\n'
                f'<nav class="menu-d" aria-label="Hlavná"><ul class="menu">{d}</ul></nav>\n'
                f'<div class="hlava-akcie">{tel}<details class="menu-m"><summary class="tlacidlo tlacidlo-obrys tlacidlo-male">Menu</summary>'
                f'<nav class="menu-panel" aria-label="Hlavná, menu"><ul>{"".join(m)}</ul></nav></details></div>\n'
                '</div></header>\n')

    def pata(self, pre, stlpec_firma=True):
        """Päta ukážky (U:1152 až U:1161): logo, jedna veta s adresou a IČO, ikony sietí (socialne, čipy 44 px bez
        popisov, krasa-8), odkazy (Oprávnenia,
        Ochrana osobných údajov), drobné písmo s © a zápisom v registri a nenápadne „Web: ARLing“.
        Stránky a fakturačné údaje sú v sekcii Kontakt (kontakt_sekcia); stlpec_firma ostáva len pre spätnú zhodu."""
        f = self.firma
        # adresa po častiach nezalomiteľná a „IČO“ s pevnou medzerou pred číslom (doladenie-1, nález drobné: pri 390 px
        # končil riadok „Pezinok. IČO“ a číslo bolo na ďalšom)
        veta = (f'{T(f["meno"])}, {self.adresa_dvojriadok()}.'
                + (f' IČO {T(f["ico"])}.' if f['ico'] else ''))
        odkazy = []
        odkazy.append(f'<a href="{esc(self.rel(pre, "opravnenia/"))}">Oprávnenia</a>')
        odkazy.append(f'<a href="{esc(self.rel(pre, "ochrana-udajov/"))}">Ochrana osobných údajov</a>')
        rok = self.najnovsi[:4]
        # s_bodkou: meno „BM Building, s.r.o.“ už bodkou končí, „s.r.o..“ nie (overenie kolo 1)
        male = (f'<span>© {T(rok + " " if rok else "")}{T(s_bodkou(f["meno"]))}</span> '
                + (f'<span>{T(f["zapis"])}</span> ' if f['zapis'] else '')
                + '<a class="pata-arling" href="https://arling.sk/web/">Web: ARLing</a>')
        return ('<footer class="pata" data-vyzva-dole>\n<div class="obsah mriezka pata-mriezka">\n'
                f'<div class="pata-zaver"><a class="znak pata-znak" href="{esc(self.rel(pre, ""))}"><img src="{esc(pre)}logo.svg" '
                'width="40" height="40" alt="BM Building, úvod" loading="lazy"></a>'
                f'<p class="pata-veta">{veta}</p>{self.siete("pata-siete")}<p class="pata-odkazy">{"".join(odkazy)}</p></div>\n'
                f'<p class="male">{male}</p>\n</div>\n</footer>\n')

    def lista(self):
        """Spodná lišta na mobile (ukážka 12): Zavolať a Napísať e-mail, vyjde až keď tlačidlá úvodu odídu z obrazu."""
        f = self.firma
        a = []
        if f['tel_href']:
            a.append(f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať</a>')
        if f['email']:
            # pod 320 px „E-mail“ (styl.css lista-kratky, audit naplno 25. 11. 2026, položka 35): skrytý span (display
            # none) nie je ani v prístupnom mene, meno je vždy viditeľný text
            a.append(f'<a class="tlacidlo tlacidlo-obrys" href="mailto:{esc(f["email"])}"><span class="lista-dlhy">Napísať e-mail</span>'
                     '<span class="lista-kratky">E-mail</span></a>')
        return f'<nav class="lista" aria-label="Rýchly kontakt">{"".join(a)}</nav>\n' if a else ''

    def omrvinky(self, pre, kroky):
        li = []
        for i, (ciel, text) in enumerate(kroky):
            if i == len(kroky) - 1:
                li.append(f'<li><span aria-current="page">{T(text)}</span></li>')
            else:
                li.append(f'<li><a href="{esc(self.rel(pre, ciel))}">{T(text)}</a></li>')
        return f'<nav class="omrvinky" aria-label="Omrvinky"><ol>{"".join(li)}</ol></nav>'

    def ld_firma(self):
        f = self.firma
        uzol = {
            '@type': ['Organization', 'ProfessionalService'],
            '@id': self.abs_url('#firma'),
            'name': 'BM Building',
            'legalName': f['meno'],
            'url': self.abs_url(''),
            # logo aj image: PNG 512 x 512 renderované zo sablona/logo.svg, absolútna URL (LOGO.md bod 5, Google žiada
            # aspoň 112 px); OG obrázok 1200 x 630 ostáva len v og:image a twitter:image
            'logo': self.abs_url('logo.png'),
            'image': self.abs_url('logo.png'),
            'address': {'@type': 'PostalAddress', 'streetAddress': f['ulica'], 'postalCode': f['psc'],
                        'addressLocality': f['mesto'], 'addressCountry': 'SK'},
            'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'},
                           {'@type': 'AdministrativeArea', 'name': f['posobnost']}],
            'knowsAbout': [v['menu'] for v in self.texty['sluzby'].values()] + ['Technický dozor investora (TDI)'],
        }
        if f['tel']:
            uzol['telephone'] = f['tel']
        if f['email']:
            uzol['email'] = f['email']
        if f['facebook']:
            uzol['sameAs'] = [f['facebook']]
        if f['ic_dph']:
            uzol['vatID'] = f['ic_dph']
        # IČO ako identifikátor organizácie (audit naplno 25. 11. 2026, položka 41; https://schema.org/identifier,
        # PropertyValue s propertyID), číslice bez medzier ako vo výpise ORSR
        ico = re.sub(r'\s+', '', f['ico'] or '')
        if ico:
            uzol['identifier'] = {'@type': 'PropertyValue', 'propertyID': 'IČO', 'value': ico}
        if platny_datum(f['den_zapisu']):
            uzol['foundingDate'] = f['den_zapisu']
        if f['konatel']:
            uzol['employee'] = {'@type': 'Person', 'name': f['konatel'], 'jobTitle': 'konateľ'}
            # LinkedIn aj Instagram (mariobulko) sú profily konateľa, nie firmy: patria k osobe, nie do sameAs firmy
            osoba = [x for x in (self.linkedin, self.instagram) if x]
            if osoba:
                uzol['employee']['sameAs'] = osoba
        return uzol

    def ld_breadcrumb(self, kroky):
        return {'@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': t, 'item': self.abs_url(c)}
            for i, (c, t) in enumerate(kroky, 1)]}

    def stranka(self, cesta, title, description, telo, aktivne, ld_uzly, og_obr='og/bmbuilding.jpg',
                og_typ='website', do_mapy=True, lastmod=None, zaklad=None, pata_firma=True, trieda='', preload_obr=None,
                css_navyse=''):
        # zaklad (len 404): absolútne cesty od koreňa webu („/“ alebo „/bmbuilding/“) namiesto <base>; s <base> by
        # „Preskočiť na obsah“ (#obsah) viedlo na úvod (brána pokus 2, správnosť 8)
        # css_navyse (úvod): pravidlá vypočítané z nastavení (stred výrezu fotiek slidera), CSP nedovolí atribút style
        pre = zaklad if zaklad else self.pre(cesta)
        css = (self.css_uvod if cesta == '' else self.css_sablona).replace('@PRE@', pre) + css_navyse
        ld = ld_json({'@context': 'https://schema.org', '@graph': ld_uzly}) if ld_uzly else ''
        # connect-src 'self': Lighthouse a PageSpeed Insights čítajú robots.txt cez stránku (kontrola kolo 1).
        # script-src: vlastný app.js ('self'), jeden vložený skript a JSON-LD cez sha256; žiadne cudzie zdroje,
        # žiadne inline handlery ani unsafe-inline (v2, SMER.md 2.12)
        skripty = ["'self'", sha(SKRIPT_JS)] + ([sha(ld)] if ld else [])
        csp = ["default-src 'none'", "img-src 'self'", "font-src 'self'", "connect-src 'self'", "manifest-src 'self'",
               'style-src ' + sha(css) + ' ' + sha(NOSCRIPT_CSS), 'script-src ' + ' '.join(skripty),
               "base-uri 'none'", "form-action 'none'"]
        canon = self.abs_url(cesta or '')
        h = ['<!doctype html>', '<html lang="sk">', '<head>', '<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             # CSP s doslovnými apostrofmi (len pevné kľúčové slová a base64 hashe, žiadny obsah z JSON)
             f'<meta http-equiv="Content-Security-Policy" content="{"; ".join(csp)}">']
        if self.nahlad:
            h.append('<meta name="robots" content="noindex, nofollow">')
        h += ['<meta name="color-scheme" content="light">', '<meta name="theme-color" content="#17476f">',
              f'<title>{esc(title)}</title>']
        if description:
            h.append(f'<meta name="description" content="{esc(description)}">')
        if cesta is not None and not zaklad:
            h.append(f'<link rel="canonical" href="{esc(canon)}">')
        h += [f'<link rel="icon" href="{esc(pre)}favicon.svg" type="image/svg+xml">',
              f'<link rel="icon" href="{esc(pre)}favicon-32.png" type="image/png" sizes="32x32">',
              f'<link rel="icon" href="{esc(pre)}favicon-48.png" type="image/png" sizes="48x48">',
              f'<link rel="icon" href="{esc(pre)}favicon.ico" sizes="16x16 32x32 48x48">',
              f'<link rel="apple-touch-icon" href="{esc(pre)}apple-touch-icon.png" sizes="180x180">',
              f'<link rel="manifest" href="{esc(pre)}site.webmanifest">']
        # Geist a Geist Mono (OFL 1.1, ukážka Olovnica): preload latinky textu, slovenskej diakritiky a čísel v Mono
        h += [f'<link rel="preload" href="{esc(pre)}fonts/{p}" as="font" type="font/woff2" crossorigin>' for p in PRELOAD_PISMA]
        # úvod: preload fotky pohľadu A (LCP) podľa šírky, aby sa sťahovala hneď s hlavou dokumentu (Lighthouse mobil)
        h += preload_obr or []
        # logo hlavičky s vysokou prioritou hneď s hlavou dokumentu: inak ako lenivý obrázok s nízkou prioritou čakalo
        # na písma a fotky a pri Slow 4G sa ukázalo asi 0,7 s po prvom vykreslení (doladenie-2, nález drobné)
        if self.logo_hlavicka:
            h.append(f'<link rel="preload" href="{esc(pre)}logo-hlavicka.svg" as="image" type="image/svg+xml" '
                     'fetchpriority="high">')
        # podmnožina číslic Geist Mono s preloadom len na stránke, ktorá Mono kreslí (audit naplno 25. 11. 2026, položka
        # 44; inde by Chrome hlásil nepoužitý preload, napríklad Kontakt). V hlave len za prednačítanou fotkou LCP
        # (úvod), inak na konci tela za fotkami, ktoré parser nájde skôr: pri HTTP/1.1 (6 spojení na hostiteľa) preload
        # v hlave obsadil spojenie, kým parser našiel fotku LCP v tele, a LCP /realizacie/ pri 390 px, Slow 4G a CPU 4x
        # bolo 3 676 ms proti 2 940 ms s preloadom na konci tela; pri HTTP/2 (GitHub Pages) je poloha pre LCP jedno
        # (3 920 a 3 956 ms). Prednačítané písmo nie je v reťazi kritických požiadaviek (Lighthouse network dependency
        # tree), ani keď je preload v tele (rel=preload smie byť v body, HTML: body-ok)
        body = (self.menu(pre, aktivne) + f'<main id="obsah">\n{telo}\n</main>\n' + self.pata(pre, pata_firma)
                + self.lista())
        preload_mono = ''
        if znaky_mono(body):
            preload_mono = (f'<link rel="preload" href="{esc(pre)}fonts/{PISMO_MONO_CISLA}" as="font" type="font/woff2" '
                            'crossorigin>')
            if preload_obr:
                h.append(preload_mono)
                preload_mono = ''
        h += ['<meta property="og:locale" content="sk_SK">', f'<meta property="og:type" content="{og_typ}">',
              '<meta property="og:site_name" content="BM Building">',
              f'<meta property="og:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta property="og:description" content="{esc(description)}">')
        if not zaklad:
            h.append(f'<meta property="og:url" content="{esc(canon)}">')
        h += [f'<meta property="og:image" content="{esc(self.og_abs_url(og_obr))}">',
              f'<meta property="og:image:width" content="{OG_W}">', f'<meta property="og:image:height" content="{OG_H}">',
              '<meta name="twitter:card" content="summary_large_image">',
              f'<meta name="twitter:title" content="{esc(title)}">']
        if description:
            h.append(f'<meta name="twitter:description" content="{esc(description)}">')
        h.append(f'<meta name="twitter:image" content="{esc(self.og_abs_url(og_obr))}">')
        h.append(f'<style>{css}</style>')
        h.append(f'<noscript><style>{NOSCRIPT_CSS}</style></noscript>')
        h.append(f'<script>{SKRIPT_JS}</script>')
        if self.js:
            h.append(f'<script src="{esc(pre)}app.js?v={self.js_verzia}" defer></script>')
        if ld:
            h.append(f'<script type="application/ld+json">{ld}</script>')
        h += ['</head>', f'<body{f" class={chr(34)}{trieda}{chr(34)}" if trieda else ""}>',
              '<a class="preskocit" href="#obsah">Preskočiť na obsah</a>', CIARY_HTML, body + preload_mono + '</body>',
              '</html>', '']
        subor = (cesta + 'index.html') if cesta is not None else '404.html'
        self.subory[subor] = '\n'.join(h).encode('utf-8')
        if do_mapy and cesta is not None:
            self.mapa_stranok.append((cesta, platny_datum(lastmod) or self.najnovsi))

    # --- časti
    def znak_karty(self, pre):
        """Karta bez jedinej použiteľnej fotky (len novinky, ktorých obrázky sú cudzia grafika alebo osobné údaje,
        obsah-zmeny.md): logo firmy v ráme 4 : 5, nie cudzia fotka inej stavby (bola by zavádzajúca)."""
        return (f'<img class="karta-znak" src="{esc(pre)}logo.svg" width="160" height="160" alt="" loading="lazy" '
                'decoding="async">')

    @staticmethod
    def foto_karty(r):
        """Fotka svetlej karty realizácie (mriežka, služby): karta_len z nastavenia karta_foto s "detail": false,
        inak fotka karty (tá je aj titulnou fotkou detailu a kartou pásu)."""
        return r.get('karta_len') or r.get('karta')

    @staticmethod
    def mala_karta(f):
        """Výrez 4 : 5 zdroja je menší než KARTA_MIN_OREZ: na karte mriežky je fotka mäkšia (brána bmb-presnost);
        stránka služby dá do štvorcov kariet najprv fotky s väčším zdrojom. Fotka s karta_foto cely_ram sa meria
        predlohou (f['zaklad'], zväčšené Upscaylom), nie zdrojom."""
        if not f:
            return False
        w, h = f['zaklad'] if f.get('cely_ram') and f.get('zaklad') else (f['w'], f['h'])
        return orez_rozmer(w, h, OREZY['45'])[0] < KARTA_MIN_OREZ

    def img_karty(self, pre, r, f, alt, lcp=False):
        """Fotka svetlej karty: vždy výrez 4 : 5 cez celý rám (object-fit cover), aj pri malom zdroji (mala_karta).
        Doladenie-1 (nález vážne, krása asi 6 až 6,5 z 10): zmenšená celá fotka 4 : 3 v strede rámu na rozmazanej kópii
        (foto-cela a foto-podklad z krasa-8) pôsobila v mriežke ostrých fotiek ako pokazená alebo ešte sa načítavajúca
        fotka. Výrez malého zdroja je pri hustote 2 a 3 mierne mäkší (vedome, záložné riešenie overenia 1); šírky výrezu
        idú z predlohy, keď ju fotka má (fotky-web, zväčšené Upscaylom), takže nová predloha kartu sama zaostrí."""
        return self.img(pre, r, f, SIZES_KARTA, lcp=lcp, alt=alt, orez='45')

    def dlazdica(self, pre, r, nadpis='h3', data=False, poradie=0, lcp=False, neskor=False):
        """Svetlá karta realizácie v mriežke (karta pásu ukážky na svetlom podklade, PLAN-1-1.md časť 5): fotka 4 : 5
        (výrez zo stredu fotky karty: obal, inak prvá ostrá skutočná, vizualizácia len pri obal_vizualizacia so
        štítkom), kóta s rokmi stavby alebo rokom príspevku, nadpis s odkazom, druh a miesto, rola. data: atribúty
        pre filter a zoradenie v app.js (pevné kľúče a čísla); neskor (karta za prvou dávkou zoznamu): data-neskor."""
        url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
        f = self.foto_karty(r)
        nm = self.nazov_s_miestom(r)
        stitok = ''
        if f:
            # lcp: prvá karta prehľadu je LCP, bez loading=lazy a s fetchpriority=high (Lighthouse lcp-discovery, kolo 1)
            foto = self.img_karty(pre, r, f, self.alt_bez_cisla(f) or nm, lcp=lcp)
            if f.get('viz'):
                stitok = '<span class="stitok-viz">Vizualizácia</span>'
        else:
            # realizácia bez jedinej ostrej fotky (skutočný obsah taký nemá, test_kazda_karta_ma_fotku)
            foto = self.znak_karty(pre)
        roky = self.roky_stitok(r)
        # zoznam realizácií (data): kóta bez pretáčania číslic, kratšie HTML pri desiatkach kariet
        kota = self.kota(roky, staticka=data) if roky else '<span class="kota kota-prazdna" aria-hidden="true"></span>'
        udaje = BODKA.join(T(x) for x in (self.druh_karty(r), r['miesto']) if x)
        atributy = ''
        if data:
            atributy = (f' data-druh="{" ".join(r["kategorie"])}" data-rok="{self.rok_filtra(r)}" '
                        f'data-mesto="{esc(kluc_mesta(self.mesto(r)))}" data-poradie="{int(poradie)}"'
                        + (' data-neskor' if neskor else ''))
        return (f'<li class="dlazdica projekt projekt-svetly{"" if f else " bez-fotky"}"{atributy}><figure>'
                f'<a class="projekt-foto" href="{url}" tabindex="-1" aria-hidden="true">{foto}{stitok}</a>'
                f'<figcaption>{kota}<{nadpis} class="dlazdica-nazov"><a href="{url}">{T(r["nazov"])}</a></{nadpis}>'
                + (f'<p class="projekt-udaje">{udaje}</p>' if udaje else '')
                + (f'<p class="projekt-text">{T(r["rola"])}</p>' if r['rola'] else '') + '</figcaption></figure></li>')

    def obrazok_dokladu(self, o):
        """Zakrytý obrázok osvedčenia SKSI pre stránku Oprávnenia (zadanie bmb-fb-uplnost, Navyše; FAKTY-WEB.md časť
        Oprávnenia: osvedčenia SKSI). Zdroj len zo šablóny: nastavenia.json opravnenia_obrazky (evidenčné číslo ->
        súbor v sablona/opravnenia/, kópia zakrytej verzie z ops/weby-na-mieru/klienti/bmbuilding/opravnenia/;
        originály s rodným číslom nikdy). Výstup opravnenia/<meno>-<šírka>.webp zmenšením, prevodom formátu alebo
        zväčšením najviac 2-krát Lanczosom s jemným doostrením (bez AI, linka bmb-presnost). None, keď obrázok nie
        je určený alebo chýba."""
        mapa = self.nastavenia.get('opravnenia_obrazky')
        rel = mapa.get(o['cislo']) if isinstance(mapa, dict) and o['cislo'] else None
        if not rel:
            return None
        rel = str(rel).replace('\\', '/')
        if not re.match(r'^opravnenia/[a-z0-9-]+\.(png|jpg|jpeg|webp)$', rel):
            varuj(f'nastavenia opravnenia_obrazky: neplatná cesta {rel!r}, obrázok vynechaný')
            return None
        p = SABLONA / rel
        if not p.is_file():
            varuj(f'sablona/{rel} chýba, osvedčenie {o["cislo"]} bez obrázka')
            return None
        w, h = sken_dokladu(p).size
        meno = Path(rel).stem
        # aj zväčšené súbory pre hustotu 2 (sirky_dokladu, doklad_webp_bajty: Lanczos a jemné doostrenie, bez AI);
        # w a h ostávajú rozmerom skenu orezaného na hranu papiera (atribúty img a data-sirka: lightbox doklad nezväčší
        # nad šírku skenu v CSS px)
        sirky = sirky_dokladu(w)
        cesty = {}
        for s in sirky:
            v = max(1, round(h * s / w))
            cesta = f'opravnenia/{meno}-{s}.webp'
            odt = odtlacok_suboru(p, VERZIA_DOKLADU, OREZ_DOKLADU.get(meno), s, v)
            self.obrazky[cesta] = (odt, (lambda p=p, s=s, v=v: doklad_webp_bajty(p, s, v)))
            cesty[s] = (cesta, v)
        return {'w': w, 'h': h, 'cesty': cesty}

    def doklady(self, pre, cinnosti=None, nadpis='h3', obrazky=False):
        out = []
        n_obr = 0

        def riadky_dokladu(o):
            return [('Vydala', o['organizacia']), ('Doklad', o['nazov']), ('Meno', o['osoba']), ('Činnosť', o['cinnost']),
                    ('Odborné zameranie', o['zameranie']), ('Evidenčné číslo', o['cislo']), ('Vydané', o['datum'])]
        vybrane = [o for o in self.firma['opravnenia'] if not (cinnosti and o['cinnost'] not in cinnosti)]
        # stránka Oprávnenia (obrazky): doklady stoja vedľa seba, tabuľky majú rovnaké riadky v rovnakom poradí, aby
        # riadky oboch tabuliek ležali v spoločných riadkoch mriežky (styl.css riadkov-N, linka bmb-presnost); riadok,
        # ktorý má aspoň jeden doklad, majú všetky, chýbajúca hodnota je „v osvedčení neuvedené“ (osvedčenie stavebného
        # dozoru 2010 odborné zameranie neuvádza, zakrytý sken sablona/opravnenia/osvedcenie-sksi-stavebny-dozor-2010.png)
        spolocne = None
        if obrazky and len(vybrane) > 1:
            spolocne = {k for o in vybrane for k, v in riadky_dokladu(o) if v}
        neuvedene = self.texty['opravnenia'].get('neuvedene', 'v osvedčení neuvedené')
        for o in vybrane:
            # nadpis = činnosť a zameranie, aby sa dva doklady SKSI dali rozlíšiť (brána dizajnu pokus 1, nález 10)
            hlavicka = ', '.join(x for x in (zaciatok_velky(o['cinnost']), o['zameranie']) if x) or o['nazov'] or 'Osvedčenie'
            if spolocne is None:
                riadky = [(k, v) for k, v in riadky_dokladu(o) if v]
            else:
                riadky = [(k, v or neuvedene) for k, v in riadky_dokladu(o) if k in spolocne]
            dl = ''.join(f'<div><dt>{T(k)}</dt><dd>{T(v)}</dd></div>' for k, v in riadky)
            trieda = f'doklad riadkov-{len(riadky)}' if obrazky and 1 <= len(riadky) <= 7 else 'doklad'
            figura = ''
            obr = self.obrazok_dokladu(o) if obrazky else None
            if obr:
                n_obr += 1
                sirky = sorted(obr['cesty'])
                srcset = ', '.join(f'{esc(pre + obr["cesty"][s][0])} {s}w' for s in sirky)
                plna = esc(pre + obr['cesty'][sirky[-1]][0])
                src = esc(pre + obr['cesty'][sirky[0]][0])
                popis = ', '.join(x for x in (o['cinnost'], o['zameranie'] and f'odborné zameranie {o["zameranie"]}',
                                              o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                                              o['datum'] and f'vydané {o["datum"]}') if x)
                alt = ', '.join(x for x in (o['nazov'] or 'Osvedčenie', o['organizacia'], o['osoba']) if x)
                alt += f': {popis}; osobné údaje zakryté'
                udaje = LB_ODDELOVAC.join(x for x in (o['nazov'], o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                                                      o['datum'] and f'vydané {o["datum"]}') if x)
                # odkaz na plný obrázok (bez JS sa otvorí priamo), app.js ho otvorí v lightboxe skupiny osvedcenia;
                # data-presne: lightbox obrázok nezväčší nad šírku súboru (doklad, nie fotka)
                figura = (f'<figure class="doklad-obr"><a class="doklad-odkaz" href="{plna}" data-lightbox data-galeria="osvedcenia" '
                          f'data-plna="{plna}" data-srcset="{srcset}" data-sirka="{obr["w"]}" data-vyska="{obr["h"]}" '
                          f'data-nazov="{esc("Osvedčenie SKSI: " + hlavicka)}" data-popis="" data-udaje="{esc(udaje)}" '
                          f'data-index="{n_obr}" data-alt="{esc(alt)}" data-presne="1">'
                          f'<img src="{src}" srcset="{srcset}" sizes="(min-width: 640px) min(528px, 45vw), calc(100vw - 32px)" '
                          f'width="{obr["w"]}" height="{obr["h"]}" alt="{esc(alt)}" decoding="async"></a>'
                          f'<figcaption class="doklad-popis">{T(self.popis_dokladu(o))}</figcaption></figure>')
            # odkaz do verejného zoznamu SKSI (Verejný portál SKSI): len pri osvedčení s obrázkom, len adresa zo šablóny
            # (nastavenia.json opravnenia_register, kľúč = evidenčné číslo) a len na hostiteľa verejnyportal.sksi.sk
            overit = ''
            reg = self.nastavenia.get('opravnenia_register')
            url = bezpecna_url(reg.get(o['cislo']), ['verejnyportal.sksi.sk']) if obrazky and isinstance(reg, dict) and o['cislo'] else ''
            if url:
                nap = self.texty['opravnenia'].get('register_overit', 'Overiť v registri SKSI')
                overit = (f'<p class="doklad-register"><a class="odkaz-von" href="{esc(url)}" rel="noopener" '
                          f'aria-label="{esc(nap + ": " + o["cinnost"])}">{T(nap)} {SVG_VON}</a></p>')
            out.append(f'<div class="{trieda}"><{nadpis} class="doklad-h">{T(hlavicka)}</{nadpis}>{figura}'
                       f'<dl class="fakty">{dl}</dl>{overit}</div>')
        return out

    def doklady_kratko(self, cinnosti=None, nadpis='h2'):
        """Box stránky služby s viacerými osvedčeniami (Stavebný manažment: stavbyvedúci aj stavebný dozor) skrátene:
        vydavateľ a meno raz, potom jeden riadok na osvedčenie (činnosť, odborné zameranie, evidenčné číslo a dátum
        vydania), celé tabuľky so skenmi sú na stránke Oprávnenia (odkaz v boxe). Audit naplno 25. 11. 2026, položka 25:
        dve celé tabuľky (13 riadkov) dali boxu pri 1366 px 1 127 px, text služby mal 553 px a vedľa boxu ostalo pod
        textom 505 až 599 px prázdneho stĺpca. Len polia obsah/firma.json, žiadne ručne písané číslo. None, keď je
        osvedčenie len jedno alebo majú rôzneho vydavateľa či držiteľa (vtedy celé tabuľky ako doteraz)."""
        vybrane = [o for o in self.firma['opravnenia'] if not (cinnosti and o['cinnost'] not in cinnosti)]
        if len(vybrane) < 2:
            return None
        org = {o['organizacia'] for o in vybrane}
        osoby = {o['osoba'] for o in vybrane}
        if len(org) != 1 or len(osoby) != 1:
            return None
        org, osoba = org.pop(), osoby.pop()
        riadky = [(k, T(v)) for k, v in (('Vydala', org), ('Meno', osoba)) if v]
        for o in vybrane:
            # nezalomí sa len „číslo 00912*10*,“ (span.nz aj s čiarkou za ním) a dátum (pevné medzery zo sadzby); čiarka
            # je vždy v tom istom celku ako slovo pred ňou. Oprava po overení (kontrola dizajnu, nález vážne): span.nz
            # okolo celého „evidenčné číslo …“ a čiarka mimo neho dali v boxe pri 1024 až 1064 px riadok len s čiarkou,
            # pri 1104 až 1248 px riadok začínajúci čiarkou a pri 1024 až 1096 px číslo cez pravý rámik (o 3 až 28 px)
            kusy = [(pred, hodnota, nz) for pred, hodnota, nz in (('', o['zameranie'], False),
                                                                  ('evidenčné ', o['cislo'] and f'číslo {o["cislo"]}', True),
                                                                  ('vydané ', o['datum'], False)) if hodnota]
            casti = []
            for i, (pred, hodnota, nz) in enumerate(kusy):
                hodnota += ',' if i < len(kusy) - 1 else ''
                casti.append(T(pred) + (f'<span class="nz">{T(hodnota)}</span>' if nz else T(hodnota)))
            riadky.append((zaciatok_velky(o['cinnost']) or 'Osvedčenie', ' '.join(casti)))
        dl = ''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in riadky if v)
        nazov = 'Osvedčenia SKSI' if 'SKSI' in org else 'Osvedčenia'
        return f'<div class="doklad doklad-kratky"><{nadpis} class="doklad-h">{T(nazov)}</{nadpis}><dl class="fakty">{dl}</dl></div>'

    @staticmethod
    def popis_dokladu(o):
        """Doslovný popis osvedčenia podľa zadania bmb-fotky (časť 3): „Osvedčenie SKSI o vykonaní ...: činnosť, odborné
        zameranie, evidenčné číslo X, vydané D“. Skladá sa len z polí obsah/firma.json, žiadne ručne písané číslo."""
        nazov = o['nazov'] or 'Osvedčenie'
        if 'SKSI' in o['organizacia'] and nazov.startswith('Osvedčenie ') and 'SKSI' not in nazov:
            nazov = 'Osvedčenie SKSI ' + nazov[len('Osvedčenie '):]
        casti = [x for x in (o['cinnost'], o['zameranie'], o['cislo'] and f'evidenčné číslo {o["cislo"]}',
                             o['datum'] and f'vydané {o["datum"]}') if x]
        return nazov + (': ' + ', '.join(casti) if casti else '')

    def otazky_html(self, kluce, h='h3', prva_otvorena=False):
        """Rozbaľovacie otázky zo stavebného zákona (details a summary, funguje bez JS), pri každej zdroj Slov-Lex.
        prva_otvorena: prvá otázka je otvorená (Činnosť na úvode, ako náhľad pred 1. kolom)."""
        z = self.texty['zakon']
        out = []
        for k in kluce:
            q = self.texty['otazky'].get(k)
            if not q:
                continue
            otv = ' open' if prva_otvorena and not out else ''
            out.append(f'<details class="otazka"{otv}><summary><{h}>{T(q["otazka"])}</{h}></summary><p>{T(q["odpoved"])}</p>'
                       f'<p class="zdroj">Zdroj: <a href="{esc(z["url"])}">{T(z["nazov"])}, {T(q["zdroj"])}</a></p></details>')
        return ''.join(out)

    def otazky_sekcia(self, kluce, ident):
        otazky = self.otazky_html(kluce)
        if not otazky:
            return ''
        return (f'<section class="sekcia otazky-sekcia" aria-labelledby="{ident}"><div class="obsah mriezka">'
                f'<h2 class="nadpis otazky-nadpis" id="{ident}">Čo hovorí stavebný zákon</h2>'
                f'<div class="otazky">{otazky}</div></div></section>')

    def realizacia_novinky(self, n):
        return next((r for r in self.realizacie if r['slug'] == n.get('realizacia')), None) if n.get('realizacia') else None

    def novinka_karta(self, pre, n, nadpis='h3'):
        """Svetlá karta novinky ako karta realizácie: fotka 4 : 5 (prvá ostrá skutočná, aj na výšku), kóta s dátumom
        príspevku, nadpis s odkazom, stavba s rokmi (pole realizacia), úryvok textu. Bez vlastnej použiteľnej fotky
        ilustračná fotka stavby so štítkom a popisom „Ilustračná fotka: …“ (ilustracna_fotka); logo len keď chýba aj tá."""
        url = esc(self.rel(pre, f'novinky/{n["slug"]}/'))
        f = n['karta']
        stitok = ilustr = ''
        if f:
            foto = self.img_karty(pre, n, f, self.alt_bez_cisla(f) or n['nadpis'])
            if f.get('viz'):
                stitok = '<span class="stitok-viz">Vizualizácia</span>'
        elif n.get('ilustracna_foto'):
            f = n['ilustracna_foto']
            foto = self.img_karty(pre, n['ilustracna_r'], f, n['ilustracna'])
            stitok = '<span class="stitok-viz stitok-ilustr">Ilustračná fotka</span>'
            ilustr = f'<p class="projekt-ilustr">{T(n["ilustracna"])}</p>'
        else:
            foto = self.znak_karty(pre)
        ukazka = skrat(re.sub(r'\s+', ' ', n['text']), 160) if n['text'] else ''
        if ukazka and ukazka.strip() == n['nadpis'].strip():
            ukazka = ''
        r = self.realizacia_novinky(n)
        stavba = ''
        if r:
            stavba = BODKA.join(T(x) for x in (r['nazov'], self.roky_stitok(r)) if x)
        return (f'<li class="novinka-karta projekt projekt-svetly{"" if f else " karta-bez-fotky"}"><figure>'
                f'<a class="projekt-foto{"" if f else " projekt-znak"}" href="{url}" tabindex="-1" aria-hidden="true">{foto}{stitok}</a>'
                f'<figcaption>{self.kota(datum_text(n["datum"]))}<{nadpis} class="novinka-nazov"><a href="{url}">{T(n["nadpis"])}</a></{nadpis}>'
                + ilustr + (f'<p class="projekt-udaje">{stavba}</p>' if stavba else '')
                + (f'<p class="projekt-text">{T(ukazka)}</p>' if ukazka else '') + '</figcaption></figure></li>')

    # --- úvod
    def slider_fotky(self):
        """Fotky slidera stavieb v úvode (1. kolo pripomienok 24. 11. 2026, ZMENY-KOLO-1.md 3.3): nastavenia.json
        uvod_slider, zoznam {"slug", "subor", "stred": [x, y]} v pevnom poradí, ktoré vybral klient. Bez súboru fotka
        karty realizácie. Fotka smie byť aj menšia než prah velka (vybral ju klient), ale musí byť v galérii (nie
        rozmazaná) a nesmie byť vizualizácia ani pôvodný stav. stred: bod fotky v podieloch 0 až 1, ktorý ostane
        v ráme pri orezaní (object-position), predvolene stred. Chýbajúca realizácia alebo fotka je prísne varovanie
        (lokálne sa vynechá, v GitHub Actions chyba zostavy). Výsledok [(r, f, (x, y))] sa počíta raz."""
        if getattr(self, '_slider', None) is not None:
            return self._slider
        zoznam = self.nastavenia.get('uvod_slider')
        out = []
        self.slider_orez = {}
        self.slider_nazov = {}
        if not isinstance(zoznam, list) or not zoznam:
            if self.realizacie:
                varuj('nastavenia uvod_slider chýba alebo nie je zoznam: úvod bez slidera stavieb')
            self._slider = self.zalozny_slider() if self.realizacie else out
            return self._slider
        podla = {r['slug']: r for r in self.realizacie}
        for polozka in zoznam:
            if not isinstance(polozka, dict):
                varuj(f'nastavenia uvod_slider: položka {polozka!r} nie je objekt, vynechaná')
                continue
            slug = jeden_riadok(polozka.get('slug'))
            subor = jeden_riadok(polozka.get('subor'))
            r = podla.get(slug)
            if r is None:
                varuj_prisne(f'uvod_slider: realizácia {slug!r} nie je v obsahu, fotka vynechaná')
                continue
            f = next((x for x in r['galeria'] if x['rel'] == f'{slug}/{subor}'), None) if subor else r['karta']
            if f is None or f.get('viz') or self.povodny_stav(f):
                varuj_prisne(f'uvod_slider: fotka {subor or "karty"} realizácie {slug} chýba, je rozmazaná, '
                             'vizualizácia alebo pôvodný stav, vynechaná')
                continue
            if any(x[1] is f for x in out):
                varuj(f'nastavenia uvod_slider: fotka {f["rel"]} je v zozname dvakrát, druhá vynechaná')
                continue
            stred = polozka.get('stred', [0.5, 0.5])
            if not (isinstance(stred, list) and len(stred) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                                       and 0 <= v <= 1 for v in stred)):
                varuj(f'nastavenia uvod_slider: stred {stred!r} fotky {f["rel"]} nie je [x, y] v podieloch 0 až 1, stred fotky')
                stred = [0.5, 0.5]
            # orez_dole: podiel výšky fotky odspodu, ktorý sa v ráme neukáže nikdy, ani v ráme na výšku (vysoké okno,
            # mobil), kde cover inak ukáže celú výšku fotky na výšku: RD Ahoj 01 má dole cestu s kanálom (ÚVOD v3)
            orez = polozka.get('orez_dole', 0)
            if not (isinstance(orez, (int, float)) and not isinstance(orez, bool) and 0 <= orez <= 0.4):
                varuj(f'nastavenia uvod_slider: orez_dole {orez!r} fotky {f["rel"]} nie je číslo 0 až 0,4, bez orezu')
                orez = 0
            if orez:
                self.slider_orez[f['rel']] = float(orez)
            # nazov_mobil: kratší názov stavby v popise pod fotkou do 1023 px, kde má popis jeden riadok na názov
            # (doladenie-1, nález drobné: dlhý názov sa pri 390 a 360 px skrátil tromi bodkami uprostred slova), a od
            # doladenia-2 aj v štítku na fotke pri 1024 až 1099 px (celý názov mal až 5 riadkov); len časť úradného
            # názvu z obsahu, ulica je v riadku miesta; celý názov ostáva od 1100 px a na stránke stavby
            kratky = polozka.get('nazov_mobil')
            if kratky is not None:
                kratky = jeden_riadok(kratky) if isinstance(kratky, str) else ''
                if 0 < len(kratky) <= 40:
                    self.slider_nazov[f['rel']] = kratky
                else:
                    varuj(f'nastavenia uvod_slider: nazov_mobil fotky {f["rel"]} nie je text 1 až 40 znakov, celý názov')
            out.append((r, f, (float(stred[0]), float(stred[1]))))
        self._slider = out or self.zalozny_slider()
        return self._slider

    def zalozny_slider(self):
        """Keď z uvod_slider neostala žiadna fotka (iný obsah, napríklad testovací), hrdina nie je prázdny: fotka karty
        prvej realizácie zo záložného výberu (skutočná, nie vizualizácia ani pôvodný stav), s varovaním."""
        r = next((x for x in self.zalozny_vyber() if not x['karta'].get('viz') and not self.povodny_stav(x['karta'])), None)
        if r is None:
            return []
        varuj(f'uvod_slider: bez použiteľnej fotky, úvod ukáže fotku karty realizácie {r["slug"]}')
        return [(r, r['karta'], (0.5, 0.5))]

    def hrdina_foto(self):
        """Prvá fotka slidera úvodu (LCP a OG obrázok úvodu), (r, f) alebo (None, None). Nástroje (nastroje/predlohy_web.py,
        titulné fotky s prísnejším prahom) ju čítajú týmto menom ako predtým fotku pohľadu A."""
        fotky = self.slider_fotky()
        return (fotky[0][0], fotky[0][1]) if fotky else (None, None)

    def mierka(self, pre):
        """Časová mierka (grafická mierka z ukážky preložená na roky): os od najstaršieho po najnovší rok realizácie,
        dielik na každý rok, výška dielika podľa počtu realizácií ukončených v tom roku. Len realizácie so známymi
        rokmi (rok z dátumu príspevku nie je rok stavby). Rastie s obsahom, nič nie je natvrdo."""
        so_rokmi = [r for r in self.realizacie if r['rok'] and not r['rok_z_datumu']]
        if len(so_rokmi) < 2:
            return ''
        od, do = min(r['rok_od'] for r in so_rokmi), max(r['rok'] for r in so_rokmi)
        if do - od < 2:
            return ''
        pocty = {}
        for r in so_rokmi:
            pocty[r['rok']] = pocty.get(r['rok'], 0) + 1
        mx = max(pocty.values())
        popisy = {od, do} | {y for y in range(od, do + 1) if y % 10 == 0 and y - od >= 3 and do - y >= 3}
        bunky = []
        for y in range(od, do + 1):
            n = pocty.get(y, 0)
            v = 0 if not n else max(1, min(4, round(n / mx * 4)))
            bunky.append(f'<span class="m-rok v{v}">' + (f'<b>{y}</b>' if y in popisy else '') + '</span>')
        # na zozname realizácií (Stavba 2) je mierka pod filtrom ako popis, nie odkaz na tú istú stránku
        return (f'<div class="casova-mierka"><span class="mierka-os" aria-hidden="true">{"".join(bunky)}</span>'
                f'<span class="mierka-pozn">{T(f"Realizácie so známymi rokmi podľa roku ukončenia, {od} až {do}. Modrý dielik: počet stavieb v tom roku.")}</span></div>')

    # --- úvod (1. kolo pripomienok 24. 11. 2026): hrdina so sliderom a O firme, Činnosť, Kontakt (pás prác preč, 3A.6)
    def foto_nastavenia(self, kluc):
        """Fotka sekcie z nastavenia.json (slug a súbor), len skutočná ostrá fotka z galérie; inak (None, None).
        Úvod ju od 1. kola pripomienok nepoužíva (Postup a samostatná sekcia O firme sú preč); ostáva pre nástroje
        (nastroje/predlohy_web.py číta postup_foto a firma_foto, nenastavené dajú (None, None) bez varovania).
        Nenastavený kľúč je sekcia bez fotky; nastavená realizácia alebo fotka, ktorá chýba, je prísne varovanie
        (v Actions chyba zostavy, fb-ostro A4)."""
        nast = self.nastavenia.get(kluc) or {}
        r = next((x for x in self.realizacie if x['slug'] == nast.get('slug')), None)
        if not r:
            if nast.get('slug'):
                varuj_prisne(f'{kluc}: realizácia {nast.get("slug")!r} nie je v obsahu')
            return None, None
        subor = jeden_riadok(nast.get('subor'))
        f = next((x for x in r['galeria'] if x['rel'] == f'{r["slug"]}/{subor}' and not x.get('viz')), None)
        if f is None:
            varuj_prisne(f'{kluc}: fotka {subor} realizácie {r["slug"]} chýba, je rozmazaná alebo je vizualizácia')
            return None, None
        return r, f

    @staticmethod
    def alt_bez_cisla(f):
        return re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')

    def druh_karty(self, r):
        """Druh stavby na karte (jednotné číslo) z vlastného názvu, potom z textu realizácie; nič nevymýšľa."""
        for zdroj in (r['nazov'], r['text']):
            for nazov, vzor in DRUHY_KARTY:
                if re.search(vzor, zdroj, re.I):
                    return nazov
        return ''

    def lb_data(self, pre, r, f, i, n, nazov, popis_udaje, skupina='', nahlad=False, popis=None):
        """Odkaz na plnú fotku a jeho dáta pre lightbox (zmluva PRIPRAVA-BUILD.md bod 3, rozšírená o viac galérií
        na stránke: data-galeria, data-alt; vizualizácia data-viz a data-autor). nahlad: odkaz obsahuje náhľad
        self.img celej fotky (galéria, nie malá fotka), ktorého srcset WebP a zdroj AVIF sú bajtovo rovnaké ako
        data-srcset a data-avif; tie sa vtedy vynechajú a app.js ich vezme z náhľadu (overenie zostavy bmb-presnost,
        menšie HTML detailu s galériou, meranie v ops/weby-na-mieru/klienti/bmbuilding/presnost/overenie-zostava.md)."""
        cesty = self.obrazok_cesty(r, f)
        plna = esc(pre + cesty[f['plna_w']])
        alt = self.alt_bez_cisla(f)
        alt = f'{alt}, fotka {i} z {n}' if alt and n > 1 else alt
        # data-avif: srcset AVIF pre zdroj type=image/avif v lightboxe (app.js), WebP ostáva v data-srcset
        avif = self.srcset(pre, r, f, 'avif')
        data = {'data-plna': plna}
        if not nahlad:
            data['data-srcset'] = self.srcset(pre, r, f)
            if avif:
                data['data-avif'] = avif
        # data-sirka a data-vyska: plný súbor WebP (odkaz), lightbox fotku nezväčší nad jeho 1,25 násobok
        data.update({'data-sirka': str(f['plna_w']),
                'data-vyska': str(f['plna_h']), 'data-nazov': esc(nazov),
                'data-popis': '' if f.get('viz') else esc(self.popis_fotky(r, f) if popis is None else popis),
                'data-udaje': esc(popis_udaje), 'data-index': str(i), 'data-alt': esc(alt)})
        if skupina:
            data['data-galeria'] = esc(skupina)
        if f.get('viz'):
            data['data-viz'] = '1'
            data['data-autor'] = esc(f.get('autor', ''))
        return plna, alt, ' '.join(f'{k}="{v}"' for k, v in data.items())

    @staticmethod
    def udaje_lightboxu(r):
        """Údaje stavby pod fotkou v lightboxe, časti oddelené LB_ODDELOVAC: rola BM Building (malým písmenom uprostred
        vety), projektant, roky. app.js pri vizualizácii vynechá projektanta, ak je to ten istý človek ako autor
        (overenie kolo 1: „autor X, … projektant X“ dvakrát a „Stavebný dozor“ s veľkým písmenom uprostred vety)."""
        return LB_ODDELOVAC.join(x for x in (f'rola BM Building: {male(r["rola"])}' if r['rola'] else '',
                                             f'projektant {r["projektant"]}' if r['projektant'] else '',
                                             f'autor {r["autor"]}' if r.get('autor') else '', r['roky']) if x)

    def lightbox_dialog(self, pre, nazov, r, f):
        """Jeden dialog#lightbox na stránku (realizácia a novinka: jej galéria)."""
        return self.lightbox_html(f'Fotky: {nazov}', pre + self.obrazok_cesty(r, f)[f['plna_w']], f['max_w'], f['max_h'])

    @staticmethod
    def lightbox_html(nazov, src, w, h, slovo=''):
        """Markup dialog#lightbox (zmluva app.js časť 6) s prvým obrázkom src w x h; nazov ide do aria-label.
        slovo (doklady na stránke Oprávnenia): počítadlo „<slovo> 1 z 2“ a aria-label bez predpony Fotky."""
        navyse = f' data-predpona="" data-slovo="{esc(slovo)}"' if slovo else ''
        return (f'<dialog class="lightbox" id="lightbox" aria-label="{esc(nazov)}"{navyse}>'
                f'<figure class="lightbox-figura"><img class="lightbox-foto" src="{esc(src)}" '
                f'width="{w}" height="{h}" alt="" loading="lazy" decoding="async">'
                '<span class="lightbox-stitok" hidden>Vizualizácia</span>'
                '<figcaption class="lightbox-popis" aria-live="polite"><span class="lightbox-pocet"></span><b class="lightbox-nazov"></b>'
                '<span class="lightbox-udaje"></span></figcaption></figure>'
                f'<button class="lightbox-tl lightbox-predosla" type="button" data-predosla>{SVG_SIPKA_SPAT}<span>Predošlá</span></button>'
                f'<button class="lightbox-tl lightbox-dalsia" type="button" data-dalsia><span>Ďalšia</span>{SVG_SIPKA}</button>'
                f'<button class="lightbox-tl lightbox-zavriet" type="button" data-zavriet>{SVG_KRIZIK}<span>Zavrieť</span></button></dialog>')

    def zalozny_vyber(self):
        """Záložné poradie realizácií pre slider úvodu bez použiteľnej fotky z uvod_slider: len s fotkou karty, najprv so
        skutočnou fotkou (nie len vizualizácia), so známymi rokmi stavby a s najviac fotkami v galérii; pri zhode
        novšia podľa zoznamu realizácií. Nič sa nevymýšľa, len sa vyberá z obsahu."""
        poradie = {r['slug']: i for i, r in enumerate(self.realizacie)}
        return sorted((r for r in self.realizacie if r['karta']),
                      key=lambda r: (bool(r['karta'].get('viz')), not (r['roky'] and not r['rok_z_datumu']),
                                     -len(r['galeria']), poradie[r['slug']]))

    def cinnost_casti(self):
        """Časti doslovného textu Činnosti (texty.json cinnost_doslova) v poradí kľúčov: [(kľúč, časť)]. Časť bez nadpisu
        alebo iného typu než objekt sa vynechá s varovaním (zlý JSON nesmie zhodiť build)."""
        out = []
        for k, c in (self.texty.get('cinnost_doslova') or {}).items():
            if k.startswith('_'):
                continue
            if not isinstance(c, dict) or not jeden_riadok(c.get('nadpis')):
                varuj(f'texty.json cinnost_doslova.{k}: chýba nadpis alebo nie je objekt, časť vynechaná')
                continue
            out.append((k, c))
        return out

    @staticmethod
    def zoznam_textov(hodnota, meno):
        """Zoznam viet z texty.json; iný typ než zoznam je varovanie a prázdny zoznam."""
        if hodnota is None or isinstance(hodnota, list):
            return hodnota or []
        varuj(f'texty.json: {meno} nie je zoznam, vynechané')
        return []

    def doslovny_text(self, kluc, c, stlpce=True, uvodna=False, riadky=False):
        """HTML jednej časti doslovného textu starého webu (texty.json cinnost_doslova, ZMENY-KOLO-1.md časť 4): odseky,
        potom úvodná veta a body, druhá úvodná veta a body, v tomto poradí a slovo od slova. Jeden zdroj pre Činnosť na
        úvode aj hlavný text stránky služby. stlpce: zoznam s viac ako štyrmi bodmi na počítači v dvoch stĺpcoch (CSS
        cinnost-body-dlhe). uvodna: prvý odsek väčším písmom (stránka služby). riadky (stránka služby, stlpce=False):
        zoznam s viac ako štyrmi bodmi dostane len triedu riadky-N, do 1920 px ostáva v jednom stĺpci a v dvoch
        stĺpcoch so spoločnými riadkami je až od 1921 px (styl.css úsek ŠIROKÉ OKNO)."""
        odseky = self.zoznam_textov(c.get('odseky'), f'cinnost_doslova.{kluc}.odseky')
        trieda = ' class="uvodna"'
        telo = ''.join(f'<p{trieda if uvodna and i == 0 else ""}>{T(jeden_riadok(o))}</p>' for i, o in enumerate(odseky))
        for u, b in (('uvod', 'body'), ('uvod_2', 'body_2')):
            if jeden_riadok(c.get(u)):
                telo += f'<p class="cinnost-uvod">{T(jeden_riadok(c[u]))}</p>'
            body = [jeden_riadok(x) for x in self.zoznam_textov(c.get(b), f'cinnost_doslova.{kluc}.{b}') if jeden_riadok(x)]
            if body:
                # dva stĺpce so spoločnými riadkami (krasa-8): v ľavom stĺpci polovica bodov nahor, trieda riadky-N
                # (styl.css má N od 3 do RIADKY_ZOZNAMU_MAX); dlhší zoznam ide v stĺpcoch po riadkoch, bez triedy
                dlhe = ''
                if (stlpce or riadky) and len(body) > 4:
                    n = (len(body) + 1) // 2
                    pocet = f' riadky-{n}' if n <= RIADKY_ZOZNAMU_MAX else ''
                    dlhe = ' cinnost-body-dlhe' + pocet if stlpce else pocet
                telo += f'<ul class="cinnost-body{dlhe}">' + ''.join(f'<li>{T(x)}</li>' for x in body) + '</ul>'
        return telo

    def realizacie_sluzby(self, slug):
        """Realizácie s rolou služby (texty.json sluzby.<slug>.kluce v poli rola, bez ohľadu na veľkosť písmen): jeden
        výpočet pre zoznam na stránke služby aj počet v hlave karty Činnosti na úvode."""
        kluce = (self.texty['sluzby'].get(slug) or {}).get('kluce') or []
        return [r for r in self.realizacie if any(kl in r['rola'].lower() for kl in kluce)]

    @staticmethod
    def slovo_realizacie(n):
        """Slovo k počtu: 1 realizácia, 2 až 4 realizácie, 0 a 5 a viac realizácií."""
        return 'realizácia' if n == 1 else 'realizácie' if 2 <= n <= 4 else 'realizácií'

    def cinnost_fakty(self, pre, slug, nadpis):
        """Hlava karty Činnosti ako technický list (ZMENY-KOLO-1.md 3A.7, náhľad pred 1. kolom): počet realizácií služby
        s odkazom na realizácie na stránke služby (len keď je aspoň jedna) a popisom z texty.json cinnost.hlava
        (pocet_popis), osvedčenie SKSI z roku vydania (obsah/firma.json opravnenia podľa cinnost.hlava osvedcenie)
        s odkazom na Oprávnenia, pod tým nenápadný odkaz na stránku služby. Nič sa nevymýšľa: počet z rolí, rok
        z dátumu vydania osvedčenia."""
        tc = self.texty['cinnost']
        h = (tc.get('hlava') or {}).get(slug) or {}
        riadky = []
        if slug in self.texty['sluzby']:
            n = len(self.realizacie_sluzby(slug))
            if n:
                popis = jeden_riadok(h.get('pocet_popis'))
                # služba v prístupnom mene odkazu, pre čítačku „13 realizácií: stavebný manažment“ (audit naplno
                # 25. 11. 2026, položka 30; WCAG 2.4.9 Link Purpose (Link Only), viditeľný text sa nemení)
                sluzba = male(jeden_riadok(self.texty['sluzby'][slug].get('menu')))
                riadky.append(f'<li class="cinnost-fakt"><a class="cinnost-pocet" href="{esc(self.rel(pre, slug + "/#real-nadpis"))}">'
                              f'<span class="cinnost-pocet-c cisla">{n}</span> <span>{self.slovo_realizacie(n)}</span>'
                              + (f'<span class="vh">: {T(sluzba)}</span>' if sluzba else '') + '</a>'
                              + (f'<small>{T(popis)}</small>' if popis else '') + '</li>')
        osv = jeden_riadok(h.get('osvedcenie'))
        o = next((x for x in self.firma['opravnenia'] if osv and x['cinnost'] == osv and 'SKSI' in x['organizacia']
                  and x['rok'].isdigit()), None)
        if o:
            riadky.append(f'<li class="cinnost-fakt"><a href="{esc(self.rel(pre, "opravnenia/"))}">'
                          f'{T("osvedčenie SKSI z roku " + o["rok"])}</a></li>')
        out = f'<ul class="cinnost-fakty">{"".join(riadky)}</ul>' if riadky else ''
        if slug in self.texty['sluzby']:
            out += (f'<p class="cinnost-odkaz"><a class="odkaz-sipka" href="{esc(self.rel(pre, slug + "/"))}">'
                    f'{T(tc.get("odkaz_sluzba", "Stránka služby"))}<span class="vh">: {nadpis}</span> {SVG_SIPKA}</a></p>')
        return out

    def veta_osvedceni(self):
        """Veta o osvedčeniach SKSI s činnosťou a dátumom vydania z obsah/firma.json (pod kartami Činnosti a v llms.txt),
        bez osvedčení prázdna."""
        osv = [o for o in self.firma['opravnenia'] if 'SKSI' in o['organizacia'] and o['cinnost'] and o['datum']]
        if not osv:
            return ''
        return (f'{self.texty["cinnost"].get("sksi_veta", "Osvedčenia Slovenskej komory stavebných inžinierov")}: '
                + '; '.join(f'{o["cinnost"]}, vydané {o["datum"]}' for o in osv) + '.')

    def cinnost_pata(self, pre):
        """Pod kartami Činnosti (ZMENY-KOLO-1.md 3A.7, ako náhľad pred 1. kolom): veta o osvedčeniach SKSI s dátumami
        vydania z obsah/firma.json a odkazom Oprávnenia, potom riadok listu „Čo hovorí stavebný zákon“ s rozbaľovacími
        otázkami z texty.json cinnost.otazky (zdroj Slov-Lex pri každej, prvá otvorená, „Kto je pri kolaudácii?“ nie)."""
        tc = self.texty['cinnost']
        out = ''
        veta = self.veta_osvedceni()
        if veta:
            out += (f'<p class="cinnost-sksi">{T(veta)} <a href="{esc(self.rel(pre, "opravnenia/"))}">Oprávnenia</a></p>')
        otazky = self.otazky_html(self.zoznam_textov(tc.get('otazky'), 'cinnost.otazky'), h='h4', prva_otvorena=True)
        if otazky:
            out += (f'<div class="zakon-list" role="group" aria-labelledby="zakon-uvod"><div class="zakon-hlava">'
                    f'<span class="cinnost-ikona zakon-ikona" aria-hidden="true">§</span>'
                    f'<h3 class="cinnost-nadpis zakon-nadpis" id="zakon-uvod">{T(tc.get("zakon_nadpis", "Čo hovorí stavebný zákon"))}</h3>'
                    f'</div><div class="otazky">{otazky}</div></div>')
        return out

    def cinnost(self, pre):
        """Činnosť hneď pod hrdinom úvodu (ZMENY-KOLO-1.md 3A.3 a 3A.7, menu Činnosť, kotva #cinnost): nadpis sekcie
        a celý doslovný text stránky Činnosť starého webu z texty.json cinnost_doslova, slovo od slova (opravené len
        medzery), nič skryté. Podoba technického listu (rozhodnutie Fable 24. 11. 2026 večer): tri služby ako riadky
        oddelené vlasovou čiarou; vľavo hlava (na počítači pripnutá pod hlavičkou webu) s ikonou, veľkým číslom ako
        kótou, nadpisom, počtom realizácií, osvedčením SKSI a odkazom na stránku služby; vpravo celý text, dlhé zoznamy
        v dvoch stĺpcoch s číslami v štýle kót (CSS počítadlo, text bodov ostáva doslovný). Na mobile hlava nad textom.
        Pod kartami veta o osvedčeniach SKSI a otázky zo zákona (cinnost_pata). Bez odkazov „všetkých N činností“."""
        tc = self.texty['cinnost']
        karty = []
        for i, (kluc, c) in enumerate(self.cinnost_casti(), 1):
            slug = jeden_riadok(c.get('slug'))
            hid = f'cinnost-{slug if SLUG_RE.match(slug) else kluc}'
            nadpis = T(jeden_riadok(c['nadpis']))
            telo = self.doslovny_text(kluc, c)
            ikona = IKONY_CINNOSTI.get(slug, '')
            karty.append(f'<article class="cinnost-karta" aria-labelledby="{hid}"><div class="cinnost-hlava">'
                         + (f'<span class="cinnost-ikona">{ikona}</span>' if ikona else '')
                         + f'<div class="cinnost-cislo" aria-hidden="true">{self.kota(f"{i:02d}", "kota-velka")}</div>'
                         f'<h3 class="cinnost-nadpis" id="{hid}">{nadpis}</h3>{self.cinnost_fakty(pre, slug, nadpis.rstrip(":"))}</div>'
                         f'<div class="cinnost-text">{telo}</div></article>')
        if not karty:
            return ''
        return (f'<section class="sekcia cinnost-sekcia" id="cinnost" aria-labelledby="cinnost-nadpis"><div class="obsah">'
                f'<h2 class="nadpis" id="cinnost-nadpis">{T(tc["nadpis"])}</h2>'
                f'<div class="cinnost-karty">{"".join(karty)}</div>{self.cinnost_pata(pre)}</div></section>')

    def slider(self, pre, fotky):
        """Slider stavieb v úvode (ZMENY-KOLO-1.md 3.3 a 3A.2): fotky v ráme cez seba (prelínanie riadi app.js, 8 s na
        fotku), popis aktuálnej stavby ako štítok v rohu fotky (názov s odkazom na realizáciu, miesto, len roky) a
        nenápadné ovládanie (na počítači na fotke, na mobile pod ňou): počítadlo v štýle kót a tlačidlá Predchádzajúca,
        Pozastaviť a Ďalšia. Prvá fotka je LCP (eager, fetchpriority high, preload s tými istými šírkami a sizes),
        ostatné majú skutočný zdroj v data-src, data-srcset a data-avif (tie isté šírky a kódovanie ako ostatné fotky)
        a app.js ich načíta tesne pred prechodom. Bez JS prvá fotka s popisom, ostatné skryté (NOSCRIPT_CSS
        img[data-src]) a ovládanie skryté (styl.css html:not(.js)). Výrez pri orezaní drží stred z nastavení (trieda snimka-N a pravidlo
        object-position v css_navyse, CSP nedovolí atribút style). sizes každej fotky podľa jej pomeru a orez_dole
        (sizes_slidera), AVIF so šírkami sirky_slidera (krasa-8: hustota aspoň 1 aj pri fotke 16 : 9)."""
        t = self.texty['uvod']
        n = len(fotky)
        snimky, popisy, css = [], [], []
        preload = []
        for i, (r, f, (sx, sy)) in enumerate(fotky):
            je = ' je' if i == 0 else ''
            alt = self.alt_bez_cisla(f) or self.nazov_s_miestom(r)
            # orez_dole: obrázok je vyšší než rám o túto časť, rám (overflow hidden) ju dole odreže pri každom pomere
            orez = getattr(self, 'slider_orez', {}).get(f['rel'])
            sizes = sizes_slidera(f['max_w'], f['max_h'], orez or 0.0)
            sirky_avif = self.sirky_slidera(f, 'avif')
            obr = self.img(pre, r, f, sizes, lcp=(i == 0), alt=alt, odlozene=(i > 0), sirky_avif=sirky_avif)
            if i == 0:
                # preload len formátu, ktorý picture naozaj vyberie (AVIF, bez neho WebP): dva by stiahli fotku dvakrát
                avif = self.srcset(pre, r, f, 'avif', sirky=sirky_avif)
                typ, s = ('image/avif', avif) if avif else ('image/webp', self.srcset(pre, r, f))
                preload.append(f'<link rel="preload" as="image" type="{typ}" imagesrcset="{s}" '
                               f'imagesizes="{sizes}" fetchpriority="high">')
            snimky.append(f'<li class="snimka snimka-{i}{je}" data-slug="{esc(r["slug"])}">{obr}</li>')
            pravidla = []
            if (sx, sy) != (0.5, 0.5):
                pravidla.append(f'object-position:{sx * 100:g}% {sy * 100:g}%')
            if orez:
                pravidla.append(f'height:{100 / (1 - orez):.2f}%')
            if pravidla:
                css.append(f'.snimka-{i} img{{{";".join(pravidla)}}}')
            url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
            udaje = [T(x) for x in (r['miesto'],) if x]
            roky = self.roky_karty(r)
            if roky:
                # roky sa nezalomia („2018 až“ a „2021“ na dvoch riadkoch v užšom štítku pri 1024 px, doladenie-1)
                udaje.append(f'<span class="cisla nz">{T(roky)}</span>')
            # kratší názov do 1099 px (nastavenia uvod_slider nazov_mobil, doladenie-1 a 2): CSS ukáže vždy len jeden
            # z dvoch spanov (display none ho vynechá aj z prístupného mena odkazu), bez neho celý názov
            kratky = getattr(self, 'slider_nazov', {}).get(f['rel'])
            nazov = (f'<span class="slider-nazov">{T(r["nazov"])}</span><span class="slider-nazov-kratky">{T(kratky)}</span>'
                     if kratky and kratky != r['nazov'] else T(r['nazov']))
            popisy.append(f'<li class="slider-popis{je}"><a href="{url}">{nazov}</a>'
                          + (f'<span class="slider-udaje">{BODKA.join(udaje)}</span>' if udaje else '') + '</li>')
        self.preload_hrdina = preload
        self.css_slider = '\n'.join(css) + ('\n' if css else '')
        pocet = f'{n:02d}'
        ovladanie = ''
        if n > 1:
            # počítadlo ako kóta s pretáčanými číslicami (pohybový jazyk pôvodného webu, ÚVOD v3): pri vstupe do obrazu
            # sa 01 / 11 raz pretočí (app.js trieda otocene); prvá zmena fotky prepíše číslo aktuálnej fotky textom.
            # Bez JS skryté cez styl.css html:not(.js), nie atribútom hidden: miesto ovládania je v rozložení od prvého
            # vykreslenia (doladenie-1: na mobile ho odkryl až app.js a údaje pod ním sa posunuli o 48 px)
            ovladanie = (f'<div class="slider-ovladanie" role="group" aria-label="{esc(t["slider_ovladanie"])}">'
                         f'<span class="kota slider-pocet"><span class="kota-text cisla otoc"><span class="slider-cislo">'
                         f'{self.valce_cisla("01")}</span><span class="kota-slovo"> / </span>'
                         f'<span class="slider-spolu">{self.valce_cisla(pocet, 2)}</span></span></span>'
                         f'<button class="slider-tl" type="button" data-slider="-1" aria-controls="slider-snimky" '
                         f'aria-label="Predchádzajúca">{SVG_SIPKA_SPAT}</button>'
                         f'<button class="slider-tl slider-pauza" type="button" data-slider="pauza" aria-controls="slider-snimky" '
                         f'aria-label="Pozastaviť" aria-pressed="false">{SVG_PAUZA}</button>'
                         f'<button class="slider-tl" type="button" data-slider="1" aria-controls="slider-snimky" '
                         f'aria-label="Ďalšia">{SVG_SIPKA}</button></div>')
        return (f'<div class="slider" role="region" aria-label="{esc(t["slider_nazov"])}" '
                f'data-interval="{SLIDER_INTERVAL_MS}" data-prelinanie="{SLIDER_PRELINANIE_MS}">'
                f'<ul class="snimky" id="slider-snimky">{"".join(snimky)}</ul>'
                f'<div class="slider-spodok"><ul class="slider-popisy" aria-live="off">{"".join(popisy)}</ul>'
                f'{ovladanie}</div></div>')

    @staticmethod
    def valce_cisla(text, od=0):
        """Číslice ako pretáčané stĺpce kóty (ako v Web.kota, triedy c0 až c9 a oneskorenie i0 až i9 od indexu od);
        čítačka číta skrytý text, stĺpce sú pred ňou skryté."""
        casti = ''.join(f'<span class="valec"><span class="stlpec c{c} i{min(9, od + i)}"></span></span>'
                        for i, c in enumerate(text) if c.isdigit())
        return f'<span class="vh">{esc(text)}</span><span class="valce" aria-hidden="true">{casti}</span>'

    def o_firme_text(self):
        """Dva odseky O firme pod H1 hrdinu (texty.json uvod.o_firme_hrdina, agent ÚVOD v3: pôvodné znenie sekcie
        O firme bez vety o výročí). Na mobile, tablete a v nižších oknách počítača celý blok pod hrdinom; vo vysokom okne
        počítača (od 1080 px, doladenie-2) prvý odsek vpravo pod H1 a druhý pod fotkou od čiary 8. stĺpca, nadpis len
        pre čítačku (krasa-8, styl.css display: contents). Bez textov nič."""
        odseky = [jeden_riadok(x) for x in self.zoznam_textov(self.texty['uvod'].get('o_firme_hrdina'), 'uvod.o_firme_hrdina')
                  if jeden_riadok(x)]
        if not odseky:
            return ''
        # nadtitulok O firme (oprava-2): blok alebo riadok pod hrdinom uvedie (styl.css .firma-text-nad)
        nad = jeden_riadok(self.texty['uvod'].get('o_firme_nadpis')) or 'O firme'
        return ('<div class="firma-text">' + f'<h2 class="firma-text-nad">{T(nad)}</h2>'
                + ''.join(f'<p>{T(o)}</p>' for o in odseky) + '</div>')

    def o_firme_udaje(self, pre):
        """Údaje firmy v hrdinovi úvodu ako rad s ikonami zo vzorov (ZMENY-KOLO-1.md 3A.2) v poradí Sídlo, Na trhu,
        Konateľ, Oprávnenia (s odkazom na stránku Oprávnenia); na mobile Sídlo a Oprávnenia cez celý riadok (udaj-siroky),
        Na trhu a Konateľ vedľa seba, na počítači 2 x 2 na mriežke (Sídlo a Konateľ 2 stĺpce, Na trhu a Oprávnenia 3 stĺpce,
        krasa-8). Ikona je v dt a skrytá pred čítačkou. Pod údajmi malým písmom zdroj (výpis z Obchodného
        registra SR, len adresa orsr.sk z obsah/firma.json zapis_or_zdroj) a tlačidlá Zavolať a Napísať e-mail. Odkaz
        Vybrané realizácie je preč aj s tmavým pásom (ZMENY-KOLO-1.md 3A.6)."""
        t = self.texty['uvod']
        fi = self.firma
        na_trhu = f'od roku {fi["den_zapisu"][:4]}' if platny_datum(fi['den_zapisu']) else ''
        udaje = [('Sídlo', self.adresa_dvojriadok(), True), ('Na trhu', T(na_trhu), False), ('Konateľ', T(fi['konatel']), False),
                 ('Oprávnenia', f'<a href="{esc(self.rel(pre, "opravnenia/"))}">{T(t["opravnenia"])}</a>', True)]
        def riadky(zoznam):
            return ''.join(f'<div class="udaj{" udaj-siroky" if siroky else ""}{" udaj-media" if siroky == "media" else ""}">'
                           f'<dt>{IKONY_UDAJOV.get(k, "")}<span>{T(k)}</span></dt>'
                           f'<dd>{v}</dd></div>' for k, v, siroky in zoznam if v)
        dl = riadky(udaje)
        zdroj = ''
        if fi['zapis_zdroj']:
            zdroj = (f'<p class="firma-zdroj">Zdroj: <a href="{esc(fi["zapis_zdroj"])}">{T(t["zdroj"])}</a></p>')
        # V médiách (ZMENY-KOLO-1.md 5.7): nenápadný údaj cez celý riadok s odkazom na blok na stránke realizácie
        # (časopis ASB č. 244 pri Lumière, kotva #casopis-asb); text z nastavení v_mediach uvod. Vlastný rad až za
        # zdrojom z ORSR, aby zdroj patril len k údajom z registra (oprava-2, nález drobné).
        media = ''
        med = next((m for m in self.media.values() if m['uvod']), None)
        if med:
            ciel = self.rel(pre, f'realizacie/{med["realizacia"]}/#{med["kotva"]}')
            # meno firmy sa nezalomí (na mobile „BM“ na konci riadka a „Building“ na ďalšom)
            text = T(med['uvod']).replace('BM Building', 'BM' + chr(160) + 'Building')
            odkaz = f'<a href="{esc(ciel)}">{text}</a>'
            media = f'<dl class="firma-fakty firma-media">{riadky([("V médiách", odkaz, "media")])}</dl>'
        return (f'<div class="firma-udaje"><dl class="firma-fakty">{dl}</dl>{zdroj}{media}'
                + self.akcie('uvod-akcie', 'data-vyzva-hore') + '</div>')

    def nadtitulok_uvodu(self, slova):
        """Riadok kľúčových slov nad H1 (texty.json uvod.nadtitulok, spojené bodkou v strede). Celý text je v jednom span
        (jedna položka flexu vedľa čiarky), posledné slovo (miesto, „Bratislava a okolie“) aj s bodkou pred ním je
        v span.uvod-nadtitulok-miesto: styl.css ho v užšom stĺpci skryje, aby sa riadok nezlomil (krasa-8, brána 3).
        Bez slov nič."""
        casti = [T(jeden_riadok(x)) for x in self.zoznam_textov(slova, 'uvod.nadtitulok') if jeden_riadok(x)]
        if not casti:
            return ''
        text = BODKA.join(casti[:-1])
        text = (text + f'<span class="uvod-nadtitulok-miesto">{BODKA}{casti[-1]}</span>') if text else casti[-1]
        return f'<p class="uvod-nadtitulok"><span>{text}</span></p>'

    def adresa_dvojriadok(self):
        """Adresa sídla v údajoch hrdinu: ulica s číslom a PSČ s mestom sa vnútri nezalomia (span.nz), v úzkom stĺpci
        idú pod seba ako na obálke (krasa-8: stĺpec údajov má 2 z 12 stĺpcov mriežky)."""
        f = self.firma
        mesto = ' '.join(y for y in (f['psc'], f['mesto']) if y)
        casti = [T(x) for x in (f['ulica'], mesto) if x]
        if len(casti) < 2:
            return T(self.adresa())
        return f'<span class="nz">{casti[0]},</span> <span class="nz">{casti[1]}</span>'

    @staticmethod
    def h1_uvodu(text):
        """H1 úvodu so sadzbou: slovo so spojovníkom (stavebno-manažérska) sa nezalomí (span.nz, Geist nemá U+2011);
        veľkosť písma v styl.css je podiel šírky stĺpca, takže slovo sa do riadka vždy zmestí."""
        return re.sub(r'(?<![\w>-])(\w+-\w+)', r'<span class="nz">\1</span>', T(text))

    def uvod(self):
        """Úvod (1. kolo pripomienok 24. 11. 2026, ZMENY-KOLO-1.md 3A má prednosť pred 3): hrdina na prvú obrazovku
        so sliderom stavieb vľavo (7 z 12 stĺpcov, presah k ľavému okraju okna, výška okna pod hlavičkou) a vpravo
        nadtitulok s kľúčovými slovami, H1 doslovne podľa Andreja, údaje s ikonami, zdroj a tlačidlá (kotva #firma).
        Na mobile a tablete je fotka na celú šírku a nadtitulok s H1 bielym písmom priamo na nej (tmavý prechod v spodnej
        časti), pod ňou údaje ako kartičky a tlačidlá; jeden DOM, oblasti mriežky v styl.css. Hneď pod hrdinom Činnosť
        doslovne zo starého webu a Kontakt (na úvode modrý blok). Tmavý pás Vybrané realizácie je preč (Andrej 24. 11.
        2026 večer, ZMENY-KOLO-1.md 3A.6: „nechať len slideshow“); zoznam je na /realizacie/ a v menu."""
        t = self.texty['uvod']
        pre = ''
        fotky = self.slider_fotky()
        _, f = self.hrdina_foto()
        self.registruj_og('og/bmbuilding.jpg', self.og_foto(f), 'Stavebný dozor',
                          'Stavbyvedúci a stavebný manažment, Bratislava a okolie', 'bmbuilding.sk')
        self.preload_hrdina, self.css_slider = [], ''
        slider = self.slider(pre, fotky) if fotky else ''
        hlava = (f'<div class="firma-hlava" id="firma" data-vyzva-hore>'
                 + self.nadtitulok_uvodu(t.get('nadtitulok'))
                 + f'<h1 class="uvod-h1" id="uvod-nadpis">{self.h1_uvodu(t["h1"])}</h1></div>')
        casti = [f'<section class="uvod{"" if slider else " bez-slidera"}" aria-labelledby="uvod-nadpis">'
                 f'<div class="obsah mriezka uvod-mriezka">{hlava}{slider}{self.o_firme_text()}'
                 f'{self.o_firme_udaje(pre)}</div></section>']
        casti.append(self.cinnost(pre))
        casti.append(self.kontakt_sekcia(pre))
        uzly = [self.ld_firma(), {'@type': 'WebSite', '@id': self.abs_url('#web'), 'url': self.abs_url(''),
                                  'name': 'BM Building', 'inLanguage': 'sk', 'publisher': {'@id': self.abs_url('#firma')}},
                {'@type': 'WebPage', '@id': self.abs_url(''), 'url': self.abs_url(''), 'name': t['title'],
                 'isPartOf': {'@id': self.abs_url('#web')}, 'about': {'@id': self.abs_url('#firma')}, 'inLanguage': 'sk'}]
        self.stranka('', t['title'], t['description'], '\n'.join(c for c in casti if c), '', uzly, trieda='stranka-uvod',
                     preload_obr=self.preload_hrdina or None, css_navyse=self.css_slider)

    # --- podstránky
    def podstranka(self, cesta, kroky, title, description, telo, aktivne, uzly_navyse, og='og/bmbuilding.jpg',
                   og_typ='website', lastmod=None, zaver=True, zaver_hlavny=None, zaver_dalej=None, pata_firma=True,
                   typ_stranky='WebPage', stranka_navyse=None):
        uzly = [self.ld_firma(), self.ld_breadcrumb(kroky),
                {'@type': typ_stranky, '@id': self.abs_url(cesta), 'url': self.abs_url(cesta), 'name': title,
                 'inLanguage': 'sk', 'isPartOf': {'@id': self.abs_url('#web')}}]
        # jeden WebSite v grafe (uzol s @id #web je na úvode; brána pokus 2, správnosť 7)
        if stranka_navyse:
            uzly[-1].update(stranka_navyse)
        uzly += uzly_navyse
        if zaver:
            telo += '\n' + self.zaver(self.pre(cesta), zaver_hlavny, zaver_dalej)
        self.stranka(cesta, title, description, telo, aktivne, uzly, og_obr=og, og_typ=og_typ, lastmod=lastmod,
                     pata_firma=pata_firma)

    def zoznam_dalsich(self, pre, zoznam, nadpis='h3'):
        polozky = []
        for r in zoznam:
            url = esc(self.rel(pre, f'realizacie/{r["slug"]}/'))
            udaje = BODKA.join([T(x) for x in (r['miesto'], r['rola']) if x] + ([self.kedy(r)] if self.kedy(r) else []))
            polozky.append(f'<li><a href="{url}">{T(r["nazov"])}</a>'
                           + (f'<span>{udaje}</span>' if udaje else '') + '</li>')
        return (f'<div class="dalsie"><{nadpis} class="dalsie-nadpis">Ďalšie realizácie</{nadpis}>'
                f'<ul class="zoznam-dalsie">{"".join(polozky)}</ul></div>')

    def cast_doslova(self, slug):
        """(kľúč, časť) doslovného textu cinnost_doslova pre stránku služby so slug, inak (None, None)."""
        return next(((k, c) for k, c in self.cinnost_casti() if jeden_riadok(c.get('slug')) == slug), (None, None))

    def stranky_sluzieb(self):
        """Stránky služieb (1. kolo pripomienok, ZMENY-KOLO-1.md časť 4): hlavný text je presne text starého webu,
        slovo od slova, z tej istej časti texty.json cinnost_doslova ako Činnosť na úvode (doslovny_text). Naše
        prepísané odseky, vety, príklady a očíslovaný zoznam činností sú preč; H1, title a description ostávajú.
        Vpravo doklad SKSI a Zavolať, pod textom realizácie s rolou a otázky zo zákona (bez „Kto je pri kolaudácii?“)."""
        for k, t in self.texty['sluzby'].items():
            cesta = f'{k}/'
            pre = self.pre(cesta)
            kluc_d, cast = self.cast_doslova(k)
            if cast is None:
                varuj(f'texty.json cinnost_doslova: chýba časť so slug {k}, stránka služby bez hlavného textu')
                text = ''
            else:
                ikona = IKONY_CINNOSTI.get(k, '')
                text = ((f'<span class="cinnost-ikona sluzba-ikona" aria-hidden="true">{ikona}</span>' if ikona else '')
                        + self.doslovny_text(kluc_d, cast, stlpce=False, uvodna=True, riadky=True))
            # doklady v boku sú h2: bok je v DOM pred prvým h2 stránky, h3 by bol skok úrovne po h1; viac osvedčení
            # (Stavebný manažment) skrátene v jednej tabuľke (doklady_kratko, audit naplno 25. 11. 2026, položka 25)
            kratko = self.doklady_kratko(t['opravnenie'], nadpis='h2')
            doklady = [kratko] if kratko else self.doklady(pre, t['opravnenie'], nadpis='h2')
            bok = ''
            if doklady or self.firma['tel_href']:
                bok = '<aside class="kalkulacka sluzba-bok" aria-label="Oprávnenie a kontakt">' + ''.join(doklady)
                if self.firma['tel_href']:
                    bok += (f'<p class="bok-tlacidlo"><a class="tlacidlo" href="tel:{esc(self.firma["tel_href"])}">'
                            f'Zavolať <span class="cisla">{T(self.firma["tel"])}</span></a></p>')
                bok += (f'<p class="bok-odkaz"><a class="odkaz-sipka" href="{esc(self.rel(pre, "opravnenia/"))}">'
                        f'Oprávnenia a osvedčenia {SVG_SIPKA}</a></p></aside>')
            suvisiace = self.realizacie_sluzby(k)
            real = ''
            if suvisiace:
                naraz = int(self.nastavenia.get('sluzba_pocet_kariet', 4))
                # stránka služby odkazuje na každú realizáciu s danou rolou (brána pokus 2, správnosť 2): nad počet
                # kariet ostatné ako kompaktný zoznam; karta vždy s fotkou (r['karta'])
                # karty s fotkou dosť veľkou na celý rám 4 : 5 majú prednosť (brána bmb-presnost pokus 1, nález 1:
                # Vila X 640 x 480 px na karte Stavebného manažmentu); malé ostávajú v kompaktnom zozname nižšie
                s_obalom = sorted((r for r in suvisiace if self.foto_karty(r)),
                                  key=lambda r: self.mala_karta(self.foto_karty(r)))[:naraz]
                zvysok = [r for r in suvisiace if r not in s_obalom]
                real = (f'<section class="sekcia sekcia-karty" aria-labelledby="real-nadpis"><div class="obsah">'
                        f'<div class="sekcia-hlava"><h2 class="nadpis" id="real-nadpis">{T(t["realizacie_nadpis"])}'
                        f' <span class="nadpis-pocet cisla">{len(suvisiace)}</span></h2></div>'
                        f'<ul class="karty">{"".join(self.dlazdica(pre, r) for r in s_obalom)}</ul>'
                        + (self.zoznam_dalsich(pre, zvysok) if zvysok else '')
                        + f'<p class="viac"><a class="odkaz-sipka" href="../realizacie/">Všetky realizácie {SVG_SIPKA}</a></p></div></section>')
            elif text:
                # služba bez realizácie s jej rolou (Stavbyvedúci: žiadna rola v obsahu ho nemá) má pod textom ten istý
                # odkaz Všetky realizácie ako ostatné služby (audit naplno 25. 11. 2026, položka 29)
                text += (f'<p class="viac"><a class="odkaz-sipka" href="{esc(self.rel(pre, "realizacie/"))}">'
                         f'Všetky realizácie {SVG_SIPKA}</a></p>')
            kroky = [('', 'Úvod'), (cesta, t['menu'])]
            # hlava-sluzba: menší H1 na počítači, aby doslovný text Činnosti začínal vyššie (overenie-1, nález drobné)
            hlava = self.hlava_stranky(pre, kroky, t['h1'], trieda='hlava-sluzba')
            telo = (hlava + f'\n<div class="obsah mriezka sluzba-obal"><div class="sluzba-hlavne atelier-text cinnost-text sluzba-doslova">'
                    f'{text}</div>{bok}</div>\n{real}\n{self.otazky_sekcia(t["otazky"], "zakon-nadpis")}')
            sluzba = {'@type': 'Service', '@id': self.abs_url(cesta + '#sluzba'), 'name': t['h1'], 'serviceType': t['typ_sluzby'],
                      'description': t['description'], 'provider': {'@id': self.abs_url('#firma')},
                      'areaServed': [{'@type': 'City', 'name': 'Bratislava'}, {'@type': 'City', 'name': 'Pezinok'}],
                      'url': self.abs_url(cesta)}
            self.podstranka(cesta, kroky, t['title'], t['description'], telo, k, [sluzba], zaver_dalej=t.get('zaver'))

    def filtre(self, zoznam, roky_vyber=True):
        """Ovládanie filtra a zoradenia pre app.js: bez JS skryté (styl.css html:not(.js), triedu js dá vložený skript
        v hlave pred prvým vykreslením), zoznam ukazuje všetko v poradí od najnovších. Miesto filtra je v rozložení od
        prvého vykreslenia (doladenie-1, nález vážne: s atribútom hidden ho odkryl až app.js a pri pomalej sieti
        skočila časová os aj celý zoznam o 236 px nadol, CLS 0,155 pri 390 px). Druh z kategórií build.py, roky po
        desaťročiach podľa najvyššieho roku v roky, mesto z poľa miesto."""
        pocty = self.pocty_druhov(zoznam)
        druhy = [f'<button class="filter" type="button" data-druh="" aria-pressed="true">Všetky<span>{len(zoznam)}</span></button>']
        for k, nazov in KATEGORIE:
            if pocty[k]:
                druhy.append(f'<button class="filter" type="button" data-druh="{k}" aria-pressed="false">{T(nazov)}'
                             f'<span>{pocty[k]}</span></button>')
        vyber = ''
        if roky_vyber:
            # len skutočné roky realizácie; rok príspevku a chýbajúci rok spolu ako „rok neuvedený“ (nález 1 brány)
            dekady = {}
            for r in zoznam:
                rok = self.rok_filtra(r)
                dekady.setdefault((rok // 10) * 10 if rok else 0, []).append(rok)
            moznosti = ['<option value="">všetky</option>']
            for d in sorted(dekady, reverse=True):
                if d:
                    od, do = min(dekady[d]), max(dekady[d])
                    moznosti.append(f'<option value="{d}">{od if od == do else f"{od} až {do}"}</option>')
                else:
                    moznosti.append('<option value="0">rok neuvedený</option>')
            vyber = f'<label class="vyber">Roky <select data-roky>{"".join(moznosti)}</select></label>'
        mesta = {}
        for r in zoznam:
            m = self.mesto(r)
            if m:
                mesta[kluc_mesta(m)] = (mesta.get(kluc_mesta(m), (m, 0))[0], mesta.get(kluc_mesta(m), (m, 0))[1] + 1)
        if len(mesta) > 1:
            moznosti = ['<option value="">všetky</option>'] + [
                f'<option value="{esc(k)}">{T(m)} ({n})</option>' for k, (m, n) in sorted(mesta.items(), key=lambda x: (-x[1][1], x[1][0]))]
            # Miesto prvé: na mobile cez celý riadok nad Roky a Zoradiť (najdlhšie hodnoty), poradie v DOM je poradie na
            # obrazovke aj poradie fokusu; zoradenie ostáva posledné aj na počítači (audit naplno 25. 11. 2026, položka 4)
            vyber = f'<label class="vyber vyber-miesto">Miesto <select data-mesto>{"".join(moznosti)}</select></label>' + vyber
        # podľa názvu: app.js triedi text nadpisu karty slovenským poradím (Intl.Collator sk)
        # krátke popisky (oprava 1 linky bmb-fb-uplnost): pri 390 px má select v trojstĺpcovom riadku asi 110 px,
        # „od najnovších“ sa orezalo na „od najnovšíc“
        vyber += ('<label class="vyber">Zoradiť <select data-zoradit><option value="najnovsie">najnovšie</option>'
                  '<option value="najstarsie">najstaršie</option><option value="nazov">abecedne</option></select></label>')
        return (f'<div class="filtre" data-filtre><div class="filtre-druh" role="group" aria-label="Druh stavby">'
                f'{"".join(druhy)}</div><div class="filtre-vyber">{vyber}</div></div>')

    def naraz(self):
        """Koľko kariet zoznamu ukáže app.js naraz (nastavenia realizacie_naraz, predvolene REALIZACIE_NARAZ);
        0 = všetky naraz bez tlačidla Zobraziť ďalšie."""
        try:
            n = int(self.nastavenia.get('realizacie_naraz', REALIZACIE_NARAZ))
        except (TypeError, ValueError):
            varuj('nastavenia realizacie_naraz nie je číslo, použitých ' + str(REALIZACIE_NARAZ))
            n = REALIZACIE_NARAZ
        return max(0, n)

    def prvych_zoznamu(self, pocet):
        """Koľko kariet zoznamu realizácií ukáže app.js po spustení bez filtra v adrese (hranica v časti 5): prvých
        naraz(), no zvyšok najviac polovica dávky sa ukáže naraz s ňou (audit naplno 25. 11. 2026, položka 17:
        „Zobraziť ďalšie (14)“ ukázalo 12 a nechalo „(2)“), takže pri 13 až 18 kartách a dávke 12 sú hneď všetky;
        bez dávky (0) všetky."""
        naraz = self.naraz()
        return naraz if naraz and pocet - naraz > naraz // 2 else pocet

    def stav_zoznamu(self, pocet):
        """Text riadka stavu zoznamu realizácií pred prvým filtrom, rovnaký ako v app.js (obnov bez výberu):
        „Zobrazené: N z M“, keď app.js ukáže len prvých naraz() kariet (prvych_zoznamu), inak prázdny."""
        m = self.prvych_zoznamu(pocet)
        return f'Zobrazené: {m} z {pocet}' if m < pocet else ''

    def stranka_realizacii(self):
        t = self.texty['realizacie']
        cesta = 'realizacie/'
        kroky = [('', 'Úvod'), (cesta, 'Realizácie')]
        limit = int(self.nastavenia.get('realizacie_na_stranu', 300))
        if len(self.realizacie) <= limit:
            stranky = [(cesta, self.realizacie, '')]
            nav = []
        else:
            # nad limit: stránky po desaťročiach, úvodná stránka zoznamu má najnovšie desaťročie
            dekady = {}
            for r in self.realizacie:
                rok = self.rok_filtra(r)
                dekady.setdefault((rok // 10) * 10 if rok else 0, []).append(r)
            poradie = sorted((d for d in dekady if d), reverse=True) + ([0] if 0 in dekady else [])
            stranky = []
            for i, d in enumerate(poradie):
                c = cesta if i == 0 else f'{cesta}roky-{d if d else "bez"}/'
                stranky.append((c, dekady[d], d))
            nav = []
            for c, zoz, d in stranky:
                text = (f'{d} až {d + 9}' if d else 'Rok neuvedený')
                nav.append((c, text, len(zoz)))
        for c, zoznam, dekada in stranky:
            pre_c = self.pre(c)
            kroky_c = kroky if c == cesta else kroky + [(c, nav[[x[0] for x in nav].index(c)][1])]
            navigacia = ''
            if len(stranky) > 1:
                polozky = []
                for c2, text, pocet in nav:
                    cur = ' aria-current="page"' if c2 == c else ''
                    polozky.append(f'<li><a href="{esc(self.rel(pre_c, c2))}"{cur}>{T(text)} <span>{pocet}</span></a></li>')
                navigacia = f'<nav class="dekady" aria-label="Realizácie podľa rokov"><ul>{"".join(polozky)}</ul></nav>'
            hlava = self.hlava_stranky(pre_c, kroky_c, 'Realizácie', bok=f'<p class="uvodny">{T(t["veta"])}</p>',
                                       navyse=navigacia + (self.filtre(zoznam, roky_vyber=len(stranky) == 1) if zoznam else '')
                                       + (self.mierka(pre_c) if zoznam and len(stranky) == 1 else ''))
            if zoznam:
                # karty za prvou dávkou majú data-neskor: do spustenia app.js sú mimo rozloženia a ich lenivé fotky sa
                # nesťahujú (audit naplno 25. 11. 2026, položka 46, pri 1440 px a Slow 4G 8 fotiek, 357 kB, ktoré app.js
                # hneď skryl); bez JS ich nič neskryje (styl.css .js [data-neskor])
                prvych = self.prvych_zoznamu(len(zoznam))
                dlazdice = ''.join(self.dlazdica(pre_c, r, 'h2', data=True, poradie=i, lcp=(i == 1), neskor=i > prvych)
                                    for i, r in enumerate(zoznam, 1))
                # postupné načítanie (zadanie bmb-fb-uplnost bod 4): všetky karty sú v HTML (bez JS viditeľné, odkaz
                # na každú realizáciu pre vyhľadávače), app.js ukáže prvých data-naraz z výberu a tlačidlo Zobraziť
                # ďalšie s počtom zvyšných; filter a zoradenie pracujú nad všetkými kartami, nielen nad zobrazenými.
                # Riadok stavu má text už v HTML (ten istý, aký napíše app.js bez filtra v adrese, doladenie-1): pri
                # pomalej sieti sa neobjaví dodatočne; bez JS je skrytý (styl.css html:not(.js)), lebo vidno všetky karty
                telo = (hlava + f'\n<div class="obsah zoznam"><p class="filtre-stav" data-stav aria-live="polite">'
                        f'{esc(self.stav_zoznamu(len(zoznam)))}</p>'
                        f'<ul class="karty karty-zoznam" id="zoznam-realizacii" data-zoznam data-naraz="{self.naraz()}">{dlazdice}</ul>'
                        '<p class="viac-obal" data-viac-obal hidden><button class="tlacidlo tlacidlo-obrys" type="button" '
                        'data-viac aria-controls="zoznam-realizacii">Zobraziť ďalšie <span class="viac-pocet" data-viac-pocet></span></button></p>'
                        '<div class="prazdne" data-prazdne hidden><p>Pre tento výber nie je žiadna realizácia.</p>'
                        '<button class="tlacidlo tlacidlo-obrys" type="button" data-zrusit>Zrušiť filter</button></div></div>')
            else:
                telo = hlava + '<div class="obsah text"><p>Realizácie pripravujeme.</p></div>'
            zoznam_ld = {'@type': 'ItemList', 'itemListElement': [
                {'@type': 'ListItem', 'position': i, 'url': self.abs_url(f'realizacie/{r["slug"]}/'), 'name': r['nazov']}
                for i, r in enumerate(zoznam, 1)]}
            title = t['title'] if c == cesta else f'Realizácie {kroky_c[-1][1]} | BM Building'
            self.podstranka(c, kroky_c, title, t['description'], telo, 'realizacie', [zoznam_ld])

    def titulok_realizacie(self, r):
        """Title stránky realizácie do 60 znakov bez useknutia na nezmysel (brána pokus 1, správnosť 3).

        Poradie: vlastné pole titulok; názov s mestom; názov; prvá a posledná časť názvu (ulica) s mestom alebo bez;
        názov s mestom alebo názov bez prípony značky; potom začiatok názvu po prvú čiarku s mestom alebo bez,
        s príponou alebo bez; nakoniec celý názov, hoci má viac ako 60 znakov (Google ho skráti sám). Názov sa
        nikdy neseká uprostred vety, lebo skrátenie po slovách dávalo „Rekonštrukcia bytového, …“ a pri dlhom
        mieste title bez názvu (brána pokus 2, správnosť 1). Prípona „ | BM Building“ ide všade, kde sa zmestí."""
        pripona, limit = ' | BM Building', 60
        if r['titulok']:
            if len(r['titulok']) + len(pripona) <= limit:
                return r['titulok'] + pripona
            if len(r['titulok']) <= limit:
                return r['titulok']
        nm, m = self.nazov_s_miestom(r), self.mesto(r)
        casti = [c.strip() for c in r['nazov'].split(',') if c.strip()]

        def s_mestom(t):
            return f'{t}, {m}' if m and m.lower() not in t.lower() else t
        kandidati = [nm, r['nazov']]
        if len(casti) > 2:
            kratsi = f'{casti[0]}, {casti[-1]}'
            kandidati += [s_mestom(kratsi), kratsi]
        for k in kandidati:
            if len(k) + len(pripona) <= limit:
                return k + pripona
        for k in (nm, r['nazov']):
            if len(k) <= limit:
                return k
        zaciatok = [s_mestom(casti[0]), casti[0]] if len(casti) > 1 else []
        for k in zaciatok:
            if len(k) + len(pripona) <= limit:
                return k + pripona
        for k in kandidati[2:] + zaciatok:
            if len(k) <= limit:
                return k
        return r['nazov']

    def popis_fotky(self, r, f):
        """Popis fotky z alt bez „fotka N z M“; prázdny, ak nehovorí nič nad názov a miesto stavby."""
        s = re.sub(r',?\s*fotka \d+ z \d+$', '', f['alt']).strip(' ,')
        zname = {w[:4] for w in re.findall(r'\w+', f'{r.get("nazov", r.get("nadpis", ""))} {r.get("miesto", "")}'.lower()) if len(w) >= 3}
        if not s or not any(len(w) >= 3 and w[:4] not in zname for w in re.findall(r'\w+', s.lower())):
            return ''
        return s

    @staticmethod
    def nezvacsena(f, bunka):
        """True, keď fotka vyplní bunku (object-fit:cover) najviac s 1,25 násobným zväčšením súboru."""
        return f['plna_w'] * ZVACSENIE_MAX >= bunka[0] and f['plna_h'] * ZVACSENIE_MAX >= bunka[1]

    def galeria(self, pre, r, nazov, popis_udaje):
        """Galéria: prvá fotka veľká, ostatné v mriežke 3 : 2; každá fotka je odkaz na plnú verziu (funguje bez JS)
        a nesie dáta pre lightbox v app.js (zmluva v dizajn-olovnica/PRIPRAVA-BUILD.md bod 3). Rozmazané fotky
        v nej nie sú (priprav_fotky)."""
        fotky = r['galeria']
        if not fotky:
            return ''
        polozky = []
        malo = len(fotky) <= 3
        for i, f in enumerate(fotky, 1):
            # vizualizácia: pod náhľadom meno autora (projektant alebo autor z príspevku, obsah-zmeny.md), štítok na fotke
            popis = (('Vizualizácia, autor: ' + f['autor']) if f.get('autor') else '') if f.get('viz') else self.popis_fotky(r, f)
            # prvá veľká len keď nie je tou istou fotkou ako pohľad A nad galériou (detail_dvojstrana), inak by
            # rovnaká fotka bola dvakrát veľká pod sebou; ani fotka karty nahradená nastavením karta_foto (malý zdroj),
            # galéria má potom tú istú stavbu ako pred zmenou karty (overenie zostavy bmb-presnost)
            prva = (i == 1 and len(fotky) >= 4 and f['w'] >= 960 and f['w'] >= f['h']
                    and f is not r.get('karta') and f is not r.get('karta_nahradena')
                    and self.nezvacsena(f, GALERIA_BUNKY['prva']))
            # dáta lightboxu (lb_data, Stavba 1): alt nesie číslo podľa poradia v galérii (to isté ukáže počítadlo),
            # data-galeria = slug, vizualizácia so štítkom na náhľade, v lightboxe aj v alt texte; pri náhľade so
            # srcset (nie malá fotka) berie app.js srcset a zdroj AVIF lightboxu z náhľadu (lb_data nahlad)
            sizes = SIZES_GALERIA_PRVA if prva else SIZES_GALERIA_MALO if malo else SIZES_GALERIA
            mala = not prva and not self.nezvacsena(f, GALERIA_BUNKY['malo' if malo else 'mriezka'])
            plna, alt, atributy = self.lb_data(pre, r, f, i, len(fotky), nazov, popis_udaje, r.get('slug', ''),
                                               nahlad=not mala)
            if mala:
                # malá fotka (GALERIA_BUNKY): len plný súbor bez srcset (prirodzená veľkosť = súbor v CSS px), CSS
                # .galeria-odkaz.mala img object-fit:scale-down ju nikdy nezväčší. Len WebP: jeden malý súbor
                # (užší než 460 px) v prirodzenej veľkosti, AVIF by ušetril pár kB (fotky-kvalita/KODOVANIE.md)
                self.registruj_fotku(r, f)
                obr = (f'<img src="{plna}" width="{f["plna_w"]}" height="{f["plna_h"]}" '
                       f'alt="{esc(alt or f["alt"])}" loading="lazy" decoding="async">')
            else:
                # bunka 3 : 2 s object-fit: cover: širšia fotka potrebuje širší súbor (sizes_pre_cover)
                obr = self.img(pre, r, f, sizes_pre_cover(sizes, f['w'], f['h']), alt=alt or None)
            stitok = '<span class="stitok-viz">Vizualizácia</span>' if f.get('viz') else ''
            polozky.append(f'<li class="galeria-polozka{" prva" if prva else ""}"><a class="galeria-odkaz{" mala" if mala else ""}" href="{plna}" '
                           f'data-lightbox {atributy}>{obr}{stitok}<span class="galeria-c cisla" aria-hidden="true">{i:02d}</span></a>'
                           + (f'<span class="galeria-popis">{T(popis)}</span>' if popis else '') + '</li>')
        pocet = tvar(len(fotky), 'fotka', 'fotky', 'fotiek')
        return (f'<section class="sekcia galeria-sekcia" id="galeria" aria-labelledby="galeria-nadpis"><div class="obsah">'
                f'<div class="sekcia-hlava galeria-hlava"><h2 class="nadpis" id="galeria-nadpis">Galéria <span class="nadpis-pocet cisla">{T(pocet)}</span></h2>'
                f'<p class="navod">{T("Fotka sa otvorí cez celú obrazovku. Ďalšia: šípka alebo potiahnutie prstom.")}</p></div>'
                f'<ul class="galeria{" galeria-malo" if malo else ""}" data-galeria>{"".join(polozky)}</ul></div></section>'
                + self.lightbox_dialog(pre, nazov, r, fotky[0]))

    def pred_po(self, pre, r):
        """Pred a po len pre dvojicu zo sablona/fotky-pravidla.json (istota vysoká, oba súbory v tejto realizácii, nie
        rozmazané, rovnaký pomer strán do 2 %); inak nič (FOTKY.md bod 5). Stredná istota nestačí: zadanie
        bmb-olovnica 21. 11. 2026 „ak istota nie je vysoká, pred a po nedávaj“ (pár Rosnička je len v navrhy_pred_po)."""
        for par in self.pravidla['pary']:
            if par.get('slug') != r['slug']:
                continue
            if str(par.get('istota', '')).strip().lower() != 'vysoká':
                varuj(f'pred a po {r["slug"]}: istota {par.get("istota")!r} nestačí, nezobrazené')
                continue
            najdi = {('obrazky/' + f['rel']): f for f in r['galeria']}
            fp, fo = najdi.get(str(par.get('pred'))), najdi.get(str(par.get('po')))
            if not fp or not fo:
                varuj(f'pred a po {r["slug"]}: fotka chýba alebo je rozmazaná, nezobrazené')
                continue
            if abs(fp['w'] / fp['h'] - fo['w'] / fo['h']) > 0.02 * (fo['w'] / fo['h']):
                varuj(f'pred a po {r["slug"]}: fotky majú iný pomer strán, nezobrazené')
                continue
            popis_pred = jeden_riadok(par.get('popis_pred')) or 'Pred'
            popis_po = jeden_riadok(par.get('popis_po')) or 'Po'
            nm = self.nazov_s_miestom(r)
            return (f'<section class="sekcia pred-po-sekcia" aria-labelledby="pred-po-nadpis"><div class="obsah">'
                    f'<h2 class="nadpis" id="pred-po-nadpis">{T(popis_pred)} a {T(popis_po.lower())}</h2>'
                    f'<div class="pred-po" data-pred-po>'
                    f'<figure class="pred-po-pred">{self.img(pre, r, fp, SIZES_PRED_PO, alt=f"{nm}, {popis_pred.lower()}")}'
                    f'<figcaption>{T(popis_pred)}</figcaption></figure>'
                    f'<figure class="pred-po-po">{self.img(pre, r, fo, SIZES_PRED_PO, alt=f"{nm}, {popis_po.lower()}")}'
                    f'<figcaption>{T(popis_po)}</figcaption></figure></div>'
                    '<p class="poznamka">Obe fotky sú z toho istého miesta.</p></div></section>')
        return ''

    def susedia(self, zoznam, i, nazov_kluc, slovo):
        """Navigácia Novšia a Staršia pod detailom (realizácia, novinka)."""
        out = []
        if i > 0:
            p = zoznam[i - 1]
            out.append(f'<a class="predch" href="../{esc(p["slug"])}/"><span class="susedia-smer">{SVG_SIPKA_SPAT}Novšia'
                       f'<span class="vh"> {slovo}:</span></span><b>{T(p[nazov_kluc])}</b></a>')
        if i + 1 < len(zoznam):
            p = zoznam[i + 1]
            out.append(f'<a class="dalsia" href="../{esc(p["slug"])}/"><span class="susedia-smer">Staršia'
                       f'<span class="vh"> {slovo}:</span>{SVG_SIPKA}</span><b>{T(p[nazov_kluc])}</b></a>')
        return f'<nav class="susedia" aria-label="Ďalšie: {slovo}">{"".join(out)}</nav>' if out else ''

    @staticmethod
    def popisok_rokov(roky):
        """Popisok rokov stavby: Rok pri jednom roku (UFO 2025, RD Kalvária 2009), inak Roky („2021 až 2022“). Audit
        naplno 25. 11. 2026, položka 2: detail aj description písali „Roky 2025“ pri jednom roku."""
        return 'Rok' if re.fullmatch(r'(19|20)\d{2}', (roky or '').strip()) else 'Roky'

    def udaje_realizacie(self, r):
        """Údaje realizácie v hlave stránky (dl.fakty ukážky): miesto, druh stavby, rola BM Building s osobou zo starého
        webu, projektant, investor a zhotoviteľ s adresou a webom pod menom (sablona/stary-web.json), roky stavby pod
        popiskom Rok alebo Roky (popisok_rokov) a etapy. Bez rokov stavby riadok rokov nie je, rok príspevku na web
        nejde (roky_karty, overenie-1). Termín výstavby s mesiacmi nie je (ZMENY-KOLO-1.md 5.2, len roky). Weby strán
        ako text, nie odkaz (cudzie domény sa mohli od starého webu zmeniť).
        Druh stavby je ten istý ako na karte, v jednotnom čísle (druh_karty, audit naplno 25. 11. 2026, položka 2:
        detail písal názov filtra „Rodinné domy“ pri jednom dome). Keď ho názov ani text neurčí (Okánikova, Dom pod
        lesom), detail ukáže kategóriu filtra pod popiskom Kategória; Iné sa neukazuje."""
        druh = self.druh_karty(r)
        kategoria = '' if druh else ', '.join(self.kat_nazov(k) for k in r['kategorie'] if k != 'ine')
        strany = r.get('strany') or {}

        def strana(kluc, meno):
            zoz = strany.get(kluc) or []
            if not meno and zoz:
                meno = ', '.join(x['meno'] for x in zoz if x.get('meno'))
            if not meno:
                return ''
            riadky = []
            for x in zoz:
                kusy = [T(v) for v in (x.get('adresa'), x.get('web')) if v]
                if not kusy:
                    continue
                predpona = f'{T(x["meno"])}: ' if len(zoz) > 1 and x.get('meno') else ''
                riadky.append(f'<span class="fakty-pod">{predpona}{BODKA.join(kusy)}</span>')
            return T(meno) + ''.join(riadky)
        rola = T(r['rola']) + (f'<span class="fakty-pod">{T(r["rola_osoba"])}</span>' if r['rola'] and r.get('rola_osoba') else '')
        pole = [('Miesto', T(r['miesto'])), ('Druh stavby', T(druh)), ('Kategória', T(kategoria)),
                ('Rola BM Building', rola),
                ('Projektant', strana('projektant', r['projektant'])), ('Autor', T(r.get('autor') or '')),
                ('Investor', strana('investor', r['investor'])),
                ('Zhotoviteľ', strana('zhotovitel', r['zhotovitel'])), (self.popisok_rokov(r['roky']), T(r['roky']))]
        if r.get('etapy'):
            pole.append(('Etapy', ''.join(f'<span class="fakty-riadok">{T(e)}</span>' for e in r['etapy'])))
        # bez rokov stavby nie je ani riadok Rok: rok príspevku na web nejde (roky_karty, overenie-1)
        html_ =''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in pole if v)
        return f'<dl class="fakty udaje">{html_}</dl>' if html_ else ''

    def detail_dvojstrana(self, pre, r, nazov, text, roky='', udaje=''):
        """Dvojstrana detailu ako úvod ukážky: vľavo fotka (pohľad A, odkrytie sprava), vpravo kóta s rokmi a text
        (prvý odsek väčší, ďalšie tlmené). Fotka je fotka karty (nikdy vizualizácia, ak existuje skutočná).
        udaje (tabuľka údajov realizácie, len s fotkou): div.detail-udaje za fotkou, trieda detail-s-udajmi. Fotka je
        pri každej šírke hneď pod H1: do 1279 px fotka, údaje a text pod sebou, od 1280 px fotka vľavo a údaje s textom
        vpravo (audit naplno 25. 11. 2026, položka 13; oprava po overení, kontrola dizajnu dva nálezy vážne: s údajmi
        pred fotkou bola titulná fotka pri 360 a 390 px pri 11 až 16 z 26 detailov celá pod prvou obrazovkou a pri 1024
        až 1279 px mala hodnota vedľa fotky 147 až 223 px, pod fotkou ostávalo až 595 px prázdna)."""
        f = r.get('karta')
        foto = ''
        if f:
            n = len(r['galeria'])
            stitok = '<span class="stitok-viz">Vizualizácia</span>' if f.get('viz') else ''
            sizes = SIZES_DETAIL_UDAJE if udaje else SIZES_DETAIL
            obr = self.img(pre, r, f, sizes_pre_cover(sizes, f['w'], f['h']), lcp=True, alt=self.alt_bez_cisla(f) or nazov)
            popis = self.popis_fotky(r, f) if not f.get('viz') else (
                'Vizualizácia' + (f', autor: {f["autor"]}' if f.get('autor') else ''))
            casti = [T(popis or nazov)]
            if n > 1:
                # „Galéria, 8 fotiek“ sa nezalomí za čiarkou (kontrola zadania 26. 11. 2026: pri 360 až 412 px ostalo
                # „Galéria,“ na konci riadka a „8 fotiek“ samo na ďalšom)
                casti.append(f'<a href="#galeria">{T("Galéria,\u00a0" + tvar(n, "fotka", "fotky", "fotiek"))}</a>')
            foto = (f'<figure class="pohlad detail-foto"><div class="pohlad-ramec">{obr}{stitok}</div>'
                    f'<figcaption>{SVG_POHLAD_ZNAK}<span><strong>Pohľad A:</strong> {BODKA.join(casti)}</span></figcaption></figure>')
        if not text and not foto:
            return ''
        kota = self.kota(roky, 'kota-detail') if roky else ''
        pri = f'<div class="detail-udaje">{udaje}</div>' if udaje and foto else ''
        return (f'<div class="obsah mriezka detail{"" if foto else " detail-bez-fotky"}{" detail-s-udajmi" if pri else ""}">{foto}{pri}'
                f'<div class="detail-text atelier-text">{kota}{text}</div></div>')

    def vety_realizacie(self, r):
        """Vety údajov realizácie bez názvu: rola, projektant alebo autor a roky stavby (pri jednom roku popisok Rok,
        popisok_rokov, položka 2). Pre description stránky a popis odkazu v llms.txt."""
        vety = []
        if r['rola']:
            vety.append(s_bodkou(f'Rola BM Building: {male(r["rola"])}'))
        if r['projektant']:
            vety.append(s_bodkou(f'Projektant: {r["projektant"]}'))
        if r.get('autor'):
            vety.append(s_bodkou(f'Autor: {r["autor"]}'))
        if r['roky']:
            vety.append(s_bodkou(f'{self.popisok_rokov(r["roky"])}: {r["roky"]}'))
        return vety

    def popis_realizacie(self, r):
        """meta description stránky realizácie: názov s miestom a vety údajov (vety_realizacie), vety celé do 155
        znakov."""
        vety = [s_bodkou(self.nazov_s_miestom(r))] + self.vety_realizacie(r)
        desc = ''
        for v in vety:
            skus = (desc + ' ' + v).strip()
            if len(skus) <= 155:
                desc = skus
            elif not desc:
                desc = s_bodkou(skrat(v, 154))
        return desc

    def stranky_realizacie(self):
        poradie = self.realizacie
        for i, r in enumerate(poradie):
            cesta = f'realizacie/{r["slug"]}/'
            pre = self.pre(cesta)
            nm = self.nazov_s_miestom(r)
            title = self.titulok_realizacie(r)
            desc = self.popis_realizacie(r)
            pole_html = self.udaje_realizacie(r)
            odseky = [p for p in r['text'].split('\n') if p.strip()]
            text = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">{T(p)}</p>' for i, p in enumerate(odseky))
            # realizácia z Facebooku: odkaz na príspevok (inak stránka firmy) bez slova príspevok (ZMENY-KOLO-1.md 5.2)
            fb = r['fb_url'] or (self.firma['facebook'] if r['zdroj'] == 'fb' else '')
            if fb:
                text += f'<p class="tlmene"><a class="odkaz-von" href="{esc(fb)}">Pozrieť na Facebooku {SVG_VON}</a></p>'
            kroky = [('', 'Úvod'), ('realizacie/', 'Realizácie'), (cesta, r['nazov'])]
            m = self.mesto(r) if self.mesto(r).lower() not in r['nazov'].lower() else ''
            # s titulnou fotkou je tabuľka údajov v dvojstrane pri fotke, hlava má len H1 (audit naplno 25. 11. 2026,
            # položka 13: H1 bol dole vedľa vysokej tabuľky a fotka začínala pri 1366 x 768 až na 657 px)
            pri_fotke = bool(r.get('karta') and pole_html)
            hlava = self.hlava_stranky(pre, kroky, r['nazov'], m, bok='' if pri_fotke else pole_html, trieda='hlava-detail')
            udaje_lb = self.udaje_lightboxu(r)
            galeria = self.galeria(pre, r, nm, udaje_lb)
            obal = r['obal'] or (r['galeria'][0] if r['galeria'] else None)
            og = self.og_cesta_realizacie(r)
            self.registruj_og(og, self.og_foto(obal), r['nazov'],
                              ', '.join(x for x in (r['rola'], r['roky']) if x), 'BM Building, s.r.o., bmbuilding.sk')
            telo_detail = self.detail_dvojstrana(pre, r, nm, text, self.roky_stitok(r), udaje=pole_html if pri_fotke else '')
            # V médiách (ZMENY-KOLO-1.md 5.7) hneď pod textom, pred galériou; fotky bloku idú do toho istého dialógu
            # lightboxu ako galéria (iná skupina), bez galérie dostane stránka dialóg z prvého obrázka bloku
            media = self.v_mediach_blok(pre, r)
            m = self.media.get(r['slug'])
            if media and not galeria and m and self.media_obrazky(m):
                media += self.lightbox_dialog(pre, m['nazov_lightboxu'], m, self.media_obrazky(m)[0])
            telo = (hlava + '\n' + telo_detail + '\n' + media + self.pred_po(pre, r) + galeria + '\n'
                    + f'<div class="obsah susedia-obal">{self.susedia(poradie, i, "nazov", "realizácia")}'
                    f'<p class="viac"><a class="odkaz-sipka" href="../">Všetky realizácie {SVG_SIPKA}</a></p></div>')
            obrazky = [self.abs_url(self.obrazok_cesty(r, f)[f['plna_w']]) for f in r['galeria']]
            # CreativeWork bez creator a about: BM Building stavbu nevytvoril (dozor, manažment); contributor len pri
            # známej role (UFO rolu nemá, FAKTY-WEB: rolu nevymýšľať; brána pokus 1, správnosť 7)
            dielo = {'@type': 'CreativeWork', '@id': self.abs_url(cesta + '#realizacia'), 'name': nm,
                     'url': self.abs_url(cesta), 'image': obrazky or [self.abs_url(og)]}
            if r['rola']:
                dielo['contributor'] = {'@id': self.abs_url('#firma')}
            if r['miesto']:
                dielo['locationCreated'] = {'@type': 'Place', 'name': r['miesto']}
            if r['text']:
                dielo['description'] = r['text'].replace('\n', ' ')
            # bez datePublished a dateModified (overenie 3, nález vážne): pri realizáciách len rok alebo roky, nikde
            # mesiac ani deň (ZMENY-KOLO-1.md 5.2). datum je deň príspevku na Facebooku alebo článku starého webu, nie
            # dátum stavby (Dvojdom S: 2026-10-04 pri stavbe 2021 až 2022), upravene je deň úpravy obsahu; vyhľadávač
            # by ich mohol ukázať pri výsledku. Roky stavby nesie text a description stránky, lastmod ostáva v sitemap.
            if self.ld_media(r):
                dielo['subjectOf'] = self.ld_media(r)
            self.podstranka(cesta, kroky, title, desc, telo, 'realizacie/' + r['slug'], [dielo], og=og,
                            og_typ='article', lastmod=self.zmena(r), zaver_dalej=self.texty['vyzvy']['realizacia'])

    def stranky_noviniek(self):
        if not self.novinky:
            return
        t = self.texty['novinky']
        cesta = 'novinky/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Novinky')]
        karty = ''.join(self.novinka_karta(pre, n, nadpis='h2') for n in self.novinky)
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'\n<div class="obsah zoznam"><ul class="karty novinky-karty">{karty}</ul></div>')
        zoznam = {'@type': 'ItemList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'url': self.abs_url(f'novinky/{n["slug"]}/'), 'name': n['nadpis']}
            for i, n in enumerate(self.novinky, 1)]}
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'novinky', [zoznam], typ_stranky='CollectionPage',
                        lastmod=max(self.zmena(n) for n in self.novinky))
        for i, n in enumerate(self.novinky):
            cesta_n = f'novinky/{n["slug"]}/'
            pre_n = self.pre(cesta_n)
            kroky_n = kroky + [(cesta_n, n['nadpis'])]
            odseky = ''.join(f'<p class="{"uvodna" if i == 0 else "tlmene"}">{T(p)}</p>'
                             for i, p in enumerate(p for p in n['text'].split('\n') if p.strip()))
            # odkaz na Facebook len pri príspevku z Facebooku (novinka ASB je z časopisu, nie z Facebooku)
            fb = n['fb_url'] or (self.firma['facebook'] if n.get('zdroj') == 'fb' else '')
            if fb:
                odseky += f'<p class="tlmene"><a href="{esc(fb)}">{T("Pôvodný príspevok na Facebooku")}</a></p>'
            galeria = self.galeria(pre_n, n, n['nadpis'], datum_text(n['datum']))
            # údaje vpravo v hlave: dátum a stavba, ku ktorej novinka patrí, s rokmi stavby (pole realizacia)
            r_n = self.realizacia_novinky(n)
            fakty = [('Dátum', f'<span class="cisla">{T(datum_text(n["datum"]))}</span>')]
            if r_n:
                fakty.append(('Stavba', f'<a href="{esc(self.rel(pre_n, "realizacie/" + r_n["slug"] + "/"))}">{T(r_n["nazov"])}</a>'))
                if self.roky_karty(r_n):
                    fakty.append((self.popisok_rokov(self.roky_karty(r_n)), T(self.roky_karty(r_n))))
            bok_n = '<dl class="fakty udaje">' + ''.join(f'<div><dt>{T(k)}</dt><dd>{v}</dd></div>' for k, v in fakty) + '</dl>'
            dvoj = self.detail_dvojstrana(pre_n, n, n['nadpis'], odseky)
            telo_n = (self.hlava_stranky(pre_n, kroky_n, n['nadpis'], datum_text(n['datum']), bok=bok_n, trieda='hlava-detail')
                      + f'\n{dvoj}\n{galeria}'
                      + f'\n<div class="obsah susedia-obal">{self.susedia(self.novinky, i, "nadpis", "novinka")}'
                      f'<p class="viac"><a class="odkaz-sipka" href="../">Všetky novinky {SVG_SIPKA}</a></p></div>')
            og = f'og/novinka-{n["slug"]}.jpg'
            obal = n['obal'] or (n['galeria'][0] if n['galeria'] else None)
            self.registruj_og(og, self.og_foto(obal), n['nadpis'],
                              datum_text(n['datum']), 'BM Building, s.r.o., bmbuilding.sk')
            clanok = {'@type': 'Article', '@id': self.abs_url(cesta_n + '#clanok'), 'headline': skrat(n['nadpis'], 110),
                      'url': self.abs_url(cesta_n), 'datePublished': n['datum'], 'inLanguage': 'sk',
                      'image': [self.abs_url(self.obrazok_cesty(n, f)[f['plna_w']]) for f in n['galeria']] or [self.abs_url(og)],
                      'author': {'@id': self.abs_url('#firma')}, 'publisher': {'@id': self.abs_url('#firma')},
                      'mainEntityOfPage': {'@id': self.abs_url(cesta_n)}}
            if n['upravene']:
                clanok['dateModified'] = n['upravene']
            if n['text']:
                clanok['articleBody'] = n['text'].replace('\n', ' ')
            desc = skrat(re.sub(r'\s+', ' ', n['text'] or n['nadpis']), 155)
            if len(desc) < 70 and datum_text(n['datum']):
                # krátky príspevok (34 a 47 znakov, overenie kolo 1): doplní sa len kto a kedy ho uverejnil
                desc = s_bodkou(desc) + f' Príspevok BM Building z {datum_text(n["datum"])}.'
            title = n['nadpis'] + ' | BM Building' if len(n['nadpis']) <= 46 else skrat(n['nadpis'], 60)
            self.podstranka(cesta_n, kroky_n, title, desc, telo_n, 'novinky/' + n['slug'], [clanok], og=og,
                            og_typ='article', lastmod=self.zmena(n))

    def stranka_opravnenia(self):
        t = self.texty['opravnenia']
        cesta = 'opravnenia/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Oprávnenia')]
        doklady = self.doklady(pre, nadpis='h2', obrazky=True)
        s_obrazkom = sum(1 for x in doklady if 'data-lightbox' in x)
        # veta o zakrytí pod obrázkami (FAKTY-WEB.md, Oprávnenia: osvedčenia SKSI); bez obrázkov pôvodná poznámka
        poznamka = t['poznamka'] if s_obrazkom else t.get('poznamka_bez_obrazkov', t['poznamka'])
        lb = ''
        if s_obrazkom:
            prvy = re.search(r'data-plna="([^"]*)" data-srcset="[^"]*" data-sirka="(\d+)" data-vyska="(\d+)"', ''.join(doklady))
            lb = self.lightbox_html('Osvedčenia SKSI', html.unescape(prvy.group(1)), prvy.group(2), prvy.group(3),
                                    slovo='Osvedčenie')
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'\n<div class="obsah stranka-text"><div class="doklady">{"".join(doklady)}</div><p class="poznamka">{T(poznamka)}</p>{lb}'
                # Obchodný register ako riadok cenníka ukážky: nadpis, text zápisu a zdroj v stĺpci ceny
                f'<ul class="cennik register"><li class="sluzba"><h2 class="nadpis-maly" id="obchodny-register">{T(t["register_nadpis"])}</h2>'
                f'<p class="sluzba-opis">{T(t["register"])}</p>'
                f'<p class="cena"><small>Zdroj: <a href="{esc(t["register_zdroj"])}">výpis z Obchodného registra SR</a></small></p></li></ul></div>')
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'opravnenia', [], lastmod=self.vydanie)

    def stranka_kontakt(self):
        """Kontakt: hlava stránky a sekcia Kontakt z ukážky ako hlavný obsah (kontakt_sekcia, hlavna=True): všetky
        kontaktné a fakturačné údaje zo starého webu, kontaktný blok v poradí banneru, Navigovať a Facebook."""
        t = self.texty['kontakt']
        cesta = 'kontakt/'
        pre = self.pre(cesta)
        kroky = [('', 'Úvod'), (cesta, 'Kontakt')]
        # hlava-kontakt: sekcia hneď pod hlavou má menšie horné odsadenie a menší nadpis Kontaktné údaje (styl.css
        # .hlava-kontakt+.kontakt; doladenie-1, nález drobné: pri 1440 px 165 px prázdnej plochy a dva veľké nadpisy)
        telo = (self.hlava_stranky(pre, kroky, t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>', trieda='hlava-kontakt')
                + '\n' + self.kontakt_sekcia(pre, hlavna=True))
        # jeden uzol WebPage a ContactPage s @id namiesto dvoch uzlov pre tú istú URL (brána pokus 2, správnosť 7)
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'kontakt', [], zaver=False,
                        lastmod=self.vydanie, pata_firma=False, typ_stranky=['WebPage', 'ContactPage'],
                        stranka_navyse={'about': {'@id': self.abs_url('#firma')}})

    def ochrana_hosting(self, t):
        """Odseky časti Hosting na /ochrana-udajov/ a zdroje k nim podľa hostingu ostrého webu (čl. 13 ods. 1 písm. c)
        až f) a ods. 2 písm. a) nariadenia (EÚ) 2016/679, https://eur-lex.europa.eu/legal-content/SK/TXT/?uri=CELEX:32016R0679).
        GitHub Pages (pages): GitHub, Inc. a prenos do USA ako doteraz. WebHouse (apache, audit naplno 25. 11. 2026,
        položka 1): údaje o WebHouse len z jeho verejných stránok (texty.json ochrana.webhouse, overené 25. 11. 2026):
        identita z informácií o ochrane osobných údajov (bod 1), úloha sprostredkovateľa a logy z Podmienok spracúvania
        osobných údajov (DPA čl. 1 a 45), miesto spracúvania a prenos (DPA čl. 29 a 30), uchovávanie bezpečnostných
        logov (informácie o ochrane osobných údajov, bod 7; presnú lehotu WebHouse neuvádza). Vráti (odseky, zdroje)."""
        if self.hosting != 'apache':
            return ([
                'Web je statický a beží na službe GitHub Pages spoločnosti GitHub, Inc. Pri návšteve webu GitHub podľa svojej dokumentácie zaznamenáva a ukladá IP adresu návštevníka na bezpečnostné účely. Tieto technické záznamy spracúva GitHub, nie BM Building.',
                'GitHub, Inc. sídli v USA. Podľa svojho vyhlásenia o ochrane osobných údajov spracúva osobné údaje aj v USA a pri prenose z Európskej únie do krajín, ktorým Európska komisia nepriznala primeranú úroveň ochrany, sa spravidla opiera o štandardné zmluvné doložky Európskej komisie.'],
                [(t['github_url'], 'GitHub Pages, Data collection'),
                 (t['github_privacy_url'], 'GitHub General Privacy Statement')])
        w = t.get('webhouse') if isinstance(t.get('webhouse'), dict) else {}
        udaje = {k: jeden_riadok(w.get(k)) for k in ('meno', 'adresa', 'ico')}
        urls = {k: bezpecna_url(w.get(k), ['webhouse.sk']) for k in ('dpa_url', 'ochrana_url')}
        chyba = [k for k, v in list(udaje.items()) + list(urls.items()) if not v]
        if chyba:
            # bez overených údajov o hostingu by text ochrany údajov nebol pravdivý: zostava pre FTP sa nezapíše
            raise ChybaZostavy('sablona/texty.json ochrana.webhouse: chýba alebo je neplatné ' + ', '.join(chyba)
                               + ' (údaje o WebHouse len z jeho verejných stránok)')
        meno = udaje['meno']
        return ([
            f'Web je statický a beží na webhostingu spoločnosti {meno}, {udaje["adresa"]}, IČO {udaje["ico"]}. Pri návšteve webu server hostingu zaznamenáva IP adresu návštevníka, čas a technické údaje o požiadavke do technických a bezpečnostných záznamov (logov).',
            'Účelom týchto záznamov je bezpečná a spoľahlivá prevádzka webu, napríklad odhalenie útoku alebo chyby. Právnym základom je čl. 6 ods. 1 písm. f) nariadenia (EÚ) 2016/679, teda oprávnený záujem na bezpečnej prevádzke webu.',
            f'Príjemcom týchto údajov je {meno}, ako poskytovateľ webhostingu. Podľa svojich podmienok spracúvania osobných údajov ich spracúva ako sprostredkovateľ podľa čl. 28 nariadenia; na účely bezpečnosti a správy svojej infraštruktúry môže časť technických záznamov spracúvať aj ako samostatný prevádzkovateľ.',
            'WebHouse podľa svojich informácií o ochrane osobných údajov uchováva bezpečnostné záznamy počas obdobia primeraného ich účelu, typu služby a riziku. Osobné údaje spracúva najmä v Európskom hospodárskom priestore a do tretej krajiny ich prenáša len pri splnení podmienok kapitoly V nariadenia.'],
            [(urls['dpa_url'], 'WebHouse, Podmienky spracúvania osobných údajov (DPA), čl. 1, 29, 30 a 45'),
             # body 1 (meno, sídlo a IČO v prvom odseku) a 7 (logy, uchovávanie); oprava po overení, kontrola spustenia
             (urls['ochrana_url'], 'WebHouse, Ochrana osobných údajov, body 1 a 7')])

    def stranka_ochrana(self):
        t = self.texty['ochrana']
        cesta = 'ochrana-udajov/'
        pre = self.pre(cesta)
        f = self.firma
        adresa = self.adresa()
        kontakt = ' alebo '.join(x for x in (f['email'] and f'e-mailom na {f["email"]}', f['tel'] and f'telefonicky na {f["tel"]}') if x)
        prev = f'{f["meno"]}, {adresa}' + (f', IČO {f["ico"]}' if f['ico'] else '') + '.'
        if f['zapis']:
            prev += ' ' + f['zapis']
        hosting, zdroje_hostingu = self.ochrana_hosting(t)
        casti = [
            ('Prevádzkovateľ', [prev, f'Kontakt na prevádzkovateľa: {kontakt}.' if kontakt else '']),
            ('Čo tento web nerobí', [
                'Web nepoužíva cookies, analytiku ani iné sledovacie nástroje a nemá žiadne formuláre. Nevkladá obsah tretích strán: mapy, Facebook ani písma z cudzích serverov sa nenačítavajú. Písmo a malý skript na pohyb a ovládanie stránky sú uložené priamo na tomto webe a nič neodosielajú.',
                'Odkazy na Mapy Google, Mapy.cz a Facebook otvoria stránku tretej strany až po kliknutí. Na tej stránke platia pravidlá jej prevádzkovateľa.']),
            # čl. 13 nariadenia 2016/679: právny základ, doba uchovávania a prenos mimo EÚ (brána pokus 1, správnosť 10);
            # text podľa hostingu ostrého webu (audit naplno 25. 11. 2026, položka 1, ochrana_hosting)
            ('Hosting', hosting),
            ('Ak nás kontaktujete', [
                'Ak nám napíšete e-mail alebo zavoláte, údaje, ktoré nám pritom poskytnete, použijeme len na vybavenie vašej požiadavky.',
                'Právnym základom je čl. 6 ods. 1 písm. b) nariadenia (EÚ) 2016/679, ak ide o opatrenia pred uzatvorením zmluvy na vašu žiadosť, napríklad dopyt na stavebný dozor. Pri ostatných otázkach je ním čl. 6 ods. 1 písm. f), teda náš oprávnený záujem odpovedať na správu, ktorú ste nám poslali.',
                'Údaje uchovávame len tak dlho, ako je potrebné na vybavenie požiadavky. Ak z nej vznikne zmluva, uchovávame ich po dobu, ktorú vyžadujú právne predpisy.']),
            ('Vaše práva', [
                'Podľa nariadenia (EÚ) 2016/679 (GDPR) máte právo na prístup k svojim osobným údajom, na ich opravu, vymazanie a obmedzenie spracúvania, právo namietať a právo na prenosnosť údajov. Uplatniť ich môžete ' + (kontakt + ' alebo ' if kontakt else '') + f'písomne na adrese {adresa}.',
                'Máte tiež právo podať sťažnosť Úradu na ochranu osobných údajov Slovenskej republiky.']),
        ]
        html_casti = []
        for nadpis, odseky in casti:
            html_casti.append(f'<h2 class="nadpis-maly">{T(nadpis)}</h2>' + ''.join(f'<p>{T(p)}</p>' for p in odseky if p))
        odkazy = ('<p class="poznamka">Zdroje: '
                  + ''.join(f'<a href="{esc(url)}">{T(text)}</a>, ' for url, text in zdroje_hostingu)
                  + f'<a href="{esc(t["uoou_url"])}">Úrad na ochranu osobných údajov SR</a>. '
                  'Písmo Geist a Geist Mono (The Geist Project Authors), licencia SIL Open Font License 1.1: '
                  f'<a href="{esc(pre)}fonts/OFL-Geist.txt">fonts/OFL-Geist.txt</a>.</p>')
        kroky = [('', 'Úvod'), (cesta, 'Ochrana osobných údajov')]
        telo = (self.hlava_stranky(pre, kroky, t['h1'])
                + f'\n<div class="obsah stranka-text"><div class="text">{"".join(html_casti)}{odkazy}</div></div>')
        self.podstranka(cesta, kroky, t['title'], t['description'], telo, 'ochrana-udajov', [], lastmod=self.vydanie)

    def stranka_404(self):
        t = self.texty['chyba']
        zaklad = self.nastavenia.get('zaklad_nahladu', '/bmbuilding/') if self.nahlad else '/'
        if not re.match(r'^/[A-Za-z0-9._/-]*$', zaklad) or '..' in zaklad:
            zaklad = '/'
        if not zaklad.endswith('/'):
            zaklad += '/'
        z = esc(zaklad)
        # text sľubuje „alebo nám zavolajte“: tlačidlo Zavolať aj v tele, nielen v hlavičke (brána dizajnu pokus 2, nález 6)
        f = self.firma
        tel = f'<a class="tlacidlo" href="tel:{esc(f["tel_href"])}">Zavolať {SVG_SIPKA}</a>' if f['tel_href'] else ''
        # akcie ako v úvode ukážky: plné tlačidlo Zavolať a odkazy so šípkou
        telo = (self.hlava_stranky(zaklad, [], t['h1'], bok=f'<p class="uvodny">{T(t["veta"])}</p>')
                + f'<div class="obsah chyba"><div class="uvod-akcie akcie-vlavo">{tel}<a class="odkaz-sipka" href="{z}">Na úvod {SVG_SIPKA}</a>'
                f'<a class="odkaz-sipka" href="{z}realizacie/">Realizácie {SVG_SIPKA}</a><a class="odkaz-sipka" href="{z}kontakt/">Kontakt {SVG_SIPKA}</a></div></div>')
        self.stranka(None, t['title'], '', telo, '', [], do_mapy=False, zaklad=zaklad)

    def presmerovania(self):
        """Staré adresy Joomly (ciele_presmerovani). GitHub Pages: statická stránka s meta refresh a canonical pre
        každú adresu v index.php/. Apache (FTP na WebHouse): nič sa nezapisuje, priečinok index.php/ by sa vedľa súboru
        index.php Joomly nedal vytvoriť; tie isté ciele ako 301 zapíše htaccess() (audit naplno, položka 1)."""
        self.ciele_starych = self.ciele_presmerovani()
        if self.hosting == 'apache':
            return
        for stara, ciel in self.ciele_starych:
            cesta = stara.strip('/') + '/'
            subor = cesta + 'index.html'
            ciel_cesta, _, kotva = ciel.lstrip('/').partition('#')
            rel = self.rel(self.pre(cesta), ciel_cesta) + (('#' + kotva) if kotva else '')
            canon = self.abs_url(ciel_cesta)
            robots = '<meta name="robots" content="noindex, nofollow">\n' if self.nahlad else ''
            # prázdna ikona (data:,) a img-src data:: bez nej Firefox pýta /favicon.ico mimo CSP (porušenie, overenie
            # kolo 1); skutočná ikona by sa pri okamžitom presmerovaní aj tak zrušila (requestfailed, oprava 1)
            obsah = ('<!doctype html>\n<html lang="sk">\n<head>\n<meta charset="utf-8">\n'
                     '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; base-uri \'none\'; form-action \'none\'">\n'
                     f'{robots}<title>Stránka sa presunula | BM Building</title>\n'
                     '<link rel="icon" href="data:,">\n'
                     f'<meta http-equiv="refresh" content="0; url={esc(rel)}">\n'
                     f'<link rel="canonical" href="{esc(canon)}">\n'
                     '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
                     f'<p>Stránka sa presunula na novú adresu: <a href="{esc(rel)}">{esc(canon)}</a></p>\n</body>\n</html>\n')
            self.subory[subor] = obsah.encode('utf-8')

    def ciele_presmerovani(self):
        """[(stará cesta, cieľ)] zo sablona/presmerovania.json v poradí zoznamu; cieľ je cesta od koreňa webu, voliteľne
        s kotvou. Rovnaké pravidlá pre oba hostingy: premenovaná kotva (KOTVY_PREMENOVANE), neplatná stará cesta sa
        preskočí, neplatný cieľ je úvod, nezverejnená realizácia ide na zoznam, stará adresa na mieste stránky webu sa
        preskočí. Volá sa až po zostavení stránok (kolízia so self.subory)."""
        cesty_webu = set(self.subory)
        d = json.loads((SABLONA / 'presmerovania.json').read_text(encoding='utf-8'))
        slugy = {r['slug'] for r in self.realizacie}
        vysledok = []
        for z in d.get('presmerovania', []):
            stara = str(z.get('stara') or '')
            ciel = str(z.get('ciel') or '/')
            # sekcia Činnosť na úvode má od zmeny 3A kotvu #cinnost (predtým #sluzby); zoznam presmerovaní je kópia
            # zo seo/PRESMEROVANIA.json, preto sa stará kotva prepíše tu
            ciel = KOTVY_PREMENOVANE.get(ciel, ciel)
            if not re.match(r'^/[A-Za-z0-9._/-]+$', stara) or '..' in stara or '//' in stara:
                varuj(f'presmerovanie {stara!r}: neplatná stará cesta, preskočené')
                continue
            if not re.match(r'^/[a-z0-9/-]*(#[a-z0-9-]+)?$', ciel):
                varuj(f'presmerovanie {stara!r}: neplatný cieľ {ciel!r}, použitý úvod')
                ciel = '/'
            m = re.match(r'^/realizacie/([a-z0-9-]+)/$', ciel)
            if m and m.group(1) not in slugy:
                # realizácia skrytá zámerne (zverejnene: false, ZMENY-KOLO-1.md 5.6) ide na zoznam bez varovania;
                # varuje sa len cieľ, ktorý v obsahu vôbec nie je
                if not (Path(self.obsah) / 'realizacie' / f'{m.group(1)}.json').is_file():
                    varuj(f'presmerovanie {stara}: realizácia {m.group(1)} nie je zverejnená, cieľ /realizacie/')
                ciel = '/realizacie/'
            if stara.strip('/') + '/index.html' in cesty_webu:
                varuj(f'presmerovanie {stara}: koliduje so stránkou webu, preskočené')
                continue
            vysledok.append((stara, ciel))
        return vysledok

    def htaccess(self):
        """.htaccess ostrého webu na webhostingu WebHouse (Apache 2.4; hosting apache, audit naplno 25. 11. 2026,
        položka 1). Pravidlá prepisu platia len pre doménu a jej www: iná doména alebo subdoména s koreňom
        v podpriečinku webu ich nezdedí. Poradie:
          1. /.well-known/ bez presmerovania (overenie certifikátu Let's Encrypt, ktorý WebHouse vydáva),
          2. staré adresy Joomly (ciele_presmerovani) jedným skokom 301 rovno na https a holú doménu, query sa zahodí
             (QSD), kotva sa nekóduje (NE); ostatné adresy pod index.php (index.php?option=..., /index.php/component/)
             na úvod,
          3. priečinok stránky bez lomky na konci rovno na kanonickú adresu s lomkou (z http aj www jeden skok), len
             priečinok zostavy (oprava po overení: priečinok Joomly s prázdnym index.html, napríklad /images alebo
             /cache, dostal najprv 301 na adresu s lomkou a až potom 404, čo prezrádzalo Joomlu na hostingu),
          4. www na holú doménu a http na https; %{HTTPS} !=on podľa návodu WebHouse „Ako nastaviť https prístup“
             (https://helpdesk.webhouse.sk/357913--Ako-nastavi%C5%A5-https-pr%C3%ADstup),
          5. na webe je len to, čo zapísala zostava: iné meno v koreni (súbory a priečinky Joomly, staré zálohy, súbory
             zostavy pre GitHub Pages) vráti 404 s vlastnou stránkou, priečinok bez index.html tiež (bez výpisu obsahu
             a bez Options, ktoré hosting v .htaccess nemusí povoliť), PHP nikde.
        Ďalej DirectoryIndex index.html pred index.php Joomly (https://httpd.apache.org/docs/2.4/mod/mod_dir.html),
        ErrorDocument 404 (https://httpd.apache.org/docs/2.4/mod/core.html#errordocument), typy AVIF, WebP, woff2
        a webmanifest, UTF-8, hlavičky, ktoré web s CSP v meta znesie (nosniff, Referrer-Policy, zákaz rámca inou
        stránkou cez X-Frame-Options a frame-ancestors, ktoré meta CSP nevie, Permissions-Policy, COOP), cache podľa
        typu (app.js má v adrese ?v=, preto rok; písma a fotky bez odtlačku v mene mesiac; HTML vždy overiť) a gzip.
        HSTS zámerne nie: pre návštevníkov je na rok nevratné a pri výpadku certifikátu alebo pri prechode na GitHub
        Pages (cesta DNS ostáva zálohou) by web nešiel otvoriť ani s výnimkou. RewriteEngine nie je v IfModule:
        bez mod_rewrite (WebHouse ho v návode používa) radšej chyba 500 hneď pri kontrole po nahratí, než web
        s verejnými súbormi Joomly bez presmerovaní. Pravidlá overuje simulácia nastroje/htaccess_simulacia.py."""
        p = urllib.parse.urlsplit(self.domena)
        host = p.hostname or ''
        if p.scheme != 'https' or not re.match(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$', host) or p.path not in ('', '/'):
            raise ChybaZostavy(f'hosting apache potrebuje doménu https bez cesty (nastavenia.json domena), nie {self.domena!r}')
        d = 'https://' + host
        hre = host.replace('.', '\\.')

        def vzor(cesta):
            # cesty sú overené na [A-Za-z0-9._/-]; zo znakov regulárneho výrazu je v nich len bodka
            return cesta.replace('.', '\\.')

        # mená v koreni výstupu (súbory a priečinky zostavy): pravidlo 3 len pre ne, pravidlo 5 všetko ostatné na 404
        koren = sorted({k.split('/', 1)[0] for k in list(self.subory) + list(self.obrazky)} - {HTACCESS})
        for meno in koren:
            if not re.match(r'^[A-Za-z0-9._-]+$', meno) or meno in ('.', '..'):
                raise ChybaZostavy(f'súbor webu {meno!r} sa nedá zapísať do .htaccess')
        mena_korena = '(?:' + '|'.join(vzor(m) for m in koren) + ')'
        r = [f'# Web {d}/ na webhostingu WebHouse (Apache). Súbor zapisuje build.py --hosting apache pri každej zostave,',
             '# ručne ho nemeniť. Pravidlá overuje test_build.py (TestHostingApache) a nastroje/htaccess_simulacia.py.',
             '',
             '# znaková sada a typy, ktoré staršie mime.types nepoznajú',
             'AddDefaultCharset UTF-8',
             '<IfModule mod_mime.c>',
             '  AddType image/avif .avif',
             '  AddType image/webp .webp',
             '  AddType font/woff2 .woff2',
             '  AddType application/manifest+json .webmanifest',
             '  AddCharset UTF-8 .css .js .json .svg .txt .webmanifest .xml',
             '</IfModule>',
             '',
             '# úvod je index.html, nikdy nie index.php Joomly (index.php ostáva len pre iný web v podpriečinku)',
             'DirectoryIndex index.html index.php',
             'ErrorDocument 404 /404.html',
             '',
             'RewriteEngine On',
             '',
             f'# 0. pravidlá len pre {host} a www.{host}; iná doména alebo subdoména s koreňom v podpriečinku ich nezdedí',
             f'RewriteCond %{{HTTP_HOST}} !^(?:www\\.)?{hre}\\.?(?::[0-9]+)?$ [NC]',
             'RewriteRule ^ - [L]',
             '',
             '# 1. overenie certifikátu (Let\'s Encrypt) a iné súbory v /.well-known/ bez presmerovania',
             'RewriteRule ^\\.well-known/ - [L]',
             '',
             f'# 2. staré adresy Joomly (sablona/presmerovania.json, {len(self.ciele_starych)}) jedným skokom 301 na {d}, bez query']
        for stara, ciel in self.ciele_starych:
            priznaky = 'R=301,L,QSD' + (',NE' if '#' in ciel else '')
            r.append(f'RewriteRule ^{vzor(stara.strip("/"))}/?$ {d}{ciel} [{priznaky}]')
        r += ['# ostatné adresy Joomly pod index.php (index.php?option=..., /index.php/component/...) na úvod',
              f'RewriteRule ^index\\.php/ {d}/ [R=301,L,QSD]',
              '',
              '# 3. priečinok stránky bez lomky na konci rovno na kanonickú adresu s lomkou (len priečinok zostavy:',
              '# priečinok Joomly s prázdnym index.html, napríklad /images, dostane hneď 404 z pravidla 5, nie skok)',
              f'RewriteCond $1 ^{mena_korena}(?:/|$)',
              'RewriteCond %{REQUEST_FILENAME} -d',
              'RewriteCond %{REQUEST_FILENAME}/index.html -f',
              f'RewriteRule ^(.+[^/])$ {d}/$1/ [R=301,L]',
              '',
              '# 4. www na holú doménu a http na https (návod WebHouse: Ako nastaviť https prístup)',
              'RewriteCond %{HTTP_HOST} ^www\\. [NC]',
              f'RewriteRule ^ {d}%{{REQUEST_URI}} [R=301,L]',
              'RewriteCond %{HTTPS} !=on',
              f'RewriteRule ^ {d}%{{REQUEST_URI}} [R=301,L]',
              '',
              '# 5. na webe je len to, čo zapísala zostava: iné meno v koreni (Joomla, staré súbory) vráti 404']
        r += [f'RewriteCond $1 !^{mena_korena}$',
              'RewriteRule ^([^/]+) - [R=404,L]',
              '# priečinok bez index.html (fonts/, obrazky/, og/) bez výpisu obsahu',
              'RewriteCond %{REQUEST_FILENAME} -d',
              'RewriteCond %{REQUEST_FILENAME}/index.html !-f',
              'RewriteRule ^. - [R=404,L]',
              '# PHP sa nespúšťa nikde, nový web ho nemá',
              'RewriteRule \\.ph(?:p[0-9]?|tml|ar)(?:/|$) - [R=404,L]',
              '',
              '# hlavičky, ktoré web s CSP v meta znesie; frame-ancestors meta CSP nevie, preto v hlavičke',
              '<IfModule mod_headers.c>',
              '  Header always set X-Content-Type-Options "nosniff"',
              '  Header always set Referrer-Policy "strict-origin-when-cross-origin"',
              '  Header always set X-Frame-Options "SAMEORIGIN"',
              '  Header always set Content-Security-Policy "frame-ancestors \'self\'"',
              '  Header always set Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()"',
              '  Header always set Cross-Origin-Opener-Policy "same-origin"',
              '</IfModule>',
              '',
              '# cache: HTML vždy overiť, app.js má v adrese ?v=<odtlačok> (rok), písma a fotky bez odtlačku v mene mesiac',
              '<IfModule mod_expires.c>',
              '  ExpiresActive On']
        for typ, cas in CACHE_APACHE:
            r.append(f'  ExpiresByType {typ} "{cas}"')
        r += ['</IfModule>',
              '',
              '<IfModule mod_deflate.c>',
              '  <IfModule mod_filter.c>',
              '    AddOutputFilterByType DEFLATE ' + ' '.join(GZIP_APACHE),
              '  </IfModule>',
              '</IfModule>',
              '']
        return '\n'.join(r)

    def llms_txt(self):
        """llms.txt ostrého webu (audit naplno 25. 11. 2026, položka 7; firemný checklist zverejnenia ops/runbook.md
        r. 44; formát https://llmstxt.org/): H1 s menom firmy, súhrn v citácii (description úvodu), odsek o firme
        (H1 a O firme z hrdinu úvodu), kontaktné a fakturačné údaje, osvedčenia SKSI, potom zoznamy odkazov Služby,
        Realizácie a Firma s popisom z texty.json alebo z údajov realizácie. Len z obsah/firma.json, texty.json,
        realizácií a osvedčení, adresy cez doménu (abs_url), nič sa nevymýšľa. Údaje z obsahu idú do markdownu
        s escapovaním znakov odkazu a HTML (\\ [ ] < > `), každý údaj na jednom riadku; výkričník a pomlčky em a en
        stráži kontrola výstupu (skontroluj_text)."""
        f = self.firma
        t = self.texty

        def md(s):
            s = jeden_riadok(s)
            for z in '\\[]<>`':
                s = s.replace(z, '\\' + z)
            return s

        def odkaz(text, cesta, popis=''):
            return f'- [{md(text)}]({self.abs_url(cesta)})' + (f': {md(popis)}' if jeden_riadok(popis) else '')

        r = [f'# {md(f["meno"])}', '']
        if jeden_riadok(t['uvod'].get('description')):
            r += [f'> {md(t["uvod"]["description"])}', '']
        o_firme = [jeden_riadok(t['uvod'].get('h1'))] + [jeden_riadok(x) for x in
                                                          self.zoznam_textov(t['uvod'].get('o_firme_hrdina'), 'uvod.o_firme_hrdina')]
        if any(o_firme):
            r += [md(' '.join(x for x in o_firme if x)), '']
        udaje = [(k, v) for k, v in (('Sídlo', self.adresa()), ('Telefón', f['tel']), ('E-mail', f['email']),
                                      ('IČO', f['ico']), ('IČ DPH', f['ic_dph'])) if v]
        veta = ' '.join(s_bodkou(f'{k}: {v}') for k, v in udaje)
        if f['zapis']:
            veta = (veta + ' ' + s_bodkou(f['zapis'])).strip()
        if veta:
            r += [md(veta), '']
        if self.veta_osvedceni():
            r += [md(self.veta_osvedceni()), '']
        sluzby = [odkaz(t['sluzby'][s]['menu'], f'{s}/', t['sluzby'][s].get('description', ''))
                  for s in self.poradie_sluzieb()]
        if sluzby:
            r += ['## Služby', ''] + sluzby + ['']
        r += ['## Realizácie', '', odkaz('Všetky realizácie', 'realizacie/', t['realizacie'].get('description', ''))]
        for x in self.realizacie:
            druh = self.druh_karty(x)
            popis = ' '.join(([s_bodkou(f'Druh stavby: {druh}')] if druh else []) + self.vety_realizacie(x))
            r.append(odkaz(self.nazov_s_miestom(x), f'realizacie/{x["slug"]}/', popis))
        r += ['', '## Firma', '',
              odkaz(t['opravnenia']['h1'], 'opravnenia/', t['opravnenia'].get('description', '')),
              odkaz(t['kontakt']['h1'], 'kontakt/', t['kontakt'].get('description', '')),
              odkaz(t['ochrana']['h1'], 'ochrana-udajov/', t['ochrana'].get('description', '')), '']
        return '\n'.join(r)

    def staticke(self):
        # favicon a logá zo značky (LOGO.md bod 5): SVG priamo zo sablona/, rastre renderované z tých istých ciest
        self.subory['favicon.svg'] = self.favicon_svg.encode('utf-8')
        self.subory['logo.svg'] = self.logo_svg.encode('utf-8')
        if self.logo_hlavicka:
            self.subory['logo-hlavicka.svg'] = self.logo_hlavicka.encode('utf-8')
        # prázdny obrázok 4 : 5 vo farbe podkladu karty pásu (src kariet, kým app.js nedosadí skutočnú fotku)
        buf = io.BytesIO()
        Image.new('RGB', (4, 5), (0x2A, 0x2B, 0x2B)).save(buf, 'WEBP', lossless=True)
        self.subory['prazdna-45.webp'] = buf.getvalue()
        # ručne kreslené rastre favicon (nastroje favicon.py v ops/.../favicon/): kopírujú sa po bajtoch, nič sa
        # nezmenšuje z SVG (Lanczos by zahodil ručné pixely 16 a 32 px)
        for meno in FAVICON_SUBORY:
            p = SABLONA / 'favicon' / meno
            if p.is_file():
                self.subory[meno] = p.read_bytes()
            else:
                varuj(f'sablona/favicon/{meno} chýba, web ho nebude mať')
        logo = self.logo_svg
        odt_logo = hashlib.sha256((VERZIA_OG + '|logo|' + logo).encode('utf-8')).hexdigest()
        self.obrazky['logo.png'] = (odt_logo + '-512', lambda: png_bajty(svg_obraz(logo, 512)))
        for zdroj in PISMA:
            p = SABLONA / zdroj
            if p.is_file():
                self.subory['fonts/' + zdroj] = p.read_bytes()
            elif zdroj.endswith('.woff2'):
                varuj(f'sablona/{zdroj} chýba, web pôjde s náhradným písmom')
        if self.js:
            self.subory['app.js'] = self.js
        if self.nahlad:
            self.subory['robots.txt'] = b'User-agent: *\nDisallow: /\n'
        else:
            # roboti AI výslovne (AI_ROBOTI) a llms.txt len v ostrom behu: náhľad nesmie ísť do indexu ani do AI
            self.subory['robots.txt'] = (''.join(f'User-agent: {b}\n' for b in AI_ROBOTI) + 'Allow: /\n\n'
                                         f'User-agent: *\nAllow: /\n\nSitemap: {self.domena}/sitemap.xml\n').encode('utf-8')
            self.subory['llms.txt'] = self.llms_txt().encode('utf-8')
            riadky = ['<?xml version="1.0" encoding="UTF-8"?>',
                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for cesta, lastmod in sorted(self.mapa_stranok):
                riadky.append(f'<url><loc>{esc(self.abs_url(cesta))}</loc>' + (f'<lastmod>{lastmod}</lastmod>' if lastmod else '') + '</url>')
            riadky.append('</urlset>')
            self.subory['sitemap.xml'] = ('\n'.join(riadky) + '\n').encode('utf-8')
            if self.hosting == 'apache':
                # FTP na WebHouse: CNAME a .nojekyll sú len pre GitHub Pages; .htaccess až po všetkých súboroch webu
                # (zoznam mien v koreni pre pravidlo 404)
                self.subory[HTACCESS] = self.htaccess().encode('utf-8')
            else:
                self.subory['CNAME'] = (urllib.parse.urlsplit(self.domena).hostname + '\n').encode('utf-8')
                self.subory['.nojekyll'] = b''

    def skontroluj_znaky_pisma(self):
        """Písmo Geist je podmnožina latinky (sablona/pismo-znaky.txt, nastroje/znaky_geist.py); znak mimo nej by
        sa vykreslil náhradným písmom, preto varovanie s prvou stránkou, kde je."""
        if self.znaky_pisma is None:
            return
        chybajuce = {}
        for cesta, data in sorted(self.subory.items()):
            if not cesta.endswith('.html') or cesta.startswith('index.php/'):
                continue
            p = _TextStranky()
            p.feed(data.decode('utf-8'))
            p.close()
            for ch in set(''.join(p.texty)) - self.znaky_pisma - {'\n', '\t', '\r'}:
                chybajuce.setdefault(ch, cesta)
        if chybajuce:
            varuj('písmo Geist nemá znaky ' + ' '.join(f'{c!r} ({p})' for c, p in sorted(chybajuce.items()))
                  + ' (sablona/pismo-znaky.txt)')

    def postav(self):
        self.uvod()
        self.stranky_sluzieb()
        self.stranka_realizacii()
        self.stranky_realizacie()
        self.stranky_noviniek()
        self.stranka_opravnenia()
        self.stranka_kontakt()
        self.stranka_ochrana()
        self.stranka_404()
        self.presmerovania()
        self.staticke()
        self.skontroluj_znaky_pisma()


# ---------------------------------------------------------------- kontrola výstupu

class _Zber(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texty = []

    def handle_data(self, data):
        self.texty.append(data)

    def handle_starttag(self, tag, attrs):
        for _, v in attrs:
            if v:
                self.texty.append(v)


def skontroluj_text(subory):
    chyby = []
    for cesta, data in subory.items():
        if not cesta.endswith(('.html', '.xml', '.txt', '.svg')) or cesta.startswith('fonts/'):
            continue
        s = data.decode('utf-8')
        if any(p in s for p in '\u2013\u2014'):
            chyby.append(f'{cesta}: pomlčka em alebo en')
        telo = s[len('<!doctype html>'):] if s.startswith('<!doctype html>') else s
        if '!' in telo:
            chyby.append(f'{cesta}: výkričník')
        if cesta.endswith('.html'):
            z = _Zber()
            z.feed(s)
            z.close()
    if chyby:
        raise ChybaZostavy('výstup neprešiel kontrolou textu: ' + '; '.join(chyby[:10]))


# ---------------------------------------------------------------- zápis

def bezpecna_rel(rel):
    kusy = rel.split('/')
    return rel and not rel.startswith('/') and all(k not in ('', '.', '..') for k in kusy) and '\\' not in rel


PAMAT_KODOVANIA = 64 * 1024 * 1024
_KODOVANE = {}


_KODOVANE_BAJTY = [0]


def _kluc_pamate(odt):
    # aj totožnosť kódovacích funkcií: testy ich nahrádzajú rýchlou náhradou, tá nesmie zostať v pamäti pre ostrý beh
    return f'{odt}|{id(webp_bajty)}|{id(avif_bajty)}'


def _zapamataj(odt, data):
    """Zakódované bajty podľa odtlačku (najstaršie von, keď súčet presiahne PAMAT_KODOVANIA)."""
    k = _kluc_pamate(odt)
    if k in _KODOVANE:
        return
    _KODOVANE[k] = data
    _KODOVANE_BAJTY[0] += len(data)
    while _KODOVANE and _KODOVANE_BAJTY[0] > PAMAT_KODOVANIA:
        _KODOVANE_BAJTY[0] -= len(_KODOVANE.pop(next(iter(_KODOVANE))))


def zapis(web, vystup, manifest=None, navyse=None):
    """Zapíše web do vystup. manifest: zoznam zostavy (predvolený vystup/.zostava.json; pri hostingu apache vedľa
    výstupu, aby sa nenahral na FTP). navyse: ďalšie polia zoznamu zostavy (pri hostingu apache commit, z ktorého sa
    stavalo); zoznam zostavy pre GitHub Pages ostáva bez nich."""
    vystup.mkdir(parents=True, exist_ok=True)
    stary = {}
    m = vystup / MANIFEST if manifest is None else manifest
    if m.is_file():
        try:
            d = json.loads(m.read_text(encoding='utf-8'))
            if isinstance(d.get('subory'), dict):
                stary = {k: v for k, v in d['subory'].items() if isinstance(k, str) and bezpecna_rel(k)}
        except ValueError:
            varuj('výstupný .zostava.json je poškodený, nič sa nemaže')
    elif any(vystup.iterdir()):
        varuj(f'{vystup} nie je prázdny a nemá {MANIFEST}: cudzie súbory nechávam, prepíšem len vlastné')
    novy = {}
    plan = dict(web.subory)
    for rel, (odt, _) in web.obrazky.items():
        novy[rel] = odt
    for rel in plan:
        novy[rel] = hashlib.sha256(plan[rel]).hexdigest()
    # najprv vyrob všetky nové obrázky do pamäte; ak zlyhá čo i len jeden, predchádzajúci výstup ostane celý
    # (brána pokus 1, správnosť 2: useknutá fotka zmazala web a build spadol s OSError)
    obrazky = {}
    ulohy = {}
    for rel, (odt, funkcia) in sorted(web.obrazky.items()):
        if stary.get(rel) == odt and (vystup / rel).is_file():
            continue
        # rovnaký odtlačok = rovnaké bajty (tá istá fotka vo viacerých realizáciách, ilustračná fotka, ďalší beh
        # v tom istom procese): kóduje sa raz, pamäť najviac PAMAT_KODOVANIA bajtov
        if _kluc_pamate(odt) in _KODOVANE:
            obrazky[rel] = _KODOVANE[_kluc_pamate(odt)]
            continue
        ulohy.setdefault(odt, (funkcia, []))[1].append(rel)
    # kódovanie vo vláknach (Pillow pri kódovaní a zmene veľkosti uvoľní GIL, na PC asi 5-krát rýchlejšie);
    # každý súbor sa kóduje sám v jednom vlákne kodeku, takže bajty nezávisia od počtu vlákien
    with ThreadPoolExecutor(max_workers=max(1, min(16, os.cpu_count() or 1))) as ex:
        buduce = {ex.submit(funkcia): (odt, rely) for odt, (funkcia, rely) in ulohy.items()}
        for fut in as_completed(buduce):
            odt, rely = buduce[fut]
            try:
                data = fut.result()
            except Exception as e:
                for f in buduce:
                    f.cancel()
                raise ChybaZostavy(f'obrázok {rely[0]} sa nedá vyrobiť ({e.__class__.__name__}: {e}); '
                                   f'predchádzajúci výstup ostal bez zmeny')
            _zapamataj(odt, data)
            for rel in rely:
                obrazky[rel] = data
    # zmaž vlastný predchádzajúci výstup, ktorý v novom nie je alebo sa zmenil (obrázky s rovnakým odtlačkom ostanú)
    for rel, odt in stary.items():
        p = vystup / rel
        if (rel in web.obrazky or rel in plan) and odt == novy.get(rel) and p.is_file():
            continue
        if p.is_file():
            p.unlink()
    for rel, data in obrazky.items():
        p = vystup / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    kodovanych = len(obrazky)
    for rel, data in sorted(plan.items()):
        p = vystup / rel
        # rovnaké bajty sa neprepisujú (opakovaný build bez zmeny nič nezmení, ani čas súboru)
        if stary.get(rel) == novy[rel] and p.is_file() and p.stat().st_size == len(data) and p.read_bytes() == data:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    # prázdne priečinky po vlastných súboroch
    for rel in sorted(stary, key=lambda x: -x.count('/')):
        rodic = (vystup / rel).parent
        while rodic != vystup and rodic.is_dir() and not any(rodic.iterdir()):
            rodic.rmdir()
            rodic = rodic.parent
    zoznam = {'verzia': 1, **(navyse or {}), 'subory': dict(sorted(novy.items()))}
    zostava = (json.dumps(zoznam, ensure_ascii=False, indent=0) + '\n').encode('utf-8')
    if not (m.is_file() and m.read_bytes() == zostava):
        m.write_bytes(zostava)
    return kodovanych


def commit_repozitara(koren=KOREN):
    """Commit, z ktorého zostava stavia: len čítanie .git/HEAD, referencie a packed-refs, git sa nespúšťa. Pri hostingu
    apache ide do zoznamu zostavy vedľa výstupu a do výpisu, aby sa dal porovnať s commitom náhľadu, ktorý Mário
    schválil (oprava po overení, kontrola spustenia: postup FTP stavia z disku, ktorý môže mať necommitnuté zmeny alebo
    iný commit ako nasadený náhľad; čistotu pracovného stromu overuje postup príkazom git status, SPUSTENIE-FTP.md
    časť 1). Vráti 40 znakov hex, alebo '' (bez priečinka .git, poškodený HEAD, neznáma referencia)."""
    git = Path(koren) / '.git'
    hex40 = re.compile(r'[0-9a-f]{40}')
    try:
        if not git.is_dir():
            return ''
        head = (git / 'HEAD').read_text(encoding='utf-8').strip()
        if hex40.fullmatch(head):
            return head
        m = re.fullmatch(r'ref:\s*(refs/[A-Za-z0-9._/-]+)', head)
        if not m or '..' in m.group(1):
            return ''
        ref = m.group(1)
        if (git / ref).is_file():
            sha = (git / ref).read_text(encoding='utf-8').strip()
            return sha if hex40.fullmatch(sha) else ''
        if (git / 'packed-refs').is_file():
            for riadok in (git / 'packed-refs').read_text(encoding='utf-8').splitlines():
                casti = riadok.split()
                if len(casti) == 2 and casti[1] == ref and hex40.fullmatch(casti[0]):
                    return casti[0]
    except (OSError, UnicodeDecodeError):
        return ''
    return ''


def manifest_ftp(vystup):
    """Zoznam zostavy pre hosting apache: vedľa výstupu (<priečinok>.zostava.json), aby sa s priečinkom nenahral na FTP.
    Priečinok musí byť mimo repozitára a prázdny alebo len so súbormi predchádzajúcej FTP zostavy, lebo na FTP sa
    nahrá celý: zostava pre GitHub Pages (index.php/, CNAME, .zostava.json vnútri) ani ručne pridaný súbor v ňom
    byť nesmú. Vráti (cesta zoznamu, chyba)."""
    if vystup == KOREN or KOREN in vystup.parents:
        return None, f'výstup pre FTP {vystup} patrí mimo repozitára, napríklad D:/ARLing-media/klienti/bmbuilding/web-ftp'
    manifest = vystup.parent / (vystup.name + MANIFEST)
    if not vystup.exists():
        return manifest, ''
    if not vystup.is_dir():
        return None, f'výstup {vystup} nie je priečinok'
    if (vystup / MANIFEST).exists():
        return None, f'{vystup} je zostava pre GitHub Pages ({MANIFEST} vnútri); pre FTP zvoľ prázdny priečinok'
    subory = {p.relative_to(vystup).as_posix() for p in vystup.rglob('*') if p.is_file()}
    stary = set()
    if subory and manifest.is_file():
        try:
            d = json.loads(manifest.read_text(encoding='utf-8'))
            stary = set(d['subory'])
        except (ValueError, KeyError, TypeError):
            return None, f'zoznam zostavy {manifest} je poškodený; pre FTP zvoľ prázdny priečinok'
    cudzie = sorted(subory - stary)
    if cudzie:
        return None, (f'{vystup} má súbory, ktoré nezapísala predchádzajúca FTP zostava ({", ".join(cudzie[:5])}'
                      f'{", ..." if len(cudzie) > 5 else ""}); na FTP sa nahrá celý priečinok, zvoľ prázdny')
    return manifest, ''


def main(argv=None):
    ap = argparse.ArgumentParser(description='Zostava statického webu BM Building z obsah/.')
    ap.add_argument('--nahlad', action='store_true', help='náhľad: noindex, robots Disallow, bez sitemap, llms.txt, CNAME a .nojekyll')
    ap.add_argument('--vystup', type=Path, default=KOREN / 'web', help='výstupný priečinok (predvolený web/)')
    ap.add_argument('--obsah', type=Path, default=KOREN / 'obsah', help='priečinok obsahu (predvolený obsah/)')
    ap.add_argument('--fotky-web', type=Path, default=FOTKY_WEB,
                    help='hotové webové verzie fotiek s manifest.json (predvolený fotky-web/)')
    ap.add_argument('--hosting', choices=HOSTINGY, default=None,
                    help='hosting ostrého webu: pages (GitHub Pages, predvolený) alebo apache (FTP na WebHouse: .htaccess, '
                         'presmerovania 301, bez CNAME, .nojekyll a index.php/, výstup mimo repozitára, zoznam zostavy '
                         'vedľa neho); bez prepínača z premennej BMB_HOSTING')
    a = ap.parse_args(argv)
    for prud in (sys.stdout, sys.stderr):
        try:
            prud.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    VAROVANIA.clear()
    PRISNE_VAROVANIA.clear()
    nahlad = a.nahlad
    v_actions = os.environ.get('GITHUB_ACTIONS') == 'true'
    # GitHub Actions (workflow linky bmb-obsah volá „python build.py“): ostrý beh len s BMB_OSTRY=ano, inak náhľad
    # s noindex, Disallow a 404 pod /bmbuilding/, kým doména nie je prepnutá (brána pokus 1, správnosť 1)
    if not nahlad and v_actions and os.environ.get('BMB_OSTRY', '').strip().lower() != 'ano':
        nahlad = True
        print('GitHub Actions bez BMB_OSTRY=ano: zostavujem náhľad (noindex). Ostrý web po prepnutí domény: BMB_OSTRY=ano.')
    # hosting ostrého webu (audit naplno 25. 11. 2026, položka 1): prepínač má prednosť pred premennou BMB_HOSTING
    hosting = a.hosting or os.environ.get('BMB_HOSTING', '').strip().lower() or 'pages'
    if hosting not in HOSTINGY:
        print(f'CHYBA: BMB_HOSTING={hosting!r} nepoznám, povolené: {", ".join(HOSTINGY)}', file=sys.stderr)
        return 2
    if hosting == 'apache' and nahlad:
        print('CHYBA: hosting apache je len pre ostrý web na doméne (bez --nahlad; v GitHub Actions len s BMB_OSTRY=ano)',
              file=sys.stderr)
        return 2
    vystup = a.vystup.resolve()
    obsah = a.obsah.resolve()
    if vystup in (KOREN, obsah, SABLONA) or vystup in obsah.parents or obsah in vystup.parents or vystup == Path(vystup.anchor):
        print(f'CHYBA: výstup {vystup} nesmie byť koreň, obsah ani šablóna', file=sys.stderr)
        return 2
    manifest = None
    if hosting == 'apache':
        manifest, chyba = manifest_ftp(vystup)
        if chyba:
            print('CHYBA: ' + chyba, file=sys.stderr)
            return 2
    try:
        web = Web(obsah, nahlad, fotky_web=a.fotky_web.resolve(), hosting=hosting)
        web.postav()
        skontroluj_text(web.subory)
        # fb-ostro A4 (OPONENT.md S2): v Actions bez fotky sekcie alebo realizácie slidera nič nezapísať a skončiť kódom 1;
        # krok nasadenia sa nespustí a na webe ostane posledná dobrá verzia
        if v_actions and PRISNE_VAROVANIA:
            raise ChybaZostavy('v GitHub Actions chýba fotka sekcie alebo realizácia slidera z nastavenia.json (nič som '
                               'nezapísal, nasadenie sa nespustí a ostane posledný dobrý web): '
                               + '; '.join(PRISNE_VAROVANIA[:10]))
        commit = commit_repozitara() if hosting == 'apache' else ''
        kodovanych = zapis(web, vystup, manifest=manifest,
                           navyse={'commit': commit or 'neznámy'} if hosting == 'apache' else None)
    except ChybaZostavy as e:
        print('CHYBA: ' + str(e), file=sys.stderr)
        return 1
    stranky = sum(1 for k in web.subory if k.endswith('.html'))
    druh = ('náhľad' if nahlad else 'ostrý') + (', hosting apache' if hosting == 'apache' else '')
    print(f'hotovo: {vystup} ({druh}), {stranky} HTML, {len(web.realizacie)} realizácií, '
          f'{len(web.obrazky)} obrázkov (nových {kodovanych}), varovaní {len(VAROVANIA)}')
    if manifest is not None:
        print(f'zoznam zostavy: {manifest} (na FTP nepatrí; nahrať celý priečinok {vystup} vrátane .htaccess)')
        # commit zostavy pre záznam spustenia: musí byť commit schváleného náhľadu (SPUSTENIE-FTP.md časť 1)
        print(f'commit: {commit or "neznámy (products/bmbuilding bez .git alebo s nečitateľným HEAD)"}')
    # súbory fotiek: hotové z fotky-web/ (kópia) a zakódované z obsah/ (nová alebo zmenená fotka, AVIF len s kodekom)
    print(f'fotky: z fotky-web {web.pocty_fotiek["fotky-web"]} súborov, z obsahu {web.pocty_fotiek["obsah"]} súborov, '
          f'AVIF {"áno" if web.avif else "nie (len WebP)"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
