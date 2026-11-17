"""eGramSwaraj geotagged panchayat works (https://egramswaraj.gov.in/geoTaggingReport.do).

Public path, read in November 2026 (no login, no CAPTCHA, no robots.txt: the host answers 404 for it):

1. geoTaggingReport.do: the report page. Its "Select Plan Year" dropdown lists the plan years.
2. Count JSON, one list per level, each entry {code, name, level, gpCount, count}:
   - geoTaggingStateReport.do?year=2025-2026: states.
   - geoTaggingZpReport.do?stateId=3&year=2025-2026: districts (zilla panchayats). An entry with
     level "I" has blocks below it; level "V" lists its gram panchayats directly (Goa).
   - geoTaggingBpReport.do?stateId=3&code=302426&year=2025-2026: blocks of a district.
   - geoTaggingGpReport.do?stateId=3&code=922&year=2025-2026: gram panchayats of a block (or of a
     level "V" district). Only GPs with geotagged activities were listed in the block read.
   The page script sends Nagaland (13), Mizoram (15) and Meghalaya (17) straight to the GP list with
   fixed codes 12, 11 and 22, and Puducherry (34) to the block list with code 2. Mizoram was seen
   working that way (its district list is [null]); the other three had a count of 0 and are unverified.
   "count" is the number of activities with geotagged photos: in GP 236204 (2025-2026) count 3 matched
   3 activity codes on 9 photo rows. A unit whose count is 0 is not requested.
3. geotaggedDetailsforgpdp.do?year=2025&gpcode=236204: one HTML table per GP and plan year, one row
   per photo: S.No, Activity Code, Activity Name, Admin Approved Cost (in INR), Asset Name, Asset
   Stage, Photo (image, latitude, longitude, file name), Date Of Upload (to the microsecond). No paging.
4. getMactionPhoto.do?imgId=<server path>: the photo. Only paths printed in a GP page are requested.

Codes: the state codes are LGD state codes (Punjab 3). District, block and GP codes are the
portal's panchayat codes (Punjab districts 27, 555, 260791, ...), not LGD district codes, so
code_system is "LGD-PRI". Never join them to another scheme's codes without a mapping table.

One record is one asset of one activity in one plan year: the rows of a GP page that share
Activity Code and Asset Name. Its photos are the stage photos of those rows.

Field mapping (portal field -> common field):

| portal | common |
|---|---|
| plan year, Activity Code, Asset Name | record_id: "<plan year start>-<activity code>-<first 8 hex of sha1 of asset name>" |
| Activity Code | parent_id |
| Activity Name | structure_type |
| Admin Approved Cost(in INR) | amount, amount_kind "approved" |
| Asset Stage of the latest uploaded photo | stage (portal label, unchanged) |
| latitude, Longitude of the earliest uploaded photo with a point | lat, lon |
| state, district, block names and codes from the count JSON | state, district, block, state_code, district_code, block_code |
| GP name and code | village, village_code (and the GP name again as gp) |
| plan year | year ("2025-2026"), so the photos pass can be limited by --year and --gp |
| GP code | group_key, group_kind "gp" |
| GP page URL | source_url |
| every photo row (photo URL, Date Of Upload, Asset Stage, latitude, Longitude, file name) | photo_source (a list), and the saved photos: url, uploaded_at, stage, lat, lon, label (file name) |
| plan year | extra.plan_year |
| Asset Name | extra.asset_name |
| count of the GP in the GP list JSON | extra.gp_listed_count |
| second latitude/longitude pair printed in a photo cell, when it differs from the first | extra.second_points |
| text of a cost, date or point that could not be read | extra.unreadable |
| (none on the portal) | start_date, end_date, status, created_by: left empty |

Counts: the count JSON can be lower than the GP page. In the two trial areas (November 2026),
3 of 15 GPs (Malerkotla HQ block, 2025-2026) and 4 of 17 GPs (South Goa, 2025-2026) showed more
activities on the page than in the JSON. In Ambaulim (South Goa) the extra work's photo was
uploaded a week before the trial, so the JSON probably lags; that reason is not verified. A GP
that is not in the JSON yet, or shows 0, is not requested, so its works wait for a later refresh.
Telangana's Medchal Malkajgiri district comes with level null; the page script does not show
it, and neither does this adapter.

The portal prints no names of people on these pages; asset and activity names are work
descriptions. Nothing here stores a beneficiary name or number.
"""
import hashlib
import html
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser

from ..base import AccessDenied, Area, FilterNotFound, LayoutChanged, PortalChanged, SchemeAdapter, SkippedUnit
from ..http import FetchError

log = logging.getLogger("collector.egramswaraj")

HOST = "egramswaraj.gov.in"
BASE = f"https://{HOST}/"
REPORT_URL = BASE + "geoTaggingReport.do"
PHOTO_PREFIX = "getMactionPhoto.do?imgId="
CODE_SYSTEM = "LGD-PRI"

# The page script's own shortcuts (geoTagging-controller.js, onDistrictCount):
# state code -> GP list code (no district or block level) or block list code (no district level).
GP_LIST_DIRECT = {"13": "12", "15": "11", "17": "22"}
BLOCK_LIST_DIRECT = {"34": "2"}

COLUMNS = ("s.no", "activity code", "activity name", "admin approved cost(in inr)", "asset name",
           "asset stage", "photo", "date of upload")
YEAR_RE = re.compile(r"^(\d{4})-(\d{4})$")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def norm(s):
    return " ".join(str(s or "").split()).casefold()


def looks_blocked(text):
    """A login form, CAPTCHA or bot check instead of the expected page."""
    low = text.casefold()
    return ('type="password"' in low or "type='password'" in low or "captcha" in low
            or "verify you are human" in low or "access denied" in low)


def upload_time(text):
    """'2026-06-19 09:26:17.115769' -> '2026-06-19T09:26:17.115769' (Indian Standard Time, no zone)."""
    t = " ".join((text or "").split())
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).isoformat()
    except ValueError:
        return None


def money(text):
    t = (text or "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", t):
        return None
    return float(t)


def points(text):
    """'latitude : 30.52,30.52 Longitude : 75.88,75.88' -> [(30.52, 75.88), ...]; [] if unreadable."""
    m = re.search(r"latitude\s*:\s*(.*?)\s*longitude\s*:\s*(.*?)(?:\s*file\s*name\s*:|$)", text, re.I | re.S)
    if not m:
        return None
    lats = [x.strip() for x in m.group(1).split(",") if x.strip()]
    lons = [x.strip() for x in m.group(2).split(",") if x.strip()]
    out = []
    for la, lo in zip(lats, lons):
        try:
            a, b = float(la), float(lo)
        except ValueError:
            return None
        if not (-90 <= a <= 90 and -180 <= b <= 180):
            return None
        out.append((a, b))
    return out if out and len(lats) == len(lons) else None


