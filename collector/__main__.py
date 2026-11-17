"""python -m collector <scheme> [--state ...] [--district ...] [--list-only | --photos] ..."""
import argparse
import logging
import math
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .base import AccessDenied, FilterNotFound, LayoutChanged, NothingListed, PortalChanged, SkippedUnit
from .http import Client, FetchError
from .schemes import BROKEN, SCHEMES
from .store import DataInUse, DataLock, Store, StoreError, image_type, now_iso

log = logging.getLogger("collector")

MAX_PHOTO_BYTES = 25 * 1024 * 1024
EXIT = {"layout": 2, "filter": 3, "network": 4, "disk": 6, "busy": 7, "access": 8, "error": 1, "interrupted": 130}
# Optional fields a photo entry from list_photos may carry; they are saved with the photo.
PHOTO_EXTRAS = ("lat", "lon", "taken_at", "stage", "label")
MISSING = {"sha256": None, "bytes": None, "file": None}


class StopRun(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _download(client, url, max_bytes):
    if hasattr(client, "fetch_image"):
        return client.fetch_image(url, max_bytes)
    return client.fetch(url), None


def download_many(client, urls, workers, max_bytes):
    """Download photos with up to `workers` at a time; each worker keeps the client delay between its
    own requests. Returns {url: (content, ctype) or the exception}. The bytes are checked by the caller."""
    clients = client.photo_clients(workers) if hasattr(client, "photo_clients") else [client] * workers
    todo, out, lock, stop = list(urls), {}, threading.Lock(), threading.Event()

    def work(c):
        while not stop.is_set():
            with lock:
                if not todo:
                    return
                url = todo.pop(0)
            try:
                out[url] = _download(c, url, max_bytes)
            except BaseException as e:
                out[url] = e

    threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in clients[:max(1, min(workers, len(urls)))]]
    for t in threads:
        t.start()
    try:
        # Short joins so Ctrl+C reaches the main thread on Windows too.
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.2)
    except BaseException:
        stop.set()
        raise
    finally:
        if clients[0] is not client and hasattr(client, "photos_done"):
            client.photos_done(clients)
    for url, v in out.items():
        if isinstance(v, KeyboardInterrupt):
            raise v
    return out


def fetch_photo(client, store, url, max_bytes=MAX_PHOTO_BYTES, fetched=None):
    """Returns (index entry, outcome) where outcome is "ok", "missing" (404, not retried) or "failed".
    fetched is what download_many returned for this url, when it was downloaded ahead."""
    missing = {"sha256": None, "bytes": None, "file": None}
    try:
        if fetched is None:
            content, ctype = _download(client, url, max_bytes)
        elif isinstance(fetched, BaseException):
            raise fetched
        else:
            content, ctype = fetched
    except FetchError as e:
        if e.status in (404, 410):
            log.warning("photo missing on portal, not retried: %s", url)
            store.photo_index[url] = dict(missing, error=f"HTTP {e.status}")
            return missing, "missing"
        log.warning("photo not saved, will retry next run: %s", e)
        return missing, "failed"
    except Exception as e:
        log.warning("photo not saved, will retry next run: %s: %r", url, e)
        return missing, "failed"
    # Error pages, empty bodies and oversized files are never hashed or stored.
    ext = image_type(content)
    if len(content) > max_bytes:
        why = f"larger than {max_bytes} bytes"
    elif not ext:
        why = f"not an image ({len(content)} bytes, content type {ctype or 'not given'})"
    else:
        return store.save_photo(url, content, ext), "ok"
    log.warning("photo not saved, will retry next run: %s: %s", url, why)
    return missing, "failed"


def _name(area):
    if getattr(area, "levels", None):
        return " > ".join(lv["name"] or lv["code"] for lv in area.levels)
    return f"{area.state} > {area.district} > {area.block} > {area.village}"


def _denied(adapter):
    """The client stopped asking a host that answered a page with 401 or 403."""
    denied = getattr(adapter.client, "denied", None)
    if isinstance(denied, dict) and denied:
        host, status = next(iter(denied.items()))
        return f"{host} answered HTTP {status}; the portal refuses access. Stopped; nothing was tried to get past it"
    return None


