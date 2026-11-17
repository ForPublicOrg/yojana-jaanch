import json
import os
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


def copy_photo(root, site, src):
    if not src or not src.get("file") or not src.get("sha256"):
        return None
    path = Path(root) / src["file"]
    if not path.is_file():
        return None
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


def write(root, cases, schemes, coverage, checks_run=None):
    site = Path(root) / "site"
    for c in cases:
        c["photo"] = copy_photo(root, site, c.pop("_photo_src", None))
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    write_json(site / "data" / "cases.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks_run": checks_run or [], "schemes": schemes, "coverage": coverage, "cases": cases})
    write_json(site / "data" / "areas.json", areas(cases))


def write_preview(preview_dir, scheme, cases, scheme_info, coverage, checks_run):
    """Cases of a scheme that is not public yet, for a person to review. Never under site/: the
    photo of each case is the original's path under data/, not a public copy."""
    for c in cases:
        src = c.pop("_photo_src", None)
        c["photo"] = src.get("file") if src and src.get("sha256") else None
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    path = Path(preview_dir) / f"cases.{scheme}.json"
    write_json(path, {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preview": True, "checks_run": checks_run, "schemes": [scheme_info], "coverage": coverage,
        "cases": cases, "areas": areas(cases)})
    return path
