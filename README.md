# ARES

> **A modular AI assistant designed to evolve into a fully autonomous personal companion and robotic brain.**

---

# Vision

ARES is a long-term AI engineering project.

The objective is to build a local-first intelligent assistant that starts as a terminal application on a Raspberry Pi and gradually evolves into an autonomous robot capable of understanding, remembering, reasoning, searching knowledge, interacting naturally with people and controlling the physical world.

Every milestone is designed to keep ARES stable while continuously adding new capabilities.

---

# Current Status

**Version:** ARES v0.5

**Repository:**
https://github.com/gabyiy/ares-brain

**Platform**

- Raspberry Pi 5
- Python 3
- Debian Linux

---

# Current Milestone

## 🚧 Phase 2 — Intelligence

### Completed

- Intent Router
- Natural language detection
- Wikipedia Provider
- Weather Provider
- News Provider
- Google RSS integration
- HTTP client
- Cache system
- Modular architecture
- Session handoff system

### In Progress

- Knowledge Provider
- Better topic extraction
- Automatic provider selection

### Next Goals

- Stock Market Provider
- Cryptocurrency Provider
- Company Information Provider
- Better reasoning
- Better summaries
- Multiple provider responses

Goal:

ARES should automatically understand the user's question and decide which provider should answer without requiring commands like **weather**, **news** or **wiki**.

---

# Current Features

## Natural Language

Examples

hello ares

weather madrid

weather tomorrow

weather next week

what are the latest news on rheinmetall

I heard NVIDIA is doing well

what happened with bitcoin today

goodbye ares

---

## Weather

- Current weather
- Tomorrow
- 7-day forecast

Displays

- Weather
- High / Low
- Rain probability
- Wind
- Day

---

## News

Google RSS provider

Displays

- Headline
- Source
- Date

Automatically extracts the topic from normal language.

---

## Wikipedia

Returns concise summaries for general knowledge questions.

---

# Architecture

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

---

# Development Roadmap

## ✅ Phase 1 — Foundation

- Project structure
- Intent Router
- HTTP Client
- Cache
- Wikipedia Provider
- Weather Provider
- News Provider
- GitHub repository
- Automatic documentation

## 🚧 Phase 2 — Intelligence

- Knowledge Provider
- Automatic Provider Selection
- Better Natural Language Understanding
- Stock Market Provider
- Cryptocurrency Provider
- Company Information
- Better Search
- Reasoning Engine

## ⬜ Phase 3 — Memory

- Conversation history
- Long-term memory
- User profile
- Preferences
- Daily summaries
- Context awareness

## ⬜ Phase 4 — Voice

- Wake word
- Speech-to-text
- Text-to-speech
- Continuous conversations

## ⬜ Phase 5 — Personal Assistant

- Calendar
- Notes
- Reminders
- Email
- Daily Briefings
- Stock alerts
- News alerts

## ⬜ Phase 6 — Home Automation

- Telegram
- Smart plugs
- PC control
- Lights
- Sensors
- Cameras

## ⬜ Phase 7 — Vision

- Camera
- OCR
- Face Recognition
- Object Detection
- Scene Understanding

## ⬜ Phase 8 — Robotics

- ROS2
- Jetson Orin
- LiDAR
- Navigation
- Docking Station
- Robotic Arm

## ⬜ Phase 9 — Companion AI

- Personality
- Initiative
- Learning
- Planning
- Emotional memory

## ⬜ Phase 10 — ARES 2.0

- Full robotic assistant
- Voice
- Vision
- Memory
- Reasoning
- Home control
- Autonomous operation

---

# Overall Progress

Foundation        ██████████ 100%

Intelligence      ████░░░░░░ 40%

Memory            ░░░░░░░░░░ 0%

Voice             ░░░░░░░░░░ 0%

Vision            ░░░░░░░░░░ 0%

Robotics          ░░░░░░░░░░ 0%

---

# Development Workflow

- Never manually edit generated documentation.
- Always recreate files from scratch.
- Always test before committing.
- Always update README.md.
- Always update docs/SESSION_HANDOFF.md.
- Commit every stable milestone.
- Push every stable milestone.

---

# Long-Term Vision

ARES is not meant to remain a chatbot.

The final objective is to create a local AI companion capable of:

- Understanding natural language
- Remembering conversations
- Learning user preferences
- Searching the internet intelligently
- Monitoring news and markets
- Assisting with everyday tasks
- Controlling smart devices
- Seeing through cameras
- Speaking naturally
- Moving inside a robotic body

Each phase brings ARES one step closer to becoming a true AI companion.

---

# License

MIT

---

# Author

Gabriel

ARES Project

2026
