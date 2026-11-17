"""drive-links without an API key, against a saved fake folder page served locally. Never calls Google.

    python -m unittest discover -s tests/publisher
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from publisher import common, drive  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "drive_folder_page.html").read_text(encoding="utf-8")
FOLDER = "FolderId_abc-123"
SHA = [f"{i:x}" * 64 for i in range(1, 6)]
COPY = {s: f"blurred copy of {s}".encode() for s in SHA}


def page(keep=None, extra=""):
    """The fixture page with only the entries whose name is in keep (all when None), plus extra markup."""
    lines = []
    for line in FIXTURE.splitlines(keepends=True):
        if line.startswith('<div class="flip-entry" '):
            name = line.split('class="flip-entry-title">', 1)[1].split("<", 1)[0]
            if keep is not None and name not in keep:
                continue
        elif line.startswith("</div></div></body>"):
            lines.append(extra)
        lines.append(line)
    return "".join(lines)


class FakePage(BaseHTTPRequestHandler):
    body = ""
    status = 200
    requests = []

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        FakePage.requests.append((self.command, url.path, urllib.parse.parse_qs(url.query),
                                  self.headers.get("User-Agent")))
        data = FakePage.body.encode("utf-8")
        self.send_response(FakePage.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        FakePage.requests.append((self.command, self.path, {}, None))
        self.send_response(405)
        self.end_headers()

    do_PATCH = do_PUT = do_DELETE = do_POST

    def log_message(self, *a):
        pass


class DrivePublicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakePage)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakePage.requests = []
        FakePage.status = 200
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = common.Paths(self.root)
        self.paths.out_photos.mkdir(parents=True)
        self.sync = self.root / "sync"
        self.sync.mkdir()
        # SHA[3] is hidden, SHA[4] was never published.
        man = {}
        for s in SHA[:4]:
            (self.paths.out_photos / f"{s}.jpg").write_bytes(COPY[s])
            man[s] = {"sha256": s, "faces": 0, "copy_sha256": hashlib.sha256(COPY[s]).hexdigest()}
        common.write_manifest(self.paths.manifest, man)
        self.paths.hide.write_text(f"{SHA[3]}\n", encoding="utf-8")
        self.paths.approved.write_text("".join(common.approval_line(s, man[s]["copy_sha256"]) + "\n"
                                               for s in SHA[:4]), encoding="utf-8")
        self.out = self.root / "site" / "data" / "photo_links.json"

    def tearDown(self):
        self.tmp.cleanup()

    def sync_files(self, shas, desktop_ini=True):
        for s in shas:
            (self.sync / f"{s}.jpg").write_bytes(COPY[s])
        if desktop_ini:
            (self.sync / "desktop.ini").write_text("[.ShellClassInfo]\n", encoding="utf-8")

    def go(self, body):
        FakePage.body = body
        logs = []
        code = drive.run_public(self.paths, FOLDER, str(self.sync), self.base, log=logs.append)
        links = json.loads(self.out.read_text(encoding="utf-8")) if self.out.exists() else None
        return code, links, " | ".join(logs)

    def test_clean_folder(self):
        self.sync_files(SHA[:3])
        code, links, text = self.go(page({f"{s}.jpg" for s in SHA[:3]}))
        self.assertEqual(code, 0, text)
        self.assertEqual(set(links), set(SHA[:3]))
        self.assertEqual(links[SHA[0]], {"full": "https://drive.google.com/thumbnail?id=FileId_0001&sz=w1600",
                                         "thumb": "https://drive.google.com/thumbnail?id=FileId_0001&sz=w480"})
        self.assertEqual(len(FakePage.requests), 1)
        method, path, q, agent = FakePage.requests[0]
        self.assertEqual((method, path, q["id"]), ("GET", "/embeddedfolderview", [FOLDER]))
        self.assertEqual(agent, drive.USER_AGENT)

    def test_desktop_ini_on_page_is_ignored(self):
        self.sync_files(SHA[:1])
        extra = page({"notes.txt"}).split('<div class="flip-entries">\n', 1)[1].split("</div></div></body>")[0]
        extra = extra.replace("notes.txt", "desktop.ini").replace("FileId_0005", "FileId_0009")
        code, links, text = self.go(page({f"{SHA[0]}.jpg"}, extra))
        self.assertEqual((code, set(links)), (0, {SHA[0]}), text)

    def test_every_other_file_is_reported(self):
        self.sync_files(SHA[:4])
        (self.sync / "notes.txt").write_text("x", encoding="utf-8")
        (self.sync / "old photos").mkdir()
        code, links, text = self.go(page())
        self.assertEqual(code, 4, text)
        self.assertEqual(set(links), set(SHA[:3]))
        self.assertIn(f"{SHA[3]}.jpg: listed in hide.txt", text)
        self.assertIn("notes.txt: not a published photo", text)
        self.assertIn("old photos: a subfolder", text)

    def test_original_changed_unapproved_and_unknown(self):
        original = b"original photo bytes"
        orig_sha = hashlib.sha256(original).hexdigest()
        self.sync_files([SHA[0]])
        (self.sync / f"{SHA[1]}.jpg").write_bytes(b"changed")
        (self.sync / f"{SHA[2]}.jpg").write_bytes(original)
        (self.sync / f"{SHA[3]}.jpg").write_bytes(COPY[SHA[3]])
        self.paths.hide.write_text("", encoding="utf-8")
        self.paths.approved.write_text(common.approval_line(SHA[0], hashlib.sha256(COPY[SHA[0]]).hexdigest())
                                       + "\n" + common.approval_line(SHA[2], "0" * 64) + "\n", encoding="utf-8")
        body = page({f"{s}.jpg" for s in SHA[:4]}).replace(f"{SHA[2]}.jpg", f"{orig_sha}.jpg")
        (self.sync / f"{SHA[2]}.jpg").rename(self.sync / f"{orig_sha}.jpg")
        code, links, text = self.go(body)
        self.assertEqual(code, 4, text)
        self.assertEqual(set(links), {SHA[0]})
        self.assertIn(f"{SHA[1]}.jpg: content differs", text)
        self.assertIn(f"{orig_sha}.jpg: ORIGINAL unblurred photo", text)
        self.assertIn(f"{SHA[3]}.jpg: not approved", text)

    def test_unpublished_name_and_local_only_file(self):
        self.sync_files(SHA[:1])
        (self.sync / f"{SHA[4]}.jpg").write_bytes(COPY[SHA[4]])
        body = page({f"{SHA[0]}.jpg", f"{SHA[1]}.jpg"}).replace(f"{SHA[1]}.jpg", f"{SHA[4]}.jpg")
        code, links, text = self.go(body)
        self.assertEqual((code, set(links)), (4, {SHA[0]}), text)
        self.assertIn(f"{SHA[4]}.jpg: not a published copy", text)
        (self.sync / f"{SHA[4]}.jpg").rename(self.sync / f"{SHA[2]}.jpg")
        code, links, text = self.go(body)
        self.assertEqual((code, set(links)), (4, {SHA[0]}), text)
        self.assertIn(f"{SHA[4]}.jpg: not in the local folder", text)
        self.assertIn(f"{SHA[2]}.jpg: in the local folder but not on the Drive page", text)

    def test_duplicate_names_are_not_linked(self):
        self.sync_files(SHA[:2])
        body = page({f"{SHA[0]}.jpg", f"{SHA[1]}.jpg"}).replace(f"{SHA[1]}.jpg", f"{SHA[0]}.jpg")
        (self.sync / "other.jpg").write_bytes(b"y")
        (self.sync / f"{SHA[1]}.jpg").unlink()
        code, links, text = self.go(body)
        self.assertEqual((code, links), (4, {}), text)
        self.assertIn("appears more than once", text)

    def test_count_mismatch_stops_without_writing(self):
        self.sync_files(SHA[:3])
        code, links, text = self.go(page({f"{s}.jpg" for s in SHA[:2]}))
        self.assertEqual((code, links), (1, None))
        self.assertIn("truncated", text)
        self.assertIn("syncing", text)

    def test_not_public_or_unknown_layout(self):
        self.sync_files(SHA[:1])
        FakePage.status = 404
        code, links, text = self.go("<html>not found</html>")
        self.assertEqual((code, links), (1, None))
        self.assertIn("not shared", text)
        FakePage.status = 200
        code, links, text = self.go("<html><body><div class='something-else'></div></body></html>")
        self.assertEqual((code, links), (1, None))
        self.assertIn("not recognised", text)
        code, links, text = self.go(page().replace('id="entry-FileId_0001"', 'id="x"'))
        self.assertEqual((code, links), (1, None))
        self.assertIn("not recognised", text)

    def test_needs_local_folder(self):
        logs = []
        self.assertEqual(drive.run_public(self.paths, FOLDER, None, self.base, log=logs.append), 2)
        self.assertIn("--local-folder", " ".join(logs))
        self.assertEqual(drive.run_public(self.paths, "x' or 1", str(self.sync), self.base, log=logs.append), 2)
        self.assertEqual(FakePage.requests, [])

    def test_local_folder_is_not_changed(self):
        self.sync_files(SHA[:4])
        before = {p.name: p.read_bytes() for p in self.sync.iterdir()}
        self.go(page({f"{s}.jpg" for s in SHA[:4]}))
        self.assertEqual({p.name: p.read_bytes() for p in self.sync.iterdir()}, before)
        self.assertEqual({m for m, *_ in FakePage.requests}, {"GET"})

    def test_command_line_without_key(self):
        self.sync_files(SHA[:3])
        FakePage.body = page({f"{s}.jpg" for s in SHA[:3]})
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
        r = subprocess.run([sys.executable, "-m", "publisher", "drive-links", "--folder-id", FOLDER,
                            "--root", str(self.root), "--local-folder", str(self.sync), "--page-base", self.base],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(set(json.loads(self.out.read_text(encoding="utf-8"))), set(SHA[:3]))


if __name__ == "__main__":
    unittest.main()
