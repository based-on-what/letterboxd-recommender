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

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("letterboxd-recommender")

# Configuración de Flask
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Variables de configuración desde variables de entorno
TMDB_KEY = os.getenv("TMDB_KEY")
REDIS_URL = os.getenv("REDIS_URL")
MAX_SCRAPE_PAGES = int(os.getenv("MAX_SCRAPE_PAGES") or 50)
DEFAULT_MAX_FILMS = int(os.getenv("DEFAULT_MAX_FILMS") or 30)
DEFAULT_LIMIT_RECS = int(os.getenv("DEFAULT_LIMIT_RECS") or 60)
MIN_RECOMMEND_RATING = float(os.getenv("MIN_RECOMMEND_RATING", "7.0"))

# Constantes de tiempo para cache
ONE_MONTH = 60 * 60 * 24 * 30
ONE_WEEK = 60 * 60 * 24 * 7


class RateLimiter:
    """
    Controlador de tasa de peticiones para evitar sobrecarga de APIs.
    
    Implementa un patrón de limitación temporal entre llamadas consecutivas
    para respetar los límites de las APIs externas y evitar bloqueos.
    """
    
    def __init__(self, min_interval=0.25):
        """
        Args:
            min_interval: Tiempo mínimo en segundos entre peticiones
        """
        self.min_interval = min_interval
        self._lock = Lock()
        self._last = 0.0
    
    def wait(self):
        """
        Espera el tiempo necesario antes de permitir la siguiente petición.
        Thread-safe mediante el uso de Lock.
        """
        with self._lock:
            now = time.time()
            diff = now - self._last
            if diff < self.min_interval:
                sleep_time = self.min_interval - diff
                time.sleep(sleep_time)
            self._last = time.time()


# Importación condicional de dependencias de caché
try:
    import redis
except ImportError:
    redis = None
    logger.warning("Redis no disponible, usando caché en memoria")

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None
    logger.warning("cachetools no disponible, usando dict simple")


class Cache:
    """
    Sistema de caché con soporte para Redis y memoria local.
    
    Proporciona una capa de abstracción para cachear datos con TTL,
    soportando tanto Redis (distribuido) como caché en memoria (local).
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
        """Intenta inicializar conexión a Redis una sola vez."""
        if self._redis_attempted:
            return
        
        self._redis_attempted = True
        if not REDIS_URL or not redis:
            return
        
        try:
            self.redis = redis.from_url(REDIS_URL, decode_responses=True)
            self.redis.ping()
            logger.info("Redis conectado exitosamente")
        except Exception as e:
            logger.warning(f"No se pudo conectar a Redis: {e}")
            self.redis = None
    
    def _redis_get(self, key):
        """Obtiene un valor de Redis."""
        try:
            val = self.redis.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None
    
    def _redis_set(self, key, value, ex=None):
        """Almacena un valor en Redis."""
        try:
            self.redis.set(key, json.dumps(value), ex=ex)
        except Exception:
            pass
    
    def get(self, namespace, key):
        """
        Obtiene un valor del caché (Redis primero, luego memoria).
        
        Args:
            namespace: Categoría del caché (tmdb, similar, streaming, user_scrape)
            key: Clave específica dentro del namespace
        """
        if not self._redis_attempted:
            self._init_redis()
        
        if self.redis:
            return self._redis_get(f"{namespace}:{key}")
        
        cache = self.caches.get(namespace)
        return cache.get(key) if cache else None
    
    def set(self, namespace, key, value, ttl=None):
        """
        Almacena un valor en el caché.
        
        Args:
            namespace: Categoría del caché
            key: Clave específica
            value: Valor a almacenar
            ttl: Tiempo de vida en segundos (solo Redis)
        """
        if not self._redis_attempted:
            self._init_redis()
        
        if self.redis:
            self._redis_set(f"{namespace}:{key}", value, ex=ttl)
        else:
            if self.caches.get(namespace) is not None:
                self.caches[namespace][key] = value


cache = Cache()

# Configuración de requests con reintentos
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session():
    """
    Crea una sesión HTTP con reintentos automáticos.
    
    Configura estrategia de reintentos exponenciales para manejar
    fallos transitorios de red y rate limiting.
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

