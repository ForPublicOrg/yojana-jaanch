"""Fault injection against the checker, offline. A failing test is a defect.
Run: python tests/checker/test_attacks.py"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import fixture  # noqa: E402
from checker import __main__ as checker  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.recs = fixture.build(self.root)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, recs):
        with open(Path(self.root) / "data" / "records" / "jsjb.jsonl", "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def kinds(self, **kw):
        cases = checker.run(self.root, log=self.logs.append, **kw)
        return {c["kind"] for c in cases}


class OneOddTimestamp(Base):
    def test_one_record_with_a_timezone_does_not_drop_whole_checks(self):
        before = self.kinds()
        self.assertTrue({"photo_reuse", "upload_burst"} <= before)
        odd = dict(self.recs[0], record_id="9999")
        odd["photos"] = [dict(self.recs[0]["photos"][0], uploaded_at="2026-05-20T11:00:00+05:30")]
        self.write(self.recs + [odd])
        after = self.kinds()
        self.assertEqual(before - after, set(), [l for l in self.logs if "check failed" in l])


class DeepJson(Base):
    def test_deeply_nested_coverage_file(self):
        with open(Path(self.root) / "data" / "records" / "jsjb.coverage.json", "w") as f:
            f.write("[" * 100000 + "]" * 100000)
        self.kinds()


class _Truncated(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"choices": [{"message": {"content": '{"answer": "no", "seen": "a road"}'}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:10])
        self.wfile.flush()
        self.close_connection = True


class VisionTruncated(Base):
    def test_one_cut_off_answer_does_not_drop_the_vision_check(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Truncated)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = {"VISION_API_BASE": f"http://127.0.0.1:{srv.server_port}", "VISION_MODEL": "m"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            self.kinds(use_vision=True, vision_limit=2)
        finally:
            srv.shutdown()
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        dropped = [l for l in self.logs if l.startswith("vision: check failed")]
        self.assertEqual(dropped, [], dropped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
