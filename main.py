"""
main.py — Application entry point and thin orchestrator.

Responsibilities:
- Load environment (dotenv must run before any os.getenv at module level).
- Configure logging with SSE QueueHandler.
- Create the Flask app and register the Blueprint.
- Re-export symbols expected by tests (session, sjw, cache, …).
- Provide the `app` object for Gunicorn / Flask CLI.
"""

# load_dotenv MUST run before any module that calls os.getenv at import time.
from dotenv import load_dotenv
load_dotenv()

import atexit
import logging
import os

from flask import Flask
from flask_cors import CORS

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.getLogger('httpx').setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("letterboxd-recommender")

# Attach the SSE QueueHandler so log records flow into per-request streams.
from sse import QueueHandler as _QueueHandler

_queue_handler = _QueueHandler()
logger.addHandler(_queue_handler)

# ─── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ─── Rate limiter ─────────────────────────────────────────────────────────────
from limiter import limiter
limiter.init_app(app)

# ─── Blueprint ────────────────────────────────────────────────────────────────
from routes import bp
app.register_blueprint(bp)

# ─── Re-exports for test / backward-compat ────────────────────────────────────
# Tests reference these via `main.<name>`; __all__ tells Pylance they're public.
from cache import cache
from recommender import (
    session,
    enrich_film_task,
    IncidentTracker,
    INCIDENT_TRACKER,
    LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD,
    MovieRecommender,
    normalize_title,
)

try:
    from recommender import sjw
except ImportError:
    sjw = None

__all__ = [
    "app",
    "cache",
    "session",
    "sjw",
    "enrich_film_task",
    "IncidentTracker",
    "INCIDENT_TRACKER",
    "LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD",
    "MovieRecommender",
    "normalize_title",
]

# ─── Shutdown hook ────────────────────────────────────────────────────────────
@atexit.register
def on_exit():
    logger.info("Shutting down worker and freeing resources...")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)
