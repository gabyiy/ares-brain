import json
import os
import re
import time
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests

# ============================================================
# ARES Online Web Helper (API-first, cache-first)
# - No scraping of Google pages.
# - Uses free public APIs (no keys) + Wikipedia.
# - 1-hour cache to avoid repeating requests.
# ============================================================

USER_AGENT = "ARES-assistant/1.0 (+local)"
CACHE_TTL_SECONDS = 3600  # 1 hour
CACHE_PATH = os.path.expanduser("~/.ares_web_cache.json")

# Global request pacing (prevents bursts)
GLOBAL_MIN_SECONDS_BETWEEN_REQUESTS = 1.2
_last_request_ts = 0.0

# Per-host pacing (extra safety)
_host_last_ts: Dict[str, float] = {}
HOST_MIN_SECONDS = {
    "wikipedia.org": 1.0,
    "api.duckduckgo.com": 1.0,
    "wttr.in": 1.0,
    "api.open-meteo.com": 1.0,
    "geocoding-api.open-meteo.com": 1.0,
    "nominatim.openstreetmap.org": 1.5,
    "hn.algolia.com": 0.8,
    "api.stackexchange.com": 0.8,
    "api.github.com": 1.0,
    "openlibrary.org": 0.8,
    "api.crossref.org": 0.8,
    "export.arxiv.org": 0.8,
    "api.coingecko.com": 1.2,
    "api.exchangerate.host": 0.8,
    "api.tvmaze.com": 0.8,
}

DEFAULT_TIMEOUT = 10


# ----------------------------
# Cache
# ----------------------------
def _load_cache() -> Dict[str, Any]:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _normalize_query(q: str) -> str:
    q = q.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q


def _cache_get(q: str) -> Optional[str]:
    cache = _load_cache()
    key = _normalize_query(q)
    item = cache.get(key)
    if not item:
        return None
    ts = item.get("ts", 0)
    if time.time() - ts > CACHE_TTL_SECONDS:
        # expired
        cache.pop(key, None)
        _save_cache(cache)
        return None
    return item.get("answer")


def _cache_set(q: str, answer: str) -> None:
    cache = _load_cache()
    key = _normalize_query(q)
    cache[key] = {"ts": time.time(), "answer": answer}
    _save_cache(cache)


# ----------------------------
# HTTP helpers (rate limiting + backoff)
# ----------------------------
def _sleep_if_needed(host: str) -> None:
    global _last_request_ts

    now = time.time()
    # global pacing
    dt_global = now - _last_request_ts
    if dt_global < GLOBAL_MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(GLOBAL_MIN_SECONDS_BETWEEN_REQUESTS - dt_global)

    # host pacing
    min_host = HOST_MIN_SECONDS.get(host, 1.0)
    last = _host_last_ts.get(host, 0.0)
    dt_host = now - last
    if dt_host < min_host:
        time.sleep(min_host - dt_host)


def _request_json(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Optional[dict]:
    host = re.sub(r"^https?://", "", url).split("/")[0]
    _sleep_if_needed(host)

    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)

    # small backoff retries for transient errors
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=h, timeout=DEFAULT_TIMEOUT)
            global _last_request_ts
            _last_request_ts = time.time()
            _host_last_ts[host] = _last_request_ts

            if resp.status_code in (429, 503, 502, 500):
                time.sleep((attempt + 1) * 1.5)
                continue

            resp.raise_for_status()
            return resp.json()
        except Exception:
            time.sleep((attempt + 1) * 0.8)
    return None


def _request_text(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Optional[str]:
    host = re.sub(r"^https?://", "", url).split("/")[0]
    _sleep_if_needed(host)

    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=h, timeout=DEFAULT_TIMEOUT)
            global _last_request_ts
            _last_request_ts = time.time()
            _host_last_ts[host] = _last_request_ts

            if resp.status_code in (429, 503, 502, 500):
                time.sleep((attempt + 1) * 1.5)
                continue

            resp.raise_for_status()
            return resp.text
        except Exception:
            time.sleep((attempt + 1) * 0.8)
    return None


# ----------------------------
# Query intent detection
# ----------------------------
def _is_weather_query(q: str) -> bool:
    ql = q.lower()
    return any(k in ql for k in ["weather", "temperature", "forecast", "meteo", "how hot", "how cold", "rain", "sunny", "snow"])


def _is_score_query(q: str) -> bool:
    ql = q.lower()
    return any(k in ql for k in ["score", "result", "last match", "final score", "vs", "match recap"])


