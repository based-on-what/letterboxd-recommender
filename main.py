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
from dotenv import load_dotenv
import unicodedata
import re

load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("letterboxd-recommender")

# Flask configuration
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Configuration variables from the environment
TMDB_KEY = os.getenv("TMDB_KEY")
REDIS_URL = os.getenv("REDIS_URL")
MAX_SCRAPE_PAGES = int(os.getenv("MAX_SCRAPE_PAGES") or 50)
DEFAULT_MAX_FILMS = int(os.getenv("DEFAULT_MAX_FILMS") or 30)
DEFAULT_LIMIT_RECS = int(os.getenv("DEFAULT_LIMIT_RECS") or 60)
MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))

# Cache TTL constants
ONE_MONTH = 60 * 60 * 24 * 30
ONE_WEEK = 60 * 60 * 24 * 7


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


class Cache:
    """
    Cache abstraction with Redis and in-memory fallbacks.

    Provides simple namespaced storage with TTL support. If Redis is
    unavailable, falls back to local TTL caches or plain dictionaries.
    """
    
    def __init__(self):
        self.redis = None
        self._redis_attempted = False
        self.caches = {}
        
        if TTLCache is None:
            self.caches['tmdb'] = {}
            self.caches['similar'] = {}
            self.caches['streaming'] = {}
            self.caches['user_scrape'] = {}
        else:
            self.caches['tmdb'] = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['similar'] = TTLCache(maxsize=5000, ttl=ONE_MONTH)
            self.caches['streaming'] = TTLCache(maxsize=5000, ttl=60 * 60 * 24)
            self.caches['user_scrape'] = TTLCache(maxsize=1000, ttl=ONE_WEEK)
    
    def _init_redis(self):
        """Attempt to initialize Redis exactly once."""
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

# Requests configuration with retries
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session():
    """
    Build an HTTP session with retry support.

    Configures exponential backoff to smooth over transient network
    failures and rate limiting responses.
    """
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504)
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": "Letterboxd-Recommender/1.0"})
    return s

session = make_session()

# Rate limiters per service
tmdb_limiter = RateLimiter(min_interval=0.25)
letterboxd_limiter = RateLimiter(min_interval=0.35)
streaming_limiter = RateLimiter(min_interval=0.3)


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
    title = ''.join([c for c in title if not unicodedata.combining(c)])
    
    # Lowercase text
    title = title.lower()
    
    # Remove non alphanumeric characters except spaces
    title = re.sub(r'[^a-z0-9\s]', ' ', title)
    
    # Collapse repeated whitespace
    return re.sub(r'\s+', ' ', title).strip()


