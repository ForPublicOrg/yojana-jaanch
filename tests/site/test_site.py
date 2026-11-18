import json, re, shutil, subprocess, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).parent / 'sample-data'
CASE_KEYS = {'id', 'scheme', 'kind', 'headline', 'state', 'district', 'block', 'villages', 'record_count',
             'reasons', 'photo', 'offices', 'records', 'source_url', 'severity', 'needs_human_review'}
INDEX_DROP = {'reasons', 'source_url'}  # only in the whole case file, for the case page


class SiteTests(unittest.TestCase):
    def test_sample_follows_contract(self):
        summary = json.loads((SAMPLE / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(set(summary), {'generated_at', 'checks_run', 'schemes', 'coverage', 'index', 'total',
                                        'villages_with_case', 'kinds', 'top'})
        for entry in summary['index']:
            part = json.loads((SAMPLE / entry['file'].replace('data/', '', 1)).read_text(encoding='utf-8'))
            for c in part['cases']:
                self.assertTrue(CASE_KEYS - INDEX_DROP <= set(c), CASE_KEYS - INDEX_DROP - set(c))
                self.assertFalse(INDEX_DROP & set(c))
                self.assertLessEqual(len(c['records']), 3)
                whole = json.loads((SAMPLE / 'cases' / (c['id'] + '.json')).read_text(encoding='utf-8'))
                self.assertTrue(CASE_KEYS <= set(whole), CASE_KEYS - set(whole))
                self.assertLessEqual(len(whole['records']), whole['record_count'])
        areas = json.loads((SAMPLE / 'areas.json').read_text(encoding='utf-8'))
        self.assertIn('states', areas)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_summary_matches_the_site_count(self):
        folders = [SAMPLE] + ([ROOT / 'site' / 'data'] if (ROOT / 'site' / 'data' / 'summary.json').exists() else [])
        for folder in folders:
            r = subprocess.run(['node', str(Path(__file__).parent / 'check_summary.js'), str(folder)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

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

    def test_photos_send_no_referrer(self):
        js = (ROOT / 'site/app.js').read_text(encoding='utf-8')
        imgs = re.findall(r"<img\b[^>]*", js)
        self.assertTrue(imgs)
        for tag in imgs:
            self.assertIn('referrerpolicy="no-referrer"', tag)
        for link in re.findall(r"<a class=\"photo-frame\"[^>]*", js):
            self.assertIn('rel="noopener noreferrer"', link)

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