class GpPage(HTMLParser):
    """Header cells and body rows of the first table after the 'Geo Tagged Asset Report' heading.
    Each body cell is (text, [img src, ...])."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.heading = False
        self.header = []
        self.rows = []
        self.tables = 0
        # True once the report table's closing tag was read: a page cut off in transfer has none.
        self.closed = False
        self._depth = 0
        self._in_thead = self._in_tbody = False
        self._row = None
        self._cell = None
        self._text = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self._depth:
                self._depth += 1
            elif self.heading and self.tables == 0:
                self._depth = 1
            if self._depth == 1:
                self.tables += 1
            return
        if self._depth != 1:
            if self._depth and tag == "img" and self._cell is not None:
                self._cell[1].append(a.get("src") or "")
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ["", []]
            self._text = []
        elif tag == "img" and self._cell is not None:
            self._cell[1].append(a.get("src") or "")
        elif tag == "br" and self._cell is not None:
            self._text.append(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.closed = True
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None:
            self._cell[0] = " ".join("".join(self._text).split())
            self._row.append(tuple(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._in_thead:
                self.header.append([c[0] for c in self._row])
            elif self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False

    def handle_data(self, data):
        if "Geo Tagged Asset Report" in data and not self._depth:
            self.heading = True
        if self._cell is not None:
            self._text.append(data)


def parse_gp_page(text, page_url):
    """Photo rows of one GP page, as dicts. Raises LayoutChanged when the page does not add up;
    a row with no readable activity code is left out and logged.

    The login and error tests run only on a page without the report table. Asset and activity
    names are typed by panchayat staff, so a word such as "captcha" inside a real report must not
    stop a run of days."""
    page = GpPage(text)
    if not page.heading or page.tables == 0:
        if looks_blocked(text):
            raise AccessDenied(f"{page_url} shows a login, CAPTCHA or bot check")
        if "does not exist" in text or "Bad Request Error" in text:
            raise LayoutChanged(f"{page_url}: the portal answered with its error page")
        raise LayoutChanged(f"{page_url}: no 'Geo Tagged Asset Report' table")
    if not page.closed:
        raise LayoutChanged(f"{page_url}: the report table does not end; the page was cut off")
    if not page.header or tuple(norm(c) for c in page.header[-1]) != COLUMNS:
        raise LayoutChanged(f"{page_url}: table columns changed: {page.header[-1] if page.header else None}")
    rows = []
    for n, cells in enumerate(page.rows, 1):
        if len(cells) != len(COLUMNS):
            raise LayoutChanged(f"{page_url}: row {n} has {len(cells)} cells, expected {len(COLUMNS)}")
        sno = cells[0][0].strip()
        if sno != str(n):
            raise LayoutChanged(f"{page_url}: row {n} is numbered {sno!r}; rows missing or out of order")
        code = cells[1][0].strip()
        if not code.isdigit():
            log.warning("%s: row %d left out, no readable activity code (%r)", page_url, n, code[:40])
            continue
        unreadable = {}
        cost = money(cells[3][0])
        if cost is None and cells[3][0].strip():
            unreadable["cost"] = cells[3][0][:80]
        when = upload_time(cells[7][0])
        if when is None and cells[7][0].strip():
            unreadable["uploaded_at"] = cells[7][0][:80]
        photo_text = cells[6][0]
        pts = points(photo_text) if photo_text else None
        if pts is None and re.search(r"latitude", photo_text, re.I):
            unreadable["point"] = photo_text[:160]
        url = None
        srcs = [s.strip() for s in cells[6][1] if s and s.strip()]
        if srcs:
            src = html.unescape(srcs[0])
            path = src[len(PHOTO_PREFIX):] if src.startswith(PHOTO_PREFIX) else None
            if path and ".." not in path and path.startswith("/"):
                url = urllib.parse.urljoin(page_url, src)
            else:
                log.warning("%s: row %d photo link not of the expected form, left out: %.120r", page_url, n, src)
        m = re.search(r"file\s*name\s*:\s*(\S+)", photo_text, re.I)
        rows.append({"sno": n, "activity_code": code, "activity_name": cells[2][0] or None, "cost": cost,
                     "asset_name": cells[4][0] or None, "stage": cells[5][0] or None, "url": url,
                     "points": pts or [], "file_name": m.group(1) if m else None, "uploaded_at": when,
                     "unreadable": unreadable})
    # The same field unreadable in every row means the layout moved, not one bad cell.
    if len(rows) >= 2:
        for field in ("cost", "uploaded_at"):
            if all(field in r["unreadable"] for r in rows):
                raise LayoutChanged(f"{page_url}: {field} unreadable in every row")
    return rows


def asset_hash(name):
    return hashlib.sha1(norm(name).encode("utf-8")).hexdigest()[:8]


class EGramSwarajAdapter(SchemeAdapter):
    __doc__ = __doc__
    id = "egramswaraj"
    name = "eGramSwaraj geotagged panchayat works"
    portal_url = REPORT_URL
    filters = ("year", "state", "district", "block", "gp")
    plan_levels = ("state", "district")
    hosts = {HOST: {"delay": 1.0, "timeout": 60, "retries": 4}}
    supports_list_only = True

    # The report page, state lists and district lists are kept for this many seconds, so one queue
    # unit and the "remaining" district listing do not read them twice, and a queue that runs for
    # weeks still sees new counts. Block and GP lists are read once per unit and are not kept, so
    # the memory used does not grow with the number of units run.
    CACHE_SECONDS = 6 * 3600
    CACHED_LISTS = ("geoTaggingStateReport.do", "geoTaggingZpReport.do")

    def __init__(self, client):
        super().__init__(client)
        self._json_cache = {}
        self._years_cache = None
        self.clock = time.monotonic

    def _cached(self, key):
        hit = self._json_cache.get(key)
        if hit and self.clock() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        self._json_cache.pop(key, None)
        return None

    # ---- pages -------------------------------------------------------------------------------

    def _get_text(self, url):
        """The page as text. The caller decides whether it is the expected page; a page that is not
        is tested for a login, CAPTCHA or bot check there."""
        raw = self.client.fetch(url)
        return raw.decode("utf-8", errors="replace")

    def _get_list(self, path, **params):
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        hit = self._cached(url)
        if hit is not None:
            return hit
        text = self._get_text(url)
        try:
            data = json.loads(text)
        except ValueError:
            if looks_blocked(text):
                raise AccessDenied(f"{url} shows a login, CAPTCHA or bot check") from None
            raise LayoutChanged(f"{url} is not JSON") from None
        if not isinstance(data, list):
            raise LayoutChanged(f"{url} is not a JSON list")
        out = []
        for e in data:
            if e is None:
                continue
            if not isinstance(e, dict) or e.get("code") in (None, "") or not isinstance(e.get("name"), str):
                raise LayoutChanged(f"{url}: entry without code and name: {e!r:.120}")
            count = e.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LayoutChanged(f"{url}: entry {e.get('name')!r} has no usable count")
            out.append({"code": str(e["code"]), "name": " ".join(e["name"].split()), "level": e.get("level"),
                        "count": count})
        if path in self.CACHED_LISTS:
            self._json_cache[url] = (self.clock(), out)
        return out

    def plan_years(self):
        """Plan years offered by the report page's dropdown, newest first, as '2025-2026'."""
        if self._years_cache and self.clock() - self._years_cache[0] < self.CACHE_SECONDS:
            return list(self._years_cache[1])
        text = self._get_text(REPORT_URL)
        m = re.search(r'<select[^>]*id="planYear"[^>]*>(.*?)</select>', text, re.S | re.I)
        if not m:
            if looks_blocked(text):
                raise AccessDenied(f"{REPORT_URL} shows a login, CAPTCHA or bot check")
            raise PortalChanged("the report page has no plan year dropdown")
        years = []
        for v in re.findall(r'<option[^>]*value="([^"]*)"', m.group(1)):
            v = v.strip()
            if YEAR_RE.match(v) and v not in years:
                years.append(v)
        if not years:
            raise PortalChanged("the plan year dropdown has no years")
        self._years_cache = (self.clock(), years)
        return list(years)

    # ---- areas -------------------------------------------------------------------------------

    def _years(self, wanted):
        years = self.plan_years()
        if not wanted:
            return years
        w = str(wanted).strip()
        for y in years:
            if w == y or w == y[:4]:
                return [y]
        raise FilterNotFound(f"plan year {wanted!r} is not offered; the portal lists {', '.join(years)}")

    def list_districts(self, state):
        """District names of a state across all plan years, in portal order (for "district": "remaining")."""
        names = []
        found = read = False
        for year in self.plan_years():
            try:
                st = self._match_state(year, state)
                if st is None:
                    read = True
                    continue
                found = True
                if st["count"] == 0:
                    read = True
                    continue
                for d in self._districts(year, st):
                    if d["name"] not in names:
                        names.append(d["name"])
                read = True
            except (FetchError, LayoutChanged) as e:
                if isinstance(e, FetchError) and e.denied:
                    raise
                log.warning("district list of %s for %s not read: %s", state, year, e)
        if not read:
            raise FetchError(f"no district list of {state} could be read")
        if not found:
            raise FilterNotFound(f"state {state!r} not on the portal")
        return names

    def _match_state(self, year, name):
        for s in self._get_list("geoTaggingStateReport.do", year=year):
            if norm(s["name"]) == norm(name):
                return s
        return None

    def _districts(self, year, st):
        """[{code, name, count, kind}] where kind is "blocks" or "gps" (what the code lists)."""
        if st["code"] in GP_LIST_DIRECT:
            return [{"code": GP_LIST_DIRECT[st["code"]], "name": st["name"], "count": st["count"], "kind": "gps"}]
        if st["code"] in BLOCK_LIST_DIRECT:
            return [{"code": BLOCK_LIST_DIRECT[st["code"]], "name": st["name"], "count": st["count"],
                     "kind": "blocks"}]
        out = []
        for d in self._get_list("geoTaggingZpReport.do", stateId=st["code"], year=year):
            if d["level"] == "I":
                out.append(dict(d, kind="blocks"))
            elif d["level"] == "V":
                out.append(dict(d, kind="gps"))
            elif d["level"] is not None:
                raise LayoutChanged(f"district {d['name']!r} has unknown level {d['level']!r}")
        return out

    def list_areas(self, filters):
        f = {k: (filters.get(k) or None) for k in self.filters}
        matched = {k: False for k in ("state", "district", "block", "gp") if f[k]}
        for year in self._years(f["year"]):
            ystart = year[:4]
            try:
                states = self._get_list("geoTaggingStateReport.do", year=year)
            except (FetchError, LayoutChanged) as e:
                if isinstance(e, FetchError) and e.denied:
                    raise
                yield SkippedUnit("states", year, "network" if isinstance(e, FetchError) else "layout", str(e))
                continue
            for st in states:
                if f["state"] and norm(st["name"]) != norm(f["state"]):
                    continue
                if f["state"]:
                    matched["state"] = True
                if st["count"] == 0:
                    continue
                path0 = [{"level": "plan_year", "code": ystart, "name": year},
                         {"level": "state", "code": st["code"], "name": st["name"]}]
                yield from self._walk_state(year, st, path0, f, matched)
        for k, ok in matched.items():
            if ok is False:
                raise FilterNotFound(f"{k} {f[k]!r} matched nothing on the portal (within the other filters)")

    def _walk_state(self, year, st, path0, f, matched):
        try:
            districts = self._districts(year, st)
        except (FetchError, LayoutChanged) as e:
            if isinstance(e, FetchError) and e.denied:
                raise
            yield SkippedUnit("districts", f"{year} {st['name']}", _kind(e), str(e))
            return
        for d in districts:
            if f["district"] and norm(d["name"]) != norm(f["district"]):
                continue
            if f["district"]:
                matched["district"] = True
            if d["count"] == 0:
                continue
            path1 = path0 + [{"level": "district", "code": d["code"], "name": d["name"]}]
            if d["kind"] == "gps":
                if f["block"]:
                    continue
                yield from self._walk_gps(year, st, d["code"], path1, f, matched, f"{year} {d['name']}")
                continue
            try:
                blocks = self._get_list("geoTaggingBpReport.do", stateId=st["code"], code=d["code"], year=year)
            except (FetchError, LayoutChanged) as e:
                if isinstance(e, FetchError) and e.denied:
                    raise
                yield SkippedUnit("blocks", f"{year} {st['name']} > {d['name']}", _kind(e), str(e))
                continue
            for b in blocks:
                if f["block"] and norm(b["name"]) != norm(f["block"]):
                    continue
                if f["block"]:
                    matched["block"] = True
                if b["count"] == 0:
                    continue
                path2 = path1 + [{"level": "block", "code": b["code"], "name": b["name"]}]
                yield from self._walk_gps(year, st, b["code"], path2, f, matched,
                                          f"{year} {d['name']} > {b['name']}")

    def _walk_gps(self, year, st, code, path, f, matched, label):
        try:
            gps = self._get_list("geoTaggingGpReport.do", stateId=st["code"], code=code, year=year)
        except (FetchError, LayoutChanged) as e:
            if isinstance(e, FetchError) and e.denied:
                raise
            yield SkippedUnit("gram panchayats", label, _kind(e), str(e))
            return
        for g in gps:
            if f["gp"] and norm(g["name"]) != norm(f["gp"]):
                continue
            if f["gp"]:
                matched["gp"] = True
            if g["count"] == 0:
                continue
            yield Area(levels=path + [{"level": "gp", "code": g["code"], "name": g["name"]}],
                       code_system=CODE_SYSTEM,
                       context={"year": year, "gpcode": g["code"], "count": g["count"]})

    # ---- records -----------------------------------------------------------------------------

    def gp_url(self, area):
        return BASE + "geotaggedDetailsforgpdp.do?" + urllib.parse.urlencode(
            {"year": area.context["year"][:4], "gpcode": area.context["gpcode"]})

    def list_records(self, area):
        url = self.gp_url(area)
        rows = parse_gp_page(self._get_text(url), url)
        return self.records_from_rows(area, rows, url)

    def records_from_rows(self, area, rows, url):
        codes = {lv["level"]: lv for lv in area.levels}
        groups = {}
        for r in rows:
            groups.setdefault((r["activity_code"], norm(r["asset_name"])), []).append(r)
        listed = area.context.get("count")
        activities = {k[0] for k in groups}
        if isinstance(listed, int) and listed != len(activities):
            log.warning("%s: GP list says %d activities, the page has %d", url, listed, len(activities))
        out = []
        for (code, _), grp in groups.items():
            first = grp[0]
            timed = sorted((r for r in grp if r["uploaded_at"]), key=lambda r: r["uploaded_at"])
            located = [r for r in (timed + [r for r in grp if not r["uploaded_at"]]) if r["points"]]
            lat, lon = located[0]["points"][0] if located else (None, None)
            costs = {r["cost"] for r in grp if r["cost"] is not None}
            extra = {"plan_year": area.context["year"], "asset_name": first["asset_name"],
                     "gp_listed_count": listed}
            if len(costs) > 1:
                extra["costs_seen"] = sorted(costs)
            second = [{"file_name": r["file_name"], "lat": p[0], "lon": p[1]}
                      for r in grp for p in r["points"][1:] if p != r["points"][0]]
            if second:
                extra["second_points"] = second
            unreadable = [dict(r["unreadable"], sno=r["sno"]) for r in grp if r["unreadable"]]
            if unreadable:
                extra["unreadable"] = unreadable
            source = [{"url": r["url"], "uploaded_at": r["uploaded_at"], "stage": r["stage"],
                       "lat": r["points"][0][0] if r["points"] else None,
                       "lon": r["points"][0][1] if r["points"] else None,
                       "label": r["file_name"]} for r in grp]
            rec = {
                "scheme": self.id,
                "record_id": f"{area.context['year'][:4]}-{code}-{asset_hash(first['asset_name'])}",
                "parent_id": code,
                "state": area.state, "state_code": codes.get("state", {}).get("code"),
                "district": area.district, "district_code": codes.get("district", {}).get("code"),
                "block": area.block, "block_code": codes.get("block", {}).get("code"),
                "village": area.village, "village_code": area.village_code,
                # Named like the collector filters, so "--photos --year ... --gp ..." finds them.
                "year": area.context["year"], "gp": area.village,
                "lat": lat, "lon": lon,
                "structure_type": first["activity_name"],
                "stage": timed[-1]["stage"] if timed else first["stage"],
                "amount": next((r["cost"] for r in grp if r["cost"] is not None), None),
                "amount_kind": "approved",
                "group_key": area.village_code, "group_kind": "gp",
                "source_url": url,
                "photo_source": source,
                "extra": extra,
            }
            if not any(s["url"] for s in source):
                rec["photos_state"] = "none_on_portal"
            out.append(rec)
        return out

    def list_photos(self, record):
        """The photo rows printed on the GP page; no request is made here."""
        out = []
        for s in record.get("photo_source") or []:
            if not isinstance(s, dict) or not s.get("url"):
                continue
            if not s["url"].startswith(BASE + PHOTO_PREFIX):
                log.warning("record %s: photo URL not from the GP page form, left out", record.get("record_id"))
                continue
            p = {"url": s["url"], "uploaded_at": s.get("uploaded_at")}
            for k in ("stage", "lat", "lon", "label"):
                if s.get(k) is not None:
                    p[k] = s[k]
            out.append(p)
        return out


def _kind(e):
    return "network" if isinstance(e, FetchError) else "layout"
