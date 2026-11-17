"""Near-duplicate photo matching (checker/nearmatch.py, photo_reuse with phash.jsonl). Offline,
standard library only: the phash lines are written by hand, with 32x32 copies made of noise."""
import base64
import copy
import hashlib
import json
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402
from checker import nearmatch, photo_reuse, prune  # noqa: E402
from checker.__main__ import run  # noqa: E402
from checker.common import load_config, stable_id  # noqa: E402


def quiet(*_):
    pass


def sha_of(text):
    return hashlib.sha256(text.encode()).hexdigest()


def texture(seed):
    rnd = random.Random(seed)
    return bytes(rnd.randrange(256) for _ in range(1024))


def noisy(px, amount, seed=1):
    rnd = random.Random(seed)
    return bytes(min(255, max(0, v + rnd.randint(-amount, amount))) for v in px)


def flip(h, bits, start=0):
    for i in range(bits):
        h ^= 1 << (start + i * 7) % 256
    return h


def line(sha, dhash, px, w=1280, h=960, flat=False, size=None, levels=200):
    return {"sha256": sha, "v": nearmatch.VERSION, "dhash": f"{dhash:064x}", "w": w, "h": h, "flat": flat,
            "size": size or [w, h], "levels": levels, "thumb": base64.b64encode(px).decode()}


def write_phash(folder, lines, tail=""):
    path = Path(folder) / "state" / "phash.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines) + tail, encoding="utf-8")
    return path


def rec(rid, sha, village="Duggan", office="BDPO SANGRUR", lat=30.1, lon=75.1):
    return {"record_id": str(rid), "village": village, "created_by": office, "lat": lat, "lon": lon,
            "structure_type": "Farm pond", "start_date": "2026-01-01", "end_date": "2026-01-09",
            "state": "Punjab", "district": "Sangrur", "block": "Sangrur",
            "photos": [{"url": f"https://example.invalid/{rid}.jpg", "sha256": sha,
                        "file": f"data/photos/{sha[:2]}/{sha}.jpg", "uploaded_at": None}]}


BASE = random.Random(7).getrandbits(256)
A, B, C, D = (sha_of(x) for x in "ABCD")


