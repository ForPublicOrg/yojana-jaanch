"""python run.py collect --scheme <id> --refetch-unhashed: download again the photos whose files the
checker deleted before the phash step saw them, so the near-duplicate match can compare them.

Before the phash step existed, the checker deleted every photo file that was in no case
(checker/prune.py) and kept only its sha256. Those photos cannot be compared by picture. This mode
reads the saved records (read only), and for every photo whose sha256 has no line in
<data>/state/phash.jsonl and no file under <data>/photos/, downloads it again from a URL it was
saved under. When the bytes hash to the stored sha256 the file is stored at
<data>/photos/<sha256[:2]>/<sha256>.<ext>, with the extension the records give the file (some
say .jpeg), so it is back where they point. Nothing else is
written: no record, no coverage line, no photo index entry, no run summary. A download that hashes
to something else is logged and counted and not stored (the portal now serves another file at that
URL); the next URL saved for the same sha256, if any, is tried. 404 is counted as missing.

It can run while the normal collector or queue of the same scheme runs. It does not take
data/state/collector.<id>.lock (or collector.lock for jsjb), because it writes none of the files
that lock protects; photo files are written through store_shared, which the README describes as
safe for two processes storing the same photo. It takes its own lock,
data/state/refetch.<id>.lock, so two refetch runs of one scheme do not overlap.

Both processes then ask the same host for photos, each with its own throttle and ban guard, so
the load adds up. This mode therefore defaults to --photo-workers 3: at most 3 photo requests start
a second, fewer when the portal has trouble. Every request goes through the same Client,
PhotoThrottle, ban guard and PhotoPool as a normal run, and it stops for the same reasons: a ban
guard stop, a 401/403 refusal, --max-minutes, --max-records (photos here), disk trouble or Ctrl+C.

Run the phash step first: without phash.jsonl the checker deletes unhashed files again, so this
mode refuses to start."""
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict, deque

from .http import FetchError
from .store import DataInUse, DataLock, StoreError, image_type, now_iso, store_shared

log = logging.getLogger("collector")

PHASH_FILE = "phash.jsonl"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^([0-9a-f]{64})\.[A-Za-z0-9]{1,5}$")
# URLs tried for one sha256 in one run, when a URL is missing or now serves another file.
MAX_TRIES = 3
_NONE = object()


class RefetchLock(DataLock):
    """data/state/refetch.<scheme>.lock: one refetch run per scheme and data folder."""

    def __init__(self, data_dir, scheme):
        self.files = []
        self.path = os.path.join(data_dir, "state", f"refetch.{scheme}.lock")
        try:
            self.files.append(self._take(data_dir, self.path))
        except DataInUse:
            raise DataInUse(f"another --refetch-unhashed run of {scheme} holds {self.path}") from None


def hashed_shas(path):
    """sha256 values with a line in phash.jsonl that the checker reads (checker/nearmatch.py
    parse_line, so both agree on what counts as hashed), or None when the file does not exist."""
    from checker.nearmatch import parse_line
    if not os.path.exists(path):
        return None
    out = set()
    with open(path, "rb") as f:
        for raw in f:
            e = parse_line(raw) if raw.strip() else None
            if e is not None:
                out.add(e.sha)
    return out


def stored_shas(photos_dir):
    out = set()
    if not os.path.isdir(photos_dir):
        return out
    for sub in os.listdir(photos_dir):
        full = os.path.join(photos_dir, sub)
        if os.path.isdir(full):
            for name in os.listdir(full):
                m = NAME_RE.match(name)
                if m:
                    out.add(m.group(1))
    return out


def read_records(path):
    """{record_id: record} from the records file, read only; a later line for a record wins and
    unreadable lines are skipped."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "rb") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if isinstance(r, dict) and r.get("record_id") not in (None, ""):
                out[str(r["record_id"])] = r
    return out


def record_ext(sha, file):
    """The extension of a record's file path when it is <...>photos/<xx>/<sha256>.<ext>, else None."""
    if not isinstance(file, str):
        return None
    parts = file.replace("\\", "/").split("/")
    m = NAME_RE.match(parts[-1])
    if len(parts) >= 3 and parts[-3] == "photos" and parts[-2] == sha[:2] and m and m.group(1) == sha:
        return "." + parts[-1].split(".", 1)[1]
    return None


