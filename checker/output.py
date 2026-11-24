import json
import os
import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def coverage_rows(scheme, records, coverage):
    blocks = defaultdict(lambda: {"villages": set(), "records": set(), "photos": set(), "names": None,
                                  "listed_only": set()})

    # The portal spells the same place in different cases (PUNJAB, Punjab); merge them.
    def block(row):
        names = (row.get("state"), row.get("district"), row.get("block"))
        b = blocks[tuple((x or "").strip().casefold() for x in names)]
        b["names"] = b["names"] or names
        return b

    for r in records:
        b = block(r)
        b["villages"].add(r.get("village_code") or r.get("village"))
        b["records"].add(str(r["record_id"]))
        b["photos"].update(p["sha256"] for p in r.get("photos") or [] if p.get("sha256"))
    for c in coverage:
        b = block(c)
        b["villages"].add(c.get("village_code") or c.get("village"))
        if c.get("photos_state") == "not_fetched":
            b["listed_only"].add(c.get("village_code") or c.get("village"))
    rows = []
    for _, v in sorted(blocks.items()):
        row = {"scheme": scheme, "state": v["names"][0], "district": v["names"][1], "block": v["names"][2],
               "villages_checked": len(v["villages"] - {None}), "records_checked": len(v["records"]),
               "photos_checked": len(v["photos"])}
        listed = len(v["listed_only"] - {None})
        if listed:
            # Areas saved by a list-only run: their records are in, their photos not checked yet.
            row["villages_photos_not_checked"] = listed
        rows.append(row)
    return rows


def areas(cases):
    states = {}
    for c in cases:
        if not c["state"]:
            continue
        st = states.setdefault(c["state"], {"cases": 0, "by_scheme": {}, "districts": {}})
        st["cases"] += 1
        st["by_scheme"][c["scheme"]] = st["by_scheme"].get(c["scheme"], 0) + 1
        if c["district"]:
            d = st["districts"].setdefault(c["district"], {"cases": 0, "by_scheme": {}})
            d["cases"] += 1
            d["by_scheme"][c["scheme"]] = d["by_scheme"].get(c["scheme"], 0) + 1
    return {"states": states}


def _tmp(path):
    # Each process writes its own temporary file, so two checkers never write into one.
    return path.with_name(f"{path.name}.{os.getpid()}.part")


def _replace(tmp, path, tries=5, wait=0.5):
    """On Windows a reader (the site server, an antivirus) can hold the target open for a moment."""
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == tries - 1:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(wait)


def find_photo(root, file, sha):
    """The stored file of a photo: the path the record names, or the same sha256 under another
    extension in that folder (a file downloaded again may be .jpg where the record says .jpeg)."""
    if not file or not sha:
        return None
    path = Path(root) / file
    if path.is_file():
        return path
    try:
        return next((p for p in sorted(path.parent.glob(sha + ".*"))
                     if p.is_file() and ".tmp" not in p.name and p.name.count(".") == 1), None)
    except OSError:
        return None


def copy_photo(root, site, src, gone_ok=False):
    """site/photos/ path of the photo, copied there; None if its file is gone. With gone_ok the path is
    returned anyway: the original may be deleted once its blurred copy is on Drive, which the site finds
    by sha256."""
    if not src or not src.get("file") or not src.get("sha256"):
        return None
    path = find_photo(root, src["file"], src["sha256"])
    if path is None:
        return f"photos/{src['sha256']}{Path(src['file']).suffix.lower() or '.jpg'}" if gone_ok else None
    rel = f"photos/{src['sha256']}{path.suffix.lower() or '.jpg'}"
    dest = site / rel
    if not dest.exists():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = _tmp(dest)
            shutil.copyfile(path, tmp)
            _replace(tmp, dest)
        except OSError:
            return None
    return rel


def write_json(path, data):
    """Whole file or nothing: written to a temporary file of this process, then renamed into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _replace(tmp, path)


def write_text(path, text):
    """Whole file or nothing, like write_json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _replace(tmp, path)


# The site loads summary.json on the home page, data/index/<state>.json on the state, district and
# all-cases pages, and data/cases/<id>.json, the whole case, on a case page.
INDEX_RECORDS = 3
INDEX_DROP = ("reasons", "source_url")
TOP_CASES = 5  # TOP_CASES in site/app.js
ALIASES = {"sahibzadaajitsinghnagar": "sasnagar", "mohali": "sasnagar", "firozpur": "ferozepur",
           "muktsar": "srimuktsarsahib", "nawanshahr": "shahidbhagatsinghnagar", "ropar": "rupnagar"}


def norm(s):
    """Same as norm in site/lib.js, so counts here match what the site would count."""
    k = re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("&", "and"))
    return ALIASES.get(k, k)


def case_file_name(case_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(case_id)) + ".json"


def _write_if_changed(path, body):
    try:
        if json.loads(path.read_text(encoding="utf-8")) == body:
            return
    except (OSError, ValueError):
        pass
    write_json(path, body)


def _remove_others(folder, keep):
    if folder.is_dir():
        for p in folder.glob("*.json"):
            if p.name not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass


def write_case_files(site, cases):
    folder = site / "data" / "cases"
    keep = set()
    for c in cases:
        name = case_file_name(c["id"])
        keep.add(name)
        _write_if_changed(folder / name, c)
    _remove_others(folder, keep)


