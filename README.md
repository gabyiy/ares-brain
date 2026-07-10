ARES

Autonomous Reasoning & Exploration System

ARES is a modular AI assistant built on Raspberry Pi.

The project focuses on building an assistant that can eventually understand natural language, remember conversations, reason, search information, control hardware, and interact completely by voice.

---

Current Version

ARES v1.61 - Speech-to-Text Adapter Abstraction

---

Current Architecture

The active runtime includes `core.IntentParser` for structured local intents, `core.Planner` and `core.MultiStepPlan` for ordered local multi-step execution plans, context-aware planning through safe `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` interfaces, `core.Confirmation` for explicit user confirmation before destructive or important actions, `core.ToolChain` for bounded local tool chaining, `core.ExecutionPipeline` for sequential plan execution and partial-result reporting, `core.ToolAdapter` for offline adapter-backed tools, `core.DeviceAction`, `DeviceActionRegistry`, `AppLaunchConfig`, `AppAllowlistLoader`, `core.PCService`, `PCServiceResult`, `PCStatus`, `PCCapabilities`, `WindowsPCService`, and `LocalDeviceActionAdapter` for local device action foundations with `safe`, `confirmation_required`, and `forbidden` classifications, confirmed Windows-only `lock_pc` and `sleep_pc`, a config-backed allowlist-only Windows app launcher with only `calculator` enabled in `config/apps.json`, disabled `notepad` and `browser` entries, `list_apps`, confirmation-gated `open_app`, no arbitrary user paths, no shell commands, all PC operations routed through PCService as the dedicated entry point, structured local `get_status()` responses for safe PC information, and dynamic local `get_capabilities()` responses for supported actions, apps, status providers, and services, `skills.builtin.DeviceActionSkill` for safe live routing of `echo`, `list device actions`, `list apps`, and `system status` plus confirmation-gated `lock_pc`/`sleep_pc`/`open_app` and stable forbidden responses for dangerous placeholders, `core.AdapterConfig` and `core.SecretsGuard` for future adapter configuration safety, `core.RealWeatherAdapter` as an opt-in HTTP-capable weather adapter gated by real-mode config and environment keys, `core.RealMarketAdapter` as an opt-in HTTP-capable market adapter gated by real-mode config and environment keys, `skills.builtin.WeatherSkill` for mock/local weather answers, `skills.builtin.MarketSkill` for mock/local market quotes, `skills.builtin.CalendarSkill` for mock/local schedule answers, `memory.GoalsStore` for persistent long-term goals, `memory.NotesStore` for persistent local notes, `memory.TasksStore` for offline tasks, `memory.ReminderScheduler` for passive due-time queries, and `core.ConversationContextManager` for short-term in-memory skill context.

The permanent architecture reference is `docs/ARCHITECTURE.md`. It documents the Brain/CoreService capital city model, current CoreService and PCService boundaries, capability discovery, future cities, upgrade philosophy, design rules, and long-term vision.

`core.CoreService` now sits between the Brain and external/local services where practical. It owns service registration, registers `PCService` as `pc` by default, exposes `get_service(name)`, `list_services()`, `get_capabilities()`, and `route_by_capability()`, and aggregates capability data from registered services without adding GPT, internet, remote execution, or hardware behavior. CoreService tracks city lifecycle states as `idle`, `active`, `failed`, and `disabled`; lazy capability routing activates only the selected city for a request and leaves unused cities idle.

`core.EventBus` now provides an internal future city event skeleton with `Event` records shaped as source, type, priority, payload, and timestamp. Supported priorities are `low`, `normal`, `high`, and `critical`. This is future-use infrastructure only; it does not start background listeners, notifications, camera loops, internet access, GPT, or any daemon.

CoreService can now receive internal city events through `handle_event(event)` and return a stable decision result: `recorded`, `ignored`, or `escalated`. Low and normal events are recorded only, high and critical events are marked escalated, and disabled or unknown event sources fail safely. This is internal routing only; it does not send notifications, start listeners, call devices, access the internet, or use GPT.

`events.EventHistoryStore` now persists internal event decisions/results to local JSON history with safe size limits. It can query recent events by source, type, and priority. This is internal memory/logging only; it does not send notifications, call devices, start a daemon, access the internet, or use GPT.

CoreService now accepts an optional `EventHistoryStore`. When configured, `handle_event(event)` stores each handled internal event decision/result locally, including `recorded`, `escalated`, and safely ignored unknown/disabled source events. The `failed` decision value is reserved for future failed event-handling paths. This is synchronous internal history persistence only; it does not start listeners, send notifications, call devices, access the internet, or use GPT.

`skills.EventHistorySkill` now provides read-only local queries for recent internal events. It supports "what happened recently", "show recent events", and "show critical events" through the existing IntentParser, Planner, ExecutionPipeline, SkillManager, and REPL path. It only reads local `EventHistoryStore` data and does not send notifications, start listeners, call devices, access the internet, or use GPT.

`core.VoiceService` now provides the Voice City skeleton. CoreService registers a safe placeholder VoiceService as `voice` by default. It owns `VoiceInput` and `VoiceOutput` components, currently implemented as adapter-backed `NullVoiceInput` and `NullVoiceOutput`. These expose `listen_once()`, `speak(text)`, `get_capabilities()`, and `get_status()` contracts with structured placeholder data and explicit safeguards showing microphone, speaker, STT, TTS, wake word, background listening, GPT, and internet are disabled. `VoiceInputAdapter`, `VoiceOutputAdapter`, `MockVoiceInputAdapter`, and `MockVoiceOutputAdapter` now form the safe audio adapter contract layer for future real audio providers. `VoiceTextRequest`, `VoiceSingleTurnLoop`, and `VoiceSessionLoop` provide adapter-backed Voice City flow foundations: mock input capture is converted to text requests, passed to an injected existing text/CoreService handler, and responses are sent to mock output. `VoiceSessionLoop` supports bounded multi-turn mock sessions with `max_turns`, stop phrases, safe empty-input handling, and transcript/history output. `core.VoiceLoop` remains the one-shot text loop foundation: it calls `VoiceInput.listen_once()`, ignores empty input safely, passes recognized text to an injected existing text/planner/execution handler, and sends the final response text to `VoiceOutput.speak()`.

`core.Microphone` now defines the microphone adapter abstraction for future real audio capture without binding ARES to Whisper, Vosk, Piper, wake word detection, or any hardware-specific implementation. `AudioChunk` models raw audio chunk metadata, `MicrophoneAdapter` defines `start()`, `stop()`, and `read_chunk(timeout_seconds, cancel_requested)`, and `MockMicrophoneAdapter` provides deterministic test behavior for lifecycle, timeout, cancellation, and safe failure paths. `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept the microphone adapter through dependency injection so Voice City can swap implementations later without changing the Brain or current text/skill path.

`core.SpeechToText` now defines the speech-to-text adapter abstraction for converting microphone `AudioChunk` objects into text without binding ARES to Whisper, Vosk, wake word detection, internet services, GPT, or hardware-specific code. `TranscriptionResult` includes transcription text, status, error details, and a bounded `confidence` field. `SpeechToTextAdapter` defines `transcribe(audio_chunk)`, `get_status()`, and `get_capabilities()`, and `MockSpeechToTextAdapter` provides deterministic success, empty-audio, low-confidence, no-transcription, and failure behavior. `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept the speech-to-text adapter through dependency injection.

Real Whisper, Vosk, Piper, microphone, speaker, wake word, and background listener integrations come later. The current adapter layer is mock/local only and does not access audio hardware.

