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
import unicodedata
import re

load_dotenv()

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("letterboxd-recommender")

# Configurar static_folder='.' permite servir index.html y results.html desde la raíz
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Configurable defaults via env vars
TMDB_KEY = os.getenv("TMDB_KEY")
REDIS_URL = os.getenv("REDIS_URL")  # optional
MAX_SCRAPE_PAGES = int(os.getenv("MAX_SCRAPE_PAGES") or 50)
DEFAULT_MAX_FILMS = int(os.getenv("DEFAULT_MAX_FILMS") or 30)
DEFAULT_LIMIT_RECS = int(os.getenv("DEFAULT_LIMIT_RECS") or 60)
MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))

# --- (MANTENER CLASES RateLimiter, Cache, MovieRecommender Y FUNCIONES AUXILIARES IGUAL QUE ANTES) ---
# He omitido las clases internas para ahorrar espacio en la respuesta, 
# pero DEBES mantener todo el código de lógica (RateLimiter, Cache, MovieRecommender) 
# exactamente igual a tu archivo original hasta llegar a las rutas de Flask.

# ... [INSERTA AQUÍ LAS CLASES RateLimiter, Cache, MovieRecommender y funciones auxiliares] ...

# Para que el código funcione, copiaré las clases críticas de tu archivo original aquí abajo
# Si ya las tienes, solo asegúrate de no borrarlas.
# ---------------------------------------------------
# (Aquí iría todo el bloque de lógica que ya tienes en tu main.py original)
# ---------------------------------------------------

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

# Caching options
try:
    import redis
except Exception:
    redis = None
try:
    from cachetools import TTLCache
except Exception:
    TTLCache = None

# 1 mes (60s * 60m * 24h * 30d)
ONE_MONTH = 60 * 60 * 24 * 30 
# 1 semana
ONE_WEEK = 60 * 60 * 24 * 7
class Cache:
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
            self.caches['streaming'] = TTLCache(maxsize=5000, ttl=60 * 60 * 24) # 1 día está bien para streaming
            self.caches['user_scrape'] = TTLCache(maxsize=1000, ttl=ONE_WEEK)   # Guardar perfil 1 semana
    def _init_redis(self):
        if self._redis_attempted: return
        self._redis_attempted = True
        if not REDIS_URL or not redis: return
        try:
            self.redis = redis.from_url(REDIS_URL, decode_responses=True)
            self.redis.ping()
            logger.info("Using Redis for cache")
        except Exception as e:
            logger.warning("Redis fallback: %s", e)
            self.redis = None
    def _redis_get(self, key):
        try:
            val = self.redis.get(key)
            return json.loads(val) if val else None
        except Exception: return None
    def _redis_set(self, key, value, ex=None):
        try: self.redis.set(key, json.dumps(value), ex=ex)
        except Exception: pass
    def get(self, namespace, key):
        if not self._redis_attempted: self._init_redis()
        if self.redis: return self._redis_get(f"{namespace}:{key}")
        cache = self.caches.get(namespace)
        return cache.get(key) if cache else None
    def set(self, namespace, key, value, ttl=None):
        if not self._redis_attempted: self._init_redis()
        if self.redis: self._redis_set(f"{namespace}:{key}", value, ex=ttl)
        else:
            if self.caches.get(namespace) is not None: self.caches[namespace][key] = value

cache = Cache()

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
def make_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": "Letterboxd-Recommender/1.0"})
    return s
session = make_session()

tmdb_limiter = RateLimiter(min_interval=0.25)
letterboxd_limiter = RateLimiter(min_interval=0.35)
streaming_limiter = RateLimiter(min_interval=0.3)