def index_entry(c):
    e = {k: v for k, v in c.items() if k not in INDEX_DROP}
    e["records"] = [{"record_id": r.get("record_id")} for r in c["records"][:INDEX_RECORDS]]
    return e


def write_index(site, entries):
    """One file per state, in the order of `entries`; returns [{state, file, cases}] by state name."""
    by_state = {}
    for e in entries:
        by_state.setdefault(e.get("state") or "", []).append(e)
    folder = site / "data" / "index"
    files, keep = [], set()
    for state in sorted(by_state):
        base = re.sub(r"[^a-z0-9]+", "-", state.lower()).strip("-") or "other"
        name, i = base + ".json", 2
        while name in keep:
            name, i = f"{base}-{i}.json", i + 1
        keep.add(name)
        _write_if_changed(folder / name, {"state": state or None, "cases": by_state[state]})
        files.append({"state": state or None, "file": "data/index/" + name, "cases": len(by_state[state])})
    return files, keep


def summary(entries):
    """What the home and method pages show without loading every case. `entries` in site order."""
    counts, villages = {}, set()
    for e in entries:
        key = (e["scheme"], e["kind"])
        counts[key] = counts.get(key, 0) + 1
        villages.update(norm(e.get("block")) + "|" + norm(v) for v in e.get("villages") or [])
    top = entries[:TOP_CASES]
    # Cases tied with the last one go too, so the site picks the same ones with its own sort.
    tie = lambda e: (e.get("severity") or 0, e.get("record_count") or 0)
    while top and len(top) < len(entries) and tie(entries[len(top)]) == tie(top[-1]):
        top = entries[:len(top) + 1]
    return {"total": len(entries), "villages_with_case": len(villages),
            "kinds": [{"scheme": s, "kind": k, "n": n} for (s, k), n in sorted(counts.items())], "top": top}


def read_site_data(site):
    """The site data as one object: summary.json plus "cases", every whole case from data/cases/,
    in site order."""
    site = Path(site)
    data = json.loads((site / "data" / "summary.json").read_text(encoding="utf-8"))
    cases = []
    for entry in data["index"]:
        for e in json.loads((site / entry["file"]).read_text(encoding="utf-8"))["cases"]:
            cases.append(json.loads((site / "data" / "cases" / case_file_name(e["id"])).read_text(encoding="utf-8")))
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    return dict(data, cases=cases)


def _private(c):
    """Take the keys starting with "_" off a case: (evidence photo, other photos of the same picture)."""
    src, alts = c.pop("_photo_src", None), c.pop("_photo_alts", None) or []
    for k in [k for k in c if k.startswith("_")]:
        del c[k]
    return src, alts


def _linked_photos(site):
    """sha256 of every photo with a link in site/data/links/, written by python run.py drive-links."""
    out = set()
    for f in sorted((Path(site) / "data" / "links").glob("*.json")):
        try:
            out.update(k.lower() for k in json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError, AttributeError):
            pass
    return out


def write(root, cases, schemes, coverage, checks_run=None):
    site = Path(root) / "site"
    linked = _linked_photos(site)
    for c in cases:
        src, alts = _private(c)
        # A picture saved as several files: show the file that has a Drive link, even once its original is
        # deleted; else the first file still on disk.
        sources = [s for s in [src] + alts if s]
        on_drive = [s for s in sources if str(s.get("sha256") or "").lower() in linked]
        c["photo"] = (next((rel for rel in (copy_photo(root, site, s, gone_ok=True) for s in on_drive) if rel), None)
                      or next((rel for rel in (copy_photo(root, site, s) for s in sources) if rel), None))
    write_site_data(site, cases, schemes, coverage, checks_run)


def write_site_data(site, cases, schemes, coverage, checks_run=None, generated_at=None):
    """Case files, then the index, then summary.json, which names the index files, then areas.json."""
    site = Path(site)
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    write_case_files(site, cases)
    entries = [index_entry(c) for c in cases]
    files, keep = write_index(site, entries)
    write_json(site / "data" / "summary.json", dict({
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks_run": checks_run or [], "schemes": schemes, "coverage": coverage, "index": files}, **summary(entries)))
    write_json(site / "data" / "areas.json", areas(entries))
    _remove_others(site / "data" / "index", keep)
    try:
        (site / "data" / "cases.json").unlink()  # the single file used before summary.json and data/index/
    except OSError:
        pass


def write_preview(preview_dir, scheme, cases, scheme_info, coverage, checks_run, root=None):
    """Cases of a scheme that is not public yet, for a person to review. Never under site/: the
    photo of each case is the original's path under data/, not a public copy."""
    for c in cases:
        src, alts = _private(c)
        c["photo"] = src.get("file") if src and src.get("sha256") else None
        if root is not None and alts and not (c["photo"] and (Path(root) / c["photo"]).is_file()):
            c["photo"] = next((s["file"] for s in alts if s.get("file") and (Path(root) / s["file"]).is_file()),
                              c["photo"])
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    path = Path(preview_dir) / f"cases.{scheme}.json"
    write_json(path, {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preview": True, "checks_run": checks_run, "schemes": [scheme_info], "coverage": coverage,
        "cases": cases, "areas": areas(cases)})
    return path
