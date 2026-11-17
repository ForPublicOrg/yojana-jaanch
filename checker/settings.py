"""Per-scheme checker settings, read from checker/schemes/<id>.json.

A scheme's settings are config.json's schemes.<id> (where jsjb has always kept its name and
portal link) with checker/schemes/<id>.json laid over it, so adding a scheme needs a new file
and no edit to a shared one. Keys (all optional):

- name, portal_url: shown on the site.
- public: true sends the scheme's cases to site/data. Anything else makes it a preview scheme:
  its cases go to data/preview/cases.<id>.json only.
- checks: {"same_day": false, ...} turns a built-in check off for this scheme. A check whose
  fields no record of the scheme has is off anyway (see NEEDS in checker/__main__.py).
- extra_checks: names of modules in checker/extra/ to run for this scheme, each with
  KIND, NEEDS (field names) and find(scheme, records, cfg).
- photo_reuse, same_gps, same_day, upload_burst, vision, severity_weight: values that replace
  config.json's for this scheme only, key by key.

A file that cannot be read is reported and treated as empty; the scheme is then a preview scheme
unless config.json says public."""
import copy
import json
import re
from pathlib import Path

SCHEMES_DIR = Path(__file__).with_name("schemes")
OVERRIDABLE = ("photo_reuse", "same_gps", "same_day", "upload_burst", "vision", "severity_weight")
SCHEME_ID = re.compile(r"^[a-z0-9_-]+$")


def scheme_file(scheme_id, folder=None):
    return Path(folder or SCHEMES_DIR) / f"{scheme_id}.json"


def load(scheme_id, cfg, folder=None, log=print):
    """Merged settings dict for one scheme. folder defaults to checker/schemes."""
    base = dict((cfg.get("schemes") or {}).get(scheme_id) or {})
    own = {}
    path = scheme_file(scheme_id, folder)
    if SCHEME_ID.match(scheme_id) and path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                own = json.load(f)
            if not isinstance(own, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError, RecursionError) as e:
            log(f"{path}: unreadable, ignored ({e})")
            own = {}
    out = dict(base)
    out.update(own)
    out["public"] = out.get("public") is True
    checks = out.get("checks")
    out["checks"] = {k: v for k, v in checks.items() if isinstance(v, bool)} if isinstance(checks, dict) else {}
    extra = out.get("extra_checks")
    out["extra_checks"] = [x for x in extra if isinstance(x, str) and SCHEME_ID.match(x)] \
        if isinstance(extra, list) else []
    return out


def scheme_config(cfg, settings):
    """config.json with this scheme's overrides laid over it, key by key."""
    merged = copy.deepcopy(cfg)
    for section in OVERRIDABLE:
        value = settings.get(section)
        if isinstance(value, dict):
            merged.setdefault(section, {})
            merged[section].update(value)
    return merged
