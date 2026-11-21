#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Naplni products/bmbuilding/obsah/ zo stareho webu BM Building a z FB faktov.

Zdroje faktov: ops/weby-na-mieru/klienti/bmbuilding/FAKTY-WEB.md a stary-web/*.md.
Obrazky: D:/ARLing-media/klienti/bmbuilding/stary-web-obrazky/ (podla stary-web/zoznam.json).
Idempotentne: pri kazdom behu prepise realizacie/*.json, obrazky/<slug>/ , firma.json a logo.png.
Spustenie: python -B -X utf8 products/bmbuilding/nastroje/naplnenie_zo_stareho_webu.py
"""
import json
import os
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:
    from PIL import ImageCms
except Exception:  # pragma: no cover
    ImageCms = None

KOREN = Path(__file__).resolve().parents[3]
STARY = KOREN / "ops/weby-na-mieru/klienti/bmbuilding/stary-web"
OBR_D = Path("D:/ARLing-media/klienti/bmbuilding/stary-web-obrazky")
OBSAH = KOREN / "products/bmbuilding/obsah"
SUPIS = KOREN / "ops/weby-na-mieru/klienti/bmbuilding/obsah-supis.md"

# slug -> (zoznam fotiek ako casti src cesty zo zoznam.json, v poradi na webe; alt popis)
F = "/images/fotografie-stavieb/"


def _num(folder, nums, ext="jpg"):
    return [f"{F}{folder}/{n}.{ext}" for n in nums]


REALIZACIE = [
    # FB a Okanikova
    dict(slug="dvojdom-s-bratislava", nazov="Dvojdom S", miesto="Bratislava",
         rola="Projektový manažment a stavebný dozor",
         projektant="Ing. arch. Pavel Kosnáč, Kosi Architects", roky="2021 až 2022",
         text="Dvojdom S v Bratislave. BM Building tu zabezpečoval projektový manažment a stavebný dozor. Realizácia prebehla v rokoch 2021 až 2022.",
         zdroj="fb", datum="2026-10-04", fotky=[]),
    dict(slug="rd-ahoj-bratislava-nove-mesto", nazov="RD Ahoj", miesto="Bratislava, Nové Mesto",
         rola="Stavebný dozor", projektant="Ing. arch. Ilja Skoček", roky="",
         text="Rodinný dom RD Ahoj v Bratislave, v mestskej časti Nové Mesto. BM Building tu vykonával stavebný dozor.",
         zdroj="fb", datum="2026-08-25", fotky=[]),
    dict(slug="ufo-most-snp-bratislava", nazov="Reštaurácia UFO, nástupný priestor na Moste SNP",
         miesto="Bratislava, Most SNP", rola="", projektant="", roky="2025",
         text="Stavebné úpravy nástupného priestoru do reštaurácie UFO na Moste SNP v Bratislave. Kolaudácia v novembri 2025.",
         zdroj="fb", datum="2025-11-15", fotky=[]),
    dict(slug="rekonstrukcia-okanikova-bratislava",
         nazov="Rekonštrukcia objektu, nadstavba a prístavba, Okánikova ulica",
         miesto="Bratislava, Okánikova ulica", rola="Projektový manažér a stavebný dozor",
         projektant="Ing. arch. Martin Rezník, Ing. arch. Matúš Polák, Ing. arch. Silvia Kračúnová, architekti.sk",
         roky="2018 až 2021",
         text="Rekonštrukcia objektu s nadstavbou a prístavbou na Okánikovej ulici v Bratislave. BM Building tu pôsobil ako projektový manažér a stavebný dozor. Realizácia prebehla v rokoch 2018 až 2021.",
         investor="súkromný investor", zdroj="web", datum="2018-12-01",
         fotky=_num("RO_Okanikova", ["01", "02", "03"]),
         alt_zaklad="Okánikova ulica, Bratislava, pôvodný stav objektu pred rekonštrukciou"),
    # web, najnovsie hore
    dict(slug="bytove-domy-vtacnik-bratislava", nazov="Bytové domy Vtáčnik, Tupého ulica",
         miesto="Bratislava, Tupého ulica", rola="Projektový manažér",
         projektant="Koneval / Manikova architekti", roky="2016 až 2018",
         text="Bytové domy sú navrhnuté ako mestské vilové domy. Celkovo bude sedem bytových domov, ktoré sú voči sebe posunuté tak, aby si neprekážali vo výhľade na mesto. V jednom bytovom dome je 14 bytových jednotiek v tejto skladbe: 2 x 2-izbové, 6 x 3-izbové, 6 x 4-izbové.",
         investor="BB Finance s.r.o.", zhotovitel="HANT BA", zdroj="web", datum="2013-01-09",
         fotky=_num("BD_Vtacnik_Tupeho_BA", ["00", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13"]),
         alt_zaklad="Bytové domy Vtáčnik, Tupého ulica, Bratislava"),
    dict(slug="bytovy-dom-devinske-luky-bratislava", nazov="Bytový dom Devínske lúky",
         miesto="Bratislava, Devínska Nová Ves, Bystrická ulica", rola="Projektový manažér",
         projektant="Ing. arch. Milan Kiaček", roky="2016 až 2018",
         text="Bytový dom na Bystrickej ulici v Bratislave, v Devínskej Novej Vsi, pozostáva z 32 bytových jednotiek. Dom má 4 nadzemné podlažia.",
         investor="KOBAX reality", zhotovitel="Colorstav - Team, s.r.o.", zdroj="web", datum="2017-07-13",
         fotky=_num("BD_Devinske_luky", ["00", "02", "04", "05", "06"]),
         alt_zaklad="Bytový dom Devínske lúky, Bratislava"),
    dict(slug="bytovy-dom-rosnicka-bratislava", nazov="Bytový dom Rosnička", miesto="Bratislava",
         rola="Stavebný dozor, TDI", projektant="", roky="2015 až 2016",
         text="V bytovom dome Rosnička je navrhovaných 44 bytov, 9 apartmánov a 1 komerčný priestor. Objekt má štyri nadzemné podlažia, piate ustúpené a jedno podzemné podlažie. V suteréne sú technické priestory, samostatne predajné garážové státia a pivničné kobky.",
         investor="BD Rosnička s.r.o.", zhotovitel="HANT BA, a.s.", zdroj="web", datum="2017-07-13",
         fotky=_num("BD-Rosnicka", ["02", "01", "032"]), alt_zaklad="Bytový dom Rosnička, Bratislava"),
    dict(slug="bytovy-dom-na-krizovatkach-bratislava", nazov="Bytový dom, ulica Na Križovatkách",
         miesto="Bratislava, ulica Na Križovatkách", rola="Projektový manažér",
         projektant="Koneval / Manikova architekti", roky="2014 až 2016",
         text="Bytový dom pozostáva z 95 bytov a 2 nebytových priestorov. Objekt má 2 až 6 obytných nadzemných podlaží a polozapustený suterén, v ktorom je parkovanie, pivničné priestory a technické zabezpečenie budovy.",
         investor="IN DEVELOP s.r.o.", zhotovitel="HANT BA, a.s.", zdroj="web", datum="2013-01-09",
         fotky=_num("BD_Na-Krizovatkach", ["03", "02", "04"], "JPG"),
         alt_zaklad="Bytový dom na ulici Na Križovatkách, Bratislava"),
    dict(slug="filmovy-klub-spitalska-bratislava", nazov="Stavebné úpravy Filmového klubu, Špitálska ulica",
         miesto="Bratislava, Špitálska ulica", rola="Stavebný dozor, TDI",
         projektant="A3a, s. r. o., Ing. arch. Peter Kručay a spol.", roky="2013 až 2016",
         text="Stavebné úpravy Filmového klubu na Špitálskej ulici č. 4 v Bratislave. V 1. etape (06/2013 až 11/2013) prebiehala výstavba digitalizačného pracoviska v suterénnych priestoroch Kina Lumière. V 2. etape (09/2014 až 12/2014) prebiehala rekonštrukcia VZT, elektroinštalácie a súvisiace stavebné úpravy. 3. etapa prebehla od 06/2016 do 12/2016.",
         investor="Slovenský filmový ústav",
         zhotovitel="Trading universal services, s.r.o. (1. a 2. etapa), COFELY a.s. (3. etapa)",
         zdroj="web", datum="2013-12-08",
         fotky=["pic-Filmovy-klub-Spitalska-Bratislava.jpg"] + _num("Filmovy_klub", ["01", "02", "03", "04", "05", "06"]),
         alt_zaklad="Filmový klub, Kino Lumière, Špitálska ulica, Bratislava",
         alt_extra={0: "Vstup do Kina Lumière, Filmový klub na Špitálskej ulici, Bratislava"}),
    dict(slug="rekonstrukcia-5-ambulancii-pre-lekarov", nazov="Rekonštrukcia obchodných priestorov na 5 ambulancií",
         miesto="", rola="Stavebný dozor, TDI", projektant="CONCEPT Slovakia s.r.o.", roky="2014",
         text="Rekonštrukcia a prestavba pôvodných obchodných priestorov spoločnosti na 5 ambulancií pre lekárov. Realizácia prebehla od apríla do októbra 2014.",
         investor="SPINNER s.r.o.", zhotovitel="Colorstav - Team, s.r.o.", zdroj="web", datum="2017-07-13",
         fotky=_num("ambulancia", ["01", "02", "03"]), alt_zaklad="Rekonštrukcia obchodných priestorov na 5 ambulancií"),
    dict(slug="rekonstrukcia-venturska-bratislava", nazov="Rekonštrukcia a nadstavba objektu Ventúrska č. 3",
         miesto="Bratislava, Ventúrska ulica", rola="Stavebný dozor, TDI", projektant="", roky="2013 až 2014",
         text="Apartmánový dom na Ventúrskej ulici pozostáva z 10 apartmánov a štúdií. Navrhovaná budova je päťpodlažná a má jedno podzemné podlažie. Podzemné podlažie je určené pre technologické zabezpečenie domu. Ostatné podlažia sú určené pre štúdiá a byty.",
         investor="VENTURIAN, s.r.o.", zhotovitel="TINAMA, s.r.o.", zdroj="web", datum="2013-12-08",
         fotky=["pic-Rekonstrukcia-a-nadstavba-Venturska-Bratislava-2.JPG"] + _num("Venturian", ["01", "02"]),
         alt_zaklad="Rekonštrukcia a nadstavba objektu Ventúrska č. 3, Bratislava"),
    dict(slug="rd-ivanka-pri-dunaji", nazov="RD Ivanka pri Dunaji, prístavba wellness", miesto="Ivanka pri Dunaji",
         rola="Stavebný dozor", projektant="PRO STYLE, s.r.o.", roky="2013 až 2014",
         text="Prístavba wellness k rodinnému domu v Ivanke pri Dunaji.",
         investor="súkromný investor", zhotovitel="Korektstav s.r.o.", zdroj="web", datum="2017-07-13",
         fotky=_num("RD_Ivanka_pri_Dunaji", ["01", "02", "03"]), alt_zaklad="RD Ivanka pri Dunaji, prístavba wellness"),
    dict(slug="rd-jarovce-bratislava", nazov="RD Jarovce", miesto="Bratislava, Jarovce", rola="Stavebný dozor",
         projektant="", roky="2013 až 2014",
         text="Rodinný dom v Jarovciach. Stavebný dozor vykonával Ing. Mário Bulko, BM Building.",
         investor="súkromný investor", zdroj="web", datum="2014-10-22",
         fotky=["pic-rd-jarovce.jpg"], alt_zaklad="RD Jarovce, Bratislava"),
    dict(slug="dom-betliarska-bratislava-petrzalka", nazov="Dom Betliarska", miesto="Bratislava, Petržalka, Betliarska ulica",
         rola="Projektový manažment", projektant="CONCEPT Slovakia s.r.o.", roky="2012 až 2013",
         text="Dom na Betliarskej ulici pozostáva z 18 bytov a 35 apartmánov a štúdií.",
         investor="BETLIARSKA s.r.o.", zhotovitel="Tinama spol. s r.o.", zdroj="web", datum="2013-01-09",
         fotky=["pic-Dom-Betliarska-Bratislava-Petrzalka11.jpg"] + _num("BD_Betliarska_BA", ["01", "03"]),
         alt_zaklad="Dom Betliarska, Bratislava, Petržalka"),
    dict(slug="rd-pezinok", nazov="RD Pezinok", miesto="Pezinok", rola="Stavebný dozor",
         projektant="Koneval / Manikova architekti", roky="2011 až 2012",
         text="Stavba rodinného domu v Pezinku.", investor="súkromný investor",
         zhotovitel="TATRASOD spol. s.r.o.", zdroj="web", datum="2013-01-09",
         fotky=["pic-RD-Gogolova-ul-Pezinok3.jpg"], alt_zaklad="RD Pezinok"),
    dict(slug="rd-kalvaria-bratislava", nazov="RD Kalvária", miesto="Bratislava", rola="Stavebný dozor",
         projektant="CONCEPT Slovakia s.r.o.", roky="2009",
         text="Rekonštrukcia a prestavba rodinného domu na mestskú vilu.", investor="súkromný investor",
         zhotovitel="Colorstav - Team, s.r.o.", zdroj="web", datum="2013-01-09",
         fotky=["pic-RD-Kalvaria-Bratislava3.jpg"], alt_zaklad="RD Kalvária, Bratislava"),
    dict(slug="polyfunkcny-objekt-na-krizovatkach-bratislava", nazov="Polyfunkčný objekt, ulica Na Križovatkách",
         miesto="Bratislava, ulica Na Križovatkách", rola="Stavebný manažment", projektant="CONCEPT Slovakia s.r.o.",
         roky="2008 až 2009", text="Polyfunkčný dom pozostáva z 32 bytov, 5 obchodných priestorov a reštaurácie.",
         investor="Vazex Reality s.r.o.", zhotovitel="HANT BA, a.s.", zdroj="web", datum="2013-01-09", fotky=[]),
    dict(slug="rezidencia-grunty-bratislava", nazov="Rezidencia Grunty", miesto="Bratislava, Karlova Ves",
         rola="Projektový manažment", projektant="Ing. arch. Dušan Dinaj, autorizovaný architekt", roky="2007 až 2009",
         text="Rezidenčná štvrť pozostáva z 9 objektov, v ktorých je 105 bytov a 6 obchodných priestorov. Projektový manažment zabezpečovali Vazex, s.r.o. a BM Building s.r.o.",
         investor="Realitná spoločnosť SLSP a.s.", zhotovitel="HANT BA, a.s.", zdroj="web", datum="2013-01-09",
         fotky=_num("Rezidencia_Grunty", ["01", "02", "03"]), alt_zaklad="Rezidencia Grunty, Bratislava, Karlova Ves"),
    dict(slug="polyfunkcny-objekt-budatinska-bratislava", nazov="Polyfunkčný objekt, Budatínska ulica",
         miesto="Bratislava, Budatínska ulica", rola="Stavebný manažment", projektant="Bajoprojekt s.r.o.",
         roky="2004 až 2005", text="Polyfunkčný dom pozostáva zo 166 bytov, 6 obchodných priestorov a 2 reštaurácií.",
         investor="Vazex s.r.o.", zhotovitel="SKANSKA, a.s.", zdroj="web", datum="2013-01-09", fotky=[]),
    dict(slug="bytovy-dom-haanova-bratislava", nazov="Bytový dom, Haanova ulica", miesto="Bratislava, Haanova ulica",
         rola="Projektový manažment", projektant="Kappa 1 s.r.o.", roky="2003 až 2004",
         text="Bytový dom pavlačového typu pozostáva z 80 bytov a 2 obchodných priestorov.",
         investor="Vazex, s.r.o.", zhotovitel="TINAMA, Ing. Čahoj", zdroj="web", datum="2013-01-09", fotky=[]),
]


def uprav_obrazok(zdroj, ciel):
    im = Image.open(zdroj)
    im = ImageOps.exif_transpose(im)
    icc = im.info.get("icc_profile")
    if icc and ImageCms is not None:
        try:
            import io
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im.convert("RGB"), src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            pass
    im = im.convert("RGB")
    w, h = im.size
    d = max(w, h)
    if d > 2000:
        k = 2000 / d
        im = im.resize((round(w * k), round(h * k)), Image.LANCZOS)
    ciel.parent.mkdir(parents=True, exist_ok=True)
    im.save(ciel, "JPEG", quality=86, progressive=True, optimize=True)
    return im.size


def main():
    if not OBR_D.exists():
        sys.exit(f"Chyba priecinok s obrazkami: {OBR_D}")
    zoznam = json.loads((STARY / "zoznam.json").read_text(encoding="utf8"))
    mapa = {}  # src cesta -> lokalny subor
    for e in zoznam:
        for o in e["obrazky"]:
            cesta = o["src"].split("bmbuilding.sk", 1)[1]
            mapa[cesta] = OBR_D / Path(o["subor"]).name

    OBSAH.mkdir(parents=True, exist_ok=True)
    rdir = OBSAH / "realizacie"
    if rdir.exists():
        for f in rdir.glob("*.json"):
            f.unlink()
    rdir.mkdir(parents=True, exist_ok=True)
    pouzite = set()
    supis = []
    slugy = set()
    for i, r in enumerate(REALIZACIE, 1):
        slug = r["slug"]
        assert slug not in slugy, slug
        slugy.add(slug)
        odir = OBSAH / "obrazky" / slug
        if odir.exists():
            shutil.rmtree(odir)
        zdroje = []
        for c in r["fotky"]:
            cesta = c if c.startswith("/") else F + c
            assert cesta in mapa, cesta
            assert mapa[cesta].exists(), mapa[cesta]
            zdroje.append(mapa[cesta])
        n = len(zdroje)
        fotky = []
        for j, z in enumerate(zdroje):
            uprav_obrazok(z, odir / f"{j + 1:02d}.jpg")
            pouzite.add(z.name)
            alt = r.get("alt_extra", {}).get(j) or f"{r['alt_zaklad']}, fotka {j + 1} z {n}"
            fotky.append({"subor": f"obsah/obrazky/{slug}/{j + 1:02d}.jpg", "alt": alt})
        out = {
            "id": slug, "slug": slug, "nazov": r["nazov"], "miesto": r["miesto"], "rola": r["rola"],
            "projektant": r["projektant"], "roky": r["roky"], "text": r["text"],
            "investor": r.get("investor", ""), "zhotovitel": r.get("zhotovitel", ""),
            "fotky": fotky, "zdroj": r["zdroj"], "fb_url": None, "datum": r["datum"],
            "poradie": i, "zverejnene": True,
        }
        (rdir / f"{slug}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        supis.append((r["nazov"], slug, n, r["zdroj"]))

    shutil.copyfile(OBR_D / "7f10ecaeb3-250-logo-bm-building.png", OBSAH / "logo.png")

    firma = {
        "meno": "BM Building, s.r.o.",
        "konatel": "Ing. Mário Bulko",
        "adresa": {"ulica": "Podhorská 9", "psc": "902 01", "mesto": "Pezinok"},
        "ico": "35 947 519",
        "ic_dph": "SK2022036929",
        "zapis_or": "Spoločnosť zapísaná v obchodnom registri Okresného súdu Bratislava I, oddiel: Sro, vložka číslo: 36965/B",
        "zapis_or_overit": True,
        "telefon": "+421 903 794 738",
        "telefon_href": "+421903794738",
        "email": "info@bmbuilding.sk",
        "facebook": "https://www.facebook.com/BmBuilding",
        "posobnost": "Bratislava a okolie",
        "sluzby": [
            {"slug": "stavebny-dozor", "nazov": "Stavebný dozor",
             "kratky_popis": "Kontrolujeme dodržiavanie podmienok stavebného povolenia, postup prác podľa časového plánu výstavby a zmlúv, kvalitu materiálov a odstraňovanie vád. Zúčastníme sa aj na kolaudačnom konaní.",
             "cinnosti": [
                 "Oboznámenie sa s podkladmi (projekt, zmluvy, stavebné povolenia, územné rozhodnutie)",
                 "Odovzdanie staveniska a zabezpečenie zápisu o tom do stavebného denníka",
                 "Protokolárne odovzdanie základného smerového a výškového vytýčenia stavby zhotoviteľovi",
                 "Kontrola dodržiavania podmienok vydaných na uskutočnenie stavby, najmä stavebného povolenia a územného rozhodnutia",
                 "Odsúhlasovanie dodatkov a zmien projektu",
                 "Bezodkladné informovanie investora o všetkých závažných okolnostiach",
                 "Kontrola vecnej a cenovej správnosti a úplnosti oceňovaných podkladov a platobných dokladov",
                 "Kontrola vhodnosti stavebných materiálov a prevádzky technického vybavenia na stavbe",
                 "Kontrola odborného ukladania stavebných materiálov a stavebných hmôt",
                 "Kontrola tých častí diela, ktoré boli v ďalšom postupe zakryté alebo sa stali neprístupnými, a zapísanie výsledkov kontroly do stavebného denníka",
                 "Preberanie vykonaných stavebných prác zo strany dodávateľa",
                 "Spolupráca s projektantom",
                 "Sledovanie, či zhotoviteľ vykonáva predpísané a dohodnuté skúšky materiálov, konštrukcií a prác",
                 "Sledovanie vedenia stavebného denníka",
                 "Uplatňovanie námetov smerujúcich k zhospodárneniu výstavby",
                 "Kontrola postupu prác podľa časového plánu výstavby a zmlúv",
                 "Kontrola odstránenia vád a nedorobkov",
                 "Kontrola dodržiavania BOZP",
                 "Účasť na kolaudačnom konaní"]},
            {"slug": "stavbyveduci", "nazov": "Stavbyvedúci",
             "kratky_popis": "Podľa stavebného zákona riadime a koordinujeme stavebné práce a iné činnosti na stavenisku a na stavbe a vedieme o nich evidenciu v stavebnom denníku.",
             "cinnosti": [
                 "Riadenie a koordinácia stavebných prác a iných činností na stavenisku a na stavbe a vedenie evidencie o nich v stavebnom denníku",
                 "Určovanie začatia a ukončenia jednotlivých činností na stavenisku a na stavbe",
                 "Riadenie vykonávania stavebných prác a iných činností na stavenisku a na stavbe",
                 "Organizovanie pohybu osôb na stavenisku a na stavbe",
                 "Kontrola výrobkov a materiálov pre stavbu, zisťovanie ich kvality a vhodnosti na zabudovanie do stavby, určovanie spôsobu ich skladovania na stavenisku",
                 "Dávanie príkazov na okamžité zastavenie stavebných prác a iných činností na stavenisku a na stavbe, ak sa vyskytla prekážka, pre ktorú je ich ďalšie vykonávanie neprípustné",
                 "Koordinácia poradia stavebných prác",
                 "Vykázanie cudzích osôb zo staveniska a zo stavby"]},
            {"slug": "stavebny-manazment", "nazov": "Stavebný manažment",
             "kratky_popis": "Sme stavebno-manažérska spoločnosť, ktorá poskytuje podporné služby investorom a developerom pri realizácii ich projektov. Zabezpečíme bezproblémový prechod z projektovej do realizačnej fázy.",
             "cinnosti": [
                 "Koordinácia všetkých profesií, riadenie a kontrola stavby počas prípravy, realizácie až po kolaudáciu",
                 "Odhaľovanie a obmedzovanie rizík a využitie poskytnutých príležitostí",
                 "Plnenie cieľov investora alebo užívateľa v požadovanej kvalite, v určenom termíne a za vopred určené náklady",
                 "Riadenie a koordinácia procesu zadania stavebných prác za klienta",
                 "Zabezpečenie uzatvorenia zmlúv s dodávateľom stavby vrátane koordinácie ich právnej kontroly"]},
        ],
        "opravnenia": [
            {"organizacia": "Slovenská komora stavebných inžinierov (SKSI)",
             "nazov": "Osvedčenie o vykonaní odbornej skúšky", "osoba": "Ing. Mário Bulko",
             "cinnost": "stavbyvedúci", "odborne_zameranie": "pozemné stavby",
             "evidencne_cislo": "00912*10*", "datum_vydania": "2003-11-05", "datum_vydania_text": "5. 11. 2003"},
            {"organizacia": "Slovenská komora stavebných inžinierov (SKSI)",
             "nazov": "Osvedčenie o vykonaní skúšky odbornej spôsobilosti", "osoba": "Ing. Mário Bulko",
             "cinnost": "stavebný dozor", "odborne_zameranie": "",
             "evidencne_cislo": "09903", "datum_vydania": "2010-06-10", "datum_vydania_text": "10. 6. 2010"},
        ],
        "od_roku": 2003,
        "od_roku_zdroj": "Osvedčenie SKSI pre stavbyvedúceho vydané 5. 11. 2003; najstaršia referencia na starom webe je Bytový dom Haanova ulica, výstavba 2003 až 2004.",
        "hodiny": None,
    }
    (OBSAH / "firma.json").write_text(json.dumps(firma, ensure_ascii=False, indent=2) + "\n", encoding="utf8")

    # supis
    nepouzite = sorted(p.name for p in OBR_D.iterdir() if p.is_file() and p.name not in pouzite)
    riadky = ["# Súpis obsahu BM Building (generované skriptom naplnenie_zo_stareho_webu.py)", "",
              "| # | Realizácia | Slug | Fotiek | Zdroj |", "|---|---|---|---|---|"]
    for i, (n, s, c, z) in enumerate(supis, 1):
        riadky.append(f"| {i} | {n} | {s} | {c} | {z} |")
    riadky += ["", "## Vynechané súbory", ""]
    for n in nepouzite:
        dovod = "ikona alebo logo, nie fotka stavby" if any(k in n for k in ("arrow", "Button", "Social", "logo")) else "nepriradené"
        riadky.append(f"- {n}: {dovod}")
    riadky += ["", "- Obrázky osvedčení SKSI (priečinok opravnenia na D:) sú na web zakázané (rodné číslo), nepoužité.",
               "- Priradenie fotiek je podľa priečinka zdroja na starom webe (napr. BD-Rosnicka), pri 4 stavbách (RD Ivanka, ambulancie, Betliarska, Rezidencia Grunty) len zo stránky zoznamu.",
               "- Miniatúry pic-*.jpg: RD Jarovce, RD Kalvária, RD Pezinok (názov súboru Gogolova) a Dom Betliarska, Filmový klub, Ventúrska (titulná fotka).",
               "- Úvodné titulky: Vtáčnik 00 a Devínske lúky 00 prvé; Na Križovatkách 03 a Rosnička 02 posunuté na prvé miesto podľa názvov súborov v úvodnej galérii; pri Ventúrskej a Filmovom klube je prvá miniatúra pic-*.",
               "- Okánikova: fotky zo starého webu ukazujú pôvodný stav, alt to uvádza.",
               "- Polyfunkčný objekt Budatínska, Polyfunkčný objekt Na Križovatkách a Bytový dom Haanova nemajú na starom webe fotky.",
               "- Ambulancie: miesto na starom webe nie je uvedené, pole miesto je prázdne.",
               "- RD Pezinok: ulica Gogolova je iba z názvu súboru miniatúry, do miesta nedaná.",
               ""]
    SUPIS.write_text("\n".join(riadky), encoding="utf8")
    print(f"Hotovo: {len(supis)} realizacii, {sum(c for _, _, c, _ in supis)} fotiek.")


if __name__ == "__main__":
    main()
