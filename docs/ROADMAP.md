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
- `config/adapters.example.json` keeps real market disabled and mock-mode by default with a fake placeholder env name
- Existing MarketSkill behavior stays on `mock_market` unless an explicit real-market adapter is configured and selected
- Tests cover default mock behavior, real-mode missing-env failure, env-name-only config, adapter instantiation, safe MarketSkill failure, and SecretsGuard compatibility
- No real API keys, default real mode, GPT, voice, calendar writes, notifications, or background jobs

Phase 25: Real Market Adapter HTTP Logic

- RealMarketAdapter performs market HTTP requests only when adapter config mode is `real`
- Required API key value is read only from the configured environment variable name
- Raw keys are not stored in config and are not returned in adapter responses
- Configured timeout seconds are passed to the HTTP client
- Response normalization returns stable ARES market data: symbol, price, currency, capability, source, and optional name/change fields
- HTTP timeout, HTTP status errors, invalid JSON, and unsupported payloads return safe deterministic errors
- Tests mock HTTP and do not make real network calls
- Default MarketSkill behavior remains `mock_market`
- No real API keys, default real mode, GPT, voice, calendar writes, notifications, or background jobs

Phase 26: Device Action Framework Skeleton

- `core.DeviceAction`
- `core.DeviceActionResult`
- `core.DeviceActionRegistry`
- `core.LocalDeviceActionAdapter`
- Safe built-in actions only: `echo`, `system_status_mock`, and `list_actions`
- Unknown actions fail safely
- Dangerous placeholders such as shutdown and restart are rejected
- Result payloads have stable action name, success, text, data, error message, and metadata fields
- No arbitrary shell commands, shutdown/restart actions, Telegram, voice, internet, GPT, remote control, notifications, or dangerous device automation
- Future dangerous actions must require explicit confirmation

Phase 27: DeviceActionSkill Safe Live Path

