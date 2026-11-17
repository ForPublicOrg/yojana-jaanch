"""Polite HTTP client: robots.txt, a delay between requests to each host, retries with backoff.

Each host has its own delay, timeout and number of tries (Client(hosts=...), from the adapter's
hosts attribute). A slow host only slows its own requests. A page answered with 401 or 403 marks
its host as refusing access: later requests to it fail at once, without a request.

After set_photo_rate(), photo requests go through a PhotoThrottle per host (collector/throttle.py),
shared with the photo worker clients: it spaces them, gets the outcome of every attempt and runs
the ban guard. Page requests to a host that has a throttle take a slot from it as well, so pages
and photos together stay within its rate, and its pauses and Retry-After holds apply to pages."""
import email.utils
import http.client
import http.cookiejar
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from .store import image_complete, image_type
from .throttle import MISSING_RUN, RETRY_AFTER_CAP, SUSPECT, HostStopped, PhotoThrottle

USER_AGENT = "Mozilla/5.0 (compatible; public-records-collector/1.0)"
MAX_PAGE_BYTES = 64 * 1024 * 1024
log = logging.getLogger("collector.http")


ACCESS_STATUSES = (401, 403)
# A photo redirected to a URL like this is a file the portal does not have (JSJB: PageNotFound.aspx).
NOT_FOUND_URL = re.compile(r"not[-_]?found|/404(?:[./]|$)", re.I)


class FetchError(Exception):
    def __init__(self, message, status=None, denied=False):
        super().__init__(message)
        self.status = status
        # True when the host answered a page with 401 or 403 (now or earlier in this run).
        self.denied = denied


class GuardStop(FetchError):
    """The ban guard stopped every request to this host (see collector/throttle.py).
    kind is "access" (the portal refuses us) or "network" (still failing after the pauses)."""

    def __init__(self, message, kind):
        super().__init__(message, denied=kind == "access")
        self.kind = kind


