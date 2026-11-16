"""The queue: order, resume, failed areas retried at the end, dry run. Offline, with a fake portal.
Run: python tests/collector/test_queue.py"""
import json
import logging
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)

from collector.__main__ import collect  # noqa: E402
from collector.http import FetchError  # noqa: E402
from collector.queue_runner import QueueState, order_units, resolve, run_queue  # noqa: E402
from collector.schemes.jsjb import JsjbAdapter  # noqa: E402
from collector.store import DataInUse, DataLock, Store  # noqa: E402
from test_resilience import JPEG, form, results, row  # noqa: E402

logging.disable(logging.CRITICAL)

STATES = [("1", "PUNJAB"), ("2", "HARYANA"), ("3", "CHANDIGARH")]
DISTRICTS = {"1": [("11", "Amritsar"), ("12", "Sangrur")], "2": [("21", "Ambala"), ("22", "Panchkula")],
             "3": [("31", "Chandigarh")]}
AREAS = [("3", "Rural"), ("4", "Urban")]


def blocks(d, a):
    return [] if (d, a) == ("31", "3") else [(f"{d}{a}", f"Block {d}-{a}")]


def villages(b):
    return [(f"{b}{i}", f"V{b}-{i}") for i in (1, 2)]


class Portal:
    """Three states; every district has one block per area type with two villages, except
    Chandigarh, which lists no rural blocks. down = district codes whose block list fails."""

    def __init__(self, down=()):
        self.down = set(down)
        self.searches = []

    def new_session(self):
        pass

    def fetch_image(self, url, max_bytes):
        return JPEG + url.encode(), "image/jpeg"

    def fetch(self, url, data=None, referer=None):
        if data is None:
            return form({}, {"ddlState": STATES}).encode()
        t = data.get("__EVENTTARGET")
        st, d, a, b, v = (data.get(k) for k in ("ddlState", "ddlDistrict", "ddlArea", "ddlBlockTown", "ddlVillageWard"))
        opts = {"ddlState": STATES, "ddlDistrict": DISTRICTS[st], "ddlArea": AREAS}
        if t == "ddlState":
            return form({"ddlState": st}, opts).encode()
        if d in self.down:
            raise FetchError("timed out")
        sel = {"ddlState": st, "ddlDistrict": d, "ddlArea": a}
        opts["ddlBlockTown"] = blocks(d, a)
        if t == "ddlArea":
            return form(sel, opts).encode()
        sel["ddlBlockTown"] = b
        opts["ddlVillageWard"] = villages(b)
        if t == "ddlBlockTown":
            return form(sel, opts).encode()
        sel["ddlVillageWard"] = v
        self.searches.append(v)
        return form(sel, opts, results([row(v, v, [f"https://jsjb.mowr.gov.in/P/{v}_Image1.jpg"])])).encode()


PLAN = {"units": [
    {"phase": "punjab", "state": "Punjab", "area": "Rural"},
    {"phase": "punjab", "state": "Punjab", "area": "Urban"},
    {"phase": "capitals", "state": "CHANDIGARH", "district": "Chandigarh", "area": ["Urban", "Rural"], "note": "Punjab"},
    {"phase": "capitals", "state": "HARYANA", "district": "Panchkula", "area": "Urban"},
    {"phase": "capitals", "state": "CHANDIGARH", "district": "Chandigarh", "area": ["Urban", "Rural"], "note": "again"},
    {"phase": "rest", "state": "PUNJAB", "district": "remaining"},
    {"phase": "rest", "state": "HARYANA", "district": "remaining"},
    {"phase": "rest", "state": "CHANDIGARH", "district": "remaining"},
]}

ORDER = ["punjab|*|rural", "punjab|*|urban", "chandigarh|chandigarh|urban", "chandigarh|chandigarh|rural",
         "haryana|panchkula|urban", "haryana|ambala|rural", "haryana|ambala|urban", "haryana|panchkula|rural"]