# Rate limiters por servicio
tmdb_limiter = RateLimiter(min_interval=0.25)
letterboxd_limiter = RateLimiter(min_interval=0.35)
streaming_limiter = RateLimiter(min_interval=0.3)


def normalize_title(title):
    """
    Normaliza un título para comparación robusta.
    
    Remueve diacríticos, convierte a minúsculas, elimina caracteres especiales
    y normaliza espacios. Esto permite comparar títulos de forma más robusta.
    
    Args:
        title: Título a normalizar
        
    Returns:
        Título normalizado para comparación
        
    Example:
        >>> normalize_title("El Señor de los Anillos")
        'el senor de los anillos'
    """
    if not title:
        return ""
    
    # Descomponer caracteres Unicode y remover diacríticos
    title = unicodedata.normalize('NFKD', title)
    title = ''.join([c for c in title if not unicodedata.combining(c)])
    
    # Convertir a minúsculas
    title = title.lower()
    
    # Remover caracteres especiales, mantener solo alfanuméricos y espacios
    title = re.sub(r'[^a-z0-9\s]', ' ', title)
    
    # Normalizar espacios múltiples
    return re.sub(r'\s+', ' ', title).strip()


class MovieRecommender:
    """
    Sistema principal de recomendación de películas.
    
    Integra:
    - Scraping de Letterboxd
    - Enriquecimiento con TMDB
    - Análisis de preferencias
    - Generación de recomendaciones personalizadas
    - Consulta de disponibilidad en streaming
    """
    
    def __init__(self, country='CL', max_workers=8):
        """
        Args:
            country: Código ISO del país para streaming (default: CL)
            max_workers: Hilos para ejecución paralela (default: 8)
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
        """Retorna el nombre completo del país configurado."""
        return self.country_names.get(self.country, self.country)
    
    def _safe_get(self, url, params=None, headers=None, max_retries=2, service='generic'):
        """
        Realiza una petición HTTP con rate limiting y reintentos.
        
        Args:
            url: URL a la que hacer la petición
            params: Parámetros de query string
            headers: Headers HTTP adicionales
            max_retries: Número máximo de reintentos
            service: Identificador del servicio para rate limiting
            
        Returns:
            Response si exitosa, None si falla tras todos los reintentos
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
                logger.debug(f"Error en petición (intento {attempt + 1}): {e}")
                time.sleep(0.4 * (attempt + 1))
        
        logger.warning(f"Fallo después de {max_retries + 1} intentos: {url}")
        return None
    
    def get_page_count(self, username):
        """
        Obtiene el número total de páginas de películas del usuario.
        
        Args:
            username: Nombre de usuario en Letterboxd
            
        Returns:
            Número de páginas o 0 si hay error
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
            logger.error(f"Error obteniendo número de páginas: {e}")
            return 0
    
    def get_all_rated_films(self, username, max_pages=None, include_unrated=True):
        """
        Obtiene todas las películas del perfil del usuario.
        
        MEJORA: Incluye películas sin calificar para evitar recomendarlas.
        
        Args:
            username: Nombre de usuario en Letterboxd
            max_pages: Límite de páginas a scrapear
            include_unrated: Si incluir películas sin calificar
            
        Returns:
            Tupla (lista de películas, número de páginas scrapeadas)
            
        Note:
            Las películas sin calificar tendrán rating=0 pero serán marcadas
            como vistas para evitar su recomendación.
        """
        if not username:
            return [], 0
        
        max_pages = max_pages or MAX_SCRAPE_PAGES
        
        cache_key = f"{username}:pages:v2"
        cached = cache.get('user_scrape', cache_key)
        if cached:
            logger.info(f"Perfil de {username} obtenido de caché")
            return cached.get('films', []), cached.get('pages', 0)
        
        base_url = f"{self.letterboxd_base}/{username}/films/"
        
        try:
            pages = self.get_page_count(username)
            if pages <= 0:
                return [], 0
            
            if pages > max_pages:
                logger.info(f"Limitando scraping a {max_pages} de {pages} páginas")
                pages = max_pages
            
            rating_map = {f'rated-{i}': i / 2.0 for i in range(1, 11)}
            headers = {'User-Agent': session.headers.get('User-Agent')}
            
            def scrape_page(page):
                """Scrapea una página individual del perfil."""
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
                        logger.debug(f"Error procesando película: {e}")
                        continue
                
                return page_films
            
            films = []
            logger.info(f"Scrapeando {pages} páginas del perfil de {username}")
            
            with ThreadPoolExecutor(max_workers=min(self.max_workers, 6)) as ex:
                futures = [ex.submit(scrape_page, p) for p in range(1, pages + 1)]
                for f in as_completed(futures):
                    films.extend(f.result())
            
            if not include_unrated:
                films = [f for f in films if f.get('has_rating')]
            
            logger.info(f"Total películas encontradas: {len(films)} "
                       f"({len([f for f in films if f.get('has_rating')])} calificadas)")
            
            cache.set('user_scrape', cache_key, {'pages': pages, 'films': films}, ttl=60 * 30)
            
            return films, pages
            
        except Exception as e:
            logger.error(f"Error scrapeando perfil: {e}")
            return [], 0
    
    def get_tmdb_details(self, title, force_refresh=False):
        """
        Busca detalles de una película en TMDB por título.
        
        Args:
            title: Título de la película
            force_refresh: Forzar actualización ignorando caché
            
        Returns:
            Diccionario con metadatos o None si no se encuentra
        """
        if not self.tmdb_key:
            logger.warning("TMDB_KEY no configurada")
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
            logger.debug(f"Error buscando en TMDB: {e}")
            return None
    
    def get_tmdb_details_by_id(self, movie_id, force_refresh=False):
        """
        Obtiene detalles completos de una película por ID de TMDB.
        
        Args:
            movie_id: ID de TMDB
            force_refresh: Forzar actualización
            
        Returns:
            Diccionario con metadatos completos:
            - tmdb_id, title, original_title
            - year, director, genres
            - poster, rating_tmdb, runtime
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
        Analiza las preferencias del usuario basándose en sus películas.
        
        Identifica géneros favoritos, directores recurrentes y décadas
        mediante análisis de frecuencia.
        
        Args:
            enriched_films: Lista de películas con metadatos
            
        Returns:
            Diccionario con:
            - genres: Top 3 géneros
            - directors: Top 3 directores
            - decades: Top 3 décadas
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
        Genera recomendaciones basadas en las películas del usuario.
        
        CRITERIO ACTUALIZADO: Usa TODAS las películas con 4+ estrellas
        (no solo top 10) como semillas para buscar similares.
        
        Proceso:
        1. Filtra películas con rating >= 4.0
        2. Para cada una, busca similares en TMDB
        3. Elimina duplicados y películas ya vistas
        4. Filtra por rating mínimo de TMDB
        5. Retorna las mejores recomendaciones
        
        Args:
            enriched_films: Películas del usuario con metadatos
            count: Número de recomendaciones a retornar
            force_refresh: Ignorar caché
            
        Returns:
            Lista de recomendaciones con metadatos completos
        """
        # Construir conjunto de películas vistas
        seen_ids = set()
        seen_titles_norm = set()
        
        logger.info("=== PELÍCULAS DEL USUARIO ===")
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
            
            rating_display = f"{film.get('user_rating', 0):.1f}★" if film.get('user_rating', 0) > 0 else "Sin calificar"
            logger.info(f"  • {film.get('title', 'Sin título')} [{film.get('year', '????')}] "
                       f"- TMDB ID: {film.get('tmdb_id', 'N/A')} - Rating: {rating_display}")
        
        logger.info(f"Total películas vistas: {len(enriched_films)} "
                   f"(IDs únicos: {len(seen_ids)}, Títulos normalizados: {len(seen_titles_norm)})")
        
        recs = []
        
        # CAMBIO PRINCIPAL: Usar TODAS las películas con 4+ estrellas
        highly_rated_films = [
            film for film in enriched_films 
            if film.get('user_rating', 0) >= 4.0
        ]
        
        highly_rated_films = sorted(
            highly_rated_films,
            key=lambda x: x.get('user_rating', 0),
            reverse=True
        )
        
        logger.info(f"\n=== PELÍCULAS CON 4+ ESTRELLAS PARA RECOMENDACIONES ===")
        logger.info(f"Total de películas con 4+ estrellas: {len(highly_rated_films)}")
        
        # Exportar a JSON si estamos en desarrollo local
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
                
                logger.info(f"✓ Películas de alta calificación exportadas a: {filename}")
                
            except Exception as e:
                logger.warning(f"No se pudo exportar JSON: {e}")
        
        for idx, film in enumerate(highly_rated_films, 1):
            logger.info(f"  {idx}. {film.get('title')} [{film.get('year')}] - "
                       f"{film.get('user_rating')}★ (TMDB: {film.get('rating_tmdb', 'N/A')})")
        
        logger.info(f"\n=== BUSCANDO PELÍCULAS SIMILARES ===")
        
        def process_similar(film):
            """Procesa películas similares a una película semilla."""
            if not film.get('tmdb_id'):
                return []
            
            logger.info(f"Buscando similares a: {film.get('title')}")
            
            cache_key = f"similar:{film.get('tmdb_id')}"
            
            if not force_refresh:
                cached = cache.get('similar', cache_key)
                if cached:
                    logger.info(f"  ↳ Obtenidas {len(cached)} de caché")
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
                        logger.debug(f"  ✗ {title} - Ya vista (ID match)")
                        continue
                    
                    if title_norm in seen_titles_norm:
                        logger.debug(f"  ✗ {title} - Ya vista (título match)")
                        continue
                    
                    det = self.get_tmdb_details_by_id(mid, force_refresh)
                    
                    if not det:
                        continue
                    
                    det_title_norm = normalize_title(det.get('title', ''))
                    det_orig_norm = normalize_title(det.get('original_title', ''))
                    
                    if (str(det.get('tmdb_id')) in seen_ids or 
                        det_title_norm in seen_titles_norm or 
                        det_orig_norm in seen_titles_norm):
                        logger.debug(f"  ✗ {det.get('title')} - Ya vista (verificación secundaria)")
                        continue
                    
                    det['reason'] = f"Since you liked {film.get('title')}"
                    local.append(det)
                    logger.debug(f"  ✓ {det.get('title')} - Candidata válida")
                
                cache.set('similar', cache_key, local, ttl=60 * 60 * 24)
                logger.info(f"  ↳ {len(local)} películas nuevas encontradas")
                
                return local
                
            except Exception as e:
                logger.error(f"Error buscando similares: {e}")
                return []
        
        # Procesar en paralelo todas las películas altamente calificadas
        with ThreadPoolExecutor(max_workers=min(6, self.max_workers)) as ex:
            futures = [ex.submit(process_similar, f) for f in highly_rated_films]
            for f in as_completed(futures):
                recs.extend(f.result())
        
        # Deduplicar recomendaciones
        unique = {}
        for r in recs:
            key = str(r['tmdb_id'])
            r_title_norm = normalize_title(r['title'])
            
            if (key not in unique and 
                key not in seen_ids and 
                r_title_norm not in seen_titles_norm):
                unique[key] = r
        
        # Filtrar por rating mínimo de TMDB
        filtered = [
            v for v in unique.values()
            if v.get('rating_tmdb') is not None and 
               float(v['rating_tmdb']) >= MIN_RECOMMEND_RATING
        ]
        
        logger.info(f"\n=== RESULTADO FINAL ===")
        logger.info(f"Candidatas iniciales: {len(recs)}")
        logger.info(f"Después de deduplicar: {len(unique)}")
        logger.info(f"Después de filtro de rating (>={MIN_RECOMMEND_RATING}): {len(filtered)}")
        logger.info(f"Retornando top {min(count, len(filtered))}")
        
        return filtered[:count]
    
    def get_streaming(self, title, year=None, force_refresh=False):
        """
        Obtiene plataformas de streaming donde está disponible una película.
        
        Args:
            title: Título de la película
            year: Año de lanzamiento (opcional)
            force_refresh: Ignorar caché
            
        Returns:
            Lista de nombres de plataformas de streaming
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
            logger.debug(f"Error obteniendo streaming para {title}: {e}")
            cache.set('streaming', cache_key, [], ttl=60 * 60 * 2)
            return []