class MovieRecommender:
    """
    Core pipeline that powers Letterboxd-based movie recommendations.

    Combines Letterboxd scraping, TMDB enrichment, preference analysis,
    similar-title discovery, and optional streaming availability lookups.
    """
    
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
        
        self.country_names = {
            'CL': 'Chile', 'AR': 'Argentina', 'MX': 'Mexico', 'US': 'United States',
            'ES': 'Spain', 'BR': 'Brazil', 'CO': 'Colombia', 'PE': 'Peru',
            'UY': 'Uruguay', 'IT': 'Italy', 'FR': 'France', 'DE': 'Germany',
            'GB': 'United Kingdom'
        }
    
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
        limiter_map = {
            'tmdb': tmdb_limiter,
            'letterboxd': letterboxd_limiter,
            'streaming': streaming_limiter
        }
        limiter = limiter_map.get(service)
        
        if limiter:
            limiter.wait()
        
        for attempt in range(max_retries + 1):
            try:
                r = session.get(url, params=params, headers=headers, timeout=12)
                
                if r.status_code == 200:
                    return r
                elif r.status_code == 429:
                    sleep_time = (attempt + 1) * 1.5
                    logger.warning(f"Rate limit alcanzado, esperando {sleep_time}s")
                    time.sleep(sleep_time)
                else:
                    time.sleep(0.4)
                    
            except requests.RequestException as e:
                logger.debug(f"Request error (attempt {attempt + 1}): {e}")
                time.sleep(0.4 * (attempt + 1))
        
        logger.warning(f"Failed after {max_retries + 1} attempts: {url}")
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
            r = self._safe_get(url, service='letterboxd')
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
        
        max_pages = max_pages or MAX_SCRAPE_PAGES
        
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
            
            if pages > max_pages:
                logger.info(f"Limiting scraping to {max_pages} of {pages} pages")
                pages = max_pages
            
            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = {'User-Agent': session.headers.get('User-Agent')}
            
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
            
            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=60 * 30)
            
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
            resp = self._safe_get(
                f"{self.tmdb_base}/search/movie",
                params={'api_key': self.tmdb_key, 'query': title},
                service='tmdb'
            )
            
            if not resp or not resp.json().get('results'):
                return None
            
            movie_id = resp.json()['results'][0].get('id')
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
            resp = self._safe_get(
                f"{self.tmdb_base}/movie/{movie_id}",
                params={
                    'api_key': self.tmdb_key,
                    'append_to_response': 'credits'
                },
                service='tmdb'
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
                'runtime': det.get('runtime') or 0
            }
            
            cache.set('tmdb', key, out, ttl=60 * 60 * 24)
            cache.set('tmdb', f"tmdb:search:{out['title'].lower()}", out, ttl=60 * 60 * 24)
            
            return out
            
        except Exception as e:
            logger.error(f"Error obteniendo detalles de TMDB: {e}")
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
    
    def get_recommendations(self, enriched_films, count=DEFAULT_LIMIT_RECS, force_refresh=False):
        """
        Generate recommendations based on the user's films.

        Updated criteria: every film rated 4+ stars (not just the top 10)
        seeds the similar-movie search.

        Steps:
        1. Filter films rated >= 4.0.
        2. For each, fetch similar titles from TMDB.
        3. Drop duplicates and already-seen titles.
        4. Enforce a minimum TMDB rating threshold.
        5. Return the best-scoring results.

        Args:
            enriched_films: User films enriched with metadata.
            count: Number of recommendations to return.
            force_refresh: Ignore cache when True.

        Returns:
            List of recommendation dictionaries with metadata.
        """
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
        
        # Export JSON locally for development debugging
        if os.getenv('FLASK_ENV') == 'development' or os.getenv('LOCAL_DEV') == 'true':
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
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, indent=2, ensure_ascii=False)
                
                logger.info(f"✓ High-rated films exported to: {filename}")
                
            except Exception as e:
                logger.warning(f"Could not export JSON: {e}")
        
        for idx, film in enumerate(highly_rated_films, 1):
            logger.info(f"  {idx}. {film.get('title')} [{film.get('year')}] - "
                       f"{film.get('user_rating')}★ (TMDB: {film.get('rating_tmdb', 'N/A')})")
        
        logger.info(f"\n=== LOOKING FOR SIMILAR MOVIES ===")
        
        def process_similar(film):
            """Collect similar movies for a given seed title."""
            if not film.get('tmdb_id'):
                return []
            
            logger.info(f"Searching movies similar to: {film.get('title')}")
            
            cache_key = f"similar:{film.get('tmdb_id')}"
            
            if not force_refresh:
                cached = cache.get('similar', cache_key)
                if cached:
                    logger.info(f"  ↳ Retrieved {len(cached)} from cache")
                    return cached
            
            try:
                url = f"{self.tmdb_base}/movie/{film['tmdb_id']}/similar"
                resp = self._safe_get(url, params={'api_key': self.tmdb_key}, service='tmdb')
                
                if not resp:
                    return []
                
                local = []
                results = resp.json().get('results', [])[:12]
                
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
                    
                    det_title_norm = normalize_title(det.get('title', ''))
                    det_orig_norm = normalize_title(det.get('original_title', ''))
                    
                    if (str(det.get('tmdb_id')) in seen_ids or 
                        det_title_norm in seen_titles_norm or 
                        det_orig_norm in seen_titles_norm):
                        logger.debug(f"  ✗ {det.get('title')} - Already seen (secondary check)")
                        continue
                    
                    det['reason'] = f"Since you liked {film.get('title')}"
                    local.append(det)
                    logger.debug(f"  ✓ {det.get('title')} - Valid candidate")
                
                cache.set('similar', cache_key, local, ttl=60 * 60 * 24)
                logger.info(f"  ↳ {len(local)} new movies found")
                
                return local
                
            except Exception as e:
                logger.error(f"Error searching similar movies: {e}")
                return []
        
        # Process all highly-rated films in parallel
        with ThreadPoolExecutor(max_workers=min(6, self.max_workers)) as ex:
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
        
        # Filter by minimum TMDB rating
        filtered = [
            v for v in unique.values()
            if v.get('rating_tmdb') is not None and 
               float(v['rating_tmdb']) >= MIN_RECOMMEND_RATING
        ]
        
        logger.info(f"\n=== FINAL RESULT ===")
        logger.info(f"Initial candidates: {len(recs)}")
        logger.info(f"After deduplication: {len(unique)}")
        logger.info(f"After rating filter (>={MIN_RECOMMEND_RATING}): {len(filtered)}")
        logger.info(f"Returning top {min(count, len(filtered))}")
        
        return filtered[:count]
    
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
            streaming_limiter.wait()
            from simplejustwatchapi import justwatch as sjw
            
            entries = sjw.search(
                title,
                country=self.country,
                language='en',
                count=1,
                best_only=True
            )
            
            if not entries:
                cache.set('streaming', cache_key, [], ttl=60 * 60 * 6)
                return []
            
            providers = sorted(list(set(
                o.package.name for o in entries[0].offers if o.package
            )))
            
            cache.set('streaming', cache_key, providers, ttl=60 * 60 * 6)
            return providers
            
        except Exception as e:
            logger.debug(f"Error fetching streaming for {title}: {e}")
            cache.set('streaming', cache_key, [], ttl=60 * 60 * 2)
            return []


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/_health', methods=['GET'])
def health():
    """Lightweight health check endpoint for monitoring."""
    return jsonify({"status": "ok"}), 200


