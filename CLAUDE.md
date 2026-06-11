# CLAUDE.md — Letterboxd Recommender

Guía de referencia rápida para Claude Code. Léela antes de modificar cualquier archivo.

---

## Qué hace este proyecto

Sistema de recomendaciones de películas que analiza el perfil Letterboxd de un usuario (historial + ratings), enriquece cada film con TMDB, y sugiere títulos no vistos filtrados por disponibilidad de streaming en el país del usuario. Las recomendaciones llegan en tiempo real vía Server-Sent Events (SSE).

**Demo en producción:** https://letterboxd-recommender.up.railway.app/  
**Repo:** https://github.com/based-on-what/letterboxd-recommender

---

## Stack

- **Python 3.11**, Flask 3.1, Gunicorn (`gthread`)
- **Scraping:** BeautifulSoup4 + cadena de fallbacks anti-bot (requests → cloudscraper → curl_cffi → camoufox)
- **APIs externas:** TMDB API (metadata + providers), simplejustwatchapi (streaming)
- **Caché:** Redis (opcional) con fallback a `_ExpiringDict` en memoria
- **Rate limiting:** Flask-Limiter por IP
- **Frontend:** Vanilla JS + SSE, sin frameworks. Dos páginas estáticas en `static/`.

---

## Estructura del proyecto

```
letterboxd-recommender/
├── main.py              # App factory: carga .env, crea Flask app, registra blueprint + re-exports para tests
├── app.py               # WSGI entry point: `from main import app`
├── routes.py            # Blueprint Flask — solo HTTP: parsea input, llama servicios, devuelve JSON
├── recommender.py       # Fachada pública: MovieRecommender + re-exports backward-compat
├── cache.py             # Cache (Redis + _ExpiringDict), RateLimiter, constantes TTL
├── sse.py               # Gestión de streams SSE (colas de logs/recs/status por request_id)
├── limiter.py           # Flask-Limiter singleton (patrón deferred init_app)
├── utils.py             # normalize_title, IS_DEV, export_debug_json
├── tests/               # Tests pytest: test_routes, test_cache, test_sse, test_infra, test_services
│
├── infra/               # Capa I/O — sin lógica de negocio
│   ├── http.py          # Sessions, retry, circuit breaker (IncidentTracker), rate limiters, fallbacks anti-bot
│   ├── letterboxd.py    # LetterboxdClient: scraping de perfiles con cadena de fallbacks
│   ├── tmdb.py          # TmdbClient: search, detalles, similares, watch providers
│   └── streaming.py     # StreamingClient: JustWatch (primario) + TMDB providers (fallback)
│
├── services/            # Lógica de dominio — funciones puras, sin I/O directa
│   ├── enricher.py      # enrich_film_task + batch_enrich (agrega metadata TMDB a cada film)
│   ├── preferences.py   # analyze_preferences: top géneros, directores, décadas
│   └── recommender.py   # get_recommendations: pipeline completo de generación
│
└── static/
    ├── index.html       # Landing page
    └── results.html     # Página de resultados con SSE
```

---

## Regla arquitectural fundamental

**`infra/` solo hace I/O. `services/` solo hace lógica. `routes.py` solo hace HTTP.**

- Si escribes lógica de negocio en `infra/` → mal.
- Si escribes requests HTTP en `services/` → mal.
- Si escribes cálculos/análisis en `routes.py` → mal.
- `recommender.py` es la única fachada: `routes.py` y `main.py` solo importan de aquí.

---

## Inicialización y orden de imports crítico

`main.py` hace `load_dotenv()` **antes** de cualquier otro import. Esto es esencial porque varios módulos leen `os.getenv()` en tiempo de importación (`TMDB_KEY`, `REDIS_URL`, etc.). No mover ni reordenar esa línea.

