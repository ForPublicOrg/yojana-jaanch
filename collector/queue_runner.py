"""Work through a plan of areas in order, for days, and pick up where it stopped.

    python -m collector.queue_runner --plan plans/jsjb-india.json [--dry-run] [--only-phase capitals]

See docs/QUEUE.md."""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from .__main__ import EXIT, _on_signal, collect, exit_code, summary_lines
from .base import FilterNotFound
from .http import Client, FetchError
from .schemes import SCHEMES
from .store import DataInUse, DataLock, Store, StoreError, _finish_in_place, _parse_time, _write_atomic, now_iso

log = logging.getLogger("collector.queue")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AREAS = ("Rural", "Urban")
DISTRICT_LIST_DAYS = 7


class PlanError(Exception):
    pass


def _cf(s):
    return " ".join((s or "").split()).casefold()


def unit_key(state, district, area):
    return f"{_cf(state)}|{_cf(district) or '*'}|{_cf(area) or '*'}"


def _areas(value):
    if value in (None, "", "both"):
        return list(AREAS)
    values = [value] if isinstance(value, str) else list(value)
    for v in values:
        if v not in AREAS:
            raise PlanError(f"area must be Rural, Urban, both or a list of those, not {v!r}")
    return values


def load_plan(path):
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise PlanError(f"could not read the plan {path}: {e}") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("units"), list) or not plan["units"]:
        raise PlanError(f"{path}: expected an object with a non-empty \"units\" list")
    for i, u in enumerate(plan["units"], 1):
        if not isinstance(u, dict) or not u.get("state") or not u.get("phase"):
            raise PlanError(f"{path}: unit {i} needs at least \"phase\" and \"state\"")
        _areas(u.get("area"))
    return plan


def resolve(plan, districts_of, only_phases=None):
    """Plan units -> ordered list of runnable units, one per (state, district or whole state, area).

    A unit with "district": "remaining" stands for every district of that state, in portal order, that
    no earlier unit covers; districts_of(state) returns the portal's names or raises. Units repeated
    (Chandigarh is the capital of two states and a territory) run once, at their first place."""
    out, seen, covered, problems = [], set(), set(), []
    for u in plan["units"]:
        state, district, phase = u["state"], u.get("district"), u["phase"]
        if district == "remaining":
            try:
                names = districts_of(state)
            except Exception as e:
                problems.append({"phase": phase, "state": state, "error": str(e)})
                continue
            for d in names:
                for area in _areas(u.get("area")):
                    if (_cf(state), "*", _cf(area)) in covered or (_cf(state), _cf(d), _cf(area)) in covered:
                        continue
                    covered.add((_cf(state), _cf(d), _cf(area)))
                    key = unit_key(state, d, area)
                    seen.add(key)
                    out.append({"key": key, "phase": phase, "state": state, "district": d, "area": area, "note": ""})
            continue
        for area in _areas(u.get("area")):
            key = unit_key(state, district, area)
            covered.add((_cf(state), _cf(district) or "*", _cf(area)))
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "phase": phase, "state": state, "district": district, "area": area,
                        "note": u.get("note", "")})
    if only_phases:
        wanted = {_cf(p) for p in only_phases}
        out = [u for u in out if _cf(u["phase"]) in wanted]
        problems = [p for p in problems if _cf(p["phase"]) in wanted]
    return out, problems


def describe(u):
    return f"{u['state']} > {u['district'] or 'all districts'} > {u['area']}"


