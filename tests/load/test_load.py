"""Load scenario: concurrent async pipelines + SSE consumers, all I/O mocked.

Run explicitly with: pytest -m load
Asserts no request starves, SSE queues stay bounded, and P95 completion
latency stays within budget. Exists to catch concurrency regressions in
the executor/SSE/job plumbing.
"""

import threading
import time
from unittest.mock import patch

import pytest

import main

N_PIPELINES = 8
SSE_CONSUMERS_PER_PIPELINE = 1   # consumes the recommendations stream
COMPLETION_BUDGET_S = 30
P95_BUDGET_S = 15


def _fake_enrich(rec_sys, film):
    time.sleep(0.01)  # simulate TMDB latency without I/O
    return {'title': film['title'], 'user_rating': film.get('rating', 0)}


@pytest.mark.load
def test_concurrent_pipelines_and_sse_consumers_under_load():
    import sse

    from limiter import limiter
    if hasattr(limiter, 'enabled'):
        limiter.enabled = False  # per-IP limits would 429 the loop

    client = main.app.test_client()
    films = [{'title': f'F{i}', 'rating': 4.0, 'has_rating': True} for i in range(30)]
    done_events = {}

    def consume(rid, done):
        sub = sse.subscribe(rid, 'recommendations')
        deadline = time.time() + COMPLETION_BUDGET_S
        try:
            while time.time() < deadline:
                try:
                    if sub.get(timeout=1) == 'DONE':
                        done.set()
                        return
                except Exception:
                    continue
        finally:
            sub.close()

    with patch.object(main.MovieRecommender, 'get_all_rated_films', return_value=(films, 1)), \
         patch.object(main.MovieRecommender, 'analyze_preferences',
                      return_value={'genres': [], 'directors': [], 'decades': []}), \
         patch.object(main.MovieRecommender, 'get_recommendations', return_value=[]), \
         patch.object(main, 'enrich_film_task', side_effect=_fake_enrich):

        starts, rids, consumers = {}, [], []
        for i in range(N_PIPELINES):
            resp = client.post('/api/recommend', json={'username': f'loaduser{i}'})
            assert resp.status_code == 202
            rid = resp.get_json()['request_id']
            rids.append(rid)
            starts[rid] = time.time()
            done = threading.Event()
            done_events[rid] = done
            for _ in range(SSE_CONSUMERS_PER_PIPELINE):
                t = threading.Thread(target=consume, args=(rid, done), daemon=True)
                t.start()
                consumers.append(t)

        # Poll until every pipeline reports a final result.
        latencies = {}
        pending = set(rids)
        deadline = time.time() + COMPLETION_BUDGET_S
        while pending and time.time() < deadline:
            for rid in list(pending):
                res = client.get(f'/api/result?request_id={rid}')
                if res.status_code == 200:
                    latencies[rid] = time.time() - starts[rid]
                    pending.discard(rid)
            time.sleep(0.05)

        for t in consumers:
            t.join(timeout=5)

    # No request starves.
    assert not pending, f"pipelines never completed: {pending}"

    # P95 latency within budget.
    ordered = sorted(latencies.values())
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    assert p95 <= P95_BUDGET_S, f"P95 {p95:.2f}s exceeds {P95_BUDGET_S}s"

    # Every SSE consumer observed stream completion.
    assert all(e.is_set() for e in done_events.values())

    # No queue grew unbounded.
    with sse.REQUEST_STREAMS_LOCK:
        for streams in sse.REQUEST_STREAMS.values():
            for name in ('logs', 'recommendations', 'status'):
                assert streams[name].qsize() <= sse.SSE_QUEUE_MAXSIZE
