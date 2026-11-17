"""python -m publisher phash: picture fingerprints of the stored photos, for the checker's
near-duplicate match (checker/nearmatch.py). Run it as python run.py phash, which uses .venv-publish.

For every file <data>/photos/<xx>/<sha256>.<ext> whose sha256 has no line yet in
<data>/state/phash.jsonl, the file is read, its sha256 is checked against its name, the image is
decoded with Pillow and one line is appended:

    {"sha256", "v": 2, "dhash": 64 hex, "w", "h", "flat", "size", "box", "levels", "thumb"}

- box: (left, top, right, bottom) of the picture inside plain borders (content_box): the bars of a
  phone screenshot or letterboxed copy are cut off, so they cannot make two different pictures look
  alike. dhash, thumb, w and h are taken on that part; size is the whole image.
- dhash: the part in greyscale, resized to 17x16 (Lanczos); each of the 256 bits says whether a
  pixel is brighter than its left neighbour, row by row, first bit highest.
- w, h: the size of the part in pixels. EXIF orientation is not applied (see checker/nearmatch.py).
- thumb: the part in greyscale resized to 32x32 (Lanczos), 1024 bytes, base64. The checker uses it
  for the pixel test, so it needs no image decoder.
- levels: distinct grey levels in a 32x32 copy of the whole image (checker placeholder rule).
- flat: true when the file is too plain for a picture match (checker/nearmatch.py low_information),
  or when the part inside the borders is under nearmatch.MIN_CONTENT_SHARE of the whole image.

A file whose bytes match its name but cannot be decoded gets {"sha256", "v": 2, "error": "..."}
and never matches another file by picture; the same bytes would fail again. A file whose content
does not hash to its name gets no line and is logged: prune keeps it, and deleting it by hand lets
--refetch-unhashed fetch a good copy. A file that cannot be opened at the moment (in use, just
deleted) or runs out of memory while decoding gets no line and is tried on the next run.

The file is only appended to, a line at a time, so the run can be stopped at any point; a line cut
off by a crash is ignored when the file is read and closed off by the next run. --compact rewrites
the file once, keeping the first readable line of each sha256 and dropping broken lines, through a
temporary file that replaces it. One phash run at a time per data folder (<data>/state/phash.lock)."""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from checker import nearmatch

NAME_RE = re.compile(r"^([0-9a-f]{64})\.[A-Za-z0-9]{1,5}$")
FLUSH_EVERY = 50
# Border trim (content_box): a row or column is plain when the middle 80% of its grey levels (10th
# to 90th percentile) lie within PLAIN_SPREAD levels, so a status bar or button row across a black
# bar does not make the bar look busy. Plain stretches shorter than GAP_SHARE of the side stay in
# the picture.
PLAIN_SPREAD = 8.0
GAP_SHARE = 0.03


def photo_files(photos_dir):
    """{sha256: path} of the stored photo files; a sha256 stored under two extensions counts once."""
    out = {}
    photos_dir = Path(photos_dir)
    if not photos_dir.is_dir():
        return out
    for sub in sorted(photos_dir.iterdir()):
        if not sub.is_dir():
            continue
        for f in sorted(sub.iterdir()):
            m = NAME_RE.match(f.name)
            if m and f.is_file():
                out.setdefault(m.group(1), f)
    return out


def done_hashes(path):
    """sha256 values that already have a readable line."""
    out = set()
    if not Path(path).exists():
        return out
    with open(path, "rb") as f:
        for raw in f:
            e = nearmatch.parse_line(raw) if raw.strip() else None
            if e is not None:
                out.add(e.sha)
    return out


def _spread(a, axis):
    import numpy as np
    lo, hi = np.percentile(a, [10, 90], axis=axis)
    return hi - lo


def _main_run(spreads):
    """[start, end) of the longest stretch of rows (or columns) that are not plain. Plain stretches
    shorter than GAP_SHARE of the side are bridged, so a plain band inside the picture does not
    split it. With no row that is not plain, the whole side."""
    n = len(spreads)
    idx = [i for i, s in enumerate(spreads) if s > PLAIN_SPREAD]
    if not idx:
        return 0, n
    gap = max(2, int(n * GAP_SHARE))
    runs, start, prev = [], idx[0], idx[0]
    for k in idx[1:]:
        if k - prev - 1 > gap:
            runs.append((start, prev + 1))
            start = k
        prev = k
    runs.append((start, prev + 1))
    return max(runs, key=lambda r: (r[1] - r[0], -r[0]))


