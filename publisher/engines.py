"""The two ONNX engines used by the publisher, run with onnxruntime, and their verified model files."""
import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

MODEL_DIR = Path(__file__).resolve().parent / "models"


@dataclass
class Model:
    file: str
    url: str
    sha256: str
    licence: str


MODELS = {
    "yoloface-m": Model("yolov12m-face.onnx", "https://github.com/akanametov/yolo-face/releases/download/1.0.0/yolov12m-face.onnx",
                        "1d54b41c9e5dd2e428b841656eeef9ff5ebd8bca6a6d9381eb78d3eea902373e", "AGPL-3.0"),
    "yolopose": Model("yolo11x-pose.onnx", "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11x-pose.onnx",
                      "485164bd938c2d5e726336275dc8fd7ad04de495ebf3f8ec1ed15bde99f178c4", "AGPL-3.0"),
}


@dataclass
class Hit:
    box: list
    score: float
    engine: str


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path(key, model_dir=None, allow_download=True, log=print):
    """Path to a verified model file, downloading it once from its official release if allowed."""
    m = MODELS[key]
    d = Path(model_dir or MODEL_DIR)
    path = d / m.file
    if not path.exists():
        if not allow_download:
            raise FileNotFoundError(f"{path} is missing; run without --no-download or see publisher/models/SOURCE.md")
        d.mkdir(parents=True, exist_ok=True)
        log(f"Downloading {m.file} from {m.url}")
        tmp = path.with_suffix(".part")
        req = urllib.request.Request(m.url, headers={"User-Agent": "publisher-model-fetch"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if sha256_file(tmp) != m.sha256:
            tmp.unlink()
            raise ValueError(f"downloaded {m.file} does not match the sha256 in publisher/models/SOURCE.md")
        tmp.replace(path)
    if sha256_file(path) != m.sha256:
        raise ValueError(f"{path.name} does not match the expected sha256; delete it to fetch it again")
    return path


PROVIDERS = {"cpu": "CPUExecutionProvider", "dml": "DmlExecutionProvider"}


def onnx_session(path, threads=0):
    """PUBLISH_ONNX_PROVIDER=dml runs the models on the graphics chip through DirectML (needs the
    onnxruntime-directml package instead of onnxruntime); unset or cpu runs them on the CPU."""
    import onnxruntime as ort
    want = os.environ.get("PUBLISH_ONNX_PROVIDER", "cpu").strip().lower() or "cpu"
    if want not in PROVIDERS:
        raise ValueError(f"PUBLISH_ONNX_PROVIDER={want!r}: use one of {', '.join(PROVIDERS)}")
    if PROVIDERS[want] not in ort.get_available_providers():
        raise ValueError(f"PUBLISH_ONNX_PROVIDER={want} needs {PROVIDERS[want]}, which this onnxruntime lacks")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if threads:
        so.intra_op_num_threads = threads
    if want == "dml":
        # DirectML runs one request at a time and needs these off.
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(str(path), so, providers=[PROVIDERS[want]])


def nms(hits, iou=0.4):
    if not hits:
        return []
    boxes = [[float(v) for v in h.box] for h in hits]
    keep = cv2.dnn.NMSBoxes(boxes, [float(h.score) for h in hits], 0.0, iou)
    return [hits[i] for i in np.array(keep).flatten()]


def tiles(w, h, size, overlap=0.25):
    """Square-ish windows of `size` pixels covering the image with some overlap."""
    if max(w, h) <= size:
        return [(0, 0, w, h)]
    out = []
    step = int(size * (1 - overlap))
    xs = list(range(0, max(1, w - size) + 1, step)) or [0]
    ys = list(range(0, max(1, h - size) + 1, step)) or [0]
    if xs[-1] + size < w:
        xs.append(max(0, w - size))
    if ys[-1] + size < h:
        ys.append(max(0, h - size))
    for y in ys:
        for x in xs:
            out.append((x, y, min(size, w - x), min(size, h - y)))
    return out


def letterbox(img, side):
    h, w = img.shape[:2]
    s = side / max(h, w)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    pad = np.full((side, side, 3), 114, np.uint8)
    pad[:nh, :nw] = r
    return pad, s


class YoloFace:
    """A YOLO face model exported to ONNX with NMS (output [1, 300, 6]), whole image plus tiles."""
    name = "yoloface"

    def __init__(self, key="yoloface", score=0.3, tile=800, threads=0, **kw):
        self.sess = onnx_session(model_path(key, **kw), threads)
        self.inp = self.sess.get_inputs()[0].name
        self.score, self.tile = score, tile
        self.name = key

    def _run(self, img):
        pad, s = letterbox(img, 640)
        blob = cv2.dnn.blobFromImage(pad, 1 / 255.0, (640, 640), swapRB=True)
        out = self.sess.run(None, {self.inp: blob})[0][0]
        return [Hit([r[0] / s, r[1] / s, (r[2] - r[0]) / s, (r[3] - r[1]) / s], float(r[4]), self.name)
                for r in out if r[4] >= self.score]

    def detect(self, img, windows=None, hits=None):
        """Faces on the whole image and tiles, or only in `windows` (added to `hits`)."""
        h, w = img.shape[:2]
        if windows is None:
            hits = self._run(img)
            windows = tiles(w, h, self.tile) if self.tile else []
        hits = list(hits or [])
        for x0, y0, tw, th in windows:
            hits += [Hit([x0 + x.box[0], y0 + x.box[1], x.box[2], x.box[3]], x.score, x.engine)
                     for x in self._run(img[y0:y0 + th, x0:x0 + tw])]
        return nms(hits)


HEAD_KP = (0, 1, 2, 3, 4)


def head_from_keypoints(kp, person_box, kp_conf=0.5):
    """Head box from COCO keypoints [17, 3] (nose, eyes, ears, shoulders). None if the head is not visible."""
    pts = [kp[i, :2] for i in HEAD_KP if kp[i, 2] >= kp_conf]
    if not pts:
        return None
    pts = np.array(pts)
    cx, cy = pts.mean(axis=0)
    spread = float(np.max(pts.max(axis=0) - pts.min(axis=0))) if len(pts) > 1 else 0.0
    sh = [kp[i, :2] for i in (5, 6) if kp[i, 2] >= kp_conf]
    sw = float(np.linalg.norm(sh[0] - sh[1])) if len(sh) == 2 else 0.0
    px, py, pw, ph = person_box
    size = max(spread * 1.8, sw * 0.6, min(pw, ph) * 0.18, 8.0)
    top, bottom = cy - size * 0.6, cy + size * 0.55
    # A small upright person's head is at the top of the box; keypoints that land lower are off.
    if ph > 2 * pw and ph < 160 and cy > py + 0.2 * ph:
        top = py
    return [float(cx - size / 2), float(top), float(size), float(bottom - top)]


class YoloPose:
    """YOLO pose: people with 17 COCO keypoints, whole image plus tiles."""
    name = "yolopose"

    def __init__(self, key="yolopose", score=0.3, kp_conf=0.5, tile=0, threads=0, **kw):
        self.sess = onnx_session(model_path(key, **kw), threads)
        self.inp = self.sess.get_inputs()[0].name
        self.score, self.kp_conf, self.tile = score, kp_conf, tile
        self.name = key

    def _run(self, img):
        pad, s = letterbox(img, 640)
        blob = cv2.dnn.blobFromImage(pad, 1 / 255.0, (640, 640), swapRB=True)
        out = self.sess.run(None, {self.inp: blob})[0][0].T
        out = out[out[:, 4] >= self.score]
        if not len(out):
            return []
        boxes = [[float(r[0] - r[2] / 2), float(r[1] - r[3] / 2), float(r[2]), float(r[3])] for r in out]
        keep = np.array(cv2.dnn.NMSBoxes(boxes, out[:, 4].astype(float).tolist(), 0.0, 0.5)).flatten()
        people = []
        for i in keep:
            kp = out[i, 5:].reshape(17, 3).copy()
            kp[:, :2] /= s
            people.append(([v / s for v in boxes[i]], float(out[i, 4]), kp))
        return people

    def people(self, img, windows=None, found=None):
        """People on the whole image and tiles, or only in `windows` (added to `found`)."""
        h, w = img.shape[:2]
        if windows is None:
            found = self._run(img)
            windows = tiles(w, h, self.tile) if self.tile else []
        found = list(found or [])
        for x0, y0, tw, th in windows:
            for b, sc, kp in self._run(img[y0:y0 + th, x0:x0 + tw]):
                kp[:, 0] += x0
                kp[:, 1] += y0
                found.append(([b[0] + x0, b[1] + y0, b[2], b[3]], sc, kp))
        if not found:
            return []
        keep = np.array(cv2.dnn.NMSBoxes([f[0] for f in found], [f[1] for f in found], 0.0, 0.5)).flatten()
        return [found[i] for i in keep]


def zoom_windows(boxes, w, h, side=320, limit=8):
    """Windows of `side` pixels around small boxes, so the engines can look again at twice the size."""
    wins = []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        if any(x <= cx <= x + ww and y <= cy <= y + hh for x, y, ww, hh in wins):
            continue
        x0 = int(min(max(0, cx - side / 2), max(0, w - side)))
        y0 = int(min(max(0, cy - side / 2), max(0, h - side)))
        wins.append((x0, y0, min(side, w - x0), min(side, h - y0)))
        if len(wins) >= limit:
            break
    return wins
