"""Offline tests of the PMGSY adapter against saved OMMAS pages (monitor names blanked).
Run: python tests/collector/test_pmgsy_offline.py"""
import json
import logging
import os
import re
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import collector.__main__ as runner  # noqa: E402
import collector.queue_runner as qr  # noqa: E402
from collector.base import AccessDenied, FilterNotFound, LayoutChanged  # noqa: E402
from collector.http import FetchError  # noqa: E402
from collector.schemes import SCHEMES  # noqa: E402
from collector.schemes.pmgsy import (DETAIL_URL, DISTRICTS_URL, LAYOUT_URL, REPORT_FORM_URL, PmgsyAdapter,  # noqa: E402
                                     norm, parse_detail, parse_report, parse_road_page, window_of)
from collector.store import Store  # noqa: E402

logging.disable(logging.CRITICAL)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pmgsy")
OCT = {"state": "Punjab", "district": "Bathinda", "from_month": "2026-10", "to_month": "2026-10"}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def old_detail(obs, day):
    """The saved 2021 inspection page relabelled as another observation on another day."""
    t = fixture("inspection_380579S_old.html")
    t = t.replace('name="ObservationId" type="hidden" value="380579"', f'name="ObservationId" type="hidden" value="{obs}"')
    return re.sub(r"22 Oct 2021", day, t)


class FakeClient:
    """Serves the fixtures by URL. pages: {url: text or exception} overrides."""

    def __init__(self, pages=None, report="report_bathinda_2026-10.html"):
        self.pages = dict(pages or {})
        self.report = report
        self.requests = []
        self.denied = {}

    def fetch(self, url, data=None, referer=None):
        self.requests.append((url, data))
        if url in self.pages:
            v = self.pages[url]
            if isinstance(v, BaseException):
                raise v
            return v.encode()
        if url == LAYOUT_URL:
            return fixture("layout.html").encode()
        if url == DISTRICTS_URL:
            return (fixture("districts_28.json") if str(data["StateCode"]) == "28" else "[]").encode()
        if url == REPORT_FORM_URL:
            return fixture("report_form_answer.html").encode()
        if "MvcReportViewer.aspx" in url:
            return fixture(self.report).encode()
        if url.endswith("InspectionGradingDetails?id=239074$S"):
            return fixture("road_239074S.html").encode()
        if url == DETAIL_URL.format(obs="618892", kind="S"):
            return fixture("inspection_618892S.html").encode()
        raise FetchError(f"no fixture for {url}", 404)

    def fetch_image(self, url, max_bytes):
        self.requests.append((url, None))
        if url.endswith("1792732445291.jpeg"):
            # The portal's answer for a missing photo: HTTP 200 and an HTML page.
            return fixture("photo_error_page.html").encode(), "text/html"
        return JPEG + url.encode() + b"\xff\xd9", "image/jpeg"


