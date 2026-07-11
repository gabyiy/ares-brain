# ARES Philosophy

ARES is built around a central Brain. The Brain is the stable identity of the system and must remain independent from hardware, operating systems, vendor APIs, and replaceable implementation details.

The Brain owns:

- identity
- long-term memory
- personality
- goals
- reasoning
- planning
- history with the owner

The Brain should never contain hardware-specific code. Windows calls, Raspberry Pi details, camera drivers, microphones, speakers, cloud APIs, robot motors, and external service credentials belong behind service boundaries.

This lets ARES grow without changing who ARES is. Hardware, AI models, voice engines, cameras, APIs, and device adapters can be replaced while the Brain keeps its memory, goals, personality, and history.

# Capital City Architecture

ARES uses a capital city model. The Brain is the capital city. Every specialized city connects through explicit bridges instead of bypassing the Brain.

Current target flow:

```text
Brain
  |
  v
CoreService
  |
  v
Registered Services
  |
  v
Skills
  |
  v
Adapters
  |
  v
Devices
```

The Brain communicates with external or hardware-facing capabilities through CoreService. CoreService owns service registration, service lookup, and service capability discovery. Registered services hide implementation details behind structured interfaces. Skills translate user intent into local actions. Adapters isolate concrete providers and devices.

The current runtime is still text-first, but the architectural rule is stable: the Brain should communicate through CoreService, not directly with Windows, devices, APIs, or future city implementations.

# ARES Behavior Schematic

ARES behavior uses a Capital City / Cities / Districts / Villages / Houses model.

Hierarchy:

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

# Future Architecture Vision

This section describes long-term architecture vision only. It is not implemented runtime behavior unless a capability is documented elsewhere as current and verified.

Future target roles:

- Brain = identity, memory, reasoning, planning, goals, personality, owner history, and decision history.
- CoreService = intent and capability router between the Brain and registered services.
- Cities = major abilities exposed behind service boundaries.
- Adapters = hardware or API connectors owned by cities.
- Devices = physical bodies, local interfaces, cloud services, files, models, and hardware endpoints.

In the long-term model, the Brain owns identity-level state and decisions. The Brain does not know how a microphone records audio, how a camera stores frames, how a weather provider formats JSON, how Windows locks a session, or how a robot motor is controlled. Those details belong below the Brain.

CoreService is the routing and discovery layer. Its future role is to route intents and capability requests from the Brain to registered cities and services, then return structured responses. CoreService should expose capabilities without leaking hardware, operating system, provider, or secret details into the Brain.

Cities are replaceable ability modules. Voice City owns speech input and output. Vision City owns cameras and visual recognition. PC City owns local computer actions. Weather City owns weather providers. Market City owns market providers. Calendar City owns schedules. Home City owns smart-home devices. Robot Body City owns movement and sensors. Codex City owns repository maintenance workflows.

Adapters are connectors below cities. They translate city-level requests into provider-specific or hardware-specific calls, such as a speech engine, camera library, app launcher, weather API, market API, calendar provider, home-device protocol, or robot control library.

Devices are the bodies and interfaces ARES can use. A device can be a Raspberry Pi, Jetson Orin, Windows PC, phone, robot body, microphone, speaker, camera, file, local model, or future hardware module. Devices are replaceable and must communicate through services and adapters.

# One Brain, Many Bodies

The future deployment model is one ARES Brain that can coordinate many bodies while keeping one identity.

Future home-server vision:

- ARES Home Server stores the Brain, master memory, user profile, goals, relationship history, sync service, and backups.
- Raspberry Pi clients act as small local bodies for lightweight interaction.
- Jetson Orin clients act as stronger robot or vision bodies.
- Windows PC clients act as desktop and device-control bodies.
- Phone clients act as mobile interface bodies.
- Future bodies can join only through service registration, capability discovery, adapter boundaries, and owner-approved permissions.

The Home Server is the continuity point. Clients and bodies can be replaced, upgraded, disconnected, or retired without changing the Brain's identity or master memory.