@app.route("/")
def home():
    """Serve the landing page."""
    return app.send_static_file('index.html')


@app.route('/api/get_pages', methods=['POST'])
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
    data = request.get_json() or {}
    username = data.get('username')
    
    if not username:
        return jsonify({'error': 'username is required'}), 400
    
    try:
        rec_sys = MovieRecommender(country=data.get('country', 'CL'))
        
        logger.info(f"\n{'='*60}")
        logger.info(f"STARTING ANALYSIS FOR: {username}")
        logger.info(f"{'='*60}")
        
        # Fetch user films (including unrated entries)
        user_films, pages = rec_sys.get_all_rated_films(username, include_unrated=True)
        
        if not user_films:
            return jsonify({'error': 'No movies found'}), 404
        
        # Enrich with TMDB data
        enriched = []
        
        def enrich_task(film):
            """Augment a scraped film with TMDB data."""
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
        
        logger.info(f"\nEnriching films with TMDB metadata...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(enrich_task, f) for f in user_films[:DEFAULT_MAX_FILMS]]
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    enriched.append(result)
        
        # Analyze preferences
        preferences = rec_sys.analyze_preferences(enriched)
        logger.info(f"\nPreferences detected:")
        logger.info(f"  Genres: {', '.join(preferences.get('genres', []))}")
        logger.info(f"  Directors: {', '.join(preferences.get('directors', []))}")
        logger.info(f"  Decades: {', '.join(preferences.get('decades', []))}")
        
        # Generate recommendations
        recommendations = rec_sys.get_recommendations(enriched, count=DEFAULT_LIMIT_RECS)
        
        # Retrieve streaming information if requested
        if data.get('include_streaming', True) and recommendations:
            logger.info(f"\nFetching streaming availability...")
            
            with ThreadPoolExecutor(max_workers=6) as ex:
                future_map = {
                    ex.submit(rec_sys.get_streaming, r['title'], r.get('year')): r
                    for r in recommendations
                }
                
                for fut in as_completed(future_map):
                    try:
                        future_map[fut]['streaming'] = fut.result() or []
                    except Exception as e:
                        logger.debug(f"Error fetching streaming data: {e}")
                        future_map[fut]['streaming'] = []
        else:
            for r in recommendations:
                r['streaming'] = []
        
        logger.info(f"\n{'='*60}")
        logger.info(f"ANALYSIS COMPLETE")
        logger.info(f"{'='*60}\n")
        
        return jsonify({
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'pages': pages,
            'preferences': preferences,
            'recommendations': recommendations,
        })
        
    except Exception as e:
        logger.exception("Error generando recomendaciones")
        return jsonify({'error': str(e)}), 500


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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)
