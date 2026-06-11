import time
from unittest.mock import Mock, patch

import main


class _FakeRedisKV:
    """Minimal dict-backed redis stand-in for circuit-breaker tests."""

    def __init__(self):
        self.store = {}
        self.expiry = {}

    def incr(self, k):
        v = int(self.store.get(k, 0)) + 1
        self.store[k] = v
        return v

    def set(self, k, v, ex=None):
        self.store[k] = v
        if ex is not None:
            self.expiry[k] = ex

    def get(self, k):
        v = self.store.get(k)
        return str(v) if v is not None else None

    def delete(self, k):
        self.store.pop(k, None)

    def exists(self, k):
        return 1 if k in self.store else 0

    def ttl(self, k):
        if k not in self.store:
            return -2
        return self.expiry.get(k, -1)


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


def test_analyze_preferences():
    r = main.MovieRecommender()
    p = r.analyze_preferences([{'genres': ['Drama'], 'director': 'A', 'year': '1999'}])
    assert p['genres'][0] == 'Drama'


def test_get_recommendations():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seed = [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 4.5}]
    sim_resp = _resp({'results': [{'id': 2, 'title': 'Rec'}]})
    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, '_get', return_value=sim_resp), \
         patch.object(r._tmdb, 'get_details_by_id', return_value={
             'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0,
         }), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        recs = r.get_recommendations(seed, request_id='rid')
    assert recs and recs[0]['tmdb_id'] == 2


def test_service_recommendations_callable_without_flask_or_sse():
    from services.recommender import get_recommendations as svc_get

    tmdb = Mock()
    tmdb.get_similar.return_value = [{'id': 2, 'title': 'Rec'}]
    tmdb.get_details_by_id.return_value = {
        'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0,
    }
    streaming = Mock()
    streaming.get_by_tmdb_id.return_value = []
    streaming.get_by_title.return_value = []

    seen_recs, seen_status = [], []
    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'):
        recs = svc_get(
            tmdb, streaming,
            [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 5.0}],
            username='u',
            on_recommendation=seen_recs.append,
            on_status=seen_status.append,
        )

    assert recs and recs[0]['tmdb_id'] == 2
    assert seen_recs and seen_recs[0]['title'] == 'Rec'
    assert seen_status and seen_status[0]['username'] == 'u'


def test_similar_cache_stores_only_id_list():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seed = [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 5.0}]
    stored = {}

    def fake_set(ns, key, val, ttl=None):
        stored.setdefault(ns, {})[key] = val

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set', side_effect=fake_set), \
         patch.object(r._tmdb, 'get_similar', return_value=[{'id': 2, 'title': 'Rec'}]), \
         patch.object(r._tmdb, 'get_details_by_id', return_value={
             'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0,
         }), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        r.get_recommendations(seed)

    # global namespace holds the bare ID list — no enriched payloads, no user data
    assert stored['similar']['similar:1'] == [2]


def test_similar_flow_does_not_mutate_cached_details():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seed = [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 5.0}]
    cached_det = {'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0}

    def fake_get(ns, key):
        return [2] if ns == 'similar' else None

    with patch.object(main.cache, 'get', side_effect=fake_get), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, 'get_details_by_id', return_value=cached_det), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=['Netflix']), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        recs = r.get_recommendations(seed)

    assert recs and recs[0]['streaming'] == ['Netflix']
    # the dict held by the tmdb cache must stay pristine
    assert 'reason' not in cached_det and 'streaming' not in cached_det


def test_dedup_drops_missing_ids_collisions_and_double_seeds():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seeds = [
        {'tmdb_id': 1, 'title': 'SeedOne', 'user_rating': 5.0},
        {'tmdb_id': 2, 'title': 'SeedTwo', 'user_rating': 4.5},
    ]

    def fake_details(mid, fr=False):
        return {
            99: {'tmdb_id': 99, 'title': 'Unique', 'original_title': 'Unique', 'rating_tmdb': 8.0},
            100: {'tmdb_id': None, 'title': 'NoId', 'original_title': 'NoId', 'rating_tmdb': 8.0},
            101: {'tmdb_id': 101, 'title': 'SeedOne', 'original_title': 'SeedOne', 'rating_tmdb': 8.0},
        }[mid]

    similar = [{'id': 99, 'title': 'Unique'}, {'id': 100, 'title': 'NoId'}, {'id': 101, 'title': 'SeedOne'}]

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, 'get_similar', return_value=similar), \
         patch.object(r._tmdb, 'get_details_by_id', side_effect=fake_details), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        recs = r.get_recommendations(seeds)

    # same film from both seeds collapses to one; tmdb_id=None dropped;
    # title collision with a seen film excluded
    assert [x['tmdb_id'] for x in recs] == [99]