def normalize_title(t):
    if not t: return ""
    t = unicodedata.normalize('NFKD', t)
    t = ''.join([c for c in t if not unicodedata.combining(c)])
    t = t.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()

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
        limiter = {'tmdb': tmdb_limiter, 'letterboxd': letterboxd_limiter, 'streaming': streaming_limiter}.get(service)
        if limiter: limiter.wait()
        for attempt in range(max_retries + 1):
            try:
                r = session.get(url, params=params, headers=headers, timeout=12)
                if r.status_code == 200: return r
                elif r.status_code == 429: time.sleep((attempt + 1) * 1.5)
                else: time.sleep(0.4)
            except requests.RequestException: time.sleep(0.4 * (attempt + 1))
        return None
    def get_page_count(self, username):
        url = f"{self.letterboxd_base}/{username}/films/"
        try:
            r = self._safe_get(url, service='letterboxd')
            if not r: return 0
            soup = BeautifulSoup(r.text, 'html.parser')
            pagination = soup.find_all('li', class_='paginate-page')
            if not pagination: return 1
            pages = [int(p.get_text(strip=True)) for p in pagination if p.get_text(strip=True).isdigit()]
            return max(pages) if pages else 1
        except: return 0
    def get_all_rated_films(self, username, max_pages=None):
        if not username: return [], 0
        max_pages = max_pages or MAX_SCRAPE_PAGES
        cache_key = f"{username}:pages"
        cached = cache.get('user_scrape', cache_key)
        if cached: return cached.get('films', []), cached.get('pages', 0)
        base_url = f"{self.letterboxd_base}/{username}/films/"
        try:
            pages = self.get_page_count(username)
            if pages <= 0: return [], 0
            if pages > max_pages: pages = max_pages
            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = {'User-Agent': session.headers.get('User-Agent')}
            def scrape_page(page):
                url = f"{base_url}page/{page}/" if page > 1 else base_url
                r = self._safe_get(url, headers=headers, service='letterboxd')
                if not r: return []
                soup = BeautifulSoup(r.text, 'html.parser')
                items = soup.find_all('li', class_='poster-container') or soup.find_all('li', class_='griditem')
                page_films = []
                for item in items:
                    try:
                        img = item.find('img', alt=True)
                        name = img.get('alt')
                        rating = 0.0
                        viewingdata = item.find('p', class_='poster-viewingdata')
                        if viewingdata:
                            r_span = viewingdata.find('span', class_='rating')
                            if r_span:
                                for cls in r_span.get('class', []):
                                    if cls in rating_map:
                                        rating = rating_map[cls]; break
                        if name and rating > 0: page_films.append({'title': name.strip(), 'rating': rating})
                    except: continue
                return page_films
            films = []
            with ThreadPoolExecutor(max_workers=min(self.max_workers, 6)) as ex:
                futures = [ex.submit(scrape_page, p) for p in range(1, pages + 1)]
                for f in as_completed(futures): films.extend(f.result())
            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=60 * 30)
            return films, pages
        except: return [], 0
    def get_tmdb_details(self, title, force_refresh=False):
        if not self.tmdb_key: return None
        key = f"tmdb:search:{title.lower()}"
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached: return cached
        try:
            resp = self._safe_get(f"{self.tmdb_base}/search/movie", params={'api_key': self.tmdb_key, 'query': title}, service='tmdb')
            if not resp or not resp.json().get('results'): return None
            movie_id = resp.json()['results'][0].get('id')
            return self.get_tmdb_details_by_id(movie_id, force_refresh)
        except: return None
    def get_tmdb_details_by_id(self, movie_id, force_refresh=False):
        if not self.tmdb_key or not movie_id: return None
        key = f"id:{movie_id}"
        if not force_refresh:
            cached = cache.get('tmdb', key)
            if cached: return cached
        try:
            resp = self._safe_get(f"{self.tmdb_base}/movie/{movie_id}", params={'api_key': self.tmdb_key, 'append_to_response': 'credits'}, service='tmdb')
            if not resp: return None
            det = resp.json()
            cre = det.get('credits', {})
            director = next((c['name'] for c in cre.get('crew', []) if c.get('job') == 'Director'), "Unknown")
            out = {
                'tmdb_id': movie_id, 'title': det.get('title'), 'original_title': det.get('original_title'),
                'year': (det.get('release_date') or '')[:4], 'director': director,
                'genres': [g['name'] for g in det.get('genres', [])] if det.get('genres') else [],
                'poster': f"https://image.tmdb.org/t/p/w500{det.get('poster_path')}" if det.get('poster_path') else None,
                'rating_tmdb': round(det.get('vote_average', 0), 1) if det.get('vote_average') else None,
                'runtime': det.get('runtime') or 0
            }
            cache.set('tmdb', key, out, ttl=60 * 60 * 24)
            cache.set('tmdb', f"tmdb:search:{out['title'].lower()}", out, ttl=60 * 60 * 24)
            return out
        except: return None
    def analyze_preferences(self, enriched_films):
        genres, directors, decades = [], [], []
        for film in enriched_films:
            if film.get('genres'): genres.extend(film['genres'])
            if film.get('director') and film['director'] != "Unknown": directors.append(film['director'])
            if film.get('year'):
                try: decades.append((int(film['year']) // 10) * 10)
                except: pass
        return {
            'genres': [g[0] for g in Counter(genres).most_common(3)],
            'directors': [d[0] for d in Counter(directors).most_common(3)],
            'decades': [f"{d[0]}s" for d in Counter(decades).most_common(3)]
        }
    def get_recommendations(self, enriched_films, count=DEFAULT_LIMIT_RECS, force_refresh=False):
        seen_ids = set()
        seen_titles_norm = set()
        for f in enriched_films:
            if f.get('tmdb_id'): seen_ids.add(str(f['tmdb_id']))
            seen_titles_norm.add(normalize_title(f.get('title')))
            if f.get('original_title'): seen_titles_norm.add(normalize_title(f.get('original_title')))
        recs = []
        top_films = sorted(enriched_films, key=lambda x: x.get('user_rating', 0), reverse=True)[:10]
        def process_similar(film):
            if not film.get('tmdb_id'): return []
            cache_key = f"similar:{film.get('tmdb_id')}"
            if not force_refresh:
                cached = cache.get('similar', cache_key)
                if cached: return cached
            try:
                url = f"{self.tmdb_base}/movie/{film['tmdb_id']}/similar"
                resp = self._safe_get(url, params={'api_key': self.tmdb_key}, service='tmdb')
                if not resp: return []
                local = []
                for m in resp.json().get('results', [])[:12]:
                    mid, title = m.get('id'), m.get('title')
                    if not title or str(mid) in seen_ids or normalize_title(title) in seen_titles_norm: continue
                    det = self.get_tmdb_details_by_id(mid, force_refresh)
                    if det:
                        if str(det.get('tmdb_id')) in seen_ids or normalize_title(det.get('title')) in seen_titles_norm: continue
                        det['reason'] = f"Since you liked {film.get('title')}"
                        local.append(det)
                cache.set('similar', cache_key, local, ttl=60 * 60 * 24)
                return local
            except: return []
        with ThreadPoolExecutor(max_workers=min(6, self.max_workers)) as ex:
            futures = [ex.submit(process_similar, f) for f in top_films]
            for f in as_completed(futures): recs.extend(f.result())
        unique = {}
        for r in recs:
            key = str(r['tmdb_id'])
            if key not in unique and key not in seen_ids and normalize_title(r['title']) not in seen_titles_norm:
                unique[key] = r
        filtered = [v for v in unique.values() if v.get('rating_tmdb') is not None and float(v['rating_tmdb']) >= MIN_RECOMMEND_RATING]
        return filtered[:count]
    def get_streaming(self, title, year=None, force_refresh=False):
        cache_key = f"{title.lower()}:{year or ''}"
        if not force_refresh:
            cached = cache.get('streaming', cache_key)
            if cached is not None: return cached
        try:
            streaming_limiter.wait()
            from simplejustwatchapi import justwatch as sjw
            entries = sjw.search(title, country=self.country, language='en', count=1, best_only=True)
            providers = sorted(list(set(o.package.name for o in entries[0].offers if o.package))) if entries else []
            cache.set('streaming', cache_key, providers, ttl=60 * 60 * 6)
            return providers
        except:
            cache.set('streaming', cache_key, [], ttl=60 * 60 * 2)
            return []

# --- FLASK ROUTES ---

@app.route('/_health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/")
def home():
    return app.send_static_file('index.html')

@app.route('/api/get_pages', methods=['POST'])
def get_pages():
    payload = request.get_json() or {}
    try:
        return jsonify({'pages': MovieRecommender().get_page_count(payload.get('username'))})
    except: return jsonify({'error': 'internal error'}), 500

@app.route('/api/recommend', methods=['POST'])
def recommend():
    data = request.get_json() or {}
    username = data.get('username')
    if not username: return jsonify({'error': 'username is required'}), 400
    try:
        rec_sys = MovieRecommender(country=data.get('country', 'CL'))
        user_films, pages = rec_sys.get_all_rated_films(username)
        if not user_films: return jsonify({'error': 'No movies found'}), 404
        
        enriched = []
        def enrich_task(f):
            d = rec_sys.get_tmdb_details(f['title'])
            if d:
                d['user_rating'] = f.get('rating', 0)
                return d
            return {'tmdb_id': None, 'title': f['title'], 'user_rating': f.get('rating', 0), 'genres': [], 'director': None}

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(enrich_task, f) for f in user_films[:DEFAULT_MAX_FILMS]]
            for fut in as_completed(futures):
                r = fut.result()
                if r: enriched.append(r)

        preferences = rec_sys.analyze_preferences(enriched)
        recommendations = rec_sys.get_recommendations(enriched, count=DEFAULT_LIMIT_RECS)

        if data.get('include_streaming', True) and recommendations:
            with ThreadPoolExecutor(max_workers=6) as ex:
                future_map = {ex.submit(rec_sys.get_streaming, r['title'], r.get('year')): r for r in recommendations}
                for fut in as_completed(future_map):
                    try: future_map[fut]['streaming'] = fut.result() or []
                    except: future_map[fut]['streaming'] = []
        else:
            for r in recommendations: r['streaming'] = []

        return jsonify({
            'username': username,
            'country_name': rec_sys.get_country_name(),
            'pages': pages,
            'preferences': preferences,
            'recommendations': recommendations,
        })
    except Exception as e:
        logger.exception("Error")
        return jsonify({'error': str(e)}), 500

# NUEVA RUTA: Sirve el archivo de resultados para cualquier /nombre_usuario
@app.route('/<path:username>')
def user_view(username):
    # Evitar conflictos con rutas de API o assets (favicon, css, js) si no están en carpeta
    if username.startswith('api/') or username == 'favicon.ico':
        return jsonify({'error': 'not found'}), 404
    
    # Comprobación básica de seguridad o existencia del archivo
    if os.path.exists('results.html'):
        return app.send_static_file('results.html')
    else:
        return "results.html not found on server", 500

import atexit
@atexit.register
def on_exit():
    logger.info("worker exiting")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
