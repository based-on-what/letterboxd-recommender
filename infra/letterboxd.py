"""
infra/letterboxd.py — Letterboxd scraping client.

Owns: page-count discovery, profile scraping, anti-bot fallback chain.
Does NOT know about recommendations or preferences.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from cache import cache, USER_CACHE_TTL, USER_STALE_CACHE_TTL
from infra.http import (
    CAMOUFOX_TIMEOUT,
    INCIDENT_TRACKER,
    LETTERBOXD_HEADERS,
    LETTERBOXD_HTTP_TIMEOUT,
    letterboxd_limiter,
    tmdb_limiter,
    cloudscraper_session,
    request_with_fallback,
    curl_get,
    camoufox_get,
    session,
)

logger = logging.getLogger("letterboxd-recommender")

_LETTERBOXD_BASE = "https://letterboxd.com"


class LetterboxdClient:
    """Scrapes a Letterboxd user's film list with an anti-bot fallback chain."""

    def __init__(self, max_workers: int = 6):
        self._max_workers = max_workers
        self._last_failures: list = []
        self.used_stale_cache: bool = False

    # ------------------------------------------------------------------
    # Internal HTTP — Letterboxd-specific: circuit breaker + fallbacks
    # ------------------------------------------------------------------
    def _try_fallbacks(self, url, params, headers, attempt, primary_status, failures):
        failures.append(f"attempt={attempt + 1},status={primary_status},source=requests")

        if cloudscraper_session is not None:
            try:
                alt = cloudscraper_session.get(url, params=params, headers=headers, timeout=LETTERBOXD_HTTP_TIMEOUT)
                if alt.status_code == 200:
                    logger.info("Letterboxd succeeded via cloudscraper fallback")
                    return alt
                failures.append(f"attempt={attempt + 1},status={alt.status_code},source=cloudscraper")
            except requests.RequestException as exc:
                failures.append(f"attempt={attempt + 1},error={type(exc).__name__},source=cloudscraper")

        curl_resp = curl_get(url, params, headers, LETTERBOXD_HTTP_TIMEOUT)
        if curl_resp is not None:
            if getattr(curl_resp, 'status_code', None) == 200:
                logger.info("Letterboxd succeeded via curl_cffi fallback")
                return curl_resp
            failures.append(f"attempt={attempt + 1},status={getattr(curl_resp, 'status_code', 'n/a')},source=curl_cffi")

        return None

    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='letterboxd'):
        limiter_map = {'letterboxd': letterboxd_limiter, 'tmdb': tmdb_limiter}
        svc_limiter = limiter_map.get(service)
        if svc_limiter:
            svc_limiter.wait()

        _failures: list = []

        if service == 'letterboxd' and INCIDENT_TRACKER.is_circuit_open():
            _failures.append('circuit=open')
            self._last_failures = _failures
            logger.warning("Letterboxd circuit open; skipping scrape")
            return None

        last_status = None
        merged_headers = dict(session.headers)
        if headers:
            merged_headers.update(headers)

        for attempt in range(max_retries + 1):
            try:

                r = request_with_fallback(url, params, merged_headers, LETTERBOXD_HTTP_TIMEOUT, use_proxy=(service == 'letterboxd'))
                last_status = r.status_code

                if r.status_code == 200:
                    if service == 'letterboxd':
                        INCIDENT_TRACKER.record_letterboxd_result(success=True, status=200)
                        self._last_failures = _failures
                    return r

                if service == 'letterboxd' and r.status_code in (403, 429, 503):
                    fallback = self._try_fallbacks(url, params, merged_headers, attempt, r.status_code, _failures)
                    if fallback:
                        self._last_failures = _failures
                        return fallback

                if r.status_code == 429:
                    time.sleep((attempt + 1) * 1.5)
                else:
                    time.sleep(0.4)

            except requests.RequestException as exc:
                _failures.append(f"attempt={attempt + 1},error={type(exc).__name__},source=requests")
                logger.warning("Request error for %s (attempt %d): %s", url, attempt + 1, exc)
                time.sleep(0.4 * (attempt + 1))

        if service == 'letterboxd':
            camoufox_resp = camoufox_get(url, params, CAMOUFOX_TIMEOUT)
            if camoufox_resp is not None:
                logger.info("Letterboxd succeeded via camoufox (last resort)")
                INCIDENT_TRACKER.record_letterboxd_result(success=True, status=200)
                self._last_failures = _failures
                return camoufox_resp
            _failures.append("last_resort,status=fail,source=camoufox")
            INCIDENT_TRACKER.record_letterboxd_result(success=False, status=last_status)
            self._last_failures = _failures

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_page_count(self, username: str) -> int:
        url = f"{_LETTERBOXD_BASE}/{username}/films/"
        try:
            r = self._safe_get(url, headers=LETTERBOXD_HEADERS)
            if not r:
                return 0
            soup = BeautifulSoup(r.text, 'html.parser')
            pagination = soup.find_all('li', class_='paginate-page')
            if not pagination:
                return 1
            pages = [int(p.get_text(strip=True)) for p in pagination if p.get_text(strip=True).isdigit()]
            return max(pages) if pages else 1
        except Exception as exc:
            logger.error("Error retrieving page count: %s", exc)
            return 0

    def _scrape_page(self, page: int, base_url: str, headers: dict, rating_map: dict) -> list:
        url = f"{base_url}page/{page}/" if page > 1 else base_url
        r = self._safe_get(url, headers=headers)
        if not r:
            return []

        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.find_all('li', class_='poster-container') or soup.find_all('li', class_='griditem')
        films = []
        for item in items:
            try:
                poster_div = item.find(attrs={'data-film-name': True})
                if poster_div:
                    name = poster_div.get('data-film-name')
                else:
                    img = item.find('img', alt=True)
                    if not img:
                        continue
                    alt = img.get('alt', '')
                    m = re.match(r'^Poster for (.+?)(?:\s*\(\d{4}\))?$', alt)
                    name = m.group(1) if m else alt

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
                    films.append({'title': name.strip(), 'rating': rating, 'has_rating': rating > 0})
            except Exception as exc:
                logger.debug("Error processing film item: %s", exc)
        return films

    def get_all_rated_films(self, username: str, max_pages=None, include_unrated: bool = True):
        if not username:
            return [], 0

        self.used_stale_cache = False
        stale_key = f"{username}:pages:stale:v1"

        def load_stale():
            stale = cache.get('user_scrape', stale_key)
            if stale:
                self.used_stale_cache = True
                logger.warning("Serving stale cached profile for %s", username)
                return stale.get('films', []), stale.get('pages', 0)
            return [], 0

        fresh_key = f"{username}:pages:v2"
        cached = cache.get('user_scrape', fresh_key)
        if cached:
            logger.info("Profile for %s loaded from cache", username)
            return cached.get('films', []), cached.get('pages', 0)

        base_url = f"{_LETTERBOXD_BASE}/{username}/films/"
        try:
            pages = self.get_page_count(username)
            if pages <= 0:
                return load_stale()

            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = dict(LETTERBOXD_HEADERS)
            films = []

            logger.info("Scraping %d pages from %s's profile", pages, username)
            with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
                futures = [ex.submit(self._scrape_page, p, base_url, headers, rating_map) for p in range(1, pages + 1)]
                completed = 0
                interval = max(1, pages // 10)
                for f in as_completed(futures):
                    films.extend(f.result())
                    completed += 1
                    if completed % interval == 0 or completed == pages:
                        logger.info("Scrape: %d/%d pages | %d films", completed, pages, len(films))

            if not include_unrated:
                films = [f for f in films if f.get('has_rating')]

            if not films:
                return load_stale()

            cache.set('user_scrape', fresh_key, {'pages': pages, 'films': films}, ttl=USER_CACHE_TTL)
            cache.set('user_scrape', stale_key, {'pages': pages, 'films': films}, ttl=USER_STALE_CACHE_TTL)
            return films, pages

        except Exception as exc:
            logger.error("Error scraping profile: %s", exc)
            return load_stale()