def _photo_extras(p):
    out = {}
    for k in PHOTO_EXTRAS:
        v = p.get(k)
        if v is None:
            continue
        if k in ("lat", "lon"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                continue
        elif not isinstance(v, str):
            continue
        out[k] = v
    return out


def save_record_photos(adapter, store, rec, listed, failed_urls, stats, ahead, max_photo_bytes):
    """Turn list_photos entries into saved photo entries. Returns (photos, downloaded, failed).
    A photo entry that is neither a URL nor bytes is logged and left out; one bad photo never
    costs the record."""
    photos, downloaded, failed = [], 0, 0
    for p in listed:
        if not isinstance(p, dict) or not (isinstance(p.get("url"), str) and p["url"]
                                           or isinstance(p.get("data"), (bytes, bytearray))):
            log.warning("record %s: photo entry left out, it has neither a url nor data bytes: %.200r",
                        rec.get("record_id"), p)
            continue
        extras = _photo_extras(p)
        if p.get("url"):
            url = p["url"]
            entry = store.known_photo(url)
            if entry is None:
                if url in failed_urls:
                    entry, outcome = dict(MISSING), failed_urls[url]
                else:
                    entry, outcome = fetch_photo(adapter.client, store, url, max_photo_bytes, ahead.pop(url, None))
                    if outcome == "ok":
                        downloaded += 1
                    else:
                        stats["photos_" + outcome] += 1
                        failed_urls[url] = outcome
                failed += outcome == "failed"
            photos.append(dict({"url": url, "sha256": entry["sha256"], "bytes": entry["bytes"],
                                "file": entry["file"], "uploaded_at": p.get("uploaded_at")}, **extras))
            continue
        # Bytes sent inline by the portal (for example base64 in the page).
        content = bytes(p["data"])
        ext = image_type(content)
        if ext and len(content) <= max_photo_bytes:
            entry = store.save_photo(None, content, ext)
        else:
            log.warning("record %s: inline photo not saved: %s", rec.get("record_id"),
                        "too large" if ext else f"not an image ({len(content)} bytes)")
            stats["photos_missing"] += 1
            entry = dict(MISSING)
        photos.append(dict({"url": None, "sha256": entry["sha256"], "bytes": entry["bytes"],
                            "file": entry["file"], "uploaded_at": p.get("uploaded_at")}, **extras))
    return photos, downloaded, failed


def _strip_private(rec):
    # Keys starting with "_" are the adapter's own working data and are never saved.
    for k in [k for k in rec if isinstance(k, str) and k.startswith("_")]:
        del rec[k]


def collect(adapter, store, filters, max_villages=None, max_minutes=None, since_days=None, flush_every=10,
            max_layout_failures=10, max_network_failures=10, outage_pause=300, max_outage_pauses=12,
            max_photo_bytes=MAX_PHOTO_BYTES, photo_workers=1, sleep=time.sleep, list_only=False):
    """Collect every matching village. Never raises for one bad village, list or photo.

    The run stops early only for: max_layout_failures layout problems in a row, the dropdowns
    missing from the start page, the network down through max_outage_pauses pauses, local disk
    trouble, a login, CAPTCHA or 401/403 page (AccessDenied), or Ctrl+C. stats["stop"] then holds
    (kind, message).

    list_only=True saves the records without calling list_photos: each gets photos_state
    "not_fetched" (unless the adapter set "none_on_portal") and a later photos_pass fetches them.
    A record already saved with its photos keeps them when its photo_source has not changed."""
    started = time.monotonic()
    stats = {"started_at": now_iso(), "villages": 0, "not_due": 0, "records": 0, "new_records": 0,
             "photos_downloaded": 0, "photos_failed": 0, "photos_missing": 0,
             "skipped_villages": [], "skipped_units": [], "stopped": None, "stop": None,
             "store_problems": list(store.problems), "nothing_listed": False}
    failed_urls = {}
    streak = {"layout": 0, "network": 0}
    pauses = 0
    pending = 0

    def failure(kind, entry, bucket):
        nonlocal pauses
        stats[bucket].append(entry)
        why = _denied(adapter)
        if why:
            raise StopRun("access", why)
        streak[kind if kind in streak else "layout"] += 1
        other = "network" if kind != "network" else "layout"
        streak[other] = 0
        if streak["layout"] >= max_layout_failures:
            raise StopRun("layout", f"{streak['layout']} villages or lists in a row did not match the expected "
                                    "page layout; the portal layout has probably changed everywhere")
        if streak["network"] >= max_network_failures:
            if pauses >= max_outage_pauses:
                raise StopRun("network", f"the portal could not be reached for {pauses} pauses of "
                                         f"{outage_pause}s; network or portal is down")
            pauses += 1
            log.warning("%d network failures in a row; pausing %ds before going on (pause %d of %d)",
                        streak["network"], outage_pause, pauses, max_outage_pauses)
            streak["network"] = 0
            sleep(outage_pause)

    def do_village(area):
        records = adapter.list_records(area)
        seen_at = now_iso()
        downloaded = failed = 0
        ahead = {}
        listed = {}
        if not list_only:
            for i, rec in enumerate(records):
                if rec.get("photos_state") == "none_on_portal":
                    listed[i] = []
                    continue
                try:
                    listed[i] = adapter.list_photos(rec)
                except (AccessDenied, PortalChanged, StoreError):
                    raise
                except Exception as e:
                    if isinstance(e, FetchError) and e.denied:
                        raise
                    # The record is saved without photos; the village stays due and a --photos
                    # pass can fetch them.
                    log.warning("%s: photos of record %s not listed: %s", _name(area), rec.get("record_id"), e)
                    listed[i] = None
                    failed += 1
                    stats["photos_failed"] += 1
        if photo_workers > 1:
            new = list(dict.fromkeys(p["url"] for ps in listed.values() for p in ps or []
                                     if isinstance(p, dict) and isinstance(p.get("url"), str) and p["url"]
                                     and p["url"] not in failed_urls and store.known_photo(p["url"]) is None))
            if len(new) > 1:
                ahead = download_many(adapter.client, new, photo_workers, max_photo_bytes)
        not_fetched = 0
        for i, rec in enumerate(records):
            _strip_private(rec)
            if list_only:
                old = store.records.get(str(rec["record_id"]))
                if rec.get("photos_state") == "none_on_portal":
                    rec["photos"] = []
                elif old and old.get("photos_state") in (None, "fetched") and old.get("photos") \
                        and old.get("photo_source") == rec.get("photo_source"):
                    rec["photos"], rec["photos_state"] = old["photos"], old.get("photos_state") or "fetched"
                else:
                    rec["photos"], rec["photos_state"] = [], "not_fetched"
                    not_fetched += 1
                continue
            if listed[i] is None:
                rec["photos"], rec["photos_state"] = [], "not_fetched"
                not_fetched += 1
                continue
            photos, d, f = save_record_photos(adapter, store, rec, listed[i], failed_urls, stats, ahead,
                                              max_photo_bytes)
            downloaded += d
            failed += f
            rec["photos"] = photos
            rec["photos_state"] = "fetched" if photos else "none_on_portal"
        if not_fetched:
            state = "not_fetched"
        else:
            # JSJB coverage lines keep the shape they always had.
            state = "fetched" if getattr(area, "levels", None) else None
        new = store.commit(area, records, seen_at, photos_failed=failed, photos_state=state)
        stats["villages"] += 1
        stats["records"] += len(records)
        stats["new_records"] += new
        stats["photos_downloaded"] += downloaded
        log.info("%s: %d records (%d new), %d photos downloaded", _name(area), len(records), new, downloaded)

    try:
        for area in adapter.list_areas(filters):
            if isinstance(area, SkippedUnit):
                failure(area.kind, {"level": area.level, "name": area.name, "reason": area.kind,
                                    "error": area.error}, "skipped_units")
                continue
            if max_villages is not None and stats["villages"] >= max_villages:
                stats["stopped"] = f"--max-villages {max_villages} reached"
                break
            if max_minutes is not None and time.monotonic() - started > max_minutes * 60:
                stats["stopped"] = f"--max-minutes {max_minutes} reached"
                break
            if since_days is not None:
                last = store.last_collected(area)
                # A village whose photos failed last time stays due, so the rerun retries them.
                # A village only listed so far is due for a run that fetches photos.
                listed_only = not list_only and store.photos_state(area) == "not_fetched"
                if last and not store.photos_failed(area) and not listed_only \
                        and datetime.now(timezone.utc) - last < timedelta(days=since_days):
                    stats["not_due"] += 1
                    continue
            try:
                do_village(area)
            except (PortalChanged, StoreError, AccessDenied):
                raise
            except FetchError as e:
                if e.denied:
                    raise StopRun("access", _denied(adapter) or str(e)) from None
                kind, err = "network", e
            except LayoutChanged as e:
                kind, err = "layout", e
            except Exception as e:
                log.exception("unexpected error in %s", _name(area))
                kind, err = "error", e
            else:
                streak["layout"] = streak["network"] = pauses = 0
                pending += 1
                if pending >= flush_every:
                    store.flush(final=False)
                    pending = 0
                continue
            log.error("village skipped, not marked as checked: %s: %s", _name(area), err)
            failure(kind, {"name": _name(area), "village_code": area.village_code, "reason": kind,
                           "error": str(err)}, "skipped_villages")
    except KeyboardInterrupt:
        stats["stopped"] = "interrupted"
        stats["stop"] = ("interrupted", "stopped by Ctrl+C or a stop signal")
    except StopRun as e:
        stats["stop"] = (e.kind, str(e))
    except AccessDenied as e:
        stats["stop"] = ("access", f"the portal asks for a login, CAPTCHA or bot check: {e}. Stopped; "
                                   "nothing was tried to get past it")
    except PortalChanged as e:
        stats["stop"] = ("layout", f"portal layout changed: {e}")
    except FilterNotFound as e:
        stats["stop"] = ("filter", str(e))
    except FetchError as e:
        if e.denied:
            stats["stop"] = ("access", _denied(adapter) or str(e))
        else:
            stats["stop"] = ("network", f"could not read the portal start page: {e}")
    except LayoutChanged as e:
        stats["stop"] = ("layout", str(e))
        stats["nothing_listed"] = isinstance(e, NothingListed)
    except StoreError as e:
        stats["stop"] = ("disk", str(e))
    except Exception as e:
        log.exception("unexpected error")
        stats["stop"] = ("error", f"unexpected error: {e!r}")
    finally:
        try:
            store.flush(final=True)
        except StoreError as e:
            stats["stop"] = ("disk", str(e))
        except KeyboardInterrupt:
            stats["stop"] = ("interrupted", "stopped while saving; the files on disk are the previous version")
    return stats


def photos_pass(adapter, store, filters=None, max_records=None, max_minutes=None, flush_every=50,
                max_failures=10, max_photo_bytes=MAX_PHOTO_BYTES, photo_workers=1):
    """Fetch the photos of saved records whose photos_state is "not_fetched" (from a --list-only
    run). No area or record list is read again. A record whose photos all came is marked
    "fetched" (or "none_on_portal" when the portal lists none); one with a failed photo stays
    "not_fetched" and is tried again by the next pass. When every record of an area is done, the
    area's coverage line says "fetched". Never raises for one bad record or photo; stops after
    max_failures records in a row could not be read, at a 401/403 or login, or for disk trouble."""
    started = time.monotonic()
    stats = {"started_at": now_iso(), "mode": "photos", "villages": 0, "not_due": 0, "records": 0,
             "new_records": 0, "photos_downloaded": 0, "photos_failed": 0, "photos_missing": 0,
             "skipped_villages": [], "skipped_units": [], "skipped_records": [], "stopped": None, "stop": None,
             "store_problems": list(store.problems), "nothing_listed": False, "records_left": 0}
    wanted = {k: " ".join(str(v).split()).casefold() for k, v in (filters or {}).items() if v}

    def matches(r):
        return all(" ".join(str(r.get(k) or "").split()).casefold() == v for k, v in wanted.items())

    todo = sorted((r for r in store.records.values() if r.get("photos_state") == "not_fetched" and matches(r)),
                  key=lambda r: (str(r.get("area_key") or ""), str(r["record_id"]).zfill(20)))
    members = defaultdict(set)
    for r in store.records.values():
        if r.get("area_key"):
            members[r["area_key"]].add(str(r["record_id"]))
    failed_urls, streak, pending, touched = {}, 0, 0, set()

    def finish_areas():
        for key in list(touched):
            entry = store.coverage.get(key)
            if entry and all(store.records.get(i, {}).get("photos_state") != "not_fetched" for i in members[key]):
                store.coverage[key] = dict(entry, photos_state="fetched", photos_failed=0)
                stats["villages"] += 1
            touched.discard(key)

    try:
        for n, old in enumerate(todo):
            if max_records is not None and stats["records"] >= max_records:
                stats["stopped"] = f"--max-records {max_records} reached"
                break
            if max_minutes is not None and time.monotonic() - started > max_minutes * 60:
                stats["stopped"] = f"--max-minutes {max_minutes} reached"
                break
            rec = dict(old)
            try:
                listed = adapter.list_photos(rec)
                ahead = {}
                if photo_workers > 1:
                    new = list(dict.fromkeys(p["url"] for p in listed or [] if isinstance(p, dict)
                                             and isinstance(p.get("url"), str) and p["url"]
                                             and p["url"] not in failed_urls and store.known_photo(p["url"]) is None))
                    if len(new) > 1:
                        ahead = download_many(adapter.client, new, photo_workers, max_photo_bytes)
                photos, downloaded, failed = save_record_photos(adapter, store, rec, listed or [], failed_urls,
                                                                stats, ahead, max_photo_bytes)
            except (AccessDenied, StoreError):
                raise
            except Exception as e:
                if isinstance(e, FetchError) and e.denied:
                    raise StopRun("access", _denied(adapter) or str(e)) from None
                if not isinstance(e, (FetchError, LayoutChanged)):
                    log.exception("unexpected error in the photos of record %s", rec.get("record_id"))
                log.error("photos of record %s not fetched, tried again next pass: %s", rec.get("record_id"), e)
                stats["skipped_records"].append({"record_id": str(rec["record_id"]), "error": str(e)})
                streak += 1
                if streak >= max_failures:
                    raise StopRun("network" if isinstance(e, FetchError) else "layout",
                                  f"the photos of {streak} records in a row could not be read") from None
                continue
            streak = 0
            _strip_private(rec)
            rec["photos"] = photos
            rec["photos_state"] = "not_fetched" if failed else ("fetched" if photos else "none_on_portal")
            store.write_record(rec)
            stats["records"] += 1
            stats["photos_downloaded"] += downloaded
            if rec.get("area_key"):
                touched.add(rec["area_key"])
            pending += 1
            if pending >= flush_every:
                finish_areas()
                store.flush(final=False)
                pending = 0
        finish_areas()
    except KeyboardInterrupt:
        stats["stopped"] = "interrupted"
        stats["stop"] = ("interrupted", "stopped by Ctrl+C or a stop signal")
    except StopRun as e:
        stats["stop"] = (e.kind, str(e))
    except AccessDenied as e:
        stats["stop"] = ("access", f"the portal asks for a login, CAPTCHA or bot check: {e}. Stopped; "
                                   "nothing was tried to get past it")
    except StoreError as e:
        stats["stop"] = ("disk", str(e))
    except Exception as e:
        log.exception("unexpected error")
        stats["stop"] = ("error", f"unexpected error: {e!r}")
    finally:
        try:
            finish_areas()
            store.flush(final=True)
        except StoreError as e:
            stats["stop"] = ("disk", str(e))
        except KeyboardInterrupt:
            stats["stop"] = ("interrupted", "stopped while saving; the files on disk are the previous version")
    stats["records_left"] = sum(1 for r in store.records.values() if r.get("photos_state") == "not_fetched")
    return stats


def exit_code(stats):
    if stats["stop"]:
        return EXIT[stats["stop"][0]]
    return 5 if stats["skipped_villages"] or stats["skipped_units"] or stats["photos_failed"] \
        or stats.get("skipped_records") else 0


def summary_lines(stats):
    if stats.get("mode") == "photos":
        out = [f"Records whose photos were fetched: {stats['records']}; areas now fully fetched: "
               f"{stats['villages']}; records still waiting for photos: {stats['records_left']}"]
    else:
        out = [f"Villages saved: {stats['villages']} ({stats['records']} records, {stats['new_records']} new)"]
    if stats["not_due"]:
        out.append(f"Villages not due yet (--since-days): {stats['not_due']}")
    out.append(f"Photos downloaded: {stats['photos_downloaded']}; failed, will retry next run: "
               f"{stats['photos_failed']}; missing on the portal: {stats['photos_missing']}")
    out.append(f"Villages skipped: {len(stats['skipped_villages'])}")
    for v in stats["skipped_villages"]:
        out.append(f"  [{v['reason']}] {v['name']}: {v['error']}")
    out.append(f"Lists skipped: {len(stats['skipped_units'])}")
    for u in stats["skipped_units"]:
        out.append(f"  [{u['reason']}] {u['level']} of {u['name']}: {u['error']}")
    if stats.get("skipped_records"):
        out.append(f"Records whose photos could not be read: {len(stats['skipped_records'])}")
        for r in stats["skipped_records"][:20]:
            out.append(f"  record {r['record_id']}: {r['error']}")
    for p in stats["store_problems"]:
        out.append(f"Repaired on load: {p}")
    if stats["stop"]:
        out.append(f"STOPPED EARLY ({stats['stop'][0]}): {stats['stop'][1]}. Work saved before this is kept.")
    elif stats["stopped"]:
        out.append(f"Stopped: {stats['stopped']}")
    if stats["skipped_villages"] or stats["skipped_units"] or stats["photos_failed"] or stats.get("skipped_records"):
        out.append("Run the same command again to retry what was skipped.")
    return out


def default_log(data_dir, scheme):
    return os.path.join(data_dir, "logs", f"{scheme}.log")


class LogFile:
    """Copies log lines to a file as well as the screen, while the run lasts. write() adds one
    more line (the summary). An empty path, or one that cannot be opened, turns it off."""

    def __init__(self, path):
        self.handler = None
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.handler = logging.FileHandler(path, encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("log file %s not opened (%s); logging to the screen only", path, e)
            return
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(self.handler)

    def write(self, text):
        if self.handler is None:
            return
        self.handler.acquire()
        try:
            self.handler.stream.write(text + chr(10))
            self.handler.stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            self.handler.release()

    def close(self):
        if self.handler is not None:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()
            self.handler = None


def scheme_choice(argv):
    """The scheme id given on the command line, read before the full parser is built, so the
    filter options can come from that scheme's adapter."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("scheme", nargs="?")
    known, _ = pre.parse_known_args(argv)
    return known.scheme if known.scheme in SCHEMES else None


def _on_signal(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    chosen = scheme_choice(argv)
    cls = SCHEMES.get(chosen)
    levels = tuple(getattr(cls, "filters", None) or ("state", "district", "area", "block", "village"))
    epilog = ("Adapters left out because they do not import: "
              + "; ".join(f"{k}.py ({v})" for k, v in sorted(BROKEN.items()))) if BROKEN else None
    ap = argparse.ArgumentParser(prog="python -m collector", epilog=epilog,
                                 description="Collect public scheme records and photos. No AI involved.")
    ap.add_argument("scheme", choices=sorted(SCHEMES))
    for lvl in levels:
        ap.add_argument(f"--{lvl}", help=f"{lvl} name, case-insensitive; omit for all")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list-only", action="store_true",
                      help="save records without their photos (photos_state not_fetched); fetch them later with --photos")
    mode.add_argument("--photos", action="store_true",
                      help="fetch the photos of saved records that have none yet; reads no area or record list")
    ap.add_argument("--max-records", type=int, help="with --photos: stop after this many records")
    ap.add_argument("--log", help="also write the log to this file (default: <data-dir>/logs/<scheme>.log; "
                                  "empty to turn off)")
    ap.add_argument("--max-villages", type=int, help="stop after this many villages")
    ap.add_argument("--max-minutes", type=float, help="stop after this many minutes")
    ap.add_argument("--since-days", type=float, help="skip villages collected within this many days")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    ap.add_argument("--max-layout-failures", type=int, default=10,
                    help="stop after this many layout failures in a row (default 10)")
    ap.add_argument("--max-photo-mb", type=float, default=MAX_PHOTO_BYTES / 2 ** 20,
                    help="largest photo to download, in MB (default 25)")
    ap.add_argument("--photo-workers", type=int, default=1, choices=range(1, 5), metavar="N",
                    help="photos downloaded at a time, 1 to 4 (default 1); each keeps --delay between its requests")
    ap.add_argument("--data-dir", default="data")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for sig in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _on_signal)
            except (ValueError, OSError):
                pass

    filters = {k: getattr(a, k) for k in levels}
    if a.list_only and not getattr(SCHEMES[a.scheme], "supports_list_only", False):
        log.error("%s does not support --list-only: its records do not keep what list_photos needs", a.scheme)
        return 3
    try:
        lock = DataLock(a.data_dir, a.scheme)
    except StoreError as e:
        log.error("%s", e)
        return EXIT["busy" if isinstance(e, DataInUse) else "disk"]
    try:
        try:
            store = Store(a.data_dir, a.scheme)
        except StoreError as e:
            log.error("%s", e)
            return EXIT["disk"]
        return _run(a, filters, store)
    finally:
        lock.release()


def make_adapter(scheme, store, delay=1.0):
    """The adapter with a client set up from its hosts settings, and its salt source."""
    cls = SCHEMES[scheme]
    hosts = getattr(cls, "hosts", None)
    adapter = cls(Client(delay=delay, hosts=hosts) if hosts else Client(delay=delay))
    if store is not None:
        lazy_salt(adapter, store)
    return adapter


def lazy_salt(adapter, store):
    """adapter.ref() reads or makes data/state/<scheme>.salt the first time it is used."""
    base_ref = type(adapter).ref

    def ref(value):
        if adapter.salt is None:
            adapter.salt = store.salt()
        return base_ref(adapter, value)
    adapter.ref = ref


def _run(a, filters, store):
    logfile = LogFile(default_log(a.data_dir, a.scheme) if a.log is None else a.log)
    try:
        return _collect(a, filters, store, logfile)
    finally:
        logfile.close()


def _collect(a, filters, store, logfile):
    adapter = make_adapter(a.scheme, store, a.delay)
    client = adapter.client
    max_bytes = int(a.max_photo_mb * 2 ** 20)
    if a.photos:
        log.info("fetching photos of %s records saved without them", a.scheme)
        stats = photos_pass(adapter, store, filters, a.max_records, a.max_minutes, max_photo_bytes=max_bytes,
                            photo_workers=a.photo_workers)
    else:
        log.info("collecting %s%s with filters %s", a.scheme, " (list only)" if a.list_only else "",
                 {k: v for k, v in filters.items() if v} or "none")
        stats = collect(adapter, store, filters, a.max_villages, a.max_minutes, a.since_days,
                        max_layout_failures=a.max_layout_failures, max_photo_bytes=max_bytes,
                        photo_workers=a.photo_workers, list_only=a.list_only)
    code = exit_code(stats)
    if stats["stop"]:
        log.error("%s", stats["stop"][1])
    log.info("%d requests, %d records on disk in %s", getattr(client, "requests", 0), len(store.records),
             store.records_path)
    text = "\n".join(["", "Run summary"] + summary_lines(stats))
    print(text, flush=True)
    logfile.write(text)
    summary = dict(stats, stop=None, finished_at=now_iso(), scheme=a.scheme,
                   filters={k: v for k, v in filters.items() if v}, exit_code=code,
                   stopped_early=dict(zip(("kind", "message"), stats["stop"])) if stats["stop"] else None)
    try:
        store.write_summary(summary)
    except StoreError as e:
        log.error("run summary not saved: %s", e)
    return code


if __name__ == "__main__":
    sys.exit(main())
