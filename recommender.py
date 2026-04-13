"""
recommender.py — HTTP client, Letterboxd scraper, TMDB enrichment, MovieRecommender.

Provides:
- IncidentTracker: lightweight circuit breaker for Letterboxd scraping.
- HTTP session factory + multi-tier fallback (requests → proxy → cloudscraper → curl_cffi).
- normalize_title: lru_cache'd title normalizer.
- MovieRecommender: full recommendation pipeline.
- enrich_film_task: stand-alone worker used by ThreadPoolExecutor in routes.
"""

import os
import re
import json
import time
import logging
import unicodedata
import queue
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from threading import Lock
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cache import (
    cache, RateLimiter,
    ONE_DAY, ONE_MONTH, SIX_HOURS, TWO_HOURS,
    USER_CACHE_TTL, USER_STALE_CACHE_TTL,
)
from sse import _get_or_create_streams

logger = logging.getLogger("letterboxd-recommender")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IS_DEV = os.getenv('FLASK_ENV') == 'development' or os.getenv('LOCAL_DEV') == 'true'

TMDB_KEY = os.getenv("TMDB_KEY")
MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))

TIMEOUT_WARNING_S = 240
ENRICH_WORKERS = 6
SIMILAR_WORKERS = 4
SIMILAR_RESULTS_PER_FILM = 12

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

PROVIDER_NAME_MAP = {
    'Amazon Prime Video': 'Prime Video',
    'Prime Video': 'Prime Video',
    'Disney+': 'Disney Plus',
    'Disney Plus': 'Disney Plus',
    'Apple TV+': 'Apple TV+',
    'HBO Max': 'HBO Max',
    'Max': 'Max',
}

LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv('LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD', '5'))
LETTERBOXD_CIRCUIT_COOLDOWN_S = int(os.getenv('LETTERBOXD_CIRCUIT_COOLDOWN_S', '180'))

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    from simplejustwatchapi import justwatch as sjw
except ImportError:
    sjw = None
    logger.warning("simplejustwatchapi unavailable; streaming fallback disabled")

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# ---------------------------------------------------------------------------
# IncidentTracker
# ---------------------------------------------------------------------------
class IncidentTracker:
    """Track scrape failures and apply a lightweight circuit breaker."""

    def __init__(self):
        self._lock = Lock()
        self.letterboxd_total_failures = 0
        self.letterboxd_consecutive_failures = 0
        self.letterboxd_last_status = None
        self.circuit_open_until = 0.0

    def is_circuit_open(self):
        with self._lock:
            return time.time() < self.circuit_open_until

    def record_letterboxd_result(self, success, status=None):
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

    def snapshot(self):
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
# HTTP sessions
# ---------------------------------------------------------------------------
def make_session(trust_env=False):
    """
    Build an HTTP session with retry support.

    Configures exponential backoff to smooth over transient network
    failures and rate limiting responses.
    """
    s = requests.Session()
    s.trust_env = trust_env
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504)
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return s


session = make_session(trust_env=False)
proxy_session = make_session(trust_env=True)
cloudscraper_session = cloudscraper.create_scraper() if cloudscraper else None


def _request_with_fallback(http_session, url, params, headers, timeout, service):
    """Send GET request with optional proxy-session fallback for Letterboxd."""
    try:
        return http_session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        if service == 'letterboxd':
            logger.debug(f"Direct Letterboxd request failed, trying proxy-enabled session: {exc}")
            return proxy_session.get(url, params=params, headers=headers, timeout=timeout)
        raise


def _curl_letterboxd_get(url, params, headers, timeout):
    """Optional high-fidelity browser impersonation fallback for Letterboxd."""
    if curl_requests is None:
        return None
    try:
        return curl_requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            impersonate='chrome120',
        )
    except Exception as exc:
        logger.debug(f"curl_cffi fallback error: {exc}")
        return None


