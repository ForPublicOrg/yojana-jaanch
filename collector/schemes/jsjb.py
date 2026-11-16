"""Jal Sanchay Jan Bhagidari public dashboard (ASP.NET WebForms, one postback per dropdown)."""
import logging
import math
import re
import urllib.parse
from datetime import date

from ..base import Area, FilterNotFound, LayoutChanged, NothingListed, PortalChanged, SchemeAdapter, SkippedUnit
from ..htmlparse import FormPage, label_text
from ..http import FetchError

log = logging.getLogger("collector.jsjb")

URL = "https://jsjb.mowr.gov.in/PublicDashboardJsjb.aspx"
DROPDOWNS = ("ddlState", "ddlDistrict", "ddlArea", "ddlBlockTown", "ddlVillageWard")
COLUMNS = ["S. No (Id)", "State", "District", "Area Type", "Block / Town", "Village / Ward",
           "Location of Structure", "Nature of Works", "Type of Structure", "Latitude", "Longitude",
           "Work Status", "Work Start Date", "Date Of Completion", "Community Participation",
           "Created By", "Image", "Feedback"]
BLANKS = ("", "-", "NA", "N/A")


def clean_name(name):
    """'Duggan (67)' -> 'Duggan'. The bracketed number is not a record count."""
    return re.sub(r"\s*\(\d+\)\s*$", "", name or "").strip()


def iso_date(s):
    """'29/04/2026' -> '2026-04-29'; blank -> None. Anything else raises ValueError."""
    s = (s or "").strip()
    if s in BLANKS:
        return None
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?", s, re.I)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            raise ValueError(f"unreadable date '{s}'")
        y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        raise ValueError(f"impossible date '{s}'") from None


def coordinate(s, limit):
    """Blank -> None. Not a number, NaN or out of range raises ValueError."""
    s = (s or "").strip()
    if s in BLANKS:
        return None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"coordinate '{s}' is not a number") from None
    if not math.isfinite(v) or abs(v) > limit:
        raise ValueError(f"coordinate '{s}' out of range")
    return v


def upload_time(url):
    """Upload time encoded in the photo filename, in either of the two known formats."""
    m = re.search(r"_(\d{2})-(\d{2})-(\d{4})-(\d{2})-(\d{2})-(\d{2})-\d+_Image", url)
    if not m:
        m = re.search(r"_(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})\d*_JanBhagidari", url)
    if not m:
        return None
    d, mo, y, h, mi, s = m.groups()
    try:
        date(int(y), int(mo), int(d))
    except ValueError:
        return None
    if int(h) > 23 or int(mi) > 59 or int(s) > 59:
        return None
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def _blank(s):
    s = (s or "").strip()
    return s or None


class SessionLost(Exception):
    """The portal answered with its start page or an error page instead of what was asked for."""

    def __init__(self, message, at_start=False):
        super().__init__(message)
        self.at_start = at_start


