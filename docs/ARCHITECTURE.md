ARES Architecture

This document describes the current ARES architecture and the intended integration points for future work. It is a planning document only; it does not introduce new runtime behavior.

Current System Flow

1. The user starts the text REPL with `py interfaces\text_repl.py`.
2. The REPL creates one shared `EventBus`.
3. The REPL creates `MemoryStore` for conversation turns.
4. The REPL creates `UserProfileStore` for persistent user facts.
5. The REPL creates `GoalsStore` for persistent long-term goals.
6. The REPL creates `NotesStore` for persistent local notes.
7. The REPL creates `TasksStore` for persistent offline tasks.
8. The REPL creates the shared in-memory `ConversationContextManager`.
9. The REPL creates `SkillManager`, which owns an offline `ToolAdapterRegistry` with `MockWeatherAdapter`, `MockMarketAdapter`, and `MockCalendarAdapter`, registers the built-in skill plugin, and passes the manager to `IntentRouter`.
10. User input is sent to `IntentRouter`.
11. `IntentRouter` publishes input lifecycle events.
12. When a skill path is checked, `SkillManager` parses user text into `core.Intent` with `core.IntentParser`.
13. `ToolSelector` asks `core.Planner` to build a local execution plan before skill selection. Planner can read existing store context through safe store interfaces provided by `SkillManager`.
14. `ToolSelector` scores the structured intent against registered skill `intent_names`.
15. Priority skills are selected before normal intents only when a skill opts in.
16. `SkillManager` validates executable plans through `core.ToolChain`.
17. `ToolChain` enforces chain depth, loop prevention, and execution tracing.
18. `ToolChain` delegates accepted plans to `core.ExecutionPipeline`.
19. `ExecutionPipeline` executes each `PlanStep` sequentially and records step results.
20. Normal intents run next when no priority skill path handles the message.
21. Non-priority skills are selected by `ToolSelector` as a fallback when no normal intent matches.
22. Responses are published as events.
23. `SkillManager` records handled skill turns in `ConversationContextManager`.
24. The REPL stores each conversation turn in `MemoryStore`.
25. The REPL scans each user message for profile facts and stores them in `UserProfileStore`.

Event Bus

`events.EventBus` is the in-process publish/subscribe layer.

Current responsibilities:

- Publish user input lifecycle events.
- Publish intent and skill detection events.
- Publish response generation events.
- Publish memory and profile write events.
- Keep a bounded event history for verification and tests.

Current event examples:

- `user_message_received`
- `intent_detected`
- `response_generated`
- `memory.recorded`
- `memory.promoted`
- `memory.cleared`
- `profile.fact_saved`
- `goals.recorded`
- `goals.completed`
- `goals.paused`
- `goals.deleted`
- `goals.milestone_added`
- `notes.recorded`
- `notes.deleted`
- `notes.cleared`
- `tasks.recorded`
- `tasks.completed`
- `tasks.deleted`
- `tasks.completed_cleared`
- `skill.registered`
- `skill.plugin_registered`
- `skill.detected`
- `skill.response_generated`
- `execution.started`
- `execution.step_started`
- `execution.step_completed`
- `execution.step_failed`
- `execution.rollback_requested`
- `execution.rollback_failed`
- `execution.completed`
- `tool_chain.started`
- `tool_chain.rejected`
- `tool_chain.completed`

Intent

`core.Intent` is the structured representation of local user intent before skill selection.

Current fields:

- `intent_name`
- `confidence`
- `extracted_entities`
- `raw_text`

The object is intentionally small so deterministic parsers, tests, and future local tools can share one contract.

IntentParser

`core.IntentParser` converts natural language into a structured `Intent` before `ToolSelector` runs.

Current recognized intents:

- `calculate`
- `goal`
- `note`
- `task`
- `memory_recall`
- `weather`
- `market`
- `calendar`
- `time_date`
- `unknown`

Current entity extraction examples:

- `remember buy milk tomorrow` becomes a `task` intent with `action`, `text`, and `due`.
- `remember to buy milk` becomes a `task` intent with task text `buy milk`.
- `remember this idea: build ARES memory` becomes a `note` intent with note text.
- `calculate 15*8` becomes a `calculate` intent with an arithmetic `expression`.
- `add goal build ARES memory` becomes a `goal` intent with an add action and title.
- `add milestone to goal <id> write tests` becomes a `goal` intent with goal id and milestone text.
- `show my notes` becomes a `note` intent with a list action.
- `notes about gym` becomes a `note` intent with a search action and keyword `gym`.
- `remind me about my main goal tomorrow` becomes a `task` intent with task text `my main goal` and due text `tomorrow`.
- `what should I do next for my goals` becomes a `goal` intent with a next-step action.
- `what is my birthday` becomes a `memory_recall` intent for the birthday profile fact.
- `what did I tell you about my job` becomes a `memory_recall` intent with a recall topic.
- `weather in Madrid` becomes a `weather` intent with location `Madrid`, adapter `mock_weather`, and capability `weather.current`.
- `weather tomorrow` becomes a `weather` intent with period `tomorrow` and capability `weather.forecast`.
- `stock nvidia` becomes a `market` intent with symbol `NVIDIA`, adapter `mock_market`, and capability `market.quote`.
- `market price for tesla` becomes a `market` intent with symbol `TESLA`.
- `calendar tomorrow` becomes a `calendar` intent with period `tomorrow`, adapter `mock_calendar`, and capability `calendar.events`.
- `schedule today` becomes a `calendar` intent with period `today`.

The parser is deterministic and offline. It does not use AI, GPT, embeddings, external APIs, or a broad regex-only dispatcher.

Planner

`core.Planner` converts structured intents into local execution plans.

Current planning objects:

- `PlanStep`
- `Plan`
- `MultiStepPlan`
- `Planner`

Current responsibilities:

- Receive an `Intent`.
- Produce ordered `PlanStep` entries.
- Return a regular `Plan` for single-step requests.
- Return a `MultiStepPlan` for compatible requests with more than one executable step.
- Estimate execution order through the step `order` field.
- Skip impossible steps and return planning errors.
- Serialize plans and steps for events, tests, and REPL display.
- Split compatible compound requests such as `What's the weather tomorrow and remind me to go to the gym`.
- Split compatible compound requests such as `Show my goals and today's calendar`.
- Read context through existing `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` interfaces when those stores are injected.
- Resolve context references such as main goal reminders, profile favorite reminders, note topic searches, and next goal actions.
- Return safe local empty-context responses when required context is missing.

Current supported planner targets:

- `notes`
- `tasks`
- `goals`
- `calculator`
- `weather`
- `market`
- `calendar`
- `conversation_memory`
- `planner_context`

Planner boundaries:

- Planner never executes skills.
- Planner does not write memory, goals, notes, or tasks.
- Planner does not read data files directly.
- Planner does not call GPT, voice, notifications, calendar APIs, or external APIs.
- `ToolChain` validates compatible planner steps before execution.
- Planner can hold a `ToolAdapterRegistry` for future adapter-aware planning, but it does not create real external API calls.

ToolChain

`core.ToolChain` validates and traces compatible local multi-step requests before execution.

Current chain objects:

- `ToolChainTraceStep`
- `ToolChainResult`
- `ToolChain`

Current responsibilities:

- Receive a `Plan`.
- Enforce max chain depth 5.
- Reject repeated step signatures to prevent loop-style chains.
- Record an ordered execution trace.
- Record bounded chain history for REPL inspection.
- Publish tool chain lifecycle events.
- Delegate accepted plans to `ExecutionPipeline`.

Current supported chain examples:

- Memory plus calculator.
- Note plus memory.
- Task/reminder plus memory.
- Goal plus calculator.
- Goal plus memory.

ToolChain boundaries:

- ToolChain does not plan.
- ToolChain does not execute skills directly.
- ToolChain does not write memory, goals, notes, or tasks directly.
- ToolChain does not call GPT, voice, notifications, calendar APIs, stocks, or external APIs.
- ToolChain does not change storage formats.

ToolAdapter

`core.ToolAdapter` defines the future adapter contract for external tools without enabling real external integrations yet.

