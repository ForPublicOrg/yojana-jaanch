"""One bad item must not end or corrupt a long run. Offline, with a fake portal.
Run: python tests/collector/test_resilience.py"""
import contextlib
import errno
import html
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import collector.__main__ as runner  # noqa: E402
from collector.__main__ import collect, exit_code  # noqa: E402
from collector.http import FetchError  # noqa: E402
from collector.schemes.jsjb import COLUMNS, DROPDOWNS, JsjbAdapter  # noqa: E402
from collector.store import Store, StoreError  # noqa: E402

logging.disable(logging.CRITICAL)

PHOTO = "https://jsjb.mowr.gov.in/PhotoJSJB2/3/1/"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
WHOLE_JPEG = JPEG + b"\xff\xd9"
STATES = [("1", "Alpha"), ("2", "Beta")]
DISTRICTS = {"1": [("11", "A-One")], "2": [("21", "B-One")]}
BLOCKS = {"11": [("111", "A-Block1"), ("112", "A-Block2")], "21": [("211", "B-Block1")]}
VILLAGES = {b: [(f"{b}{i}", f"{b}-V{i}") for i in (1, 2)] for bl in BLOCKS.values() for b, _ in bl}


def form(sel, options, body=""):
    out = ['<html><body><form><input type="hidden" name="__VIEWSTATE" value="vs">']
    for ddl in DROPDOWNS:
        out.append(f'<select id="{ddl}" name="{ddl}"><option value="0">-- Select --</option>')
        for v, t in options.get(ddl, []):
            s = ' selected="selected"' if sel.get(ddl) == v else ""
            out.append(f'<option{s} value="{v}">{html.escape(t)}</option>')
        out.append("</select>")
    return "".join(out) + body + "</form></body></html>"


def row(rid, vcode, photos, **over):
    cells = [f"{rid} ({rid}00)", "Alpha", "A-One", "Rural", "Block : X", "Village : Y", "", "Work",
             "Farm pond", "30.1", "75.2", "Completed", "01/04/2026", "05/04/2026", "Others", "BDPO X", None, ""]
    for k, v in over.items():
        cells[COLUMNS.index(k)] = v
    links = "".join(f'<a class="img-view" data-url="{u}" href="#">img-{i}</a>' for i, u in enumerate(photos, 1))
    cells[16] = links
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def results(rows, total=None):
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in COLUMNS) + "</tr>"
    total = len(rows) if total is None else total
    return f'<table id="gvData">{head}{"".join(rows)}</table><label id="Label8">Total : {total}</label>'


class FakePortal:
    """Three blocks, two villages each, two records per village, one photo per record.

    faults maps a key to a list of behaviours used one per request (then normal again), or to
    a single behaviour used every time. Keys: ("districts", state), ("blocks", district),
    ("villages", block), ("search", village), ("photo", url), "start". A behaviour is an
    exception to raise, "home" (start page), "error" (error page), or text/bytes to return."""

    def __init__(self, faults=None):
        self.faults = faults or {}
        self.calls = {}
        self.sessions = 0
        self.photo_calls = []
        self.requests = 0

    def new_session(self):
        self.sessions += 1

    def _fault(self, key):
        self.calls[key] = self.calls.get(key, 0) + 1
        f = self.faults.get(key)
        if isinstance(f, list):
            return f.pop(0) if f else None
        return f

    def _apply(self, f, normal):
        if f is None:
            return normal
        if isinstance(f, BaseException):
            raise f
        if callable(f):
            return f()
        if f == "home":
            return self._start()
        if f == "error":
            return "<html><body><h1>Server Error in '/' Application.</h1></body></html>"
        return f

    def _start(self):
        return form({}, {"ddlState": STATES})

    def photo_bytes(self, url):
        return JPEG + url.encode()

    def fetch_image(self, url, max_bytes):
        self.photo_calls.append(url)
        body = self._apply(self._fault(("photo", url)), None)
        if body is None:
            return self.photo_bytes(url), "image/jpeg"
        if isinstance(body, tuple):
            return body
        return (body.encode() if isinstance(body, str) else body), "text/html"

    def fetch(self, url, data=None, referer=None):
        self.requests += 1
        out = self._page(data)
        return out.encode() if isinstance(out, str) else out

    def _page(self, data):
        if data is None:
            return self._apply(self._fault("start"), self._start())
        t = data.get("__EVENTTARGET")
        st, d, b, v = (data.get(k) for k in ("ddlState", "ddlDistrict", "ddlBlockTown", "ddlVillageWard"))
        area = [("1", "Rural")]
        if t == "ddlState":
            return self._apply(self._fault(("districts", st)),
                               form({"ddlState": st}, {"ddlState": STATES, "ddlDistrict": DISTRICTS[st],
                                                       "ddlArea": area}))
        sel = {"ddlState": st, "ddlDistrict": d, "ddlArea": "1"}
        opts = {"ddlState": STATES, "ddlDistrict": DISTRICTS[st], "ddlArea": area, "ddlBlockTown": BLOCKS[d]}
        if t == "ddlArea":
            return self._apply(self._fault(("blocks", d)), form(sel, opts))
        sel["ddlBlockTown"] = b
        opts["ddlVillageWard"] = VILLAGES[b]
        if t == "ddlBlockTown":
            return self._apply(self._fault(("villages", b)), form(sel, opts))
        if data.get("btnSearch"):
            sel["ddlVillageWard"] = v
            # The portal writes the selected state in each row, in its own case.
            rows = [row(f"{v}{i}", v, [f"{PHOTO}{v}_{i}_Image1.jpg"], State=dict(STATES)[st].upper()) for i in (1, 2)]
            return self._apply(self._fault(("search", v)), form(sel, opts, results(rows)))
        raise AssertionError(f"unexpected post {t}")


def village_page(v, rows, total=None):
    b = v[:3]
    d = {"111": "11", "112": "11", "211": "21"}[b]
    st = d[0]
    sel = {"ddlState": st, "ddlDistrict": d, "ddlArea": "1", "ddlBlockTown": b, "ddlVillageWard": v}
    opts = {"ddlState": STATES, "ddlDistrict": DISTRICTS[st], "ddlArea": [("1", "Rural")],
            "ddlBlockTown": BLOCKS[d], "ddlVillageWard": VILLAGES[b]}
    return form(sel, opts, results(rows, total))


ALL = {k: None for k in ("state", "district", "area", "block", "village")}
ALL_VILLAGES = [v for vs in VILLAGES.values() for v, _ in vs]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, portal, **kw):
        return collect(JsjbAdapter(portal), Store(self.d, "jsjb"), ALL, sleep=lambda s: None, **kw)

    def saved(self):
        with open(os.path.join(self.d, "records", "jsjb.jsonl"), encoding="utf-8") as f:
            recs = [json.loads(l) for l in f]
        with open(os.path.join(self.d, "records", "jsjb.coverage.json"), encoding="utf-8") as f:
            cov = {c["village_code"] for c in json.load(f)}
        return recs, cov

    def index(self):
        with open(os.path.join(self.d, "state", "jsjb.photo_index.json"), encoding="utf-8") as f:
            return json.load(f)


class Listings(Base):
    def test_clean_run(self):
        stats = self.run_once(FakePortal())
        recs, cov = self.saved()
        self.assertEqual(cov, set(ALL_VILLAGES))
        self.assertEqual(len(recs), 12)
        self.assertEqual((exit_code(stats), stats["photos_downloaded"]), (0, 12))

    def test_failed_lists_are_retried_then_skipped(self):
        for key, lost in ((("districts", "1"), {"1111", "1112", "1121", "1122"}),
                          (("blocks", "11"), {"1111", "1112", "1121", "1122"}),
                          (("villages", "111"), {"1111", "1112"})):
            with self.subTest(key):
                self.tearDown()
                self.setUp()
                portal = FakePortal({key: FetchError("could not fetch: timed out")})
                stats = self.run_once(portal)
                _, cov = self.saved()
                self.assertEqual(cov, set(ALL_VILLAGES) - lost)
                self.assertEqual([(u["level"], u["reason"]) for u in stats["skipped_units"]],
                                 [(key[0], "network")])
                self.assertEqual(portal.calls[key], 1 + JsjbAdapter.session_retries)
                self.assertEqual(exit_code(stats), 5)
                self.assertIsNone(stats["stop"])

    def test_list_recovers_on_retry(self):
        portal = FakePortal({("blocks", "11"): [FetchError("reset"), "error"]})
        stats = self.run_once(portal)
        self.assertEqual((stats["skipped_units"], stats["villages"]), ([], 6))

    def test_start_page_without_dropdowns_stops(self):
        stats = self.run_once(FakePortal({"start": "<html>Under maintenance</html>"}))
        self.assertEqual(stats["stop"][0], "layout")
        self.assertIn("start page has no dropdowns", stats["stop"][1])
        self.assertEqual(exit_code(stats), 2)

    def test_network_outage_pauses_then_stops(self):
        portal = FakePortal({("search", v): FetchError("down") for v in ALL_VILLAGES})
        slept = []
        stats = collect(JsjbAdapter(portal), Store(self.d, "jsjb"), ALL, max_network_failures=2,
                        max_outage_pauses=1, outage_pause=7, sleep=slept.append)
        self.assertEqual(slept, [7])
        self.assertEqual((stats["stop"][0], exit_code(stats)), ("network", 4))