def test_streaming_lookup_deduped_across_seeds():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seeds = [
        {'tmdb_id': 1, 'title': 'A', 'user_rating': 5.0},
        {'tmdb_id': 2, 'title': 'B', 'user_rating': 4.5},
    ]
    stream_calls = []

    def fake_stream(tid, force_refresh=False):
        stream_calls.append(tid)
        return ['Netflix']

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, 'get_similar', return_value=[{'id': 99, 'title': 'R'}]), \
         patch.object(r._tmdb, 'get_details_by_id', return_value={
             'tmdb_id': 99, 'title': 'R', 'original_title': 'R', 'rating_tmdb': 8.0,
         }), \
         patch.object(r._streaming, 'get_by_tmdb_id', side_effect=fake_stream), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        recs = r.get_recommendations(seeds)

    # film 99 reached from both seeds: exactly one streaming lookup
    assert stream_calls.count(99) == 1
    assert recs and recs[0]['streaming'] == ['Netflix']


def test_get_recommendations_early_stop_cancels_pending_seeds():
    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seeds = [{'tmdb_id': i, 'title': f'S{i}', 'user_rating': 4.5} for i in range(1, 41)]
    similar_calls = []

    def fake_get_similar(mid, limit=12):
        similar_calls.append(mid)
        time.sleep(0.02)
        return [{'id': 1000 + mid, 'title': f'R{mid}'}]

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, 'get_similar', side_effect=fake_get_similar), \
         patch.object(r._tmdb, 'get_details_by_id',
                      side_effect=lambda mid, fr=False: {
                          'tmdb_id': mid, 'title': f'R{mid}',
                          'original_title': f'R{mid}', 'rating_tmdb': 8.0,
                      }), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        recs = r.get_recommendations(seeds, count=3)

    assert len(recs) == 3
    # without early stop all 40 seeds would be processed
    assert len(similar_calls) < 40


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


def test_concurrent_recommendations_on_shared_pool():
    import threading

    r = main.MovieRecommender()
    r.tmdb_key = 'k'
    seed = [{'tmdb_id': 1, 'title': 'Seen', 'user_rating': 4.5}]
    sim_resp = _resp({'results': [{'id': 2, 'title': 'Rec'}]})
    results = []

    with patch.object(main.cache, 'get', return_value=None), \
         patch.object(main.cache, 'set'), \
         patch.object(r._tmdb, '_get', return_value=sim_resp), \
         patch.object(r._tmdb, 'get_details_by_id', return_value={
             'tmdb_id': 2, 'title': 'Rec', 'original_title': 'Rec', 'rating_tmdb': 8.0,
         }), \
         patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]), \
         patch.object(r._streaming, 'get_by_title', return_value=[]):
        def run():
            results.append(r.get_recommendations(list(seed)))

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(results) == 5
    assert all(res and res[0]['tmdb_id'] == 2 for res in results)


def test_sse_publish_subscribe_in_memory_roundtrip():
    import sse

    rid = 'rid-mem-roundtrip'
    sub = sse.subscribe(rid, 'recommendations')
    sse.publish(rid, 'recommendations', {'tmdb_id': 7, 'title': 'X'})
    assert sub.get(timeout=1) == {'tmdb_id': 7, 'title': 'X'}
    sub.close()
    sse._cleanup_request_streams(rid)


def test_sse_redis_pubsub_delivers_across_clients():
    import json
    import queue as q

    import sse

    channels: dict = {}

    class FakePubSub:
        def __init__(self):
            self.msgs = []

        def subscribe(self, ch):
            channels.setdefault(ch, []).append(self)

        def get_message(self, ignore_subscribe_messages=True, timeout=None):
            if self.msgs:
                return {'type': 'message', 'data': self.msgs.pop(0)}
            return None

        def unsubscribe(self):
            pass

        def close(self):
            pass

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

        def publish(self, ch, data):
            for s in channels.get(ch, []):
                s.msgs.append(data)

    old = (sse._redis_client, sse._redis_attempted)
    sse._redis_client, sse._redis_attempted = FakeRedis(), True
    try:
        sub = sse.subscribe('rid-redis', 'logs')          # consumer "worker"
        FakeRedis().publish('sse:rid-redis:logs', json.dumps('hola'))  # producer "worker"
        assert sub.get(timeout=1) == 'hola'
        try:
            sub.get(timeout=0)
            raise AssertionError('expected queue.Empty')
        except q.Empty:
            pass
        sub.close()
    finally:
        sse._redis_client, sse._redis_attempted = old