# ============================================================================
# RUTAS DE FLASK
# ============================================================================

@app.route('/_health', methods=['GET'])
def health():
    """Endpoint de health check para monitoring."""
    return jsonify({"status": "ok"}), 200


@app.route("/")
def home():
    """Página principal de la aplicación."""
    return app.send_static_file('index.html')


@app.route('/api/get_pages', methods=['POST'])
def get_pages():
    """
    Endpoint para obtener número de páginas de un perfil.
    
    Request JSON: {"username": str}
    Returns: {"pages": int}
    """
    payload = request.get_json() or {}
    
    try:
        recommender = MovieRecommender()
        page_count = recommender.get_page_count(payload.get('username'))
        return jsonify({'pages': page_count})
        
    except Exception as e:
        logger.exception("Error en get_pages")
        return jsonify({'error': 'internal error'}), 500


@app.route('/api/recommend', methods=['POST'])
def recommend():
    """
    Endpoint principal para generar recomendaciones.
    
    Request JSON:
        - username: str (requerido)
        - country: str (opcional, default: CL)
        - include_streaming: bool (opcional, default: True)
    
    Returns JSON:
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
        logger.info(f"INICIANDO ANÁLISIS PARA: {username}")
        logger.info(f"{'='*60}")
        
        # Obtener películas del usuario (incluye no calificadas)
        user_films, pages = rec_sys.get_all_rated_films(username, include_unrated=True)
        
        if not user_films:
            return jsonify({'error': 'No movies found'}), 404
        
        # Enriquecer con datos de TMDB
        enriched = []
        
        def enrich_task(film):
            """Enriquece una película con datos de TMDB."""
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
        
        logger.info(f"\nEnriqueciendo películas con datos de TMDB...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(enrich_task, f) for f in user_films[:DEFAULT_MAX_FILMS]]
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    enriched.append(result)
        
        # Analizar preferencias
        preferences = rec_sys.analyze_preferences(enriched)
        logger.info(f"\nPreferencias detectadas:")
        logger.info(f"  Géneros: {', '.join(preferences.get('genres', []))}")
        logger.info(f"  Directores: {', '.join(preferences.get('directors', []))}")
        logger.info(f"  Décadas: {', '.join(preferences.get('decades', []))}")
        
        # Generar recomendaciones
        recommendations = rec_sys.get_recommendations(enriched, count=DEFAULT_LIMIT_RECS)
        
        # Obtener información de streaming si se solicita
        if data.get('include_streaming', True) and recommendations:
            logger.info(f"\nObteniendo información de streaming...")
            
            with ThreadPoolExecutor(max_workers=6) as ex:
                future_map = {
                    ex.submit(rec_sys.get_streaming, r['title'], r.get('year')): r
                    for r in recommendations
                }
                
                for fut in as_completed(future_map):
                    try:
                        future_map[fut]['streaming'] = fut.result() or []
                    except Exception as e:
                        logger.debug(f"Error obteniendo streaming: {e}")
                        future_map[fut]['streaming'] = []
        else:
            for r in recommendations:
                r['streaming'] = []
        
        logger.info(f"\n{'='*60}")
        logger.info(f"ANÁLISIS COMPLETADO")
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
    """Vista de resultados para un usuario específico."""
    return app.send_static_file('results.html')


# ============================================================================
# INICIALIZACIÓN
# ============================================================================

import atexit

@atexit.register
def on_exit():
    """Función de limpieza ejecutada al salir del programa."""
    logger.info("Cerrando worker y liberando recursos...")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Iniciando servidor en puerto {port}")
    app.run(host='0.0.0.0', port=port, debug=True)
