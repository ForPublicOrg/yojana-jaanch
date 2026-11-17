import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")
MIN_CASE_RECORDS = 2
# Upload times from photo filenames carry no zone and are Indian Standard Time.
PORTAL_TZ = timezone(timedelta(hours=5, minutes=30))


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


TEXT_FIELDS = ("scheme", "state", "state_code", "district", "district_code", "area_type", "block", "block_code",
               "village", "village_code", "location_text", "nature_of_work", "structure_type", "status",
               "start_date", "end_date", "community_participation", "created_by", "source_url",
               "first_seen", "last_seen",
               # Optional fields for schemes other than JSJB (docs/SCHEMES.md section 3).
               "parent_id", "stage", "amount_kind", "group_key", "group_kind", "beneficiary_ref")
PHOTO_FIELDS = ("url", "sha256", "file", "uploaded_at", "taken_at", "stage", "label")
PHOTOS_STATES = ("fetched", "not_fetched", "none_on_portal")


class Skipped:
    """Counts records the checker could not use, with a few examples for the log."""

    def __init__(self):
        self.count = 0
        self.examples = []

    def add(self, where, why):
        self.count += 1
        if len(self.examples) < 10:
            self.examples.append(f"{where}: {why}")


def clean_record(r):
    """Return a record safe for every check, or raise ValueError saying what is wrong."""
    if not isinstance(r, dict):
        raise ValueError("not a JSON object")
    rid = r.get("record_id")
    if isinstance(rid, bool) or not isinstance(rid, (str, int)) or str(rid).strip() == "":
        raise ValueError("no record_id")
    r = dict(r, record_id=str(rid))
    for k in TEXT_FIELDS:
        v = r.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            r[k] = str(v)
        elif v is not None and not isinstance(v, str):
            raise ValueError(f"{k} has unexpected type {type(v).__name__}")
    for k in ("lat", "lon", "amount"):
        v = r.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
            raise ValueError(f"{k} is not a number")
    if r.get("photos_state") is not None and r["photos_state"] not in PHOTOS_STATES:
        raise ValueError(f"photos_state is not one of {', '.join(PHOTOS_STATES)}")
    photos = r.get("photos")
    if photos is None:
        photos = []
    if not isinstance(photos, list):
        raise ValueError("photos is not a list")
    for p in photos:
        if not isinstance(p, dict):
            raise ValueError("a photo entry is not a JSON object")
        for k in PHOTO_FIELDS:
            if p.get(k) is not None and not isinstance(p[k], str):
                raise ValueError(f"photo {k} has unexpected type {type(p[k]).__name__}")
        for k in ("lat", "lon"):
            v = p.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                raise ValueError(f"photo {k} is not a number")
    r["photos"] = photos
    return r


def office(r):
    """The "office" a record is grouped by: group_key when the scheme sets it (device id, GP,
    monitor), else created_by (JSJB)."""
    return r.get("group_key") or r.get("created_by")


def photos_fetched(r):
    """False for a record saved by a list-only run whose photos have not been fetched yet."""
    return r.get("photos_state") != "not_fetched"


def data_path(root, data_dir=None):
    return Path(data_dir) if data_dir else Path(root) / "data"


def load_records(root, scheme=None, skipped=None, data_dir=None):
    """Return {scheme: [records]}, one record per record_id (latest last_seen wins).

    Lines that are not valid JSON or not usable records are left out and counted in skipped."""
    skipped = skipped if skipped is not None else Skipped()
    out = {}
    rec_dir = data_path(root, data_dir) / "records"
    for path in sorted(rec_dir.glob("*.jsonl")):
        name = path.stem
        if scheme and name != scheme:
            continue
        by_id = {}
        try:
            f = open(path, "rb")
        except OSError as e:
            skipped.add(path.name, f"could not open ({e})")
            continue
        with f:
            for n, raw in enumerate(f, 1):
                if not raw.strip():
                    continue
                try:
                    r = clean_record(json.loads(raw.decode("utf-8")))
                except (ValueError, UnicodeDecodeError, RecursionError) as e:
                    skipped.add(f"{path.name} line {n}", e)
                    continue
                rid = r["record_id"]
                old = by_id.get(rid)
                if old is None or (r.get("last_seen") or "") >= (old.get("last_seen") or ""):
                    by_id[rid] = r
        out[name] = [by_id[k] for k in sorted(by_id, key=sort_key)]
    return out


