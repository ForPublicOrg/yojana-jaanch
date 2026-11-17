"""phash step (publisher/phash.py) on generated images, and the checker's match on its output.
Needs Pillow from .venv-publish:

    .venv-publish/Scripts/python -m unittest discover -s tests/publisher
"""
import hashlib
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from checker import nearmatch  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFilter
    from publisher import phash
except ImportError:
    Image = None


def scene(seed, size=(1280, 960)):
    """A made-up outdoor-like picture: shapes in many colours, softened."""
    rnd = random.Random(seed)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(60):
        x, y = rnd.randrange(w), rnd.randrange(h)
        rw, rh = rnd.randrange(w // 20, w // 4), rnd.randrange(h // 20, h // 4)
        box = (x, y, x + rw, y + rh)
        colour = tuple(rnd.randrange(256) for _ in range(3))
        (d.ellipse if rnd.random() < 0.5 else d.rectangle)(box, fill=colour)
    # Light that changes across the frame, as in a photo, so no row is one plain colour.
    down = Image.linear_gradient("L").resize(size)
    across = Image.linear_gradient("L").rotate(90 if rnd.random() < 0.5 else -90).resize(size)
    light = Image.blend(down, across, 0.5)
    im = Image.blend(im, Image.merge("RGB", (light, light, light)), 0.3)
    return im.filter(ImageFilter.GaussianBlur(2))


def encode(im, fmt="JPEG", **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


@unittest.skipIf(Image is None, "needs Pillow from requirements-publish.txt")
class PhashStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.photos = self.data / "photos"
        self.out = self.data / "state" / "phash.jsonl"
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, content, ext=".jpg", name=None):
        sha = name or hashlib.sha256(content).hexdigest()
        path = self.photos / sha[:2] / (sha + ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return sha

    def run_step(self, **kw):
        return phash.run(self.photos, self.out, log=self.logs.append, **kw)

    def lines(self):
        return [json.loads(x) for x in self.out.read_text(encoding="ascii").splitlines() if x.strip()]

    def test_same_picture_files_match_and_others_do_not(self):
        base = scene(1)
        files = {
            "original": self.store(encode(base, quality=92)),
            "png": self.store(encode(base, "PNG"), ".png"),
            "bmp": self.store(encode(base, "BMP"), ".bmp"),
            "low_quality": self.store(encode(base, quality=35)),
            "resized": self.store(encode(base.resize((640, 480), Image.LANCZOS), quality=85)),
            "other": self.store(encode(scene(2), quality=92)),
            "other_size": self.store(encode(scene(3, (960, 1280)), quality=92)),
            "flat": self.store(encode(Image.new("RGB", (1280, 960), (90, 120, 160)), quality=92)),
            "small": self.store(encode(base.resize((80, 60), Image.LANCZOS), quality=92)),
        }
        counts = self.run_step()
        self.assertEqual((counts["hashed"], counts["errors"]), (9, 0))
        lines = {x["sha256"]: x for x in self.lines()}
        self.assertEqual(set(lines), set(files.values()))
        o = lines[files["original"]]
        self.assertEqual((o["w"], o["h"], o["v"], o["flat"]), (1280, 960, nearmatch.VERSION, False))
        self.assertEqual((o["size"], o["box"]), ([1280, 960], [0, 0, 1280, 960]))
        self.assertGreater(o["levels"], nearmatch.PLACEHOLDER_LEVELS)
        self.assertRegex(o["dhash"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(nearmatch.Entry(o["sha256"], thumb=o["thumb"]).pixels()), 1024)
        self.assertTrue(lines[files["flat"]]["flat"])
        self.assertTrue(lines[files["small"]]["flat"])
        # PNG and BMP hold exactly the pixels of the source image.
        self.assertEqual(lines[files["png"]]["dhash"], lines[files["bmp"]]["dhash"])
        self.assertEqual(lines[files["png"]]["thumb"], lines[files["bmp"]]["thumb"])

        near = nearmatch.NearMatch(nearmatch.read_phash(self.out))
        limits, _ = nearmatch.settings({"near_max_distance": 8, "near_max_mad": 0.03, "near_max_aspect_gap": 0.01})
        groups, accepted, counts = near.group({s: 1 for s in files.values()}, limits)
        by_file = {s: k for k, s in files.items()}
        named = sorted(sorted(by_file[s] for s in g) for g in groups)
        self.assertIn(sorted(["original", "png", "bmp", "low_quality", "resized"]), named)
        for alone in ("other", "other_size", "flat", "small"):
            self.assertIn([alone], named)
        self.assertEqual(counts["low_information"], 2)
        # Different pictures are far apart on both tests.
        e = near.entries
        far = (e[files["original"]].dhash ^ e[files["other"]].dhash).bit_count()
        self.assertGreater(far, 40)
        self.assertGreater(nearmatch.pixel_difference(e[files["original"]], e[files["other"]]), 0.3)

    def test_corrupt_file_gets_an_error_line_and_a_mismatched_one_none(self):
        good = encode(scene(4), quality=90)
        corrupt = self.store(good[:200] + b"\0" * 50)
        self.store(good, name="0" * 64)
        self.store(b"x", ".jpg.1234-abcd.tmp", name="1" * 64)
        counts = self.run_step()
        self.assertEqual((counts["hashed"], counts["errors"], counts["damaged"]), (0, 1, 1))
        lines = {x["sha256"]: x for x in self.lines()}
        # The corrupt bytes are the portal's own file: the same bytes would fail again.
        self.assertEqual(set(lines), {corrupt})
        self.assertIn("could not be decoded", lines[corrupt]["error"])
        entries = nearmatch.read_phash(self.out)
        self.assertFalse(entries[corrupt].usable)
        # A file that does not hash to its name gets no line, so prune keeps it; the log says what to do.
        self.assertTrue(any("0" * 64 in m and "not to the file name" in m for m in self.logs))
        self.assertEqual(self.run_step()["damaged"], 1)

    def test_out_of_memory_is_tried_again(self):
        sha = self.store(encode(scene(4), quality=90))
        real = Image.Image.load

        def no_memory(im):
            raise MemoryError()
        Image.Image.load = no_memory
        try:
            counts = self.run_step()
        finally:
            Image.Image.load = real
        self.assertEqual((counts["hashed"], counts["errors"], counts["unreadable"]), (0, 0, 1))
        self.assertFalse(self.out.exists() and self.lines())
        self.assertEqual(self.run_step()["hashed"], 1)
        self.assertEqual([x["sha256"] for x in self.lines()], [sha])

    def framed(self, im, W=720, H=1600, content_w=500, bg="black"):
        """A phone screenshot of im: centred on a plain canvas, with a status bar at the top."""
        c = Image.new("RGB", (W, H), bg)
        ch = int(im.height * content_w / im.width)
        c.paste(im.resize((content_w, ch), Image.LANCZOS), ((W - content_w) // 2, (H - ch) // 2))
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, W, 40), fill=(30, 30, 30))
        d.text((10, 10), "12:41  4G  87%", fill="white")
        return c

    def test_two_different_shots_in_the_same_frame_are_not_merged(self):
        # Two shots of one site a moment apart: the same scene, the camera moved a little.
        base = scene(21, (1400, 1050))
        a, b = base.crop((0, 0, 1280, 960)), base.crop((60, 40, 1340, 1000))
        files = {}
        for name, im in (("a", a), ("b", b)):
            for frame in ((720, 1600, 500, "black"), (716, 1600, 716, "black"), (720, 1600, 400, "white"),
                          (1600, 720, 700, "black")):
                files[(name,) + frame] = self.store(encode(self.framed(im, *frame), quality=90))
        files[("a", "original")] = self.store(encode(a, quality=92))
        self.run_step()
        near = nearmatch.NearMatch(nearmatch.read_phash(self.out))
        limits, _ = nearmatch.settings({"near_max_distance": 8, "near_max_mad": 0.03, "near_max_aspect_gap": 0.01})
        groups, accepted, _ = near.group({s: 1 for s in files.values()}, limits)
        side = {s: k[0] for k, s in files.items()}
        for g in groups:
            self.assertEqual(len({side[s] for s in g}), 1, [k for k, s in files.items() if s in g])
        # The border is cut off: a screenshot of a is hashed as the picture inside it.
        line = next(x for x in self.lines() if x["sha256"] == files[("a", 720, 1600, 500, "black")])
        left, top, right, bottom = line["box"]
        self.assertLessEqual(abs((right - left) - 500), 8)
        self.assertLessEqual(abs((bottom - top) - 375), 8)
        self.assertEqual(line["size"], [720, 1600])
        self.assertFalse(line["flat"])
        # And it still matches the original file of the same picture.
        ga = next(g for g in groups if files[("a", "original")] in g)
        self.assertIn(files[("a", 720, 1600, 500, "black")], ga)

    def test_placeholder_graphics(self):
        icon = Image.new("RGB", (128, 128), "black")
        ImageDraw.Draw(icon).rectangle((12, 12, 115, 115), fill=(250, 200, 100))
        error = Image.new("RGB", (533, 470), (249, 249, 249))
        ImageDraw.Draw(error).text((170, 215), "Error loading image", fill=(170, 170, 170))
        broken = Image.new("RGB", (73, 73), (252, 244, 236))
        ImageDraw.Draw(broken).polygon([(8, 60), (40, 30), (66, 60)], fill=(90, 160, 60))
        shas = [self.store(encode(icon, "PNG"), ".png"), self.store(encode(error, "PNG"), ".png"),
                self.store(encode(broken, "PNG"), ".png"),
                self.store(encode(scene(3), quality=92)), self.store(encode(scene(3).resize((90, 68)), quality=92))]
        self.run_step()
        entries = nearmatch.read_phash(self.out)
        self.assertEqual([bool(entries[s].placeholder()) for s in shas], [True, True, True, False, False])

    def test_incremental_and_safe_to_interrupt(self):
        a = self.store(encode(scene(5), quality=90))
        self.run_step()
        first = self.out.read_bytes()
        self.assertEqual(self.run_step()["hashed"], 0)
        self.assertEqual(self.out.read_bytes(), first)
        # A run killed in the middle of a line: the cut line is ignored and closed off.
        with open(self.out, "ab") as f:
            f.write(b'{"sha256": "' + b"f" * 64 + b'", "v": 1, "dha')
        b = self.store(encode(scene(6), quality=90))
        counts = self.run_step()
        self.assertEqual(counts["hashed"], 1)
        text = self.out.read_bytes()
        self.assertTrue(text.startswith(first))
        self.assertEqual(sorted(nearmatch.read_phash(self.out)), sorted([a, b]))
        before, after = phash.compact(self.out, log=self.logs.append)
        self.assertEqual((before, after), (3, 2))
        self.assertEqual(sorted(nearmatch.read_phash(self.out)), sorted([a, b]))

    def test_limit_and_a_sha_stored_twice(self):
        content = encode(scene(7), quality=90)
        sha = self.store(content)
        self.store(content, ".jpeg")
        self.store(encode(scene(8), quality=90))
        self.assertEqual(self.run_step(limit=1)["hashed"], 1)
        self.assertEqual(self.run_step()["hashed"], 1)
        self.assertEqual(sum(1 for x in self.lines() if x["sha256"] == sha), 1)

    def test_workers_give_the_same_lines(self):
        for i in range(6):
            self.store(encode(scene(10 + i), quality=90))
        self.run_step(workers=2)
        two = sorted(self.out.read_text(encoding="ascii").splitlines())
        self.out.unlink()
        self.run_step(workers=1)
        self.assertEqual(sorted(self.out.read_text(encoding="ascii").splitlines()), two)

    def test_one_run_at_a_time(self):
        lock = phash.Lock(str(self.data / "state" / "phash.lock"))
        try:
            self.assertEqual(phash.main(["--root", self.tmp.name, "--data-dir", str(self.data)]), 7)
        finally:
            lock.release()
        self.assertEqual(phash.main(["--root", self.tmp.name, "--data-dir", str(self.data)]), 0)


if __name__ == "__main__":
    unittest.main()
