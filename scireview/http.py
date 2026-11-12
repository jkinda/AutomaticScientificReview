"""A polite HTTP client: timeouts, retries with backoff, and per-host pacing."""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import requests

from . import __version__

# Minimum seconds between two requests to the same host. arXiv asks for 3 s.
HOST_MIN_INTERVAL = {
    "export.arxiv.org": 3.1,
    "arxiv.org": 3.1,
    "api.semanticscholar.org": 1.1,
    "api.openalex.org": 0.15,
    "api.crossref.org": 0.3,
}
DEFAULT_INTERVAL = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, timeout: float = 30.0, max_retries: int = 4) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        contact = os.environ.get("SCIREVIEW_CONTACT_EMAIL")
        ua = f"scireview/{__version__}"
        if contact:  # OpenAlex/Crossref "polite pool" — only if the user opts in
            ua += f" (mailto:{contact})"
        self.session.headers["User-Agent"] = ua
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _pace(self, host: str) -> None:
        interval = HOST_MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
        with self._lock:
            wait = self._last.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        host = urlparse(url).netloc
        kwargs.setdefault("timeout", self.timeout)
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._pace(host)
            try:
                resp = self.session.get(url, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.max_retries:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS or attempt == self.max_retries:
                    resp.raise_for_status()
                    return resp
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")
