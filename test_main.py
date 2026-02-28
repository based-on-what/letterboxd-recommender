import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main


def _resp(payload, status=200, text=''):
    m = Mock()
    m.status_code = status
    m.json.return_value = payload
    m.text = text
    return m


def test_normalize_title():
    assert main.normalize_title('El Señor!!!') == 'el senor'


def test_get_country_name():
    r = main.MovieRecommender(country='CL')
    assert r.get_country_name() == 'Chile'


def test_safe_get_success():
    r = main.MovieRecommender()
    with patch.object(main.session, 'get', return_value=_resp({}, 200)):
        assert r._safe_get('http://x') is not None


def test_get_page_count():
    html = '<li class="paginate-page">1</li><li class="paginate-page">3</li>'
    r = main.MovieRecommender()
    with patch.object(r, '_safe_get', return_value=_resp({}, text=html)):
        assert r.get_page_count('user') == 3


def test_get_all_rated_films_uses_cache():
    r = main.MovieRecommender()
    with patch.object(main.cache, 'get', return_value={'films': [{'title': 'A'}], 'pages': 2}):
        films, pages = r.get_all_rated_films('user')
    assert films == [{'title': 'A'}]
    assert pages == 2


def test_get_tmdb_details():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    with patch.object(main.cache, 'get', return_value=None), patch.object(r, '_safe_get', return_value=_resp({'results': [{'id': 1}]})), patch.object(r, 'get_tmdb_details_by_id', return_value={'tmdb_id': 1}):
        assert r.get_tmdb_details('x')['tmdb_id'] == 1


def test_get_tmdb_details_by_id():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    payload = {'title': 'A', 'original_title': 'A', 'release_date': '2000-01-01', 'credits': {'crew': [{'job': 'Director', 'name': 'D'}]}, 'genres': [{'name': 'Drama'}], 'poster_path': None, 'vote_average': 7.3, 'runtime': 90, 'external_ids': {'imdb_id': 'tt'}}
    with patch.object(main.cache, 'get', return_value=None), patch.object(main.cache, 'set'), patch.object(r, '_safe_get', return_value=_resp(payload)):
        out = r.get_tmdb_details_by_id(1)
    assert out['director'] == 'D'


def test_analyze_preferences():
    r = main.MovieRecommender()
    p = r.analyze_preferences([{'genres': ['Drama'], 'director': 'A', 'year': '1999'}])
    assert p['genres'][0] == 'Drama'


def test_get_recommendations():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seed = [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 4.5}]
    sim_resp = _resp({'results': [{'id': 2, 'title': 'Rec'}]})
    with patch.object(main.cache, 'get', return_value=None), patch.object(main.cache, 'set'), patch.object(r, '_safe_get', return_value=sim_resp), patch.object(r, 'get_tmdb_details_by_id', return_value={'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0}), patch.object(r, 'get_streaming_by_tmdb', return_value=[]), patch.object(r, 'get_streaming', return_value=[]):
        recs = r.get_recommendations(seed, request_id='rid')
    assert recs and recs[0]['tmdb_id'] == 2


def test_get_streaming_by_tmdb():
    r = main.MovieRecommender(country='CL')
    r.tmdb_key = 'k'
    payload = {'results': {'CL': {'flatrate': [{'provider_name': 'Disney+'}]}}}
    with patch.object(main.cache, 'get', return_value=None), patch.object(main.cache, 'set'), patch.object(r, '_safe_get', return_value=_resp(payload)):
        assert r.get_streaming_by_tmdb(1) == ['Disney Plus']


def test_get_streaming_without_dependency():
    r = main.MovieRecommender()
    with patch.object(main, 'sjw', None):
        assert r.get_streaming('A') == []


def test_routes_health_get_pages_recommend():
    client = main.app.test_client()
    assert client.get('/_health').status_code == 200
    assert client.get('/api/status-stream').status_code == 400
    with patch.object(main.MovieRecommender, 'get_page_count', return_value=2):
        assert client.post('/api/get_pages', json={'username': 'u'}).get_json()['pages'] == 2
    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([{'title': 'A', 'rating': 4}], 1)), patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        body = client.post('/api/recommend', json={'username': 'u'}).get_json()
    assert 'request_id' in body


def test_get_all_rated_films_falls_back_to_stale_cache_when_live_scrape_fails():
    r = main.MovieRecommender()

    def fake_cache_get(namespace, key):
        if key.endswith(':pages:v2'):
            return None
        if key.endswith(':pages:stale:v1'):
            return {'films': [{'title': 'Stale Film', 'rating': 3.5, 'has_rating': True}], 'pages': 4}
        return None

    with patch.object(main.cache, 'get', side_effect=fake_cache_get), patch.object(r, 'get_page_count', return_value=0):
        films, pages = r.get_all_rated_films('user')

    assert pages == 4
    assert films and films[0]['title'] == 'Stale Film'
    assert r.used_stale_profile_cache is True


def test_recommend_response_marks_stale_cache_usage():
    client = main.app.test_client()

    def fake_get_all(self, username, include_unrated=True, max_pages=None):
        self.used_stale_profile_cache = True
        return ([{'title': 'A', 'rating': 4}], 1)

    with patch.object(main.MovieRecommender, 'get_all_rated_films', new=fake_get_all), patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        body = client.post('/api/recommend', json={'username': 'u'}).get_json()

    assert body['data_freshness'] == 'stale_cache'


def test_incident_tracker_opens_circuit_after_threshold():
    tracker = main.IncidentTracker()
    threshold = main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD

    for _ in range(threshold):
        tracker.record_letterboxd_result(success=False, status=403)

    snap = tracker.snapshot()
    assert snap['letterboxd_circuit_open'] is True
    assert snap['letterboxd_consecutive_failures'] >= threshold


def test_health_includes_incident_snapshot():
    client = main.app.test_client()
    resp = client.get('/_health')
    body = resp.get_json()

    assert resp.status_code == 200
    assert body['status'] == 'ok'
    assert 'incident' in body


def test_recommend_503_exposes_incident_payload():
    client = main.app.test_client()

    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([], 0)):
        resp = client.post('/api/recommend', json={'username': 'u'})

    body = resp.get_json()
    assert resp.status_code in (404, 503)
    if resp.status_code == 503:
        assert 'incident' in body
