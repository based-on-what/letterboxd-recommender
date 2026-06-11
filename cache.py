"""
cache.py — Cache abstraction and rate-limiter utilities.

Provides:
- TTL constants shared across the application.
- RateLimiter: token-bucket-style per-service throttle.
- Cache: Redis-first, in-memory-fallback cache with namespaced keys.
- Module-level ``cache`` singleton used by the rest of the app.
"""

import os
import json
import time
import uuid
import logging
from collections import OrderedDict
from threading import Lock

logger = logging.getLogger("letterboxd-recommender")

# ---------------------------------------------------------------------------
# TTL constants
# ---------------------------------------------------------------------------
ONE_MONTH         = 60 * 60 * 24 * 30
ONE_WEEK          = 60 * 60 * 24 * 7
ONE_DAY           = 60 * 60 * 24
SIX_HOURS         = 60 * 60 * 6
TWO_HOURS         = 60 * 60 * 2
USER_CACHE_TTL    = 60 * 30          # fresh profile cache: 30 min
USER_STALE_CACHE_TTL = ONE_WEEK      # stale-fallback profile cache: 7 days

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import redis as _redis_lib
except ImportError:
    _redis_lib = None
    logger.warning("Redis unavailable; falling back to in-memory cache")

REDIS_URL = os.getenv("REDIS_URL")


# ---------------------------------------------------------------------------
# _ExpiringDict — per-key TTL in-memory store (used when Redis is absent)
# ---------------------------------------------------------------------------
_EXPIRING_DICT_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "10000"))
_EXPIRING_DICT_EVICT_INTERVAL = 120.0


class _ExpiringDict:
    """Thread-safe LRU dict with per-key TTL. In-memory fallback for Cache.

    Backed by an OrderedDict in access order: reads move_to_end (O(1)) and
    the size-cap eviction pops the least-recently-used entry (O(1)) instead
    of scanning the whole dict.
    """

    def __init__(self, max_size: int = _EXPIRING_DICT_MAX_SIZE):
        self._data: OrderedDict = OrderedDict()
        self._lock = Lock()
        self._max_size = max_size
        self._last_sweep = 0.0

    def _evict_expired(self) -> None:
        now = time.time()
        if now - self._last_sweep < _EXPIRING_DICT_EVICT_INTERVAL:
            return
        self._last_sweep = now
        expired = [k for k, (_, exp) in self._data.items() if exp is not None and now >= exp]
        for k in expired:
            del self._data[k]

    def get(self, key, default=None):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return default
            value, exp = item
            if exp is not None and time.time() >= exp:
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def set(self, key, value, ttl=None):
        exp = time.time() + ttl if ttl is not None else None
        with self._lock:
            self._evict_expired()
            if key in self._data:
                self._data.move_to_end(key)
            elif len(self._data) >= self._max_size:
                self._data.popitem(last=False)  # O(1) LRU eviction
            self._data[key] = (value, exp)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------
# Atomically reserves the next departure slot for a shared limiter key.
# Keys self-expire so an idle limiter leaves no state behind.
_RATE_SLOT_LUA = """
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = now
if nxt > now then slot = nxt end
redis.call('SET', KEYS[1], tostring(slot + interval), 'EX', 60)
return tostring(slot)
"""


