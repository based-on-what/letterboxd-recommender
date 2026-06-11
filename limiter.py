"""
limiter.py — Flask-Limiter singleton using deferred init_app pattern.

Import `limiter` in routes.py and apply @limiter.limit() decorators.
In main.py, call limiter.init_app(app) before registering the blueprint.

Storage resolution: RATELIMIT_STORAGE_URI wins if set; otherwise REDIS_URL
is reused so multi-worker deployments share one rate-limit store; otherwise
memory:// (per-process, limits multiply by worker count).
"""

import os
import logging

from utils import IS_DEV

logger = logging.getLogger("letterboxd-recommender")


def _resolve_storage_uri() -> str:
    explicit = os.getenv('RATELIMIT_STORAGE_URI')
    if explicit:
        return explicit
    redis_url = os.getenv('REDIS_URL')
    if redis_url:
        return redis_url
    return 'memory://'


RATE_LIMIT_STORAGE_URI = _resolve_storage_uri()

if RATE_LIMIT_STORAGE_URI.startswith('memory') and not IS_DEV:
    logger.warning(
        "Flask-Limiter using memory:// storage: per-IP limits are per-process "
        "and multiply by gunicorn worker count. Set REDIS_URL or "
        "RATELIMIT_STORAGE_URI for shared storage."
    )

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[],
        storage_uri=RATE_LIMIT_STORAGE_URI,
    )
except ImportError:
    logger.warning('flask-limiter unavailable; endpoint rate limiting disabled')

    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def deco(func):
                return func
            return deco

        def init_app(self, app):
            pass

    limiter = _NoopLimiter()