`skills.VoiceSessionSkill` now exposes the safe mock voice session through the normal text command path. It recognizes "start voice session", "start mock voice", and "run voice test", then uses `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, and `VoiceSessionLoop` with a bounded `max_turns` limit. It returns a transcript summary through the existing IntentParser, Planner, ExecutionPipeline, SkillManager, and REPL path. It does not access a microphone, speaker, wake word, background listener, GPT, internet, or real audio provider.

Voice sessions now write safe local operational events to `events.EventHistoryStore` when a store is available in `SkillContext`. Recorded event types are `voice_session.started`, `voice_session.stopped`, `voice_session.adapter_failure`, and `voice_session.max_turns_reached`. Event payloads store status, turn counts, max-turn metadata, and adapter failure details only; they do not store mock transcript content. `skills.EventHistorySkill` can show these events through "show recent events".

ARES can now answer "what happened in voice session", "show last voice session", and "voice session status". The query reads the latest local `voice_session.*` events and returns a started/stopped/failure/max_turns summary without starting audio hardware or a new voice session.

Phase 3 Voice Checkpoint

The Phase 3 voice foundation is documented and frozen before any real audio work.

Checkpoint status:

- Test count: `391 tests`
- Voice City skeleton
- Audio adapter contracts
- Single-turn loop
- Multi-turn mock session
- `VoiceSessionSkill`
- Voice session event logging
- Voice session status query

This checkpoint is documentation-only. It does not add real microphone access, speaker output, wake word detection, real STT, real TTS, background listening, notifications, GPT, internet access, or real device/event automation.

Phase 3 Foundation Checkpoint

The Phase 3 foundation is frozen before adding real audio. This checkpoint confirms the currently implemented foundation:

- Voice City skeleton
- Manual Voice City text loop simulation
- Lazy city routing through CoreService capability metadata
- Internal `core.EventBus`
- Local `events.EventHistoryStore`
- Read-only `skills.EventHistorySkill`

Checkpoint pytest collection before the audio adapter contracts: `351 tests`.

Latest checkpoint baseline commits:

- `8d39403` Document event history query skill
- `844d4a5` Add event history query skill
- `bd72981` Document core service event history persistence
- `d2c8a6c` Persist core service event decisions
- `8803fb8` Document local event history store
- `9fe355c` Add local event history store
- `70c5e04` Document core service event decisions
- `5c71bb1` Add core service event decisions
- `1de5e09` Document internal core event bus skeleton
- `032f132` Add internal core event bus skeleton
- `78cb4a9` Document city lifecycle lazy routing
- `16cc8d4` Add city lifecycle lazy routing

No runtime code changed for this checkpoint. Real microphone access, speaker output, wake word detection, real STT, real TTS, background listening, notifications, GPT, internet access, and real device/event automation remain disabled until explicitly approved.

Phase 2 Complete

Phase 2 is now stabilized as the architecture baseline for future city work. The current foundation has a consistent Brain-to-service path: `SkillManager` carries `CoreService`, `SkillContext` exposes that same service boundary, `CoreService` owns service registration and capability aggregation, and `PCService` remains the dedicated status/capability/action boundary for local PC operations. The `pc` service name is centralized through `PC_SERVICE_NAME`, services expose `get_capabilities()`, and PC services expose `get_status()`/`status()` where applicable. This cleanup did not add new runtime behavior.

Long-Term City Model

The long-term architecture treats ARES Brain as the capital city. The capital owns identity, memory, profile, goals, planning, decisions, and history with the owner. Specialized cities connect to the capital through explicit bridges instead of bypassing the brain.

ARES Behavior Schematic

ARES behavior uses a Capital City / Cities / Districts / Villages / Houses model:

- Capital = Brain identity
- City Hall = CoreService
- Cities = major services
- Districts = sub-services
- Villages = adapters
- Houses = concrete devices, APIs, files, models

The Brain stores only identity-level knowledge:

- long-term memory
- short-term context
- user profile
- known people and friends
- learned preferences
- goals
- personality
- relationship history
- decision history

Replaceable services handle implementation details:

- Weather City handles weather APIs.
- Voice City handles STT, TTS, and wake word.
- Vision City handles camera and face recognition.
- PC City handles Windows and device actions.
- Codex City handles GitHub and testing.
- Home City handles smart plugs and home devices.

Examples:

- Brain remembers "Gabriel wants morning weather reports"; Weather City decides which API or provider to use.
- Brain remembers "Andrei is a known friend"; Vision City handles face embeddings and matching.

Design rule: The Brain must never directly know API keys, Windows commands, camera internals, model internals, hardware commands, or provider-specific parsing.

Specialized cities planned around the capital:

- Voice City
- Vision City
- Device/PC City
- Weather City
- Market City
- Calendar City
- Home City
- Robot Body City
- Codex City

Core Services City provides shared infrastructure for the whole system: scheduler, permissions, logging, configuration, health monitoring, plugin manager, secrets guard, and confirmation layer.

Codex City is a future maintenance city. Its planned role is to check the ARES GitHub repository, pull latest code, run tests, check compile, check docs freshness, report problems, and suggest fixes. Codex City must never auto-edit without owner approval.

This city model is documentation only. It does not add scheduler implementation, GitHub API integration, self-modifying behavior, GPT, voice, internet access, real APIs, or notifications.

Future Architecture Vision

The long-term ARES architecture is one modular Brain connected to many replaceable services, adapters, and devices.

Future target roles:

- Brain = identity, memory, reasoning, planning, goals, personality, owner history, and decision history.
- CoreService = intent and capability router between the Brain and registered services.
- Cities = major abilities such as Voice, Vision, PC, Weather, Market, Calendar, Home, Robot Body, and Codex.
- Adapters = hardware or API connectors used by a city, such as speech engines, cameras, weather providers, app launchers, or robot control libraries.
- Devices = physical bodies and interfaces such as Raspberry Pi, Jetson Orin, Windows PC, phone, robot body, microphone, speaker, camera, and home devices.

This is future vision, not implemented runtime behavior. The current runtime remains local, text-first, and deterministic unless a specific feature has been explicitly implemented and verified.

One Brain, Many Bodies

The long-term deployment model is one ARES Brain that can inhabit or coordinate multiple bodies without losing identity.

Future home-server vision:

- ARES Home Server stores the Brain, master memory, user profile, goals, relationship history, sync service, and backups.
- Raspberry Pi acts as a small local body for lightweight interaction.
- Jetson Orin acts as a stronger robot/vision body.
- Windows PC acts as a desktop/device-control body.
- Phone acts as a mobile interface body.
- Other future devices can join as clients only through service boundaries and capability discovery.

Hard rule: hardware-specific code must never enter the Brain. Operating-system commands, camera drivers, microphone and speaker internals, robot motor code, model-specific parsing, provider-specific API formats, and raw secrets belong in services, adapters, or devices. The Brain should communicate through structured data and CoreService capabilities.

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
- Persistent local goals store
- Built-in goals skill
- In-memory conversation context manager
- Structured intent parser and `Intent` object
- Execution pipeline for planner steps
- Explicit `MultiStepPlan` support
- Context-aware planner support
- Action confirmation layer
- Tool chaining for bounded local multi-step requests
- External tool adapter interface
- External adapter config and secrets guard
- Real weather adapter skeleton
- Real weather adapter HTTP logic
- Real market adapter skeleton
- Real market adapter HTTP logic
- Safe local device action framework skeleton
- Built-in weather skill using the offline mock weather adapter
- Built-in market skill using the offline mock market adapter
- Built-in calendar skill using the offline mock calendar adapter
- Built-in device action skill using safe local mock actions
- Device dangerous-action confirmation gate
- Confirmed Windows lock device action
- Confirmed Windows sleep device action
- Device app launcher skeleton
- Confirmed Windows app launcher
- App launcher allowlist config
- Calculator app allowlist enablement
- Manual calculator launch verification script
- PCService abstraction for Windows operations
- PCService structured status provider
- PCService capability discovery
- CoreService orchestration layer
- City lifecycle states and lazy capability routing
- Internal Core EventBus skeleton
- CoreService internal event decision routing
- Local EventHistoryStore for internal event decisions/results
- CoreService optional EventHistoryStore persistence
- EventHistorySkill read-only local event queries
- Phase 2 architecture stabilization
- Voice City service skeleton
- Voice City input/output contracts
- Voice City text loop foundation
- Voice City audio adapter contracts
- Voice City adapter-backed single-turn loop
- Voice City multi-turn mock session
- Voice Session Skill
- Voice Session event logging
- Voice Session status query
- Automated pytest suite
- Session handoff documentation
- Modular project structure
- Git version control

---

Current Features

ARES currently understands questions such as:

- hello ares
- goodbye ares
- weather
- weather today
- weather tomorrow
- weather in Madrid
- stock nvidia
- nvidia stock
- apple stock
- market price for tesla
- what is on my calendar today
- calendar tomorrow
- schedule today
- do I have anything tomorrow
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
- add goal build ARES memory
- list goals
- show my goals
- show goal <id>
- complete goal <id>
- pause goal <id>
- delete goal <id>
- add milestone to goal <id>
- yes
- confirm
- no
- cancel
- What's the weather tomorrow and remind me to go to the gym
- Show my goals and today's calendar
- remind me about my main goal tomorrow
- what should I do next for my goals
- show my goals and notes about gym
- echo hello ARES
- list device actions
- list apps
- system status
- lock pc
- sleep pc
- open app calculator
- start voice session
- start mock voice
- run voice test
- what happened in voice session
- show last voice session
- voice session status

Each request is automatically routed to its correct intent.

Implemented Features

- Modular text intent routing
- Event bus for runtime lifecycle events
- Short-term and long-term memory v1 storage
- Separate persistent user profile memory
- Skill registry, skill manager, and skill plugin foundation
- Tool selector for best local skill selection
- Structured intent parser for deterministic local intent/entity extraction
- Multi-step planner for local goals, notes, tasks, calculator, weather, market, calendar, and conversation memory steps
- `MultiStepPlan` marker support for ordered requests with more than one executable step
- Context-aware planner reads user profile, goals, notes, and tasks through store interfaces only
- Safe context-only planner responses when local context is missing
- Action confirmation model with pending confirmation ids and confirm/cancel decisions
- Tool chaining with max depth, loop prevention, execution trace, and chain history
- Execution pipeline for ordered plan step execution, confirmation pauses, aggregated final responses, partial-result reporting, execution results, logging, and rollback hooks
- External ToolAdapter foundation with `ToolRequest`, `ToolResponse`, `ToolAdapterRegistry`, and offline mock weather/market adapters
- DeviceAction foundation with `DeviceAction`, `DeviceActionResult`, `DeviceActionRegistry`, and `LocalDeviceActionAdapter`
- PCService abstraction with `PCServiceResult` and `WindowsPCService` as the single DeviceAction entry point for `lock`, `sleep`, `open_app`, and `status`
- Structured PCService status provider with `PCStatus` and safe local `get_status()` fields for operating system, hostname, current user, Python version, optional uptime, and available actions
- Dynamic PCService capability discovery with `PCCapabilities` and safe local `get_capabilities()` fields for supported device actions, supported applications, available status providers, and available services
- CoreService orchestration layer with service registration, default `PCService` registration as `pc`, `get_service(name)`, `list_services()`, aggregate `get_capabilities()` over registered services, city lifecycle metadata, and lazy `route_by_capability()` execution
- Internal `core.EventBus` skeleton with event source, type, priority, payload, timestamp, publish/subscribe, and priority-ordered history
- CoreService internal event handling with `ignored`, `recorded`, and `escalated` decisions for city events
- Local `events.EventHistoryStore` for persisted internal event decisions/results with source/type/priority queries and bounded history size
- Optional CoreService to EventHistoryStore persistence for handled internal event decisions/results
- Read-only `skills.EventHistorySkill` for querying recent and critical internal event history
- Phase 2 architecture cleanup with centralized `PC_SERVICE_NAME`, CoreService carried through `SkillContext`, and focused service registration/capability contract tests
- Voice City foundation with `VoiceService`, `PlaceholderVoiceService`, `VoiceInput`, `VoiceOutput`, `VoiceInputAdapter`, `VoiceOutputAdapter`, `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, `NullVoiceInput`, `NullVoiceOutput`, `VoiceStatus`, `VoiceCapabilities`, `VoiceTextRequest`, `VoiceLoop`, `VoiceLoopResult`, `VoiceSingleTurnLoop`, `VoiceSessionLoop`, `VoiceSessionResult`, `VoiceSessionTurn`, default CoreService registration as `voice`, safe placeholder status, adapter-backed mock/local input and output, a one-shot placeholder text loop, an adapter-backed single-turn loop, bounded multi-turn mock sessions, and no audio hardware access
- Microphone adapter abstraction with `AudioChunk`, `MicrophoneAdapter`, `MicrophoneResult`, `MockMicrophoneAdapter`, safe lifecycle methods, timeout handling, cancellation support, and Voice City dependency injection
- Speech-to-text adapter abstraction with `TranscriptionResult`, `SpeechToTextAdapter`, `MockSpeechToTextAdapter`, confidence scores, empty transcription handling, low-confidence handling, safe failure results, and Voice City dependency injection
- Built-in `VoiceSessionSkill` for starting bounded mock Voice City sessions from text commands through IntentParser, Planner, ExecutionPipeline, SkillManager, and the REPL path
- Safe Voice Session event logging to `EventHistoryStore` for session start, stop, adapter failure, and max-turn completion events
- Read-only Voice Session status queries for the latest mock session event group
- Device action danger classification with `safe`, `confirmation_required`, and `forbidden`
- Confirmed Windows-only `lock_pc` action after explicit user approval
- Confirmed Windows-only `sleep_pc` action after explicit user approval
- Config-backed allowlist-only Windows app launcher with `AppLaunchConfig`, `AppAllowlistLoader`, one enabled calculator entry in `config/apps.json`, disabled notepad/browser entries, `list_apps`, and confirmation-gated `open_app`
- Owner-run manual calculator launch verification script with exact typed confirmation
- Owner-run manual Voice City text simulation script using typed input, VoiceLoop, and NullVoiceOutput
- External adapter config model with enabled, mode, env-key name, base URL, timeout, placeholder detection, and secret validation
- RealWeatherAdapter HTTP logic gated by `mode=real`, env-key lookup, timeout handling, safe errors, and normalized ARES weather output
- RealMarketAdapter HTTP logic gated by `mode=real`, env-key lookup, timeout handling, safe errors, and normalized ARES market output
- Built-in weather skill backed by `ToolAdapterRegistry` and `MockWeatherAdapter`
- Built-in market skill backed by `ToolAdapterRegistry` and `MockMarketAdapter`
- Built-in calendar skill backed by `ToolAdapterRegistry` and `MockCalendarAdapter`
- Built-in device action skill backed by `LocalDeviceActionAdapter`
- Built-in time/date skill
- Built-in memory recall skill for saved profile facts
- Built-in calculator skill for safe local arithmetic
- Built-in notes skill for persistent local notes
- Built-in tasks skill for offline reminders/tasks
- Built-in goals skill for persistent long-term goal management
- ReminderScheduler foundation for parsing task due text and finding due/upcoming tasks
- In-memory conversation context for recent skill turns
- Text REPL with conversation turn storage
- Pytest automated coverage for 412 tests across current core modules
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

