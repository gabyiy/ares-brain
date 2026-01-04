# ARES – Autonomous Reasoning & Exploration System

ARES is a modular AI assistant project designed to run locally (Raspberry Pi / embedded systems),
with the long-term goal of integrating perception (audio, sensors), reasoning, memory, and external
information sources into a single coherent system.

This project is **experimental, evolving, and built step by step** with a strong focus on:
- Offline-first operation
- Clear architecture
- Extensibility
- Avoiding unnecessary cloud dependencies

---

## ✅ What is already done

### 1. Project structure
The repository is organized into clear functional modules:

- `core/` – Core logic, brain loop, decision making
- `memory/` – Long-term and short-term memory handling
- `emotion/` – Emotional state and modulation (experimental)
- `movement/` – Movement logic (for robot integration)
- `audio/` – Audio input/output handling
- `network/` – Networking, APIs, external data access
- `config/` – Configuration files
- `models/` – Local AI / speech models (e.g. Vosk)
- `logs/` – Runtime logs
- `data/` – Stored data and cache

The project is now **successfully versioned on GitHub** and can be tracked and extended cleanly.

---

### 2. Git & workflow
- Git repository is properly initialized
- `main` branch is set
- Remote GitHub repository is connected
- Full project pushed and verified
- Ready for continuous development

---

## 🚧 What we are working on RIGHT NOW

### External knowledge & information access

We are currently adding **controlled, rate-limited access to external information**, without
overloading APIs or getting blocked.

### Current focus:
- Wikipedia search & summaries
- Free public APIs (no paid dependencies)
- Proper rate limiting, caching, and request spacing

---

## 🧠 Planned integrations (next steps)

### Knowledge sources
- Wikipedia (MediaWiki API)
- General news (GDELT, The Guardian, Hacker News)

### Data APIs (free tiers)
- Weather (Open-Meteo, wttr.in)
- Football data (fixtures, scores, news – free tiers only)
- Financial markets (stocks, crypto, FX – limited polling)
- Geolocation (OpenStreetMap / Nominatim)

All APIs will be:
- Rate-limited
- Cached
- Called through a single unified network layer

---

## 🛑 Design rules (important)

- No uncontrolled API spam
- No hardcoded API keys
- No direct HTTP calls from core logic
- Everything goes through a rate-limited network layer
- Cache first, network second

---

## 🎯 Long-term vision

ARES is intended to evolve into:
- A local-first AI assistant
- A robotic control brain
- A research platform for autonomous reasoning
- A system that can be migrated from Raspberry Pi to more powerful hardware later (e.g. Orion Nano)

---

## 📌 Status

**Active development – architecture phase**

This README will be updated continuously to reflect:
- What is done
- What is being built
- What is planned next
