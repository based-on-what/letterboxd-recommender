"""
recommender.py — Public facade + backward-compat re-exports.

MovieRecommender composes LetterboxdClient, TmdbClient, and StreamingClient.
All domain logic lives in services/; all I/O lives in infra/.
This module is the only entry point routes.py and main.py need to touch.
"""

import os

from infra.http import (
    IncidentTracker,
    INCIDENT_TRACKER,
    session,
    LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD,
)
from infra.letterboxd import LetterboxdClient
from infra.tmdb import TmdbClient
from infra.streaming import StreamingClient, sjw
from services.enricher import enrich_film_task
from services.preferences import analyze_preferences
from services.recommender import get_recommendations as _get_recommendations
from utils import normalize_title, IS_DEV, export_debug_json as _export_debug_json

TMDB_KEY = os.getenv("TMDB_KEY")

ENRICH_WORKERS = 6

# Re-exports: main.py and tests import these via `from recommender import ...`
__all__ = [
    "MovieRecommender",
    "enrich_film_task",
    "normalize_title",
    "IS_DEV",
    "_export_debug_json",
    "IncidentTracker",
    "INCIDENT_TRACKER",
    "LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD",
    "session",
    "sjw",
    "ENRICH_WORKERS",
]


# ---------------------------------------------------------------------------
# MovieRecommender — thin orchestrator, same public surface as before
# ---------------------------------------------------------------------------
class MovieRecommender:
    """
    Orchestrates Letterboxd scraping, TMDB enrichment, and recommendation
    generation by composing specialized infrastructure clients.

    Exposes the same public methods as the previous monolithic implementation
    so routes.py and tests require minimal changes.
    """

    _COUNTRY_NAMES = {
        'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
        'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru',
        'UY': 'Uruguay', 'IT': 'Italy', 'FR': 'France', 'DE': 'Germany',
        'GB': 'United Kingdom',
    }

    def __init__(self, country: str = 'CL', max_workers: int = 8):
        self.country = country.upper()
        self.max_workers = max_workers
        self.used_stale_profile_cache = False

        self._lb = LetterboxdClient(max_workers=min(max_workers, 6))
        self._tmdb = TmdbClient(api_key=TMDB_KEY)
        self._streaming = StreamingClient(tmdb_client=self._tmdb, country=self.country)

    # ---- Letterboxd -------------------------------------------------------
    def get_page_count(self, username: str) -> int:
        return self._lb.get_page_count(username)

    def get_all_rated_films(self, username: str, max_pages=None, include_unrated: bool = True):
        result = self._lb.get_all_rated_films(username, max_pages, include_unrated)
        self.used_stale_profile_cache = self._lb.used_stale_cache
        return result

    # ---- TMDB -------------------------------------------------------------
    def get_tmdb_details(self, title: str, year=None, force_refresh: bool = False):
        return self._tmdb.get_details(title, year=year, force_refresh=force_refresh)

    def get_tmdb_details_by_id(self, movie_id, force_refresh: bool = False):
        return self._tmdb.get_details_by_id(movie_id, force_refresh)

    # ---- Streaming --------------------------------------------------------
    def get_streaming(self, title: str, year=None, force_refresh: bool = False) -> list:
        return self._streaming.get_by_title(title, year, force_refresh)

    def get_streaming_by_tmdb(self, tmdb_id, force_refresh: bool = False) -> list:
        return self._streaming.get_by_tmdb_id(tmdb_id, force_refresh)

    # ---- Analysis / recommendations ----------------------------------------
    def analyze_preferences(self, enriched_films: list) -> dict:
        return analyze_preferences(enriched_films)

    def get_recommendations(
        self,
        enriched_films: list,
        count=None,
        force_refresh: bool = False,
        request_id=None,
        username=None,
    ) -> list:
        return _get_recommendations(
            self._tmdb,
            self._streaming,
            enriched_films,
            count=count,
            force_refresh=force_refresh,
            request_id=request_id,
            username=username,
            max_workers=self.max_workers,
        )

    # ---- Utility ----------------------------------------------------------
    def get_country_name(self) -> str:
        return self._COUNTRY_NAMES.get(self.country, self.country)

    # ---- Test-compat surface ----------------------------------------------
    # Tests that patch r._safe_get / r.tmdb_key / r._letterboxd_last_failures
    # still work through these delegating properties and methods.

    @property
    def tmdb_key(self) -> str:
        return self._tmdb.api_key

    @tmdb_key.setter
    def tmdb_key(self, value: str) -> None:
        self._tmdb.api_key = value

    @property
    def _letterboxd_last_failures(self) -> list:
        return self._lb._last_failures

    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='letterboxd'):
        """Delegates to LetterboxdClient._safe_get; kept for test patching."""
        return self._lb._safe_get(url, params=params, headers=headers, max_retries=max_retries, service=service)
