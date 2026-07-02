ARES Session Handoff

Last Updated: 2026-07-02

Current Version

ARES v1.17 - Multi-Step Planner Hardening

---

Current Status

The project has been fully reorganized into a modular architecture.

The original monolithic "intent_router.py" has been split into separate intent modules.

Phase 2 foundation work has started.

New foundation modules:

- Event Bus
- Memory v1 interface

Current Phase 2 wiring:

- Text REPL uses a shared event bus.
- Intent Router publishes the public Phase 2 events.
- Text REPL stores basic conversation turns through MemoryStore v1.

Phase 3 planning/foundation has started.

New Phase 3 skill modules:

- `skills.Skill`
- `skills.SkillContext`
- `skills.SkillResponse`
- `skills.SkillRegistry`
- `skills.SkillManager`
- `skills.SkillPlugin`
- `skills.builtin.TimeDateSkill`

The skill layer is minimally wired into the text REPL through `IntentRouter`.
Normal intents still run first; SkillManager is only a fallback when no intent matches.
Voice work has not started.

Phase 4 long-term memory recall has started.

New memory recall modules:

- `memory.UserProfileStore`
- `memory.detect_profile_facts`
- `skills.builtin.MemoryRecallSkill`

User profile facts are stored separately from conversation history.
The default profile path is `data/user_profile.json`, which is ignored by git to avoid committing personal facts.
The profile path can be overridden with `ARES_USER_PROFILE_PATH` for tests.

Automated tests have been added.

New test coverage:

- Event bus
- MemoryStore v1
- UserProfileStore
- MemoryRecallSkill
- SkillRegistry and SkillManager
- TimeDateSkill
- CalculatorSkill
- NotesStore
- NotesSkill
- TasksStore
- TasksSkill
- GoalsStore
- GoalsSkill
- ReminderScheduler
- ConversationContextManager
- IntentParser
- Planner
- MultiStepPlan
- ExecutionPipeline
- ToolChain
- ToolAdapter
- WeatherSkill
- MarketSkill
- CalendarSkill
- ToolSelector structured intent routing
- Text REPL profile recall flow

Pytest is configured to collect only `tests/`, because legacy interactive scripts under `scripts/` also use `test_*.py` names.

Phase 4 Tool Selection foundation has been added.

New tool selection module:

- `skills.ToolSelector`
- `skills.ToolSelection`

Selection behavior:

- Scores local skills instead of relying on first registered match only.
- Supports exact trigger matches, contained trigger phrases, token overlap, optional `selection_keywords`, optional `selection_priority`, and `run_before_intents` filtering.
- Currently routes `TimeDateSkill`, `MemoryRecallSkill`, `CalculatorSkill`, `GoalsSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, `MarketSkill`, and `CalendarSkill`.

Phase 4 CalculatorSkill has been added as the first real local tool.

New calculator module:

- `skills.builtin.CalculatorSkill`

Calculator behavior:

- Supports addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers.
- Uses AST parsing with explicit operator handling; it does not use `eval()`.
- Rejects unsupported or unsafe input with a clear response.
- Runs as a priority skill so arithmetic questions are handled before generic knowledge lookup.

Phase 5 NotesSkill has been added as the first persistent local note system.

New notes modules:

- `memory.NotesStore`
- `memory.NoteRecord`
- `skills.builtin.NotesSkill`

Notes behavior:

- Stores notes in `data/notes.json`, which is ignored by git.
- Keeps notes separate from conversation memory and user profile memory.
- Supports `remember this...`, `save note...`, `take a note...`, `list my notes`, `show my notes`, `delete note <id>`, `delete all notes`, and `search notes <keyword>`.
- Requires explicit confirmation with `confirm delete all notes` before clearing all notes.
- Publishes `notes.recorded`, `notes.deleted`, and `notes.cleared`.
- Uses `ARES_NOTES_PATH` for test isolation.

Phase 6 TasksSkill has been added as the first offline reminders/tasks system.

New task modules:

- `memory.TasksStore`
- `memory.TaskRecord`
- `memory.ReminderScheduler`
- `skills.builtin.TasksSkill`

Tasks behavior:

- Stores tasks in `data/tasks.json`, which is ignored by git.
- Keeps tasks separate from conversation memory, user profile memory, and notes.
- Supports `add task...`, `remind me to...`, `list tasks`, `show tasks`, `mark task <id> done`, `delete task <id>`, and `clear completed tasks`.
- Each task stores id, text, created timestamp, optional due text, and completed state.
- Stores due text only; no notifications, calendar integration, voice, or GPT integration has been added.
- Publishes `tasks.recorded`, `tasks.completed`, `tasks.deleted`, and `tasks.completed_cleared`.
- Uses `ARES_TASKS_PATH` for test isolation.

ReminderScheduler foundation has been added.

Reminder scheduler behavior:

- Parses existing task due text without changing `data/tasks.json`.
- Supports `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- Provides `parse_due_text(text)`, `due_tasks(now)`, and `upcoming_tasks(now, limit)`.
- Returns incomplete due/upcoming tasks from `TasksStore`.
- Ignores invalid due text safely.
- Does not send notifications.
- Does not schedule background jobs.
- Does not call calendar APIs.
- Does not use voice, GPT, or external APIs.