def _is_currency_query(q: str) -> bool:
    ql = q.lower()
    return any(k in ql for k in ["exchange rate", "convert", "usd to", "eur to", "gbp to", "ron to"])


def _is_crypto_query(q: str) -> bool:
    ql = q.lower()
    return any(k in ql for k in ["btc", "bitcoin", "eth", "ethereum", "solana", "doge", "crypto price"])


# ----------------------------
# Weather (no-key APIs)
# Sources:
# - wttr.in (simple)
# - Open-Meteo (reliable forecast, no key)
# ----------------------------
def _extract_location_from_query(q: str) -> str:
    # crude: take last 1-4 words after "in" or end
    q = q.strip()
    m = re.search(r"\b(in|at|for)\s+([a-zA-Z\u00C0-\u024F\s\-]+)$", q, re.IGNORECASE)
    if m:
        loc = m.group(2).strip()
        return loc
    # fallback: last 3 words
    words = re.sub(r"[^\w\s\-]", " ", q).split()
    return " ".join(words[-3:]) if len(words) >= 2 else q


def _weather_wttr(location: str) -> Optional[str]:
    # wttr.in supports JSON with ?format=j1
    url = f"https://wttr.in/{location}"
    data = _request_json(url, params={"format": "j1"})
    if not data:
        return None
    try:
        # weather[1] often corresponds to "tomorrow" (0=today)
        weather = data.get("weather", [])
        idx = 1 if len(weather) > 1 else 0
        day = weather[idx]
        maxt = day.get("maxtempC")
        mint = day.get("mintempC")
        hourly = day.get("hourly", [])
        desc = None
        if hourly:
            desc = hourly[len(hourly)//2].get("weatherDesc", [{}])[0].get("value")
        if not desc:
            desc = "Forecast available"
        return f"{location}: {desc}. High {maxt}°C, low {mint}°C."
    except Exception:
        return None


def _open_meteo_geocode(location: str) -> Optional[Tuple[float, float, str]]:
    url = "https://geocoding-api.open-meteo.com/v1/search"
    js = _request_json(url, params={"name": location, "count": 1, "language": "en", "format": "json"})
    if not js or "results" not in js or not js["results"]:
        return None
    r = js["results"][0]
    lat = float(r["latitude"])
    lon = float(r["longitude"])
    name = f'{r.get("name","")}, {r.get("country","")}'.strip().strip(",")
    return lat, lon, name


def _weather_open_meteo(location: str) -> Optional[str]:
    geo = _open_meteo_geocode(location)
    if not geo:
        return None
    lat, lon, nice = geo
    url = "https://api.open-meteo.com/v1/forecast"
    js = _request_json(url, params={
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
        "timezone": "auto"
    })
    if not js:
        return None
    try:
        daily = js["daily"]
        # tomorrow index = 1 if exists
        idx = 1 if len(daily["time"]) > 1 else 0
        tmax = daily["temperature_2m_max"][idx]
        tmin = daily["temperature_2m_min"][idx]
        pop = daily.get("precipitation_probability_max", [None])[idx]
        code = daily.get("weathercode", [None])[idx]
        # Minimal weathercode mapping (enough for human usefulness)
        code_map = {
            0: "Clear",
            1: "Mostly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Fog",
            48: "Fog",
            51: "Light drizzle",
            53: "Drizzle",
            55: "Heavy drizzle",
            61: "Light rain",
            63: "Rain",
            65: "Heavy rain",
            71: "Light snow",
            73: "Snow",
            75: "Heavy snow",
            80: "Rain showers",
            81: "Showers",
            82: "Heavy showers",
            95: "Thunderstorm",
        }
        desc = code_map.get(code, "Forecast")
        if pop is None:
            return f"{nice}: {desc}. High {tmax}°C, low {tmin}°C."
        return f"{nice}: {desc}. High {tmax}°C, low {tmin}°C. Rain chance {pop}%."
    except Exception:
        return None


def _answer_weather(query: str) -> Optional[str]:
    loc = _extract_location_from_query(query)
    # Prefer Open-Meteo (stable), fallback to wttr
    ans = _weather_open_meteo(loc)
    if ans:
        return ans
    return _weather_wttr(loc)


# ----------------------------
# Wikipedia (best general fallback)
# ----------------------------
def _wiki_summary(query: str) -> Optional[str]:
    # Wikipedia REST summary endpoint (no key)
    # First search, then summary.
    search = _request_json(
        "https://en.wikipedia.org/w/rest.php/v1/search/title",
        params={"q": query, "limit": 1},
        headers={"Accept": "application/json"}
    )
    if not search or not search.get("pages"):
        return None
    title = search["pages"][0].get("title")
    if not title:
        return None

    # Summary
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title)
    summ = _request_json(url, headers={"Accept": "application/json"})
    if not summ:
        return None

    extract = summ.get("extract")
    if not extract:
        return None

    # keep it short and useful
    extract = re.sub(r"\s+", " ", extract).strip()
    if len(extract) > 420:
        extract = extract[:420].rsplit(" ", 1)[0] + "..."
    return f"{title}: {extract}"


