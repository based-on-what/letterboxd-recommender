"""Cache layer: LRU/TTL behavior, stampede protection, rate limiter, limiter config."""

import time
from unittest.mock import patch

import main


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


def test_limiter_storage_uri_resolution(monkeypatch):
    from limiter import _resolve_storage_uri

    monkeypatch.delenv('RATELIMIT_STORAGE_URI', raising=False)
    monkeypatch.delenv('REDIS_URL', raising=False)
    assert _resolve_storage_uri() == 'memory://'

    monkeypatch.setenv('REDIS_URL', 'redis://shared:6379')
    assert _resolve_storage_uri() == 'redis://shared:6379'

    monkeypatch.setenv('RATELIMIT_STORAGE_URI', 'memory://')
    assert _resolve_storage_uri() == 'memory://'