Current adapter objects:

- `ToolRequest`
- `ToolResponse`
- `ToolAdapter`
- `ToolAdapterRegistry`
- `MockWeatherAdapter`
- `MockMarketAdapter`
- `MockCalendarAdapter`

Current metadata fields:

- `name`
- `description`
- `capabilities`
- `requires_network`
- `requires_auth`

Current responsibilities:

- Register local adapters.
- Look up adapters by name.
- Find adapters by capability.
- Return clear missing-adapter and unsupported-capability responses.
- Provide offline mock weather and market responses for tests.

Current boundaries:

- Mock adapters do not call real APIs.
- Mock adapters do not require network.
- Mock adapters do not require authentication.
- No API keys are read or stored.
- WeatherSkill uses `MockWeatherAdapter` through this registry for local weather answers.
- MarketSkill uses `MockMarketAdapter` through this registry for local market quote answers.
- CalendarSkill uses `MockCalendarAdapter` through this registry for local schedule answers.
- No real weather API, real market API, Google Calendar integration, real calendar API, GPT, voice, or web adapter has been added.

ExecutionPipeline

`core.ExecutionPipeline` executes plans produced by `core.Planner`.

Current execution objects:

- `StepResult`
- `ExecutionResult`
- `RollbackHook`
- `ExecutionPipeline`

Current responsibilities:

- Receive a `Plan`.
- Execute each `PlanStep` in order.
- Resolve local skill targets through `SkillManager` and `SkillRegistry`.
- Execute conversation memory steps through `MemoryStore`.
- Execute internal `planner_context` response steps for deterministic context-only answers.
- Execute explicit `tool_adapter` steps through an injected `ToolAdapterRegistry`.
- Stop safely on unrecoverable failures such as missing skills or raised exceptions.
- Continue after recoverable skill-level failures, such as safe local tool rejection.
- Continue remaining steps after recoverable failures and report partial success.
- Aggregate all step outputs into one final response.
- Record start time, end time, duration, success/failure, returned data, and error messages for every step.
- Publish execution lifecycle events.
- Emit standard execution logs through the `ares.execution` logger.
- Expose a no-op `RollbackHook` extension point for future reversible local actions.

Current integration verification:

- Live REPL tests verify multi-step plan creation.
- Live REPL tests verify weather plus reminder multi-step execution.
- Live REPL tests verify goals plus calendar multi-step execution.
- Live REPL tests verify notes plus calculator execution through ExecutionPipeline.
- Live REPL tests verify task plus memory execution through ExecutionPipeline.
- Live REPL tests verify goal add, list, add milestone, pause, complete, and show commands.
- Live REPL tests verify weather requests through `WeatherSkill` and `MockWeatherAdapter`.
- Hardened weather live-path tests verify `IntentParser -> Planner -> ExecutionPipeline -> WeatherSkill -> MockWeatherAdapter` through the text REPL.
- Live REPL tests verify market requests through `MarketSkill` and `MockMarketAdapter`.
- Live REPL tests verify calendar requests through `CalendarSkill` and `MockCalendarAdapter`.
- ToolChain tests verify repeated weather steps are rejected before execution to prevent loop-style chains.
- Live REPL tests verify recoverable partial failure reporting and continued execution.
- Multi-step planner tests verify single-step compatibility, two-step plans, three-step plans, planner ordering, execution ordering, and partial-result formatting.
- Context-aware planner tests verify goal context, profile favorite context, note topic context, related task context, missing-context responses, multi-step context plans, partial failure recovery, and REPL integration.
- Live REPL tests verify `show execution` and `show last execution`.
- A live-path spy verifies the active path uses `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill`.

Execution boundaries:

- ExecutionPipeline does not plan.
- ExecutionPipeline does not create new skills.
- ExecutionPipeline does not call GPT, voice, notifications, calendar APIs, or external APIs.
- ExecutionPipeline does not choose external providers; it only executes explicit adapter steps already present in a plan.
- Rollback hooks are defined, but no real rollback behavior is implemented yet.

Intent Router

`core.intent_router.IntentRouter` remains the main text routing path.

Current order:

1. Empty input handling.
2. Priority skill fallback path for skills that must run before generic intents.
3. Normal intent modules.
4. Non-priority skill fallback path.
5. Unknown response.

Both skill paths pass through `SkillManager`, which parses text into `Intent` before selection.

Current intent modules:

- Greeting
- Goodbye
- Weather
- News
- Knowledge
- Stocks

MemoryStore

`memory.MemoryStore` is the v1 structured memory interface for conversation-style memories.

Current responsibilities:

- Store short-term memories.
- Store long-term memories.
- Recall memories by category, tags, text query, and importance.
- Promote a short-term memory to long-term.
- Clear memory files when explicitly requested.

Current storage:

- `data/memories_short.json`
- `data/memories_long.json`

UserProfileStore

`memory.UserProfileStore` stores user facts separately from conversation history.

Current responsibilities:

- Detect profile facts from user text.
- Store profile facts persistently.
- Recall profile values for personal memory questions.

Current supported fact patterns:

- `My name is...`
- `I live in...`
- `My birthday is...`
- `My favorite ... is...`
- `I own...`

Current storage:

- `data/user_profile.json`

The profile file is ignored by git because it can contain personal facts. Tests can override the path with `ARES_USER_PROFILE_PATH`.

GoalsStore

`memory.GoalsStore` stores long-term user goals separately from conversation history, user profile facts, notes, and tasks.

Current responsibilities:

- Add goals with id, title, description, created timestamp, status, priority, and milestones.
- List all goals.
- Show one goal by id.
- Mark one goal completed by id.
- Pause one goal by id.
- Delete one goal by id.
- Add milestones to one goal by id.

Current storage:

- `data/goals.json`

The goals file is ignored by git because it can contain personal goals. Tests can override the path with `ARES_GOALS_PATH`.

NotesStore

`memory.NotesStore` stores user-created notes separately from conversation history, user profile facts, goals, and tasks.

Current responsibilities:

- Add notes with a unique id, timestamp, and text.
- List all notes.
- Search notes by keyword.
- Delete one note by id.
- Clear all notes only through an explicit confirmation flow in `NotesSkill`.

Current storage:

- `data/notes.json`

The notes file is ignored by git because it can contain personal notes. Tests can override the path with `ARES_NOTES_PATH`.

TasksStore

`memory.TasksStore` stores offline tasks and simple reminders separately from conversation history, user profile facts, goals, and notes.

Current responsibilities:

- Add tasks with an id, text, created timestamp, optional due text, and completed flag.
- List all tasks.
- Mark one task completed by id.
- Delete one task by id.
- Clear completed tasks.

Current storage:

- `data/tasks.json`

The tasks file is ignored by git because it can contain personal tasks. Tests can override the path with `ARES_TASKS_PATH`.

ReminderScheduler

`memory.ReminderScheduler` is the passive local due-time layer for tasks.

Current responsibilities:

- Parse stored task due text with `parse_due_text(text)`.
- Support simple due phrases: `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- Return incomplete due tasks with `due_tasks(now)`.
- Return incomplete upcoming tasks ordered by parsed due time with `upcoming_tasks(now, limit)`.
- Ignore invalid or unsupported due text safely.

Current boundaries:

- It reads from `TasksStore`.
- It does not change `data/tasks.json`.
- It does not schedule jobs.
- It does not send notifications.
- It does not call calendar APIs.
- It does not use GPT or any external API.

ConversationContextManager

`core.ConversationContextManager` stores short-term conversational context in RAM only.

Current responsibilities:

- Keep the last 20 handled skill turns.
- Store timestamp, user message, assistant response, and detected skill for each turn.
- Return the latest turn through `last_message()`.
- Return latest user text, assistant text, and skill through `last_user_message()`, `last_assistant_message()`, and `last_skill()`.
- Return ordered recent history with `history(limit)`.
- Clear in-memory state with `clear()`.

Current storage:

- RAM only

Conversation context is not saved to disk. It does not use embeddings, GPT, external APIs, or voice.

SkillRegistry

`skills.SkillRegistry` owns skill registration and lookup.

Current responsibilities:

- Register skills.
- Reject duplicate skill names.
- Return all skills.
- Find matching skills for text input.
- Filter priority skills with `run_before_intents`.

ToolSelector

`skills.ToolSelector` chooses the best local skill for a user text request.

ToolSelector also builds and attaches a `Plan` before returning a selection. This keeps planning visible before execution while preserving the existing skill scoring rules.

Current scoring rules:

- Matching structured intent name gets priority over trigger scoring.
- Exact trigger match gets the strongest confidence.
- Contained trigger phrase gets high confidence.
- Trigger token overlap gets partial confidence.
- Skills can add `selection_keywords` without changing selector code.
- Skills can add `selection_priority` for explicit tie-breaking.
- Selection can be filtered with `run_before_intents`.
- Exact and contained trigger fallback paths can still run when the structured intent is `unknown`.
- Loose token-overlap fallback is disabled for `unknown` structured intents so generic text does not get misrouted to a local skill.
- `can_handle` fallback remains available for unknown intents and compatibility.

Current supported runtime skills:

- `TimeDateSkill`
- `MemoryRecallSkill`
- `CalculatorSkill`
- `CalendarSkill`
- `GoalsSkill`
- `MarketSkill`
- `NotesSkill`
- `TasksSkill`
- `WeatherSkill`

Future local skills should define clear triggers and optional `selection_keywords` so they can use the same selector without a giant if/else chain.
New deterministic skills should also define `intent_names` when they have a parser-recognized intent.

SkillManager

`skills.SkillManager` owns skill detection and execution.

Current responsibilities:

- Register individual skills.
- Register skill plugins.
- Parse user text into `Intent` with `IntentParser`.
- Select the best matching local skill through `ToolSelector`.
- Validate executable local plans through `ToolChain`.
- Delegate accepted local plans to `ExecutionPipeline`.
- Execute a skill with `SkillContext`.
- Record each handled skill interaction in `ConversationContextManager`.
- Publish skill lifecycle events.

Skill context currently carries:

- `event_bus`
- `memory_store`
- `profile_store`
- `goals_store`
- `notes_store`
- `tasks_store`
- `tool_adapter_registry`
- `conversation_context`
- `metadata`

For handled skills, `metadata` includes the parsed `intent` and extracted `entities`.

Built-In Skills

Current built-in plugin:

- `skills.builtin.create_builtin_plugin`

Current built-in skills:

- `TimeDateSkill`
- `MemoryRecallSkill`
- `CalculatorSkill`
- `CalendarSkill`
- `GoalsSkill`
- `MarketSkill`
- `NotesSkill`
- `TasksSkill`
- `WeatherSkill`

`TimeDateSkill` answers local time and date questions.

`MemoryRecallSkill` answers profile questions from `UserProfileStore` without using an LLM. It is a priority skill so questions such as `What is my name?` are answered before the generic knowledge intent.

`CalculatorSkill` answers local arithmetic questions without using an LLM. It supports addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers through AST parsing and explicit operator handling, not `eval()`. It rejects unsupported or unsafe input with a clear response.

`GoalsSkill` stores and manages long-term local goals through `GoalsStore`. It supports `add goal...`, `list goals`, `show goal <id>`, `complete goal <id>`, `pause goal <id>`, `delete goal <id>`, and `add milestone to goal <id> ...`. It does not run autonomous background actions or notifications.

`NotesSkill` stores, lists, searches, and deletes local notes through `NotesStore`. It supports `remember this...`, `save note...`, `take a note...`, `list my notes`, `show my notes`, `delete note <id>`, `delete all notes`, and `search notes <keyword>`. `delete all notes` requires explicit confirmation with `confirm delete all notes`.

`TasksSkill` stores and manages offline reminders/tasks through `TasksStore`. It supports `add task...`, `remind me to...`, `list tasks`, `show tasks`, `mark task <id> done`, `delete task <id>`, and `clear completed tasks`. It stores optional due text but does not notify.

`TasksSkill` can also consume parser-derived entities, so text such as `remember buy milk tomorrow` is stored as task text `buy milk` with due text `tomorrow`.

`WeatherSkill` answers weather requests through `ToolAdapterRegistry` and the offline `MockWeatherAdapter`. It supports `weather`, `weather today`, `weather tomorrow`, and `weather in Madrid`. It does not call real APIs, require API keys, or use internet access.

`MarketSkill` answers stock/market quote requests through `ToolAdapterRegistry` and the offline `MockMarketAdapter`. It supports `stock nvidia`, `nvidia stock`, `apple stock`, and `market price for tesla`. It does not call real APIs, require API keys, or use internet access.

`CalendarSkill` answers calendar/schedule requests through `ToolAdapterRegistry` and the offline `MockCalendarAdapter`. It supports `what is on my calendar today`, `calendar tomorrow`, `schedule today`, and `do I have anything tomorrow`. It does not call Google Calendar, real APIs, require API keys, use internet access, or run background automation.

No notifications, voice, real weather APIs, real market APIs, Google Calendar integration, real calendar APIs, external API, or GPT integration has been added as part of the current local skill milestones.

Conversation context is not a persistent memory store. It only tracks recent handled skill turns in RAM so local skills and interfaces can inspect short-term context without GPT or embeddings.

REPL Flow

`interfaces.text_repl` is the active user interface.

Current responsibilities:

- Wake on `hello`, `hello ares`, `hi ares`, or `hey ares`.
- Exit on `goodbye`, `goodbye ares`, `exit`, or `quit`.
- Share one event bus across router, memory, profile, goals, notes, tasks, conversation context, and skills.
- Store each user/ARES turn as a conversation memory.
- Scan each user message for profile facts.
- Route goal commands to `GoalsSkill` and persist goals in `GoalsStore`.
- Route note commands to `NotesSkill` and persist notes in `NotesStore`.
- Route task commands to `TasksSkill` and persist tasks in `TasksStore`.
- Route weather commands to `WeatherSkill` through `MockWeatherAdapter`.
- Route stock/market commands to `MarketSkill` through `MockMarketAdapter`.
- Route calendar/schedule commands to `CalendarSkill` through `MockCalendarAdapter`.
- Route parser-recognized local intents through `SkillManager` and `ToolSelector`.
- Preserve unknown input safety when IntentParser returns `unknown`.
- Show the last plan with `show plan` or `show steps`.
- Show the last execution result with `show execution` or `show last execution`.
- Show the last tool chain with `show chain`.
- Show chain history with `show chain history`.
- Share one in-memory conversation context with `SkillManager` for handled skill turns.
- Print the final ARES response.

Future Integration Points

Voice

Voice should connect at the interface layer, beside the text REPL. It should reuse:

- `EventBus`
- `IntentRouter`
- `MemoryStore`
- `UserProfileStore`
- `GoalsStore`
- `NotesStore`
- `TasksStore`
- `ConversationContextManager`
- `SkillManager`
- `ToolChain`
- `ExecutionPipeline`

Voice must not bypass the existing routing, memory, or verification rules.

Vision

Vision should enter as a separate interface or provider layer. It should publish events and store structured observations only after the data model and safety rules are defined.

LLM Integration

LLM integration is not active.

Future LLM calls should be added only behind clear interfaces. They should not replace deterministic skills for answers already known from memory, such as user profile recall.
They should also not replace deterministic parser routes for local skills that already have structured intent coverage.

Raspberry Pi Deployment

Deployment scripts already exist, but no new Raspberry Pi deployment work should begin until the roadmap and architecture plan are approved.

Testing Boundary

Every architecture change must keep this suite passing:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```

Current verification snapshot:

- Pytest collection: 172 tests.
- Current local foundation modules include `core.IntentParser`, `core.Planner`, `core.MultiStepPlan`, `core.ToolAdapter`, `core.ToolChain`, `core.ExecutionPipeline`, `core.ConversationContextManager`, `memory.GoalsStore`, `memory.TasksStore`, `memory.ReminderScheduler`, `skills.builtin.GoalsSkill`, `skills.builtin.TasksSkill`, `skills.builtin.WeatherSkill`, `skills.builtin.MarketSkill`, and `skills.builtin.CalendarSkill`.