def targets(records, hashed, stored):
    """[(sha256, [urls], ext)] for photos with a sha256 and a URL, no phash line and no file; the
    photo on the most records first. ext is the extension the records give the file (the first one
    seen), so the file comes back at the path the records and the photo index name; None if no
    record names one."""
    urls, works, exts = defaultdict(list), defaultdict(set), {}
    for rid, r in records.items():
        for p in r.get("photos") or []:
            if not isinstance(p, dict):
                continue
            sha, url = p.get("sha256"), p.get("url")
            if not (isinstance(sha, str) and SHA_RE.match(sha) and isinstance(url, str) and url):
                continue
            if sha in hashed or sha in stored:
                continue
            if url not in urls[sha]:
                urls[sha].append(url)
            works[sha].add(rid)
            if sha not in exts:
                ext = record_ext(sha, p.get("file"))
                if ext:
                    exts[sha] = ext
    return [(sha, urls[sha], exts.get(sha)) for sha in sorted(urls, key=lambda s: (-len(works[s]), s))]


def refetch_unhashed(adapter, data_dir, scheme, max_photos=None, max_minutes=None, photo_workers=3,
                     photo_max=None, max_photo_bytes=None):
    """Download and store the photos described above. Returns stats with "stop" as (kind, message) or None."""
    from .__main__ import (DEFAULT_PHOTO_MAX, MAX_PHOTO_BYTES, PhotoPool, StopRun, _denied, _guard_stop,
                           _photo_report, photo_settings)
    max_photo_bytes = max_photo_bytes or MAX_PHOTO_BYTES
    started = time.monotonic()
    stats = {"started_at": now_iso(), "mode": "refetch", "to_fetch": 0, "saved": 0, "mismatch": 0,
             "missing": 0, "failed": 0, "left": 0, "stopped": None, "stop": None}
    hashed = hashed_shas(os.path.join(data_dir, "state", PHASH_FILE))
    if hashed is None:
        stats["stop"] = ("filter", f"{os.path.join(data_dir, 'state', PHASH_FILE)} does not exist. Run "
                                   "python run.py phash first; without it the checker deletes these files again")
        return stats
    todo = targets(read_records(os.path.join(data_dir, "records", f"{scheme}.jsonl")), hashed,
                   stored_shas(os.path.join(data_dir, "photos")))
    stats["to_fetch"] = len(todo)
    log.info("refetch %s: %d photos have a sha256 but neither a file nor a phash line", scheme, len(todo))
    capped = None
    if max_photos is not None and len(todo) > max(0, max_photos):
        todo = todo[:max(0, max_photos)]
        capped = f"--max-records {max_photos} reached"
    threads, rate, ceiling = photo_settings(photo_workers, photo_max or DEFAULT_PHOTO_MAX)
    if hasattr(adapter.client, "set_photo_rate"):
        adapter.client.set_photo_rate(rate, ceiling)
    pool = PhotoPool(adapter.client, threads, max_photo_bytes)
    log.info("refetch %s: %d workers, starting at %d a second, at most %d", scheme, threads, rate, ceiling)
    queue = deque((sha, list(urls), 0, ext) for sha, urls, ext in todo)
    flight = {}

    def done_with(url, result):
        sha, urls, tries, want_ext = flight.pop(url)
        nxt = None
        if isinstance(result, BaseException):
            if isinstance(result, FetchError) and result.denied:
                raise StopRun("access", _denied(adapter) or str(result))
            if isinstance(result, FetchError) and result.status in (404, 410):
                stats["missing"] += 1
                log.warning("refetch: photo missing on the portal: %s", url)
                nxt = "next"
            else:
                stats["failed"] += 1
                log.warning("refetch: photo not downloaded, tried again next run: %s: %s", url, result)
        else:
            content = result[0] if isinstance(result, tuple) else result
            ext = image_type(content)
            real = hashlib.sha256(content).hexdigest()
            if not ext or len(content) > max_photo_bytes:
                stats["failed"] += 1
                log.warning("refetch: not an image or too large (%d bytes), not stored: %s", len(content), url)
            elif real != sha:
                stats["mismatch"] += 1
                log.warning("refetch: %s now serves a different file (sha256 %s, saved as %s); not stored",
                            url, real, sha)
                nxt = "next"
            else:
                store_shared(os.path.join(data_dir, "photos", sha[:2], sha + (want_ext or ext)), content)
                stats["saved"] += 1
        if nxt and urls and tries + 1 < MAX_TRIES:
            queue.appendleft((sha, urls, tries + 1, want_ext))

    settle = True
    tried = set()
    try:
        while (queue and not stats["stopped"]) or flight:
            stop = _guard_stop(adapter)
            if stop:
                raise StopRun(*stop)
            if max_minutes is not None and time.monotonic() - started > max_minutes * 60 and not stats["stopped"]:
                # Nothing new is started; the downloads under way are still checked and stored.
                stats["stopped"] = f"--max-minutes {max_minutes} reached"
            while queue and not stats["stopped"] and len(flight) < 2 * threads:
                sha, urls, tries, want_ext = queue.popleft()
                url = urls.pop(0)
                if url in tried or url in flight:
                    if urls:
                        queue.appendleft((sha, urls, tries, want_ext))
                    continue
                tried.add(url)
                flight[url] = (sha, urls, tries, want_ext)
                pool.submit(url)
            for url in list(flight):
                result = pool.pop(url, _NONE)
                if result is not _NONE:
                    done_with(url, result)
            if flight:
                pool.wait()
    except KeyboardInterrupt:
        settle = False
        stats["stopped"] = "interrupted"
        stats["stop"] = ("interrupted", "stopped by Ctrl+C or a stop signal")
    except StopRun as e:
        stats["stop"] = (e.kind, str(e))
    except StoreError as e:
        settle = False
        stats["stop"] = ("disk", str(e))
    finally:
        pool.close(cancel=not settle or bool(stats["stop"]))
        if not stats["stop"] and _guard_stop(adapter):
            stats["stop"] = _guard_stop(adapter)
        _photo_report(adapter)
    stats["left"] = len(queue) + len(flight) + (stats["to_fetch"] - len(todo))
    stats["stopped"] = stats["stopped"] or (capped if not stats["stop"] else None)
    return stats


