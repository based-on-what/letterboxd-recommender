"""
infra/tmdb.py — TMDB API client.

Owns: authentication, search, movie details, similar movies, watch providers.
No scraping, no streaming logic, no recommendation logic.
"""

import logging

from cache import cache, ONE_DAY
from infra.http import session, tmdb_limiter

logger = logging.getLogger("letterboxd-recommender")

_TMDB_BASE = "https://api.themoviedb.org/3"


class TmdbClient:
    def __init__(self, api_key: str):
        self._auth_warning_emitted = False
        self._api_key = None
        self._auth_params: dict = {}
        self._auth_headers: dict = {}
        self.api_key = api_key  # triggers setter

    @property
    def api_key(self) -> str:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key = value
        token = (value or '').strip()
        if token.startswith('eyJ'):
            self._auth_params = {}
            self._auth_headers = {'Authorization': f'Bearer {token}'}
        else:
            self._auth_params = {'api_key': token} if token else {}
            self._auth_headers = {}

    def _get(self, path: str, params: dict = None):
        auth_params, auth_headers = self._auth_params, self._auth_headers
        merged_params = dict(params or {})
        merged_params.update(auth_params)

        tmdb_limiter.wait()
        try:
            merged_headers = dict(session.headers)
            merged_headers.update(auth_headers)
            r = session.get(
                f"{_TMDB_BASE}{path}",
                params=merged_params,
                headers=merged_headers,
                timeout=12,
            )
            if r.status_code == 200:
                return r
            if not self._auth_warning_emitted:
                self._auth_warning_emitted = True
                logger.warning(
                    "TMDB request failed (status=%d). Verify TMDB_KEY: "
                    "v3 key uses api_key param; v4 token uses Bearer auth.",
                    r.status_code,
                )
            return None
        except Exception as exc:
            logger.debug("TMDB request error: %s", exc)
            return None

    def get_details(self, title: str, year=None, force_refresh: bool = False):
        if not self._api_key:
            logger.warning("TMDB_KEY not configured")
            return None

        key = f"tmdb:search:{title.lower()}:{year or ''}"
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached

        try:
            params = {'query': title}
            if year:
                params['year'] = year
            resp = self._get('/search/movie', params=params)
            if not resp:
                return None
            results = resp.json().get('results')
            if not results:
                return None
            # Prefer exact year match when year is known; fall back to top result.
            best_id = results[0].get('id')
            if year:
                for r in results:
                    if (r.get('release_date') or '')[:4] == str(year):
                        best_id = r.get('id')
                        break
            result = self.get_details_by_id(best_id, force_refresh)
            if result:
                cache.set('tmdb', key, result, ttl=ONE_DAY)
            return result
        except Exception as exc:
            logger.debug("Error searching TMDB: %s", exc)
            return None

    def get_details_by_id(self, movie_id, force_refresh: bool = False):
        if not self._api_key or not movie_id:
            return None

        key = f"id:{movie_id}"
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached

        try:
            resp = self._get(f'/movie/{movie_id}', params={'append_to_response': 'credits,external_ids'})
            if not resp:
                return None

            det = resp.json()
            crew = det.get('credits', {}).get('crew', [])
            director = next((c['name'] for c in crew if c.get('job') == 'Director'), "Unknown")

            out = {
                'tmdb_id': movie_id,
                'title': det.get('title'),
                'original_title': det.get('original_title'),
                'year': (det.get('release_date') or '')[:4],
                'director': director,
                'genres': [g['name'] for g in det.get('genres', [])],
                'poster': f"https://image.tmdb.org/t/p/w500{det['poster_path']}" if det.get('poster_path') else None,
                'rating_tmdb': round(det['vote_average'], 1) if det.get('vote_average') else None,
                'runtime': det.get('runtime') or 0,
                'imdb_id': (det.get('external_ids') or {}).get('imdb_id'),
            }
            cache.set('tmdb', key, out, ttl=ONE_DAY)
            return out
        except Exception as exc:
            logger.error("Error getting TMDB details for id=%s: %s", movie_id, exc)
            return None

    def get_similar(self, movie_id, limit: int = 12) -> list:
        try:
            resp = self._get(f'/movie/{movie_id}/similar')
            if not resp:
                return []
            return resp.json().get('results', [])[:limit]
        except Exception as exc:
            logger.debug("Error fetching similar for %s: %s", movie_id, exc)
            return []

    def get_watch_providers(self, movie_id, country: str) -> dict:
        try:
            resp = self._get(f'/movie/{movie_id}/watch/providers')
            if not resp:
                return {}
            return (resp.json().get('results') or {}).get(country, {})
        except Exception as exc:
            logger.debug("Error fetching providers for %s: %s", movie_id, exc)
            return {}
