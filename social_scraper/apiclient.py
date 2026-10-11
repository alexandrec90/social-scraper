"""A `Browser` for public JSON APIs: plain HTTPS GETs, no Chrome.

Bluesky's AppView and Polymarket's Gamma API answer any client, with no key (measured
2026-10-10: `api.bsky.app` served `searchPosts` signed out -- `public.api.bsky.app` did
not, it answered 403 -- and `gamma-api.polymarket.com` served `/events`). A browser
would add a profile and a launch for nothing, so these platforms get this instead.

It speaks the same `Browser` protocol as `PlaywrightBrowser`, so a crawler over an API
is paced by the same `Throttle`, classified the same way, and tested against the same
`conftest.FakeBrowser`. It is a context manager for the same reason: the runner's
browser factory opens whatever it is handed.

Only HTTPS, only GET, and only the hosts it was built for: a URL anywhere else is a
bug in a crawler, not a request to make.
"""

from __future__ import annotations

import http.client
import json
import logging
from collections.abc import Callable
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from social_scraper.fetch import PageResult, Payload

log = logging.getLogger(__name__)

# The same contact convention as the Wikidata client (`sports.wikidata.USER_AGENT`): a
# client name and the project URL, no personal address.
USER_AGENT = "social-scraper/0.1 (https://github.com/alexandrec90/social-scraper)"
TIMEOUT_S = 30.0
TEXT_EXCERPT_CHARS = 3_000
# A response bigger than this is not one of the pages these crawlers ask for.
MAX_BODY_BYTES = 32 * 1024 * 1024

Connect = Callable[[str, float], http.client.HTTPSConnection]


def _connect(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, timeout=timeout)


class ApiClient:
    def __init__(
        self,
        hosts: frozenset[str],
        timeout_s: float = TIMEOUT_S,
        connect: Connect = _connect,
    ) -> None:
        self.hosts = hosts
        self.timeout_s = timeout_s
        self.connect = connect

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def _target(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.hosts:
            raise ValueError(f"not an https URL on {sorted(self.hosts)}: {url}")
        path = parts.path or "/"
        return parts.hostname, path + (f"?{parts.query}" if parts.query else "")

    def get_json(self, url: str) -> PageResult:
        """GET `url`; the parsed body in `data`, or its text when it is not JSON."""
        host, path = self._target(url)
        conn = self.connect(host, self.timeout_s)
        try:
            conn.request(
                "GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            resp = conn.getresponse()
            body = resp.read(MAX_BODY_BYTES + 1)
        except TimeoutError as exc:
            log.warning("request timed out: %s (%s)", url, exc)
            return PageResult(status=None, final_url=url, timed_out=True)
        except (OSError, http.client.HTTPException) as exc:
            # No answer at all: to the throttle, the same as a 5xx.
            return PageResult(status=None, final_url=url, text=f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()
        if len(body) > MAX_BODY_BYTES:
            # Refused whole rather than cut: a truncated body fails to parse and reads as
            # "no JSON", which hides that the page was too big (2026-10-10, 33.6 MB).
            too_big = f"response larger than {MAX_BODY_BYTES} bytes, not read"
            return PageResult(status=resp.status, final_url=url, text=too_big)
        data: Any = None
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        text = "" if data is not None else body[:TEXT_EXCERPT_CHARS].decode("utf-8", "replace")
        return PageResult(status=resp.status, final_url=url, data=data, text=text)

    def get_html(self, url: str) -> PageResult:
        result = self.get_json(url)
        return PageResult(
            status=result.status,
            final_url=result.final_url,
            text=result.text,
            html=result.text,
            timed_out=result.timed_out,
        )

    def capture(
        self,
        url: str,
        want: Callable[[str], bool],
        scrolls: int,
        between: Callable[[], None],
    ) -> PageResult:
        """An API page does not scroll: the one response, as a payload when `want` takes it."""
        result = self.get_json(url)
        payloads = [Payload(url, result.status or 0, result.data)] if want(url) else []
        return PageResult(
            status=result.status,
            final_url=url,
            payloads=payloads if result.data is not None else [],
            text=result.text,
            timed_out=result.timed_out,
        )
