"""
routes.py — Flask Blueprint. HTTP only: parse input, call services, return JSON.

No business logic here. Enrichment, preference analysis, and recommendation
generation are orchestrated by MovieRecommender; the route just tracks timing
and formats the HTTP response.
"""

import json
import logging
import os
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from cache import cache
from limiter import limiter
from recommender import (
    ENRICH_WORKERS,
    IS_DEV,
    TIMEOUT_WARNING_S,
    MovieRecommender,
    _export_debug_json,
    enrich_film_task,
    normalize_title,
    INCIDENT_TRACKER,
)
from sse import (
    _get_or_create_streams,
    _mark_recommendations_done,
    _mark_status_done,
    _track_stream_connection,
)

logger = logging.getLogger("letterboxd-recommender")

bp = Blueprint('api', __name__)

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,50}$')

# Singleton used by lightweight endpoints that only need page-count lookups.
# Created lazily to avoid import-time failures when TMDB_KEY is not yet set.
_default_rec_sys: MovieRecommender | None = None


def _get_default_rec_sys() -> MovieRecommender:
    global _default_rec_sys
    if _default_rec_sys is None:
        _default_rec_sys = MovieRecommender()
    return _default_rec_sys


# ---------------------------------------------------------------------------
# Health / diagnostics
# ---------------------------------------------------------------------------
@bp.route('/_health', methods=['GET'])
def health():
    incident = INCIDENT_TRACKER.snapshot()
    return jsonify({
        "status": "ok",
        "degraded": incident.get('letterboxd_circuit_open', False),
        "incident": incident,
    }), 200


@bp.route('/_incident-status', methods=['GET'])
@limiter.limit('30 per minute')
def incident_status():
    expected = os.getenv('INTERNAL_TOKEN', '')
    if expected:
        provided = request.headers.get('X-Internal-Token', '')
        if provided != expected:
            return jsonify({'error': 'forbidden'}), 403
    return jsonify(INCIDENT_TRACKER.snapshot()), 200


# ---------------------------------------------------------------------------
# SSE streams
# ---------------------------------------------------------------------------
@bp.route('/api/logs-stream', methods=['GET'])
@limiter.limit('20 per minute')
def logs_stream():
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    log_queue = _get_or_create_streams(request_id)['logs']

    def generate():
        _track_stream_connection(request_id, 'logs', True)
        try:
            while True:
                try:
                    yield f"data: {log_queue.get(timeout=1)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            _track_stream_connection(request_id, 'logs', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )


@bp.route('/api/recommendations-stream', methods=['GET'])
@limiter.limit('20 per minute')
def recommendations_stream():
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    recs_queue = _get_or_create_streams(request_id)['recommendations']

    def generate():
        _track_stream_connection(request_id, 'recommendations', True)
        try:
            while True:
                try:
                    rec = recs_queue.get(timeout=2)
                    if rec == 'DONE':
                        yield "data: {\"status\": \"complete\"}\n\n"
                        break
                    yield f"data: {json.dumps(rec)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except Exception as exc:
            logger.debug("Recommendations stream ended for %s: %s", request_id, exc)
        finally:
            _track_stream_connection(request_id, 'recommendations', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )


@bp.route('/api/status-stream', methods=['GET'])
@limiter.limit('20 per minute')
def status_stream():
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    status_queue = _get_or_create_streams(request_id)['status']

    def generate():
        _track_stream_connection(request_id, 'status', True)
        try:
            while True:
                try:
                    status = status_queue.get(timeout=2)
                    if status == 'DONE':
                        yield "data: {\"status\": \"complete\"}\n\n"
                        break
                    yield f"data: {json.dumps(status)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            _track_stream_connection(request_id, 'status', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------
@bp.route('/')
def home():
    return current_app.send_static_file('index.html')


@bp.route('/favicon.ico')
def favicon():
    return ('', 204)


@bp.route('/<username>')
def user_view(username):
    return current_app.send_static_file('results.html')


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@bp.route('/api/get_pages', methods=['POST'])
@limiter.limit('10 per minute')
def get_pages():
    payload = request.get_json() or {}
    username = (payload.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'username is required'}), 400
    if not _USERNAME_RE.match(username):
        return jsonify({'error': 'invalid username format'}), 400

    try:
        page_count = _get_default_rec_sys().get_page_count(username)
        return jsonify({'pages': page_count})
    except Exception:
        logger.exception("Error in get_pages")
        return jsonify({'error': 'internal error'}), 500