Phase 7 ConversationContextManager has been added for short-term in-memory context.

New conversation context module:

- `core.ConversationContextManager`
- `core.ConversationTurn`
- `core.get_global_conversation_context`

Conversation context behavior:

- Keeps the last 20 handled skill turns in RAM.
- Each turn stores timestamp, user message, assistant response, and detected skill.
- Supports `last_message()`, `last_user_message()`, `last_assistant_message()`, `last_skill()`, `history(limit)`, and `clear()`.
- `SkillManager` records handled skill interactions automatically.
- `interfaces.text_repl` passes the shared global in-memory context to `SkillManager`.
- Does not save conversation context to disk.
- Does not use embeddings, GPT, external APIs, or voice.

Phase 8 structured IntentParser has been added.

New intent parser modules:

- `core.Intent`
- `core.IntentParser`

Intent behavior:

- `Intent` stores `intent_name`, `confidence`, `extracted_entities`, and `raw_text`.
- `IntentParser` recognizes `calculate`, `note`, `task`, `memory_recall`, `weather`, `market`, `calendar`, `time_date`, and `unknown`.
- Useful entities are extracted for local tools, including task action/text/due, note actions, calculator expressions, and memory recall topics.
- Example: `remember buy milk tomorrow` becomes a `task` intent with text `buy milk` and due text `tomorrow`.
- Example: `remember to buy milk` becomes a `task` intent with text `buy milk`.
- Example: `remember this idea: build ARES memory` becomes a `note` intent with note text `idea: build ARES memory`.
- Example: `calculate 15*8` becomes a `calculate` intent with expression `15*8`.
- Example: `show my notes` becomes a `note` intent.
- Example: `what is my birthday` becomes a `memory_recall` intent for the birthday profile fact.
- Example: `what did I tell you about my job` becomes a `memory_recall` intent with topic `my job`.
- The parser is deterministic and offline. It does not use AI, GPT, embeddings, voice, or external APIs.

Selection behavior:

- `SkillManager` parses user text into an `Intent` before calling `ToolSelector`.
- Skills can declare `intent_names` for structured matching.
- `ToolSelector` scores structured intent matches before legacy trigger scoring.
- Exact and contained trigger fallback paths remain available for unknown intents and compatibility.
- Loose token-overlap fallback is disabled for unknown structured intents so generic text does not get misrouted to local skills.
- `can_handle` fallback remains available for unknown intents and compatibility.
- `SkillContext.metadata` carries the parsed `intent` and extracted `entities`.
- `TasksSkill` consumes parser-derived task entities so `remember buy milk tomorrow` can route through the REPL as an offline task.

Multi-step Planner foundation has been added.

New planner modules:

