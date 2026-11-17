# collector

Downloads public records and photos from scheme portals. Python 3.12 standard library only, no AI.

    python -m collector jsjb --state Punjab --district Sangrur [--area Rural] [--block Sangrur] [--village Duggan]

Names match the portal dropdowns, ignoring case. A level you leave out means all of it.
Bounds: `--max-villages N`, `--max-minutes M`, `--since-days D` (skip villages collected in the last D days).
Other flags: `--delay` (seconds between requests, default 1), `--data-dir` (default `data`),
`--photo-workers N` (see Speed below), `--log FILE` (default `<data-dir>/logs/<scheme>.log`, `--log ""`
for none), `--list-only` and `--photos` (see "List first, photos later" below). The filter options
come from the adapter: JSJB has state, district, area, block and village; another scheme may have others.

To collect many areas in order over days, use the queue: `python run.py queue --plan
plans/jsjb-india.json` (see `docs/QUEUE.md`).

Refresh: run the same command again. Every village is re-read, new records are added, `last_seen`
is updated, and only photo URLs missing from `data/state/<scheme>.photo_index.json` are downloaded.
Progress is saved every 10 villages and on Ctrl+C, so a stopped run can be restarted.
Only one collector or queue per scheme writes to a data folder at a time
(`data/state/collector.<scheme>.lock`); a second one of the same scheme exits with code 7. JSJB
also takes the older `data/state/collector.lock`, so a JSJB run started with older code still
blocks a new one. The operating system releases the locks when the process ends.

Outputs: `data/records/<scheme>.jsonl`, `data/records/<scheme>.coverage.json`,
`data/photos/<sha256[:2]>/<sha256>.<ext>` (stored once per unique image).

## Several schemes at once

Each scheme runs in its own process, so each portal gets its own stream of requests at its own
delay. Every file a run writes belongs to its scheme: records, coverage, journals and photo index
(`<scheme>.*`), the run summary (`last_run.json` for jsjb, `last_run.<scheme>.json` for others),
the queue progress (`queue.json` for jsjb, `queue.<plan file name>.json` for others), the salt
(`<scheme>.salt`) and the log (`logs/<scheme>.log`). The only shared folder is `data/photos/`: a photo
is written to a temporary file of its own and renamed into place, so two processes storing the
same photo both end with one whole file.

    python run.py queue --plan plans/jsjb-india.json          # terminal 1
    python run.py queue --plan plans/<id>-<scope>.json        # terminal 2, another scheme

Two queues each run the checker after an area; the checker takes `data/state/checker.lock`, so the
second waits for the first to finish (up to 30 minutes, `--lock-wait`).

## List first, photos later

For a scheme whose adapter sets `supports_list_only = True`:

    python run.py collect --scheme <id> --list-only [filters]   # records only, photos_state "not_fetched"
    python run.py collect --scheme <id> --photos [--max-records N] [--max-minutes M]

The list-only run saves every record with `photos_state: "not_fetched"` and its area's coverage line
with `photos_state: "not_fetched"`; it never calls `list_photos`. A record already saved with its
photos keeps them when its `photo_source` has not changed. The photos pass reads no area or record
list: it calls `list_photos` on each saved record that is still `not_fetched`, marks it `fetched`
(or `none_on_portal`), and marks the area `fetched` once all its records are. A record with a photo
that failed stays `not_fetched` for the next pass. The checker leaves `not_fetched` records out of
photo checks. A normal run treats a listed-only area as due under `--since-days`. JSJB does not
support list-only (see Speed).

## Trial runs

Point `--data-dir` at a scratch folder; nothing then touches `data/` or `site/data`:

    python run.py collect --scheme <id> --data-dir data/_trial/<id> --max-minutes 10 [filters]
    python run.py check --data-dir data/_trial/<id>       # cases in data/_trial/<id>/preview/

A village is identified in the coverage file by state, area type and code, not by code alone:
urban ward codes use the same number range as rural village codes (Leh has wards 10856 to 10864,
inside the range of Punjab village codes). Each coverage entry keeps that identity under `key`,
taken from the dropdown names, because the names in the records can be spelled differently.
Entries written before `key` existed are matched by their state and area type, ignoring case.

Other settings: `--max-layout-failures N` (default 10), `--max-photo-mb M` (default 25).

## Saving at scale

The checker reads `data/records/<scheme>.jsonl` and `data/records/<scheme>.coverage.json`, and
their format is unchanged. During a run, saving every 10 villages appends the changed records to
the `.jsonl` (a later line for the same `record_id` wins, as it always did in the checker), and
appends coverage and photo index changes to `data/state/<scheme>.coverage.journal.jsonl` and
`data/state/<scheme>.photo_index.journal.jsonl`. At the end of a run the coverage journal is folded
into the coverage file. A file is rewritten in full only when it is under 32 MB at the end of a run,
or once it has grown to twice its live size. Records are still written before coverage.

