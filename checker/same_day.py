from collections import defaultdict

from .common import counted, keep, make_case, office, village_key


def find(scheme, records, cfg, explained_ids):
    conf = cfg["same_day"]
    ignore = {t.lower() for t in conf.get("ignore_structure_types", [])}
    groups = defaultdict(list)
    for r in records:
        st = r.get("structure_type")
        if str(r["record_id"]) in explained_ids or not st or st.lower() in ignore:
            continue
        if r.get("start_date") and r.get("start_date") == r.get("end_date"):
            groups[(village_key(r), office(r))].append(r)

    cases = []
    for (vkey, by_office), recs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        n = len(recs)
        if n < conf["min_records"]:
            continue
        village = recs[0].get("village") or "one village"
        by = f" by {by_office}" if by_office else ""
        dates = sorted({r["start_date"] for r in recs})
        reasons = [f"{n} works in {village} entered{by} each started and ended within a single day.",
                   f"The works are recorded as: {counted(r.get('structure_type') for r in recs)}."
                   " Check whether each of these can really be built in one day.",
                   f"Dates used: {', '.join(dates)}."]
        cases.append(make_case(scheme, "same_day", [vkey, by_office],
                               f"{n} works in {village} each started and finished within one day", recs, reasons, cfg))
    return keep(cases)