class Parsers(unittest.TestCase):
    def test_report_counts_and_totals(self):
        r = parse_report(fixture("report_bathinda_2026-01_2026-11.html"))
        self.assertEqual((len(r["roads"]), r["total_nqm"], r["total_sqm"]), (16, 7, 14))
        links = [link for row in r["roads"] for link in row["links"]]
        self.assertEqual(len(links), 20)
        sangat = [row for row in r["roads"] if row["package"] == "PB02029"][0]
        self.assertEqual((sangat["block"], sangat["sanction_year"], sangat["road_length_km"]),
                         ("Sangat", "2020-2021", 7.285))
        self.assertEqual([(x["kind"], x["road"], x["count"]) for x in sangat["links"]], [("S", "239074", 2)])
        # Rows under a spanned block cell keep that block.
        rampura = [row["block"] for row in r["roads"] if row["package"] in ("PB0238", "PB0244", "PB0245")]
        self.assertEqual(rampura, ["Rampura"] * 3)

    def test_empty_report(self):
        self.assertEqual(parse_report(fixture("report_empty.html"))["roads"], [])

    def test_broken_reports_raise(self):
        good = fixture("report_bathinda_2026-01_2026-11.html")
        wrong_total = re.sub(r"(>Total<.*?>124\.785<.*?<DIV[^>]*>)7<", r"\g<1>8<", good, count=1, flags=re.S)
        self.assertNotEqual(wrong_total, good)
        cut = good[:good.index("PB0239")]
        renamed = good.replace(">Package No.<", ">Package<")
        for page in (wrong_total, cut, renamed, "<html><body>nothing here</body></html>"):
            with self.assertRaises(LayoutChanged):
                parse_report(page)
        with self.assertRaises(LayoutChanged):
            parse_report(good.replace('id="ReportViewer_ctl05_ctl00_TotalPages" style="font-family:Verdana;'
                                      'font-size:8pt;white-space:nowrap;">1<',
                                      'id="ReportViewer_ctl05_ctl00_TotalPages" style="">3<'))

    def test_road_page(self):
        times, ids = parse_road_page(fixture("road_239074S.html"), "239074", "S")
        self.assertEqual((len(times), len(ids)), (11, 11))
        self.assertEqual(times[0].isoformat(), "2026-10-23T10:43:00")
        self.assertEqual((ids[0], ids[-1]), ("380579", "618892"))
        with self.assertRaises(LayoutChanged):
            parse_road_page(fixture("road_239074S.html"), "239075", "S")

    def test_detail(self):
        d = parse_detail(fixture("inspection_618892S.html"), "618892", "S")
        f = d["fields"]
        self.assertEqual((f["Monitor Name"], f["District"], f["Package"], f["Status"], f["Type"]),
                         ("Monitor A", "Bathinda", "PB02029", "Maintenance", "Road"))
        self.assertEqual((d["day"].isoformat(), d["overall"], len(d["grades"])), ("2026-10-23", "Required Improvement", 7))
        self.assertEqual(len(d["photos"]), 21)
        p = d["photos"][0]
        self.assertTrue(p["url"].startswith("https://pmgsygov.dord.gov.in/OMMASII/QM/SQM/2026/2026$150728_239074_"))
        self.assertEqual((p["lat"], p["lon"], p["taken_at"], p["label"]),
                         (30.1260689, 74.7861421, "2026-10-23T10:43:00", "Start point of the road"))
        old = parse_detail(fixture("inspection_380579S_old.html"), "380579", "S")
        self.assertEqual((old["day"].isoformat(), len(old["photos"]), old["photos"][0]["taken_at"]),
                         ("2021-10-22", 20, "2021-10-22"))

    def test_broken_details_raise(self):
        good = fixture("inspection_618892S.html")
        pages = [fixture("photo_error_page.html"), good.replace("Monitor Name", "Monitor"),
                 good[:good.index("divContentQMGrading")]]
        for page in pages:
            with self.assertRaises(LayoutChanged):
                parse_detail(page, "618892", "S")
        with self.assertRaises(LayoutChanged):
            parse_detail(good, "618893", "S")
        with self.assertRaises(AccessDenied):
            parse_detail('<form><input type="password" name="p"></form>', "618892", "S")

    def test_window_and_names(self):
        from datetime import date
        self.assertEqual(window_of({}, today=date(2026, 11, 17)), ((2025, 12), (2026, 11)))
        self.assertEqual(window_of({"from_month": "2026-01", "to_month": "2026-03"}), ((2026, 1), (2026, 3)))
        for bad in ({"from_month": "2026-13"}, {"from_month": "2026-05", "to_month": "2026-04"}):
            with self.assertRaises(FilterNotFound):
                window_of(bad)
        self.assertEqual(norm("Andaman And Nicobar (UT) "), norm("andaman and  nicobar"))