Measured on a copy of the real data scaled 25 times (133,525 records, 11,075 villages, 194 MB):
a full save took 3.4 s, the append after 10 villages took 0.009 s; loading took 3.8 s.

## Speed

The portal answers a search only for one village: a search with the village or the block left at
"Select" returns `Total : 0` even for a block with records, and an empty village value returns
HTTP 500. So there is one search per village, and no block-wide shortcut. The number in brackets
after a village name is not a record count (Andheri (22) has 1 record, Bhagwanpura (34) has 0), so
villages with no records cannot be skipped in advance.

Photos are most of the requests (about 1.3 per record on Punjab data so far). Each photo request
waits for the 1 second delay; the download itself took about 0.5 s. `--photo-workers 2` downloads
a village's new photos two at a time, each worker keeping the delay between its own requests, so
photos reach about 2 requests a second while page requests stay at one at a time. The photos are
on the same host as the pages, so this is above one request a second while photos download; it is
opt-in for that reason. The workers' requests are spaced evenly (half a delay apart with 2), the
first comes at least that long after the last page request, and the next page request waits a full
delay after the last photo. In a test of 12
photos it took 6.6 s, against about 1 s per photo one at a time. Every photo is checked exactly as
before (the same bytes are stored, tested on a live village). Default 1. A HEAD request was not
adopted: a known photo URL is never downloaded again anyway, and a matching size does not prove a
matching photo. A later photo pass was not adopted for JSJB: there it makes the same number of requests and would
show villages as checked before their photos are.

## One bad item does not stop the run

- Network errors, including truncated downloads, are retried with backoff. If the portal still
  fails, or answers with its start page or an error page (expired session), the collector starts
  a fresh session and walks the same dropdown path again, twice.
- A list that still cannot be read (the districts of a state, the blocks of a district, the
  villages of a block) is skipped and listed at the end. The run goes on with the next one.
- A village whose results page looks wrong is skipped, never saved in part and never written to
  the coverage file, so the next run tries it again. Wrong means: a row with the wrong number of
  cells, rows that do not add up to "Total", the same record id twice, or the same unreadable
  field in every row of the village.
- One unreadable cell does not cost the village. A date or coordinate that cannot be read is saved
  as `null`, with the portal text kept under `unreadable`; photo labels that do not match the photo
  links keep the links that were found. A row with no readable record id is left out and logged.
- The run stops only when the layout looks changed everywhere: `--max-layout-failures` villages
  or lists in a row looked wrong, the start page has no dropdowns when the run begins, or every
  block and village list read was empty. A start page or error page later in the run skips only
  that village or list. After 10 network failures in a row it pauses 5 minutes, up to 12 times,
  then stops. Pages over 64 MB, or slower than 3 times the timeout, count as network failures.
- A photo is stored only when its bytes are a real, whole image (JPEG, PNG, GIF, WebP, BMP, HEIC)
  and under the size cap. Empty bodies, HTML error pages, other files, images cut off before their
  end marker, redirect loops and timeouts are left out of the photo index and tried on the next
  run. A URL that failed is not tried twice in one run. Photo URLs that return 404 are recorded
  with `sha256: null` and not retried. A photo whose file was deleted is downloaded again.
  A village with a failed photo stays due under `--since-days`, so a rerun retries the photo.
- Files are written to a temporary file and swapped in, records first and coverage last, so a
  crash, Ctrl+C or kill leaves the previous or the new version. When another program (such as
  the checker) holds a file open and the swap is refused, the file is overwritten in place; the
  full new copy waits in `<file>.inplace` and an interrupted write is finished on the next start.
  On start, damaged lines in the
  `.jsonl` are skipped and copied to `<scheme>.jsonl.damaged-lines`; a damaged coverage file or
  photo index is moved aside and rebuilt from the records.
- At the end it always prints a summary (villages saved, villages and lists skipped with reasons,
  photos failed) and writes it to `data/state/last_run.json`.

- A page answered with 401 or 403, or an adapter raising `AccessDenied` (a login form, a CAPTCHA,
  a bot check), stops the run at once with exit code 8, and no more requests go to that host. A
  photo answered with 401 or 403 is only that photo failing.

