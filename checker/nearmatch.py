"""Same picture saved as different files: the near-duplicate test used by photo_reuse.

The phash step (python run.py phash, publisher/phash.py) decodes every stored photo and appends one
line per sha256 to <data>/state/phash.jsonl:

    {"sha256", "v": 2, "dhash": 64 hex, "w", "h", "flat", "size", "box", "levels",
     "thumb": base64 of a 32x32 greyscale copy}
    {"sha256", "v": 2, "error": "why it could not be decoded"}

This module reads that file with the standard library only and decides which files hold one
picture. Two files count as one picture only when all of these hold:

- both have a line with a dhash and neither is low information (see low_information below);
- the 256-bit dHashes differ in at most near_max_distance bits;
- the 32x32 greyscale copies, each scaled to mean 0 and standard deviation 1, differ by at most
  near_max_mad on average per pixel (the pixel test). dHash alone cannot tell two shots of one
  pond taken a minute apart from a re-saved copy; the pixel test alone can be fooled the other
  way, so both must pass;
- width / height of the two differ by at most near_max_aspect_gap (relative), and neither is
  landscape while the other is portrait;
- data/state/not_same.txt does not list the pair.

Groups are formed by complete linkage: every file of a group passes all of the tests above against
every other file of the group. A group therefore never joins A and C through a B that is close to
both. The other way to stop that, comparing each file only with one representative at half the
limits, also bounds every pair by the limit, but it keeps fewer true copies, and complete linkage
costs nothing here because the groups are a few files each.

Candidate pairs come from a multi-index: the 256 bits are cut into near_max_distance + 1 bands.
Two hashes within near_max_distance bits differ in at most that many bands, so they agree exactly
on at least one band; only hashes that share a band value are compared.

EXIF orientation is not applied, in this step or in the phash step, as in the calibration. A copy
whose pixels were rotated and one that only carries a rotation tag then fail the aspect test, so
the cost is a missed match, never a wrong one."""
import base64
import html
import json
import math
import os
import re
import struct
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PHASH_FILE = "phash.jsonl"
NOT_SAME_FILE = "not_same.txt"
PLACEHOLDER_FILE = "placeholders.txt"
AUDIT_FILE = "near_matches.html"
# 2: hashes are taken inside plain borders (publisher/phash.py content_box); lines of version 1
# are ignored, so the phash step reads those files again.
VERSION = 2
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_BITS = 256
THUMB_SIDE = 32
MAX_SIDE = 100000        # a line with a larger width or height is broken

# Low-information rule, from the calibration on the collected photos. A file that meets any of
# these never matches another file by picture (an exact sha256 match is not affected).
MIN_SIDE = 100           # shorter side in pixels
MIN_STD = 15.0           # standard deviation of the 32x32 grey levels
MIN_GRADIENT = 6.0       # mean absolute difference between neighbouring pixels of the 32x32
MAX_FLAT_SHARE = 0.6     # share of neighbouring pixel pairs that differ by under 2 grey levels
MIN_CONTENT_SHARE = 0.1   # least share of the whole image left inside plain borders
BAND_RANGE = 8           # a 32x32 row or column within this many grey levels is plain
MAX_BAND_SHARE = 0.2     # most share of the 32x32 copy in plain bands of one colour at the edges

# Placeholder rule: an image that is not a photo of a site (a portal's broken-image icon, a plain
# coloured square, an "Error loading image" graphic). Such a file on several works is not made a
# photo_reuse case. Real photos of bare ground are low information but not placeholders. Levels
# are the distinct grey levels of a 32x32 copy of the whole image. In the calibration the 8 files
# under 32 levels were drawn graphics or near-black map tiles; the 73x73 broken-image icons had 46
# and 47, while real photos under 100 px a side had 125 or more.
PLACEHOLDER_LEVELS = 32
PLACEHOLDER_SIDE = 100        # shorter side of the whole image in pixels
PLACEHOLDER_SMALL_LEVELS = 64  # levels limit for an image under PLACEHOLDER_SIDE

