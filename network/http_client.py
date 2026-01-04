import time
import requests
from threading import Lock


class RateLimitedHttpClient:
    """
    Centralized HTTP client for all external network access.
    Enforces rate limiting, timeouts, and thread safety.
    """

    def __init__(self, min_delay=2.0, timeout=8):
        self.min_delay = min_delay
        self.timeout = timeout
        self._last_request = 0.0
        self._lock = Lock()

    def get(self, url, params=None, headers=None):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request

            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)

            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout
                )
                self._last_request = time.time()
                response.raise_for_status()
                return response

            except requests.RequestException as e:
                self._last_request = time.time()
                raise RuntimeError(f"HTTP error while accessing {url}: {e}")
