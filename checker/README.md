# checker

Reads `data/records/<scheme>.jsonl` and `data/records/<scheme>.coverage.json`, then writes
`site/data/cases.json`, `site/data/areas.json` and the case photos in `site/photos/`.
Python 3.12 standard library only. No AI is used unless you pass `--vision`.

```
python -m checker                      # all schemes found in data/records
python -m checker --scheme jsjb        # one scheme (cases.json then holds only that scheme)
python -m checker --vision --vision-limit 20
python -m checker --root path/to/project
```

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
`vision_mismatch` case, which can be about one work. `cases.json` also has `checks_run`, the kinds
that ran this time, so the site can tell "not run" apart from "found nothing".

| Module | Kind | What it flags |
| --- | --- | --- |
| `photo_reuse.py` | `photo_reuse` | One photo file (same SHA-256) attached to 2 or more record ids. One case per photo. Adds reasons when all its works share one GPS point, each start and end within a single day, the uploads fall within `photo_reuse.upload_minutes`, or more than one office made the entries. |
| `same_gps.py` | `same_gps` | `same_gps.min_records` or more works in one village with identical lat and lon. Works already in a photo_reuse case are left out. |
| `same_day.py` | `same_day` | `same_day.min_records` or more works in one village, entered by one office, whose start date equals the end date. Works already in a photo_reuse case are left out. |
| `upload_burst.py` | `upload_burst` | One office uploading photos for `upload_burst.min_records` or more different works, with that many different photos, inside `upload_burst.window_minutes`. |
| `vision.py` | `vision_mismatch` | Only with `--vision`. Works in one village and office whose photo a vision model judged as not showing the claimed structure type. Always `needs_human_review: true`. |

Severity is `severity_weight[kind] x record_count`, plus 1 for each supporting reason on a
photo_reuse case. Cases are sorted by severity, highest first.

## config.json

JSON has no comments, so every key is described here.

- `schemes.<id>.name`, `schemes.<id>.portal_url`: display name and portal link for the site. A scheme missing here uses its id as the name and the first record's `source_url`.
- `photo_reuse.upload_minutes`: if every upload of a reused photo falls within this many minutes, the case says so. Default 30.
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

## Tests

```
python -m unittest discover -s tests/checker -v
```

`tests/checker/fixture.py` builds a small data set that follows the collector's data contract,
including a 23-record photo reuse group. `python tests/checker/fixture.py <dir>` writes it to a
folder so you can run `python -m checker --root <dir>` on it. The vision test uses a fake local
HTTP server that returns one rate limit response first; no real API is called.

## Next check to add

Perceptual hashing, to catch the same photo after resizing, recompression or a small crop.
The SHA-256 match only catches byte-identical files. It needs an image decoder (Pillow or similar),
which is outside the standard library, so it is not built yet.