class BadVillagePages(Base):
    def test_broken_page_skips_the_village(self):
        p = f"{PHOTO}x_Image1.jpg"
        good = row("90", "1111", [p])
        pages = {
            "wrong cell count": village_page("1111", [good, row("91", "1111", [p]).replace("<td>Others</td>", "")]),
            "total mismatch": village_page("1111", [good], total=5),
            "duplicate id": village_page("1111", [good, good]),
            "same bad date in every row": village_page(
                "1111", [row(str(i), "1111", [p], **{"Work Start Date": "Monday"}) for i in (1, 2, 3)]),
        }
        for label, page in pages.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                stats = self.run_once(FakePortal({("search", "1111"): page}))
                recs, cov = self.saved()
                self.assertFalse([r for r in recs if r["village_code"] == "1111"])
                self.assertEqual(cov, set(ALL_VILLAGES) - {"1111"})
                self.assertEqual([v["reason"] for v in stats["skipped_villages"]], ["layout"])
                self.assertIsNone(stats["stop"])

    def test_one_bad_cell_keeps_the_village(self):
        p = f"{PHOTO}x_Image1.jpg"
        good = row("90", "1111", [p])
        variants = {
            "missing photo link attribute": (row("92", "1111", [p]).replace("data-url=", "data-src="), "photos"),
            "odd date": (row("93", "1111", [p], **{"Work Start Date": "31/02/2026"}), "start_date"),
            "text in date": (row("94", "1111", [p], **{"Date Of Completion": "Completed"}), "end_date"),
            "odd coordinate": (row("95", "1111", [p], Latitude="NaN"), "lat"),
            "coordinate out of range": (row("96", "1111", [p], Longitude="975.2"), "lon"),
            "unreadable id": (row("97", "1111", [p], **{"S. No (Id)": "no id here"}), None),
        }
        for label, (bad, field) in variants.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                stats = self.run_once(FakePortal({("search", "1111"): village_page("1111", [good, bad])}))
                recs, cov = self.saved()
                mine = {r["record_id"]: r for r in recs if r["village_code"] == "1111"}
                self.assertIn("1111", cov)
                self.assertEqual(stats["skipped_villages"], [])
                self.assertIn("9000", mine)
                if field is None:
                    self.assertEqual(len(mine), 1)
                else:
                    odd = mine[bad.split("(")[1].split(")")[0]]
                    self.assertIn(field, odd["unreadable"])
                    if field != "photos":
                        self.assertIsNone(odd[field])

    def test_layout_failures_in_a_row_stop_the_run(self):
        broken = village_page("1111", [row("1", "1111", [])], total=9)
        portal = FakePortal({("search", v): broken for v in ALL_VILLAGES})
        stats = self.run_once(portal, max_layout_failures=3)
        self.assertEqual(len(stats["skipped_villages"]), 3)
        self.assertEqual((stats["stop"][0], exit_code(stats)), ("layout", 2))
        self.assertIn("in a row", stats["stop"][1])

    def test_good_village_resets_the_count(self):
        broken = village_page("1111", [row("1", "1111", [])], total=9)
        bad = {v: broken for v in ("1111", "1112", "1121", "2111")}
        portal = FakePortal({("search", v): p for v, p in bad.items()})
        stats = self.run_once(portal, max_layout_failures=4)
        self.assertIsNone(stats["stop"])
        self.assertEqual(len(stats["skipped_villages"]), 4)

    def test_unexpected_exception_skips_village(self):
        def boom():
            raise KeyError("surprise")
        stats = self.run_once(FakePortal({("search", "1111"): boom}))
        self.assertEqual([v["reason"] for v in stats["skipped_villages"]], ["error"])
        self.assertEqual(stats["villages"], 5)


