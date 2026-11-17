"""Adaptive photo rate for one host, and the ban guard.

PhotoThrottle decides how many photo requests start per second to one host. The rate r is a
whole number from 1 to the ceiling the caller gives (never above HARD_CAP). Starts are spaced
1/r seconds apart and at most r requests are in flight at once.

Every finished request reports one outcome:

    ok         the photo came, with its latency in seconds
    missing    404 or 410, a redirect to a not-found page, the host's own page for a file it does
               not have (hosts setting missing_page): neutral, up to MISSING_RUN in a row
    throttled  429 or 503 (Retry-After is honoured, capped at RETRY_AFTER_CAP)
    error      timeout, connection reset or refused, another 5xx or 4xx, a cut-off body
    refused    401 or 403
    not_image  the body is HTML or some other non-image
    None       the request says nothing about the portal (our size cap, a bad URL, Ctrl+C)

Page requests to a host with a throttle take a slot too (acquire_page), ahead of waiting photos,
so pages and photos together start at most r requests a second. A page answered 429 or 503
sets the Retry-After hold and halves r; a failed page halves r. Pages never add to the guard.

The rate goes up by 1 after RAMP_AFTER ok photos at the current rate that had to wait for their
slot because of the rate (with no trouble in between; photos that found a free slot neither count
nor reset the count, so the rate never climbs past what the photos actually use), when the median
latency of the last WINDOW ok photos is at most RAMP_LATENCY times the baseline (the median of
the first BASELINE_OKS ok photos this throttle saw), and when at least COOLDOWN ok photos came
since the last back off. It halves on any trouble outcome (a lone failed request after
ISOLATED_AFTER good photos only steps it down by 1), and drops by 1 when that median is
over SLOW_LATENCY times the baseline. Latency is the time to the response headers, so a large
file or this computer's own busy connection does not read as a busy portal. A rate that caused two
back offs for trouble within LEARN_WINDOW good photos is off limits for a while: the ceiling drops
to one below it, and rises by 1 again after RECOVER_AFTER good photos in a row. A slow median only
steps the rate down; it does not lower the ceiling, since a portal busy with other users for a
while says little about the rate. The rate, the baseline and that ceiling last as long as the
throttle does: under the queue, the whole queue process (see new_run).

More than MISSING_RUN missing answers in a row, with no ok photo and no page answered normally in
between, are no longer believed: a firewall or an overloaded host can answer that way. A village
whose photos were never uploaded gives such a run, and the next search page resets it. Each further one counts as trouble, and release()
returns SUSPECT so the caller records the photo as failed, to be asked for again.

The ban guard is always on. GUARD_STREAK trouble outcomes in a row (an ok ends the streak, a
believed missing leaves it as it is), or RATIO_TROUBLE trouble outcomes among the last
RATIO_WINDOW ok or trouble outcomes, pause every request to the host, pages included, for
GUARD_PAUSE seconds and set the rate to 1. If the first PROBATION photo requests after a pause
are all trouble, it pauses again. Trouble after MAX_PAUSES pauses in a row stops the host. When
most of the trouble that tripped the guard is 401 or 403, the host stops at once: the portal is
refusing us, and asking again does not help.
"""
import logging
import statistics
import threading
import time
from collections import Counter, deque

log = logging.getLogger("collector.photos")

HARD_CAP = 16
BASELINE_OKS = 20
WINDOW = 20
RAMP_AFTER = 40
RAMP_LATENCY = 1.5
SLOW_LATENCY = 2.0
COOLDOWN = 120
RETRY_AFTER_CAP = 15 * 60
GUARD_STREAK = 5
GUARD_PAUSE = 10 * 60
PROBATION = 3
MAX_PAUSES = 3
MISSING_RUN = 60
RATIO_WINDOW = 20
RATIO_TROUBLE = 6
# A lone failed request (timeout, reset, cut-off body) after at least ISOLATED_AFTER good photos
# steps the rate down by 1 instead of halving it, and does not count towards a lowered ceiling:
# on JSJB, about 1 in 2,000 large photos arrived cut off with no other sign of trouble.
ISOLATED_AFTER = 50
# A ceiling is lowered only when one rate backs off twice within LEARN_WINDOW good photos, and it
# comes back up by 1 after RECOVER_AFTER good photos in a row without trouble, never above the
# ceiling the caller gave.
LEARN_WINDOW = 2000
RECOVER_AFTER = 3000

