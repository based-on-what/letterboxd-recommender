from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from collections import Counter
from bs4 import BeautifulSoup
import time
import os
import logging
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from functools import wraps
from dotenv import load_dotenv
load_dotenv()

# Caching options
try:
    import redis
except Exception:
    redis = None

try:
    from cachetools import TTLCache
except Exception:
    TTLCache = None

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("letterboxd-recommender")

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Configurable defaults via env vars
TMDB_KEY = os.getenv("TMDB_KEY")
REDIS_URL = os.getenv("REDIS_URL")  # optional
MAX_SCRAPE_PAGES = int(os.getenv("MAX_SCRAPE_PAGES") or 50)
DEFAULT_MAX_FILMS = int(os.getenv("DEFAULT_MAX_FILMS") or 30)
DEFAULT_LIMIT_RECS = int(os.getenv("DEFAULT_LIMIT_RECS") or 60)

# Simple RateLimiter (per-service)
class RateLimiter:
    def __init__(self, min_interval=0.25):
        self.min_interval = min_interval
        self._lock = Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            diff = now - self._last
            if diff < self.min_interval:
                time.sleep(self.min_interval - diff)
            self._last = time.time()

# Cache wrapper that uses Redis if available else an in-memory TTLCache
class Cache:
    def __init__(self):
        self.redis = None
        if REDIS_URL and redis:
            try:
                self.redis = redis.from_url(REDIS_URL, decode_responses=True)
                # test connection
                self.redis.ping()
                logger.info("Using Redis for cache")
            except Exception as e:
                logger.warning("Redis enabled but connection failed. Falling back to in-memory cache. %s", e)
                self.redis = None

        # Fallback caches (simple TTL caches)
        self.caches = {}
        if not self.redis:
            if TTLCache is None:
                # Minimal fallback (very small)
                self.caches['tmdb'] = {}
                self.caches['similar'] = {}
                self.caches['streaming'] = {}
                self.caches['user_scrape'] = {}
            else:
                self.caches['tmdb'] = TTLCache(maxsize=2000, ttl=60 * 60 * 24)        # 24h
                self.caches['similar'] = TTLCache(maxsize=2000, ttl=60 * 60 * 24)     # 24h
                self.caches['streaming'] = TTLCache(maxsize=2000, ttl=60 * 60 * 6)    # 6h
                self.caches['user_scrape'] = TTLCache(maxsize=500, ttl=60 * 30)       # 30m

    def _redis_get(self, key):
        try:
            val = self.redis.get(key)
            if val is None:
                return None
            return json.loads(val)
        except Exception:
            return None

    def _redis_set(self, key, value, ex=None):
        try:
            self.redis.set(key, json.dumps(value), ex=ex)
        except Exception:
            pass

    def get(self, namespace, key):
        if self.redis:
            return self._redis_get(f"{namespace}:{key}")
        else:
            cache = self.caches.get(namespace)
            try:
                return cache.get(key)
            except Exception:
                return None

    def set(self, namespace, key, value, ttl=None):
        if self.redis:
            self._redis_set(f"{namespace}:{key}", value, ex=ttl)
        else:
            cache = self.caches.get(namespace)
            try:
                if isinstance(cache, dict):
                    cache[key] = value
                else:
                    cache[key] = value
            except Exception:
                pass

cache = Cache()

