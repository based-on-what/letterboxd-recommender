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
from dataclasses import dataclass
from typing import Callable, Optional

from cache import cache, ONE_DAY
from executors import WORK_EXECUTOR
from utils import normalize_title, IS_DEV, export_debug_json

logger = logging.getLogger("letterboxd-recommender")

MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))
SIMILAR_WORKERS = 4
SIMILAR_RESULTS_PER_FILM = 12


@dataclass(frozen=True)
class RecommendationContext:
    """Per-request pipeline state threaded through seed processing."""

    seen_ids: frozenset
    seen_titles_norm: frozenset
    username: Optional[str] = None
    force_refresh: bool = False
    on_recommendation: Optional[Callable[[dict], None]] = None
    on_status: Optional[Callable[[dict], None]] = None


def get_recommendations(
    tmdb_client,
    streaming_client,
    enriched_films: list,
    count: Optional[int] = None,
    force_refresh: bool = False,
    username: Optional[str] = None,
    max_workers: int = 4,
    on_recommendation: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[dict], None]] = None,
) -> list:
    """Generate recommendations. Pure domain logic: progress is reported via
    the optional on_recommendation(rec_dict) / on_status(status_dict)
    callbacks, so this is callable with no Flask or SSE context.
    """
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

    ctx = RecommendationContext(
        seen_ids=frozenset(seen_ids),
        seen_titles_norm=frozenset(seen_titles_norm),
        username=username,
        force_refresh=force_refresh,
        on_recommendation=on_recommendation,
        on_status=on_status,
    )

    # Early termination: each seed costs ~12 TMDB + streaming calls. Once we
    # have target unique candidates (count + overshoot buffer for dedup
    # losses), cancel seeds that have not started yet.
    target = None if count is None else count + max(5, count // 2)
    unique_keys: set = set()

    recs = []
    futures = [
        WORK_EXECUTOR.submit(_get_similar, tmdb_client, streaming_client, film, ctx)
        for film in highly_rated
    ]
    for f in as_completed(futures):
        try:
            batch = f.result()
        except Exception:
            logger.exception("Similar-film task failed")
            continue
        recs.extend(batch)
        if target is None:
            continue
        for r in batch:
            key = str(r.get('tmdb_id'))
            if key not in seen_ids:
                unique_keys.add(key)
        if len(unique_keys) >= target:
            cancelled = sum(1 for pending in futures if pending.cancel())
            logger.info("Early stop: %d unique candidates >= target %d; cancelled %d pending seeds",
                        len(unique_keys), target, cancelled)
            break

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
    ctx: RecommendationContext,
) -> list:
    if not film.get('tmdb_id'):
        return []

    if ctx.on_status:
        ctx.on_status({
            'title': film.get('title', 'Untitled'),
            'user_rating': film.get('user_rating', 0),
            'username': ctx.username or 'user',
        })

    cache_key = f"similar:{film['tmdb_id']}"

    # Cache only the ordered similar-ID list: it is globally valid, small,
    # and immune to enrichment-shape changes. Enriched results are rebuilt
    # on demand from the per-ID tmdb/streaming caches (own TTLs).
    def _fetch_similar_ids() -> list:
        raw_results = tmdb_client.get_similar(film['tmdb_id'], limit=SIMILAR_RESULTS_PER_FILM)
        return [m['id'] for m in raw_results if m.get('id') and m.get('title')]

    if ctx.force_refresh:
        ids = _fetch_similar_ids()
        cache.set('similar', cache_key, ids, ttl=ONE_DAY)
    else:
        ids = cache.get_or_compute('similar', cache_key, _fetch_similar_ids, ttl=ONE_DAY)

    local = []
    for mid in ids:
        try:
            det = tmdb_client.get_details_by_id(mid, ctx.force_refresh)
            if not det:
                continue
            rating = det.get('rating_tmdb')
            if rating is None or float(rating) < MIN_RECOMMEND_RATING:
                continue
            det = dict(det)  # cached dict is shared across requests: never mutate it
            det['reason'] = f"Since you liked {film.get('title')}"
            det['_title_norm'] = normalize_title(det.get('title', ''))
            det['_orig_norm'] = normalize_title(det.get('original_title', ''))
            if (
                str(det.get('tmdb_id', '')) in ctx.seen_ids
                or det['_title_norm'] in ctx.seen_titles_norm
                or det['_orig_norm'] in ctx.seen_titles_norm
            ):
                continue
            streaming = []
            try:
                if det.get('tmdb_id'):
                    streaming = streaming_client.get_by_tmdb_id(det['tmdb_id'])
                if not streaming:
                    streaming = streaming_client.get_by_title(det.get('title'), det.get('year'))
            except Exception as exc:
                logger.debug("Streaming fetch error for %s: %s", det.get('title'), exc)
            det['streaming'] = streaming
            local.append(det)
            if ctx.on_recommendation:
                try:
                    ctx.on_recommendation({
                        k: det.get(k)
                        for k in ('tmdb_id', 'title', 'year', 'rating_tmdb', 'poster',
                                  'director', 'genres', 'runtime', 'streaming', 'reason')
                    })
                except Exception:
                    pass
        except Exception as exc:
            logger.error("Error processing similar film %s for %s: %s", mid, film.get('title'), exc)
    return local


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
