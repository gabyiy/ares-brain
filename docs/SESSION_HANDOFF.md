# ARES Session Handoff

Repo:
https://github.com/gabyiy/ares-brain

Local path:
~/ares-brain

Python:
Use virtual environment:
source .venv/bin/activate

Current status:
- GitHub repo working and pushed.
- README updated with project purpose and roadmap.
- Python .venv created.
- requirements.txt added.
- Central rate-limited HTTP client added:
  network/http_client.py
- TTL cache added:
  network/cache.py
- Wikipedia provider added and tested:
  network/providers/wikipedia.py
  network/providers/_test_wikipedia.py

Important rule:
Run provider tests from repo root with module mode:
python -m network.providers._test_wikipedia

Do NOT run provider files directly.

Current milestone:
Network provider layer.

Wikipedia status:
PASSED.
- Summary works through Wikipedia REST API.
- Search works through MediaWiki API.
- Uses RateLimitedHttpClient.
- Uses TTLCache.

Next task:
Add Weather provider using Open-Meteo.

Planned provider order:
1. Wikipedia - DONE
2. Weather - Open-Meteo
3. Geocoding - Nominatim or Open-Meteo geocoding
4. Stocks - Stooq
5. Crypto - CoinGecko
6. News - GDELT / RSS
7. Football - free football API / TheSportsDB / football-data.org
8. Time / timezone - WorldTimeAPI

Development rule:
One feature at a time.
Create file -> test -> commit -> push.
No direct requests outside network/http_client.py.
No hardcoded secrets.
No leaked tokens.

Standard git workflow:
git status
git add -A
git commit -m "Clear commit message"
git push

Next instruction for ChatGPT:
Continue from Weather provider using the existing HTTP client and cache pattern.