def load_coverage(root, scheme, skipped=None, data_dir=None):
    path = data_path(root, data_dir) / "records" / f"{scheme}.coverage.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        if skipped is not None:
            skipped.add(path.name, f"unreadable, coverage left out ({e})")
        return []
    rows = []
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict) and all(c.get(k) is None or isinstance(c.get(k), (str, int))
                                       for k in ("state", "district", "block", "village", "village_code")):
            rows.append({k: (str(v) if isinstance(v, int) else v) for k, v in c.items()})
        elif skipped is not None:
            skipped.add(path.name, "a coverage entry has unexpected fields")
    return rows


def sort_key(rid):
    s = str(rid)
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


def village_key(r):
    return (r.get("state"), r.get("district"), r.get("block"), r.get("village_code") or r.get("village"))


def parse_time(s):
    """Naive portal-local time, so times with and without a zone can be compared."""
    if not s or not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.astimezone(PORTAL_TZ).replace(tzinfo=None) if t.tzinfo else t


def stable_id(scheme, kind, *parts):
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]
    return f"{scheme}-{kind}-{digest}"


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def span_text(seconds):
    if seconds < 60:
        return plural(int(seconds), "second")
    return plural(math.ceil(seconds / 60), "minute")


def counted(values):
    c = Counter(v for v in values if v)
    return ", ".join(f"{k} ({n})" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def top(values):
    c = Counter(v for v in values if v)
    if not c:
        return None, 0
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0], len(c)


def make_case(scheme, kind, key_parts, headline, records, reasons, cfg, photo=None,
              extra_severity=0, needs_review=False, min_records=MIN_CASE_RECORDS):
    """Build a case dict, or None when it rests on fewer than min_records records."""
    records = sorted({str(r["record_id"]): r for r in records}.values(), key=lambda r: sort_key(r["record_id"]))
    if len(records) < min_records:
        return None
    place = {}
    spread = []
    for field, label in (("state", "states"), ("district", "districts"), ("block", "blocks")):
        value, n = top(r.get(field) for r in records)
        place[field] = value
        if n > 1 and not spread:
            names = sorted({r.get(field) for r in records if r.get(field)})
            spread.append(f"These works are spread over {n} {label}: {', '.join(names)}.")
    weight = cfg["severity_weight"].get(kind, 1)
    return {
        "id": stable_id(scheme, kind, *key_parts),
        "scheme": scheme,
        "kind": kind,
        "headline": headline,
        "state": place["state"],
        "district": place["district"],
        "block": place["block"],
        "villages": sorted({r.get("village") for r in records if r.get("village")}),
        "record_count": len(records),
        "reasons": reasons + spread,
        "photo": None,
        "offices": sorted({office(r) for r in records if office(r)}),
        "records": [case_record(r) for r in records],
        "source_url": next((r.get("source_url") for r in records if r.get("source_url")), None),
        "severity": weight * len(records) + extra_severity,
        "needs_human_review": needs_review,
        "_photo_src": photo,
    }


CASE_RECORD_FIELDS = ("record_id", "village", "structure_type", "created_by", "start_date", "end_date", "lat", "lon")
# Shown only when a scheme fills them, so JSJB cases keep the shape they always had.
CASE_RECORD_OPTIONAL = ("parent_id", "stage", "amount", "amount_kind", "group_key", "group_kind")
# The portal's own filter values, so the site can tell a reader what to choose on the portal to
# reach the record (JSJB has no link to a single record).
CASE_RECORD_PORTAL_PATH = ("state", "district", "area_type", "block")


def case_record(r):
    out = {k: r.get(k) for k in CASE_RECORD_FIELDS}
    out.update({k: r[k] for k in CASE_RECORD_OPTIONAL if r.get(k) is not None})
    out.update({k: r[k] for k in CASE_RECORD_PORTAL_PATH if r.get(k)})
    return out


def keep(cases):
    return [c for c in cases if c is not None]
