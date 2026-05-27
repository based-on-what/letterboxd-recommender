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
import logging
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
    """Thread-safe dict with per-key TTL. Used as in-memory fallback for Cache."""

    def __init__(self, max_size: int = _EXPIRING_DICT_MAX_SIZE):
        self._data: dict = {}
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
            return value

    def set(self, key, value, ttl=None):
        exp = time.time() + ttl if ttl is not None else None
        with self._lock:
            self._evict_expired()
            if len(self._data) >= self._max_size and key not in self._data:
                # Evict oldest-expiring entry to stay under cap.
                oldest = min(self._data, key=lambda k: self._data[k][1] or float('inf'))
                del self._data[oldest]
            self._data[key] = (value, exp)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Simple rate controller used to avoid overwhelming external APIs.

    Enforces a minimum interval between requests so shared services respect
    vendor throttling limits even when multiple threads issue calls.
    """

    def __init__(self, min_interval: float = 0.25):
        self.min_interval = min_interval
        self._lock = Lock()
        self._last = 0.0

    def wait(self):
        """Block until this thread's reserved slot arrives.

        Pre-allocates a slot by advancing _last under the lock, so concurrent
        callers each get a distinct departure time instead of all waking at
        the same instant (thundering herd).
        """
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
        self.caches: dict = {
            'tmdb':       _ExpiringDict(),
            'similar':    _ExpiringDict(),
            'streaming':  _ExpiringDict(),
            'user_scrape': _ExpiringDict(),
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


# Module-level singleton
cache = Cache()
