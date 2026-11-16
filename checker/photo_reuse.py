from collections import defaultdict

from .common import counted, keep, make_case, parse_time, plural, span_text


def find(scheme, records, cfg):
    groups = defaultdict(dict)
    photo_of = {}
    uploads = defaultdict(list)
    for r in records:
        for p in r.get("photos") or []:
            sha = p.get("sha256")
            if not sha:
                continue
            groups[sha][str(r["record_id"])] = r
            photo_of.setdefault(sha, p)
            t = parse_time(p.get("uploaded_at"))
            if t:
                uploads[sha].append(t)

    cases = []
    for sha in sorted(groups):
        recs = list(groups[sha].values())
        n = len(recs)
        if n < 2:
            continue
        villages = {r.get("village") for r in recs if r.get("village")}
        reasons = [f"The same photo file (fingerprint {sha[:12]}) is attached to {n} different works"
                   f" in {plural(len(villages), 'village')}. A photo of one site cannot be evidence for {n} separate works."]
        extra = 0
        types = counted(r.get("structure_type") for r in recs)
        if types:
            reasons.append(f"The works are recorded as: {types}.")
        points = {(r.get("lat"), r.get("lon")) for r in recs}
        if len(points) == 1 and None not in next(iter(points)):
            lat, lon = next(iter(points))
            reasons.append(f"All {n} works report the same GPS point ({lat}, {lon}).")
            extra += 1
        if all(r.get("start_date") and r.get("start_date") == r.get("end_date") for r in recs):
            dates = sorted({r["start_date"] for r in recs})
            if len(dates) == 1:
                reasons.append(f"All {n} works started and ended on the same day ({dates[0]}).")
            else:
                reasons.append(f"Each of the {n} works started and ended on a single day."
                               f" The works are spread over {len(dates)} dates: {', '.join(dates)}.")
            extra += 1
        times = sorted(uploads[sha])
        if len(times) >= 2:
            span = (times[-1] - times[0]).total_seconds()
            if span <= cfg["photo_reuse"]["upload_minutes"] * 60:
                reasons.append(f"The photo was uploaded {len(times)} times within {span_text(span)}"
                               f" ({times[0].isoformat(sep=' ')} to {times[-1].isoformat(sep=' ')}).")
                extra += 1
        offices = sorted({r.get("created_by") for r in recs if r.get("created_by")})
        if len(offices) > 1:
            reasons.append(f"The entries were made by {len(offices)} different offices: {', '.join(offices)}.")
            extra += 1
        cases.append(make_case(scheme, "photo_reuse", [sha], f"One photo, {n} different works",
                               recs, reasons, cfg, photo=photo_of[sha], extra_severity=extra))
    return keep(cases)
