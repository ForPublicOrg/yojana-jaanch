"""PMGSY road quality inspections on OMMAS (pmgsy.dord.gov.in), photos on pmgsygov.dord.gov.in.

Public path, all without a login (scouted November 2026):

1. QMMonWorksDetailLayout: the "Works-wise Grading Abstract" form. It lists the states and holds
   the anti-forgery token the form posts back.
2. DistrictDetails (POST StateCode): the districts of a state as JSON.
3. QMMonWorksDetailReport (POST the form): answers with an iframe whose src is the SSRS report
   (MvcReportViewer.aspx). The report lists every road of the state or district with its NQM and
   SQM inspection counts inside a month window, a link per count, and a Total row.
4. InspectionGradingDetails?id=<road>$<S or I>: the road's inspections of that monitor type, all
   dates ever. Two dropdowns: INSP_DATE (newest first, with time) and CHAINAGE (one option per
   inspection, its value the observation id, oldest first).
5. InspDetails/<observation>$<S or I>: one inspection: monitor, date, road fields, item grades and
   the photos (img src, caption in title, "lat$$lon" in data-latlong, date in data-date).

The two dropdowns of step 4 are not in the same order. Inspections are paired newest date with
highest observation id, and each pairing is checked against the date printed on the detail page;
if a check fails, every inspection of that road is read and chosen by its printed date. The number
of inspections found in the window must equal the report's count for that road, and the counts
must add up to the report's Total row, or the area is skipped (LayoutChanged).

Old photos (2021 seen) answer HTTP 200 with a 345 byte HTML "Oops! Something went wrong" page.
The runner stores only bytes that are a real image, so that page is never stored; the photo is
counted as failed and tried again on the next run.

One area is one district (code system "PMGSY", OMMAS's own state and district codes, which are not
LGD codes). The month window is not part of the area: --from_month and --to_month (YYYY-MM) set it,
by default the current month and the 11 before it.

Field mapping (portal field -> common field):

| portal | common |
|---|---|
| observation id and monitor type (SQM or NQM) | record_id, as "SQM-618892" |
| State, District (detail page) | state, district; also the area levels |
| Block (report row) | block |
| road code (IMS_PR_ROAD_CODE) | parent_id |
| Inspection Date (detail page) | start_date (YYYY-MM-DD) |
| Status (In Progress, Completed, Maintenance) | stage |
| Type (Road, Bridge) | structure_type |
| Overall Grading | status |
| Monitor Name | group_key, group_kind "quality_monitor" (an official act; no other person is stored) |
| road page URL | source_url |
| detail page URL | photo_source |
| photo src, caption, data-latlong | photo url, label, lat and lon |
| time in the photo file name (epoch ms), else data-date | photo taken_at (India time, no zone) |
| monitor type, package, sanction year, road name, road length, chainage, item grades, inspection time from INSP_DATE, report window | kept under extra |

Not stored: anything about people other than the monitor's name. Photos of identifiable people
are excluded from the portal's reuse permission, so they stay under data/ and go out only through
the blurring publish step.

Terms read on 17 November 2026 (/Home/HomeTermsOfUse/, /Home/HomeCopyrightPolicy/): automated
requests are not barred unless at a rate that affects other users; reuse needs credit to "the
OMMAS portal of the National Rural Infrastructure Development Agency, Ministry of Rural
Development" and the date taken. Neither host has a robots.txt (404 and an HTML error page)."""
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..base import AccessDenied, Area, FilterNotFound, LayoutChanged, PortalChanged, SchemeAdapter, SkippedUnit
from ..http import FetchError

BASE = "https://pmgsy.dord.gov.in"
IMAGE_HOST = "pmgsygov.dord.gov.in"
LAYOUT_URL = BASE + "/QualityMonitoringArea/QualityMonitoring/QMMonWorksDetailLayout"
DISTRICTS_URL = BASE + "/ProposalArea/Proposal/DistrictDetails"
REPORT_FORM_URL = BASE + "/QualityMonitoringArea/QualityMonitoring/QMMonWorksDetailReport/"
DETAIL_URL = BASE + "/QualityMonitoringArea/QualityMonitoring/InspDetails/{obs}${kind}"
STATE_SELECT = "StateList_CompletedRoadDetails"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
KINDS = {"S": "SQM", "I": "NQM"}
IST = timezone(timedelta(hours=5, minutes=30))
REPORT_HEADER = ("Sr.No.", "District", "Block", "Sanction Year", "Package No.", "Road Name", "Road Length",
                 "NQM* Inspection Count", "Last NQM Grading", "SQM# Inspection Count", "Last SQM Grading")
