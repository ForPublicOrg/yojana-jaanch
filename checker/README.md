# checker

Reads `data/records/<scheme>.jsonl` and `data/records/<scheme>.coverage.json`, then writes
the site data in `site/data/` and the case photos in `site/photos/`:

- `summary.json`: totals, counts by scheme and problem, coverage, the largest cases and the list of
  index files. The home and method pages need nothing else.
- `index/<state>.json`: the cases of one state, each with its first 3 record IDs and without its
  reasons, for the state, district and all-cases pages.
- `cases/<id>.json`: one whole case with every record, for its case page.
- `areas.json`: case counts by state and district, for the maps.
Python 3.12 standard library only. No AI is used unless you pass `--vision`.

```
python -m checker                      # all schemes found in data/records
python -m checker --scheme jsjb        # one scheme (the site data then holds only that scheme)
python -m checker --vision --vision-limit 20
python -m checker --root path/to/project
python -m checker --data-dir data/_trial/<id>   # cases in data/_trial/<id>/preview/, site/ untouched
```

Public schemes (`"public": true` in `checker/schemes/<id>.json`; jsjb is public) go to `site/data`.
Every other scheme is a preview scheme: its cases go to `data/preview/cases.<id>.json`, with each
case photo given as the original's path under `data/`, and never to `site/`. `--scheme <id>` for a
preview scheme leaves `site/data` alone.

Only one checker runs at a time per data folder (`data/state/checker.lock`); another waits up to
`--lock-wait` minutes (default 30), then exits with code 7. Output files are written to a
temporary file and renamed into place.

Each entry of `schemes` in `summary.json` lists `checks_run` for that scheme, and `checks_off` with
the reason a check did not run. A check is off for a scheme when its settings turn it off, or when
no record of the scheme has a field the check needs: `photo_reuse` needs photo sha256,
`same_gps` lat and lon, `same_day` start_date, end_date and structure_type, `upload_burst` an
office (`group_key`, else `created_by`) and photo upload times. Records with `photos_state`
`"not_fetched"` are left out of the photo checks. The top level `checks_run` lists the checks run
for at least one public scheme.

A line that is not valid JSON, a record without `record_id`, or a field of an unexpected type
(for example `photos` that is not a list, or `lat` that is text) is left out and counted; the count
and the first few line numbers are printed. Null fields and missing photo files are fine. A damaged
coverage file or vision cache is treated as empty. A check that fails is reported and the others
still run.

Run it again after the collector refreshes data. Case ids are built from what the case is about
(the photo hash, the village and GPS point, the village and office, the office and burst start
time), so a case keeps its id across runs while its records stay the same.

## Checks

Each check is one module. A case is never emitted from fewer than 2 records, except a
`vision_mismatch` case, which can be about one work. `summary.json` also has `checks_run`, the kinds
that ran this time, so the site can tell "not run" apart from "found nothing".

| Module | Kind | What it flags |
| --- | --- | --- |
| `photo_reuse.py` | `photo_reuse` | One photo attached to 2 or more record ids. One case per photo. A photo is one file (same SHA-256), or, once the phash step has run, several files that hold one picture (see "Same picture, different files" below). Once the phash step has run, a placeholder image (broken-image icon, plain graphic) on several works is not a case. Adds reasons when all its works share one GPS point, each start and end within a single day, the uploads fall within `photo_reuse.upload_minutes`, or more than one office made the entries. |
| `same_gps.py` | `same_gps` | `same_gps.min_records` or more works in one village with identical lat and lon. Works already in a photo_reuse case are left out. |
| `same_day.py` | `same_day` | `same_day.min_records` or more works in one village, entered by one office, whose start date equals the end date. Works already in a photo_reuse case are left out. |
| `upload_burst.py` | `upload_burst` | One office uploading photos for `upload_burst.min_records` or more different works, with that many different photos, inside `upload_burst.window_minutes`. |
| `vision.py` | `vision_mismatch` | Only with `--vision`. Works in one village and office whose photo a vision model judged as not showing the claimed structure type. Always `needs_human_review: true`. |

Severity is `severity_weight[kind] x record_count`, plus 1 for each supporting reason on a
photo_reuse case. Cases are sorted by severity, highest first.

## checker/schemes/<id>.json

One file per scheme, laid over `config.json`'s `schemes.<id>`. Keys, all optional: `name`,
`portal_url`, `public` (true to publish), `checks` (for example `{"same_day": false}`),
`extra_checks` (module names in `checker/extra/`, see `checker/extra/__init__.py`), and any of
`photo_reuse`, `same_gps`, `same_day`, `upload_burst`, `vision`, `severity_weight` to replace
`config.json` values for this scheme only, key by key. A file that cannot be read is reported and
ignored.