class QueueState:
    """data/state/queue.json: what each unit did last, and cached district lists."""

    def __init__(self, data_dir, read_only=False):
        self.path = os.path.join(data_dir, "state", "queue.json")
        self.data = {"units": {}, "districts": {}}
        src = self.path
        if read_only:
            # A write cut off mid-way left the full new copy in .inplace; read it without touching data/.
            if os.path.exists(self.path + ".inplace"):
                src = self.path + ".inplace"
        else:
            problems = []
            _finish_in_place(self.path, problems)
            for p in problems:
                log.warning("%s", p)
        if os.path.exists(src):
            try:
                with open(src, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.data.update({k: v for k, v in data.items() if isinstance(v, dict)})
            except (OSError, ValueError) as e:
                if read_only:
                    log.warning("%s is unreadable (%s); showing the plan as if nothing was done", src, e)
                    return
                dest = f"{self.path}.damaged-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                try:
                    os.replace(self.path, dest)
                except OSError:
                    pass
                log.warning("%s was unreadable (%s); moved aside, starting the queue from the plan. "
                            "Areas collected recently are still skipped village by village.", self.path, e)

    def unit(self, key):
        return self.data["units"].get(key)

    def save(self):
        self.data["updated_at"] = now_iso()
        _write_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def done_recently(self, key, since_days, now=None):
        u = self.unit(key)
        if not u or u.get("status") != "done":
            return False
        t = _parse_time(u.get("finished_at"))
        now = now or datetime.now(timezone.utc)
        return bool(t) and now - t < timedelta(days=since_days)


def order_units(units, state):
    """Plan order in three groups: areas never finished, then areas that failed or stayed incomplete
    last time, then finished areas due for a refresh. A pass of India takes longer than since_days,
    so a refresh never comes before an area that has not been collected yet."""
    groups = ([], [], [])
    for u in units:
        status = (state.unit(u["key"]) or {}).get("status")
        groups[2 if status == "done" else 1 if status in ("failed", "incomplete") else 0].append(u)
    return groups[0] + groups[1] + groups[2]


def district_lister(adapter, state, refresh=True, now=None):
    """districts_of(state) for resolve(): cached in queue.json for DISTRICT_LIST_DAYS, read from the portal otherwise."""
    cache = state.data.setdefault("districts", {})

    def districts_of(name):
        hit = cache.get(_cf(name))
        t = _parse_time(hit.get("read_at")) if isinstance(hit, dict) else None
        if t and (now or datetime.now(timezone.utc)) - t < timedelta(days=DISTRICT_LIST_DAYS) and hit.get("names"):
            return hit["names"]
        if not refresh or adapter is None:
            if hit and hit.get("names"):
                return hit["names"]
            raise FetchError("district list not read yet")
        names = adapter.list_districts(name)
        if not names:
            raise FilterNotFound(f"the portal lists no districts for {name}")
        cache[_cf(name)] = {"read_at": now_iso(), "names": names}
        return names
    return districts_of


def run_checker(data_dir):
    """Rebuild site/data from the records. Only for the project's own data folder."""
    data_dir = os.path.abspath(data_dir)
    if os.path.basename(data_dir) != "data":
        log.info("checker not run: the data folder is not named data")
        return None
    root = os.path.dirname(data_dir)
    cmd = [sys.executable, "-m", "checker", "--root", root]
    log.info("updating the website data: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def run_queue(plan, adapter, store, data_dir, since_days=30, max_minutes=None, only_phases=None, dry_run=False,
              photo_workers=1, check=run_checker, out=print, sleep=time.sleep, collect_fn=collect,
              outage_wait=1800, max_layout_stops=2, max_empty_in_a_row=10, clock=time.monotonic):
    """Returns an exit code: 0 all done (or time budget used), 5 some units failed (rerun to retry them),
    2 the layout looks changed everywhere, 6 disk trouble, 130 stopped."""
    started = clock()
    state = QueueState(data_dir, read_only=dry_run)
    units, problems = resolve(plan, district_lister(adapter, state), only_phases)
    if not dry_run:
        state.save()
    order = order_units(units, state)
    due = [u for u in order if not state.done_recently(u["key"], since_days)]

    if dry_run:
        out(f"Plan: {len(units)} units, {len(units) - len(due)} finished in the last {since_days:g} days, "
            f"{len(due)} to do. Order:")
        for n, u in enumerate(order, 1):
            rec = state.unit(u["key"]) or {}
            if state.done_recently(u["key"], since_days):
                what = f"done {rec.get('finished_at')}, skipped"
            elif rec.get("status"):
                what = f"to do (last: {rec['status']} {rec.get('finished_at') or rec.get('started_at') or ''})".rstrip()
            else:
                what = "to do"
            out(f"{n:5d}. [{u['phase']}] {describe(u)}: {what}" + (f"  ({u['note']})" if u.get("note") else ""))
        for p in problems:
            out(f"  [{p['phase']}] remaining districts of {p['state']} not listed: {p['error']}")
        return 0

    totals = {"done": 0, "failed": [], "left_skipped": [], "incomplete": 0, "villages": 0, "records": 0, "new_records": 0,
              "photos": 0}
    code = None
    layout_stops = network_stops = 0
    empty_streak = [0]
    retry = []
    wait = 0

    def minutes_left():
        if max_minutes is None:
            return None
        return max_minutes - (clock() - started) / 60

    def run_unit(u, last_try):
        nonlocal layout_stops, network_stops
        rec = state.data["units"].setdefault(u["key"], {"state": u["state"], "district": u["district"],
                                                         "area": u["area"], "phase": u["phase"], "attempts": 0})
        rec.update(status="running", started_at=now_iso(), attempts=rec.get("attempts", 0) + 1)
        state.save()
        out(f"\n=== [{u['phase']}] {describe(u)} (attempt {rec['attempts']})")
        filters = {"state": u["state"], "district": u["district"], "area": u["area"], "block": None, "village": None}
        stats = collect_fn(adapter, store, filters, max_minutes=minutes_left(), since_days=since_days,
                           photo_workers=photo_workers)
        c = exit_code(stats)
        for k, t in (("villages", "villages"), ("records", "records"), ("new_records", "new_records"),
                     ("photos_downloaded", "photos")):
            totals[t] += stats.get(k, 0)
        rec.update(finished_at=now_iso(), exit_code=c, villages=stats["villages"], not_due=stats["not_due"],
                   records=stats["records"], new_records=stats["new_records"],
                   skipped=len(stats["skipped_villages"]) + len(stats["skipped_units"]),
                   photos_failed=stats["photos_failed"],
                   error=stats["stop"][1] if stats["stop"] else None)
        if stats.get("nothing_listed"):
            out("  No towns or villages are listed for this area on the portal; counted as done.")
        else:
            for line in summary_lines(stats):
                out("  " + line)
        kind = stats["stop"][0] if stats["stop"] else None
        if stats.get("nothing_listed"):
            # An area with no towns or villages listed is plausible (many districts have no urban
            # entries); many such areas in a row are not, and stop the queue below.
            kind, c = "empty", 0
            rec.update(exit_code=0, error=None, empty=True)
        if kind == "interrupted":
            rec["status"] = "stopped"
        elif stats["stopped"] and "max-minutes" in stats["stopped"]:
            rec["status"] = "partial"
        elif c == 0:
            rec["status"] = "done"
            totals["done"] += 1
        elif c == 5:
            # Not done: the next run visits this area again, and only what is still missing is
            # fetched, because the villages already saved are skipped under --since-days.
            rec["status"] = "incomplete"
            totals["incomplete"] += 1
            if last_try:
                totals["left_skipped"].append((u, rec["skipped"], rec["photos_failed"]))
        else:
            rec["status"] = "failed"
        state.save()
        empty_streak[0] = empty_streak[0] + 1 if kind == "empty" else 0
        layout_stops = layout_stops + 1 if kind == "layout" else 0
        network_stops = network_stops + 1 if kind == "network" else 0
        if stats["villages"] and check:
            try:
                r = check(data_dir)
                if r not in (None, 0):
                    out(f"  the checker ended with code {r}; the queue goes on")
            except Exception as e:
                out(f"  the checker could not run ({e}); the queue goes on")
        return rec["status"], kind, c

    try:
        for last_try, todo in ((False, due), (True, retry)):
            for u in todo:
                left = minutes_left()
                if left is not None and (left <= 0 or left * 60 < wait):
                    out(f"\n--max-minutes {max_minutes:g} reached; run the same command to go on.")
                    code = 0
                    break
                if wait:
                    out(f"  the portal could not be reached; waiting {wait / 60:.0f} minutes before the next area")
                    sleep(wait)
                    wait = 0
                try:
                    status, kind, c = run_unit(u, last_try)
                except StoreError as e:
                    out(f"  local disk problem: {e}")
                    code = EXIT["disk"]
                    break
                except Exception as e:
                    # A bug in one area must not end a run of days; record it and go on.
                    log.exception("unexpected error in %s", describe(u))
                    rec = state.data["units"].get(u["key"], {})
                    rec.update(status="failed", finished_at=now_iso(), exit_code=1, error=f"unexpected error: {e!r}")
                    status, kind, c = "failed", "error", 1
                if status == "stopped":
                    code = EXIT["interrupted"]
                    break
                if status == "partial":
                    out(f"\n--max-minutes {max_minutes:g} reached; run the same command to go on.")
                    code = 0
                    break
                if kind == "disk":
                    code = EXIT["disk"]
                    break
                if empty_streak[0] >= max_empty_in_a_row:
                    out(f"\n{empty_streak[0]} areas in a row listed no towns or villages at all; the portal has "
                        "probably changed how it fills its lists. Stopping the queue.")
                    code = EXIT["layout"]
                    break
                if layout_stops >= max_layout_stops:
                    out(f"\n{layout_stops} areas in a row stopped because the pages looked different; "
                        "the portal has probably changed. Stopping the queue.")
                    code = EXIT["layout"]
                    break
                if status in ("failed", "incomplete"):
                    if last_try:
                        if status == "failed":
                            totals["failed"].append((u, c, state.unit(u["key"]).get("error")))
                    else:
                        retry.append(u)
                        out(f"  will be tried again at the end of this pass")
                if kind == "network":
                    # Waited out before the next area, so never after the last one or past --max-minutes.
                    wait = min(outage_wait * 2 ** (network_stops - 1), 4 * 3600)
            if code is not None:
                break
            if not last_try and problems:
                # District lists that could not be read at the start: try once more before the retry pass.
                known = {u["key"] for u in units}
                units2, problems = resolve(plan, district_lister(adapter, state), only_phases)
                extra = [u for u in units2 if u["key"] not in known]
                units += extra
                retry[:0] = [u for u in extra if not state.done_recently(u["key"], since_days)]
    except KeyboardInterrupt:
        code = EXIT["interrupted"]
    finally:
        try:
            state.save()
        except StoreError as e:
            out(f"queue progress not saved: {e}")

    if code is None:
        code = 5 if totals["failed"] or totals["left_skipped"] or problems else 0
    done_all = sum(1 for u in units if state.done_recently(u["key"], since_days))
    out("\nQueue summary")
    out(f"  Areas finished this run: {totals['done']}; failed: {len(totals['failed'])}; "
        f"plan progress: {done_all} of {len(units)} areas done in the last {since_days:g} days")
    out(f"  Villages saved: {totals['villages']} ({totals['records']} records, {totals['new_records']} new), "
        f"photos downloaded: {totals['photos']}, time: {(clock() - started) / 60:.0f} minutes")
    for u, c, err in totals["failed"]:
        out(f"  FAILED (exit {c}) {describe(u)}: {err or 'some villages or lists were skipped'}")
    for u, n, photos in totals["left_skipped"]:
        out(f"  STILL SKIPPED in {describe(u)}: {n} villages or lists, {photos} photos (tried again on the next run)")
    for p in problems:
        out(f"  NOT EXPANDED remaining districts of {p['state']}: {p['error']} (tried again next run)")
    if code == EXIT["interrupted"]:
        out("  Stopped. Run the same command to go on where it stopped.")
    elif code == 5:
        out("  Run the same command again to retry what failed.")
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python run.py queue", description="Collect a plan of areas in order. No AI involved.")
    ap.add_argument("--plan", required=True, help="plan file, for example plans/jsjb-india.json")
    ap.add_argument("--since-days", type=float, help="skip areas and villages collected within this many days "
                                                     "(default: the plan's since_days, else 30)")
    ap.add_argument("--max-minutes", type=float, help="stop after this many minutes")
    ap.add_argument("--only-phase", action="append", help="run only this phase (repeatable), e.g. capitals")
    ap.add_argument("--dry-run", action="store_true", help="print the order and what is done; collect nothing")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between page requests (default 1)")
    ap.add_argument("--photo-workers", type=int, default=1, choices=range(1, 5), metavar="N",
                    help="photos downloaded at a time, 1 to 4 (default 1)")
    ap.add_argument("--no-check", action="store_true", help="do not run the checker after each area")
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
    try:
        plan = load_plan(a.plan)
        phases = {_cf(u["phase"]) for u in plan["units"]}
        unknown = [p for p in a.only_phase or [] if _cf(p) not in phases]
        if unknown:
            raise PlanError(f"--only-phase {', '.join(unknown)}: the plan has phases "
                            + ", ".join(sorted({u["phase"] for u in plan["units"]})))
    except PlanError as e:
        log.error("%s", e)
        return 3
    scheme = plan.get("scheme", "jsjb")
    since = a.since_days if a.since_days is not None else float(plan.get("since_days", 30))
    adapter = SCHEMES[scheme](Client(delay=a.delay))
    out = lambda s: print(s, flush=True)
    if a.dry_run:
        return run_queue(plan, adapter, None, a.data_dir, since, only_phases=a.only_phase, dry_run=True, out=out)
    try:
        lock = DataLock(a.data_dir)
    except StoreError as e:
        log.error("%s", e)
        return EXIT["busy" if isinstance(e, DataInUse) else "disk"]
    try:
        try:
            store = Store(a.data_dir, scheme)
        except StoreError as e:
            log.error("%s", e)
            return EXIT["disk"]
        return run_queue(plan, adapter, store, a.data_dir, since, a.max_minutes, a.only_phase,
                         photo_workers=a.photo_workers, check=None if a.no_check else run_checker, out=out)
    except KeyboardInterrupt:
        return EXIT["interrupted"]
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