class Sessions(Base):
    def test_home_or_error_page_gets_a_fresh_session(self):
        for fault in ("home", "error"):
            with self.subTest(fault):
                self.tearDown()
                self.setUp()
                portal = FakePortal({("search", "1112"): [fault]})
                stats = self.run_once(portal)
                _, cov = self.saved()
                self.assertIn("1112", cov)
                self.assertEqual(stats["skipped_villages"], [])
                self.assertEqual(portal.sessions, 1)

    def test_lost_session_mid_list(self):
        portal = FakePortal({("villages", "112"): ["home"]})
        stats = self.run_once(portal)
        self.assertEqual((stats["skipped_units"], stats["villages"]), ([], 6))

    def test_session_never_recovers_skips_only_that_village(self):
        portal = FakePortal({("search", "1112"): "home"})
        stats = self.run_once(portal)
        self.assertEqual([v["village_code"] for v in stats["skipped_villages"]], ["1112"])
        self.assertEqual(stats["villages"], 5)


class Photos(Base):
    def test_bad_photos_not_stored_and_retried(self):
        url = lambda v, i=1: f"{PHOTO}{v}_{i}_Image1.jpg"  # noqa: E731
        faults = {
            ("photo", url("1111")): b"",
            ("photo", url("1112")): b"<!DOCTYPE html><html><body>Not found</body></html>",
            ("photo", url("1121")): (b"<html>oops</html>", "image/jpeg"),
            ("photo", url("1122")): (b"PK\x03\x04 zip file", "application/octet-stream"),
            ("photo", url("2111")): (JPEG + b"x" * 5000, "image/jpeg"),
            ("photo", url("2112")): FetchError("could not fetch: HTTP 302 redirect loop", 302),
            ("photo", url("1111", 2)): FetchError("could not fetch: timed out"),
        }
        portal = FakePortal(dict(faults))
        stats = self.run_once(portal, max_photo_bytes=4000)
        self.assertEqual(stats["photos_failed"], 7)
        self.assertEqual(stats["villages"], 6)
        idx = self.index()
        for _, u in faults:
            self.assertNotIn(u, idx)
        recs, _ = self.saved()
        bad = {u for _, u in faults}
        for r in recs:
            for p in r["photos"]:
                self.assertEqual(p["sha256"] is None, p["url"] in bad)
        stored = [f for _, _, fs in os.walk(os.path.join(self.d, "photos")) for f in fs]
        self.assertEqual(len(stored), 12 - 7)
        self.assertEqual(exit_code(stats), 5)

        healed = FakePortal()
        stats = self.run_once(healed)
        self.assertEqual(sorted(healed.photo_calls), sorted(bad))
        self.assertEqual((stats["photos_downloaded"], exit_code(stats)), (7, 0))

    def test_duplicate_url_fetched_once(self):
        shared = f"{PHOTO}shared_Image1.jpg"
        page = village_page("1111", [row("1", "1111", [shared]), row("2", "1111", [shared, shared])])
        portal = FakePortal({("search", "1111"): page, ("photo", shared): FetchError("timed out")})
        stats = self.run_once(portal)
        self.assertEqual(portal.photo_calls.count(shared), 1)
        self.assertEqual(stats["photos_failed"], 1)

    def test_odd_filename(self):
        odd = PHOTO + "फोटो 1 (copy)#?.JPG"
        page = village_page("1111", [row("1", "1111", [html.escape(odd)])])
        portal = FakePortal({("search", "1111"): page})
        self.run_once(portal)
        entry = self.index()[odd]
        self.assertTrue(entry["file"].endswith(entry["sha256"] + ".jpg"))

    def test_pruned_photo_file_is_not_downloaded_again(self):
        """The checker deletes files of photos in no case; their sha256 is enough from then on."""
        self.run_once(FakePortal())
        entry = next(iter(self.index().values()))
        os.remove(entry["file"])
        portal = FakePortal()
        self.run_once(portal, since_days=0)
        self.assertTrue(any(k[0] == "search" for k in portal.calls if isinstance(k, tuple)), portal.calls)
        self.assertEqual(portal.photo_calls, [])
        self.assertEqual(next(iter(self.index().values()))["sha256"], entry["sha256"])