class JsjbAdapter(SchemeAdapter):
    id = "jsjb"
    name = "Jal Sanchay Jan Bhagidari (JSJB)"
    portal_url = URL
    session_retries = 2
    max_pages = 500

    def __init__(self, client):
        super().__init__(client)
        # Pages along the current dropdown path: [(step, page)], index 0 is the start page.
        self._chain = []

    # Every page is reached by replaying postback steps from the start page, so after an expired
    # session the same path can be walked again with a fresh one.
    def _fetch_page(self, data, expect):
        text = self.client.fetch(URL, data, referer=URL if data else None).decode("utf-8", "replace")
        page = FormPage(text, "gvData")
        page.text = text
        missing = [d for d in DROPDOWNS if d not in page.selects]
        if "__VIEWSTATE" not in page.hidden or missing:
            raise SessionLost(f"page has no {', '.join(missing) or '__VIEWSTATE'} (error page, or layout changed)",
                              at_start=data is None)
        wrong = [k for k, v in expect.items() if k in DROPDOWNS and page.selected(k) != v]
        if wrong:
            raise SessionLost(f"portal did not keep the selection of {', '.join(wrong)} "
                              "(it returned its start page; session expired?)")
        return page

    def _replay(self, steps):
        if not self._chain:
            self._chain = [(None, self._fetch_page(None, {}))]
        page = self._chain[0][1]
        for i, step in enumerate(steps, 1):
            if len(self._chain) > i and self._chain[i][0] == step:
                page = self._chain[i][1]
                continue
            del self._chain[i:]
            target, values, argument = step
            data = dict(page.hidden)
            data.update({"__EVENTTARGET": target, "__EVENTARGUMENT": argument})
            data.update(values)
            page = self._fetch_page(data, values)
            self._chain.append((step, page))
        return page

    def _get(self, steps, first=False):
        """first=True only for the opening start page: a start page without dropdowns there stops the run.
        Later in a run the same page is usually a short burst of error pages, so only that item is skipped."""
        for attempt in range(self.session_retries + 1):
            try:
                return self._replay(steps)
            except (SessionLost, FetchError) as e:
                self._chain = []
                if attempt == self.session_retries:
                    if isinstance(e, FetchError):
                        raise
                    if e.at_start and first:
                        raise PortalChanged(f"the JSJB start page has no dropdowns: {e}") from None
                    raise LayoutChanged(f"{e}, even after {self.session_retries} fresh sessions") from None
                log.warning("%s; starting a fresh session and trying again", e)
                if hasattr(self.client, "new_session"):
                    self.client.new_session()

    def _options(self, page, ddl, wanted, level):
        opts = [(v, clean_name(t)) for v, t, _ in page.selects[ddl] if v not in ("", "0")]
        if wanted is None:
            return opts
        hit = [o for o in opts if o[1].casefold() == wanted.strip().casefold()]
        if not hit:
            self._miss = f"{level} '{wanted}' not found (options in the last list checked: " + ", ".join(t for _, t in opts) + ")"
        return hit

    def list_districts(self, state):
        """District names of one state as the portal lists them. Two requests when the session is fresh."""
        start = self._get((), first=True)
        hit = self._options(start, "ddlState", state, "state")
        if not hit:
            raise FilterNotFound(self._miss)
        page = self._get((("ddlState", {"ddlState": hit[0][0]}, ""),))
        return [name for _, name in self._options(page, "ddlDistrict", None, "district")]

    def list_areas(self, filters):
        # A name may exist under only some parents (e.g. a village in Rural but not Urban),
        # so a miss is an error only when nothing matched anywhere.
        self._miss = None
        self._lists = {"blocks": 0, "villages": 0}
        found = skipped = False
        for area in self._walk(filters):
            found = found or isinstance(area, Area)
            skipped = skipped or isinstance(area, SkippedUnit)
            yield area
        if not found and self._miss:
            raise FilterNotFound(self._miss)
        if not found and not skipped and any(self._lists.values()):
            raise NothingListed(f"{self._lists['blocks']} block list(s) and {self._lists['villages']} village "
                                "list(s) were read and every one was empty; the portal may now fill these "
                                "lists some other way")

    def _unit(self, steps, level, name):
        """(page, None), or (None, SkippedUnit) when this list could not be read."""
        try:
            return self._get(steps), None
        except PortalChanged:
            raise
        except FetchError as e:
            kind, err = "network", e
        except Exception as e:
            kind, err = "layout", e
        log.error("list of %s skipped: %s: %s", level, name, err)
        return None, SkippedUnit(level, name, kind, str(err))

    def _walk(self, filters):
        start = self._get((), first=True)
        for st_code, st in self._options(start, "ddlState", filters.get("state"), "state"):
            vals = {"ddlState": st_code}
            s1 = (("ddlState", vals, ""),)
            st_page, skip = self._unit(s1, "districts", st)
            if skip:
                yield skip
                continue
            districts = self._options(st_page, "ddlDistrict", filters.get("district"), "district")
            area_types = self._options(st_page, "ddlArea", filters.get("area"), "area type")
            for d_code, dist in districts:
                for a_code, area in area_types:
                    v2 = dict(vals, ddlDistrict=d_code, ddlArea=a_code)
                    s2 = s1 + (("ddlArea", v2, ""),)
                    a_page, skip = self._unit(s2, "blocks", f"{st} > {dist} > {area}")
                    if skip:
                        yield skip
                        continue
                    blocks = self._options(a_page, "ddlBlockTown", filters.get("block"), "block")
                    self._lists["blocks"] += 1
                    if not blocks:
                        log.info("%s / %s / %s: no blocks listed", st, dist, area)
                    for b_code, block in blocks:
                        v3 = dict(v2, ddlBlockTown=b_code)
                        s3 = s2 + (("ddlBlockTown", v3, ""),)
                        b_page, skip = self._unit(s3, "villages", f"{st} > {dist} > {area} > {block}")
                        if skip:
                            yield skip
                            continue
                        villages = self._options(b_page, "ddlVillageWard", filters.get("village"), "village")
                        self._lists["villages"] += 1
                        if not villages:
                            log.info("%s / %s / %s / %s: no villages listed", st, dist, area, block)
                        for vi_code, village in villages:
                            yield Area(state=st, district=dist, area_type=area, block=block,
                                       village=village, village_code=vi_code,
                                       codes={"state_code": st_code, "district_code": d_code,
                                              "area_code": a_code, "block_code": b_code},
                                       context=(s3, v3))

    def list_records(self, area):
        steps, v3 = area.context
        vals = dict(v3, ddlVillageWard=area.village_code)
        steps = steps + (("", dict(vals, btnSearch="Search"), ""),)
        page = self._get(steps)
        total = label_text(page.text, "Label8")
        if not page.table_found or total is None:
            raise LayoutChanged("JSJB results table 'gvData' or 'Total' label not found")
        digits = re.sub(r"\D", "", total)
        if not digits:
            raise LayoutChanged(f"JSJB 'Total' label has no number: '{total}'")
        expected = int(digits)
        rows = self._rows(page)
        seen_pages = {"Page$1"}
        while True:
            nxt = sorted((p for p in page.pager_args if p not in seen_pages and re.fullmatch(r"Page\$\d+", p)),
                         key=lambda p: int(p[5:]))
            if not nxt:
                break
            if len(seen_pages) >= self.max_pages:
                raise LayoutChanged(f"more than {self.max_pages} result pages")
            seen_pages.add(nxt[0])
            steps = steps + (("gvData", vals, nxt[0]),)
            page = self._get(steps)
            rows += self._rows(page)
        if len(rows) != expected:
            raise LayoutChanged(f"portal says Total {expected} but {len(rows)} rows were read")
        records = self._records(rows, area)
        ids = [r["record_id"] for r in records]
        if len(set(ids)) != len(ids):
            raise LayoutChanged("the same record id appears twice; paging is not working as expected")
        return records

    @staticmethod
    def _rows(page):
        header = next((r["cells"] for r in page.rows if r.get("is_header")), None)
        if header != COLUMNS:
            # An empty GridView renders no header, only an optional "no data" row.
            if header is None and all(len(r["cells"]) < 2 for r in page.rows):
                return []
            raise LayoutChanged(f"JSJB results columns changed: {header}")
        data = [r for r in page.rows if not r.get("is_header") and len(r["cells"]) > 1]
        bad = [r for r in data if len(r["cells"]) != len(COLUMNS)]
        if bad:
            raise LayoutChanged(f"JSJB result row has {len(bad[0]['cells'])} cells, expected {len(COLUMNS)}")
        return data

    def _records(self, rows, area):
        """One odd cell blanks that field (raw text kept in "unreadable"); one row without an id is left out.
        The same problem in every row of a village of two or more means the layout changed."""
        out, problems = [], {}
        for row in rows:
            rec, found = self._record(row, area)
            for p in found:
                problems.setdefault(p, []).append(rec["record_id"] if rec else row["cells"][0][:40])
            if rec:
                out.append(rec)
        for p, where in problems.items():
            if len(rows) >= 2 and len(where) == len(rows):
                raise LayoutChanged(f"JSJB {p} in every row, for example {where[0]}")
            log.warning("%s > %s: %s in %d row(s): %s", area.block, area.village, p, len(where),
                        ", ".join(map(str, where[:5])))
        return out

    def _record(self, row, area):
        """(record or None, list of problem kinds)."""
        c = row["cells"]
        m = re.search(r"\((\d+)\)", c[0])
        if not m:
            return None, ["record id not found"]
        rid = m.group(1)
        problems, unreadable = [], {}
        # The Image cell shows one "img-N" label per photo; a mismatch means the link attribute moved.
        if len(re.findall(r"img-\d+", c[16])) != len(row["links"]):
            problems.append("photo labels and photo links do not match")
            unreadable["photos"] = re.sub(r"<[^>]*>", " ", c[16]).strip()[:200]
        values = {}
        for key, i, read in (("lat", 9, lambda s: coordinate(s, 90)), ("lon", 10, lambda s: coordinate(s, 180)),
                             ("start_date", 12, iso_date), ("end_date", 13, iso_date)):
            try:
                values[key] = read(c[i])
            except ValueError:
                values[key] = None
                problems.append(f"unreadable {COLUMNS[i]}")
                unreadable[key] = c[i][:200]
        lat, lon, start, end = values["lat"], values["lon"], values["start_date"], values["end_date"]
        rec = {
            "scheme": self.id, "record_id": rid,
            "state": _blank(c[1]) or area.state, "state_code": area.codes["state_code"],
            "district": _blank(c[2]) or area.district, "district_code": area.codes["district_code"],
            "area_type": _blank(c[3]) or area.area_type,
            "block": clean_name(re.sub(r"^(Block|Town)\s*:\s*", "", c[4])) or area.block,
            "block_code": area.codes["block_code"],
            "village": clean_name(re.sub(r"^(Village|Ward)\s*:\s*", "", c[5])) or area.village,
            "village_code": area.village_code,
            "location_text": _blank(c[6]), "nature_of_work": _blank(c[7]), "structure_type": _blank(c[8]),
            "lat": lat, "lon": lon, "status": _blank(c[11]),
            "start_date": start, "end_date": end,
            "community_participation": _blank(c[14]), "created_by": _blank(c[15]),
            "source_url": URL, "_photo_urls": [u for u in row["links"] if u],
        }
        if unreadable:
            rec["unreadable"] = unreadable
        return rec, problems

    def list_photos(self, record):
        urls = [urllib.parse.urljoin(URL, u.replace("\\", "/")) for u in record.get("_photo_urls", []) if u]
        return [{"url": u, "uploaded_at": upload_time(u)} for u in urls]
