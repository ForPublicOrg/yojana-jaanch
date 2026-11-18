"""publish step on generated images (no photos of people). Needs the .venv-publish packages:

    .venv-publish/Scripts/python -m unittest discover -s tests/publisher

The ONNX engines are replaced by FakeDetector, so these tests need no model files.
"""
import contextlib
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import run as run_py  # noqa: E402
from publisher import common  # noqa: E402

try:
    import cv2
    import numpy as np
    from publisher import engines, faces, publish
except ImportError:
    cv2 = None

SECRET = b"GPS 30.1234N 75.5678E private-note"


def exif_segment():
    payload = b"Exif\x00\x00" + b"MM\x00\x2a\x00\x00\x00\x08" + SECRET
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def make_jpeg(path, w, h, seed):
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (60, 120, 160)
    for _ in range(20):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.rectangle(img, (x, y), (x + 80, y + 50), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    img = np.clip(img.astype(int) + rng.integers(-40, 40, img.shape), 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    data = buf.tobytes()
    data = data[:2] + exif_segment() + data[2:]
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class FakeDetector:
    """Stands in for the two ONNX engines: one face and one head per photo, at fixed places."""
    name = "fake-face+fake-pose"

    def __init__(self, people=True, version="test1"):
        self.people = people
        self.version = version

    def detect(self, img):
        if not self.people:
            return [], 0
        h, w = img.shape[:2]
        return ([{"box": [w // 4, h // 4, w // 10, h // 10], "engine": "yoloface-m", "kind": "face", "score": 0.9},
                 {"box": [w // 2, h // 2, w // 10, h // 10], "engine": "yolopose", "kind": "head", "score": 0.8}], 3)


def kp_person(nose=None, eyes=(), ears=(), shoulders=()):
    kp = np.zeros((17, 3))
    if nose:
        kp[0] = (*nose, 0.9)
    for i, p in zip((1, 2), eyes):
        kp[i] = (*p, 0.9)
    for i, p in zip((3, 4), ears):
        kp[i] = (*p, 0.9)
    for i, p in zip((5, 6), shoulders):
        kp[i] = (*p, 0.9)
    return kp


@unittest.skipIf(cv2 is None, "needs the packages from requirements-publish.txt")
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        photos = self.root / "site" / "photos"
        photos.mkdir(parents=True)
        self.shas = []
        cases = []
        for i, (w, h) in enumerate([(3200, 2400), (800, 1000), (1200, 900)]):
            tmp = photos / f"tmp{i}.jpg"
            sha = make_jpeg(tmp, w, h, i)
            ext = ".jpeg" if i % 2 else ".jpg"
            if i == 2:
                sub = self.root / "data" / "photos" / sha[:2]
                sub.mkdir(parents=True)
                tmp.rename(sub / f"{sha}{ext}")
            else:
                tmp.rename(photos / f"{sha}{ext}")
            self.shas.append(sha)
            cases += [{"id": f"c{i}", "photo": f"photos/{sha}{ext}"}, {"id": f"d{i}", "photo": f"photos/{sha}{ext}"}]
        cases.append({"id": "none", "photo": None})
        (self.root / "site" / "data" / "index").mkdir(parents=True)
        (self.root / "site" / "data" / "index" / "x.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
        (self.root / "site" / "data" / "summary.json").write_text(
            json.dumps({"index": [{"state": "X", "file": "data/index/x.json", "cases": len(cases)}]}), encoding="utf-8")
        self.paths = common.Paths(self.root)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, detector=None):
        self.logs = []
        return publish.run(self.paths, allow_download=False, detector=detector or FakeDetector(),
                           log=self.logs.append)

    def test_copies_are_blurred_resized_and_metadata_free(self):
        self.assertEqual(self.publish(), 0)
        man = common.read_manifest(self.paths.manifest)
        self.assertEqual(set(man), set(self.shas))
        for sha in self.shas:
            out = self.paths.out_photos / f"{sha}.jpg"
            data = out.read_bytes()
            self.assertNotIn(b"Exif", data)
            self.assertNotIn(SECRET, data)
            self.assertNotEqual(hashlib.sha256(data).hexdigest(), sha)
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            self.assertLessEqual(max(img.shape[:2]), 1600)
            e = man[sha]
            self.assertEqual((e["width"], e["height"]), (img.shape[1], img.shape[0]))
            for key in ("faces", "detector", "settings_version", "processed_at", "people_signal"):
                self.assertIn(key, e)
        self.assertEqual(sorted(p.name for p in self.paths.out_photos.iterdir()),
                         sorted(f"{s}.jpg" for s in self.shas))
        self.assertTrue(self.paths.hide.exists() and self.paths.extra_blur.exists())
        page = self.paths.review.read_text(encoding="utf-8")
        for sha in self.shas:
            self.assertIn(f"photos/{sha}.jpg", page)
        self.assertNotIn("http", page.split("<script>")[0].replace("http-equiv", ""))

    def test_only_the_found_regions_change(self):
        self.publish()
        sha = self.shas[1]
        src = next((self.root / "site" / "photos").glob(sha + ".*"))
        before = faces.fit(faces.load_image(src), 1600).astype(int)
        after = cv2.imread(str(self.paths.out_photos / f"{sha}.jpg")).astype(int)
        changed = np.abs(after - before).max(axis=2) > 40
        h, w = changed.shape
        self.assertLess(changed.mean(), 0.05)
        face = (slice(h // 4 + 5, h // 4 + h // 10 - 5), slice(w // 4 + 5, w // 4 + w // 10 - 5))
        def detail(img):
            return cv2.Laplacian(img[face].astype(np.uint8), cv2.CV_64F).std()
        self.assertLess(detail(after), detail(before) / 3)

    def test_refresh_is_incremental_and_honours_hand_controls(self):
        self.publish()
        first = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        self.publish()
        self.assertIn("0 to process", " ".join(self.logs))
        self.assertEqual(json.loads(self.paths.manifest.read_text(encoding="utf-8")), first)

        hidden, boxed = self.shas[0], self.shas[1]
        before = cv2.imread(str(self.paths.out_photos / f"{boxed}.jpg"))
        self.paths.hide.write_text(self.paths.hide.read_text(encoding="utf-8") + hidden + "\n", encoding="utf-8")
        self.paths.extra_blur.write_text(json.dumps({boxed: [[10, 10, 150, 150]]}), encoding="utf-8")
        self.publish()
        self.assertIn("1 to process", " ".join(self.logs))
        man = common.read_manifest(self.paths.manifest)
        self.assertNotIn(hidden, man)
        self.assertFalse((self.paths.out_photos / f"{hidden}.jpg").exists())
        self.assertEqual(man[boxed]["extra_boxes"], [[10, 10, 150, 150]])
        after = cv2.imread(str(self.paths.out_photos / f"{boxed}.jpg"))

        def detail(img):
            return img[20:150, 20:150].astype(float).std(axis=(0, 1)).mean()
        self.assertLess(detail(after), detail(before) / 3)
        self.assertIn(hidden, self.paths.hide.read_text(encoding="utf-8"))

    def test_only_approved_copies_go_to_upload(self):
        self.publish()
        self.assertEqual(list(self.paths.upload.iterdir()), [])
        self.assertIn("Waiting for a check (3)", self.paths.review.read_text(encoding="utf-8"))
        sha = self.shas[0]
        copy = common.read_manifest(self.paths.manifest)[sha]["copy_sha256"]
        self.assertEqual(copy, hashlib.sha256((self.paths.out_photos / f"{sha}.jpg").read_bytes()).hexdigest())
        line = common.approval_line(sha, copy)
        self.assertIn(line, self.paths.review.read_text(encoding="utf-8"))
        self.paths.approved.write_text(line + "\n", encoding="utf-8")
        self.publish()
        self.assertEqual([p.name for p in self.paths.upload.iterdir()], [f"{sha}.jpg"])
        self.assertIn("Approved (1)", self.paths.review.read_text(encoding="utf-8"))
        self.paths.extra_blur.write_text(json.dumps({sha: [[10, 10, 50, 50]]}), encoding="utf-8")
        self.publish()
        self.assertEqual(list(self.paths.upload.iterdir()), [])

    def test_missing_models_stop_without_changes(self):
        old = engines.MODEL_DIR
        try:
            engines.MODEL_DIR = self.root / "no-models"
            self.assertEqual(publish.run(self.paths, allow_download=False, log=self.logs.append), 2)
        finally:
            engines.MODEL_DIR = old
        self.assertFalse(self.paths.manifest.exists())
        self.assertIn("could not be loaded", " ".join(self.logs))

    def test_model_hash_is_checked(self):
        d = self.root / "models"
        d.mkdir()
        old = dict(engines.MODELS)
        try:
            engines.MODELS["fake"] = engines.Model("fake.onnx", "https://example.invalid/fake.onnx",
                                                   hashlib.sha256(b"right").hexdigest(), "test")
            (d / "fake.onnx").write_bytes(b"right")
            self.assertEqual(engines.model_path("fake", d, allow_download=False), d / "fake.onnx")
            (d / "fake.onnx").write_bytes(b"wrong")
            with self.assertRaises(ValueError):
                engines.model_path("fake", d, allow_download=False)
        finally:
            engines.MODELS.clear()
            engines.MODELS.update(old)

    def test_settings_change_reprocesses(self):
        self.publish()
        self.publish(FakeDetector(version="test2"))
        self.assertIn(f"{len(self.shas)} to process", " ".join(self.logs))

    def test_manifest_and_review_name_the_engine_of_each_box(self):
        self.publish()
        e = common.read_manifest(self.paths.manifest)[self.shas[0]]
        self.assertEqual(e["box_engines"], ["yoloface-m", "yolopose"])
        self.assertEqual(len(e["boxes"]), 2)
        page = self.paths.review.read_text(encoding="utf-8")
        self.assertIn("<span>face</span>", page)
        self.assertIn("<span>head (pose)</span>", page)

    def test_auto_approval_is_off_by_default(self):
        self.publish(FakeDetector(people=False))
        self.assertEqual(json.loads(self.paths.settings.read_text(encoding="utf-8")),
                         {"auto_approve_no_person": False, "auto_approve_all": False})
        self.assertEqual(list(self.paths.upload.iterdir()), [])

    def test_auto_approval_only_for_photos_without_people(self):
        self.paths.publish.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_text('{"auto_approve_no_person": true}', encoding="utf-8")
        self.publish(FakeDetector(people=True))
        self.assertEqual(list(self.paths.upload.iterdir()), [])
        nobody = FakeDetector(people=False, version="test2")
        self.publish(nobody)
        self.assertEqual(len(list(self.paths.upload.iterdir())), 3)
        self.assertIn(common.AUTO_NOTE, self.paths.approved.read_text(encoding="utf-8"))
        self.assertIn("Approved automatically", self.paths.review.read_text(encoding="utf-8"))
        self.paths.extra_blur.write_text(json.dumps({self.shas[0]: [[5, 5, 20, 20]]}), encoding="utf-8")
        self.publish(nobody)
        self.assertEqual(len(list(self.paths.upload.iterdir())), 2)
        self.paths.settings.write_text('{"auto_approve_no_person": false}', encoding="utf-8")
        self.publish(nobody)
        self.assertEqual(list(self.paths.upload.iterdir()), [])
        self.assertNotIn(common.AUTO_NOTE, self.paths.approved.read_text(encoding="utf-8"))

    def test_auto_approve_all_approves_every_copy_and_follows_the_setting(self):
        self.paths.publish.mkdir(parents=True, exist_ok=True)
        self.paths.settings.write_text('{"auto_approve_all": true}', encoding="utf-8")
        self.publish(FakeDetector(people=True))
        self.assertEqual(len(list(self.paths.upload.iterdir())), 3)
        self.assertEqual(self.paths.approved.read_text(encoding="utf-8").count(common.ALL_NOTE), 3)
        self.assertIn("auto_approve_all is on", self.paths.review.read_text(encoding="utf-8"))
        # A hidden photo stays out.
        self.paths.hide.write_text(self.shas[0] + "\n", encoding="utf-8")
        self.publish(FakeDetector(people=True))
        self.assertEqual(len(list(self.paths.upload.iterdir())), 2)
        # Turning it off drops its lines and empties the upload folder.
        self.paths.settings.write_text('{"auto_approve_all": false}', encoding="utf-8")
        self.publish(FakeDetector(people=True))
        self.assertEqual(list(self.paths.upload.iterdir()), [])
        self.assertNotIn(common.ALL_NOTE, self.paths.approved.read_text(encoding="utf-8"))

    def test_mismatched_original_is_skipped(self):
        p = next((self.root / "site" / "photos").glob(self.shas[0] + ".*"))
        p.write_bytes(p.read_bytes() + b"x")
        self.publish()
        self.assertNotIn(self.shas[0], common.read_manifest(self.paths.manifest))

    def test_blur_hides_detail_and_touches_nothing_else(self):
        rng = np.random.default_rng(1)
        img = rng.integers(0, 255, (400, 400, 3), dtype=np.uint8)
        out = faces.blur_regions(img.copy(), [{"box": [150, 150, 100, 100], "kind": "face"}])
        self.assertLess(out[170:230, 170:230].astype(float).std(), img[170:230, 170:230].astype(float).std() / 3)
        changed = (out != img).any(axis=2)
        ys, xs = np.nonzero(changed)
        m = faces.SETTINGS["face_margin"] / 2 * 100 + 10
        self.assertTrue(xs.min() >= 150 - m and xs.max() <= 250 + m and ys.min() >= 150 - m and ys.max() <= 250 + m)
        self.assertFalse(changed[150 - int(m) + 5, 150 - int(m) + 5])

    def test_exif_rotation_is_applied_before_stripping(self):
        data = cv2.imencode(".jpg", np.zeros((100, 200, 3), np.uint8))[1].tobytes()
        tiff = b"MM\x00\x2a\x00\x00\x00\x08" + struct.pack(">HHHIHH", 1, 0x0112, 3, 1, 6, 0) + b"\x00" * 4
        seg = b"Exif\x00\x00" + tiff
        src = self.root / "rotated.jpg"
        src.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg + data[2:])
        self.assertEqual(faces.load_image(src).shape[:2], (200, 100))

    def test_head_box_from_keypoints(self):
        kp = kp_person(nose=(100, 100), eyes=((92, 95), (108, 95)), ears=((85, 98), (115, 98)),
                       shoulders=((70, 140), (130, 140)))
        x, y, w, h = engines.head_from_keypoints(kp, [60, 80, 80, 200])
        self.assertTrue(x < 85 and x + w > 115 and y < 95 and y + h > 105)
        self.assertLess(w, 80)
        self.assertIsNone(engines.head_from_keypoints(kp_person(shoulders=((70, 140), (130, 140))),
                                                      [60, 80, 80, 200]))

    def test_rules_drop_weak_stamp_and_huge_boxes_but_keep_heads(self):
        hit = engines.Hit
        found = [hit([100, 100, 30, 30], 0.8, "yoloface-m"),
                 hit([100, 1300, 30, 30], 0.4, "yoloface-m"),
                 hit([100, 1300, 30, 30], 0.9, "yoloface-m"),
                 hit([0, 0, 1000, 1000], 0.4, "yoloface-m"),
                 hit([500, 500, 30, 30], 0.2, "yoloface-m"),
                 hit([500, 500, 3, 3], 0.9, "yoloface-m")]
        person = ([300, 200, 100, 300], 0.9, kp_person(nose=(350, 230), eyes=((345, 225), (355, 225))))
        regions, signal = faces.select((1600, 1200), found, [person])
        self.assertEqual([(r["engine"], r["box"]) for r in regions[:2]],
                         [("yoloface-m", [100, 100, 30, 30]), ("yoloface-m", [100, 1300, 30, 30])])
        self.assertEqual(regions[1]["score"], 0.9)
        self.assertEqual([r["engine"] for r in regions], ["yoloface-m", "yoloface-m", "yolopose"])
        self.assertEqual(signal, 7)
        self.assertEqual(faces.select((1600, 1200), [], []), ([], 0))

    def test_rules_drop_caption_letters_slivers_and_odd_shapes(self):
        hit = engines.Hit
        dropped = []
        regions, _ = faces.select((1280, 720), [hit([374, 117, 14, 19], 0.49, "yoloface-m"),
                                                hit([262, 116, 14, 19], 0.33, "yoloface-m"),
                                                hit([563, 0, 68, 24], 0.33, "yoloface-m"),
                                                hit([300, 500, 23, 78], 0.39, "yoloface-m")], [], dropped)
        self.assertEqual(regions, [])
        self.assertEqual(sorted(d["rule"] for d in dropped), ["edge", "lone", "lone", "shape"])

    def test_small_face_in_a_group_is_kept(self):
        hit = engines.Hit
        found = [hit([500, 200, 40, 44], 0.8, "yoloface-m"), hit([560, 210, 11, 13], 0.4, "yoloface-m")]
        regions, _ = faces.select((900, 1600), found, [])
        self.assertEqual(len(regions), 2)
        regions, _ = faces.select((900, 1600), found[1:], [])
        self.assertEqual(regions, [])

    def test_rules_drop_heads_on_stamp_and_shadows(self):
        def person(box, score, nose, spread=8):
            x, y = nose
            kp = kp_person(nose=nose, eyes=((x - spread, y - 4), (x + spread, y - 4)),
                           ears=((x - 2 * spread, y), (x + 2 * spread, y)))
            return (box, score, kp)
        on_map = person([139, 1300, 151, 269], 0.32, (205, 1330))
        shadow = person([69, 826, 411, 453], 0.32, (325, 900), spread=40)
        worker = person([300, 200, 200, 500], 0.9, (400, 260))
        low_worker = person([300, 1000, 300, 590], 0.9, (450, 1060))
        dropped = []
        regions, _ = faces.select((1600, 1200), [], [on_map, shadow, worker, low_worker], dropped)
        self.assertEqual(len(regions), 2)
        self.assertEqual(sorted(d["rule"] for d in dropped), ["shadow", "stamp"])
        regions, _ = faces.select((1600, 1200), [engines.Hit([300, 860, 50, 60], 0.3, "yoloface-m")], [shadow])
        self.assertEqual([r["kind"] for r in regions], ["face", "head"])

    def test_zoom_windows_stay_inside_the_photo(self):
        wins = engines.zoom_windows([[5, 5, 10, 30], [8, 9, 10, 30], [1590, 750, 9, 9]], 1600, 759, 320)
        self.assertEqual(len(wins), 2)
        for x, y, w, h in wins:
            self.assertTrue(x >= 0 and y >= 0 and x + w <= 1600 and y + h <= 759 and w == h == 320)

    def test_stray_images_in_the_publish_folder_are_reported(self):
        self.publish()
        self.assertNotIn("WARNING", " ".join(self.logs))
        (self.paths.publish / "eval" / "label").mkdir(parents=True)
        (self.paths.publish / "eval" / "label" / "x.jpg").write_bytes(b"x")
        self.publish()
        self.assertIn("WARNING: 1 image files", " ".join(self.logs))
        self.assertIn("x.jpg", " ".join(self.logs))


class SetupMessageTests(unittest.TestCase):
    def test_missing_venv_prints_setup_commands(self):
        old = run_py.VENV, run_py.VENV_GPU
        try:
            run_py.VENV = str(Path(tempfile.gettempdir()) / "no-such-venv-for-test")
            run_py.VENV_GPU = str(Path(tempfile.gettempdir()) / "no-such-gpu-venv-for-test")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_py.publish([])
        finally:
            run_py.VENV, run_py.VENV_GPU = old
        self.assertEqual(code, 3)
        self.assertIn("python -m venv .venv-publish", buf.getvalue())
        self.assertIn("install -r requirements-publish.txt", buf.getvalue())


class GraphicsChipChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        scripts = Path(self.tmp.name) / "Scripts"
        scripts.mkdir()
        (scripts / "python.exe").write_bytes(b"")
        self.calls = []
        patches = {"VENV_GPU": self.tmp.name, "works": lambda py, code: bool(py),
                   "subprocess": types.SimpleNamespace(call=self.call, DEVNULL=None)}
        for name, value in patches.items():
            old = getattr(run_py, name)
            setattr(run_py, name, value)
            self.addCleanup(setattr, run_py, name, old)
        old_env = os.environ.pop("PUBLISH_ONNX_PROVIDER", None)
        self.addCleanup(lambda: os.environ.__setitem__("PUBLISH_ONNX_PROVIDER", old_env) if old_env
                        else os.environ.pop("PUBLISH_ONNX_PROVIDER", None))

    def call(self, cmd, cwd=None, env=None, **kw):
        self.calls.append((cmd, (env or {}).get("PUBLISH_ONNX_PROVIDER")))
        return 0

    def run_step(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_py.publish([], *args)

    def test_publish_uses_the_gpu_environment_with_directml(self):
        self.assertEqual(self.run_step(), 0)
        cmd, provider = self.calls[-1]
        self.assertTrue(cmd[0].startswith(self.tmp.name))
        self.assertEqual(provider, "dml")

    def test_provider_cpu_keeps_publish_on_the_cpu_environment(self):
        os.environ["PUBLISH_ONNX_PROVIDER"] = "cpu"
        self.assertIsNone(run_py.gpu_python())

    def test_phash_never_uses_the_gpu_environment(self):
        old = run_py.VENV
        run_py.VENV = str(Path(tempfile.gettempdir()) / "no-such-venv-for-test")
        self.addCleanup(setattr, run_py, "VENV", old)
        self.assertEqual(self.run_step("phash", "import PIL"), 3)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
