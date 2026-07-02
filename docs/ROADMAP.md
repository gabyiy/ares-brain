ARES Roadmap

This roadmap describes the current state and planned direction. It is a planning document only; it does not introduce new runtime behavior.

Completed Phases

Phase 1: Modular Text Intelligence

- Modular intent router
- Greeting intent
- Goodbye intent
- Weather intent and provider
- News intent and provider
- Knowledge intent and provider
- Stock intent and Alpha Vantage provider
- HTTP client
- Cache system

Phase 2: Event and Memory Foundation

- Event bus
- Router lifecycle events
- Memory v1 interface
- Conversation turn storage from the text REPL
- Phase 2 verification script

Phase 3: Skill Foundation

- Base `Skill` interface
- `SkillContext`
- `SkillResponse`
- `SkillRegistry`
- `SkillManager`
- `SkillPlugin`
- Built-in skill plugin
- Built-in `TimeDateSkill`
- Text REPL skill fallback wiring

Phase 4: Long-Term Profile Memory Recall

- Persistent `UserProfileStore`
- User facts stored separately from conversation history
- Supported profile fact detection
- Built-in `MemoryRecallSkill`
- Priority skill routing for personal recall questions
- Automated pytest suite
- Strict engineering rules

Phase 4B: Tool Selection Foundation

- `ToolSelector`
- `ToolSelection`
- Confidence/scoring rules for local skills
- Priority selection before generic intents
- Fallback selection after normal intents
- Tests for current TimeDate/MemoryRecall skills
- Tests for current Calculator, Notes, and Tasks skill selection

Phase 4C: Local Calculator Skill

- Built-in `CalculatorSkill`
- Safe local arithmetic without `eval()`
- Addition, subtraction, multiplication, division, parentheses, decimals, and bounded powers
- Clear rejection for unsupported or unsafe input
- REPL routing through `ToolSelector` and `SkillManager`
- Automated calculator and REPL path tests

Phase 5: Local Notes Skill

- Persistent `NotesStore`
- Notes stored in `data/notes.json`
- Notes kept separate from conversation memory and user profile memory
- Built-in `NotesSkill`
- Add, list, search, delete-one, and confirmed delete-all commands
- Automated store, skill, selector, and REPL path tests

Phase 6: Local Tasks Skill

- Persistent `TasksStore`
- Tasks stored in `data/tasks.json`
- Tasks kept separate from conversation memory, user profile memory, and notes
- Built-in `TasksSkill`
- Add, list, mark done, delete-one, and clear-completed commands
- Optional due text storage without real scheduling
- No notifications or calendar integration
- Automated store, skill, selector, and REPL path tests

Phase 7: In-Memory Conversation Context

- `ConversationContextManager`
- Last 20 handled skill turns kept in RAM
- Turn fields: timestamp, user message, assistant response, detected skill
- Retrieval APIs for last message, last user message, last assistant message, last skill, history, and clear
- `SkillManager` records handled skill interactions automatically
- REPL uses shared in-memory context for skill turns
- No disk persistence, embeddings, GPT, external APIs, or voice integration

Phase 8: Structured Intent Parser

- `core.Intent`
- `core.IntentParser`
- Deterministic local intent parsing before ToolSelector runs
- Recognized intents: `calculate`, `goal`, `note`, `task`, `memory_recall`, `weather`, `market`, `calendar`, `time_date`, and `unknown`
- Entity extraction for local tools, including task text, due text, note actions, calculator expressions, and memory recall topics
- `SkillManager` consumes `Intent` objects before calling `ToolSelector`
- Skills declare `intent_names` for structured matching
- Automated parser, ToolSelector, SkillManager, and REPL path tests
- Hardened parser coverage for ambiguous local phrases such as `remember to buy milk`, note reminders, birthday recall, task actions, note actions, calculator requests, and unknown text
- Live REPL integration tests confirm IntentParser output is used before local skill selection
- Unknown structured intents preserve safe fallback behavior and avoid loose token-overlap skill routing
- No AI, GPT, embeddings, voice, or external API integration

