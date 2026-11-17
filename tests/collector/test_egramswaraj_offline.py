"""Offline tests of the eGramSwaraj adapter against saved portal pages, and of its stage check.
No network. Run: python tests/collector/test_egramswaraj_offline.py

Fixtures (tests/collector/fixtures/egramswaraj/) are pages saved from egramswaraj.gov.in in
November 2026. gp_broken_cells.html is the saved GP page with one cell removed from row 5, and
photo_blank.png is a blank 8x8 image standing in for a portal photo (no real photo is kept)."""
import http.client
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import urllib.robotparser

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import collector.__main__ as runner  # noqa: E402
from checker import egramswaraj_stages as stages  # noqa: E402
from checker.common import clean_record, load_config  # noqa: E402
from collector.base import AccessDenied, Area, FilterNotFound, LayoutChanged, SkippedUnit  # noqa: E402
from collector.http import Client, FetchError  # noqa: E402
from collector.schemes import SCHEMES  # noqa: E402
from collector.schemes.egramswaraj import EGramSwarajAdapter, parse_gp_page, points, upload_time  # noqa: E402
from collector.store import Store, path_key  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "egramswaraj")
BASE = "https://egramswaraj.gov.in/"
GP_URL = BASE + "geotaggedDetailsforgpdp.do?year=2025&gpcode=236204"
PHOTO = BASE + "getMactionPhoto.do?imgId=/opt/actionsoft/attachment/mActionSoft/photoupload/3/2025/"


