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

- **Personalized Recommendations**: Analyzes your highly-rated films (4+ stars) to suggest similar movies
- **Streaming Availability**: Shows where each recommendation is available to stream in your country
- **Smart Preference Analysis**: Identifies your favorite genres, directors, and decades
- **Real-time Updates**: Server-Sent Events (SSE) for live progress updates and streaming recommendations
- **Intelligent Caching**: Redis support with automatic fallback to in-memory cache
- **Concurrent Processing**: Multi-threaded enrichment for fast recommendation generation
- **Export Functionality**: Download recommendations to Excel for offline reference
- **Clean UI**: Framework-free vanilla JavaScript frontend with dark mode
- **Deduplication**: Ensures you never see movies you have already watched
- **Multi-Country Support**: Streaming availability for 13+ countries
- **Anti-bot Resilience**: Layered fallback chain (cloudscraper, curl_cffi) for reliable scraping
- **Rate Limiting**: Per-IP endpoint throttling via Flask-Limiter

---

## How It Works

The recommendation engine follows a multi-step pipeline:

1. **Profile Scraping**: Fetches all films from a user's Letterboxd profile (including unrated entries to track what you have already seen)
2. **TMDB Enrichment**: Augments each film with metadata from The Movie Database (year, genres, director, runtime, poster, rating)
3. **Preference Analysis**: Identifies patterns in your viewing history — favorite genres, directors, and decades
4. **Similar Film Discovery**: For every 4+ star film, queries TMDB for similar titles
5. **Intelligent Filtering**: Removes already-watched films, applies minimum rating threshold (default: 7.0), and deduplicates
6. **Streaming Lookup**: Checks availability across multiple platforms for your selected country
7. **Real-time Delivery**: Streams recommendations to the frontend as they are discovered via three SSE channels (logs, recommendations, status)

---

## Tech Stack