- `core.PlanStep`
- `core.Plan`
- `core.MultiStepPlan`
- `core.Planner`

Planner behavior:

- Receives an `Intent`.
- Produces ordered executable steps.
- Returns a regular `Plan` for single-step requests.
- Returns a `MultiStepPlan` for compatible requests with more than one executable step.
- Supports goals, notes, tasks, calculator, weather, market, calendar, and conversation memory targets.
- Skips impossible steps and returns planning errors cleanly.
- Splits compatible compound requests such as `What's the weather tomorrow and remind me to go to the gym`.
- Splits compatible compound requests such as `Show my goals and today's calendar`.
- Serializes plans and steps for tests, events, and REPL display.
- Planner never executes skills.
- ToolSelector builds a plan before returning a skill selection.
- SkillManager executes multi-step local plans when needed.
- `interfaces.text_repl` supports `show plan` and `show steps`.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes were added.

Execution Pipeline foundation has been added.

New execution modules:

- `core.ExecutionPipeline`
- `core.ExecutionResult`
- `core.StepResult`
- `core.RollbackHook`

Execution behavior:

- Receives a `Plan` from Planner.
- Executes each `PlanStep` sequentially.
- Calls registered local skills through SkillManager and SkillRegistry.
- Executes conversation memory steps through MemoryStore.
- Stops safely on unrecoverable failures.
- Continues after recoverable local tool failures when appropriate.
- Aggregates all step outputs into one final response.
- Labels mixed successful and failed recoverable steps as `Partial results:`.
- Records start time, end time, duration, success/failure, returned data, and error message for every step.
- Publishes execution lifecycle events.
- Emits standard execution logs through `ares.execution`.
- Provides a no-op rollback hook interface for future reversible local actions.
- `interfaces.text_repl` supports `show execution` and `show last execution`.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes were added.

Execution Pipeline verification hardening has been added.

New integration coverage:

- Live REPL multi-step plan creation.
- Live REPL notes plus calculator execution through ExecutionPipeline.
- Live REPL task plus memory execution through ExecutionPipeline.
- Live REPL recoverable partial failure reporting and continued execution.
- Live REPL `show execution` and `show last execution`.
- Live-path spy coverage for `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill`.

Multi-step Planner hardening has been added.

New hardening coverage:

- Single-step requests still return regular `Plan` objects.
- Compatible multi-step requests return ordered `MultiStepPlan` objects.
- Weather plus reminder requests plan and execute through the live REPL path.
- Goals plus calendar requests plan and execute through the live REPL path.
- Three-step requests preserve planner and execution ordering.
- Recoverable step failures continue remaining work and report partial results.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Tool Chaining foundation has been added.

New chain modules:

- `core.ToolChain`
- `core.ToolChainResult`
- `core.ToolChainTraceStep`

ToolChain behavior:

- Receives a `Plan`.
- Enforces max chain depth 5.
- Rejects repeated step signatures to prevent loop-style chains.
- Records ordered execution trace.
- Keeps bounded chain history for REPL inspection.
- Delegates accepted plans to ExecutionPipeline.
- Supports memory plus calculator, note plus memory, and task/reminder plus memory examples.
- `interfaces.text_repl` supports `show chain` and `show chain history`.
- No external APIs, GPT, voice, weather, stocks, calendar, notifications, or storage format changes were added.

Long-Term Goal Management foundation has been added.

New goal modules:

- `memory.GoalsStore`
- `memory.GoalRecord`
- `skills.builtin.GoalsSkill`

Goals behavior:

- Stores goals in `data/goals.json`, which is ignored by git.
- Keeps goals separate from conversation memory, user profile memory, notes, and tasks.
- Goal fields include id, title, description, created timestamp, active/completed/paused status, priority, and milestones.
- Supports `add goal`, `list goals`, `show goal <id>`, `complete goal <id>`, `pause goal <id>`, `delete goal <id>`, and `add milestone to goal <id>`.
- `IntentParser`, `ToolSelector`, `Planner`, `ToolChain`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `goal` intent.
- Tests cover GoalsStore persistence, GoalsSkill commands, ToolSelector routing, IntentParser routing, Planner path, ExecutionPipeline path, ToolChain goal chains, SkillManager path, REPL lifecycle commands, and persistence after reload.
- No GPT, autonomous background actions, notifications, external APIs, voice, weather, stocks, or calendar integration were added.