Current pytest collection: `412 tests`.

Manual Calculator Launch Verification

The calculator launch verification script is owner-run only and is not executed by automated tests:

```powershell
py scripts\manual_verify_calculator_launch.py
```

The script prints a warning, shows `App id: calculator`, requires the exact typed confirmation `YES_OPEN_CALCULATOR`, and then calls the same `LocalDeviceActionAdapter.execute("open_app", ...)` path used by ARES. Any other input refuses the launch. Tests mock this path and do not open Calculator.

Manual Voice Text Simulation

The Voice City text simulation script is owner-run only and is not executed by automated tests:

```powershell
py scripts\manual_voice_text_loop.py
```

The script prints a text-only warning, accepts one typed input line, passes it through `VoiceLoop` into the existing local SkillManager planner/execution path, and prints the final response. It uses typed text input plus `NullVoiceOutput`; it does not access a microphone, speaker, wake word, real STT, real TTS, GPT, internet, or a background loop.

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
- GoalsSkill runs as a priority local skill for long-term goal commands.
- NotesSkill runs as a priority local skill for note commands.
- TasksSkill runs as a priority local skill for task/reminder commands.
- WeatherSkill runs as a priority local skill for weather commands and uses only the offline `MockWeatherAdapter`.
- MarketSkill runs as a priority local skill for stock/market quote commands and uses only the offline `MockMarketAdapter`.
- CalendarSkill runs as a priority local skill for schedule/calendar commands and uses only the offline `MockCalendarAdapter`.
- SkillManager parses user text into a structured `Intent` before ToolSelector runs.
- ToolSelector builds a `Plan` before selection so multi-step requests can be inspected before execution.
- Planner returns a `MultiStepPlan` when a request contains more than one executable local step.
- Planner can use existing store interfaces for profile favorites, main goals, matching notes, and related open tasks.
- Planner never reads data files directly and returns deterministic empty-context responses when context is unavailable.
- ToolSelector first scores matching `intent_names`, then falls back to legacy triggers only for unknown intents.
- SkillManager delegates executable planner steps to ExecutionPipeline.
- SkillManager validates executable planner steps through ToolChain before ExecutionPipeline runs.
- SkillManager handles `yes`, `confirm`, `no`, and `cancel` as confirmation decisions when a confirmation is pending.
- ToolChain enforces max chain depth 5, prevents repeated-step loops, and records chain trace/history.
- ExecutionPipeline executes plan steps sequentially and records `StepResult` and `ExecutionResult` details.
- ExecutionPipeline pauses before destructive actions and returns a `ConfirmationRequest`.
- ExecutionPipeline can execute internal `planner_context` response steps for safe context-only answers.
- ExecutionPipeline collects all step outputs into one response and labels mixed success/failure as `Partial results:`.
- ExecutionPipeline emits execution events and standard logs for start, step completion, recoverable failure, unrecoverable failure, rollback, and completion.
- ToolAdapter defines external-tool contracts with adapter metadata, requests, responses, and registry lookup.
- ToolAdapterRegistry can enforce `ExternalAdapterConfig` for enabled state, mock/local/real mode, env-key names, base URLs, and timeouts.
- DeviceAction defines safe local action metadata and stable execution result formatting.
- DeviceActionRegistry registers named local actions, blocks unapproved confirmation-required actions, rejects forbidden placeholders, and returns safe failures for unknown actions.
- LocalDeviceActionAdapter exposes `echo`, `system_status_mock`, `list_actions`, `list_apps`, confirmation-gated `lock_pc`/`sleep_pc`, and confirmation-gated Windows `open_app`; it loads app definitions through `AppAllowlistLoader` from `config/apps.json`, does not run arbitrary shell commands, and does not accept user-provided paths.
- DeviceActionSkill routes safe device commands through IntentParser, ToolSelector, Planner, ExecutionPipeline, SkillManager, and the text REPL.
- DeviceActionSkill supports `echo <text>`, `list device actions`, `list apps`, `system status`, and confirmation-gated `lock_pc`/`sleep_pc`/`open_app`.
- DeviceActionSkill returns stable confirmation-required responses for shutdown, restart, and unapproved `lock_pc`/`sleep_pc`/`open_app` requests.
- Confirmed `lock_pc` and `sleep_pc` call narrow Windows implementations only after `yes` or `confirm`; non-Windows platforms return safe unsupported responses. Confirmed `open_app` calls only the narrow Windows launcher for enabled allowlisted apps, and tests mock that launcher.
- DeviceActionSkill returns stable forbidden responses for run command, delete, and arbitrary shell placeholders.
- DeviceActionSkill never executes unapproved confirmation-required actions or forbidden actions directly.
- DeviceAction app launcher tests verify config loading, invalid config rejection, duplicate app id rejection, app listing, unknown app rejection, disabled app rejection, confirmation gating, confirmed Windows launch through a mocked launcher, arbitrary path rejection, shell-like input rejection, non-Windows unsupported handling, and no arbitrary command execution.
- SecretsGuard rejects raw-looking secrets and validates that adapter config files reference environment variable names instead of storing keys.
- Real adapter mode fails closed when an env key is missing, when the env-key name is only a placeholder, or when real execution is not implemented for an adapter.
- `config/adapters.example.json` contains fake placeholder config only; local/private adapter config files are ignored by git.
- RealWeatherAdapter is available as an explicit `real_weather` adapter, but the default WeatherSkill path still uses `mock_weather`.
- RealWeatherAdapter marks `requires_network` and `requires_auth` true, reads API keys only from the configured environment variable name, applies configured timeouts, performs HTTP only after real-mode/env gating, and normalizes supported weather responses into ARES weather data.
- RealMarketAdapter is available as an explicit `real_market` adapter, but the default MarketSkill path still uses `mock_market`.
- RealMarketAdapter marks `requires_network` and `requires_auth` true, reads API keys only from the configured environment variable name, applies configured timeouts, performs HTTP only after real-mode/env gating, and normalizes supported market quote responses into ARES market data.
- MockWeatherAdapter, MockMarketAdapter, and MockCalendarAdapter are offline-only adapters that do not require network, auth, API keys, GPT, or voice.
- Planner can hold a ToolAdapterRegistry for adapter-aware planning, and ExecutionPipeline can safely execute weather skill steps or explicit `tool_adapter` plan steps through an injected registry.
- Live REPL integration tests verify multi-step plan creation, notes plus calculator execution, task plus memory execution, goal command routing, weather routing through `MockWeatherAdapter`, recoverable partial failure reporting, last execution display, and the active `SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill` path.
- Weather live-path tests verify `IntentParser -> Planner -> ExecutionPipeline -> WeatherSkill -> MockWeatherAdapter` through the text REPL.
- Market tests verify parser, selector, planner, execution pipeline, REPL routing, missing adapter handling, and `MockMarketAdapter` responses.
- Calendar tests verify parser, selector, planner, execution pipeline, REPL routing, missing adapter handling, and `MockCalendarAdapter` responses.
- Multi-step planner tests verify single-step compatibility, weather plus reminder planning, goals plus calendar planning, three-step ordering, execution ordering, partial failure recovery, and REPL execution.
- Context-aware planner tests verify goal context, profile favorite context, note topic context, related task context, missing-context responses, multi-step context plans, partial failure recovery, and REPL integration.
- Confirmation tests verify delete note/task/goal pauses, confirm executes, cancel does not execute, missing pending confirmation fails safely, weather/market/calendar remain unaffected, future external write actions require confirmation, and multi-step plans pause safely.
- Adapter config tests verify mock mode, real-mode fail-closed behavior without env keys, placeholder handling, raw-secret rejection, example config loading, read-only mock adapter behavior, and confirmation-layer compatibility.
- RealWeatherAdapter tests verify default weather remains mock, missing env keys fail safely, mocked HTTP succeeds, HTTP timeouts fail safely, bad API responses fail safely, normalized output stays stable, raw key values are not exposed, explicit real-weather failures are handled safely, and SecretsGuard still accepts placeholder config.
- RealMarketAdapter tests verify default market remains mock, missing env keys fail safely, mocked HTTP succeeds, HTTP timeouts fail safely, bad API responses fail safely, HTTP status errors fail safely, normalized output stays stable, raw key values are not exposed, explicit real-market failures are handled safely, and SecretsGuard still accepts placeholder config.
- ToolChain tests verify repeated weather steps are rejected before execution to prevent loop-style chains.
- Goals live-path integration tests verify REPL add/list/milestone/pause/complete commands, persistence after reload, Planner goal steps, ExecutionPipeline goal execution, and ToolChain goal chains.
- SkillContext metadata carries the parsed intent and extracted entities for skills that need them.
- IntentParser tests cover ambiguous local phrases such as `remember to buy milk`, note reminders, birthday recall, goal actions, task actions, note actions, calculator requests, and unknown text.
- REPL integration tests confirm live text input reaches IntentParser before SkillManager selects local skills.
- Unknown structured intents do not use loose token-overlap fallback, preventing generic text from being misrouted to memory recall.
- SkillManager uses ToolSelector confidence scoring instead of first-match-only selection.
- SkillManager records handled skill turns into the in-memory conversation context.
- Conversation history, user profile facts, goals, notes, and tasks are stored separately.
- `show plan` and `show steps` in the text REPL display the last execution plan.
- `show execution` and `show last execution` in the text REPL display the last execution result.
- `show chain` and `show chain history` in the text REPL display the latest local tool chain trace.
- ReminderScheduler reads existing task due text and can identify due or upcoming tasks without changing `data/tasks.json`.
- Supported due phrases include `today`, `tomorrow`, `next week`, `in 10 minutes`, `in 2 hours`, and `at 18:00`.
- ConversationContextManager keeps only the last 20 skill turns in RAM and does not write to disk.
- GitHub Actions CI now enforces the local verification suite on `main`.
- Voice City has a safe service skeleton only; real microphone, speaker, STT, TTS, wake word, GPT, internet, and background listening have not started.