def fixture(name, mode="r"):
    with open(os.path.join(FIX, name), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


class FakePortal:
    """Answers the URLs the adapter builds from the saved pages. pages: {url: text or exception}
    laid over the defaults."""

    def __init__(self, pages=None):
        self.requests = []
        self.photo_requests = []
        self.denied = {}
        y = "2025-2026"
        self.pages = {
            BASE + "geoTaggingReport.do": fixture("report.html"),
            f"{BASE}geoTaggingStateReport.do?year={y}": fixture(f"state_{y}.json"),
            f"{BASE}geoTaggingZpReport.do?stateId=3&year={y}": fixture(f"zp_3_{y}.json"),
            f"{BASE}geoTaggingBpReport.do?stateId=3&code=302426&year={y}": fixture(f"bp_302426_{y}.json"),
            f"{BASE}geoTaggingGpReport.do?stateId=3&code=922&year={y}": fixture(f"gp_922_{y}.json"),
            f"{BASE}geoTaggingZpReport.do?stateId=30&year={y}": fixture(f"zp_30_{y}.json"),
            f"{BASE}geoTaggingZpReport.do?stateId=15&year={y}": fixture(f"zp_15_{y}.json"),
            f"{BASE}geoTaggingGpReport.do?stateId=15&code=11&year={y}": fixture(f"gp_15_11_{y}.json"),
            GP_URL: fixture("gp_236204_2025.html"),
        }
        self.pages.update(pages or {})

    def fetch(self, url, data=None, referer=None):
        self.requests.append(url)
        hit = self.pages.get(url)
        if hit is None:
            if "geotaggedDetailsforgpdp.do" in url:
                return fixture("gp_236204_2026_empty.html").encode()
            raise FetchError(f"no fixture for {url}", 404)
        if isinstance(hit, BaseException):
            raise hit
        return hit.encode() if isinstance(hit, str) else hit

    def fetch_image(self, url, max_bytes):
        self.photo_requests.append(url)
        return fixture("photo_blank.png", "rb"), None


MALERKOTLA_HQ = {"year": "2025-2026", "state": "Punjab", "district": "Malerkotla",
                 "block": "Malerkotla (Head Quarter)", "gp": None}


def adapter(portal=None):
    return EGramSwarajAdapter(portal or FakePortal())


def pharwali_area():
    return Area(levels=[{"level": "plan_year", "code": "2025", "name": "2025-2026"},
                        {"level": "state", "code": "3", "name": "Punjab"},
                        {"level": "district", "code": "302426", "name": "Malerkotla"},
                        {"level": "block", "code": "922", "name": "Malerkotla (Head Quarter)"},
                        {"level": "gp", "code": "236204", "name": "Pharwali"}],
                code_system="LGD-PRI", context={"year": "2025-2026", "gpcode": "236204", "count": 3})


class Registry(unittest.TestCase):
    def test_registered_with_its_filters(self):
        cls = SCHEMES["egramswaraj"]
        self.assertIs(cls, EGramSwarajAdapter)
        self.assertEqual(cls.filters, ("year", "state", "district", "block", "gp"))
        self.assertEqual(cls.plan_levels, ("state", "district"))
        self.assertTrue(cls.supports_list_only)


class Areas(unittest.TestCase):
    def test_plan_years_from_the_dropdown(self):
        self.assertEqual(adapter().plan_years(), ["2026-2027", "2025-2026", "2024-2025", "2023-2024"])

    def test_one_block_lists_its_15_gps(self):
        portal = FakePortal()
        areas = list(adapter(portal).list_areas(MALERKOTLA_HQ))
        self.assertEqual(len(areas), 15)
        a = next(x for x in areas if x.village_code == "236204")
        self.assertEqual((a.state, a.district, a.block, a.village), ("Punjab", "Malerkotla",
                                                                     "Malerkotla (Head Quarter)", "Pharwali"))
        self.assertEqual(a.context, {"year": "2025-2026", "gpcode": "236204", "count": 3})
        self.assertEqual(path_key("egramswaraj", a.code_system, a.levels),
                         "egramswaraj|LGD-PRI|2025/3/302426/922/236204")
        # report page, states, districts, blocks, GPs: one request each, no GP page yet.
        self.assertEqual(len(portal.requests), 5)

    def test_gps_with_no_assets_are_not_listed(self):
        gps = json.loads(fixture("gp_922_2025-2026.json"))
        gps[0]["count"] = 0
        portal = FakePortal({f"{BASE}geoTaggingGpReport.do?stateId=3&code=922&year=2025-2026": json.dumps(gps)})
        codes = [a.village_code for a in adapter(portal).list_areas(MALERKOTLA_HQ)]
        self.assertEqual(len(codes), 14)
        self.assertNotIn(str(gps[0]["code"]), codes)

    def test_district_that_lists_gps_directly(self):
        gp_list = json.dumps([{"code": 1001, "name": "Test GP", "level": "V", "gpCount": 1, "count": 2}])
        portal = FakePortal({f"{BASE}geoTaggingGpReport.do?stateId=30&code=506&year=2025-2026": gp_list})
        areas = list(adapter(portal).list_areas({"year": "2025", "state": "Goa", "district": "North Goa"}))
        self.assertEqual([a.village_code for a in areas], ["1001"])
        self.assertEqual([lv["level"] for lv in areas[0].levels], ["plan_year", "state", "district", "gp"])

    def test_state_without_district_level_uses_the_page_script_code(self):
        areas = list(adapter().list_areas({"year": "2025-2026", "state": "Mizoram"}))
        self.assertEqual(len(areas), 5)
        self.assertEqual(areas[0].levels[2], {"level": "district", "code": "11", "name": "Mizoram"})

    def test_unknown_names_raise_filter_not_found(self):
        with self.assertRaises(FilterNotFound):
            list(adapter().list_areas({"year": "2025-2026", "state": "Punjab", "district": "Nowhere"}))
        with self.assertRaises(FilterNotFound):
            list(adapter().list_areas({"year": "1999-2000", "state": "Punjab"}))

    def test_unreadable_list_is_skipped_not_fatal(self):
        portal = FakePortal({f"{BASE}geoTaggingBpReport.do?stateId=3&code=302426&year=2025-2026": "<html>oops"})
        out = list(adapter(portal).list_areas({"year": "2025-2026", "state": "Punjab", "district": "Malerkotla"}))
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], SkippedUnit)
        self.assertEqual((out[0].level, out[0].kind), ("blocks", "layout"))

    def test_login_page_stops(self):
        login = '<html><form><input type="password" name="pwd"></form></html>'
        portal = FakePortal({f"{BASE}geoTaggingStateReport.do?year=2025-2026": login})
        with self.assertRaises(AccessDenied):
            list(adapter(portal).list_areas({"year": "2025-2026"}))

    def test_list_districts(self):
        names = adapter().list_districts("PUNJAB")
        self.assertEqual(len(names), 23)
        self.assertEqual(names[0], "Amritsar")


