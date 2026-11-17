"""python -m publisher drive-links: read a public Google Drive folder and write site/data/photo_links.json.

Read only: it never uploads, shares or changes anything in Drive or in the local sync folder.

With GOOGLE_API_KEY set it lists the folder through the Drive API and checks the checksums Drive reports.
Without a key it reads the folder's public page, which has no checksums, so --local-folder (the Drive
sync folder on this computer) is required and each file is checked against the local file of that name.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import common

API_BASE = "https://www.googleapis.com/drive/v3"
PAGE_BASE = "https://drive.google.com"
USER_AGENT = "Mozilla/5.0"
THUMB = "https://drive.google.com/thumbnail?id={id}&sz=w{size}"
FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
NAME_RE = re.compile(r"^([0-9a-f]{64})\.jpg$")
IGNORED = {"desktop.ini"}
MAX_PAGE = 50 << 20


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


def approved_copies(paths):
    """(manifest, hidden, expected, allowed) where allowed holds the approved, non-hidden published copies."""
    manifest = common.read_manifest(paths.manifest)
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
    return manifest, hidden, expected, allowed


def report(log, out, total, links, allowed, problems, dupes=()):
    log(f"{total} files in the folder, {len(links)} linked, "
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


def run(paths, folder_id, api_key, api_base=API_BASE, out=None, log=print):
    if not FOLDER_RE.match(folder_id or ""):
        log("The folder id looks wrong. Copy it from the folder URL: drive.google.com/drive/folders/<id>")
        return 2
    if not api_key:
        log("Set GOOGLE_API_KEY to an API key with the Google Drive API enabled.")
        return 2
    manifest, hidden, expected, allowed = approved_copies(paths)
    if not manifest:
        log(f"{paths.manifest} is empty or missing. Run python run.py publish first.")
        return 1
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
    return report(log, out, len(files), links, allowed, problems, dupes)


class FolderPage(HTMLParser):
    """Collects the entries of drive.google.com/embeddedfolderview: one div.flip-entry per file or subfolder."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found_list = False
        self.entries = []
        self.depth = 0
        self.entry = None
        self.entry_depth = self.title_depth = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and self.entry is not None and self.entry["href"] is None:
            self.entry["href"] = a.get("href") or ""
        if tag != "div":
            return
        classes = (a.get("class") or "").split()
        if "flip-entries" in classes:
            self.found_list = True
        if "flip-entry" in classes and self.entry is None:
            self.entry = {"div_id": a.get("id") or "", "href": None, "title": None}
            self.entry_depth = self.depth
        elif "flip-entry-title" in classes and self.entry is not None and self.title_depth is None:
            self.entry["title"] = ""
            self.title_depth = self.depth
        self.depth += 1

    def handle_endtag(self, tag):
        if tag != "div":
            return
        self.depth -= 1
        if self.title_depth is not None and self.depth == self.title_depth:
            self.title_depth = None
        if self.entry is not None and self.depth == self.entry_depth:
            self.entries.append(self.entry)
            self.entry = None

    def handle_data(self, data):
        if self.title_depth is not None:
            self.entry["title"] += data


LAYOUT_HELP = ("The Drive folder page was not recognised ({why}). Google may have changed the page, or the "
               "folder is not shared with 'Anyone with the link'. Use the API-key mode instead (GOOGLE_API_KEY).")


def parse_folder_page(html):
    """[{"id", "name", "folder"}] for every entry on the page. Raises DriveError if the layout is unknown."""
    p = FolderPage()
    p.feed(html)
    p.close()
    if not p.found_list:
        raise DriveError(LAYOUT_HELP.format(why="no file list on the page"))
    out = []
    for e in p.entries:
        fid = e["div_id"][len("entry-"):] if e["div_id"].startswith("entry-") else ""
        name = (e["title"] or "").strip()
        href = e["href"] or ""
        if not FILE_ID_RE.match(fid) or not name or fid not in href:
            raise DriveError(LAYOUT_HELP.format(why="an entry without a usable file id or name"))
        out.append({"id": fid, "name": name, "folder": "/drive/folders/" in href})
    return out


