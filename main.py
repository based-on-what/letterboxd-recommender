from flask import Flask, request, jsonify, g
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
from dotenv import load_dotenv
import unicodedata
import re
import queue
from queue import Queue
from uuid import uuid4
from flask import stream_with_context, Response
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:
    Limiter = None

    def get_remote_address():
        return '127.0.0.1'

load_dotenv()

# === CONFIGURATION ===

# Suppress verbose httpx logging
logging.getLogger('httpx').setLevel(logging.WARNING)

# Logging configuration with SSE support
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("letterboxd-recommender")

# === CONSTANTS ===
IS_DEV = os.getenv('FLASK_ENV') == 'development' or os.getenv('LOCAL_DEV') == 'true'
RATE_LIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URI', 'memory://')

ONE_MONTH = 60 * 60 * 24 * 30
ONE_WEEK = 60 * 60 * 24 * 7
ONE_DAY = 60 * 60 * 24
SIX_HOURS = 60 * 60 * 6
TWO_HOURS = 60 * 60 * 2
USER_CACHE_TTL = 60 * 30
MAX_FILMS_TO_ENRICH = 500
MAX_SEED_FILMS = 100
TIMEOUT_WARNING_S = 240
ENRICH_WORKERS = 6
SIMILAR_WORKERS = 4
SIMILAR_RESULTS_PER_FILM = 12
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)
LETTERBOXD_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'DNT': '1',
}

PROVIDER_NAME_MAP = {
    'Amazon Prime Video': 'Prime Video',
    'Prime Video': 'Prime Video',
    'Disney+': 'Disney Plus',
    'Disney Plus': 'Disney Plus',
    'Apple TV+': 'Apple TV+',
    'HBO Max': 'HBO Max',
    'Max': 'Max',
}

REQUEST_STREAMS = {}
REQUEST_STREAMS_LOCK = Lock()


def _get_or_create_streams(request_id):
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if streams is None:
            streams = {
                'logs': Queue(),
                'recommendations': Queue(),
                'logs_connected': 0,
                'recommendations_connected': 0,
                'recommendations_done': False,
            }
            REQUEST_STREAMS[request_id] = streams
        return streams


def _cleanup_request_streams(request_id):
    with REQUEST_STREAMS_LOCK:
        REQUEST_STREAMS.pop(request_id, None)




def _mark_recommendations_done(request_id):
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if streams:
            streams['recommendations_done'] = True


def _track_stream_connection(request_id, stream_name, connected):
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if not streams:
            return

        counter_key = f"{stream_name}_connected"
        current = streams.get(counter_key, 0)
        streams[counter_key] = max(0, current + (1 if connected else -1))

        if (
            streams.get('recommendations_done')
            and streams.get('logs_connected', 0) == 0
            and streams.get('recommendations_connected', 0) == 0
        ):
            REQUEST_STREAMS.pop(request_id, None)


def _export_debug_json(filename, data):
    if not IS_DEV:
        return
    try:
        with open(filename, 'w', encoding='utf-8') as debug_file:
            json.dump(data, debug_file, ensure_ascii=False, indent=2)
        logger.info(f"Debug JSON exported to: {filename}")
    except Exception as exc:
        logger.warning(f"Could not export debug JSON {filename}: {exc}")

class QueueHandler(logging.Handler):
    """Custom handler to send logs to the queue for SSE streaming."""
    def emit(self, record):
        try:
            msg = self.format(record)
            request_id = getattr(record, 'request_id', None)
            if not request_id:
                try:
                    request_id = getattr(g, 'request_id', None)
                except RuntimeError:
                    request_id = None
            if request_id:
                _get_or_create_streams(request_id)['logs'].put(msg)
        except Exception:
            self.handleError(record)

# Add the queue handler to the logger
queue_handler = QueueHandler()
logger.addHandler(queue_handler)

# Flask configuration
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)
if Limiter is not None:
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=RATE_LIMIT_STORAGE_URI,
    )
else:
    logger.warning('flask-limiter unavailable; endpoint rate limiting disabled')

    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def deco(func):
                return func
            return deco

    limiter = _NoopLimiter()

# Configuration variables from the environment
TMDB_KEY = os.getenv("TMDB_KEY")
REDIS_URL = os.getenv("REDIS_URL")
MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))

class RateLimiter:
    """
    Simple rate controller used to avoid overwhelming external APIs.

    Enforces a minimum interval between requests so shared services respect
    vendor throttling limits even when multiple threads issue calls.
    """
    
    def __init__(self, min_interval=0.25):
        """Initialize the limiter.

        Args:
            min_interval: Minimum number of seconds to wait between requests.
        """
        self.min_interval = min_interval
        self._lock = Lock()
        self._last = 0.0
    
    def wait(self):
        """Block until enough time has elapsed since the previous request."""
        with self._lock:
            now = time.time()
            diff = now - self._last
            if diff < self.min_interval:
                sleep_time = self.min_interval - diff
                time.sleep(sleep_time)
            self._last = time.time()


# Conditional import of cache dependencies
try:
    import redis
except ImportError:
    redis = None
    logger.warning("Redis unavailable; falling back to in-memory cache")

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None
    logger.warning("cachetools unavailable; using simple dict cache")

try:
    from simplejustwatchapi import justwatch as sjw
except ImportError:
    sjw = None
    logger.warning("simplejustwatchapi unavailable; streaming fallback disabled")

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


