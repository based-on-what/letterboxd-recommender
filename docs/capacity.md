# Deployment capacity

Numbers derived from the actual code and config (`Procfile`, `gunicorn.conf.py`, `sse.py`, `routes.py`, `executors.py`) as of this commit.

## Current production config (Procfile)

```text
gunicorn --workers 2 --worker-class gthread --threads 4 --timeout 180 --max-requests 500
```

- **Request threads:** 2 workers x 4 threads = **8 concurrent HTTP requests total**. Under gthread every in-flight request, including each open SSE connection, holds one thread for its whole lifetime.
- **Pipelines:** `/api/recommend` is async (202 + `/api/result` polling). Pipelines run on `PIPELINE_EXECUTOR` (`PIPELINE_POOL_SIZE`, default 4 per process) — **8 concurrent pipelines** across 2 workers, independent of request threads. Excess jobs queue FIFO inside the executor.
- **Internal work:** per process, scraping is capped at `SCRAPE_POOL_SIZE` (6) threads and enrichment/similar work at `WORK_POOL_SIZE` (8); camoufox at `CAMOUFOX_MAX_CONCURRENT` (1) browser.

## What actually limits concurrency: SSE connections

Each active user holds **3 SSE connections** (logs, recommendations, status) plus short-lived POST/poll requests. With 8 request threads:

| Concurrent active users | SSE threads held | Remaining threads for POST/poll/static |
|---|---|---|
| 1 | 3 | 5 |
| 2 | 6 | 2 |
| 3 | 9 | **starved** |

**Sustainable: ~2 concurrent active users with live streams.** A third user's stream requests queue behind the others until something disconnects. The pipeline itself no longer blocks request threads, so results still arrive via `/api/result` even when streams cannot connect — degraded UX, not failure.

## Worker recycling (`max-requests 500`)

Each worker restarts after ~500 requests (with jitter). Open SSE streams on that worker drop at recycle. Mitigations already in the code:

- The frontend reconnects with exponential backoff (`createReconnectingStream`, cap 5 retries).
- Final results live in the `jobs` cache (`JOB_RESULT_TTL` 900 s, Redis-backed when `REDIS_URL` is set), so a dropped stream never loses the result.
- With Redis pub/sub SSE, a reconnect can land on the *other* worker and still receive messages.

## Memory per active stream

In-memory queues are bounded (`SSE_QUEUE_MAXSIZE`, default 1000, drop-oldest). Worst case per request: 3 queues x 1000 messages x ~0.2 KB ≈ **0.6 MB**, evicted after `STREAM_MAX_AGE_S` (1 h) of inactivity. With Redis pub/sub there is no per-message buffering in the worker at all.

## Scaling guidance

1. **First: set `REDIS_URL`.** It switches on cross-worker SSE delivery, a shared circuit breaker, shared outbound rate limiters, shared Flask-Limiter storage, and cross-worker job results. Without it, anything beyond 1 worker degrades correctness, not just capacity.
2. **Add threads before workers** for SSE headroom: connections are idle-waiting I/O, so `--threads 8` doubles sustainable users (~4-5) with negligible CPU cost. Keep `WORK_POOL_SIZE`/`SCRAPE_POOL_SIZE` in mind: total outbound concurrency is per process.
3. **Add workers** when CPU-bound (HTML parsing during scrapes) — only after Redis is in place (see 1). Outbound rate limiters are shared via Redis, so adding workers does not multiply pressure on TMDB/Letterboxd.
4. **Raise or drop `--max-requests`** once memory is observed stable (the historical leak motivations — unbounded queues and per-request pools — were fixed). Recycling every 500 requests mostly costs SSE reconnects now.
5. **Beyond ~10 concurrent users:** move pipelines to Celery/RQ (Redis broker; `PIPELINE_EXECUTOR` is documented as the interim step) and serve SSE from an async worker (e.g. uvicorn/ASGI) so connections stop costing a thread each.