# Limits on the settings. Above these the near match does not run at all: in the calibration the
# closest pair of different photos was 17 bits apart, and the lowest pixel difference of a pair
# of different photos was 0.057.
DISTANCE_CAP = 16
MAD_CAP = 0.05
ASPECT_CAP = 0.05


class Entry:
    __slots__ = ("sha", "dhash", "w", "h", "flat", "thumb", "error", "size", "levels", "_z", "_low")

    def __init__(self, sha, dhash=None, w=None, h=None, flat=False, thumb=None, error=None, size=None,
                 levels=None):
        self.sha, self.dhash, self.w, self.h = sha, dhash, w, h
        self.flat, self.thumb, self.error = flat, thumb, error
        # size: (width, height) of the whole image; w and h are the part inside plain borders.
        self.size, self.levels = size or (w, h), levels
        self._z = self._low = None

    def placeholder(self):
        """Why this file is not a photo of a site, or None."""
        if not self.usable:
            return None
        if self.levels is None:
            return None
        if self.levels < PLACEHOLDER_LEVELS:
            return f"only {self.levels} grey levels (a drawn graphic or a nearly blank image)"
        if min(self.size) < PLACEHOLDER_SIDE and self.levels < PLACEHOLDER_SMALL_LEVELS:
            return f"shorter side under {PLACEHOLDER_SIDE} px and only {self.levels} grey levels (an icon)"
        return None

    @property
    def usable(self):
        return self.dhash is not None and self.error is None

    def pixels(self):
        """The 32x32 grey levels as bytes, or None."""
        if not self.thumb:
            return None
        try:
            raw = base64.b64decode(self.thumb, validate=True)
        except (ValueError, TypeError):
            return None
        return raw if len(raw) == THUMB_SIDE * THUMB_SIDE else None

    def low_information(self):
        """Why this file never matches another by picture, or None."""
        if self._low is None:
            W, H = self.size
            if self.flat:
                self._low = "flat (phash step)"
            elif self.w * self.h < MIN_CONTENT_SHARE * W * H:
                self._low = "mostly plain border"
            else:
                self._low = low_information(self.pixels(), self.w, self.h) or ""
        return self._low or None

    def zscores(self):
        if self._z is None:
            px = self.pixels()
            if px is None:
                return None
            mean = sum(px) / len(px)
            std = math.sqrt(sum((v - mean) ** 2 for v in px) / len(px))
            std = max(std, 1e-6)
            self._z = [(v - mean) / std for v in px]
        return self._z


def _int_or_none(v, top=MAX_SIDE):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= top else None


def parse_line(raw):
    """Entry for one line of phash.jsonl, or None for a broken or unknown line."""
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(d, dict) or d.get("v") != VERSION:
        return None
    sha = d.get("sha256")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        return None
    if d.get("error") is not None:
        return Entry(sha, error=str(d["error"]))
    h = d.get("dhash")
    w, hh = _int_or_none(d.get("w")), _int_or_none(d.get("h"))
    if not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) or w is None or hh is None:
        return None
    size = d.get("size")
    if not (isinstance(size, list) and len(size) == 2):
        return None
    size = (_int_or_none(size[0]), _int_or_none(size[1]))
    if None in size or w > size[0] or hh > size[1]:
        return None
    levels = _int_or_none(d.get("levels"), 256)
    thumb = d.get("thumb") if isinstance(d.get("thumb"), str) else None
    return Entry(sha, int(h, 16), w, hh, d.get("flat") is True, thumb, size=size, levels=levels)


