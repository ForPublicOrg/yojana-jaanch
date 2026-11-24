"""drive-copy: approved copies go into subfolders 0 to f of the sync folder. Standard library only.

    python -m unittest discover -s tests/publisher
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from publisher import common, drive_copy  # noqa: E402

SHA = [f"{i:x}" * 64 for i in range(1, 6)]
COPY = {s: f"blurred copy of {s}".encode() for s in SHA}


class DriveCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = common.Paths(self.root)
        self.paths.out_photos.mkdir(parents=True)
        self.sync = self.root / "sync"
        self.sync.mkdir()
        # SHA[3] is hidden, SHA[4] is published but not approved.
        self.man = {}
        for s in SHA:
            self.publish(s, COPY[s])
        self.paths.hide.write_text(f"{SHA[3]}\n", encoding="utf-8")
        self.approve(SHA[:4])

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, sha, data):
        (self.paths.out_photos / f"{sha}.jpg").write_bytes(data)
        self.man[sha] = {"sha256": sha, "faces": 0, "copy_sha256": hashlib.sha256(data).hexdigest()}
        common.write_manifest(self.paths.manifest, self.man)

    def approve(self, shas):
        self.paths.approved.write_text("".join(common.approval_line(s, self.man[s]["copy_sha256"]) + "\n"
                                               for s in shas), encoding="utf-8")

    def go(self):
        logs = []
        code = drive_copy.run(self.paths, str(self.sync), log=logs.append)
        return code, " | ".join(logs)

    def tree(self):
        return sorted(str(p.relative_to(self.sync)).replace("\\", "/") for p in self.sync.rglob("*") if p.is_file())

    def test_copies_moves_and_skips(self):
        (self.sync / f"{SHA[0]}.jpg").write_bytes(COPY[SHA[0]])      # at the top: moved
        (self.sync / "3").mkdir()
        (self.sync / "3" / f"{SHA[2]}.jpg").write_bytes(COPY[SHA[2]])  # already in place
        (self.sync / f"{SHA[4]}.jpg").write_bytes(COPY[SHA[4]])      # not approved: left alone
        code, text = self.go()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.tree(), [f"1/{SHA[0]}.jpg", f"2/{SHA[1]}.jpg", f"3/{SHA[2]}.jpg", f"{SHA[4]}.jpg"])
        self.assertEqual((self.sync / "2" / f"{SHA[1]}.jpg").read_bytes(), COPY[SHA[1]])
        self.assertIn("3 approved copies: 1 copied, 1 moved into their subfolder, 1 already there.", text)
        self.assertIn("1 photos at the top", text)
        self.assertNotIn(SHA[3], " ".join(self.tree()))
        self.assertEqual(self.go()[1].split(" | ")[0], "3 approved copies: 0 copied, 0 moved into their subfolder, "
                                                       "3 already there.")

    def test_other_bytes_are_never_replaced(self):
        (self.sync / "1").mkdir()
        (self.sync / "1" / f"{SHA[0]}.jpg").write_bytes(b"something else")
        (self.sync / f"{SHA[1]}.jpg").write_bytes(b"also something else")
        code, text = self.go()
        self.assertEqual(code, 4, text)
        self.assertEqual((self.sync / "1" / f"{SHA[0]}.jpg").read_bytes(), b"something else")
        self.assertEqual((self.sync / f"{SHA[1]}.jpg").read_bytes(), b"also something else")
        self.assertEqual((self.sync / "2" / f"{SHA[1]}.jpg").read_bytes(), COPY[SHA[1]])
        self.assertIn(f"1/{SHA[0]}.jpg: has other bytes than the approved copy; not replaced", text)
        self.assertIn(f"{SHA[1]}.jpg: has other bytes than the approved copy; left where it is", text)

    def test_a_copy_left_at_the_top_twice_is_reported_not_deleted(self):
        (self.sync / "1").mkdir()
        for p in (self.sync / "1" / f"{SHA[0]}.jpg", self.sync / f"{SHA[0]}.jpg"):
            p.write_bytes(COPY[SHA[0]])
        code, text = self.go()
        self.assertEqual(code, 4, text)
        self.assertTrue((self.sync / f"{SHA[0]}.jpg").exists())
        self.assertIn("is also in 1/", text)

    def test_never_copies_a_file_with_the_original_bytes(self):
        original = b"original photo bytes"
        sha = hashlib.sha256(original).hexdigest()
        self.publish(sha, original)
        self.approve(SHA[:3] + [sha])
        code, text = self.go()
        self.assertEqual(code, 4, text)
        self.assertNotIn(f"{sha}.jpg", " ".join(self.tree()))
        self.assertIn("has the bytes of the original", text)

    def test_deleted_local_copies(self):
        (self.sync / "1").mkdir()
        (self.sync / "1" / f"{SHA[0]}.jpg").write_bytes(COPY[SHA[0]])
        for s in SHA:
            (self.paths.out_photos / f"{s}.jpg").unlink()
        code, text = self.go()
        self.assertEqual(code, 4, text)
        self.assertEqual(self.tree(), [f"1/{SHA[0]}.jpg"])
        self.assertIn("3 approved copies: 0 copied, 0 moved into their subfolder, 1 already there.", text)
        self.assertIn(f"2/{SHA[1]}.jpg: is not in the Drive folder, and its blurred copy is no longer in", text)

    def test_missing_folder_or_manifest(self):
        logs = []
        self.assertEqual(drive_copy.run(self.paths, str(self.root / "nope"), log=logs.append), 2)
        self.paths.manifest.unlink()
        self.assertEqual(drive_copy.run(self.paths, str(self.sync), log=logs.append), 1)
        self.assertEqual(self.tree(), [])


if __name__ == "__main__":
    unittest.main()