OUTCOMES = ("ok", "missing", "throttled", "error", "refused", "not_image")
TROUBLE = ("throttled", "error", "refused", "not_image")
# release() returns this for a missing answer that came after MISSING_RUN others in a row.
SUSPECT = "suspect_missing"
# Longest single sleep, so a stop or Ctrl+C is noticed within about a second.
SLEEP_STEP = 1.0


class HostStopped(Exception):
    """The guard stopped all requests to a host. kind is "access" or "network", as in StopRun."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class Cancelled(Exception):
    """The caller's cancel event was set while waiting for a slot."""


class Token:
    __slots__ = ("started", "generation", "page", "limited")

    def __init__(self, started, generation, page=False, limited=False):
        self.started = started
        self.generation = generation
        self.page = page
        # True when this request had to wait because of the rate itself (spacing or in-flight
        # bound), not a pause. Only such requests count towards going up: a rate the photos never
        # reach has not been tested on the portal.
        self.limited = limited


def _clamp(value, low, high):
    return max(low, min(int(value), high))


class PhotoThrottle:
    def __init__(self, host, start=1, ceiling=1, clock=time.monotonic, sleep=time.sleep):
        self.host = host
        self.clock = clock
        self.sleep = sleep
        self.ceiling = _clamp(ceiling, 1, HARD_CAP)
        self.top = self.ceiling
        self.rate = _clamp(start, 1, self.ceiling)
        # Good photos so far, and since the last trouble outcome.
        self._oks = 0
        self._oks_since_trouble = 0
        self._cond = threading.Condition(threading.Lock())
        self.in_flight = 0
        self._next_start = float("-inf")
        # Retry-After and guard pauses: no request to the host starts before these times.
        self._hold_until = float("-inf")
        self.paused_until = float("-inf")
        # Set by hold() for the collector's outage pause, cleared by unhold().
        self._outage_until = float("-inf")
        # Page requests waiting for a slot; photos let them go first.
        self._pages_waiting = 0
        self._missing_run = 0
        # Last RATIO_WINDOW ok or trouble outcomes, True for trouble.
        self._window = deque(maxlen=RATIO_WINDOW)
        # Requests started before the latest pause report into counts only.
        self._generation = 0
        self._first_oks = []
        self.baseline = None
        self._recent = deque(maxlen=WINDOW)
        self._clean = 0
        self._since_backoff = None
        # Rate -> good-photo count at its last counted back off.
        self._backoffs_at = {}
        self._streak = []
        self._probation = False
        self.pauses_in_a_row = 0
        self.stopped = None
        self.counts = Counter()
        self.max_in_flight = 0

    # Waiting.

    def _nap(self, seconds, cancel):
        while seconds > 0:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            step = min(seconds, SLEEP_STEP)
            self.sleep(step)
            seconds -= step

    def _check(self, cancel):
        if self.stopped:
            raise HostStopped(*self.stopped)
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    def acquire(self, cancel=None):
        """Wait for a photo slot. Returns a token for release(). Raises HostStopped or Cancelled."""
        return self._take(cancel, page=False)

    def acquire_page(self, cancel=None):
        """Wait for a slot for a page request, ahead of waiting photos. Release it with release()."""
        with self._cond:
            self._pages_waiting += 1
        try:
            return self._take(cancel, page=True)
        finally:
            with self._cond:
                self._pages_waiting -= 1
                self._cond.notify_all()

    def _take(self, cancel, page):
        limited = False
        while True:
            with self._cond:
                self._check(cancel)
                now = self.clock()
                held = max(self.paused_until, self._hold_until, self._outage_until)
                until = max(held, self._next_start)
                if now < until:
                    limited = limited or self._next_start > held
                    wait = until - now
                elif self.in_flight >= self.rate or (not page and self._pages_waiting):
                    limited = limited or self.in_flight >= self.rate
                    self._cond.wait(0.05 if self._pages_waiting else 0.5)
                    continue
                else:
                    self.in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self.in_flight)
                    self._next_start = now + 1.0 / self.rate
                    self.counts["pages" if page else "requests"] += 1
                    return Token(now, self._generation, page, limited)
            self._nap(wait, cancel)

    def wait_if_paused(self, cancel=None):
        """Waits out a guard pause, a Retry-After or an outage hold without taking a slot."""
        while True:
            with self._cond:
                self._check(cancel)
                wait = max(self.paused_until, self._hold_until, self._outage_until) - self.clock()
                if wait <= 0:
                    return
            self._nap(wait, cancel)

    def hold(self, seconds):
        """No request to the host starts for this long, or until unhold() (the collector's outage pause)."""
        with self._cond:
            self._outage_until = max(self._outage_until, self.clock() + seconds)

    def unhold(self):
        with self._cond:
            self._outage_until = float("-inf")
            self._cond.notify_all()

    def sleep_for(self, seconds, cancel=None):
        """Sleep between retries in short steps, so a cancel is noticed."""
        self._nap(seconds, cancel)

    # Reporting.

    def release(self, token, outcome, latency=None, retry_after=None, detail=None):
        """Report how a request ended. Returns the outcome as counted: SUSPECT for a missing
        answer that is no longer believed, else the outcome given."""
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"unknown photo outcome {outcome!r}")
        with self._cond:
            self.in_flight -= 1
            self._cond.notify_all()
            # A Retry-After holds the host even when it answers a request from before a pause.
            if outcome == "throttled" and retry_after:
                self._hold(retry_after, "pages" if token.page else "photos")
            if token.page:
                if outcome == "ok" and token.generation == self._generation:
                    # A page answered normally: the host is not refusing us, so a run of missing
                    # photos before it is believed (a village whose photos were never uploaded).
                    self._missing_run = 0
                if outcome in ("throttled", "error") and not self.stopped and token.generation == self._generation:
                    self._back_off(max(1, self.rate // 2), f"a page request: {detail or outcome}")
                return outcome
            if outcome is None:
                return None
            self.counts[outcome] += 1
            if outcome == "ok":
                self._missing_run = 0
            elif outcome == "missing":
                self._missing_run += 1
                if self._missing_run > MISSING_RUN:
                    self.counts[SUSPECT] += 1
                    if not self.stopped and token.generation == self._generation:
                        self._trouble("missing", None, f"{self._missing_run} missing in a row and no photo")
                    return SUSPECT
                return outcome
            if self.stopped or token.generation != self._generation:
                return outcome
            if outcome == "ok":
                self._ok(latency if latency is not None else 0.0, token.limited)
            elif outcome in TROUBLE:
                self._trouble(outcome, retry_after, detail)
            return outcome

    def _hold(self, retry_after, who):
        wait = min(float(retry_after), RETRY_AFTER_CAP)
        until = self.clock() + wait
        if until > self._hold_until:
            self._hold_until = until
            log.warning("%s to %s: the portal asks to wait %.0f s (Retry-After); every request to it waits",
                        who, self.host, wait)

    def _ok(self, latency, limited=True):
        self._streak.clear()
        self._missing_run = 0
        self._window.append("ok")
        if self._probation:
            self._probation = False
            self.pauses_in_a_row = 0
            log.info("photos to %s: answering again after the pause", self.host)
        if self.baseline is None:
            self._first_oks.append(latency)
            if len(self._first_oks) >= BASELINE_OKS:
                self.baseline = statistics.median(self._first_oks)
                self._first_oks = []
        self._recent.append(latency)
        self._oks += 1
        self._oks_since_trouble += 1
        if self.ceiling < self.top and self._oks_since_trouble >= RECOVER_AFTER:
            self.ceiling += 1
            self._oks_since_trouble = 0
            log.info("photos to %s: at most %d a second again (%d photos without trouble)",
                     self.host, self.ceiling, RECOVER_AFTER)
        if limited:
            self._clean += 1
        if self._since_backoff is not None:
            self._since_backoff += 1
        if self.baseline is None or len(self._recent) < WINDOW:
            return
        median = statistics.median(self._recent)
        if median > SLOW_LATENCY * self.baseline and self.rate > 1:
            self._back_off(self.rate - 1, f"median {median:.2f} s of the last {WINDOW} photos is over "
                                          f"{SLOW_LATENCY:g} times the first {self.baseline:.2f} s", learn=False)
            return
        if self._clean >= RAMP_AFTER and self.rate < self.ceiling and median <= RAMP_LATENCY * self.baseline \
                and (self._since_backoff is None or self._since_backoff >= COOLDOWN):
            self._set_rate(self.rate + 1, f"{self._clean} photos waited for a slot without trouble, "
                                          f"median {median:.2f} s")

    def _trouble(self, outcome, retry_after, detail):
        """outcome is one of TROUBLE, or "missing" for a missing answer no longer believed."""
        self._clean = 0
        self._streak.append(outcome)
        self._window.append(outcome)
        what = detail or {"throttled": "HTTP 429 or 503", "refused": "HTTP 401 or 403",
                          "not_image": "not an image"}.get(outcome, "a failed request")
        isolated = outcome == "error" and self._oks_since_trouble >= ISOLATED_AFTER
        self._oks_since_trouble = 0
        if isolated:
            self._back_off(self.rate - 1, f"{what}; one failure after {ISOLATED_AFTER} or more good photos",
                           learn=False)
        else:
            self._back_off(max(1, self.rate // 2), what)
        limit = PROBATION if self._probation else GUARD_STREAK
        if len(self._streak) >= limit:
            self._trip(list(self._streak), "in a row")
            return
        trouble = [k for k in self._window if k != "ok"]
        # An ok ends a streak, so trouble spread between good photos is caught by its share.
        if len(self._window) == RATIO_WINDOW and len(trouble) >= RATIO_TROUBLE:
            self._trip(trouble, f"of the last {RATIO_WINDOW}")

    def _back_off(self, new, reason, learn=True):
        old = self.rate
        self._since_backoff = 0
        if learn:
            last = self._backoffs_at.get(old)
            self._backoffs_at[old] = self._oks
            if last is not None and self._oks - last <= LEARN_WINDOW and old > 1 and self.ceiling > old - 1:
                self.ceiling = old - 1
                log.info("photos to %s: at most %d a second for now (%d a second backed off twice within "
                         "%d photos)", self.host, self.ceiling, old, LEARN_WINDOW)
        self._set_rate(min(new, self.ceiling), reason)

    def _set_rate(self, new, reason):
        new = _clamp(new, 1, self.ceiling)
        if new == self.rate:
            return
        self.rate = new
        self._clean = 0
        self._recent.clear()
        self._cond.notify_all()
        log.info("photos to %s: %d a second (%s)", self.host, new, reason)

    def _trip(self, trouble, where):
        """trouble: the outcomes that tripped the guard; where: "in a row" or "of the last N"."""
        self._streak.clear()
        self._window.clear()
        kinds = ", ".join(f"{k} {n}" for k, n in Counter(trouble).most_common())
        refused = trouble.count("refused")
        if refused * 2 > len(trouble):
            self._stop("access", f"{refused} photo requests to {self.host} {where} were refused (HTTP 401 or 403); "
                                 "the portal refuses access. Stopped; nothing was tried to get past it")
            return
        if self.pauses_in_a_row >= MAX_PAUSES:
            self._stop("network", f"photo requests to {self.host} still fail after {self.pauses_in_a_row} pauses of "
                                  f"{GUARD_PAUSE // 60} minutes ({kinds}); the portal may be blocking this address "
                                  "or be down. Stopped")
            return
        self.pauses_in_a_row += 1
        self._generation += 1
        self._probation = True
        self.paused_until = self.clock() + GUARD_PAUSE
        self._set_rate(1, "ban guard")
        log.warning("photos to %s: %d failed %s (%s); no requests to %s for %d minutes, then 1 a second "
                    "(pause %d of %d)", self.host, len(trouble), where, kinds, self.host, GUARD_PAUSE // 60,
                    self.pauses_in_a_row, MAX_PAUSES)

    def _stop(self, kind, message):
        self.stopped = (kind, message)
        self._cond.notify_all()
        log.warning("photos to %s: %s", self.host, message)

    # Run control and reporting.

    def new_run(self):
        """A new collect run on the same client (the queue): clear a stop, the pause count and the
        trouble counts. The rate, the baseline and a ceiling learned from back offs are kept, so
        under the queue they carry over from area to area while the queue process runs."""
        with self._cond:
            self.stopped = None
            self.pauses_in_a_row = 0
            self._probation = False
            self._streak.clear()
            self._window.clear()

    def snapshot(self):
        with self._cond:
            return {"host": self.host, "rate": self.rate, "ceiling": self.ceiling, "in_flight": self.in_flight,
                    "baseline": self.baseline,
                    "median": statistics.median(self._recent) if self._recent else None,
                    "paused": self.clock() < self.paused_until, "pauses_in_a_row": self.pauses_in_a_row,
                    "stopped": self.stopped, "counts": dict(self.counts)}

    def summary(self):
        s = self.snapshot()
        c = s["counts"]
        parts = [f"photos to {self.host}: {s['rate']} a second (at most {s['ceiling']})"]
        parts.append(", ".join(f"{c.get(k, 0)} {k.replace('_', ' ')}" for k in OUTCOMES)
                     + (f" ({c[SUSPECT]} of the missing not believed)" if c.get(SUSPECT) else ""))
        if s["median"] is not None and s["baseline"] is not None:
            parts.append(f"median {s['median']:.2f} s, first {s['baseline']:.2f} s")
        if s["paused"]:
            parts.append("paused by the ban guard")
        if s["stopped"]:
            parts.append("stopped")
        return "; ".join(parts)