## config.json

JSON has no comments, so every key is described here.

- `schemes.<id>.name`, `schemes.<id>.portal_url`, `schemes.<id>.public`: display name, portal link and publish flag for the site. New schemes put these in `checker/schemes/<id>.json` instead. A scheme missing from both uses its id as the name and the first record's `source_url`.
- `photo_reuse.upload_minutes`: if every upload of a reused photo falls within this many minutes, the case says so. Default 30.
- `photo_reuse.near_max_distance`: most of the 256 dHash bits two files may differ in and still count as one picture. 0 turns the picture match off; the check then matches identical files only, as before. Default 8. Values above 16 turn the match off.
- `photo_reuse.near_max_mad`: most mean absolute difference between the two 32x32 greyscale copies, each scaled to mean 0 and standard deviation 1. Default 0.03. Values above 0.05 turn the match off.
- `photo_reuse.near_max_aspect_gap`: most relative difference of width / height, `abs(r1 - r2) / max(r1, r2)`. A landscape file never matches a portrait one. Default 0.01. Values above 0.05 turn the match off.
- `same_gps.min_records`: smallest group of works sharing one GPS point in a village. Default 5.
- `same_day.min_records`: smallest group of same-day works per village and office. Default 5.
- `same_day.ignore_structure_types`: structure types that can honestly finish in one day, skipped by this check. Matched without case. Default empty.
- `upload_burst.window_minutes`: length of the upload window. Default 10.
- `upload_burst.min_records`: smallest number of different works (and different photos) in one window. Default 10.
- `vision.limit`: most new model calls per run when `--vision-limit` is not given. Cached answers do not count. Default 50.
- `vision.timeout_seconds`: timeout for one model call. Default 60.
- `vision.max_retries`: retries after HTTP 429 or 5xx. The wait is the server's `Retry-After`, else `retry_base_seconds x 2^attempt`, capped at 120 seconds.
- `vision.retry_base_seconds`: base wait for the backoff above. Default 5.
- `vision.max_consecutive_failures`: after this many photos fail in a row, the run stops calling the model and keeps the answers it has. Default 3.
- `severity_weight.<kind>`: multiplier on record count for each kind.

## Vision (optional)

Any OpenAI-compatible chat completions endpoint that accepts images works. Set:

- `VISION_API_BASE`: base URL ending before `/chat/completions`
- `VISION_API_KEY`: API key (leave empty for local Ollama)
- `VISION_MODEL`: model name

Examples of base URLs: OpenRouter `https://openrouter.ai/api/v1`, Gemini
`https://generativelanguage.googleapis.com/v1beta/openai`, local Ollama `http://localhost:11434/v1`.
Pick a model on that service that accepts image input.

The checker makes one call per unique photo hash and asks whether the photo plausibly shows the
claimed structure type. Answers are stored in `data/state/vision_cache.json` under the key
`<model>|<sha256>`, so a rerun only calls the model for new photos. Without the env vars,
`--vision` prints a notice and the other checks run as normal.

## Photo files kept

After a check of every scheme, the checker deletes the files under `data/photos/` of every photo
that is not a case's evidence photo, public or preview (`checker/prune.py`). For a reused photo
that is the photo itself, plus every file of a picture saved as several files; for the other checks
it is the photo shown with the case. Their sha256 stays in the records and the photo index, and the
collector does not download them again. Files younger than 3 hours are left for the next check,
since their records may not be saved yet. If a later record reuses a deleted photo, that record's
copy is downloaded under its own URL, so the new case has a file.

Once `data/state/phash.jsonl` exists, a file whose sha256 has no line in it is never deleted, so
the phash step always reads a file before it can go. Without `phash.jsonl` files are deleted as
before, and the checker prints that near-duplicate matching needs the phash step. When
`phash.jsonl` exists but cannot be read (another program holds a lock on it, for example), or when
any check of any scheme failed this run, no file is deleted, and the log says why: in the first
case unhashed files cannot be told apart, in the second the failed check's evidence photos are not
known.

The cost: the vision check, and any later check that needs the image itself, sees only the kept
photos. To keep every file, run the checker with `--keep-all-photos`.

## Tests

```
python -m unittest discover -s tests/checker -v
```

`tests/checker/fixture.py` builds a small data set that follows the collector's data contract,
including a 23-record photo reuse group. `python tests/checker/fixture.py <dir>` writes it to a
folder so you can run `python -m checker --root <dir>` on it. The vision test uses a fake local
HTTP server that returns one rate limit response first; no real API is called.