Orden de init en `main.py`:
1. `load_dotenv()`
2. Logging setup + `QueueHandler` (SSE log routing)
3. `Flask` app + CORS
4. `limiter.init_app(app)` (deferred init — limiter se importa antes pero se conecta al app aquí)
5. `routes.bp` registration

---

## MovieRecommender — fachada principal

```python
# Uso normal
rec = MovieRecommender(country='CL')
films, pages = rec.get_all_rated_films('username', include_unrated=True)
enriched = [enrich_film_task(rec, f) for f in films]  # o con ThreadPoolExecutor
prefs = rec.analyze_preferences(enriched)
recs = rec.get_recommendations(enriched, request_id='uuid')
```

`MovieRecommender` compone internamente `LetterboxdClient`, `TmdbClient`, y `StreamingClient`. Expone propiedades de compatibilidad (`tmdb_key`, `_letterboxd_last_failures`, `_safe_get`) para que los tests puedan parchear sin cambios.

---

## Sistema de caché

### Namespaces y TTLs

| Namespace     | TTL           | Qué guarda |
|---------------|---------------|-----------|
| `tmdb`        | 1 día         | Detalles de película por ID o búsqueda |
| `similar`     | 1 día         | Films similares por tmdb_id (sin filtro de usuario — es global) |
| `streaming`   | 6h (hit) / 2h (miss) | Disponibilidad de streaming |
| `user_scrape` | 30 min (fresh) / 7 días (stale) | Perfil completo de un usuario |

### API

```python
from cache import cache
cache.get('tmdb', 'key')          # → valor o None
cache.set('tmdb', 'key', val, ttl=ONE_DAY)
```

La caché de `similar` es **global** (no contiene exclusiones por usuario). El filtrado de films ya vistos ocurre en `services/recommender.py` después de leer la caché.

---

## SSE — Server-Sent Events

Cada request a `/api/recommend` genera un `request_id` (UUID). Tres streams paralelos:

- `/api/logs-stream?request_id=<id>` — logs del pipeline
- `/api/recommendations-stream?request_id=<id>` — recomendaciones individuales según llegan
- `/api/status-stream?request_id=<id>` — qué film semilla se está procesando ahora

Los streams terminan cuando reciben `{"status": "complete"}`. Las colas viven en `sse.REQUEST_STREAMS` y se evictan automáticamente tras `STREAM_MAX_AGE_S` (default: 1h) de inactividad.

El `QueueHandler` en `sse.py` intercepta todos los logs de Python y los enruta a la cola `logs` del `request_id` activo en `flask.g`.

---

## Circuit breaker (IncidentTracker)

Vive en `infra/http.py`. Se abre tras `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` (default: 5) fallos consecutivos de scraping, y bloquea nuevos intentos durante `LETTERBOXD_CIRCUIT_COOLDOWN_S` (default: 180s).

Cuando el circuito está abierto, `get_all_rated_films` sirve el perfil desde la caché stale (7 días). La respuesta de la API incluye `data_freshness: "stale_cache"` y un objeto `incident`.

---

## Variables de entorno

