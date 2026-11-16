import argparse
import sys

from . import output, photo_reuse, same_day, same_gps, upload_burst, vision
from .common import Skipped, load_config, load_coverage, load_records


def _safe(name, check, log, *args):
    """A check that trips over unexpected data is reported and left out; the others still run."""
    try:
        return check(*args)
    except Exception as e:
        log(f"{name}: check failed and was left out of this run: {e!r}")
        return []


def run(root=".", scheme=None, use_vision=False, vision_limit=None, log=print):
    cfg = load_config()
    skipped = Skipped()
    data = load_records(root, scheme, skipped)
    if not data:
        log(f"no records found in {root}/data/records" + (f" for scheme {scheme}" if scheme else ""))
    cases, schemes, coverage = [], [], []
    s = vision.settings()
    checks_run = ["photo_reuse", "same_gps", "same_day", "upload_burst"] +         (["vision_mismatch"] if use_vision and s["base"] and s["model"] else [])
    for name, records in data.items():
        meta = cfg["schemes"].get(name, {})
        schemes.append({"id": name, "name": meta.get("name", name),
                        "portal_url": meta.get("portal_url") or next((r.get("source_url") for r in records if r.get("source_url")), None)})
        coverage += _safe("coverage", lambda: output.coverage_rows(name, records, load_coverage(root, name, skipped)), log)
        reuse = _safe("photo_reuse", photo_reuse.find, log, name, records, cfg)
        explained = {str(r["record_id"]) for c in reuse for r in c["records"]}
        found = reuse + _safe("same_gps", same_gps.find, log, name, records, cfg, explained) \
            + _safe("same_day", same_day.find, log, name, records, cfg, explained) \
            + _safe("upload_burst", upload_burst.find, log, name, records, cfg)
        if use_vision:
            found += _safe("vision", vision.find, log, name, records, cfg, root, vision_limit, log)
        log(f"{name}: {len(records)} records, {len(found)} cases")
        cases += found
    log(f"skipped {skipped.count} malformed record(s) or file(s)" + ("; first ones:" if skipped.count else ""))
    for e in skipped.examples:
        log(f"  {e}")
    output.write(root, cases, schemes, coverage, checks_run)
    return cases


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m checker", description="Find likely fake evidence photos in collected records.")
    ap.add_argument("--scheme", help="only check this scheme id, for example jsjb")
    ap.add_argument("--vision", action="store_true", help="also ask a vision model (needs VISION_API_BASE and VISION_MODEL)")
    ap.add_argument("--vision-limit", type=int, help="most new vision calls this run (cached answers are free)")
    ap.add_argument("--root", default=".", help="project folder holding data/ and site/ (default: current folder)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        run(a.root, a.scheme, a.vision, a.vision_limit)
    except OSError as e:
        print(f"could not write the results in {a.root}/site/data: {e}", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