def retry_after_seconds(value):
    """Seconds from a Retry-After header (a number or an HTTP date), or None."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class Client:
    def __init__(self, delay=1.0, retries=4, timeout=60, max_page_bytes=MAX_PAGE_BYTES, hosts=None):
        self.max_page_bytes = max_page_bytes
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.hosts = {}
        for host, conf in (hosts or {}).items():
            conf = conf if isinstance(conf, dict) else {"delay": conf}
            self.hosts[host.lower()] = {"delay": max(float(conf.get("delay", delay)), delay),
                                        "timeout": float(conf.get("timeout", timeout)),
                                        "retries": max(1, int(conf.get("retries", retries)))}
            # missing_page(body) -> True for the host's own page for a photo it does not have.
            if callable(conf.get("missing_page")):
                self.hosts[host.lower()]["missing_page"] = conf["missing_page"]
        # Time of the last request to each host, kept by this client only.
        self._last = {}
        self._robots = {}
        self._robots_lock = threading.Lock()
        self.denied = {}
        # host -> PhotoThrottle, shared with the photo worker clients; set up by set_photo_rate().
        self.throttles = {}
        self._throttle_lock = threading.Lock()
        self.photo_rate = None
        self.throttle_options = {}
        # Photo worker clients leave the page delay to the page client and are spaced by the throttle.
        self.photo_worker = False
        # A threading.Event; once set, waits in this client end with throttle.Cancelled.
        self.cancel = None
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.requests = 0

    def settings(self, host):
        """{"delay", "timeout", "retries"} for one host."""
        return self.hosts.get(host) or {"delay": self.delay, "timeout": self.timeout, "retries": self.retries}

    def _wait(self, host=""):
        delay = self.settings(host)["delay"]
        gap = time.monotonic() - self._last.get(host, float("-inf"))
        if gap < delay:
            time.sleep(delay - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        host = host_of(url)
        # Photo workers share the rules; only one of them reads a new host's robots.txt.
        with self._robots_lock:
            if root not in self._robots:
                self._read_robots(root, host)
        return self._robots[root].can_fetch(USER_AGENT, url)

    def _read_robots(self, root, host):
        rp = urllib.robotparser.RobotFileParser()
        robots_url = root + "/robots.txt"
        try:
            self._wait(host)
            with self.opener.open(urllib.request.Request(
                    robots_url, headers={"User-Agent": USER_AGENT}),
                    timeout=self.settings(host)["timeout"]) as r:
                text = r.read().decode("utf-8", "replace")
                # A redirect to an HTML error page means the site has no robots.txt.
                if r.geturl() != robots_url or "<html" in text[:500].lower():
                    text = ""
        except urllib.error.HTTPError as e:
            text = "" if e.code < 500 else None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
            text = ""
        if text is None:
            raise FetchError(f"robots.txt at {robots_url} returned a server error; not crawling")
        rp.parse(text.splitlines())
        self._robots[root] = rp
        log.info("robots.txt for %s: %s", root, "rules loaded" if text.strip() else "none, all allowed")

    def set_photo_rate(self, start, ceiling, clock=None, sleep=None):
        """Send photo requests through a PhotoThrottle per host, starting at `start` a second and
        never above `ceiling`. Called again with the same values (the queue calls it once per area),
        the throttles are kept with their rate and learned ceiling, and only a guard stop is cleared.
        clock and sleep (for tests) are kept for later calls that leave them out."""
        options = dict(self.throttle_options)
        options.update({k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None})
        with self._throttle_lock:
            if self.photo_rate != (start, ceiling) or self.throttle_options != options:
                self.throttles.clear()
            self.photo_rate = (start, ceiling)
            self.throttle_options.clear()
            self.throttle_options.update(options)
            for t in self.throttles.values():
                t.new_run()

    def throttle(self, host, create=True):
        """The PhotoThrottle of a host, or None when photo rates are not set up."""
        t = self.throttles.get(host)
        if t is not None or not create or self.photo_rate is None:
            return t
        with self._throttle_lock:
            if host not in self.throttles:
                start, ceiling = self.photo_rate
                self.throttles[host] = PhotoThrottle(host, start, ceiling, **self.throttle_options)
            return self.throttles[host]

    def photo_clients(self, n):
        """n clients for downloading photos side by side. They share the robots.txt rules, the hosts
        refusing access and the photo throttles with this client, and keep their own cookies. Their
        photo requests are spaced by the throttle only; the page delay stays with this client, whose
        pages take a throttle slot ahead of waiting photos. Call photos_done(clients) afterwards."""
        out = []
        for _ in range(n):
            c = Client(self.delay, self.retries, self.timeout, self.max_page_bytes)
            c.hosts = self.hosts
            c._robots = self._robots
            c._robots_lock = self._robots_lock
            c.denied = self.denied
            c.throttles = self.throttles
            c._throttle_lock = self._throttle_lock
            c.photo_rate = self.photo_rate
            c.throttle_options = self.throttle_options
            c.photo_worker = True
            out.append(c)
        return out

    def photos_done(self, clients):
        """Adds the photo clients' requests to this client's count."""
        self.requests += sum(c.requests for c in clients)

    def new_session(self):
        """Forget cookies so the next request starts a fresh ASP.NET session."""
        self.cookies.clear()

    def fetch(self, url, data=None, referer=None):
        """GET, or POST when data is a dict. Returns bytes."""
        return self._fetch(url, data, referer, max_bytes=self.max_page_bytes)[0]

    def fetch_image(self, url, max_bytes):
        """GET a photo. Returns (bytes, content type). Bodies over max_bytes raise FetchError, not retried."""
        return self._fetch(url, max_bytes=max_bytes, image=True)

    def _read(self, r, max_bytes, image=False, timeout=None):
        hdrs = getattr(r, "headers", None)
        length = hdrs.get("Content-Length") if hdrs else None
        length = int(length) if length and length.strip().isdigit() else None
        if max_bytes and length and length > max_bytes:
            raise TooLarge(f"{length} bytes, over the {max_bytes} byte limit")
        # A server that sends a few bytes at a time must not hold the run for hours.
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout * 3
        parts, size = [], 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            if max_bytes and size > max_bytes:
                raise TooLarge(f"more than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError(f"download took longer than {timeout * 3}s")
        body = b"".join(parts)
        if length is not None and len(body) != length:
            raise http.client.IncompleteRead(body, length - len(body))
        # Without a length or chunked framing, a dropped connection looks like a normal end.
        if image and length is None and not getattr(r, "chunked", False) and not image_complete(body):
            raise http.client.IncompleteRead(body)
        return body

    @staticmethod
    def _photo_outcome(conf, url, final_url, content):
        """Outcome of a photo request that returned a body, for the throttle."""
        if final_url and final_url != url and NOT_FOUND_URL.search(urllib.parse.urlsplit(final_url).path):
            return "missing"
        if image_type(content):
            return "ok"
        if conf.get("missing_page") and conf["missing_page"](content):
            return "missing"
        return "not_image"

    def _fetch(self, url, data=None, referer=None, max_bytes=None, image=False):
        url = safe_url(url)
        host = host_of(url)
        if host in self.denied:
            raise FetchError(f"not fetching {url}: {host} refused access earlier in this run "
                             f"(HTTP {self.denied[host]})", self.denied[host], denied=True)
        conf = self.settings(host)
        if not self.allowed(url):
            raise FetchError(f"robots.txt disallows {url}")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {"User-Agent": USER_AGENT}
        if referer:
            headers["Referer"] = referer
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        # A throttle slot per attempt, for photos and for pages to a host that has a throttle.
        throttle = self.throttle(host, create=image)
        last = status = None
        suspect = False
        retries = conf["retries"]
        for attempt in range(1, retries + 1):
            token = None
            paced = not (image and self.photo_worker)
            if paced:
                self._wait(host)
            try:
                if throttle is not None:
                    token = throttle.acquire(self.cancel) if image else throttle.acquire_page(self.cancel)
            except HostStopped as e:
                raise GuardStop(f"not fetching {url}: {e}", e.kind) from None
            if paced:
                # Time spent waiting for the slot must not shorten the gap before this client's next request.
                self._last[host] = time.monotonic()
            self.requests += 1
            outcome, retry_after, started = "error", None, throttle.clock() if token else 0.0
            result, final, answered = None, False, None
            try:
                with self.opener.open(urllib.request.Request(url, data=body, headers=headers),
                                      timeout=conf["timeout"]) as r:
                    # Time to the response headers: how busy the portal is. The body time also
                    # depends on the file size and on this computer's own bandwidth.
                    answered = throttle.clock() if token else None
                    content = self._read(r, max_bytes, image, conf["timeout"])
                    hdrs = getattr(r, "headers", None)
                    ctype = hdrs.get_content_type() if hdrs is not None and hdrs.get("Content-Type") else None
                    if token and image:
                        geturl = getattr(r, "geturl", None)
                        outcome = self._photo_outcome(conf, url, geturl() if geturl else None, content)
                    else:
                        outcome = "ok"
                    result = content, ctype
            except TooLarge as e:
                # Our own size cap, not the portal's doing.
                outcome = None
                raise FetchError(f"could not fetch {url}: {e}") from None
            except urllib.error.HTTPError as e:
                # Redirect loops also end here, with the redirect status.
                last, status = f"HTTP {e.code}", e.code
                if e.code in (404, 410):
                    outcome = "missing"
                elif e.code in (429, 503):
                    outcome = "throttled"
                    retry_after = retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                elif e.code in ACCESS_STATUSES:
                    outcome = "refused"
                e.close()
                if e.code in ACCESS_STATUSES and not image:
                    # A login wall or a ban: stop asking this host. One photo answered 401 or 403
                    # is only that photo failing; the ban guard watches photo refusals in a row.
                    self.denied[host] = e.code
                    raise FetchError(f"{url} answered HTTP {e.code}: the portal refuses access; "
                                     "no more requests to this host in this run", e.code, denied=True)
                final = e.code < 500 and e.code != 429
            except ValueError as e:
                outcome = None
                raise FetchError(f"could not fetch {url}: bad URL ({e})") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError,
                    ssl.SSLError, OSError) as e:
                last = str(getattr(e, "reason", e)) or repr(e)
            except BaseException:
                # Ctrl+C or a bug here says nothing about the portal.
                outcome = None
                raise
            finally:
                if token:
                    counted = throttle.release(token, outcome,
                                               latency=(answered if answered is not None else throttle.clock()) - started,
                                               retry_after=retry_after, detail=last)
                    suspect = counted == SUSPECT
            if suspect:
                # Too many missing answers in a row to believe: failed, so the next run asks again.
                raise FetchError(f"could not fetch {url}: {last or 'not found'}, after more than {MISSING_RUN} "
                                 "photos in a row answered as missing; counted as failed, not missing")
            if result is not None:
                return result
            if final:
                break
            if attempt < retries:
                wait = 2 ** attempt
                if retry_after:
                    wait = max(wait, min(retry_after, RETRY_AFTER_CAP))
                log.warning("%s failed (%s), retry %d/%d in %ds", url, last, attempt, retries - 1, wait)
                if throttle is not None and image:
                    throttle.sleep_for(wait, self.cancel)
                else:
                    time.sleep(wait)
        raise FetchError(f"could not fetch {url}: {last}", status)


class TooLarge(Exception):
    pass


def safe_url(url):
    """Percent-encode spaces, non-ASCII and other characters http.client refuses; keep existing escapes."""
    url = (url or "").strip().replace("\\", "/")
    return urllib.parse.quote(url, safe="%/:=&?~#+!$,;'@()*[]")