| Variable | Requerida | Default | Descripción |
|----------|-----------|---------|-------------|
| `TMDB_KEY` | **Sí** | — | API key v3 (`api_key=`) o Bearer token v4 (`eyJ...`); auto-detectado |
| `REDIS_URL` | No | — | Ej: `redis://localhost:6379` |
| `RATELIMIT_STORAGE_URI` | No | `REDIS_URL` si existe, sino `memory://` | Backend para Flask-Limiter (memory:// es por proceso) |
| `PORT` | No | `8080` | Puerto del servidor |
| `FLASK_ENV` | No | `production` | `development` activa debug logs y export de JSON |
| `LOCAL_DEV` | No | — | `true` equivale a `FLASK_ENV=development` |
| `MIN_RECOMMEND_RATING` | No | `7.0` | Rating TMDB mínimo para incluir en recomendaciones |
| `LETTERBOXD_CIRCUIT_FAILURE_THRESHOLD` | No | `5` | Fallos consecutivos para abrir el circuito |
| `LETTERBOXD_CIRCUIT_COOLDOWN_S` | No | `180` | Segundos de cooldown del circuit breaker |
| `CACHE_MAX_SIZE` | No | `10000` | Máximo de entradas en `_ExpiringDict` antes de evicción LRU |
| `STREAM_MAX_AGE_S` | No | `3600` | Tiempo hasta evictar streams SSE huérfanos |
| `SSE_QUEUE_MAXSIZE` | No | `1000` | Máximo de mensajes por cola SSE (drop-oldest al desbordar) |
| `SCRAPE_POOL_SIZE` | No | `6` | Threads del pool compartido de scraping (`executors.py`, por proceso) |
| `WORK_POOL_SIZE` | No | `8` | Threads del pool compartido de enriquecimiento/recomendación (`executors.py`, por proceso) |
| `PIPELINE_POOL_SIZE` | No | `4` | Jobs asíncronos de recomendación concurrentes por proceso |
| `JOB_RESULT_TTL` | No | `900` | Segundos que el resultado de `/api/result` queda disponible |
| `LETTERBOXD_HTTP_TIMEOUT` | No | `12` | Timeout (s) de requests a Letterboxd (requests/cloudscraper/curl_cffi) |
| `TMDB_HTTP_TIMEOUT` | No | `12` | Timeout (s) de requests a TMDB |
| `CAMOUFOX_TIMEOUT` | No | `20` | Timeout (s) de carga de página del fallback camoufox |
| `CAMOUFOX_MAX_CONCURRENT` | No | `1` | Instancias camoufox concurrentes máximas (al saturarse: stale cache) |
| `HTTP_POOL_MAXSIZE` | No | `20` | Conexiones por pool urllib3 (por host) |
| `LETTERBOXD_RETRY_SLEEP_S` | No | `0.4` | Espera base (s) entre reintentos de scraping |
| `LETTERBOXD_THROTTLE_SLEEP_S` | No | `1.5` | Espera base (s) entre reintentos tras 429 |
| `SIMILAR_RESULTS_PER_FILM` | No | `12` | Títulos similares pedidos a TMDB por film semilla |
| `INTERNAL_TOKEN` | No | — | Bearer para proteger `/_incident-status` |

---

## Cómo correr localmente

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Crear .env con al menos TMDB_KEY=...
cp .env.example .env   # o crear manualmente

# Modo desarrollo (auto-reload, exports JSON debug)
FLASK_ENV=development python main.py

# Modo producción local
gunicorn -c gunicorn.conf.py main:app
```

Navegar a `http://localhost:8080`.

---

## Tests

```bash
pytest tests -v
```

Los tests importan todo desde `main` (que re-exporta los símbolos necesarios). Patrón de patching:

```python
import main

# Parchear la sesión HTTP (es infra.http.session, pero main la re-exporta)
with patch.object(main.session, 'get', return_value=mock_resp): ...

# Parchear caché
with patch.object(main.cache, 'get', return_value={...}): ...

# Parchear cliente interno directamente
r = main.MovieRecommender()
with patch.object(r._lb, '_safe_get', return_value=mock_resp): ...
with patch.object(r._tmdb, '_get', return_value=mock_resp): ...
with patch.object(r._streaming, 'get_by_tmdb_id', return_value=[]): ...
```

Al agregar tests nuevos, mantener el patrón `import main` y parchear a través de `main.<símbolo>` o directamente en los clientes internos (`r._lb`, `r._tmdb`, `r._streaming`).

---

## Convenciones de código

- **PEP 8** estricto.
- Type hints donde sea claro. No usar `Any` genérico cuando hay algo más preciso.
- Docstrings en inglés (el código existente está en inglés).
- Commits con prefijos: `Add:`, `Fix:`, `Update:`, `Docs:`, `Refactor:`.
- Logs: usar `logger.info` / `logger.warning` / `logger.error` / `logger.debug`. Nunca `print()`.
- No exponer detalles de infraestructura en mensajes de error HTTP al cliente.

---

## Endpoints API

| Método | Ruta | Rate limit | Descripción |
|--------|------|-----------|-------------|
| `GET` | `/_health` | — | Estado del sistema + snapshot del circuit breaker |
| `GET` | `/_incident-status` | 30/min | Circuit breaker detallado (protegido con `INTERNAL_TOKEN`) |
| `POST` | `/api/get_pages` | 10/min | Devuelve número de páginas del perfil |
| `POST` | `/api/recommend` | 5/min | Encola el pipeline y devuelve `202 {request_id}`; con `"sync": true` corre inline y devuelve la respuesta completa |
| `GET` | `/api/result` | 60/min | Resultado del job asíncrono: `202` pendiente, `200` payload final, `404` desconocido/expirado |
| `GET` | `/api/logs-stream` | 20/min | SSE: logs en tiempo real |
| `GET` | `/api/recommendations-stream` | 20/min | SSE: recomendaciones según llegan |
| `GET` | `/api/status-stream` | 20/min | SSE: estado del pipeline |
| `GET` | `/` | — | Landing page |
| `GET` | `/<username>` | — | Página de resultados |

**Validación de username:** `^[a-zA-Z0-9_-]{1,50}$`

---

## Cadena anti-bot para Letterboxd

Orden de fallback en `LetterboxdClient._safe_get`:
1. `requests` (sesión directa)
2. `cloudscraper` (bypass CloudFlare, si instalado)
3. `curl_cffi` con impersonación `chrome120` (si instalado)
4. `camoufox` headless Firefox (último recurso, solo si todos fallan)

Los errores 403/429/503 activan el fallback. Los 429/503 **no** están en `Retry.status_forcelist` para no amplificar requests contra servicios que están throttleando.

---

## Deployment (Railway)

```
# Procfile
web: gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 4 --timeout 180 --graceful-timeout 30 --max-requests 500 --max-requests-jitter 50 --capture-output --log-level info
```

- Workers: 2 (Railway free tier). En producción con más recursos, ajustar según CPUs.
- `gthread` permite múltiples threads por worker (necesario para SSE + ThreadPoolExecutor concurrente).
- Timeout 180s: el pipeline completo puede tardar en perfiles grandes.
- `$PORT` lo provee Railway automáticamente.
- **Capacidad real**: ~2 usuarios activos concurrentes con streams en vivo (cada usuario sostiene 3 conexiones SSE y hay 2×4=8 threads de request). Los pipelines corren en `PIPELINE_EXECUTOR` (4 por proceso) y no bloquean threads HTTP. Detalle y guía de escalado en `docs/capacity.md`.

---

## Dónde agregar qué

| Quiero agregar... | Va en... |
|-------------------|----------|
| Nueva fuente de streaming | `infra/streaming.py` → nuevo método en `StreamingClient` |
| Nuevo campo de metadata de film | `infra/tmdb.py` → `get_details_by_id` |
| Nueva estrategia de recomendación | `services/recommender.py` |
| Nuevo análisis de preferencias | `services/preferences.py` |
| Nuevo endpoint HTTP | `routes.py` (solo parsing + respuesta) |
| Nuevo país soportado | `MovieRecommender._COUNTRY_NAMES` en `recommender.py` |
| Nueva variable de entorno | `.env.example` + tabla en `README.md` + aquí |
| Nuevo test | `tests/test_<área>.py` — importar desde `main`, parchear con `patch.object` |

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **letterboxd-recommender** (914 symbols, 1873 relationships, 79 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/letterboxd-recommender/context` | Codebase overview, check index freshness |
| `gitnexus://repo/letterboxd-recommender/clusters` | All functional areas |
| `gitnexus://repo/letterboxd-recommender/processes` | All execution flows |
| `gitnexus://repo/letterboxd-recommender/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