class Records(unittest.TestCase):
    def test_gp_page_rows(self):
        rows = parse_gp_page(fixture("gp_236204_2025.html"), GP_URL)
        self.assertEqual(len(rows), 9)
        r = rows[6]
        self.assertEqual((r["activity_code"], r["stage"], r["cost"]), ("120346822", "Completion of work", 390650.0))
        self.assertEqual(r["url"], PHOTO + "Image_19062026094535570.png")
        self.assertEqual(r["points"], [(30.5239121, 75.8861911), (30.5239121, 75.8861911)])
        self.assertEqual(r["uploaded_at"], "2026-06-19T09:45:35.661975")

    def test_records_of_one_gp(self):
        recs = adapter().list_records(pharwali_area())
        self.assertEqual(len(recs), 3)
        by_parent = {r["parent_id"]: r for r in recs}
        self.assertEqual(sorted(by_parent), ["120346615", "120346713", "120346822"])
        r = by_parent["120346615"]
        self.assertTrue(r["record_id"].startswith("2025-120346615-"))
        self.assertEqual(r["structure_type"], "Any other operation and maintenance needs related to solid waste management")
        self.assertEqual((r["amount"], r["amount_kind"]), (292985.0, "approved"))
        self.assertEqual((r["lat"], r["lon"]), (30.5239037, 75.8861814))
        # Stage of the latest upload: Roof laid at 09:30, after Completion at 09:26.
        self.assertEqual(r["stage"], "Roof laid")
        self.assertEqual((r["group_key"], r["group_kind"], r["village_code"]), ("236204", "gp", "236204"))
        self.assertEqual(r["source_url"], GP_URL)
        self.assertEqual(r["extra"]["plan_year"], "2025-2026")
        self.assertNotIn("second_points", r["extra"])
        self.assertEqual([p["url"] for p in adapter().list_photos(r)],
                         [PHOTO + "Image_19062026092617029.png", PHOTO + "Image_19062026092813797.png",
                          PHOTO + "Image_19062026093018563.png"])
        self.assertEqual(adapter().list_photos(r)[1]["stage"], "Plinth level")
        # record ids are stable between runs
        self.assertEqual([x["record_id"] for x in recs], [x["record_id"] for x in adapter().list_records(pharwali_area())])

    def test_empty_gp_page(self):
        self.assertEqual(parse_gp_page(fixture("gp_236204_2026_empty.html"), GP_URL), [])

    def test_broken_page_raises(self):
        with self.assertRaises(LayoutChanged):
            parse_gp_page(fixture("gp_broken_cells.html"), GP_URL)

    def test_error_page_raises(self):
        with self.assertRaises(LayoutChanged):
            parse_gp_page(fixture("error_400.html"), GP_URL)

    def test_missing_row_number_raises(self):
        page = fixture("gp_236204_2025.html").replace("<td>4 </td>", "<td>5 </td>", 1)
        with self.assertRaises(LayoutChanged):
            parse_gp_page(page, GP_URL)

    def test_one_bad_cell_does_not_cost_the_page(self):
        page = fixture("gp_236204_2025.html").replace("<td>2026-06-19 09:28:13.88434</td>", "<td>soon</td>")
        rows = parse_gp_page(page, GP_URL)
        self.assertEqual(len(rows), 9)
        self.assertIsNone(rows[1]["uploaded_at"])
        self.assertEqual(rows[1]["unreadable"], {"uploaded_at": "soon"})

    def test_photo_link_of_another_form_is_not_requested(self):
        page = fixture("gp_236204_2025.html").replace(
            'src="getMactionPhoto.do?imgId=/opt/actionsoft/attachment/mActionSoft/photoupload/3/2025/Image_19062026092617029.png"',
            'src="https://elsewhere.example/x.png"')
        rows = parse_gp_page(page, GP_URL)
        self.assertIsNone(rows[0]["url"])

    def test_helpers(self):
        self.assertEqual(points("latitude : 1.5,1.5 Longitude : 2.5,2.5 file name : x"), [(1.5, 2.5), (1.5, 2.5)])
        self.assertIsNone(points("latitude : null Longitude : null"))
        self.assertIsNone(upload_time("not a date"))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        self.tmp.cleanup()

    def filters(self):
        return dict(MALERKOTLA_HQ, gp="Pharwali")

    def test_collect_saves_records_and_photos(self):
        portal = FakePortal()
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), self.filters())
        self.assertEqual(runner.exit_code(stats), 0)
        self.assertEqual(stats["records"], 3)
        self.assertEqual(len(portal.photo_requests), 9)
        self.assertTrue(all(u.startswith(PHOTO) for u in portal.photo_requests))
        store = Store(self.d, "egramswaraj")
        self.assertEqual(len(store.records), 3)
        for r in store.records.values():
            self.assertEqual(r["photos_state"], "fetched")
            self.assertEqual(len(r["photos"]), 3)
            clean_record(json.loads(json.dumps(r)))  # the checker accepts every saved record
        self.assertIn("egramswaraj|LGD-PRI|2025/3/302426/922/236204",
                      {c.get("key") for c in json.load(open(store.coverage_path, encoding="utf-8"))})

    def test_list_only_then_photos_pass(self):
        portal = FakePortal()
        runner.collect(adapter(portal), Store(self.d, "egramswaraj"), self.filters(), list_only=True)
        self.assertEqual(portal.photo_requests, [])
        store = Store(self.d, "egramswaraj")
        self.assertTrue(all(r["photos_state"] == "not_fetched" for r in store.records.values()))
        # The stage check runs on the list alone.
        recs = [clean_record(json.loads(json.dumps(r))) for r in store.records.values()]
        self.assertEqual(len(stages.find("egramswaraj", recs, load_config())), 1)
        stats = runner.photos_pass(adapter(portal), store)
        self.assertEqual(len(portal.photo_requests), 9)
        store = Store(self.d, "egramswaraj")
        self.assertTrue(all(r["photos_state"] == "fetched" for r in store.records.values()))
        self.assertEqual(stats["records_left"], 0)

    def test_broken_gp_page_is_skipped_and_the_run_goes_on(self):
        portal = FakePortal({GP_URL: fixture("gp_broken_cells.html")})
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ))
        self.assertEqual(runner.exit_code(stats), 5)
        self.assertEqual(len(stats["skipped_villages"]), 1)
        self.assertEqual(stats["villages"], 14)

    def test_gp_page_answering_403_stops_the_run(self):
        portal = FakePortal({GP_URL: FetchError("HTTP 403", 403, denied=True)})
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), self.filters())
        self.assertEqual(runner.exit_code(stats), 8)


