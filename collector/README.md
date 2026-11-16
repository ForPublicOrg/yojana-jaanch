# collector

Downloads public records and photos from scheme portals. Python 3.12 standard library only, no AI.

    python -m collector jsjb --state Punjab --district Sangrur [--area Rural] [--block Sangrur] [--village Duggan]

Names match the portal dropdowns, ignoring case. A level you leave out means all of it.
Bounds: `--max-villages N`, `--max-minutes M`, `--since-days D` (skip villages collected in the last D days).
Other flags: `--delay` (seconds between requests, default 1), `--data-dir` (default `data`),
`--photo-workers N` (see Speed below).

To collect many areas in order over days, use the queue: `python run.py queue --plan
plans/jsjb-india.json` (see `docs/QUEUE.md`).

Refresh: run the same command again. Every village is re-read, new records are added, `last_seen`
is updated, and only photo URLs missing from `data/state/<scheme>.photo_index.json` are downloaded.
Progress is saved every 10 villages and on Ctrl+C, so a stopped run can be restarted.
Only one collector or queue writes to a data folder at a time (`data/state/collector.lock`); a
second one exits with code 7. The operating system releases the lock when the process ends.

Outputs: `data/records/<scheme>.jsonl`, `data/records/<scheme>.coverage.json`,
`data/photos/<sha256[:2]>/<sha256>.<ext>` (stored once per unique image).

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
matching photo. A later photo pass was not adopted: it makes the same number of requests and would
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

Exit codes: 0 nothing skipped, 5 finished but something was skipped (rerun to retry it),
2 layout changed everywhere, 3 a filter name was not found, 4 portal unreachable,
6 local disk problem (disk full, no permission), 7 another collector is using the data folder,
130 stopped by Ctrl+C, 1 unexpected error.
Work saved before an early stop is kept.

## Adding a scheme

Create `collector/schemes/<id>.py` with a subclass of `collector.base.SchemeAdapter`, then add it
to `SCHEMES` in `collector/schemes/__init__.py`. Set `id`, `name`, `portal_url` and implement:

- `list_areas(filters)`: yield `Area` objects (one per village or ward), and a
  `SkippedUnit` for each list that could not be read. `filters` has keys
  state, district, area, block, village; `None` means all. Put whatever you need for the
  next request in `Area.context`. Raise `FilterNotFound` if a name matches nothing.
- `list_records(area)`: return record dicts with the fields of data contract 1, except
  `photos`, `first_seen` and `last_seen`, which the runner fills in. Handle paging here.
- `list_photos(record)`: return `[{"url": ..., "uploaded_at": ISO or None}]`.

Use `self.client.fetch(url, data=None)` for every request; it applies robots.txt, the delay and
retries. Raise `LayoutChanged` when an expected table or field is missing, or when what was read
does not add up; the runner then skips that village. Raise `PortalChanged` only when the whole
portal looks different. Never return a partial list, because the runner replaces stored records
with it.

Tests: `python tests/collector/test_jsjb_offline.py` (saved pages),
`python tests/collector/test_resilience.py` (fake portal with injected faults),
`python tests/collector/test_saving.py` (appends, journals, photo workers),
`python tests/collector/test_queue.py` (the queue) and `python tests/collector/test_attacks.py`.
No network.
