"""
utils.py — Shared utilities with no application-level dependencies.
"""

import os
import re
import json
import logging
import unicodedata
from functools import lru_cache

logger = logging.getLogger("letterboxd-recommender")

IS_DEV = os.getenv('FLASK_ENV') == 'development' or os.getenv('LOCAL_DEV') == 'true'


@lru_cache(maxsize=4096)
def normalize_title(title: str) -> str:
    if not title:
        return ""
    title = unicodedata.normalize('NFKD', title)
    title = ''.join(c for c in title if not unicodedata.combining(c))
    title = title.lower()
    title = re.sub(r'[^a-z0-9\s]', ' ', title)
    return re.sub(r'\s+', ' ', title).strip()


def export_debug_json(filename: str, data) -> None:
    if not IS_DEV:
        return
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Debug JSON exported: %s", filename)
    except Exception as exc:
        logger.warning("Could not export debug JSON %s: %s", filename, exc)