def test_sse_bounded_queue_drops_oldest_without_blocking():
    from sse import BoundedDropQueue

    q = BoundedDropQueue(maxsize=5)
    for i in range(10):
        q.put(i)  # must never block

    assert q.qsize() == 5
    assert q.dropped == 5
    assert q.get_nowait() == 5  # oldest five were dropped


def test_sse_connection_tracking_never_negative_under_concurrency():
    import threading

    import sse

    rid = 'test-concurrent-tracking'
    sse._get_or_create_streams(rid)

    def churn():
        for _ in range(200):
            sse._track_stream_connection(rid, 'logs', True)
            sse._track_stream_connection(rid, 'logs', False)

    threads = [threading.Thread(target=churn) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    streams = sse.REQUEST_STREAMS.get(rid)
    assert streams is not None  # not done → must not be orphan-collected
    assert streams['logs_connected'] == 0
    sse._cleanup_request_streams(rid)


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


def test_expiring_dict_evicts_lru_not_scan():
    from cache import _ExpiringDict

    d = _ExpiringDict(max_size=3)
    d.set('a', 1)
    d.set('b', 2)
    d.set('c', 3)
    assert d.get('a') == 1  # 'a' becomes most recently used

    d.set('d', 4)  # cap reached: least-recently-used ('b') is evicted in O(1)

    assert d.get('b') is None
    assert d.get('a') == 1
    assert d.get('c') == 3
    assert d.get('d') == 4


def test_expiring_dict_honors_ttl_on_read():
    from cache import _ExpiringDict

    d = _ExpiringDict(max_size=3)
    d.set('k', 'v', ttl=0.05)
    assert d.get('k') == 'v'
    time.sleep(0.06)
    assert d.get('k') is None


def test_get_or_compute_runs_fn_once_under_concurrency():
    import threading

    from cache import Cache

    c = Cache()
    c._redis_attempted = True  # skip Redis: exercise the in-memory path
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.1)
        return {'v': 42}

    results = []

    def worker():
        results.append(c.get_or_compute('tmdb', 'stampede-key', compute, ttl=60))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert results == [{'v': 42}, {'v': 42}]


def test_limiter_storage_uri_resolution(monkeypatch):
    from limiter import _resolve_storage_uri

    monkeypatch.delenv('RATELIMIT_STORAGE_URI', raising=False)
    monkeypatch.delenv('REDIS_URL', raising=False)
    assert _resolve_storage_uri() == 'memory://'

    monkeypatch.setenv('REDIS_URL', 'redis://shared:6379')
    assert _resolve_storage_uri() == 'redis://shared:6379'

    monkeypatch.setenv('RATELIMIT_STORAGE_URI', 'memory://')
    assert _resolve_storage_uri() == 'memory://'


def test_incident_tracker_opens_circuit_after_threshold():
    tracker = main.IncidentTracker()
    threshold = main.LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD

    for _ in range(threshold):
        tracker.record_letterboxd_result(success=False, status=403)

    snap = tracker.snapshot()
    assert snap['letterboxd_circuit_open'] is True
    assert snap['letterboxd_consecutive_failures'] >= threshold


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


def test_rate_limiter_redis_backend_spaces_calls():
    from cache import RateLimiter

    class _FakeRedisEval:
        def __init__(self):
            self.next_slot = {}

        def eval(self, script, numkeys, key, now, interval):
            now, interval = float(now), float(interval)
            slot = max(now, self.next_slot.get(key, 0.0))
            self.next_slot[key] = slot + interval
            return str(slot)

    rl = RateLimiter(min_interval=0.05, name='test-shared')
    with patch.object(main.cache, 'redis', _FakeRedisEval()), \
         patch.object(main.cache, '_redis_attempted', True):
        t0 = time.time()
        for _ in range(3):
            rl.wait()
        elapsed = time.time() - t0

    # three calls share slots spaced 0.05s apart: >= ~0.1s total
    assert elapsed >= 0.08


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
        resp = client.post('/api/recommend', json={'username': 'u', 'sync': True})

    body = resp.get_json()
    assert resp.status_code in (404, 503)
    if resp.status_code == 503:
        assert 'incident' in body


def test_recommend_async_returns_202_then_result():
    import time as _t

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
            _t.sleep(0.1)

    assert result.status_code == 200
    assert result.get_json()['username'] == 'u'
    assert client.get('/api/result?request_id=nope').status_code == 404
