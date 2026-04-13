"""
routes.py — Flask Blueprint with all HTTP route handlers.

Imports the Blueprint `bp`; main.py registers it with `app.register_blueprint(bp)`.
"""

import json
import queue
import re
import time
import os
import logging
from uuid import uuid4

from flask import Blueprint, request, jsonify, g, Response, stream_with_context, current_app

from limiter import limiter
from sse import (
    _get_or_create_streams,
    _mark_recommendations_done,
    _mark_status_done,
    _track_stream_connection,
)
from recommender import (
    MovieRecommender,
    enrich_film_task,
    normalize_title,
    INCIDENT_TRACKER,
    TIMEOUT_WARNING_S,
    ENRICH_WORKERS,
    IS_DEV,
)
from cache import cache

logger = logging.getLogger("letterboxd-recommender")

bp = Blueprint('api', __name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@bp.route('/_health', methods=['GET'])
def health():
    """Health check endpoint with degraded-mode visibility."""
    incident = INCIDENT_TRACKER.snapshot()
    return jsonify({
        "status": "ok",
        "degraded": incident.get('letterboxd_circuit_open', False),
        "incident": incident,
    }), 200


@bp.route('/_incident-status', methods=['GET'])
@limiter.limit('30 per minute')
def incident_status():
    """Operational incident snapshot for alerting/diagnostics."""
    expected = os.getenv('INTERNAL_TOKEN', '')
    if expected:
        provided = request.headers.get('X-Internal-Token', '')
        if provided != expected:
            return jsonify({'error': 'forbidden'}), 403
    return jsonify(INCIDENT_TRACKER.snapshot()), 200


@bp.route('/api/logs-stream', methods=['GET'])
@limiter.limit('20 per minute')
def logs_stream():
    """
    SSE endpoint that streams logs in real-time to the browser.

    Requires request_id query param.
    """
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    log_queue = _get_or_create_streams(request_id)['logs']

    def generate():
        _track_stream_connection(request_id, 'logs', True)
        try:
            while True:
                try:
                    log_msg = log_queue.get(timeout=1)
                    yield f"data: {log_msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            _track_stream_connection(request_id, 'logs', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@bp.route('/api/recommendations-stream', methods=['GET'])
@limiter.limit('20 per minute')
def recommendations_stream():
    """SSE endpoint that streams recommendations in real-time to the browser."""
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    recommendations_queue = _get_or_create_streams(request_id)['recommendations']

    def generate():
        _track_stream_connection(request_id, 'recommendations', True)
        try:
            while True:
                try:
                    rec = recommendations_queue.get(timeout=2)
                    if rec == 'DONE':
                        yield "data: {\"status\": \"complete\"}\n\n"
                        break
                    yield f"data: {json.dumps(rec)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except Exception as exc:
            logger.debug(f"Recommendations stream ended for {request_id}: {exc}")
        finally:
            _track_stream_connection(request_id, 'recommendations', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@bp.route('/api/status-stream', methods=['GET'])
@limiter.limit('20 per minute')
def status_stream():
    """SSE endpoint that streams currently analyzed movie metadata."""
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
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@bp.route('/')
def home():
    """Serve the landing page."""
    return current_app.send_static_file('index.html')


@bp.route('/favicon.ico')
def favicon():
    """Avoid noisy favicon 404/502s on platforms expecting an icon file."""
    return ('', 204)


@bp.route('/api/get_pages', methods=['POST'])
@limiter.limit('10 per minute')
def get_pages():
    """
    Endpoint that returns how many film pages a profile has.

    Request JSON: {"username": str}
    Response JSON: {"pages": int}
    """
    payload = request.get_json() or {}

    username = (payload.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'username is required'}), 400
    if not re.match(r'^[a-zA-Z0-9_-]{1,50}$', username):
        return jsonify({'error': 'invalid username format'}), 400

    try:
        recommender = MovieRecommender()
        page_count = recommender.get_page_count(username)
        return jsonify({'pages': page_count})

    except Exception:
        logger.exception("Error in get_pages")
        return jsonify({'error': 'internal error'}), 500


@bp.route('/api/recommend', methods=['POST'])
@limiter.limit('5 per minute')
def recommend():
    """
    Main endpoint that orchestrates recommendation generation.

    Request JSON:
        - username: str (required)
        - country: str (optional, default: CL)
        - include_streaming: bool (optional, default: True)

    Response JSON:
        - username, country_name, pages
        - preferences: {genres, directors, decades}
        - recommendations: [{title, year, rating, streaming, ...}]
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_time = time.time()
    data = request.get_json() or {}
    username = data.get('username')
    request_id = data.get('request_id') or str(uuid4())
    g.request_id = request_id
    _get_or_create_streams(request_id)

    if not username:
        return jsonify({'error': 'username is required'}), 400
    username = username.strip()
    if not re.match(r'^[a-zA-Z0-9_-]{1,50}$', username):
        return jsonify({'error': 'invalid username format'}), 400

    try:
        rec_sys = MovieRecommender(country=data.get('country', 'CL'))

        logger.info(f"\n{'='*60}")
        logger.info(f"STARTING ANALYSIS FOR: {username}")
        logger.info(f"{'='*60}")

        # Fetch user films (including unrated entries)
        user_films, pages = rec_sys.get_all_rated_films(username, include_unrated=True)
        logger.info(f"Fetched {len(user_films)} films in {time.time() - start_time:.2f}s")

        if not user_films:
            failures = rec_sys._letterboxd_last_failures[-8:]
            blocked_by_letterboxd = any('status=403' in f for f in failures)
            throttled_or_temp_block = any(('status=429' in f) or ('status=503' in f) for f in failures)

            if blocked_by_letterboxd or throttled_or_temp_block:
                incident = INCIDENT_TRACKER.snapshot()
                response = jsonify({
                    'error': 'Could not read this public Letterboxd profile from the server network (blocked/throttled by Letterboxd).',
                    'username': username,
                    'request_id': request_id,
                    'hint': 'Try again in a few minutes. If it persists, use a deployment region/proxy with lower bot reputation risk.',
                })
                if incident.get('letterboxd_circuit_open'):
                    response.headers['Retry-After'] = str(incident.get('letterboxd_circuit_retry_after_s', 0))
                return response, 503

            return jsonify({
                'error': 'No movies found for this username.',
                'username': username,
                'request_id': request_id,
                'hint': 'Check backend logs for Letterboxd HTTP status / proxy diagnostics.',
            }), 404

        # Enrich with TMDB data
        enriched = []

        logger.info(f"\nEnriching {len(user_films)} films with TMDB metadata...")
        logger.info(
            f"Stage: TMDB enrichment started | workers={ENRICH_WORKERS} | elapsed_since_start={time.time() - start_time:.2f}s"
        )
        enrich_start = time.time()
        total_to_enrich = len(user_films)
        progress_interval = max(1, total_to_enrich // 10)
        completed_enrich = 0
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            futures = [ex.submit(enrich_film_task, rec_sys, film) for film in user_films]
            for fut in as_completed(futures):
                completed_enrich += 1
                try:
                    result = fut.result()
                    if result:
                        enriched.append(result)
                except Exception as exc:
                    logger.error(f"Enrichment task failed: {exc}")

                if completed_enrich % progress_interval == 0 or completed_enrich == total_to_enrich:
                    logger.info(
                        "Enrichment progress: "
                        f"{completed_enrich}/{total_to_enrich} "
                        f"({(completed_enrich / max(total_to_enrich, 1)) * 100:.0f}%) | "
                        f"enriched_ok={len(enriched)} | "
                        f"stage_elapsed={time.time() - enrich_start:.2f}s"
                    )

        logger.info(f"Enrichment completed in {time.time() - enrich_start:.2f}s")
        logger.info(
            f"Stage: TMDB enrichment finished | enriched_ok={len(enriched)} | elapsed_since_start={time.time() - start_time:.2f}s"
        )

        # Analyze preferences
        logger.info(
            f"Stage: preference analysis started | input_films={len(enriched)} | elapsed_since_start={time.time() - start_time:.2f}s"
        )
        pref_start = time.time()
        preferences = rec_sys.analyze_preferences(enriched)
        logger.info(f"\nPreferences detected in {time.time() - pref_start:.2f}s:")
        logger.info(
            f"Stage: preference analysis finished | elapsed_since_start={time.time() - start_time:.2f}s"
        )
        logger.info(f"  Genres: {', '.join(preferences.get('genres', []))}")
        logger.info(f"  Directors: {', '.join(preferences.get('directors', []))}")
        logger.info(f"  Decades: {', '.join(preferences.get('decades', []))}")

        # Check elapsed time and adjust processing
        elapsed = time.time() - start_time
        logger.info(f"\nTime elapsed so far: {elapsed:.2f}s")

        if elapsed > TIMEOUT_WARNING_S:
            logger.warning("Approaching timeout limit, limiting recommendation processing")

        # Generate recommendations
        logger.info(
            f"Stage: recommendation generation started | seed_input_films={len(enriched)} | elapsed_since_start={time.time() - start_time:.2f}s"
        )
        rec_start = time.time()
        recommendations = rec_sys.get_recommendations(
            enriched,
            request_id=request_id,
            username=username,
        )
        logger.info(f"Recommendations generated in {time.time() - rec_start:.2f}s")

        # Streaming is resolved inline by get_recommendations; ensure the field
        # always exists and strip it when the caller opts out.
        for r in recommendations:
            r.setdefault('streaming', [])
        if not data.get('include_streaming', True):
            for r in recommendations:
                r['streaming'] = []

        logger.info(f"\n{'='*60}")
        logger.info(f"ANALYSIS COMPLETE")
        logger.info(f"Total processing time: {time.time() - start_time:.2f}s")
        logger.info(f"{'='*60}\n")

        # Send completion signal for recommendation stream
        try:
            _mark_recommendations_done(request_id)
            _get_or_create_streams(request_id)['recommendations'].put('DONE')
            _mark_status_done(request_id)
            _get_or_create_streams(request_id)['status'].put('DONE')
        except Exception:
            pass

        # Export JSON files locally when in development mode
        if IS_DEV:
            _export_dev_json(username, enriched, recommendations)

        response_payload = {
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'country_code': rec_sys.country,
            'pages': pages,
            'preferences': preferences,
            'recommendations': recommendations,
            'request_id': request_id,
        }
        if rec_sys.used_stale_profile_cache:
            response_payload['data_freshness'] = 'stale_cache'
            response_payload['hint'] = (
                'Showing last successful profile snapshot because live Letterboxd scraping was blocked or throttled.'
            )
            response_payload['incident'] = INCIDENT_TRACKER.snapshot()

        return jsonify(response_payload)

    except Exception:
        logger.exception("Error generating recommendations")
        return jsonify({'error': 'internal server error'}), 500


@bp.route('/<username>')
def user_view(username):
    """Serve the results page for a specific user."""
    return current_app.send_static_file('results.html')


# ---------------------------------------------------------------------------
# Dev-only debug export (called from recommend())
# ---------------------------------------------------------------------------
def _export_dev_json(username, enriched, recommendations):
    import json as _json
    from recommender import _export_debug_json

    try:
        movies_data = {
            'username': username,
            'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_movies': len(enriched),
            'movies': [
                {
                    'title': f.get('title'),
                    'original_title': f.get('original_title'),
                    'year': f.get('year'),
                    'tmdb_id': f.get('tmdb_id'),
                    'user_rating': f.get('user_rating'),
                    'director': f.get('director'),
                    'genres': f.get('genres', []),
                    'rating_tmdb': f.get('rating_tmdb'),
                    'runtime': f.get('runtime')
                }
                for f in enriched
            ]
        }
        _export_debug_json(f"{username}_movies.json", movies_data)

        seen_ids = {str(m.get('tmdb_id')) for m in enriched if m.get('tmdb_id')}
        seen_titles_norm = {normalize_title(m.get('title', '')) for m in enriched}

        filtered_recs = [
            rec for rec in recommendations
            if str(rec.get('tmdb_id')) not in seen_ids
            and normalize_title(rec.get('title', '')) not in seen_titles_norm
        ]

        recs_data = {
            'username': username,
            'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_recommendations': len(filtered_recs),
            'recommendations': [
                {
                    'title': r.get('title'),
                    'original_title': r.get('original_title'),
                    'year': r.get('year'),
                    'tmdb_id': r.get('tmdb_id'),
                    'rating_tmdb': r.get('rating_tmdb'),
                    'director': r.get('director'),
                    'genres': r.get('genres', []),
                    'runtime': r.get('runtime'),
                    'streaming': r.get('streaming', [])
                }
                for r in filtered_recs
            ]
        }
        _export_debug_json(f"{username}_recs.json", recs_data)
        logger.info("  (Verified: no duplicates with user's movies)")

    except Exception as e:
        logger.warning(f"Could not export JSON files: {e}")
