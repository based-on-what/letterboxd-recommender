"""
sse.py — Server-Sent Events stream management.

Provides:
- Per-request stream registries (logs, recommendations, status queues).
- QueueHandler: routes Python log records into the SSE logs queue.
- Helper functions for connection tracking and stream cleanup.
"""

import os
import time
import queue
import logging
from threading import Lock

try:
    from flask import g
except ImportError:  # outside Flask context (tests that don't need SSE)
    g = None  # type: ignore

logger = logging.getLogger("letterboxd-recommender")

# ---------------------------------------------------------------------------
# Stream registry
# ---------------------------------------------------------------------------
REQUEST_STREAMS: dict = {}
REQUEST_STREAMS_LOCK = Lock()
STREAM_MAX_AGE_S = int(os.getenv('STREAM_MAX_AGE_S', '3600'))  # evict orphaned entries after 1h

_STREAM_EVICTION_INTERVAL_S = 30.0  # scan for stale entries at most this often
_last_stream_eviction_s = 0.0       # guarded by REQUEST_STREAMS_LOCK


def _get_or_create_streams(request_id: str) -> dict:
    """Return (or lazily create) the stream-queue bundle for *request_id*.

    Also runs a throttled eviction sweep to reclaim memory from orphaned
    entries (e.g. clients that disconnected without consuming their streams).
    """
    global _last_stream_eviction_s
    with REQUEST_STREAMS_LOCK:
        now = time.time()
        # Throttle the full-scan eviction so it doesn't run on every log
        # message.  With dozens of threads logging concurrently this was the
        # dominant lock-contention source.
        if now - _last_stream_eviction_s >= _STREAM_EVICTION_INTERVAL_S:
            stale = [
                rid for rid, s in REQUEST_STREAMS.items()
                if now - s.get('_created', now) > STREAM_MAX_AGE_S
            ]
            if stale:
                for rid in stale:
                    REQUEST_STREAMS.pop(rid, None)
                logger.debug(f"[stream-cleanup] evicted {len(stale)} stale stream entries")
            _last_stream_eviction_s = now

        streams = REQUEST_STREAMS.get(request_id)
        if streams is None:
            streams = {
                'logs':                    queue.Queue(),
                'recommendations':         queue.Queue(),
                'status':                  queue.Queue(),
                'logs_connected':          0,
                'recommendations_connected': 0,
                'status_connected':        0,
                'recommendations_done':    False,
                'status_done':             False,
                '_created':                now,
            }
            REQUEST_STREAMS[request_id] = streams
        return streams


def _cleanup_request_streams(request_id: str) -> None:
    with REQUEST_STREAMS_LOCK:
        REQUEST_STREAMS.pop(request_id, None)


def _mark_recommendations_done(request_id: str) -> None:
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if streams:
            streams['recommendations_done'] = True


def _mark_status_done(request_id: str) -> None:
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if streams:
            streams['status_done'] = True


def _track_stream_connection(request_id: str, stream_name: str, connected: bool) -> None:
    with REQUEST_STREAMS_LOCK:
        streams = REQUEST_STREAMS.get(request_id)
        if not streams:
            return

        counter_key = f"{stream_name}_connected"
        current = streams.get(counter_key, 0)
        streams[counter_key] = max(0, current + (1 if connected else -1))

        if (
            streams.get('recommendations_done')
            and streams.get('status_done')
            and streams.get('logs_connected', 0) == 0
            and streams.get('recommendations_connected', 0) == 0
            and streams.get('status_connected', 0) == 0
        ):
            REQUEST_STREAMS.pop(request_id, None)


# ---------------------------------------------------------------------------
# QueueHandler — routes log records into the per-request SSE logs queue
# ---------------------------------------------------------------------------
class QueueHandler(logging.Handler):
    """Custom logging handler that forwards records to the SSE logs stream."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            request_id = getattr(record, 'request_id', None)
            if not request_id and g is not None:
                try:
                    request_id = getattr(g, 'request_id', None)
                except RuntimeError:
                    request_id = None
            if request_id:
                _get_or_create_streams(request_id)['logs'].put(msg)
        except Exception:
            self.handleError(record)
