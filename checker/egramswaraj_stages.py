"""eGramSwaraj stage photos out of order, read from the GP page metadata alone (no photo needed).

Each eGramSwaraj record keeps its photo rows under photo_source: stage label and upload time.
A work is flagged when, by upload time:

- a later stage was uploaded before an earlier one (for example "Completion of work" before
  "Plinth level"), or
- a plinth or roof photo was uploaded after the completion photo, so the work was recorded as
  complete before its earlier stages had a photo.

One case per gram panchayat with at least MIN_RECORDS flagged works. Only the stage labels in
STAGE_RANK are ranked; any other label is left out of the comparison.

Innocent reading, stated on every case: stage photos are sometimes taken during the work and
uploaded later from the phone, all at once and in any order. The upload order then says nothing
about the building order. The date stamped in the photo is the thing to check.

Loading: checker/__main__.py loads extra checks only from checker/extra/<name>.py. This module
sits in checker/ and needs a one-line checker/extra/egramswaraj_stages.py
(`from checker.egramswaraj_stages import KIND, NEEDS, find`) before the checker runs it."""
from collections import defaultdict

from .common import make_case, parse_time, plural, village_key

KIND = "stage_order"
# Fields a record must have for this check; photo_source is a list on every eGramSwaraj record,
# filled from the page, so the check also runs on records saved by a list-only run.
NEEDS = ("group_key", "photo_source")
MIN_RECORDS = 2
# Stage labels seen on GP pages in November 2026, in building order. Matched without case.
STAGE_RANK = {"plinth level": 1, "roof laid": 2, "completion of work": 3}
COMPLETION = 3


def stage_rows(r):
    """[(upload time, rank, label)] of the ranked stage photos of one record, by upload time."""
    rows = r.get("photo_source")
    if not isinstance(rows, list):
        rows = r.get("photos") or []
    out = []
    for p in rows:
        if not isinstance(p, dict) or not isinstance(p.get("stage"), str):
            continue
        rank = STAGE_RANK.get(" ".join(p["stage"].split()).casefold())
        t = parse_time(p.get("uploaded_at"))
        if rank is None or t is None:
            continue
        out.append((t, rank, p["stage"]))
    return sorted(out)


def flags(r):
    """(out_of_order, after_completion) for one record."""
    rows = stage_rows(r)
    if len(rows) < 2:
        return False, False
    out_of_order = any(b[1] < a[1] for a, b in zip(rows, rows[1:]))
    done = [t for t, rank, _ in rows if rank == COMPLETION]
    after = bool(done) and any(t > done[0] and rank < COMPLETION for t, rank, _ in rows)
    return out_of_order, after


def find(scheme, records, cfg):
    groups = defaultdict(list)
    for r in records:
        if not r.get("group_key"):
            continue
        ooo, after = flags(r)
        if ooo or after:
            groups[(village_key(r), r["group_key"])].append((r, after))
    cases = []
    for (vkey, gp), items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(items) < MIN_RECORDS:
            continue
        recs = [r for r, _ in items]
        n_after = sum(1 for _, a in items if a)
        place = recs[0].get("village") or f"GP {gp}"
        reasons = [f"{plural(len(recs), 'work')} in {place} have stage photos uploaded out of building order "
                   "(a later stage uploaded before an earlier one)."]
        if n_after:
            reasons.append(f"On {plural(n_after, 'work')} a plinth or roof photo was uploaded after the "
                           "completion photo.")
        reasons.append("Innocent reading: photos taken during the work may have been uploaded later, all at "
                       "once and in any order. Check the date stamped in each photo.")
        cases.append(make_case(scheme, KIND, [vkey, gp],
                               f"{plural(len(recs), 'work')} with stage photos out of order in {place}",
                               recs, reasons, cfg, min_records=MIN_RECORDS))
    return [c for c in cases if c is not None]