Engineering Rules

- Strict engineering rules are documented in `docs/ENGINEERING_RULES.md`.
- No failing tests may be skipped, hidden, or weakened to pass.
- Every meaningful change must keep `py -m pytest`, `py -m compileall core interfaces events memory skills scripts`, and `py scripts\verify_phase2_events_memory.py` passing.
- GitHub Actions CI must stay green for pushes and pull requests to `main`.
- `main` should be protected and merged through the feature branch -> tests -> PR -> CI -> merge workflow.
- README and session handoff documentation must be updated after every meaningful change.

Project Documents

- Permanent architecture reference: `docs/ARCHITECTURE.md`
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
- Long-term goal management
- External tool adapter interface
- Weather skill
- Stock/market skill
- Calendar skill
- External adapter config and secrets guard
- Real weather adapter skeleton
- Real weather adapter HTTP logic
- Real market adapter skeleton
- Real market adapter HTTP logic
- Device action framework skeleton
- DeviceActionSkill safe live path
- Device dangerous-action confirmation gate
- Confirmed Windows lock device action
- Confirmed Windows sleep device action
- Device app launcher skeleton
- Confirmed Windows app launcher
- App launcher allowlist config
- Calculator app allowlist enablement
- Manual calculator launch verification script
- PCService abstraction for Windows operations
- PCService structured status provider
- PCService capability discovery
- CoreService orchestration layer
- Phase 2 architecture stabilization
- Voice City foundation
- Voice City input/output contracts
- Voice City text loop foundation

