"""
main.py — Application entry point.

load_dotenv runs first so every os.getenv() at module-level in imported
modules sees the correct values.
"""

from dotenv import load_dotenv
load_dotenv()

import atexit
import logging
import os

from flask import Flask
from flask_cors import CORS

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger("letterboxd-recommender")

from sse import QueueHandler as _QueueHandler
_queue_handler = _QueueHandler()
logger.addHandler(_queue_handler)

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

from limiter import limiter
limiter.init_app(app)

from routes import bp
app.register_blueprint(bp)

# Re-exports consumed by tests via `main.<name>`
from cache import cache
from recommender import (
    session,
    enrich_film_task,
    IncidentTracker,
    INCIDENT_TRACKER,
    LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD,
    MovieRecommender,
    normalize_title,
    sjw,
)

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


@atexit.register
def on_exit():
    logger.info("Shutting down worker and freeing resources...")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    logger.info("Starting server on port %d (debug=%s)", port, debug)
    app.run(host='0.0.0.0', port=port, debug=debug)
