"""HTTP layer: health, validation, sync/async recommend, result polling."""

import time
from unittest.mock import patch

import main


def test_routes_health_get_pages_recommend():
    client = main.app.test_client()
    assert client.get('/_health').status_code == 200
    assert client.get('/api/status-stream').status_code == 400
    with patch.object(main.MovieRecommender, 'get_page_count', return_value=2):
        assert client.post('/api/get_pages', json={'username': 'u'}).get_json()['pages'] == 2
    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([{'title': 'A', 'rating': 4}], 1)), \
         patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), \
         patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), \
         patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        body = client.post('/api/recommend', json={'username': 'u', 'sync': True}).get_json()
    assert 'request_id' in body


def test_health_includes_incident_snapshot():
    client = main.app.test_client()
    resp = client.get('/_health')
    body = resp.get_json()

    assert resp.status_code == 200
    assert body['status'] == 'ok'
    assert 'incident' in body


def test_recommend_response_marks_stale_cache_usage():
    client = main.app.test_client()

    def fake_get_all(self, *_, **__):
        self.used_stale_profile_cache = True
        return ([{'title': 'A', 'rating': 4}], 1)

    with patch.object(main.MovieRecommender, 'get_all_rated_films', new=fake_get_all), \
         patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), \
         patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), \
         patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        body = client.post('/api/recommend', json={'username': 'u', 'sync': True}).get_json()

    assert body['data_freshness'] == 'stale_cache'


def test_recommend_503_exposes_incident_payload():
    client = main.app.test_client()

    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([], 0)):
        resp = client.post('/api/recommend', json={'username': 'u', 'sync': True})

    body = resp.get_json()
    assert resp.status_code in (404, 503)
    if resp.status_code == 503:
        assert 'incident' in body


def test_recommend_async_returns_202_then_result():
    client = main.app.test_client()

    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([{'title': 'A', 'rating': 4}], 1)), \
         patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), \
         patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), \
         patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        resp = client.post('/api/recommend', json={'username': 'u'})
        assert resp.status_code == 202
        rid = resp.get_json()['request_id']

        result = None
        for _ in range(100):
            result = client.get(f'/api/result?request_id={rid}')
            if result.status_code != 202:
                break
            time.sleep(0.1)

    assert result.status_code == 200
    assert result.get_json()['username'] == 'u'
    assert client.get('/api/result?request_id=nope').status_code == 404


def test_concurrent_pipelines_fully_mocked():
    client = main.app.test_client()

    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=([{'title': 'A', 'rating': 4}], 1)), \
         patch.object(main.MovieRecommender, 'analyze_preferences', return_value={'genres': [], 'directors': [], 'decades': []}), \
         patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), \
         patch.object(main, 'enrich_film_task', return_value={'title': 'A', 'user_rating': 4}):
        rids = []
        for i in range(4):
            resp = client.post('/api/recommend', json={'username': f'user{i}'})
            assert resp.status_code == 202
            rids.append(resp.get_json()['request_id'])

        pending = set(rids)
        deadline = time.time() + 15
        while pending and time.time() < deadline:
            for rid in list(pending):
                res = client.get(f'/api/result?request_id={rid}')
                if res.status_code == 200:
                    pending.discard(rid)
            time.sleep(0.1)

    assert not pending  # every concurrent pipeline completed