`tests/checker/test_nearmatch.py` covers the picture match with hand-written `phash.jsonl` lines;
`tests/publisher/test_phash.py` (run with `.venv-publish`) covers the phash step on generated
images.

## Same picture, different files

The SHA-256 match only catches byte-identical files. The portal also holds one picture saved as
several files: re-saved, re-compressed at another JPEG quality, or resized. `photo_reuse` groups
those too, in two steps.

1. `python run.py phash` (needs `.venv-publish`; `publisher/phash.py`) decodes every file under
   `data/photos/` that has no line yet and appends one line per sha256 to `data/state/phash.jsonl`.
   It first cuts off plain borders: rows and columns of one colour along the edges, such as the
   black or white bars of a phone screenshot, and a status bar or button row that such a bar
   separates from the picture (`box`, in pixels of the whole image). Without this, two different
   shots shown in identical bars agree on every bar pixel and look closer than they are. On the
   part inside the borders it stores a 256-bit dHash (greyscale, resized to 17x16 with Lanczos, each
   bit says whether a pixel is brighter than its left neighbour), the width and height `w`, `h`, a
   32x32 greyscale copy (base64) and `flat`; `size` is the whole image and `levels` the number of
   grey levels in a 32x32 copy of the whole image. Lines have `"v": 2`; lines of version 1 (no
   border cut) are ignored, so those files are read again. A file whose bytes match its name but
   cannot be decoded gets an `error` line and never matches by picture: the portal's own file is
   broken, so a new download would fail the same way. A file whose bytes do not hash to its name
   (damage on this disk) gets no line; it is logged, prune keeps it, and deleting it by hand lets
   `--refetch-unhashed` download a good copy. A file that runs out of memory while decoding gets no
   line either and is read again next run. The file is only appended to; `--compact` rewrites it
   once without duplicate or broken lines. `--data-dir` works as for the checker, `--workers N` sets
   the number of decoding processes and `--limit N` the most new files per run. One phash run at a
   time (`data/state/phash.lock`).
2. The checker (`checker/nearmatch.py`, standard library) reads that file. Two files are one
   picture only when all of these hold: both have a hash and neither is low information; the
   dHashes differ in at most `near_max_distance` bits; the normalised 32x32 copies differ by at
   most `near_max_mad`; the width / height ratios differ by at most `near_max_aspect_gap`, with the
   same orientation; and `data/state/not_same.txt` does not list the pair. A file is low
   information when the part inside its borders is under 10% of the whole image or has a shorter
   side under 100 px, or its 32x32 copy has a standard deviation under 15, a mean difference
   between neighbouring pixels under 6 grey levels, more than 60% of neighbouring pixel pairs
   within 2 grey levels, or more than 20% of its area in plain bands of one colour along the edges
   (bars the border cut missed). Such files still match by SHA-256.

A file on 2 or more works whose image is a placeholder, not a photo of a site, is still a case once
`phash.jsonl` exists, with one more reason saying the file is a graphic of a given size and not a
photograph, and a `placeholder` field (`width`, `height`) the site uses to show it enlarged. A
placeholder is: fewer than 32 grey levels in the whole image (a drawn graphic such as a plain
coloured square or an "Error loading image" picture), or a shorter side under 100 px with fewer
than 64 levels (a broken-image icon), or its sha256 listed in `data/state/placeholders.txt` (one
per line, `#` starts a comment). The audit page lists these cases, and `photo_match` counts them
(`placeholder_cases`, `placeholder_records`). A real photo of bare ground is low information but
not a placeholder. Placeholders never join another file by picture. The rule applies even with `near_max_distance` 0, as long as
`phash.jsonl` exists.

Groups use complete linkage: every file in a group passes every test against every other file, so
A and C are never joined through a B that is close to both. Comparing each file with one
representative at half the limits would also bound every pair, but it keeps fewer true copies, and
the groups are a few files each, so checking every pair costs nothing. Candidate pairs come from a
multi-index: the hash is cut into `near_max_distance + 1` bands, and two hashes within that many
bits agree exactly on at least one band. EXIF orientation is not applied: a rotated copy then fails
the aspect test, which costs a match and never makes a wrong one.

A group of several files with 2 or more record ids is one case. Its id comes from the file attached
to the most records (ties: the smallest sha256), so an existing case keeps its id when its file is
the biggest member. Its evidence photo is that file, or the next file of the group that still
exists. The case gets `photo_files` (each file's sha256 and number of works) and `other_ids` (the
ids the other files would have had as cases of their own; the site opens this case for them). The
headline says "One picture, N different works", and the first reasons give the number of files, the
largest distance and pixel difference within the group, and each file's first 12 hex characters
with its number of works. A group of one file gives the same case and text as before. Severity is
unchanged.

Each scheme entry in `summary.json` has `photo_match`: whether the match ran (or why not), how many
photos had a hash, how many were too plain to compare, how many files were merged into how many
pictures, the largest distance and pixel difference used, and the limits. The log prints the same.

### Checking the merges by hand

Every check of all schemes (once `phash.jsonl` exists) writes `data/preview/near_matches.html`,
never under `site/`. A check of one scheme (`--scheme`) leaves the page as it is, since it would
drop the other schemes' pairs. The page lists every pair of different files counted as one
picture, most bits apart first, then largest pixel difference, with both files side by side (or
the stored 32x32 copy when a file is gone), the numbers and the case id, and at the end the
placeholder files that were not made cases. When a row shows two different photos, put its line
of two sha256 values into `data/state/not_same.txt`, one pair per line (`#` starts a comment). The
next check never puts those two files in one picture.

### Calibration

The defaults come from a calibration on the photos collected by 2026-11-17 (16,724 unique files).
All 189 pairs of files within 40 bits of each other were looked at one by one. Every pair up to 16
bits apart was the same picture; the first different photos were 17 bits apart (then 19). The
lowest pixel difference of two different photos was 0.057, for two shots of one pond taken a
minute apart and 19 bits apart, which is why both tests are needed. All 27 pairs from 4 to 8 bits
apart were the same picture. The limits 8 and 0.03 are about half of 17 and 0.057.

A later check on 22,251 files (2026-11-17) found two pairs 10 bits apart that look like two shots
of one pit, each stamped with a different village; their pixel differences were 0.16 and 0.23, so
the pixel test kept them apart. The distance limit of 8 is therefore only 2 bits under the closest
different photos seen so far, and the pixel test is what keeps such pairs apart. The lowest pixel
difference of two different photos was still 0.057. All 155 merged pairs of that check were the
same picture.

What the limits merge and what they miss. A copy whose date or GPS stamp was replaced, or that was
cut by a few pixels, is merged when both tests still pass; several such pairs are in the merges
below (for example two copies of one picture whose stamps give different coordinates and dates).
The multi-file case text names these as possible ways the copies differ. A copy cut down by more
than a few pixels, or with a stamp that covers more of the picture, is missed.

Border cut (phash version 2). A review showed that without it, two different shots of one site
shown in identical phone screenshot bars (two pond shots a minute apart, 19 bits and 0.057 apart
as plain files) came within 6 bits and 0.021 when each was centred in a black 720 x 1600 frame,
and were merged. With the border cut, three such pairs of different photos in 10 frames each
(black and white bars, picture 300 to 900 px wide, portrait and landscape frames, a status bar
across the top) gave no merge: 28 framed pairs stayed 10 to 47 bits and 0.057 to 0.119 apart, and
the other 2 were too plain to compare. The plain-band rule in the low-information
test catches bars the cut misses. `tests/publisher/test_phash.py` frames two shots of one made-up
scene and checks they are not merged.

The version 2 step was run over the 22,151 files present on 2026-11-17 (2,601 had a border cut;
many of them only lose a plain sky band). With the defaults, 160 pairs of different files were
merged, all JSJB (largest distance 8, largest pixel difference 0.0272); in the checker that is 279
JSJB files in 135 pictures. Compared with version 1 on the same files, 6 pairs were added (phone
screenshots now matched to their source picture) and 5 lost (one copy trimmed, the other not);
every added pair, every pair with a trimmed file (20), the 7 pairs with files new since version 1
and the 6 riskiest other pairs (distance 7 or more, or pixel difference 0.02 or more) were looked
at; each is one picture. No PMGSY files were merged.

Placeholders. In the same run 8 files had fewer than 32 grey levels: 4 drawn graphics (plain
orange squares, an "Error loading image" picture) and 4 near-black or plain map tiles. Files from
32 to 71 levels include real photos (bare ground, a road, a milestone). The two 73 x 73
broken-image icons had 46 and 47 levels, while real photos under 100 px a side had 125 or more.
With these rules 7 JSJB files on 630 records are no longer cases: the broken-image icons (285 and
68 records), the orange squares (130, 96 and 34), the error picture (15) and one near-black map
tile (2).

### Order of the steps

Run `python run.py phash` before `python run.py check`, every time, so new files are hashed before
the check can delete them. Photos deleted before the first phash run have no file left to read:
`python run.py collect --scheme jsjb --refetch-unhashed` downloads them again (see
`collector/README.md`); then run phash and the check.