- `skills.builtin.DeviceActionSkill`
- Live routing for safe device actions through `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL
- Supported commands: `echo <text>`, `list device actions`, and `system status`
- Unknown device actions fail safely
- Dangerous actions such as shutdown, restart, run command, open app, delete, and arbitrary shell requests are rejected safely or gated by later confirmation phases; lock requests now route to Phase 29 `lock_pc`, sleep requests now route to Phase 30 `sleep_pc`, and open-app requests now route to the Phase 32 confirmed Windows allowlist launcher.
- No real OS commands, shutdown/restart, Telegram, voice, internet, GPT, remote access, notifications, or background jobs
- Future dangerous actions must require explicit confirmation

Phase 28: Device Dangerous-Action Confirmation Gate

- Device actions now carry `safe`, `confirmation_required`, or `forbidden` classifications.
- Shutdown, restart, and open app were classified as confirmation-required placeholders; lock requests now route to Phase 29 `lock_pc`, sleep requests now route to Phase 30 `sleep_pc`, and open-app requests now route to the Phase 32 confirmed Windows allowlist launcher.
- Run command, delete, and arbitrary shell are forbidden placeholders.
- `DeviceActionSkill` never executes confirmation-required or forbidden actions directly.
- Confirmation-required responses include a stable device action confirmation request token.
- Planner and REPL paths preserve confirmation-required results safely.
- Tests cover safe actions, shutdown/restart confirmation-required responses, run command/delete not-executed responses, unknown safe failures, planner propagation, and REPL display.
- No real OS commands, shutdown/restart, Telegram, voice, internet, GPT, remote access, notifications, or background jobs

Phase 29: Confirmed Windows Lock Device Action

- `lock_pc` is the first real OS-backed local device action.
- `lock_pc` requires explicit confirmation before execution.
- Confirmed `lock_pc` calls the Windows lock implementation through `LocalDeviceActionAdapter`.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows lock implementation and do not lock the workstation.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, internet, GPT, remote access, notifications, and background jobs were not added; sleep arrived later in Phase 30 as `sleep_pc`, mocked app launching arrived later in Phase 31, and confirmed allowlisted Windows app launching arrived later in Phase 32.

Phase 30: Confirmed Windows Sleep Device Action

- `sleep_pc` is the second real OS-backed local device action.
- `sleep_pc` requires explicit confirmation before execution.
- Confirmed `sleep_pc` calls the Windows sleep implementation through `LocalDeviceActionAdapter`.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows sleep implementation and do not put the workstation to sleep.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, internet, GPT, remote access, notifications, and background jobs were not added; mocked app launching arrived later in Phase 31, and confirmed allowlisted Windows app launching arrived later in Phase 32.

Phase 31: Device App Launcher Skeleton

- `AppLaunchConfig` models allowlisted apps with app id, display name, command placeholder, enabled flag, and confirmation flag.
- `LocalDeviceActionAdapter` exposes safe `list_apps`.
- `open_app <app_id>` is confirmation-gated and calls only a mocked launcher after explicit approval.
- Unknown app ids and disabled app ids are rejected safely before the launcher callback is called.
- Arbitrary app names and command-like app ids are not executable.
- Tests cover list apps, unknown app rejection, disabled app rejection, confirmation gating, confirmed mocked launch, no arbitrary command execution, planner routing, SkillManager confirmation, and REPL output.
- No real app launch, arbitrary shell execution, shutdown/restart/delete, Telegram, voice, internet, GPT, remote access, notifications, or background jobs were added.

Phase 32: Confirmed Windows App Launcher

- `open_app <app_id>` remains confirmation-gated through `ExecutionPipeline`.
- At this phase, default allowlist examples were disabled: `notepad`, `calculator`, and `browser`.
- Confirmed app launches run only for enabled allowlisted app ids.
- The Windows launcher uses the configured allowlist command with `shell=False`.
- User-provided paths, shell-like input, unknown apps, and disabled apps fail safely before launch.
- Non-Windows platforms return unsupported safely.
- Tests mock the Windows launcher and do not open real apps.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, notifications, remote access, arbitrary shell commands, or arbitrary app launching was added.

Phase 33: App Launcher Allowlist Config

- `config/apps.json` stores approved app definitions outside runtime code.
- `core.AppAllowlistLoader` validates app id, display name, command/path, enabled flag, and confirmation flag before `LocalDeviceActionAdapter` builds the allowlist.
- Invalid config and duplicate normalized app ids fail closed.
- The tracked example config initially kept `notepad`, `calculator`, and `browser` disabled by default.
- `open_app <app_id>` still requires confirmation and can only use configured allowlist commands, never user-supplied paths.
- Tests cover valid config loading, invalid config rejection, duplicate app id rejection, disabled and unknown app rejection, confirmed enabled launch through a mocked launcher, and user-supplied path isolation.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, notifications, remote access, arbitrary shell commands, or arbitrary app launching was added.

Phase 34: Calculator App Allowlist Enablement

- `calculator` is the only enabled app in `config/apps.json`.
- `notepad` and `browser` remain disabled.
- `open_app calculator` still requires confirmation.
- Confirmed calculator launch uses only the existing safe Windows launcher path and configured allowlist command.
- User-supplied paths and shell-like app ids remain rejected.
- Unknown apps and disabled apps fail safely before launch.
- Non-Windows platforms return unsupported safely.
- Tests cover loaded config state, confirmation-required behavior, confirmed calculator launch through a mocked launcher, disabled notepad/browser rejection, arbitrary path rejection, and the full suite.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, notifications, remote access, arbitrary shell commands, or arbitrary app launching was added.

Phase 35: Manual Calculator Launch Verification

- `scripts/manual_verify_calculator_launch.py` is owner-run only.
- The script prints a warning and shows `App id: calculator`.
- The script requires the exact typed confirmation `YES_OPEN_CALCULATOR`.
- Only after that confirmation does it call the existing `LocalDeviceActionAdapter.execute("open_app", ...)` path.
- Automated tests mock this path and do not open Calculator.
- No new apps, shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, or notifications were added.

Phase 36: PCService Abstraction

- `core.PCService` defines the dedicated interface for future PC operations: `lock()`, `sleep()`, `open_app(app_id)`, and `status()`.
- `core.PCServiceResult` provides the service-layer result shape.
- `core.WindowsPCService` holds the Windows-specific implementation behind the PC service boundary.
- `LocalDeviceActionAdapter` delegates status, lock, sleep, and open-app behavior through PCService instead of directly calling Windows helpers.
- Existing device-action behavior, confirmation gates, app allowlist rules, and tests remain compatible.
- No new device actions, shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, arbitrary shell commands, or behavior changes were added.

Phase 37: PCService Status Provider

- `core.PCStatus` defines a structured safe status object.
- `PCService.get_status()` returns local status through the PC service boundary.
- Safe status fields include operating system, hostname, current user, Python version, optional uptime, and available actions.
- `PCService.status()` remains a compatibility wrapper around `get_status()`.
- DeviceAction `system status` obtains status information only through PCService.
- Tests cover direct PCService status structure, the compatibility wrapper, DeviceAction status routing, and SkillManager pipeline status output.
- No network access, hardware telemetry, process enumeration, remote control, internet, GPT, voice, or new device actions were added.

Phase 38: PCService Capability Discovery

- `core.PCCapabilities` defines a structured safe capability object.
- `PCService.get_capabilities()` returns capability data through the PC service boundary.
- Capability data includes supported device actions, supported applications, available status providers, available services, and explicit safeguards.
- DeviceAction `list device actions` and `list apps` obtain discovery data through PCService instead of direct hardcoded lists.
- Tests cover direct PCService capability structure, DeviceAction action discovery through PCService, and DeviceAction app discovery through PCService.
- No internet, GPT, network, remote execution, process enumeration, hardware telemetry, or new device actions were added.

Phase 39: CoreService Orchestration Layer

- `core.CoreService` owns local/external service registration.
- `CoreService` initially registers `PCService` as the `pc` service.
- `CoreService.get_service(name)` returns registered services.
- `CoreService.list_services()` returns stable service metadata.
- `CoreService.get_capabilities()` aggregates capability data from every registered service.
- `SkillManager` and `LocalDeviceActionAdapter` use CoreService to reach PCService where practical.
- Tests cover default PCService registration, service lookup, service listing, capability aggregation, capability failure reporting, and DeviceAction/SkillManager CoreService integration.
- No behavior changes, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Phase 40: Phase 2 Complete Architecture Cleanup

- Phase 2 is stabilized as the architecture baseline before adding new cities.
- `PC_SERVICE_NAME` centralizes the CoreService PC registration key.
- `SkillContext` carries `core_service` so the Brain-to-skill path has one consistent service boundary.
- `CoreService` has clearer registration, lookup, listing, and capability aggregation documentation.
- CoreService capability aggregation fails safely when a registered service does not expose `get_capabilities()`.
- Tests verify default PCService status/capability interfaces, safe missing-capability reporting, and SkillManager context propagation.
- No behavior changes, new functionality, new cities, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Phase 41: Voice City Foundation

- `core.VoiceService` defines the Voice City service interface.
- `core.PlaceholderVoiceService` provides safe placeholder status and capability responses.
- `core.VoiceStatus` returns structured status with audio hardware disabled.
- `core.VoiceCapabilities` returns structured capability data with microphone, speaker, STT, TTS, wake word, background listening, internet, and GPT disabled.
- `CoreService` registers the VoiceService skeleton as `voice` by default alongside `pc`.
- Tests cover CoreService registration, VoiceService capabilities, safe placeholder status, CoreService aggregation of PC and Voice services, and no audio hardware access.
- No microphone, speaker, Whisper, Vosk, Piper, GPT, internet, wake word, or background listening was added.

Phase 42: Voice City STT/TTS Contracts

- `core.VoiceInput` defines `listen_once()`, `get_status()`, and `get_capabilities()`.
- `core.VoiceOutput` defines `speak(text)`, `get_status()`, and `get_capabilities()`.
- `core.NullVoiceInput` returns safe placeholder listen results without microphone access, STT, wake word, or background listening.
- `core.NullVoiceOutput` accepts text safely without speaker access or TTS.
- `PlaceholderVoiceService` owns the input/output components and includes their status and capability data in service status/capabilities.
- Tests cover VoiceService ownership, NullVoiceInput placeholder behavior, NullVoiceOutput placeholder behavior, status aggregation, CoreService capability aggregation, and no audio hardware access.
- No microphone, speaker, Whisper, Vosk, Piper, real STT, real TTS, wake word, GPT, internet, or background listening was added.

Phase 43: Voice City Text Loop Foundation

- `core.VoiceLoop` provides a one-shot text bridge for Voice City.
- `VoiceLoop.run_once()` calls `VoiceInput.listen_once()` once.
- Empty or missing input is ignored safely.
- Recognized text is passed to an injected existing text/planner/execution handler.
- Final response text is sent to `VoiceOutput.speak(text)`.
- Default components remain `NullVoiceInput` and `NullVoiceOutput`.
- Tests cover empty input, recognized text reaching the mocked planner/execution handler, output text reaching `NullVoiceOutput`, and safe input/handler/output error responses.
- No microphone, speaker, wake word, background loop, real STT, real TTS, GPT, internet, new skills, or behavior changes outside Voice City were added.

Phase 44: City Lifecycle and Lazy Routing

- CoreService tracks city lifecycle states: `idle`, `active`, `failed`, and `disabled`.
- CoreService capability registry metadata includes service name, service type, city status, and registered capabilities.
- `CoreService.route_by_capability()` routes one request to one matching idle city.
- Only the selected city becomes active for that route.
- Unused cities remain idle and are not called.
- Disabled cities are not routed.
- Handler failures mark only the selected city as `failed`.
- Tests prove unused cities are not called, disabled cities are skipped, and failed routes are reported safely.
- Event Bus city activation is documented as future-only; no event-driven city wakeup runtime was added.
- No real audio, GPT, internet, new APIs, external calls, notifications, or background city activation were added.

Phase 45: Internal Event Bus Skeleton

- `core.EventBus` provides a future internal city event bus skeleton.
- `core.Event` stores source, type, priority, payload, and timestamp.
- Supported priority levels are `low`, `normal`, `high`, and `critical`.
- Publish/subscribe is in-process only.
- Event history is returned in priority order.
- Tests cover publish, subscribe, no-subscriber safety, stable priority levels, invalid priorities, and priority ordering.
- This is future-use infrastructure only.
- No real camera, notifications, background daemon, internet, GPT, new APIs, or event-driven city activation was added.

Phase 46: CoreService Event Decision Routing

- `CoreService.handle_event(event)` receives internal `core.Event` records from registered city sources.
- Stable event decisions are `ignored`, `recorded`, and `escalated`.
- Low and normal priority events are recorded only.
- High and critical priority events are marked escalated.
- Unknown and disabled event sources fail safely with an ignored decision.
- Tests cover low, normal, high, critical, unknown-source, and disabled-source handling.
- This is internal routing only.
- No notifications, background listeners, real devices, internet, GPT, new APIs, or daemon behavior was added.

Phase 47: Local Event History Store

- `events.EventHistoryStore` persists internal event decisions/results locally.
- The default path is `data/event_history.json`.
- `data/event_history.json` is ignored by git.
- Stored records include source, type, priority, decision, event data, result data, and timestamp.
- Recent event history can be queried by source, type, and priority.
- Stored history is bounded by a safe maximum record limit.
- Tests cover add, query, max size, empty history, persistence after reload, invalid priority rejection, and zero-size history.
- This is internal memory/logging only.
- No notifications, devices, background daemon, GPT, internet, new APIs, or automatic listeners were added.

Phase 48: CoreService Event History Persistence

- `CoreService` accepts an optional `EventHistoryStore`.
- `CoreService.handle_event(event)` stores handled event decisions/results when a store is configured.
- Low and normal events are stored as `recorded`.
- High and critical events are stored as `escalated`.
- Unknown and disabled source events are stored as safe `ignored` decisions.
- The `failed` event decision value is reserved for future failed event-handling paths.
- Tests cover stored low, normal, high, critical, unknown-source, and disabled-source event decisions.
- This is synchronous internal memory/logging only.
- No notifications, devices, background daemon, GPT, internet, new APIs, or automatic listeners were added.

Phase 49: Event History Skill

- `skills.EventHistorySkill` provides read-only access to local event history.
- Supported phrases are `what happened recently`, `show recent events`, and `show critical events`.
- `IntentParser` recognizes event-history requests.
- `Planner` creates `event_history` steps.
- `ExecutionPipeline`, `SkillManager`, and the text REPL route requests through the normal live path.
- Empty history returns a safe local response.
- Tests cover recent events, critical events, empty history, parser phrases, planner steps, and SkillManager live path.
- This is read-only local querying only.
- No notifications, devices, background daemon, GPT, internet, new APIs, or automatic listeners were added.

Phase 50: Phase 3 Foundation Checkpoint

- Phase 3 foundation is frozen before adding real audio.
- Current implemented foundation is Voice City skeleton, manual Voice City text loop simulation, lazy city routing, internal `core.EventBus`, local `events.EventHistoryStore`, and read-only `skills.EventHistorySkill`.
- Checkpoint pytest collection before audio adapter contracts was 351 tests.
- No runtime code changed for this checkpoint.
- Real microphone access, speaker output, wake word detection, real STT, real TTS, background listening, notifications, GPT, internet access, and real device/event automation remain disabled until explicitly approved.

Phase 51: Voice City Audio Adapter Contracts

- `VoiceInputAdapter` defines the future speech-input adapter boundary.
- `VoiceOutputAdapter` defines the future speech-output adapter boundary.
- `MockVoiceInputAdapter` captures safe mock text without microphone access.
- `MockVoiceOutputAdapter` records safe mock spoken text without speaker access.
- `NullVoiceInput` and `NullVoiceOutput` now delegate through adapters while preserving placeholder no-audio behavior.
- `PlaceholderVoiceService` can receive injected input/output adapters for test and future provider wiring.
- `VoiceLoop` reports adapter failures safely and continues to ignore empty input safely.
- Manual Voice City text simulation uses the mock input adapter and `NullVoiceOutput`.
- Tests cover input capture, output speak, empty input, adapter failure, adapter injection, and no audio hardware access.
- Phase pytest collection at this point was 357 tests.
- Real Whisper, Vosk, Piper, microphone, speaker, wake word, background listener, GPT, and internet integrations remain future work.

Phase 52: Voice City Adapter-Backed Single-Turn Loop

- `VoiceInputAdapter.capture()` is the explicit one-turn capture entry point while `capture_input()` remains compatible.
- `VoiceTextRequest` represents the text request converted from voice input.
- `VoiceSingleTurnLoop` runs one safe voice-style turn with mock input and mock output adapters.
- Flow: `MockVoiceInputAdapter.capture()` -> `VoiceTextRequest` -> injected existing text/CoreService handler -> `MockVoiceOutputAdapter.speak(response)`.
- Empty input returns a safe no-op.
- Input adapter failures fail safely before text handling.
- Output adapter failures fail safely after response generation.
- Tests cover normal one-turn input/output, empty input, input adapter failure, output adapter failure, and no real microphone/speaker access.
- Phase pytest collection at this point was 363 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Phase 53: Voice City Multi-Turn Mock Session

- `VoiceSessionLoop` processes multiple queued mock inputs in sequence.
- Sessions are bounded by a `max_turns` limit.
- Stop phrases are `stop`, `exit`, and `goodbye`.
- Empty input is recorded as a safe no-op turn and does not call the text handler or output adapter.
- Session output includes `VoiceSessionTurn` records, transcript, and history.
- Input adapter failures stop the session safely before text handling.
- Output adapter failures stop the session safely after response generation.
- Tests cover multi-turn flow, stop phrase handling, max-turn limiting, empty input handling, input failure, output failure, and no real microphone/speaker access.
- Phase pytest collection at this point was 370 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Phase 54: Voice Session Skill

- `skills.VoiceSessionSkill` starts a safe bounded mock voice session from a text command.
- Supported phrases are `start voice session`, `start mock voice`, and `run voice test`.
- The skill uses `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, and `VoiceSessionLoop` only.
- It returns a transcript summary with max-turn, stop-phrase, and empty-session behavior.
- The live path is IntentParser -> Planner -> ExecutionPipeline -> SkillManager -> VoiceSessionSkill.
- Tests cover parser phrases, planner routing, ToolSelector routing, direct skill start, stop phrase, max-turn limiting, empty session, and SkillManager execution.
- Phase pytest collection at this point was 379 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Phase 55: Voice Session Event Logging