ROAD_LINK = re.compile(r"InspectionGradingDetails\?id=(\d+)\$([SI])$")
ACCESS_MARKS = re.compile(r"""type\s*=\s*["']?password|captcha|g-recaptcha|cf-challenge|are you a robot""", re.I)
ERROR_PAGE = re.compile(r"Oops! Something went wrong", re.I)


def norm(name):
    """Name for matching: case and spacing ignored, a trailing "(UT)" dropped."""
    text = " ".join(str(name or "").split()).casefold()
    return re.sub(r"\s*\(ut\)$", "", text).strip()


def text_of(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""


def guard(text, where):
    """Stop at a login, CAPTCHA or bot check; skip the area on the portal's own error page."""
    if ACCESS_MARKS.search(text):
        raise AccessDenied(f"{where} shows a login, CAPTCHA or bot check")
    if ERROR_PAGE.search(text[:3000]) and len(text) < 5000:
        raise LayoutChanged(f"{where} is the portal's error page")


def month_value(text, name):
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", str(text))
    if not m or not 1 <= int(m.group(2)) <= 12 or int(m.group(1)) < 2000:
        raise FilterNotFound(f"--{name} must be a month written YYYY-MM, not {text!r}")
    return int(m.group(1)), int(m.group(2))


def window_of(filters, today=None):
    """(from (year, month), to (year, month)); default the current month and the 11 before it."""
    today = today or datetime.now(IST).date()
    to = month_value(filters["to_month"], "to_month") if filters.get("to_month") else (today.year, today.month)
    if filters.get("from_month"):
        frm = month_value(filters["from_month"], "from_month")
    else:
        n = to[0] * 12 + to[1] - 1 - 11
        frm = (n // 12, n % 12 + 1)
    if frm > to:
        raise FilterNotFound(f"--from_month {frm[0]}-{frm[1]:02d} is after --to_month {to[0]}-{to[1]:02d}")
    return frm, to


def in_window(d, window):
    return window[0] <= (d.year, d.month) <= window[1]


def select_options(text, select_id):
    m = re.search(r'(?is)<select[^>]*\bid="%s"[^>]*>(.*?)</select>' % re.escape(select_id), text)
    if not m:
        return None
    return [(html.unescape(v), " ".join(html.unescape(re.sub(r"<[^>]+>", "", t)).split()))
            for v, t in re.findall(r'(?is)<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1))]


def hidden_value(text, name):
    m = re.search(r'<input[^>]*name="%s"[^>]*>' % re.escape(name), text)
    if not m:
        return None
    v = re.search(r'value="([^"]*)"', m.group(0))
    return html.unescape(v.group(1)) if v else None


class Tables(HTMLParser):
    """Every <table> as a list of rows; each cell is (text, rowspan, colspan, hrefs). Text inside a
    nested table belongs to the nested table only."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._stack = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            t = {"rows": [], "row": None, "cell": None}
            self._stack.append(t)
            self.tables.append(t["rows"])
        elif not self._stack:
            return
        elif tag == "tr":
            self._stack[-1]["row"] = []
        elif tag in ("td", "th") and self._stack[-1]["row"] is not None:
            def num(k):
                v = (a.get(k) or "1").strip()
                return int(v) if v.isdigit() and int(v) > 0 else 1
            self._stack[-1]["cell"] = {"text": [], "rowspan": num("rowspan"), "colspan": num("colspan"), "links": []}
        elif tag == "a" and self._stack[-1]["cell"] is not None and a.get("href"):
            self._stack[-1]["cell"]["links"].append(html.unescape(a["href"]).strip())

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag == "table":
            self._end_cell(t)
            self._end_row(t)
            self._stack.pop()
        elif tag in ("td", "th"):
            self._end_cell(t)
        elif tag == "tr":
            self._end_cell(t)
            self._end_row(t)

    def _end_cell(self, t):
        c = t["cell"]
        if c is not None and t["row"] is not None:
            t["row"].append((" ".join("".join(c["text"]).split()), c["rowspan"], c["colspan"], c["links"]))
        t["cell"] = None

    def _end_row(self, t):
        if t["row"] is not None:
            t["rows"].append(t["row"])
        t["row"] = None

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(data)


def grid(rows):
    """Rows with rowspan and colspan filled in, as lists of (text, hrefs)."""
    out, carry = [], {}
    for row in rows:
        line, col, cells = [], 0, list(row)
        while cells or any(c >= col for c in carry):
            if col in carry:
                text, links, left = carry[col]
                line.append((text, links))
                if left > 1:
                    carry[col] = (text, [], left - 1)
                else:
                    del carry[col]
                col += 1
                continue
            if not cells:
                break
            text, rowspan, colspan, links = cells.pop(0)
            for k in range(colspan):
                line.append((text, links if k == 0 else []))
                if rowspan > 1:
                    # Links stay with the first row of a spanned cell.
                    carry[col] = (text, [], rowspan - 1)
                col += 1
        out.append(line)
    return out


def number(text):
    text = (text or "").replace(",", "").strip()
    return float(text) if re.fullmatch(r"-?\d+(\.\d+)?", text) else None


def parse_report(raw):
    """{"roads": [row dicts with links], "total_nqm", "total_sqm", "pages"} from the SSRS page.
    Raises LayoutChanged when the table is missing or does not add up."""
    text = text_of(raw)
    guard(text, "report page")
    pages = re.search(r'id="ReportViewer_ctl05_ctl00_TotalPages"[^>]*>\s*([^<]*)<', text)
    if pages and pages.group(1).strip() not in ("", "1"):
        raise LayoutChanged(f"report has {pages.group(1).strip()} pages; only one-page reports are read")
    table = None
    for rows in Tables(text).tables:
        for i, row in enumerate(rows):
            texts = [c[0] for c in row]
            if "Sr.No." in texts and "Package No." in texts and "Road Name" in texts:
                table = rows[i:]
                break
        if table:
            break
    if not table:
        if "Works Wise Grading Abstract" in text:
            raise LayoutChanged("report page has its title but no road table")
        raise LayoutChanged("report page has no road table")
    filled = grid(table)
    head = [t for t, _ in filled[0]]
    try:
        start = head.index("Sr.No.")
    except ValueError:
        raise LayoutChanged("report header has no Sr.No. column") from None
    if tuple(head[start:start + len(REPORT_HEADER)]) != REPORT_HEADER:
        raise LayoutChanged(f"report columns changed: {head[start:]}")
    roads, totals = [], None
    for line in filled[1:]:
        cells = line[start:start + len(REPORT_HEADER)]
        if len(cells) < len(REPORT_HEADER):
            if any(t for t, _ in line):
                raise LayoutChanged(f"report row with {len(cells)} cells: {[t for t, _ in line]}")
            continue
        get = dict(zip(REPORT_HEADER, cells))
        if get["District"][0] == "Total" or get["Block"][0] == "Total":
            totals = (number(get["NQM* Inspection Count"][0]), number(get["SQM# Inspection Count"][0]))
            continue
        if not get["Package No."][0] and not get["Road Name"][0]:
            continue
        row = {"district": get["District"][0], "block": get["Block"][0], "sanction_year": get["Sanction Year"][0],
               "package": get["Package No."][0], "road_name": get["Road Name"][0],
               "road_length_km": number(get["Road Length"][0]), "links": []}
        for kind, col in (("I", "NQM* Inspection Count"), ("S", "SQM# Inspection Count")):
            count_text, hrefs = get[col]
            count = number(count_text)
            if count is None or count != int(count):
                raise LayoutChanged(f"unreadable {col} {count_text!r} for package {row['package']}")
            count = int(count)
            links = [h for h in hrefs if ROAD_LINK.search(h)]
            if count and len(links) != 1:
                raise LayoutChanged(f"package {row['package']}: count {count} with {len(links)} inspection links")
            if count:
                m = ROAD_LINK.search(links[0])
                if m.group(2) != kind:
                    raise LayoutChanged(f"package {row['package']}: {col} links to type {m.group(2)}")
                row["links"].append({"kind": kind, "road": m.group(1), "count": count,
                                     "url": urljoin(BASE + "/", links[0])})
        roads.append(row)
    nqm = sum(link["count"] for r in roads for link in r["links"] if link["kind"] == "I")
    sqm = sum(link["count"] for r in roads for link in r["links"] if link["kind"] == "S")
    if totals is None:
        if roads:
            raise LayoutChanged("report has road rows but no Total row")
        totals = (0, 0)
    if (totals[0] or 0, totals[1] or 0) != (nqm, sqm):
        raise LayoutChanged(f"report counts add up to NQM {nqm}, SQM {sqm}; its Total row says {totals}")
    return {"roads": roads, "total_nqm": nqm, "total_sqm": sqm}


def parse_road_page(raw, road, kind):
    """[(datetime, text)] inspection times newest first and [observation ids] from a road page."""
    text = text_of(raw)
    guard(text, f"road page {road}${kind}")
    if hidden_value(text, "IMS_PR_ROAD_CODE") != road or hidden_value(text, "QM_TYPE") != kind:
        raise LayoutChanged(f"road page {road}${kind} names another road or monitor type")
    dates, obs = select_options(text, "INSP_DATE"), select_options(text, "CHAINAGE")
    if dates is None or obs is None:
        raise LayoutChanged(f"road page {road}${kind} has no inspection dropdowns")
    times = []
    for v, _ in dates:
        try:
            times.append(datetime.strptime(v.strip(), "%d/%m/%Y %H:%M:%S"))
        except ValueError:
            raise LayoutChanged(f"road page {road}${kind}: unreadable inspection date {v!r}") from None
    ids = [v.strip() for v, _ in obs]
    if not all(i.isdigit() for i in ids):
        raise LayoutChanged(f"road page {road}${kind}: observation ids are not numbers")
    return times, ids


def _cell_text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def photo_time(url, day):
    """India time from a file name ending in epoch milliseconds when it falls on the printed day,
    else the printed day."""
    m = re.search(r"_(\d{13})\.(?:jpe?g|png)$", url or "", re.I)
    if m:
        t = datetime.fromtimestamp(int(m.group(1)) / 1000, IST).replace(tzinfo=None, microsecond=0)
        if day is None or t.date() == day:
            return t.isoformat()
    return day.isoformat() if day else None


def parse_detail(raw, obs, kind):
    """Fields, grades and photos of one inspection. Raises LayoutChanged when it is not readable."""
    text = text_of(raw)
    guard(text, f"inspection {obs}${kind}")
    if hidden_value(text, "ObservationId") != obs:
        raise LayoutChanged(f"inspection page {obs}${kind} is not about observation {obs}")
    fields = {}
    m = re.search(r'(?is)id="divContentQMGrading".*?<table[^>]*>(.*?)</table>(.*?)</table>', text)
    if not m:
        raise LayoutChanged(f"inspection page {obs}${kind} has no grading table")
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m.group(1)):
        cells = [_cell_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        for i in range(0, len(cells) - 1, 2):
            fields[cells[i]] = cells[i + 1]
    grades = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m.group(2)):
        cells = [_cell_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        if len(cells) == 3 and cells[0].isdigit():
            grades.append({"item": cells[1], "grade": cells[2]})
    for need in ("Monitor Name", "Inspection Date", "State", "District"):
        if not fields.get(need):
            raise LayoutChanged(f"inspection page {obs}${kind} has no {need}")
    try:
        day = datetime.strptime(fields["Inspection Date"], "%d %b %Y").date()
    except ValueError:
        raise LayoutChanged(f"inspection page {obs}${kind}: unreadable date {fields['Inspection Date']!r}") from None
    photos = []
    for tag in re.findall(r"(?is)<img\b[^>]*\bid=['\"]qmimage\d+['\"][^>]*>", text):
        attrs = {k.lower(): html.unescape(v) for k, _, v in
                 re.findall(r"""([\w-]+)\s*=\s*(['"])(.*?)\2""", tag, re.S)}
        src = (attrs.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        url = urljoin(BASE + "/", src)
        lat = lon = None
        parts = (attrs.get("data-latlong") or "").split("$$")
        if len(parts) == 2:
            lat, lon = number(parts[0]), number(parts[1])
            if lat is None or lon is None or (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                lat = lon = None
        pday = None
        try:
            pday = datetime.strptime((attrs.get("data-date") or "").strip(), "%d/%m/%Y").date()
        except ValueError:
            pass
        photos.append({"url": url, "uploaded_at": None, "lat": lat, "lon": lon,
                       "taken_at": photo_time(url, pday or day), "label": " ".join((attrs.get("title") or "").split()) or None})
    overall = next((g["grade"] for g in grades if g["item"].casefold() == "overall grading"), None)
    return {"fields": fields, "day": day, "grades": grades, "overall": overall, "photos": photos}


class PmgsyAdapter(SchemeAdapter):
    id = "pmgsy"
    name = "PMGSY road quality inspections (OMMAS)"
    portal_url = BASE + "/"
    filters = ("state", "district", "from_month", "to_month")
    plan_levels = ("state", "district")
    hosts = {"pmgsy.dord.gov.in": {"delay": 1.0, "timeout": 120, "retries": 3},
             IMAGE_HOST: {"delay": 1.0, "timeout": 90, "retries": 3}}
    supports_list_only = True

    def __init__(self, client):
        super().__init__(client)
        self._form = None

    def _page(self, url, data=None, where=None):
        text = text_of(self.client.fetch(url, data=data))
        guard(text, where or url)
        return text

    def _start(self, fresh=False):
        """States and the form token from the report form page, read once per run."""
        if self._form is None or fresh:
            text = self._page(LAYOUT_URL, where="report form page")
            states = select_options(text, STATE_SELECT)
            token = hidden_value(text, "__RequestVerificationToken")
            if not states or token is None:
                raise PortalChanged("the Works-wise Grading Abstract form has no state list or token")
            self._form = {"states": [(v, t) for v, t in states if v.strip().lstrip("-").isdigit() and int(v) > 0],
                          "token": token}
        return self._form

    def _districts(self, state_code):
        raw = text_of(self.client.fetch(DISTRICTS_URL, data={"StateCode": state_code}))
        guard(raw, f"district list of state {state_code}")
        try:
            items = json.loads(raw)
            return [(str(d["Value"]), " ".join(str(d["Text"]).split())) for d in items if str(d["Value"]) != "0"]
        except (ValueError, KeyError, TypeError):
            raise LayoutChanged(f"district list of state {state_code} is not the expected JSON") from None

    def list_districts(self, state):
        code = next((v for v, t in self._start()["states"] if norm(t) == norm(state)), None)
        if code is None:
            raise FilterNotFound(f"state {state!r} is not in the OMMAS state list")
        return [name for _, name in self._districts(code)]

    def list_areas(self, filters):
        window = window_of(filters)
        states = self._start()["states"]
        if filters.get("state"):
            states = [(v, t) for v, t in states if norm(t) == norm(filters["state"])]
            if not states:
                raise FilterNotFound(f"state {filters['state']!r} is not in the OMMAS state list")
        district_found = False
        for scode, sname in states:
            try:
                districts = self._districts(scode)
            except LayoutChanged as e:
                yield SkippedUnit("districts", sname, "layout", str(e))
                continue
            except FetchError as e:
                if e.denied:
                    raise
                yield SkippedUnit("districts", sname, "network", str(e))
                continue
            for dcode, dname in districts:
                if filters.get("district") and norm(dname) != norm(filters["district"]):
                    continue
                district_found = True
                yield Area(levels=[{"level": "state", "code": scode, "name": sname},
                                   {"level": "district", "code": dcode, "name": dname}],
                           code_system="PMGSY",
                           context={"state_code": scode, "state": sname, "district_code": dcode,
                                    "district": dname, "window": window})
        if filters.get("district") and not district_found:
            raise FilterNotFound(f"district {filters['district']!r} is not listed for the chosen state(s)")

    def _report(self, ctx):
        (fy, fm), (ty, tm) = ctx["window"]
        for attempt in (1, 2):
            form = self._start(fresh=attempt == 2)
            data = {"__RequestVerificationToken": form["token"], "StateCode": ctx["state_code"],
                    "DistrictCode": ctx["district_code"], "FrmMonth": fm, "FrmYear": fy, "ToMonth": tm,
                    "ToYear": ty, "Mast_State_Code": 0, "Mast_District_Code": 0, "Mast_Block_Code": 0,
                    "StateName": ctx["state"], "DistName": ctx["district"], "BlockName": 0, "LevelCode": 1,
                    "localizedValue": "en", "FromMonthName": MONTHS[fm - 1], "FromYearName": fy,
                    "ToMonthName": MONTHS[tm - 1], "ToYearName": ty}
            answer = self._page(REPORT_FORM_URL, data=data, where="report form answer")
            m = re.search(r"""<iframe[^>]*\bsrc=["']([^"']*MvcReportViewer\.aspx[^"']*)["']""", answer, re.I)
            if m:
                return urljoin(BASE + "/", html.unescape(m.group(1)))
        raise LayoutChanged("the report form did not answer with a report frame")

    def list_records(self, area):
        ctx = area.context
        report_url = self._report(ctx)
        report = parse_report(self._page(report_url, where="report page"))
        window = ctx["window"]
        win_text = f"{window[0][0]}-{window[0][1]:02d} to {window[1][0]}-{window[1][1]:02d}"
        records, seen = [], set()
        for row in report["roads"]:
            if norm(row["district"]) != norm(ctx["district"]):
                raise LayoutChanged(f"report for {ctx['district']} lists a road in {row['district']}")
            for link in row["links"]:
                for rec in self._road_records(link, row, ctx, window, win_text):
                    if rec["record_id"] in seen:
                        raise LayoutChanged(f"inspection {rec['record_id']} listed twice")
                    seen.add(rec["record_id"])
                    records.append(rec)
        return records

    def _road_records(self, link, row, ctx, window, win_text):
        road, kind = link["road"], link["kind"]
        times, ids = parse_road_page(self._page(link["url"], where=f"road page {road}${kind}"), road, kind)
        details = {}
        chosen = None
        if len(times) == len(ids):
            pairs = list(zip(sorted(times, reverse=True), sorted(ids, key=int, reverse=True)))
            want = [(t, o) for t, o in pairs if in_window(t.date(), window)]
            chosen = []
            for t, o in want:
                try:
                    details[o] = parse_detail(self._page(DETAIL_URL.format(obs=o, kind=kind), where=f"inspection {o}"),
                                              o, kind)
                except LayoutChanged as e:
                    details[o] = e
                    chosen.append((t, o))
                    continue
                if details[o]["day"] != t.date():
                    chosen = None
                    break
                chosen.append((t, o))
        if chosen is None:
            # The dropdowns did not pair up: read every inspection of the road and go by its printed date.
            chosen = []
            for o in sorted(ids, key=int, reverse=True):
                d = details.get(o)
                if d is None:
                    d = details[o] = parse_detail(self._page(DETAIL_URL.format(obs=o, kind=kind),
                                                             where=f"inspection {o}"), o, kind)
                if isinstance(d, LayoutChanged):
                    raise d
                if in_window(d["day"], window):
                    match = [t for t in times if t.date() == d["day"]]
                    chosen.append((match[0] if len(match) == 1 else None, o))
        if len(chosen) != link["count"]:
            raise LayoutChanged(f"road {road}${kind}: the report counts {link['count']} inspections in "
                                f"{win_text}, the road page has {len(chosen)}")
        out = []
        for t, o in chosen:
            out.append(self._record(o, kind, t, details[o], link, row, ctx, win_text))
        return out

    def _record(self, obs, kind, when, detail, link, row, ctx, win_text):
        unreadable = isinstance(detail, LayoutChanged)
        f = {} if unreadable else detail["fields"]
        extra = {"monitor_type": KINDS[kind], "package": row["package"], "sanction_year": row["sanction_year"],
                 "road_name": row["road_name"], "road_length_km": row["road_length_km"],
                 "inspected_at": when.isoformat() if when else None, "report_window": win_text}
        if unreadable:
            extra["detail_unreadable"] = str(detail)
        else:
            extra.update({"chainage_start_km": number(f.get("Start Chainage (in Km.)")),
                          "chainage_end_km": number(f.get("End Chainage (in Km.)")),
                          "grades": detail["grades"]})
        day = when.date() if unreadable and when else (None if unreadable else detail["day"])
        rec = {"scheme": self.id, "record_id": f"{KINDS[kind]}-{obs}",
               "state": f.get("State") or ctx["state"], "district": f.get("District") or ctx["district"],
               "block": row["block"] or None, "parent_id": link["road"],
               "start_date": day.isoformat() if day else None, "stage": f.get("Status") or None,
               "structure_type": f.get("Type") or None,
               "status": None if unreadable else detail["overall"],
               "group_key": f.get("Monitor Name") or None, "group_kind": "quality_monitor",
               "source_url": link["url"], "photo_source": DETAIL_URL.format(obs=obs, kind=kind),
               "extra": extra}
        if not unreadable:
            # Photos read with the detail page, so a full run does not fetch it twice.
            rec["_photos"] = detail["photos"]
        return rec

    def list_photos(self, record):
        if "_photos" in record:
            return record["_photos"]
        url = record.get("photo_source") or ""
        m = re.search(r"/InspDetails/(\d+)\$([SI])$", url)
        if not m:
            raise LayoutChanged(f"record {record.get('record_id')} has no inspection page to read photos from")
        return parse_detail(self._page(url, where=f"inspection {m.group(1)}"), m.group(1), m.group(2))["photos"]