class RateLimiter:
    """
    Simple rate controller used to avoid overwhelming external APIs.

    Enforces a minimum interval between requests so shared services respect
    vendor throttling limits even when multiple threads issue calls. When a
    `name` is given and Redis is available, the interval is enforced across
    all workers (otherwise each process rate-limits independently).
    """

    def __init__(self, min_interval: float = 0.25, name: str = None):
        self.min_interval = min_interval
        self._name = name
        self._lock = Lock()
        self._last = 0.0

    def _redis(self):
        if not self._name:
            return None
        if not cache._redis_attempted:
            cache._init_redis()
        return cache.redis

    def wait(self):
        """Block until this caller's reserved slot arrives.

        Pre-allocates a slot (atomically, in Redis when available) so
        concurrent callers each get a distinct departure time instead of all
        waking at the same instant (thundering herd).
        """
        r = self._redis()
        if r is not None:
            try:
                now = time.time()
                slot = float(r.eval(_RATE_SLOT_LUA, 1, f"rl:{self._name}", now, self.min_interval))
                delay = slot - now
                if delay > 0:
                    time.sleep(delay)
                return
            except Exception as exc:
                logger.debug("Redis rate limiter failed; using in-process fallback: %s", exc)

        while True:
            with self._lock:
                now = time.time()
                next_allowed = self._last + self.min_interval
                if now >= next_allowed:
                    self._last = now
                    return
                # Reserve this thread's slot at next_allowed so the next
                # waiter gets scheduled one interval later.
                self._last = next_allowed
                sleep_time = next_allowed - now
            time.sleep(sleep_time)
            return


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class Cache:
    """
    Cache abstraction with Redis and in-memory fallbacks.

    Provides simple namespaced storage with per-key TTL support. If Redis is
    unavailable, falls back to an in-memory _ExpiringDict per namespace.
    """

    def __init__(self):
        self.redis = None
        self._redis_attempted = False
        self._init_lock = Lock()
        self._compute_locks: dict = {}
        self._compute_locks_guard = Lock()
        self.caches: dict = {
            'tmdb':       _ExpiringDict(),
            'similar':    _ExpiringDict(),
            'streaming':  _ExpiringDict(),
            'user_scrape': _ExpiringDict(),
            'jobs':       _ExpiringDict(),
        }

    def _init_redis(self):
        """Attempt to initialize Redis exactly once."""
        with self._init_lock:
            if self._redis_attempted:
                return
            self._redis_attempted = True
            if not REDIS_URL or not _redis_lib:
                return
            try:
                self.redis = _redis_lib.from_url(REDIS_URL, decode_responses=True)
                self.redis.ping()
                logger.info("Redis connected successfully")
            except Exception as e:
                logger.warning(f"Could not connect to Redis: {e}")
                self.redis = None

    def _redis_get(self, key: str):
        """Fetch a JSON value from Redis."""
        try:
            val = self.redis.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None

    def _redis_set(self, key: str, value, ex=None):
        """Store a JSON value in Redis."""
        try:
            self.redis.set(key, json.dumps(value), ex=ex)
        except Exception:
            pass

    def get(self, namespace: str, key: str):
        """Return a value from cache, preferring Redis over memory.

        Args:
            namespace: Cache bucket name (tmdb, similar, streaming, user_scrape).
            key: Item key inside the namespace.
        """
        if not self._redis_attempted:
            self._init_redis()
        if self.redis:
            return self._redis_get(f"{namespace}:{key}")
        bucket: _ExpiringDict | None = self.caches.get(namespace)
        return bucket.get(key) if bucket else None

    def set(self, namespace: str, key: str, value, ttl=None):
        """Store a value in cache.

        Args:
            namespace: Cache bucket name.
            key: Item key to write.
            value: Data to persist.
            ttl: Optional TTL in seconds (honored by both Redis and in-memory backends).
        """
        if not self._redis_attempted:
            self._init_redis()
        if self.redis:
            self._redis_set(f"{namespace}:{key}", value, ex=ttl)
        else:
            bucket: _ExpiringDict | None = self.caches.get(namespace)
            if bucket is not None:
                bucket.set(key, value, ttl=ttl)

    def get_or_compute(self, namespace: str, key: str, fn, ttl=None, lock_timeout: int = 30):
        """Stampede-safe read-through: on a miss, one caller runs *fn* while
        concurrent callers briefly wait for the value (Redis: SET NX fetch
        lock; in-memory: per-key thread lock). fn() results that are None are
        served but not cached.
        """
        val = self.get(namespace, key)
        if val is not None:
            return val

        if not self._redis_attempted:
            self._init_redis()

        if self.redis:
            lock_key = f"lock:{namespace}:{key}"
            token = uuid.uuid4().hex
            try:
                acquired = self.redis.set(lock_key, token, nx=True, ex=lock_timeout)
            except Exception:
                acquired = True  # Redis flaky: compute without coordination
            if acquired:
                try:
                    val = self.get(namespace, key)  # may have landed meanwhile
                    if val is not None:
                        return val
                    val = fn()
                    if val is not None:
                        self.set(namespace, key, val, ttl=ttl)
                    return val
                finally:
                    try:
                        if self.redis.get(lock_key) == token:
                            self.redis.delete(lock_key)
                    except Exception:
                        pass

            # Another caller is computing: wait for the value or the lock.
            deadline = time.time() + lock_timeout
            while time.time() < deadline:
                time.sleep(0.1)
                val = self.get(namespace, key)
                if val is not None:
                    return val
                try:
                    if not self.redis.exists(lock_key):
                        break
                except Exception:
                    break
            val = fn()
            if val is not None:
                self.set(namespace, key, val, ttl=ttl)
            return val

        with self._compute_locks_guard:
            lock = self._compute_locks.setdefault(f"{namespace}:{key}", Lock())
        with lock:
            val = self.get(namespace, key)
            if val is not None:
                return val
            val = fn()
            if val is not None:
                self.set(namespace, key, val, ttl=ttl)
            return val


# Module-level singleton
cache = Cache()
