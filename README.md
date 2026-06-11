# Letterboxd Recommender

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.1+-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Live Demo](https://img.shields.io/badge/Demo-Live-success.svg)](https://letterboxd-recommender.up.railway.app/)

**Letterboxd Recommender** is an intelligent movie recommendation system that analyzes your Letterboxd profile to deliver personalized film suggestions. By combining web scraping, TMDB metadata enrichment, and streaming availability lookups, it helps you discover your next favorite movie based on what you already love.

Live Demo: [letterboxd-recommender.up.railway.app](https://letterboxd-recommender.up.railway.app/)

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Personalized Recommendations**: Analyzes your 4+ star films and queries TMDB for similar titles
- **Streaming Availability**: Shows where each recommendation is available to stream in your country
- **Smart Preference Analysis**: Identifies your top genres, directors, and decades from your viewing history
- **Real-Time Updates**: Server-Sent Events (SSE) deliver logs, recommendations, and status updates live
- **Intelligent Caching**: Redis support with automatic fallback to a thread-safe in-memory TTL store
- **Concurrent Processing**: ThreadPoolExecutor with 6 enrichment workers and 4 similarity workers
- **Clean UI**: Framework-free vanilla JavaScript frontend with dark mode
- **Deduplication**: Filters out already-watched films by TMDB ID and normalized title
- **Multi-Country Support**: Streaming availability for 13+ countries
- **Circuit Breaker**: Auto-pauses live Letterboxd scraping after consecutive failures and serves stale cache
- **Anti-Bot Resilience**: 4-tier fallback chain (requests → cloudscraper → curl_cffi → camoufox)
- **Rate Limiting**: Per-IP endpoint throttling via Flask-Limiter
- **Dual TMDB Auth**: Auto-detects v3 API key (query-param) vs v4 Bearer token

---

## How It Works

The recommendation engine follows a multi-step pipeline:

1. **Profile Scraping**: Fetches all films from a user's Letterboxd profile (including unrated entries to track what you have already seen)
2. **TMDB Enrichment**: Augments each film with metadata from The Movie Database (year, genres, director, runtime, poster, rating) using up to 6 parallel workers
3. **Preference Analysis**: Identifies top-3 genres, directors, and decades from your viewing history
4. **Similar Film Discovery**: For every 4+ star film (seed films), queries TMDB for up to 12 similar titles per seed
5. **Intelligent Filtering**: Removes already-watched films, applies minimum rating threshold (default: 7.0), deduplicates by TMDB ID and normalized title
6. **Streaming Lookup**: Checks availability across multiple platforms for your selected country via JustWatch (with TMDB watch providers as fallback)
7. **Real-Time Delivery**: Streams recommendations to the frontend as they are discovered via three SSE channels (logs, recommendations, status)

If live Letterboxd scraping fails (circuit breaker open), the API returns a cached profile snapshot with a `data_freshness: stale_cache` marker so the UI can warn the user.

---

## Tech Stack

### Backend

- **[Python 3.10+](https://www.python.org/)**: Core application language
- **[Flask 3.1](https://flask.palletsprojects.com/)**: Lightweight web framework with Blueprint routing
- **[Flask-CORS](https://flask-cors.readthedocs.io/)**: Cross-Origin Resource Sharing headers
- **[BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)**: HTML parsing for Letterboxd scraping
- **[Requests](https://requests.readthedocs.io/)**: HTTP library for primary scraping and API calls
- **[cloudscraper](https://github.com/VeNoMouS/cloudscraper)**: Anti-bot fallback tier 2 (CloudFlare bypass)
- **[curl_cffi](https://github.com/yifeikong/curl_cffi)**: Anti-bot fallback tier 3 (Chrome impersonation via libcurl)
- **camoufox**: Anti-bot fallback tier 4 (headless Firefox, last resort)
- **[Flask-Limiter](https://flask-limiter.readthedocs.io/)**: Per-IP rate limiting
- **[Gunicorn](https://gunicorn.org/)**: Production WSGI server (`gthread` worker class)

### External APIs

- **[TMDB API](https://www.themoviedb.org/documentation/api)**: Movie metadata, similar film discovery, and watch-provider lookups
- **[SimpleJustWatch](https://github.com/Electronic-Mango/simple-justwatch-python-api)**: Primary streaming availability source

### Caching and Performance

- **[Redis](https://redis.io/)**: Optional distributed cache (automatic fallback to in-memory)
- **Custom `_ExpiringDict`**: Thread-safe in-memory TTL store used when Redis is absent
- **ThreadPoolExecutor**: 6 workers for TMDB enrichment, 4 workers for similarity lookups

### Frontend

- **Vanilla JavaScript**: No framework dependencies
- **Server-Sent Events (SSE)**: Three real-time channels — logs, recommendations, status
- **LocalStorage**: Client-side caching

### Deployment

- **[Railway](https://railway.app/)**: Cloud platform hosting
- **[Procfile](https://devcenter.heroku.com/articles/procfile)**: `gunicorn` with `gthread` workers, 180s timeout

---

## Prerequisites

Before installing, ensure you have:

- **Python 3.10 or higher** ([Download](https://www.python.org/downloads/))
- **pip** (Python package manager, included with Python)
- **TMDB API Key** ([Get one free here](https://www.themoviedb.org/settings/api))
- **(Optional) Redis** for distributed caching ([Installation guide](https://redis.io/docs/getting-started/installation/))

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/based-on-what/letterboxd-recommender.git
cd letterboxd-recommender
```

### 2. Create a Virtual Environment (Recommended)

```bash
python -m venv venv

# macOS/Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root:

```bash
# Required
TMDB_KEY=your_tmdb_api_key_here

# Optional - Redis
REDIS_URL=redis://localhost:6379
RATELIMIT_STORAGE_URI=redis://localhost:6379

# Optional - Application Settings
PORT=8080
FLASK_ENV=development
MIN_RECOMMEND_RATING=7.0
LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD=5
LETTERBOXD_CIRCUIT_COOLDOWN_S=180
CACHE_MAX_SIZE=10000
STREAM_MAX_AGE_S=3600

# Optional - Outbound HTTP timeouts (seconds)
LETTERBOXD_HTTP_TIMEOUT=12
TMDB_HTTP_TIMEOUT=12
CAMOUFOX_TIMEOUT=20

# Optional - Internal auth for /_incident-status
INTERNAL_TOKEN=
```

### Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TMDB_KEY` | Yes | — | TMDB API key (v3) or Bearer token (v4); auto-detected |
| `REDIS_URL` | No | — | Redis connection string; falls back to in-memory if not set |
| `RATELIMIT_STORAGE_URI` | No | `REDIS_URL` if set, else `memory://` | Storage backend for Flask-Limiter (memory:// is per-process: limits multiply by worker count) |
| `PORT` | No | `8080` | Port for Flask/Gunicorn server |
| `FLASK_ENV` | No | `production` | Set to `development` for debug output and JSON exports |
| `LOCAL_DEV` | No | — | Alternative dev mode flag (`true` enables the same debug behavior as `FLASK_ENV=development`) |
| `MIN_RECOMMEND_RATING` | No | `7.0` | Minimum TMDB rating threshold for recommendations |
| `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` | No | `5` | Consecutive failures before opening the circuit breaker |
| `LETTERBOXD_CIRCUIT_COOLDOWN_S` | No | `180` | Seconds to skip live scraping while the circuit is open |
| `CACHE_MAX_SIZE` | No | `10000` | Maximum entries in the in-memory cache before LRU eviction |
| `STREAM_MAX_AGE_S` | No | `3600` | Seconds before an inactive SSE stream is evicted from memory |
| `SSE_QUEUE_MAXSIZE` | No | `1000` | Max messages per SSE queue; oldest are dropped on overflow |
| `SCRAPE_POOL_SIZE` | No | `6` | Threads in the shared Letterboxd scraping pool (process-wide) |
| `WORK_POOL_SIZE` | No | `8` | Threads in the shared enrichment/recommendation pool (process-wide) |
| `PIPELINE_POOL_SIZE` | No | `4` | Concurrent async recommendation jobs per process |
| `JOB_RESULT_TTL` | No | `900` | Seconds an async `/api/result` payload stays fetchable |
| `LETTERBOXD_HTTP_TIMEOUT` | No | `12` | Timeout (s) for Letterboxd scraping requests (requests/cloudscraper/curl_cffi) |
| `TMDB_HTTP_TIMEOUT` | No | `12` | Timeout (s) for TMDB API requests |
| `CAMOUFOX_TIMEOUT` | No | `20` | Page-load timeout (s) for the camoufox headless-browser fallback |
| `INTERNAL_TOKEN` | No | — | Bearer token to protect `/_incident-status` (unprotected if unset) |

---

## Usage

### Running Locally

```bash
# Development mode (auto-reload, debug output)
FLASK_ENV=development python main.py

# Production mode
gunicorn -c gunicorn.conf.py main:app
```

### Using the Application

1. Open your browser and navigate to `http://localhost:8080`
2. Enter a Letterboxd username (e.g., `karsten`, `davidehrlich`)
3. Select your country for streaming availability
4. Click **Get Recommendations**
5. View results at `http://localhost:8080/<username>`

---

## API Reference

### Health Check

```http
GET /_health
```

**Response:**
```json
{
  "status": "ok",
  "degraded": false,
  "incident": {
    "letterboxd_total_failures": 0,
    "letterboxd_consecutive_failures": 0,
    "letterboxd_last_status": 200,
    "letterboxd_circuit_open": false,
    "letterboxd_circuit_retry_after_s": 0
  }
}
```

---

### Incident Status

```http
GET /_incident-status
```

Returns the live circuit-breaker snapshot. Rate-limited to 30 requests/minute. Protected by `X-Internal-Token` header when `INTERNAL_TOKEN` is set.

---

### Get Page Count

```http
POST /api/get_pages
```

Rate-limited to 10 requests/minute.

**Request:**
```json
{ "username": "karsten" }
```

**Response:**
```json
{ "pages": 42 }
```

---

### Generate Recommendations

```http
POST /api/recommend
```

Rate-limited to 5 requests/minute.

**Request:**
```json
{
  "username": "karsten",
  "country": "US",
  "include_streaming": true
}
```

**Parameters:**
- `username` (string, required): Letterboxd username — alphanumeric, underscores, hyphens, 1–50 chars
- `country` (string, optional): ISO country code (default: `"CL"`)
- `include_streaming` (boolean, optional): Include streaming availability (default: `true`)
- `request_id` (string, optional): Client-supplied ID for SSE stream correlation (auto-generated if omitted)

**Response:**
```json
{
  "username": "karsten",
  "country_name": "United States",
  "country_code": "US",
  "pages": 42,
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "preferences": {
    "genres": ["Drama", "Thriller", "Science Fiction"],
    "directors": ["Denis Villeneuve", "Christopher Nolan"],
    "decades": ["2010s", "2020s"]
  },
  "recommendations": [
    {
      "tmdb_id": 335984,
      "title": "Blade Runner 2049",
      "original_title": "Blade Runner 2049",
      "year": "2017",
      "director": "Denis Villeneuve",
      "genres": ["Science Fiction", "Drama"],
      "poster": "https://image.tmdb.org/t/p/w500/...",
      "rating_tmdb": 7.9,
      "runtime": 164,
      "streaming": ["Prime Video", "Max"],
      "reason": "Since you liked Arrival"
    }
  ]
}
```

When live scraping is unavailable, the response additionally includes:

```json
{
  "data_freshness": "stale_cache",
  "hint": "Showing last successful profile snapshot because live Letterboxd scraping was blocked or throttled.",
  "incident": { "letterboxd_circuit_open": true, "letterboxd_circuit_retry_after_s": 120 }
}
```

---

### Real-Time SSE Endpoints

All three streams require a `request_id` query parameter matching the value returned by `/api/recommend`. Rate-limited to 20 requests/minute each.

| Endpoint | Description |
|----------|-------------|
| `GET /api/logs-stream?request_id=<id>` | Log messages as the pipeline runs |
| `GET /api/recommendations-stream?request_id=<id>` | Recommendations as they are found; ends with `{"status": "complete"}` |
| `GET /api/status-stream?request_id=<id>` | Pipeline status updates (current seed film); ends with `{"status": "complete"}` |

---

### Static Routes

| Route | Description |
|-------|-------------|
| `GET /` | Landing page with username input |
| `GET /<username>` | Results page for a given user |

---

## Project Structure

```
letterboxd-recommender/
├── main.py                  # App factory: loads env, creates Flask app, registers blueprint
├── app.py                   # WSGI entry point
├── routes.py                # Flask Blueprint — HTTP only, no business logic
├── recommender.py           # Public facade: MovieRecommender orchestrator + backward-compat re-exports
├── cache.py                 # Cache abstraction (Redis + _ExpiringDict fallback), TTL constants
├── sse.py                   # SSE stream management (logs, recommendations, status queues)
├── limiter.py               # Flask-Limiter singleton (deferred init_app)
├── utils.py                 # Shared utilities: normalize_title, IS_DEV, export_debug_json
├── test_main.py             # Unit and integration tests (pytest)
│
├── infra/                   # I/O layer — no business logic
│   ├── http.py              # Sessions, retry config, circuit breaker (IncidentTracker), per-service rate limiters, anti-bot fallbacks
│   ├── letterboxd.py        # Letterboxd scraping client (page count, profile scraping, 4-tier fallback chain)
│   ├── tmdb.py              # TMDB API client (search, details, similar films, watch providers, dual v3/v4 auth)
│   └── streaming.py         # JustWatch + TMDB watch-provider streaming availability client
│
├── services/                # Domain logic — pure functions, no direct I/O
│   ├── enricher.py          # Film enrichment task (TMDB metadata fetch per film)
│   ├── preferences.py       # Preference analysis: top genres, directors, decades
│   └── recommender.py       # Core recommendation generation pipeline
│
├── static/
│   ├── index.html           # Landing page UI
│   └── results.html         # Recommendations display page
│
├── requirements.txt         # Python dependencies
├── runtime.txt              # Python version specification
├── gunicorn.conf.py         # Gunicorn configuration
├── Procfile                 # Railway/Heroku deployment config
└── .env                     # Environment variables (not in repo)
```

### Key Components

- **`MovieRecommender`** (`recommender.py`): Thin orchestrator that composes `LetterboxdClient`, `TmdbClient`, and `StreamingClient`. Exposes the same public surface as the previous monolithic implementation so routes require no changes.

- **`LetterboxdClient`** (`infra/letterboxd.py`): Scrapes a Letterboxd user's film list with a 4-tier anti-bot fallback chain: plain `requests` → `cloudscraper` → `curl_cffi` → `camoufox`. Integrates with `IncidentTracker` for circuit-breaker behavior. Caches fresh profiles for 30 minutes and stale profiles for 7 days.

- **`TmdbClient`** (`infra/tmdb.py`): Handles search, movie details, similar film discovery, and watch-provider lookups. Auto-detects v3 API key vs v4 Bearer token. Caches results for 1 day.

- **`StreamingClient`** (`infra/streaming.py`): Resolves streaming availability via JustWatch (primary) or TMDB watch providers (fallback). Normalizes provider names. Caches hits for 6 hours and failures for 2 hours.

- **`IncidentTracker`** (`infra/http.py`): Circuit breaker that opens after `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` consecutive scraping failures and suppresses live requests for `LETTERBOXD_CIRCUIT_COOLDOWN_S` seconds.

- **`Cache`** (`cache.py`): Namespaced key-value store. Uses Redis when `REDIS_URL` is set; falls back to `_ExpiringDict` (LRU eviction, configurable max size).

- **SSE streams** (`sse.py`): Three per-request queues (logs, recommendations, status) managed by `QueueHandler`. Stale streams are evicted after `STREAM_MAX_AGE_S` of inactivity.

---

## Contributing

Contributions are welcome.

### Pull Requests

1. Fork the repository and create a feature branch: `git checkout -b feature/my-feature`
2. Follow PEP 8 style guidelines
3. Keep business logic in `services/` and I/O in `infra/`; routes should only parse input and format responses
4. Run `pytest test_main.py` before submitting
5. Commit using conventional prefixes: `Add:`, `Fix:`, `Update:`, `Docs:`
6. Open a pull request with a clear description referencing any related issues

### Reporting Issues

Open an issue at [github.com/based-on-what/letterboxd-recommender/issues](https://github.com/based-on-what/letterboxd-recommender/issues) with steps to reproduce, expected vs. actual behavior, and your environment details.

---

## License

MIT License. See source for full text.

---

## Acknowledgments

- **[Letterboxd](https://letterboxd.com/)** — The social network for film lovers that inspired this project
- **[The Movie Database (TMDB)](https://www.themoviedb.org/)** — Comprehensive movie metadata and API
- **[JustWatch](https://www.justwatch.com/)** — Streaming availability data via SimpleJustWatch
- **[Railway](https://railway.app/)** — Hassle-free deployment and hosting