- `VoiceSessionSkill` writes safe local operational events to `EventHistoryStore` when a store is available in `SkillContext`.
- Recorded event types are `voice_session.started`, `voice_session.stopped`, `voice_session.adapter_failure`, and `voice_session.max_turns_reached`.
- Adapter failures are recorded with high priority and an escalated decision, but no notifications or background listeners are started.
- Event payloads store status, turn counts, max-turn metadata, and adapter failure details only.
- Mock transcript content is not stored in event payloads.
- `EventHistorySkill` can display these records through recent event queries.
- Tests cover all voice session event cases and the live SkillManager path.
- Phase pytest collection at this point was 384 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Phase 56: Voice Session Status Query

- `VoiceSessionSkill` can answer what happened in the latest mock voice session.
- Supported phrases are `what happened in voice session`, `show last voice session`, and `voice session status`.
- The query reads the latest local `voice_session.*` event group from `EventHistoryStore`.
- It returns started/stopped/failure/max_turns summary lines.
- It is read-only and does not start a new mock session.
- Tests cover no session, normal stopped session, failed session, max-turn session, parser routing, planner routing, and SkillManager live path.
- Phase pytest collection at this point was 391 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Phase 3 Voice Checkpoint

- The current Phase 3 voice foundation is documented and frozen before real audio work.
- Test count: 391 tests.
- Confirmed foundation:
  - Voice City skeleton
  - Audio adapter contracts
  - Single-turn loop
  - Multi-turn mock session
  - `VoiceSessionSkill`
  - Voice session event logging
  - Voice session status query
- This checkpoint is documentation-only.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, notifications, or real audio hardware access was added.

Phase 57: Microphone Adapter Abstraction

- `core.AudioChunk` models raw audio chunk metadata for future microphone providers.
- `core.MicrophoneAdapter` defines `start()`, `stop()`, `read_chunk(timeout_seconds, cancel_requested)`, `get_status()`, and `get_capabilities()`.
- `core.MockMicrophoneAdapter` provides deterministic local/test lifecycle, queued chunk reads, timeout handling, cancellation support, status/capability data, and safe failure paths.
- `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept microphone adapters through dependency injection.
- Voice City can swap microphone implementations later without changing the Brain, CoreService, skills, or current text loops.
- Tests cover audio chunk serialization/validation, start/stop, read-before-start, queued chunk reads, timeout, cancellation callable/event support, failure modes, status/capabilities, and Voice City injection.
- Phase pytest collection at this point was 403 tests.
- No Whisper, Vosk, Piper, wake word, hardware-specific code, real microphone access, real STT, speaker access, GPT, internet, or background listener was added.

Phase 58: Speech-to-Text Adapter Abstraction

- `core.TranscriptionResult` models transcription text, status, error details, and bounded confidence values.
- `core.SpeechToTextAdapter` defines `transcribe(audio_chunk)`, `get_status()`, and `get_capabilities()`.
- `core.MockSpeechToTextAdapter` converts `AudioChunk` objects into deterministic test transcriptions without calling a real speech engine.
- The mock adapter handles successful transcription, empty audio, no transcription, low confidence, and safe adapter failure.
- `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept speech-to-text adapters through dependency injection.
- Voice City can swap transcription implementations later without changing the Brain, CoreService, skills, or current text loops.
- Tests cover success, empty audio, low confidence, adapter failure, no transcription, confidence clamping, status/capability data, and Voice City injection.
- Phase pytest collection at this point was 412 tests.
- No Whisper, Vosk, wake word, real microphone access, real STT, hardware-specific code, internet, GPT, or background listener was added.

Phase 59: Voice Command Router

- `core.VoiceCommandRouter` accepts `TranscriptionResult` objects and routes valid transcribed text.
- `core.VoiceCommandRoutingResult` returns structured success, status, route, confidence, response, error, data, and metadata.
- `core.VoiceCommandRouterMetrics` tracks total, routed, rejected, unknown, and failed command counts.
- Empty transcriptions are ignored safely.
- Low-confidence transcriptions are rejected before command handling.
- Transcription adapter failures are propagated as structured routing failures.
- Valid text is routed through CoreService's `voice.text_loop` capability.
- Unknown commands return safe structured `unknown_command` results.
- Routed and rejected commands emit local `voice_command.routed` and `voice_command.rejected` events.
- Tests cover successful routing, empty transcription, low-confidence rejection, unknown command handling, transcription failure propagation, metrics, and event-bus publication.
- Phase pytest collection at this point was 418 tests.
- No Whisper, Vosk, GPT, wake word, internet, hardware access, real microphone, real STT, or background listener was added.

Phase 60: Simulated VoicePipeline

- `core.VoicePipeline` orchestrates a simulated end-to-end Voice City command path.
- The pipeline receives audio only through an injected `MicrophoneAdapter`.
- It transcribes through an injected `SpeechToTextAdapter`.
- It passes `TranscriptionResult` through `VoiceCommandRouter`.
- Valid commands route through CoreService's `voice.text_loop` capability.
- Only the required city is activated; unrelated cities remain idle.
- Final response text is sent through an injected `VoiceOutputAdapter`.
- `VoicePipelineResult` preserves success/status, final text, response text, session id, correlation id, stage data, events, and safe metadata.
- Structured events are recorded for audio captured, transcription accepted/rejected, command routed/rejected, city activated, execution completed/failed, and output produced.
- Tests cover successful complete command, empty audio, microphone failure, STT failure, empty transcription, low-confidence transcription, unknown command, CoreService routing failure, target city failure, output adapter failure, reusable session state after failure, requested-city activation only, stable correlation id propagation, and unrelated city idleness.
- Phase pytest collection at this point was 432 tests.
- No real microphone access, Whisper, Vosk, Piper, GPT, wake-word detection, internet access, background listening, daemon/service installation, or guessed RAM/CPU limits were added.

Phase 61: Enforced Module Lifecycle Foundation

- `core.ModuleLifecycleManager` enforces lifecycle for CoreService-managed modules.
- `core.LifecycleRequest`, `core.LifecycleResult`, `core.LifecycleStatus`, and `core.LifecycleTransition` provide structured lifecycle data.
- Required states are `UNLOADED`, `STARTING`, `READY`, `BUSY`, `DEGRADED`, `STOPPING`, `STOPPED`, and `FAILED`.
- Required operations are `start()`, `health_check()`, `execute(request)`, and `stop()`.
- CoreService registers every service with the lifecycle manager.
- `route_by_capability()` starts and health-checks only the selected module before execution.
- Execution is rejected until a module is successfully started and healthy.
- Idempotent start and stop behavior is implemented for already READY and STOPPED modules.
- Failed startup leaves the module in `FAILED`.
- Failed execution marks only the selected module `FAILED` and leaves unrelated modules usable.
- Failed or degraded modules require explicit `recover_service()` before retry.
- Lifecycle status and transition history are queryable through CoreService.
- Transition records preserve session ids and correlation ids.
- Inactivity policy metadata exists, but no background lifecycle timer was added.
- Voice City is integrated through the lifecycle gate, and the simulated VoicePipeline continues to pass.
- Phase pytest collection at this point was 452 tests.
- No real microphone access, Whisper, Vosk, Piper, wake word detection, GPT, internet access, background listening, background lifecycle timers, daemon/service installation, process spawning, Docker, or guessed RAM/CPU limits were added.

Phase 62: Versioned Interface Contracts

- `core.Contracts` defines the central contract registry and V1 public boundary contracts.
- Every public request/result contract exposes contract name, version, correlation id, optional session id, created timestamp, and metadata.
- Supported runtime version format is integer major versions such as `v1`.
- Current V1 contracts cover microphone capture, speech-to-text, voice command routing, CoreService execution, lifecycle execution, VoicePipeline, and event publication envelopes.
- CoreService rejects unsupported core execution contracts before city lookup or activation.
- ModuleLifecycleManager rejects unsupported lifecycle contracts before state transitions.
- VoicePipeline and VoiceCommandRouter validate V1 contracts across microphone, STT, command routing, lifecycle, and core execution boundaries.
- Event envelopes are versioned for future city event routing and event-history storage.
- Unsupported or malformed contracts fail safely with structured compatibility errors and preserve correlation ids where available.
- Phase pytest collection at this point was 471 tests.
- No real microphone access, Whisper, Vosk, Piper, wake word detection, GPT, internet access, background listeners, remote registries, plugin downloads, dynamic code loading, database migrations, or guessed hardware resource limits were added.

Phase 63: Capability Manifest Foundation

