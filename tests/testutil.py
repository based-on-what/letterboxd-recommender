"""Shared test helpers for the tests package."""

from unittest.mock import Mock


def _resp(payload, status=200, text=''):
    m = Mock()
    m.status_code = status
    m.json.return_value = payload
    m.text = text
    return m


class _FakeRedisKV:
    """Minimal dict-backed redis stand-in for circuit-breaker tests."""

    def __init__(self):
        self.store = {}
        self.expiry = {}

    def incr(self, k):
        v = int(self.store.get(k, 0)) + 1
        self.store[k] = v
        return v

    def set(self, k, v, ex=None):
        self.store[k] = v
        if ex is not None:
            self.expiry[k] = ex

    def get(self, k):
        v = self.store.get(k)
        return str(v) if v is not None else None

    def delete(self, k):
        self.store.pop(k, None)

    def exists(self, k):
        return 1 if k in self.store else 0

    def ttl(self, k):
        if k not in self.store:
            return -2
        return self.expiry.get(k, -1)