# HTTP session with retries & backoff
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": "Letterboxd-Recommender/1.0 (+https://example)"})
    return s

session = make_session()
tmdb_limiter = RateLimiter(min_interval=0.25)      # ~4 reqs/sec default
letterboxd_limiter = RateLimiter(min_interval=0.35) # ~3 reqs/sec to be polite
streaming_limiter = RateLimiter(min_interval=0.3)

class MovieRecommender:
    def __init__(self, country='CL', max_workers=8):
        self.letterboxd_base = "https://letterboxd.com"
        self.tmdb_base = "https://api.themoviedb.org/3"
        self.tmdb_key = TMDB_KEY
        self.country = country.upper()
        self.max_workers = max_workers
        self.country_names = {
            'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
            'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru', 'UY': 'Uruguay',
            'IT': 'Italy', 'FR': 'France', 'DE': 'Germany', 'GB': 'United Kingdom'
        }

    def get_country_name(self):
        return self.country_names.get(self.country, self.country)

    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='generic'):
        """Safe GET with session retries and small backoff. Returns Response or None."""
        limiter = {'tmdb': tmdb_limiter, 'letterboxd': letterboxd_limiter, 'streaming': streaming_limiter}.get(service)
        if limiter:
            limiter.wait()

        for attempt in range(max_retries + 1):
            try:
                r = session.get(url, params=params, headers=headers, timeout=12)
                if r.status_code == 200:
                    return r
                elif r.status_code == 429:
                    # rate limited — wait longer
                    sleep_time = (attempt + 1) * 1.5
                    logger.warning("429 from %s. sleeping %s", url, sleep_time)
                    time.sleep(sleep_time)
                else:
                    logger.debug("Non-200 %s for %s", r.status_code, url)
                    # small sleep and retry
                    time.sleep(0.4)
            except requests.RequestException as e:
                logger.debug("Request exception %s for %s", e, url)
                time.sleep(0.4 * (attempt + 1))
        return None

    def get_page_count(self, username):
        """Return number of 'films' pages for username. Returns 0 on error."""
        if not username:
            return 0
        url = f"{self.letterboxd_base}/{username}/films/"
        try:
            r = self._safe_get(url, service='letterboxd')
            if not r:
                return 0
            soup = BeautifulSoup(r.text, 'html.parser')
            pagination = soup.find_all('li', class_='paginate-page')
            if not pagination:
                return 1
            pages = []
            for p in pagination:
                txt = p.get_text(strip=True)
                if txt.isdigit():
                    pages.append(int(txt))
            return max(pages) if pages else 1
        except Exception as e:
            logger.exception("Failed to get page count for %s: %s", username, e)
            return 0

    def get_all_rated_films(self, username, max_pages=None):
        """Scrape all rated films (with rating) and return list of dicts and pages count.
        Uses caching for short TTL to prevent repeated scrapes."""
        if not username:
            return [], 0

        max_pages = max_pages or MAX_SCRAPE_PAGES
        cache_key = f"{username}:pages"
        cached = cache.get('user_scrape', cache_key)
        if cached:
            # cached structure: {pages, films}
            logger.info("Using cached scrape for %s", username)
            return cached.get('films', []), cached.get('pages', 0)

        base_url = f"{self.letterboxd_base}/{username}/films/"
        try:
            pages = self.get_page_count(username)
            if pages <= 0:
                return [], 0
            if pages > max_pages:
                logger.info("Capping pages from %s to %s (env MAX_SCRAPE_PAGES=%s)", pages, max_pages, MAX_SCRAPE_PAGES)
                pages = max_pages

            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = {'User-Agent': session.headers.get('User-Agent')}

            def scrape_page(page):
                url = f"{base_url}page/{page}/" if page > 1 else base_url
                r = self._safe_get(url, headers=headers, service='letterboxd')
                if not r:
                    return []
                soup = BeautifulSoup(r.text, 'html.parser')
                # find poster containers that include viewingdata
                items = soup.find_all('li', class_='poster-container') or soup.find_all('li', class_='griditem')
                page_films = []
                for item in items:
                    try:
                        img = item.find('img', alt=True)
                        name = img.get('alt') if img else None
                        rating = 0.0
                        viewingdata = item.find('p', class_='poster-viewingdata')
                        if viewingdata:
                            r_span = viewingdata.find('span', class_='rating')
                            if r_span:
                                for cls in r_span.get('class', []):
                                    if cls in rating_map:
                                        rating = rating_map[cls]
                                        break
                        if name and rating > 0:
                            page_films.append({'title': name.strip(), 'rating': rating, 'year': None})
                    except Exception:
                        continue
                return page_films

            films = []
            with ThreadPoolExecutor(max_workers=min(self.max_workers, 6)) as ex:
                futures = [ex.submit(scrape_page, p) for p in range(1, pages + 1)]
                for f in as_completed(futures):
                    try:
                        films.extend(f.result())
                    except Exception:
                        logger.exception("Error scraping a page for %s", username)

            # store in cache for short TTL
            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=60 * 30)
            return films, pages
        except Exception as e:
            logger.exception("Failed to scrape films for %s: %s", username, e)
            return [], 0

    def get_tmdb_details(self, title, force_refresh=False):
        """Return detailed TMDb info for a title. Caches results."""
        if not self.tmdb_key:
            logger.error("TMDB_KEY not configured")
            return None

        key = f"tmdb:{title.lower()}"
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached

        # 1) search
        try:
            resp = self._safe_get(f"{self.tmdb_base}/search/movie",
                                  params={'api_key': self.tmdb_key, 'query': title, 'language': 'en-US'}, service='tmdb')
            if not resp:
                return None
            results = resp.json().get('results') or []
            if not results:
                return None

            movie = results[0]
            movie_id = movie.get('id')
            if not movie_id:
                return None

            # 2) fetch details with credits appended (reduces number of API calls)
            resp2 = self._safe_get(f"{self.tmdb_base}/movie/{movie_id}",
                                   params={'api_key': self.tmdb_key, 'language': 'en-US', 'append_to_response': 'credits'},
                                   service='tmdb')
            if not resp2:
                return None
            det = resp2.json()

            cre = det.get('credits', {})
            director = next((c['name'] for c in cre.get('crew', []) if c.get('job') == 'Director'), "Unknown")

            poster_path = det.get('poster_path')
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

            out = {
                'tmdb_id': movie_id,
                'title': det.get('title') or title,
                'year': (det.get('release_date') or '')[:4] or None,
                'director': director,
                'genres': [g['name'] for g in det.get('genres', [])] if det.get('genres') else [],
                'poster': poster_url,
                'rating_tmdb': round(det.get('vote_average', 0), 1) if det.get('vote_average') is not None else None,
                'runtime': det.get('runtime') or 0
            }
            cache.set('tmdb', key, out, ttl=60 * 60 * 24)  # cache 24h
            return out
        except Exception:
            logger.exception("TMDb lookup failed for %s", title)
            return None

    def analyze_preferences(self, enriched_films):
        """Return top genres, directors, decades (fixed logic & safe counts)."""
        genres = []
        directors = []
        decades = []

        for film in enriched_films:
            if film.get('genres'):
                genres.extend(film['genres'])
            if film.get('director') and film['director'] != "Unknown":
                directors.append(film['director'])
            if film.get('year'):
                try:
                    decade = (int(film['year']) // 10) * 10
                    decades.append(decade)
                except Exception:
                    pass

        genre_counts = Counter(genres).most_common(3)
        director_counts = Counter(directors).most_common(3)
        decade_counts = Counter(decades).most_common(3)

        return {
            'genres': [g[0] for g in genre_counts] if genre_counts else ['N/A'],
            'directors': [d[0] for d in director_counts] if director_counts else ['N/A'],
            'decades': [f"{d[0]}s" for d in decade_counts] if decade_counts else []
        }

    def get_recommendations(self, enriched_films, count=DEFAULT_LIMIT_RECS, force_refresh=False):
        """For top user-rated films, fetch similar movies from TMDb and return enriched dets.
        Uses caching for similar endpoints and limits concurrent TMDb calls."""
        seen = {f['title'].lower() for f in enriched_films}
        recs = []
        top_films = sorted(enriched_films, key=lambda x: x.get('user_rating', 0), reverse=True)[:10]

        def process_similar(film):
            # cache key for similar lists
            cache_key = f"similar:{film.get('tmdb_id')}"
            if not force_refresh:
                cached = cache.get('similar', cache_key)
                if cached:
                    return cached

            url = f"{self.tmdb_base}/movie/{film['tmdb_id']}/similar"
            resp = self._safe_get(url, params={'api_key': self.tmdb_key, 'language': 'en-US'}, service='tmdb')
            local = []
            if not resp:
                cache.set('similar', cache_key, [], ttl=60 * 60 * 24)
                return []
            try:
                results = resp.json().get('results', [])[:8]
                for m in results:
                    title = m.get('title')
                    if not title:
                        continue
                    if title.lower() in seen:
                        continue
                    # reuse tmdb detail cache by title
                    det = self.get_tmdb_details(title, force_refresh=force_refresh)
                    if det:
                        det['reason'] = f"Since you liked {film.get('title')}"
                        local.append(det)
                # store
                cache.set('similar', cache_key, local, ttl=60 * 60 * 24)
            except Exception:
                logger.exception("Failed parsing similar for %s", film)
            return local

        with ThreadPoolExecutor(max_workers=min(6, self.max_workers)) as ex:
            futures = [ex.submit(process_similar, f) for f in top_films]
            for f in as_completed(futures):
                try:
                    recs.extend(f.result())
                except Exception:
                    logger.exception("Error while gathering similar movies")

        # deduplicate by tmdb_id and limit
        unique = {}
        for r in recs:
            if r and r.get('tmdb_id') and r['tmdb_id'] not in unique:
                unique[r['tmdb_id']] = r
        out = list(unique.values())[:count]
        return out

    def get_streaming(self, title, year=None, force_refresh=False):
        """Resolve streaming providers using simplejustwatchapi. Caching applied."""
        cache_key = f"{title.lower()}:{year or ''}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            # simplejustwatchapi uses internal calls — wrap with limiter
            streaming_limiter.wait()
            # Import on-demand to avoid hard dependency if not needed in tests
            from simplejustwatchapi import justwatch as sjw
            entries = sjw.search(title, country=self.country, language='en', count=1, best_only=True)
            if entries:
                providers = [o.package.name for o in entries[0].offers if o.package]
                providers = sorted(list(set(p for p in providers if p)))
                cache.set('streaming', cache_key, providers, ttl=60 * 60 * 6)
                return providers
        except Exception:
            logger.debug("Streaming lookup failed for %s", title)
        cache.set('streaming', cache_key, [], ttl=60 * 60 * 2)
        return []

# Flask routes
@app.route('/')
def root():
    return app.send_static_file('index.html')

@app.route('/api/get_pages', methods=['POST'])
def get_pages():
    payload = request.get_json() or {}
    username = payload.get('username')
    if not username:
        return jsonify({'error': 'username is required'}), 400
    try:
        rec = MovieRecommender()
        pages = rec.get_page_count(username)
        return jsonify({'pages': pages})
    except Exception as e:
        logger.exception("get_pages error")
        return jsonify({'error': 'internal error'}), 500

@app.route('/api/recommend', methods=['POST'])
def recommend():
    data = request.get_json() or {}
    username = data.get('username')
    if not username:
        return jsonify({'error': 'username is required'}), 400

    country = data.get('country', 'CL')
    max_films = int(data.get('max_films', DEFAULT_MAX_FILMS))
    limit_recs = int(data.get('limit_recommendations', DEFAULT_LIMIT_RECS))
    include_streaming = bool(data.get('include_streaming', True))
    force_refresh = bool(data.get('force_refresh', False))

    try:
        rec_sys = MovieRecommender(country=country)
        user_films, pages = rec_sys.get_all_rated_films(username, max_pages=MAX_SCRAPE_PAGES)
        if not user_films:
            return jsonify({'error': 'No movies found for this user'}), 404

        # Enrich (parallel, capped)
        enriched = []
        def enrich_task(f):
            d = rec_sys.get_tmdb_details(f['title'], force_refresh=force_refresh)
            if d:
                d['user_rating'] = f.get('rating', 0)
                return d
            return None

        with ThreadPoolExecutor(max_workers=min(8, rec_sys.max_workers)) as ex:
            futures = [ex.submit(enrich_task, f) for f in user_films[:max_films]]
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        enriched.append(res)
                except Exception:
                    logger.exception("Enrich task failure")

        preferences = rec_sys.analyze_preferences(enriched)
        recommendations = rec_sys.get_recommendations(enriched, count=limit_recs, force_refresh=force_refresh)

        # Optionally fetch streaming providers (parallel but rate-limited & cached)
        if include_streaming and recommendations:
            with ThreadPoolExecutor(max_workers=min(6, rec_sys.max_workers)) as ex:
                future_map = {ex.submit(rec_sys.get_streaming, r['title'], r.get('year'), force_refresh): r for r in recommendations}
                for fut in as_completed(future_map):
                    try:
                        providers = fut.result()
                        future_map[fut]['streaming'] = providers or []
                    except Exception:
                        logger.exception("Streaming lookup failed for a recommendation")
                        future_map[fut]['streaming'] = []
        else:
            for r in recommendations:
                r['streaming'] = []

        # Only return minimal fields per rec (to reduce JSON size)
        minimal_recs = []
        for r in recommendations:
            minimal_recs.append({
                'tmdb_id': r.get('tmdb_id'),
                'title': r.get('title'),
                'year': r.get('year'),
                'director': r.get('director'),
                'genres': r.get('genres'),
                'poster': r.get('poster'),
                'rating_tmdb': r.get('rating_tmdb'),
                'runtime': r.get('runtime'),
                'reason': r.get('reason'),
                'streaming': r.get('streaming', [])
            })

        return jsonify({
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'pages': pages,
            'preferences': preferences,
            'recommendations': minimal_recs
        })
    except Exception:
        logger.exception("recommend endpoint failed")
        return jsonify({'error': 'internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # For production use Gunicorn. dev: python main.py
    app.run(host='0.0.0.0', port=port)
