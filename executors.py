"""
executors.py — Process-wide bounded thread pools.

Per-request ThreadPoolExecutors let N concurrent requests spawn N*workers
threads. These two shared pools cap total thread count regardless of
request concurrency: one for Letterboxd page scraping, one for TMDB
enrichment / recommendation work.
"""

import os
from concurrent.futures import ThreadPoolExecutor

SCRAPE_POOL_SIZE = int(os.getenv('SCRAPE_POOL_SIZE', '6'))
WORK_POOL_SIZE = int(os.getenv('WORK_POOL_SIZE', '8'))

SCRAPE_EXECUTOR = ThreadPoolExecutor(max_workers=SCRAPE_POOL_SIZE, thread_name_prefix='scrape')
WORK_EXECUTOR = ThreadPoolExecutor(max_workers=WORK_POOL_SIZE, thread_name_prefix='work')
