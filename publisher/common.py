"""Paths and files shared by the publish and drive-links steps. Standard library only."""
import hashlib
import json
import os
import shutil
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Paths:
    def __init__(self, root=ROOT, publish_dir=None):
        self.root = Path(root)
        self.publish = Path(publish_dir) if publish_dir else self.root / "publish"
        self.cases = self.root / "site" / "data" / "cases.json"
        self.site_photos = self.root / "site" / "photos"
        self.data_photos = self.root / "data" / "photos"
        self.out_photos = self.publish / "photos"
        self.manifest = self.publish / "manifest.json"
        self.hide = self.publish / "hide.txt"
        self.extra_blur = self.publish / "extra_blur.json"
        self.review = self.publish / "review.html"
        self.approved = self.publish / "approved.txt"
        self.settings = self.publish / "settings.json"
        self.upload = self.publish / "upload"
        self.links = self.root / "site" / "data" / "photo_links.json"


def case_photo_hashes(cases_path):
    """sha256 -> relative photo path, for every unique photo the cases reference."""
    data = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    out = {}
    for c in data.get("cases", []):
        rel = c.get("photo")
        if not rel:
            continue
        sha = Path(rel).stem.lower()
        if SHA_RE.match(sha):
            out.setdefault(sha, rel)
    return out


def file_hashes(path):
    """(sha256, md5) of a file's bytes."""
    s, m = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            s.update(chunk)
            m.update(chunk)
    return s.hexdigest(), m.hexdigest()


def approval_line(sha, copy_sha256):
    return f"{sha} {copy_sha256[:16]}"


def read_approved(path):
    """sha256 of the original -> first 16 hex of the sha256 of the public copy a person checked."""
    out = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            parts = line.split("#", 1)[0].lower().split()
            if not parts:
                continue
            if len(parts) != 2 or not SHA_RE.match(parts[0]) or not re.match(r"^[0-9a-f]{16}$", parts[1]):
                print(f"approved.txt: ignoring a line that is not '<sha256> <copy check>': {line[:90]}")
                continue
            out[parts[0]] = parts[1]
    return out


def is_approved(approved, sha, entry):
    copy = (entry or {}).get("copy_sha256", "")
    return bool(copy) and approved.get(sha) == copy[:16]


def sync_upload(paths, manifest, approved, hidden):
    """publish/upload/ holds only the copies a person approved; everything else is removed from it."""
    paths.upload.mkdir(parents=True, exist_ok=True)
    keep = {sha for sha, e in manifest.items() if sha not in hidden and is_approved(approved, sha, e)
            and (paths.out_photos / f"{sha}.jpg").exists()}
    for f in paths.upload.iterdir():
        if f.is_file() and (f.suffix != ".jpg" or f.stem not in keep):
            f.unlink()
    for sha in keep:
        src, dest = paths.out_photos / f"{sha}.jpg", paths.upload / f"{sha}.jpg"
        if not dest.exists() or file_hashes(dest)[0] != manifest[sha]["copy_sha256"]:
            shutil.copyfile(src, dest)
    return keep


def read_hide(path):
    hidden = set()
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            token = line.split("#", 1)[0].strip().lower()
            if token:
                if not SHA_RE.match(token):
                    print(f"hide.txt: ignoring a line that is not a sha256: {token[:70]}")
                    continue
                hidden.add(token)
    return hidden


def ensure_hand_files(paths):
    paths.publish.mkdir(parents=True, exist_ok=True)
    if not paths.hide.exists():
        paths.hide.write_text(
            "# One sha256 per line. A photo listed here is left out of publish/upload/ and photo_links.json.\n"
            "# If it is already in the Drive folder, delete it there too.\n"
            "# Text after # is ignored, so you can add a note.\n", encoding="utf-8")
    if not paths.approved.exists():
        paths.approved.write_text(
            "# One line per photo a person has looked at in review.html and found no recognisable face in.\n"
            "# Copy the lines from review.html. Each line ends with a check of the blurred copy, so a photo\n"
            "# that is blurred again must be approved again.\n", encoding="utf-8")
    if not paths.extra_blur.exists():
        paths.extra_blur.write_text("{}\n", encoding="utf-8")
    if not paths.settings.exists():
        write_json(paths.settings, DEFAULT_SETTINGS)


DEFAULT_SETTINGS = {
    "auto_approve_no_person": False,
    # The owner's standing choice to approve every blurred copy without looking at each one.
    "auto_approve_all": False,
}
AUTO_NOTE = "# auto: no person found by any engine"
ALL_NOTE = "# auto: approved in bulk by the owner (auto_approve_all), not checked one by one"


def read_settings(path):
    out = dict(DEFAULT_SETTINGS)
    if Path(path).exists():
        raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
        for key, value in raw.items():
            if key in DEFAULT_SETTINGS and isinstance(value, bool):
                out[key] = value
            else:
                print(f"settings.json: ignoring {key!r}")
    return out


def auto_approve(path, manifest, approved, hidden, enabled, approve_all=False):
    """With enabled (auto_approve_no_person) on, add approval lines for copies where no engine saw any
    face or person. With approve_all (auto_approve_all) on, add them for every blurred copy, marked
    ALL_NOTE: the owner chose to approve in bulk without a check.

    Earlier automatic lines are dropped when their setting is off or the copy has changed. Returns how many
    lines were added.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep = []
    for line in lines:
        if line.endswith(ALL_NOTE):
            sha = line.split()[0].lower()
            e = manifest.get(sha)
            if not approve_all or not e or sha in hidden or approved.get(sha) != e.get("copy_sha256", "")[:16]:
                approved.pop(sha, None)
                continue
        elif line.endswith(AUTO_NOTE):
            sha = line.split()[0].lower()
            e = manifest.get(sha)
            if (not enabled or not e or sha in hidden or e.get("people_signal", 1) or e.get("extra_boxes")
                    or approved.get(sha) != e.get("copy_sha256", "")[:16]):
                approved.pop(sha, None)
                continue
        keep.append(line)
    added = 0
    for sha, e in sorted(manifest.items()) if enabled else ():
        if sha in hidden or sha in approved or not e.get("copy_sha256"):
            continue
        if e.get("people_signal", 1) == 0 and not e.get("extra_boxes"):
            keep.append(f"{approval_line(sha, e['copy_sha256'])}  {AUTO_NOTE}")
            approved[sha] = e["copy_sha256"][:16]
            added += 1
    for sha, e in sorted(manifest.items()) if approve_all else ():
        if sha in hidden or sha in approved or not e.get("copy_sha256"):
            continue
        keep.append(f"{approval_line(sha, e['copy_sha256'])}  {ALL_NOTE}")
        approved[sha] = e["copy_sha256"][:16]
        added += 1
    if keep != lines:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return added


def read_extra_blur(path):
    """sha256 -> list of [x, y, w, h] boxes in published-copy pixels."""
    if not Path(path).exists():
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    out = {}
    for sha, boxes in raw.items():
        sha = sha.lower()
        if not SHA_RE.match(sha) or not isinstance(boxes, list):
            print(f"extra_blur.json: ignoring entry {sha[:70]!r}")
            continue
        clean = []
        for b in boxes:
            if isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b) and b[2] > 0 and b[3] > 0:
                clean.append([int(v) for v in b])
            else:
                print(f"extra_blur.json: ignoring box {b!r} for {sha[:12]}")
        if clean:
            out[sha] = clean
    return out


def read_manifest(path):
    if Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("photos", {})
    return {}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def write_manifest(path, photos):
    write_json(path, {"version": 1, "photos": photos})
