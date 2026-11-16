"""Offline tests against saved JSJB pages. Run: python tests/collector/test_jsjb_offline.py"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from collector.__main__ import collect
from collector.base import FilterNotFound, LayoutChanged
from collector.http import FetchError
from collector.schemes.jsjb import JsjbAdapter, clean_name, upload_time

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def paged_results():
    """Page 1 and page 2 built from the saved Duggan results, with a GridView pager row added."""
    base = fixture("jsjb_results.html").replace("Total : 26", "Total : 52")
    pager = ('<tr><td colspan="18"><table><tr><td><span>{cur}</span></td><td>'
             '<a href="javascript:__doPostBack(&#39;gvData&#39;,&#39;Page${other}&#39;)">{other}</a>'
             '</td></tr></table></td></tr>')
    i = base.index('id="gvData"')
    end = base.index("</table>", i)
    p1 = base[:end] + pager.format(cur=1, other=2) + base[end:]
    p2 = re.sub(r"(\s)(\d+) \((\d+)\)(\s)", lambda m: f"{m[1]}{int(m[2]) + 26} (9{m[3]}){m[4]}", base)
    end2 = p2.index("</table>", p2.index('id="gvData"'))
    p2 = p2[:end2] + pager.format(cur=2, other=1) + p2[end2:]
    return p1, p2


class FakeClient:
    def __init__(self, paged=False):
        self.paged = paged
        self.calls = []
        self.photo_calls = 0

    def fetch(self, url, data=None, referer=None):
        if "PhotoJSJB2" in url:
            self.photo_calls += 1
            if "_JanBhagidari_" in url:
                raise FetchError("HTTP 404", 404)
            # Every Image1 upload returns the same bytes, so reuse must collapse to one file.
            return b"\xff\xd8\xffsame-image" if "Image1" in url else b"\xff\xd8\xffother-" + url.encode()
        target = (data or {}).get("__EVENTTARGET")
        self.calls.append(target)
        if data is None:
            name = "jsjb_start.html"
        elif target in ("ddlState", "ddlArea", "ddlBlockTown"):
            name = {"ddlState": "jsjb_state.html", "ddlArea": "jsjb_area.html",
                    "ddlBlockTown": "jsjb_block.html"}[target]
        elif data.get("btnSearch"):
            return (paged_results()[0] if self.paged else fixture("jsjb_results.html")).encode()
        elif target == "gvData":
            return paged_results()[1].encode()
        else:
            raise AssertionError(f"unexpected post {target}")
        return fixture(name).encode()


FILTERS = {"state": "punjab", "district": "SANGRUR", "area": "Rural", "block": "sangrur", "village": "Duggan"}


class JsjbOffline(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(clean_name("Duggan (67)"), "Duggan")
        self.assertEqual(upload_time("x/3_43_3_347_39781_20-05-2026-11-11-40-944_Image1.jpg"),
                         "2026-05-20T11:11:40")
        self.assertEqual(upload_time("x/3_43_R_347_39781_07042025123833604_JanBhagidari_Image1.jpg"),
                         "2025-04-07T12:38:33")
        self.assertIsNone(upload_time("x/photo.jpg"))

    def test_records_and_photos(self):
        ad = JsjbAdapter(FakeClient())
        areas = list(ad.list_areas(FILTERS))
        self.assertEqual([(a.village, a.village_code) for a in areas], [("Duggan", "39781")])
        recs = ad.list_records(areas[0])
        self.assertEqual(len(recs), 26)
        r = next(r for r in recs if r["record_id"] == "7438259")
        self.assertEqual((r["block"], r["village"], r["structure_type"], r["start_date"], r["end_date"]),
                         ("Sangrur", "Duggan", "Contour Trench", "2026-04-29", "2026-04-29"))
        self.assertEqual((r["lat"], r["lon"], r["district_code"], r["block_code"]),
                         (30.251337, 75.735085, "43", "347"))
        self.assertEqual(sum(len(ad.list_photos(r)) for r in recs), 27)

    def test_paging(self):
        ad = JsjbAdapter(FakeClient(paged=True))
        recs = ad.list_records(next(ad.list_areas(FILTERS)))
        self.assertEqual(len(recs), 52)
        self.assertEqual(len({r["record_id"] for r in recs}), 52)

    def test_bad_filter_and_layout_change(self):
        with self.assertRaises(FilterNotFound):
            list(JsjbAdapter(FakeClient()).list_areas(dict(FILTERS, village="Nowhere")))

        class Broken(FakeClient):
            def fetch(self, url, data=None, referer=None):
                if data and data.get("btnSearch"):
                    return fixture("jsjb_block.html").encode()
                return super().fetch(url, data, referer)
        ad = JsjbAdapter(Broken())
        with self.assertRaises(LayoutChanged):
            ad.list_records(next(ad.list_areas(FILTERS)))

    def test_changed_results_page_raises(self):
        base = fixture("jsjb_results.html")
        variants = {
            "data-url renamed": base.replace("data-url=", "data-src="),
            "cell removed": re.sub(r"<td>Others</td>", "", base),
            "total mismatch": base.replace("Total : 26", "Total : 40"),
        }
        for label, page in variants.items():
            class Changed(FakeClient):
                def fetch(self, url, data=None, referer=None):
                    if data and data.get("btnSearch"):
                        return page.encode()
                    return super().fetch(url, data, referer)
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                from collector.store import Store
                stats = collect(JsjbAdapter(Changed()), Store(d, "jsjb"), FILTERS)
                self.assertEqual([v["reason"] for v in stats["skipped_villages"]], ["layout"])
                self.assertIsNone(stats["stop"])
                with open(os.path.join(d, "records", "jsjb.jsonl"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), "")
                self.assertFalse(os.path.exists(os.path.join(d, "records", "jsjb.coverage.json"))
                                 and json.load(open(os.path.join(d, "records", "jsjb.coverage.json"))))

    def test_incremental_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            from collector.store import Store
            client = FakeClient()
            s1 = collect(JsjbAdapter(client), Store(d, "jsjb"), FILTERS)
            self.assertEqual((s1["records"], s1["new_records"]), (26, 26))
            first_photo_calls = client.photo_calls
            self.assertEqual(first_photo_calls, 27)
            with open(os.path.join(d, "records", "jsjb.jsonl"), encoding="utf-8") as f:
                rows = [json.loads(l) for l in f]
            self.assertEqual(len(rows), 26)
            shas = [p["sha256"] for r in rows for p in r["photos"] if p["sha256"]]
            stored = [f for _, _, fs in os.walk(os.path.join(d, "photos")) for f in fs]
            self.assertEqual(len(stored), len(set(shas)))
            with open(os.path.join(d, "records", "jsjb.coverage.json"), encoding="utf-8") as f:
                cov = json.load(f)
            self.assertEqual([(c["village"], c["records"]) for c in cov], [("Duggan", 26)])

            s2 = collect(JsjbAdapter(client), Store(d, "jsjb"), FILTERS)
            self.assertEqual((s2["records"], s2["new_records"], s2["photos_downloaded"]), (26, 0, 0))
            self.assertEqual(client.photo_calls, first_photo_calls)
            with open(os.path.join(d, "records", "jsjb.jsonl"), encoding="utf-8") as f:
                again = {r["record_id"]: r for r in map(json.loads, f)}
            self.assertTrue(all(again[r["record_id"]]["first_seen"] == r["first_seen"] for r in rows))

            searches = client.calls.count("")
            s3 = collect(JsjbAdapter(client), Store(d, "jsjb"), FILTERS, since_days=1)
            self.assertEqual((s3["villages"], s3["not_due"]), (0, 1))
            self.assertEqual(client.calls.count(""), searches)


class _Resp:
    def __init__(self, body=None, exc=None):
        self.body, self.exc = body, exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self.exc:
            raise self.exc
        out, self.body = self.body or b"", b""
        return out


class _Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.opened = 0

    def open(self, req, timeout=None):
        self.opened += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TransientFailures(unittest.TestCase):
    def setUp(self):
        import collector.http as h
        self._sleep = h.time.sleep
        h.time.sleep = lambda s: None

    def tearDown(self):
        import collector.http as h
        h.time.sleep = self._sleep

    def _client(self, responses):
        import urllib.robotparser
        from collector.http import Client
        c = Client(delay=0, retries=4)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse([])
        c._robots["https://example.test"] = rp
        c.opener = _Opener(responses)
        return c

    def test_incomplete_read_then_success(self):
        import http.client
        c = self._client([_Resp(exc=http.client.IncompleteRead(b"x" * 10, 5)), _Resp(b"full")])
        self.assertEqual(c.fetch("https://example.test/a.jpg"), b"full")
        self.assertEqual(c.opener.opened, 2)

    def test_other_transport_errors_retried(self):
        import http.client
        import ssl
        import socket
        import urllib.error
        c = self._client([ConnectionResetError("reset"), socket.timeout("slow"),
                          ssl.SSLError("bad record"), urllib.error.URLError(TimeoutError("t")),
                          _Resp(exc=http.client.RemoteDisconnected("gone"))])
        c.retries = 5
        with self.assertRaises(FetchError) as cm:
            c.fetch("https://example.test/a.jpg")
        self.assertIsNone(cm.exception.status)
        self.assertEqual(c.opener.opened, 5)

    def test_404_not_retried(self):
        import urllib.error
        c = self._client([urllib.error.HTTPError("https://example.test/a.jpg", 404, "nf", {}, None)])
        with self.assertRaises(FetchError) as cm:
            c.fetch("https://example.test/a.jpg")
        self.assertEqual((cm.exception.status, c.opener.opened), (404, 1))


class SurviveBadItems(unittest.TestCase):
    def test_failing_photo_retried_next_run(self):
        from collector.store import Store
        bad = "3_43_3_347_39781_20-05-2026-11-11-40-944_Image1"

        class Flaky(FakeClient):
            broken = True

            def fetch(self, url, data=None, referer=None):
                if bad in url and self.broken:
                    self.photo_calls += 1
                    raise FetchError(f"could not fetch {url}: IncompleteRead")
                return super().fetch(url, data, referer)

        with tempfile.TemporaryDirectory() as d:
            client = Flaky()
            s1 = collect(JsjbAdapter(client), Store(d, "jsjb"), FILTERS)
            store = Store(d, "jsjb")
            self.assertGreaterEqual(s1["photos_failed"], 1)
            self.assertEqual([u for u in store.photo_index if bad in u], [])

            client.broken = False
            before = client.photo_calls
            s2 = collect(JsjbAdapter(client), Store(d, "jsjb"), FILTERS)
            self.assertGreater(client.photo_calls, before)
            self.assertGreaterEqual(s2["photos_downloaded"], 1)
            store = Store(d, "jsjb")
            self.assertTrue(any(bad in u and e["sha256"] for u, e in store.photo_index.items()))

    def test_failing_village_skipped_and_not_covered(self):
        from collector.store import Store
        import collector.__main__ as runner

        class Down(FakeClient):
            requests = 0

            def fetch(self, url, data=None, referer=None):
                if data and data.get("btnSearch"):
                    raise FetchError(f"could not fetch {url}: IncompleteRead")
                return super().fetch(url, data, referer)

        with tempfile.TemporaryDirectory() as d:
            stats = collect(JsjbAdapter(Down()), Store(d, "jsjb"), FILTERS)
            self.assertEqual([v["name"] for v in stats["skipped_villages"]], ["PUNJAB > Sangrur > Sangrur > Duggan"])
            self.assertEqual(stats["villages"], 0)
            cov = os.path.join(d, "records", "jsjb.coverage.json")
            self.assertFalse(os.path.exists(cov) and json.load(open(cov, encoding="utf-8")))

            orig = runner.Client
            runner.Client = lambda delay: Down()
            try:
                import contextlib
                import io
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = runner.main(["jsjb", "--state", "Punjab", "--district", "Sangrur", "--area", "Rural",
                                        "--block", "Sangrur", "--village", "Duggan", "--data-dir", d])
            finally:
                runner.Client = orig
            self.assertEqual(code, 5)
            self.assertIn("Duggan", out.getvalue())

    def test_one_layout_failure_does_not_stop(self):
        from collector.store import Store

        class Broken(FakeClient):
            def fetch(self, url, data=None, referer=None):
                if data and data.get("btnSearch"):
                    return fixture("jsjb_block.html").encode()
                return super().fetch(url, data, referer)
        with tempfile.TemporaryDirectory() as d:
            stats = collect(JsjbAdapter(Broken()), Store(d, "jsjb"), FILTERS)
            self.assertIsNone(stats["stop"])
            self.assertEqual([v["reason"] for v in stats["skipped_villages"]], ["layout"])


if __name__ == "__main__":
    unittest.main()