class Adapter(unittest.TestCase):
    def test_registered(self):
        self.assertIs(SCHEMES.get("pmgsy"), PmgsyAdapter)

    def test_areas(self):
        a = PmgsyAdapter(FakeClient())
        areas = list(a.list_areas(OCT))
        self.assertEqual(len(areas), 1)
        self.assertEqual([lv["code"] for lv in areas[0].levels], ["28", "60"])
        self.assertEqual((areas[0].state, areas[0].district, areas[0].code_system), ("Punjab", "Bathinda", "PMGSY"))
        self.assertEqual(len(list(PmgsyAdapter(FakeClient()).list_areas(dict(OCT, district=None)))), 23)
        with self.assertRaises(FilterNotFound):
            list(PmgsyAdapter(FakeClient()).list_areas(dict(OCT, state="Atlantis")))
        with self.assertRaises(FilterNotFound):
            list(PmgsyAdapter(FakeClient()).list_areas(dict(OCT, district="Nowhere")))
        self.assertIn("Bathinda", PmgsyAdapter(FakeClient()).list_districts("PUNJAB"))

    def test_records(self):
        client = FakeClient()
        a = PmgsyAdapter(client)
        area = next(iter(a.list_areas(OCT)))
        recs = a.list_records(area)
        self.assertEqual([r["record_id"] for r in recs], ["SQM-618892"])
        r = recs[0]
        self.assertEqual((r["parent_id"], r["block"], r["start_date"], r["stage"], r["status"], r["structure_type"]),
                         ("239074", "Sangat", "2026-10-23", "Maintenance", "Required Improvement", "Road"))
        self.assertEqual((r["group_key"], r["group_kind"]), ("Monitor A", "quality_monitor"))
        self.assertEqual(r["extra"]["inspected_at"], "2026-10-23T10:43:00")
        self.assertEqual(r["extra"]["package"], "PB02029")
        self.assertEqual(len(a.list_photos(r)), 21)
        # One detail page read: the dropdown pairing matched the printed date.
        details = [u for u, _ in client.requests if "/InspDetails/" in u]
        self.assertEqual(details, [DETAIL_URL.format(obs="618892", kind="S")])
        posted = [d for u, d in client.requests if u == REPORT_FORM_URL][0]
        self.assertEqual((posted["StateCode"], posted["DistrictCode"], posted["FrmMonth"], posted["ToYear"]),
                         ("28", "60", 10, 2026))

    def test_count_mismatch_raises(self):
        # The October report against a September window: the road page has none in September.
        a = PmgsyAdapter(FakeClient())
        area = next(iter(a.list_areas(dict(OCT, from_month="2026-09", to_month="2026-09"))))
        with self.assertRaises(LayoutChanged):
            a.list_records(area)

    def test_unpaired_dropdowns_read_every_inspection(self):
        road = fixture("road_239074S.html").replace(
            '<option value="618892">0.000-0.000</option>',
            '<option value="618892">0.000-0.000</option> <option value="618900">0.000-0.000</option>')
        pages = {"https://pmgsy.dord.gov.in/QualityMonitoringArea/QualityMonitoring/InspectionGradingDetails?id=239074$S": road}
        for obs in ("380579", "395745", "415370", "423161", "437159", "499173", "540875", "565138", "580083",
                    "598511", "618900"):
            pages[DETAIL_URL.format(obs=obs, kind="S")] = old_detail(obs, "22 Oct 2021")
        client = FakeClient(pages)
        a = PmgsyAdapter(client)
        recs = a.list_records(next(iter(a.list_areas(OCT))))
        self.assertEqual([r["record_id"] for r in recs], ["SQM-618892"])
        self.assertEqual(len([u for u, _ in client.requests if "/InspDetails/" in u]), 12)

    def test_unreadable_detail_keeps_the_record_without_photos(self):
        pages = {DETAIL_URL.format(obs="618892", kind="S"): fixture("photo_error_page.html")}
        a = PmgsyAdapter(FakeClient(pages))
        recs = a.list_records(next(iter(a.list_areas(OCT))))
        self.assertEqual(recs[0]["start_date"], "2026-10-23")
        self.assertIn("detail_unreadable", recs[0]["extra"])
        self.assertNotIn("_photos", recs[0])
        with self.assertRaises(LayoutChanged):
            a.list_photos(recs[0])

    def test_login_page_stops(self):
        a = PmgsyAdapter(FakeClient({LAYOUT_URL: '<form><input type="password" name="pw"></form>'}))
        with self.assertRaises(AccessDenied):
            list(a.list_areas(OCT))


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_saves_record_and_never_stores_the_error_page(self):
        client = FakeClient()
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertIsNone(stats["stop"])
        self.assertEqual((stats["records"], stats["photos_downloaded"], stats["photos_failed"]), (1, 20, 1))
        self.assertEqual(runner.exit_code(stats), 5)
        store = Store(self.d, "pmgsy")
        r = store.records["SQM-618892"]
        self.assertEqual(r["area_key"], "pmgsy|PMGSY|28/60")
        self.assertFalse([k for k in r if k.startswith("_")])
        bad = [p for p in r["photos"] if p["url"].endswith("1792732445291.jpeg")][0]
        self.assertIsNone(bad["sha256"])
        self.assertEqual(sum(1 for p in r["photos"] if p["sha256"]), 20)
        stored = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(self.d, "photos")) for f in fs]
        self.assertEqual(len(stored), 20)
        for path in stored:
            with open(path, "rb") as f:
                self.assertTrue(f.read(3) == b"\xff\xd8\xff")
        # Only the photo URLs the detail page printed were requested on the image host.
        printed = {p["url"] for p in parse_detail(fixture("inspection_618892S.html"), "618892", "S")["photos"]}
        asked = {u for u, _ in client.requests if "pmgsygov.dord.gov.in" in u}
        self.assertEqual(asked, printed)

    def test_list_only_then_photos(self):
        stats = runner.collect(PmgsyAdapter(FakeClient()), Store(self.d, "pmgsy"), OCT, list_only=True)
        self.assertEqual(stats["records"], 1)
        store = Store(self.d, "pmgsy")
        self.assertEqual(store.records["SQM-618892"]["photos_state"], "not_fetched")
        stats = runner.photos_pass(PmgsyAdapter(FakeClient()), store)
        self.assertEqual((stats["photos_downloaded"], stats["photos_failed"]), (20, 1))
        # The runner keeps a record with a failed photo not_fetched, so the error page photo holds it back.
        self.assertEqual(Store(self.d, "pmgsy").records["SQM-618892"]["photos_state"], "not_fetched")

    def test_access_denied_exit_8(self):
        a = PmgsyAdapter(FakeClient({LAYOUT_URL: "<div class='g-recaptcha'></div>"}))
        stats = runner.collect(a, Store(self.d, "pmgsy"), OCT)
        self.assertEqual(runner.exit_code(stats), 8)

    def test_plan_resolves(self):
        with open(os.path.join(ROOT, "plans", "pmgsy-india.json"), encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["scheme"], "pmgsy")
        names = {"Punjab": ["Amritsar", "Bathinda"]}
        units, problems = qr.resolve(plan, lambda s: names.get(s, ["X"]), adapter_cls=PmgsyAdapter)
        self.assertEqual([u["district"] for u in units[:2]], ["Amritsar", "Bathinda"])
        self.assertEqual(units[0]["phase"], "punjab")
        self.assertEqual(units[2]["phase"], "capitals")
        keys = [u["key"] for u in units]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
