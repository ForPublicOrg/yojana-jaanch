import json, re, shutil, subprocess, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).parent / 'sample-data'
CASE_KEYS = {'id', 'scheme', 'kind', 'headline', 'state', 'district', 'block', 'villages', 'record_count',
             'reasons', 'photo', 'offices', 'records', 'source_url', 'severity', 'needs_human_review'}


class SiteTests(unittest.TestCase):
    def test_sample_follows_contract(self):
        cases = json.loads((SAMPLE / 'cases.json').read_text(encoding='utf-8'))
        self.assertEqual(set(cases), {'generated_at', 'schemes', 'coverage', 'cases'})
        for c in cases['cases']:
            self.assertTrue(CASE_KEYS <= set(c), CASE_KEYS - set(c))
            self.assertLessEqual(len(c['records']), c['record_count'])
        areas = json.loads((SAMPLE / 'areas.json').read_text(encoding='utf-8'))
        self.assertIn('states', areas)

    def test_no_em_dash_or_cdn(self):
        for p in ['index.html', 'app.js', 'lib.js', 'style.css']:
            text = (ROOT / 'site' / p).read_text(encoding='utf-8')
            self.assertNotIn('\u2014', text, p)
            self.assertIsNone(re.search(r'(src|href)="https?://[^"]*\.(js|css)', text), p)

    def test_site_name(self):
        cfg = json.loads((ROOT / 'site/config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg['site_name'], 'Yojana Jaanch')
        self.assertEqual(cfg['site_name_hi'], 'योजना जाँच')
        html = (ROOT / 'site/index.html').read_text(encoding='utf-8')
        self.assertIn('<title>Yojana Jaanch</title>', html)
        self.assertIn('lang="hi"', html)
        self.assertNotIn('Scheme photo checker', html + (ROOT / 'site/app.js').read_text(encoding='utf-8'))
        self.assertIn('rel="icon"', html)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_list_logic(self):
        r = subprocess.run(['node', str(Path(__file__).parent / 'test_logic.js')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_geo_files_exist(self):
        idx = json.loads((ROOT / 'site/vendor/geo/districts-index.json').read_text(encoding='utf-8'))
        states = json.loads((ROOT / 'site/vendor/geo/india-states.json').read_text(encoding='utf-8'))
        self.assertIn('Ladakh', states)
        self.assertIn('Punjab', idx)
        for rel in idx.values():
            self.assertTrue((ROOT / 'site/vendor/geo' / rel).is_file(), rel)


if __name__ == '__main__':
    unittest.main()