Hard rule: hardware-specific code must never enter the Brain. Operating-system commands, camera drivers, microphone and speaker internals, robot motor code, model-specific parsing, provider-specific API formats, and raw secrets belong in services, adapters, or devices. The Brain communicates through structured data, CoreService capabilities, and explicit permissions.

# Current Services

## CoreService

`core.CoreService` is the orchestration layer between the Brain and registered services.

Current responsibilities:

- register local or external service boundaries
- provide `get_service(name)` lookup
- provide `list_services()` metadata
- aggregate capabilities with `get_capabilities()`
- route a request to one matching city with `route_by_capability()`
- enforce module lifecycle through `ModuleLifecycleManager`
- expose `get_lifecycle_status()`
- expose `get_lifecycle_history()`
- expose explicit `recover_service()` for failed or degraded modules
- receive internal city events with `handle_event(event)`
- track city lifecycle states: `idle`, `active`, `failed`, and `disabled`
- register PCService as the default `pc` service
- register the Voice City placeholder service as the default `voice` service
- fail safely when a registered service does not expose required capability interfaces

CoreService does not implement device behavior itself. It discovers and routes to registered services.

## PCService

`core.PCService` is the current PC service boundary. `core.WindowsPCService` is the Windows-specific implementation behind that boundary.

Current responsibilities:

- expose `get_capabilities()`
- expose `get_status()` and compatibility `status()`
- provide structured safe PC status data
- provide structured PC capability data
- keep Windows-specific implementations behind the service boundary
- support only approved and confirmation-gated device actions

## VoiceService

`core.VoiceService` is the current Voice City service boundary. `core.PlaceholderVoiceService` is the safe placeholder implementation behind that boundary.

Current responsibilities:

- expose `get_capabilities()`
- expose `get_status()` and compatibility `status()`
- own `VoiceInput` and `VoiceOutput` components
- keep concrete audio providers behind `MicrophoneAdapter`, `SpeechToTextAdapter`, `VoiceInputAdapter`, and `VoiceOutputAdapter`
- provide structured placeholder Voice City status data
- provide structured Voice City capability data
- expose input/output status and capability data
- support a one-shot `VoiceLoop` text bridge from voice input to the existing text handler path and back to voice output
- support `VoiceSingleTurnLoop` for one adapter-backed mock input/output turn
- support `VoiceSessionLoop` for bounded multi-turn mock sessions
- expose `VoiceSessionSkill` for starting bounded mock voice sessions from text commands through the normal skill path
- report that microphone, speaker, STT, TTS, wake word, background listening, GPT, and internet are disabled
- avoid all audio hardware access

Current Voice City component contracts:

- `VoiceInput.listen_once()`
- `VoiceInput.get_status()`
- `VoiceInput.get_capabilities()`
- `VoiceOutput.speak(text)`
- `VoiceOutput.get_status()`
- `VoiceOutput.get_capabilities()`

Current Voice City adapter contracts:

- `MicrophoneAdapter.start()`
- `MicrophoneAdapter.stop()`
- `MicrophoneAdapter.read_chunk(timeout_seconds, cancel_requested)`
- `MicrophoneAdapter.get_status()`
- `MicrophoneAdapter.get_capabilities()`
- `SpeechToTextAdapter.transcribe(audio_chunk)`
- `SpeechToTextAdapter.get_status()`
- `SpeechToTextAdapter.get_capabilities()`
- `VoiceInputAdapter.capture()`
- `VoiceInputAdapter.capture_input()`
- `VoiceInputAdapter.get_status()`
- `VoiceInputAdapter.get_capabilities()`
- `VoiceOutputAdapter.speak(text)`
- `VoiceOutputAdapter.get_status()`
- `VoiceOutputAdapter.get_capabilities()`

Current placeholder implementations:

- `AudioChunk` stores raw audio chunk metadata for future microphone adapters without binding to a speech engine.
- `MockMicrophoneAdapter` provides deterministic local/test microphone lifecycle, chunk reads, timeout handling, cancellation support, and safe failure paths without hardware access.
- `TranscriptionResult` stores transcription text, status, error details, and bounded confidence values.
- `MockSpeechToTextAdapter` converts `AudioChunk` objects into deterministic test transcriptions, including empty-audio, low-confidence, no-transcription, and failure results without a real speech engine.
- `NullVoiceInput` is backed by a safe placeholder input adapter and does not access a microphone or run STT.
- `NullVoiceOutput` is backed by a safe placeholder output adapter and does not access speakers or run TTS.
- `MockVoiceInputAdapter` provides deterministic local/test text capture without microphone access and accepts injected microphone and speech-to-text adapters for future provider wiring.
- `MockVoiceOutputAdapter` records deterministic local/test speech output without speaker access.
- `PlaceholderVoiceService` and `NullVoiceInput` accept an injected `MicrophoneAdapter` so Voice City can swap microphone implementations later without changing Brain, CoreService, skills, or current text loops.
- `PlaceholderVoiceService` and `NullVoiceInput` accept an injected `SpeechToTextAdapter` so Voice City can swap transcription implementations later without changing Brain, CoreService, skills, or current text loops.

Current voice loop foundation:

- `VoiceTextRequest` stores the text request converted from voice adapter input.
- `VoiceLoop.run_once()` calls `VoiceInput.listen_once()` once.
- Empty or missing input returns a safe no-input result.
- Recognized text is passed to an injected existing text/planner/execution handler.
- Final response text is passed to `VoiceOutput.speak(text)`.
- Adapter failures are reported as safe input/output errors.
- `VoiceSingleTurnLoop` wires `MockVoiceInputAdapter` and `MockVoiceOutputAdapter` through the existing `VoiceLoop` path for one safe voice-style turn.
- The single-turn flow is `MockVoiceInputAdapter.capture()` -> `VoiceTextRequest` -> injected existing text/CoreService handler -> `MockVoiceOutputAdapter.speak(response)`.
- `VoiceSessionLoop` repeats the same mock adapter-backed path up to a configured `max_turns` limit.
- Session stop phrases are `stop`, `exit`, and `goodbye`.
- Empty input is recorded as a safe no-op turn.
- Session output includes structured `VoiceSessionTurn` records plus transcript/history lists.
- `skills.VoiceSessionSkill` starts a bounded mock session from "start voice session", "start mock voice", or "run voice test".
- `VoiceSessionSkill` is wired through IntentParser, Planner, ExecutionPipeline, SkillManager, and the REPL path.
- `VoiceSessionSkill` uses only `MockVoiceInputAdapter` and `MockVoiceOutputAdapter` and returns a transcript summary.
- When `SkillContext.event_history_store` is available, `VoiceSessionSkill` records safe local operational events for session start, stop phrase, adapter failure, and max-turn completion.
- Voice session event payloads store status, turn counts, max-turn metadata, and adapter failure details only; they do not store mock transcript content.
- `EventHistorySkill` can show these Voice City operational records through recent event queries.
- `VoiceSessionSkill` can also answer "what happened in voice session", "show last voice session", and "voice session status" by reading the latest local `voice_session.*` event group.
- Voice Session status queries are read-only and return started/stopped/failure/max_turns summaries without starting a new session.
- `VoicePipeline` is the simulated end-to-end Voice City command pipeline.
- `VoicePipeline.run_once()` accepts audio through an injected `MicrophoneAdapter`.
- Audio is transcribed through an injected `SpeechToTextAdapter`.
- The resulting `TranscriptionResult` is passed through `VoiceCommandRouter`.
- Valid commands route through CoreService's `voice.text_loop` capability.
- Only the required city is activated; unrelated cities remain idle.
- Final response text is sent through an injected `VoiceOutputAdapter`.
- Session ids and correlation ids are preserved in stage data and structured events.
- The pipeline records local events for audio captured, transcription accepted/rejected, command routed/rejected, city activated, execution completed/failed, and output produced.
- The pipeline fails safely at microphone, STT, routing, command execution, and output stages without corrupting the voice session state.
- The loop does not own routing, planning, or skill execution logic.
- The loop does not start background listening, wake word detection, microphone access, speaker access, GPT, or internet access.

