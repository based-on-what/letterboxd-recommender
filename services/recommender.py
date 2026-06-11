"""
services/recommender.py — Recommendation pipeline.

Orchestrates TmdbClient + StreamingClient to turn a list of enriched
user films into ranked, de-duplicated, streaming-annotated recommendations.
No HTTP sessions, no scraping, no caching of its own.
"""

import logging
import os
import time
from concurrent.futures import as_completed

from cache import cache, ONE_DAY
from executors import WORK_EXECUTOR
from utils import normalize_title, IS_DEV, export_debug_json
from sse import publish

logger = logging.getLogger("letterboxd-recommender")

MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))
SIMILAR_WORKERS = 4
SIMILAR_RESULTS_PER_FILM = 12


def get_recommendations(
    tmdb_client,
    streaming_client,
    enriched_films: list,
    count=None,
    force_refresh: bool = False,
    request_id=None,
    username=None,
    max_workers: int = 4,
) -> list:
    seen_ids: set = set()
    seen_titles_norm: set = set()
    for film in enriched_films:
        if film.get('tmdb_id'):
            seen_ids.add(str(film['tmdb_id']))
        t = normalize_title(film.get('title', ''))
        if t:
            seen_titles_norm.add(t)
        if film.get('original_title'):
            o = normalize_title(film['original_title'])
            if o:
                seen_titles_norm.add(o)

    highly_rated = sorted(
        [f for f in enriched_films if f.get('user_rating', 0) >= 4.0],
        key=lambda x: x.get('user_rating', 0),
        reverse=True,
    )

    logger.info("4+ star seed films: %d", len(highly_rated))

    if IS_DEV and highly_rated:
        _debug_export_seed(highly_rated)

    recs = []
    futures = [
        WORK_EXECUTOR.submit(
            _get_similar,
            tmdb_client,
            streaming_client,
            film,
            seen_ids,
            seen_titles_norm,
            request_id,
            force_refresh,
            username,
        )
        for film in highly_rated
    ]
    for f in as_completed(futures):
        try:
            recs.extend(f.result())
        except Exception:
            logger.exception("Similar-film task failed")

    unique: dict = {}
    for r in recs:
        key = str(r['tmdb_id'])
        if key in unique or key in seen_ids:
            continue
        t_norm = r.get('_title_norm') or normalize_title(r.get('title', ''))
        if t_norm not in seen_titles_norm:
            unique[key] = r

    result = list(unique.values())
    logger.info("Recommendations: %d candidates → %d unique", len(recs), len(result))
    return result if count is None else result[:count]


def _get_similar(
    tmdb_client,
    streaming_client,
    film: dict,
    seen_ids: set,
    seen_titles_norm: set,
    request_id,
    force_refresh: bool,
    username,
) -> list:
    if not film.get('tmdb_id'):
        return []

    if request_id:
        publish(request_id, 'status', {
            'title': film.get('title', 'Untitled'),
            'user_rating': film.get('user_rating', 0),
            'username': username or 'user',
        })

    cache_key = f"similar:{film['tmdb_id']}"

    # Load or build the raw similar-film pool (no user-specific filtering here:
    # the cache is global so it must not contain user-watched exclusions).
    raw: list | None = None
    if not force_refresh:
        raw = cache.get('similar', cache_key)

    if raw is None:
        raw_results = tmdb_client.get_similar(film['tmdb_id'], limit=SIMILAR_RESULTS_PER_FILM)
        raw = []
        for m in raw_results:
            mid = m.get('id')
            title = m.get('title')
            if not title or not mid:
                continue
            det = tmdb_client.get_details_by_id(mid, force_refresh)
            if not det:
                continue
            rating = det.get('rating_tmdb')
            if rating is None or float(rating) < MIN_RECOMMEND_RATING:
                continue
            det['reason'] = f"Since you liked {film.get('title')}"
            det['_title_norm'] = normalize_title(det.get('title', ''))
            det['_orig_norm'] = normalize_title(det.get('original_title', ''))
            streaming = []
            try:
                if det.get('tmdb_id'):
                    streaming = streaming_client.get_by_tmdb_id(det['tmdb_id'])
                if not streaming:
                    streaming = streaming_client.get_by_title(det.get('title'), det.get('year'))
            except Exception as exc:
                logger.debug("Streaming fetch error for %s: %s", det.get('title'), exc)
            det['streaming'] = streaming
            raw.append(det)
        cache.set('similar', cache_key, raw, ttl=ONE_DAY)

    try:
        local = []
        for det in raw:
            det_title_norm = det.get('_title_norm') or normalize_title(det.get('title', ''))
            det_orig_norm = det.get('_orig_norm') or normalize_title(det.get('original_title', ''))
            if (
                str(det.get('tmdb_id', '')) in seen_ids
                or det_title_norm in seen_titles_norm
                or det_orig_norm in seen_titles_norm
            ):
                continue
            local.append(det)
            if request_id:
                try:
                    publish(request_id, 'recommendations', {
                        k: det.get(k)
                        for k in ('tmdb_id', 'title', 'year', 'rating_tmdb', 'poster',
                                  'director', 'genres', 'runtime', 'streaming', 'reason')
                    })
                except Exception:
                    pass
        return local

    except Exception as exc:
        logger.error("Error fetching similar for %s: %s", film.get('title'), exc)
        return []


def _debug_export_seed(films: list) -> None:
    data = {
        'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total_highly_rated': len(films),
        'films': [
            {k: f.get(k) for k in ('title', 'year', 'tmdb_id', 'user_rating', 'director', 'genres', 'rating_tmdb')}
            for f in films
        ],
    }
    export_debug_json(f"highly_rated_{time.strftime('%Y%m%d_%H%M%S')}.json", data)