def content_box(grey):
    """(left, top, right, bottom) of the picture inside plain borders: the black or white bars of a
    phone screenshot or a letterboxed copy, and a status bar or button row cut off from the picture
    by such a bar. Found on a copy reduced to at most about 400 px a side, in two passes so bars on
    all four sides are found; then each cut edge is placed to the pixel on the full image, because
    a strip of border left in the box shifts the edge of the small copies and flips a whole column
    of dHash bits."""
    import numpy as np
    W, H = grey.size
    f = max(1, max(W, H) // 400)
    small = grey.reduce(f) if f > 1 else grey
    a = np.asarray(small, dtype=np.float32)
    sh, sw = a.shape
    top, bottom, left, right = 0, sh, 0, sw
    for _ in range(2):
        t, b = _main_run(_spread(a[top:bottom, left:right], 1).tolist())
        top, bottom = top + t, top + b
        lft, rgt = _main_run(_spread(a[top:bottom, left:right], 0).tolist())
        left, right = left + lft, left + rgt
    box = [left * f, top * f, min(W, right * f), min(H, bottom * f)]
    if box == [0, 0, W, H]:
        return 0, 0, W, H
    full = np.asarray(grey, dtype=np.float32)
    l0, t0, r0, b0 = box

    def first_busy(lines, lo, hi, row):
        """The first line, in the order given, that is not plain; None if all are plain."""
        for i in lines:
            if _spread(full[i, lo:hi] if row else full[lo:hi, i], 0) > PLAIN_SPREAD:
                return i
        return None
    if t0 > 0:
        i = first_busy(range(max(0, t0 - f), min(H, t0 + 2 * f)), l0, r0, True)
        box[1] = i if i is not None else t0
    if b0 < H:
        i = first_busy(range(min(H, b0 + f) - 1, max(-1, b0 - 2 * f - 1), -1), l0, r0, True)
        box[3] = i + 1 if i is not None else b0
    if l0 > 0:
        i = first_busy(range(max(0, l0 - f), min(W, l0 + 2 * f)), box[1], box[3], False)
        box[0] = i if i is not None else l0
    if r0 < W:
        i = first_busy(range(min(W, r0 + f) - 1, max(-1, r0 - 2 * f - 1), -1), box[1], box[3], False)
        box[2] = i + 1 if i is not None else r0
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return l0, t0, r0, b0
    return tuple(box)


def fingerprint(sha, path):
    """The line for one file; {"skip": why} when the file's bytes do not hash to its name; None when
    the file cannot be read or decoded right now (tried again next run)."""
    from PIL import Image

    try:
        content = Path(path).read_bytes()
    except OSError:
        return None
    real = hashlib.sha256(content).hexdigest()
    if real != sha:
        # Damage on this disk, not a property of the photo: no line, so prune keeps the file and
        # the next run looks again. Deleting it by hand lets --refetch-unhashed fetch a good copy.
        return {"sha256": sha, "skip": f"the content hashes to {real}, not to the file name"}
    try:
        import io
        from PIL import UnidentifiedImageError
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            W, H = im.size
            whole = im.convert("L")
        levels = len(set(whole.resize((nearmatch.THUMB_SIDE, nearmatch.THUMB_SIDE), Image.LANCZOS).tobytes()))
        box = content_box(whole)
        grey = whole.crop(box) if box != (0, 0, W, H) else whole
        w, h = grey.size
        small = grey.resize((17, 16), Image.LANCZOS).tobytes()
        thumb = grey.resize((nearmatch.THUMB_SIDE, nearmatch.THUMB_SIDE), Image.LANCZOS).tobytes()
    except MemoryError:
        # Often caused by the blur run or other decoders at the same moment; retried next run.
        return None
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError,
            EOFError, IndexError, TypeError, KeyError, ZeroDivisionError) as e:
        # The bytes match the name, so the portal's own file is broken; the same bytes fail again.
        return {"sha256": sha, "v": nearmatch.VERSION, "error": f"could not be decoded: {e!r:.200}"}
    except Exception:
        # Not known to be a broken file: no line, so the file is kept and tried again next run.
        return None
    bits = 0
    for r in range(16):
        row = small[r * 17:(r + 1) * 17]
        for c in range(16):
            bits = (bits << 1) | (row[c + 1] > row[c])
    flat = (nearmatch.low_information(thumb, w, h) is not None
            or w * h < nearmatch.MIN_CONTENT_SHARE * W * H)
    return {"sha256": sha, "v": nearmatch.VERSION, "dhash": f"{bits:064x}", "w": w, "h": h, "flat": flat,
            "size": [W, H], "box": list(box), "levels": levels,
            "thumb": base64.b64encode(thumb).decode("ascii")}


def _one(job):
    return job[0], fingerprint(*job)


class Lock:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise

    def release(self):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()


