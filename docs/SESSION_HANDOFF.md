# ARES Session Handoff

Last Updated: 2026-06-26

## Repository

https://github.com/gabyiy/ares-brain

## Current Stable Version

ARES v0.4

## Current Architecture

core/
    intent_router.py

interfaces/
    text_repl.py

network/
    http_client.py
    cache.py
    providers/
        wikipedia.py
        weather.py
        news.py

docs/
    SESSION_HANDOFF.md

## Working Features

- Wake command: hello ares
- Sleep command: goodbye ares
- Wikipedia summary: wiki Raspberry Pi
- Wikipedia search: search wikipedia Apollo program
- Weather: weather Madrid
- News search: news defense
- Natural news query extraction:
  - what are the latest news on rheinmetal my friend
  - I saw that rheinmetal is not doing well what are the news

## Providers

WikipediaProvider:
- Summary lookup
- Search results

WeatherProvider:
- Uses Open-Meteo
- No API key
- City geocoding
- Current temperature
- Humidity
- Wind speed

NewsProvider:
- Uses Google News RSS
- No API key
- Returns headline, source, and date
- Cleaner output than raw URLs
- Replaced GDELT because GDELT was flaky and noisy

## Current Commands

hello ares

goodbye ares

wiki <topic>

search wikipedia <topic>

weather <city>

news <topic>

Natural language examples:

what are the latest news on rheinmetal my friend

I saw that rheinmetal is not doing well what are the news

## Important Development Rules

All code changes must use the delete-and-recreate method.

Do not manually edit files with nano unless absolutely necessary.

Preferred pattern:

rm -f path/to/file.py
mkdir -p path/to/folder
cat > path/to/file.py << 'EOF'
complete file here
