# Letterboxd Recommender

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.1+-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Demo en vivo](https://img.shields.io/badge/Demo-Live-success.svg)](https://letterboxd-recommender.up.railway.app/)

**Letterboxd Recommender** es un sistema inteligente de recomendacion de peliculas que analiza tu perfil de Letterboxd para ofrecerte sugerencias personalizadas. Combinando web scraping, enriquecimiento de metadatos via TMDB y consultas de disponibilidad en plataformas de streaming, te ayuda a descubrir tu proxima pelicula favorita en base a lo que ya te gusto.

Demo en vivo: [letterboxd-recommender.up.railway.app](https://letterboxd-recommender.up.railway.app/)

---

## Tabla de Contenidos

- [Funcionalidades](#funcionalidades)
- [Como Funciona](#como-funciona)
- [Stack Tecnologico](#stack-tecnologico)
- [Requisitos Previos](#requisitos-previos)
- [Instalacion](#instalacion)
- [Configuracion](#configuracion)
- [Uso](#uso)
- [Referencia de API](#referencia-de-api)
- [Estructura del Proyecto](#estructura-del-proyecto)
- [Contribuir](#contribuir)
- [Licencia](#licencia)

---

## Funcionalidades

- **Recomendaciones Personalizadas**: Analiza tus peliculas con 4+ estrellas y consulta TMDB para encontrar titulos similares
- **Disponibilidad en Streaming**: Muestra en que plataformas esta disponible cada recomendacion en tu pais
- **Analisis Inteligente de Preferencias**: Identifica tus generos, directores y decadas favoritas a partir de tu historial
- **Actualizaciones en Tiempo Real**: Server-Sent Events (SSE) entregan logs, recomendaciones y actualizaciones de estado en vivo
- **Cache Inteligente**: Soporte para Redis con fallback automatico a un almacen en memoria con TTL y thread safety
- **Procesamiento Concurrente**: ThreadPoolExecutor con 6 workers de enriquecimiento y 4 workers de similitud
- **Interfaz Limpia**: Frontend en JavaScript vanilla sin dependencias de frameworks, con modo oscuro
- **Deduplicacion**: Filtra peliculas ya vistas por TMDB ID y titulo normalizado
- **Soporte Multi-Pais**: Disponibilidad en streaming para 13+ paises
- **Circuit Breaker**: Pausa automaticamente el scraping de Letterboxd tras errores consecutivos y sirve cache desactualizada
- **Resiliencia Anti-Bot**: Cadena de fallback de 4 niveles (requests → cloudscraper → curl_cffi → camoufox)
- **Rate Limiting**: Throttling por IP en todos los endpoints via Flask-Limiter
- **Autenticacion Dual TMDB**: Detecta automaticamente API key v3 (query param) vs Bearer token v4

---

## Como Funciona

El motor de recomendaciones sigue un pipeline de multiples pasos:

1. **Scraping del Perfil**: Obtiene todas las peliculas del perfil de Letterboxd del usuario (incluyendo entradas sin calificar para saber que ya vio)
2. **Enriquecimiento TMDB**: Agrega metadatos de The Movie Database a cada pelicula (año, generos, director, duracion, poster, rating) usando hasta 6 workers en paralelo
3. **Analisis de Preferencias**: Identifica el top-3 de generos, directores y decadas de tu historial
4. **Descubrimiento de Peliculas Similares**: Por cada pelicula semilla (4+ estrellas), consulta TMDB para hasta 12 titulos similares por semilla
5. **Filtrado Inteligente**: Elimina peliculas ya vistas, aplica umbral minimo de rating (default: 7.0) y deduplica por TMDB ID y titulo normalizado
6. **Consulta de Streaming**: Verifica disponibilidad en plataformas para el pais seleccionado via JustWatch (con TMDB watch providers como fallback)
7. **Entrega en Tiempo Real**: Hace streaming de recomendaciones al frontend a medida que se descubren, via tres canales SSE (logs, recomendaciones, estado)

Si el scraping en vivo de Letterboxd falla (circuit breaker abierto), la API devuelve una copia en cache del perfil con el marcador `data_freshness: stale_cache` para que la UI pueda avisar al usuario.

---

## Stack Tecnologico

### Backend

- **[Python 3.10+](https://www.python.org/)**: Lenguaje principal de la aplicacion
- **[Flask 3.1](https://flask.palletsprojects.com/)**: Framework web ligero con routing via Blueprint
- **[Flask-CORS](https://flask-cors.readthedocs.io/)**: Headers de Cross-Origin Resource Sharing
- **[BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)**: Parseo de HTML para el scraping de Letterboxd
- **[Requests](https://requests.readthedocs.io/)**: Cliente HTTP para scraping y llamadas a APIs
- **[cloudscraper](https://github.com/VeNoMouS/cloudscraper)**: Fallback anti-bot nivel 2 (bypass de CloudFlare)
- **[curl_cffi](https://github.com/yifeikong/curl_cffi)**: Fallback anti-bot nivel 3 (impersonacion de Chrome via libcurl)
- **camoufox**: Fallback anti-bot nivel 4 (Firefox headless, ultimo recurso)
- **[Flask-Limiter](https://flask-limiter.readthedocs.io/)**: Rate limiting por IP
- **[Gunicorn](https://gunicorn.org/)**: Servidor WSGI de produccion (worker class `gthread`)

### APIs Externas

- **[TMDB API](https://www.themoviedb.org/documentation/api)**: Metadatos de peliculas, descubrimiento de similares y consulta de proveedores de streaming
- **[SimpleJustWatch](https://github.com/Electronic-Mango/simple-justwatch-python-api)**: Fuente principal de disponibilidad en streaming

### Cache y Rendimiento

- **[Redis](https://redis.io/)**: Cache distribuida opcional (fallback automatico a memoria)
- **`_ExpiringDict` personalizado**: Almacen TTL en memoria con thread safety y eviccion LRU
- **ThreadPoolExecutor**: 6 workers para enriquecimiento TMDB, 4 workers para consultas de similitud

### Frontend

- **JavaScript Vanilla**: Sin dependencias de frameworks
- **Server-Sent Events (SSE)**: Tres canales en tiempo real: logs, recomendaciones, estado
- **LocalStorage**: Cache del lado del cliente

### Despliegue

- **[Railway](https://railway.app/)**: Plataforma cloud de hosting
- **[Procfile](https://devcenter.heroku.com/articles/procfile)**: `gunicorn` con workers `gthread`, timeout de 180s

---

## Requisitos Previos

Antes de instalar, asegurate de tener:

- **Python 3.10 o superior** ([Descargar](https://www.python.org/downloads/))
- **pip** (gestor de paquetes de Python, incluido con Python)
- **TMDB API Key** ([Obtener una gratis aqui](https://www.themoviedb.org/settings/api))
- **(Opcional) Redis** para cache distribuida ([Guia de instalacion](https://redis.io/docs/getting-started/installation/))

---

## Instalacion

### 1. Clonar el Repositorio

```bash
git clone https://github.com/based-on-what/letterboxd-recommender.git
cd letterboxd-recommender
```

### 2. Crear un Entorno Virtual (Recomendado)

```bash
python -m venv venv

# macOS/Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Instalar Dependencias

```bash
pip install -r requirements.txt
```

---

## Configuracion

Crea un archivo `.env` en la raiz del proyecto:

```bash
# Requerido
TMDB_KEY=tu_api_key_de_tmdb

# Opcional - Redis
REDIS_URL=redis://localhost:6379
RATELIMIT_STORAGE_URI=redis://localhost:6379

# Opcional - Configuracion de la aplicacion
PORT=8080
FLASK_ENV=development
MIN_RECOMMEND_RATING=7.0
LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD=5
LETTERBOXD_CIRCUIT_COOLDOWN_S=180
CACHE_MAX_SIZE=10000
STREAM_MAX_AGE_S=3600

# Opcional - Timeouts HTTP salientes (segundos)
LETTERBOXD_HTTP_TIMEOUT=12
TMDB_HTTP_TIMEOUT=12
CAMOUFOX_TIMEOUT=20

# Opcional - Auth interna para /_incident-status
INTERNAL_TOKEN=
```

### Referencia de Variables de Entorno

| Variable | Requerida | Default | Descripcion |
|----------|-----------|---------|-------------|
| `TMDB_KEY` | Si | — | API key de TMDB (v3) o Bearer token (v4); se detecta automaticamente |
| `REDIS_URL` | No | — | Cadena de conexion a Redis; usa cache en memoria si no se define |
| `RATELIMIT_STORAGE_URI` | No | `REDIS_URL` si existe, sino `memory://` | Backend de almacenamiento para Flask-Limiter (memory:// es por proceso: los limites se multiplican por worker) |
| `PORT` | No | `8080` | Puerto para el servidor Flask/Gunicorn |
| `FLASK_ENV` | No | `production` | Usar `development` para salida de debug y exports JSON |
| `LOCAL_DEV` | No | — | Flag alternativo de modo dev (`true` habilita el mismo comportamiento que `FLASK_ENV=development`) |
| `MIN_RECOMMEND_RATING` | No | `7.0` | Rating minimo de TMDB para incluir en recomendaciones |
| `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` | No | `5` | Fallos consecutivos antes de abrir el circuit breaker |
| `LETTERBOXD_CIRCUIT_COOLDOWN_S` | No | `180` | Segundos de espera mientras el circuit breaker esta abierto |
| `CACHE_MAX_SIZE` | No | `10000` | Maximo de entradas en la cache en memoria antes de eviccion LRU |
| `STREAM_MAX_AGE_S` | No | `3600` | Segundos antes de evictar un stream SSE inactivo de la memoria |
| `SSE_QUEUE_MAXSIZE` | No | `1000` | Maximo de mensajes por cola SSE; al desbordar se descartan los mas antiguos |
| `SCRAPE_POOL_SIZE` | No | `6` | Threads del pool compartido de scraping de Letterboxd (por proceso) |
| `WORK_POOL_SIZE` | No | `8` | Threads del pool compartido de enriquecimiento/recomendacion (por proceso) |
| `PIPELINE_POOL_SIZE` | No | `4` | Jobs asincronos de recomendacion concurrentes por proceso |
| `JOB_RESULT_TTL` | No | `900` | Segundos que el payload de `/api/result` queda disponible |
| `LETTERBOXD_HTTP_TIMEOUT` | No | `12` | Timeout (s) para requests de scraping a Letterboxd (requests/cloudscraper/curl_cffi) |
| `TMDB_HTTP_TIMEOUT` | No | `12` | Timeout (s) para requests a la API de TMDB |
| `CAMOUFOX_TIMEOUT` | No | `20` | Timeout (s) de carga de pagina para el fallback camoufox (navegador headless) |
| `HTTP_POOL_MAXSIZE` | No | `20` | Conexiones mantenidas por pool urllib3 (por host) |
| `LETTERBOXD_RETRY_SLEEP_S` | No | `0.4` | Espera base (s) entre reintentos de scraping |
| `LETTERBOXD_THROTTLE_SLEEP_S` | No | `1.5` | Espera base (s) entre reintentos tras un 429 |
| `SIMILAR_RESULTS_PER_FILM` | No | `12` | Titulos similares pedidos a TMDB por film semilla |
| `INTERNAL_TOKEN` | No | — | Token para proteger `/_incident-status` (sin proteccion si no se define) |

---

## Uso

### Ejecutar Localmente

```bash
# Modo desarrollo (auto-reload, output de debug)
FLASK_ENV=development python main.py

# Modo produccion
gunicorn -c gunicorn.conf.py main:app
```

### Usar la Aplicacion

1. Abre el navegador en `http://localhost:8080`
2. Ingresa un nombre de usuario de Letterboxd (ej: `karsten`, `davidehrlich`)
3. Selecciona tu pais para la disponibilidad en streaming
4. Haz clic en **Get Recommendations**
5. Ve los resultados en `http://localhost:8080/<username>`

---

## Referencia de API

### Health Check

```http
GET /_health
```

**Respuesta:**
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

### Estado de Incidentes

```http
GET /_incident-status
```

Devuelve el snapshot actual del circuit breaker. Limitado a 30 requests/minuto. Protegido por el header `X-Internal-Token` si `INTERNAL_TOKEN` esta definido.

---

### Obtener Cantidad de Paginas

```http
POST /api/get_pages
```

Limitado a 10 requests/minuto.

**Request:**
```json
{ "username": "karsten" }
```

**Respuesta:**
```json
{ "pages": 42 }
```

---

### Generar Recomendaciones

```http
POST /api/recommend
```

Limitado a 5 requests/minuto.

**Request:**
```json
{
  "username": "karsten",
  "country": "AR",
  "include_streaming": true
}
```

**Parametros:**
- `username` (string, requerido): Nombre de usuario de Letterboxd — alfanumerico, guiones bajos y medios, 1–50 caracteres
- `country` (string, opcional): Codigo ISO de pais (default: `"CL"`)
- `include_streaming` (boolean, opcional): Incluir disponibilidad en streaming (default: `true`)
- `request_id` (string, opcional): ID del cliente para correlacion con streams SSE (se genera automaticamente si no se envia)

**Respuesta:**
```json
{
  "username": "karsten",
  "country_name": "Argentina",
  "country_code": "AR",
  "pages": 42,
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "preferences": {
    "genres": ["Drama", "Thriller", "Ciencia ficcion"],
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
      "genres": ["Ciencia ficcion", "Drama"],
      "poster": "https://image.tmdb.org/t/p/w500/...",
      "rating_tmdb": 7.9,
      "runtime": 164,
      "streaming": ["Prime Video", "Max"],
      "reason": "Since you liked Arrival"
    }
  ]
}
```

Cuando el scraping en vivo no esta disponible, la respuesta incluye ademas:

```json
{
  "data_freshness": "stale_cache",
  "hint": "Showing last successful profile snapshot because live Letterboxd scraping was blocked or throttled.",
  "incident": { "letterboxd_circuit_open": true, "letterboxd_circuit_retry_after_s": 120 }
}
```

---

### Endpoints SSE en Tiempo Real

Los tres streams requieren el parametro `request_id` que devuelve `/api/recommend`. Limitados a 20 requests/minuto cada uno.

| Endpoint | Descripcion |
|----------|-------------|
| `GET /api/logs-stream?request_id=<id>` | Mensajes de log mientras corre el pipeline |
| `GET /api/recommendations-stream?request_id=<id>` | Recomendaciones a medida que se descubren; finaliza con `{"status": "complete"}` |
| `GET /api/status-stream?request_id=<id>` | Actualizaciones de estado del pipeline (pelicula semilla actual); finaliza con `{"status": "complete"}` |

---

### Rutas Estaticas

| Ruta | Descripcion |
|------|-------------|
| `GET /` | Pagina de inicio con el input de usuario |
| `GET /<username>` | Pagina de resultados para un usuario |

---

## Estructura del Proyecto

```
letterboxd-recommender/
├── main.py                  # App factory: carga env, crea la app Flask, registra el blueprint
├── app.py                   # Entry point WSGI
├── routes.py                # Flask Blueprint: solo HTTP, sin logica de negocio
├── recommender.py           # Fachada publica: orquestador MovieRecommender + re-exports de compatibilidad
├── cache.py                 # Abstraccion de cache (Redis + fallback _ExpiringDict), constantes TTL
├── sse.py                   # Gestion de streams SSE (colas de logs, recomendaciones y estado)
├── limiter.py               # Singleton de Flask-Limiter (init_app diferido)
├── utils.py                 # Utilidades compartidas: normalize_title, IS_DEV, export_debug_json
├── test_main.py             # Tests unitarios e integracion (pytest)
│
├── infra/                   # Capa de I/O: sin logica de negocio
│   ├── http.py              # Sesiones, circuit breaker (IncidentTracker), rate limiters, fallbacks anti-bot
│   ├── letterboxd.py        # Cliente de scraping de Letterboxd (paginas, perfil, cadena de fallback de 4 niveles)
│   ├── tmdb.py              # Cliente TMDB (busqueda, detalles, similares, watch providers, auth dual v3/v4)
│   └── streaming.py         # Cliente JustWatch + TMDB watch providers para disponibilidad en streaming
│
├── services/                # Logica de dominio: funciones puras, sin I/O directo
│   ├── enricher.py          # Tarea de enriquecimiento (metadata TMDB por pelicula)
│   ├── preferences.py       # Analisis de preferencias: top generos, directores, decadas
│   └── recommender.py       # Pipeline principal de generacion de recomendaciones
│
├── static/
│   ├── index.html           # UI de la pagina de inicio
│   └── results.html         # Pagina de visualizacion de recomendaciones
│
├── requirements.txt         # Dependencias Python
├── runtime.txt              # Version de Python
├── gunicorn.conf.py         # Configuracion de Gunicorn
├── Procfile                 # Configuracion de despliegue Railway/Heroku
└── .env                     # Variables de entorno (no incluido en el repo)
```

### Componentes Clave

- **`MovieRecommender`** (`recommender.py`): Orquestador ligero que compone `LetterboxdClient`, `TmdbClient` y `StreamingClient`. Expone la misma interfaz publica que la implementacion monolitica anterior.

- **`LetterboxdClient`** (`infra/letterboxd.py`): Hace scraping del historial de peliculas del usuario con una cadena de fallback anti-bot de 4 niveles: `requests` → `cloudscraper` → `curl_cffi` → `camoufox`. Integra el circuit breaker via `IncidentTracker`. Cachea perfiles frescos por 30 minutos y obsoletos por 7 dias.

- **`TmdbClient`** (`infra/tmdb.py`): Maneja busqueda, detalles de peliculas, descubrimiento de similares y consulta de proveedores de streaming. Detecta automaticamente API key v3 vs Bearer token v4. Cachea resultados por 1 dia.

- **`StreamingClient`** (`infra/streaming.py`): Resuelve disponibilidad via JustWatch (preferido) o TMDB watch providers (fallback). Normaliza nombres de proveedores. Cachea exitos por 6 horas y fallos por 2 horas.

- **`IncidentTracker`** (`infra/http.py`): Circuit breaker que se abre luego de `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` fallos consecutivos de scraping y suprime requests en vivo por `LETTERBOXD_CIRCUIT_COOLDOWN_S` segundos.

- **`Cache`** (`cache.py`): Almacen clave-valor con namespaces. Usa Redis si `REDIS_URL` esta definido; sino usa `_ExpiringDict` (eviccion LRU, tamaño maximo configurable).

- **Streams SSE** (`sse.py`): Tres colas por request (logs, recomendaciones, estado) gestionadas por `QueueHandler`. Los streams inactivos se evictan tras `STREAM_MAX_AGE_S` segundos.

---

## Contribuir

Las contribuciones son bienvenidas.

### Pull Requests

1. Haz fork del repositorio y crea una rama de feature: `git checkout -b feature/mi-feature`
2. Sigue las guias de estilo PEP 8
3. Mantene la logica de negocio en `services/` y el I/O en `infra/`; las rutas solo deben parsear input y formatear respuestas
4. Ejecuta `pytest test_main.py` antes de enviar
5. Usa prefijos convencionales en los commits: `Add:`, `Fix:`, `Update:`, `Docs:`
6. Abre un pull request con una descripcion clara que referencie los issues relacionados

### Reportar Problemas

Abre un issue en [github.com/based-on-what/letterboxd-recommender/issues](https://github.com/based-on-what/letterboxd-recommender/issues) con los pasos para reproducir, comportamiento esperado vs real, y detalles de tu entorno.

---

## Licencia

Licencia MIT. Ver el codigo fuente para el texto completo.

---

## Reconocimientos

- **[Letterboxd](https://letterboxd.com/)** — La red social para cineastas que inspiro este proyecto
- **[The Movie Database (TMDB)](https://www.themoviedb.org/)** — Metadatos y API de peliculas
- **[JustWatch](https://www.justwatch.com/)** — Datos de disponibilidad en streaming via SimpleJustWatch
- **[Railway](https://railway.app/)** — Despliegue y hosting sin complicaciones