def fetch_folder_page(folder_id, page_base=PAGE_BASE, timeout=60):
    url = f"{page_base.rstrip('/')}/embeddedfolderview?id={urllib.parse.quote(folder_id, safe='-_')}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if urllib.parse.urlparse(r.geturl()).netloc != urllib.parse.urlparse(url).netloc:
                raise DriveError("Drive sent the request to a sign-in page, so the folder is not public. Share it: "
                                 "Share, General access, 'Anyone with the link', Viewer.")
            body = r.read(MAX_PAGE + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise DriveError(f"Drive answered {e.code}: the folder does not exist or is not shared with "
                             f"'Anyone with the link'. Check the folder id and the sharing setting.") from None
        raise DriveError(f"Drive answered {e.code}: {e.reason}") from None
    except urllib.error.URLError as e:
        raise DriveError(f"could not reach Google Drive: {e.reason}") from None
    if len(body) > MAX_PAGE:
        raise DriveError(LAYOUT_HELP.format(why="the page is unexpectedly large"))
    return body.decode("utf-8", errors="replace")


def local_problem(sha, s256, hidden, expected, allowed):
    if s256 == sha:
        return "ORIGINAL unblurred photo"
    if sha in hidden:
        return "listed in hide.txt"
    if sha not in expected:
        return "not a published copy"
    if s256 != expected[sha]["sha256"]:
        return "content differs from the blurred copy in publish/photos"
    if sha not in allowed:
        return "not approved in approved.txt"
    return None


def run_public(paths, folder_id, local_folder, page_base=PAGE_BASE, out=None, log=print):
    """No-key mode: link a file only if its name is an approved copy and the synced local file matches it."""
    if not FOLDER_RE.match(folder_id or ""):
        log("The folder id looks wrong. Copy it from the folder URL: drive.google.com/drive/folders/<id>")
        return 2
    if not local_folder:
        log("Without GOOGLE_API_KEY, give --local-folder: the Google Drive sync folder on this computer that holds "
            "the same files. The public page has no checksums, so the files are checked there.")
        return 2
    local_folder = Path(local_folder)
    if not local_folder.is_dir():
        log(f"{local_folder} is not a folder.")
        return 2
    manifest, hidden, expected, allowed = approved_copies(paths)
    if not manifest:
        log(f"{paths.manifest} is empty or missing. Run python run.py publish first.")
        return 1
    local = {p.name.lower(): p for p in local_folder.iterdir() if p.name.lower() not in IGNORED}
    try:
        entries = [e for e in parse_folder_page(fetch_folder_page(folder_id, page_base))
                   if e["name"].lower() not in IGNORED]
    except DriveError as e:
        log(str(e))
        return 1
    if len(entries) != len(local):
        log(f"The Drive page lists {len(entries)} files but {local_folder} holds {len(local)}. The page may be "
            f"truncated, or Drive may still be syncing. Wait for the sync to finish and run this again, or use "
            f"the API-key mode (GOOGLE_API_KEY). Nothing was written.")
        return 1
    counts = {}
    for e in entries:
        counts[e["name"].lower()] = counts.get(e["name"].lower(), 0) + 1
    links, problems, seen = {}, [], set()
    for e in sorted(entries, key=lambda e: e["id"]):
        name, key = e["name"], e["name"].lower()
        seen.add(key)
        m = NAME_RE.match(key)
        path = local.get(key)
        if e["folder"]:
            reason = "a subfolder; its contents were not checked"
        elif not m:
            reason = "not a published photo"
        elif counts[key] > 1:
            reason = "appears more than once in the Drive folder, so not every copy can be checked"
        elif path is None or not path.is_file():
            reason = "not in the local folder, so its content cannot be checked"
        else:
            reason = local_problem(m.group(1), common.file_hashes(path)[0], hidden, expected, allowed)
        if reason:
            problems.append((name, reason))
            continue
        fid = urllib.parse.quote(e["id"], safe="-_")
        links[m.group(1)] = {"full": THUMB.format(id=fid, size=1600), "thumb": THUMB.format(id=fid, size=480)}
    for key, path in sorted(local.items()):
        if key not in seen:
            problems.append((path.name, "in the local folder but not on the Drive page"))
    out = Path(out) if out else paths.links
    common.write_json(out, links)
    return report(log, out, len(entries), links, allowed, problems)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher drive-links", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder-id", required=True)
    ap.add_argument("--local-folder", help="the Google Drive sync folder on this computer; needed without an API key")
    ap.add_argument("--root", default=str(common.ROOT))
    ap.add_argument("--publish-dir")
    ap.add_argument("--out", help="default: site/data/photo_links.json")
    ap.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    ap.add_argument("--page-base", default=PAGE_BASE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    paths = common.Paths(Path(a.root), a.publish_dir)
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if key:
        return run(paths, a.folder_id, key, a.api_base, a.out)
    print("GOOGLE_API_KEY is not set, so the folder's public page is read instead.")
    return run_public(paths, a.folder_id, a.local_folder, a.page_base, a.out)


if __name__ == "__main__":
    sys.exit(main())