- `core.CapabilityManifest` declares module identity, module type, module version, manifest version, description, provider, enabled state, explicit capabilities, consumed and produced contracts, dependencies, platform compatibility, permissions, lifecycle support, and metadata.
- `core.CapabilityManifestRegistry` registers manifests, validates duplicates, looks up modules by type/capability, finds providers, validates contract compatibility, validates dependencies, validates platform compatibility, validates permission policy, validates lifecycle declarations, and performs deterministic provider selection.
- `core.ManifestPolicy` provides safe local enable/disable flags, preferred providers, and allowed permissions.
- CoreService validates a selected module manifest before lifecycle start and can record safe `manifest.validation_failed` event-history entries.
- Voice City, mock microphone adapter, mock speech-to-text adapter, mock voice output adapter, VoiceCommandRouter, and VoiceSessionSkill have registered manifests.
- SkillRegistry registers skill manifests from explicit skill metadata.
- `config/modules.example.json` documents safe local module configuration without remote config, package downloads, dynamic loading, secrets, internet discovery, or automatic dependency installation.
- Phase pytest collection at this point was 502 tests.
- No real microphone access, Whisper, Vosk, Piper, wake word detection, GPT, internet access, background listeners, automatic dependency installation, dynamic plugin loading, database migrations, runtime provider fallback, Docker, daemon installation, or guessed hardware resource limits were added.

Phase 64: Memory Schema Migration Foundation

- `memory.schema_migrations` centralizes durable-store schema migration behavior.
- `SchemaEnvelope` wraps durable JSON stores with `schema_name`, `schema_version`, `created_at`, `updated_at`, `data`, and optional `metadata`.
- `MigrationRegistry` handles schema registration, current-version lookup, supported-version lookup, migration-path calculation, sequential execution, dry-run mode, duplicate edge rejection, cycle rejection, missing path rejection, and pre/post migration validation.
- Active schemas are `ares.user_profile`, `ares.goals`, `ares.notes`, `ares.tasks`, `ares.memory.short`, `ares.memory.long`, and `ares.event_history`.
- At the Phase 64 checkpoint, production schemas remained v1; a controlled fixture demonstrated v1 -> v2 migration. Phase 87 moved `ares.owner_profile` to v2, and Phase 88 extended that same preserving path sequentially to v3.
- Known legacy unversioned JSON formats import into v1 only when the structure matches the requested store exactly.
- Backup-before-write, temporary writes, atomic replacement where practical, final load verification, simple local write locks, and read-only inspection reports are implemented.
- Corrupted files, truncated files, malformed envelopes, wrong schema names, future versions, downgrades, missing paths, failed migration steps, and validation failures fail closed without resetting memory to empty data.
- Store integration covers profile, goals, notes, tasks, short/long memory, and event history.
- Phase pytest collection at this point was 527 tests.
- No remote database, cloud synchronization, distributed locking, PostgreSQL, Docker, automatic cloud backup, GPT, internet access, health fallback, real audio, or guessed resource limits were added.

Phase 65: Health Checks and Controlled Adapter Fallback

- `core.Health` defines the common `HealthResult` model, `HealthPolicyConfig`, `AdapterCandidate`, `AdapterFallbackPolicy`, `FallbackExecutionResult`, `CircuitBreaker`, and `HealthCache`.
- Health statuses are explicit: `healthy`, `degraded`, `unavailable`, `failed`, `disabled`, and `unknown`.
- CoreService exposes read-only health visibility through `get_service_health(name)`, `list_service_health()`, and `get_capability_health(capability)`.
- Lazy health inspection reports manifest/lifecycle/city state without activating every heavy City; active probes are explicit.
- Adapter fallback checks enabled state, required capability, interface version, health status, degraded-mode policy, and circuit state before selection.
- Rejection reasons are returned for every skipped candidate.
- Runtime fallback is bounded and allowed only for explicitly `retry_safe` operations.
- Retry-unsafe operations do not automatically fall back.
- Circuit breaker states are `closed`, `open`, and `half_open`, with injected clocks for deterministic tests and no background timer.
- Health cache supports TTL reuse, expiration, forced refresh, and disabled-adapter invalidation.
- VoicePipeline can use candidate lists for mock microphone selection and mock speech-to-text fallback while preserving the default single-adapter path.
- EventHistoryStore can record bounded health/fallback events for health-check failures, fallback selection, all-unavailable decisions, circuit-open events, half-open probes, and recovery.
- Phase pytest collection at this point was 560 tests.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet access, real weather/market calls, notifications, automatic PC actions, background listeners, or measured resource budgets were added.

Phase 66: Measured Resource Budgets

- `core.ResourceBudget` defines `ResourceDeclaration`, `ResourcePolicy`, `ResourceManager`, `ResourceReservation`, `ResourceDecision`, and `CancellationToken`.
- Capability manifests now carry optional validated resource declarations.
- Declared resource fields cover estimated RAM, CPU weight, startup/shutdown cost, heavy/persistent flags, inactivity timeout, maximum concurrent tasks, task priority, network requirement, and hardware-acceleration requirement.
- Platform profiles are `test`, `raspberry_pi_5`, `desktop`, and `future_orin`.
- The Raspberry Pi 5 profile keeps a conservative one-heavy-module limit as declared policy data, not a measured hardware benchmark.
- CoreService reserves capacity before lifecycle start, acquires bounded task slots before execution, records activity, and releases task slots on success/failure.
- Failed activation and failed execution do not leak task slots or newly created reservations.
- CoreService exposes `get_resource_status()`, `get_module_resource_status()`, `list_loaded_modules()`, `list_resource_reservations()`, `explain_activation()`, and `run_resource_maintenance()`.
- Explicit maintenance ticks unload inactive non-persistent modules; no background timer, thread, daemon, scheduler, or OS service was added.
- Eviction is optional and conservative: only inactive, non-persistent, lower-priority, safe-to-stop modules can be candidates.
- Cooperative cancellation tokens release task slots safely when cancellation is supported.
- Observed metrics are process-level only: uptime, CPU time, optional RSS when available, active module/task counts, loaded City count, and declared reserved RAM.
- EventHistoryStore can record bounded resource events without transcripts, secrets, personal memory, or raw exception traces.
- Phase pytest collection at this point was 596 tests.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet access, background listeners, threads, Docker, remote telemetry, distributed scheduler, operating-system process killing, real hardware benchmarking, or exact per-module memory measurement was added.

Architecture Hardening Checkpoint

- This checkpoint comes after the simulated Phase 3 Voice City command pipeline and before real hardware/adapters.
- Implemented:
  - enforced module lifecycle
  - versioned interface contracts
  - capability manifests
  - memory/database migrations
  - health checks and adapter fallback
  - measured resource budgets
  - final integration, recovery, and safety regression checkpoint
- Remaining hardening items:
  - none. Architecture Hardening is complete before real Phase 3 voice hardware work.
- Permanent rule: Every ARES ability must be independently installable, replaceable, disableable, health-checkable, version-compatible, and testable without modifying the Brain.
- Permanent contract rule: No City, Skill, adapter, device, or service may exchange an unversioned public request or response across an ARES architectural boundary.
- Permanent manifest rule: No independently loadable ARES module may start without a valid registered capability manifest.
- Permanent memory rules: durable ARES data may never be rewritten without validation and backup; unknown future schema versions must never be silently downgraded; a failed load must never be interpreted as empty memory; hardware-specific paths must not become part of the durable memory schema.
- Permanent health/fallback rules: the Brain never selects concrete adapters; automatic fallback is allowed only for explicitly retry-safe operations; a failed adapter must never cause unrelated Cities to activate; fallback must never hide the original failure; disabled or circuit-open adapters must not be selected; health checks must not perform destructive actions.
- Permanent resource rules: the Brain never manages RAM, CPU, adapters, or hardware; CoreService controls activation and resource reservations; no module activates before capacity is reserved; no failed operation may leak a reservation or task slot; declared estimates must never be represented as exact measurements; resource inspection must not activate inactive Cities; dangerous actions must never be repeated because of eviction, retry, or cancellation.
- Permanent execution-safety rule: confirmed destructive actions use bounded local idempotency tokens through `core.ExecutionGuard`; retries, duplicate confirmations, response failures, output failures, and wrong-scope submissions must not execute the action twice.

Phase 67: Final Integration, Recovery, And Safety Regression Checkpoint

- Deterministic integration tests now prove complete internal routes across VoicePipeline, VoiceCommandRouter, IntentParser, Planner, ExecutionPipeline, selected local skill/service, CoreService, PCService, confirmation-gated DeviceActionSkill, and mock voice output.
- Recovery tests deliberately fail microphone, STT, lifecycle, health, resource, execution, output, event-history, manifest, contract, disabled-city, unknown-city, cancellation, and fallback paths while proving CoreService remains usable.
- Safety regression tests cover no shell execution from the Brain path, no CoreService/confirmation bypass from Voice City, allowlist-only app launch, fail-closed disabled/incompatible modules, operational event redaction, bounded turns/tasks/heavy modules, migration compatibility, and resource-estimate wording.
- `core.ExecutionGuard` provides exactly-once protection for confirmed destructive device actions.
- Phase pytest collection at this point was 630 tests.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, background listener, remote control, notifications, scheduler, daemon, or new product feature was added.

Phase 68: Linux ALSA Microphone Adapter

- `core.LinuxAlsaMicrophoneAdapter` implements the existing `MicrophoneAdapter` contract for Raspberry Pi/Linux capture through `arecord`.
- `SafeSubprocessRunner` uses argument lists with `shell=False`; no user text becomes a shell command.
- The adapter checks `arecord`, lists ALSA capture devices, validates an optional selected device, records bounded WAV samples, validates WAV output, and returns structured `MicrophoneResult` data.
- `linux_alsa_microphone_adapter` is registered as a disabled-by-default capability manifest provider and remains replaceable through Voice City injection.
- `scripts/manual_verify_linux_alsa_microphone.py` lists devices and health status by default and records only with explicit `--record`.
- Phase pytest collection at this point was 646 tests.
- No Whisper, Vosk, real STT, wake word, background listener, GPT, internet, speaker/TTS, default live voice loop, or background microphone capture was added.

