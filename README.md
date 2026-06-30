ARES

Autonomous Reasoning & Exploration System

ARES is a modular AI assistant built on Raspberry Pi.

The project focuses on building an assistant that can eventually understand natural language, remember conversations, reason, search information, control hardware, and interact completely by voice.

---

Current Version

ARES v1.1 - Tool Selection Foundation

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
├── User Profile Store
├── Skill Manager
├── Skill Registry
├── Tool Selector
├── Skill Plugins
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
- Text REPL event bus wiring
- Basic conversation turn memory
- Skill base interface
- Skill registry
- Skill manager
- Built-in time/date skill
- Persistent user profile facts
- Memory recall skill
- Tool selection confidence/scoring foundation
- Automated pytest suite
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
- what is my name
- where do I live
- what is my favorite tank
- when is my birthday

Each request is automatically routed to its correct intent.

Implemented Features

- Modular text intent routing
- Event bus for runtime lifecycle events
- Short-term and long-term memory v1 storage
- Separate persistent user profile memory
- Skill registry, skill manager, and skill plugin foundation
- Tool selector for best local skill selection
- Built-in time/date skill
- Built-in memory recall skill for saved profile facts
- Text REPL with conversation turn storage
- Pytest automated coverage for core Phase 2-4 modules

Run Tests

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

Run automated tests:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```

Run Text REPL

```powershell
py interfaces\text_repl.py
```

Say `hello` or `hello ares` to wake ARES, then type a supported request.

Latest Architecture Status

- Intent router remains the main text command path.
- Priority skills can run before generic intents when needed, such as memory recall.
- Normal skills run as fallback when no regular intent matches, such as time/date.
- SkillManager uses ToolSelector confidence scoring instead of first-match-only selection.
- Conversation history and user profile facts are stored separately.
- Voice has not started.

Engineering Rules

- Strict engineering rules are documented in `docs/ENGINEERING_RULES.md`.
- No failing tests may be skipped, hidden, or weakened to pass.
- Every meaningful change must keep `py -m pytest`, `py -m compileall core interfaces events memory skills scripts`, and `py scripts\verify_phase2_events_memory.py` passing.
- README and session handoff documentation must be updated after every meaningful change.

Project Documents

- Architecture: `docs/ARCHITECTURE.md`
- Roadmap: `docs/ROADMAP.md`
- Engineering rules: `docs/ENGINEERING_RULES.md`
- Session handoff: `docs/SESSION_HANDOFF.md`

---

Roadmap

Phase 1 ✅

- Modular architecture
- Intent routing
- Weather
- News
- Knowledge
- Stocks

Phase 2 (Foundation Complete, Provider Work Pending)

- Event bus foundation
- Memory v1 interface
- Text REPL event/memory integration
- Company information
- Cryptocurrency
- Better stock analysis
- Better natural language understanding

Phase 2 Foundation Modules

- `events.EventBus` provides in-process publish/subscribe events for ARES modules.
- `IntentRouter` publishes `user_message_received`, `intent_detected`, and `response_generated`.
- The text REPL uses a shared event bus and records basic conversation turns.
- `memory.MemoryStore` provides the v1 interface for short-term and long-term memories.
- Memory v1 stores data in the existing `data/memories_short.json` and `data/memories_long.json` files.

Verification

- Run `python scripts/verify_phase2_events_memory.py` to verify router events and memory turn storage.

Phase 3 Skill Foundation

- Plugin/skill architecture
- Skill registration system
- Skill manager
- Base `Skill` interface
- Example time/date skill
- Long-term memory
- User profile
- Conversation memory
- Personal reminders

Phase 3 Skill Modules

- `skills.Skill` defines the base interface for future capabilities.
- `skills.SkillRegistry` registers and looks up skills.
- `skills.SkillManager` detects and executes registered skills.
- `skills.SkillPlugin` groups skills into a plugin bundle.
- `skills.builtin.TimeDateSkill` is the first example skill.

The text REPL registers the built-in skill plugin and passes `SkillManager` to `IntentRouter`.
Intent routing still runs first; skills are used only as a fallback when no normal intent matches.

Voice has not started.

Phase 4 Long-Term Memory Recall (Current)

- User facts are stored separately from conversation history in `data/user_profile.json`.
- `memory.UserProfileStore` detects and saves facts from text input.
- Supported fact patterns include `My name is...`, `I live in...`, `My birthday is...`, `My favorite ... is...`, and `I own...`.
- `skills.builtin.MemoryRecallSkill` answers personal profile questions from stored facts without using an LLM.
- Memory recall is a priority skill, so it runs before generic knowledge lookup for profile questions.

Phase 4 Tool Selection Foundation

- `skills.ToolSelector` scores local skills using trigger match strength, optional selection keywords, skill priority, and priority-intent filtering.
- Current supported skills are `TimeDateSkill` and `MemoryRecallSkill`.
- Future `CalculatorSkill` and `NotesSkill` can participate by defining triggers and optional `selection_keywords`.
- No calculator, notes, voice, external API, weather, stocks, calendar, or GPT integration has been added.

Phase 5

- Voice wake word
- Speech-to-text
- Text-to-speech
- Continuous conversation

Phase 6

- Vision
- Camera understanding
- Face recognition
- Object recognition

Phase 7

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
