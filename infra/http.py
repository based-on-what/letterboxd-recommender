"""
infra/http.py — HTTP transport layer.

Owns: sessions, retry config, circuit breaker, rate limiters,
and the fallback strategy helpers (cloudscraper / curl_cffi / camoufox).
Nothing here knows about business logic or caching.
"""

import os
import time
import logging
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cache import RateLimiter

logger = logging.getLogger("letterboxd-recommender")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv('LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD', '5'))
LETTERBOXD_CIRCUIT_COOLDOWN_S = int(os.getenv('LETTERBOXD_CIRCUIT_COOLDOWN_S', '180'))

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)

LETTERBOXD_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'DNT': '1',
}

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import cloudscraper as _cloudscraper_lib
except ImportError:
    _cloudscraper_lib = None

try:
    from curl_cffi import requests as _curl_requests
except ImportError:
    _curl_requests = None

try:
    from camoufox.sync_api import Camoufox as _Camoufox
except ImportError:
    _Camoufox = None


# ---------------------------------------------------------------------------
# IncidentTracker — circuit breaker for Letterboxd scraping
# ---------------------------------------------------------------------------
class IncidentTracker:
    def __init__(self):
        self._lock = Lock()
        self.letterboxd_total_failures = 0
        self.letterboxd_consecutive_failures = 0
        self.letterboxd_last_status = None
        self.circuit_open_until = 0.0

    def is_circuit_open(self) -> bool:
        with self._lock:
            return time.time() < self.circuit_open_until

    def record_letterboxd_result(self, success: bool, status=None) -> None:
        with self._lock:
            if success:
                self.letterboxd_consecutive_failures = 0
                self.letterboxd_last_status = 200
                return
            self.letterboxd_total_failures += 1
            self.letterboxd_consecutive_failures += 1
            self.letterboxd_last_status = status
            if self.letterboxd_consecutive_failures >= LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD:
                self.circuit_open_until = time.time() + LETTERBOXD_CIRCUIT_COOLDOWN_S

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            open_now = now < self.circuit_open_until
            return {
                'letterboxd_total_failures': self.letterboxd_total_failures,
                'letterboxd_consecutive_failures': self.letterboxd_consecutive_failures,
                'letterboxd_last_status': self.letterboxd_last_status,
                'letterboxd_circuit_open': open_now,
                'letterboxd_circuit_retry_after_s': max(0, int(self.circuit_open_until - now)) if open_now else 0,
            }


INCIDENT_TRACKER = IncidentTracker()

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
def make_session(trust_env: bool = False) -> requests.Session:
    s = requests.Session()
    s.trust_env = trust_env
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        # 429 and 503 are handled explicitly by _safe_get + circuit breaker.
        # Including them here causes urllib3 to silently retry, which
        # (a) amplifies requests against a throttling endpoint and
        # (b) masks the failure from IncidentTracker (status becomes None).
        status_forcelist=(500, 502, 504),
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return s


session = make_session(trust_env=False)
proxy_session = make_session(trust_env=True)
cloudscraper_session = _cloudscraper_lib.create_scraper() if _cloudscraper_lib else None

# ---------------------------------------------------------------------------
# Rate limiters — one per external service
# ---------------------------------------------------------------------------
tmdb_limiter = RateLimiter(min_interval=0.1)
letterboxd_limiter = RateLimiter(min_interval=0.15)
streaming_limiter = RateLimiter(min_interval=0.1)

# ---------------------------------------------------------------------------
# Low-level request helpers
# ---------------------------------------------------------------------------
def request_with_fallback(url: str, params, headers, timeout: int, use_proxy: bool = False):
    """GET with optional proxy-session fallback on network error."""
    try:
        return session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        if use_proxy:
            logger.debug("Direct request failed, trying proxy session: %s", exc)
            return proxy_session.get(url, params=params, headers=headers, timeout=timeout)
        raise


def curl_get(url: str, params, headers, timeout: int):
    """curl_cffi fallback — high-fidelity browser impersonation."""
    if _curl_requests is None:
        return None
    try:
        return _curl_requests.get(url, params=params, headers=headers, timeout=timeout, impersonate='chrome120')
    except Exception as exc:
        logger.debug("curl_cffi error: %s", exc)
        return None


def camoufox_get(url: str, params, timeout: int):
    """Camoufox fallback — full headless Firefox, last resort."""
    if _Camoufox is None:
        return None
    try:
        from urllib.parse import urlencode
        full_url = f"{url}?{urlencode(params)}" if params else url
        with _Camoufox(headless=True, geoip=True) as browser:
            page = browser.new_page()
            page.goto(full_url, timeout=timeout * 1000)
            html = page.content()

        class _FakeResponse:
            status_code = 200
            text = html

        return _FakeResponse()
    except Exception as exc:
        logger.debug("camoufox error: %s", exc)
        return None
