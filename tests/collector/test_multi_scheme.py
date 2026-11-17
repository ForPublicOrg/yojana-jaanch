"""Several schemes in one data folder: per-scheme locks and files, the shared photo store, level
paths, per-host settings, the adapter registry, list-only runs and the photos pass, and queues of
any scheme. Offline. Run: python tests/collector/test_multi_scheme.py"""
import http.server
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import collector.__main__ as runner  # noqa: E402
import collector.http as chttp  # noqa: E402
import collector.queue_runner as qr  # noqa: E402
import collector.store as st  # noqa: E402
from collector.base import AccessDenied, Area, SchemeAdapter  # noqa: E402
from collector.http import Client, FetchError  # noqa: E402
from collector.schemes import SCHEMES, scan  # noqa: E402
from collector.schemes.jsjb import JsjbAdapter  # noqa: E402
from collector.store import DataInUse, DataLock, Store, coverage_key, store_shared  # noqa: E402
from demo_scheme import PHOTO_HOST, DemoAdapter, FakeDemoPortal  # noqa: E402

logging.disable(logging.CRITICAL)
ALL = {"state": None, "district": None, "gp": None}


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")

    def tearDown(self):
        self.tmp.cleanup()


class Locks(Temp):
    def test_different_schemes_run_side_by_side_and_the_same_scheme_is_refused(self):
        a, b = DataLock(self.d, "jsjb"), DataLock(self.d, "demo")
        try:
            for scheme in ("jsjb", "demo"):
                with self.assertRaises(DataInUse):
                    DataLock(self.d, scheme)
        finally:
            a.release()
            b.release()
        DataLock(self.d, "demo").release()
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "state"))),
                         ["collector.demo.lock", "collector.jsjb.lock", "collector.lock"])

    def test_jsjb_takes_the_old_lock_too(self):
        old = DataLock(self.d)  # what a JSJB run with the older code holds
        try:
            with self.assertRaises(DataInUse):
                DataLock(self.d, "jsjb")
            DataLock(self.d, "demo").release()
        finally:
            old.release()
        new = DataLock(self.d, "jsjb")
        try:
            with self.assertRaises(DataInUse):
                DataLock(self.d)
        finally:
            new.release()
        # A refused jsjb lock leaves no lock behind.
        DataLock(self.d).release()

    def test_lock_held_by_another_process(self):
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {ROOT!r})
            from collector.store import DataLock
            lock = DataLock({self.d!r})
            print("held", flush=True)
            sys.stdin.readline()
        """)
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with self.assertRaises(DataInUse):
                DataLock(self.d, "jsjb")
            DataLock(self.d, "demo").release()
        finally:
            child.stdin.close()
            child.wait(10)
        DataLock(self.d, "jsjb").release()

    def test_collector_main_refuses_a_second_run_of_the_same_scheme(self):
        lock = DataLock(self.d, "jsjb")
        try:
            self.assertEqual(runner.main(["jsjb", "--data-dir", self.d, "--log", ""]), 7)
        finally:
            lock.release()


class FilesPerScheme(Temp):
    def test_state_files_are_named_by_scheme(self):
        j, d = Store(self.d, "jsjb"), Store(self.d, "demo")
        self.assertTrue(j.summary_path.endswith(os.path.join("state", "last_run.json")))
        self.assertTrue(d.summary_path.endswith(os.path.join("state", "last_run.demo.json")))
        paths = [d.records_path, d.coverage_path, d.index_path, d.coverage_journal, d.index_journal, d.salt_path]
        self.assertTrue(all(os.path.basename(p).startswith("demo.") for p in paths), paths)
        self.assertEqual(qr.queue_file("jsjb", "plans/jsjb-india.json"), "queue.json")
        self.assertEqual(qr.queue_file("demo", "plans/demo-goa.json"), "queue.demo-goa.json")
        self.assertEqual(runner.default_log("data", "demo"), os.path.join("data", "logs", "demo.log"))

    def test_salt_is_made_once_and_only_when_used(self):
        store = Store(self.d, "demo")
        adapter = DemoAdapter(FakeDemoPortal())
        runner.lazy_salt(adapter, store)
        self.assertFalse(os.path.exists(store.salt_path))
        ref = adapter.ref(" REG 123 ")
        self.assertEqual(ref, adapter.ref("reg 123"))
        self.assertNotIn("123", ref)
        self.assertEqual(len(ref), 32)
        again = DemoAdapter(FakeDemoPortal())
        runner.lazy_salt(again, Store(self.d, "demo"))
        self.assertEqual(again.ref("REG 123"), ref)
        with self.assertRaises(RuntimeError):
            DemoAdapter(None).ref("x")


class SharedPhotoStore(Temp):
    def test_two_schemes_store_one_copy(self):
        a, b = Store(self.d, "jsjb"), Store(self.d, "demo")
        body = b"\xff\xd8\xff" + b"x" * 100 + b"\xff\xd9"
        ea, eb = a.save_photo("https://one/a.jpg", body), b.save_photo(None, body)
        self.assertEqual(ea["file"], eb["file"])
        self.assertNotIn(None, b.photo_index)
        files = [f for _, _, fs in os.walk(os.path.join(self.d, "photos")) for f in fs]
        self.assertEqual(files, [ea["sha256"] + ".jpg"])

    def test_writers_racing_on_one_photo_leave_one_whole_file(self):
        path = os.path.join(self.d, "photos", "ab", "ab.jpg")
        body = os.urandom(200000)
        errors = []

        def write():
            try:
                for _ in range(20):
                    store_shared(path, body)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
        threads = [threading.Thread(target=write) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["ab.jpg"])

    def test_rename_refused_because_the_other_writer_is_renaming(self):
        path = os.path.join(self.d, "photos", "cd", "cd.jpg")
        body = b"same bytes"
        real = st.os.replace

        def busy(src, dst):
            with open(dst, "wb") as f:  # the other process's copy lands first
                f.write(body)
            raise PermissionError(13, "in use")
        st.os.replace = busy
        try:
            store_shared(path, body)
        finally:
            st.os.replace = real
        # The file is there and whole now, so a second writer does not even write a copy.
        st.os.replace = lambda *a: self.fail("rewrote a photo that was already stored")
        try:
            store_shared(path, body)
        finally:
            st.os.replace = real
        with open(path, "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["cd.jpg"])


class LevelPaths(unittest.TestCase):
    def test_general_path_fills_the_old_fields(self):
        a = Area(levels=[{"level": "state", "code": 30, "name": "Goa"}, {"level": "district", "code": "1", "name": "North"},
                         {"level": "gp", "code": "101", "name": "Alpha GP"}], code_system="LGD")
        self.assertEqual((a.state, a.district, a.village, a.village_code), ("Goa", "North", "Alpha GP", "101"))
        self.assertEqual(a.levels[0]["code"], "30")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "data"), "demo")
            self.assertEqual(store.area_key(a), "demo|LGD|30/1/101")
        with self.assertRaises(ValueError):
            Area(levels=[{"level": "state"}])

    def test_jsjb_key_unchanged(self):
        a = Area(state="LADAKH", district="Leh Ladakh", area_type="Urban", block="Leh", village="Ward 1",
                 village_code="10856", codes={"state_code": "37"})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Store(os.path.join(tmp, "data"), "jsjb").area_key(a), coverage_key("LADAKH", "Urban", "10856"))
        self.assertEqual([lv["level"] for lv in a.path()], ["state", "district", "area_type", "block", "village"])
        self.assertEqual(a.path()[0]["code"], "37")


class PerHost(unittest.TestCase):
    def test_settings_per_host(self):
        c = Client(delay=1.0, hosts={"Slow.Example": {"delay": 3, "timeout": 120, "retries": 2}, "fast.example": 0.2})
        self.assertEqual(c.settings("slow.example"), {"delay": 3.0, "timeout": 120.0, "retries": 2})
        # Never below --delay.
        self.assertEqual(c.settings("fast.example")["delay"], 1.0)
        self.assertEqual(c.settings("other.example"), {"delay": 1.0, "timeout": 60, "retries": 4})

    def test_each_host_waits_only_for_itself(self):
        c = Client(delay=1.0, hosts={"slow": 5.0})
        slept, now = [], [100.0]
        real = chttp.time
        chttp.time = type("T", (), {"monotonic": staticmethod(lambda: now[0]),
                                    "sleep": staticmethod(lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))})
        try:
            c._wait("slow")
            c._wait("fast")
            c._wait("fast")
            c._wait("slow")
        finally:
            chttp.time = real
        # The second fast request waits 1 s, not 5; the slow host then waits only what is left of its 5 s.
        self.assertEqual(slept, [1.0, 4.0])

    def test_photo_workers_share_one_throttle_per_host(self):
        c = Client(delay=2.0)
        c.set_photo_rate(4, 8)
        workers = c.photo_clients(2)
        self.assertTrue(all(w.photo_worker for w in workers))
        self.assertIs(workers[0].denied, c.denied)
        # Spacing photos to a new host is the throttle's job, shared by every worker and the page client.
        self.assertIs(workers[0].throttle("new.example"), workers[1].throttle("new.example"))
        self.assertIs(workers[0].throttle("new.example"), c.throttle("new.example"))
        self.assertEqual((c.throttle("new.example").rate, c.throttle("new.example").ceiling), (4, 8))


class Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(403 if self.path.startswith("/page") or self.path.startswith("/photo") else 200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"no")


class AccessRefused(unittest.TestCase):
    def setUp(self):
        Handler.hits = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_page_403_stops_requests_to_that_host(self):
        c = Client(delay=0, retries=3)
        with self.assertRaises(FetchError) as e:
            c.fetch(self.base + "/page1")
        self.assertTrue(e.exception.denied)
        with self.assertRaises(FetchError) as e:
            c.fetch(self.base + "/other")
        self.assertTrue(e.exception.denied)
        self.assertEqual(Handler.hits, ["/robots.txt", "/page1"])

    def test_photo_403_is_only_that_photo(self):
        c = Client(delay=0, retries=1)
        with self.assertRaises(FetchError) as e:
            c.fetch_image(self.base + "/photo1.jpg", 1000)
        self.assertFalse(e.exception.denied)
        self.assertEqual(c.denied, {})

    def test_collect_stops_with_exit_8(self):
        class Refusing(DemoAdapter):
            pass
        portal = FakeDemoPortal()
        portal.denied = {"demo.example.invalid": 403}

        def fetch(url, data=None, referer=None):
            raise FetchError("HTTP 403", 403, denied=True)
        portal.fetch = fetch
        with tempfile.TemporaryDirectory() as tmp:
            stats = runner.collect(Refusing(portal), Store(os.path.join(tmp, "data"), "demo"), ALL)
        self.assertEqual(stats["stop"][0], "access")
        self.assertEqual(runner.exit_code(stats), 8)


class Registry(unittest.TestCase):
    def test_jsjb_is_registered(self):
        self.assertIs(SCHEMES["jsjb"], JsjbAdapter)

    def test_scan_finds_new_files_and_survives_broken_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "fake_schemes_pkg")
            os.makedirs(pkg)
            open(os.path.join(pkg, "__init__.py"), "w").close()
            files = {
                "good.py": "from collector.base import SchemeAdapter\nclass G(SchemeAdapter):\n    id = 'good'\n",
                "broken.py": "def (:\n",
                "_helper.py": "from collector.base import SchemeAdapter\nclass H(SchemeAdapter):\n    id = 'hidden'\n",
                "twin.py": "from collector.base import SchemeAdapter\nclass T(SchemeAdapter):\n    id = 'good'\n",
                "noid.py": "from collector.base import SchemeAdapter\nclass N(SchemeAdapter):\n    pass\n",
            }
            for name, text in files.items():
                with open(os.path.join(pkg, name), "w") as f:
                    f.write(text)
            sys.path.insert(0, tmp)
            try:
                found, broken = scan([pkg], "fake_schemes_pkg")
            finally:
                sys.path.remove(tmp)
        self.assertEqual(sorted(found), ["good"])
        self.assertEqual(sorted(broken), ["broken", "twin"])
        self.assertIn("SyntaxError", broken["broken"])


class ListThenPhotos(Temp):
    def setUp(self):
        super().setUp()
        self.portal = FakeDemoPortal()

    def adapter(self, portal=None):
        return DemoAdapter(portal or self.portal)

    def test_list_only_then_photos_pass(self):
        stats = runner.collect(self.adapter(), Store(self.d, "demo"), ALL, list_only=True)
        self.assertEqual(runner.exit_code(stats), 0)
        self.assertFalse([u for u in self.portal.requests if "detail" in u or PHOTO_HOST in u])
        store = Store(self.d, "demo")
        self.assertEqual({r["photos_state"] for r in store.records.values()}, {"not_fetched"})
        self.assertEqual({r["area_key"] for r in store.records.values()}, {"demo|LGD|30/1/101", "demo|LGD|30/1/102"})
        cov = store.coverage["demo|LGD|30/1/101"]
        self.assertEqual((cov["photos_state"], cov["code_system"], cov["levels"][-1]["name"]),
                         ("not_fetched", "LGD", "Alpha GP"))
        self.assertEqual(store.records["W1"]["amount"], 120000.0)

        # A failing detail page costs only that record; it stays not_fetched.
        bad = FakeDemoPortal({"https://demo.example.invalid/detail?work=W3": FetchError("timed out")})
        stats = runner.photos_pass(self.adapter(bad), store)
        self.assertEqual((stats["records"], len(stats["skipped_records"]), stats["records_left"]), (2, 1, 1))
        self.assertEqual(runner.exit_code(stats), 5)
        store = Store(self.d, "demo")
        w1, w2 = store.records["W1"], store.records["W2"]
        self.assertEqual(w1["photos_state"], "fetched")
        self.assertEqual(w1["photos"][0]["stage"], "completed")
        self.assertEqual((w1["photos"][0]["lat"], w1["photos"][0]["label"]), (15.5, "site photo"))
        self.assertEqual((w2["photos"][0]["url"], w2["photos"][0]["taken_at"]), (None, "2026-05-02T09:00:00"))
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(self.d), w2["photos"][0]["file"]))
                        or os.path.exists(w2["photos"][0]["file"]))
        self.assertEqual(store.coverage["demo|LGD|30/1/101"]["photos_state"], "fetched")
        self.assertEqual(store.coverage["demo|LGD|30/1/102"]["photos_state"], "not_fetched")
        self.assertEqual(store.records["W3"]["photos_state"], "not_fetched")

        stats = runner.photos_pass(self.adapter(FakeDemoPortal()), Store(self.d, "demo"))
        self.assertEqual((stats["records"], stats["records_left"], runner.exit_code(stats)), (1, 0, 0))
        store = Store(self.d, "demo")
        self.assertEqual(store.coverage["demo|LGD|30/1/102"]["photos_state"], "fetched")
        shared = {p["sha256"] for r in store.records.values() for p in r["photos"] if p["url"] and "shared" in p["url"]}
        self.assertEqual(len(shared), 1)

    def test_list_only_rerun_keeps_photos_already_fetched(self):
        runner.collect(self.adapter(), Store(self.d, "demo"), ALL)
        store = Store(self.d, "demo")
        self.assertEqual({r["photos_state"] for r in store.records.values()}, {"fetched"})
        runner.collect(self.adapter(), store, ALL, list_only=True)
        store = Store(self.d, "demo")
        self.assertEqual({r["photos_state"] for r in store.records.values()}, {"fetched"})
        self.assertTrue(all(r["photos"] for r in store.records.values()))

    def test_listed_only_village_is_due_for_a_full_run(self):
        runner.collect(self.adapter(), Store(self.d, "demo"), ALL, list_only=True)
        portal = FakeDemoPortal()
        stats = runner.collect(self.adapter(portal), Store(self.d, "demo"), ALL, since_days=30)
        # Gamma GP has no records, so it was complete after the listing; the other two were only listed.
        self.assertEqual((stats["villages"], stats["not_due"]), (2, 1))
        stats = runner.collect(self.adapter(portal), Store(self.d, "demo"), ALL, since_days=30)
        self.assertEqual((stats["villages"], stats["not_due"]), (0, 3))

    def test_one_bad_photo_entry_and_one_failing_photo_list(self):
        class Odd(DemoAdapter):
            def list_photos(self, record):
                if record["record_id"] == "W3":
                    raise FetchError("detail page timed out")
                return [{"nothing": 1}, {"data": b"not an image"}] + super().list_photos(record)
        stats = runner.collect(Odd(self.portal), Store(self.d, "demo"), ALL)
        self.assertIsNone(stats["stop"])
        store = Store(self.d, "demo")
        self.assertEqual(store.records["W3"]["photos_state"], "not_fetched")
        self.assertEqual(store.records["W1"]["photos_state"], "fetched")
        self.assertEqual(store.coverage["demo|LGD|30/1/102"]["photos_state"], "not_fetched")
        self.assertEqual(stats["photos_missing"], 2)

    def test_access_denied_from_adapter_stops_the_run(self):
        portal = FakeDemoPortal({"https://demo.example.invalid/tree": "login"})
        stats = runner.collect(self.adapter(portal), Store(self.d, "demo"), ALL)
        self.assertEqual((stats["stop"][0], runner.exit_code(stats)), ("access", 8))

    def test_list_only_refused_for_jsjb(self):
        self.assertEqual(runner.main(["jsjb", "--list-only", "--data-dir", self.d, "--log", ""]), 3)

    def test_main_with_a_registered_new_scheme(self):
        SCHEMES["demo"] = DemoAdapter
        made = []
        real = runner.Client

        def fake(delay=1.0, hosts=None):
            made.append(hosts)
            return FakeDemoPortal()
        runner.Client = fake
        try:
            code = runner.main(["demo", "--list-only", "--data-dir", self.d, "--district", "North"])
            self.assertEqual(code, 0)
            self.assertEqual(made[0], DemoAdapter.hosts)
            code = runner.main(["demo", "--photos", "--data-dir", self.d, "--max-records", "1"])
            self.assertEqual(code, 0)
        finally:
            runner.Client = real
            del SCHEMES["demo"]
        with open(os.path.join(self.d, "state", "last_run.demo.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["scheme"], "demo")
        with open(os.path.join(self.d, "logs", "demo.log"), encoding="utf-8") as f:
            self.assertIn("Run summary", f.read())
        store = Store(self.d, "demo")
        self.assertEqual(sorted(r["photos_state"] for r in store.records.values()), ["fetched", "not_fetched", "not_fetched"])


DEMO_PLAN = {"scheme": "demo", "since_days": 30, "units": [
    {"phase": "first", "state": "Demo State", "district": "North"},
    {"phase": "rest", "state": "Demo State", "district": ["South", "North"]}]}


class QueueAnyScheme(Temp):
    def test_resolve_and_run_a_plan_of_another_scheme(self):
        units, problems = qr.resolve(DEMO_PLAN, lambda s: [], adapter_cls=DemoAdapter)
        self.assertEqual([u["key"] for u in units], ["demo state|north", "demo state|south"])
        self.assertEqual(problems, [])
        self.assertEqual(qr.describe(units[1]), "Demo State > South")
        self.assertEqual(qr.unit_filters(units[0], DemoAdapter(None)),
                         {"state": "Demo State", "district": "North", "gp": None})
        out = []
        code = qr.run_queue(DEMO_PLAN, DemoAdapter(FakeDemoPortal()), Store(self.d, "demo"), self.d, check=None,
                            out=out.append, state_name="queue.demo-test.json", list_only=True)
        self.assertEqual(code, 0)
        with open(os.path.join(self.d, "state", "queue.demo-test.json"), encoding="utf-8") as f:
            saved = json.load(f)["units"]
        self.assertEqual({k: v["status"] for k, v in saved.items()},
                         {"demo state|north": "done", "demo state|south": "done"})
        self.assertEqual({r["photos_state"] for r in Store(self.d, "demo").records.values()}, {"not_fetched"})
        self.assertFalse(os.path.exists(os.path.join(self.d, "state", "queue.json")))

    def test_plan_checks(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"scheme": "demo", "units": [{"phase": "x", "block": "B"}]}, f)
        try:
            with self.assertRaises(qr.PlanError):
                qr.load_plan(f.name, DemoAdapter)
            with open(f.name, "w") as g:
                json.dump(DEMO_PLAN, g)
            self.assertEqual(qr.load_plan(f.name, DemoAdapter)["scheme"], "demo")
        finally:
            os.remove(f.name)

    def test_jsjb_plan_keys_unchanged(self):
        plan = qr.load_plan(os.path.join(ROOT, "plans", "jsjb-india.json"))
        units, _ = qr.resolve(plan, lambda s: ["X"], adapter_cls=JsjbAdapter)
        old, _ = qr.resolve(plan, lambda s: ["X"])
        self.assertEqual(units, old)
        # The owner's requested districts run first, then Punjab as before.
        first = next(u for u in units if u["phase"] != "requested")
        self.assertEqual(first["key"], "punjab|*|rural")

    def test_queue_main_for_a_new_scheme_and_dry_run(self):
        SCHEMES["demo"] = DemoAdapter
        real = runner.Client
        runner.Client = lambda delay=1.0, hosts=None: FakeDemoPortal()
        plan = os.path.join(self.tmp.name, "demo-test.json")
        with open(plan, "w", encoding="utf-8") as f:
            json.dump(DEMO_PLAN, f)
        out = []
        try:
            import builtins
            orig = builtins.print
            builtins.print = lambda *a, **k: out.append(" ".join(map(str, a)))
            try:
                code = qr.main(["--plan", plan, "--data-dir", self.d, "--dry-run"])
                self.assertEqual(code, 0)
                self.assertFalse(os.path.exists(self.d))
                code = qr.main(["--plan", plan, "--data-dir", self.d, "--no-check", "--list-only"])
            finally:
                builtins.print = orig
        finally:
            runner.Client = real
            del SCHEMES["demo"]
        self.assertEqual(code, 0)
        self.assertIn("Plan: 2 units, 0 finished in the last 30 days, 2 to do. Order:", out)
        self.assertTrue(os.path.exists(os.path.join(self.d, "state", "queue.demo-test.json")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "logs", "demo.log")))

    def test_unknown_scheme_in_plan(self):
        plan = os.path.join(self.tmp.name, "nothing.json")
        with open(plan, "w", encoding="utf-8") as f:
            json.dump({"scheme": "nothing", "units": [{"phase": "a"}]}, f)
        self.assertEqual(qr.main(["--plan", plan, "--data-dir", self.d, "--dry-run"]), 3)

    def test_run_checker_for_a_trial_folder_writes_preview_only(self):
        calls = []
        real = qr.subprocess.call
        qr.subprocess.call = lambda cmd, cwd=None: calls.append(cmd) or 0
        try:
            qr.run_checker(os.path.join(self.tmp.name, "data", "_trial", "demo"))
            qr.run_checker(self.d)
        finally:
            qr.subprocess.call = real
        self.assertIn("--data-dir", calls[0])
        self.assertNotIn("--data-dir", calls[1])
        self.assertEqual(calls[1][-1], self.tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
