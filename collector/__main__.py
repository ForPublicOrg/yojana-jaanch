"""python -m collector <scheme> [--state ...] [--district ...] ..."""
import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from .base import FilterNotFound, LayoutChanged, NothingListed, PortalChanged, SkippedUnit
from .http import Client, FetchError
from .schemes import SCHEMES
from .store import DataInUse, DataLock, Store, StoreError, image_type, now_iso

log = logging.getLogger("collector")

MAX_PHOTO_BYTES = 25 * 1024 * 1024
EXIT = {"layout": 2, "filter": 3, "network": 4, "disk": 6, "busy": 7, "error": 1, "interrupted": 130}


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
    return f"{area.state} > {area.district} > {area.block} > {area.village}"


def collect(adapter, store, filters, max_villages=None, max_minutes=None, since_days=None, flush_every=10,
            max_layout_failures=10, max_network_failures=10, outage_pause=300, max_outage_pauses=12,
            max_photo_bytes=MAX_PHOTO_BYTES, photo_workers=1, sleep=time.sleep):
    """Collect every matching village. Never raises for one bad village, list or photo.

    The run stops early only for: max_layout_failures layout problems in a row, the dropdowns
    missing from the start page, the network down through max_outage_pauses pauses, local disk
    trouble, or Ctrl+C. stats["stop"] then holds (kind, message)."""
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
        if photo_workers > 1:
            new = list(dict.fromkeys(p["url"] for rec in records for p in adapter.list_photos(rec)
                                     if p["url"] not in failed_urls and store.known_photo(p["url"]) is None))
            if len(new) > 1:
                ahead = download_many(adapter.client, new, photo_workers, max_photo_bytes)
        for rec in records:
            photos = []
            for p in adapter.list_photos(rec):
                url = p["url"]
                entry = store.known_photo(url)
                if entry is None:
                    if url in failed_urls:
                        entry, outcome = {"sha256": None, "bytes": None, "file": None}, failed_urls[url]
                    else:
                        entry, outcome = fetch_photo(adapter.client, store, url, max_photo_bytes, ahead.pop(url, None))
                        if outcome == "ok":
                            downloaded += 1
                        else:
                            stats["photos_" + outcome] += 1
                            failed_urls[url] = outcome
                    failed += outcome == "failed"
                photos.append({"url": url, "sha256": entry["sha256"], "bytes": entry["bytes"],
                               "file": entry["file"], "uploaded_at": p["uploaded_at"]})
            rec.pop("_photo_urls", None)
            rec["photos"] = photos
        new = store.commit(area, records, seen_at, photos_failed=failed)
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
                if last and not store.photos_failed(area) and datetime.now(timezone.utc) - last < timedelta(days=since_days):
                    stats["not_due"] += 1
                    continue
            try:
                do_village(area)
            except (PortalChanged, StoreError):
                raise
            except FetchError as e:
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
    except PortalChanged as e:
        stats["stop"] = ("layout", f"portal layout changed: {e}")
    except FilterNotFound as e:
        stats["stop"] = ("filter", str(e))
    except FetchError as e:
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


def exit_code(stats):
    if stats["stop"]:
        return EXIT[stats["stop"][0]]
    return 5 if stats["skipped_villages"] or stats["skipped_units"] or stats["photos_failed"] else 0


def summary_lines(stats):
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
    for p in stats["store_problems"]:
        out.append(f"Repaired on load: {p}")
    if stats["stop"]:
        out.append(f"STOPPED EARLY ({stats['stop'][0]}): {stats['stop'][1]}. Work saved before this is kept.")
    elif stats["stopped"]:
        out.append(f"Stopped: {stats['stopped']}")
    if stats["skipped_villages"] or stats["skipped_units"] or stats["photos_failed"]:
        out.append("Run the same command again to retry what was skipped.")
    return out


def _on_signal(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m collector",
                                 description="Collect public scheme records and photos. No AI involved.")
    ap.add_argument("scheme", choices=sorted(SCHEMES))
    for lvl in ("state", "district", "area", "block", "village"):
        ap.add_argument(f"--{lvl}", help=f"{lvl} name, case-insensitive; omit for all")
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

    filters = {k: getattr(a, k) for k in ("state", "district", "area", "block", "village")}
    try:
        lock = DataLock(a.data_dir)
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


def _run(a, filters, store):
    client = Client(delay=a.delay)
    adapter = SCHEMES[a.scheme](client)
    log.info("collecting %s with filters %s", a.scheme, {k: v for k, v in filters.items() if v} or "none")
    stats = collect(adapter, store, filters, a.max_villages, a.max_minutes, a.since_days,
                    max_layout_failures=a.max_layout_failures, max_photo_bytes=int(a.max_photo_mb * 2 ** 20),
                    photo_workers=a.photo_workers)
    code = exit_code(stats)
    if stats["stop"]:
        log.error("%s", stats["stop"][1])
    log.info("%d requests, %d records on disk in %s", getattr(client, "requests", 0), len(store.records),
             store.records_path)
    print("\n".join(["", "Run summary"] + summary_lines(stats)), flush=True)
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
