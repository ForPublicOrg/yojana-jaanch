"""Find faces and heads, blur only those, and write the public copy.

Engines (chosen by measurement, see README "Publishing photos"):
- yoloface: YOLOv12m-face, finds faces, also small and side-on ones.
- yolopose: YOLO11x-pose, finds people; a head box is placed on the nose, eyes and ears, so a person
  bending, turned away or wearing a turban or scarf still gets the head blurred.
Needs opencv-python-headless, numpy and onnxruntime (requirements-publish.txt).
"""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from . import engines
from .engines import sha256_file  # noqa: F401  (used by publish.py)

SETTINGS = {
    "max_side": 1600,
    "jpeg_quality": 85,
    "face_engine": "yoloface-m",
    "face_score": 0.3,
    "face_tile": 800,
    "pose_engine": "yolopose",
    "pose_score": 0.3,
    "pose_kp_score": 0.3,
    "pose_tile": 800,
    "person_signal_score": 0.15,
    "stamp_band": 0.35,
    "stamp_face_score": 0.6,
    "stamp_person_score": 0.6,
    "body_to_head": 2.5,
    "lone_face_size": 24,
    "lone_face_score": 0.6,
    "group_reach": 8,
    "edge_face_ratio": 0.6,
    "edge_face_score": 0.6,
    "face_shape": [0.5, 2.0],
    "weak_person_score": 0.5,
    "big_head": 40,
    "zoom_small": 160,
    "zoom_side": 320,
    "min_box": 6,
    "big_box": 0.6,
    "big_box_score": 0.6,
    "face_margin": 0.3,
    "head_margin": 0.15,
    "pixel_blocks": 6,
}
ENGINE_LABELS = {"yoloface-m": "face (YOLOv12m-face)", "yolopose": "head from pose (YOLO11x-pose)",
                 "hand": "extra_blur.json"}


class Detector:
    """Runs the face engine and the pose engine. detect() returns (regions, people)."""

    def __init__(self, allow_download=True, model_dir=None, log=print, threads=0):
        kw = dict(model_dir=model_dir, allow_download=allow_download, log=log)
        s = SETTINGS
        self.face = engines.YoloFace(s["face_engine"], score=min(s["face_score"], s["person_signal_score"]),
                                     tile=s["face_tile"], threads=threads, **kw)
        self.pose = engines.YoloPose(s["pose_engine"], score=s["person_signal_score"], kp_conf=s["pose_kp_score"],
                                     tile=s["pose_tile"], threads=threads, **kw)
        self.name = f"{s['face_engine']}+{s['pose_engine']}"
        models = [engines.MODELS[k].sha256 for k in (s["face_engine"], s["pose_engine"])]
        self.version = hashlib.sha256(json.dumps([self.name, SETTINGS, models], sort_keys=True)
                                      .encode()).hexdigest()[:12]

    def detect(self, img):
        face_hits, people = self.zoom(img, self.face.detect(img), self.pose.people(img))
        return select(img.shape[:2], face_hits, people)

    def zoom(self, img, face_hits, people):
        """Look again, at about twice the size, around small people and small faces with no person."""
        s = SETTINGS
        h, w = img.shape[:2]
        small = [f.box for f in face_hits if f.score >= s["face_score"] and max(f.box[2:]) < s["lone_face_size"]
                  and not any(inside((f.box[0] + f.box[2] / 2, f.box[1] + f.box[3] / 2), p[0], 0.15)
                              for p in people if p[1] >= s["pose_score"])]
        small += [p[0] for p in sorted(people, key=lambda p: -p[1]) if p[0][3] < s["zoom_small"]]
        wins = engines.zoom_windows(small, w, h, s["zoom_side"])
        if not wins:
            return face_hits, people
        return self.face.detect(img, wins, face_hits), self.pose.people(img, wins, people)


