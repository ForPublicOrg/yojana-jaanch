"""One photo attached to 2 or more works.

Files are grouped by sha256 (the same file). When near is given (phash.jsonl exists) and
photo_reuse.near_max_distance is above 0, files that hold one picture saved differently are grouped
too (checker/nearmatch.py). With phash.jsonl, a group whose main file is a placeholder (a portal's
broken-image icon or a plain graphic, nearmatch.Entry.placeholder, or listed in
data/state/placeholders.txt) is still a case, as the owner asked, with a reason saying the file is a
graphic and not a photograph, and a "placeholder" field the site uses to show it. A group of one file gives the same case, id and text as before near
matching existed. A group of several files gives one case whose id comes from the file attached to
the most records (ties: the smallest sha256), so an existing case keeps its id when its file is the
biggest member."""
from collections import defaultdict

from . import nearmatch
from .common import counted, keep, make_case, office, parse_time, plural, span_text, stable_id


def find(scheme, records, cfg, near=None):
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

    pcfg = cfg["photo_reuse"]
    pictures, accepted, limits = [[sha] for sha in sorted(groups)], {}, None
    if near is not None:
        limits, why = nearmatch.settings(pcfg)
        if limits is None:
            near.report[scheme] = {"near_matching": False, "why_off": why}
        else:
            weights = {sha: len(recs) for sha, recs in groups.items()}
            pictures, accepted, counts = near.group(weights, limits)
            near.report[scheme] = dict(counts, near_matching=True, max_distance=limits[0], max_mad=limits[1],
                                       max_aspect_gap=limits[2], files_merged=0, pictures=0,
                                       largest_distance=0, largest_mad=0.0)
        near.report[scheme].update(placeholder_cases=0, placeholder_records=0)

    cases = []
    for members in pictures:
        by_id = {}
        for sha in members:
            by_id.update(groups[sha])
        recs = list(by_id.values())
        n = len(recs)
        # A broken-image icon or plain graphic on many works: the portal shows no photo of a site for
        # them at all. It stays a case and the case says what the file is. Placeholders are never
        # merged with other files.
        why_placeholder = near.placeholder(members[0]) if near is not None and n >= 2 else None
        placeholder = None
        if why_placeholder:
            near.report[scheme]["placeholder_cases"] += 1
            near.report[scheme]["placeholder_records"] += n
            near.left_out.append({"scheme": scheme, "sha": members[0], "file": photo_of[members[0]].get("file"),
                                  "records": n, "why": why_placeholder})
            e = near.entries.get(members[0])
            size = tuple(e.size) if e is not None and e.size and None not in e.size else None
            placeholder = {"why": why_placeholder, "width": size[0] if size else None,
                           "height": size[1] if size else None}
        pairs = [(a, b) for i, a in enumerate(members) for b in members[i + 1:]]
        info = [accepted[tuple(sorted(p))] for p in pairs]
        if len(members) > 1:
            rep = near.report[scheme]
            rep["files_merged"] += len(members)
            rep["pictures"] += 1
            rep["largest_distance"] = max([rep["largest_distance"]] + [d for d, _, _ in info])
            rep["largest_mad"] = max([rep["largest_mad"]] + [m for _, m, _ in info])
        case = None
        if n >= 2:
            case = _case(scheme, members, recs, groups, photo_of, uploads, info, limits, cfg, placeholder)
            cases.append(case)
        for (a, b), (d, mad, gap) in zip(pairs, info):
            near.pairs.append({"scheme": scheme, "a": a, "b": b, "distance": d, "mad": mad, "gap": gap,
                               "case_id": case["id"] if case else None,
                               "a_file": photo_of[a].get("file"), "b_file": photo_of[b].get("file"),
                               "a_records": len(groups[a]), "b_records": len(groups[b])})
    return keep(cases)


def _where(recs):
    # Some schemes (PMGSY) record a block but no village: count the smallest place every record names.
    for field, word in (("village", "village"), ("block", "block"), ("district", "district")):
        if all(r.get(field) for r in recs):
            return f" in {plural(len({r[field] for r in recs}), word)}"
    return ""


def _case(scheme, members, recs, groups, photo_of, uploads, info, limits, cfg, placeholder=None):
    sha = members[0]
    n = len(recs)
    one_file = len(members) == 1
    where = _where(recs)
    if one_file:
        reasons = [f"The same photo file (fingerprint {sha[:12]}) is attached to {n} different works"
                   f"{where}. A photo of one site cannot be evidence for {n} separate works."]
    else:
        k = len(members)
        reasons = [
            f"The same picture was saved as {k} different files and attached to {n} different works"
            f"{where}. A photo of one site cannot be evidence for {n} separate works.",
            f"The {k} files hold one picture saved more than once (for example re-compressed, resized, cut by a"
            f" few pixels or with another date or GPS stamp printed on it), so their bytes differ. They were matched by comparing the pictures: every two of these files"
            f" differ in at most {max(d for d, _, _ in info)} of 256 bits of a picture fingerprint (the limit is"
            f" {limits[0]}), and their small greyscale copies differ by at most {max(m for _, m, _ in info):.3f}"
            f" (the limit is {limits[1]:g}).",
            "The files (first 12 characters of each fingerprint): "
            + ", ".join(f"{s[:12]} on {plural(len(groups[s]), 'work')}" for s in members) + "."]
    if placeholder:
        size = (f"a {placeholder['width']} x {placeholder['height']} pixel image"
                if placeholder.get("width") else "a small image")
        reasons.append(f"This file is not a photograph of a work: it is {size} such as a broken-image icon or a"
                       f" plain graphic. The portal shows this same image as the evidence photo for all {n} works,"
                       f" so none of them has a photo of the site in this slot.")
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
    times = sorted(t for s in members for t in uploads[s])
    if len(times) >= 2:
        span = (times[-1] - times[0]).total_seconds()
        if span <= cfg["photo_reuse"]["upload_minutes"] * 60:
            reasons.append(f"The {'photo' if one_file else 'picture'} was uploaded {len(times)} times within"
                           f" {span_text(span)} ({times[0].isoformat(sep=' ')} to {times[-1].isoformat(sep=' ')}).")
            extra += 1
    offices = sorted({office(r) for r in recs if office(r)})
    if len(offices) > 1:
        reasons.append(f"The entries were made by {len(offices)} different offices: {', '.join(offices)}.")
        extra += 1
    headline = f"One photo, {n} different works" if one_file else f"One picture, {n} different works"
    case = make_case(scheme, "photo_reuse", [sha], headline, recs, reasons, cfg, photo=photo_of[sha],
                     extra_severity=extra)
    if case is not None and placeholder:
        case["placeholder"] = {"width": placeholder.get("width"), "height": placeholder.get("height")}
    if case is not None and not one_file:
        # Every file of the picture, heaviest first; the site shows how many files there were.
        case["photo_files"] = [{"sha256": s, "records": len(groups[s])} for s in members]
        # Ids the other files would have as one-file cases, so old links can find this case.
        case["other_ids"] = [stable_id(scheme, "photo_reuse", s) for s in members[1:]]
        case["_photo_alts"] = [photo_of[s] for s in members[1:]]
        case["_member_shas"] = list(members)
    return case