class Resilience(unittest.TestCase):
    """Damaged pages, an empty area, a timeout, bad photos and long runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        self.tmp.cleanup()

    def gp_urls(self):
        gps = json.loads(fixture("gp_922_2025-2026.json"))
        return [f"{BASE}geotaggedDetailsforgpdp.do?year=2025&gpcode={g['code']}" for g in gps if g["count"]]

    def test_timeout_on_one_gp_page_skips_that_gp_only(self):
        portal = FakePortal({GP_URL: FetchError("could not fetch: timed out")})
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ))
        self.assertEqual(runner.exit_code(stats), 5)
        self.assertEqual([v["reason"] for v in stats["skipped_villages"]], ["network"])
        self.assertEqual(stats["villages"], 14)
        # Not marked as collected, so the next run with --since-days tries it again.
        self.assertNotIn("egramswaraj|LGD-PRI|2025/3/302426/922/236204", Store(self.d, "egramswaraj").coverage)

    def test_truncated_and_html_photos_are_not_stored(self):
        """Photos go through the real HTTP client (no network: its opener is replaced). The photo
        endpoint sends no Content-Type; here it also sends no Content-Length, as a dropped
        connection would look."""
        png = fixture("photo_blank.png", "rb")
        cut, html_url = PHOTO + "Image_19062026092617029.png", PHOTO + "Image_19062026092813797.png"
        bodies = {cut: png[: len(png) // 2], html_url: fixture("error_400.html").encode()}

        class Response:
            chunked = False

            def __init__(self, body):
                self.parts = [body, b""]
                self.headers = http.client.HTTPMessage()

            def read(self, n=-1):
                return self.parts.pop(0) if self.parts else b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def __init__(self):
                self.urls = []

            def open(self, req, timeout=None):
                self.urls.append(req.full_url)
                return Response(bodies.get(req.full_url, png))

        def client():
            c = Client(delay=0, retries=1)
            c.opener = Opener()
            rp = urllib.robotparser.RobotFileParser()
            rp.parse([])
            c._robots["https://egramswaraj.gov.in"] = rp
            return c

        portal = FakePortal()
        a = adapter(portal)
        a.client.fetch_image = client().fetch_image
        stats = runner.collect(a, Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ, gp="Pharwali"))
        self.assertEqual(runner.exit_code(stats), 5)
        self.assertEqual((stats["photos_failed"], stats["photos_downloaded"]), (2, 7))
        store = Store(self.d, "egramswaraj")
        self.assertIsNone(store.known_photo(cut))
        self.assertIsNone(store.known_photo(html_url))
        self.assertEqual(len([p for r in store.records.values() for p in r["photos"] if p.get("sha256")]), 7)
        # The next run asks only for the two photos that failed.
        c2 = client()
        a2 = adapter(FakePortal())
        a2.client.fetch_image = c2.fetch_image
        runner.collect(a2, Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ, gp="Pharwali"))
        self.assertEqual(sorted(c2.opener.urls), sorted(bodies))

    def test_empty_area_makes_no_gp_page_request(self):
        gps = json.loads(fixture("gp_922_2025-2026.json"))
        for g in gps:
            g["count"] = 0
        portal = FakePortal({f"{BASE}geoTaggingGpReport.do?stateId=3&code=922&year=2025-2026": json.dumps(gps)})
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ))
        self.assertEqual(runner.exit_code(stats), 0)
        self.assertEqual(stats["villages"], 0)
        self.assertFalse([u for u in portal.requests if "geotaggedDetailsforgpdp.do" in u])
        portal = FakePortal({f"{BASE}geoTaggingGpReport.do?stateId=3&code=922&year=2025-2026": "[]"})
        self.assertEqual(list(adapter(portal).list_areas(MALERKOTLA_HQ)), [])

    def test_truncated_gp_page_is_skipped(self):
        page = fixture("gp_236204_2025.html")
        cut = page[: page.index("Image_19062026094535570")]
        with self.assertRaises(LayoutChanged):
            parse_gp_page(cut, GP_URL)

    def test_free_text_saying_captcha_does_not_stop_the_run(self):
        page = fixture("gp_236204_2025.html").replace("Construction of roads1", "Road near captcha access denied", 1)
        self.assertIn("captcha", page)
        self.assertEqual(len(parse_gp_page(page, GP_URL)), 9)

    def test_login_page_instead_of_gp_page_stops(self):
        login = '<html><body><form><input type="password" name="pwd"></form></body></html>'
        with self.assertRaises(AccessDenied):
            parse_gp_page(login, GP_URL)
        portal = FakePortal({GP_URL: login})
        stats = runner.collect(adapter(portal), Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ, gp="Pharwali"))
        self.assertEqual(runner.exit_code(stats), 8)

    def test_lists_kept_for_a_while_block_and_gp_lists_not_kept(self):
        portal = FakePortal()
        a = adapter(portal)
        now = [1000.0]
        a.clock = lambda: now[0]
        list(a.list_areas(MALERKOTLA_HQ))
        list(a.list_areas(MALERKOTLA_HQ))
        count = lambda part: sum(1 for u in portal.requests if part in u)
        self.assertEqual((count("geoTaggingReport.do"), count("StateReport"), count("ZpReport")), (1, 1, 1))
        self.assertEqual((count("BpReport"), count("GpReport")), (2, 2))
        now[0] += a.CACHE_SECONDS + 1
        list(a.list_areas(MALERKOTLA_HQ))
        self.assertEqual((count("geoTaggingReport.do"), count("StateReport"), count("ZpReport")), (2, 2, 2))
        self.assertTrue(all(k.split("?")[0].endswith(a.CACHED_LISTS) for k in a._json_cache))

    def test_photos_pass_can_be_limited_to_one_gp_and_year(self):
        portal = FakePortal()
        runner.collect(adapter(portal), Store(self.d, "egramswaraj"), dict(MALERKOTLA_HQ), list_only=True)
        store = Store(self.d, "egramswaraj")
        self.assertEqual({r["year"] for r in store.records.values()}, {"2025-2026"})
        stats = runner.photos_pass(adapter(portal), store, {"year": "2025-2026", "gp": "pharwali"})
        self.assertEqual(stats["records"], 3)
        self.assertEqual(len(portal.photo_requests), 9)


def stage_record(rid, gp, rows):
    return {"record_id": rid, "group_key": gp, "village_code": gp, "village": f"GP {gp}", "state": "S",
            "photo_source": [{"stage": s, "uploaded_at": t} for s, t in rows]}


class StageCheck(unittest.TestCase):
    def test_fixture_gp_is_one_case(self):
        recs = adapter().list_records(pharwali_area())
        cases = stages.find("egramswaraj", recs, load_config())
        self.assertEqual(len(cases), 1)
        c = cases[0]
        self.assertEqual((c["kind"], c["record_count"]), ("stage_order", 3))
        self.assertIn("after the completion photo", " ".join(c["reasons"]))
        self.assertIn("Innocent reading", " ".join(c["reasons"]))

    def test_in_order_work_is_not_flagged(self):
        ok = [("Plinth level", "2026-01-01T10:00:00"), ("Roof laid", "2026-02-01T10:00:00"),
              ("Completion of work", "2026-03-01T10:00:00")]
        recs = [stage_record("a", "1", ok), stage_record("b", "1", ok)]
        self.assertEqual(stages.find("x", recs, load_config()), [])

    def test_one_flagged_work_is_not_a_case_and_unknown_stages_are_ignored(self):
        bad = [("Completion of work", "2026-01-01T10:00:00"), ("Plinth level", "2026-01-01T10:05:00")]
        odd = [("Some other label", "2026-01-01T10:00:00"), ("Plinth level", "2026-01-01T09:00:00")]
        recs = [stage_record("a", "1", bad), stage_record("b", "1", odd), stage_record("c", "2", bad)]
        self.assertEqual(stages.find("x", recs, load_config()), [])
        self.assertEqual(stages.flags(recs[0]), (True, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
