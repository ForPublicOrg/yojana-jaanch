import argparse
import importlib
import sys
from pathlib import Path

from . import output, photo_reuse, prune, same_day, same_gps, upload_burst, vision
from . import settings as scheme_settings
from .common import Skipped, data_path, load_config, load_coverage, load_records, office, photos_fetched
from .lock import Busy, CheckerLock

CHECKS = ("photo_reuse", "same_gps", "same_day", "upload_burst")
# Fields a check needs. A scheme none of whose records has one of them does not run that check,
# and the site is told "not run for this scheme" instead of "found nothing". "photo." fields are
# looked for in photos of records whose photos were fetched; "office" is group_key or created_by.
NEEDS = {
    "photo_reuse": ("photo.sha256",),
    "same_gps": ("lat", "lon"),
    "same_day": ("start_date", "end_date", "structure_type"),
    "upload_burst": ("office", "photo.sha256", "photo.uploaded_at"),
    "vision_mismatch": ("structure_type", "photo.sha256", "photo.file"),
}


def _safe(name, check, log, *args):
    """A check that trips over unexpected data is reported and left out; the others still run."""
    try:
        return check(*args)
    except Exception as e:
        log(f"{name}: check failed and was left out of this run: {e!r}")
        return []


def missing_fields(needs, records, photo_records):
    """The fields in needs that no record has."""
    out = []
    for f in needs:
        if f.startswith("photo."):
            key = f[6:]
            ok = any(p.get(key) for r in photo_records for p in r.get("photos") or [])
        elif f == "office":
            ok = any(office(r) for r in records)
        else:
            ok = any(r.get(f) not in (None, "") for r in records)
        if not ok:
            out.append(f)
    return out


def plan_checks(name, settings, records, photo_records, use_vision, extra):
    """{check: None (runs) or the reason it is off} for one scheme."""
    plan = {}
    wanted = list(CHECKS) + (["vision_mismatch"] if use_vision else []) + [m.KIND for m in extra]
    needs = dict(NEEDS, **{m.KIND: tuple(getattr(m, "NEEDS", ())) for m in extra})
    for kind in wanted:
        if settings["checks"].get(kind) is False:
            plan[kind] = f"turned off in checker/schemes/{name}.json"
            continue
        lacking = missing_fields(needs.get(kind, ()), records, photo_records)
        plan[kind] = f"no record has {', '.join(lacking)}" if lacking else None
    return plan


def load_extra(names, log):
    mods = []
    for n in names:
        try:
            m = importlib.import_module(f"{__package__}.extra.{n}")
            if not isinstance(getattr(m, "KIND", None), str) or not callable(getattr(m, "find", None)):
                raise ValueError("needs KIND and find(scheme, records, cfg)")
            mods.append(m)
        except Exception as e:
            log(f"extra check {n}: left out ({e!r})")
    return mods


def check_scheme(name, records, settings, cfg, root, data_dir, use_vision, vision_limit, log, vision_ready):
    """Cases and the checks that ran (and those that did not, with the reason) for one scheme."""
    scfg = scheme_settings.scheme_config(cfg, settings)
    # Records saved by a list-only run have no photos yet; photo checks leave them out.
    photo_records = [r for r in records if photos_fetched(r)]
    extra = load_extra(settings["extra_checks"], log)
    plan = plan_checks(name, settings, records, photo_records, use_vision, extra)
    on = {k for k, why in plan.items() if why is None}
    found = []
    reuse = _safe("photo_reuse", photo_reuse.find, log, name, photo_records, scfg) if "photo_reuse" in on else []
    explained = {str(r["record_id"]) for c in reuse for r in c["records"]}
    found += reuse
    if "same_gps" in on:
        found += _safe("same_gps", same_gps.find, log, name, records, scfg, explained)
    if "same_day" in on:
        found += _safe("same_day", same_day.find, log, name, records, scfg, explained)
    if "upload_burst" in on:
        found += _safe("upload_burst", upload_burst.find, log, name, photo_records, scfg)
    if "vision_mismatch" in on:
        found += _safe("vision", vision.find, log, name, photo_records, scfg, root, vision_limit, log, data_dir)
    for m in extra:
        if m.KIND in on:
            recs = photo_records if any(f.startswith("photo.") for f in getattr(m, "NEEDS", ())) else records
            found += _safe(m.KIND, m.find, log, name, recs, scfg)
    ran = [k for k in plan if k in on and (k != "vision_mismatch" or vision_ready)]
    off = {k: why for k, why in plan.items() if why is not None}
    return found, ran, off


