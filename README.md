ARES

Autonomous Reasoning & Exploration System

ARES is a modular AI assistant built on Raspberry Pi.

The project focuses on building an assistant that can eventually understand natural language, remember conversations, reason, search information, control hardware, and interact completely by voice.

---

Current Version

ARES v0.8 - Phase 2 Foundation

---

Current Architecture

ARES
│
├── Event Bus
│
├── Intent Router
│
├── Intents
│   ├── Greeting
│   ├── Goodbye
│   ├── Weather
│   ├── News
│   ├── Knowledge
│   └── Stocks
│
├── Providers
│   ├── Weather
│   ├── News
│   ├── Wikipedia
│   ├── Knowledge
│   └── Alpha Vantage Stocks
│
├── HTTP Client
├── Cache
├── Memory v1 Interface
└── Text Interface

---

Completed

- Modular Intent Router
- Greeting Intent
- Goodbye Intent
- Weather Intent
- News Intent
- Knowledge Intent
- Stock Intent
- Weather Provider
- News Provider
- Wikipedia Provider
- Knowledge Provider
- Alpha Vantage Stock Provider
- HTTP Client
- Cache System
- Event Bus foundation
- Memory v1 interface
- Session handoff documentation
- Modular project structure
- Git version control

---

Current Features

ARES currently understands questions such as:

- hello ares
- goodbye ares
- weather madrid tomorrow
- latest defense news
- what is artificial intelligence
- nvidia stock
- apple stock

Each request is automatically routed to its correct intent.

---

Roadmap

Phase 1 ✅

- Modular architecture
- Intent routing
- Weather
- News
- Knowledge
- Stocks

Phase 2 (Current)

- Event bus foundation
- Memory v1 interface
- Company information
- Cryptocurrency
- Better stock analysis
- Better natural language understanding

Phase 2 Foundation Modules

- `events.EventBus` provides in-process publish/subscribe events for ARES modules.
- `IntentRouter` publishes input and routing lifecycle events.
- `memory.MemoryStore` provides the v1 interface for short-term and long-term memories.
- Memory v1 stores data in the existing `data/memories_short.json` and `data/memories_long.json` files.

Phase 3

- Long-term memory
- User profile
- Conversation memory
- Personal reminders

Phase 4

- Voice wake word
- Speech-to-text
- Text-to-speech
- Continuous conversation

Phase 5

- Vision
- Camera understanding
- Face recognition
- Object recognition

Phase 6

- Robotics
- ROS2
- Jetson Orin migration
- Autonomous navigation

---

Long-Term Goal

ARES should become a complete autonomous personal assistant capable of:

- Natural conversation
- Long-term memory
- Internet research
- Stock analysis
- Home automation
- Robotics
- Autonomous reasoning
- Voice interaction
- Vision
- Learning from experience