class Q(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")
        self.calls, self.checks, self.out, self.slept = [], [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, portal=None, fail=None, **kw):
        """fail(key, n) -> stats to return instead of collecting, or None. n counts calls for that key."""
        portal = portal or Portal()
        seen = {}

        def collect_fn(adapter, store, filters, **ckw):
            key = "|".join((filters[k] or "*").casefold() for k in ("state", "district", "area"))
            self.calls.append(key)
            seen[key] = seen.get(key, 0) + 1
            forced = fail(key, seen[key]) if fail else None
            if forced:
                return dict(base_stats(), **forced)
            return collect(adapter, store, filters, sleep=lambda s: None, **ckw)

        store = Store(self.d, "jsjb")
        code = run_queue(PLAN, JsjbAdapter(portal), store, self.d, since_days=30, check=self.checks.append,
                         out=self.out.append, sleep=self.slept.append, collect_fn=collect_fn, **kw)
        return code, QueueState(self.d)

    def test_order_skips_repeats_and_areas_already_planned(self):
        state = QueueState(self.d)
        units, problems = resolve(PLAN, lambda s: [n for c, n in DISTRICTS[dict((t, c) for c, t in STATES)[s.upper()]]])
        self.assertEqual([u["key"] for u in units], ORDER)
        self.assertEqual(problems, [])
        units, _ = resolve(PLAN, lambda s: [], only_phases=["capitals"])
        self.assertEqual([u["phase"] for u in units], ["capitals"] * 3)
        self.assertEqual(state.data["units"], {})

    def test_full_run_records_progress_and_checks_after_each_area(self):
        code, state = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ORDER)
        statuses = {k: v["status"] for k, v in state.data["units"].items()}
        self.assertEqual(set(statuses.values()), {"done"})
        self.assertTrue(state.unit("chandigarh|chandigarh|rural").get("empty"))
        # The empty area saved no village, so the checker ran once per area that did.
        self.assertEqual(len(self.checks), len(ORDER) - 1)
        with open(os.path.join(self.d, "records", "jsjb.coverage.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 2 * 2 * 2 + 2 * 1 + 2 * 4)

    def test_rerun_skips_areas_finished_recently(self):
        self.go()
        self.calls.clear()
        code, _ = self.go()
        self.assertEqual((code, self.calls), (0, []))
        self.assertIn("plan progress: 8 of 8", "\n".join(self.out))

    def test_stop_and_resume_where_it_stopped(self):
        portal = Portal()
        code, state = self.go(portal, fail=lambda k, n: {"stop": ("interrupted", "Ctrl+C")} if k == ORDER[2] else None)
        self.assertEqual(code, 130)
        self.assertEqual(self.calls, ORDER[:3])
        self.assertEqual(state.unit(ORDER[2])["status"], "stopped")
        self.calls.clear()
        code, state = self.go(portal)
        self.assertEqual((code, self.calls), (0, ORDER[2:]))

    def test_time_budget_stops_between_areas_and_resumes(self):
        t = [0.0]

        def clock():
            t[0] += 60 * 30  # every look at the clock is half an hour later
            return t[0]
        code, state = self.go(max_minutes=100, clock=clock)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ORDER[:2])
        self.assertEqual(state.unit(ORDER[1])["status"], "partial")
        self.calls.clear()
        self.go()
        self.assertEqual(self.calls, ORDER[1:])

    def test_failed_area_recorded_then_retried_at_the_end(self):
        def fail(key, n):
            if key == ORDER[1] and n == 1:
                return {"stop": ("network", "portal down for 12 pauses")}
        code, state = self.go(fail=fail)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ORDER + [ORDER[1]])
        self.assertEqual(self.slept, [1800])
        self.assertEqual(state.unit(ORDER[1])["status"], "done")
        self.assertEqual(state.unit(ORDER[1])["attempts"], 2)

    def test_area_failing_twice_is_reported_and_retried_next_run(self):
        code, state = self.go(fail=lambda k, n: {"stop": ("filter", "district not found")} if k == ORDER[0] else None)
        self.assertEqual(code, 5)
        self.assertEqual(self.calls, ORDER + [ORDER[0]])
        self.assertEqual(state.unit(ORDER[0])["status"], "failed")
        self.assertIn("FAILED (exit 3) Punjab > all districts > Rural: district not found", "\n".join(self.out))
        self.calls.clear()
        self.go()
        self.assertEqual(self.calls, [ORDER[0]])

    def test_skipped_lists_are_retried_at_the_end_of_the_pass(self):
        code, state = self.go(Portal(down={"22"}))
        self.assertEqual(code, 5)
        self.assertIn("STILL SKIPPED in HARYANA > Panchkula > Urban", "\n".join(self.out))
        self.assertEqual(self.calls, ORDER + [ORDER[4], ORDER[7]])
        self.assertEqual(state.unit(ORDER[4])["status"], "incomplete")
        self.assertEqual(state.unit(ORDER[4])["skipped"], 1)
        self.calls.clear()
        code, state = self.go(Portal())
        self.assertEqual((code, self.calls), (0, [ORDER[4], ORDER[7]]))
        self.assertEqual(state.unit(ORDER[4])["status"], "done")

    def test_photo_failing_twice_is_fetched_on_the_next_run(self):
        bad = "https://jsjb.mowr.gov.in/P/1131_Image1.jpg"

        class Flaky(Portal):
            broken = True

            def fetch_image(self, url, max_bytes):
                if url == bad and self.broken:
                    raise FetchError("HTTP 503", 503)
                return super().fetch_image(url, max_bytes)
        portal = Flaky()
        code, state = self.go(portal)
        self.assertEqual(code, 5)
        self.assertEqual(state.unit(ORDER[0])["status"], "incomplete")
        self.assertIn("Run the same command again to retry what failed.", "\n".join(self.out))
        portal.broken = False
        portal.searches.clear()
        self.calls.clear()
        code, state = self.go(portal)
        self.assertEqual((code, self.calls, portal.searches), (0, [ORDER[0]], ["1131"]))
        self.assertIn(bad, Store(self.d, "jsjb").photo_index)

    def test_outage_wait_never_runs_past_the_time_budget(self):
        t = [0.0]
        code, state = self.go(fail=lambda k, n: {"stop": ("network", "down")} if k == ORDER[0] else None,
                              max_minutes=5, clock=lambda: t[0])
        self.assertEqual((code, self.calls, self.slept), (0, [ORDER[0]], []))
        self.assertIn("--max-minutes 5 reached", "\n".join(self.out))

    def test_no_outage_wait_after_the_last_area(self):
        code, _ = self.go(fail=lambda k, n: {"stop": ("network", "down")} if k == ORDER[-1] else None)
        self.assertEqual(code, 5)
        # One wait before the retry of the last area, none after it.
        self.assertEqual((self.calls[-2:], self.slept), ([ORDER[-1], ORDER[-1]], [1800]))

    def test_areas_never_collected_come_before_refreshes(self):
        state = QueueState(self.d)
        units, _ = resolve(PLAN, lambda s: [n for c, n in DISTRICTS[dict((t, c) for c, t in STATES)[s.upper()]]])
        state.data["units"] = {ORDER[0]: {"status": "done", "finished_at": "2026-01-01T00:00:00+00:00"},
                               ORDER[1]: {"status": "failed"}, ORDER[2]: {"status": "partial"},
                               ORDER[3]: {"status": "incomplete"}}
        self.assertEqual([u["key"] for u in order_units(units, state)],
                         [ORDER[2]] + ORDER[4:] + [ORDER[1], ORDER[3], ORDER[0]])

    def test_dry_run_leaves_a_damaged_queue_file_alone(self):
        path = os.path.join(self.d, "state", "queue.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"units": {"punj')
        code = run_queue(PLAN, JsjbAdapter(Portal()), None, self.d, dry_run=True, out=self.out.append)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["queue.json"])

    def test_queue_file_cut_off_during_an_in_place_write_is_finished(self):
        path = os.path.join(self.d, "state", "queue.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"units": {"punj')
        with open(path + ".inplace", "w", encoding="utf-8") as f:
            json.dump({"units": {ORDER[0]: {"status": "done"}}, "districts": {}}, f)
        self.assertEqual(QueueState(self.d, read_only=True).unit(ORDER[0]), {"status": "done"})
        self.assertTrue(os.path.exists(path + ".inplace"))
        self.assertEqual(QueueState(self.d).unit(ORDER[0]), {"status": "done"})
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["queue.json"])

    def test_layout_changed_twice_in_a_row_stops_the_queue(self):
        code, _ = self.go(fail=lambda k, n: {"stop": ("layout", "columns changed")})
        self.assertEqual((code, self.calls), (2, ORDER[:2]))

    def test_disk_trouble_stops_the_queue(self):
        code, _ = self.go(fail=lambda k, n: {"stop": ("disk", "disk is full")})
        self.assertEqual((code, self.calls), (6, ORDER[:1]))

    def test_many_empty_areas_in_a_row_stop_the_queue(self):
        code, _ = self.go(fail=lambda k, n: {"stop": ("layout", "every list empty"), "nothing_listed": True},
                          max_empty_in_a_row=3)
        self.assertEqual((code, self.calls), (2, ORDER[:3]))

    def test_dry_run_prints_order_and_collects_nothing(self):
        self.go(fail=lambda k, n: {"stop": ("interrupted", "Ctrl+C")} if k == ORDER[3] else None)
        self.calls.clear()
        self.out.clear()
        path = os.path.join(self.d, "state", "queue.json")
        with open(path, "rb") as f:
            before = f.read()
        code, _ = self.go(dry_run=True)
        self.assertEqual((code, self.calls), (0, []))
        text = "\n".join(self.out)
        self.assertIn("Plan: 8 units, 3 finished in the last 30 days, 5 to do", text)
        # Areas not finished yet come first; finished ones go to the end.
        self.assertIn("1. [capitals] CHANDIGARH > Chandigarh > Rural: to do (last: stopped", text)
        self.assertIn("6. [punjab] Punjab > all districts > Rural: done", text)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_district_list_unreadable_at_start_is_tried_again(self):
        class Flaky(Portal):
            def __init__(self):
                super().__init__()
                self.fails = 3  # the HARYANA district list, three fresh sessions in a row

            def fetch(self, url, data=None, referer=None):
                if data and data.get("__EVENTTARGET") == "ddlState" and data["ddlState"] == "2" and self.fails:
                    self.fails -= 1
                    raise FetchError("timed out")
                return super().fetch(url, data, referer)
        code, _ = self.go(Flaky())
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[-3:], ["haryana|ambala|rural", "haryana|ambala|urban", "haryana|panchkula|rural"])

    def test_unexpected_error_in_one_area_does_not_end_the_queue(self):
        def fail(key, n):
            if key == ORDER[0] and n == 1:
                raise RuntimeError("bug")
        code, state = self.go(fail=fail)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ORDER + [ORDER[0]])
        self.assertEqual(state.unit(ORDER[0])["status"], "done")

    def test_one_writer_per_data_folder(self):
        lock = DataLock(self.d)
        try:
            with self.assertRaises(DataInUse):
                DataLock(self.d)
        finally:
            lock.release()
        DataLock(self.d).release()


def base_stats():
    return {"villages": 0, "not_due": 0, "records": 0, "new_records": 0, "photos_downloaded": 0,
            "photos_failed": 0, "photos_missing": 0, "skipped_villages": [], "skipped_units": [],
            "stopped": None, "stop": None, "store_problems": [], "nothing_listed": False}


if __name__ == "__main__":
    unittest.main(verbosity=1)
