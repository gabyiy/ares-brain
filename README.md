ARES

Autonomous Reasoning & Exploration System

ARES is a modular AI assistant built on Raspberry Pi.

The project focuses on building an assistant that can eventually understand natural language, remember conversations, reason, search information, control hardware, and interact completely by voice.

---

Current Version

ARES v1.11 - Tool Chaining Foundation

---

Current Architecture

The active runtime includes `core.IntentParser` for structured local intents, `core.Planner` for local multi-step execution plans, `core.ToolChain` for bounded local tool chaining, `core.ExecutionPipeline` for sequential plan execution, `memory.NotesStore` for persistent local notes, `memory.TasksStore` for offline tasks, `memory.ReminderScheduler` for passive due-time queries, and `core.ConversationContextManager` for short-term in-memory skill context.

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
- Built-in calculator skill
- Persistent local notes store
- Built-in notes skill
- Persistent local tasks store
- Built-in tasks skill
- In-memory conversation context manager
- Structured intent parser and `Intent` object
- Execution pipeline for planner steps
- Tool chaining for bounded local multi-step requests
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
- what is 2 + 3 * 4
- calculate 15*8
- calculate (2 + 3) * 4
- save note calibrate rover sensors
- list my notes
- search notes rover
- delete note <id>
- add task buy milk
- remember buy milk tomorrow
- remind me to call mom
- list tasks
- mark task <id> done

Each request is automatically routed to its correct intent.

Implemented Features

- Modular text intent routing
- Event bus for runtime lifecycle events
- Short-term and long-term memory v1 storage
- Separate persistent user profile memory
- Skill registry, skill manager, and skill plugin foundation
- Tool selector for best local skill selection
- Structured intent parser for deterministic local intent/entity extraction
- Multi-step planner for local notes, tasks, calculator, and conversation memory steps
- Tool chaining with max depth, loop prevention, execution trace, and chain history
- Execution pipeline for ordered plan step execution, execution results, logging, and rollback hooks
- Built-in time/date skill
- Built-in memory recall skill for saved profile facts
- Built-in calculator skill for safe local arithmetic
- Built-in notes skill for persistent local notes
- Built-in tasks skill for offline reminders/tasks
- ReminderScheduler foundation for parsing task due text and finding due/upcoming tasks
- In-memory conversation context for recent skill turns
- Text REPL with conversation turn storage
- Pytest automated coverage for 105 tests across core Phase 2-12 modules
- GitHub Actions CI for pushes and pull requests to `main`

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

Current pytest collection: `105 tests`.

Continuous Integration

- GitHub Actions runs on every push and pull request to `main`.
- CI uses `windows-latest` with Python 3.13.
- CI installs `requirements.txt` and runs the same verification commands used locally.
- The latest `main` CI run must stay green before additional changes are merged.
- The `main` branch should be protected so changes flow through pull requests and required CI checks.

Recommended workflow:

1. Create a feature branch from latest `main`.
2. Make one scoped change.
3. Run the local verification suite.
4. Open a pull request into `main`.
5. Wait for GitHub Actions CI to pass.
6. Merge only after local tests and CI are green.

Run Text REPL

```powershell
py interfaces\text_repl.py
```

Say `hello` or `hello ares` to wake ARES, then type a supported request. Use `show plan` or `show steps` to display the last local execution plan. Use `show execution` or `show last execution` to display the latest execution result. Use `show chain` or `show chain history` to inspect the latest tool chain.

Latest Architecture Status

