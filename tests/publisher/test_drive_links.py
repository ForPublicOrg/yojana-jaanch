"""drive-links against a fake local Drive API. Standard library only; never calls Google.

    python -m unittest discover -s tests/publisher
"""
import hashlib
import json
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

FOLDER = "FolderId_abc-123"
KEY = "test-key-not-real"
SHA = [f"{i:x}" * 64 for i in range(1, 6)]
COPY = {s: f"blurred copy of {s}".encode() for s in SHA}


def drive_file(fid, sha, data=None):
    data = COPY[sha] if data is None else data
    return {"id": fid, "name": f"{sha}.jpg", "md5Checksum": hashlib.md5(data).hexdigest(),
            "sha256Checksum": hashlib.sha256(data).hexdigest()}


class FakeDrive(BaseHTTPRequestHandler):
    pages = []
    subfolders = {}
    requests = []
    status = 200

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        FakeDrive.requests.append((self.command, url.path, q))
        if FakeDrive.status != 200:
            self._send(FakeDrive.status, {"error": {"message": "API key not valid."}})
            return
        parent = q["q"][0].split("'")[1]
        if parent in FakeDrive.subfolders:
            self._send(200, {"files": FakeDrive.subfolders[parent]})
            return
        page = int(q.get("pageToken", ["0"])[0])
        body = {"files": FakeDrive.pages[page]}
        if page + 1 < len(FakeDrive.pages):
            body["nextPageToken"] = str(page + 1)
        self._send(200, body)

    def do_POST(self):
        FakeDrive.requests.append((self.command, self.path, {}))
        self._send(405, {})

    do_PATCH = do_PUT = do_DELETE = do_POST

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class DriveLinksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeDrive)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/drive/v3"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeDrive.requests = []
        FakeDrive.status = 200
        FakeDrive.subfolders = {}
        FakeDrive.pages = [
            [drive_file("id1", SHA[0]), drive_file("id2", SHA[1]), {"id": "junk", "name": "notes.txt"}],
            [drive_file("id3", SHA[2]), drive_file("id4", SHA[3]), drive_file("id5", SHA[4])],
        ]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = common.Paths(self.root)
        self.paths.out_photos.mkdir(parents=True)
        # SHA[3] is hidden, SHA[4] was never published.
        man = {}
        for s in SHA[:4]:
            (self.paths.out_photos / f"{s}.jpg").write_bytes(COPY[s])
            man[s] = {"sha256": s, "faces": 0, "copy_sha256": hashlib.sha256(COPY[s]).hexdigest()}
        common.write_manifest(self.paths.manifest, man)
        self.paths.hide.write_text(f"# note\n{SHA[3]}  # hidden by hand\n", encoding="utf-8")
        self.paths.approved.write_text("".join(common.approval_line(s, man[s]["copy_sha256"]) + "\n"
                                               for s in SHA[:4]), encoding="utf-8")
        self.out = self.root / "site" / "data" / "photo_links.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_subfolders_0_to_f_are_listed_too(self):
        folder = {"mimeType": drive.FOLDER_MIME}
        FakeDrive.pages = [[dict(folder, id="sub1", name="1"), dict(folder, id="old", name="old photos"),
                            drive_file("id3", SHA[2])]]
        FakeDrive.subfolders = {"sub1": [drive_file("id1", SHA[0])]}
        logs = []
        code = drive.run(self.paths, FOLDER, KEY, self.base, log=logs.append)
        text = " | ".join(logs)
        self.assertEqual(code, 4, text)
        links = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(set(links), {SHA[0], SHA[2]})
        self.assertIn("id=id1&", links[SHA[0]]["full"])
        self.assertIn("old photos: a subfolder; its contents were not checked", text)
        self.assertEqual([q["q"][0].split("'")[1] for _, _, q in FakeDrive.requests], [FOLDER, "sub1"])

    def test_pages_and_filters(self):
        logs = []
        code = drive.run(self.paths, FOLDER, KEY, self.base, log=logs.append)
        self.assertEqual(code, 4)
        text = " | ".join(logs)
        self.assertIn(f"{SHA[3]}.jpg: listed in hide.txt", text)
        self.assertIn(f"{SHA[4]}.jpg: not a published copy", text)
        self.assertIn("notes.txt: not a published photo", text)
        links = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(set(links), set(SHA[:3]))
        self.assertEqual(links[SHA[0]], {"full": "https://drive.google.com/thumbnail?id=id1&sz=w1600",
                                         "thumb": "https://drive.google.com/thumbnail?id=id1&sz=w480"})
        self.assertEqual(len(FakeDrive.requests), 2)
        for method, path, q in FakeDrive.requests:
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/drive/v3/files")
            self.assertEqual(q["key"], [KEY])
            self.assertEqual(q["q"], [f"'{FOLDER}' in parents and trashed = false"])
        self.assertEqual(FakeDrive.requests[1][2]["pageToken"], ["1"])

    def run_with(self, files):
        FakeDrive.pages = [files]
        logs = []
        code = drive.run(self.paths, FOLDER, KEY, self.base, log=logs.append)
        return code, json.loads(self.out.read_text(encoding="utf-8")), " | ".join(logs)

    def test_clean_folder_returns_zero(self):
        code, links, _ = self.run_with([drive_file("id1", SHA[0]), drive_file("id2", SHA[1])])
        self.assertEqual((code, set(links)), (0, {SHA[0], SHA[1]}))

    def test_original_or_changed_upload_is_refused(self):
        original = b"original photo bytes"
        sha = hashlib.sha256(original).hexdigest()
        code, links, text = self.run_with([drive_file("id1", SHA[0]), drive_file("id2", SHA[1], b"x"),
                                           {"id": "o", "name": f"{sha}.jpg", "sha256Checksum": sha}])
        self.assertEqual(code, 4)
        self.assertEqual(set(links), {SHA[0]})
        self.assertIn(f"{sha}.jpg: ORIGINAL unblurred photo", text)
        self.assertIn(f"{SHA[1]}.jpg: content differs", text)

    def test_unapproved_or_reblurred_copy_is_not_linked(self):
        self.paths.approved.write_text(common.approval_line(SHA[0], "0" * 64), encoding="utf-8")
        code, links, text = self.run_with([drive_file("id1", SHA[0]), drive_file("id2", SHA[1])])
        self.assertEqual((code, links), (4, {}))
        self.assertIn("not approved", text)

    def test_missing_checksum_is_not_linked(self):
        code, links, text = self.run_with([{"id": "id1", "name": f"{SHA[0]}.jpg"}])
        self.assertEqual((code, links), (4, {}))
        self.assertIn("no checksum", text)

    def test_api_error_writes_nothing(self):
        FakeDrive.status = 403
        logs = []
        code = drive.run(self.paths, FOLDER, KEY, self.base, log=logs.append)
        self.assertEqual(code, 1)
        self.assertFalse(self.out.exists())
        self.assertIn("API key not valid", " ".join(logs))
        self.assertNotIn(KEY, " ".join(logs))

    def test_rejects_bad_input(self):
        self.assertEqual(drive.run(self.paths, "x' or 1=1", KEY, self.base, log=lambda *a: None), 2)
        self.assertEqual(drive.run(self.paths, FOLDER, "", self.base, log=lambda *a: None), 2)
        self.assertEqual(FakeDrive.requests, [])

    def test_command_line(self):
        env = {"GOOGLE_API_KEY": KEY, "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
        r = subprocess.run([sys.executable, "-m", "publisher", "drive-links", "--folder-id", FOLDER,
                            "--root", str(self.root), "--api-base", self.base],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(set(json.loads(self.out.read_text(encoding="utf-8"))), set(SHA[:3]))


if __name__ == "__main__":
    unittest.main()
