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

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None
    logger.warning("cachetools unavailable; using simple dict cache")

REDIS_URL = os.getenv("REDIS_URL")


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
        """Block until enough time has elapsed since the previous request.

        Releases the lock before sleeping so other threads can compute their
        own slot concurrently instead of serializing behind one sleep call.
        """
        while True:
            with self._lock:
                now = time.time()
                diff = now - self._last
                if diff >= self.min_interval:
                    self._last = now
                    return
                sleep_time = self.min_interval - diff
            time.sleep(sleep_time)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class Cache:
    """
    Cache abstraction with Redis and in-memory fallbacks.

    Provides simple namespaced storage with TTL support. If Redis is
    unavailable, falls back to local TTL caches or plain dictionaries.
    """

    def __init__(self):
        self.redis = None
        self._redis_attempted = False
        self._init_lock = Lock()
        self.caches: dict = {}

        if TTLCache is None:
            self.caches['tmdb']       = {}
            self.caches['similar']    = {}
            self.caches['streaming']  = {}
            self.caches['user_scrape'] = {}
        else:
            self.caches['tmdb']       = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['similar']    = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['streaming']  = TTLCache(maxsize=5000, ttl=ONE_DAY)
            self.caches['user_scrape'] = TTLCache(maxsize=1000, ttl=ONE_WEEK)

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
        bucket = self.caches.get(namespace)
        return bucket.get(key) if bucket else None

    def set(self, namespace: str, key: str, value, ttl=None):
        """Store a value in cache.

        Args:
            namespace: Cache bucket name.
            key: Item key to write.
            value: Data to persist.
            ttl: Optional TTL in seconds (Redis only).
        """
        if not self._redis_attempted:
            self._init_redis()
        if self.redis:
            self._redis_set(f"{namespace}:{key}", value, ex=ttl)
        else:
            if self.caches.get(namespace) is not None:
                self.caches[namespace][key] = value


# Module-level singleton
cache = Cache()
