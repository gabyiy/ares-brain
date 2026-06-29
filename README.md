# ARES

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%205-red)
![OS](https://img.shields.io/badge/OS-Debian-green)
![Status](https://img.shields.io/badge/Status-In%20Development-orange)
![Version](https://img.shields.io/badge/Version-v0.6-blueviolet)

> Modular AI assistant designed to evolve into a personal companion and future robot brain.

---

# Current Mission

## Phase 2 — Intelligence Engine

ARES has now moved from one large router file into a modular intent system.

This makes the project cleaner, easier to extend and ready for future skills.

Progress:

Foundation        ██████████ 100%
Intelligence      █████░░░░░ 50%
Memory            ░░░░░░░░░░ 0%
Voice             ░░░░░░░░░░ 0%
Robotics          ░░░░░░░░░░ 0%

---

# Working Features

- Wake command
- Sleep command
- Weather
- News
- Natural news detection
- Natural weather detection

Examples:

hello ares
weather madrid tomorrow
weather madrid next week
news defense
what happened with bitcoin today
I heard NVIDIA is doing well
goodbye ares

---

# Current Architecture

core/
    intent_router.py

core/intents/
    greeting.py
    goodbye.py
    weather.py
    news.py
    knowledge.py

network/
    http_client.py
    cache.py
    providers/
        weather.py
        news.py
        wikipedia.py

interfaces/
    text_repl.py

docs/
    SESSION_HANDOFF.md

---

# Architecture Principle

Intent modules understand the question.

Provider modules fetch information.

The router only connects the user input to the correct intent.

This avoids giant files and keeps ARES scalable.

---

# Roadmap

## ✅ Phase 1 — Foundation

- Raspberry Pi setup
- GitHub repository
- Text interface
- HTTP client
- Cache
- Wikipedia
- Weather
- News

## 🚧 Phase 2 — Intelligence

- Modular intent system
- Natural language detection
- Knowledge intent ✅
- Stock provider
- Crypto provider
- Company information
- Better reasoning

## ⬜ Phase 3 — Memory

- Conversation history
- User profile
- Preferences
- Long-term memory

## ⬜ Phase 4 — Voice

- Wake word
- Speech-to-text
- Text-to-speech
- Continuous conversation

## ⬜ Phase 5 — Assistant

- Calendar
- Notes
- Reminders
- Email
- Daily briefing

## ⬜ Phase 6 — Home Automation

- Telegram
- Smart plugs
- PC control
- Home Assistant

## ⬜ Phase 7 — Vision

- Camera
- OCR
- Face recognition
- Object detection

## ⬜ Phase 8 — Robotics

- ROS2
- Jetson Orin
- LiDAR
- Navigation
- Docking station

## ⬜ Phase 9 — Companion AI

- Personality
- Initiative
- Learning
- Planning
- Emotional memory

---

# Next Milestone

ARES v0.8

Add modular StockIntent.

Target examples:

how hot is the sun
tell me about Mars
what is artificial intelligence
who is Nikola Tesla

---

# Author

Gabriel  
ARES Project  
2026
