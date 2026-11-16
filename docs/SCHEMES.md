# Adding more schemes

Plan for taking the JSJB pipeline to other scheme portals. Everything here comes from scout visits
in November 2026, each a few dozen requests. Anything marked unverified was not seen working.
Numbers are the scouts' own measurements; none are estimates unless they say so.

## 1. Which schemes, in order

| Rank | Scheme and portal | Feasibility | Effort | What it would expose | What blocks it |
|---|---|---|---|---|---|
| 1 | eGramSwaraj geotagged panchayat works (egramswaraj.gov.in) | high | small | Same photo on several works, stage photos out of order (completion uploaded before plinth), indoor photos for physical works, one GP's works all photographed near one point | Nothing seen. Photo endpoint takes a raw server path, so request only paths the page prints |
| 2 | Mission Amrit Sarovar (amritsarovar.gov.in) | high | medium | Same photo under two filenames (already seen on one pond), event photos (flag, yoga, environment day) reused or uploaded months late, GPS stamp in the photo far from the pond | Listing is AES-encrypted with a key in the page. Python's standard library has no AES, so it needs a small AES routine or a browser |
| 3 | GeoMGNREGA on Bhuvan (bhuvan-app2/vec3/fdc1.nrsc.gov.in) | high | medium | Same photo across assets or beneficiaries, before and after photo identical, device-day bursts, quick moderator approval, high spend on small assets | Needs WMS handling, HTML detail parsing, up to 6 photos per asset. NREGASoft reports behind it return 401 |
| 4 | PMAY-G rural housing (awaassoft.pmayg.dord.gov.in) | high | medium | One house photo for several beneficiaries, instalment paid before the stage photo, stages inspected on one day | Search forms have a math CAPTCHA (not used; the map route avoids it). Copyright policy needs written permission from MoRD. Highest privacy risk |
| 5 | PMGSY road quality inspections (pmgsy.dord.gov.in) | high | medium | Same road photo reused across inspections or roads, photos seconds apart but kilometres apart by caption, a monitor covering too many roads in a day | SSRS HTML only (CSV export broken). Older photos return an error page with HTTP 200. Payment join not done (agency list 401) |
| 6 | Swachh Bharat Gramin, community sanitary complexes (sbm.gov.in) | medium | medium | Many complexes at one GPS point (seen: 4 of 12 in one village), year-end bulk uploads, one structure counted twice | Slow server (a postback timed out at 90 s). Photos re-encoded, so exact hashes miss copies. Household toilet photos are not public |
| 7 | Jal Shakti Abhiyan: Catch the Rain (jsactr.mowr.gov.in) | medium | small to medium | Same checks as JSJB, and the same photo in both CTR and JSJB | Host did not connect from the scouting network. Work-level photos unverified |
| 8 | Jal Jeevan Mission (ejalshakti.gov.in) | low | large | Many water sources at one point, Har Ghar Jal certificates for villages with no working source | No public asset photos found. One village per request. District postback goes to a login error page |

Cheapest next scheme: eGramSwaraj. It is plain GETs with a JSON area tree, one HTML page per gram
panchayat with every photo row (work id, approved cost, stage, coordinates, upload time to the
microsecond), and no postbacks or viewstate. In one GP of 9 works the metadata alone showed stage
photos uploaded after the recorded completion date on 5 works. Metadata checks run before any photo
is downloaded.

Needs a login or CAPTCHA, so out of scope unless access is granted:

- PMAY-G beneficiary search by registration number (math CAPTCHA). The map route does not need it.
- NREGASoft citizen drill-downs (muster rolls, work status): 401 Unauthorized, also in a browser.
  Treat as "do not automate". NMMS attendance photos: link 404, report host unreachable.
- Swachh Bharat household toilet (IHHL) photos: not on any public page; the mSBM app is for officials.
- Amrit Sarovar, CTR and the eGramSwaraj header all have official logins; the public data does not use them.
- PMGSY final bill payments (agency list 401). jaljeevanmission.gov.in runs a bot check: stay off it.

## 2. Workflow for adding a scheme

1. Scout the portal. Find the public path from area list to records to photos, note login, CAPTCHA,
   robots.txt, terms and rate behaviour, and measure one small area. Done: a scout report in the
   shape used above, with every claim backed by a URL or marked unverified, and a stop at any login,
   CAPTCHA or 401.
2. Save page fixtures. Keep one copy of each page type (area list, record list, detail, a photo, an
   error page, an empty area) under `tests/collector/fixtures/<id>/`, with faces and names blanked
   in anything committed. Done: every parser has a saved input, including one broken case.
3. Write the adapter behind the existing interface: `collector/schemes/<id>.py`, a subclass of
   `SchemeAdapter` with `list_areas`, `list_records`, `list_photos`, all requests through
   `self.client.fetch`. Raise `LayoutChanged` on anything that does not add up; never return a
   partial list. Done: the adapter imports, is listed in `SCHEMES`, and runs on fixtures.