Exit codes: 0 nothing skipped, 5 finished but something was skipped (rerun to retry it),
2 layout changed everywhere, 3 a filter name was not found, 4 portal unreachable,
6 local disk problem (disk full, no permission), 7 another collector of this scheme is using the
data folder, 8 the portal asks for a login, CAPTCHA or bot check, or refused access (401, 403),
130 stopped by Ctrl+C, 1 unexpected error.
Work saved before an early stop is kept.

## Adding a scheme

Add one file, `collector/schemes/<id>.py`. Every file there with a `SchemeAdapter` subclass that
has an `id` is registered when the collector starts; nothing else needs editing. A file that fails
to import is left out with a warning and the other schemes still run. Files starting with `_` are
not scanned. `tests/collector/demo_scheme.py` is a small working example.

Class attributes:

- `id` (lower case, also the file prefix in `data/`), `name`, `portal_url`.
- `filters`: filter names `list_areas` understands; each becomes a `--<name>` option and a key a
  plan unit may set. Default: state, district, area, block, village.
- `hosts`: `{"host": seconds}` or `{"host": {"delay": s, "timeout": s, "retries": n}}`. Unlisted
  hosts use `--delay` (default 1 s), 60 s timeout, 4 tries. A delay below `--delay` is raised to it.
- `supports_list_only`: `True` when each record carries what `list_photos` needs (put it under
  `photo_source`), so `--list-only` and `--photos` work. Default `False`.
- `plan_levels`: the filters that name a queue unit, in order (default: `filters`). `area_types`:
  only for a JSJB-style Rural/Urban split.

Methods:

- `list_areas(filters)`: yield `Area` objects and a `SkippedUnit` for each list that could not be
  read. `filters` has one key per name in `filters`; `None` means all. Raise `FilterNotFound` if a
  name matches nothing. Name an area by its level path:
  `Area(levels=[{"level": "state", "code": "26", "name": "Haryana"}, ...], code_system="NREGA", context=...)`.
  Levels named state, district, block and village fill those fields; the last level fills
  `village` and `village_code`. The coverage key is `<id>|<code_system>|<code>/<code>/...`. JSJB
  names its areas by the old fields and keeps its old keys.
- `list_records(area)`: return record dicts (below). Handle paging here. Never return a partial list.
- `list_photos(record)`: return photo entries: `{"url": ..., "uploaded_at": ISO or None}` or
  `{"data": bytes}`, each optionally with `lat`, `lon` (numbers), `taken_at`, `stage`, `label`
  (text). Request only photo paths the portal prints. With `supports_list_only`, this gets the
  record as saved, so it may fetch a detail page from `photo_source`.
- `list_districts(state)`: only if plans use `"district": "remaining"`.

Record fields: `scheme`, `record_id` (required), then any of state, district, block, village,
village_code, lat, lon, start_date, end_date, structure_type, status, created_by, source_url, and
the optional fields `parent_id` (work or road code), `stage`, `amount` (number) and `amount_kind`
(approved, spent, instalment), `group_key` and `group_kind` (the "office" the checks group by),
`beneficiary_ref`, `photos_state` (set `"none_on_portal"` when the portal has no photos for the
record; the runner sets the rest), `photo_source`, and `extra` (a dict the checker ignores). The
runner fills `photos`, `first_seen`, `last_seen` and `area_key`. Keys starting with `_` are working
data and are never saved. Never store names, father's or husband's names, job card or registration
numbers; if a check needs to match a number, store `self.ref(number)` as `beneficiary_ref`, a
salted hash (salt in `data/state/<id>.salt`).

Use `self.client.fetch(url, data=None)` for every page request (the runner downloads the photo
URLs you return); the client applies robots.txt, the per-host delay and retries. Raise `LayoutChanged`
when an expected table or field is missing or what was read does not add up (the runner skips
that area), `PortalChanged` only when the whole portal looks different, and `AccessDenied` on any
login form, CAPTCHA or bot check page (the run stops).

Checker settings go in `checker/schemes/<id>.json` (see `checker/README.md`); the scheme stays a
preview scheme, out of `site/data`, until that file says `"public": true`. Queue plans go in
`plans/<id>-<scope>.json`: `{"scheme": "<id>", "since_days": 30, "units": [{"phase": "...", "<level>": "..."}]}`,
where a level given as a list runs once per value.

Tests: `python tests/collector/test_jsjb_offline.py` (saved pages),
`python tests/collector/test_resilience.py` (fake portal with injected faults),
`python tests/collector/test_saving.py` (appends, journals, photo workers),
`python tests/collector/test_queue.py` (the queue), `python tests/collector/test_attacks.py` and
`python tests/collector/test_multi_scheme.py` (locks, shared photos, levels, hosts, registry,
list-only and photos pass, queues of other schemes). No network.
