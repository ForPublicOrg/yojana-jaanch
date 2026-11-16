"""python -m publisher drive-links: read a public Google Drive folder and write site/data/photo_links.json.

Read only: it lists files with an API key and never uploads, shares or changes anything.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import common

API_BASE = "https://www.googleapis.com/drive/v3"
THUMB = "https://drive.google.com/thumbnail?id={id}&sz=w{size}"
FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
NAME_RE = re.compile(r"^([0-9a-f]{64})\.jpg$")


class DriveError(Exception):
    pass


def list_folder(folder_id, api_key, api_base=API_BASE, timeout=60):
    files, token = [], None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed = false",
                  "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, sha256Checksum)",
                  "pageSize": "1000", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                  "key": api_key}
        if token:
            params["pageToken"] = token
        url = f"{api_base.rstrip('/')}/files?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            raise DriveError(f"Drive API answered {e.code}: {msg or e.reason}") from None
        except urllib.error.URLError as e:
            raise DriveError(f"could not reach the Drive API: {e.reason}") from None
        files += data.get("files", [])
        token = data.get("nextPageToken")
        if not token:
            return files


def content_problem(f, sha, expected, originals):
    got = {"sha256": (f.get("sha256Checksum") or "").lower(), "md5": (f.get("md5Checksum") or "").lower()}
    if got["sha256"] == sha or (got["md5"] and got["md5"] == originals.get(sha)):
        return "ORIGINAL unblurred photo"
    want = expected.get(sha)
    if not want:
        return "not a published copy"
    if not got["sha256"] and not got["md5"]:
        return "Drive gave no checksum, so its content cannot be checked"
    if any(got[k] and got[k] != want[k] for k in ("sha256", "md5")):
        return "content differs from the blurred copy in publish/photos"
    return None


def build_links(files, allowed, expected=None, originals=None, hidden=()):
    """Link a Drive file only when its name and its bytes match an approved blurred copy.

    expected: sha -> {"sha256", "md5"} of publish/photos/<sha>.jpg; originals: sha -> md5 of the original.
    Returns (links, dupes, problems) where problems lists (file name, reason) for files that should not be public.
    """
    expected, originals = expected or {}, originals or {}
    links, dupes, problems = {}, [], []
    for f in sorted(files, key=lambda f: f.get("id", "")):
        name = f.get("name", "")
        m = NAME_RE.match(name.lower())
        if not m or not f.get("id"):
            problems.append((name, "not a published photo"))
            continue
        sha = m.group(1)
        reason = content_problem(f, sha, expected, originals)
        if reason != "ORIGINAL unblurred photo" and sha in hidden:
            reason = "listed in hide.txt"
        elif not reason and sha not in allowed:
            reason = "not approved in approved.txt"
        if reason:
            problems.append((name, reason))
            continue
        if sha in links:
            dupes.append(sha)
            continue
        fid = urllib.parse.quote(f["id"], safe="-_")
        links[sha] = {"full": THUMB.format(id=fid, size=1600), "thumb": THUMB.format(id=fid, size=480)}
    return links, dupes, problems


def run(paths, folder_id, api_key, api_base=API_BASE, out=None, log=print):
    if not FOLDER_RE.match(folder_id or ""):
        log("The folder id looks wrong. Copy it from the folder URL: drive.google.com/drive/folders/<id>")
        return 2
    if not api_key:
        log("Set GOOGLE_API_KEY to an API key with the Google Drive API enabled.")
        return 2
    manifest = common.read_manifest(paths.manifest)
    if not manifest:
        log(f"{paths.manifest} is empty or missing. Run python run.py publish first.")
        return 1
    hidden = common.read_hide(paths.hide)
    approved = common.read_approved(paths.approved)
    expected = {}
    for sha in manifest:
        copy = paths.out_photos / f"{sha}.jpg"
        if copy.exists():
            s256, md5 = common.file_hashes(copy)
            expected[sha] = {"sha256": s256, "md5": md5}
    allowed = {sha for sha, e in manifest.items() if sha not in hidden and sha in expected
               and common.is_approved(approved, sha, {"copy_sha256": expected[sha]["sha256"]})}
    try:
        files = list_folder(folder_id, api_key, api_base)
    except DriveError as e:
        log(str(e))
        return 1
    originals = {}
    for f in files:
        m = NAME_RE.match(f.get("name", "").lower())
        if m and f.get("md5Checksum"):
            for orig in sorted(paths.site_photos.glob(m.group(1) + ".*")) if paths.site_photos.is_dir() else []:
                originals[m.group(1)] = common.file_hashes(orig)[1]
                break
    links, dupes, problems = build_links(files, allowed, expected, originals, hidden)
    out = Path(out) if out else paths.links
    common.write_json(out, links)
    log(f"{len(files)} files in the folder, {len(links)} linked, "
        f"{len(allowed) - len(links)} approved photos not found in the folder.")
    if dupes:
        log(f"{len(dupes)} photos appear more than once in the folder; the first file id was used.")
    log(f"Wrote {out}")
    if problems:
        log(f"WARNING: {len(problems)} files in the folder were not linked, but anyone with the folder link "
            f"can still open them. Delete them from the Drive folder:")
        for name, reason in problems:
            log(f"  {name}: {reason}")
        return 4
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher drive-links", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder-id", required=True)
    ap.add_argument("--root", default=str(common.ROOT))
    ap.add_argument("--publish-dir")
    ap.add_argument("--out", help="default: site/data/photo_links.json")
    ap.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    paths = common.Paths(Path(a.root), a.publish_dir)
    return run(paths, a.folder_id, os.environ.get("GOOGLE_API_KEY", "").strip(), a.api_base, a.out)


if __name__ == "__main__":
    sys.exit(main())