4. Offline tests. `tests/collector/test_<id>_offline.py` reads the fixtures with no network and
   checks record counts, field values, photo URLs and that the broken pages raise. Done: the test
   passes and the existing tests still pass.
5. Ground-truth check by hand. Pick one small area (one block or GP), count its records and photos
   on the portal in a browser, then compare with what the adapter saved. Done: counts match, and
   any mismatch is explained in the adapter's notes.
6. Map the scheme's fields to the common record (section 3). Write the mapping as a table in the
   adapter's docstring: portal field, common field, or "kept under `extra`". Done: the checker loads
   the records with nothing left out for type errors.
7. Choose the checks. Mark which of `photo_reuse`, `same_gps`, `same_day`, `upload_burst` apply,
   what the "office" grouping means for this scheme, and which scheme-specific checks to add as new
   modules. A check that needs a field the scheme lacks is turned off, not fed blanks. Done: settings
   under `schemes.<id>` in `checker/config.json` and a fixture case for each new check.
8. Add the scheme to the site and the queue plan. Name and portal URL in `checker/config.json`, a
   plan file `plans/<id>-<scope>.json`. Done: the site shows the scheme's cases and coverage on
   fixture data, and `run.py queue --dry-run` prints the plan.
9. First bounded live run: one small area with `--max-minutes` and the default delay. Done: exit
   code 0 or 5 with every skip explained, and no request pattern heavier than the portal's own page.
10. Review of the first cases by a person. Open each of the first cases against the portal and write
    down which are real leads and which have an innocent cause. Done: thresholds adjusted or an
    innocent pattern added to the scheme's skip list, before the scheme goes into the full queue.

## 3. What the common parts need

Area hierarchies. `Area` today assumes state, district, area type, block, village. Other schemes stop
at a gram panchayat (eGramSwaraj, MGNREGA, PMAY-G), at a road and inspection (PMGSY), or at a
district with no lower walk (PMAY-G map JSON, Amrit Sarovar national paging). Add a general path to
`Area`: a list of levels, each with level name, code and name, plus a `code_system` ("LGD", "NREGA",
"PMGSY"). Keep the current fields filled where they exist so JSJB code and coverage keys do not
change. The coverage key becomes scheme, code system and the full code path. NREGA and LGD codes
differ (Punjab is 26 in NREGA, 3 on JSJB), so never join schemes by code without a mapping table.

Records that are not works. Add optional common fields: `parent_id` (work code or road code),
`stage` (before, during, completed, or the portal's label), `amount` and `amount_kind` (approved,
spent, instalment), `group_key` and `group_kind` (the "office" for grouping: device id for MGNREGA,
GP for eGramSwaraj, monitor for PMGSY, `created_by` for JSJB). Beneficiary schemes (PMAY-G) get a
`beneficiary_ref`, a salted hash of the registration number, never the name. Anything else goes
under `extra` so the checker ignores it.

Records without photos. GeoMGNREGA and PMAY-G list records first and photos later, and JJM has no
photos. Add `photos_state` to each record: "fetched", "not_fetched" or "none_on_portal". Photo checks
skip "not_fetched", and the site never says a record was photo-checked when it was not.

Photos without URLs, or richer than a URL. Swachh Bharat sends photos inline as base64, so
`list_photos` must be allowed to return `{"data": bytes}` as well as `{"url": ...}`. Photos also carry
their own GPS, date and stage on PMGSY, eGramSwaraj and Amrit Sarovar: allow `lat`, `lon`,
`taken_at`, `stage` and `label` on each photo entry. Event photos (Amrit Sarovar flag hoisting) hang
on a pond, not a work; keep them as photos of that record with `stage` set to the event.

Per-scheme rate limits. The client applies one delay. Make the delay a per-host setting, read from
the adapter (`hosts = {"bhuvan-vec3.nrsc.gov.in": 1.0, ...}`), with a longer timeout and fewer retries
for slow hosts such as sbm.gov.in. A slow or 401 host lowers its own rate; it never raises another's.

Grouping on the site. `cases.json` already carries the scheme per case. The site needs a scheme
filter, per-scheme coverage on the map, the scheme name on every case, and a separate list for
cross-scheme cases (section 5), which show every scheme involved. The checker already writes
`checks_run`; make it per scheme, so "not run for this scheme" is not shown as "found nothing".

## 4. Faster collection, still polite

Use bulk listings where a scout found one. Measured:

- GeoMGNREGA: Morni block, 2849 assets in one WMS call (1.8 MB, 3.7 s). JSJB needs one search per village.
- PMAY-G: Namchi district, 281 houses and 1424 stage geotags in one JSON call (160 KB, 0.68 s).
- PMGSY: all inspected roads of Sikkim for January to October 2026 on one report page (88 inspection links).
- Amrit Sarovar: national list of 68,827 ponds in about 35 pages if `size=2000` works (unverified;
  100 rows per page was tested), plus 1 page for Phase 2.
- eGramSwaraj: one page per GP with assets, and the count JSON says which GPs have none (Goa 2025-26:
  63 of 191 GPs had assets), so the rest need no request.