External Tool Adapter foundation has been added.

New adapter modules:

- `core.ToolAdapter`
- `core.ToolRequest`
- `core.ToolResponse`
- `core.ToolAdapterRegistry`
- `core.MockWeatherAdapter`
- `core.MockMarketAdapter`

ToolAdapter behavior:

- Registers and looks up local adapters by name.
- Finds adapters by capability.
- Exposes adapter metadata: name, description, capabilities, `requires_network`, and `requires_auth`.
- Returns clear missing-adapter and unsupported-capability responses.
- Provides offline mock weather and market adapters for tests only.
- Planner accepts an optional ToolAdapterRegistry for future adapter-aware planning.
- ExecutionPipeline can execute explicit `tool_adapter` PlanSteps through an injected registry.
- No real APIs, API keys, GPT, voice, stock skill, calendar integration, web adapter, or network calls were added.

Adapter-backed WeatherSkill has been added.

New weather module:

- `skills.builtin.WeatherSkill`

Weather behavior:

- Uses `ToolAdapterRegistry` and `MockWeatherAdapter`.
- Supports `weather`, `weather today`, `weather tomorrow`, and `weather in Madrid`.
- Runs as a priority local skill so weather requests are handled before legacy weather provider routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `weather` intent.
- Tests cover weather intent parsing, mock weather adapter calls, WeatherSkill responses, planner weather steps, execution pipeline weather steps, REPL routing, and missing adapter errors.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, calendar, stocks, or notifications.

Adapter-backed MarketSkill has been added.

New market module:

- `skills.builtin.MarketSkill`

Market behavior:

- Uses `ToolAdapterRegistry` and `MockMarketAdapter`.
- Supports `stock nvidia`, `nvidia stock`, `apple stock`, and `market price for tesla`.
- Runs as a priority local skill so stock/market requests are handled before legacy stock provider routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `market` intent.
- Tests cover market intent parsing, mock market adapter calls, MarketSkill responses, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, calendar, or notifications.

Adapter-backed CalendarSkill has been added.

New calendar modules:

- `core.MockCalendarAdapter`
- `skills.builtin.CalendarSkill`

Calendar behavior:

- Uses `ToolAdapterRegistry` and `MockCalendarAdapter`.
- Supports `what is on my calendar today`, `calendar tomorrow`, `schedule today`, and `do I have anything tomorrow`.
- Runs as a priority local skill so calendar/schedule requests are handled before generic date or knowledge routing.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `calendar` intent.
- Tests cover calendar intent parsing, mock calendar adapter calls, CalendarSkill responses, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors.
- Does not call Google Calendar.
- Does not call real APIs.
- Does not use API keys.
- Does not use internet access, GPT, voice, notifications, or background automation.

GitHub Actions CI has been added.

CI behavior:

- Workflow file: `.github/workflows/ci.yml`
- Runs on push to `main`.
- Runs on pull requests targeting `main`.
- Uses `windows-latest`.
- Sets up Python 3.13.
- Installs dependencies with `py -m pip install -r requirements.txt`.
- Runs `py -m pytest`.
- Runs `py -m compileall core interfaces events memory skills scripts`.
- Runs `py scripts\verify_phase2_events_memory.py`.
- Latest checked `main` CI run for `e37e5d4` completed successfully.
- `main` should be protected with required CI checks before merge.
- Future work should use: feature branch -> local tests -> pull request -> CI -> merge.

Strict engineering rules have been added in `docs/ENGINEERING_RULES.md`.

Required rules going forward:

- Never skip, xfail, or weaken failing tests without explicit approval.
- Never hide errors with broad try/except blocks.
- Fix root causes rather than symptoms.
- Keep the full verification suite passing before every push.
- Keep GitHub Actions CI green for `main` pushes and pull requests.
- Protect `main` with required CI checks and use pull requests for future changes.
- Update README and SESSION_HANDOFF after every meaningful change.

Master planning documents have been added.

New planning docs:

- `docs/ARCHITECTURE.md`
- `docs/ROADMAP.md`

These documents explain the current system flow, event bus, intent router, memory stores, skill system, REPL flow, future integration points, completed phases, current state, next priorities, blocked work, and testing rules before each phase.

Current working intents:

- Greeting
- Goodbye
- Weather
- News
- Knowledge
- Stocks

Current providers:

- Weather Provider
- News Provider
- Wikipedia Provider
- Knowledge Provider
- Alpha Vantage Stock Provider

The stock provider now uses Alpha Vantage instead of Yahoo Finance due to Yahoo's authentication restrictions.

Event Bus:

- New `events.EventBus` supports in-process publish/subscribe.
- Subscribers can listen to one event name or `"*"` for all events.
- `IntentRouter` now publishes `user_message_received`, `intent_detected`, and `response_generated`.
- Existing internal aliases remain: `input.received`, `intent.empty`, `intent.matched`, `intent.response`, and `intent.unmatched`.

Memory v1:

- New `memory.MemoryStore` reads the existing `data/memories_short.json` and `data/memories_long.json` files.
- New `memory.MemoryRecord` normalizes legacy memory entries without rewriting files until a write happens.
- Supports `remember`, `recall`, `promote`, `clear`, and `stats`.
- Publishes `memory.recorded`, `memory.promoted`, and `memory.cleared` events.

User Profile:

- Detects `My name is...`
- Detects `I live in...`
- Detects `My birthday is...`
- Detects `My favorite ... is...`
- Detects `I own...`
- Publishes `profile.fact_saved`

Notes Store:

- Stores note records with unique id, timestamp, and text.
- Lists, searches, deletes one note, and clears all notes through explicit skill confirmation.
- Uses `data/notes.json` by default.
- Does not write to `MemoryStore` or `UserProfileStore`.

Tasks Store:

- Stores task records with id, text, created timestamp, optional due text, and completed boolean.
- Lists, marks done, deletes one task, and clears completed tasks.
- Uses `data/tasks.json` by default.
- Does not write to `MemoryStore`, `UserProfileStore`, or `NotesStore`.
- ReminderScheduler can read task due text for passive due/upcoming queries.
- Does not send notifications or run background scheduling.

Goals Store:

- Stores goal records with id, title, description, created timestamp, status, priority, and milestones.
- Lists, shows, completes, pauses, deletes one goal, and adds milestones.
- Uses `data/goals.json` by default.
- Does not write to `MemoryStore`, `UserProfileStore`, `NotesStore`, or `TasksStore`.
- Does not run autonomous background actions, notifications, or external APIs.

Conversation Context:

- Stores recent handled skill turns in RAM only.
- Default limit is 20 turns.
- Is separate from `MemoryStore`, `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore`.
- Is shared by the text REPL through `core.get_global_conversation_context()`.
- Does not write any conversation context file.

Text REPL memory:

- `interfaces.text_repl` creates one shared event bus and passes it to `IntentRouter`, `MemoryStore`, `UserProfileStore`, `GoalsStore`, `NotesStore`, `TasksStore`, `ConversationContextManager`, and `SkillManager`.
- Each text response records a short-term `conversation_turn` memory.
- Each text input is also scanned for user profile facts.
- Sleeping-mode responses also emit `user_message_received` and `response_generated`.
- The text REPL now also registers the built-in skill plugin.

Previous v0.7 modules compiled successfully.

Current Phase 2 foundation changes passed git whitespace checks locally.

---

Current Architecture