Phase 9: ReminderScheduler Foundation

- `memory.ReminderScheduler`
- Passive due-time parsing for existing task due text
- Supported phrases: `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`
- `parse_due_text(text)`
- `due_tasks(now)`
- `upcoming_tasks(now, limit)`
- Invalid due text is ignored safely
- No notifications, calendar integration, voice, GPT, external APIs, or storage format changes

Phase 10: Multi-Step Task Planner Foundation

- `core.PlanStep`
- `core.Plan`
- `core.MultiStepPlan`
- `core.Planner`
- Planner receives an `Intent` and produces ordered executable steps.
- Planner returns a regular `Plan` for single-step requests.
- Planner returns a `MultiStepPlan` for compatible requests with more than one executable step.
- Supported planner targets: goals, notes, tasks, calculator, weather, market, calendar, and conversation memory.
- Planner skips impossible steps and returns errors cleanly.
- Planner serializes plans for events, tests, and REPL display.
- Planner can split compatible compound requests such as `What's the weather tomorrow and remind me to go to the gym`.
- Planner can split compatible compound requests such as `Show my goals and today's calendar`.
- Planner can use injected profile, goals, notes, and tasks stores through their public interfaces.
- Planner returns safe local empty-context responses when requested context is unavailable.
- ToolSelector builds a plan before returning a skill selection.
- Planner itself never executes skills.
- Text REPL supports `show plan` and `show steps`.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes

Phase 11: Execution Pipeline Foundation

- `core.StepResult`
- `core.ExecutionResult`
- `core.RollbackHook`
- `core.ExecutionPipeline`
- ExecutionPipeline receives a `Plan` and executes each `PlanStep` sequentially.
- Each step records start time, end time, duration, success/failure, returned data, and error messages.
- SkillManager delegates executable planner steps to ExecutionPipeline.
- ExecutionPipeline pauses before destructive or important actions and returns confirmation requests.
- ExecutionPipeline can execute internal `planner_context` response steps.
- ExecutionPipeline stops on unrecoverable failures and continues after recoverable local tool failures when appropriate.
- ExecutionPipeline aggregates every step output into one final response.
- Mixed successful and failed recoverable steps are reported as partial results while remaining steps continue.
- ExecutionPipeline publishes execution events and emits standard logs.
- Rollback hook interface exists as a no-op extension point.
- Text REPL supports `show execution` and `show last execution`.
- Live REPL integration tests now verify multi-step plan creation, notes plus calculator execution, task plus memory execution, recoverable partial failure reporting, last execution display, and the active `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill` path.
- No new skills, GPT, voice, notifications, calendar integration, external APIs, or storage format changes

Phase 12: Tool Chaining Foundation

- `core.ToolChain`
- `core.ToolChainResult`
- `core.ToolChainTraceStep`
- Max chain depth is 5
- Repeated step signatures are rejected to prevent loop-style chains
- ToolChain records ordered execution traces and bounded chain history
- SkillManager validates executable plans through ToolChain before ExecutionPipeline runs
- Text REPL supports `show chain` and `show chain history`
- Tests cover memory plus calculator, note plus memory, task/reminder plus memory, ordering, max depth, loop prevention, and REPL chain display
- No new external APIs, GPT, voice, weather, stocks, calendar, notifications, or storage format changes

Phase 13: Long-Term Goal Management Foundation

- `memory.GoalsStore`
- `memory.GoalRecord`
- `skills.builtin.GoalsSkill`
- Goals are stored in `data/goals.json`
- Goals stay separate from conversation memory, user profile memory, notes, and tasks
- Goal fields include id, title, description, created timestamp, active/completed/paused status, priority, and milestones
- GoalsSkill supports add, list, show, complete, pause, delete, and add-milestone commands
- `IntentParser`, `ToolSelector`, `Planner`, `ToolChain`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `goal` intent
- Tests cover store persistence, skill commands, selector routing, parser routing, planner steps, execution pipeline, SkillManager, ToolChain goal chains, REPL lifecycle commands, and persistence after reload
- No GPT, autonomous background actions, notifications, external APIs, voice, weather, stocks, or calendar integration

