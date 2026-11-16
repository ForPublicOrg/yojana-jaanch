"""Builds a small DATA CONTRACT 1 fixture. Run directly to write one: python tests/checker/fixture.py <dir>"""
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

URL = "https://jsjb.mowr.gov.in/PublicDashboardJsjb.aspx"
SEEN = "2026-11-01T10:00:00"
PUNJAB = {"state": "Punjab", "state_code": "3", "district": "Sangrur", "district_code": "43", "area_type": "Rural"}


def photo(root, content, uploaded_at):
    data = content.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    rel = f"data/photos/{sha[:2]}/{sha}.jpg"
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"url": f"https://example.invalid/{sha[:8]}.jpg", "sha256": sha, "bytes": len(data), "file": rel,
            "uploaded_at": uploaded_at}


def rec(rid, village, vcode, block, bcode, office, st, lat, lon, start, end, photos, place=PUNJAB):
    return place | {"scheme": "jsjb", "record_id": str(rid), "block": block, "block_code": bcode,
                    "village": village, "village_code": vcode, "location_text": None, "nature_of_work": None,
                    "structure_type": st, "lat": lat, "lon": lon, "status": "Completed", "start_date": start,
                    "end_date": end, "community_participation": "Others", "created_by": office,
                    "source_url": URL, "photos": photos, "first_seen": SEEN, "last_seen": SEEN}


def build(root):
    root = Path(root)
    t0 = datetime(2026, 5, 20, 11, 0, 0)
    out = []
    for i in range(23):
        village, vcode, office = ("Duggan", "39781", "BDPO SANGRUR") if i < 20 else ("Kheri", "39783", "BDPO BHAWANIGARH")
        up = (t0 + timedelta(seconds=30 * i)).isoformat()
        out.append(rec(1001 + i, village, vcode, "Sangrur", "347", office, "Contour trench", 30.251337, 75.735085,
                       "2026-04-29", "2026-04-29", [photo(root, "photo-reuse-A", up)]))
    for i in range(6):
        out.append(rec(2001 + i, "Balian", "39782", "Sangrur", "347", "BDPO SANGRUR", "Farm pond", 30.3, 75.8,
                       f"2026-04-0{i + 1}", f"2026-04-1{i}", [photo(root, f"gps-{i}", f"2026-05-0{i + 1}T09:00:00")]))
    for i in range(5):
        tag = "mismatch" if i < 2 else "ok"
        out.append(rec(3001 + i, "Kheri", "39783", "Sangrur", "347", "BDPO SANGRUR", "Check dam", 30.31 + i / 100, 75.81,
                       f"2026-03-1{i}", f"2026-03-1{i}", [photo(root, f"sameday-{i}-{tag}", f"2026-04-1{i}T10:00:00")]))
    t1 = datetime(2026, 5, 20, 14, 0, 0)
    for i in range(10):
        out.append(rec(4001 + i, "Ghabdan", "39790", "Bhawanigarh", "348", "BDPO BHAWANIGARH", "Recharge pit",
                       30.2 + i / 100, 75.9, "2026-02-01", "2026-02-20",
                       [photo(root, f"burst-{i}", (t1 + timedelta(seconds=30 * i)).isoformat())]))
    dup = photo(root, "single-record-two-entries", "2026-05-01T08:00:00")
    out.append(rec(5001, "Duggan", "39781", "Sangrur", "347", "BDPO SANGRUR", "Check dam", 30.4, 75.7,
                   "2026-01-01", "2026-01-05", [dup, dict(dup)]))
    for i in range(4):
        out.append(rec(6001 + i, "Duggan", "39781", "Sangrur", "347", "BDPO SANGRUR", "Farm pond", 30.5, 75.5,
                       "2026-01-01", "2026-01-09", [photo(root, f"four-gps-{i}", f"2026-06-0{i + 1}T08:00:00")]))
    out.append(rec(7001, "Duggan", "39781", "Sangrur", "347", "BDPO SANGRUR", None, None, None, None, None, []))
    haryana = {"state": "Haryana", "state_code": "6", "district": "Karnal", "district_code": "70", "area_type": "Rural"}
    for i in range(2):
        out.append(rec(8001 + i, "Gharaunda", "50001", "Gharaunda", "500", "BDPO GHARAUNDA", "Soak pit", 29.5 + i, 76.9,
                       "2026-03-01", "2026-03-04", [photo(root, "photo-reuse-B", f"2026-03-0{i + 5}T12:00:00")], haryana))

    rec_dir = root / "data" / "records"
    rec_dir.mkdir(parents=True, exist_ok=True)
    with open(rec_dir / "jsjb.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    villages = {}
    for r in out:
        villages.setdefault(r["village_code"], r)
    cov = [{"state": r["state"], "district": r["district"], "area_type": "Rural", "block": r["block"],
            "village": r["village"], "village_code": code,
            "records": sum(1 for x in out if x["village_code"] == code), "collected_at": SEEN}
           for code, r in villages.items()]
    cov.append({"state": "Punjab", "district": "Sangrur", "area_type": "Rural", "block": "Sangrur",
                "village": "Empty", "village_code": "39799", "records": 0, "collected_at": SEEN})
    with open(rec_dir / "jsjb.coverage.json", "w", encoding="utf-8") as f:
        json.dump(cov, f, indent=1)
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "fixture_out")