Intent Router
        │
        ├── EventBus lifecycle events
        │
        ├── GreetingIntent
        ├── GoodbyeIntent
        ├── WeatherIntent
        ├── NewsIntent
        ├── KnowledgeIntent
        └── StockIntent

Each intent owns its own logic and communicates with its corresponding provider.

MemoryStore v1 is separate from the legacy `memory_manager.py` API so existing scripts keep working.

The built-in skill plugin currently registers `MemoryRecallSkill`, `CalculatorSkill`, `CalendarSkill`, `GoalsSkill`, `MarketSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, and `TimeDateSkill`.
The REPL priority skill path currently covers profile memory recall, calculator arithmetic, goal commands, note commands, task commands, weather commands, stock/market commands, and calendar/schedule commands.
`SkillManager` parses text into `core.Intent` before `ToolSelector` selects a local skill.
`ToolSelector` builds a `core.Plan` or `core.MultiStepPlan` before selection, `SkillManager` validates executable plan steps through `core.ToolChain`, and accepted chains execute through `core.ExecutionPipeline`.
`ExecutionPipeline` can execute weather skill PlanSteps, market skill PlanSteps, calendar skill PlanSteps, and explicit `tool_adapter` PlanSteps through `core.ToolAdapterRegistry`.
`ExecutionPipeline` aggregates all step outputs and reports mixed recoverable success/failure as partial results.
`SkillManager` records handled skill turns in `ConversationContextManager`.

Skill Manager
        │
        ├── SkillRegistry
        ├── ToolSelector
        ├── SkillPlugin
        └── Built-in skills
            ├── MemoryRecallSkill
            └── TimeDateSkill

Text REPL
        │
        ├── IntentRouter
        ├── SkillManager fallback
        ├── Priority memory recall skill
        ├── EventBus
        ├── MemoryStore v1
        └── UserProfileStore

---

Immediate Next Milestone

GPT fallback planning on top of the existing deterministic skill/planner architecture.

Next technical choices:

- Add profile acknowledgement responses if desired; current fact statements are stored even when the response is generic.
- Keep voice, GPT, embeddings, external weather/stocks/calendar APIs, real scheduling, notifications, and Raspberry Pi deployment out of scope until explicitly approved.
- Connect selected daily reflection scripts and future providers to MemoryStore v1 only after the memory contract is documented.

---

Future Roadmap

1. GPT fallback integration
2. Voice interface
3. Raspberry Pi deployment
4. Robot body / sensors
5. Vision
6. Robotics
7. Jetson Orin migration
8. Autonomous ARES

Verification Notes

- `scripts/verify_phase2_events_memory.py` verifies router event publication and memory turn storage with temporary memory files.
- Run it with `python scripts/verify_phase2_events_memory.py`.
- Automated tests run with `py -m pytest`.
- Current pytest collection: 164 tests.
- Phase 3 skill package compiles with `py -m compileall skills`.
- `SkillManager` was manually checked with the built-in time/date skill.
- Text REPL was verified with `hello`, `what time is it`, `what date is it`, and `quit`.
- Long-term profile recall was verified through the text REPL with name, location, birthday, favorite tank, and owned item facts.
- Current verification passed:
  - `py -m pytest`
  - `py -m compileall core interfaces events memory skills scripts`
  - `py scripts\verify_phase2_events_memory.py`
- GitHub Actions CI runs the same verification suite on Windows with Python 3.13 for `main` pushes and pull requests.
- GitHub Actions should be checked after push for the latest `main` commit.
- Tool selection tests cover current TimeDate/MemoryRecall/Calculator/Goals/Notes/Tasks selection.
- Calculator tests cover simple arithmetic, precedence, parentheses, decimals, bounded powers, unsafe input rejection, and the REPL routing path.
- Notes tests cover add, list, search, delete, duplicate note text, empty note rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Tasks tests cover add, list, mark done, delete, empty task rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Goals tests cover add, list, show, complete, pause, delete, add milestone, persistence after reload, ToolSelector routing, IntentParser routing, Planner path, ExecutionPipeline path, ToolChain goal chains, SkillManager path, REPL lifecycle commands, and the REPL routing path.
- ToolAdapter tests cover adapter registration, lookup, missing adapter responses, mock weather responses, mock market responses, no-network/no-auth metadata, Planner registry wiring, and ExecutionPipeline adapter execution.
- WeatherSkill tests cover weather intent parsing, mock adapter calls, WeatherSkill responses, ToolSelector routing, planner weather steps, execution pipeline weather steps, REPL routing, full live path into `MockWeatherAdapter`, ToolChain loop prevention for repeated weather steps, and missing adapter errors.
- MarketSkill tests cover market intent parsing, mock adapter calls, MarketSkill responses, ToolSelector routing, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors.
- CalendarSkill tests cover calendar intent parsing, mock adapter calls, CalendarSkill responses, ToolSelector routing, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors.
- ReminderScheduler tests cover tomorrow parsing, relative minutes/hours, clock time parsing, due task detection, upcoming task ordering, and invalid due text handling.
- Planner tests cover single-step plans, two-step plans, mixed notes/calculator, mixed task/memory, goal steps, invalid plans, ordering, serialization, ToolSelector plan attachment, SkillManager execution, and REPL plan display.
- Multi-step planner tests cover single-step compatibility, weather plus reminder, goals plus calendar, three-step plans, planner ordering, execution ordering, partial failure recovery, and REPL integration.
- ExecutionPipeline tests cover single-step execution, multi-step execution, notes plus calculator, task plus memory, unrecoverable failure, recoverable partial failure, execution ordering, execution logging, rollback hooks, SkillManager integration, REPL execution display, live REPL multi-step planning, live REPL pipeline execution, live REPL partial failure reporting, and live-path component usage.
- ToolChain tests cover memory plus calculator, note plus memory, task/reminder plus memory, ordering, max depth enforcement, repeated-step loop prevention, and REPL chain display/history.
- Conversation context tests cover history ordering, max history size, clear, retrieval APIs, SkillManager integration, and REPL integration.
- Intent parser tests cover intent detection, confidence values, entity extraction, goal commands, ambiguous local phrasing, unknown intent, ToolSelector integration, SkillManager integration, live REPL parser use, and the REPL task path.
- `git diff --check` passed after the automated test changes.
- Runtime Python checks may not be available in some Windows sessions if `python`/`py` are not installed, only Microsoft Store aliases are present.
- Config and logging were left unchanged because the event bus and memory v1 work did not require changes there.

Latest Commits

- `7683dbc` Harden multi-step planner execution
- `8678a16` Add external tool adapter foundation
- `f7f5e78` Add adapter-backed local weather skill
- `7d56178` Harden weather live path tests
- `7e7f3e3` Add adapter-backed local market skill
- `63027a6` Add adapter-backed local calendar skill
- `bc5a4e7` Deepen goals live path integration tests
- `4e5ec44` Add local long-term goal management
- `5bad0d5` Add local tool chaining foundation
- `b742116` Add REPL integration tests for execution pipeline
- `cee3841` Add execution pipeline for planner steps
- `98d8ff0` Document multi-step planner foundation
- `aaca6b4` Add local multi-step planner foundation
- `5cf4a3e` Document reminder scheduler foundation
- `0ba1c90` Add reminder scheduler foundation
- `c7b665d` Document intent parser runtime integration
- `8ae29d7` Deepen intent parser runtime integration tests
- `85f2a6c` Update docs for parser hardening
- `34a7b57` Harden intent parser phrase handling
- `e3c8501` Update docs for intent parser
- `f2e7a6b` Add structured intent parser

Next Planned Step

- Plan GPT fallback integration only after explicit approval.
- Keep CI green before merging or pushing further changes.
- Prefer feature branch -> local verification -> PR -> CI -> merge for future work.
- Do not add real weather APIs, real market APIs, Google Calendar integration, GPT, embeddings, voice, vision, scheduling, notifications, or background automation yet.
- Do not start voice yet.