# === CACHE ===

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
        self.caches = {}
        
        if TTLCache is None:
            self.caches['tmdb'] = {}
            self.caches['similar'] = {}
            self.caches['streaming'] = {}
            self.caches['user_scrape'] = {}
        else:
            self.caches['tmdb'] = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['similar'] = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['streaming'] = TTLCache(maxsize=5000, ttl=ONE_DAY)
            self.caches['user_scrape'] = TTLCache(maxsize=1000, ttl=ONE_WEEK)
    
    def _init_redis(self):
        """Attempt to initialize Redis exactly once."""
        with self._init_lock:
            if self._redis_attempted:
                return

            self._redis_attempted = True
            if not REDIS_URL or not redis:
                return

            try:
                self.redis = redis.from_url(REDIS_URL, decode_responses=True)
                self.redis.ping()
                logger.info("Redis connected successfully")
            except Exception as e:
                logger.warning(f"Could not connect to Redis: {e}")
                self.redis = None
    
    def _redis_get(self, key):
        """Fetch a JSON value from Redis."""
        try:
            val = self.redis.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None
    
    def _redis_set(self, key, value, ex=None):
        """Store a JSON value in Redis."""
        try:
            self.redis.set(key, json.dumps(value), ex=ex)
        except Exception:
            pass
    
    def get(self, namespace, key):
        """Return a value from cache, preferring Redis over memory.

        Args:
            namespace: Cache bucket name (tmdb, similar, streaming, user_scrape).
            key: Item key inside the namespace.
        """
        if not self._redis_attempted:
            self._init_redis()
        
        if self.redis:
            return self._redis_get(f"{namespace}:{key}")
        
        cache = self.caches.get(namespace)
        return cache.get(key) if cache else None
    
    def set(self, namespace, key, value, ttl=None):
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


cache = Cache()

# === HTTP / RATE LIMITING ===

# Requests configuration with retries
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session(trust_env=False):
    """
    Build an HTTP session with retry support.

    Configures exponential backoff to smooth over transient network
    failures and rate limiting responses.
    """
    s = requests.Session()
    s.trust_env = trust_env
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504)
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    return s

session = make_session(trust_env=False)
proxy_session = make_session(trust_env=True)
cloudscraper_session = cloudscraper.create_scraper() if cloudscraper else None

def _request_with_fallback(http_session, url, params, headers, timeout, service):
    """Send GET request with optional proxy-session fallback for Letterboxd."""
    try:
        return http_session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        if service == 'letterboxd':
            logger.debug(f"Direct Letterboxd request failed, trying proxy-enabled session: {exc}")
            return proxy_session.get(url, params=params, headers=headers, timeout=timeout)
        raise


def _curl_letterboxd_get(url, params, headers, timeout):
    """Optional high-fidelity browser impersonation fallback for Letterboxd."""
    if curl_requests is None:
        return None
    try:
        return curl_requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            impersonate='chrome120',
        )
    except Exception as exc:
        logger.debug(f"curl_cffi fallback error: {exc}")
        return None


# Rate limiters per service
tmdb_limiter = RateLimiter(min_interval=0.1)
letterboxd_limiter = RateLimiter(min_interval=0.15)
streaming_limiter = RateLimiter(min_interval=0.1)


def normalize_title(title):
    """
    Normalize a title string for resilient comparisons.

    Removes diacritics, lowercases, strips special characters, and
    collapses whitespace to produce a stable token for matching.

    Args:
        title: Title to normalize.

    Returns:
        Lowercased, accent-free, alphanumeric title token.

    Example:
        >>> normalize_title("El Señor de los Anillos")
        'el senor de los anillos'
    """
    if not title:
        return ""
    
    # Decompose Unicode characters and strip diacritics
    title = unicodedata.normalize('NFKD', title)
    title = ''.join(c for c in title if not unicodedata.combining(c))
    
    # Lowercase text
    title = title.lower()
    
    # Remove non alphanumeric characters except spaces
    title = re.sub(r'[^a-z0-9\s]', ' ', title)
    
    # Collapse repeated whitespace
    return re.sub(r'\s+', ' ', title).strip()


# === SCRAPER / TMDB / STREAMING / RECOMMENDER ===