def select(shape, face_hits, people, dropped=None):
    """Apply the confidence, size, edge and stamp rules. people: [(person_box, score, keypoints[17, 3])].

    Returns (regions, signal): regions are dicts {box, engine, kind, score}; signal counts every face or
    person seen at any score above person_signal_score, used for the optional auto-approval. Boxes left
    out by a rule are added to `dropped` with the rule's name, if a list is given.
    """
    s = SETTINGS
    h, w = shape
    signal = len(face_hits) + len(people)
    band = h * (1 - s["stamp_band"])
    people = [p for p in people if p[1] >= s["pose_score"]]
    regions, lone, out = [], [], []
    for hit in face_hits:
        cut = clip(hit.box, w, h)
        r = {"box": cut, "engine": hit.engine, "kind": "face", "score": round(hit.score, 3)}
        why = face_rule(hit, cut, people, band, min(h, w))
        if why == "lone":
            lone.append(r)
        elif why:
            if hit.score >= s["face_score"]:
                out.append(dict(r, rule=why))
        else:
            regions.append(r)
    for pbox, score, kp in people:
        head = engines.head_from_keypoints(kp, pbox, s["pose_kp_score"])
        if not head:
            continue
        r = {"box": clip(head, w, h), "engine": s["pose_engine"], "kind": "head", "score": round(score, 3)}
        why = head_rule(head, pbox, score, face_hits, band)
        if why:
            out.append(dict(r, rule=why))
        else:
            regions.append(r)
    # A small face among other faces or heads is a face in a group, not a letter of a caption.
    centres = [(o["box"][0] + o["box"][2] / 2, o["box"][1] + o["box"][3] / 2) for o in regions]
    for r in lone:
        x, y, bw, bh = r["box"]
        reach = s["group_reach"] * max(bw, bh)
        if any(abs(cx - x - bw / 2) < reach and abs(cy - y - bh / 2) < reach for cx, cy in centres):
            regions.append(r)
        else:
            out.append(dict(r, rule="lone"))
    if dropped is not None:
        dropped.extend(out)
    regions = [r for r in regions if r["box"][2] >= 2 and r["box"][3] >= 2]
    return regions, signal


def face_rule(hit, cut, people, band, short_side):
    """Name of the rule that leaves out this face hit, or None to blur it."""
    s = SETTINGS
    x, y, bw, bh = hit.box
    if hit.score < s["face_score"] or min(bw, bh) < s["min_box"]:
        return "weak"
    if max(bw, bh) > s["big_box"] * short_side and hit.score < s["big_box_score"]:
        return "huge"
    at_edge = x < 1 or y < 1 or x + bw > cut[0] + cut[2] + 1 or y + bh > cut[1] + cut[3] + 1
    if not at_edge and not s["face_shape"][0] <= bw / bh <= s["face_shape"][1] and hit.score < s["edge_face_score"]:
        return "shape"
    cx, cy = x + bw / 2, y + bh / 2
    near_person = any(inside((cx, cy), p[0], 0.15) for p in people)
    # The bottom band holds the GPS stamp and its map inset: a face there needs a person under it.
    if cy > band and hit.score < s["stamp_face_score"] and not near_person:
        return "stamp"
    # Small faces with no person around are mostly letters of a caption.
    if max(bw, bh) < s["lone_face_size"] and hit.score < s["lone_face_score"] and not near_person:
        return "lone"
    # A sliver cut off by the photo edge, with no person found there, is a hand or cloth.
    if at_edge and min(cut[2], cut[3]) < s["edge_face_ratio"] * max(cut[2], cut[3]) \
            and hit.score < s["edge_face_score"] and not near_person:
        return "edge"
    return None


def head_rule(head, pbox, score, face_hits, band):
    """Name of the rule that leaves out this pose head, or None to blur it."""
    s = SETTINGS
    size = max(head[2], head[3])
    seen = any(covers(head, f.box) for f in face_hits if f.score >= s["person_signal_score"])
    strong_face = any(covers(head, f.box) and f.score >= s["stamp_face_score"] for f in face_hits)
    body = max(pbox[2], pbox[3]) >= s["body_to_head"] * size
    # In the stamp band a head needs a clear person with a body, or a clear face.
    if head[1] + head[3] / 2 > band and not strong_face and not (score >= s["stamp_person_score"] and body):
        return "stamp"
    # A large head from a weak person that the face engine does not confirm is usually a shadow or soil.
    if size >= s["big_head"] and score < s["weak_person_score"] and not seen:
        return "shadow"
    return None


def covers(head, face):
    """True when at least half of the face box lies inside the head box."""
    x, y, w, h = head
    fx, fy, fw, fh = face
    ix = max(0, min(x + w, fx + fw) - max(x, fx))
    iy = max(0, min(y + h, fy + fh) - max(y, fy))
    return ix * iy >= 0.5 * fw * fh


