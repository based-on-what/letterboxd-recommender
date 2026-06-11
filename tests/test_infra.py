"""Infra layer: scraping client, TMDB client, streaming, circuit breaker."""

import time
from unittest.mock import patch

import main

from testutil import _FakeRedisKV, _resp


def test_safe_get_success():
    r = main.MovieRecommender()
    # main.session IS infra.http.session — patching .get on the object works regardless of import path
    with patch.object(main.session, 'get', return_value=_resp({}, 200)):
        assert r._lb._safe_get('http://x') is not None


def test_get_page_count():
    html = '<li class="paginate-page">1</li><li class="paginate-page">3</li>'
    r = main.MovieRecommender()
    # _safe_get lives on LetterboxdClient now; patch the inner client
    with patch.object(r._lb, '_safe_get', return_value=_resp({}, text=html)):
        assert r.get_page_count('user') == 3


def test_get_all_rated_films_uses_cache():
    r = main.MovieRecommender()
    with patch.object(main.cache, 'get', return_value={'films': [{'title': 'A'}], 'pages': 2}):
        films, pages = r.get_all_rated_films('user')
    assert films == [{'title': 'A'}]
    assert pages == 2


def test_get_all_rated_films_falls_back_to_stale_cache_when_live_scrape_fails():
    r = main.MovieRecommender()

    def fake_cache_get(namespace, key):
        if key.endswith(':pages:v2'):
            return None
        if key.endswith(':pages:stale:v1'):
            return {'films': [{'title': 'Stale Film', 'rating': 3.5, 'has_rating': True}], 'pages': 4}
        return None

    # get_page_count lives on LetterboxdClient now
    with patch.object(main.cache, 'get', side_effect=fake_cache_get), \
         patch.object(r._lb, 'get_page_count', return_value=0):
        films, pages = r.get_all_rated_films('user')

    assert pages == 4
    assert films and films[0]['title'] == 'Stale Film'
    assert r.used_stale_profile_cache is True


def test_validate_scrape_rules():
    from infra.letterboxd import _validate_scrape

    assert _validate_scrape([{'title': 'A', 'rating': 4.5}], 1) is True
    assert _validate_scrape([{'title': '', 'rating': 4.5}], 1) is False        # empty title
    assert _validate_scrape([{'title': 'A', 'rating': 9.0}], 1) is False       # rating out of range
    assert _validate_scrape([], 1) is False                                    # no films
    # 10 pages but only 10 films: partially failed scrape
    assert _validate_scrape([{'title': 'A', 'rating': 4.0}] * 10, 10) is False


def test_invalid_scrape_served_but_not_cached():
    r = main.MovieRecommender()
    set_namespaces = []

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set',
                      side_effect=lambda ns, *a, **k: set_namespaces.append(ns)), \
         patch.object(r._lb, 'get_page_count', return_value=8), \
         patch.object(r._lb, '_scrape_page',
                      return_value=[{'title': 'A', 'rating': 4.0, 'has_rating': True}]):
        films, pages = r.get_all_rated_films('user')

    # 8 pages x 1 film = partial scrape: served to the caller...
    assert pages == 8 and len(films) == 8
    # ...but never persisted to the user_scrape cache
    assert 'user_scrape' not in set_namespaces


def test_camoufox_concurrency_cap_short_circuits():
    import infra.http as http

    class DummyCamoufox:
        def __init__(self, **_kw):
            raise AssertionError('browser must not launch when cap is saturated')

    with patch.object(http, '_Camoufox', DummyCamoufox):
        assert http._camoufox_semaphore.acquire(blocking=False)  # saturate cap (default 1)
        try:
            assert http.camoufox_get('http://x', None, 1) is None
        finally:
            http._camoufox_semaphore.release()


def test_get_tmdb_details():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    # HTTP goes through TmdbClient._get; patch at that boundary
    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(r._tmdb, '_get', return_value=_resp({'results': [{'id': 1}]})), \
         patch.object(r, 'get_tmdb_details_by_id', return_value={'tmdb_id': 1}):
        assert r.get_tmdb_details('x')['tmdb_id'] == 1


def test_get_tmdb_details_by_id():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    payload = {
        'title': 'A', 'original_title': 'A', 'release_date': '2000-01-01',
        'credits': {'crew': [{'job': 'Director', 'name': 'D'}]},
        'genres': [{'name': 'Drama'}], 'poster_path': None,
        'vote_average': 7.3, 'runtime': 90, 'external_ids': {'imdb_id': 'tt'},
    }
    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, '_get', return_value=_resp(payload)):
        out = r.get_tmdb_details_by_id(1)
    assert out['director'] == 'D'


def test_get_streaming_by_tmdb():
    r = main.MovieRecommender(country='CL')
    r.tmdb_key = 'k'
    payload = {'results': {'CL': {'flatrate': [{'provider_name': 'Disney+'}]}}}
    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, '_get', return_value=_resp(payload)):
        assert r.get_streaming_by_tmdb(1) == ['Disney Plus']


def test_get_streaming_without_dependency():
    r = main.MovieRecommender()
    # sjw lives in infra.streaming; patch there so StreamingClient.get_by_title sees None
    with patch('infra.streaming.sjw', None):
        assert r.get_streaming('A') == []


def test_incident_tracker_opens_circuit_after_threshold():
    tracker = main.IncidentTracker()
    threshold = main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD

    for _ in range(threshold):
        tracker.record_letterboxd_result(success=False, status=403)

    snap = tracker.snapshot()
    assert snap['letterboxd_circuit_open'] is True
    assert snap['letterboxd_consecutive_failures'] >= threshold


def test_incident_tracker_circuit_closes_after_cooldown():
    tracker = main.IncidentTracker()
    for _ in range(main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD):
        tracker.record_letterboxd_result(success=False, status=403)
    assert tracker.is_circuit_open() is True

    tracker.circuit_open_until = time.time() - 1  # simulate cooldown expiry
    assert tracker.is_circuit_open() is False

    tracker.record_letterboxd_result(success=True)
    snap = tracker.snapshot()
    assert snap['letterboxd_circuit_open'] is False
    assert snap['letterboxd_consecutive_failures'] == 0


def test_incident_tracker_redis_backend():
    tracker = main.IncidentTracker()
    fake = _FakeRedisKV()

    with patch.object(main.cache, 'redis', fake), \
         patch.object(main.cache, '_redis_attempted', True):
        for _ in range(main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD):
            tracker.record_letterboxd_result(success=False, status=403)

        assert tracker.is_circuit_open() is True
        snap = tracker.snapshot()
        assert snap['letterboxd_circuit_open'] is True
        assert snap['letterboxd_consecutive_failures'] >= main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD
        assert snap['letterboxd_last_status'] == 403

        tracker.record_letterboxd_result(success=True)
        assert tracker.snapshot()['letterboxd_consecutive_failures'] == 0
