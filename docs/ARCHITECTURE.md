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

# Current Services

## CoreService

`core.CoreService` is the orchestration layer between the Brain and registered services.

Current responsibilities:

- register local or external service boundaries
- provide `get_service(name)` lookup
- provide `list_services()` metadata
- aggregate capabilities with `get_capabilities()`
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