Phase 14: External Tool Adapter Foundation

- `core.ToolAdapter`
- `core.ToolRequest`
- `core.ToolResponse`
- `core.ToolAdapterRegistry`
- Adapter metadata includes name, description, capabilities, `requires_network`, and `requires_auth`
- `MockWeatherAdapter` returns offline mock weather data only
- `MockMarketAdapter` returns offline mock market data only
- Planner accepts an optional ToolAdapterRegistry for future adapter-aware planning
- ExecutionPipeline can execute explicit `tool_adapter` PlanSteps through an injected registry
- Tests cover adapter registration, lookup, missing adapters, mock weather, mock market, no-network metadata, and safe pipeline execution
- No real APIs, API keys, GPT, voice, stock skill, calendar integration, or web adapter

Phase 15: Adapter-Backed Weather Skill

- `skills.builtin.WeatherSkill`
- Uses `ToolAdapterRegistry` and `MockWeatherAdapter`
- Supports `weather`, `weather today`, `weather tomorrow`, and `weather in Madrid`
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `weather` intent
- Tests cover weather intent parsing, mock adapter calls, WeatherSkill responses, planner weather steps, execution pipeline weather steps, REPL routing, full live path into `MockWeatherAdapter`, ToolChain loop prevention for repeated weather steps, and missing adapter errors
- No real APIs, API keys, internet access, GPT, voice, calendar integration, stocks, or notifications

Phase 16: Adapter-Backed Market Skill

- `skills.builtin.MarketSkill`
- Uses `ToolAdapterRegistry` and `MockMarketAdapter`
- Supports `stock nvidia`, `nvidia stock`, `apple stock`, and `market price for tesla`
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `market` intent
- Tests cover market intent parsing, mock adapter calls, MarketSkill responses, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors
- No real APIs, API keys, internet access, GPT, voice, calendar integration, notifications, or real market provider integration

Phase 17: Adapter-Backed Calendar Skill

- `core.MockCalendarAdapter`
- `skills.builtin.CalendarSkill`
- Uses `ToolAdapterRegistry` and `MockCalendarAdapter`
- Supports `what is on my calendar today`, `calendar tomorrow`, `schedule today`, and `do I have anything tomorrow`
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `calendar` intent
- Tests cover calendar intent parsing, mock adapter calls, CalendarSkill responses, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors
- No Google Calendar integration, real APIs, API keys, internet access, GPT, voice, notifications, or background automation

Phase 18: Multi-Step Planner Hardening

- Explicit `core.MultiStepPlan` support
- Single-step requests remain compatible with regular `Plan` objects
- Compatible multi-step requests produce ordered `MultiStepPlan` objects
- Supported examples include weather plus reminder and goals plus calendar requests
- ExecutionPipeline continues after recoverable step failures and aggregates all step outputs
- Mixed successful and failed recoverable steps are labeled as partial results
- Tests cover single-step compatibility, two-step plans, three-step plans, planner ordering, execution ordering, partial failure recovery, and REPL integration
- No GPT, internet access, real APIs, voice, notifications, or background automation

Phase 19: Context-Aware Planner Foundation

- Planner reads `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` through existing safe interfaces only
- Planner does not read data files directly
- Planner does not write memory, goals, notes, or tasks
- Supports context-aware examples such as `remind me about my main goal tomorrow`, `what should I do next for my goals`, and `show my goals and notes about gym`
- Adds internal `planner_context` response steps for deterministic context-only answers and missing-context responses
- SkillManager injects its existing store handles into ToolSelector's Planner
- ExecutionPipeline executes `planner_context` steps without requiring a new public skill
- Tests cover goal context, profile favorite context, notes context, task context, missing context, multi-step context plans, partial failure recovery, and REPL integration
- No GPT, internet access, real APIs, voice, notifications, or background automation