def _open_for_append(path):
    """Open phash.jsonl for appending, after closing off a line cut short by an interrupted run."""
    f = open(path, "ab")
    if f.tell() > 0:
        with open(path, "rb") as r:
            r.seek(-1, os.SEEK_END)
            if r.read(1) != b"\n":
                f.write(b"\n")
    return f


def run(photos_dir, phash_path, workers=1, limit=None, log=print):
    """Append a line for every photo file that has none. Returns a dict of counts."""
    files = photo_files(photos_dir)
    done = done_hashes(phash_path)
    todo = [(sha, files[sha]) for sha in sorted(files) if sha not in done]
    if limit is not None:
        todo = todo[:max(0, limit)]
    counts = {"files": len(files), "already": len(files) - sum(1 for s in files if s not in done),
              "hashed": 0, "flat": 0, "errors": 0, "unreadable": 0, "damaged": 0, "left": 0}
    log(f"phash: {len(files)} photo files, {counts['already']} already have a line, {len(todo)} to read")
    started = time.monotonic()
    os.makedirs(os.path.dirname(os.path.abspath(phash_path)), exist_ok=True)
    out = _open_for_append(phash_path)
    pool = ProcessPoolExecutor(workers) if workers > 1 else None
    written = 0
    try:
        results = pool.map(_one, todo, chunksize=16) if pool else map(_one, todo)
        for n, (sha, line) in enumerate(results, 1):
            if line is None:
                counts["unreadable"] += 1
                continue
            if "skip" in line:
                counts["damaged"] += 1
                log(f"phash: {files[sha]}: {line['skip']}; no line written and the file is kept. Delete it by "
                    f"hand so that python run.py collect --refetch-unhashed downloads a good copy")
                continue
            out.write((json.dumps(line, separators=(",", ":")) + "\n").encode("ascii"))
            written += 1
            if "error" in line:
                counts["errors"] += 1
            else:
                counts["hashed"] += 1
                counts["flat"] += line["flat"]
            if written % FLUSH_EVERY == 0:
                out.flush()
                os.fsync(out.fileno())
            if n % 1000 == 0:
                log(f"phash: {n} of {len(todo)} read ({time.monotonic() - started:.0f} s)")
    except KeyboardInterrupt:
        counts["left"] = len(todo) - written - counts["unreadable"] - counts["damaged"]
        log("phash: stopped; the lines written so far are kept, the rest are read next run")
    finally:
        out.flush()
        os.fsync(out.fileno())
        out.close()
        if pool:
            pool.shutdown(wait=counts["left"] == 0, cancel_futures=True)
    log(f"phash: {counts['hashed']} hashed ({counts['flat']} too plain to match), {counts['errors']} could not be "
        f"read as images, {counts['unreadable']} could not be opened or decoded now (tried again next run), "
        f"{counts['damaged']} do not hash to their name (kept, no line) in {time.monotonic() - started:.0f} s")
    return counts


def compact(phash_path, log=print):
    """Rewrite phash.jsonl with the first readable line of each sha256 (a hashed line before an error
    line) and no broken lines. Returns (lines before, lines after)."""
    path = Path(phash_path)
    if not path.exists():
        return 0, 0
    best, order, before = {}, [], 0
    with open(path, "rb") as f:
        for raw in f:
            before += 1
            e = nearmatch.parse_line(raw) if raw.strip() else None
            if e is None:
                continue
            old = best.get(e.sha)
            if old is None:
                order.append(e.sha)
            if old is None or (not old[0].usable and e.usable):
                best[e.sha] = (e, raw.rstrip(b"\r\n"))
    tmp = path.with_name(path.name + f".{os.getpid()}.part")
    with open(tmp, "wb") as f:
        for sha in order:
            f.write(best[sha][1] + b"\n")
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 4:
                os.remove(tmp)
                raise
            time.sleep(0.5)
    log(f"phash: compacted {path}: {before} lines, now {len(order)}")
    return before, len(order)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher phash", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--data-dir", help="data folder (default: <root>/data)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="processes that decode photos (default: half the processor count)")
    ap.add_argument("--limit", type=int, help="read at most this many new files this run")
    ap.add_argument("--compact", action="store_true", help="rewrite phash.jsonl without duplicate or broken lines")
    a = ap.parse_args(argv)
    data = Path(a.data_dir) if a.data_dir else Path(a.root) / "data"
    if not os.path.isabs(data):
        data = Path(a.root) / data
    state = data / "state"
    try:
        lock = Lock(str(state / "phash.lock"))
    except OSError:
        print(f"another phash run holds {state / 'phash.lock'}; not run", file=sys.stderr)
        return 7
    try:
        if a.compact:
            compact(state / nearmatch.PHASH_FILE)
            return 0
        counts = run(data / "photos", state / nearmatch.PHASH_FILE, max(1, a.workers), a.limit)
        return 130 if counts["left"] else 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