Phase 69: Offline Whisper Speech-To-Text Adapter

- `core.LinuxWhisperSpeechToTextAdapter` implements the existing `SpeechToTextAdapter` contract for offline Raspberry Pi/Linux WAV transcription.
- The adapter accepts WAV files recorded by `LinuxAlsaMicrophoneAdapter` or `AudioChunk` input.
- It requires a local Whisper/whisper.cpp-style executable and local model file; recommended first Raspberry Pi model is `ggml-tiny.en.bin`.
- It returns recognized text, processing time, language metadata when available, success/failure status, and structured error data.
- Missing binary, missing model, invalid audio, timeout, non-zero process exit, and no-transcription results fail or report safely.
- `linux_whisper_speech_to_text_adapter` is registered as a disabled-by-default capability manifest provider.
- `scripts/manual_verify_linux_whisper_stt.py` records through ALSA only when `--record` is provided, sends the WAV to offline Whisper, prints recognized text, and reports timing.
- Phase pytest collection at this point was 663 tests.
- No wake word, continuous/background listening, GPT, internet, speaker/TTS, autonomous loop, or conversation loop was added.

Phase 70: Raspberry Pi Whisper Runtime Preparation

- `scripts/install_whisper_cpp_raspberry_pi.py` prepares the local offline Whisper runtime on Raspberry Pi.
- The installer clones `https://github.com/ggml-org/whisper.cpp.git` into `external/whisper.cpp` if missing.
- It builds `whisper-cli` through CMake and verifies the built executable exists.
- It downloads the recommended `tiny.en` GGML model into `models/whisper/ggml-tiny.en.bin` by default and verifies the model exists.
- `scripts/verify_whisper_cpp_runtime.py` locates `whisper-cli`, locates the model, transcribes an existing recorded WAV sample, and prints PASS/FAIL diagnostics with recognized text and timing.
- Generated local samples, the cloned whisper.cpp tree, and downloaded GGML binaries are ignored by git.
- Phase pytest collection at this point was 675 tests.
- No wake word, background listener, GPT, internet runtime path, speaker/TTS, autonomous loop, or conversation loop was added.

Phase 71: Raspberry Pi Speech Input Verification Hardening

- `LinuxWhisperSpeechToTextAdapter` now validates WAV signal diagnostics before transcription: file size, duration, sample rate, channels, sample width, peak amplitude, and RMS amplitude.
- Silent recordings fail before `whisper-cli` runs.
- Configurable below-threshold RMS recordings fail clearly.
- Whisper `[BLANK_AUDIO]` output is treated as `no_usable_speech`, not a successful transcription.
- `scripts/verify_whisper_cpp_runtime.py` prints selected WAV diagnostics, exact `whisper-cli` command, exit code, and stdout/stderr previews on failures.
- `scripts/manual_verify_linux_whisper_stt.py` records, validates, and transcribes the exact WAV file, with optional `aplay` playback only when `--playback` is explicitly provided.
- `scripts/configure_linux_alsa_monitoring.py` mutes microphone playback monitoring while preserving microphone capture and speaker playback mixer controls.
- Phase pytest collection at this point was 688 tests.
- No wake word, background listener, GPT, internet runtime path, speaker/TTS output, autonomous loop, or conversation loop was added.

Phase 72: Reliable Offline Speech Recognition Verification

- Root cause fixed: the verification scripts defaulted to `--language auto` while the recommended installed Raspberry Pi model is the English-only `ggml-tiny.en.bin`; reliable direct manual Whisper runs use English mode.
- `LinuxWhisperSpeechToTextAdapter` now resolves English-only GGML model filenames to effective language `en` when configured as `auto`.
- `scripts/verify_whisper_cpp_runtime.py` and `scripts/manual_verify_linux_whisper_stt.py` now default to `--language en` for the recommended tiny English model.
- Transcription results include requested and effective language metadata.
- No-speech marker parsing now rejects `[BLANK_AUDIO]`, `<|nospeech|>`, `<|no_speech|>`, `(no speech)`, and silence-style markers as `no_usable_speech`.
- Regression tests cover English-only language resolution, missing model, missing executable, valid speech WAVs, silent/near-silent WAVs, corrupt WAVs, no-speech output parsing, and script default language behavior.
- Phase pytest collection at this point was 693 tests.
- No wake word, background listener, GPT, internet runtime path, speaker/TTS output, autonomous loop, or conversation loop was added.

Phase 72: Modular Offline Linux Text-To-Speech Output

- `core.TextToSpeech` defines the replaceable TTS adapter boundary.
- `TextToSpeechRequestV1` and `TextToSpeechResultV1` provide versioned TTS request/result contracts.
- `core.LinuxPiperTextToSpeechAdapter` implements offline Piper WAV generation for Raspberry Pi/Linux through safe argument-list subprocess execution.
- `core.LinuxAlsaSpeakerAdapter` owns explicit ALSA speaker playback through `aplay`; playback is disabled unless requested.
- `scripts/install_piper_raspberry_pi.py` prepares the local Piper runtime and `en_US-amy-low` voice model/config when manually run.
- `scripts/manual_verify_linux_tts.py` generates a WAV from explicit text and plays it only with `--playback`.
- TTS and speaker adapters have capability manifests and declared resource metadata, remain disabled by default, and do not add cloud fallback.
- No GPT, wake word, background listener, automatic microphone activation, memory writes based on voice, autonomous loop, or conversation loop was added.

Phase 73: Reliable Raspberry Pi Text-To-Speech Verification

- Real Raspberry Pi 5 verification confirmed that Piper loads the `en_US-amy-low` model, generates a valid WAV, and direct ALSA playback reaches the USB speaker audibly.
- Root cause fixed: `scripts/manual_verify_linux_tts.py` serialized `TextToSpeechResultV1` and queried a nonexistent top-level `healthy` key. The V1 contract represents healthy state as `success=true` and `status=healthy`, with nested speaker success/status.
- The verifier now validates the structured composite health result, generated WAV metadata, and requested playback outcome before returning success.
- `LinuxAlsaSpeakerAdapter` validates an explicitly selected device against `aplay -l` without starting playback.
- Diagnostics now include resolved runtime/model/config/output paths, selected device, exact Piper and explicit-playback commands, exit codes, WAV metadata, and raw subprocess output on failures.
- Playback remains disabled by default, and a playback failure preserves the generated WAV.
- Phase pytest collection at this point was 742 tests.
- No GPT, wake word, background listener, automatic microphone activation, memory writes based on voice, cloud fallback, autonomous loop, or conversation loop was added.

Phase 74: Configurable Piper Voice Profiles

- `core.VoiceProfiles` adds a strict `VoiceProfile` model and `VoiceProfileRegistry` as the only Piper model/config resolution boundary.
- `config/voice_profiles.json` configures the official `en_US-hfc_male-medium` profile as the default ARES voice and retains the verified `en_US-amy-low` profile as optional.
- Profile validation covers schema, unique identifiers, exactly one enabled default, supported engine/language/locale fields, enabled selection, approved model directories, model/config readability, and optional integrity metadata.
- `LinuxPiperTextToSpeechAdapter` resolves requested/default profile identifiers and reports full profile metadata in `TextToSpeechResultV1`; invalid profile selection never silently falls back.
- `scripts/install_piper_raspberry_pi.py` installs the configured default or an explicit registered profile, verifies files, skips valid downloads, and fails safely for unknown or partial profiles.
- `scripts/manual_verify_linux_tts.py --list-voices` reports registered/installed/default status without starting Piper or ALSA; `--voice-profile` selects an explicit profile.
- Phase pytest collection at this point was 771 tests.
- The male profile is implementation/test verified but still requires explicit owner-run installation and audible Raspberry Pi validation.
- No GPT, cloud TTS, wake word, background listener, automatic voice switching, autonomous loop, or conversation loop was added.

Phase 75: Controlled Single-Turn Voice Pipeline

- `SingleTurnVoiceRequestV1` and `SingleTurnVoiceResultV1` define one complete owner-triggered turn.
- `SingleTurnVoicePipeline` composes existing microphone, STT, VoiceCommandRouter/CoreService, SkillManager text execution, TTS, and speaker boundaries.
- Recognized text uses `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill`; unsupported requests return a safe local response.
- Lifecycle and resource managers gate the pipeline, reserve one heavy module, enforce one task slot, and release state after the turn.
- `VoiceStageCoordinator` prevents simultaneous microphone/speaker activity and concurrent Whisper/Piper execution.
- Silence, blank transcription, adapter failures, Brain fallback, cancellation, per-stage/total timeouts, diagnostic WAV preservation, and bounded redacted events are covered.
- `scripts/manual_verify_single_turn_voice.py` supports real capture and hardware-free `--text-input` simulation without subprocess code in the script.
- Phase pytest collection at this point was 812 tests.
- No wake word, background microphone, infinite loop, GPT, internet, automatic transcript memory write, robot movement, or boot service was added.

Phase 76: Controlled Multi-Turn Voice Conversation Session

- `MultiTurnVoiceSessionRequestV1` and `MultiTurnVoiceSessionResultV1` define one bounded owner-triggered session.
- `MultiTurnVoiceSession` repeatedly invokes `SingleTurnVoicePipeline`; it does not duplicate ALSA, Whisper, Brain, Piper, or speaker implementation.
- Exact normalized stop phrases are intercepted before Brain routing, and local greeting/closing output reuses the existing TTS/speaker path.
- Defaults are five turns, 180 seconds, three consecutive failures, five-second captures, a 0.75-second delay, and playback off.
- Lifecycle/resource gates, per-turn correlation IDs, mutual exclusion, structured cancellation, bounded redacted events, and cleanup are enforced.
- Bounded multi-turn checkpoint collection was 872 tests.
- No wake word, background listener, boot service, unbounded conversation loop, GPT, cloud dependency, or automatic transcript persistence was added.