Phase 20: Action Confirmation Layer

- `core.ConfirmationRequest`
- `core.ConfirmationDecision`
- `core.ConfirmationManager`
- One in-memory pending confirmation id for the active runtime
- ExecutionPipeline pauses destructive or important actions before skill execution
- SkillManager handles `yes`, `confirm`, `no`, and `cancel` as confirmation decisions
- Confirmed actions rerun with an approval marker
- Cancelled actions do not execute
- Missing pending confirmation fails safely
- Protected actions include note deletion, delete-all notes, task deletion, clear-completed tasks, goal delete/pause/complete, and future external adapter write/delete actions
- Multi-step plans pause safely at a confirmation step without executing the protected action or later steps
- Tests cover confirmation-required actions, confirm, cancel, missing pending confirmation, unaffected weather/market/calendar paths, future external writes, and multi-step pause behavior
- No GPT, internet access, real APIs, voice, notifications, or background automation

Phase 21: External Adapter Config and Secrets Guard

- `core.ExternalAdapterConfig`
- `core.SecretsGuard`
- `core.SecretValidationError`
- `config/adapters.example.json`
- Adapter config fields include enabled state, mock/local/real mode, API key environment variable name, base URL, and timeout seconds
- `ToolAdapterRegistry` enforces adapter config before adapter execution
- Mock/local mode preserves existing offline WeatherSkill, MarketSkill, and CalendarSkill behavior
- Real mode fails safely without a configured env key and does not call real APIs
- Fake placeholder values are accepted only as placeholders
- Raw-looking API keys and tokens are rejected
- Local/private adapter config files are ignored by git
- Tests cover mock mode, real-mode missing-env failure, placeholder handling, raw-secret rejection, example config loading, read-only mock adapter behavior, and confirmation-layer compatibility
- No real APIs, API keys, internet access, GPT, voice, notifications, or background automation

Phase 22: Real Weather Adapter Skeleton

- `core.RealWeatherAdapter`
- Explicit `real_weather` adapter name for future real weather capability
- Supports weather current and forecast capabilities
- Requires network/auth metadata but is not registered in the default SkillManager adapter registry
- Reads API keys only from the configured environment variable name
- Fails safely when the env key is missing
- `config/adapters.example.json` keeps real weather disabled and mock-mode by default with a fake placeholder env name
- Existing WeatherSkill behavior stays on `mock_weather` unless an explicit real-weather adapter is configured and selected
- Tests cover default mock behavior, real-mode missing-env failure, env-name-only config, adapter instantiation, safe WeatherSkill failure, and SecretsGuard compatibility
- No real API keys, real API calls, GPT, voice, calendar writes, notifications, or background jobs

Phase 23: Real Weather Adapter HTTP Logic

- RealWeatherAdapter performs weather HTTP requests only when adapter config mode is `real`
- Required API key value is read only from the configured environment variable name
- Raw keys are not stored in config and are not returned in adapter responses
- Configured timeout seconds are passed to the HTTP client
- Response normalization returns stable ARES weather data: location, condition, temperature C, period, capability, and source
- HTTP timeout, HTTP status errors, invalid JSON, and unsupported payloads return safe deterministic errors
- Tests mock HTTP and do not make real network calls
- Default WeatherSkill behavior remains `mock_weather`
- No real API keys, default real mode, GPT, voice, calendar writes, stocks real API, notifications, or background jobs

Phase 24: Real Market Adapter Skeleton

- `core.RealMarketAdapter`
- Explicit `real_market` adapter name for future real market capability
- Supports market quote and summary capabilities
- Requires network/auth metadata but is not registered in the default SkillManager adapter registry
- Reads API keys only from the configured environment variable name
- Fails safely when the env key is missing
- Returns a deterministic not-implemented response instead of making network calls
- `config/adapters.example.json` keeps real market disabled and mock-mode by default with a fake placeholder env name
- Existing MarketSkill behavior stays on `mock_market` unless an explicit real-market adapter is configured and selected
- Tests cover default mock behavior, real-mode missing-env failure, env-name-only config, adapter instantiation, safe MarketSkill failure, and SecretsGuard compatibility
- No real API keys, real market API calls, GPT, voice, calendar writes, notifications, or background jobs

