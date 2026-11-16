"""Fault injection against the collector, offline. Each test asserts the behaviour the owner wants;
a failing test is a defect. Run: python tests/collector/test_attacks.py"""
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.robotparser

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from test_resilience import (ALL, ALL_VILLAGES, JPEG, PHOTO, Base, FakePortal, Resp,  # noqa: E402
                             row, village_page)
from collector.__main__ import collect, exit_code  # noqa: E402
from collector.http import Client  # noqa: E402
from collector.schemes.jsjb import JsjbAdapter  # noqa: E402
from collector.store import Store  # noqa: E402

ERROR_PAGE = "<html><body><h1>Server Error in '/' Application.</h1></body></html>"


class TransientErrorPage(Base):
    def test_short_error_page_burst_does_not_end_a_long_run(self):
        # Village 1112 gets the portal's error page, and so do the next two start-page loads
        # (a few seconds of trouble). Everything after that is normal.
        portal = FakePortal({("search", "1112"): ["error"], "start": [None, "error", "error"]})
        stats = self.run_once(portal)
        self.assertIsNone(stats["stop"], stats["stop"])
        self.assertGreaterEqual(stats["villages"], 5)


class SiteWideEmptyLists(Base):
    def test_all_village_lists_empty_is_not_a_clean_run(self):
        portal = FakePortal()
        real = portal._page

        def page(data):
            out = real(data)
            if data and data.get("__EVENTTARGET") == "ddlBlockTown":
                out = out.split('<select id="ddlVillageWard"')[0] + \
                    '<select id="ddlVillageWard" name="ddlVillageWard"><option value="0">-- Select --</option></select></form></body></html>'
            return out
        portal._page = page
        stats = self.run_once(portal)
        self.assertEqual(stats["villages"], 0)
        self.assertNotEqual(exit_code(stats), 0, "0 villages found everywhere, yet exit 0 and no warning")


class SinceDaysRetry(Base):
    def test_failed_photo_retried_on_rerun_with_since_days(self):
        url = f"{PHOTO}1111_1_Image1.jpg"
        stats = self.run_once(FakePortal({("photo", url): b""}), since_days=7)
        self.assertEqual((stats["photos_failed"], exit_code(stats)), (1, 5))
        healed = FakePortal()
        self.run_once(healed, since_days=7)
        self.assertIn(url, healed.photo_calls, "summary says rerun retries it, but --since-days skips the village")


class OneBadRecord(Base):
    def test_one_garbage_date_keeps_the_rest_of_the_village(self):
        rows = [row(str(i), "1111", [f"{PHOTO}g{i}_Image1.jpg"]) for i in range(1, 50)]
        rows.append(row("50", "1111", [], **{"Work Start Date": "00/00/0000"}))
        page = village_page("1111", rows)
        for _ in range(2):
            self.run_once(FakePortal({("search", "1111"): page}))
        recs, _ = self.saved()
        self.assertGreaterEqual(len([r for r in recs if r["village_code"] == "1111"]), 49,
                                "49 good records dropped, every run, for one bad date")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        # Connection: close and no Content-Length; the server dies half way through the photo.
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(JPEG_FULL[: len(JPEG_FULL) // 2])
        self.wfile.flush()
        self.close_connection = True


JPEG_FULL = JPEG + b"\x11" * 4000 + b"\xff\xd9"


class TruncatedPhoto(Base):
    def test_photo_cut_off_without_length_is_not_stored(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        srv.protocol_version = "HTTP/1.0"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = Client(delay=0, retries=1, timeout=5)
            url = f"http://127.0.0.1:{srv.server_port}/p_Image1.jpg"
            from collector.__main__ import fetch_photo
            store = Store(self.d, "jsjb")
            entry, outcome = fetch_photo(c, store, url)
        finally:
            srv.shutdown()
        self.assertNotEqual(outcome, "ok", f"half a JPEG stored as a good photo: {entry}")


class ReaderHoldsRecords(Base):
    def test_checker_reading_records_does_not_stop_collection(self):
        self.run_once(FakePortal())
        path = os.path.join(self.d, "records", "jsjb.jsonl")
        import collector.store as st
        real_sleep = st.time.sleep
        st.time.sleep = lambda s: None
        try:
            with open(path, "rb"):  # what checker.common.load_records does while it reads
                stats = self.run_once(FakePortal(), flush_every=1)
        finally:
            st.time.sleep = real_sleep
        self.assertIsNone(stats["stop"], stats["stop"])


class InterruptedInPlaceWrite(Base):
    def test_parked_copy_is_finished_on_next_start(self):
        self.run_once(FakePortal())
        path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(path, "rb") as f:
            whole = f.read()
        with open(path + ".inplace", "wb") as f:
            f.write(whole)
        with open(path, "wb") as f:
            f.write(whole[: len(whole) // 3])
        store = Store(self.d, "jsjb")
        self.assertEqual(len(store.records), 12)
        self.assertFalse(os.path.exists(path + ".inplace"))


class ImageEnds(unittest.TestCase):
    def test_cut_images_are_noticed(self):
        from collector.store import image_complete
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40 + b"\0\0\0\0IEND\xaeB`\x82"
        gif = b"GIF89a" + b"\0" * 20 + b";"
        jpeg_extra = JPEG_FULL + b"trailing camera data"
        for whole in (JPEG_FULL, png, gif, jpeg_extra):
            self.assertTrue(image_complete(whole))
            self.assertFalse(image_complete(whole[: len(whole) // 2]))
        self.assertTrue(image_complete(b"<html>not an image</html>"))


class PageSize(unittest.TestCase):
    def test_page_body_has_a_cap(self):
        c = Client(delay=0, retries=1)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse([])
        c._robots["https://example.test"] = rp

        class O:
            def open(self, req, timeout=None):
                return Resp(b"x" * (300 * 2 ** 20))
        c.opener = O()
        try:
            body = c.fetch("https://example.test/PublicDashboardJsjb.aspx")
        except Exception:
            return
        self.fail(f"a {len(body) >> 20} MB page was read whole into memory with no cap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