def inside(pt, b, grow=0.0):
    x, y, w, h = b
    return x - w * grow <= pt[0] <= x + w * (1 + grow) and y - h * grow <= pt[1] <= y + h * (1 + grow)


def clip(b, w, h):
    x0, y0 = max(0, int(round(b[0]))), max(0, int(round(b[1])))
    x1, y1 = min(w, int(round(b[0] + b[2]))), min(h, int(round(b[1] + b[3])))
    return [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]


def blur_regions(img, regions):
    """Blur an ellipse around each region (face or head) and nothing else.

    Each region is pixelated to a few blocks and smoothed, so a face cannot be recognised; the edge of
    the ellipse is feathered so the rest of the photo is left as it was.
    """
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.float32)
    for r in regions:
        x, y, bw, bh = r["box"]
        m = SETTINGS["face_margin"] if r.get("kind") == "face" else SETTINGS["head_margin"]
        if r.get("kind") == "hand":
            m = 0.0
        cx, cy = x + bw / 2, y + bh / 2
        ax, ay = bw / 2 * (1 + m), bh / 2 * (1 + m)
        if r.get("kind") == "hand":
            cv2.rectangle(mask, (int(x), int(y)), (int(x + bw), int(y + bh)), 1.0, -1)
        else:
            cv2.ellipse(mask, (int(round(cx)), int(round(cy))), (max(1, int(round(ax))), max(1, int(round(ay)))),
                        0, 0, 360, 1.0, -1)
    if not mask.any():
        return img
    ys, xs = np.nonzero(mask)
    pad = 8
    x0, y0 = max(0, xs.min() - pad), max(0, ys.min() - pad)
    x1, y1 = min(w, xs.max() + pad + 1), min(h, ys.max() + pad + 1)
    blurred = img[y0:y1, x0:x1].copy()
    for r in regions:
        x, y, bw, bh = r["box"]
        m = 0.0 if r.get("kind") == "hand" else max(SETTINGS["face_margin"], SETTINGS["head_margin"])
        rx0, ry0 = max(x0, int(x - bw * m / 2) - 2), max(y0, int(y - bh * m / 2) - 2)
        rx1, ry1 = min(x1, int(x + bw * (1 + m / 2)) + 2), min(y1, int(y + bh * (1 + m / 2)) + 2)
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            continue
        region = img[ry0:ry1, rx0:rx1]
        n = SETTINGS["pixel_blocks"]
        small = cv2.resize(region, (n, max(1, round(n * (ry1 - ry0) / (rx1 - rx0)))), interpolation=cv2.INTER_AREA)
        block = cv2.resize(small, (rx1 - rx0, ry1 - ry0), interpolation=cv2.INTER_NEAREST)
        k = max(3, (min(rx1 - rx0, ry1 - ry0) // 3) | 1)
        blurred[ry0 - y0:ry1 - y0, rx0 - x0:rx1 - x0] = cv2.GaussianBlur(block, (k, k), 0)
    feather = cv2.GaussianBlur(mask[y0:y1, x0:x1], (0, 0), 1.5)
    feather = np.maximum(feather, mask[y0:y1, x0:x1])[:, :, None]
    out = img[y0:y1, x0:x1].astype(np.float32) * (1 - feather) + blurred.astype(np.float32) * feather
    img[y0:y1, x0:x1] = np.clip(out, 0, 255).astype(np.uint8)
    return img


def load_image(path):
    """Decode with EXIF orientation applied; the decoded pixels carry no metadata."""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not decode {path}")
    return img


def fit(img, max_side):
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return img
    return cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)


def make_public_copy(src, dest, detector, extra_boxes=()):
    """Write a blurred, resized, metadata-free JPEG. Returns (regions, signal, width, height)."""
    img = fit(load_image(src), SETTINGS["max_side"])
    regions, signal = detector.detect(img)
    hand = [{"box": [int(v) for v in b], "engine": "hand", "kind": "hand"} for b in extra_boxes]
    blur_regions(img, regions + hand)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, SETTINGS["jpeg_quality"]])
    if not ok:
        raise ValueError(f"could not encode {src}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(dest)
    return regions, signal, img.shape[1], img.shape[0]