# ----------------------------
# DuckDuckGo Instant Answer (no key)
# ----------------------------
def _ddg_instant_answer(query: str) -> Optional[str]:
    js = _request_json("https://api.duckduckgo.com/", params={
        "q": query,
        "format": "json",
        "no_redirect": 1,
        "no_html": 1,
        "skip_disambig": 1
    })
    if not js:
        return None

    # direct answer
    ans = js.get("Answer")
    if ans:
        ans = re.sub(r"\s+", " ", ans).strip()
        return ans

    # abstract
    abs_text = js.get("AbstractText")
    heading = js.get("Heading")
    if abs_text:
        abs_text = re.sub(r"\s+", " ", abs_text).strip()
        if len(abs_text) > 420:
            abs_text = abs_text[:420].rsplit(" ", 1)[0] + "..."
        return f"{heading + ': ' if heading else ''}{abs_text}"

    return None


# ----------------------------
# Extra free APIs (no keys)
# 1) Hacker News (Algolia)
# 2) StackExchange
# 3) GitHub repos
# 4) OpenLibrary
# 5) Crossref
# 6) arXiv
# 7) CoinGecko
# 8) exchangerate.host
# 9) TVMaze
# ----------------------------
def _hn_search(query: str) -> Optional[str]:
    js = _request_json("https://hn.algolia.com/api/v1/search", params={"query": query, "hitsPerPage": 1})
    if not js or not js.get("hits"):
        return None
    h = js["hits"][0]
    title = h.get("title") or h.get("story_title")
    url = h.get("url") or h.get("story_url")
    if not title:
        return None
    return f"HN: {title}" + (f" ({url})" if url else "")


def _stackexchange_search(query: str) -> Optional[str]:
    js = _request_json("https://api.stackexchange.com/2.3/search/advanced", params={
        "order": "desc",
        "sort": "relevance",
        "q": query,
        "site": "stackoverflow",
        "pagesize": 1
    })
    if not js or not js.get("items"):
        return None
    it = js["items"][0]
    title = it.get("title")
    link = it.get("link")
    if not title:
        return None
    return f"StackOverflow: {title}" + (f" ({link})" if link else "")


def _github_repo_search(query: str) -> Optional[str]:
    js = _request_json("https://api.github.com/search/repositories", params={"q": query, "per_page": 1})
    if not js or not js.get("items"):
        return None
    it = js["items"][0]
    name = it.get("full_name")
    desc = it.get("description") or ""
    if desc:
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 220:
            desc = desc[:220].rsplit(" ", 1)[0] + "..."
    return f"GitHub: {name} — {desc}".strip(" —")


def _openlibrary_search(query: str) -> Optional[str]:
    js = _request_json("https://openlibrary.org/search.json", params={"q": query, "limit": 1})
    if not js or not js.get("docs"):
        return None
    d = js["docs"][0]
    title = d.get("title")
    author = (d.get("author_name") or [""])[0]
    year = d.get("first_publish_year")
    if not title:
        return None
    bits = [title]
    if author:
        bits.append(f"by {author}")
    if year:
        bits.append(f"({year})")
    return "OpenLibrary: " + " ".join(bits)


def _crossref_search(query: str) -> Optional[str]:
    js = _request_json("https://api.crossref.org/works", params={"query": query, "rows": 1})
    if not js:
        return None
    items = js.get("message", {}).get("items", [])
    if not items:
        return None
    it = items[0]
    title = (it.get("title") or [""])[0]
    doi = it.get("DOI")
    if not title:
        return None
    return f"Crossref: {title}" + (f" (DOI: {doi})" if doi else "")


