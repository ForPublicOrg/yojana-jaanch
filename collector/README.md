# collector

Downloads public records and photos from scheme portals. Python 3.12 standard library only, no AI.

    python -m collector jsjb --state Punjab --district Sangrur [--area Rural] [--block Sangrur] [--village Duggan]

Names match the portal dropdowns, ignoring case. A level you leave out means all of it.
Bounds: `--max-villages N`, `--max-minutes M`, `--since-days D` (skip villages collected in the last D days).
Other flags: `--delay` (seconds between requests, default 1), `--data-dir` (default `data`),
`--photo-workers N|auto` and `--photo-max N` (see Speed below), `--log FILE` (default `<data-dir>/logs/<scheme>.log`, `--log ""`
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

## Download again photos deleted before the phash step

    python run.py collect --scheme jsjb --refetch-unhashed [--max-records N] [--max-minutes M] [--photo-workers 3]

The checker deletes photo files that are in no case (`checker/prune.py`). Photos deleted before the
first `python run.py phash` run were never decoded, so the near-duplicate match cannot compare
them. This mode (`collector/refetch.py`) reads the saved records, read only, and for every photo
whose sha256 has neither a file under `data/photos/` nor a line in `data/state/phash.jsonl`,
downloads it again from a URL it was saved under. Bytes that hash to the stored sha256 are stored
as `data/photos/<sha256[:2]>/<sha256>.<ext>`, with the extension the records give that file (some
say `.jpeg`), so the file is back exactly where the records point; only when no record names a
file is the extension taken from the bytes. A line in `phash.jsonl` counts as hashed when the
checker reads it (`checker/nearmatch.py` `parse_line`), so lines of an older version do not stop a
download. A URL that now serves another file is logged and
counted, nothing is stored, and the next URL saved for that sha256 is tried (at most 3 per run). A
404 is counted as missing. `--max-records N` stops after N photos, the photo on the most works
first. Nothing else is written: no record, coverage line, photo index entry or run summary. The
log goes to `data/logs/<scheme>.refetch.log`.

It refuses to start when `data/state/phash.jsonl` does not exist, because the checker would delete
the files again. Order: `python run.py phash`, then this, then `python run.py phash` and
`python run.py check`.

It can run while the normal queue of the same scheme runs. It does not take
`data/state/collector.<scheme>.lock` (or `collector.lock` for jsjb), since it writes none of the
files those locks protect; photo files go through the same shared write as every collector (see
"Several schemes at once"). It takes `data/state/refetch.<scheme>.lock`, so two refetch runs do
not overlap. Both processes then ask the same host for photos, each with its own throttle and ban
guard, so their load adds up. That is why this mode defaults to `--photo-workers 3`: at most 3
photo requests start a second, fewer when the portal has trouble. Every request goes through the
same Client, photo throttle, ban guard and photo pool as a normal run, and it stops for the same
reasons: a ban guard stop (exit 4, or 8 for a refusal), `--max-minutes`, disk trouble (exit 6) or
Ctrl+C (exit 130). A photo that failed is asked for again on the next run (exit 5).

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

Photos are most of the requests. Measured on the live JSJB portal: a village costs one search
postback plus 7.7 photos on average, photos are about 90% of the requests, and pages answer in
0.3 to 0.5 s. The photos are plain files on the same host as the pages.

`--photo-workers` sets how photos are downloaded:

- `1` (default): one at a time in the main thread, each after the `--delay` gap, before the next
  village is read. This is how the collector has always worked.
- `N` from 2 to 16: photos download in background threads while the main thread reads the next
  villages. At most N photo requests start per second, evenly spaced, with at most N in flight.
  The rate drops when the portal has trouble (below) and climbs back, never above N.
- `auto`: as N, starting at 4 a second and rising by 1 while the portal keeps up, up to
  `--photo-max` (default 12, at most 16).

Page requests stay one at a time with the 1 s delay between them, in the main thread. Once a host
has served a photo, each page request to it also takes one of its photo slots, ahead of waiting
photos, so pages and photos together start at most the photo rate per second (on JSJB, where
both come from one host, the rate is the total). Villages are still saved one by one in the order
they were listed, each as soon as all its photos are done, with the same log line. At most 3
villages are listed and not yet saved; then the main thread waits. A photo URL that two of those
villages share is downloaded once. At the end of a run, at `--max-villages` or `--max-minutes`,
and at a layout, network, 401/403 page or ban guard stop, the listed villages are saved first.
After Ctrl+C, a login or CAPTCHA page, or a disk problem they are not saved and stay due.
The bytes are checked, hashed and stored in the main thread exactly as with 1, so the records and
photo files are the same (tested against a run with 1). `--photos` passes work the same way, per
record. The queue (`python run.py queue`) takes the same two options.

How the rate moves (`collector/throttle.py`, one rate per host, every try of every photo counts):

- Up by 1 after 40 photos at the current rate that had to wait for their slot, with no trouble in
  between, when the median time of the last 20 photos is at most 1.5 times the median of the
  first 20 photos to that host, and at least 120 photos after the last step down. Photos that found a free slot do not count (nor do
  they reset the count): the rate climbs only while photos actually queue for it, so it does not
  rise past what the portal has been sent.
- Halved (never below 1) on trouble: HTTP 429 or 503, a timeout, a reset or refused connection,
  another 5xx, a cut-off file, a 401 or 403, or an HTML page instead of an image. A page request
  answered 429 or 503, or failing, halves it too. A Retry-After header, on a photo or a page,
  holds every request to that host for that long (at most 15 minutes). One exception: a lone
  failed request (timeout, reset, cut-off file) after 50 or more good photos only steps the rate
  down by 1. On JSJB about 1 in 2,000 large photos arrived cut off with no other sign of trouble,
  and halving for each one, with the rule below, had walked the top down from 8 to 2 in two hours.
- Down by 1 when the median time of the last 20 photos is over twice the median of the first 20.
  The time is until the portal's response headers arrive, not the whole download, so a large photo
  or this computer's own busy connection does not count as a slow portal.
- A rate that had to step down for trouble twice within 2,000 good photos is not used for a while:
  the top becomes one below it, and rises by 1 again after 3,000 good photos in a row, never above
  the top you gave. A lone failure and a slow median do not lower the top.
- Neutral: 404, 410, a redirect to a not-found page (JSJB sends missing photos to
  PageNotFound.aspx), the PMGSY image host's error page. After 60 of these in a row with no good
  photo or normally answered search page in between they are no longer believed (a firewall or an
  overloaded host can answer that way): each further one counts as trouble and the photo as
  failed, not missing, so the next run asks again. A village whose photos were never uploaded
  gives a long run; the next search page that loads normally resets the count.
- Under the queue (`python run.py queue`), the rate, the first-20 median and a lowered top carry
  over from area to area for as long as the queue process runs. Only a ban guard stop and the
  pause count are cleared for each area.

Ban guard, on with every setting including 1. Five troubled photo requests in a row (a neutral
answer neither breaks nor adds to the streak, a good photo ends it), or 6 troubled among the last
20 photo answers that were good or troubled, stop all requests to that host, pages included, for
10 minutes, and the rate restarts at 1. If the first 3 photo requests after a pause are all
trouble, it pauses again. Trouble after the third pause in a row stops the run as a network stop
(exit 4; the queue waits 30 minutes or more before the next area, which starts again at 1 a
second). When most of the trouble that tripped the guard is 401 or 403, the run stops at once as
a refusal (exit 8; the queue stops). Photos the guard stopped count as failed, never as missing,
so the next run asks for them again. During the collector's own outage pause (10 page failures in
a row) the photo workers wait as well.