Next:

1. Voice wake word/STT/TTS planning
2. GPT fallback integration
3. Raspberry Pi deployment
4. Robot body / sensors

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

Voice City now has a safe service skeleton only. Real microphone, speaker, STT, TTS, wake word, GPT, internet, and background listening have not started.

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
- Recognized intents include `calculate`, `goal`, `note`, `task`, `memory_recall`, `weather`, `market`, `calendar`, `time_date`, and `unknown`.
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
- `core.MultiStepPlan` marks plans with more than one executable step.
- `core.Planner` converts an `Intent` into a local execution plan without executing skills.
- Supported planner targets are goals, notes, tasks, calculator, weather, market, calendar, and conversation memory.
- Planner can split compatible user requests such as `What's the weather tomorrow and remind me to go to the gym`.
- Planner can split compatible user requests such as `Show my goals and today's calendar`.
- ToolSelector attaches plans before SkillManager executes anything.
- The text REPL supports `show plan` and `show steps`.
- No new skills, GPT, voice, notifications, calendar integration, or external APIs were added.

Phase 11 Execution Pipeline Foundation

- `core.ExecutionPipeline` executes planner steps sequentially.
- `core.StepResult` records start time, end time, duration, success/failure, returned data, and error messages.
- `core.ExecutionResult` records full plan execution status and rollback metadata.
- `core.RollbackHook` provides a no-op rollback extension point for future reversible local actions.
- Multi-step responses aggregate every step output into one final response.
- Mixed successful and failed recoverable steps are reported as partial results while remaining steps continue.
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

Phase 13 Long-Term Goal Management Foundation

- `memory.GoalsStore` persists long-term goals in `data/goals.json`.
- Goals are separate from conversation memory, user profile memory, notes, and tasks.
- `skills.builtin.GoalsSkill` supports add, list, show, complete, pause, delete, and add-milestone commands.
- Each goal contains id, title, description, created timestamp, status, priority, and milestones.
- `IntentParser`, `ToolSelector`, `Planner`, `ToolChain`, `ExecutionPipeline`, `SkillManager`, and the text REPL all route the local `goal` intent.
- Live-path tests verify goal commands through the REPL plus goal-related ToolChain execution.
- No GPT, autonomous background actions, notifications, external APIs, voice, weather, stocks, or calendar integration were added.

Phase 14 External Tool Adapter Foundation

- `core.ToolAdapter` defines `ToolAdapter`, `ToolRequest`, `ToolResponse`, and `ToolAdapterRegistry`.
- Adapter metadata includes name, description, capabilities, `requires_network`, and `requires_auth`.
- `MockWeatherAdapter` and `MockMarketAdapter` provide offline mock responses only.
- Planner accepts an optional adapter registry for future adapter-aware planning.
- ExecutionPipeline can execute explicit `tool_adapter` plan steps through an injected registry.
- No real APIs, API keys, GPT, voice, stock skill, calendar integration, or web access were added.

