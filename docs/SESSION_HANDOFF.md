ARES Session Handoff

Last Updated: 2026-06-30

Current Version

ARES v1.7

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
- ConversationContextManager
- IntentParser
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
- Currently routes `TimeDateSkill`, `MemoryRecallSkill`, `CalculatorSkill`, `NotesSkill`, and `TasksSkill`.

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
- `skills.builtin.TasksSkill`

Tasks behavior:

- Stores tasks in `data/tasks.json`, which is ignored by git.
- Keeps tasks separate from conversation memory, user profile memory, and notes.
- Supports `add task...`, `remind me to...`, `list tasks`, `show tasks`, `mark task <id> done`, `delete task <id>`, and `clear completed tasks`.
- Each task stores id, text, created timestamp, optional due text, and completed state.
- Stores due text only; no real scheduling, notifications, calendar integration, voice, or GPT integration has been added.
- Publishes `tasks.recorded`, `tasks.completed`, `tasks.deleted`, and `tasks.completed_cleared`.
- Uses `ARES_TASKS_PATH` for test isolation.

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
- `IntentParser` recognizes `calculate`, `note`, `task`, `memory_recall`, `time_date`, and `unknown`.
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
- Does not schedule tasks or send notifications.

Conversation Context:

- Stores recent handled skill turns in RAM only.
- Default limit is 20 turns.
- Is separate from `MemoryStore`, `UserProfileStore`, `NotesStore`, and `TasksStore`.
- Is shared by the text REPL through `core.get_global_conversation_context()`.
- Does not write any conversation context file.

Text REPL memory:

- `interfaces.text_repl` creates one shared event bus and passes it to `IntentRouter`, `MemoryStore`, `UserProfileStore`, `NotesStore`, `TasksStore`, `ConversationContextManager`, and `SkillManager`.
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

The built-in skill plugin currently registers `MemoryRecallSkill`, `CalculatorSkill`, `NotesSkill`, `TasksSkill`, and `TimeDateSkill`.
The REPL priority skill path currently covers profile memory recall, calculator arithmetic, note commands, and task commands.
`SkillManager` parses text into `core.Intent` before `ToolSelector` selects a local skill.
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

Next scoped local tool or provider decision.

Next technical choices:

- Add profile acknowledgement responses if desired; current fact statements are stored even when the response is generic.
- Decide whether the next approved runtime capability is company information or another documented provider.
- Keep voice, GPT, embeddings, external weather/stocks/calendar APIs, real scheduling, notifications, and Raspberry Pi deployment out of scope until explicitly approved.
- Connect selected daily reflection scripts and future providers to MemoryStore v1 only after the memory contract is documented.

---

Future Roadmap

1. Company Provider
2. Cryptocurrency Provider
3. Better reasoning
4. Memory v1 integration with conversations
5. Long-term memory retrieval
6. Company profile provider
7. Voice interface
8. Vision
9. Robotics
10. Jetson Orin migration
11. Autonomous ARES

Verification Notes

- `scripts/verify_phase2_events_memory.py` verifies router event publication and memory turn storage with temporary memory files.
- Run it with `python scripts/verify_phase2_events_memory.py`.
- Automated tests run with `py -m pytest`.
- Phase 3 skill package compiles with `py -m compileall skills`.
- `SkillManager` was manually checked with the built-in time/date skill.
- Text REPL was verified with `hello`, `what time is it`, `what date is it`, and `quit`.
- Long-term profile recall was verified through the text REPL with name, location, birthday, favorite tank, and owned item facts.
- Current verification passed:
  - `py -m pytest`
  - `py -m compileall core interfaces events memory skills scripts`
  - `py scripts\verify_phase2_events_memory.py`
- GitHub Actions CI runs the same verification suite on Windows with Python 3.13 for `main` pushes and pull requests.
- Latest checked GitHub Actions run on `main` completed successfully for commit `e37e5d4`.
- Tool selection tests cover current TimeDate/MemoryRecall/Calculator/Notes/Tasks selection.
- Calculator tests cover simple arithmetic, precedence, parentheses, decimals, bounded powers, unsafe input rejection, and the REPL routing path.
- Notes tests cover add, list, search, delete, duplicate note text, empty note rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Tasks tests cover add, list, mark done, delete, empty task rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Conversation context tests cover history ordering, max history size, clear, retrieval APIs, SkillManager integration, and REPL integration.
- Intent parser tests cover intent detection, confidence values, entity extraction, ambiguous local phrasing, unknown intent, ToolSelector integration, SkillManager integration, live REPL parser use, and the REPL task path.
- `git diff --check` passed after the automated test changes.
- Runtime Python checks may not be available in some Windows sessions if `python`/`py` are not installed, only Microsoft Store aliases are present.
- Config and logging were left unchanged because the event bus and memory v1 work did not require changes there.

Latest Commits

- `0faffc1` Add long-term profile memory recall
- `97fcbeb` Add automated pytest suite
- Documentation update for tests and current architecture status
- Documentation update for strict engineering rules
- Documentation update for master architecture and roadmap docs
- Tool selection foundation with scoring and tests
- CalculatorSkill with safe arithmetic tests
- Persistent NotesSkill with storage and routing tests
- Persistent TasksSkill with storage and routing tests
- In-memory ConversationContextManager with SkillManager and REPL tests
- GitHub Actions CI for local verification commands
- `f2e7a6b` Add structured intent parser
- `34a7b57` Harden intent parser phrase handling
- `8ae29d7` Deepen intent parser runtime integration tests

Next Planned Step

- Review and approve the next local tool or provider scope.
- Keep CI green before merging or pushing further changes.
- Prefer feature branch -> local verification -> PR -> CI -> merge for future work.
- Do not add weather, stocks, calendar, GPT, embeddings, voice, vision, scheduling, or notifications yet.
- Do not start voice yet.
