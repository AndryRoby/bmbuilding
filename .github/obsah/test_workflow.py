"""Kontrola workflows/obsah.yml podľa SPEC.md časť 7 (text; ak je dostupný PyYAML, aj štruktúra) a brzdy mazania
(fb-ostro A3): skript kroku „Uložiť zmenu obsahu do main“ beží v bash s falošným git, skutočný git sa nespúšťa."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

CESTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'workflows', 'obsah.yml')
ULOZIT = 'Uložiť zmenu obsahu do main'
# výpis súborov odobratých z obsah/, podľa ktorého brzda ráta (OPONENT.md S1; --no-renames: aj premenovaný súbor)
VYPIS_ZMAZANYCH = 'git diff --cached --no-renames --name-only --diff-filter=D -- obsah'
SHA = '0123456789abcdef0123456789abcdef01234567'

# Falošný git pre skript kroku: funkcia v bash má prednosť pred príkazom z PATH, skutočný git sa nespustí.
# FALOSNA_ZMENA=1: v obsah/ je zmena; FALOSNE_ZMAZANE=N: výpis odobratých súborov má N riadkov;
# FALOSNY_DIFF_ZLYHA=1: výpis odobratých skončí chybou (napríklad poškodený repozitár).
FALOSNY_GIT = r'''
git() {
  printf 'git %s\n' "$*" >> "$FALOSNY_GIT_LOG"
  case "$1" in
    add|config|commit|pull|push) return 0 ;;
    rev-parse) echo "$FALOSNE_SHA"; return 0 ;;
    diff)
      if [[ " $* " != *" --cached "* ]]; then
        echo "falošný git: diff bez --cached: $*" >&2
        return 97
      fi
      if [[ " $* " == *" --quiet "* ]]; then
        [ "$FALOSNA_ZMENA" = 0 ]
        return
      fi
      if [[ " $* " == *" --name-only "* && " $* " == *" --diff-filter=D "* && " $* " == *" -- obsah " ]]; then
        if [ "$FALOSNY_DIFF_ZLYHA" = 1 ]; then
          echo "fatal: falošná chyba výpisu" >&2
          return 128
        fi
        i=0
        while [ "$i" -lt "$FALOSNE_ZMAZANE" ]; do
          i=$((i + 1))
          echo "obsah/obrazky/stavba-$i/01.jpg"
        done
        return 0
      fi ;;
  esac
  echo "falošný git: neznámy príkaz: $*" >&2
  return 98
}
'''


def odsadenie(riadok):
    return len(riadok) - len(riadok.lstrip(' '))


def krok_text(text, nazov):
    """Riadky kroku s menom nazov: od „- name:“ po prvý neprázdny riadok s odsadením nanajvýš ako „- name:“
    (ďalší krok, jeho komentár alebo ďalšia úloha). Bez PyYAML."""
    riadky = text.splitlines()
    zaciatok = next(i for i, r in enumerate(riadky) if r.strip() == f'- name: {nazov}')
    hlbka = odsadenie(riadky[zaciatok])
    koniec = zaciatok + 1
    while koniec < len(riadky) and not (riadky[koniec].strip() and odsadenie(riadky[koniec]) <= hlbka):
        koniec += 1
    return riadky[zaciatok:koniec]


def krok_run(text, nazov):
    """Skript „run: |“ kroku nazov tak, ako ho dá YAML: bez spoločného odsadenia, na konci jeden nový riadok."""
    riadky = krok_text(text, nazov)
    i = next(i for i, r in enumerate(riadky) if r.strip() == 'run: |')
    telo = []
    for r in riadky[i + 1:]:
        if r.strip() and odsadenie(r) <= odsadenie(riadky[i]):
            break
        telo.append(r)
    return textwrap.dedent('\n'.join(telo)).strip('\n') + '\n'


def najdi_bash():
    """bash z Git for Windows alebo z Linuxu; nie WSL (System32, WindowsApps), ten má iné cesty k súborom."""
    kandidati = []
    if os.name == 'nt':
        for koren in (os.environ.get('ProgramW6432'), os.environ.get('ProgramFiles'), r'C:\Program Files'):
            if koren:
                kandidati.append(os.path.join(koren, 'Git', 'usr', 'bin', 'bash.exe'))
    kandidati.append(shutil.which('bash'))
    for k in kandidati:
        if k and os.path.isfile(k) and not any(s in k.lower() for s in ('system32', 'windowsapps')):
            return k
    return None


def posix(cesta):
    return cesta.replace('\\', '/')


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        with open(CESTA, encoding='utf-8') as f:
            self.text = f.read()

    def test_bez_tajomstiev_a_tokenov(self):
        self.assertNotIn('secrets.', self.text)
        self.assertNotIn('GITHUB_TOKEN', self.text)

    def test_vstup_z_dispatch_ide_len_cez_premennu_prostredia(self):
        for riadok in self.text.splitlines():
            if '${{' in riadok and 'inputs.' in riadok:
                self.assertTrue(riadok.strip().startswith('POVOL_PRAZDNE:'), riadok)

    def test_kroky_a_poradie(self):
        poradie = ['actions/checkout@', 'actions/setup-python@', '.github/obsah/stiahni.py --api "$OBSAH_API" --obsah obsah',
                   'git push origin HEAD:main', 'python build.py', 'actions/configure-pages@',
                   'actions/upload-pages-artifact@', 'actions/deploy-pages@']
        pozicie = [self.text.index(p) for p in poradie]
        self.assertEqual(pozicie, sorted(pozicie))
        self.assertIn('python-version: "3.12"', self.text)
        self.assertIn('path: web', self.text)
        self.assertIn('vars.OBSAH_API', self.text)

    def test_pillow_pred_build(self):
        # build.py importuje PIL; bez inštalácie na ubuntu-latest padne (INTEGRÁCIA, 21. 11. 2026).
        self.assertIn('pillow==', self.text.lower())
        self.assertLess(self.text.lower().index('pillow=='), self.text.index('python build.py'))

    def test_commit_spat_nespusti_slucku(self):
        riadky = [r for r in self.text.splitlines() if 'git commit' in r]
        self.assertEqual(len(riadky), 1)
        self.assertIn('[skip ci]', riadky[0])

    def test_struktura(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML nie je dostupný')
        d = yaml.safe_load(self.text)
        spustace = d.get('on', d.get(True))
        self.assertEqual(spustace['schedule'], [{'cron': '*/30 * * * *'}])
        self.assertIn('workflow_dispatch', spustace)
        self.assertEqual(spustace['push'], {'branches': ['main']})
        self.assertEqual(d['permissions'], {'contents': 'write', 'pages': 'write', 'id-token': 'write'})
        self.assertEqual(d['concurrency'], {'group': 'obsah', 'cancel-in-progress': False})
        nasadenie = d['jobs']['nasadenie']
        self.assertEqual(nasadenie['needs'], 'obsah')
        self.assertEqual(nasadenie['if'], "github.event_name != 'schedule' || needs.obsah.outputs.zmena == 'ano'")
        self.assertEqual(nasadenie['environment']['name'], 'github-pages')
        # Najmenšie oprávnenia na úlohu: sťahovanie bez Pages a OIDC, nasadenie bez zápisu do repozitára.
        self.assertEqual(d['jobs']['obsah']['permissions'], {'contents': 'write'})
        self.assertEqual(nasadenie['permissions'], {'contents': 'read', 'pages': 'write', 'id-token': 'write'})
        checkout = [k for k in nasadenie['steps'] if str(k.get('uses', '')).startswith('actions/checkout@')][0]
        self.assertIs(checkout['with']['persist-credentials'], False)
        # brzda mazania (fb-ostro A3): skript kroku z YAML je ten istý, ktorý skúša TestBrzdaMazania
        ulozit = [k for k in d['jobs']['obsah']['steps'] if k.get('name') == ULOZIT][0]
        self.assertEqual(ulozit['shell'], 'bash')
        self.assertEqual(ulozit['run'], krok_run(self.text, ULOZIT))


class TestBrzdaMazania(unittest.TestCase):
    """fb-ostro A3 (OPONENT.md S1, POSTUP.md časť 4): krok „Uložiť zmenu obsahu do main“ pri viac ako 20 súboroch
    odobratých z obsah/ skončí chybou bez commitu a pushu, úloha nasadenie sa nespustí. Skript kroku beží ako
    v Actions (bash -eo pipefail) s falošným git v dočasnom priečinku mimo repozitára."""

    @classmethod
    def setUpClass(cls):
        with open(CESTA, encoding='utf-8') as f:
            cls.text = f.read()
        cls.skript = krok_run(cls.text, ULOZIT)

    def test_brzda_v_texte_kroku(self):
        krok = '\n'.join(krok_text(self.text, ULOZIT))
        # shell: bash znamená bash -eo pipefail: chyba výpisu sa neskryje a krok skončí chybou (zlyhanie zatvára)
        self.assertIn('shell: bash', krok)
        poradie = ['git add -A obsah', VYPIS_ZMAZANYCH, '-gt 20 ]', 'exit 1', 'git diff --cached --quiet -- obsah',
                   'git commit', 'git push']
        pozicie = [self.skript.index(p) for p in poradie]
        self.assertEqual(pozicie, sorted(pozicie))
        # git sa volá len menom, inak by skúška s falošným git mohla spustiť skutočný
        for zakazane in ('command git', 'env git', 'exec git', 'xargs git', '/git '):
            self.assertNotIn(zakazane, self.skript)

    def spusti(self, zmazane, zmena=True, diff_zlyha=False):
        bash = najdi_bash()
        if not bash:
            self.skipTest('bash nie je dostupný (Git Bash alebo Linux), brzdu v texte kontroluje test_brzda_v_texte_kroku')
        tmp = tempfile.mkdtemp(prefix='obsah-brzda-')
        self.addCleanup(shutil.rmtree, tmp, True)
        # druhá poistka: keby skript hľadal git v PATH, nájde najprv tento a skončí chybou
        stub = os.path.join(tmp, 'stub')
        os.mkdir(stub)
        with open(os.path.join(stub, 'git'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('#!/bin/sh\necho "skutocny git sa v teste nespusta" >&2\nexit 99\n')
        os.chmod(os.path.join(stub, 'git'), 0o755)
        skript = os.path.join(tmp, 'krok.sh')
        with open(skript, 'w', encoding='utf-8', newline='\n') as f:
            f.write(FALOSNY_GIT + '\n' + self.skript)
        log, vystup = os.path.join(tmp, 'git.log'), os.path.join(tmp, 'github_output')
        for p in (log, vystup):
            open(p, 'w', encoding='utf-8').close()
        env = dict(os.environ)
        env.update({
            'PATH': os.pathsep.join([stub, env.get('PATH', ''), os.path.dirname(bash)]),
            'FALOSNY_GIT_LOG': posix(log), 'GITHUB_OUTPUT': posix(vystup), 'FALOSNE_SHA': SHA,
            'FALOSNE_ZMAZANE': str(zmazane), 'FALOSNA_ZMENA': '1' if zmena else '0',
            'FALOSNY_DIFF_ZLYHA': '1' if diff_zlyha else '0',
            'GIT_DIR': posix(os.path.join(tmp, 'nie-je-repozitar')), 'GIT_CEILING_DIRECTORIES': posix(tmp)})
        p = subprocess.run([bash, '--noprofile', '--norc', '-eo', 'pipefail', posix(skript)], cwd=tmp, env=env,
                           capture_output=True, encoding='utf-8', errors='replace', timeout=120)
        with open(log, encoding='utf-8') as f:
            volania = f.read().splitlines()
        with open(vystup, encoding='utf-8') as f:
            vystupy = f.read().splitlines()
        # falošný git naozaj bežal (skript ho zavolal ako prvý príkaz)
        self.assertEqual(volania[:1], ['git add -A obsah'], p.stderr)
        return p, volania, vystupy

    def test_viac_ako_20_odobratych_zastavi_bez_commitu(self):
        for pocet in (21, 236):  # 236 = suchý beh dnešného stiahni.py (stiahni/suchy-beh-2026-11-23.txt)
            with self.subTest(pocet=pocet):
                p, volania, vystupy = self.spusti(pocet)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertEqual([v for v in volania if v.split()[1] in ('commit', 'pull', 'push', 'config')], [])
                self.assertNotIn('zmena=ano', vystupy)
                self.assertIn(f'::error title=Brzda mazania::Zmena obsahu maže {pocet} súborov v obsah/', p.stdout)
                # zoznam odobratých do logu behu, najviac 50 riadkov
                self.assertIn('obsah/obrazky/stavba-21/01.jpg', p.stdout)
                self.assertNotIn('obsah/obrazky/stavba-51/01.jpg', p.stdout)

    def test_najviac_20_odobratych_ulozi(self):
        for pocet in (0, 1, 17, 20):  # 17 = jedna položka v2 (1 JSON a 16 fotiek, max_fotiek)
            with self.subTest(pocet=pocet):
                p, volania, vystupy = self.spusti(pocet)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn('git commit -m obsah: aktualizácia z arling-obsah [skip ci]', volania)
                self.assertLess(volania.index('git commit -m obsah: aktualizácia z arling-obsah [skip ci]'),
                                volania.index('git push origin HEAD:main'))
                self.assertEqual(vystupy, ['zmena=ano', f'sha={SHA}'])
                self.assertNotIn('Brzda mazania', p.stdout)

    def test_bez_zmeny_bez_commitu(self):
        p, volania, vystupy = self.spusti(0, zmena=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([v for v in volania if v.split()[1] in ('commit', 'pull', 'push')], [])
        self.assertEqual(vystupy, ['zmena=nie', f'sha={SHA}'])

    def test_chyba_vypisu_zastavi_bez_commitu(self):
        # zlyhanie zatvára (AGENTS.md bod 12c): chyba výpisu odobratých sa neskryje za počet 0
        p, volania, vystupy = self.spusti(0, diff_zlyha=True)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual([v for v in volania if v.split()[1] in ('commit', 'pull', 'push')], [])
        self.assertNotIn('zmena=ano', vystupy)


if __name__ == '__main__':
    unittest.main()