Phase 15

- `skills.builtin.WeatherSkill` answers local weather requests through `ToolAdapterRegistry`.
- WeatherSkill uses `MockWeatherAdapter` only.
- Supported phrases include `weather`, `weather today`, `weather tomorrow`, and `weather in Madrid`.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `weather` intent.
- Tests cover weather parsing, mock adapter calls, skill responses, planner steps, execution pipeline steps, REPL routing, and missing adapter errors.
- Hardened live-path tests also verify ToolChain loop prevention for repeated weather steps and the full REPL path into `MockWeatherAdapter`.
- The default WeatherSkill path still does not call a real weather API, use API keys, require internet access, GPT, voice, calendar, stocks, or notifications.

Phase 16

- `skills.builtin.MarketSkill` answers local stock/market quote requests through `ToolAdapterRegistry`.
- MarketSkill uses `MockMarketAdapter` only.
- Supported phrases include `stock nvidia`, `nvidia stock`, `apple stock`, and `market price for tesla`.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `market` intent.
- Tests cover market parsing, mock adapter calls, skill responses, planner steps, execution pipeline steps, REPL routing, and missing adapter errors.
- No real market API, API keys, internet access, GPT, voice, calendar, notifications, or real stock provider integration were added.

Phase 17

- `skills.builtin.CalendarSkill` answers local calendar/schedule requests through `ToolAdapterRegistry`.
- CalendarSkill uses `MockCalendarAdapter` only.
- Supported phrases include `what is on my calendar today`, `calendar tomorrow`, `schedule today`, and `do I have anything tomorrow`.
- `IntentParser`, `ToolSelector`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the local `calendar` intent.
- Tests cover calendar parsing, mock adapter calls, skill responses, planner steps, execution pipeline steps, REPL routing, and missing adapter errors.
- No Google Calendar integration, real calendar API, API keys, internet access, GPT, voice, notifications, or background automation were added.

Phase 18

- Multi-step planner hardening added explicit `core.MultiStepPlan` support.
- Planner still returns normal `Plan` objects for single-step requests.
- Planner returns ordered `MultiStepPlan` objects for compatible multi-step requests.
- ExecutionPipeline continues recoverable failures and aggregates all step outputs.
- Partial multi-step success is labeled with `Partial results:`.
- Tests cover single-step compatibility, two-step plans, three-step plans, planner ordering, execution ordering, partial failure recovery, and REPL integration.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Phase 19

- Context-aware Planner can read `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` through existing safe interfaces.
- Planner does not read data files directly and does not write to memory, goals, notes, or tasks.
- Supported context examples include `remind me about my main goal tomorrow`, `what should I do next for my goals`, and `show my goals and notes about gym`.
- Missing context returns deterministic local empty-context responses through internal `planner_context` steps.
- Tests cover goal, profile, notes, task, missing context, multi-step context, partial failure recovery, and REPL integration.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Phase 20

- Action Confirmation Layer adds `ConfirmationRequest`, `ConfirmationDecision`, and an in-memory pending confirmation id.
- ExecutionPipeline pauses before destructive or important actions and returns a confirmation request.
- Confirmed actions execute only after `yes` or `confirm`.
- Cancelled actions stop safely after `no` or `cancel`.
- Covered actions include note deletion, delete-all notes, task deletion, clear-completed tasks, goal delete/pause/complete, and future external adapter write actions.
- Multi-step plans pause safely at confirmation steps without executing the destructive action or later steps.
- Tests cover confirmation-required actions, confirm, cancel, missing pending confirmation, unaffected read-only adapter-backed skills, future external writes, and multi-step pause behavior.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Phase 21

- External Adapter Config and Secrets Guard adds `ExternalAdapterConfig`, `SecretsGuard`, `SecretValidationError`, and safe adapter config loading.
- Adapter config fields include `enabled`, `mode`, `api_key_env_name`, `base_url`, and `timeout_seconds`.
- `ToolAdapterRegistry` enforces config before adapter execution.
- Mock/local mode preserves existing offline Weather/Market/Calendar behavior.
- Real mode fails safely without configured env keys and still does not call real APIs.
- `config/adapters.example.json` uses fake placeholders only.
- Local/private adapter config files are ignored by git.
- Tests cover mock mode, real-mode missing-env failure, placeholder acceptance, raw-secret rejection, mock adapter preservation, and confirmation-layer compatibility.
- No real API keys, real weather/stocks/calendar integrations, internet calls, GPT, voice, notifications, or background automation were added.

Phase 22

- RealWeatherAdapter skeleton added an explicit `real_weather` adapter that supports weather capabilities while keeping default runtime behavior on mock weather.
- The adapter requires real-mode config and environment-variable-based API keys.
- The default WeatherSkill flow continues to use `mock_weather`.
- Real mode without the configured env key fails safely before execution.
- Config continues to use fake placeholders only, and `config/adapters.example.json` keeps real weather disabled/mock by default.
- Tests cover default mock behavior, RealWeatherAdapter instantiation, missing-env failure, env-key-name-only config, safe WeatherSkill failure, and SecretsGuard compatibility.
- No real API keys, real weather API calls, GPT, voice, notifications, calendar writes, or background jobs were added.

Phase 23

- RealWeatherAdapter HTTP logic performs weather HTTP requests only after `mode=real` and env-key gates pass.
- API keys are read only from the configured environment variable name and are not stored in config or returned in responses.
- Configured timeout seconds are passed to the HTTP client.
- Real weather responses are normalized into ARES weather data: location, condition, temperature C, period, capability, and source.
- HTTP timeout, HTTP status errors, invalid JSON, and unrecognized weather payloads return safe deterministic errors.
- Tests mock HTTP and perform no real network calls.
- The default WeatherSkill path remains `mock_weather`.
- No real API keys, default real mode, GPT, voice, calendar writes, stocks real API, notifications, or background jobs were added.

Phase 24

- RealMarketAdapter skeleton adds an explicit `real_market` adapter that supports market quote and summary capabilities while keeping default runtime behavior on mock market.
- The adapter requires real-mode config and environment-variable-based API keys.
- The default MarketSkill flow continues to use `mock_market`.
- Real mode without the configured env key fails safely before execution.
- Config continues to use fake placeholders only, and `config/adapters.example.json` keeps real market disabled/mock by default.
- Tests cover default mock behavior, RealMarketAdapter instantiation, missing-env failure, env-key-name-only config, safe MarketSkill failure, and SecretsGuard compatibility.
- No real API keys, default real mode, GPT, voice, notifications, calendar writes, or background jobs were added.

Phase 25

- RealMarketAdapter HTTP logic performs market HTTP requests only after `mode=real` and env-key gates pass.
- API keys are read only from the configured environment variable name and are not stored in config or returned in responses.
- Configured timeout seconds are passed to the HTTP client.
- Real market responses are normalized into ARES market data: symbol, price, currency, capability, source, and optional name/change fields.
- HTTP timeout, HTTP status errors, invalid JSON, and unrecognized market payloads return safe deterministic errors.
- Tests mock HTTP and perform no real network calls.
- The default MarketSkill path remains `mock_market`.
- No real API keys, default real mode, GPT, voice, calendar writes, notifications, or background jobs were added.

Phase 26

