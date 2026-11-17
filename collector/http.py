"""Polite HTTP client: robots.txt, a delay between requests to each host, retries with backoff.

Each host has its own delay, timeout and number of tries (Client(hosts=...), from the adapter's
hosts attribute). A slow host only slows its own requests. A page answered with 401 or 403 marks
its host as refusing access: later requests to it fail at once, without a request."""
import http.client
import http.cookiejar
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from .store import image_complete

USER_AGENT = "Mozilla/5.0 (compatible; public-records-collector/1.0)"
MAX_PAGE_BYTES = 64 * 1024 * 1024
log = logging.getLogger("collector.http")


ACCESS_STATUSES = (401, 403)


class FetchError(Exception):
    def __init__(self, message, status=None, denied=False):
        super().__init__(message)
        self.status = status
        # True when the host answered a page with 401 or 403 (now or earlier in this run).
        self.denied = denied


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
        # Time of the last request to each host.
        self._last = {}
        # Photo workers spread their first request to a host not seen yet over one delay.
        self._first_offset = None
        self._robots = {}
        self.denied = {}
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.requests = 0

    def settings(self, host):
        """{"delay", "timeout", "retries"} for one host."""
        return self.hosts.get(host) or {"delay": self.delay, "timeout": self.timeout, "retries": self.retries}

    def _wait(self, host=""):
        delay = self.settings(host)["delay"]
        if host not in self._last and self._first_offset is not None:
            self._last[host] = time.monotonic() - delay + delay * self._first_offset
        gap = time.monotonic() - self._last.get(host, float("-inf"))
        if gap < delay:
            time.sleep(delay - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        host = host_of(url)
        if root not in self._robots:
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
        return self._robots[root].can_fetch(USER_AGENT, url)

    def photo_clients(self, n):
        """n clients for downloading photos side by side. Each keeps this client's delay between its own
        requests and shares the robots.txt rules already read. Their first requests are spread over
        one delay after this client's last request, so together they make n requests per delay,
        evenly spaced; call photos_done(clients) afterwards."""
        out = []
        for i in range(n):
            c = Client(self.delay, self.retries, self.timeout, self.max_page_bytes)
            c.hosts = self.hosts
            c._robots = self._robots
            c.denied = self.denied
            c._last = {h: t - self.settings(h)["delay"] + self.settings(h)["delay"] * (i + 1) / n
                       for h, t in self._last.items()}
            c._first_offset = (i + 1) / n
            out.append(c)
        return out

    def photos_done(self, clients):
        """The next page request to a host waits a full delay after the last photo request to it."""
        for c in clients:
            for h, t in c._last.items():
                self._last[h] = max(self._last.get(h, t), t)
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
        last = status = None
        retries = conf["retries"]
        for attempt in range(1, retries + 1):
            self._wait(host)
            self.requests += 1
            try:
                with self.opener.open(urllib.request.Request(url, data=body, headers=headers),
                                      timeout=conf["timeout"]) as r:
                    content = self._read(r, max_bytes, image, conf["timeout"])
                    hdrs = getattr(r, "headers", None)
                    ctype = hdrs.get_content_type() if hdrs is not None and hdrs.get("Content-Type") else None
                    return content, ctype
            except TooLarge as e:
                raise FetchError(f"could not fetch {url}: {e}") from None
            except urllib.error.HTTPError as e:
                # Redirect loops also end here, with the redirect status.
                last, status = f"HTTP {e.code}", e.code
                e.close()
                if e.code in ACCESS_STATUSES and not image:
                    # A login wall or a ban: stop asking this host. One photo answered 401 or 403
                    # is only that photo failing.
                    self.denied[host] = e.code
                    raise FetchError(f"{url} answered HTTP {e.code}: the portal refuses access; "
                                     "no more requests to this host in this run", e.code, denied=True)
                if e.code < 500 and e.code != 429:
                    break
            except ValueError as e:
                raise FetchError(f"could not fetch {url}: bad URL ({e})") from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError,
                    ssl.SSLError, OSError) as e:
                last = str(getattr(e, "reason", e)) or repr(e)
            if attempt < retries:
                wait = 2 ** attempt
                log.warning("%s failed (%s), retry %d/%d in %ds", url, last, attempt, retries - 1, wait)
                time.sleep(wait)
        raise FetchError(f"could not fetch {url}: {last}", status)


class TooLarge(Exception):
    pass


def safe_url(url):
    """Percent-encode spaces, non-ASCII and other characters http.client refuses; keep existing escapes."""
    url = (url or "").strip().replace("\\", "/")
    return urllib.parse.quote(url, safe="%/:=&?~#+!$,;'@()*[]")