# Rate limiters per service
tmdb_limiter = RateLimiter(min_interval=0.1)
letterboxd_limiter = RateLimiter(min_interval=0.15)
streaming_limiter = RateLimiter(min_interval=0.1)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def normalize_title(title):
    """
    Normalize a title string for resilient comparisons.

    Removes diacritics, lowercases, strips special characters, and
    collapses whitespace to produce a stable token for matching.

    Args:
        title: Title to normalize.

    Returns:
        Lowercased, accent-free, alphanumeric title token.

    Example:
        >>> normalize_title("El Señor de los Anillos")
        'el senor de los anillos'
    """
    if not title:
        return ""

    # Decompose Unicode characters and strip diacritics
    title = unicodedata.normalize('NFKD', title)
    title = ''.join(c for c in title if not unicodedata.combining(c))

    # Lowercase text
    title = title.lower()

    # Remove non alphanumeric characters except spaces
    title = re.sub(r'[^a-z0-9\s]', ' ', title)

    # Collapse repeated whitespace
    return re.sub(r'\s+', ' ', title).strip()


def _export_debug_json(filename, data):
    if not IS_DEV:
        return
    try:
        with open(filename, 'w', encoding='utf-8') as debug_file:
            json.dump(data, debug_file, ensure_ascii=False, indent=2)
        logger.info(f"Debug JSON exported to: {filename}")
    except Exception as exc:
        logger.warning(f"Could not export debug JSON {filename}: {exc}")


