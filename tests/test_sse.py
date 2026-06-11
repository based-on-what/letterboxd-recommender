"""SSE delivery: bounded queues, pub/sub backends, eviction, context propagation."""

import time

import main  # noqa: F401  — ensures app modules are importable/initialized


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


def test_sse_stream_eviction_reclaims_stale_entries():
    import sse

    rid = 'evict-me'
    streams = sse._get_or_create_streams(rid)
    with sse.REQUEST_STREAMS_LOCK:
        streams['_created'] = time.time() - sse.STREAM_MAX_AGE_S - 1
    sse._last_stream_eviction_s = 0.0  # force the next sweep to run

    sse._get_or_create_streams('evict-trigger')  # any access triggers the sweep

    assert 'evict-me' not in sse.REQUEST_STREAMS
    sse._cleanup_request_streams('evict-trigger')


def test_request_id_propagates_to_worker_threads():
    from executors import WORK_EXECUTOR, submit_with_context
    from sse import REQUEST_ID_CTX

    token = REQUEST_ID_CTX.set('ctx-rid')
    try:
        fut = submit_with_context(WORK_EXECUTOR, REQUEST_ID_CTX.get)
        assert fut.result(timeout=5) == 'ctx-rid'
    finally:
        REQUEST_ID_CTX.reset(token)