Phase 77: Voice Activity Detection and Automatic End-of-Speech Capture

- `VoiceActivityCaptureRequestV1` and `VoiceActivityCaptureResultV1` define the versioned PCM capture boundary.
- `RmsVoiceActivityCapture` provides lightweight energy-based start detection, separate silence threshold hysteresis, consecutive speech frames, pre-roll, terminal-silence trimming, and bounded wait/utterance limits.
- `LinuxAlsaMicrophoneAdapter` streams one foreground raw PCM capture from `arecord` with `shell=False`; fixed-duration WAV capture remains available.
- `SingleTurnVoicePipeline` and `MultiTurnVoiceSession` propagate auto-stop settings without changing their Brain, Whisper, Piper, speaker, lifecycle, or resource boundaries.
- No-speech or invalid capture stops before Whisper, Brain, TTS, and playback. Multi-turn no-speech handling uses the existing bounded failure policy.
- `scripts/manual_verify_voice_activity_capture.py` reports ambient/speech RMS, peak, thresholds, duration, and stop reason without running Whisper or output audio.
- VAD checkpoint collection before adaptive calibration was 912 tests.
- No wake word, background listener, GPT, cloud dependency, transcript persistence, or arithmetic/intent text workaround was added.

Phase 78: Adaptive End-of-Speech and Voice Calculator Routing

- Ambient PCM calibration records bounded mean, median, p90, peak, and noise-floor statistics before listening.
- Separate bounded speech-start, speech-continue, and silence thresholds replace one static post-speech decision boundary.
- Consecutive start/resume/end rules plus the bounded `POSSIBLE_SILENCE` hangover prevent clicks or post-speech noise from extending capture to maximum duration.
- Calibration-disabled manual thresholds and fixed-duration capture remain supported fallbacks.
- `TranscriptNormalizationRequestV1` / `TranscriptNormalizationResultV1` preserve raw, cleaned, and normalized forms.
- Strict spoken arithmetic supports zero through one thousand, negatives, decimals, operators, and spoken parentheses before using the unchanged safe CalculatorSkill path.
- Conservative adjacent repetition cleanup reports exactly what it removed and preserves legitimate three-term arithmetic.
- Manual scripts expose capture thresholds, transition diagnostics, routing decisions, skill selection, execution status, and rejection reasons without printing private memory or secrets.
- Checkpoint pytest collection was 959 tests.
- Windows CI used synthetic PCM and mocked hardware for that checkpoint.

Phase 79: Production Voice Calculator Routing Hardening

- The owner verified real Raspberry Pi USB capture, clear recorded speech, end-of-speech completion, and base English Whisper transcription of `Calculate 2 plus 2.`.
- The observed `unknown` result isolated the remaining failure to transcript-to-skill routing rather than ALSA, VAD, or Whisper.
- Built-in skill registration is centralized through `create_builtin_skill_manager()` for text REPL, typed voice simulation, and the real single-turn script.
- Production-factory integration tests use injected hardware adapters while retaining the real VoiceCommandRouter, CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, and registered CalculatorSkill.
- Finite conversational wrappers, complete token consumption, and bounded transcript/expression lengths harden normalization without weakening calculator AST validation.
- `SingleTurnVoiceResultV1` now carries candidate skills and per-stage routing diagnostics; the manual script renders them only with `--diagnostic-routing`.
- Checkpoint pytest collection was 1002 tests.
- The owner must pull this checkpoint and confirm the new production trace and spoken `Result: 4`; Windows CI does not claim that final Raspberry Pi execution.

Phase 80: Safe Natural-Language Calculator Extraction

- A real Raspberry Pi fixed-duration turn produced the clear Whisper transcript `I'll calculate 2 plus 2.` but normalization rejected it as `unsupported_arithmetic_text` before routing.
- Root cause: arithmetic-candidate detection recognized the expression, while the approved wrapper list did not remove `I'll calculate`; the apostrophe and wrapper words then correctly failed the strict arithmetic grammar.
- `TranscriptNormalizer` now uses one bounded anchored wrapper registry for direct actions, polite requests, question forms, first-person requests, `tell me what ... is`, and an optional leading `Ares` vocative.
- The extractor removes no arbitrary middle words and permits only one exact trailing `is` form. The complete remainder must still satisfy one-expression-only arithmetic parsing and the unchanged CalculatorSkill AST policy.
- Ambiguous requests, trailing commands, multiple expressions, identifiers, imports, calls, assignments, paths, shell syntax, and unsupported operators fail before IntentParser.
- Successful and rejected extraction records `calculator_natural_language_wrapper`; diagnostic mode renders that rule with raw, cleaned, normalized, intent, candidate, planner, execution, and rejection fields.
- Production-factory integration proves `I'll calculate 2 plus 2.` and `What is two plus two?` reach the registered CalculatorSkill and return `Result: 4` with no unknown fallback.
- Historical checkpoint collection was 1058 tests.

Phase 81: Format-Safe Raspberry Pi Microphone Capture

- Root cause confirmed from Raspberry Pi ALSA output: raw `hw:2,0` accepted a 16 kHz request but supplied 44.1 kHz, while the old headerless VAD stream assumed the request had been honored.
- Production auto-stop resolves raw numeric hardware IDs to ALSA `plughw` conversion and requests canonical 16 kHz mono signed 16-bit PCM before framing or RMS analysis.
- Fixed-duration capture reads the actual WAV header and uses the centralized `core.WavAudio` boundary to downmix/resample supported PCM into a separately finalized canonical WAV.
- VAD and Whisper reject noncanonical, missing, empty, corrupt, truncated, or unsupported WAV input instead of reinterpreting bytes.
- Single-turn and bounded multi-turn requests propagate explicit diagnostic preservation; owner scripts report raw/assembled/normalized paths plus requested, actual, and canonical formats without implying playback.
- The fixed-duration fallback remains available, microphone monitoring remains disabled, and Brain/CoreService remain free of ALSA/resampling code.
- Historical checkpoint collection was 1079 tests.

Phase 82: Complete VAD Utterance Assembly and Duration-Safe Whisper Handoff

- Root cause isolated after real Raspberry Pi diagnostics: `POSSIBLE_SILENCE` compared the total pending-buffer length with the hangover, required only the smaller consecutive-low-frame guard, and discarded the entire pending block at completion.
- Repeated speech/pause transitions now append resumed pending frames exactly once to one persistent utterance buffer.
- Completion requires the full configured consecutive terminal-silence duration and removes only that suffix; earlier low-energy words and internal pauses remain ordered.
- Canonical 16 kHz mono signed 16-bit PCM normalization preserves every sample through a lossless copy path.
- Unique per-turn raw capture, assembled utterance, normalized Whisper input, and transcript diagnostic paths prevent stale-file and preview-file handoff.
- A configurable duration invariant fails before Whisper when normalized audio loses unexplained duration relative to the assembled utterance.
- Manual diagnostics report frame/sample/byte counts, intentional trim durations, stage paths, and the exact Whisper input duration. Stage playback is owner opt-in and occurs only after capture stops.
- Historical Phase 82 pytest collection was 1105 tests.

Phase 83: Safe Natural Calculator Questions and Playback Isolation

- Extended the finite anchored calculator wrapper registry for approved `how much`, `what does ... equal`, answer/result, first-person, polite, and optional ARES-address forms.
- Added the exact extracted arithmetic expression to V1 normalization, single-turn results, and bounded routing diagnostics.
- Preserved strict full-token arithmetic parsing and the existing CalculatorSkill AST evaluator; ambiguous text, trailing instructions, code-like input, malformed arithmetic, and limits still fail closed.
- Split intermediate-audio preservation from raw/assembled/normalized stage playback. Normal `--playback` now has one documented meaning: play the generated ARES response only.
- Retained old diagnostic flags as compatibility aliases without allowing preservation to imply playback.
- Historical Phase 83 pytest collection was 1162 tests.

Phase 84: Production-Style Single-Turn Voice Launcher

- Added `scripts/run_ares_voice.py` as the short owner-triggered command for exactly one production single-turn interaction.
- Reused `SingleTurnVoiceRequestV1`, the existing production factory, lifecycle/component health checks, `SingleTurnVoicePipeline`, the real Brain/skill route, the configured Piper profile registry, and the ALSA speaker adapter.
- Set verified Raspberry Pi defaults for `plughw:2,0`, `plughw:CARD=Device,DEV=0`, base English Whisper, `en_US-hfc_male-medium`, automatic end-of-speech, response playback, and a 300-second limit.
- Kept diagnostic routing, intermediate-file retention, and captured-stage playback independent and disabled by default. Normal startup cannot enable microphone monitoring or replay microphone audio.
- Added deterministic tests for repository-relative paths, defaults and overrides, preflight failure before capture, delegation, cleanup policy, response playback, diagnostic opt-in, exit codes, and the absence of duplicated speech-stage logic.
- The owner verified the complete launcher on Raspberry Pi with `How much is 2 + 2` and heard the Piper response `Result: 4`.
- Historical Phase 84 pytest collection was 1180 tests.

Phase 85: Explicit Persistent Owner Memory

- Added `OwnerProfileStore` for explicit bounded owner facts at `data/memory/owner_profile.json`.
- Registered `ares.owner_profile` v1 with the centralized schema migration, validation, backup, lock, and atomic-write framework.
- Added deterministic save, recall, update, forget, and missing-fact phrases through VoiceCommandRouter, CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, and registered OwnerMemorySkill.
- Added normalized key aliases such as `favorite colour`/`favorite color`, bounded key/value validation, path/control-character rejection, and protected credential/recovery-key rejection.
- Kept transcripts, recordings, complete conversations, inferred facts, and protected values out of the profile and normal operational events.
- Added a hardware-free production-path script and a six-process isolated persistence verifier.
- Historical Phase 85 pytest collection was 1245 tests.

