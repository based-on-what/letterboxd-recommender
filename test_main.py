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
    with patch.object(main.MovieRecommender, 'get_page_count', return_value=2):
        assert client.post('/api/get_pages', json={'username': 'u'}).get_json()['pages'] == 2
    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([{'title': 'A', 'rating': 4}], 1)), patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        body = client.post('/api/recommend', json={'username': 'u'}).get_json()
    assert 'request_id' in body