def _arxiv_search(query: str) -> Optional[str]:
    # arXiv API returns Atom XML, but we can grab the first <title> after the feed title.
    url = "https://export.arxiv.org/api/query"
    txt = _request_text(url, params={"search_query": f"all:{query}", "start": 0, "max_results": 1})
    if not txt:
        return None
    titles = re.findall(r"<title>(.*?)</title>", txt, flags=re.DOTALL)
    if len(titles) < 2:
        return None
    paper_title = re.sub(r"\s+", " ", titles[1]).strip()
    if paper_title:
        return f"arXiv: {paper_title}"
    return None


def _coingecko_price(query: str) -> Optional[str]:
    # very simple: if query mentions a coin, try common ones
    ql = query.lower()
    coin_map = {
        "bitcoin": "bitcoin",
        "btc": "bitcoin",
        "ethereum": "ethereum",
        "eth": "ethereum",
        "solana": "solana",
        "sol": "solana",
        "dogecoin": "dogecoin",
        "doge": "dogecoin",
    }
    coin = None
    for k, v in coin_map.items():
        if re.search(rf"\b{k}\b", ql):
            coin = v
            break
    if not coin:
        return None
    js = _request_json("https://api.coingecko.com/api/v3/simple/price", params={"ids": coin, "vs_currencies": "usd,eur"})
    if not js or coin not in js:
        return None
    usd = js[coin].get("usd")
    eur = js[coin].get("eur")
    return f"{coin.title()} price: ${usd} / €{eur}"


def _exchange_rate(query: str) -> Optional[str]:
    # naive pattern: "usd to eur", "convert 100 usd to eur"
    ql = query.lower()
    m = re.search(r"\b([0-9]+(\.[0-9]+)?)\s*([a-z]{3})\s*(to|in)\s*([a-z]{3})\b", ql)
    amount = 1.0
    base = None
    quote = None
    if m:
        amount = float(m.group(1))
        base = m.group(3).upper()
        quote = m.group(5).upper()
    else:
        m2 = re.search(r"\b([a-z]{3})\s*(to|in)\s*([a-z]{3})\b", ql)
        if not m2:
            return None
        base = m2.group(1).upper()
        quote = m2.group(3).upper()

    js = _request_json("https://api.exchangerate.host/convert", params={"from": base, "to": quote, "amount": amount})
    if not js or "result" not in js:
        return None
    result = js["result"]
    return f"{amount:g} {base} ≈ {result:.4g} {quote}"


def _tvmaze_search(query: str) -> Optional[str]:
    js = _request_json("https://api.tvmaze.com/search/shows", params={"q": query})
    if not js:
        return None
    if isinstance(js, list) and js:
        show = js[0].get("show", {})
        name = show.get("name")
        premiered = show.get("premiered")
        rating = (show.get("rating") or {}).get("average")
        if not name:
            return None
        bits = [name]
        if premiered:
            bits.append(f"(premiered {premiered})")
        if rating:
            bits.append(f"rating {rating}")
        return "TVMaze: " + " — ".join(bits)
    return None


# ----------------------------
# Main entry
# ----------------------------
def search_and_summarise(query: str) -> str:
    # 0) 1-hour cache
    cached = _cache_get(query)
    if cached:
        return cached

    q = query.strip()
    if not q:
        return "Ask me something."

    # 1) Weather (API-first)
    if _is_weather_query(q):
        ans = _answer_weather(q)
        if ans:
            _cache_set(query, ans)
            return ans

    # 2) Currency
    if _is_currency_query(q):
        ans = _exchange_rate(q)
        if ans:
            _cache_set(query, ans)
            return ans

    # 3) Crypto
    if _is_crypto_query(q):
        ans = _coingecko_price(q)
        if ans:
            _cache_set(query, ans)
            return ans

    # 4) Wikipedia for general knowledge (best default)
    ans = _wiki_summary(q)
    if ans:
        _cache_set(query, ans)
        return ans

    # 5) DuckDuckGo Instant Answer (no key)
    ans = _ddg_instant_answer(q)
    if ans:
        _cache_set(query, ans)
        return ans

    # 6) “special” tech/news sources (still no keys)
    # These return useful "top result" style answers without scraping.
    candidates = [
        _stackexchange_search,
        _github_repo_search,
        _hn_search,
        _openlibrary_search,
        _crossref_search,
        _arxiv_search,
        _tvmaze_search,
    ]

    for fn in candidates:
        try:
            out = fn(q)
            if out:
                _cache_set(query, out)
                return out
        except Exception:
            continue

    # Final fallback
    msg = "I couldn't find a solid answer via free APIs. Try rephrasing the question."
    _cache_set(query, msg)
    return msg