class LocalFiles(Base):
    def test_damaged_files_from_a_crash(self):
        self.run_once(FakePortal())
        rec_path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(rec_path, "a", encoding="utf-8") as f:
            f.write('{"record_id": "half-writ')
        with open(rec_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines.insert(2, "[1, 2]")
        with open(rec_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        for name in (os.path.join("records", "jsjb.coverage.json"), os.path.join("state", "jsjb.photo_index.json")):
            with open(os.path.join(self.d, name), "w", encoding="utf-8") as f:
                f.write('{"truncated": ')
        store = Store(self.d, "jsjb")
        self.assertEqual(len(store.records), 12)
        self.assertEqual({c["village_code"] for c in store.coverage.values()}, set(ALL_VILLAGES))
        self.assertEqual(len(store.photo_index), 12)
        text = " ".join(store.problems)
        self.assertIn("skipped 2 damaged line", text)
        self.assertTrue(os.path.exists(rec_path + ".damaged-lines"))

        portal = FakePortal()
        stats = collect(JsjbAdapter(portal), store, ALL, since_days=1)
        self.assertEqual((stats["not_due"], portal.photo_calls), (6, []))
        recs, _ = self.saved()
        self.assertEqual(len(recs), 12)

    def test_bad_coverage_entries_dropped(self):
        self.run_once(FakePortal())
        path = os.path.join(self.d, "records", "jsjb.coverage.json")
        with open(path, encoding="utf-8") as f:
            cov = json.load(f)
        cov[0]["collected_at"] = "yesterday"
        cov.append("junk")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cov, f)
        store = Store(self.d, "jsjb")
        self.assertEqual(len(store.coverage), 5)

    def test_disk_full_keeps_old_files(self):
        self.run_once(FakePortal())
        rec_path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(rec_path, "rb") as f:
            before = f.read()
        import collector.store as st
        real = st.os.replace

        def full(src, dst):
            raise OSError(errno.ENOSPC, "No space left on device")

        def full_append(path, mode="r", *a, **kw):
            if "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return open(path, mode, *a, **kw)
        st.os.replace, st.open = full, full_append
        try:
            stats = collect(JsjbAdapter(FakePortal()), Store(self.d, "jsjb"), ALL, flush_every=1)
        finally:
            st.os.replace = real
            del st.open
        self.assertEqual((stats["stop"][0], exit_code(stats)), ("disk", 6))
        self.assertIn("disk is full", stats["stop"][1])
        with open(rec_path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(rec_path + ".tmp"))

    def test_permission_error_message(self):
        import collector.store as st
        real = st.os.replace

        def denied(src, dst):
            raise PermissionError(errno.EACCES, "Access is denied")
        st.os.replace, st.time.sleep = denied, lambda s: None
        try:
            with self.assertRaises(StoreError) as cm:
                Store(self.d, "jsjb").flush()
        finally:
            st.os.replace = real
            import time
            st.time.sleep = time.sleep
        self.assertIn("no permission", str(cm.exception))


class Interrupt(Base):
    def test_ctrl_c_mid_village_leaves_files_consistent(self):
        portal = FakePortal({("photo", f"{PHOTO}1121_2_Image1.jpg"): KeyboardInterrupt()})
        stats = self.run_once(portal)
        self.assertEqual((stats["stop"][0], exit_code(stats)), ("interrupted", 130))
        recs, cov = self.saved()
        self.assertEqual(cov, {"1111", "1112"})
        self.assertEqual({r["village_code"] for r in recs}, {"1111", "1112"})
        self.assertTrue(all(len(r["photos"]) == 1 for r in recs))
        leftovers = [f for _, _, fs in os.walk(self.d) for f in fs if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        stats = self.run_once(FakePortal())
        self.assertEqual((stats["villages"], exit_code(stats)), (6, 0))


class Summary(Base):
    def run_main(self, portal):
        orig = runner.Client
        runner.Client = lambda delay: portal
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = runner.main(["jsjb", "--data-dir", self.d])
        finally:
            runner.Client = orig
        with open(os.path.join(self.d, "state", "last_run.json"), encoding="utf-8") as f:
            return code, out.getvalue(), json.load(f)

    def test_summary_written_and_exit_codes(self):
        code, text, last = self.run_main(FakePortal())
        self.assertEqual((code, last["exit_code"], last["villages"]), (0, 0, 6))
        self.assertIn("Villages saved: 6", text)

        portal = FakePortal({("search", "1111"): FetchError("timed out"),
                             ("villages", "211"): FetchError("reset"),
                             ("photo", f"{PHOTO}1112_1_Image1.jpg"): b""})
        os.remove(os.path.join(self.d, "records", "jsjb.coverage.json"))
        os.remove(os.path.join(self.d, "records", "jsjb.jsonl"))
        os.remove(os.path.join(self.d, "state", "jsjb.photo_index.json"))
        code, text, last = self.run_main(portal)
        self.assertEqual(code, 5)
        self.assertEqual([v["village_code"] for v in last["skipped_villages"]], ["1111"])
        self.assertEqual([u["name"] for u in last["skipped_units"]], ["Beta > B-One > Rural > B-Block1"])
        self.assertEqual(last["photos_failed"], 1)
        for part in ("Villages skipped: 1", "Lists skipped: 1", "failed, will retry next run: 1"):
            self.assertIn(part, text)

    def test_summary_on_early_stop(self):
        code, text, last = self.run_main(FakePortal({"start": "<html>maintenance</html>"}))
        self.assertEqual(code, 2)
        self.assertIn("STOPPED EARLY (layout)", text)
        self.assertEqual(last["stopped_early"]["kind"], "layout")


class HttpClient(unittest.TestCase):
    def setUp(self):
        import collector.http as h
        self._sleep = h.time.sleep
        h.time.sleep = lambda s: None

    def tearDown(self):
        import collector.http as h
        h.time.sleep = self._sleep

    def client(self, responses):
        import urllib.robotparser
        from collector.http import Client
        c = Client(delay=0, retries=3)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse([])
        c._robots["https://example.test"] = rp
        c.opener = Opener(responses)
        return c

    def test_size_cap(self):
        c = self.client([Resp(b"x" * 10, {"Content-Length": "999999"})])
        with self.assertRaises(FetchError):
            c.fetch_image("https://example.test/a.jpg", 1000)
        self.assertEqual(c.opener.opened, 1)
        c = self.client([Resp(b"x" * 5000)])
        with self.assertRaises(FetchError):
            c.fetch_image("https://example.test/a.jpg", 1000)
        self.assertEqual(c.opener.opened, 1)

    def test_short_body_retried(self):
        c = self.client([Resp(b"x" * 10, {"Content-Length": "20"}), Resp(WHOLE_JPEG, {"Content-Type": "image/jpeg"})])
        self.assertEqual(c.fetch_image("https://example.test/a.jpg", 1000), (WHOLE_JPEG, "image/jpeg"))
        self.assertEqual(c.opener.opened, 2)

    def test_redirect_loop_not_retried(self):
        c = self.client([urllib.error.HTTPError("https://example.test/a.jpg", 302, "loop", {}, None)])
        with self.assertRaises(FetchError) as cm:
            c.fetch_image("https://example.test/a.jpg", 1000)
        self.assertEqual((cm.exception.status, c.opener.opened), (302, 1))

    def test_odd_url_is_encoded(self):
        c = self.client([Resp(WHOLE_JPEG)])
        c.fetch_image("https://example.test/PhotoJSJB2\\3\\फोटो 1.jpg", 1000)
        self.assertEqual(c.opener.urls, ["https://example.test/PhotoJSJB2/3/%E0%A4%AB%E0%A5%8B%E0%A4%9F%E0%A5%8B%201.jpg"])

    def test_new_session_clears_cookies(self):
        import http.cookiejar
        from collector.http import Client
        c = Client(delay=0)
        c.cookies.set_cookie(http.cookiejar.Cookie(0, "ASP.NET_SessionId", "x", None, False, "example.test", True,
                                                   False, "/", True, False, None, False, None, None, {}))
        c.new_session()
        self.assertEqual(len(c.cookies), 0)


class Resp:
    def __init__(self, body, headers=None):
        import email.message
        self.body = body
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        pos = getattr(self, "pos", 0)
        end = len(self.body) if n is None or n < 0 else pos + n
        self.pos = min(end, len(self.body))
        return self.body[pos:self.pos]


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.opened = 0
        self.urls = []

    def open(self, req, timeout=None):
        self.opened += 1
        self.urls.append(req.full_url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


if __name__ == "__main__":
    unittest.main()
