"""
services/enricher.py — TMDB enrichment helpers.

enrich_film_task: backward-compat signature (rec_sys, film) used by routes.
batch_enrich: cleaner API operating directly on TmdbClient.
Both are thin wrappers over the single _enrich implementation.
"""

import logging
from concurrent.futures import as_completed
from typing import Callable, Optional

from executors import WORK_EXECUTOR

logger = logging.getLogger("letterboxd-recommender")


def _enrich(get_details: Callable, film: dict) -> Optional[dict]:
    """Augment a scraped film dict with TMDB metadata via *get_details*.

    Returns the enriched dict, a minimal fallback dict when TMDB has no
    match, or None on error.
    """
    try:
        data = get_details(film['title'], year=film.get('year'))
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


def enrich_film_task(rec_sys, film: dict) -> Optional[dict]:
    """Backward-compat wrapper: enrich through a MovieRecommender facade."""
    return _enrich(rec_sys.get_tmdb_details, film)


def batch_enrich(tmdb_client, films: list, max_workers: int = 6) -> list:
    """Enrich a list of scraped films concurrently using a TmdbClient directly.

    max_workers is kept for backward compatibility; work now runs on the
    shared WORK_EXECUTOR pool.
    """
    enriched = []
    futures = [WORK_EXECUTOR.submit(_enrich, tmdb_client.get_details, film) for film in films]
    for fut in as_completed(futures):
        try:
            result = fut.result()
        except Exception:
            logger.exception("Enrichment task failed")
            continue
        if result:
            enriched.append(result)

    return enriched