### Backend
- **[Python 3.10+](https://www.python.org/)**: Core application language
- **[Flask 3.1](https://flask.palletsprojects.com/)**: Lightweight web framework with Blueprint routing
- **[BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)**: HTML parsing for Letterboxd scraping
- **[Requests](https://requests.readthedocs.io/)**: HTTP library with retry logic and session management
- **[cloudscraper](https://github.com/VeNoMouS/cloudscraper)** + **[curl_cffi](https://github.com/yifeikong/curl_cffi)**: Anti-bot fallback chain for scraping resilience
- **[Flask-Limiter](https://flask-limiter.readthedocs.io/)**: Per-IP rate limiting
- **[Gunicorn](https://gunicorn.org/)**: Production WSGI server

### External APIs
- **[TMDB API](https://www.themoviedb.org/documentation/api)**: Movie metadata and similar film recommendations
- **[SimpleJustWatch](https://github.com/Electronic-Mango/simple-justwatch-python-api)**: Streaming availability lookup

### Caching and Performance

- **[Redis](https://redis.io/)**: Optional distributed cache (with automatic fallback)
- **Custom `_ExpiringDict`**: Thread-safe in-memory TTL store used when Redis is absent
- **ThreadPoolExecutor**: Concurrent processing for API calls (6 enrichment workers)

### Frontend
- **Vanilla JavaScript**: No framework dependencies
- **Server-Sent Events (SSE)**: Three real-time channels — logs, recommendations, status
- **LocalStorage**: Client-side caching

### Deployment
- **[Railway](https://railway.app/)**: Cloud platform hosting
- **[Procfile](https://devcenter.heroku.com/articles/procfile)**: Process configuration

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
MAX_SCRAPE_PAGES=50
DEFAULT_MAX_FILMS=30
DEFAULT_LIMIT_RECS=60
LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD=5
LETTERBOXD_CIRCUIT_COOLDOWN_S=180

# Optional - Internal auth for /_incident-status
INTERNAL_TOKEN=
```

### Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TMDB_KEY` | Yes | - | TMDB API key for movie data |
| `REDIS_URL` | No | - | Redis connection string (falls back to in-memory if not set) |
| `RATELIMIT_STORAGE_URI` | No | `memory://` | Storage backend for Flask-Limiter |
| `PORT` | No | `8080` | Port for Flask/Gunicorn server |
| `MIN_RECOMMEND_RATING` | No | `7.0` | Minimum TMDB rating threshold for recommendations |
| `MAX_SCRAPE_PAGES` | No | `50` | Maximum Letterboxd pages to scrape per user |
| `DEFAULT_MAX_FILMS` | No | `30` | Maximum films to enrich for preference analysis |
| `DEFAULT_LIMIT_RECS` | No | `60` | Maximum recommendations to return |
| `FLASK_ENV` | No | `production` | Set to `development` for debug mode |
| `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` | No | `5` | Consecutive failures before opening the circuit breaker |
| `LETTERBOXD_CIRCUIT_COOLDOWN_S` | No | `180` | Seconds to skip live scraping while the circuit is open |
| `INTERNAL_TOKEN` | No | - | Bearer token to protect `/_incident-status` |

---

## Usage

### Running Locally

```bash
# Development mode (auto-reload)
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
6. Export to Excel using the download button

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

Returns the live circuit-breaker snapshot. Protected by `X-Internal-Token` header when `INTERNAL_TOKEN` is set.

---

### Get Page Count

```http
POST /api/get_pages
```

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

**Request:**
```json
{
  "username": "karsten",
  "country": "US",
  "include_streaming": true
}
```

**Parameters:**
- `username` (string, required): Letterboxd username
- `country` (string, optional): ISO country code (default: `"CL"`)
- `include_streaming` (boolean, optional): Include streaming availability (default: `true`)

**Response:**
```json
{
  "username": "karsten",
  "country_name": "United States",
  "country_code": "US",
  "pages": 42,
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

---

### Real-Time SSE Endpoints

All three streams require a `request_id` query parameter matching the value returned by `/api/recommend`.

| Endpoint                                          | Description                                                              |
| ------------------------------------------------- | ----------------------------------------------------------------------- |
| `GET /api/logs-stream?request_id=<id>`            | Log messages as the pipeline runs                                       |
| `GET /api/recommendations-stream?request_id=<id>` | Recommendations as they are found; ends with `{"status": "complete"}`  |
| `GET /api/status-stream?request_id=<id>`          | Pipeline status updates; ends with `{"status": "complete"}`             |

---

### Static Routes

| Route              | Description                         |
| ------------------ | ----------------------------------- |
| `GET /`            | Landing page with username input    |
| `GET /<username>`  | Results page for a given user       |

---

## Project Structure

```
letterboxd-recommender/
├── main.py                  # App factory: loads env, creates Flask app, registers blueprint
├── app.py                   # WSGI entry point
├── routes.py                # Flask Blueprint — HTTP only, no business logic
├── recommender.py           # Public facade: MovieRecommender orchestrator + backward-compat re-exports
├── cache.py                 # Cache abstraction (Redis + _ExpiringDict fallback), RateLimiter, TTL constants
├── sse.py                   # SSE stream management (logs, recommendations, status queues)
├── limiter.py               # Flask-Limiter singleton (deferred init_app)
├── utils.py                 # Shared utilities: normalize_title, IS_DEV, export_debug_json
│
├── infra/                   # I/O layer — no business logic
│   ├── http.py              # Sessions, retry config, circuit breaker, rate limiters, anti-bot fallbacks
│   ├── letterboxd.py        # Letterboxd scraping client (page count, profile scraping)
│   ├── tmdb.py              # TMDB API client (search, details, similar films, watch providers)
│   └── streaming.py         # JustWatch streaming availability client
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

- **`MovieRecommender`** (`recommender.py`): Thin orchestrator that composes `LetterboxdClient`, `TmdbClient`, and `StreamingClient`. Exposes the same public surface as the previous monolithic implementation so routes and tests require no changes.

- **`LetterboxdClient`** (`infra/letterboxd.py`): Scrapes a Letterboxd user's film list with an anti-bot fallback chain: plain `requests` → `cloudscraper` → `curl_cffi`.

- **`TmdbClient`** (`infra/tmdb.py`): Authenticates with TMDB and handles search, movie details, similar films, and watch-provider lookups.

- **`StreamingClient`** (`infra/streaming.py`): Resolves streaming availability via JustWatch (title search) or TMDB watch providers (by TMDB ID).

- **`Cache`** (`cache.py`): Namespaced key-value store. Uses Redis when `REDIS_URL` is set; falls back to an in-memory `_ExpiringDict` per namespace.

- **SSE streams** (`sse.py`): Three per-request queues (logs, recommendations, status) managed by `QueueHandler` and evicted after one hour of inactivity.

- **`IncidentTracker`** (`infra/http.py`): Circuit breaker that opens after `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` consecutive scraping failures and suppresses live requests for `LETTERBOXD_CIRCUIT_COOLDOWN_S` seconds.

---

## Contributing

Contributions are welcome.

### Pull Requests

1. Fork the repository and create a feature branch: `git checkout -b feature/my-feature`
2. Follow PEP 8 style guidelines
3. Keep business logic in `services/` and I/O in `infra/`; routes should only parse input and format responses
4. Commit using conventional prefixes: `Add:`, `Fix:`, `Update:`, `Docs:`
5. Open a pull request with a clear description referencing any related issues

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
