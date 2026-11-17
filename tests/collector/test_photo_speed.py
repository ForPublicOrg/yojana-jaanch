"""Photo rate, ban guard and the background photo pool. Offline: fake portals and fake clocks.
Run: python tests/collector/test_photo_speed.py"""
import argparse
import email.message
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)

import collector.__main__ as runner  # noqa: E402
from collector.__main__ import add_photo_options, collect, exit_code, photo_settings, photos_pass  # noqa: E402
from collector.http import Client, FetchError, retry_after_seconds  # noqa: E402
from collector.queue_runner import run_queue  # noqa: E402
from collector.schemes.jsjb import JsjbAdapter  # noqa: E402
from collector.store import Store  # noqa: E402
import collector.http as http_module  # noqa: E402
import collector.throttle as throttle_module  # noqa: E402
from collector.base import PortalChanged  # noqa: E402
from collector.throttle import (GUARD_PAUSE, HARD_CAP, MISSING_RUN, RETRY_AFTER_CAP, SUSPECT, Cancelled,  # noqa: E402
                                HostStopped, PhotoThrottle)
from demo_scheme import PHOTO_HOST, DemoAdapter, FakeDemoPortal  # noqa: E402
from test_resilience import ALL, ALL_VILLAGES, PHOTO, FakePortal  # noqa: E402

H = "jsjb.mowr.gov.in"


class FakeClock:
    """A clock that moves only when something sleeps on it. Safe to share between threads."""

    def __init__(self, t=1000.0):
        self.t = t
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += s


class Logs(logging.Handler):
    """Collects log lines (other test files turn logging off; this one turns it back on)."""

    def __init__(self, clock=None):
        super().__init__(logging.INFO)
        self.lines = []
        self.clock = clock

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage(), self.clock() if self.clock else None))


class WithLogs(unittest.TestCase):
    def setUp(self):
        self.was_disabled = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.logs = Logs()
        self.logger = logging.getLogger("collector")
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.logs)
        self.old_propagate = self.logger.propagate
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.logs)
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate
        logging.disable(self.was_disabled)

    def messages(self, level=logging.INFO):
        return [m for lv, m, _ in self.logs.lines if lv >= level]


def one(t, outcome, latency=0.4, **kw):
    tok = t.acquire()
    t.release(tok, outcome, latency=latency, **kw)
    return tok


