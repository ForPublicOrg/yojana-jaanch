"""Common interface every scheme adapter implements."""
from dataclasses import dataclass, field


class LayoutChanged(Exception):
    """One page does not look the way the adapter expects. The runner skips that unit."""


class PortalChanged(LayoutChanged):
    """The portal itself looks different (for example the dropdowns are gone). The runner stops."""


class NothingListed(LayoutChanged):
    """Every block and village list read was empty. Plausible for one small area, suspicious for a whole run."""


class FilterNotFound(Exception):
    """A --state/--district/... name did not match any dropdown option."""


@dataclass
class Area:
    """Smallest unit the portal lists records for (for JSJB: one village or ward)."""
    state: str
    district: str
    area_type: str | None
    block: str | None
    village: str
    village_code: str
    codes: dict = field(default_factory=dict)
    # Adapter-private data needed to fetch this area's records.
    context: object = None


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

    def __init__(self, client):
        self.client = client

    def list_areas(self, filters):
        """Yield Area objects matching filters, and SkippedUnit for lists that could not be read."""
        raise NotImplementedError

    def list_records(self, area):
        """Return record dicts for one area, in DATA CONTRACT 1 shape minus photos/first_seen/last_seen."""
        raise NotImplementedError

    def list_photos(self, record):
        """Return [{"url":..., "uploaded_at": ISO or None}] for a record from list_records."""
        raise NotImplementedError