- Intent router remains the main text command path.
- Priority skills can run before generic intents when needed, such as memory recall.
- Normal skills run as fallback when no regular intent matches, such as time/date.
- CalculatorSkill runs as a priority local skill for arithmetic before generic knowledge lookup.
- NotesSkill runs as a priority local skill for note commands.
- TasksSkill runs as a priority local skill for task/reminder commands.
- SkillManager parses user text into a structured `Intent` before ToolSelector runs.
- ToolSelector builds a `Plan` before selection so multi-step requests can be inspected before execution.
- ToolSelector first scores matching `intent_names`, then falls back to legacy triggers only for unknown intents.
- SkillManager delegates executable planner steps to ExecutionPipeline.
- SkillManager validates executable planner steps through ToolChain before ExecutionPipeline runs.
- ToolChain enforces max chain depth 5, prevents repeated-step loops, and records chain trace/history.
- ExecutionPipeline executes plan steps sequentially and records `StepResult` and `ExecutionResult` details.
- ExecutionPipeline emits execution events and standard logs for start, step completion, recoverable failure, unrecoverable failure, rollback, and completion.
- Live REPL integration tests verify multi-step plan creation, notes plus calculator execution, task plus memory execution, recoverable partial failure reporting, last execution display, and the active `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill` path.
- SkillContext metadata carries the parsed intent and extracted entities for skills that need them.
- IntentParser tests cover ambiguous local phrases such as `remember to buy milk`, note reminders, birthday recall, task actions, note actions, calculator requests, and unknown text.
- REPL integration tests confirm live text input reaches IntentParser before SkillManager selects local skills.
- Unknown structured intents do not use loose token-overlap fallback, preventing generic text from being misrouted to memory recall.
- SkillManager uses ToolSelector confidence scoring instead of first-match-only selection.
- SkillManager records handled skill turns into the in-memory conversation context.
- Conversation history, user profile facts, notes, and tasks are stored separately.
- `show plan` and `show steps` in the text REPL display the last execution plan.
- `show execution` and `show last execution` in the text REPL display the last execution result.
- `show chain` and `show chain history` in the text REPL display the latest local tool chain trace.
- ReminderScheduler reads existing task due text and can identify due or upcoming tasks without changing `data/tasks.json`.
- Supported due phrases include `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- ConversationContextManager keeps only the last 20 skill turns in RAM and does not write to disk.
- GitHub Actions CI now enforces the local verification suite on `main`.
- Voice has not started.

Engineering Rules

- Strict engineering rules are documented in `docs/ENGINEERING_RULES.md`.
- No failing tests may be skipped, hidden, or weakened to pass.
- Every meaningful change must keep `py -m pytest`, `py -m compileall core interfaces events memory skills scripts`, and `py scripts\verify_phase2_events_memory.py` passing.
- GitHub Actions CI must stay green for pushes and pull requests to `main`.
- `main` should be protected and merged through the feature branch -> tests -> PR -> CI -> merge workflow.
- README and session handoff documentation must be updated after every meaningful change.

Project Documents

- Architecture: `docs/ARCHITECTURE.md`
- Roadmap: `docs/ROADMAP.md`
- Engineering rules: `docs/ENGINEERING_RULES.md`
- Session handoff: `docs/SESSION_HANDOFF.md`

---

TODO

Completed:

- Core brain
- Event bus
- Memory
- User profile memory
- Notes
- Tasks/reminders
- Calculator
- Tool selector
- Intent parser
- Conversation context
- Reminder scheduler
- Planner
- Execution pipeline
- CI/tests
- Tool chaining

Next:

1. Long-term goal management
2. External tool adapter interface
3. Weather skill
4. Calendar skill
5. Stock/market skill
6. GPT fallback integration
7. Voice input/output
8. Raspberry Pi deployment
9. Robot body / sensors

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
- Current supported skills are `TimeDateSkill`, `MemoryRecallSkill`, `CalculatorSkill`, `NotesSkill`, and `TasksSkill`.
- No real scheduling, notifications, voice, external API, weather, stocks, calendar, or GPT integration has been added.

Phase 4 Calculator Skill

- `skills.builtin.CalculatorSkill` is the first real local tool behind the selector foundation.
- Supports addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers.
- Uses Python AST parsing and explicit operator handling, not `eval()`.
- Rejects unsafe or unsupported input with a clear local response.

Phase 5 Local Notes Skill

- `memory.NotesStore` persists user-created notes in `data/notes.json`.
- Notes are separate from conversation memory and user profile memory.
- `skills.builtin.NotesSkill` supports adding, listing, searching, deleting one note, and confirmed delete-all.
- Each note contains a unique id, timestamp, and text.
- `data/notes.json` is ignored by git because it can contain personal notes.

Phase 6 Local Tasks Skill

- `memory.TasksStore` persists offline tasks in `data/tasks.json`.
- Tasks are separate from conversation memory, user profile memory, and notes.
- `skills.builtin.TasksSkill` supports adding, listing, marking done, deleting, and clearing completed tasks.
- Each task contains an id, text, created timestamp, optional due text, and completed state.
- `memory.ReminderScheduler` can parse stored due text and return due/upcoming tasks.
- No notifications, calendar integration, voice, or GPT integration has been added.
- `data/tasks.json` is ignored by git because it can contain personal tasks.

Phase 7 In-Memory Conversation Context

- `core.ConversationContextManager` keeps the last 20 handled skill turns in RAM.
- Each turn stores timestamp, user message, assistant response, and detected skill.
- APIs include `last_message()`, `last_user_message()`, `last_assistant_message()`, `last_skill()`, `history(limit)`, and `clear()`.
- `SkillManager` records handled skill interactions automatically.
- No conversation context is saved to disk.
- No embeddings, GPT, external APIs, or voice integration has been added.

Phase 8 Structured Intent Parser

- `core.Intent` stores `intent_name`, `confidence`, `extracted_entities`, and `raw_text`.
- `core.IntentParser` converts local natural language into structured intents before ToolSelector runs.
- Recognized intents include `calculate`, `note`, `task`, `memory_recall`, `time_date`, and `unknown`.
- Useful entities are extracted for local tools, such as task text and due text.
- Parser hardening covers common user phrasing without adding GPT, new skills, or storage format changes.
- SkillManager consumes structured intents without using AI, GPT, embeddings, or external APIs.

Phase 9 ReminderScheduler Foundation

- `memory.ReminderScheduler` reads tasks from `TasksStore` and interprets raw due text locally.
- `parse_due_text(text)` supports `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- `due_tasks(now)` returns incomplete tasks whose parsed due time is due.
- `upcoming_tasks(now, limit)` returns incomplete future tasks ordered by parsed due time.
- Invalid due text is ignored safely.
- No notifications, calendar integration, voice, GPT, or storage format changes were added.

