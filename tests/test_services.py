"""Domain logic: preferences, recommendation pipeline, dedup, early stop."""

import time
from unittest.mock import Mock, patch

import main

from testutil import _resp


def test_normalize_title():
    assert main.normalize_title('El Señor!!!') == 'el senor'


def test_get_country_name():
    r = main.MovieRecommender(country='CL')
    assert r.get_country_name() == 'Chile'


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
