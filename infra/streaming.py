"""
infra/streaming.py — Streaming availability client.

Two strategies: JustWatch title search, TMDB watch/providers by ID.
Falls back gracefully when simplejustwatchapi is not installed.
"""

import logging

from cache import cache, SIX_HOURS, TWO_HOURS
from infra.http import streaming_limiter

logger = logging.getLogger("letterboxd-recommender")

PROVIDER_NAME_MAP = {
    'Amazon Prime Video': 'Prime Video',
    'Prime Video': 'Prime Video',
    'Disney+': 'Disney Plus',
    'Disney Plus': 'Disney Plus',
    'Apple TV+': 'Apple TV+',
    'HBO Max': 'HBO Max',
    'Max': 'Max',
}

try:
    from simplejustwatchapi import justwatch as sjw
except ImportError:
    sjw = None
    logger.warning("simplejustwatchapi unavailable; JustWatch fallback disabled")


class StreamingClient:
    """Resolves streaming providers for a movie via JustWatch and/or TMDB."""

    def __init__(self, tmdb_client, country: str = 'CL'):
        self._tmdb = tmdb_client
        self._country = country

    def get_by_title(self, title: str, year=None, force_refresh: bool = False) -> list:
        cache_key = f"{title.lower()}:{year or ''}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            if sjw is None:
                return []
            streaming_limiter.wait()
            entries = sjw.search(title, country=self._country, language='en', count=1, best_only=True)
            if not entries:
                cache.set('streaming', cache_key, [], ttl=SIX_HOURS)
                return []

            names = []
            for o in entries[0].offers:
                try:
                    if o.package and getattr(o.package, 'name', None):
                        names.append(o.package.name)
                except Exception:
                    continue

            providers = sorted({PROVIDER_NAME_MAP.get(p, p) for p in names})
            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers
        except Exception as exc:
            logger.debug("Error fetching streaming for %s: %s", title, exc)
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []

    def get_by_tmdb_id(self, tmdb_id, force_refresh: bool = False) -> list:
        if not self._tmdb.api_key or not tmdb_id:
            return []

        cache_key = f"tmdb:{tmdb_id}:{self._country}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            country_data = self._tmdb.get_watch_providers(tmdb_id, self._country)
            if not country_data:
                cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
                return []

            names = []
            for k in ('flatrate', 'ads', 'free', 'rent', 'buy'):
                for p in country_data.get(k, []) or []:
                    n = p.get('provider_name') or p.get('providerName')
                    if n:
                        names.append(n)

            providers = sorted({PROVIDER_NAME_MAP.get(n, n) for n in names})
            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers
        except Exception as exc:
            logger.debug("Error fetching TMDB providers for %s: %s", tmdb_id, exc)
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []
