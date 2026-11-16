from collections import defaultdict

from .common import keep, make_case, parse_time, plural, span_text


def find(scheme, records, cfg):
    conf = cfg["upload_burst"]
    window = conf["window_minutes"] * 60
    need = conf["min_records"]
    by_id = {str(r["record_id"]): r for r in records}
    events = defaultdict(set)
    for r in records:
        for p in r.get("photos") or []:
            t = parse_time(p.get("uploaded_at"))
            if t and p.get("sha256") and r.get("created_by"):
                events[r["created_by"]].add((t, str(r["record_id"]), p["sha256"]))

    cases = []
    for office in sorted(events):
        ev = sorted(events[office])
        i = 0
        while i < len(ev):
            j = i
            while j + 1 < len(ev) and (ev[j + 1][0] - ev[i][0]).total_seconds() <= window:
                j += 1
            chunk = ev[i:j + 1]
            rids = {e[1] for e in chunk}
            shas = {e[2] for e in chunk}
            # Distinct photos are required too, so one photo uploaded many times stays a photo_reuse case.
            if len(rids) >= need and len(shas) >= need:
                start, end = chunk[0][0], chunk[-1][0]
                secs = (end - start).total_seconds()
                recs = [by_id[x] for x in rids]
                villages = {r.get("village") for r in recs if r.get("village")}
                reasons = [f"{len(shas)} different photos for {len(rids)} different works were uploaded by {office}"
                           f" within {span_text(secs)}, from {start.isoformat(sep=' ')} to {end.isoformat(sep=' ')}.",
                           f"That is about one work every {round(secs / len(rids))} seconds on average.",
                           f"The works are in {plural(len(villages), 'village')}: {', '.join(sorted(villages))}."]
                cases.append(make_case(scheme, "upload_burst", [office, start.isoformat()],
                                       f"{len(shas)} photos uploaded in {span_text(secs)} by {office}",
                                       recs, reasons, cfg))
                i = j + 1
            else:
                i += 1
    return keep(cases)