VoiceService is a skeleton only. Real Whisper, Vosk, Piper, microphone, speaker, wake word, and background listener integrations come later. The current microphone adapter abstraction, speech-to-text adapter abstraction, input/output adapter, single-turn, multi-turn session, VoiceSessionSkill, Voice Session event logging, Voice Session status query, VoiceCommandRouter, and VoicePipeline layers are mock/local only and do not start microphone access, speaker output, real speech-to-text, text-to-speech, wake word detection, GPT, internet, or background listening.

# Architecture Hardening Checkpoint

This checkpoint comes after the simulated Phase 3 Voice City command pipeline and before real hardware/adapters.

Implemented:

- enforced module lifecycle
- versioned interface contracts

Remaining hardening items:

1. capability manifests
2. memory/database migrations
3. health checks and adapter fallback
4. measured resource budgets

Permanent rule: Every ARES ability must be independently installable, replaceable, disableable, health-checkable, version-compatible, and testable without modifying the Brain.

Permanent contract rule: No City, Skill, adapter, device, or service may exchange an unversioned public request or response across an ARES architectural boundary.

# Versioned Interface Contracts

ARES public boundaries now use explicit V1 contracts through `core.Contracts`.

Every public request and response contract exposes:

- `contract_name`
- `contract_version`
- `correlation_id`
- optional `session_id`
- `created_at`
- `metadata`

Runtime compatibility uses integer major versions such as `v1`, `v2`, and `v3`. The current runtime supports V1 contracts only. Unsupported major versions are rejected before execution; ARES does not silently reinterpret unknown versions.

Current V1 contracts:

- `MicrophoneCaptureRequestV1`
- `MicrophoneCaptureResultV1`
- `SpeechToTextRequestV1`
- `SpeechToTextResultV1`
- `VoiceCommandRequestV1`
- `VoiceCommandResultV1`
- `CoreExecutionRequestV1`
- `CoreExecutionResultV1`
- `LifecycleExecutionRequestV1`
- `LifecycleExecutionResultV1`
- `VoicePipelineRequestV1`
- `VoicePipelineResultV1`
- `EventPublicationEnvelopeV1`

`ContractRegistry` is the central compatibility registry. It can list known contracts, report supported versions, report the current version, identify consumers, and validate whether a requested contract is compatible. Duplicate incompatible registrations are rejected.

Compatibility validation is integrated into:

- VoicePipeline
- VoiceCommandRouter
- CoreService
- ModuleLifecycleManager
- microphone adapter boundary
- speech-to-text adapter boundary
- event publication envelope

Safe rejection behavior:

- unsupported or malformed contracts fail before module execution
- CoreService rejects incompatible core execution contracts before city lookup or activation
- lifecycle rejects incompatible requests before state transition
- unrelated cities remain idle
- lifecycle state is not corrupted
- Voice sessions remain reusable after rejection
- correlation ids are preserved where available
- contract rejection can be recorded in EventHistoryStore when CoreService has one configured

Future V2 contracts must be added as new registered versions, with explicit conversion or dual-version support where needed. V2 must not replace or reinterpret V1 silently, and Brain code must not depend on adapter-specific contract details.

PCService and VoiceService are the current services registered by default. Future services should follow the same registration and capability pattern.

# Device Action Pipeline

Device actions must pass through the service boundary. The Brain must never call Windows directly.

Current device action path:

```text
Brain
  |
  v
Skill
  |
  v
DeviceAction
  |
  v
PCService
  |
  v
Windows
```

The live text path routes through `IntentParser`, `Planner`, `ExecutionPipeline`, `SkillManager`, `DeviceActionSkill`, `LocalDeviceActionAdapter`, CoreService, and PCService.

The current device action layer supports safe actions, confirmation-required actions, and forbidden placeholders. Dangerous actions must not execute unless they are explicitly implemented, allowlisted, and confirmed by the owner.

# Capability Discovery