# ---------------------------------------------------------------------------
# MovieRecommender
# ---------------------------------------------------------------------------
class MovieRecommender:
    """
    Core pipeline that powers Letterboxd-based movie recommendations.

    Combines Letterboxd scraping, TMDB enrichment, preference analysis,
    similar-title discovery, and optional streaming availability lookups.
    """

    _LIMITER_MAP = {
        'tmdb': tmdb_limiter,
        'letterboxd': letterboxd_limiter,
        'streaming': streaming_limiter,
    }

    def __init__(self, country='CL', max_workers=8):
        """Configure the recommender.

        Args:
            country: ISO country code used for streaming availability.
            max_workers: Thread pool size for concurrent tasks.
        """
        self.letterboxd_base = "https://letterboxd.com"
        self.tmdb_base = "https://api.themoviedb.org/3"
        self.tmdb_key = TMDB_KEY
        self.country = country.upper()
        self.max_workers = max_workers
        self._tmdb_auth_warning_emitted = False
        self._letterboxd_last_failures = []
        self.used_stale_profile_cache = False

        self.country_names = {
            'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
            'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru',
            'UY': 'Uruguay', 'IT': 'Italy', 'FR': 'France', 'DE': 'Germany',
            'GB': 'United Kingdom'
        }

    def _tmdb_auth_parts(self):
        """Return params/headers tuple for TMDB auth supporting v3 and v4 keys."""
        token = (self.tmdb_key or '').strip()
        if token.startswith('eyJ'):
            return {}, {'Authorization': f'Bearer {token}'}
        return {'api_key': token}, {}

    def _tmdb_get(self, path, params=None):
        """Issue a TMDB GET request with automatic auth and 401 diagnostics."""
        auth_params, auth_headers = self._tmdb_auth_parts()
        req_params = dict(params or {})
        req_params.update(auth_params)
        req_headers = dict(auth_headers)

        resp = self._safe_get(
            f"{self.tmdb_base}{path}",
            params=req_params,
            headers=req_headers or None,
            service='tmdb'
        )

        if not resp and not self._tmdb_auth_warning_emitted:
            self._tmdb_auth_warning_emitted = True
            logger.warning(
                'TMDB request failed. Verify TMDB_KEY format: v3 API key uses api_key param; v4 token uses Bearer auth.'
            )
        return resp

    def get_country_name(self):
        """Return the human-friendly name for the configured country."""
        return self.country_names.get(self.country, self.country)

    def _try_letterboxd_fallbacks(
        self, url: str, params, merged_headers: dict, attempt: int, primary_status: int
    ):
        """Try cloudscraper then curl_cffi when the primary request gets blocked.

        Records failures into self._letterboxd_last_failures so the post-loop
        diagnostic log in _safe_get has full context.

        Returns:
            A 200 response object on success, or None if all fallbacks failed.
        """
        self._letterboxd_last_failures.append(
            f"attempt={attempt + 1},status={primary_status},source=requests"
        )

        if cloudscraper_session is not None:
            try:
                alt = cloudscraper_session.get(url, params=params, headers=merged_headers, timeout=12)
                if alt.status_code == 200:
                    logger.info("Letterboxd request succeeded via cloudscraper fallback")
                    return alt
                self._letterboxd_last_failures.append(
                    f"attempt={attempt + 1},status={alt.status_code},source=cloudscraper"
                )
                logger.warning(
                    f"Cloudscraper non-200 response ({alt.status_code}) from {url} on attempt {attempt + 1}"
                )
            except requests.RequestException as exc:
                self._letterboxd_last_failures.append(
                    f"attempt={attempt + 1},error={type(exc).__name__},source=cloudscraper"
                )
                logger.debug(f"Cloudscraper request error (attempt {attempt + 1}): {exc}")

        curl_resp = _curl_letterboxd_get(url, params, merged_headers, 12)
        if curl_resp is not None:
            if getattr(curl_resp, 'status_code', None) == 200:
                logger.info("Letterboxd request succeeded via curl_cffi fallback")
                return curl_resp
            self._letterboxd_last_failures.append(
                f"attempt={attempt + 1},status={getattr(curl_resp, 'status_code', 'n/a')},source=curl_cffi"
            )

        return None

    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='generic'):
        """
        Issue an HTTP GET with rate limiting and retries.

        Args:
            url: Target URL.
            params: Optional query parameters.
            headers: Optional request headers.
            max_retries: Number of retry attempts after the first try.
            service: Service key used to pick a rate limiter.

        Returns:
            Response object on success, otherwise None.
        """
        svc_limiter = self._LIMITER_MAP.get(service)

        if svc_limiter:
            svc_limiter.wait()

        if service == 'letterboxd':
            self._letterboxd_last_failures = []
            if INCIDENT_TRACKER.is_circuit_open():
                self._letterboxd_last_failures.append('circuit=open')
                logger.warning('Letterboxd circuit breaker open; skipping live scrape attempt')
                return None

        last_status = None
        merged_headers = {}

        for attempt in range(max_retries + 1):
            try:
                merged_headers = dict(session.headers)
                if headers:
                    merged_headers.update(headers)

                r = _request_with_fallback(session, url, params, merged_headers, 12, service)
                last_status = r.status_code

                if r.status_code == 200:
                    if service == 'letterboxd':
                        INCIDENT_TRACKER.record_letterboxd_result(success=True, status=200)
                    return r

                if service == 'letterboxd' and r.status_code in (403, 429, 503):
                    fallback = self._try_letterboxd_fallbacks(
                        url, params, merged_headers, attempt, r.status_code
                    )
                    if fallback is not None:
                        return fallback

                if r.status_code == 429:
                    sleep_time = (attempt + 1) * 1.5
                    logger.warning(f"Rate limit reached, waiting {sleep_time}s")
                    time.sleep(sleep_time)
                else:
                    logger.warning(
                        f"Non-200 response ({r.status_code}) from {url} on attempt {attempt + 1}"
                    )
                    time.sleep(0.4)

            except requests.RequestException as e:
                if service == 'letterboxd':
                    self._letterboxd_last_failures.append(
                        f"attempt={attempt + 1},error={type(e).__name__},source=requests"
                    )
                logger.warning(f"Request error for {url} (attempt {attempt + 1}): {type(e).__name__}: {e}")
                time.sleep(0.4 * (attempt + 1))

        logger.warning(f"Failed after {max_retries + 1} attempts: {url}")
        if service == 'letterboxd':
            INCIDENT_TRACKER.record_letterboxd_result(success=False, status=last_status)
            logger.warning(
                "Letterboxd scraping failed after retries. "
                f"cloudscraper_available={cloudscraper_session is not None}, "
                f"curl_cffi_available={curl_requests is not None}, "
                f"proxy_env_http={bool(os.getenv('HTTP_PROXY') or os.getenv('http_proxy'))}, "
                f"proxy_env_https={bool(os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'))}, "
                f"user_agent={merged_headers.get('User-Agent', DEFAULT_USER_AGENT)}, "
                f"last_status={last_status}, "
                f"last_failures={self._letterboxd_last_failures[-8:]}"
            )
        return None

    def get_page_count(self, username):
        """
        Return the total number of film pages for a Letterboxd user.

        Args:
            username: Letterboxd username.

        Returns:
            Page count or 0 if the profile cannot be parsed.
        """
        url = f"{self.letterboxd_base}/{username}/films/"

        try:
            r = self._safe_get(url, headers=LETTERBOXD_HEADERS, service='letterboxd')
            if not r:
                return 0

            soup = BeautifulSoup(r.text, 'html.parser')
            pagination = soup.find_all('li', class_='paginate-page')

            if not pagination:
                return 1

            pages = [
                int(p.get_text(strip=True))
                for p in pagination
                if p.get_text(strip=True).isdigit()
            ]

            return max(pages) if pages else 1

        except Exception as e:
            logger.error(f"Error retrieving page count: {e}")
            return 0

    def _scrape_profile_page(self, page: int, base_url: str, headers: dict, rating_map: dict) -> list:
        """Scrape a single Letterboxd profile page and return its film list."""
        url = f"{base_url}page/{page}/" if page > 1 else base_url
        r = self._safe_get(url, headers=headers, service='letterboxd')
        if not r:
            return []

        soup = BeautifulSoup(r.text, 'html.parser')
        items = (soup.find_all('li', class_='poster-container')
                 or soup.find_all('li', class_='griditem'))

        page_films = []
        for item in items:
            try:
                img = item.find('img', alt=True)
                if not img:
                    continue
                name = img.get('alt')
                rating = 0.0
                viewingdata = item.find('p', class_='poster-viewingdata')
                if viewingdata:
                    r_span = viewingdata.find('span', class_='rating')
                    if r_span:
                        for cls in r_span.get('class', []):
                            if cls in rating_map:
                                rating = rating_map[cls]
                                break
                if name:
                    page_films.append({
                        'title': name.strip(),
                        'rating': rating,
                        'has_rating': rating > 0,
                    })
            except Exception as e:
                logger.debug(f"Error processing film item: {e}")
        return page_films

    def get_all_rated_films(self, username, max_pages=None, include_unrated=True):
        """
        Scrape every film listed in a user's profile.

        Improvement: unrated films are also captured so they are treated as
        already seen and not proposed as recommendations.

        Args:
            username: Letterboxd username.
            max_pages: Maximum number of pages to scrape.
            include_unrated: Whether to keep films without user ratings.

        Returns:
            Tuple of (film list, scraped page count).

        Note:
            Unrated films are returned with rating=0 but still marked as seen.
        """
        if not username:
            return [], 0

        self.used_stale_profile_cache = False

        stale_cache_key = f"{username}:pages:stale:v1"

        def load_stale_cache():
            stale_cached = cache.get('user_scrape', stale_cache_key)
            if stale_cached:
                self.used_stale_profile_cache = True
                logger.warning(
                    "Serving stale cached profile for %s due to live scrape failure",
                    username,
                )
                return stale_cached.get('films', []), stale_cached.get('pages', 0)
            return [], 0

        cache_key = f"{username}:pages:v2"
        cached = cache.get('user_scrape', cache_key)
        if cached:
            logger.info(f"Profile for {username} loaded from cache")
            return cached.get('films', []), cached.get('pages', 0)

        base_url = f"{self.letterboxd_base}/{username}/films/"

        try:
            pages = self.get_page_count(username)
            if pages <= 0:
                return load_stale_cache()

            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = dict(LETTERBOXD_HEADERS)

            films = []
            logger.info(f"Scraping {pages} pages from {username}'s profile")

            with ThreadPoolExecutor(max_workers=min(self.max_workers, 6)) as ex:
                futures = [
                    ex.submit(self._scrape_profile_page, p, base_url, headers, rating_map)
                    for p in range(1, pages + 1)
                ]
                completed_pages = 0
                progress_interval = max(1, pages // 10)
                for f in as_completed(futures):
                    page_films = f.result()
                    films.extend(page_films)
                    completed_pages += 1
                    if completed_pages % progress_interval == 0 or completed_pages == pages:
                        logger.info(
                            "Scrape progress: "
                            f"{completed_pages}/{pages} pages "
                            f"({(completed_pages / max(pages, 1)) * 100:.0f}%) | "
                            f"films_collected={len(films)}"
                        )

            if not include_unrated:
                films = [f for f in films if f.get('has_rating')]

            logger.info(f"Total films found: {len(films)} "
                        f"({len([f for f in films if f.get('has_rating')])} rated)")

            if not films:
                return load_stale_cache()

            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=USER_CACHE_TTL)
            cache.set('user_scrape', stale_cache_key, {'pages': pages, 'films': films}, ttl=USER_STALE_CACHE_TTL)

            return films, pages

        except Exception as e:
            logger.error(f"Error scraping profile: {e}")
            return load_stale_cache()

    def get_tmdb_details(self, title, force_refresh=False):
        """
        Look up movie metadata on TMDB by title.

        Args:
            title: Movie title.
            force_refresh: Ignore cached data when True.

        Returns:
            Metadata dictionary or None when no match is found.
        """
        if not self.tmdb_key:
            logger.warning("TMDB_KEY not configured")
            return None

        key = f"tmdb:search:{title.lower()}"

        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached

        try:
            resp = self._tmdb_get('/search/movie', params={'query': title})

            if not resp:
                return None
            body = resp.json()
            results = body.get('results')
            if not results:
                return None
            movie_id = results[0].get('id')
            return self.get_tmdb_details_by_id(movie_id, force_refresh)

        except Exception as e:
            logger.debug(f"Error searching TMDB: {e}")
            return None

    def get_tmdb_details_by_id(self, movie_id, force_refresh=False):
        """
        Retrieve full TMDB metadata for a movie ID.

        Args:
            movie_id: TMDB identifier.
            force_refresh: Ignore cached data when True.

        Returns:
            Dictionary including tmdb_id, titles, year, director, genres,
            poster URL, TMDB rating, and runtime.
        """
        if not self.tmdb_key or not movie_id:
            return None

        key = f"id:{movie_id}"

        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached

        try:
            resp = self._tmdb_get(
                f'/movie/{movie_id}',
                params={'append_to_response': 'credits,external_ids'}
            )

            if not resp:
                return None

            det = resp.json()
            cre = det.get('credits', {})

            director = next(
                (c['name'] for c in cre.get('crew', []) if c.get('job') == 'Director'),
                "Unknown"
            )

            out = {
                'tmdb_id': movie_id,
                'title': det.get('title'),
                'original_title': det.get('original_title'),
                'year': (det.get('release_date') or '')[:4],
                'director': director,
                'genres': [g['name'] for g in det.get('genres', [])] if det.get('genres') else [],
                'poster': f"https://image.tmdb.org/t/p/w500{det.get('poster_path')}" if det.get('poster_path') else None,
                'rating_tmdb': round(det.get('vote_average', 0), 1) if det.get('vote_average') else None,
                'runtime': det.get('runtime') or 0,
                'imdb_id': (det.get('external_ids') or {}).get('imdb_id')
            }

            cache.set('tmdb', key, out, ttl=ONE_DAY)
            cache.set('tmdb', f"tmdb:search:{out['title'].lower()}", out, ttl=ONE_DAY)

            return out

        except Exception as e:
            logger.error(f"Error getting TMDB details: {e}")
            return None

    def analyze_preferences(self, enriched_films):
        """
        Analyze user preferences from enriched films.

        Builds frequency counts to surface favorite genres, recurring
        directors, and most watched decades.

        Args:
            enriched_films: List of movies annotated with metadata.

        Returns:
            Dict with top three genres, directors, and decades.
        """
        genres, directors, decades = [], [], []

        for film in enriched_films:
            if film.get('genres'):
                genres.extend(film['genres'])

            if film.get('director') and film['director'] != "Unknown":
                directors.append(film['director'])

            if film.get('year'):
                try:
                    decade = (int(film['year']) // 10) * 10
                    decades.append(decade)
                except (ValueError, TypeError):
                    pass

        return {
            'genres': [g[0] for g in Counter(genres).most_common(3)],
            'directors': [d[0] for d in Counter(directors).most_common(3)],
            'decades': [f"{d[0]}s" for d in Counter(decades).most_common(3)]
        }

    def get_recommendations(self, enriched_films, count=None, force_refresh=False, request_id=None, username=None):
        """
        Generate recommendations based on the user's films.

        Updated criteria: every film rated 4+ stars (not just the top 10)
        seeds the similar-movie search.

        Steps:
        1. Filter films rated >= 4.0.
        2. For each, fetch similar titles from TMDB.
        3. Drop duplicates and already-seen titles.
        4. Enforce a minimum TMDB rating threshold.
        5. Return all matching results.

        Args:
            enriched_films: User films enriched with metadata.
            count: Optional number of recommendations to limit (None = no limit).
            force_refresh: Ignore cache when True.

        Returns:
            List of recommendation dictionaries with metadata.
        """
        stream_queues = _get_or_create_streams(request_id) if request_id else None

        # Build sets of already-seen films
        seen_ids = set()
        seen_titles_norm = set()

        for film in enriched_films:
            if film.get('tmdb_id'):
                seen_ids.add(str(film['tmdb_id']))

            title_norm = normalize_title(film.get('title', ''))
            if title_norm:
                seen_titles_norm.add(title_norm)

            if film.get('original_title'):
                orig_norm = normalize_title(film.get('original_title'))
                if orig_norm:
                    seen_titles_norm.add(orig_norm)

            rating_display = f"{film.get('user_rating', 0):.1f}★" if film.get('user_rating', 0) > 0 else "Unrated"
            logger.debug(f"  • {film.get('title', 'Untitled')} [{film.get('year', '????')}] "
                         f"- TMDB ID: {film.get('tmdb_id', 'N/A')} - Rating: {rating_display}")

        logger.info(f"Total watched films: {len(enriched_films)} "
                    f"(Unique IDs: {len(seen_ids)}, Normalized titles: {len(seen_titles_norm)})")

        recs = []

        # Primary change: use every film rated 4+ stars
        highly_rated_films = [
            film for film in enriched_films
            if film.get('user_rating', 0) >= 4.0
        ]

        highly_rated_films = sorted(
            highly_rated_films,
            key=lambda x: x.get('user_rating', 0),
            reverse=True
        )

        logger.info(f"\n=== 4+ STAR FILMS USED FOR RECOMMENDATIONS ===")
        logger.info(f"Total films with 4+ stars: {len(highly_rated_films)}")

        # Export JSON locally for development debugging
        if IS_DEV:
            try:
                export_data = {
                    'username': 'user',
                    'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'total_highly_rated': len(highly_rated_films),
                    'films': [
                        {
                            'title': f.get('title'),
                            'year': f.get('year'),
                            'tmdb_id': f.get('tmdb_id'),
                            'user_rating': f.get('user_rating'),
                            'director': f.get('director'),
                            'genres': f.get('genres', []),
                            'rating_tmdb': f.get('rating_tmdb')
                        }
                        for f in highly_rated_films
                    ]
                }

                filename = f"highly_rated_films_{time.strftime('%Y%m%d_%H%M%S')}.json"
                _export_debug_json(filename, export_data)
            except Exception as e:
                logger.warning(f"Could not build debug export payload: {e}")

        for idx, film in enumerate(highly_rated_films, 1):
            logger.debug(f"  {idx}. {film.get('title')} [{film.get('year')}] - "
                         f"{film.get('user_rating')}★ (TMDB: {film.get('rating_tmdb', 'N/A')})")

        logger.info(f"\nStarting parallel processing of {len(highly_rated_films)} films...")

        logger.info(f"\n=== LOOKING FOR SIMILAR MOVIES ===")

        # Process all highly-rated films in parallel
        with ThreadPoolExecutor(max_workers=min(SIMILAR_WORKERS, self.max_workers)) as ex:
            futures = [
                ex.submit(
                    self._get_similar_for_film,
                    film, seen_ids, seen_titles_norm, stream_queues, force_refresh, username,
                )
                for film in highly_rated_films
            ]
            for f in as_completed(futures):
                recs.extend(f.result())

        # Deduplicate recommendations
        unique = {}
        for r in recs:
            key = str(r['tmdb_id'])
            r_title_norm = normalize_title(r['title'])

            if (key not in unique and
                    key not in seen_ids and
                    r_title_norm not in seen_titles_norm):
                unique[key] = r

        # Movies are already filtered by rating in process_similar
        filtered = list(unique.values())

        logger.info(f"\n=== FINAL RESULT ===")
        logger.info(f"Initial candidates (already filtered by rating >={MIN_RECOMMEND_RATING}): {len(recs)}")
        logger.info(f"After deduplication: {len(filtered)}")
        logger.info(f"Returning {len(filtered)} recommendations")

        return filtered if count is None else filtered[:count]

    def _get_similar_for_film(
        self,
        film: dict,
        seen_ids: set,
        seen_titles_norm: set,
        stream_queues,
        force_refresh: bool,
        username,
    ) -> list:
        """Fetch and filter TMDB similar-movie candidates for a single seed film.

        Called in parallel by get_recommendations.  Returns a list of candidate
        dicts (with streaming already populated) that passed all filters and are
        not in the user's already-seen sets.
        """
        if stream_queues is not None:
            stream_queues['status'].put({
                'title': film.get('title', 'Untitled'),
                'user_rating': film.get('user_rating', 0),
                'username': username or 'user',
            })

        if not film.get('tmdb_id'):
            logger.info(f"[SKIP] {film.get('title')} - No TMDB ID")
            return []

        logger.info(f"[PROCESSING] {film.get('title')} (TMDB ID: {film.get('tmdb_id')})")

        cache_key = f"similar:{film.get('tmdb_id')}"
        if not force_refresh:
            cached = cache.get('similar', cache_key)
            if cached:
                logger.info(f"  [CACHED] {film.get('title')} - Found {len(cached)} results in cache")
                return cached

        try:
            resp = self._tmdb_get(f"/movie/{film['tmdb_id']}/similar")
            if not resp:
                return []

            local = []
            results = resp.json().get('results', [])[:SIMILAR_RESULTS_PER_FILM]

            for m in results:
                mid = m.get('id')
                title = m.get('title')
                if not title:
                    continue

                title_norm = normalize_title(title)
                if str(mid) in seen_ids:
                    logger.debug(f"  ✗ {title} - Already seen (ID match)")
                    continue
                if title_norm in seen_titles_norm:
                    logger.debug(f"  ✗ {title} - Already seen (title match)")
                    continue

                det = self.get_tmdb_details_by_id(mid, force_refresh)
                if not det:
                    continue

                if det.get('rating_tmdb') is None or float(det.get('rating_tmdb', 0)) < MIN_RECOMMEND_RATING:
                    logger.debug(
                        f"  ✗ {det.get('title')} - Rating too low "
                        f"({det.get('rating_tmdb', 'N/A')} < {MIN_RECOMMEND_RATING})"
                    )
                    continue

                det_title_norm = normalize_title(det.get('title', ''))
                det_orig_norm = normalize_title(det.get('original_title', ''))
                if (
                    str(det.get('tmdb_id')) in seen_ids
                    or det_title_norm in seen_titles_norm
                    or det_orig_norm in seen_titles_norm
                ):
                    logger.debug(f"  ✗ {det.get('title')} - Already seen (secondary check)")
                    continue

                det['reason'] = f"Since you liked {film.get('title')}"

                streaming = []
                try:
                    if det.get('tmdb_id'):
                        streaming = self.get_streaming_by_tmdb(det.get('tmdb_id'))
                    if not streaming:
                        streaming = self.get_streaming(det.get('title'), det.get('year'))
                except Exception as e:
                    logger.debug(f"Could not fetch streaming for {det.get('title')}: {e}")

                det['streaming'] = streaming
                local.append(det)
                logger.debug(
                    f"  ✓ {det.get('title')} - Valid candidate "
                    f"(rating: {det.get('rating_tmdb')}, streaming: {len(streaming)} providers)"
                )

                if stream_queues is not None:
                    try:
                        stream_queues['recommendations'].put({
                            'title': det.get('title'),
                            'year': det.get('year'),
                            'rating_tmdb': det.get('rating_tmdb'),
                            'poster': det.get('poster'),
                            'director': det.get('director'),
                            'genres': det.get('genres', []),
                            'runtime': det.get('runtime', 0),
                            'streaming': streaming,
                            'reason': det['reason'],
                        })
                    except Exception as exc:
                        logger.debug(f"Unable to stream recommendation event: {exc}")

            cache.set('similar', cache_key, local, ttl=ONE_DAY)
            logger.info(f"  ↳ {len(local)} new movies found")
            return local

        except Exception as e:
            logger.error(f"Error searching similar movies: {e}")
            return []

    def get_streaming(self, title, year=None, force_refresh=False):
        """
        Fetch streaming providers where a movie is available.

        Args:
            title: Movie title.
            year: Optional release year.
            force_refresh: Ignore cache when True.

        Returns:
            List of provider names.
        """
        cache_key = f"{title.lower()}:{year or ''}"

        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            if sjw is None:
                return []
            streaming_limiter.wait()

            entries = sjw.search(
                title,
                country=self.country,
                language='en',
                count=1,
                best_only=True
            )

            if not entries:
                cache.set('streaming', cache_key, [], ttl=SIX_HOURS)
                return []

            # Include all monetization types (flatrate, ads, free, rent, buy)
            providers_raw = []
            for o in entries[0].offers:
                try:
                    if o.package and getattr(o.package, 'name', None):
                        providers_raw.append(o.package.name)
                except Exception:
                    continue

            # Normalize common provider naming
            providers = sorted(list({PROVIDER_NAME_MAP.get(p, p) for p in providers_raw}))

            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers

        except Exception as e:
            logger.debug(f"Error fetching streaming for {title}: {e}")
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []

    def get_streaming_by_tmdb(self, tmdb_id, force_refresh=False):
        """
        Fetch streaming providers using TMDB watch/providers endpoint for exact movie ID.

        Aggregates all monetization types (flatrate, ads, free, rent, buy) for the configured country.
        """
        if not self.tmdb_key or not tmdb_id:
            return []

        cache_key = f"tmdb:{tmdb_id}:{self.country}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            resp = self._tmdb_get(f"/movie/{tmdb_id}/watch/providers")
            if not resp:
                cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
                return []

            data = resp.json() or {}
            country_data = (data.get('results') or {}).get(self.country)
            if not country_data:
                cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
                return []

            names = []
            for k in ('flatrate', 'ads', 'free', 'rent', 'buy'):
                for p in country_data.get(k, []) or []:
                    n = p.get('provider_name') or p.get('providerName')
                    if n:
                        names.append(n)

            # Normalize and dedupe
            providers = sorted({PROVIDER_NAME_MAP.get(n, n) for n in names})

            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers
        except Exception as e:
            logger.debug(f"Error fetching TMDB providers for {tmdb_id}: {e}")
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []


# ---------------------------------------------------------------------------
# Stand-alone enrichment worker
# ---------------------------------------------------------------------------
def enrich_film_task(rec_sys, film):
    """Augment a scraped film with TMDB data."""
    try:
        tmdb_data = rec_sys.get_tmdb_details(film['title'])
        if tmdb_data:
            tmdb_data['user_rating'] = film.get('rating', 0)
            return tmdb_data
        return {
            'tmdb_id': None,
            'title': film['title'],
            'user_rating': film.get('rating', 0),
            'genres': [],
            'director': None
        }
    except Exception as exc:
        logger.debug(f"Error enriching {film.get('title', 'Unknown')}: {exc}")
        return None