def summary_lines(stats):
    out = [f"Photos with a sha256 but no file and no phash line: {stats['to_fetch']}",
           f"Stored again: {stats['saved']}; now a different file on the portal (not stored): {stats['mismatch']}; "
           f"missing on the portal: {stats['missing']}; failed, tried again next run: {stats['failed']}; "
           f"not tried this run: {stats['left']}"]
    if stats["stop"]:
        out.append(f"STOPPED EARLY ({stats['stop'][0]}): {stats['stop'][1]}.")
    elif stats["stopped"]:
        out.append(f"Stopped: {stats['stopped']}")
    out.append("Records, coverage and the photo index were not changed. Run python run.py phash next.")
    return out


def run_from_args(a):
    """The --refetch-unhashed mode of python -m collector. Returns the exit code."""
    from .__main__ import EXIT, LogFile, make_adapter
    try:
        lock = RefetchLock(a.data_dir, a.scheme)
    except StoreError as e:
        log.error("%s", e)
        return EXIT["busy" if isinstance(e, DataInUse) else "disk"]
    logfile = LogFile(os.path.join(a.data_dir, "logs", f"{a.scheme}.refetch.log") if a.log is None else a.log)
    try:
        adapter = make_adapter(a.scheme, None, a.delay)
        stats = refetch_unhashed(adapter, a.data_dir, a.scheme, a.max_records, a.max_minutes,
                                 a.photo_workers, a.photo_max, int(a.max_photo_mb * 2 ** 20))
        if stats["stop"]:
            log.error("%s", stats["stop"][1])
        text = "\n".join(["", "Refetch summary"] + summary_lines(stats))
        print(text, flush=True)
        logfile.write(text)
        if stats["stop"]:
            return EXIT[stats["stop"][0]]
        return 5 if stats["failed"] else 0
    finally:
        logfile.close()
        lock.release()