- Swachh Bharat: the block's village table shows photo counts, so villages with 0 photos are skipped.

Read different portals at the same time. They are different servers, so one stream per host at that
host's own delay adds no load to any single server. GeoMGNREGA itself uses three hosts (list,
details, photos) and PMGSY two (pages, images). This needs one change: `data/state/collector.lock`
allows one collector per data folder, so either one process runs several adapters in threads, one
per host, or the lock becomes per scheme with shared photo storage made safe for two writers. Gain:
up to the number of hosts in parallel; not measured.

List first, photos second, for schemes with bulk listings. The JSJB README rejected a later photo
pass for JSJB because there it saves no requests. Here it does: same-GPS, burst and date checks run
on the list alone (GeoMGNREGA bulk list, PMAY-G map JSON, eGramSwaraj GP pages), and detail and photo
fetches go first to records in flagged groups or a recent stage. Coverage must then record two
states per area, "listed" and "photos checked", and the site must show which.

Skip unchanged areas on refresh. Where the portal gives a count or a newest timestamp per area
(eGramSwaraj GP count, Swachh Bharat village photo count, PMGSY month windows, newest `ser_time` in a
GeoMGNREGA block), skip the area when it matches what was stored. A matching count does not prove
nothing changed, so still re-read every area fully on the plan's `since_days` cycle.

Store by content hash. Photos are already stored once per sha256 under `data/photos/`, so a photo
shared by two schemes takes disk space once. It is still downloaded once per URL, since bytes are
needed to hash them, and URLs differ between portals. Make the photo index shared across schemes
(keyed by URL, holding the sha256), so a URL seen by any scheme is not fetched again.

Also keep HTTP connections open per host. The scouts did not measure the gain.

## 5. Checks that need several schemes

Same photo in two schemes. Hash every photo into one shared index and flag a sha256 that appears in
records of two schemes. Likely pairs: JSJB and CTR (same owner, JSJB once lived on the CTR host),
Amrit Sarovar and GeoMGNREGA (ponds marked "MGNREGA" under scheme_with), eGramSwaraj and Swachh
Bharat community assets. Swachh Bharat and eGramSwaraj photos are re-encoded, so this check needs the
perceptual hash that is not built yet (`checker/README.md`).

Two schemes claiming one asset at one point. Compare coordinates across schemes within a small
radius and with a matching asset type (a pond in Amrit Sarovar, a farm pond in GeoMGNREGA, a recharge
structure in JSJB). JSJB records carry no MGNREGA work code, so this match is by GPS, type and dates.

Linking a flagged work to its payment record. Done on the same page for GeoMGNREGA (work code,
person-days, expenditure), PMAY-G (sanction, instalments, FTO numbers and dates) and eGramSwaraj
(activity code, approved cost, grant). Amrit Sarovar Phase 2 has MGNREGA work codes on some ponds (13
real codes in the first 100), which join to GeoMGNREGA. PMGSY's payment reports did not load.

Innocent explanations. Convergence is policy: one pond can be funded by MGNREGA, shown in Amrit
Sarovar and counted in JSJB or CTR, and its photo may honestly appear in all of them. A PMAY-G house
also shows an MGNREGA job card. So a cross-scheme case is a lead only when the same photo or point is
claimed for different structures, different beneficiaries, or the same cost twice. Each case states
the innocent reading next to the flag and links every record so anyone can check the portals.

## 6. Privacy and legal care

| Scheme | Publish | Withhold | Terms seen |
|---|---|---|---|
| eGramSwaraj | Activity code, cost, stage, dates, hashes, blurred thumbnails, links | People and house-number stamps in photos | Reproduce after permission by mail; prior permission asked for linking |
| Amrit Sarovar | Pond id, place, hashes, landscape photos | Faces in event and inspection photos, private names in pond names | "All rights reserved by BISAG-N"; listing deliberately encrypted, keep the rate low |
| GeoMGNREGA | Work code, money fields, hashes, flags | Asset names naming people, individual-beneficiary photos | Bhuvan terms not read (unverified) |
| PMAY-G | regNo hash, block or GP totals, match counts | Names, father's name, job card, every beneficiary photo (they show the person and a name plaque) | Reproduction needs prior written permission from MoRD: ask before publishing |
| PMGSY | Road code, package, grades, hashes; monitor name as an official act | Identifiable people in photos | Reuse allowed with credit to the OMMAS portal of NRIDA, MoRD, and the date taken; photos of identifiable people excluded |
| Swachh Bharat | Village code, CSC name, GPS, upload date, hashes | Private names in location text; any household toilet photo if ever found | No terms page (link points to "#") |
| CTR | As JSJB | As JSJB | Privacy page not read (unverified) |
| JJM | Source coordinates, scheme cost | Beneficiary names and father's or husband's name shown on the page | None found |

General rules for every scheme: keep originals on this computer only, publish blurred copies through
`run.py publish` after a person's review, never put names or registration numbers in URLs or public
files, and do not collect beneficiary fields the checks do not use.