class Unit(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def near(self, lines, not_same=()):
        return nearmatch.NearMatch({x["sha256"]: nearmatch.parse_line(json.dumps(x)) for x in lines},
                                   {frozenset(p) for p in not_same})

    def cases(self, records, lines, cfg=None, not_same=()):
        near = self.near(lines, not_same)
        return photo_reuse.find("jsjb", records, cfg or self.cfg, near), near

    def test_re_saved_copy_joins_the_case_of_the_original(self):
        px = texture(1)
        lines = [line(A, BASE, px), line(B, flip(BASE, 3), noisy(px, 2)), line(C, ~BASE & (2 ** 256 - 1), texture(2))]
        records = [rec(1, A), rec(2, A), rec(3, B), rec(4, C)]
        cases, near = self.cases(records, lines)
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertEqual(sorted(r["record_id"] for r in c["records"]), ["1", "2", "3"])
        # The id is the one the original file's case always had.
        self.assertEqual(c["id"], stable_id("jsjb", "photo_reuse", A))
        self.assertEqual(c["headline"], "One picture, 3 different works")
        self.assertEqual(c["_photo_src"]["sha256"], A)
        self.assertEqual(c["photo_files"], [{"sha256": A, "records": 2}, {"sha256": B, "records": 1}])
        self.assertEqual(c["other_ids"], [stable_id("jsjb", "photo_reuse", B)])
        text = " ".join(c["reasons"])
        self.assertIn("The same picture was saved as 2 different files and attached to 3 different works", text)
        self.assertIn("differ in at most 3 of 256 bits", text)
        self.assertIn("(the limit is 8)", text)
        self.assertIn(f"{A[:12]} on 2 works, {B[:12]} on 1 work.", text)
        self.assertIn("All 3 works report the same GPS point", text)
        # 3 works x weight 3, plus 1 for the shared GPS point.
        self.assertEqual(c["severity"], 3 * 3 + 1)
        rep = near.report["jsjb"]
        self.assertEqual((rep["files_merged"], rep["pictures"], rep["largest_distance"]), (2, 1, 3))
        self.assertEqual([(p["a"], p["b"], p["case_id"]) for p in near.pairs], [(A, B, c["id"])])

    def test_one_file_case_is_unchanged_when_near_matching_runs(self):
        lines = [line(A, BASE, texture(1)), line(B, ~BASE & (2 ** 256 - 1), texture(2))]
        records = [rec(1, A), rec(2, A), rec(3, B)]
        with_near, _ = self.cases(records, lines)
        without = photo_reuse.find("jsjb", records, self.cfg)
        self.assertEqual(with_near, without)
        self.assertEqual(without[0]["headline"], "One photo, 2 different works")
        self.assertNotIn("photo_files", without[0])

    def test_threshold_zero_or_no_phash_is_the_old_check(self):
        px = texture(1)
        lines = [line(A, BASE, px), line(B, BASE, px)]
        records = [rec(1, A), rec(2, B), rec(3, B)]
        cfg = copy.deepcopy(self.cfg)
        cfg["photo_reuse"]["near_max_distance"] = 0
        off, near = self.cases(records, lines, cfg)
        self.assertEqual(off, photo_reuse.find("jsjb", records, self.cfg))
        self.assertEqual([c["record_count"] for c in off], [2])
        self.assertEqual(near.report["jsjb"], {"near_matching": False, "why_off": "photo_reuse.near_max_distance is 0",
                                               "placeholder_cases": 0, "placeholder_records": 0})

    def test_settings_above_the_safe_limits_turn_near_matching_off(self):
        px = texture(1)
        for key, value in (("near_max_distance", 17), ("near_max_mad", 0.06), ("near_max_aspect_gap", 0.2),
                           ("near_max_mad", "0.03"), ("near_max_distance", True)):
            cfg = copy.deepcopy(self.cfg)
            cfg["photo_reuse"][key] = value
            cases, near = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, px), line(B, BASE, px)], cfg)
            self.assertEqual(cases, [], (key, value))
            self.assertFalse(near.report["jsjb"]["near_matching"])

    def test_no_chaining(self):
        # A~B and B~C are within 8 bits, A and C are 10 apart: A and C are never in one case.
        px = texture(1)
        hb = flip(BASE, 5, 0)
        hc = flip(hb, 5, 100)
        lines = [line(A, BASE, px), line(B, hb, px), line(C, hc, px)]
        self.assertEqual(((BASE ^ hc).bit_count(), (BASE ^ hb).bit_count(), (hb ^ hc).bit_count()), (10, 5, 5))
        records = [rec(1, A), rec(2, A), rec(3, A), rec(4, B), rec(5, C), rec(6, C)]
        cases, _ = self.cases(records, lines)
        for c in cases:
            shas = {r["record_id"] for r in c["records"]}
            self.assertFalse({"1", "5"} <= shas, c["reasons"])
        # The heaviest file (A, 3 works) takes B; C stays a case of its own.
        self.assertEqual(sorted(sorted(r["record_id"] for r in c["records"]) for c in cases),
                         [["1", "2", "3", "4"], ["5", "6"]])

    def test_every_member_of_a_group_passes_against_every_other(self):
        rnd = random.Random(3)
        px = texture(1)
        shas = [sha_of(f"m{i}") for i in range(12)]
        lines = [line(s, flip(BASE, rnd.randrange(0, 7), rnd.randrange(256)), noisy(px, 2, i))
                 for i, s in enumerate(shas)]
        records = [rec(i, s) for i, s in enumerate(shas)]
        near = self.near(lines)
        limits, _ = nearmatch.settings(self.cfg["photo_reuse"])
        groups, accepted, _ = near.group({s: 1 for s in shas}, limits)
        for g in groups:
            for i, a in enumerate(g):
                for b in g[i + 1:]:
                    self.assertIn(tuple(sorted((a, b))), accepted)
        self.assertEqual(sorted(s for g in groups for s in g), sorted(shas))
        self.assertTrue(photo_reuse.find("jsjb", records, self.cfg, near))

    def test_aspect_ratio_must_match(self):
        px = texture(1)
        for wb, hb in ((1600, 900), (960, 1280), (1300, 960)):
            cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, px), line(B, BASE, px, wb, hb)])
            self.assertEqual(cases, [], (wb, hb))
        # Same ratio at another size (a resized copy) matches.
        cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, px), line(B, BASE, px, 640, 480)])
        self.assertEqual(len(cases), 1)

    def test_pixel_test_must_pass_too(self):
        # Same dHash, different picture: not merged.
        cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, texture(1)), line(B, BASE, texture(2))])
        self.assertEqual(cases, [])
        cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, texture(1)), line(B, BASE, noisy(texture(1), 6))])
        self.assertEqual(cases, [])

    def test_distance_over_the_limit_is_not_merged(self):
        px = texture(1)
        for bits, merged in ((8, 1), (9, 0)):
            cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, px), line(B, flip(BASE, bits), px)])
            self.assertEqual(len(cases), merged, bits)

    def test_low_information_files_never_match(self):
        grey = bytes([128]) * 1024
        stripes = bytes(255 if c < 16 else 0 for r in range(32) for c in range(32))
        px = texture(1)
        for la, lb in ((line(A, BASE, px, flat=True), line(B, BASE, px)),
                       (line(A, BASE, grey), line(B, BASE, grey)),
                       (line(A, BASE, stripes), line(B, BASE, stripes)),
                       (line(A, BASE, px, 90, 90), line(B, BASE, px, 90, 90))):
            cases, near = self.cases([rec(1, A), rec(2, B)], [la, lb])
            self.assertEqual(cases, [])
            self.assertGreaterEqual(near.report["jsjb"]["low_information"], 1)
        # An exact copy of a plain file is still one file on two works.
        cases, _ = self.cases([rec(1, A), rec(2, A)], [line(A, BASE, grey)])
        self.assertEqual(len(cases), 1)

    def test_low_information_rule(self):
        self.assertIsNone(nearmatch.low_information(texture(1), 1280, 960))
        self.assertEqual(nearmatch.low_information(texture(1), 99, 960), "shorter side under 100 px")
        self.assertEqual(nearmatch.low_information(bytes([10, 30] * 512), 1280, 960), "too little contrast")
        smooth = bytes(min(255, r * 8) for r in range(32) for c in range(32))
        self.assertEqual(nearmatch.low_information(smooth, 1280, 960), "too little detail")
        noise = texture(5)
        mostly_flat = bytes(128 if r < 22 else noise[r * 32 + c] for r in range(32) for c in range(32))
        self.assertEqual(nearmatch.low_information(mostly_flat, 1280, 960), "mostly flat areas")

    def test_not_same_pairs_are_never_merged(self):
        px = texture(1)
        lines = [line(A, BASE, px), line(B, BASE, px)]
        cases, _ = self.cases([rec(1, A), rec(2, B)], lines, not_same=[(A, B)])
        self.assertEqual(cases, [])
        cases, _ = self.cases([rec(1, A), rec(2, B)], lines, not_same=[(A, C)])
        self.assertEqual(len(cases), 1)

    def test_files_without_a_hash_keep_exact_matching(self):
        cases, near = self.cases([rec(1, A), rec(2, A), rec(3, D)], [line(B, BASE, texture(1))])
        self.assertEqual([c["record_count"] for c in cases], [2])
        self.assertEqual(near.report["jsjb"]["photos_with_hash"], 0)

    def test_tie_takes_the_smallest_sha(self):
        px = texture(1)
        first, second = sorted([A, B])
        cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, px), line(B, BASE, px)])
        self.assertEqual(cases[0]["id"], stable_id("jsjb", "photo_reuse", first))
        self.assertEqual(cases[0]["other_ids"], [stable_id("jsjb", "photo_reuse", second)])

    def test_multi_index_finds_every_close_pair(self):
        rnd = random.Random(11)
        hashes = {}
        for i in range(300):
            h = rnd.getrandbits(256) if i % 3 == 0 else flip(hashes[sha_of(f"h{i - 1}")], rnd.randrange(0, 12), rnd.randrange(256))
            hashes[sha_of(f"h{i}")] = h
        for d in (1, 4, 8, 16):
            brute = {}
            keys = sorted(hashes)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    bits = (hashes[a] ^ hashes[b]).bit_count()
                    if bits <= d:
                        brute[(a, b)] = bits
            self.assertEqual(nearmatch.close_pairs(hashes, d), brute, d)
            self.assertTrue(brute)

    def test_reading_skips_broken_and_cut_off_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            px = texture(1)
            path = write_phash(tmp, [line(A, BASE, px), {"sha256": B, "v": nearmatch.VERSION, "error": "could not be decoded"},
                                     {"sha256": "x", "v": nearmatch.VERSION}, line(C, BASE, px) | {"v": 1}],
                               tail='{"sha256": "' + D + '", "v": 2, "dhash": "ab')
            entries = nearmatch.read_phash(path)
            self.assertEqual(sorted(entries), sorted([A, B]))
            self.assertFalse(entries[B].usable)
            self.assertIsNone(nearmatch.read_phash(Path(tmp) / "nothing.jsonl"))

    def test_placeholder_on_several_works_is_a_case_with_a_note(self):
        grey = bytes([200]) * 1024
        records = [rec(1, A), rec(2, A), rec(3, A), rec(4, B), rec(5, B)]
        for la in (line(A, BASE, grey, 73, 73, levels=47),           # broken-image icon
                   line(A, BASE, texture(1), 533, 470, levels=13)):  # "Error loading image" graphic
            cases, near = self.cases(records, [la, line(B, ~BASE & (2 ** 256 - 1), texture(2))])
            self.assertEqual(sorted(c["record_count"] for c in cases), [2, 3])
            icon = next(c for c in cases if c["record_count"] == 3)
            self.assertEqual(icon["placeholder"], {"width": la["size"][0] if "size" in la else la["w"],
                                                   "height": la["size"][1] if "size" in la else la["h"]})
            self.assertIn("is not a photograph of a work", " ".join(icon["reasons"]))
            self.assertNotIn("placeholder", next(c for c in cases if c["record_count"] == 2))
            rep = near.report["jsjb"]
            self.assertEqual((rep["placeholder_cases"], rep["placeholder_records"]), (1, 3))
            self.assertEqual([(x["sha"], x["records"]) for x in near.left_out], [(A, 3)])
        # Marked by hand in placeholders.txt.
        near = self.near([line(A, BASE, texture(1)), line(B, ~BASE & (2 ** 256 - 1), texture(2))])
        near.marked_placeholders = {B}
        cases = photo_reuse.find("jsjb", records, self.cfg, near)
        self.assertEqual(sorted(c["record_count"] for c in cases), [2, 3])
        self.assertIn("placeholder", next(c for c in cases if c["record_count"] == 2))
        # A real photo of bare ground is low information but still a case when one file is on 2 works.
        flat_ground = line(A, BASE, bytes([120, 124] * 512), 4160, 3120, levels=90)
        cases, near = self.cases([rec(1, A), rec(2, A)], [flat_ground])
        self.assertEqual(len(cases), 1)
        self.assertEqual(near.left_out, [])
        # So is a small copy of a real photo (90 x 200, many grey levels).
        cases, near = self.cases([rec(1, A), rec(2, A)], [line(A, BASE, texture(1), 90, 200, levels=125)])
        self.assertEqual(len(cases), 1)
        # A placeholder never joins a photo by picture either.
        cases, _ = self.cases([rec(1, A), rec(2, B)], [line(A, BASE, texture(1)),
                                                       line(B, BASE, texture(1), levels=20)])
        self.assertEqual(cases, [])

    def test_lines_with_impossible_sizes_are_broken(self):
        px = texture(1)
        self.assertIsNone(nearmatch.parse_line(json.dumps(line(A, BASE, px) | {"w": 10 ** 400, "h": 100})))
        self.assertIsNone(nearmatch.parse_line(json.dumps(line(A, BASE, px) | {"size": [100, 100]})))
        self.assertIsNone(nearmatch.parse_line(json.dumps(line(A, BASE, px) | {"size": None})))
        self.assertIsNotNone(nearmatch.parse_line(json.dumps(line(A, BASE, px, 900, 900, size=[1000, 1600]))))

    def test_plain_bands_at_the_edges_are_low_information(self):
        noise = texture(9)
        # A screenshot whose black bars the border trim missed: 8 black rows top and bottom.
        framed = bytes(0 if r < 8 or r >= 24 else noise[r * 32 + c] for r in range(32) for c in range(32))
        self.assertEqual(nearmatch.low_information(framed, 716, 1600), "large plain borders")
        # A status bar in the first row does not hide the bar under it.
        status = bytes((40 + (c % 2) * 120 if r == 0 else 0) if r < 8 or r >= 24 else noise[r * 32 + c]
                       for r in range(32) for c in range(32))
        self.assertEqual(nearmatch.low_information(status, 716, 1600), "large plain borders")
        # A sky that gets lighter row by row is not one plain band.
        sky = bytes(min(255, 60 + r * 12) if r < 8 else noise[r * 32 + c] for r in range(32) for c in range(32))
        self.assertIsNone(nearmatch.low_information(sky, 1280, 960))
        # Little content inside the whole image (w x h against size) never matches.
        e = nearmatch.parse_line(json.dumps(line(A, BASE, texture(1), 300, 225, size=[720, 1600])))
        self.assertEqual(e.low_information(), "mostly plain border")

    def test_not_same_file_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not_same.txt"
            path.write_text(f"# checked by hand\n{A} {B}\n{C.upper()},{D}  # stamp differs\nshort {A}\n{A} {A}\n",
                            encoding="utf-8")
            msgs = []
            self.assertEqual(nearmatch.read_not_same(path, msgs.append), {frozenset((A, B)), frozenset((C, D))})
            self.assertEqual(len(msgs), 2)


