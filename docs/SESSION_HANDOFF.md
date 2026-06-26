# ARES Session Handoff

Last Updated: 2026-06-26

---

# Project

ARES (Autonomous Reasoning & Exploration System)

Repository:
https://github.com/gabyiy/ares-brain

Goal:

Build a modular, offline-first AI assistant capable of reasoning, memory, voice conversation, vision and internet information retrieval while running on Raspberry Pi today and Jetson Orin in the future.

---

# Current Architecture

core/
    intent_router.py

interfaces/
    text_repl.py

network/
    http_client.py
    cache.py

network/providers/
    wikipedia.py
    weather.py

docs/
    SESSION_HANDOFF.md

---

# Working Features

✓ Clean modular text interface

✓ Wake mode

hello ares

✓ Sleep mode

goodbye ares

✓ Wikipedia summary

wiki Raspberry Pi

✓ Wikipedia search

search wikipedia Apollo program

✓ Live weather

weather madrid

Weather uses:

- Open-Meteo Geocoding API
- Open-Meteo Weather API

Current weather returns:

- City
- Country
- Temperature
- Humidity
- Wind speed

---

# Infrastructure

HTTP Client

- Timeout protection
- Rate limiting

Cache

- TTL cache
- Prevent duplicate requests

Intent Router

Responsible only for:

- Wake state
- Sleep state
- Intent detection
- Provider routing

Providers contain all external API logic.

---

# Commands

hello ares

goodbye ares

wiki <topic>

search wikipedia <topic>

weather <city>

---

# Bash Shortcut

ARES starts using:

ares

Alias configured inside:

~/.bashrc

---

# Repository Status

Repository cleaned.

Old text_chat.py removed.

Legacy text system removed.

Clean text interface implemented.

Wikipedia provider completed.

Weather provider completed.

GitHub synchronized.

Working tree clean.

---

# Next Milestones

1. Football provider

Commands:

football Barcelona

football Real Madrid

league Premier League

2. Stock provider

Examples:

stock Rheinmetall

stock NVIDIA

stock Tesla

3. News provider

news AI

news Europe

news defense

4. Currency provider

usd eur

btc eur

5. Voice interface

6. Memory integration

7. Camera integration

8. Local LLM

---

# Long-Term Vision

ARES will become:

- Personal AI assistant
- Voice companion
- Robot brain
- Home automation controller
- Vision system
- Long-term memory
- Planner
- Research assistant

Future hardware:

Raspberry Pi 5

↓

Jetson Orin

↓

Mobile robot

---

# Development Rules

Always keep the architecture modular.

IntentRouter never performs API calls.

Every external service must be implemented as its own Provider.

Reuse the shared HTTP client.

Reuse the shared cache.

Keep features independent.

---

# End of Session

Current milestone:

✓ Modular Text Interface

✓ Wikipedia Provider

✓ Weather Provider

ARES is now capable of answering live internet questions through modular providers.

Next development session begins with:

Football Provider.