class Throttle(WithLogs):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()

    def make(self, start, ceiling):
        return PhotoThrottle(H, start, ceiling, clock=self.clock, sleep=self.clock.sleep)

    def test_ramp_up_after_40_clean_photos(self):
        t = self.make(1, 4)
        # The first photo finds a free slot, so it does not count; the next 40 wait for theirs.
        for _ in range(40):
            one(t, "ok")
        self.assertEqual(t.rate, 1)
        one(t, "ok")
        self.assertEqual(t.rate, 2)
        self.assertIn("photos to jsjb.mowr.gov.in: 2 a second (40 photos waited for a slot without trouble, "
                      "median 0.40 s)", self.messages())
        for _ in range(40):
            one(t, "ok")
        self.assertEqual(t.rate, 3)
        for _ in range(500):
            one(t, "ok")
        # Never above the ceiling the caller gave.
        self.assertEqual(t.rate, 4)

    def test_no_ramp_up_while_slower_than_one_and_a_half_times_the_start(self):
        t = self.make(1, 4)
        for _ in range(20):
            one(t, "ok", 0.4)
        for _ in range(100):
            one(t, "ok", 0.7)
        self.assertEqual(t.rate, 1)

    def test_back_off_on_429_honours_retry_after(self):
        t = self.make(8, 8)
        one(t, "ok")
        before = self.clock()
        one(t, "throttled", retry_after=30)
        self.assertEqual(t.rate, 4)
        tok = t.acquire()
        self.assertGreaterEqual(tok.started, before + 30)
        t.release(tok, "ok", 0.4)
        # Page requests wait for it too.
        before = self.clock()
        one(t, "throttled", retry_after=60)
        t.wait_if_paused()
        self.assertGreaterEqual(self.clock(), before + 60)
        # A Retry-After of a day is cut to the cap.
        before = self.clock()
        one(t, "throttled", retry_after=86400)
        tok = t.acquire()
        self.assertLessEqual(tok.started - before, RETRY_AFTER_CAP + 1)
        self.assertGreaterEqual(tok.started - before, RETRY_AFTER_CAP)

    def test_retry_after_header_forms(self):
        self.assertEqual(retry_after_seconds("120"), 120.0)
        self.assertIsNone(retry_after_seconds("soon"))
        self.assertIsNone(retry_after_seconds(None))
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 90))
        self.assertTrue(80 <= retry_after_seconds(later) <= 91)

    def test_back_off_on_errors_halves_with_floor_one(self):
        t = self.make(8, 8)
        rates = []
        for _ in range(4):
            one(t, "error")
            rates.append(t.rate)
        self.assertEqual(rates, [4, 2, 1, 1])

    def test_latency_back_off(self):
        t = self.make(4, 4)
        for _ in range(20):
            one(t, "ok", 0.4)
        self.assertEqual(t.baseline, 0.4)
        for _ in range(20):
            one(t, "ok", 1.0)
        self.assertEqual(t.rate, 3)
        self.assertTrue(any("over 2 times the first 0.40 s" in m for m in self.messages()))

    def test_slow_answers_do_not_lower_the_ceiling(self):
        t = self.make(4, 4)
        for _ in range(20):
            one(t, "ok", 0.4)
        # Slow twice at 4 a second: each time it steps down, and the ceiling stays 4.
        for _ in range(2):
            while t.rate == 4:
                one(t, "ok", 1.0)
            self.assertEqual(t.rate, 3)
            while t.rate < 4:
                one(t, "ok", 0.4)
        self.assertEqual((t.rate, t.ceiling), (4, 4))
        self.assertFalse(any("from now on" in m for m in self.messages()))

    def test_no_ramp_up_for_120_photos_after_a_back_off(self):
        t = self.make(4, 8)
        for _ in range(20):
            one(t, "ok")
        one(t, "error")
        self.assertEqual(t.rate, 2)
        for _ in range(119):
            one(t, "ok")
        self.assertEqual(t.rate, 2)
        one(t, "ok")
        self.assertEqual(t.rate, 3)

    def test_a_rate_that_backed_off_twice_becomes_the_ceiling_minus_one(self):
        t = self.make(4, 6)
        for _ in range(20):
            one(t, "ok")
        one(t, "throttled")
        while t.rate < 4:
            one(t, "ok")
        one(t, "throttled")
        self.assertEqual((t.rate, t.ceiling), (2, 3))
        self.assertIn("photos to jsjb.mowr.gov.in: at most 3 a second for now (4 a second backed off twice "
                      f"within {throttle_module.LEARN_WINDOW} photos)", self.messages())
        for _ in range(2000):
            one(t, "ok")
        self.assertEqual(t.rate, 3)
        # After a long clean stretch the ceiling comes back, one step at a time, never above 6.
        for _ in range(throttle_module.RECOVER_AFTER * 4):
            one(t, "ok")
        self.assertEqual(t.ceiling, 6)
        self.assertIn(f"photos to jsjb.mowr.gov.in: at most 4 a second again ({throttle_module.RECOVER_AFTER} "
                      "photos without trouble)", self.messages())

    def test_back_offs_far_apart_do_not_lower_the_ceiling(self):
        t = self.make(4, 4)
        for _ in range(20):
            one(t, "ok")
        one(t, "throttled")
        for _ in range(throttle_module.LEARN_WINDOW + 10):
            one(t, "ok")
        self.assertEqual(t.rate, 4)
        one(t, "throttled")
        self.assertEqual((t.rate, t.ceiling), (2, 4))

    def test_a_lone_failure_after_good_photos_steps_down_by_one(self):
        t = self.make(8, 8)
        for _ in range(throttle_module.ISOLATED_AFTER):
            one(t, "ok")
        one(t, "error", detail="IncompleteRead")
        self.assertEqual(t.rate, 7)
        # A second failure soon after is not lone: it halves.
        one(t, "ok")
        one(t, "error")
        self.assertEqual(t.rate, 3)
        # Lone failures never lower the ceiling, however many there are.
        for _ in range(5):
            for _ in range(throttle_module.ISOLATED_AFTER):
                one(t, "ok")
            one(t, "error")
        self.assertEqual(t.ceiling, 8)
        # Throttling still halves at once, lone or not.
        for _ in range(throttle_module.ISOLATED_AFTER):
            one(t, "ok")
        before = t.rate
        one(t, "throttled")
        self.assertEqual(t.rate, max(1, before // 2))

    def test_no_ramp_up_when_photos_never_wait(self):
        # Photos that always find a free slot do not show the portal takes a higher rate.
        t = self.make(2, 8)
        for _ in range(500):
            one(t, "ok")
            self.clock.sleep(1.0)
        self.assertEqual(t.rate, 2)
        # They do not reset the count either: photos that do wait still move it up.
        for _ in range(41):
            one(t, "ok")
        self.assertEqual(t.rate, 3)

    def test_a_page_answered_normally_resets_the_missing_run(self):
        t = self.make(4, 4)
        for _ in range(MISSING_RUN):
            one(t, "missing")
        tok = t.acquire_page()
        t.release(tok, "ok", latency=0.2)
        for _ in range(MISSING_RUN):
            self.assertEqual(t.release(t.acquire(), "missing"), "missing")
        self.assertEqual(t.pauses_in_a_row, 0)
        self.assertEqual(t.release(t.acquire(), "missing"), SUSPECT)

    def test_missing_is_neutral(self):
        t = self.make(1, 4)
        for _ in range(40):
            one(t, "ok")
        for _ in range(10):
            one(t, "missing")
        self.assertEqual(t.rate, 1)
        one(t, "ok")
        self.assertEqual(t.rate, 2)
        # Missing photos neither end a trouble streak nor add to it.
        g = self.make(2, 2)
        for outcome in ("error", "error", "missing", "error", "error", "missing", "missing"):
            one(g, outcome)
        self.assertEqual(g.pauses_in_a_row, 0)
        one(g, "error")
        self.assertEqual(g.pauses_in_a_row, 1)

    def test_ok_ends_a_trouble_streak(self):
        t = self.make(2, 2)
        for outcome in ("error", "not_image", "throttled", "error", "ok", "error", "error", "error", "error"):
            one(t, outcome)
        self.assertEqual(t.pauses_in_a_row, 0)

    def test_ban_guard_pauses_pages_and_photos_then_resumes(self):
        t = self.make(4, 4)
        for _ in range(5):
            one(t, "not_image")
        tripped = self.clock()
        self.assertEqual((t.rate, t.pauses_in_a_row), (1, 1))
        warn = self.messages(logging.WARNING)
        self.assertTrue(any("5 failed in a row (not_image 5)" in m and "for 10 minutes" in m for m in warn), warn)
        t.wait_if_paused()
        self.assertGreaterEqual(self.clock(), tripped + GUARD_PAUSE)
        tok = t.acquire()
        self.assertGreaterEqual(tok.started, tripped + GUARD_PAUSE)
        t.release(tok, "ok", 0.4)
        self.assertEqual(t.pauses_in_a_row, 0)
        self.assertIsNone(t.stopped)

    def test_ban_guard_stops_after_three_pauses(self):
        t = self.make(2, 2)
        for _ in range(5):
            one(t, "error")
        for pause in (2, 3):
            for _ in range(3):
                one(t, "error")
            self.assertEqual(t.pauses_in_a_row, pause)
        for _ in range(2):
            one(t, "error")
        self.assertIsNone(t.stopped)
        one(t, "error")
        self.assertEqual(t.stopped[0], "network")
        with self.assertRaises(HostStopped) as cm:
            t.acquire()
        self.assertEqual(cm.exception.kind, "network")
        with self.assertRaises(HostStopped):
            t.wait_if_paused()
        # A new collect run on the same client (the queue) may try again, at 1 a second.
        t.new_run()
        self.assertIsNone(t.stopped)
        self.assertEqual(t.rate, 1)

    def test_refused_streak_stops_at_once(self):
        t = self.make(4, 4)
        for _ in range(4):
            one(t, "refused")
        self.assertIsNone(t.stopped)
        one(t, "refused")
        self.assertEqual(t.stopped[0], "access")
        self.assertEqual(t.pauses_in_a_row, 0)
        self.assertIn("refuses access", t.stopped[1])

    def test_rate_never_above_the_cap_and_starts_evenly_spaced(self):
        t = self.make(40, 40)
        self.assertEqual((t.rate, t.ceiling), (HARD_CAP, HARD_CAP))
        starts, lock = [], threading.Lock()

        def work():
            for _ in range(25):
                tok = t.acquire()
                with lock:
                    starts.append(tok.started)
                self.clock.sleep(0.3)
                t.release(tok, "ok", 0.3)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 200)
        self.assertGreaterEqual(min(gaps), 1 / HARD_CAP - 1e-9)
        # One at a time: a full second apart.
        s = self.make(1, 1)
        times = [one(s, "ok").started for _ in range(5)]
        self.assertEqual([round(b - a, 9) for a, b in zip(times, times[1:])], [1.0] * 4)

    def test_more_threads_than_the_cap_never_get_more_than_the_cap_in_flight(self):
        t = self.make(40, 40)
        go = threading.Event()
        tokens, lock = [], threading.Lock()

        def work():
            tok = t.acquire()
            with lock:
                tokens.append(tok)
            go.wait(10)
            t.release(tok, "ok", 0.3)
        threads = [threading.Thread(target=work, daemon=True) for _ in range(24)]
        for th in threads:
            th.start()
        deadline = time.monotonic() + 5
        while len(tokens) < HARD_CAP and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        # Every slot is held; the other 8 threads wait.
        self.assertEqual((len(tokens), t.in_flight), (HARD_CAP, HARD_CAP))
        go.set()
        for th in threads:
            th.join(10)
        self.assertEqual(len(tokens), 24)
        self.assertEqual(t.max_in_flight, HARD_CAP)

    def test_ramp_up_stops_at_the_hard_cap(self):
        t = self.make(1, 40)
        for _ in range(2000):
            one(t, "ok")
        self.assertEqual((t.rate, t.ceiling), (HARD_CAP, HARD_CAP))

    def test_refused_or_html_between_good_photos_slow_down_and_trip_the_guard(self):
        t = self.make(8, 8)
        for n in range(1, 61):
            one(t, "refused" if n % 3 == 0 else "ok")
            if t.stopped:
                break
        self.assertEqual(t.rate, 1)
        self.assertEqual(t.stopped[0], "access")
        self.assertIn("of the last 20 were refused", t.stopped[1])
        self.assertLessEqual(t.counts["refused"], 7)
        h = self.make(8, 8)
        for n in range(1, 22):
            one(h, "not_image" if n % 3 == 0 else "ok")
        self.assertEqual((h.rate, h.pauses_in_a_row), (1, 1))
        self.assertTrue(any("7 failed of the last 20 (not_image 7)" in m for m in self.messages(logging.WARNING)))

    def test_a_few_bad_photos_among_many_good_ones_do_not_trip_the_guard(self):
        t = self.make(4, 4)
        for n in range(1, 201):
            one(t, "error" if n % 4 == 0 else "ok")
        self.assertEqual(t.pauses_in_a_row, 0)
        self.assertIsNone(t.stopped)

    def test_a_long_run_of_missing_is_no_longer_believed(self):
        t = self.make(8, 8)
        one(t, "ok")
        got = [t.release(t.acquire(), "missing") for _ in range(MISSING_RUN)]
        self.assertEqual(set(got), {"missing"})
        self.assertEqual((t.rate, t.pauses_in_a_row), (8, 0))
        self.assertEqual(t.release(t.acquire(), "missing"), SUSPECT)
        self.assertEqual(t.rate, 4)
        for _ in range(4):
            self.assertEqual(t.release(t.acquire(), "missing"), SUSPECT)
        self.assertEqual((t.rate, t.pauses_in_a_row), (1, 1))
        self.assertIn("5 of the missing not believed", t.summary())
        # A good photo makes missing answers believable again.
        one(t, "ok")
        self.assertEqual(t.release(t.acquire(), "missing"), "missing")

    def test_retry_after_from_a_request_started_before_a_pause_is_kept(self):
        t = self.make(8, 8)
        toks = [t.acquire() for _ in range(8)]
        for tok in toks[:5]:
            t.release(tok, "error", 0.4)
        self.assertEqual(t.pauses_in_a_row, 1)
        asked = self.clock()
        for tok in toks[5:]:
            t.release(tok, "throttled", 0.4, retry_after=900)
        self.assertGreaterEqual(t.acquire().started, asked + 900)

    def test_pages_take_a_slot_ahead_of_photos_and_report_429(self):
        t = self.make(2, 2)
        a = t.acquire()
        p = t.acquire_page()
        self.assertEqual((t.in_flight, t.counts["requests"], t.counts["pages"]), (2, 1, 1))
        self.assertAlmostEqual(p.started - a.started, 0.5)
        t.release(a, "ok", 0.4)
        before = self.clock()
        t.release(p, "throttled", retry_after=120)
        self.assertEqual(t.rate, 1)
        self.assertGreaterEqual(t.acquire().started, before + 120)
        # Pages back off the rate but never add to the guard's streak.
        g = self.make(4, 4)
        for _ in range(10):
            g.release(g.acquire_page(), "error")
        self.assertEqual((g.rate, g.pauses_in_a_row, g.counts["error"]), (1, 0, 0))

    def test_a_waiting_page_goes_before_waiting_photos(self):
        t = self.make(1, 1)
        held = t.acquire()
        order = []
        photo = threading.Thread(target=lambda: order.append(("photo", t.acquire())), daemon=True)
        photo.start()
        time.sleep(0.1)
        page = threading.Thread(target=lambda: order.append(("page", t.acquire_page())), daemon=True)
        page.start()
        time.sleep(0.1)
        t.release(held, "ok", 0.4)
        page.join(5)
        self.assertEqual(order[0][0], "page")
        t.release(order[0][1], "ok")
        photo.join(5)
        self.assertEqual([k for k, _ in order], ["page", "photo"])

    def test_hold_stops_photos_and_pages_until_unhold(self):
        t = self.make(2, 2)
        start = self.clock()
        t.hold(300)
        self.assertGreaterEqual(one(t, "ok").started, start + 300)
        t.hold(300)
        page = t.acquire_page()
        t.release(page, "ok")
        self.assertGreaterEqual(page.started, start + 600)
        t.hold(300)
        t.unhold()
        start = self.clock()
        self.assertLess(one(t, "ok").started, start + 300)

    def test_in_flight_bound(self):
        t = self.make(2, 2)
        a, b = t.acquire(), t.acquire()
        got = []
        th = threading.Thread(target=lambda: got.append(t.acquire()), daemon=True)
        th.start()
        th.join(0.4)
        self.assertTrue(th.is_alive())
        self.assertEqual(got, [])
        t.release(a, "ok", 0.4)
        th.join(3)
        self.assertFalse(th.is_alive())
        self.assertEqual(len(got), 1)
        self.assertEqual(t.in_flight, 2)
        t.release(b, "ok", 0.4)
        t.release(got[0], "ok", 0.4)
        # Many threads holding their slot for a while never exceed the rate.
        held, lock, peak = [0], threading.Lock(), [0]

        def work():
            for _ in range(5):
                tok = t.acquire()
                with lock:
                    held[0] += 1
                    peak[0] = max(peak[0], held[0])
                time.sleep(0.01)
                with lock:
                    held[0] -= 1
                t.release(tok, "ok", 0.4)
        threads = [threading.Thread(target=work) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        self.assertLessEqual(peak[0], 2)

    def test_cancel_ends_a_wait(self):
        t = self.make(1, 1)
        for _ in range(5):
            one(t, "error")
        ev = threading.Event()
        ev.set()
        with self.assertRaises(Cancelled):
            t.acquire(ev)


# A real Client whose network is a fake: pages come from FakePortal, photos from a function.

class Response:
    def __init__(self, body, url, ctype):
        self.body = io.BytesIO(body)
        self.url = url
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype
        self.headers["Content-Length"] = str(len(body))

    def read(self, n=-1):
        return self.body.read(n)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, portal, photo, clock):
        self.portal, self.photo, self.clock = portal, photo, clock
        self.log = []
        self.lock = threading.Lock()

    def open(self, req, timeout=None):
        url = req.full_url
        if "/PhotoJSJB2/" in url:
            with self.lock:
                self.log.append(("photo", url, self.clock()))
            got = self.photo(url)
            if isinstance(got, BaseException):
                raise got
            status, body, headers = got
            if status != 200:
                msg = email.message.Message()
                for k, v in headers.items():
                    msg[k] = v
                raise urllib.error.HTTPError(url, status, "fake", msg, io.BytesIO(body))
            return Response(body, headers.get("final_url", url), "image/jpeg")
        data = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else None
        with self.lock:
            self.log.append(("page", url, self.clock()))
        return Response(self.portal.fetch(url, data), url, "text/html")


class NoNetwork:
    def open(self, req, timeout=None):
        raise AssertionError(f"a test tried the real network: {req.full_url}")


class FakeNetClient(Client):
    """Photo worker clients use the same fake network as the page client."""

    def photo_clients(self, n):
        out = super().photo_clients(n)
        for c in out:
            c.opener = self.opener
        return out


def real_client(portal, photo, clock, retries=1):
    c = FakeNetClient(delay=0, retries=retries, hosts={H: {"delay": 0, "retries": retries}})
    c.opener = Opener(portal, photo, clock)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse([])
    c._robots["https://" + H] = rp
    c.set_photo_rate(1, 1, clock=clock, sleep=clock.sleep)
    return c


def good_photo(url):
    return 200, FakePortal().photo_bytes(url), {}


class Temp(WithLogs):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def coverage(self, d=None):
        return {str(c["village_code"]): c for c in Store(d or self.d, "jsjb").coverage.values()}


class RealClient(Temp):
    def setUp(self):
        super().setUp()
        # Any client built without the fake network fails at once instead of reaching a portal.
        self.real_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a: NoNetwork()

    def tearDown(self):
        urllib.request.build_opener = self.real_build
        super().tearDown()

    def test_each_retry_goes_through_the_throttle(self):
        clock = FakeClock()
        answers = [(503, b"busy", {}), (503, b"busy", {}), None]

        def photo(url):
            got = answers.pop(0)
            return got or good_photo(url)
        c = real_client(FakePortal(), photo, clock, retries=3)
        c.set_photo_rate(8, 8)
        content, _ = c.fetch_image(PHOTO + "x.jpg", 10 ** 6)
        self.assertTrue(content.startswith(b"\xff\xd8"))
        t = c.throttle(H)
        self.assertEqual((t.counts["throttled"], t.counts["ok"], t.counts["requests"]), (2, 1, 3))
        self.assertEqual(t.rate, 2)
        # The waits between tries (2 s, then 4 s) were taken on the throttle's clock.
        self.assertGreaterEqual(clock() - 1000.0, 6)

    def test_timeouts_back_off_and_every_attempt_counts(self):
        clock = FakeClock()
        c = real_client(FakePortal(), lambda url: TimeoutError("timed out"), clock, retries=3)
        c.set_photo_rate(8, 8)
        with self.assertRaises(FetchError):
            c.fetch_image(PHOTO + "x.jpg", 10 ** 6)
        t = c.throttle(H)
        self.assertEqual((t.counts["error"], t.rate), (3, 1))

    def test_outcomes_of_missing_refused_and_html_photos(self):
        clock = FakeClock()
        answers = {PHOTO + "gone.jpg": (404, b"", {}),
                   PHOTO + "moved.jpg": (200, b"<html>not found</html>", {"final_url": "https://" + H + "/PageNotFound.aspx"}),
                   PHOTO + "no.jpg": (403, b"", {}),
                   PHOTO + "html.jpg": (200, b"<html>blocked</html>", {})}
        c = real_client(FakePortal(), lambda url: answers[url], clock)
        c.set_photo_rate(2, 2)
        for url in answers:
            try:
                c.fetch_image(url, 10 ** 6)
            except FetchError:
                pass
        counts = c.throttle(H).counts
        self.assertEqual((counts["missing"], counts["refused"], counts["not_image"]), (2, 1, 1))
        # One refused photo is not a refusal of the host.
        self.assertEqual(c.denied, {})

    def guarded_run(self, photo, workers=1, retries=1, **kw):
        clock = FakeClock()
        portal = FakePortal()
        client = real_client(portal, photo, clock, retries=retries)
        logs = Logs(clock)
        logging.getLogger("collector").addHandler(logs)
        try:
            stats = collect(JsjbAdapter(client), Store(self.d, "jsjb"), ALL, sleep=lambda s: None,
                            photo_workers=workers, **kw)
        finally:
            logging.getLogger("collector").removeHandler(logs)
        return stats, client, logs

    def test_pages_wait_out_a_guard_pause(self):
        stats, client, logs = self.guarded_run(lambda url: TimeoutError("timed out"))
        pauses = [t for lv, m, t in logs.lines if lv == logging.WARNING and "for 10 minutes" in m]
        self.assertEqual(len(pauses), 3)
        pages = [t for kind, _, t in client.opener.log if kind == "page"]
        for start in pauses:
            self.assertFalse([p for p in pages if start <= p < start + GUARD_PAUSE], start)
        # 12 photos, one try each: 5, then 3 and 3 after the first two pauses, then 1 in probation.
        self.assertEqual(sum(1 for kind, _, _ in client.opener.log if kind == "photo"), 12)
        self.assertIsNone(stats["stop"])
        self.assertEqual((stats["photos_failed"], stats["photos_missing"]), (12, 0))

    def test_guard_stop_ends_the_run_like_a_network_stop(self):
        stats, client, _ = self.guarded_run(lambda url: ConnectionResetError("reset"), retries=2)
        self.assertEqual(stats["stop"][0], "network")
        self.assertEqual(exit_code(stats), runner.EXIT["network"])
        self.assertIn("pauses of 10 minutes", stats["stop"][1])
        # 5 + 3 + 3 + 3 tries; after the stop no request at all, pages included.
        log = client.opener.log
        self.assertEqual(sum(1 for kind, _, _ in log if kind == "photo"), 14)
        last_photo = max(i for i, e in enumerate(log) if e[0] == "photo")
        self.assertFalse([e for e in log[last_photo + 1:] if e[0] == "page"])
        # Photos the guard stopped are failed (retried next run), never missing.
        self.assertEqual(stats["photos_missing"], 0)
        self.assertEqual(Store(self.d, "jsjb").photo_index, {})
        cov = self.coverage()
        self.assertTrue(cov)
        self.assertTrue(all(c["photos_failed"] for c in cov.values()))

    def test_refused_photos_stop_the_run_as_access(self):
        stats, client, _ = self.guarded_run(lambda url: (403, b"", {}))
        self.assertEqual(stats["stop"][0], "access")
        self.assertEqual(exit_code(stats), runner.EXIT["access"])
        self.assertEqual(sum(1 for kind, _, _ in client.opener.log if kind == "photo"), 5)
        self.assertEqual(stats["photos_missing"], 0)

    def test_guard_stop_with_the_pool_saves_what_was_listed(self):
        stats, client, _ = self.guarded_run(lambda url: ConnectionResetError("reset"), workers=4, retries=2)
        self.assertEqual(stats["stop"][0], "network")
        self.assertEqual(stats["photos_missing"], 0)
        cov = self.coverage()
        self.assertTrue(cov)
        self.assertTrue(all(c["photos_failed"] for c in cov.values()))
        self.assertEqual(Store(self.d, "jsjb").photo_index, {})

    def test_pages_and_photos_together_stay_within_the_rate(self):
        starts, lock = [], threading.Lock()
        real_take = throttle_module.PhotoThrottle._take

        def take(t, cancel, page):
            tok = real_take(t, cancel, page)
            with lock:
                starts.append((tok.started, page))
            return tok
        throttle_module.PhotoThrottle._take = take
        try:
            clock = FakeClock()
            client = real_client(FakePortal(), good_photo, clock)
            stats = collect(JsjbAdapter(client), Store(self.d, "jsjb"), ALL, sleep=lambda s: None, photo_workers=4)
        finally:
            throttle_module.PhotoThrottle._take = real_take
        self.assertEqual((stats["photos_downloaded"], exit_code(stats)), (12, 0))
        self.assertTrue(any(page for _, page in starts))
        times = sorted(s for s, _ in starts)
        self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.25 - 1e-9)
        self.assertLessEqual(client.throttle(H).max_in_flight, 4)

    def test_a_page_answered_429_holds_photos_and_waits_its_retry_after(self):
        clock = FakeClock()
        client = real_client(FakePortal(), good_photo, clock, retries=2)
        client.set_photo_rate(4, 4)
        client.fetch_image(PHOTO + "a.jpg", 10 ** 6)
        t = client.throttle(H)
        opener = client.opener
        busy = [True]

        class Busy:
            def open(self, req, timeout=None):
                if req.data and busy[0]:
                    busy[0] = False
                    msg = email.message.Message()
                    msg["Retry-After"] = "120"
                    raise urllib.error.HTTPError(req.full_url, 429, "busy", msg, io.BytesIO(b""))
                return opener.open(req, timeout)
        client.opener = Busy()
        slept = []

        class Time:
            monotonic = staticmethod(time.monotonic)
            time = staticmethod(time.time)
            sleep = staticmethod(slept.append)
        real_time = http_module.time
        http_module.time = Time
        try:
            before = clock()
            client.fetch("https://" + H + "/search.aspx", data={"ddlState": "1", "__EVENTTARGET": "ddlState"})
        finally:
            http_module.time = real_time
        self.assertEqual(slept, [120])
        self.assertEqual(t.rate, 2)
        self.assertGreaterEqual(t.acquire().started, before + 120)

    def test_a_long_run_of_404_photos_is_recorded_as_failed_not_missing(self):
        clock = FakeClock()
        client = real_client(FakePortal(), lambda url: (404, b"", {}), clock)
        store = Store(self.d, "jsjb")
        got = [runner.fetch_photo(client, store, f"{PHOTO}{n}.jpg")[1] for n in range(MISSING_RUN + 3)]
        self.assertEqual(got, ["missing"] * MISSING_RUN + ["failed"] * 3)
        self.assertEqual(len(store.photo_index), MISSING_RUN)
        self.assertNotIn(f"{PHOTO}{MISSING_RUN}.jpg", store.photo_index)

    def test_pool_on_the_real_client_stores_what_one_at_a_time_stores(self):
        out = []
        for workers in (1, 6, "auto"):
            d = os.path.join(self.tmp.name, str(workers))
            clock = FakeClock()
            client = real_client(FakePortal(), good_photo, clock)
            stats = collect(JsjbAdapter(client), Store(d, "jsjb"), ALL, sleep=lambda s: None, photo_workers=workers)
            self.assertEqual((stats["photos_downloaded"], exit_code(stats)), (12, 0))
            self.assertEqual(client.requests, len(client.opener.log))
            store = Store(d, "jsjb")
            out.append(({k: [(p["url"], p["sha256"], os.path.basename(p["file"])) for p in r["photos"]]
                         for k, r in store.records.items()},
                        {u: e["sha256"] for u, e in store.photo_index.items()}))
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0], out[2])