Every service that participates in CoreService discovery exposes:

- `get_capabilities()`

Services that have meaningful status information should also expose:

- `get_status()`
- `status()` as a compatibility wrapper where applicable

The Brain discovers services dynamically through CoreService instead of assuming hardcoded implementation details. Capability discovery returns structured data, including available actions, services, status providers, applications, safeguards, and safe error details.

Discovery over assumptions is a core design rule. If a service is missing or incomplete, ARES should report that safely instead of guessing.

# Enforced Module Lifecycle

ARES uses `core.ModuleLifecycleManager` to give every CoreService-managed module an explicit, testable lifecycle before real hardware/adapters are added.

Required lifecycle states:

- `UNLOADED`
- `STARTING`
- `READY`
- `BUSY`
- `DEGRADED`
- `STOPPING`
- `STOPPED`
- `FAILED`

Required lifecycle operations:

- `start()`
- `health_check()`
- `execute(request)`
- `stop()`

CoreService owns the lifecycle manager and registers each service as a managed module. `route_by_capability()` now performs this sequence for the selected city only:

1. start the module, idempotently if already `READY`
2. run health check
3. execute only if the module is `READY`
4. return the module to `READY`/idle after successful execution
5. mark only that module `FAILED` when startup or execution fails

Health-check failure moves the module to `DEGRADED` or `FAILED` according to policy. Failed modules are not retried automatically; they require explicit `CoreService.recover_service(name)`.

Lifecycle transition records include timestamps, operation names, session ids, correlation ids, and structured reasons for `DEGRADED` and `FAILED` states. `CoreService.get_lifecycle_status()` and `CoreService.get_lifecycle_history()` expose the query interface.

CoreService still preserves compatibility city states:

- `idle`
- `active`
- `failed`
- `disabled`

Those compatibility states are used for existing lazy city routing and user-facing service metadata. The enforced module lifecycle is the stricter internal gate.

This preserves the rule: only the needed city activates; everything else stays inactive unless explicitly triggered.

Capability aggregation through `get_capabilities()` is still an inventory operation. It can ask registered services for their advertised capabilities. Lazy request execution should use route-by-capability behavior when the caller needs one city to handle one request.

# Event Bus City Activation

Event-driven city activation is future-only documentation right now. The Event Bus may later publish events such as voice input received, vision frame available, device status changed, or scheduled task due. Cities may later subscribe to those events and activate themselves through explicit capability routes.

`core.EventBus` is the internal skeleton for this future city-event path. It defines an `Event` record with:

- source
- type
- priority
- payload
- timestamp

Supported priority levels are:

- `low`
- `normal`
- `high`
- `critical`

The internal bus supports in-process publish/subscribe and priority-ordered history. It is not a daemon, scheduler, notification runner, camera listener, microphone listener, GPT loop, or internet client.

CoreService integrates with this skeleton through `CoreService.handle_event(event)`. The method accepts internal `core.Event` objects from registered city sources and returns a stable event decision:

- `recorded`
- `ignored`
- `escalated`

Low and normal priority events are recorded only. High and critical priority events are marked escalated. Disabled or unknown event sources fail safely with an ignored decision. This is internal routing metadata only. An escalated decision does not send a notification, trigger a device action, wake a city, start a daemon, call the internet, or invoke GPT.

`events.EventHistoryStore` persists internal event decisions/results to local JSON history at `data/event_history.json` by default. It stores normalized source, type, priority, decision, event data, and result data, then keeps only the configured maximum number of records. Callers can query recent history by source, type, and priority.

CoreService accepts an optional `EventHistoryStore`. When configured, `CoreService.handle_event(event)` synchronously saves each handled event decision/result to that store. Low and normal events are stored as `recorded`, high and critical events are stored as `escalated`, and unknown or disabled source events are stored as safe `ignored` records. The `failed` decision value is reserved for future failed event-handling paths.

This is internal memory/logging only; it is not a notification system, device action runner, background listener, daemon, GPT loop, or internet client.