class EndToEnd(unittest.TestCase):
    """fixture.py data plus a re-saved copy of photo-reuse-B on 3 more works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.records = fixture.build(self.root)
        self.b = next(p["sha256"] for r in self.records for p in r["photos"] if r["record_id"] == "8001")
        haryana = {k: self.records[-1][k] for k in ("state", "state_code", "district", "district_code", "area_type")}
        extra = []
        for i in range(3):
            p = fixture.photo(self.root, "photo-reuse-B saved again", f"2026-03-1{i}T12:00:00")
            extra.append(fixture.rec(8101 + i, "Gharaunda", "50001", "Gharaunda", "500", "BDPO GHARAUNDA", "Soak pit",
                                     29.7 + i, 76.9, "2026-03-01", "2026-03-04", [p], haryana))
        self.b2 = extra[0]["photos"][0]["sha256"]
        with open(self.root / "data" / "records" / "jsjb.jsonl", "a", encoding="utf-8") as f:
            for r in extra:
                f.write(json.dumps(r) + "\n")
        shas = sorted({p["sha256"] for r in self.records + extra for p in r["photos"]})
        self.px = texture(1)
        lines = []
        for i, s in enumerate(shas):
            if s == self.b2:
                continue
            h = random.Random(s).getrandbits(256)
            lines.append(line(s, h, self.px if s == self.b else texture(s)))
        lines.append(line(self.b2, flip(next(int(x["dhash"], 16) for x in lines if x["sha256"] == self.b), 2),
                          noisy(self.px, 1)))
        self.unhashed = shas[0] if shas[0] not in (self.b, self.b2) else shas[1]
        write_phash(self.root / "data", [x for x in lines if x["sha256"] != self.unhashed])

    def tearDown(self):
        self.tmp.cleanup()

    def site_cases(self):
        return json.loads((self.root / "site" / "data" / "cases.json").read_text(encoding="utf-8"))

    def test_checker_run_merges_writes_audit_and_reports(self):
        msgs = []
        run(self.root, log=msgs.append, prune_photos=False)
        out = self.site_cases()
        merged = [c for c in out["cases"] if c.get("photo_files")]
        self.assertEqual(len(merged), 1)
        c = merged[0]
        self.assertEqual(c["id"], stable_id("jsjb", "photo_reuse", self.b2))
        self.assertEqual(c["record_count"], 5)
        self.assertEqual(c["headline"], "One picture, 5 different works")
        self.assertEqual(c["other_ids"], [stable_id("jsjb", "photo_reuse", self.b)])
        self.assertTrue(c["photo"].startswith("photos/" + self.b2))
        self.assertNotIn("_member_shas", json.dumps(out))
        self.assertNotIn("_photo_alts", json.dumps(out))
        self.assertFalse(any(x["id"] == stable_id("jsjb", "photo_reuse", self.b) for x in out["cases"]))
        m = out["schemes"][0]["photo_match"]
        self.assertTrue(m["near_matching"])
        self.assertEqual((m["files_merged"], m["pictures"], m["largest_distance"], m["max_distance"]), (2, 1, 2, 8))
        self.assertEqual(m["photos"] - m["photos_with_hash"], 1)
        self.assertLessEqual(m["largest_mad"], 0.03)
        text = "\n".join(msgs)
        self.assertIn("2 files merged into 1 pictures; largest distance used 2 of limit 8", text)
        audit = (self.root / "data" / "preview" / "near_matches.html").read_text(encoding="utf-8")
        self.assertIn(c["id"], audit)
        # The file on more works first, ready to paste into not_same.txt.
        self.assertIn(f"{self.b2} {self.b}", audit)
        self.assertIn("../photos/", audit)
        self.assertNotIn("—", audit)
        self.assertFalse((self.root / "site" / "near_matches.html").exists())

    def test_not_same_file_splits_the_case_again(self):
        (self.root / "data" / "state" / "not_same.txt").write_text(f"{self.b} {self.b2}\n", encoding="utf-8")
        run(self.root, log=quiet, prune_photos=False)
        cases = self.site_cases()["cases"]
        self.assertFalse(any(c.get("photo_files") for c in cases))
        ids = {c["id"] for c in cases}
        self.assertIn(stable_id("jsjb", "photo_reuse", self.b), ids)
        self.assertIn(stable_id("jsjb", "photo_reuse", self.b2), ids)
        audit = (self.root / "data" / "preview" / "near_matches.html").read_text(encoding="utf-8")
        self.assertIn("No pair of different files was merged.", audit)

    def age_photos(self):
        photos = self.root / "data" / "photos"
        old = time.time() - 2 * 86400
        files = [p for p in photos.rglob("*") if p.is_file()]
        for f in files:
            os.utime(f, (old, old))
        return photos, len(files)

    def files_left(self, photos):
        return {p.name.split(".")[0] for p in photos.rglob("*") if p.is_file()}

    def test_prune_keeps_unhashed_files_and_every_file_of_a_picture(self):
        photos, before = self.age_photos()
        msgs = []
        run(self.root, log=msgs.append)
        first = self.site_cases()
        left = self.files_left(photos)
        self.assertIn(self.unhashed, left)
        self.assertTrue({self.b, self.b2} <= left)
        evidence = {Path(c["photo"]).stem for c in first["cases"] if c.get("photo")}
        self.assertEqual(left, evidence | {self.b, self.b2, self.unhashed})
        self.assertLess(len(left), before)
        # The deleted files keep their hashes and the merged case comes back the same.
        run(self.root, log=quiet)
        self.assertEqual([c["id"] for c in self.site_cases()["cases"]], [c["id"] for c in first["cases"]])
        self.assertEqual([c["photo"] for c in self.site_cases()["cases"]], [c["photo"] for c in first["cases"]])

    def test_merged_case_shows_another_file_when_its_main_file_is_gone(self):
        for f in (self.root / "data" / "photos").rglob(self.b2 + ".*"):
            f.unlink()
        run(self.root, log=quiet, prune_photos=False)
        c = next(c for c in self.site_cases()["cases"] if c.get("photo_files"))
        self.assertTrue(c["photo"].startswith("photos/" + self.b))

    def test_photo_found_under_another_extension(self):
        # A file downloaded again may be <sha>.jpg where the records say <sha>.jpeg, or the other way.
        for f in (self.root / "data" / "photos").rglob(self.b2 + ".*"):
            f.rename(f.with_suffix(".jpeg"))
        run(self.root, log=quiet, prune_photos=False)
        c = next(c for c in self.site_cases()["cases"] if c.get("photo_files"))
        self.assertTrue(c["photo"].startswith("photos/" + self.b2))
        audit = (self.root / "data" / "preview" / "near_matches.html").read_text(encoding="utf-8")
        self.assertIn(self.b2 + ".jpeg", audit)

    def test_unreadable_phash_file_deletes_nothing(self):
        photos, before = self.age_photos()
        real = nearmatch.read_phash

        def locked(path):
            raise PermissionError(13, "Permission denied")
        nearmatch.read_phash = locked
        try:
            msgs = []
            run(self.root, log=msgs.append)
        finally:
            nearmatch.read_phash = real
        text = "\n".join(msgs)
        self.assertIn("could not be read", text)
        self.assertIn("none deleted this run, because phash.jsonl could not be read", text)
        self.assertNotIn("not found", text)
        self.assertEqual(len(self.files_left(photos)), before)
        self.assertEqual(self.site_cases()["schemes"][0]["photo_match"]["why_off"],
                         "phash.jsonl could not be read this run")

    def test_a_failed_check_deletes_nothing(self):
        photos, before = self.age_photos()
        # A line the checker cannot use must not break photo_reuse.
        with open(self.root / "data" / "state" / "phash.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line(sha_of("huge"), 1, self.px) | {"w": 10 ** 400, "h": 100}) + "\n")
        real = photo_reuse.find

        def broken(*a):
            raise OverflowError("integer division result too large for a float")
        msgs = []
        run(self.root, log=msgs.append, prune_photos=False)
        self.assertFalse(any("check failed" in m for m in msgs))
        photo_reuse.find = broken
        try:
            msgs = []
            run(self.root, log=msgs.append)
        finally:
            photo_reuse.find = real
        self.assertIn("none deleted this run, because these checks failed: jsjb.photo_reuse", "\n".join(msgs))
        self.assertEqual(len(self.files_left(photos)), before)

    def test_check_of_one_scheme_keeps_the_audit_page(self):
        run(self.root, log=quiet, prune_photos=False)
        path = self.root / "data" / "preview" / "near_matches.html"
        page = path.read_text(encoding="utf-8")
        msgs = []
        run(self.root, scheme="jsjb", log=msgs.append, prune_photos=False)
        self.assertEqual(path.read_text(encoding="utf-8"), page)
        self.assertIn("not rewritten", "\n".join(msgs))

    def test_placeholder_case_is_kept_marked_and_listed_for_review(self):
        (self.root / "data" / "state" / "placeholders.txt").write_text(f"# icons\n{self.b2}\n{self.b}\n",
                                                                     encoding="utf-8")
        run(self.root, log=quiet, prune_photos=False)
        out = self.site_cases()
        ids = {c["id"] for c in out["cases"]}
        self.assertIn(stable_id("jsjb", "photo_reuse", self.b), ids)
        self.assertIn(stable_id("jsjb", "photo_reuse", self.b2), ids)
        marked = [c for c in out["cases"] if c["id"] in {stable_id("jsjb", "photo_reuse", self.b),
                                                          stable_id("jsjb", "photo_reuse", self.b2)}]
        self.assertTrue(all("placeholder" in c for c in marked))
        m = out["schemes"][0]["photo_match"]
        self.assertEqual((m["placeholder_cases"], m["placeholder_records"]), (2, 5))
        audit = (self.root / "data" / "preview" / "near_matches.html").read_text(encoding="utf-8")
        self.assertIn("Cases whose image is a placeholder", audit)
        self.assertIn(self.b, audit)

    def test_without_phash_file_prune_works_as_before_and_says_so(self):
        (self.root / "data" / "state" / "phash.jsonl").unlink()
        msgs = []
        run(self.root, log=msgs.append, prune_photos=False)
        text = "\n".join(msgs)
        self.assertIn("near-duplicate photo matching needs the phash step", text)
        self.assertFalse(any(c.get("photo_files") for c in self.site_cases()["cases"]))
        self.assertEqual(self.site_cases()["schemes"][0]["photo_match"]["near_matching"], False)
        self.assertFalse((self.root / "data" / "preview" / "near_matches.html").exists())
        photos, _ = self.age_photos()
        run(self.root, log=quiet)
        evidence = {Path(c["photo"]).stem for c in self.site_cases()["cases"] if c.get("photo")}
        self.assertEqual(self.files_left(photos), evidence)
        self.assertNotIn(self.unhashed, evidence)


if __name__ == "__main__":
    unittest.main()