class SlowPortal(FakePortal):
    """Photos take `slow` seconds (real time); pages are checked for overlap with each other."""

    def __init__(self, faults=None, slow=0.0, slow_for=None, page_time=0.0):
        super().__init__(faults)
        self.slow, self.slow_for, self.page_time = slow, slow_for, page_time
        self.lock = threading.Lock()
        self.pages_now = self.photos_now = 0
        self.max_pages = 0
        self.overlap = 0

    def fetch_image(self, url, max_bytes):
        with self.lock:
            self.photos_now += 1
        try:
            if self.slow and (self.slow_for is None or any(s in url for s in self.slow_for)):
                time.sleep(self.slow)
            return super().fetch_image(url, max_bytes)
        finally:
            with self.lock:
                self.photos_now -= 1

    def fetch(self, url, data=None, referer=None):
        with self.lock:
            self.pages_now += 1
            self.max_pages = max(self.max_pages, self.pages_now)
            if self.photos_now:
                self.overlap += 1
        try:
            if self.page_time:
                time.sleep(self.page_time)
            return super().fetch(url, data, referer)
        finally:
            with self.lock:
                self.pages_now -= 1


class Pool(Temp):
    def run_jsjb(self, portal, **kw):
        store = Store(self.d, "jsjb")
        order = []
        real = store.commit

        def commit(area, *a, **k):
            order.append(area.village_code)
            return real(area, *a, **k)
        store.commit = commit
        stats = collect(JsjbAdapter(portal), store, ALL, sleep=lambda s: None, **kw)
        return stats, order

    def test_saved_in_list_order_even_when_later_photos_finish_first(self):
        portal = SlowPortal(slow=0.3, slow_for=["1111_"])
        stats, order = self.run_jsjb(portal, photo_workers=4)
        self.assertEqual(order, ALL_VILLAGES)
        self.assertEqual((stats["villages"], stats["photos_downloaded"]), (6, 12))
        lines = [m for m in self.messages() if "photos downloaded" in m]
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].endswith(": 2 records (2 new), 2 photos downloaded"))

    def test_drain_on_max_villages(self):
        portal = SlowPortal(slow=0.2)
        stats, order = self.run_jsjb(portal, photo_workers=4, max_villages=2)
        self.assertEqual(order, ALL_VILLAGES[:2])
        self.assertEqual((stats["villages"], stats["photos_downloaded"]), (2, 4))
        self.assertEqual(len(portal.photo_calls), 4)
        self.assertEqual(stats["stopped"], "--max-villages 2 reached")

    def test_drain_on_a_stop(self):
        portal = SlowPortal({("search", "1121"): FetchError("HTTP 403", 403, denied=True)}, slow=0.3)
        stats, order = self.run_jsjb(portal, photo_workers=4)
        self.assertEqual(stats["stop"][0], "access")
        self.assertEqual(order, ["1111", "1112"])
        self.assertEqual(stats["photos_downloaded"], 4)
        self.assertEqual(set(self.coverage()), {"1111", "1112"})

    def test_a_photo_shared_by_two_villages_is_downloaded_once(self):
        shared = f"{PHOTO}1111_1_Image1.jpg"

        class Shared(SlowPortal):
            def _page(self, data):
                out = super()._page(data)
                return out.replace(f"{PHOTO}1112_1_Image1.jpg", shared) if isinstance(out, str) else out
        portal = Shared(slow=0.3, slow_for=["1111_1_"])
        stats, order = self.run_jsjb(portal, photo_workers=4)
        self.assertEqual(portal.photo_calls.count(shared), 1)
        self.assertEqual(stats["photos_downloaded"], 11)
        recs = Store(self.d, "jsjb").records
        self.assertEqual(recs["1111100"]["photos"][0]["sha256"], recs["1112100"]["photos"][0]["sha256"])

    def test_ctrl_c_leaves_unsaved_villages_due(self):
        portal = SlowPortal({("search", "1122"): KeyboardInterrupt()}, slow=0.3)
        before = threading.active_count()
        began = time.monotonic()
        stats, order = self.run_jsjb(portal, photo_workers=4)
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(stats["stop"][0], "interrupted")
        self.assertNotIn("1121", order)
        self.assertNotIn("1121", self.coverage())
        deadline = time.monotonic() + 3
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), before)
        stats = collect(JsjbAdapter(FakePortal()), Store(self.d, "jsjb"), ALL, since_days=30, sleep=lambda s: None,
                        photo_workers=4)
        self.assertEqual(set(self.coverage()), set(ALL_VILLAGES))
        self.assertGreaterEqual(stats["villages"], 3)

    def interrupting_store(self, d):
        store = Store(d, "jsjb")

        def commit(*a, **k):
            raise KeyboardInterrupt
        store.commit = commit
        return store

    def test_ctrl_c_while_saving_after_a_stop_is_an_interrupt(self):
        portal = SlowPortal({("search", "1121"): PortalChanged("form gone")}, slow=0.3)
        stats = collect(JsjbAdapter(portal), self.interrupting_store(self.d), ALL, sleep=lambda s: None,
                        photo_workers=4)
        self.assertEqual(stats["stop"][0], "interrupted")
        self.assertIn("portal layout changed: form gone", stats["stop"][1])
        self.assertEqual(exit_code(stats), 130)
        self.assertEqual(self.coverage(), {})

    def test_ctrl_c_while_saving_after_a_stop_ends_the_queue(self):
        calls = []

        def collect_fn(adapter, store, filters, **kw):
            calls.append(filters)
            return collect(adapter, store, filters, sleep=lambda s: None, **kw)
        plan = {"scheme": "jsjb", "units": [{"phase": "p", "state": "Alpha", "district": "A-One", "area": "Rural"},
                                            {"phase": "p", "state": "Beta", "district": "B-One", "area": "Rural"}]}
        portal = SlowPortal({("search", "1121"): FetchError("down")}, slow=0.3)
        slept = []
        code = run_queue(plan, JsjbAdapter(portal), self.interrupting_store(self.d), self.d, photo_workers=4,
                         collect_fn=lambda a, s, f, **kw: collect_fn(a, s, f, max_network_failures=1,
                                                                     max_outage_pauses=0, **kw),
                         check=None, out=lambda s: None, sleep=slept.append)
        self.assertEqual(code, 130)
        self.assertEqual(len(calls), 1)
        self.assertEqual(slept, [])

    def test_save_failures_stop_a_pool_run_as_they_stop_a_run_without_one(self):
        for workers in (1, 4):
            d = os.path.join(self.tmp.name, str(workers))
            store = Store(d, "jsjb")

            def commit(*a, **k):
                raise TypeError("broken commit")
            store.commit = commit
            portal = FakePortal()
            stats = collect(JsjbAdapter(portal), store, ALL, sleep=lambda s: None, photo_workers=workers,
                            max_layout_failures=4)
            self.assertEqual(stats["stop"][0], "layout", workers)
            self.assertEqual(exit_code(stats), 2)
            searches = sum(n for k, n in portal.calls.items() if isinstance(k, tuple) and k[0] == "search")
            self.assertLessEqual(searches, 4 + runner.MAX_PENDING, workers)

    def test_photos_pass_save_failures_count_with_the_pool(self):
        for workers in (1, 4):
            d = os.path.join(self.tmp.name, f"demo{workers}")
            collect(DemoAdapter(FakeDemoPortal()), Store(d, "demo"), {"state": None, "district": None, "gp": None},
                    list_only=True)
            store = Store(d, "demo")

            def write_record(rec):
                raise TypeError("broken write")
            store.write_record = write_record
            stats = photos_pass(DemoAdapter(FakeDemoPortal()), store, photo_workers=workers, max_failures=2)
            self.assertEqual(stats["stop"][0], "layout", workers)
            self.assertIn("2 records in a row", stats["stop"][1])
            # With the pool, a record still pending at the stop is tried in the drain as well.
            self.assertIn(len(stats["skipped_records"]), (2,) if workers == 1 else (2, 3), workers)

    def test_an_outage_pause_holds_the_photo_workers(self):
        events = []

        class Recorder:
            stopped = None

            def hold(self, s):
                events.append(("hold", s))

            def unhold(self):
                events.append(("unhold",))

            def summary(self):
                return "recorder"
        portal = FakePortal({("search", v): FetchError("down") for v in ALL_VILLAGES})
        portal.throttles = {H: Recorder()}
        stats = collect(JsjbAdapter(portal), Store(self.d, "jsjb"), ALL, max_network_failures=2,
                        max_outage_pauses=1, outage_pause=7, sleep=lambda s: events.append(("sleep", s)),
                        photo_workers=4)
        self.assertEqual(events, [("hold", 7), ("sleep", 7), ("unhold",)])
        self.assertEqual(stats["stop"][0], "network")

    def test_page_requests_never_overlap(self):
        portal = SlowPortal(slow=0.05, page_time=0.01)
        stats, _ = self.run_jsjb(portal, photo_workers=8)
        self.assertEqual(stats["villages"], 6)
        self.assertEqual(portal.max_pages, 1)
        # Photos downloaded while the next pages were read.
        self.assertGreater(portal.overlap, 0)

    def test_photos_pass_with_the_pool(self):
        out = []
        for workers in (1, 4):
            d = os.path.join(self.tmp.name, f"demo{workers}")
            collect(DemoAdapter(FakeDemoPortal()), Store(d, "demo"), {"state": None, "district": None, "gp": None},
                    list_only=True)
            portal = FakeDemoPortal()
            stats = photos_pass(DemoAdapter(portal), Store(d, "demo"), photo_workers=workers)
            self.assertEqual((stats["records"], stats["records_left"], exit_code(stats)), (3, 0, 0))
            shared = [u for u in portal.requests if u.endswith("/p/shared.jpg")]
            self.assertEqual(len(shared), 1)
            store = Store(d, "demo")
            out.append(({k: [(p["url"], p["sha256"]) for p in r["photos"]] for k, r in store.records.items()},
                        {k: c.get("photos_state") for k, c in store.coverage.items()}))
            self.assertTrue(any(PHOTO_HOST in u for u in portal.requests))
        self.assertEqual(out[0], out[1])


