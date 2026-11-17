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


ERROR_PHOTO = "1792732445291.jpeg"


class FakeClient:
    """Serves the fixtures by URL. pages: {url: text or exception} overrides. reports: {district
    code: report fixture text}; district 60 (Bathinda) gets `report`, any other the empty report.
    photos: {url ending: list of answers, one per request, each bytes or an exception}."""

    def __init__(self, pages=None, report="report_bathinda_2026-10.html", reports=None, photos=None,
                 form_answers=None):
        self.pages = dict(pages or {})
        self.report = report
        self.reports = dict(reports or {})
        self.photos = {k: list(v) for k, v in (photos or {}).items()}
        self.form_answers = list(form_answers or [])
        self.requests = []
        self.denied = {}
        self.sessions = 0

    def new_session(self):
        self.sessions += 1

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
            if self.form_answers:
                v = self.form_answers.pop(0)
                if isinstance(v, BaseException):
                    raise v
                return v.encode()
            return fixture("report_form_answer.html").replace("district=60", f"district={data['DistrictCode']}").encode()
        if "MvcReportViewer.aspx" in url:
            code = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["district"][0]
            if code in self.reports:
                return self.reports[code].encode()
            return fixture(self.report if code == "60" else "report_empty.html").encode()
        if url.endswith("InspectionGradingDetails?id=239074$S"):
            return fixture("road_239074S.html").encode()
        if url == DETAIL_URL.format(obs="618892", kind="S"):
            return fixture("inspection_618892S.html").encode()
        raise FetchError(f"no fixture for {url}", 404)

    def fetch_image(self, url, max_bytes):
        self.requests.append((url, None))
        for end, answers in self.photos.items():
            if url.endswith(end) and answers:
                v = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(v, BaseException):
                    raise v
                return v, "image/jpeg"
        if url.endswith(ERROR_PHOTO):
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
        oct_report = fixture("report_bathinda_2026-10.html")
        # Cut before the only road row: no rows and no Total row is a cut page, not an empty district.
        cut_before_rows = oct_report[:oct_report.index("PB02029")]
        for page in (wrong_total, cut, renamed, cut_before_rows, "<html><body>nothing here</body></html>"):
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

    def unpaired_pages(self):
        road = fixture("road_239074S.html").replace(
            '<option value="618892">0.000-0.000</option>',
            '<option value="618892">0.000-0.000</option> <option value="618900">0.000-0.000</option>')
        pages = {"https://pmgsy.dord.gov.in/QualityMonitoringArea/QualityMonitoring/InspectionGradingDetails?id=239074$S": road}
        for obs in ("380579", "395745", "415370", "423161", "437159", "499173", "540875", "565138", "580083",
                    "598511", "618900"):
            pages[DETAIL_URL.format(obs=obs, kind="S")] = old_detail(obs, "22 Oct 2021")
        return pages

    def test_unpaired_dropdowns_with_an_unreadable_old_page(self):
        pages = self.unpaired_pages()
        pages[DETAIL_URL.format(obs="415370", kind="S")] = fixture("photo_error_page.html")
        pages[DETAIL_URL.format(obs="423161", kind="S")] = FetchError("timed out")
        a = PmgsyAdapter(FakeClient(pages))
        recs = a.list_records(next(iter(a.list_areas(OCT))))
        self.assertEqual([r["record_id"] for r in recs], ["SQM-618892"])
        # When the readable pages do not give the report's count, the district is skipped.
        pages[DETAIL_URL.format(obs="618892", kind="S")] = fixture("photo_error_page.html")
        a = PmgsyAdapter(FakeClient(pages))
        with self.assertRaises(LayoutChanged):
            a.list_records(next(iter(a.list_areas(OCT))))

    def test_district_spelt_differently_in_the_report(self):
        report = fixture("report_bathinda_2026-10.html").replace(">Bathinda<", ">Bathinda District<")
        self.assertNotEqual(report, fixture("report_bathinda_2026-10.html"))
        a = PmgsyAdapter(FakeClient(reports={"60": report}))
        self.assertEqual(len(a.list_records(next(iter(a.list_areas(OCT))))), 1)

    def test_watch_on_the_real_client(self):
        """A short body against its Content-Length is a failure, not a missing photo; the error page
        twice is a missing photo (HTTP 404)."""
        import http.server
        import threading
        from collector.http import Client
        from collector.schemes import pmgsy
        err = fixture("photo_error_page.html").encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/cut"):
                    self.send_response(200)
                    self.send_header("Content-Length", "500")
                    self.end_headers()
                    self.wfile.write(JPEG)
                elif self.path.startswith("/err"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = pmgsy.IMAGE_HOST
        pmgsy.IMAGE_HOST = "127.0.0.1"
        try:
            c = PmgsyAdapter(Client(delay=0, hosts={"127.0.0.1": {"delay": 0, "retries": 1}})).client
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            with self.assertRaises(FetchError) as cm:
                c.fetch_image(base + "/cut.jpg", 10 ** 6)
            self.assertNotEqual(cm.exception.status, 404)
            with self.assertRaises(FetchError) as cm:
                c.fetch_image(base + "/err.jpg", 10 ** 6)
            self.assertEqual(cm.exception.status, 404)
            # Photo worker clients are watched too.
            w = c.photo_clients(2)[0]
            with self.assertRaises(FetchError) as cm:
                w.fetch_image(base + "/err.jpg", 10 ** 6)
            self.assertEqual(cm.exception.status, 404)
        finally:
            pmgsy.IMAGE_HOST = old
            srv.shutdown()
            srv.server_close()

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

    def stored_files(self):
        return [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(self.d, "photos")) for f in fs]

    def test_collect_saves_record_and_never_stores_the_error_page(self):
        client = FakeClient()
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertIsNone(stats["stop"])
        # The error page photo is missing on the portal: recorded, not stored, not retried.
        self.assertEqual((stats["records"], stats["photos_downloaded"], stats["photos_failed"],
                          stats["photos_missing"]), (1, 20, 0, 1))
        self.assertEqual(runner.exit_code(stats), 0)
        store = Store(self.d, "pmgsy")
        r = store.records["SQM-618892"]
        self.assertEqual((r["area_key"], r["photos_state"]), ("pmgsy|PMGSY|28/60", "fetched"))
        self.assertFalse([k for k in r if k.startswith("_")])
        bad = [p for p in r["photos"] if p["url"].endswith(ERROR_PHOTO)][0]
        self.assertIsNone(bad["sha256"])
        self.assertEqual(sum(1 for p in r["photos"] if p["sha256"]), 20)
        stored = self.stored_files()
        self.assertEqual(len(stored), 20)
        for path in stored:
            with open(path, "rb") as f:
                self.assertTrue(f.read(3) == b"\xff\xd8\xff")
        # Only the photo URLs the detail page printed were requested on the image host, and the
        # error page photo was asked for twice.
        printed = {p["url"] for p in parse_detail(fixture("inspection_618892S.html"), "618892", "S")["photos"]}
        asked = [u for u, _ in client.requests if "pmgsygov.dord.gov.in" in u]
        self.assertEqual(set(asked), printed)
        self.assertEqual(sum(1 for u in asked if u.endswith(ERROR_PHOTO)), 2)
        self.assertEqual(len(asked), 22)
        # A second run asks for no photo again, the missing one included.
        client2 = FakeClient()
        stats = runner.collect(PmgsyAdapter(client2), Store(self.d, "pmgsy"), OCT)
        self.assertEqual((stats["photos_downloaded"], stats["photos_failed"], runner.exit_code(stats)), (0, 0, 0))
        self.assertFalse([u for u, _ in client2.requests if "pmgsygov.dord.gov.in" in u])
        # With --since-days the district is not read at all.
        client3 = FakeClient()
        stats = runner.collect(PmgsyAdapter(client3), Store(self.d, "pmgsy"), OCT, since_days=30)
        self.assertEqual((stats["villages"], stats["not_due"]), (0, 1))
        self.assertFalse([u for u, _ in client3.requests if "InspDetails" in u or "MvcReportViewer" in u])

    def test_error_page_once_then_the_photo(self):
        good = JPEG + b"real" + b"\xff\xd9"
        client = FakeClient(photos={ERROR_PHOTO: [fixture("photo_error_page.html").encode(), good]})
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertEqual((stats["photos_downloaded"], stats["photos_missing"], stats["photos_failed"]), (21, 0, 0))

    def test_truncated_and_timed_out_photos_are_skipped(self):
        # How the client reports a body shorter than its Content-Length (see test_watch_on_the_real_client).
        cut = FetchError("could not fetch: IncompleteRead(44 bytes read, 456 more expected)")
        client = FakeClient(photos={ERROR_PHOTO: [cut], "1792732380000.jpeg": [FetchError("timed out")]})
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT, photo_workers=2)
        self.assertIsNone(stats["stop"])
        self.assertEqual((stats["photos_downloaded"], stats["photos_failed"]), (19, 2))
        self.assertEqual(runner.exit_code(stats), 5)
        r = Store(self.d, "pmgsy").records["SQM-618892"]
        self.assertEqual(sum(1 for p in r["photos"] if p["sha256"]), 19)
        self.assertEqual(len(self.stored_files()), 19)
        # The area stays due, so the rerun fetches only the two failed photos.
        client2 = FakeClient()
        stats = runner.collect(PmgsyAdapter(client2), Store(self.d, "pmgsy"), OCT, since_days=30)
        self.assertEqual(stats["villages"], 1)
        asked = [u for u, _ in client2.requests if "pmgsygov.dord.gov.in" in u]
        self.assertEqual(len(asked), 3)  # the timed-out one, and the error page one asked twice

    def test_bad_district_is_skipped_and_the_run_goes_on(self):
        good = fixture("report_bathinda_2026-10.html")
        reports = {"181": good[:good.index("PB02029")], "18": "<html><body>Oops! Something went wrong.</body></html>"}
        client = FakeClient(reports=reports)
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), dict(OCT, district=None))
        self.assertIsNone(stats["stop"])
        skipped = sorted(v["name"] for v in stats["skipped_villages"])
        self.assertEqual(skipped, ["Punjab > Amritsar", "Punjab > Fatehgarh Sahib"])
        # 21 districts saved: Bathinda with its record, the others empty for this month.
        self.assertEqual((stats["villages"], stats["records"]), (21, 1))
        store = Store(self.d, "pmgsy")
        self.assertNotIn("pmgsy|PMGSY|28/181", store.coverage)
        self.assertEqual(store.coverage["pmgsy|PMGSY|28/482"]["records"], 0)
        self.assertEqual(runner.exit_code(stats), 5)

    def test_empty_area(self):
        stats = runner.collect(PmgsyAdapter(FakeClient()), Store(self.d, "pmgsy"), dict(OCT, district="Mansa"))
        self.assertEqual((stats["villages"], stats["records"], runner.exit_code(stats)), (1, 0, 0))
        self.assertIn("pmgsy|PMGSY|28/357", Store(self.d, "pmgsy").coverage)

    def test_detail_timeout_keeps_record_without_photos(self):
        url = DETAIL_URL.format(obs="618892", kind="S")
        client = FakeClient({url: FetchError("timed out")})
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertIsNone(stats["stop"])
        self.assertEqual(stats["records"], 1)
        self.assertEqual(runner.exit_code(stats), 5)
        r = Store(self.d, "pmgsy").records["SQM-618892"]
        self.assertEqual((r["photos_state"], r["start_date"], r["status"]), ("not_fetched", "2026-10-23", None))
        self.assertIn("timed out", r["extra"]["detail_unreadable"])
        # The next run reads the page and the photos.
        stats = runner.collect(PmgsyAdapter(FakeClient()), Store(self.d, "pmgsy"), OCT, since_days=30)
        r = Store(self.d, "pmgsy").records["SQM-618892"]
        self.assertEqual((r["photos_state"], r["status"], stats["photos_downloaded"]),
                         ("fetched", "Required Improvement", 20))

    def test_report_form_error_retried_with_a_new_session(self):
        client = FakeClient(form_answers=[fixture("photo_error_page.html")])
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertEqual((stats["records"], client.sessions), (1, 1))
        self.assertEqual(len([u for u, _ in client.requests if u == LAYOUT_URL]), 2)
        client = FakeClient(form_answers=[FetchError("HTTP 500", 500), FetchError("HTTP 500", 500)])
        stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        self.assertEqual(len(stats["skipped_villages"]), 1)
        self.assertIsNone(stats["stop"])

    def test_low_disk_stops_before_a_district(self):
        from collector.schemes import pmgsy
        self.assertEqual(pmgsy.data_dir_of(["pmgsy", "--data-dir", "x/y"]), "x/y")
        self.assertEqual(pmgsy.data_dir_of(["--data-dir=z"]), "z")
        self.assertEqual(pmgsy.data_dir_of(["pmgsy"]), "data")
        old = pmgsy.free_bytes
        pmgsy.free_bytes = lambda path: pmgsy.MIN_FREE_BYTES - 1
        try:
            client = FakeClient()
            stats = runner.collect(PmgsyAdapter(client), Store(self.d, "pmgsy"), OCT)
        finally:
            pmgsy.free_bytes = old
        self.assertEqual((stats["stop"][0], runner.exit_code(stats)), ("disk", 6))
        self.assertFalse([u for u, _ in client.requests if "MvcReportViewer" in u or u == REPORT_FORM_URL])

    def test_list_only_then_photos(self):
        stats = runner.collect(PmgsyAdapter(FakeClient()), Store(self.d, "pmgsy"), OCT, list_only=True)
        self.assertEqual(stats["records"], 1)
        store = Store(self.d, "pmgsy")
        self.assertEqual(store.records["SQM-618892"]["photos_state"], "not_fetched")
        client = FakeClient()
        stats = runner.photos_pass(PmgsyAdapter(client), store)
        self.assertEqual((stats["photos_downloaded"], stats["photos_failed"], stats["photos_missing"]), (20, 0, 1))
        store = Store(self.d, "pmgsy")
        self.assertEqual(store.records["SQM-618892"]["photos_state"], "fetched")
        self.assertEqual(store.coverage["pmgsy|PMGSY|28/60"]["photos_state"], "fetched")
        # The photos pass reads the detail page once more: list-only costs one page per inspection.
        self.assertEqual(len([u for u, _ in client.requests if "/InspDetails/" in u]), 1)

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
