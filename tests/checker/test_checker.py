import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from checker.__main__ import run  # noqa: E402


def quiet(*_):
    pass


def ids(case):
    return sorted(r["record_id"] for r in case["records"])


class FakeVision(BaseHTTPRequestHandler):
    requests = 0
    answered = 0
    throttle_next = True

    def log_message(self, *_):
        pass

    def do_POST(self):
        cls = type(self)
        cls.requests += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if cls.throttle_next:
            cls.throttle_next = False
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        url = body["messages"][0]["content"][1]["image_url"]["url"]
        img = base64.b64decode(url.split(",", 1)[1])
        answer = "no" if b"mismatch" in img else "yes"
        cls.answered += 1
        text = json.dumps({"answer": answer, "seen": "bare field with no structure" if answer == "no" else "a check dam"})
        out = json.dumps({"choices": [{"message": {"content": "```json\n" + text + "\n```"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture.build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, name):
        """cases.json comes back with each case's full record list read from its own file."""
        data_dir = self.root / "site" / "data"
        out = json.loads((data_dir / name).read_text(encoding="utf-8"))
        if name == "cases.json":
            for c in out["cases"]:
                self.assertLessEqual(len(c["records"]), 3)
                full = json.loads((self.root / "site" / c["records_file"]).read_text(encoding="utf-8"))
                self.assertEqual(full["id"], c["id"])
                self.assertEqual([{"record_id": r["record_id"]} for r in full["records"][:3]], c["records"])
                c["records"] = full["records"]
        return out

    def test_cases_without_vision(self):
        run(self.root, log=quiet)
        out = self.load("cases.json")
        cases = out["cases"]
        kinds = sorted(c["kind"] for c in cases)
        self.assertEqual(kinds, ["photo_reuse", "photo_reuse", "same_day", "same_gps", "upload_burst"])
        self.assertTrue(all(c["record_count"] >= 2 and c["record_count"] == len(c["records"]) for c in cases))
        self.assertTrue(all(not c["needs_human_review"] for c in cases))

        big = cases[0]
        self.assertEqual(big["kind"], "photo_reuse")
        self.assertEqual(big["headline"], "One photo, 23 different works")
        self.assertEqual(ids(big), [str(i) for i in range(1001, 1024)])
        self.assertEqual(big["villages"], ["Duggan", "Kheri"])
        self.assertEqual(big["offices"], ["BDPO BHAWANIGARH", "BDPO SANGRUR"])
        self.assertEqual((big["state"], big["district"], big["block"]), ("Punjab", "Sangrur", "Sangrur"))
        text = " ".join(big["reasons"])
        for part in ("same GPS point (30.251337, 75.735085)", "same day (2026-04-29)",
                     "uploaded 23 times within 11 minutes", "2 different offices"):
            self.assertIn(part, text)
        self.assertTrue((self.root / "site" / big["photo"]).exists())
        self.assertRegex(big["photo"], r"^photos/[0-9a-f]{64}\.jpg$")

        sev = [c["severity"] for c in cases]
        self.assertEqual(sev, sorted(sev, reverse=True))

        by_kind = {c["kind"]: c for c in cases if c["kind"] != "photo_reuse"}
        self.assertEqual(ids(by_kind["same_gps"]), [str(i) for i in range(2001, 2007)])
        self.assertEqual(ids(by_kind["same_day"]), [str(i) for i in range(3001, 3006)])
        self.assertEqual(ids(by_kind["upload_burst"]), [str(i) for i in range(4001, 4011)])
        self.assertEqual(by_kind["upload_burst"]["offices"], ["BDPO BHAWANIGARH"])
        small = [c for c in cases if c["kind"] == "photo_reuse"][1]
        self.assertEqual((small["state"], ids(small)), ("Haryana", ["8001", "8002"]))
        for c in cases:
            self.assertFalse({"5001", "6001", "7001"} & set(ids(c)))

        self.assertEqual([s["id"] for s in out["schemes"]], ["jsjb"])
        cov = {(r["state"], r["block"]): r for r in out["coverage"]}
        self.assertEqual(cov[("Punjab", "Sangrur")]["villages_checked"], 4)
        self.assertEqual(cov[("Punjab", "Sangrur")]["records_checked"], 23 + 6 + 5 + 1 + 4 + 1)
        self.assertEqual(cov[("Punjab", "Sangrur")]["photos_checked"], 1 + 6 + 5 + 1 + 4)
        self.assertEqual(cov[("Haryana", "Gharaunda")]["photos_checked"], 1)

        areas = self.load("areas.json")["states"]
        self.assertEqual(areas["Punjab"]["cases"], 4)
        self.assertEqual(areas["Punjab"]["districts"]["Sangrur"]["by_scheme"], {"jsjb": 4})
        self.assertEqual(areas["Haryana"]["by_scheme"], {"jsjb": 1})

        raw = (self.root / "site" / "data" / "cases.json").read_text(encoding="utf-8")
        self.assertNotIn("—", raw)
        self.assertNotIn("_photo_src", raw)

    def test_ids_stable_across_runs(self):
        run(self.root, log=quiet)
        first = [c["id"] for c in self.load("cases.json")["cases"]]
        run(self.root, log=quiet)
        self.assertEqual(first, [c["id"] for c in self.load("cases.json")["cases"]])
        self.assertTrue(all(re.fullmatch(r"jsjb-[a-z_]+-[0-9a-f]{12}", i) for i in first))

    def test_vision_without_settings_is_skipped(self):
        env = {k: os.environ.pop(k, None) for k in ("VISION_API_BASE", "VISION_MODEL")}
        try:
            cases = run(self.root, use_vision=True, log=quiet)
        finally:
            os.environ.update({k: v for k, v in env.items() if v is not None})
        self.assertFalse(any(c["kind"] == "vision_mismatch" for c in cases))

    def test_vision_with_fake_server(self):
        server = HTTPServer(("127.0.0.1", 0), FakeVision)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = {k: os.environ.get(k) for k in ("VISION_API_BASE", "VISION_API_KEY", "VISION_MODEL")}
        os.environ.update({"VISION_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
                           "VISION_API_KEY": "test-key", "VISION_MODEL": "fake-model"})
        try:
            run(self.root, use_vision=True, vision_limit=3, log=quiet)
            self.assertEqual(FakeVision.answered, 3)
            self.assertEqual(FakeVision.requests, 4)
            run(self.root, use_vision=True, log=quiet)
            unique_photos = 28
            self.assertEqual(FakeVision.answered, unique_photos)
            run(self.root, use_vision=True, log=quiet)
            self.assertEqual(FakeVision.answered, unique_photos)
        finally:
            server.shutdown()
            server.server_close()
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cache = json.loads((self.root / "data" / "state" / "vision_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cache), 28)
        self.assertTrue(all(k.startswith("fake-model|") for k in cache))
        vis = [c for c in self.load("cases.json")["cases"] if c["kind"] == "vision_mismatch"]
        self.assertEqual(len(vis), 1)
        self.assertEqual(ids(vis[0]), ["3001", "3002"])
        self.assertTrue(vis[0]["needs_human_review"])
        self.assertIn("bare field with no structure", " ".join(vis[0]["reasons"]))

    def test_single_work_vision_case_kept(self):
        from checker import vision
        from checker.common import load_config, load_records
        recs = load_records(self.root)["jsjb"]
        target = next(r for r in recs if r.get("structure_type") and len(r.get("photos") or []) == 1
                      and sum(p["sha256"] == r["photos"][0]["sha256"] for x in recs for p in x.get("photos") or []) == 1)
        sha = target["photos"][0]["sha256"]
        old = vision.settings, vision.ask
        vision.settings = lambda: {"base": "http://127.0.0.1:9", "key": "", "model": "stub"}
        vision.ask = lambda s, path, claimed, vcfg: {"answer": "no" if sha in str(path) else "yes", "seen": "an empty road"}
        try:
            cases = vision.find("jsjb", recs, load_config(), self.root, log=quiet)
        finally:
            vision.settings, vision.ask = old
        one = [c for c in cases if c["record_count"] == 1]
        self.assertEqual([c["records"][0]["record_id"] for c in one], [str(target["record_id"])])
        self.assertTrue(one[0]["headline"].startswith("1 work whose"))

    def test_checks_run_listed(self):
        run(self.root, log=quiet)
        self.assertNotIn("vision_mismatch", self.load("cases.json")["checks_run"])

    def test_bad_records_are_skipped_and_counted(self):
        run(self.root, log=quiet)
        before = self.load("cases.json")["cases"]
        path = self.root / "data" / "records" / "jsjb.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        good = json.loads(lines[-1])
        bad = [
            '{"record_id": "9001", "state": "Pun',
            "[1, 2, 3]",
            json.dumps({"state": "Punjab"}),
            json.dumps(dict(good, record_id="9002", photos="not a list")),
            json.dumps(dict(good, record_id="9003", structure_type=5.5)),
            json.dumps(dict(good, record_id="9004", lat="thirty")),
            json.dumps(dict(good, record_id="9005", photos=[None])),
        ]
        odd_but_usable = [
            json.dumps(dict(good, record_id=9101, village_code=39781, structure_type=None, created_by=None,
                            lat=None, lon=None, photos=None)),
            json.dumps(dict(good, record_id="9006", last_seen=20261101)),
            json.dumps(dict(good, record_id="9102", photos=[{"url": "u", "sha256": "f" * 64,
                                                             "file": "data/photos/ff/gone.jpg"}])),
        ]
        path.write_text("\n".join(lines + bad + odd_but_usable) + "\n\xff\n", encoding="utf-8")
        (self.root / "data" / "records" / "jsjb.coverage.json").write_text('[{"state": 3}, "x"', encoding="utf-8")
        cache = self.root / "data" / "state" / "vision_cache.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{broken", encoding="utf-8")
        messages = []
        run(self.root, log=messages.append)
        text = "\n".join(messages)
        self.assertIn("skipped 9 malformed record(s) or file(s)", text)
        self.assertIn(f"jsjb: {len(lines) + 3} records", text)
        self.assertIn("jsjb.jsonl line", text)
        after = self.load("cases.json")
        self.assertEqual(len(after["cases"]), len(before))
        self.assertTrue(any(r["records_checked"] for r in after["coverage"]))


if __name__ == "__main__":
    unittest.main()


class PrunePhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture.build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def photo_files(self):
        return {p.name.split(".")[0] for p in (self.root / "data" / "photos").rglob("*") if p.is_file()}

    def test_only_case_photos_keep_their_files(self):
        from checker import prune
        before = self.photo_files()
        cases = run(self.root, log=quiet, prune_photos=False)
        site_cases = json.loads((self.root / "site" / "data" / "cases.json").read_text(encoding="utf-8"))["cases"]
        keep = {Path(c["photo"]).stem for c in site_cases if c.get("photo")}
        self.assertTrue(keep and keep < before, (len(keep), len(before)))
        # Fresh files are left alone during the grace period.
        self.assertEqual(prune.prune(self.root / "data" / "photos", keep)[0], 0)
        deleted, _, kept = prune.prune(self.root / "data" / "photos", keep, now=time.time() + 13 * 3600)
        self.assertEqual(deleted, len(before - keep))
        self.assertEqual(self.photo_files(), keep)
        # The hashes are still in the records, and a second check finds the same cases with their photos.
        again = run(self.root, log=quiet)
        self.assertEqual(sorted(c["id"] for c in again), sorted(c["id"] for c in cases))
        self.assertTrue(all(c.get("photo") for c in again if c["kind"] == "photo_reuse"))

    def test_checker_keeps_each_case_photo(self):
        from checker import prune
        seen = {}
        real = prune.prune
        prune.prune = lambda d, keep, **kw: seen.setdefault("keep", set(keep)) and (0, 0, 0) or (0, 0, 0)
        try:
            run(self.root, log=quiet)
        finally:
            prune.prune = real
        site_cases = json.loads((self.root / "site" / "data" / "cases.json").read_text(encoding="utf-8"))["cases"]
        self.assertEqual(seen["keep"], {Path(c["photo"]).stem for c in site_cases if c.get("photo")})