class Options(unittest.TestCase):
    def parse(self, *argv):
        ap = argparse.ArgumentParser()
        add_photo_options(ap)
        return ap.parse_args(list(argv))

    def test_photo_workers_values(self):
        self.assertEqual(self.parse().photo_workers, 1)
        self.assertEqual(self.parse("--photo-workers", "16").photo_workers, 16)
        self.assertEqual(self.parse("--photo-workers", "AUTO").photo_workers, "auto")
        self.assertEqual(self.parse().photo_max, 12)
        for bad in (["--photo-workers", "0"], ["--photo-workers", "17"], ["--photo-workers", "many"],
                    ["--photo-max", "17"]):
            with self.assertRaises(SystemExit):
                with open(os.devnull, "w") as null:
                    old, sys.stderr = sys.stderr, null
                    try:
                        self.parse(*bad)
                    finally:
                        sys.stderr = old

    def test_settings(self):
        self.assertEqual(photo_settings(1), (1, 1, 1))
        self.assertEqual(photo_settings(8), (8, 8, 8))
        self.assertEqual(photo_settings("auto"), (12, 4, 12))
        self.assertEqual(photo_settings("auto", 16), (16, 4, 16))
        self.assertEqual(photo_settings("auto", 2), (2, 2, 2))

    def test_queue_passes_auto_and_the_maximum(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "data")

            def collect_fn(adapter, store, filters, **kw):
                seen.append(kw)
                return collect(adapter, store, filters, sleep=lambda s: None, **kw)
            plan = {"scheme": "jsjb", "units": [{"phase": "p", "state": "Alpha", "district": "A-One", "area": "Rural"}]}
            code = run_queue(plan, JsjbAdapter(FakePortal()), Store(d, "jsjb"), d, photo_workers="auto", photo_max=9,
                             collect_fn=collect_fn, check=None, out=lambda s: None)
        self.assertEqual(code, 0)
        self.assertEqual((seen[0]["photo_workers"], seen[0]["photo_max"]), ("auto", 9))


if __name__ == "__main__":
    unittest.main(verbosity=1)
