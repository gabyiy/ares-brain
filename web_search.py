import re
import requests
from typing import List
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (ARES assistant)"

# ========================= WEATHER VIA WTTR =========================

def _weather_api(query: str) -> str:
    """
    Extract city from query and get weather from wttr.in (no API key required).
    """
    words = query.split()
    if len(words) < 2:
        return None

    # naive city guess: last 1–3 words
    location = " ".join(words[-3:])

    url = f"https://wttr.in/{location}?format=j1"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
    except Exception:
        return None

    try:
        tomorrow = data["weather"][1]
        maxt = tomorrow["maxtempC"]
        mint = tomorrow["mintempC"]
        cond = tomorrow["hourly"][4]["weatherDesc"][0]["value"]
        return f"Tomorrow in {location.capitalize()}: {cond}, high {maxt}°C, low {mint}°C."
    except Exception:
        return None

def _is_weather_query(query: str) -> bool:
    q = query.lower()
    return any(w in q for w in ["weather", "forecast", "temperature", "meteo"])


# ========================= GENERIC SEARCH =========================

def _fetch_html_duckduckgo(query: str) -> str:
    url = "https://duckduckgo.com/html/?q=" + requests.utils.quote(query)
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    txt = resp.text.lower()
    if "captcha" in txt or "are you human" in txt:
        raise RuntimeError("HUMAN_CHECK")
    return resp.text


def _extract_snippets(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    snippets = []

    blocked = ["cookie", "privacy", "login", "javascript", "past day"]

    for tag in soup.find_all(["p", "div", "span"]):
        t = tag.get_text(" ", strip=True)
        if not t:
            continue
        low = t.lower()
        if any(b in low for b in blocked):
            continue
        if 30 < len(t) < 250:
            snippets.append(t)
        if len(snippets) >= 5:
            break

    return snippets


# ========================= SCORE EXTRACTOR =========================

_SCORE = re.compile(r"\b(\d+\s*[-–]\s*\d+)\b")

def _summarise_score(snippets: List[str]) -> str:
    for s in snippets:
        m = _SCORE.search(s)
        if m:
            return f"The final score was {m.group(1)}."
    return "I couldn't find the match result."


def _is_score_query(query: str) -> bool:
    q = query.lower()
    return any(w in q for w in ["score", "result", "match", "who won"])


# ========================= GENERIC SUMMARY =========================

def _generic_summary(snippets: List[str]) -> str:
    if not snippets:
        return "I couldn't find a clear answer."
    t = snippets[0]
    t = re.sub(r'https?://\S+', '', t)
    if "|" in t:
        t = t.split("|")[0]
    if len(t) > 200:
        t = t[:200].rsplit(" ", 1)[0] + "..."
    return t


# ========================= MAIN LOGIC =========================

def search_and_summarise(query: str) -> str:

    # 1. Weather first (best accuracy)
    if _is_weather_query(query):
        w = _weather_api(query)
        if w:
            return w

    # 2. Web search
    try:
        html = _fetch_html_duckduckgo(query)
    except Exception:
        return "Search engine blocked the request. Try again later."

    snippets = _extract_snippets(html)

    # 3. Scores
    if _is_score_query(query):
        return _summarise_score(snippets)

    # 4. Default
    return _generic_summary(snippets)