def read_phash(path):
    """{sha256: Entry} from phash.jsonl, or None when the file does not exist. Broken lines (a line
    cut off by an interrupted run, for example) are left out. When a sha256 has several lines, a
    line with a hash wins over an error line, and the first of each kind wins."""
    path = Path(path)
    if not path.exists():
        return None
    out = {}
    with open(path, "rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            e = parse_line(raw)
            if e is None:
                continue
            old = out.get(e.sha)
            if old is None or (not old.usable and e.usable):
                out[e.sha] = e
    return out


def read_not_same(path, log=print):
    """Pairs a person marked as not the same picture: {frozenset({sha_a, sha_b})}. One pair per
    line, two full sha256 values separated by spaces or a comma. Text after # is a comment."""
    path = Path(path)
    out = set()
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        log(f"{path}: could not be read ({e}); no pair is excluded by hand this run")
        return out
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.lower() for p in re.split(r"[\s,;]+", line) if p]
        if len(parts) != 2 or not all(SHA_RE.match(p) for p in parts) or parts[0] == parts[1]:
            log(f"{path} line {n}: expected two different full sha256 values, line ignored")
            continue
        out.add(frozenset(parts))
    return out


def low_information(pixels, w, h):
    """Why a file is too plain for a picture match, or None. pixels: the 32x32 grey levels."""
    if not w or not h or min(w, h) < MIN_SIDE:
        return f"shorter side under {MIN_SIDE} px"
    if pixels is None:
        return "no 32x32 copy stored"
    n = THUMB_SIDE
    mean = sum(pixels) / len(pixels)
    std = math.sqrt(sum((v - mean) ** 2 for v in pixels) / len(pixels))
    if std < MIN_STD:
        return "too little contrast"
    gx = [abs(pixels[r * n + c + 1] - pixels[r * n + c]) for r in range(n) for c in range(n - 1)]
    gy = [abs(pixels[(r + 1) * n + c] - pixels[r * n + c]) for r in range(n - 1) for c in range(n)]
    gradient = (sum(gx) / len(gx) + sum(gy) / len(gy)) / 2
    if gradient < MIN_GRADIENT:
        return "too little detail"
    flat = (sum(1 for v in gx if v < 2) / len(gx) + sum(1 for v in gy if v < 2) / len(gy)) / 2
    if flat > MAX_FLAT_SHARE:
        return "mostly flat areas"
    if plain_band_share(pixels) > MAX_BAND_SHARE:
        return "large plain borders"
    return None