@bp.route('/api/recommend', methods=['POST'])
@limiter.limit('5 per minute')
def recommend():
    start = time.time()
    data = request.get_json() or {}

    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'username is required'}), 400
    if not _USERNAME_RE.match(username):
        return jsonify({'error': 'invalid username format'}), 400

    request_id = data.get('request_id') or str(uuid4())
    g.request_id = request_id
    _get_or_create_streams(request_id)

    try:
        rec_sys = MovieRecommender(country=data.get('country', 'CL'))

        logger.info("=== STARTING ANALYSIS FOR: %s ===", username)

        # 1. Scrape user profile
        user_films, pages = rec_sys.get_all_rated_films(username, include_unrated=True)
        logger.info("Fetched %d films in %.2fs", len(user_films), time.time() - start)

        if not user_films:
            return _handle_empty_profile(rec_sys, username, request_id)

        # 2. Enrich with TMDB metadata
        enriched = _enrich_films(rec_sys, user_films, start)

        # 3. Analyse preferences
        preferences = rec_sys.analyze_preferences(enriched)
        logger.info("Preferences — genres: %s | directors: %s | decades: %s",
                    preferences.get('genres'), preferences.get('directors'), preferences.get('decades'))

        if time.time() - start > TIMEOUT_WARNING_S:
            logger.warning("Approaching timeout; recommendation window may be limited")

        # 4. Generate recommendations
        recommendations = rec_sys.get_recommendations(enriched, request_id=request_id, username=username)

        for r in recommendations:
            r.setdefault('streaming', [])
        if not data.get('include_streaming', True):
            for r in recommendations:
                r['streaming'] = []

        logger.info("=== ANALYSIS COMPLETE — %.2fs total ===", time.time() - start)

        _signal_stream_done(request_id)

        if IS_DEV:
            _export_dev_json(username, enriched, recommendations)

        payload = {
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'country_code': rec_sys.country,
            'pages': pages,
            'preferences': preferences,
            'recommendations': recommendations,
            'request_id': request_id,
        }
        if rec_sys.used_stale_profile_cache:
            payload['data_freshness'] = 'stale_cache'
            payload['hint'] = (
                'Showing last successful profile snapshot because live Letterboxd scraping was blocked or throttled.'
            )
            payload['incident'] = INCIDENT_TRACKER.snapshot()

        return jsonify(payload)

    except Exception:
        logger.exception("Error generating recommendations")
        return jsonify({'error': 'internal server error'}), 500


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------
def _handle_empty_profile(rec_sys, username, request_id):
    failures = rec_sys._letterboxd_last_failures[-8:]
    blocked = any('status=403' in f for f in failures)
    throttled = any(('status=429' in f) or ('status=503' in f) for f in failures)

    if blocked or throttled:
        incident = INCIDENT_TRACKER.snapshot()
        resp = jsonify({
            'error': 'Could not read this public Letterboxd profile from the server network (blocked/throttled by Letterboxd).',
            'username': username,
            'request_id': request_id,
            'hint': 'Try again in a few minutes. If it persists, use a deployment region/proxy with lower bot reputation risk.',
        })
        if incident.get('letterboxd_circuit_open'):
            resp.headers['Retry-After'] = str(incident.get('letterboxd_circuit_retry_after_s', 0))
        return resp, 503

    return jsonify({
        'error': 'No movies found for this username.',
        'username': username,
        'request_id': request_id,
        'hint': 'Check backend logs for Letterboxd HTTP status / proxy diagnostics.',
    }), 404


def _enrich_films(rec_sys, user_films: list, start: float) -> list:
    enriched = []
    total = len(user_films)
    interval = max(1, total // 10)
    completed = 0
    enrich_start = time.time()

    logger.info("Enriching %d films with TMDB metadata (workers=%d)", total, ENRICH_WORKERS)

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
        futures = [ex.submit(enrich_film_task, rec_sys, film) for film in user_films]
        for fut in as_completed(futures):
            completed += 1
            try:
                result = fut.result()
                if result:
                    enriched.append(result)
            except Exception as exc:
                logger.error("Enrichment task failed: %s", exc)

            if completed % interval == 0 or completed == total:
                logger.info("Enrichment: %d/%d (%.0f%%) | ok=%d | elapsed=%.2fs",
                            completed, total, completed / max(total, 1) * 100,
                            len(enriched), time.time() - enrich_start)

    logger.info("Enrichment done in %.2fs", time.time() - enrich_start)
    return enriched


def _signal_stream_done(request_id: str) -> None:
    try:
        _mark_recommendations_done(request_id)
        _mark_status_done(request_id)
        streams = _get_or_create_streams(request_id)
        streams['recommendations'].put('DONE')
        streams['status'].put('DONE')
    except Exception:
        pass


def _export_dev_json(username: str, enriched: list, recommendations: list) -> None:
    try:
        _export_debug_json(f"{username}_movies.json", {
            'username': username,
            'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_movies': len(enriched),
            'movies': [
                {k: f.get(k) for k in ('title', 'original_title', 'year', 'tmdb_id',
                                        'user_rating', 'director', 'genres', 'rating_tmdb', 'runtime')}
                for f in enriched
            ],
        })

        seen_ids = {str(m.get('tmdb_id')) for m in enriched if m.get('tmdb_id')}
        seen_norm = {normalize_title(m.get('title', '')) for m in enriched}
        filtered = [
            r for r in recommendations
            if str(r.get('tmdb_id')) not in seen_ids and normalize_title(r.get('title', '')) not in seen_norm
        ]

        _export_debug_json(f"{username}_recs.json", {
            'username': username,
            'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_recommendations': len(filtered),
            'recommendations': [
                {k: r.get(k) for k in ('title', 'original_title', 'year', 'tmdb_id',
                                        'rating_tmdb', 'director', 'genres', 'runtime', 'streaming')}
                for r in filtered
            ],
        })
    except Exception as exc:
        logger.warning("Could not export dev JSON: %s", exc)