def run(root=".", scheme=None, use_vision=False, vision_limit=None, log=print, data_dir=None, lock_wait=1800,
        prune_photos=True):
    """Check every scheme found in <data>/records. Public schemes (checker/schemes/<id>.json says
    "public": true) go to site/data; the others to <data>/preview/cases.<id>.json. With a data
    folder other than <root>/data, every scheme goes to <data>/preview/ and site/ is not touched."""
    data = data_path(root, data_dir)
    main_data = data_dir is None or Path(data_dir).resolve() == (Path(root) / "data").resolve()
    with CheckerLock(data / "state" / "checker.lock", wait_seconds=lock_wait):
        return _run(root, scheme, use_vision, vision_limit, log, data_dir, data, main_data, prune_photos)


def _run(root, scheme, use_vision, vision_limit, log, data_dir, data, main_data, prune_photos=True):
    cfg = load_config()
    skipped = Skipped()
    found_data = load_records(root, scheme, skipped, data_dir)
    if not found_data:
        log(f"no records found in {data / 'records'}" + (f" for scheme {scheme}" if scheme else ""))
    s = vision.settings()
    vision_ready = bool(use_vision and s["base"] and s["model"])
    public = {"cases": [], "schemes": [], "coverage": [], "checks_run": []}
    previews = []
    all_cases = []
    for name, records in found_data.items():
        settings = scheme_settings.load(name, cfg, log=log)
        info = {"id": name, "name": settings.get("name", name),
                "portal_url": settings.get("portal_url") or next((r.get("source_url") for r in records
                                                                  if r.get("source_url")), None)}
        coverage = _safe("coverage", lambda: output.coverage_rows(
            name, records, load_coverage(root, name, skipped, data_dir)), log)
        found, ran, off = check_scheme(name, records, settings, cfg, root, data_dir, use_vision, vision_limit,
                                       log, vision_ready)
        info["checks_run"] = ran
        if off:
            info["checks_off"] = off
        is_public = settings["public"] and main_data
        log(f"{name}: {len(records)} records, {len(found)} cases" + ("" if is_public else " (preview, not public)"))
        for k, why in off.items():
            log(f"  {k} not run for {name}: {why}")
        all_cases += found
        if is_public:
            public["cases"] += found
            public["schemes"].append(info)
            public["coverage"] += coverage
            public["checks_run"] += [k for k in ran if k not in public["checks_run"]]
        else:
            previews.append((name, found, info, coverage, ran))
    log(f"skipped {skipped.count} malformed record(s) or file(s)" + ("; first ones:" if skipped.count else ""))
    for e in skipped.examples:
        log(f"  {e}")
    # Photo files are kept only for each case's evidence photo; the rest keep their sha256 (checker/prune.py).
    # Only after a check of every scheme, so no scheme's case photos are missed.
    if prune_photos and scheme is None and found_data:
        keep = prune.keep_hashes(found_data, all_cases)
        n, freed, kept = prune.prune(data / "photos", keep)
        log(f"photo files: kept {kept}, deleted {n} that are in no case ({freed / 1e6:.0f} MB); their hashes stay")
    # One scheme asked for by name that is a preview scheme leaves site/data alone.
    preview_only = scheme is not None and not public["schemes"] and scheme in {p[0] for p in previews}
    if main_data and not preview_only:
        checks_run = public["checks_run"] if public["schemes"] else \
            list(CHECKS) + (["vision_mismatch"] if vision_ready else [])
        output.write(root, public["cases"], public["schemes"], public["coverage"], checks_run)
    for name, found, info, coverage, ran in previews:
        path = output.write_preview(data / "preview", name, found, info, coverage, ran)
        log(f"{name}: preview cases written to {path}")
    return all_cases


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m checker", description="Find likely fake evidence photos in collected records.")
    ap.add_argument("--scheme", help="only check this scheme id, for example jsjb")
    ap.add_argument("--vision", action="store_true", help="also ask a vision model (needs VISION_API_BASE and VISION_MODEL)")
    ap.add_argument("--vision-limit", type=int, help="most new vision calls this run (cached answers are free)")
    ap.add_argument("--root", default=".", help="project folder holding data/ and site/ (default: current folder)")
    ap.add_argument("--data-dir", help="data folder to read (default: <root>/data). Any other folder, such as "
                                       "data/_trial/<id>, gets its cases in <data-dir>/preview/ and site/ is not touched")
    ap.add_argument("--keep-all-photos", action="store_true",
                    help="do not delete photo files that are in no case (by default only their sha256 is kept)")
    ap.add_argument("--lock-wait", type=float, default=30, help="minutes to wait for another checker to finish (default 30)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        run(a.root, a.scheme, a.vision, a.vision_limit, data_dir=a.data_dir, lock_wait=a.lock_wait * 60,
            prune_photos=not a.keep_all_photos)
    except Busy as e:
        print(f"{e}; not run", file=sys.stderr)
        return 7
    except OSError as e:
        print(f"could not write the results in {a.root}/site/data: {e}", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