Phase 86: Owner-Memory Routing and Persistence Hardening

- Fixed the production fall-through where an unrecognized explicit owner phrase beginning with `remember` could be accepted by the generic task rule.
- Owner-memory candidates now normalize and route before tasks; malformed candidates fail closed rather than creating tasks.
- Added declaration, explicit update, delete/forget, and bounded punctuation forms while preserving `remember to buy milk` and ordinary reminders as tasks.
- Added one exact owner-command-only alias for the observed local Whisper substitution `modified white color`; unrelated speech is not coerced into memory.
- Canonicalized the production profile path to repository-root `data/memory/owner_profile.json` independent of current working directory.
- Extended isolated fresh-process verification, owner-operation diagnostics, task-priority regression checks, and legacy profile compatibility coverage.
- Historical Phase 86 pytest collection was 1280 tests.

Phase 87: Central General Owner Memory

- Moved authoritative owner memory behind a CoreService-owned `OwnerMemoryService`; input/output adapters and voice scripts no longer construct the concrete JSON store.
- Added versioned `OwnerMemoryRequestV1` / `OwnerMemoryResultV1` boundaries and explicit remember, update, recall, forget, and bounded list actions.
- Generalized deterministic parsing for birthdays, residence, favorite items, pet names, work schedules, aliases, and bounded custom keys while keeping normal conversation non-persistent.
- Migrated `ares.owner_profile` from v1 to v2 without discarding existing facts. V2 records display/normalized keys, creation/update timestamps, and explicit-owner source metadata.
- Enforced 100 facts, 64-character normalized keys, 256-character strings, 10 scalar list items, and 65,536 serialized data bytes, plus protected-key and executable/instruction rejection.
- Added owner transaction locking, one retained last-known-good backup, atomic validated replacement, corruption fail-closed behavior, read-only inspection, and fresh-process verification.
- Added architecture guards proving voice does not own memory, the memory package imports no audio hardware providers, and production defines only one canonical owner-profile path.
- Historical Phase 87 pytest collection was 1350 tests.

Phase 88: General Explicit Long-Term Owner Memory

- Kept authoritative owner memory behind `CoreService -> OwnerMemorySkill -> OwnerMemoryService -> OwnerProfileStore`; voice remains an adapter and has no store or parser authority.
- Migrated `ares.owner_profile` v2 to v3 sequentially while preserving every keyed fact and adding structured `memories` plus the legacy bounded broad-confirmation field; Phase 90 moved current CRUD confirmation authority into separate transient state.
- Added explicit long-term trigger cleanup, a finite memory-type classifier, deterministic topic/token retrieval, exact-signature duplicate prevention, explicit superseding updates, and bounded forget/list behavior.
- Added a two-step exact confirmation for deleting all long-term owner memory; ambiguous or single-turn broad deletion fails closed.
- Added read-only memory/topic/type inspection, a fresh-process general-memory verifier, production voice/text parity, and static guards against voice-owned or hardware-coupled memory.
- Historical Phase 88 pytest collection was 1427 tests.

Phase 89: Whisper-Tolerant Explicit Owner Memory Routing

- Fixed the production fallthrough where an unrecognized explicit memory transcript could be consumed by the generic task rule.
- Added bounded, anchored normalization for `locked term memory`, `lock term memory`, `long turn memory`, and leading `remembering ... memory that` Whisper output without globally rewriting fact text.
- Preserved the structural split: `remember ... memory ... that <owner fact>` selects owner memory, while `remember to <action>` and `remember my task ...` remain task/reminder routes.
- Added canonical-trigger, extracted-fact, action, and routing-reason diagnostics before the existing central `OwnerMemoryService` write.
- Added fresh-process and production-factory regressions proving the two observed transcripts create two preferences, deduplicate, recall together, preserve keyed facts, and never create a tasks file.
- Historical Phase 89 pytest collection was 1465 tests.

Phase 90: Safe Central Owner-Memory Management

- Added central list, inspect, count, specific-delete, topic-delete, all-general-delete, keyed-fact-delete, confirmation, and cancellation actions through the existing CoreService/OwnerMemorySkill route.
- Added one versioned transient `ares.pending_owner_memory_action` v1 record at `data/runtime/pending_owner_memory_action.json`, with atomic writes, a 60-second default TTL, bounded target data, and cross-process confirmation for the one-turn production launcher.
- Specific deletion requires exactly one high-confidence match. Topic and all-general requests snapshot only general-memory ids; keyed-fact deletion binds a normalized key and revision digest. Nothing is deleted before confirmation.
- Preserved pending state across unrelated commands until expiry, replaced it on a new destructive request, and failed closed on expiry, corruption, target changes, write errors, or ambiguous/vague deletion language.
- Extended read-only inspection and added isolated fresh-process management verification plus production voice-route, task-collision, event-redaction, keyed/general separation, and architecture regressions.
- Historical Phase 90 pytest collection was 1506 tests.

Phase 91: Central Brain Session Manager

- Added one Capital/Core-owned `BrainSessionManager`, distinct from removable module lifecycle and independent of voice, hardware, skills, owner-memory storage, GPT, and network providers.
- Added versioned transition-request and lifecycle-snapshot contracts for `STOPPED`, boot/initialization, standby, active processing/response, standby return, shutdown, stopped, and explicit error/recovery states.
- Added strict rejection that preserves source state, injected-clock activity/inactivity tracking, unique active-session IDs, bounded consecutive-failure escalation, safe recovery, and lock-protected access.
- Added bounded lifecycle events and optional existing `EventHistoryStore` persistence without transcripts, memory values, audio, secrets, or file contents.
- Added read-only CoreService composition, a deterministic hardware-free verifier, and lifecycle/concurrency/privacy/regression tests. No persistent loop, background timer, wake word, listener, or City activation was added.
- Historical Phase 91 pytest collection was 1572 tests.

Phase 92: Persistent Foreground Brain Runtime

- Added Capital/Core `BrainRuntime` orchestration while retaining `BrainSessionManager` as the only Brain lifecycle-state authority.
- Added one boot-to-standby process, exact bounded activation/standby/shutdown phrases, one acknowledgement per activation, serialized multi-command active sessions, same-session CoreService/SkillManager routing, deterministic manager-owned inactivity return, and explicit full shutdown.
- Added V1 runtime request/result/snapshot/classification/loop contracts plus injected deterministic queue/collecting adapters and bounded foreground console adapters. No direct `input()` call, hardware provider, skill logic, memory store, or City lifecycle was placed in the runtime.
- Added bounded configuration validation, failure escalation/recovery, idempotent resource cleanup, privacy-safe runtime events, a fake-clock real-skill verifier, and an optional foreground text developer interface.
- No microphone wake-word listener, continuous capture, background service, daemon/systemd startup, autonomous City loading, GPT, cloud service, network listener, barge-in, or worker thread was added.
- Historical Phase 92 pytest collection was 1671 tests.

Phase 93: Foreground Raspberry Pi Standby Wake Runtime

- Added a Capital/Core-injected standby listener while retaining `BrainSessionManager` as the sole lifecycle authority. The listener never owns routing, skills, persistent memory, session IDs, standby, or shutdown decisions.
- Added staged low-cost wake handling: calibrated RMS VAD captures one bounded candidate, the existing canonical WAV boundary supplies 16 kHz mono signed 16-bit PCM, and local tiny English Whisper runs only after speech is detected.
- Added exact normalized wake classification for `ares`, `hey ares`, `hello ares`, and `wake up ares`, with bounded `ok`/`okay` fillers and false-positive rejection for arbitrary sentences containing the name.
- Reused `SingleTurnVoicePipeline` for active microphone/STT transport and Piper/ALSA output, then routed text through the real CoreService skill path. A shared capture/playback gate plus settling delay prevents self-wake and simultaneous input/output.
- Added V1 wake request/result/detection/snapshot/listen contracts, a capability manifest, strict configuration, privacy-safe events, unique temporary candidate files, default cleanup, cancellation, bounded failures, and no hidden threads.
- Added the foreground `scripts/run_ares_standby_voice.py`, deterministic injected-adapter verifier, and bounded Raspberry Pi hardware helper. No systemd unit, boot startup, daemonization, GPT, cloud service, barge-in, or autonomous City loading was added.
- Historical Phase 93 pytest collection was 1769 tests.

Phase 94: Exact Wake Alias and Diagnostic Hardening

- Real Raspberry Pi evidence proved capture, calibrated VAD, canonical 16 kHz WAV generation, cleanup, and the persistent foreground listener were operating while wake classification remained rejected. The classifier accepted only `Ares` forms even though local Whisper is known to emit `Aris`.
- Added a validated alias-plus-prefix model with defaults `ares` and `aris`. The complete normalized candidate must equal one generated phrase; no substring, arbitrary fuzzy correction, or edit-distance matching was added.
- Added explicit terminal-only `--diagnostic-wake` output for raw/cleaned/normalized local transcripts, selected alias/phrase, rejection reason, capture timing, Whisper status/timing/model, and lifecycle state. These values remain absent from events, owner memory, and normal output.
- Separated actual candidate, raw-stream, and processing durations; validated the current canonical WAV header before Whisper; added candidate/raw hard limits; reduced the diagnostic PCM margin; and adopted wake-specific 0.3-second pre-roll, two-frame speech evidence, and 0.8-second terminal silence defaults.
- Added one-attempt diagnosis and bounded seven-stage hardware verification. Explicit retention requires diagnostics, preserves only the latest candidate by default, and never replays microphone audio automatically.
- Current pytest collection is 1800 tests.

Current State

