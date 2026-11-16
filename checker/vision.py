"""Optional check: ask an OpenAI-compatible vision model whether a photo shows the claimed work."""
import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import keep, make_case, plural, village_key

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
PROMPT = ("This photo was uploaded as evidence of a water conservation work of type: \"{t}\". "
          "Does the photo plausibly show this kind of structure? Reply with JSON only, like "
          "{{\"answer\": \"yes\" or \"no\" or \"unsure\", \"seen\": \"at most 15 words on what the photo shows\"}}.")


class RateLimited(Exception):
    pass


def settings():
    return {k: os.environ.get(f"VISION_API_{k.upper()}", "") for k in ("base", "key")} | {
        "model": os.environ.get("VISION_MODEL", "")}


def load_cache(path):
    """A damaged cache costs only repeated model calls, so start empty instead of stopping."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def save_cache(path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def parse_answer(text):
    m = re.search(r"\{.*\}", text if isinstance(text, str) else "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    answer = str(data.get("answer", "")).strip().lower()
    return {"answer": answer if answer in ("yes", "no", "unsure") else "unsure",
            "seen": str(data.get("seen", "")).strip()[:200]}


def ask(s, image_path, structure_type, vcfg):
    mime = MIME.get(image_path.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = {"model": s["model"], "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT.format(t=structure_type)},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    headers = {"Content-Type": "application/json"}
    if s["key"]:
        headers["Authorization"] = f"Bearer {s['key']}"
    url = s["base"].rstrip("/") + "/chat/completions"
    for attempt in range(vcfg["max_retries"] + 1):
        req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=vcfg["timeout_seconds"]) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return parse_answer(data["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            wait = e.headers.get("Retry-After")
            e.close()
            delay = float(wait) if wait and wait.replace(".", "", 1).isdigit() else vcfg["retry_base_seconds"] * 2 ** attempt
            time.sleep(min(delay, 120))
    raise RateLimited(f"gave up after {vcfg['max_retries'] + 1} attempts")


def find(scheme, records, cfg, root, limit=None, log=print):
    s = settings()
    if not s["base"] or not s["model"]:
        log("vision: VISION_API_BASE and VISION_MODEL must be set; skipping vision check")
        return []
    vcfg = cfg["vision"]
    limit = vcfg["limit"] if limit is None else limit
    cache_path = Path(root) / "data" / "state" / "vision_cache.json"
    cache = load_cache(cache_path)

    photo_recs = defaultdict(list)
    photo_file = {}
    for r in records:
        for p in r.get("photos") or []:
            if p.get("sha256") and p.get("file") and r.get("structure_type"):
                photo_recs[p["sha256"]].append(r)
                photo_file.setdefault(p["sha256"], p)

    calls = failures = 0
    verdicts = {}
    for sha in sorted(photo_recs):
        claimed = sorted(Counter(r["structure_type"] for r in photo_recs[sha]).items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        key = f"{s['model']}|{sha}"
        if isinstance(cache.get(key), dict):
            verdicts[sha] = cache[key]
            continue
        if calls >= limit or failures >= vcfg["max_consecutive_failures"]:
            continue
        path = Path(root) / photo_file[sha]["file"]
        if not path.exists():
            continue
        calls += 1
        try:
            v = ask(s, path, claimed, vcfg)
        except (RateLimited, urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError,
                IndexError, TypeError, AttributeError, RecursionError) as e:
            failures += 1
            log(f"vision: {sha[:12]} failed: {e}")
            continue
        failures = 0
        v |= {"structure_type": claimed, "model": s["model"],
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        cache[key] = verdicts[sha] = v
        try:
            save_cache(cache_path, cache)
        except OSError as e:
            log(f"vision: answer not cached ({e})")
    log(f"vision: {calls} new calls, {len(verdicts)} photos with answers")

    groups = defaultdict(dict)
    for sha, v in verdicts.items():
        if v.get("answer") != "no":
            continue
        for r in photo_recs[sha]:
            if r["structure_type"] == v.get("structure_type"):
                groups[(village_key(r), r.get("created_by"))][str(r["record_id"])] = (r, sha, v)

    cases = []
    for (vkey, office), hits in sorted(groups.items(), key=lambda kv: str(kv[0])):
        items = list(hits.values())
        shas = sorted({sha for _, sha, _ in items})
        village = items[0][0].get("village") or "one village"
        reasons = [f"An automatic image model ({s['model']}) judged that {plural(len(shas), 'photo')} for {plural(len(items), 'work')}"
                   f" in {village} do not show the claimed type of work. A person must look at the photos before relying on this."]
        for sha in shas[:5]:
            v = verdicts[sha]
            if v.get("seen"):
                reasons.append(f"Photo {sha[:12]} is claimed to show \"{v['structure_type']}\"; the model described it as: {v['seen']}.")
        cases.append(make_case(scheme, "vision_mismatch", [vkey, office, s["model"]],
                               f"{plural(len(items), 'work')} whose photo may not show the claimed work",
                               [r for r, _, _ in items], reasons, cfg, photo=photo_file[shas[0]], needs_review=True,
                               min_records=1))
    return keep(cases)
