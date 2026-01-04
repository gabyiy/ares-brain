import time


class TTLCache:
    def __init__(self):
        self._store = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        value, expires_at = item
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value, ttl_seconds: int):
        self._store[key] = (value, time.time() + int(ttl_seconds))