What the log shows:

    photos to jsjb.mowr.gov.in: 5 a second (40 photos waited for a slot without trouble, median 0.42 s)
    photos to jsjb.mowr.gov.in: 2 a second (HTTP 429)
    photos to jsjb.mowr.gov.in: 5 failed in a row (error 5); no requests to jsjb.mowr.gov.in for 10 minutes, then 1 a second (pause 1 of 3)

At the end of each run (and every 5 minutes with a number above 1 or `auto`) a line gives the current rate, its top, and the counts
of ok, missing, throttled, error, refused and not image answers since the process started.

To force a fixed rate, give a number: `--photo-workers 6` never goes above 6 a second (it still
steps down on trouble). `--photo-workers 1` keeps the old pace. A HEAD request was not adopted: a
known photo URL is never downloaded again anyway, and a matching size does not prove a matching
photo. A later photo pass was not adopted for JSJB: there it makes the same number of requests and
would show villages as checked before their photos are.

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
  with `sha256: null` and not retried. A photo whose file was deleted keeps its sha256 and is not
  downloaded again: the checker keeps only the files of case evidence photos (`checker/prune.py`).
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
  photo answered with 401 or 403 is only that photo failing; a streak of them stops the run (see
  the ban guard under Speed).

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
`python tests/collector/test_photo_speed.py` (photo rate, ban guard, photo pool),
`python tests/collector/test_queue.py` (the queue), `python tests/collector/test_attacks.py` and
`python tests/collector/test_multi_scheme.py` (locks, shared photos, levels, hosts, registry,
list-only and photos pass, queues of other schemes). No network.
