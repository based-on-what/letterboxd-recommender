"""
executors.py — Process-wide bounded thread pools.

Per-request ThreadPoolExecutors let N concurrent requests spawn N*workers
threads. These two shared pools cap total thread count regardless of
request concurrency: one for Letterboxd page scraping, one for TMDB
enrichment / recommendation work.
"""

import contextvars
import os
from concurrent.futures import ThreadPoolExecutor

SCRAPE_POOL_SIZE = int(os.getenv('SCRAPE_POOL_SIZE', '6'))
WORK_POOL_SIZE = int(os.getenv('WORK_POOL_SIZE', '8'))
PIPELINE_POOL_SIZE = int(os.getenv('PIPELINE_POOL_SIZE', '4'))

SCRAPE_EXECUTOR = ThreadPoolExecutor(max_workers=SCRAPE_POOL_SIZE, thread_name_prefix='scrape')
WORK_EXECUTOR = ThreadPoolExecutor(max_workers=WORK_POOL_SIZE, thread_name_prefix='work')
# Whole-pipeline jobs (async /api/recommend). Separate pool: pipeline tasks
# submit into WORK_EXECUTOR/SCRAPE_EXECUTOR, sharing one pool would deadlock.
# Next step beyond an in-process pool: Celery/RQ with a Redis broker.
PIPELINE_EXECUTOR = ThreadPoolExecutor(max_workers=PIPELINE_POOL_SIZE, thread_name_prefix='pipeline')


def submit_with_context(executor: ThreadPoolExecutor, fn, *args, **kwargs):
    """Submit *fn* carrying the caller's contextvars into the worker thread.

    Pool threads do not inherit context, so without this the per-request
    request_id (sse.REQUEST_ID_CTX) is lost and worker logs never reach the
    SSE logs stream.
    """
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)