Phase 10 Multi-Step Task Planner Foundation

- `core.PlanStep` stores one ordered executable step.
- `core.Plan` stores ordered steps plus planning errors.
- `core.Planner` converts an `Intent` into a local execution plan without executing skills.
- Supported planner targets are notes, tasks, calculator, and conversation memory.
- ToolSelector attaches plans before SkillManager executes anything.
- The text REPL supports `show plan` and `show steps`.
- No new skills, GPT, voice, notifications, calendar integration, or external APIs were added.

Phase 11 Execution Pipeline Foundation

- `core.ExecutionPipeline` executes planner steps sequentially.
- `core.StepResult` records start time, end time, duration, success/failure, returned data, and error messages.
- `core.ExecutionResult` records full plan execution status and rollback metadata.
- `core.RollbackHook` provides a no-op rollback extension point for future reversible local actions.
- SkillManager delegates executable plans to ExecutionPipeline and stores the latest execution result.
- The text REPL supports `show execution` and `show last execution`.
- No new skills, GPT, voice, notifications, calendar integration, or external APIs were added.

Phase 12 Tool Chaining Foundation

- `core.ToolChain` validates and traces compatible local multi-step requests.
- Tool chains execute through the existing Planner and ExecutionPipeline.
- Max chain depth is 5.
- Repeated step signatures are rejected to prevent loops.
- ToolChain records execution trace and bounded chain history.
- Supported local examples include memory plus calculator, note plus memory, and task/reminder plus memory.
- The text REPL supports `show chain` and `show chain history`.
- No external APIs, GPT, voice, weather, stocks, calendar, notifications, or storage format changes were added.

Phase 13

- Voice wake word
- Speech-to-text
- Text-to-speech
- Continuous conversation

Phase 14

- Vision
- Camera understanding
- Face recognition
- Object recognition

Phase 15

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
