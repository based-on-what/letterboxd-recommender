"""
services/enricher.py — TMDB enrichment helpers.

enrich_film_task: backward-compat signature (rec_sys, film) used by routes.
batch_enrich: cleaner API operating directly on TmdbClient.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("letterboxd-recommender")


def enrich_film_task(rec_sys, film: dict):
    """Augment a scraped film dict with TMDB metadata.

    Kept with the original (rec_sys, film) signature so existing routes
    and tests require no change at call sites.
    """
    try:
        data = rec_sys.get_tmdb_details(film['title'], year=film.get('year'))
        if data:
            data['user_rating'] = film.get('rating', 0)
            return data
        return {
            'tmdb_id': None,
            'title': film['title'],
            'user_rating': film.get('rating', 0),
            'genres': [],
            'director': None,
        }
    except Exception as exc:
        logger.debug("Error enriching %s: %s", film.get('title', 'Unknown'), exc)
        return None


def batch_enrich(tmdb_client, films: list, max_workers: int = 6) -> list:
    """Enrich a list of scraped films concurrently using a TmdbClient directly."""
    enriched = []

    def _enrich(film):
        try:
            data = tmdb_client.get_details(film['title'], year=film.get('year'))
            if data:
                data['user_rating'] = film.get('rating', 0)
                return data
            return {
                'tmdb_id': None,
                'title': film['title'],
                'user_rating': film.get('rating', 0),
                'genres': [],
                'director': None,
            }
        except Exception as exc:
            logger.debug("Error enriching %s: %s", film.get('title', 'Unknown'), exc)
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_enrich, film) for film in films]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                enriched.append(result)

    return enriched
