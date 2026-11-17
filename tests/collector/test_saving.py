"""Cheap saving (appends and journals) and photos downloaded side by side. Offline.
Run: python tests/collector/test_saving.py"""
import json
import logging
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)

import collector.store as st  # noqa: E402
from checker.common import load_coverage, load_records  # noqa: E402
from collector.__main__ import collect, exit_code  # noqa: E402
from collector.base import Area  # noqa: E402
from collector.http import Client  # noqa: E402
from collector.schemes.jsjb import JsjbAdapter  # noqa: E402
from collector.store import Store  # noqa: E402
from test_resilience import ALL, ALL_VILLAGES, PHOTO, FakePortal  # noqa: E402

logging.disable(logging.CRITICAL)


class Saving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.d = os.path.join(self.root, "data")
        self.small, self.min_journal = st.SMALL_FILE, st.MIN_JOURNAL
        st.SMALL_FILE = 0  # behave as with a large file: append, never rewrite at the end

    def tearDown(self):
        st.SMALL_FILE, st.MIN_JOURNAL = self.small, self.min_journal
        self.tmp.cleanup()

    def run_once(self, portal=None, **kw):
        return collect(JsjbAdapter(portal or FakePortal()), Store(self.d, "jsjb"), ALL, sleep=lambda s: None, **kw)

    def lines(self, *parts):
        path = os.path.join(self.d, *parts)
        if not os.path.exists(path):
            return []
        with open(path, "rb") as f:
            return f.read().splitlines()

    def test_appends_are_read_back_by_collector_and_checker(self):
        self.run_once(flush_every=1)
        first = {json.loads(l)["record_id"]: json.loads(l)["last_seen"] for l in self.lines("records", "jsjb.jsonl")}
        self.run_once(flush_every=1)
        self.assertEqual(len(self.lines("records", "jsjb.jsonl")), 24)
        store = Store(self.d, "jsjb")
        self.assertEqual(len(store.records), 12)
        self.assertTrue(all(store.records[k]["last_seen"] >= v for k, v in first.items()))
        self.assertTrue(all(store.records[k]["first_seen"] == v for k, v in first.items()))
        recs = load_records(self.root, "jsjb")["jsjb"]
        self.assertEqual(sorted(r["record_id"] for r in recs), sorted(store.records))
        self.assertEqual({str(c["village_code"]) for c in load_coverage(self.root, "jsjb")}, set(ALL_VILLAGES))
        # End of run: the coverage journal is folded into the file the checker reads.
        self.assertEqual(self.lines("state", "jsjb.coverage.journal.jsonl"), [])

    def test_nothing_changed_writes_nothing(self):
        self.run_once()
        stamp = {p: os.path.getmtime(os.path.join(self.d, p)) for p in
                 ("records/jsjb.jsonl", "records/jsjb.coverage.json", "state/jsjb.photo_index.json")}
        stats = self.run_once(since_days=1)
        self.assertEqual(stats["not_due"], 6)
        self.assertEqual(stamp, {p: os.path.getmtime(os.path.join(self.d, p)) for p in stamp})

    def commit_new_village(self, store, code):
        area = Area(state="Alpha", district="A-One", area_type="Rural", block="X", village="New", village_code=code)
        store.commit(area, [{"record_id": code + "01", "village_code": code, "photos": []}], st.now_iso())

    def test_cheap_save_survives_a_crash_before_the_final_save(self):
        self.run_once()
        store = Store(self.d, "jsjb")
        self.commit_new_village(store, "999")
        store.flush(final=False)
        with open(os.path.join(self.d, "records", "jsjb.coverage.json"), encoding="utf-8") as f:
            self.assertNotIn("999", {c["village_code"] for c in json.load(f)})
        again = Store(self.d, "jsjb")
        self.assertIn("99901", again.records)
        self.assertIn("999", {c["village_code"] for c in again.coverage.values()})

    def test_records_saved_before_coverage(self):
        self.run_once()
        store = Store(self.d, "jsjb")
        self.commit_new_village(store, "998")
        real = st._append

        def fail_coverage(path, data):
            if "coverage" in path:
                raise st.StoreError("disk is full")
            return real(path, data)
        st._append = fail_coverage
        try:
            with self.assertRaises(st.StoreError):
                store.flush(final=False)
        finally:
            st._append = real
        again = Store(self.d, "jsjb")
        self.assertIn("99801", again.records)
        self.assertNotIn("998", {c["village_code"] for c in again.coverage.values()})

    def test_line_cut_off_by_a_crash_is_set_aside(self):
        self.run_once(flush_every=1)
        path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(path, "ab") as f:
            f.write(b'{"record_id": "11111", "half')
        with open(os.path.join(self.d, "state", "jsjb.coverage.journal.jsonl"), "ab") as f:
            f.write(b'{"village_code": "9')
        store = Store(self.d, "jsjb")
        self.assertEqual(len(store.records), 12)
        self.assertIn("skipped 1 damaged line", " ".join(store.problems))
        self.assertIn("unreadable journal line", " ".join(store.problems))
        collect(JsjbAdapter(FakePortal()), store, ALL, sleep=lambda s: None, flush_every=1)
        rows = [json.loads(l) for l in self.lines("records", "jsjb.jsonl")]
        self.assertEqual(len({r["record_id"] for r in rows}), 12)

    def test_append_after_a_line_without_newline(self):
        self.run_once()
        path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data.rstrip(b"\n"))
        self.run_once(flush_every=1)
        self.assertEqual(len(Store(self.d, "jsjb").problems), 0)
        self.assertEqual(len(self.lines("records", "jsjb.jsonl")), 24)

    def test_file_rewritten_once_it_doubles(self):
        st.MIN_JOURNAL = 0
        for _ in range(3):
            self.run_once(flush_every=1)
        n = len(self.lines("records", "jsjb.jsonl"))
        self.assertLessEqual(n, 24 + 2)
        self.assertEqual(len(Store(self.d, "jsjb").records), 12)

    def test_small_files_are_rewritten_whole_at_the_end(self):
        st.SMALL_FILE = self.small
        self.run_once(flush_every=1)
        self.run_once(flush_every=1)
        self.assertEqual(len(self.lines("records", "jsjb.jsonl")), 12)
        self.assertEqual(self.lines("state", "jsjb.photo_index.journal.jsonl"), [])


JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"x" * 64 + bytes([0xFF, 0xD9])


class DiskRoom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")
        self.free = st.free_bytes

    def tearDown(self):
        st.free_bytes = self.free
        self.tmp.cleanup()

    def test_run_stops_with_a_disk_error_before_the_disk_is_nearly_full(self):
        st.free_bytes = lambda path: st.MIN_FREE_BYTES - 1
        stats = collect(JsjbAdapter(FakePortal()), Store(self.d, "jsjb"), ALL, sleep=lambda s: None)
        self.assertEqual(stats["stop"][0], "disk")
        self.assertIn("GB free", stats["stop"][1])
        self.assertEqual(exit_code(stats), 6)
        photos = os.path.join(self.d, "photos")
        self.assertEqual(sum(len(f) for _, _, f in os.walk(photos)) if os.path.isdir(photos) else 0, 0)

    def test_photos_already_stored_need_no_room(self):
        store = Store(self.d, "jsjb")
        store.save_photo("https://example.invalid/a.jpg", JPEG)
        st.free_bytes = lambda path: 0
        self.assertTrue(store.save_photo("https://example.invalid/b.jpg", JPEG)["file"])

    def test_room_is_checked_once_every_few_photos(self):
        calls = []
        st.free_bytes = lambda path: calls.append(path) or st.MIN_FREE_BYTES * 2
        store = Store(self.d, "jsjb")
        for i in range(st.FREE_CHECK_EVERY + 1):
            store.save_photo(None, JPEG + bytes([i % 256, i // 256]))
        self.assertEqual(len(calls), 2)


class PhotoWorkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self, d):
        with open(os.path.join(d, "records", "jsjb.jsonl"), encoding="utf-8") as f:
            # Whole records, without the times of this run and with photo paths relative to the data folder.
            recs = {}
            for r in map(json.loads, f):
                r = {k: v for k, v in r.items() if k not in ("first_seen", "last_seen")}
                r["photos"] = [dict(p, file=p["file"] and os.path.relpath(p["file"], d)) for p in r["photos"]]
                recs[r["record_id"]] = r
        with open(os.path.join(d, "state", "jsjb.photo_index.json"), encoding="utf-8") as f:
            return recs, {u: e["sha256"] for u, e in json.load(f).items()}

    def files(self, d):
        """{path under photos/: bytes} of every stored photo file."""
        root = os.path.join(d, "photos")
        out = {}
        for dp, _, fs in os.walk(root):
            for name in fs:
                with open(os.path.join(dp, name), "rb") as f:
                    out[os.path.relpath(os.path.join(dp, name), root)] = f.read()
        return out

    def test_parallel_workers_store_exactly_what_one_worker_stores(self):
        faults = lambda: {("photo", f"{PHOTO}1112_1_Image1.jpg"): b"<html>error</html>",
                          ("photo", f"{PHOTO}2111_2_Image1.jpg"): b""}
        out = []
        for workers in (1, 2, 8, 16, "auto"):
            d = os.path.join(self.d, str(workers))
            portal = FakePortal(faults())
            stats = collect(JsjbAdapter(portal), Store(d, "jsjb"), ALL, sleep=lambda s: None, photo_workers=workers)
            self.assertEqual((exit_code(stats), stats["photos_downloaded"], stats["photos_failed"]), (5, 10, 2))
            self.assertEqual(len(portal.photo_calls), 12)
            out.append(self.saved(d) + (self.files(d),))
        for other in out[1:]:
            self.assertEqual(out[0], other)
        recs, index, _ = out[1]
        self.assertNotIn(f"{PHOTO}1112_1_Image1.jpg", index)

    def test_ctrl_c_during_side_by_side_downloads(self):
        portal = FakePortal({("photo", f"{PHOTO}1121_2_Image1.jpg"): KeyboardInterrupt()})
        stats = collect(JsjbAdapter(portal), Store(self.d, "jsjb"), ALL, sleep=lambda s: None, photo_workers=2)
        self.assertEqual(stats["stop"][0], "interrupted")
        recs, _ = self.saved(self.d)
        self.assertEqual({k[:4] for k in recs}, {"1111", "1112"})

    def test_photo_clients_keep_the_delay_and_robots_rules(self):
        c = Client(delay=1.5)
        c._robots["https://jsjb.mowr.gov.in"] = "rules"
        workers = c.photo_clients(2)
        self.assertEqual([w.delay for w in workers], [1.5, 1.5])
        self.assertTrue(all(w._robots is c._robots and w is not c for w in workers))
        self.assertIsNot(workers[0].cookies, workers[1].cookies)

    def test_photo_requests_do_not_move_the_page_delay(self):
        c = Client(delay=1.0)
        h = "jsjb.mowr.gov.in"
        c._last[h] = 1000.0  # the search page went out at t=1000
        workers = c.photo_clients(2)
        workers[0]._last[h], workers[1]._last[h] = 1003.5, 1004.0
        workers[0].requests, workers[1].requests = 4, 4
        c.photos_done(workers)
        # The next page waits for the last page only; the requests are still counted.
        self.assertEqual((c._last[h], c.requests), (1000.0, 8))


class SameCodeTwoPlaces(unittest.TestCase):
    """Ward codes share the number range of village codes (Leh wards 10856 to 10864)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")
        self.village = Area(state="PUNJAB", district="Sangrur", area_type="Rural", block="B", village="V",
                            village_code="10856")
        self.ward = Area(state="LADAKH", district="Leh Ladakh", area_type="Urban", block="Leh", village="Ward 1",
                         village_code="10856")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_ward_and_a_village_with_one_code_are_both_collected_and_kept(self):
        areas = [self.village, self.ward]

        class Adapter:
            client = None

            def list_areas(self, filters):
                return iter(areas)

            def list_records(self, area):
                return []

            def list_photos(self, rec):
                return []
        stats = collect(Adapter(), Store(self.d, "jsjb"), ALL, since_days=30, sleep=lambda s: None)
        self.assertEqual((stats["villages"], stats["not_due"]), (2, 0))
        cov = load_coverage(os.path.dirname(self.d), "jsjb")
        self.assertEqual(sorted((c["state"], c["area_type"]) for c in cov), [("LADAKH", "Urban"), ("PUNJAB", "Rural")])
        stats = collect(Adapter(), Store(self.d, "jsjb"), ALL, since_days=30, sleep=lambda s: None)
        self.assertEqual((stats["villages"], stats["not_due"]), (0, 2))

    def test_entries_written_before_the_key_was_stored_still_count(self):
        path = os.path.join(self.d, "records", "jsjb.coverage.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"state": "Punjab", "district": "Sangrur", "area_type": "Rural", "block": "B", "village": "V",
                        "village_code": "10856", "records": 3, "collected_at": st.now_iso()}], f)
        store = Store(self.d, "jsjb")
        self.assertIsNotNone(store.last_collected(self.village))
        self.assertIsNone(store.last_collected(self.ward))

    def test_record_names_spelled_unlike_the_dropdown(self):
        store = Store(self.d, "jsjb")
        store.commit(self.ward, [{"record_id": "1", "state": "Ladakh UT", "area_type": "Urban Local Body",
                                  "village_code": "10856", "photos": []}], st.now_iso())
        store.flush()
        again = Store(self.d, "jsjb")
        self.assertIsNotNone(again.last_collected(self.ward))
        self.assertIsNone(again.last_collected(self.village))


if __name__ == "__main__":
    unittest.main(verbosity=1)
