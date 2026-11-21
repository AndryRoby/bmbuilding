"""Kontrola workflows/obsah.yml podľa SPEC.md časť 7 (text; ak je dostupný PyYAML, aj štruktúra)."""
import os
import unittest

CESTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'workflows', 'obsah.yml')


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


if __name__ == '__main__':
    unittest.main()
