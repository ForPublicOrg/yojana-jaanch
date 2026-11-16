from collections import defaultdict

from .common import counted, keep, make_case, plural, village_key


def find(scheme, records, cfg, explained_ids):
    """Records already in a photo_reuse case are skipped; that case explains their shared point."""
    groups = defaultdict(list)
    for r in records:
        if str(r["record_id"]) in explained_ids or r.get("lat") is None or r.get("lon") is None:
            continue
        groups[(village_key(r), r["lat"], r["lon"])].append(r)

    cases = []
    for (vkey, lat, lon), recs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        n = len(recs)
        if n < cfg["same_gps"]["min_records"]:
            continue
        village = recs[0].get("village") or "one village"
        photos = {p.get("sha256") for r in recs for p in (r.get("photos") or []) if p.get("sha256")}
        reasons = [f"{n} works in {village} report exactly the same GPS point ({lat}, {lon}).",
                   "Separate works should be at separate spots, so their coordinates should differ."]
        types = counted(r.get("structure_type") for r in recs)
        if types:
            reasons.append(f"The works are recorded as: {types}.")
        if photos:
            reasons.append(f"They carry {plural(len(photos), 'different photo')}, so this is not a reused-photo case.")
        cases.append(make_case(scheme, "same_gps", [vkey, lat, lon], f"{n} works at one GPS point in {village}",
                               recs, reasons, cfg))
    return keep(cases)
