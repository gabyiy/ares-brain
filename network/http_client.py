import time
import random
import requests
from threading import Lock


class RateLimitedHttpClient:
    """
    Central choke-point for ALL external HTTP.
    Features:
      - global minimum delay between calls (anti-flood)
      - optional per-service delay (anti-ban)
      - retries with exponential backoff for 429/5xx
      - respects Retry-After header
      - JSON helper
      - thread-safe
    """

    def __init__(
        self,
        min_delay: float = 2.0,
        timeout: int = 10,
        max_retries: int = 3,
        user_agent: str = "ARES/1.0 (contact: you@example.com)",
    ):
        self.min_delay = float(min_delay)
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)

        self._last_request = 0.0
        self._lock = Lock()

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def _sleep_if_needed(self, extra_delay: float = 0.0):
        now = time.time()
        elapsed = now - self._last_request
        needed = self.min_delay + max(0.0, extra_delay)

        if elapsed < needed:
            time.sleep(needed - elapsed)

    def get(self, url: str, params=None, headers=None, service_delay: float = 0.0):
        with self._lock:
            self._sleep_if_needed(extra_delay=service_delay)

            last_err = None
            for attempt in range(self.max_retries + 1):
                try:
                    resp = self._session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=self.timeout,
                    )

                    self._last_request = time.time()

                    # Handle rate limit
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after and retry_after.isdigit() else (2.0 * (attempt + 1))
                        time.sleep(wait)
                        continue

                    # Retry transient server errors
                    if 500 <= resp.status_code <= 599:
                        backoff = (2 ** attempt) + random.uniform(0.0, 0.5)
                        time.sleep(backoff)
                        continue

                    resp.raise_for_status()
                    return resp

                except requests.RequestException as e:
                    self._last_request = time.time()
                    last_err = e
                    backoff = (2 ** attempt) + random.uniform(0.0, 0.5)
                    time.sleep(backoff)

            raise RuntimeError(f"HTTP failed after retries for {url}: {last_err}")

    def get_json(self, url: str, params=None, headers=None, service_delay: float = 0.0):
        resp = self.get(url, params=params, headers=headers, service_delay=service_delay)
        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Invalid JSON from {url}: {e}")
