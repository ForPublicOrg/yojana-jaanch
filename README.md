# Yojana Jaanch (योजना जाँच)

Checking the evidence behind government scheme records.

Downloads the public work records and evidence photos from Indian government scheme portals,
flags records whose evidence looks reused or copied, and shows the flags on a static map site.
The first scheme is JSJB (Jal Sanchay Jan Bhagidari, https://jsjb.mowr.gov.in/PublicDashboardJsjb.aspx).

Python 3.12, standard library only. No AI is used unless you pass `--vision`. The optional
publish step (below) is the one part that needs extra packages, kept in their own virtual environment.

A fresh copy of this repository has no data: `data/`, `site/data/`, `site/photos/` and `publish/` are
not in it. Run a refresh (next section) before the site or the publish step has anything to show.

## Refresh with new data

    python run.py refresh --scheme jsjb --state Punjab --district Sangrur --area Rural --block Sangrur

This collects (only new records and new photo URLs are downloaded), then rebuilds `site/data/`.
Run the same command again whenever you want fresh data. Other subcommands:

    python run.py collect --scheme jsjb --state Punjab --district Sangrur --village Duggan
    python run.py check            # rebuild cases from data/ without touching the portal
    python run.py serve --port 8000  # then open http://localhost:8000/

Useful collector options: `--max-minutes 25`, `--max-villages N`, `--since-days 7` (skip villages
read in the last 7 days), `--delay 1` (seconds between requests). A stopped run keeps what it saved;
run it again to continue. The site only claims the villages listed in `data/records/jsjb.coverage.json`.

## Add a state or district

Change the filters. Names match the portal dropdowns, ignoring case; a level you leave out means all of it.

    python run.py refresh --state Punjab --district Barnala --max-minutes 60
    python run.py refresh --state Haryana --since-days 7

## Vision check (optional, off by default)

Any OpenAI-compatible chat endpoint that accepts images works. Set three env vars, then add `--vision`:

- `VISION_API_BASE`: URL before `/chat/completions`, for example `https://openrouter.ai/api/v1`
  or a local Ollama at `http://localhost:11434/v1`
- `VISION_MODEL`: a vision model id, for example a free OpenRouter model or `llava` on Ollama
- `VISION_API_KEY`: the key, if the endpoint needs one

      python run.py refresh --state Punjab --district Sangrur --vision --vision-limit 20

Answers are cached, so a photo is sent once. Vision flags are marked for human review.

## Schedule the refresh

Windows Task Scheduler (weekly, Monday 03:00; use your own folder path):

    schtasks /Create /SC WEEKLY /D MON /ST 03:00 /TN SchemeRefresh /TR "cmd /c cd /d C:\path\to\project && python run.py refresh --state Punjab --district Sangrur --since-days 6 >> refresh.log 2>&1"

cron (Linux or macOS):

    0 3 * * 1  cd /path/to/project && python3 run.py refresh --state Punjab --district Sangrur --since-days 6 >> refresh.log 2>&1

## Add a new scheme

1. Write `collector/schemes/<id>.py`, a subclass of `collector.base.SchemeAdapter`. It is found
   by its file; no other file needs editing. `collector/README.md` lists the attributes, methods
   and record fields; `tests/collector/demo_scheme.py` is a small example.
2. Write `checker/schemes/<id>.json` with the scheme's `name` and `portal_url`. Its cases go to
   `data/preview/cases.<id>.json`, not to the site, until that file also says `"public": true`.
3. Try it in a scratch folder first:
   `python run.py collect --scheme <id> --data-dir data/_trial/<id> --max-minutes 10 ...`, then
   `python run.py check --data-dir data/_trial/<id>`.
4. Write a plan `plans/<id>-<scope>.json` and run `python run.py queue --plan plans/<id>-<scope>.json`.

## Run a second scheme next to JSJB

Each scheme has its own lock and its own files in `data/`, so queues of different schemes run at
the same time, each in its own terminal, each at its own portal's pace:

    python run.py queue --plan plans/jsjb-india.json
    python run.py queue --plan plans/<id>-<scope>.json

A second run of the same scheme is refused (exit 7). Each queue also writes its log to
`data/logs/<id>.log`. When both queues rebuild the site data at once, the second checker waits for
the first.

For portals that list records in bulk, save the records first and fetch photos later:

    python run.py collect --scheme <id> --list-only ...
    python run.py collect --scheme <id> --photos

Records waiting for photos are left out of the photo checks until the photos pass has fetched them.

## Publishing photos

`site/photos/` holds the original evidence photos, unblurred, with their metadata. They show private
citizens, so they stay on your computer. The checks compare exact bytes, so the originals are never
changed; the publish step writes separate public copies instead.

Set up once (needs OpenCV, numpy and onnxruntime; the collector and checker still need nothing extra):

    python -m venv .venv-publish
    .venv-publish\Scripts\pip install -r requirements-publish.txt      (Linux or macOS: .venv-publish/bin/pip)

Then, after each refresh:

    python run.py publish

For every photo used by a case, this finds faces and heads, blurs only those, removes all metadata
(EXIF, GPS), shrinks the photo to at most 1600 pixels on the long side, and saves it as
`publish/photos/<sha256>.jpg`, named by the original's sha256 so the site can match it. The rest of the
photo stays sharp, because it is the evidence. Photos already done are skipped; changing the detector
settings or models makes it redo all of them.

Two engines run on every photo, on the whole photo and on overlapping 800 pixel tiles so small, far
away people are found too:

- a face detector, YOLOv12m-face, for faces seen from the front or the side;
- a pose model, YOLO11x-pose, which finds each person's nose, eyes and ears. A head box is placed on
  them, so a person bending over, turned away, or wearing a turban or scarf still has the head blurred.
  The body is never blurred.

Small, far away people and small faces get a second look: the engines run again on a 320 pixel
window around each one, which shows them at twice the size.

Each face or head is covered by an ellipse with a small margin, pixelated and smoothed so the person
cannot be recognised. Some weak finds are left sharp, because they are almost always not a face:

- in the bottom third of the photo, where the GPS stamp and its map inset sit, a weak face or a head
  from the pose model is ignored unless there is a clear face or a clearly found person with a body
  under it;
- a large head from a weakly found person, with no face seen there, is usually a shadow or soil;
- a small weak face with no person and no other face near it is usually a letter of a caption;
- a weak face cut off by the photo edge, with no person there, or a weak face with a very tall or
  very wide box, is usually a hand or cloth;
- weak face boxes larger than most of the photo.

A head seen from behind is still blurred: the pose model cannot reliably tell it from a face turned
down, so this can blur the back of a head or a pan carried on it.

`publish/` must hold only the public copies and the hand files. If the publish step finds any other
image in it (for example test crops of the originals), it prints a warning with the file names; move
those files out, because anyone copying the folder would copy them too.

Both model files (about 80 MB and 235 MB) are downloaded once from the projects' GitHub releases into
`publisher/models/` and checked against the sha256 in `publisher/models/SOURCE.md` every time they are
loaded. If a model is missing and cannot be downloaded, or fails the check, the step stops and changes
nothing. Both models are AGPL-3.0; `SOURCE.md` explains what that allows.

How the engines were chosen: 60 case photos with people (bending, side-on, turbans, scarves, small and
far away, groups) and 10 without were labelled by hand, 120 visible faces and 57 heads whose face was
turned away or covered. Results on that set:

| Engine | Faces found | Heads only found | Boxes on no face |
|---|---|---|---|
| Old: YuNet plus Haar cascades | 88 of 120 | 8 of 57 | 109 |
| SCRFD-10GF (insightface), tiled, score 0.5 | 88 of 120 | 9 of 57 | 4 |
| MediaPipe BlazeFace full range, score 0.4 | 36 of 120 | 2 of 57 | 13 |
| MediaPipe Pose heavy, head from landmarks | 25 of 120 | 2 of 57 | 4 |
| YOLOv12m-face, tiled, score 0.3 | 104 of 120 | 16 of 57 | 2 |
| YOLO11x-pose, head from keypoints | 113 of 120 | 43 of 57 | 3 |
| Used now: YOLOv12m-face plus YOLO11x-pose | 119 of 120 | 45 of 57 | 6 |

SCRFD is also licensed for non-commercial research only. The heads still missed are mostly people seen
from behind at the edge of the photo or half hidden by others.

The engines can still miss a face. No copy is ready to upload until a person has looked at it:

1. Open `publish/review.html` in a browser. Copies waiting for a check come first. Each outline shows
   where the copy was blurred and which engine found it: a solid ellipse for the face engine, a dashed
   one for a head placed by the pose model, a blue box for your own boxes. An outline does not mean the
   rest of the photo is safe.
2. If a face is still recognisable, either add the photo's sha256 to `publish/hide.txt` (one per line),
   or click the face's top-left and bottom-right corners on the review page and paste the line it
   prints into `publish/extra_blur.json`, for example `{"<sha256>": [[x, y, width, height]]}`, in
   pixels of the public copy.
3. If no face can be recognised, tick "No recognisable face" under the photo and paste the lines from
   the box at the top of the page into `publish/approved.txt`. Each line holds a check of the blurred
   copy, so a copy that is blurred again must be approved again.
4. Run `python run.py publish` again. It copies only approved copies into `publish/upload/`. The hand
   files are kept between runs.

`publish/settings.json` holds one setting, `auto_approve_no_person`, which is `false`. Set it to `true`
to let the publish step approve, without a check, copies in which neither engine found any face or
person, even at a low score. It writes those lines into `approved.txt` marked
`# auto: no person found by any engine` and removes them again when you set it back to `false`. The
review page lists them as approved automatically. A person the engines missed completely would then go
out unchecked, so leave it off unless you accept that.

Put the photos on Google Drive by hand:

1. Make a folder in your Google Drive sync folder on this computer (Google Drive for desktop), for
   example `G:\My Drive\yojana-jaanch-photos`, and copy the files from `publish/upload/` into it. Copy
   only that folder's contents, never `publish/photos/`, `site/photos/` or `data/`. Wait until Drive
   has finished syncing.
2. In Google Drive, share the folder: Share, General access, "Anyone with the link", Viewer.
3. Copy the folder id from its address, `https://drive.google.com/drive/folders/<folder id>`.

Make the links file. No API key is needed:

    python run.py drive-links --folder-id <folder id> --local-folder "G:\My Drive\yojana-jaanch-photos"

This reads the folder's public page and writes `site/data/photo_links.json`. The page gives no
checksums, so each file is checked against the file of the same name in `--local-folder`. A file
gets a link only when its name matches an approved copy that is not in `hide.txt` and the local file
is byte for byte the same as that copy. If the page lists a different number of files than the local
folder holds, the command stops and writes nothing: the page may be cut short, or Drive may still be
syncing. Wait and run it again, or use an API key as below. If the folder is not public, or Google
changes the page so it cannot be read, the command says so and writes nothing.

With an API key instead: get a Google Cloud API key with the Google Drive API enabled (it can only
read public files) and run

    set GOOGLE_API_KEY=<your key>                (PowerShell: $env:GOOGLE_API_KEY="<your key>")
    python run.py drive-links --folder-id <folder id>

When `GOOGLE_API_KEY` is set this mode is used and `--local-folder` is not needed: it lists the folder
through the Drive API and checks the checksum Drive reports for each file against the approved copy.

Both ways are read only: they never upload, share or change anything in Drive or in the local folder.
Every file that does not get a link (a hidden or unapproved photo, an original, a changed file, a
subfolder, anything else) is listed with the reason and the command exits with code 4: anyone with
the folder link can still open those files, so delete them from the Drive folder. Adding a photo to
`hide.txt` does not remove it from Drive. When `photo_links.json` exists the site shows photos from
Drive, and a case whose photo is not in it shows "Photo withheld". Without the file the site uses the
local `site/photos/`, which is only for previewing on your own computer. Run `drive-links` again after
adding new photos.

## Tests

    python tests/collector/test_jsjb_offline.py      (and the other tests/collector/test_*.py files)
    python -m unittest discover -s tests/checker
    python -I tests/site/test_site.py
    .venv-publish\Scripts\python -m unittest discover -s tests/publisher

## Known limits

- Flags are leads for a person to check, not proof of fraud. Each case lists the records so anyone
  can look them up on the portal.
- Reuse is found only for byte-identical photos. Perceptual hashing (resized or recompressed copies)
  is not built yet.
- Without `site/data/photo_links.json` the site shows the original photos as published on the
  portal. Only publish with the blurred copies from `python run.py publish`, after checking them.
- Face blurring is automatic and misses some faces; the review page is there to catch them.
- Photo URLs that return 404 on the portal cannot be checked.
- If the portal page changes shape (photo links, column count, or a "Total" that does not match the
  rows read), the collector stops with exit code 2 instead of saving that village. Fix the adapter, then rerun.
- The map marks a state or district "Partly checked" when only some of its blocks were collected.
- With `run.py check --scheme <id>`, `cases.json` holds only that scheme.
