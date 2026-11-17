"""A made-up scheme used by the tests, and a short example of the adapter interface for schemes
other than JSJB: areas named by a level path, records listed first and photos fetched later
(supports_list_only), photos given as URLs or as inline bytes, with GPS, stage and label.

The portal is fake (FakeDemoPortal); nothing here makes a network request."""
import json
import urllib.parse

from collector.base import AccessDenied, Area, LayoutChanged, SchemeAdapter, SkippedUnit

BASE = "https://demo.example.invalid"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PHOTO_HOST = "photos.demo.example.invalid"

# Two districts, three gram panchayats; GP 103 has no records.
TREE = {"1": ("North", {"101": "Alpha GP", "102": "Beta GP"}), "2": ("South", {"103": "Gamma GP"})}
WORKS = {
    "101": [{"id": "W1", "cost": 120000.0, "stage": "completed", "lat": 15.5, "lon": 73.8},
            {"id": "W2", "cost": 5000.0, "stage": "plinth", "lat": 15.5, "lon": 73.8}],
    "102": [{"id": "W3", "cost": 99.5, "stage": "completed", "lat": None, "lon": None}],
    "103": [],
}


def whole_jpeg(tag):
    return JPEG + tag.encode() + b"\xff\xd9"


class FakeDemoPortal:
    """faults: {url: exception or "login"} applied on every request to that URL."""

    def __init__(self, faults=None):
        self.faults = dict(faults or {})
        self.requests = []
        self.denied = {}

    def _fault(self, url):
        f = self.faults.get(url)
        if f == "login":
            return b"<html><form id='login'>Sign in</form></html>"
        if isinstance(f, BaseException):
            raise f
        return None

    def fetch(self, url, data=None, referer=None):
        self.requests.append(url)
        hit = self._fault(url)
        if hit is not None:
            return hit
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        path = urllib.parse.urlsplit(url).path
        if path == "/tree":
            return json.dumps({d: {"name": n, "gps": gps} for d, (n, gps) in TREE.items()}).encode()
        if path == "/works":
            return json.dumps(WORKS[q["gp"]]).encode()
        if path == "/detail":
            work = q["work"]
            # W2 carries its photo inline, the others as links on the photo host.
            if work == "W2":
                return json.dumps({"inline": [whole_jpeg("W2-inline").hex()]}).encode()
            # W1 and W3 share one photo: the reuse the checker should find.
            return json.dumps({"links": [f"https://{PHOTO_HOST}/p/shared.jpg", f"https://{PHOTO_HOST}/p/{work}.jpg"]}).encode()
        raise AssertionError(f"unexpected url {url}")

    def fetch_image(self, url, max_bytes):
        self.requests.append(url)
        self._fault(url)
        return whole_jpeg(url.rsplit("/", 1)[-1]), "image/jpeg"


class DemoAdapter(SchemeAdapter):
    """Field mapping (portal field -> common field):

    | portal | common |
    |---|---|
    | work id | record_id |
    | district, GP | levels, district, village |
    | cost | amount (amount_kind "approved") |
    | stage | stage |
    | GP code | group_key (group_kind "gp") |
    | detail page URL | photo_source |
    | anything else | extra |
    """
    id = "demo"
    name = "Demo scheme (tests only)"
    portal_url = BASE
    filters = ("state", "district", "gp")
    plan_levels = ("state", "district")
    hosts = {"demo.example.invalid": 1.0, PHOTO_HOST: {"delay": 2.0, "timeout": 90, "retries": 2}}
    supports_list_only = True

    def list_areas(self, filters):
        raw = self.client.fetch(f"{BASE}/tree")
        if b"login" in raw:
            raise AccessDenied("the area list asks for a login")
        try:
            tree = json.loads(raw)
        except ValueError:
            raise LayoutChanged("area tree is not JSON") from None
        for d, info in tree.items():
            if filters.get("district") and filters["district"].casefold() != info["name"].casefold():
                continue
            if not info["gps"]:
                yield SkippedUnit("gram panchayats", info["name"], "layout", "empty list")
                continue
            for g, name in info["gps"].items():
                if filters.get("gp") and filters["gp"].casefold() != name.casefold():
                    continue
                yield Area(levels=[{"level": "state", "code": "30", "name": "Demo State"},
                                   {"level": "district", "code": d, "name": info["name"]},
                                   {"level": "gp", "code": g, "name": name}],
                           code_system="LGD", context=g)

    def list_records(self, area):
        raw = self.client.fetch(f"{BASE}/works?gp={area.context}")
        try:
            works = json.loads(raw)
        except ValueError:
            raise LayoutChanged("works list is not JSON") from None
        out = []
        for w in works:
            out.append({"scheme": self.id, "record_id": w["id"], "state": area.state, "district": area.district,
                        "village": area.village, "village_code": area.village_code,
                        "lat": w["lat"], "lon": w["lon"], "stage": w["stage"],
                        "amount": w["cost"], "amount_kind": "approved",
                        "group_key": area.village_code, "group_kind": "gp",
                        "photo_source": f"{BASE}/detail?work={w['id']}", "source_url": BASE,
                        "extra": {"portal_stage_label": w["stage"]}})
        return out

    def list_photos(self, record):
        raw = self.client.fetch(record["photo_source"])
        detail = json.loads(raw)
        photos = [{"url": u, "uploaded_at": "2026-05-01T10:00:00", "stage": record.get("stage"),
                   "lat": record.get("lat"), "lon": record.get("lon"), "label": "site photo"}
                  for u in detail.get("links", [])]
        photos += [{"data": bytes.fromhex(h), "taken_at": "2026-05-02T09:00:00", "label": "inline"}
                   for h in detail.get("inline", [])]
        return photos