- DeviceAction model and DeviceActionResult model define stable local device action metadata and result payloads.
- DeviceActionRegistry registers safe local actions and returns safe failures for unknown actions.
- LocalDeviceActionAdapter initially exposed only `echo`, `system_status_mock`, and `list_actions`; later phases added confirmation-gated lock/sleep and the app launcher skeleton.
- Dangerous placeholders such as shutdown/restart are rejected.
- No shutdown, restart, arbitrary shell execution, Telegram, voice, internet, GPT, remote control, notifications, or dangerous device action execution was added.
- Future dangerous actions must require explicit confirmation before execution.

Phase 27

- DeviceActionSkill exposes the safe device action framework through the live ARES skill path.
- IntentParser recognizes `device_action` requests for `echo <text>`, `list device actions`, `system status`, and unsafe device phrases.
- ToolSelector and Planner route safe device actions to `device_action` plan steps.
- ExecutionPipeline executes safe device actions through the registered DeviceActionSkill and LocalDeviceActionAdapter.
- Text REPL can execute safe device actions through the normal router flow.
- Dangerous requests including shutdown, restart, sleep, run command, open app, delete, and arbitrary shell return safe rejection or confirmation-required responses; lock requests now route to Phase 29 `lock_pc`, sleep requests now route to Phase 30 `sleep_pc`, and open-app requests now route to the Phase 32 confirmed Windows allowlist launcher.
- No real OS commands, shutdown/restart, Telegram, voice, internet, GPT, remote access, notifications, or background jobs were added.

Phase 28

- Device actions now have `safe`, `confirmation_required`, and `forbidden` classifications.
- Shutdown, restart, sleep, and open app were classified as confirmation-required placeholders; lock requests now route to Phase 29 `lock_pc`, sleep requests now route to Phase 30 `sleep_pc`, and open-app requests now route to the Phase 32 confirmed Windows allowlist launcher.
- Run command, delete, and arbitrary shell are forbidden placeholders.
- DeviceActionSkill returns stable confirmation-required or forbidden responses without executing those actions.
- Confirmation-required responses include a stable device action confirmation request token.
- Planner and REPL paths preserve the confirmation-required result safely.
- No real OS commands, shutdown/restart, Telegram, voice, GPT, internet, notifications, remote access, or background jobs were added.

Phase 29

- `lock_pc` is the first real OS-backed device action.
- `lock_pc` requires explicit confirmation through the existing confirmation layer before execution.
- The Windows lock implementation is called only after a confirmed request.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows lock implementation; no test locks the workstation.
- Shutdown, restart, arbitrary app execution, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; sleep arrived later in Phase 30 as `sleep_pc`, the mocked app launcher skeleton arrived later in Phase 31, and confirmed allowlisted Windows app launching arrived later in Phase 32.

Phase 30

- `sleep_pc` is the second real OS-backed device action.
- `sleep_pc` requires explicit confirmation through the existing confirmation layer before execution.
- The Windows sleep implementation is called only after a confirmed request.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows sleep implementation; no test puts the workstation to sleep.
- Shutdown, restart, arbitrary app execution, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; the mocked app launcher skeleton arrived later in Phase 31, and confirmed allowlisted Windows app launching arrived later in Phase 32.

Phase 31

- Device app launcher skeleton
- `AppLaunchConfig` allowlist entries with app id, display name, command placeholder, enabled flag, and confirmation flag
- `list_apps` safe mock action
- `open_app <app_id>` confirmation-gated mock action
- Unknown and disabled apps are rejected safely
- Confirmed `open_app` calls only the mocked launcher and does not launch real apps
- No arbitrary app names, arbitrary shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, notifications, or real app execution was added in Phase 31

Phase 32

- Confirmed Windows app launcher
- At this phase, default allowlist examples were disabled: `notepad`, `calculator`, and `browser`
- `open_app <app_id>` requires explicit confirmation
- Confirmed `open_app` launches only enabled allowlisted Windows apps
- User-provided paths and shell-like app ids are rejected safely
- Unknown and disabled apps fail safely before launcher execution
- Non-Windows platforms return unsupported safely
- Tests mock the Windows launcher; no tests open real apps
- No arbitrary shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, notifications, or background jobs were added

Phase 33

- App launcher allowlist config
- `config/apps.json` stores approved app definitions outside runtime code
- `AppAllowlistLoader` validates required app id, display name, command/path, enabled flag, and confirmation flag
- Invalid config, duplicate app ids, disabled apps, unknown apps, user-provided paths, and shell-like app ids fail safely
- Initial example apps were disabled by default: `notepad`, `calculator`, and `browser`
- `open_app` still requires explicit confirmation and only uses the configured allowlist command
- Tests mock the launcher and do not open real apps
- No arbitrary shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, notifications, or background jobs were added

Phase 34

- Calculator app allowlist enablement
- `calculator` is the only enabled app in `config/apps.json`
- `notepad` and `browser` remain disabled and fail safely before launch
- `open_app calculator` still requires explicit confirmation
- Confirmed calculator launch uses only the existing Windows app launcher path and the configured allowlist command
- User-supplied paths and shell-like app ids remain rejected
- Non-Windows platforms return unsupported safely
- Tests mock the launcher and do not open real apps
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added

Phase 35

- Manual calculator launch verification script
- `scripts/manual_verify_calculator_launch.py` can be run only by the owner
- The script prints a warning and shows `App id: calculator`
- It requires the exact typed confirmation `YES_OPEN_CALCULATOR`
- Only after that confirmation does it call the existing `LocalDeviceActionAdapter.execute("open_app", ...)` path
- Any other input refuses the launch
- Tests mock the adapter path and do not open Calculator
- No new apps, shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, or notifications were added

Phase 36

- PCService abstraction
- `core.PCService` defines the dedicated interface for future PC operations: `lock()`, `sleep()`, `open_app(app_id)`, and `status()`
- `core.WindowsPCService` holds the Windows-specific implementation behind that interface
- `LocalDeviceActionAdapter` delegates lock, sleep, open app, and status behavior through PCService instead of calling Windows helpers directly
- Existing injection points and behavior remain compatible for tests and runtime
- No new device actions, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, or behavior changes were added

Phase 37

- PCService status provider
- `core.PCStatus` defines the structured safe status object
- `PCService.get_status()` returns safe local fields only: operating system, hostname, current user, Python version, optional uptime, and available actions
- `PCService.status()` remains a compatibility wrapper around `get_status()`
- DeviceAction `system status` obtains status information only through PCService
- No network access, hardware telemetry, process enumeration, remote control, internet, GPT, voice, or new device actions were added

Phase 38

- PCService capability discovery
- `core.PCCapabilities` defines the structured safe capability object
- `PCService.get_capabilities()` returns supported device actions, supported applications, available status providers, available services, and explicit safeguards
- DeviceAction `list device actions` and `list apps` obtain discovery data through PCService instead of direct hardcoded lists
- No internet, network, GPT, remote execution, process enumeration, or new device actions were added

Phase 39

- CoreService orchestration layer
- `core.CoreService` owns service registration
- `CoreService` initially registers `PCService` as `pc`
- `CoreService.get_service(name)` returns registered services
- `CoreService.list_services()` returns registered service metadata
- `CoreService.get_capabilities()` aggregates capability data from registered services
- `SkillManager` and `LocalDeviceActionAdapter` use CoreService to reach PCService where practical
- No behavior changes, GPT, internet, remote execution, or hardware additions were added

Phase 40

- Phase 2 complete architecture cleanup
- `PC_SERVICE_NAME` centralizes the CoreService PC registration key
- `SkillContext` carries `core_service` for consistent Brain-to-service access
- `CoreService` has clearer service registration and capability aggregation documentation
- Tests verify default PC service interfaces and safe missing-capability reporting
- No behavior changes, new functionality, new cities, GPT, internet, remote execution, hardware additions, or new device actions were added

