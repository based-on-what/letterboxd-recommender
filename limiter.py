"""
limiter.py — Flask-Limiter singleton using deferred init_app pattern.

Import `limiter` in routes.py and apply @limiter.limit() decorators.
In main.py, call limiter.init_app(app) before registering the blueprint.
"""

import os
import logging

logger = logging.getLogger("letterboxd-recommender")

RATE_LIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')

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
