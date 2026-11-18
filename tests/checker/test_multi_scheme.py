"""The checker with several schemes: per-scheme settings, preview schemes, checks turned off for
missing fields, list-only records left out of photo checks, a trial data folder, and several
checkers at once. Run: python -m unittest discover -s tests/checker"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from checker import output  # noqa: E402
from checker import settings as scheme_settings  # noqa: E402
from checker.__main__ import main, run  # noqa: E402
from checker.common import clean_record, load_config, make_case  # noqa: E402
from checker.lock import Busy, CheckerLock  # noqa: E402


def quiet(*_):
    pass


def photo(root, data_dir, text):
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    path = Path(data_dir) / "photos" / sha[:2] / f"{sha}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"url": None, "sha256": sha, "bytes": len(data), "file": path.relative_to(root).as_posix(),
            "uploaded_at": "2026-05-01T10:00:00", "stage": "completed", "lat": 15.5, "lon": 73.8}


def demo_record(rid, photos, state="fetched", gp="101"):
    return {"scheme": "demo", "record_id": rid, "state": "Goa", "district": "North", "village": "Alpha GP",
            "village_code": gp, "lat": 15.5, "lon": 73.8, "stage": "completed", "amount": 1000.0,
            "amount_kind": "approved", "group_key": gp, "group_kind": "gp", "photos": photos,
            "photos_state": state, "area_key": f"demo|LGD|30/1/{gp}", "extra": {"x": [1, 2]},
            "first_seen": fixture.SEEN, "last_seen": fixture.SEEN}


def write_demo(root, data_dir=None):
    data_dir = Path(data_dir or Path(root) / "data")
    shared = photo(root, data_dir, "demo-shared")
    listed = photo(root, data_dir, "demo-listed-only")
    recs = [demo_record("D1", [shared]), demo_record("D2", [shared]),
            # Saved by a list-only run: its photo entries are not trusted yet.
            demo_record("D3", [listed], "not_fetched"), demo_record("D4", [listed], "not_fetched")]
    (data_dir / "records").mkdir(parents=True, exist_ok=True)
    (data_dir / "records" / "demo.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    cov = [{"state": "Goa", "district": "North", "block": None, "village": "Alpha GP", "village_code": "101",
            "records": 4, "collected_at": fixture.SEEN, "key": "demo|LGD|30/1/101", "photos_state": "not_fetched",
            "levels": [{"level": "gp", "code": "101", "name": "Alpha GP"}], "code_system": "LGD"}]
    (data_dir / "records" / "demo.coverage.json").write_text(json.dumps(cov), encoding="utf-8")
    return shared["sha256"], listed["sha256"]


class MultiScheme(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture.build(self.root)
        self.shared, self.listed = write_demo(self.root)
        self.settings_dir = self.root / "scheme-settings"
        self.settings_dir.mkdir()
        shutil.copy(ROOT / "checker" / "schemes" / "jsjb.json", self.settings_dir / "jsjb.json")
        self.demo_settings({"name": "Demo", "checks": {"same_gps": False}})
        self.old_dir = scheme_settings.SCHEMES_DIR
        scheme_settings.SCHEMES_DIR = self.settings_dir

    def tearDown(self):
        scheme_settings.SCHEMES_DIR = self.old_dir
        sys.modules.pop("checker.extra.demo_extra", None)
        self.tmp.cleanup()

    def demo_settings(self, data):
        (self.settings_dir / "demo.json").write_text(json.dumps(data), encoding="utf-8")

    def load(self, *parts):
        return json.loads((self.root.joinpath(*parts)).read_text(encoding="utf-8"))

    def site_data(self):
        from checker import output
        return output.read_site_data(self.root / "site")

    def test_preview_scheme_stays_out_of_the_site(self):
        run(self.root, log=quiet)
        site = self.site_data()
        self.assertEqual([s["id"] for s in site["schemes"]], ["jsjb"])
        self.assertEqual({c["scheme"] for c in site["cases"]}, {"jsjb"})
        self.assertEqual({c["scheme"] for c in site["coverage"]}, {"jsjb"})
        self.assertEqual(self.load("site", "data", "areas.json")["states"].keys(), {"Punjab", "Haryana"})
        self.assertEqual(site["schemes"][0]["checks_run"], ["photo_reuse", "same_gps", "same_day", "upload_burst"])

        prev = self.load("data", "preview", "cases.demo.json")
        self.assertTrue(prev["preview"])
        self.assertEqual(prev["schemes"][0]["name"], "Demo")
        reuse = [c for c in prev["cases"] if c["kind"] == "photo_reuse"]
        self.assertEqual([sorted(r["record_id"] for r in c["records"]) for c in reuse], [["D1", "D2"]])
        # The list-only records share a photo too, but their photos are not fetched yet.
        self.assertFalse(any({"D3", "D4"} & {r["record_id"] for r in c["records"]} for c in reuse))
        self.assertTrue(reuse[0]["photo"].startswith("data/photos/"))
        self.assertEqual(reuse[0]["offices"], ["101"])
        self.assertEqual(reuse[0]["records"][0]["amount"], 1000.0)
        off = prev["schemes"][0]["checks_off"]
        self.assertIn("turned off", off["same_gps"])
        self.assertIn("structure_type", off["same_day"])
        self.assertEqual(prev["checks_run"], ["photo_reuse", "upload_burst"])
        self.assertEqual(prev["coverage"][0]["villages_photos_not_checked"], 1)
        self.assertFalse((self.root / "site" / "photos" / f"{self.shared}.jpg").exists())

    def test_one_preview_scheme_by_name_leaves_the_site_alone(self):
        run(self.root, scheme="demo", log=quiet)
        self.assertFalse((self.root / "site").exists())
        self.assertTrue((self.root / "data" / "preview" / "cases.demo.json").exists())

    def test_public_scheme_goes_to_the_site(self):
        self.demo_settings({"public": True})
        run(self.root, log=quiet)
        site = self.site_data()
        self.assertEqual([s["id"] for s in site["schemes"]], ["demo", "jsjb"])
        self.assertIn("demo", {c["scheme"] for c in site["cases"]})
        self.assertFalse((self.root / "data" / "preview").exists())

    def test_jsjb_public_from_config_even_without_its_settings_file(self):
        (self.settings_dir / "jsjb.json").unlink()
        run(self.root, log=quiet)
        self.assertEqual([s["id"] for s in self.site_data()["schemes"]], ["jsjb"])

    def test_trial_folder_writes_only_its_preview(self):
        trial = self.root / "data" / "_trial" / "demo"
        write_demo(self.root, trial)
        shutil.rmtree(self.root / "data" / "records")
        run(self.root, data_dir=trial, log=quiet)
        self.assertFalse((self.root / "site").exists())
        self.assertTrue((trial / "preview" / "cases.demo.json").exists())
        self.assertFalse((self.root / "data" / "preview").exists())
        self.assertEqual(main(["--root", str(self.root), "--data-dir", str(trial)]), 0)
        self.assertFalse((self.root / "site").exists())

    def test_extra_check_runs_only_where_listed_and_when_its_fields_exist(self):
        mod = types.ModuleType("checker.extra.demo_extra")
        mod.KIND, mod.NEEDS = "big_amount", ("amount",)
        mod.find = lambda scheme, records, cfg: [make_case(scheme, "big_amount", ["x"], "Big", records[:2], ["r"], cfg)]
        sys.modules["checker.extra.demo_extra"] = mod
        self.demo_settings({"extra_checks": ["demo_extra", "not_there"]})
        run(self.root, log=quiet)
        prev = self.load("data", "preview", "cases.demo.json")
        self.assertIn("big_amount", prev["checks_run"])
        self.assertEqual(len([c for c in prev["cases"] if c["kind"] == "big_amount"]), 1)
        site = self.site_data()
        self.assertNotIn("big_amount", site["checks_run"])
        mod.NEEDS = ("beneficiary_ref",)
        run(self.root, log=quiet)
        prev = self.load("data", "preview", "cases.demo.json")
        self.assertIn("beneficiary_ref", prev["schemes"][0]["checks_off"]["big_amount"])

    def test_settings_override_thresholds_for_one_scheme(self):
        cfg = load_config()
        s = scheme_settings.load("demo", cfg, folder=self.settings_dir)
        self.assertFalse(s["public"])
        merged = scheme_settings.scheme_config(cfg, {"same_gps": {"min_records": 2}})
        self.assertEqual((merged["same_gps"]["min_records"], cfg["same_gps"]["min_records"]), (2, 5))
        (self.settings_dir / "broken.json").write_text("{nope", encoding="utf-8")
        logs = []
        self.assertFalse(scheme_settings.load("broken", cfg, folder=self.settings_dir, log=logs.append)["public"])
        self.assertTrue(logs)

    def test_new_fields_are_type_checked(self):
        good = demo_record("X", [])
        self.assertEqual(clean_record(good)["amount"], 1000.0)
        for bad in (dict(good, amount="lots"), dict(good, photos_state="maybe"), dict(good, stage=3.5),
                    dict(good, photos=[{"sha256": "a", "lat": "north"}])):
            with self.assertRaises(ValueError):
                clean_record(bad)


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture.build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_second_checker_waits_for_the_first(self):
        lock = CheckerLock(self.root / "data" / "state" / "checker.lock")
        try:
            with self.assertRaises(Busy):
                run(self.root, log=quiet, lock_wait=0.3)
            timer = threading.Timer(0.5, lock.release)
            timer.start()
            run(self.root, log=quiet, lock_wait=10)
            timer.join()
        finally:
            lock.release()
        self.assertTrue((self.root / "site" / "data" / "summary.json").exists())

    def test_checkers_started_together_leave_whole_files(self):
        procs = [subprocess.Popen([sys.executable, "-m", "checker", "--root", str(self.root)], cwd=str(ROOT),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(3)]
        errors = [p.communicate(timeout=120)[1] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0, 0], errors)
        out = self.root / "site" / "data"
        from checker import output
        self.assertEqual(len(output.read_site_data(out.parent)["cases"]), 5)
        json.loads((out / "areas.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(os.listdir(out)), ["areas.json", "cases", "index", "summary.json"])
        case_files = sorted(os.listdir(out / "cases"))
        self.assertEqual(len(case_files), 5)
        for f in case_files:
            self.assertTrue(f.endswith(".json"), f)
            json.loads((out / "cases" / f).read_text(encoding="utf-8"))
        self.assertFalse([f for f in os.listdir(self.root / "site" / "photos") if not f.endswith(".jpg")])

    def test_failed_write_keeps_the_old_file(self):
        path = self.root / "out.json"
        output.write_json(path, {"v": 1})
        real = output.json.dump
        output.json.dump = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "disk full"))
        try:
            with self.assertRaises(OSError):
                output.write_json(path, {"v": 2})
        finally:
            output.json.dump = real
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 1})
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), ["out.json"])


if __name__ == "__main__":
    unittest.main()


class SiteDataFiles(unittest.TestCase):
    """summary.json, one index file per state and one file per case, from output.write_site_data."""

    def case(self, i, state, kind="photo_reuse", severity=1, records=4, block="B", villages=("V",)):
        return {"id": f"jsjb-{kind}-{i:012x}", "scheme": "jsjb", "kind": kind, "state": state, "district": "D",
                "block": block, "villages": list(villages), "severity": severity, "record_count": records,
                "reasons": [f"reason {i}"], "source_url": "https://portal.example/", "photo": None,
                "records": [{"record_id": str(i * 10 + n), "village": villages[0]} for n in range(records)]}

    def write(self, site, cases):
        from checker import output
        output.write_site_data(site, cases, [{"id": "jsjb"}], [{"state": "Punjab"}], ["photo_reuse"],
                               generated_at="2026-11-19T00:00:00+00:00")
        return json.loads((site / "data" / "summary.json").read_text(encoding="utf-8"))

    def test_summary_index_and_case_files(self):
        from checker import output
        first = dict(self.case(1, "Punjab", severity=5, villages=("Mohali", "Kheri")), other_ids=["jsjb-photo_reuse-0000000000ff"],
                     photo_files=[{"sha256": "a", "records": 3}, {"sha256": "b", "records": 1}], placeholder={"width": 1, "height": 1},
                     offices=["BDPO"], headline="One picture, 4 different works", needs_human_review=False)
        cases = [first,
                 self.case(2, "Punjab", kind="same_gps", severity=3, block="b", villages=("mohali",)),
                 self.case(3, "Tamil Nadu", severity=4, villages=("Sasnagar",)),
                 self.case(4, "TAMIL NADU", severity=2),
                 self.case(5, "", severity=1, records=2)]
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "data" / "index").mkdir(parents=True)
            (site / "data" / "index" / "gone.json").write_text("{}", encoding="utf-8")
            (site / "data" / "cases.json").write_text("{}", encoding="utf-8")
            s = self.write(site, cases)
            self.assertEqual(s["total"], 5)
            # Mohali counts as Sasnagar, and block names match without case, as on the site.
            self.assertEqual(s["villages_with_case"], 3)
            self.assertEqual(s["kinds"], [{"scheme": "jsjb", "kind": "photo_reuse", "n": 4},
                                          {"scheme": "jsjb", "kind": "same_gps", "n": 1}])
            self.assertEqual([c["id"] for c in s["top"]], [c["id"] for c in sorted(cases, key=lambda c: -c["severity"])])
            self.assertEqual([(e["state"], e["file"], e["cases"]) for e in s["index"]], [
                (None, "data/index/other.json", 1), ("Punjab", "data/index/punjab.json", 2),
                ("TAMIL NADU", "data/index/tamil-nadu.json", 1), ("Tamil Nadu", "data/index/tamil-nadu-2.json", 1)])
            self.assertEqual(sorted(os.listdir(site / "data" / "index")),
                             ["other.json", "punjab.json", "tamil-nadu-2.json", "tamil-nadu.json"])
            self.assertFalse((site / "data" / "cases.json").exists())
            punjab = json.loads((site / "data" / "index" / "punjab.json").read_text(encoding="utf-8"))["cases"]
            self.assertEqual([c["id"] for c in punjab], ["jsjb-photo_reuse-000000000001", "jsjb-same_gps-000000000002"])
            # Everything the cards, filters and merged-case links use stays in the index.
            self.assertEqual(set(punjab[0]), {"id", "scheme", "kind", "state", "district", "block", "villages", "severity",
                                              "record_count", "photo", "records", "other_ids", "photo_files", "placeholder",
                                              "offices", "headline", "needs_human_review"})
            self.assertEqual(punjab[0]["records"], [{"record_id": "10"}, {"record_id": "11"}, {"record_id": "12"}])
            whole = json.loads((site / "data" / "cases" / "jsjb-photo_reuse-000000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(whole["reasons"], ["reason 1"])
            self.assertEqual(len(whole["records"]), 4)
            self.assertEqual(output.read_site_data(site)["cases"], cases)

    def test_top_cases_include_ties_with_the_last(self):
        cases = [self.case(i, "Punjab", severity=9 if i < 3 else 2) for i in range(1, 10)]
        with tempfile.TemporaryDirectory() as tmp:
            s = self.write(Path(tmp), cases)
        # Two cases with severity 9, then seven tied at 2: all seven tie with the fifth.
        self.assertEqual(len(s["top"]), 9)

    def test_unchanged_files_are_not_rewritten(self):
        cases = [self.case(1, "Punjab")]
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            self.write(site, [dict(c) for c in cases])
            files = [site / "data" / "index" / "punjab.json", site / "data" / "cases" / (cases[0]["id"] + ".json")]
            before = [f.stat().st_mtime_ns for f in files]
            time.sleep(0.05)
            self.write(site, [dict(c) for c in cases])
            self.assertEqual([f.stat().st_mtime_ns for f in files], before)


class CaseFiles(unittest.TestCase):
    def test_case_files_follow_the_cases(self):
        from checker import output
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            output.write_case_files(site, [{"id": "a/1", "records": [{"record_id": "1"}]}, {"id": "b", "records": []}])
            folder = site / "data" / "cases"
            self.assertEqual(sorted(os.listdir(folder)), ["a_1.json", "b.json"])
            output.write_case_files(site, [{"id": "b", "records": [{"record_id": "2"}]}])
            self.assertEqual(os.listdir(folder), ["b.json"])
            self.assertEqual(json.loads((folder / "b.json").read_text(encoding="utf-8"))["records"], [{"record_id": "2"}])
