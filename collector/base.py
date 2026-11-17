"""Common interface every scheme adapter implements. See collector/README.md, "Adding a scheme"."""
import hashlib
import hmac
import unicodedata
from dataclasses import dataclass, field


class LayoutChanged(Exception):
    """One page does not look the way the adapter expects. The runner skips that unit."""


class PortalChanged(LayoutChanged):
    """The portal itself looks different (for example the dropdowns are gone). The runner stops."""


class NothingListed(LayoutChanged):
    """Every block and village list read was empty. Plausible for one small area, suspicious for a whole run."""


class FilterNotFound(Exception):
    """A --state/--district/... name did not match any dropdown option."""


class AccessDenied(Exception):
    """The portal asks for a login, shows a CAPTCHA or a bot check, or answers 401 or 403 to a page.
    The runner stops at once and reports it; nothing tries to get past it."""


# Record fields every scheme may fill (DATA CONTRACT 1 plus the optional fields of docs/SCHEMES.md
# section 3). Unknown keys are kept but the checker ignores them; put anything else under "extra".
PHOTOS_STATES = ("fetched", "not_fetched", "none_on_portal")
AMOUNT_KINDS = ("approved", "spent", "instalment")


@dataclass
class Area:
    """Smallest unit the portal lists records for (JSJB: one village or ward; others: a gram
    panchayat, a road, a district).

    Two ways to name an area:
    - JSJB style: state, area_type and village_code (levels left empty). The coverage key is
      state|area type|village code, as before.
    - General: levels, a list of {"level": "state", "code": "26", "name": "Haryana"} from the top
      down, plus code_system ("LGD", "NREGA", "PMGSY", or the portal's own name). The coverage key
      is scheme|code_system|code/code/... . The old name fields are filled from levels named
      state, district, block and village when they are not given, and village_code and village
      from the last level."""
    state: str | None = None
    district: str | None = None
    area_type: str | None = None
    block: str | None = None
    village: str | None = None
    village_code: str | None = None
    codes: dict = field(default_factory=dict)
    # Adapter-private data needed to fetch this area's records.
    context: object = None
    levels: list = field(default_factory=list)
    code_system: str | None = None

    def __post_init__(self):
        if not self.levels:
            return
        clean = []
        for lv in self.levels:
            if not isinstance(lv, dict) or not lv.get("level") or lv.get("code") in (None, ""):
                raise ValueError(f"each level needs a level name and a code, not {lv!r}")
            clean.append({"level": str(lv["level"]), "code": str(lv["code"]),
                          "name": None if lv.get("name") is None else str(lv["name"])})
        self.levels = clean
        by_level = {lv["level"].casefold(): lv for lv in clean}
        for name in ("state", "district", "block", "village"):
            if getattr(self, name) is None and name in by_level:
                setattr(self, name, by_level[name]["name"])
        if self.village_code in (None, ""):
            self.village_code = clean[-1]["code"]
        if self.village is None:
            self.village = clean[-1]["name"]

    def path(self):
        """The level path, for both styles. JSJB areas get theirs from the old fields."""
        if self.levels:
            return list(self.levels)
        out = []
        for level, name, code in (("state", self.state, self.codes.get("state_code")),
                                  ("district", self.district, self.codes.get("district_code")),
                                  ("area_type", self.area_type, self.codes.get("area_code")),
                                  ("block", self.block, self.codes.get("block_code")),
                                  ("village", self.village, self.village_code)):
            if name is not None or code is not None:
                out.append({"level": level, "code": None if code is None else str(code), "name": name})
        return out


@dataclass
class SkippedUnit:
    """A list (states, districts, blocks or villages of one parent) that could not be read.

    kind is "network" or "layout"; the runner records it and carries on."""
    level: str
    name: str
    kind: str
    error: str


class SchemeAdapter:
    id = ""
    name = ""
    portal_url = ""
    # Filter names list_areas understands; each becomes a --<name> option of the collector and a
    # key a plan unit may set. A value of None means all.
    filters = ("state", "district", "area", "block", "village")
    # Per-host request settings: {"host": seconds} or {"host": {"delay": s, "timeout": s, "retries": n}}.
    # Hosts not listed use the --delay (default 1 second), 60 s timeout and 4 tries. A delay below
    # --delay is raised to it.
    hosts = {}
    # True when list_records puts everything list_photos needs into the record (for example under
    # "photo_source"), so records can be saved first and photos fetched by a later --photos pass.
    supports_list_only = False
    # Queue plans (collector/queue_runner.py). plan_levels: the filters that name a plan unit, in
    # order; None means all of filters. area_types: when set, a unit's "area" may be left out or
    # "both", meaning each of these in turn (JSJB: Rural, Urban). A unit with
    # "district": "remaining" needs a list_districts(state) method returning the portal's names.
    plan_levels = None
    area_types = ()

    def __init__(self, client):
        self.client = client
        # Set by the runner from data/state/<id>.salt; used by ref() for beneficiary_ref.
        self.salt = None

    def list_areas(self, filters):
        """Yield Area objects matching filters, and SkippedUnit for lists that could not be read."""
        raise NotImplementedError

    def list_records(self, area):
        """Return record dicts for one area, in DATA CONTRACT 1 shape minus photos/first_seen/last_seen."""
        raise NotImplementedError

    def list_photos(self, record):
        """Return photo entries for a record from list_records (or, in a --photos pass, as saved):
        [{"url": ..., "uploaded_at": ISO or None}] or [{"data": bytes, ...}], each optionally with
        lat, lon, taken_at, stage and label."""
        raise NotImplementedError

    def ref(self, value):
        """Salted hash of a registration or job card number, for beneficiary_ref. The number itself
        is never stored. The salt is per data folder and scheme, so refs do not match across folders."""
        if self.salt is None:
            raise RuntimeError("no salt set; the runner sets adapter.salt before collecting")
        text = unicodedata.normalize("NFKC", " ".join(str(value).split())).casefold()
        return hmac.new(self.salt.encode(), text.encode(), hashlib.sha256).hexdigest()[:32]