`skills.EventHistorySkill` is the read-only skill interface for this local history. It supports recent-event and critical-event queries through the normal live path: `IntentParser` -> `Planner` -> `ExecutionPipeline` -> `SkillManager` -> `EventHistorySkill`. The skill reads `EventHistoryStore` and formats local history for the user. It cannot mutate history, send notifications, trigger device actions, start listeners, call GPT, or access the internet.

Current boundary:

- no background city wakeup runtime
- no event-driven audio listener
- no real camera listener
- no notification sender
- no automatic event-history background writer
- no GPT or internet activation
- no new external APIs
- no notification scheduling

# Future Cities

## Voice City

Voice City has started with a safe service skeleton, a one-shot text loop, adapter contracts for future audio providers, a VoiceCommandRouter, and a simulated end-to-end VoicePipeline. The current placeholder service exposes status and capability discovery, and the current adapters are mock/local only. Future Voice City work will own wake word detection, real speech-to-text, real text-to-speech, microphones, speakers, and voice session state through adapters such as Whisper, Vosk, or Piper. The Brain should receive structured user text and return structured responses; it should not contain microphone, speaker, speech-engine, or audio driver code.

## Vision City

Vision City will own cameras, image capture, object detection, face recognition, scene understanding, and future visual memory hooks. The Brain should receive structured observations, not raw camera-driver logic.

## Weather City

Weather City will own weather providers, adapter selection, network mode, caching, secrets handling, and weather response normalization. The Brain should ask for weather capability through services and skills, not through hardcoded API calls.

## Stocks City

Stocks City will own market data adapters, symbol lookup, quote normalization, safe API configuration, caching, and future portfolio-related read paths. The Brain should not store provider-specific market API details.

## Codex City

Codex City is a future maintenance city. Its planned role is to check the ARES GitHub repository, pull latest code, run tests, check compilation, check documentation freshness, report problems, and suggest fixes. Codex City must never auto-edit, auto-commit, or self-modify ARES without owner approval.

## Home City

Home City will own smart home integrations, local home devices, permissions, and future home automation policies. It must use explicit allowlists and confirmation gates for important or destructive actions.

## Robot City

Robot City will own robot body capabilities: sensors, motors, navigation, battery state, motion planning, and safety boundaries. The Brain should reason about goals and commands, while Robot City handles physical implementation safely.

# Upgrade Philosophy

Any service can be replaced without changing the Brain.

Examples:

- Replace Raspberry Pi with Jetson.
- Replace Whisper with another speech engine.
- Replace one AI model with another.
- Replace one camera with another.
- Replace a weather provider.
- Replace a market data provider.
- Replace a Windows implementation with a Linux implementation.

The Brain remains unchanged because identity, memory, personality, goals, reasoning, planning, and owner history live above the service boundary.

# Design Rules

- Brain never calls Windows directly.
- Brain communicates through CoreService.
- Hardware-specific code must never enter the Brain.
- Only the needed city should activate for a routed request.
- Unused cities must stay idle unless explicitly triggered.
- Services hide implementation details.
- Communication uses structured data.
- Public architectural boundaries use versioned contracts.
- No City, Skill, adapter, device, or service may exchange an unversioned public request or response across an ARES architectural boundary.
- No hardcoded dependencies in the Brain.
- Discovery over assumptions.
- Small independent modules.
- Capability interfaces are explicit.
- Every ARES ability must be independently installable, replaceable, disableable, health-checkable, version-compatible, and testable without modifying the Brain.
- Dangerous actions require confirmation.
- Secrets are never stored in committed config.
- Real API integrations stay gated by config and environment variables.
- Tests must pass before merge.

# Long-Term Vision

ARES is intended to become an extensible personal AI operating system. It starts as a Raspberry Pi assistant, but the architecture should allow it to grow into a larger system, then into a robot body, and eventually into a humanoid robot without losing its identity.

The Brain is the continuity layer. Cities can be added, replaced, upgraded, or retired. The Brain keeps the owner relationship, memory, goals, history, personality, reasoning, and planning stable while the body and tools evolve around it.
