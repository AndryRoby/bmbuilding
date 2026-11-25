"""Skúška .htaccess na hostingu WebHouse bez dopadu na web (SPUSTENIE-FTP.md časť 3, krok 3.5).

Oprava po overení (kontrola spustenia, nález drobné): či hosting v .htaccess dovolí DirectoryIndex a ExpiresByType
(AllowOverride Indexes, https://httpd.apache.org/docs/2.4/mod/mod_dir.html#directoryindex,
https://httpd.apache.org/docs/2.4/mod/mod_expires.html#expiresbytype) a ostatné direktívy (FileInfo: AddType,
AddCharset, AddDefaultCharset, ErrorDocument, RewriteEngine, Header, AddOutputFilterByType), by sa inak ukázalo až pri
prepnutí, a to chybou 500 na celej doméne až do návratu. Nástroj z .htaccess výstupu build.py --hosting apache vyrobí
priečinok s dvoma súbormi:
- .htaccess s tými istými direktívami okrem pravidiel prepisu (RewriteEngine On ostáva a pridá sa jedno neškodné
  pravidlo, ktoré sa nikdy nepoužije), DirectoryIndex skuska.html namiesto index.html index.php,
- skuska.html (text v UTF-8).
Priečinok sa nahrá do www_root vedľa Joomly, otvorí sa https://bmbuilding.sk/skuska-htaccess/ (má ukázať skuska.html,
nie chybu 500) a potom sa zmaže; je to vlastný súbor, starého webu sa nedotkne.

  python nastroje/htaccess_skuska.py --web D:/ARLing-media/klienti/bmbuilding/web-ftp --vystup D:/ARLing-media/klienti/bmbuilding/skuska-htaccess
"""
import argparse
import sys
from pathlib import Path

KOREN = Path(__file__).resolve().parents[1]
MENO = 'skuska-htaccess'
SUBORY = ('.htaccess', 'skuska.html')
HTML = ('<!doctype html>\n<html lang="sk"><head><meta charset="utf-8"><meta name="robots" content="noindex">'
        '<title>Skúška .htaccess</title></head>\n<body><p>Skúška .htaccess: v poriadku. Hosting dovolil všetky direktívy '
        'nového webu okrem pravidiel prepisu. Priečinok skuska-htaccess teraz zmaž.</p></body></html>\n')


def skuska(text):
    """Text .htaccess skúšky z .htaccess zostavy: bez RewriteCond a RewriteRule (pridá jedno neškodné pravidlo za
    RewriteEngine On), DirectoryIndex skuska.html; ostatné direktívy a bloky IfModule bez zmeny."""
    out = ['# Skúška .htaccess (nastroje/htaccess_skuska.py): tie isté direktívy ako .htaccess nového webu okrem',
           '# pravidiel prepisu. Po skúške celý priečinok skuska-htaccess zmazať.']
    engine = index = False
    for riadok in text.splitlines():
        r = riadok.strip()
        if r.startswith('#') or r.startswith(('RewriteCond ', 'RewriteRule ')):
            continue
        if r.startswith('DirectoryIndex '):
            riadok, index = 'DirectoryIndex skuska.html', True
        out.append(riadok)
        if r == 'RewriteEngine On':
            engine = True
            out.append('RewriteRule ^nikdy-nepouzite-pravidlo-skusky$ - [R=404,L]')
    if not (engine and index):
        raise ValueError('.htaccess zostavy nemá RewriteEngine On alebo DirectoryIndex')
    vysledok = []
    for riadok in out:
        if riadok.strip() or (vysledok and vysledok[-1].strip()):
            vysledok.append(riadok)
    return '\n'.join(vysledok).rstrip('\n') + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description='Priečinok skúšky .htaccess pre hosting (bez dopadu na web).')
    ap.add_argument('--web', type=Path, required=True, help='výstup build.py --hosting apache (s .htaccess)')
    ap.add_argument('--vystup', type=Path, required=True, help=f'nový alebo prázdny priečinok, napríklad .../{MENO}')
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    web, vystup = a.web.resolve(), a.vystup.resolve()
    if not (web / '.htaccess').is_file():
        print(f'CHYBA: {web} nemá .htaccess (výstup build.py --hosting apache)', file=sys.stderr)
        return 2
    if vystup.name != MENO:
        print(f'CHYBA: priečinok skúšky sa musí volať {MENO} (adresa https://bmbuilding.sk/{MENO}/)', file=sys.stderr)
        return 2
    if vystup == KOREN or KOREN in vystup.parents or vystup == web or web in vystup.parents:
        print(f'CHYBA: {vystup} patrí mimo repozitára a mimo výstupu webu', file=sys.stderr)
        return 2
    if vystup.exists() and {p.name for p in vystup.iterdir()} - set(SUBORY):
        print(f'CHYBA: {vystup} má iné súbory ako skúšku; zvoľ nový alebo prázdny priečinok', file=sys.stderr)
        return 2
    text = skuska((web / '.htaccess').read_text(encoding='utf-8'))
    # skúška musí prejsť tou istou simuláciou ako .htaccess webu (neznáma direktíva by ju odmietla)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import htaccess_simulacia  # noqa: E402
    htaccess_simulacia.Htaccess(text)
    vystup.mkdir(parents=True, exist_ok=True)
    (vystup / '.htaccess').write_bytes(text.encode('utf-8'))
    (vystup / 'skuska.html').write_bytes(HTML.encode('utf-8'))
    print(f'hotovo: {vystup} (.htaccess {len(text.splitlines())} riadkov, skuska.html); nahrať do /www_root/{MENO}/, '
          f'otvoriť https://bmbuilding.sk/{MENO}/ a potom priečinok zmazať')
    return 0


if __name__ == '__main__':
    sys.exit(main())
