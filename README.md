# Letterboxd Recommender

Letterboxd Recommender scrapes a public Letterboxd profile, enriches the films with TMDB metadata, and builds personalized movie suggestions. Recommendations are seeded from every film the user rated 4+ stars, filtered to avoid titles they have already seen, and optionally annotated with streaming availability for the visitor’s country.

- Live site: **https://letterboxd-recommender-production.up.railway.app/**
- Tech stack: Flask, BeautifulSoup, TMDB API, simple-justwatch, Redis or in-memory TTL cache, vanilla JS frontend.

## How it works
1. The app scrapes a user’s film pages on Letterboxd (up to a configurable limit).
2. Each film is enriched with TMDB data (year, genres, director, runtime, poster, rating).
3. User preferences are inferred from frequent genres/directors/decades.
4. For every film rated 4+ stars, the service pulls TMDB “similar” titles, removes anything already watched, and applies a minimum TMDB rating threshold.
5. Optional streaming lookups show where each recommendation can be watched in the selected country.

## Running locally
1. **Python**: Install Python 3.10+.
2. **Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Environment variables** (see below). At minimum, set `TMDB_KEY`.
4. **Start the server**:
   ```bash
   # Development
   FLASK_ENV=development python main.py

   # Production-style
   gunicorn -c gunicorn.conf.py main:app
   ```
5. Open `http://localhost:8080` (or your configured port). Enter a Letterboxd username to generate recommendations, then open the personalized `/username` link to view results.

## Environment variables
- `TMDB_KEY` **(required)**: TMDB API key.
- `REDIS_URL` (optional): Redis connection string for shared caching; falls back to in-memory cache when missing.
- `MAX_SCRAPE_PAGES` (default 50): Cap on Letterboxd pages scraped per user.
- `DEFAULT_MAX_FILMS` (default 30): Maximum films enriched for preference analysis.
- `DEFAULT_LIMIT_RECS` (default 60): Maximum recommendations returned.
- `MIN_RECOMMEND_RATING` (default 7.0): Minimum TMDB rating to keep a recommendation.
- `PORT` (default 8080): Port for Flask/Gunicorn.

## API
- `GET /_health` — basic health probe.
- `POST /api/get_pages` — body: `{"username": "letterboxd_user"}` → returns page count.
- `POST /api/recommend` — body:
  ```json
  {
    "username": "letterboxd_user",
    "country": "CL",
    "include_streaming": true
  }
  ```
  Returns username, country name, scraped page count, inferred preferences, and a list of recommendations (with TMDB metadata and optional streaming providers).

Static routes:
- `/` serves the landing form.
- `/<username>` shows the recommendation grid, consuming cached data from localStorage when available.

## Notes
- Redis is optional; the app automatically falls back to a process-local TTL cache.
- The frontend is framework-free and supports exporting recommendations to Excel and filtering by streaming platform.
