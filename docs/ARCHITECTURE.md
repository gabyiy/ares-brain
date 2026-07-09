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
- provide structured placeholder Voice City status data
- provide structured Voice City capability data
- expose input/output status and capability data
- support a one-shot `VoiceLoop` text bridge from voice input to the existing text handler path and back to voice output
- report that microphone, speaker, STT, TTS, wake word, background listening, GPT, and internet are disabled
- avoid all audio hardware access

Current Voice City component contracts:

- `VoiceInput.listen_once()`
- `VoiceInput.get_status()`
- `VoiceInput.get_capabilities()`
- `VoiceOutput.speak(text)`
- `VoiceOutput.get_status()`
- `VoiceOutput.get_capabilities()`

Current placeholder implementations:

- `NullVoiceInput` returns a safe placeholder result and does not access a microphone or run STT.
- `NullVoiceOutput` accepts text as a safe placeholder and does not access speakers or run TTS.

Current voice loop foundation:

- `VoiceLoop.run_once()` calls `VoiceInput.listen_once()` once.
- Empty or missing input returns a safe no-input result.
- Recognized text is passed to an injected existing text/planner/execution handler.
- Final response text is passed to `VoiceOutput.speak(text)`.
- The loop does not own routing, planning, or skill execution logic.
- The loop does not start background listening, wake word detection, microphone access, speaker access, GPT, or internet access.

VoiceService is a skeleton only. It does not start microphone access, speaker output, speech-to-text, text-to-speech, wake word detection, GPT, internet, or background listening.

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

# City Lifecycle and Lazy Routing

ARES uses city lifecycle metadata to keep services quiet until they are needed.

Current lifecycle states:

- `idle`: the city is registered and available, but it is not currently handling a request.
- `active`: the city is handling the current routed request.
- `failed`: the city failed while handling a routed request and should not be assumed healthy.
- `disabled`: the city is registered but unavailable for routing.

CoreService stores capability registry metadata for each city:

- service name
- service type
- city lifecycle state
- registered capabilities

`CoreService.route_by_capability(capability, handler)` uses that registry to find the first matching `idle` city and calls only that city. Non-matching cities are not probed, called, or activated. Disabled cities are skipped. If the selected handler fails, only that city is marked `failed`.

This preserves the rule: only the needed city activates; everything else stays idle unless explicitly triggered.

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

Voice City has started with a safe service skeleton. The current placeholder service exposes status and capability discovery only. Future Voice City work will own wake word detection, speech-to-text, text-to-speech, microphones, speakers, and voice session state. The Brain should receive structured user text and return structured responses; it should not contain microphone or audio driver code.

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
- No hardcoded dependencies in the Brain.
- Discovery over assumptions.
- Small independent modules.
- Capability interfaces are explicit.
- Dangerous actions require confirmation.
- Secrets are never stored in committed config.
- Real API integrations stay gated by config and environment variables.
- Tests must pass before merge.

# Long-Term Vision

ARES is intended to become an extensible personal AI operating system. It starts as a Raspberry Pi assistant, but the architecture should allow it to grow into a larger system, then into a robot body, and eventually into a humanoid robot without losing its identity.

The Brain is the continuity layer. Cities can be added, replaced, upgraded, or retired. The Brain keeps the owner relationship, memory, goals, history, personality, reasoning, and planning stable while the body and tools evolve around it.