ARES is currently at the completed Architecture Hardening foundation plus a central deterministic Brain session state machine and persistent foreground runtime, explicit ALSA microphone/speaker adapters, offline Whisper and Piper adapters, verified configurable voice profiles, controlled single-turn and bounded multi-turn pipelines, adaptive calibrated RMS end-of-speech capture, ordered complete-utterance assembly, a duration-checked canonical 16 kHz mono PCM handoff, shared production skill registration, safe anchored natural-language calculator routing, and CoreService-owned general explicit long-term owner memory with confirmation-gated CRUD. The assistant remains deterministic and offline. The foreground runtime can now poll one bounded Raspberry Pi standby wake adapter, activate on an exact locally transcribed phrase, process multiple serial voice commands under one session ID, return to standby on inactivity or an owner stop phrase, and stop on an explicit shutdown phrase. It still does not activate Cities. Brain/CoreService remain free of ALSA, audio conversion, VAD, Whisper, Piper, model paths, and subprocess details; those remain injected adapters. Owner facts and general memories are written only by explicit bounded memory commands, and ordinary transcripts/recordings are not persisted.

The current active interface is:

- `interfaces.text_repl`
- `scripts.run_ares_voice` for one explicit foreground voice turn
- `scripts.run_ares_brain_runtime_text` for explicit persistent foreground text verification
- `scripts.run_ares_standby_voice` for the foreground Raspberry Pi standby wake and active voice path

The current deterministic answer paths are:

- Intent modules for weather, news, knowledge, stocks, greetings, and goodbye
- `IntentParser` plus `ToolSelector` for time/date, legacy memory recall, explicit owner-memory operations, calculator arithmetic, goals, notes, tasks, weather, market, calendar, event-history queries, voice-session starts, and voice-session status queries
- `Planner`, `MultiStepPlan`, `ConfirmationManager`, `ToolChain`, `ExecutionPipeline`, and `SkillManager` for local goals, notes, tasks, calculator, weather, market, calendar, voice sessions, voice-session status, context responses, confirmations, and conversation memory plan execution
- `ToolAdapterRegistry`, `ExternalAdapterConfig`, `SecretsGuard`, `RealWeatherAdapter`, and `RealMarketAdapter` plus explicit `tool_adapter` PlanSteps for future adapter execution infrastructure
- `CoreService`, the lifecycle/manifest/health/resource boundaries, local event infrastructure, Device/PC City, and Voice City contracts/adapters provide the safe service path. The Voice City surface now includes `RmsVoiceActivityCapture`, versioned VAD contracts, `VoiceProfile`, `VoiceProfileRegistry`, profile-aware TTS contracts, `LinuxPiperTextToSpeechAdapter`, `LinuxAlsaSpeakerAdapter`, `SingleTurnVoicePipeline`, and `MultiTurnVoiceSession` while preserving mock/null adapters, fixed-duration capture, and explicit-only real audio behavior.
- In-memory conversation context for recent handled skill turns

The current pytest collection is 1800 tests.

The current memory paths are:

- `MemoryStore` for conversation-style memory
- `UserProfileStore` for persistent user facts
- `OwnerMemoryService` as the central Brain-facing keyed-fact and general explicit long-term memory API, backed by one v3 `OwnerProfileStore` at `data/memory/owner_profile.json`
- `GoalsStore` for persistent long-term goals
- `NotesStore` for persistent local notes
- `TasksStore` for persistent offline tasks
- `ReminderScheduler` for passive due/upcoming task queries
- `ConversationContextManager` for RAM-only short-term skill context

Long-Term City Model

The long-term roadmap uses a city model. ARES Brain is the capital city and remains responsible for identity, memory, profile, goals, planning, decisions, and history with the owner.

Specialized cities connect to the capital through explicit bridges:

- Voice City
- Vision City
- Device/PC City
- Weather City
- Market City
- Calendar City
- Home City
- Robot Body City
- Codex City

Core Services City is a shared infrastructure city for scheduler, permissions, logging, configuration, health monitoring, plugin manager, secrets guard, and confirmation layer. These services should be reused by specialized cities rather than duplicated.

Codex City is a future maintenance city. It should check the ARES GitHub repository, pull latest code, run tests, check compile, check docs freshness, report problems, and suggest fixes. Codex City must never auto-edit without owner approval.

This roadmap entry documents the current safe VoiceService skeleton, explicit microphone/STT/TTS/speaker adapters, validated Piper voice profiles, Raspberry Pi setup/verification scripts, versioned RMS VAD capture, VoiceCommandRouter, simulated VoicePipeline, controlled single-turn and bounded multi-turn orchestration, and completed Architecture Hardening foundation. It does not start scheduler implementation, GitHub API integration, self-modifying behavior, GPT, internet runtime access, real APIs, notifications, daemon installation, background timers, threads, wake word, unbounded conversation loops, background listening, or cloud TTS fallback.

Next Priorities

NEXT:
Phase 3 Real Voice Integration

1. Linux ALSA microphone adapter for explicit Raspberry Pi capture tests. Completed.
2. Offline Whisper STT adapter for explicit Raspberry Pi WAV transcription. Completed.
3. Raspberry Pi whisper.cpp runtime preparation scripts. Completed.
4. Hardened Raspberry Pi speech-input verification and ALSA monitoring helper. Completed.
5. Reliable English-mode Whisper verification defaults. Completed.
6. Modular offline Piper TTS and explicit ALSA speaker playback. Completed.
7. Configurable Piper voice profiles with `en_US-hfc_male-medium` as default. Completed.
8. Install and audibly verify the default male profile on Raspberry Pi. Completed.
9. Controlled owner-triggered single-turn voice pipeline. Completed.
10. Controlled owner-triggered bounded multi-turn voice session. Completed.
11. RMS VAD and automatic end-of-speech capture with fixed-duration fallback. Completed.
12. Adaptive ambient calibration and robust calculator voice routing. Completed in CI with synthetic PCM and real Brain routing.
13. Validate clear USB capture, `completed_after_silence`, and base English Whisper transcription on Raspberry Pi. Completed by owner.
14. Centralize production skill registration and add bounded routing diagnostics. Completed.
15. Add bounded anchored natural-language calculator wrapper extraction. Completed.
16. Add canonical ALSA device resolution and validated 16 kHz mono PCM normalization before VAD/Whisper. Completed.
17. Preserve complete VAD utterances across repeated pause/resume transitions and reject unexplained normalized-duration loss. Completed in CI with deterministic PCM.
18. Verify raw/assembled/normalized duration preservation on Raspberry Pi. Completed by owner: `7.060s` raw and `5.400s` assembled/normalized/Whisper input with `duration_consistent`.
19. Confirm `How much is two plus two?` selects CalculatorSkill and speaks `Result: 4` while normal playback does not replay the microphone. Completed by owner.
20. Add the production-style one-command launcher with verified Raspberry Pi defaults. Completed in deterministic CI.
21. Verify the one-command launcher on Raspberry Pi. Completed by owner with calculator result and audible male Piper response.
22. Add explicit bounded owner-fact persistence through the real production skill route. Completed in deterministic CI and verified by the owner for favorite-color save/recall across processes.
23. Harden owner-memory routing priority and the canonical repository-root profile path. Completed.
24. Centralize general owner facts behind CoreService, migrate the profile to v2, and add inspection/fresh-process verification. Completed in deterministic CI; Raspberry Pi voice verification remains owner-run.
25. Add bounded general explicit long-term memories, migrate the profile from v2 to v3, and verify one central Brain-owned route for text and voice. Completed in deterministic CI; Raspberry Pi voice verification remains owner-run.
26. Harden explicit-memory routing for the real `locked term memory` and `remembering a long term memory that ...` Whisper transcripts. Completed in deterministic CI; post-pull Raspberry Pi verification remains owner-run.
27. Add complete central list/count/inspect and confirmation-gated specific/topic/all-general/keyed deletion with cross-process transient state. Completed in deterministic CI; post-pull Raspberry Pi verification remains owner-run.
28. Add the central deterministic Brain Session Manager. Completed in deterministic CI; Raspberry Pi hardware is not required.
29. Add the central persistent foreground Brain Runtime with deterministic text activation, multi-command sessions, manager-owned inactivity standby, and explicit shutdown. Completed in deterministic CI.
30. Verify the hardware-free Brain Runtime scripts after pulling on Raspberry Pi. Pending owner run.
31. Add one bounded real microphone wake activation adapter without moving runtime ownership out of Capital/Core. Completed in deterministic CI; real Raspberry Pi wake verification remains owner-run.
32. Run the one-attempt wake diagnostic, then the bounded standby-wake hardware helper and foreground runtime on Raspberry Pi. Confirm the exact `Ares`/`Aris` classification, acknowledgement, command response, standby, second activation, and shutdown before recording hardware proof.
33. Only after stable hardware proof consider a separately reviewed systemd/boot-startup checkpoint. Wider background listening and autonomous City activation remain out of scope.

What Must Not Be Started Yet

- No real voice/audio implementation beyond the explicit adapters, setup/verifier scripts, controlled single-turn/bounded multi-turn paths, and the foreground bounded standby-wake runtime already documented.
- No GPT or LLM integration.
- No embeddings.
- No notification scheduling or delivery.
- No calendar integration.
- No Raspberry Pi deployment automation beyond existing owner-run Whisper/Piper setup and verification scripts.
- No Vosk, daemon/service installation, internet access, unbounded hidden conversation loop, automatic boot-time microphone activation, GPT, or cloud TTS fallback. Standby wake capture exists only inside the explicit foreground runtime.
- No new skills beyond explicitly approved, bounded checkpoints such as the current owner-memory skill.
- No AI parser or regex-only parser rewrite.
- No robotics or movement integration.
- No vision integration.
- No shutdown/restart, arbitrary app launching, arbitrary shell command execution, Telegram, remote control, or unconfirmed dangerous device action execution.
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
