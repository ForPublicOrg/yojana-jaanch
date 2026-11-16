import json
import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def coverage_rows(scheme, records, coverage):
    blocks = defaultdict(lambda: {"villages": set(), "records": set(), "photos": set(), "names": None})

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
        block(c)["villages"].add(c.get("village_code") or c.get("village"))
    return [{"scheme": scheme, "state": v["names"][0], "district": v["names"][1], "block": v["names"][2],
             "villages_checked": len(v["villages"] - {None}), "records_checked": len(v["records"]),
             "photos_checked": len(v["photos"])}
            for _, v in sorted(blocks.items())]


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
            shutil.copyfile(path, dest.with_suffix(".part"))
            os.replace(dest.with_suffix(".part"), dest)
        except OSError:
            return None
    return rel


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write(root, cases, schemes, coverage, checks_run=None):
    site = Path(root) / "site"
    for c in cases:
        c["photo"] = copy_photo(root, site, c.pop("_photo_src", None))
    cases.sort(key=lambda c: (-c["severity"], -c["record_count"], c["id"]))
    write_json(site / "data" / "cases.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks_run": checks_run or [], "schemes": schemes, "coverage": coverage, "cases": cases})
    write_json(site / "data" / "areas.json", areas(cases))