Phase 41

- Voice City foundation
- `core.VoiceService` defines the Voice City service interface
- `core.PlaceholderVoiceService` returns safe placeholder status and capabilities
- CoreService registers the placeholder VoiceService as `voice` by default
- Voice capabilities explicitly mark microphone, speaker, STT, TTS, wake word, background listening, GPT, and internet as disabled
- No audio hardware access, real STT, real TTS, wake word, GPT, internet, or background listening was added

Phase 42

- Voice City STT/TTS contracts
- `core.VoiceInput` defines the input interface with `listen_once()`, `get_status()`, and `get_capabilities()`
- `core.VoiceOutput` defines the output interface with `speak(text)`, `get_status()`, and `get_capabilities()`
- `core.NullVoiceInput` returns a safe placeholder result without microphone access or STT
- `core.NullVoiceOutput` accepts text as a placeholder without speaker access or TTS
- `PlaceholderVoiceService` owns the input/output components and includes their status and capabilities
- No microphone, speaker, Whisper, Vosk, Piper, real STT, real TTS, wake word, GPT, internet, or background listening was added

Phase 43

- Voice City text loop foundation
- `core.VoiceLoop` calls `VoiceInput.listen_once()` once and never starts background listening
- Empty or missing input is ignored safely
- Recognized text is passed to an injected existing text/planner/execution handler
- Final response text is passed to `VoiceOutput.speak()`
- Defaults remain `NullVoiceInput` and `NullVoiceOutput`
- No microphone, speaker, wake word, real STT, real TTS, GPT, internet, new skills, or behavior changes outside Voice City were added

Phase 44

- City lifecycle and lazy capability routing
- CoreService tracks city states: `idle`, `active`, `failed`, and `disabled`
- CoreService capability registry metadata includes each city's status and registered capabilities
- `route_by_capability()` activates only the matching idle city for a routed request
- Unused cities remain idle and are not called
- Disabled cities are not routed
- Failed route handlers mark only the selected city as `failed`
- Event Bus city activation remains future-only documentation; no event-driven city wakeup runtime was added
- No real audio, GPT, internet, new APIs, or external calls were added

Phase 45

- Internal Event Bus skeleton
- `core.EventBus` provides a future city event bus separate from the existing Phase 2 `events.EventBus`
- `core.Event` stores source, type, priority, payload, and timestamp
- Supported priorities are `low`, `normal`, `high`, and `critical`
- Publish/subscribe is in-process only
- History is returned in priority order
- No background listener, camera loop, notification sender, internet, GPT, new APIs, or daemon was added

Phase 46

- CoreService Event Bus integration
- `CoreService.handle_event(event)` receives internal `core.Event` records from registered city sources
- Event decisions are stable: `ignored`, `recorded`, or `escalated`
- Low and normal priority events are recorded only
- High and critical priority events are marked escalated
- Disabled and unknown sources fail safely with an ignored decision
- This is internal routing only and does not add notifications, background listeners, real device calls, internet, GPT, or daemon behavior

Phase 47

- Local Event History Store
- `events.EventHistoryStore` persists internal event decisions/results in `data/event_history.json`
- Stored history is bounded by a safe max-record limit
- Recent events can be queried by source, type, and priority
- `data/event_history.json` is ignored by git because it can contain local internal history
- This is internal memory/logging only and does not add notifications, devices, background daemons, internet, GPT, or external calls

Phase 48

- CoreService Event History persistence
- `CoreService` accepts an optional `EventHistoryStore`
- `handle_event(event)` stores handled decisions/results when the store is configured
- Low and normal events are stored as `recorded`
- High and critical events are stored as `escalated`
- Unknown and disabled source events are stored as safe `ignored` decisions
- The `failed` decision value is available for future failed event-handling paths
- This is synchronous internal memory/logging only and does not add notifications, background daemons, real devices, internet, GPT, or external calls

Phase 49

- Event History Skill
- `skills.EventHistorySkill` reads local `EventHistoryStore` data only
- Supported phrases: `what happened recently`, `show recent events`, and `show critical events`
- `IntentParser`, `Planner`, `ExecutionPipeline`, `SkillManager`, and the text REPL route these requests through the normal live path
- Empty history returns a safe local response
- This is read-only local querying and does not add notifications, background daemons, real devices, internet, GPT, or external calls

Phase 50

- Phase 3 foundation checkpoint
- Voice City skeleton, manual text loop, lazy city routing, EventBus, EventHistoryStore, and EventHistorySkill confirmed before real audio
- No runtime code changed for this checkpoint

Phase 51

- Voice City audio adapter contracts
- `VoiceInputAdapter` and `VoiceOutputAdapter` define future audio provider boundaries
- `MockVoiceInputAdapter` and `MockVoiceOutputAdapter` provide safe local/test adapters
- `NullVoiceInput` and `NullVoiceOutput` are adapter-backed placeholders
- Manual Voice City text simulation uses the mock input adapter
- Tests cover input capture, output speak, empty input, and adapter failure
- Real Whisper, Vosk, Piper, microphone, speaker, wake word, and background listener integrations remain future work

Phase 52

- Voice City adapter-backed single-turn loop
- `VoiceSingleTurnLoop` runs one safe turn using `MockVoiceInputAdapter` and `MockVoiceOutputAdapter`
- `VoiceTextRequest` captures the text request produced from mock input
- The loop passes text to an injected existing text/CoreService handler and sends the response to mock output
- Tests cover normal input/output, empty no-op, input adapter failure, output adapter failure, and no audio hardware access
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, or internet was added

Phase 53

- Voice City multi-turn mock session
- `VoiceSessionLoop` processes multiple queued mock inputs with a `max_turns` limit
- Stop phrases are `stop`, `exit`, and `goodbye`
- Empty inputs are recorded as safe no-op turns
- Session results include structured turns, transcript, and history output
- Tests cover multi-turn flow, stop phrase handling, max-turn limiting, input failure, output failure, empty input, and no audio hardware access
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, or internet was added

Phase 54

- Voice Session Skill
- `skills.VoiceSessionSkill` starts bounded mock voice sessions from text commands
- Supported phrases: `start voice session`, `start mock voice`, and `run voice test`
- Uses mock adapters only and returns transcript summaries through the existing live skill path
- Tests cover parser routing, planner routing, ToolSelector routing, direct skill behavior, stop phrase, max-turn limiting, empty session, and SkillManager execution
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, or internet was added

Phase 55

- Voice Session event logging
- `VoiceSessionSkill` records safe local operational events to `EventHistoryStore`
- Event types: `voice_session.started`, `voice_session.stopped`, `voice_session.adapter_failure`, and `voice_session.max_turns_reached`
- `EventHistorySkill` can show these events through recent event queries
- Tests cover start, stop, adapter failure, max-turn completion, live SkillManager logging, and event-history display
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, or internet was added

Phase 56

- Voice Session status query
- Supported phrases: `what happened in voice session`, `show last voice session`, and `voice session status`
- Reads latest `voice_session.*` events from `EventHistoryStore`
- Returns started/stopped/failure/max_turns summary
- Tests cover no-session, stopped session, failed session, max-turn session, parser, planner, and SkillManager paths
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, or internet was added

Phase 57

- Voice wake word
- Speech-to-text
- Text-to-speech
- Continuous conversation

Phase 58

- Vision
- Camera understanding
- Face recognition
- Object recognition

Phase 59

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