class MovieRecommender:
    """
    Core pipeline that powers Letterboxd-based movie recommendations.

    Combines Letterboxd scraping, TMDB enrichment, preference analysis,
    similar-title discovery, and optional streaming availability lookups.
    """
    
    _LIMITER_MAP = {
        'tmdb': tmdb_limiter,
        'letterboxd': letterboxd_limiter,
        'streaming': streaming_limiter
    }

    def __init__(self, country='CL', max_workers=8):
        """Configure the recommender.

        Args:
            country: ISO country code used for streaming availability.
            max_workers: Thread pool size for concurrent tasks.
        """
        self.letterboxd_base = "https://letterboxd.com"
        self.tmdb_base = "https://api.themoviedb.org/3"
        self.tmdb_key = TMDB_KEY
        self.country = country.upper()
        self.max_workers = max_workers
        self._tmdb_auth_warning_emitted = False
        self._letterboxd_last_failures = []
        
        self.country_names = {
            'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
            'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru',
            'UY': 'Uruguay', 'IT': 'Italy', 'FR': 'France', 'DE': 'Germany',
            'GB': 'United Kingdom'
        }
    

    def _tmdb_auth_parts(self):
        """Return params/headers tuple for TMDB auth supporting v3 and v4 keys."""
        token = (self.tmdb_key or '').strip()
        if token.startswith('eyJ'):
            return {}, {'Authorization': f'Bearer {token}'}
        return {'api_key': token}, {}

    def _tmdb_get(self, path, params=None):
        """Issue a TMDB GET request with automatic auth and 401 diagnostics."""
        auth_params, auth_headers = self._tmdb_auth_parts()
        req_params = dict(params or {})
        req_params.update(auth_params)
        req_headers = dict(auth_headers)

        resp = self._safe_get(
            f"{self.tmdb_base}{path}",
            params=req_params,
            headers=req_headers or None,
            service='tmdb'
        )

        if not resp and not self._tmdb_auth_warning_emitted:
            self._tmdb_auth_warning_emitted = True
            logger.warning(
                'TMDB request failed. Verify TMDB_KEY format: v3 API key uses api_key param; v4 token uses Bearer auth.'
            )
        return resp

    def get_country_name(self):
        """Return the human-friendly name for the configured country."""
        return self.country_names.get(self.country, self.country)
    
    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='generic'):
        """
        Issue an HTTP GET with rate limiting and retries.

        Args:
            url: Target URL.
            params: Optional query parameters.
            headers: Optional request headers.
            max_retries: Number of retry attempts after the first try.
            service: Service key used to pick a rate limiter.

        Returns:
            Response object on success, otherwise None.
        """
        limiter = self._LIMITER_MAP.get(service)
        
        if limiter:
            limiter.wait()
        
        if service == 'letterboxd':
            self._letterboxd_last_failures = []

        for attempt in range(max_retries + 1):
            try:
                merged_headers = dict(session.headers)
                if headers:
                    merged_headers.update(headers)

                r = _request_with_fallback(session, url, params, merged_headers, 12, service)

                if r.status_code == 200:
                    return r

                if service == 'letterboxd' and r.status_code in (403, 429, 503):
                    self._letterboxd_last_failures.append(f"attempt={attempt + 1},status={r.status_code},source=requests")
                    if cloudscraper_session is not None:
                        try:
                            alt = cloudscraper_session.get(url, params=params, headers=merged_headers, timeout=12)
                            if alt.status_code == 200:
                                logger.info("Letterboxd request succeeded via cloudscraper fallback")
                                return alt
                            self._letterboxd_last_failures.append(
                                f"attempt={attempt + 1},status={alt.status_code},source=cloudscraper"
                            )
                            logger.warning(
                                f"Cloudscraper non-200 response ({alt.status_code}) from {url} on attempt {attempt + 1}"
                            )
                        except requests.RequestException as exc:
                            self._letterboxd_last_failures.append(
                                f"attempt={attempt + 1},error={type(exc).__name__},source=cloudscraper"
                            )
                            logger.debug(f"Cloudscraper request error (attempt {attempt + 1}): {exc}")

                    curl_resp = _curl_letterboxd_get(url, params, merged_headers, 12)
                    if curl_resp is not None:
                        if getattr(curl_resp, 'status_code', None) == 200:
                            logger.info("Letterboxd request succeeded via curl_cffi fallback")
                            return curl_resp
                        self._letterboxd_last_failures.append(
                            f"attempt={attempt + 1},status={getattr(curl_resp, 'status_code', 'n/a')},source=curl_cffi"
                        )

                if r.status_code == 429:
                    sleep_time = (attempt + 1) * 1.5
                    logger.warning(f"Rate limit reached, waiting {sleep_time}s")
                    time.sleep(sleep_time)
                else:
                    logger.warning(
                        f"Non-200 response ({r.status_code}) from {url} on attempt {attempt + 1}"
                    )
                    time.sleep(0.4)
                    
            except requests.RequestException as e:
                if service == 'letterboxd':
                    self._letterboxd_last_failures.append(
                        f"attempt={attempt + 1},error={type(e).__name__},source=requests"
                    )
                logger.warning(f"Request error for {url} (attempt {attempt + 1}): {type(e).__name__}: {e}")
                time.sleep(0.4 * (attempt + 1))
        
        logger.warning(f"Failed after {max_retries + 1} attempts: {url}")
        if service == 'letterboxd':
            logger.warning(
                "Letterboxd scraping failed after retries. "
                f"cloudscraper_available={cloudscraper_session is not None}, "
                f"proxy_env_http={bool(os.getenv('HTTP_PROXY') or os.getenv('http_proxy'))}, "
                f"proxy_env_https={bool(os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'))}, "
                f"last_failures={self._letterboxd_last_failures}"
            )
        return None
    
    def get_page_count(self, username):
        """
        Return the total number of film pages for a Letterboxd user.

        Args:
            username: Letterboxd username.

        Returns:
            Page count or 0 if the profile cannot be parsed.
        """
        url = f"{self.letterboxd_base}/{username}/films/"
        
        try:
            r = self._safe_get(url, headers=LETTERBOXD_HEADERS, service='letterboxd')
            if not r:
                return 0
            
            soup = BeautifulSoup(r.text, 'html.parser')
            pagination = soup.find_all('li', class_='paginate-page')
            
            if not pagination:
                return 1
            
            pages = [
                int(p.get_text(strip=True))
                for p in pagination
                if p.get_text(strip=True).isdigit()
            ]
            
            return max(pages) if pages else 1
            
        except Exception as e:
            logger.error(f"Error retrieving page count: {e}")
            return 0
    
    def get_all_rated_films(self, username, max_pages=None, include_unrated=True):
        """
        Scrape every film listed in a user's profile.

        Improvement: unrated films are also captured so they are treated as
        already seen and not proposed as recommendations.

        Args:
            username: Letterboxd username.
            max_pages: Maximum number of pages to scrape.
            include_unrated: Whether to keep films without user ratings.

        Returns:
            Tuple of (film list, scraped page count).

        Note:
            Unrated films are returned with rating=0 but still marked as seen.
        """
        if not username:
            return [], 0
        
        cache_key = f"{username}:pages:v2"
        cached = cache.get('user_scrape', cache_key)
        if cached:
            logger.info(f"Profile for {username} loaded from cache")
            return cached.get('films', []), cached.get('pages', 0)
        
        base_url = f"{self.letterboxd_base}/{username}/films/"
        
        try:
            pages = self.get_page_count(username)
            if pages <= 0:
                return [], 0
            
            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = dict(LETTERBOXD_HEADERS)
            
            def scrape_page(page):
                """Scrape a single profile page."""
                url = f"{base_url}page/{page}/" if page > 1 else base_url
                r = self._safe_get(url, headers=headers, service='letterboxd')
                
                if not r:
                    return []
                
                soup = BeautifulSoup(r.text, 'html.parser')
                items = soup.find_all('li', class_='poster-container') or \
                        soup.find_all('li', class_='griditem')
                
                page_films = []
                
                for item in items:
                    try:
                        img = item.find('img', alt=True)
                        if not img:
                            continue
                        
                        name = img.get('alt')
                        rating = 0.0
                        
                        viewingdata = item.find('p', class_='poster-viewingdata')
                        if viewingdata:
                            r_span = viewingdata.find('span', class_='rating')
                            if r_span:
                                for cls in r_span.get('class', []):
                                    if cls in rating_map:
                                        rating = rating_map[cls]
                                        break
                        
                        if name:
                            film_data = {
                                'title': name.strip(),
                                'rating': rating,
                                'has_rating': rating > 0
                            }
                            page_films.append(film_data)
                            
                    except Exception as e:
                        logger.debug(f"Error processing film: {e}")
                        continue
                
                return page_films
            
            films = []
            logger.info(f"Scraping {pages} pages from {username}'s profile")
            
            with ThreadPoolExecutor(max_workers=min(self.max_workers, 6)) as ex:
                futures = [ex.submit(scrape_page, p) for p in range(1, pages + 1)]
                for f in as_completed(futures):
                    films.extend(f.result())
            
            if not include_unrated:
                films = [f for f in films if f.get('has_rating')]
            
            logger.info(f"Total films found: {len(films)} "
                        f"({len([f for f in films if f.get('has_rating')])} rated)")
            
            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=USER_CACHE_TTL)
            
            return films, pages
            
        except Exception as e:
            logger.error(f"Error scraping profile: {e}")
            return [], 0
    
    def get_tmdb_details(self, title, force_refresh=False):
        """
        Look up movie metadata on TMDB by title.

        Args:
            title: Movie title.
            force_refresh: Ignore cached data when True.

        Returns:
            Metadata dictionary or None when no match is found.
        """
        if not self.tmdb_key:
            logger.warning("TMDB_KEY not configured")
            return None
        
        key = f"tmdb:search:{title.lower()}"
        
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached
        
        try:
            resp = self._tmdb_get('/search/movie', params={'query': title})
            
            if not resp:
                return None
            body = resp.json()
            results = body.get('results')
            if not results:
                return None
            movie_id = results[0].get('id')
            return self.get_tmdb_details_by_id(movie_id, force_refresh)
            
        except Exception as e:
            logger.debug(f"Error searching TMDB: {e}")
            return None
    
    def get_tmdb_details_by_id(self, movie_id, force_refresh=False):
        """
        Retrieve full TMDB metadata for a movie ID.

        Args:
            movie_id: TMDB identifier.
            force_refresh: Ignore cached data when True.

        Returns:
            Dictionary including tmdb_id, titles, year, director, genres,
            poster URL, TMDB rating, and runtime.
        """
        if not self.tmdb_key or not movie_id:
            return None
        
        key = f"id:{movie_id}"
        
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached:
                return cached
        
        try:
            resp = self._tmdb_get(
                f'/movie/{movie_id}',
                params={'append_to_response': 'credits,external_ids'}
            )
            
            if not resp:
                return None
            
            det = resp.json()
            cre = det.get('credits', {})
            
            director = next(
                (c['name'] for c in cre.get('crew', []) if c.get('job') == 'Director'),
                "Unknown"
            )
            
            out = {
                'tmdb_id': movie_id,
                'title': det.get('title'),
                'original_title': det.get('original_title'),
                'year': (det.get('release_date') or '')[:4],
                'director': director,
                'genres': [g['name'] for g in det.get('genres', [])] if det.get('genres') else [],
                'poster': f"https://image.tmdb.org/t/p/w500{det.get('poster_path')}" if det.get('poster_path') else None,
                'rating_tmdb': round(det.get('vote_average', 0), 1) if det.get('vote_average') else None,
                'runtime': det.get('runtime') or 0,
                'imdb_id': (det.get('external_ids') or {}).get('imdb_id')
            }
            
            cache.set('tmdb', key, out, ttl=ONE_DAY)
            cache.set('tmdb', f"tmdb:search:{out['title'].lower()}", out, ttl=ONE_DAY)
            
            return out
            
        except Exception as e:
            logger.error(f"Error getting TMDB details: {e}")
            return None
    
    def analyze_preferences(self, enriched_films):
        """
        Analyze user preferences from enriched films.

        Builds frequency counts to surface favorite genres, recurring
        directors, and most watched decades.

        Args:
            enriched_films: List of movies annotated with metadata.

        Returns:
            Dict with top three genres, directors, and decades.
        """
        genres, directors, decades = [], [], []
        
        for film in enriched_films:
            if film.get('genres'):
                genres.extend(film['genres'])
            
            if film.get('director') and film['director'] != "Unknown":
                directors.append(film['director'])
            
            if film.get('year'):
                try:
                    decade = (int(film['year']) // 10) * 10
                    decades.append(decade)
                except (ValueError, TypeError):
                    pass
        
        return {
            'genres': [g[0] for g in Counter(genres).most_common(3)],
            'directors': [d[0] for d in Counter(directors).most_common(3)],
            'decades': [f"{d[0]}s" for d in Counter(decades).most_common(3)]
        }
    
    def get_recommendations(self, enriched_films, count=None, force_refresh=False, request_id=None):
        """
        Generate recommendations based on the user's films.

        Updated criteria: every film rated 4+ stars (not just the top 10)
        seeds the similar-movie search.

        Steps:
        1. Filter films rated >= 4.0.
        2. For each, fetch similar titles from TMDB.
        3. Drop duplicates and already-seen titles.
        4. Enforce a minimum TMDB rating threshold.
        5. Return all matching results.

        Args:
            enriched_films: User films enriched with metadata.
            count: Optional number of recommendations to limit (None = no limit).
            force_refresh: Ignore cache when True.

        Returns:
            List of recommendation dictionaries with metadata.
        """
        stream_queues = _get_or_create_streams(request_id) if request_id else None

        # Build sets of already-seen films
        seen_ids = set()
        seen_titles_norm = set()
        
        logger.info("=== USER FILMS ===")
        for film in enriched_films:
            if film.get('tmdb_id'):
                seen_ids.add(str(film['tmdb_id']))
            
            title_norm = normalize_title(film.get('title', ''))
            if title_norm:
                seen_titles_norm.add(title_norm)
            
            if film.get('original_title'):
                orig_norm = normalize_title(film.get('original_title'))
                if orig_norm:
                    seen_titles_norm.add(orig_norm)
            
            rating_display = f"{film.get('user_rating', 0):.1f}★" if film.get('user_rating', 0) > 0 else "Unrated"
            logger.info(f"  • {film.get('title', 'Untitled')} [{film.get('year', '????')}] "
                        f"- TMDB ID: {film.get('tmdb_id', 'N/A')} - Rating: {rating_display}")
        
        logger.info(f"Total watched films: {len(enriched_films)} "
                   f"(Unique IDs: {len(seen_ids)}, Normalized titles: {len(seen_titles_norm)})")
        
        recs = []
        
        # Primary change: use every film rated 4+ stars
        highly_rated_films = [
            film for film in enriched_films 
            if film.get('user_rating', 0) >= 4.0
        ]
        
        highly_rated_films = sorted(
            highly_rated_films,
            key=lambda x: x.get('user_rating', 0),
            reverse=True
        )
        
        logger.info(f"\n=== 4+ STAR FILMS USED FOR RECOMMENDATIONS ===")
        logger.info(f"Total films with 4+ stars: {len(highly_rated_films)}")
        
        # Limit the number of films processed to avoid timeouts
        max_films_to_process = MAX_SEED_FILMS
        if len(highly_rated_films) > max_films_to_process:
            logger.info(f"Limiting to top {max_films_to_process} highest-rated films to prevent timeout")
            highly_rated_films = highly_rated_films[:max_films_to_process]
        
        # Export JSON locally for development debugging
        if IS_DEV:
            try:
                export_data = {
                    'username': 'user',
                    'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'total_highly_rated': len(highly_rated_films),
                    'films': [
                        {
                            'title': f.get('title'),
                            'year': f.get('year'),
                            'tmdb_id': f.get('tmdb_id'),
                            'user_rating': f.get('user_rating'),
                            'director': f.get('director'),
                            'genres': f.get('genres', []),
                            'rating_tmdb': f.get('rating_tmdb')
                        }
                        for f in highly_rated_films
                    ]
                }
                
                filename = f"highly_rated_films_{time.strftime('%Y%m%d_%H%M%S')}.json"
                _export_debug_json(filename, export_data)
            except Exception as e:
                logger.warning(f"Could not build debug export payload: {e}")
        
        for idx, film in enumerate(highly_rated_films, 1):
            logger.info(f"  {idx}. {film.get('title')} [{film.get('year')}] - "
                       f"{film.get('user_rating')}★ (TMDB: {film.get('rating_tmdb', 'N/A')})")
        
        logger.info(f"\nStarting parallel processing of {len(highly_rated_films)} films...")
        
        logger.info(f"\n=== LOOKING FOR SIMILAR MOVIES ===")
        
        def process_similar(film):
            """Collect similar movies for a given seed title."""
            if not film.get('tmdb_id'):
                logger.info(f"[SKIP] {film.get('title')} - No TMDB ID")
                return []
            
            logger.info(f"[PROCESSING] {film.get('title')} (TMDB ID: {film.get('tmdb_id')})")
            
            cache_key = f"similar:{film.get('tmdb_id')}"
            
            if not force_refresh:
                cached = cache.get('similar', cache_key)
                if cached:
                    logger.info(f"  [CACHED] {film.get('title')} - Found {len(cached)} results in cache")
                    return cached
            
            try:
                resp = self._tmdb_get(f"/movie/{film['tmdb_id']}/similar")
                
                if not resp:
                    return []
                
                local = []
                results = resp.json().get('results', [])[:SIMILAR_RESULTS_PER_FILM]
                
                for m in results:
                    mid = m.get('id')
                    title = m.get('title')
                    
                    if not title:
                        continue
                    
                    title_norm = normalize_title(title)
                    
                    if str(mid) in seen_ids:
                        logger.debug(f"  ✗ {title} - Already seen (ID match)")
                        continue
                    
                    if title_norm in seen_titles_norm:
                        logger.debug(f"  ✗ {title} - Already seen (title match)")
                        continue
                    
                    det = self.get_tmdb_details_by_id(mid, force_refresh)
                    
                    if not det:
                        continue
                    
                    # Filter immediately by TMDB rating
                    if det.get('rating_tmdb') is None or float(det.get('rating_tmdb', 0)) < MIN_RECOMMEND_RATING:
                        logger.debug(f"  ✗ {det.get('title')} - Rating too low ({det.get('rating_tmdb', 'N/A')} < {MIN_RECOMMEND_RATING})")
                        continue
                    
                    det_title_norm = normalize_title(det.get('title', ''))
                    det_orig_norm = normalize_title(det.get('original_title', ''))
                    
                    if (str(det.get('tmdb_id')) in seen_ids or 
                        det_title_norm in seen_titles_norm or 
                        det_orig_norm in seen_titles_norm):
                        logger.debug(f"  ✗ {det.get('title')} - Already seen (secondary check)")
                        continue
                    
                    det['reason'] = f"Since you liked {film.get('title')}"
                    
                    # Fetch streaming info (prefer TMDB providers by ID)
                    streaming = []
                    try:
                        if det.get('tmdb_id'):
                            streaming = self.get_streaming_by_tmdb(det.get('tmdb_id'))
                        if not streaming:
                            streaming = self.get_streaming(det.get('title'), det.get('year'))
                    except Exception as e:
                        logger.debug(f"Could not fetch streaming for {det.get('title')}: {e}")
                    
                    # Add streaming to the cached object
                    det['streaming'] = streaming
                    
                    # Add candidate with streaming information
                    local.append(det)
                    logger.debug(f"  ✓ {det.get('title')} - Valid candidate (rating: {det.get('rating_tmdb')}, streaming: {len(streaming)} providers)")
                    
                    # Stream recommendation in real time with streaming data
                    try:
                        if stream_queues is not None:
                            stream_queues['recommendations'].put({
                            'title': det.get('title'),
                            'year': det.get('year'),
                            'rating_tmdb': det.get('rating_tmdb'),
                            'poster': det.get('poster'),
                            'director': det.get('director'),
                            'genres': det.get('genres', []),
                            'runtime': det.get('runtime', 0),
                            'streaming': streaming,
                            'reason': det['reason']
                        })
                    except Exception as exc:
                        logger.debug(f"Unable to stream recommendation event: {exc}")
                
                cache.set('similar', cache_key, local, ttl=ONE_DAY)
                logger.info(f"  ↳ {len(local)} new movies found")
                
                return local
                
            except Exception as e:
                logger.error(f"Error searching similar movies: {e}")
                return []
        
        # Process all highly-rated films in parallel
        # Reduce workers to avoid rate limiting and timeouts
        with ThreadPoolExecutor(max_workers=min(SIMILAR_WORKERS, self.max_workers)) as ex:
            futures = [ex.submit(process_similar, f) for f in highly_rated_films]
            for f in as_completed(futures):
                recs.extend(f.result())
        
        # Deduplicate recommendations
        unique = {}
        for r in recs:
            key = str(r['tmdb_id'])
            r_title_norm = normalize_title(r['title'])
            
            if (key not in unique and 
                key not in seen_ids and 
                r_title_norm not in seen_titles_norm):
                unique[key] = r
        
        # Movies are already filtered by rating in process_similar
        filtered = list(unique.values())
        
        logger.info(f"\n=== FINAL RESULT ===")
        logger.info(f"Initial candidates (already filtered by rating >={MIN_RECOMMEND_RATING}): {len(recs)}")
        logger.info(f"After deduplication: {len(filtered)}")
        logger.info(f"Returning {len(filtered)} recommendations")
        
        return filtered if count is None else filtered[:count]
    
    def get_streaming(self, title, year=None, force_refresh=False):
        """
        Fetch streaming providers where a movie is available.

        Args:
            title: Movie title.
            year: Optional release year.
            force_refresh: Ignore cache when True.

        Returns:
            List of provider names.
        """
        cache_key = f"{title.lower()}:{year or ''}"
        
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached
        
        try:
            if sjw is None:
                return []
            streaming_limiter.wait()

            entries = sjw.search(
                title,
                country=self.country,
                language='en',
                count=1,
                best_only=True
            )
            
            if not entries:
                cache.set('streaming', cache_key, [], ttl=SIX_HOURS)
                return []
            
            # Include all monetization types (flatrate, ads, free, rent, buy)
            providers_raw = []
            for o in entries[0].offers:
                try:
                    if o.package and getattr(o.package, 'name', None):
                        providers_raw.append(o.package.name)
                except Exception:
                    continue

            # Normalize common provider naming
            providers = sorted(list({PROVIDER_NAME_MAP.get(p, p) for p in providers_raw}))
            
            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers
            
        except Exception as e:
            logger.debug(f"Error fetching streaming for {title}: {e}")
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []

    def get_streaming_by_tmdb(self, tmdb_id, force_refresh=False):
        """
        Fetch streaming providers using TMDB watch/providers endpoint for exact movie ID.

        Aggregates all monetization types (flatrate, ads, free, rent, buy) for the configured country.
        """
        if not self.tmdb_key or not tmdb_id:
            return []

        cache_key = f"tmdb:{tmdb_id}:{self.country}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None:
                return cached

        try:
            resp = self._tmdb_get(f"/movie/{tmdb_id}/watch/providers")
            if not resp:
                cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
                return []

            data = resp.json() or {}
            country_data = (data.get('results') or {}).get(self.country)
            if not country_data:
                cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
                return []

            names = []
            for k in ('flatrate', 'ads', 'free', 'rent', 'buy'):
                for p in country_data.get(k, []) or []:
                    n = p.get('provider_name') or p.get('providerName')
                    if n:
                        names.append(n)

            # Normalize and dedupe
            providers = sorted({PROVIDER_NAME_MAP.get(n, n) for n in names})

            cache.set('streaming', cache_key, providers, ttl=SIX_HOURS)
            return providers
        except Exception as e:
            logger.debug(f"Error fetching TMDB providers for {tmdb_id}: {e}")
            cache.set('streaming', cache_key, [], ttl=TWO_HOURS)
            return []



def enrich_film_task(rec_sys, film):
    """Augment a scraped film with TMDB data."""
    try:
        tmdb_data = rec_sys.get_tmdb_details(film['title'])
        if tmdb_data:
            tmdb_data['user_rating'] = film.get('rating', 0)
            return tmdb_data
        return {
            'tmdb_id': None,
            'title': film['title'],
            'user_rating': film.get('rating', 0),
            'genres': [],
            'director': None
        }
    except Exception as exc:
        logger.debug(f"Error enriching {film.get('title', 'Unknown')}: {exc}")
        return None


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/_health', methods=['GET'])
def health():
    """Lightweight health check endpoint for monitoring."""
    return jsonify({"status": "ok"}), 200


@app.route('/api/logs-stream', methods=['GET'])
def logs_stream():
    """
    SSE endpoint that streams logs in real-time to the browser.

    Requires request_id query param.
    """
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    log_queue = _get_or_create_streams(request_id)['logs']

    def generate():
        _track_stream_connection(request_id, 'logs', True)
        try:
            while True:
                try:
                    log_msg = log_queue.get(timeout=1)
                    yield f"data: {log_msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            _track_stream_connection(request_id, 'logs', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/recommendations-stream', methods=['GET'])
def recommendations_stream():
    """SSE endpoint that streams recommendations in real-time to the browser."""
    request_id = request.args.get('request_id')
    if not request_id:
        return jsonify({'error': 'request_id is required'}), 400

    recommendations_queue = _get_or_create_streams(request_id)['recommendations']

    def generate():
        _track_stream_connection(request_id, 'recommendations', True)
        try:
            while True:
                try:
                    rec = recommendations_queue.get(timeout=2)
                    if rec == 'DONE':
                        yield "data: {\"status\": \"complete\"}\n\n"
                        break
                    yield f"data: {json.dumps(rec)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except Exception as exc:
            logger.debug(f"Recommendations stream ended for {request_id}: {exc}")
        finally:
            _track_stream_connection(request_id, 'recommendations', False)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route("/")
def home():
    """Serve the landing page."""
    return app.send_static_file('index.html')


@app.route('/api/get_pages', methods=['POST'])
@limiter.limit('10 per minute')
def get_pages():
    """
    Endpoint that returns how many film pages a profile has.

    Request JSON: {"username": str}
    Response JSON: {"pages": int}
    """
    payload = request.get_json() or {}
    
    try:
        recommender = MovieRecommender()
        page_count = recommender.get_page_count(payload.get('username'))
        return jsonify({'pages': page_count})
        
    except Exception as e:
        logger.exception("Error in get_pages")
        return jsonify({'error': 'internal error'}), 500


@app.route('/api/recommend', methods=['POST'])
@limiter.limit('5 per minute')
def recommend():
    """
    Main endpoint that orchestrates recommendation generation.

    Request JSON:
        - username: str (required)
        - country: str (optional, default: CL)
        - include_streaming: bool (optional, default: True)

    Response JSON:
        - username, country_name, pages
        - preferences: {genres, directors, decades}
        - recommendations: [{title, year, rating, streaming, ...}]
    """
    start_time = time.time()
    data = request.get_json() or {}
    username = data.get('username')
    request_id = data.get('request_id') or str(uuid4())
    g.request_id = request_id
    _get_or_create_streams(request_id)
    
    if not username:
        return jsonify({'error': 'username is required'}), 400
    
    try:
        rec_sys = MovieRecommender(country=data.get('country', 'CL'))
        
        logger.info(f"\n{'='*60}")
        logger.info(f"STARTING ANALYSIS FOR: {username}")
        logger.info(f"{'='*60}")
        
        # Fetch user films (including unrated entries)
        user_films, pages = rec_sys.get_all_rated_films(username, include_unrated=True)
        logger.info(f"Fetched {len(user_films)} films in {time.time() - start_time:.2f}s")
        
        if not user_films:
            return jsonify({
                'error': 'No movies found. Profile may be private/unavailable or Letterboxd blocked the request.',
                'username': username,
                'request_id': request_id,
                'hint': 'Check backend logs for Letterboxd HTTP status / proxy diagnostics.',
                'letterboxd_failures': rec_sys._letterboxd_last_failures[-8:],
            }), 404
        
        # Limit processing to avoid production timeouts
        if len(user_films) > MAX_FILMS_TO_ENRICH:
            logger.info(f"Limiting processing: {len(user_films)} films -> {MAX_FILMS_TO_ENRICH} (keeping highest rated)")
            # Keep the highest-rated films
            user_films_sorted = sorted(user_films, key=lambda x: x.get('rating', 0), reverse=True)
            user_films = user_films_sorted[:MAX_FILMS_TO_ENRICH]
        
        # Enrich with TMDB data
        enriched = []
        
        logger.info(f"\nEnriching {len(user_films)} films with TMDB metadata...")
        enrich_start = time.time()
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            futures = [ex.submit(enrich_film_task, rec_sys, film) for film in user_films]
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                    if result:
                        enriched.append(result)
                except Exception as exc:
                    logger.error(f"Enrichment task failed: {exc}")
        logger.info(f"Enrichment completed in {time.time() - enrich_start:.2f}s")
        
        # Analyze preferences
        pref_start = time.time()
        preferences = rec_sys.analyze_preferences(enriched)
        logger.info(f"\nPreferences detected in {time.time() - pref_start:.2f}s:")
        logger.info(f"  Genres: {', '.join(preferences.get('genres', []))}")
        logger.info(f"  Directors: {', '.join(preferences.get('directors', []))}")
        logger.info(f"  Decades: {', '.join(preferences.get('decades', []))}")
        
        # Check elapsed time and adjust processing
        elapsed = time.time() - start_time
        logger.info(f"\nTime elapsed so far: {elapsed:.2f}s")
        
        if elapsed > TIMEOUT_WARNING_S:
            logger.warning("Approaching timeout limit, limiting recommendation processing")
        
        # Generate recommendations
        rec_start = time.time()
        recommendations = rec_sys.get_recommendations(enriched, request_id=request_id)
        logger.info(f"Recommendations generated in {time.time() - rec_start:.2f}s")
        
        # Retrieve streaming information if requested
        if data.get('include_streaming', True) and recommendations:
            # Streaming info is already fetched during recommendation processing
            # Ensure every recommendation has this field using TMDB ID when available
            for r in recommendations:
                if not r.get('streaming'):
                    try:
                        if r.get('tmdb_id'):
                            r['streaming'] = rec_sys.get_streaming_by_tmdb(r.get('tmdb_id')) or []
                        if not r.get('streaming'):
                            r['streaming'] = rec_sys.get_streaming(r.get('title'), r.get('year')) or []
                    except Exception as e:
                        logger.debug(f"Error fetching streaming data: {e}")
                        r['streaming'] = []
        else:
            for r in recommendations:
                r['streaming'] = []
        
        logger.info(f"\n{'='*60}")
        logger.info(f"ANALYSIS COMPLETE")
        logger.info(f"Total processing time: {time.time() - start_time:.2f}s")
        logger.info(f"{'='*60}\n")
        
        # Send completion signal for recommendation stream
        try:
            _mark_recommendations_done(request_id)
            _get_or_create_streams(request_id)['recommendations'].put('DONE')
        except Exception:
            pass
        
        # Export JSON files locally when in development mode
        if IS_DEV:
            try:
                # Export user movies
                movies_data = {
                    'username': username,
                    'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'total_movies': len(enriched),
                    'movies': [
                        {
                            'title': f.get('title'),
                            'original_title': f.get('original_title'),
                            'year': f.get('year'),
                            'tmdb_id': f.get('tmdb_id'),
                            'user_rating': f.get('user_rating'),
                            'director': f.get('director'),
                            'genres': f.get('genres', []),
                            'rating_tmdb': f.get('rating_tmdb'),
                            'runtime': f.get('runtime')
                        }
                        for f in enriched
                    ]
                }
                
                movies_filename = f"{username}_movies.json"
                _export_debug_json(movies_filename, movies_data)
                
                # Build set of already-seen movie IDs for deduplication check
                seen_ids = {str(m.get('tmdb_id')) for m in enriched if m.get('tmdb_id')}
                seen_titles_norm = {normalize_title(m.get('title', '')) for m in enriched}
                
                # Verify no recommendations are duplicates
                filtered_recs = []
                for rec in recommendations:
                    rec_id = str(rec.get('tmdb_id'))
                    rec_title_norm = normalize_title(rec.get('title', ''))
                    
                    if rec_id not in seen_ids and rec_title_norm not in seen_titles_norm:
                        filtered_recs.append(rec)
                
                # Export recommendations
                recs_data = {
                    'username': username,
                    'export_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'total_recommendations': len(filtered_recs),
                    'recommendations': [
                        {
                            'title': r.get('title'),
                            'original_title': r.get('original_title'),
                            'year': r.get('year'),
                            'tmdb_id': r.get('tmdb_id'),
                            'rating_tmdb': r.get('rating_tmdb'),
                            'director': r.get('director'),
                            'genres': r.get('genres', []),
                            'runtime': r.get('runtime'),
                            'streaming': r.get('streaming', [])
                        }
                        for r in filtered_recs
                    ]
                }
                
                recs_filename = f"{username}_recs.json"
                _export_debug_json(recs_filename, recs_data)
                logger.info(f"  (Verified: no duplicates with user's movies)")
                
            except Exception as e:
                logger.warning(f"Could not export JSON files: {e}")
        
        return jsonify({
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'pages': pages,
            'preferences': preferences,
            'recommendations': recommendations,
            'request_id': request_id,
        })
        
    except Exception as e:
        logger.exception("Error generating recommendations")
        elapsed = time.time() - start_time if 'start_time' in locals() else 0
        return jsonify({
            'error': str(e),
            'elapsed_time': elapsed,
            'message': 'An error occurred while generating recommendations'
        }), 500


@app.route('/<username>')
def user_view(username):
    """Serve the results page for a specific user."""
    return app.send_static_file('results.html')


# ============================================================================
# INITIALIZATION
# ============================================================================

import atexit

@atexit.register
def on_exit():
    """Cleanup hook executed on interpreter exit."""
    logger.info("Shutting down worker and freeing resources...")


# === ENTRY POINT ===

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)