def plain_band_share(pixels):
    """Share of the 32x32 copy in plain bands of one colour along its edges, such as the bars of a
    screenshot that the phash step's border trim did not remove. Such bands agree in every copy and
    would make two different pictures look closer. One edge row or column may differ (a status bar),
    and so may the 2 lightest and 2 darkest pixels of a line (a status bar across a side bar)."""
    n = THUMB_SIDE
    rows = [pixels[r * n:(r + 1) * n] for r in range(n)]
    cols = [bytes(pixels[r * n + c] for r in range(n)) for c in range(n)]

    def plain(line):
        mid = sorted(line)[2:-2]
        return mid[-1] - mid[0] <= BAND_RANGE

    def depth(lines):
        best = 0
        for skip in (0, 1):
            k, ref = skip, None
            while k < n and plain(lines[k]):
                m = sorted(lines[k])[n // 2]
                if ref is None:
                    ref = m
                elif abs(m - ref) > BAND_RANGE:
                    break
                k += 1
            if k > skip:
                best = max(best, k)
        return best
    top, bottom, left, right = depth(rows), depth(rows[::-1]), depth(cols), depth(cols[::-1])
    inside = max(0, n - top - bottom) * max(0, n - left - right)
    return 1 - inside / (n * n)


def pixel_difference(a, b):
    """Mean absolute difference of the two normalised 32x32 copies, or None."""
    za, zb = a.zscores(), b.zscores()
    if za is None or zb is None:
        return None
    return sum(abs(x - y) for x, y in zip(za, zb)) / len(za)


def aspect_gap(a, b):
    """Relative difference of width / height, or None when one is landscape and the other portrait."""
    if (a.w > a.h and b.w < b.h) or (a.w < a.h and b.w > b.h):
        return None
    ra, rb = a.w / a.h, b.w / b.h
    return abs(ra - rb) / max(ra, rb)


def bands(max_distance):
    """(first bit, width) of each of the max_distance + 1 bands of the hash."""
    n = max_distance + 1
    edges = [round(i * HASH_BITS / n) for i in range(n + 1)]
    return [(edges[i], edges[i + 1] - edges[i]) for i in range(n)]


def close_pairs(hashes, max_distance):
    """{(sha_a, sha_b): bits apart} for every pair of hashes at most max_distance bits apart, with
    sha_a < sha_b. hashes: {sha: int}. Every such pair agrees exactly on at least one band."""
    out = {}
    items = sorted(hashes.items())
    for start, width in bands(max_distance):
        shift = HASH_BITS - start - width
        mask = (1 << width) - 1
        buckets = defaultdict(list)
        for sha, h in items:
            buckets[(h >> shift) & mask].append((sha, h))
        for group in buckets.values():
            for i in range(len(group) - 1):
                a, ha = group[i]
                for b, hb in group[i + 1:]:
                    if (a, b) in out:
                        continue
                    d = (ha ^ hb).bit_count()
                    if d <= max_distance:
                        out[(a, b)] = d
    return out


def settings(pcfg):
    """(max_distance, max_mad, max_aspect_gap) from the photo_reuse settings, or (None, reason)."""
    d = pcfg.get("near_max_distance", 0)
    m = pcfg.get("near_max_mad", 0)
    g = pcfg.get("near_max_aspect_gap", 0)
    if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
        return None, "photo_reuse.near_max_distance is 0"
    for name, v in (("near_max_mad", m), ("near_max_aspect_gap", g)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
            return None, f"photo_reuse.{name} is not a number above 0"
    if d > DISTANCE_CAP or m > MAD_CAP or g > ASPECT_CAP:
        return None, (f"the settings are above the safe limits (near_max_distance at most {DISTANCE_CAP}, "
                      f"near_max_mad at most {MAD_CAP}, near_max_aspect_gap at most {ASPECT_CAP})")
    return (d, float(m), float(g)), None


def read_placeholders(path, log=print):
    """sha256 values a person marked as placeholders (not a photo of a site), one per line; text
    after # is a comment."""
    path = Path(path)
    out = set()
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        log(f"{path}: could not be read ({e}); only the automatic placeholder rule applies this run")
        return out
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        if not SHA_RE.match(line):
            log(f"{path} line {n}: expected one full sha256 value, line ignored")
            continue
        out.add(line)
    return out


class Unreadable(Exception):
    """phash.jsonl exists but could not be read this run."""


class NearMatch:
    """phash.jsonl, not_same.txt and placeholders.txt of one data folder, plus what each scheme's
    check did with them."""

    def __init__(self, entries, not_same=(), state_dir=None, placeholders=()):
        self.entries = entries
        self.not_same = set(not_same)
        self.marked_placeholders = set(placeholders)
        self.state_dir = state_dir
        self.report = {}
        self.pairs = []
        self.left_out = []

    @classmethod
    def load(cls, state_dir, log=print):
        """None when there is no phash.jsonl (the phash step has never run for this data folder).
        Raises Unreadable when the file exists but cannot be read: the caller must then not treat
        the data folder as having no phash step, because prune would delete unhashed files."""
        state_dir = Path(state_dir)
        try:
            entries = read_phash(state_dir / PHASH_FILE)
        except OSError as e:
            raise Unreadable(f"{state_dir / PHASH_FILE}: could not be read ({e})") from e
        if entries is None:
            return None
        return cls(entries, read_not_same(state_dir / NOT_SAME_FILE, log), state_dir,
                   read_placeholders(state_dir / PLACEHOLDER_FILE, log))

    def hashed(self):
        """sha256 of every file the phash step has seen: a hash, or an error line for bytes that
        match their name but cannot be decoded."""
        return set(self.entries)

    def placeholder(self, sha):
        """Why the file is not a photo of a site, or None."""
        if sha in self.marked_placeholders:
            return f"listed in {PLACEHOLDER_FILE}"
        e = self.entries.get(sha)
        return e.placeholder() if e is not None else None

    def test(self, a, b, d, limits):
        """(accepted, pixel difference, aspect gap) for two files whose hashes are d bits apart."""
        _, max_mad, max_gap = limits
        ea, eb = self.entries[a], self.entries[b]
        if frozenset((a, b)) in self.not_same:
            return False, None, None
        gap = aspect_gap(ea, eb)
        if gap is None or gap > max_gap:
            return False, None, gap
        mad = pixel_difference(ea, eb)
        if mad is None or mad > max_mad:
            return False, mad, gap
        return True, mad, gap

    def group(self, weights, limits):
        """Split the files of one scheme into pictures.

        weights: {sha256: number of records}. Returns (groups, pair_info, counts): groups is a list
        of sha256 lists, every file in exactly one; a list holds the heaviest file first (most
        records, then the smallest sha256). pair_info: {(a, b): (bits, pixel difference, aspect gap)}
        for the accepted pairs."""
        max_distance = limits[0]
        usable, low = {}, 0
        for sha in weights:
            e = self.entries.get(sha)
            if e is None or not e.usable:
                continue
            if e.low_information() or self.placeholder(sha):
                low += 1
                continue
            usable[sha] = e.dhash
        accepted = {}
        for (a, b), d in close_pairs(usable, max_distance).items():
            ok, mad, gap = self.test(a, b, d, limits)
            if ok:
                accepted[(a, b)] = (d, mad, gap)
        order = lambda s: (-weights[s], s)
        parent = {}

        def root(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a, b in accepted:
            parent[root(a)] = root(b)
        parts = defaultdict(list)
        for x in list(parent):
            parts[root(x)].append(x)
        grouped, groups = set(), []
        for members in parts.values():
            rest = sorted(members, key=order)
            while rest:
                clique = [rest[0]]
                for s in rest[1:]:
                    if all(tuple(sorted((s, x))) in accepted for x in clique):
                        clique.append(s)
                rest = [s for s in rest if s not in clique]
                groups.append(clique)
                grouped.update(clique)
        groups += [[s] for s in weights if s not in grouped]
        groups.sort(key=lambda g: order(g[0]))
        counts = {"photos": len(weights), "photos_with_hash": sum(1 for s in weights if s in self.entries
                                                                    and self.entries[s].usable),
                  "low_information": low}
        return groups, accepted, counts


def png_gray(pixels, side=THUMB_SIDE):
    """A greyscale PNG of side x side pixels, as a data: URL."""
    rows = b"".join(b"\0" + pixels[r * side:(r + 1) * side] for r in range(side))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def audit_html(near, pairs, report, root, out_dir, why_off=None):
    """The private review page: every pair of different files that the check counted as one
    picture, riskiest first (most bits apart, then largest pixel difference)."""
    esc = html.escape
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = sorted(pairs, key=lambda p: (-p["distance"], -p["mad"], p["scheme"], p["a"], p["b"]))

    def picture(side):
        sha, file = side["sha"], side.get("file")
        e = near.entries.get(sha) if near else None
        parts = []
        from .output import find_photo
        found = find_photo(root, file, sha)
        if found is not None:
            rel = os.path.relpath(found, out_dir).replace(os.sep, "/")
            parts.append(f'<a href="{esc(rel)}"><img class="big" src="{esc(rel)}" loading="lazy" alt=""></a>')
        else:
            parts.append('<p class="gone">The file was deleted; only the 32x32 copy below is left.</p>')
        px = e.pixels() if e else None
        if px:
            parts.append(f'<img class="small" src="{png_gray(px)}" alt="">')
        size = f"{e.w} x {e.h}" if e and e.w else "size unknown"
        parts.append(f"<p><code>{esc(sha)}</code><br>{esc(size)}, on {side['records']} work(s)</p>")
        return "<td>" + "".join(parts) + "</td>"

    body = []
    for i, p in enumerate(rows, 1):
        case = esc(p.get("case_id") or "no case (fewer than 2 works)")
        body.append(
            f"<tr><td>{i}</td><td>{esc(p['scheme'])}<br>{case}<br>bits apart: <b>{p['distance']}</b> of 256"
            f"<br>pixel difference: <b>{p['mad']:.4f}</b><br>width/height gap: {p['gap'] * 100:.2f}%"
            f"<br><code class=\"line\">{esc(p['a'])} {esc(p['b'])}</code></td>"
            + picture({"sha": p["a"], "file": p.get("a_file"), "records": p["a_records"]})
            + picture({"sha": p["b"], "file": p.get("b_file"), "records": p["b_records"]}) + "</tr>")
    summary = []
    for scheme, r in sorted(report.items()):
        if r.get("near_matching"):
            summary.append(
                f"<li>{esc(scheme)}: {r['files_merged']} files merged into {r['pictures']} pictures; "
                f"{r['photos_with_hash']} of {r['photos']} photos have a picture hash; largest distance used "
                f"{r['largest_distance']} (limit {r['max_distance']}), largest pixel difference "
                f"{r['largest_mad']:.4f} (limit {r['max_mad']})</li>")
        else:
            summary.append(f"<li>{esc(scheme)}: near matching did not run ({esc(r.get('why_off') or '')})</li>")
        if r.get("placeholder_cases"):
            summary.append(f"<li>{esc(scheme)}: {r['placeholder_cases']} cases show a placeholder image on "
                           f"{r['placeholder_records']} works (listed at the end)</li>")
    head = (f"<p>Written by the checker at {esc(when)}. Not public: this file stays under data/.</p>"
            + (f"<p><b>Near matching did not run:</b> {esc(why_off)}</p>" if why_off else "")
            + "<ul>" + "".join(summary) + "</ul>"
            + "<p>Each row is two different files that the check counted as one picture. If a row shows two "
              "different photos, copy its line of two fingerprints into <code>data/state/not_same.txt</code> "
              "(one pair per line). The next check never puts those two files in one case.</p>")
    table = ("<table><thead><tr><th>#</th><th>Numbers</th><th>File A</th><th>File B</th></tr></thead><tbody>"
             + "".join(body) + "</tbody></table>") if body else "<p>No pair of different files was merged.</p>"
    left = sorted(getattr(near, "left_out", None) or [], key=lambda x: (-x["records"], x["scheme"], x["sha"]))
    if left:
        rows_left = "".join(
            f"<tr><td>{esc(x['scheme'])}<br>{x['records']} works<br>{esc(x['why'])}</td>"
            + picture({"sha": x["sha"], "file": x.get("file"), "records": x["records"]}) + "</tr>" for x in left)
        table += ("<h2>Cases whose image is a placeholder</h2><p>Each of these files is attached to 2 or more works "
                  "and was taken for a placeholder (an icon or plain graphic, not a photo of a site). It is still "
                  "a case; the case says the file is not a photograph. If one is a real photo, the rule in "
                  "checker/nearmatch.py needs a change.</p><table><tbody>" + rows_left + "</tbody></table>")
    style = ("body{font:14px sans-serif;margin:16px}td{vertical-align:top;border-top:1px solid #ccc;padding:6px}"
             "img.big{max-width:420px;max-height:320px}img.small{width:128px;height:128px;"
             "image-rendering:pixelated;display:block;margin-top:4px}.gone{color:#a00}code{font-size:11px}"
             ".line{user-select:all}")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Near matches to review</title>"
            f"<style>{style}</style></head><body><h1>Files counted as one picture</h1>{head}{table}</body></html>")
