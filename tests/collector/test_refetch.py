"""--refetch-unhashed (collector/refetch.py): photos deleted before the phash step read them are
downloaded again. Offline: a real Client on a fake network, fake clock."""
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)

import collector.__main__ as runner  # noqa: E402
from collector import refetch  # noqa: E402
from collector.store import DataInUse, DataLock  # noqa: E402
from checker.nearmatch import VERSION  # noqa: E402
from test_photo_speed import H, FakeClock, NoNetwork, WithLogs, real_client  # noqa: E402
from test_resilience import JPEG, PHOTO, FakePortal  # noqa: E402


def body(name):
    return JPEG + name.encode()


def sha(content):
    return hashlib.sha256(content).hexdigest()


class Refetch(WithLogs):
    def setUp(self):
        super().setUp()
        self.real_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a: NoNetwork()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(self.d, "records"))
        os.makedirs(os.path.join(self.d, "state"))
        # sha256 -> what the portal serves now at each URL saved for it.
        self.gone = body("gone")              # file deleted, portal still has it
        self.changed = body("changed")        # file deleted, portal now serves something else
        self.missing = body("missing")        # file deleted, portal answers 404
        self.two_urls = body("two-urls")      # first URL serves another file, the second the right one
        self.hashed = body("hashed")          # already has a phash line
        self.kept = body("kept")              # its file is still there
        self.serve = {PHOTO + "gone.jpg": (200, self.gone, {}),
                      PHOTO + "changed.jpg": (200, body("something else"), {}),
                      PHOTO + "missing.jpg": (404, b"", {}),
                      PHOTO + "two-a.jpg": (200, body("not it"), {}),
                      PHOTO + "two-b.jpg": (200, self.two_urls, {}),
                      PHOTO + "hashed.jpg": (200, self.hashed, {}),
                      PHOTO + "kept.jpg": (200, self.kept, {})}
        photos = [("gone.jpg", self.gone), ("changed.jpg", self.changed), ("missing.jpg", self.missing),
                  ("two-a.jpg", self.two_urls), ("two-b.jpg", self.two_urls), ("hashed.jpg", self.hashed),
                  ("kept.jpg", self.kept)]
        records = []
        for i, (name, content) in enumerate(photos):
            s = sha(content)
            records.append({"record_id": str(100 + i), "photos": [
                {"url": PHOTO + name, "sha256": s, "bytes": len(content), "file": f"data/photos/{s[:2]}/{s}.jpg"},
                {"url": PHOTO + "404-" + name, "sha256": None, "bytes": None, "file": None}]})
        self.records_path = os.path.join(self.d, "records", "jsjb.jsonl")
        with open(self.records_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        k = sha(self.kept)
        os.makedirs(os.path.join(self.d, "photos", k[:2]))
        with open(os.path.join(self.d, "photos", k[:2], k + ".jpg"), "wb") as f:
            f.write(self.kept)
        self.phash = os.path.join(self.d, "state", "phash.jsonl")
        with open(self.phash, "w", encoding="utf-8") as f:
            f.write(json.dumps({"sha256": sha(self.hashed), "v": VERSION, "error": "x"}) + "\n")

    def tearDown(self):
        urllib.request.build_opener = self.real_build
        self.tmp.cleanup()
        super().tearDown()

    def stored(self, content):
        s = sha(content)
        return os.path.exists(os.path.join(self.d, "photos", s[:2], s + ".jpg"))

    def run_refetch(self, **kw):
        clock = FakeClock()
        client = real_client(FakePortal(), lambda url: self.serve.get(url, (403, b"", {})), clock)
        adapter = types.SimpleNamespace(client=client)
        return refetch.refetch_unhashed(adapter, self.d, "jsjb", **kw), client

    def test_downloads_checks_and_stores_without_touching_records(self):
        with open(self.records_path, "rb") as f:
            before = f.read()
        stats, client = self.run_refetch(photo_workers=3)
        self.assertIsNone(stats["stop"])
        self.assertEqual(stats["to_fetch"], 4)
        self.assertEqual((stats["saved"], stats["mismatch"], stats["missing"], stats["failed"]), (2, 2, 1, 0))
        self.assertTrue(self.stored(self.gone))
        self.assertTrue(self.stored(self.two_urls))
        self.assertFalse(self.stored(self.changed))
        self.assertFalse(self.stored(self.missing))
        self.assertFalse(self.stored(self.hashed))
        # Nothing but photo files was written.
        with open(self.records_path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "state"))), ["phash.jsonl"])
        self.assertEqual(os.listdir(os.path.join(self.d, "records")), ["jsjb.jsonl"])
        # Every request went through the photo throttle of the host.
        calls = [u for kind, u, _ in client.opener.log if kind == "photo"]
        self.assertEqual(sorted(calls), sorted([PHOTO + n for n in ("gone.jpg", "changed.jpg", "missing.jpg",
                                                                   "two-a.jpg", "two-b.jpg")]))
        t = client.throttle(H)
        self.assertEqual(t.counts["requests"], len(calls))
        self.assertEqual(t.ceiling, 3)
        self.assertTrue(any("now serves a different file" in m for m in self.messages()))

    def test_a_second_run_asks_only_for_what_is_still_missing(self):
        self.run_refetch()
        stats, client = self.run_refetch()
        # gone and two_urls now have files; changed and missing are asked again.
        self.assertEqual(stats["to_fetch"], 2)
        self.assertEqual(stats["saved"], 0)

    def test_refuses_without_phash_file(self):
        os.remove(self.phash)
        stats, client = self.run_refetch()
        self.assertEqual(stats["stop"][0], "filter")
        self.assertIn("python run.py phash", stats["stop"][1])
        self.assertEqual(client.opener.log, [])

    def test_max_records_limits_photos(self):
        stats, client = self.run_refetch(max_photos=1)
        # The photo on the most works first; its second URL is tried because the first serves another file.
        self.assertEqual([u for kind, u, _ in client.opener.log if kind == "photo"],
                         [PHOTO + "two-a.jpg", PHOTO + "two-b.jpg"])
        self.assertEqual(stats["stopped"], "--max-records 1 reached")
        self.assertEqual(stats["left"], 3)

    def test_refused_photos_stop_the_run(self):
        with open(self.records_path, "a", encoding="utf-8") as f:
            for i in range(10):
                c = body(f"more-{i}")
                f.write(json.dumps({"record_id": str(200 + i), "photos": [
                    {"url": PHOTO + f"more-{i}.jpg", "sha256": sha(c), "file": None}]}) + "\n")
        self.serve = {}
        stats, client = self.run_refetch(photo_workers=1)
        # The ban guard stops the host after 5 refusals in a row; nothing more is asked.
        self.assertEqual(stats["stop"][0], "access")
        self.assertEqual(stats["saved"], 0)
        self.assertEqual(sum(1 for kind, _, _ in client.opener.log if kind == "photo"), 5)

    def test_file_comes_back_under_the_extension_the_records_name(self):
        # Records may name <sha>.jpeg; the file must come back exactly there.
        jpeg = body("jpeg-named")
        s = sha(jpeg)
        self.serve[PHOTO + "jpeg-named.jpeg"] = (200, jpeg, {})
        with open(self.records_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"record_id": "300", "photos": [
                {"url": PHOTO + "jpeg-named.jpeg", "sha256": s, "file": f"data/photos/{s[:2]}/{s}.jpeg"}]}) + "\n")
        stats, _ = self.run_refetch()
        self.assertIsNone(stats["stop"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "photos", s[:2], s + ".jpeg")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "photos", s[:2], s + ".jpg")))
        self.assertTrue(self.stored(self.gone))
        self.assertEqual(refetch.record_ext(s, f"data/photos/{s[:2]}/{s}.jpeg"), ".jpeg")
        self.assertIsNone(refetch.record_ext(s, f"data/photos/{s[:2]}/{'0' * 64}.jpeg"))
        self.assertIsNone(refetch.record_ext(s, None))

    def test_hashed_means_what_the_checker_reads(self):
        # A line of an older version, or a broken one, is not hashed: the checker ignores it too.
        with open(self.phash, "a", encoding="utf-8") as f:
            f.write(json.dumps({"sha256": sha(self.gone), "v": 1, "dhash": "0" * 64, "w": 10, "h": 10}) + "\n")
            f.write(json.dumps({"sha256": sha(self.changed), "v": VERSION}) + "\n")
        self.assertEqual(refetch.hashed_shas(self.phash), {sha(self.hashed)})

    def test_runs_beside_the_collector_lock_but_not_beside_itself(self):
        held = DataLock(self.d, "jsjb")
        try:
            lock = refetch.RefetchLock(self.d, "jsjb")
            with self.assertRaises(DataInUse):
                refetch.RefetchLock(self.d, "jsjb")
            lock.release()
        finally:
            held.release()

    def test_command_line_defaults_to_three_workers(self):
        seen = {}
        real = refetch.run_from_args
        refetch.run_from_args = lambda a: seen.setdefault("a", a) and 0
        try:
            self.assertEqual(runner.main(["jsjb", "--refetch-unhashed", "--data-dir", self.d, "--log", ""]), 0)
            self.assertEqual(seen["a"].photo_workers, 3)
            seen.clear()
            runner.main(["jsjb", "--refetch-unhashed", "--photo-workers", "5", "--data-dir", self.d, "--log", ""])
            self.assertEqual(seen["a"].photo_workers, 5)
        finally:
            refetch.run_from_args = real


if __name__ == "__main__":
    unittest.main()
