# The collection queue

`plans/jsjb-india.json` lists the areas to collect, in the owner's order:

1. `punjab`: all of rural Punjab, then all of urban Punjab.
2. `capitals`: for each of the 28 states and 8 union territories, the district that holds its capital,
   urban then rural.
3. `rest`: every other district, state by state in the portal's order, rural then urban.

Start it from the project folder, in your own terminal:

    python run.py queue --plan plans/jsjb-india.json

It runs until the plan is done. Ctrl+C stops it; the same command goes on from where it stopped.
Only one collector or queue per scheme can write to `data/` at a time. A second JSJB one refuses to
start (exit 7), so stop any JSJB `run.py refresh` or `run.py collect` that is still running first.
A queue of another scheme can run at the same time (see "Other schemes" below).

To keep a log as well as seeing it on screen (PowerShell):

    python run.py queue --plan plans/jsjb-india.json 2>&1 | Tee-Object -Append data\queue.log

## Options

- `--dry-run`: print the full order and what is already done. Collects nothing and writes nothing,
  not even when `data/state/queue.json` is damaged. The first time it reads the district list of
  each state from the portal (about 37 requests).
- `--only-phase capitals` (repeatable): run only that phase. Areas from earlier phases are still left
  out of `rest`.
- `--max-minutes M`: stop after M minutes, between villages; the same command resumes. A wait for
  the portal to come back that would run past M minutes is not started; the queue stops instead.
- `--since-days D` (default: the plan's `since_days`, 30): an area finished within D days is skipped,
  and inside an area every village collected within D days is skipped without a request. An area
  finished more than D days ago is due for a refresh, but refreshes come last: see "Order" below.
- `--photo-workers N|auto` (default 1, N up to 16) and `--photo-max N` (default 12): see "Speed"
  in `collector/README.md`.
- `--no-check`: do not rebuild the website data after each area.
- `--delay`, `--data-dir`: as for the collector.

## Order

Each time it starts, the queue takes the areas that are not finished within `--since-days` and
puts them in three groups, each in plan order:

1. Areas never finished: not started, or stopped part way (Ctrl+C, `--max-minutes`, a crash).
2. Areas that failed or stayed incomplete last time.
3. Areas finished more than `--since-days` ago, due for a refresh.

A pass of India takes far longer than 30 days, so without this a restart after day 30 would go back
to Punjab and the early states and might never reach the later ones. Now every area is collected
once before any area is refreshed.

## What it does with each area

- Runs the collector on it (state, district, area type), in the same process, so records are
  loaded once.
- Then runs the checker, so `site/data/` is updated after every area that saved a village.
- Keeps its progress in `data/state/queue.json`: status, attempts, start and finish time, villages,
  records and the last error of every area, and the district lists it read (kept 7 days). If a
  write of that file was cut off while another program held it open, the full copy waiting in
  `queue.json.inplace` is put back on the next start.
- An area that fails (portal down, a name not found, an unexpected error) is recorded and the queue
  moves on. It is tried once more at the end of the pass. An area where some villages, lists or
  photos were skipped is also tried again at the end of the pass. If something is still skipped
  after that, the area stays `incomplete`, not done, and is listed in the summary. The next run
  visits it again; villages already saved are skipped without a request, so only what is missing
  is fetched.
- After the portal could not be reached for a whole area (the collector already waits up to an hour
  inside it), the queue waits 30 minutes before the next area, doubling each time it happens again in
  a row, up to 4 hours. There is no wait after the last area.
- An area for which the portal lists no towns or villages is counted as done. Ten such areas in a
  row stop the queue (exit 2), as do two areas in a row whose pages look different: both mean the
  portal has probably changed.
- It stops for disk trouble (exit 6) and Ctrl+C (exit 130). It ends with 0 when everything is done,
  or 5 when something failed or is still skipped: run it again to retry.
- A summary at the end lists areas finished, areas failed with their reason, and progress through
  the plan.

## Capitals

The capital districts were matched to the portal's own district names, read on 17 November 2026.
Every capital matched a portal district. These choices are judgement calls; edit the plan to change
them:

- Punjab and Haryana: Chandigarh is a union territory, so their capital is the `CHANDIGARH` >
  `Chandigarh` area. It is listed for Punjab, Haryana and Chandigarh and runs once.
- Assam: Dispur and Guwahati are in the district the portal calls `Kamrup Metro`.
- Andhra Pradesh: Amaravati, in `Guntur`. Odisha: Bhubaneswar, in `Khordha`. Goa: Panaji, in
  `North Goa`. Arunachal Pradesh: Itanagar, in `Papum Pare`. Meghalaya: Shillong, in
  `East Khasi Hills`. Tripura: Agartala, in `West Tripura`. Karnataka: `Bengaluru Urban`.
  Andaman and Nicobar: Sri Vijaya Puram (Port Blair), in `South Andamans`. Ladakh: Leh, in
  `Leh Ladakh`. Lakshadweep: Kavaratti, in `Lakshadweep District`. Dadra and Nagar Haveli and
  Daman and Diu: `Daman`.
- Jammu and Kashmir: both `Srinagar` (summer) and `Jammu` (winter) are queued.
- Delhi: all of Delhi is the capital territory; `New Delhi` district is queued. The other nine
  districts come in phase 3.
- Not queued as capitals (they come in phase 3): Dharamshala (`Kangra`, Himachal Pradesh winter
  capital), Gairsain (`Chamoli`, Uttarakhand summer capital), Nagpur (Maharashtra), `Mumbai Suburban`,
  `Imphal East`, `Kargil` (Ladakh), and the parts of Hyderabad in `Ranga Reddy` and
  `Medchal Malkajgiri`.

Resolved with the district lists read that day, the plan is 1506 areas: 2 for Punjab, 70 for
capitals and 1434 for the remaining districts.

## Other schemes

A plan with `"scheme": "<id>"` runs that scheme's adapter. Its units set some of the adapter's
`plan_levels` (default: its filters) besides `phase` and `note`; a level given as a list runs once
per value, in order. Progress goes to `data/state/queue.<plan file name>.json`, so it never mixes
with `queue.json`. `"list_only": true` in the plan, or `--list-only`, saves records without photos
for adapters that support it. A login, CAPTCHA or 401/403 page stops the queue with exit 8.

## Plan file

    {"scheme": "jsjb", "since_days": 30, "units": [
      {"phase": "punjab", "state": "PUNJAB", "area": "Rural", "note": "..."},
      {"phase": "capitals", "state": "GOA", "district": "North Goa", "area": ["Urban", "Rural"]},
      {"phase": "rest", "state": "GOA", "district": "remaining", "area": ["Rural", "Urban"]}]}

`area` is `Rural`, `Urban`, `both` (rural then urban) or a list giving the order; leaving it out
means both. No `district` means the whole state. `"district": "remaining"` stands for every district
of the state, in portal order, that no earlier line covers. Names are matched without regard to case.