Current State

ARES is currently a text-first assistant with deterministic routing, structured local intent parsing, explicit multi-step planning, context-aware planning through safe local store interfaces, an action confirmation layer for destructive or important actions, bounded local tool chaining, sequential local plan execution with aggregated responses and partial-result reporting, deterministic skills, event publishing, conversation memory, user profile memory, long-term local goals, local calculator arithmetic, persistent local notes, offline tasks, adapter-backed mock weather answers, an opt-in real-weather HTTP adapter gated by config and env keys, adapter-backed mock market quotes, an opt-in real-market skeleton gated by config and env keys, adapter-backed mock calendar answers, external tool adapter contracts with offline mocks, external adapter config and secrets guarding for future real APIs, and short-term in-memory conversation context for handled skill turns.

The current active interface is:

- `interfaces.text_repl`

The current deterministic answer paths are:

- Intent modules for weather, news, knowledge, stocks, greetings, and goodbye
- `IntentParser` plus `ToolSelector` for time/date, memory recall, calculator arithmetic, goals, notes, tasks, weather, market, and calendar
- `Planner`, `MultiStepPlan`, `ConfirmationManager`, `ToolChain`, `ExecutionPipeline`, and `SkillManager` for local goals, notes, tasks, calculator, weather, market, calendar, context responses, confirmations, and conversation memory plan execution
- `ToolAdapterRegistry`, `ExternalAdapterConfig`, `SecretsGuard`, `RealWeatherAdapter`, and `RealMarketAdapter` plus explicit `tool_adapter` PlanSteps for future adapter execution infrastructure
- In-memory conversation context for recent handled skill turns

The current pytest collection is 208 tests.

The current memory paths are:

- `MemoryStore` for conversation-style memory
- `UserProfileStore` for persistent user facts
- `GoalsStore` for persistent long-term goals
- `NotesStore` for persistent local notes
- `TasksStore` for persistent offline tasks
- `ReminderScheduler` for passive due/upcoming task queries
- `ConversationContextManager` for RAM-only short-term skill context

Next Priorities

1. GPT fallback integration.
2. Voice input/output.
3. Raspberry Pi deployment.
4. Robot body / sensors.

What Must Not Be Started Yet

- No voice implementation.
- No GPT or LLM integration.
- No embeddings.
- No notification scheduling or delivery.
- No calendar integration.
- No Raspberry Pi deployment work.
- No new skills before the roadmap and architecture decision is approved.
- No AI parser or regex-only parser rewrite.
- No robotics or movement integration.
- No vision integration.
- No broad refactors of the router, memory, or skill system.

Testing Rules Before Each Phase

Before starting a new phase:

1. Pull latest `main`.
2. Confirm the working tree is clean.
3. Run the full verification suite:

```powershell
py -m pytest
py -m compileall core interfaces events memory skills scripts
py scripts\verify_phase2_events_memory.py
```

4. Do not proceed if any check fails.
5. Fix the root cause of failures before adding new work.
6. Do not skip, xfail, or weaken tests without explicit approval.
7. Update README and SESSION_HANDOFF for meaningful changes.
8. Commit logical changes separately.
9. Push only after all checks pass.

Near-Term Planning Questions

- Should company information be an intent, a skill, a provider, or a combination?
- Which data provider should be used for company facts?
- What should be cached, and for how long?
- Which answers should remain deterministic and avoid LLM calls?
- How should profile memory be used in future skills without leaking private facts?

Exit Criteria For Next Implementation Phase

The next implementation phase can begin only after:

- `docs/ARCHITECTURE.md` is current.
- `docs/ROADMAP.md` is current.
- `docs/ENGINEERING_RULES.md` is followed.
- The full verification suite passes.
- The scope is limited to one logical capability.
