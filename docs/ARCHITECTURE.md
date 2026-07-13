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
- expose lazy health visibility with `get_service_health()`, `list_service_health()`, and `get_capability_health()`
- route a request to one matching city with `route_by_capability()`
- validate capability manifests before module activation
- expose `get_manifest(name)` and `list_manifests()`
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
- `LinuxAlsaMicrophoneAdapter` is the first real Linux/Raspberry Pi microphone provider. It stays behind the `MicrophoneAdapter` boundary, uses `arecord` through a safe argument-list subprocess wrapper, can list ALSA capture devices, health-check `arecord` and selected devices, record bounded WAV files, validate the WAV output, and return structured `MicrophoneResult` data.
- `TranscriptionResult` stores transcription text, status, error details, and bounded confidence values.
- `MockSpeechToTextAdapter` converts `AudioChunk` objects into deterministic test transcriptions, including empty-audio, low-confidence, no-transcription, and failure results without a real speech engine.
- `LinuxWhisperSpeechToTextAdapter` is the first real offline STT provider. It stays behind the `SpeechToTextAdapter` boundary, accepts WAV files from `LinuxAlsaMicrophoneAdapter` or `AudioChunk` input, runs a local Whisper/whisper.cpp executable with `shell=False`, requires a local model file, and returns structured transcription text, timing, language metadata, and safe failure statuses.
- `TextToSpeechAdapter` is the speech-output engine boundary. `LinuxPiperTextToSpeechAdapter` is the first real offline TTS provider. It stays behind the TTS adapter boundary, accepts versioned TTS requests, resolves validated profile identifiers through `VoiceProfileRegistry`, runs a local Piper executable with `shell=False`, writes a WAV file, and returns structured profile/generation/playback status.
- `SpeakerOutputAdapter` is the playback-device boundary. `LinuxAlsaSpeakerAdapter` owns explicit `aplay` WAV playback, validates WAV output, supports optional ALSA device selection, and never enables microphone monitoring or automatic playback.
- `NullVoiceInput` is backed by a safe placeholder input adapter and does not access a microphone or run STT.
- `NullVoiceOutput` is backed by a safe placeholder output adapter and does not access speakers or run TTS.
- `MockVoiceInputAdapter` provides deterministic local/test text capture without microphone access and accepts injected microphone and speech-to-text adapters for future provider wiring.
- `MockVoiceOutputAdapter` records deterministic local/test speech output without speaker access.
- `PlaceholderVoiceService` and `NullVoiceInput` accept an injected `MicrophoneAdapter` so Voice City can swap microphone implementations later without changing Brain, CoreService, skills, or current text loops.
- `PlaceholderVoiceService` and `NullVoiceInput` accept an injected `SpeechToTextAdapter` so Voice City can swap transcription implementations later without changing Brain, CoreService, skills, or current text loops.
- Voice City can swap future TTS and speaker implementations without changing Brain, CoreService, skills, or current text loops.

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

VoiceService remains the boundary. The current real-audio surface is limited to explicit Linux ALSA capture through `LinuxAlsaMicrophoneAdapter`, offline Whisper transcription through `LinuxWhisperSpeechToTextAdapter`, offline Piper WAV generation through `LinuxPiperTextToSpeechAdapter`, explicit ALSA playback through `LinuxAlsaSpeakerAdapter`, the controlled single-turn pipeline, and a bounded owner-triggered multi-turn session. All real providers remain replaceable and outside autonomous startup paths. Real Vosk, wake word, background listeners, GPT, internet, and unbounded conversation integrations come later.

# Linux ALSA Microphone Adapter

`core.LinuxAlsaMicrophoneAdapter` is hardware-specific code for Linux/Raspberry Pi and must not be imported by the Brain. It implements the existing `MicrophoneAdapter` contract and can be injected into Voice City in place of `MockMicrophoneAdapter`.

Responsibilities:

- check whether `arecord` is available
- list ALSA capture devices with `arecord -l`
- run `health_check()`
- select an optional ALSA device such as `hw:1,0`
- record a bounded WAV file with `arecord`
- validate missing, empty, invalid, or malformed WAV output
- return structured `MicrophoneResult` objects

Safety boundaries:

- `SafeSubprocessRunner` calls `subprocess.run()` with argument lists and `shell=False`
- no user text becomes a shell command
- recording has bounded duration and timeout
- `linux_alsa_microphone_adapter` is disabled by default in local module config
- the adapter does not run STT, TTS, wake word detection, GPT, internet access, or background listening

Manual Raspberry Pi verification:

```bash
python scripts/manual_verify_linux_alsa_microphone.py
python scripts/manual_verify_linux_alsa_microphone.py --record --seconds 3 --output /tmp/ares_mic_test.wav
python scripts/manual_verify_linux_alsa_microphone.py --device hw:1,0 --record --seconds 3 --output /tmp/ares_mic_hw_1_0.wav
```

Phase pytest collection after this checkpoint was 646 tests.

# Offline Whisper Speech-To-Text Adapter

`core.LinuxWhisperSpeechToTextAdapter` is hardware/runtime-specific code for Linux/Raspberry Pi and must not be imported by the Brain. It implements the existing `SpeechToTextAdapter` contract and can be injected into Voice City in place of `MockSpeechToTextAdapter`.

Responsibilities:

- accept WAV files recorded by `LinuxAlsaMicrophoneAdapter`
- accept `AudioChunk` input and use its WAV path metadata where available
- verify that a local Whisper executable is installed
- verify that the configured local model file exists
- run offline transcription through a local command-line Whisper engine
- return recognized text, processing time, requested/effective/detected language metadata, status, and structured failure data

Safety boundaries:

- command execution uses argument lists with `shell=False`
- the recommended first Raspberry Pi model is `ggml-tiny.en.bin`
- English-only GGML models such as `ggml-tiny.en.bin` resolve automatic language configuration to `en`
- no model is downloaded automatically
- no internet access, GPT, wake word detection, TTS, background listening, or conversation loop is started
- `linux_whisper_speech_to_text_adapter` is disabled by default in local module config

Manual Raspberry Pi verification:

```bash
python scripts/manual_verify_linux_whisper_stt.py --model models/whisper/ggml-tiny.en.bin --whisper-command whisper-cli

python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command whisper-cli \
  --output /tmp/ares_whisper_test.wav \
  --language en

python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --device hw:1,0 \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command /path/to/whisper-cli \
  --output /tmp/ares_whisper_hw_1_0.wav \
  --language en
```

Phase pytest collection after this checkpoint was 663 tests.

# Raspberry Pi Whisper Runtime Preparation

The offline STT adapter requires an installed local Whisper runtime and a local GGML model. ARES now provides owner-run setup scripts for that Raspberry Pi preparation without changing the Brain, CoreService, Voice City contracts, or default runtime path.

Setup flow:

```bash
sudo apt update
sudo apt install -y git cmake build-essential curl
python scripts/install_whisper_cpp_raspberry_pi.py
```

`scripts/install_whisper_cpp_raspberry_pi.py`:

- clones `https://github.com/ggml-org/whisper.cpp.git` into `external/whisper.cpp` if missing
- builds `external/whisper.cpp/build/bin/whisper-cli` through CMake
- downloads the recommended `tiny.en` GGML model into `models/whisper/ggml-tiny.en.bin`
- verifies the executable and model exist before reporting PASS
- uses subprocess argument lists through the existing safe runner boundary

Runtime verification flow:

```bash
python scripts/manual_verify_linux_alsa_microphone.py --record --seconds 3 --output /tmp/ares_mic_test.wav
python scripts/verify_whisper_cpp_runtime.py --wav /tmp/ares_mic_test.wav --language en
```

`scripts/verify_whisper_cpp_runtime.py` locates `whisper-cli`, locates the model, sends an existing recorded WAV sample to `LinuxWhisperSpeechToTextAdapter`, and prints clear PASS/FAIL diagnostics with recognized text and timing.

Safety boundaries:

- setup/downloads are explicit owner-run preparation steps, not ARES runtime behavior
- no wake word detection, background listening, TTS, GPT, internet runtime path, autonomous loop, or conversation loop is started
- downloaded GGML binaries, local manual samples, and the cloned whisper.cpp checkout are ignored by git

Phase pytest collection after this checkpoint was 675 tests.

# Raspberry Pi Speech Input Verification Hardening

The real speech-input path is still manual and explicit, but it is now hardened for Raspberry Pi recordings that are audible through `aplay` yet may be reported by Whisper as `[BLANK_AUDIO]`.

Root cause fixed in ARES code:

- the previous verifier treated any non-empty Whisper transcript text as success
- Whisper's `[BLANK_AUDIO]` marker is non-empty text, so it could be reported as a successful transcription
- the verification scripts used `--language auto` even though the recommended installed model is the English-only `ggml-tiny.en.bin`; direct reliable manual commands use English mode
- the verifier did not print enough WAV signal diagnostics or exact process diagnostics to distinguish wrong-file selection, a silent recording, a below-threshold recording, or Whisper output parsing

Current behavior:

- `LinuxWhisperSpeechToTextAdapter` validates WAV headers and PCM statistics before transcription
- diagnostics include selected path, file size, duration, sample rate, channels, sample width, peak amplitude, and RMS amplitude
- silent WAV files fail before `whisper-cli` runs
- below-threshold RMS files fail when `minimum_rms` is configured
- `[BLANK_AUDIO]`, `<|nospeech|>`, `(no speech)`, `[SILENCE]`, and `[NO_SPEECH]` style output is normalized to no usable speech
- the verifier and manual STT script default to `--language en`, and the adapter records requested/effective language diagnostics
- failures include the exact `whisper-cli` argument list, exit code, stdout preview, and stderr preview where available

Controlled manual verification:

```bash
python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command external/whisper.cpp/build/bin/whisper-cli \
  --output /tmp/ares_speech_input.wav \
  --language en \
  --min-rms 50
```

Speaker playback is disabled by default. To hear the recorded file for troubleshooting, playback must be explicit:

```bash
python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --playback \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command external/whisper.cpp/build/bin/whisper-cli \
  --output /tmp/ares_speech_input.wav \
  --language en
```

Microphone monitoring control is a separate ALSA mixer helper and not part of the Brain or CoreService:

```bash
python scripts/configure_linux_alsa_monitoring.py --card 1 --apply
```

Equivalent commands:

```bash
amixer -c 1 sset Mic 0% mute
amixer -c 1 sset Capture 80% cap
amixer -c 1 sset Speaker 70% unmute
```

Design boundary:

- microphone capture remains in `LinuxAlsaMicrophoneAdapter`
- offline Whisper remains in `LinuxWhisperSpeechToTextAdapter`
- speaker output remains behind Voice City output contracts and explicit owner-run manual tools
- the Brain and CoreService do not call ALSA, `amixer`, `aplay`, or `whisper-cli` directly

# Offline Piper Text-To-Speech Adapter

`core.LinuxPiperTextToSpeechAdapter` is hardware/runtime-specific code for Linux/Raspberry Pi and must not be imported by the Brain. It implements the replaceable `TextToSpeechAdapter` boundary and consumes `TextToSpeechRequestV1`.

Adapter responsibilities:

- verify that a local Piper executable is installed and executable
- resolve a requested profile or the one configured enabled default through `VoiceProfileRegistry`
- verify the resolved ONNX voice model and JSON config exist, are readable, and stay inside approved model directories
- verify that the actual output directory is writable
- validate text length and voice selection
- generate a WAV file from explicit text using `shell=False`
- return structured `TextToSpeechResultV1` data with requested/resolved profile, display name, language, locale, gender metadata, quality, engine, model/config paths, generated WAV path, duration, processing time, playback status, and safe errors
- preserve text output fallback if generation or playback fails
- never download models or call cloud services at runtime

`core.VoiceProfiles` is the only Piper voice-resolution boundary. Its immutable `VoiceProfile` model and `VoiceProfileRegistry` validate schema identity/version, unique profile identifiers, supported engine/language/locale fields, exactly one enabled default, enabled selection, approved model paths, source metadata, and optional file size/checksum data. `config/voice_profiles.json` configures the official `en_US-hfc_male-medium` profile as the default ARES voice and retains `en_US-amy-low` as an optional profile. Brain and CoreService receive structured TTS contracts and never know Piper paths or choose ONNX files.

The default profile was selected from the [official Piper voice catalog](https://github.com/rhasspy/piper/blob/master/VOICES.md) and [official `hfc_male` model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/hfc_male/medium/MODEL_CARD). It is a single-speaker U.S. English male voice, medium quality, 22,050 Hz, with an approximately 63 MB model. This is configuration metadata, not automatic runtime installation.

`core.LinuxAlsaSpeakerAdapter` is the playback-device boundary. It validates an explicitly configured ALSA device against structured `aplay -l` output, validates generated WAV files, and plays them with `aplay` only when requested by an explicit TTS request or owner-run script. It never enables microphone monitoring, never plays during capture, and never runs shell commands.

The composite Piper health contract uses the existing V1 structured result. A healthy adapter returns `success=true` and `status=healthy`, with the nested `SpeakerPlaybackResult` also reporting `success=true` and `status=healthy`. Callers must inspect those contract fields explicitly; serialized result dictionaries do not contain a separate top-level `healthy` boolean.

Manual Raspberry Pi setup:

```bash
sudo apt update
sudo apt install -y curl tar alsa-utils
python scripts/install_piper_raspberry_pi.py
```

Default local paths:

- Piper runtime: `external/piper/`
- Voice registry: `config/voice_profiles.json`
- Default model: `models/piper/en_US-hfc_male-medium.onnx`
- Default config: `models/piper/en_US-hfc_male-medium.onnx.json`
- Optional Amy model/config: `models/piper/en_US-amy-low.onnx` and `models/piper/en_US-amy-low.onnx.json`
- Generated samples: `data/manual_tts_samples/`

List and install registered profiles:

```bash
python scripts/manual_verify_linux_tts.py --list-voices
python scripts/install_piper_raspberry_pi.py
python scripts/install_piper_raspberry_pi.py --voice en_US-hfc_male-medium
```

Manual verification without playback:

```bash
python scripts/manual_verify_linux_tts.py \
  --text "Hello Gabriel. I am Ares, and my new voice is working."
```

Manual verification with explicit USB speaker playback:

```bash
python scripts/manual_verify_linux_tts.py \
  --text "Hello Gabriel. I am Ares, and my new voice is working." \
  --voice-profile en_US-hfc_male-medium \
  --playback \
  --device plughw:CARD=Device,DEV=0
```

The verifier resolves and prints the Piper/model/config/output paths, selected speaker device, exact Piper command, exact `aplay` command when enabled, process exit codes, raw stdout/stderr on process failures, and generated WAV duration, sample rate, channels, sample width, and file size. It exits nonzero for genuine health, synthesis, WAV validation, or requested-playback failures. Playback failure does not delete the generated WAV.

Real Raspberry Pi 5 evidence for this checkpoint:

- Piper runtime installation completed successfully.
- The `en_US-amy-low` model and configuration loaded successfully.
- Piper generated a valid WAV.
- Direct ALSA playback through `plughw:CARD=Device,DEV=0` succeeded.
- The owner confirmed the generated voice was audible through the USB speaker.
- The configured `en_US-hfc_male-medium` default loaded and played successfully; the owner audibly confirmed the male voice on Raspberry Pi 5.

The previous `FAIL: TTS health check failed.` result was isolated to the manual verifier: it serialized the valid V1 result and queried a nonexistent `healthy` key. Piper generation and ALSA playback were already healthy. The verifier now checks the explicit V1 success/status fields and nested speaker health result.

This checkpoint does not add GPT, wake-word detection, background listening, automatic microphone activation, memory writes based on voice, a conversation loop, cloud fallback, or robot movement.

Voice-profile checkpoint collection: 771 tests.

# Controlled Single-Turn Voice Pipeline

`core.SingleTurnVoicePipeline` is an owner-triggered orchestration service. It runs exactly one request and stops; it does not own a wake word, loop, daemon, scheduler, or background microphone.

Runtime route:

```text
SingleTurnVoiceRequestV1
  -> LinuxAlsaMicrophoneAdapter
  -> SpeechToTextAdapter / LinuxWhisperSpeechToTextAdapter
  -> VoiceCommandRouter
  -> CoreService voice.text_loop
  -> SkillManager
  -> IntentParser
  -> Planner / ExecutionPipeline
  -> selected local Skill
  -> TextToSpeechAdapter / LinuxPiperTextToSpeechAdapter
  -> SpeakerOutputAdapter / LinuxAlsaSpeakerAdapter
  -> SingleTurnVoiceResultV1
```

The orchestration is split into focused boundaries:

- `SingleTurnVoicePipeline` owns lifecycle, resource reservation, task-slot release, health preflight, bounded event logging, cleanup, and final V1 result assembly.
- `SingleTurnVoiceStageMixin` owns the ordered capture, transcription, Brain, synthesis, and playback stages.
- `VoiceStageCoordinator` rejects capture/playback overlap and concurrent Whisper/Piper stages.
- `core.WavAudio` provides engine-neutral WAV read/write and PCM signal diagnostics.
- `scripts/manual_verify_single_turn_voice.py` only creates adapters/contracts and invokes the pipeline; it contains no ALSA, Whisper, Piper, or subprocess implementation.

The `single_turn_voice_pipeline` capability manifest declares its contracts, lifecycle operations, permissions, and a logical 160 MB heavy-module reservation with one task slot. The Raspberry Pi resource profile permits only one heavy module, so one turn cannot overlap another local heavy speech turn. Declared estimates remain policy values, not exact per-module measurements.

Execution order is enforced:

1. validate the V1 request, reserve capacity, start lifecycle, and health-check required components
2. reject pre-existing speaker playback, then capture one WAV and stop the microphone
3. validate WAV metadata/RMS and run Whisper only for usable audio
4. route recognized text through the existing local Brain/text path
5. run Piper only after Whisper has released the heavy stage
6. run ALSA playback only after capture is inactive and only when explicitly requested
7. stop adapters, release task/resource state, apply cleanup policy, and return

Silence and blank transcription stop before Brain/TTS. Brain failures produce the local fallback `I could not process that request.` without cloud services. TTS failures preserve and print the Brain response. Playback failures preserve the generated WAV. Cancellation and adapter timeouts release task slots and invoke adapter cleanup hooks. Operational events contain stage/status/timing metadata, not raw audio or transcript contents.

The single-turn checkpoint collection was 812 tests.

# Controlled Multi-Turn Voice Session

`core.MultiTurnVoiceSession` is a foreground, owner-triggered orchestrator that repeatedly invokes the existing `SingleTurnVoicePipeline`. It does not implement microphone capture, Whisper parsing, Brain routing, Piper synthesis, or ALSA playback. Those responsibilities remain in their existing adapters and single-turn boundaries.

```text
MultiTurnVoiceSessionRequestV1
  -> MultiTurnVoiceSession
  -> SingleTurnVoicePipeline.run_once()
  -> LinuxAlsaMicrophoneAdapter
  -> LinuxWhisperSpeechToTextAdapter
  -> exact normalized stop-phrase gate
  -> VoiceCommandRouter
  -> CoreService voice.text_loop
  -> SkillManager
  -> IntentParser
  -> Planner / ExecutionPipeline
  -> selected local Skill
  -> LinuxPiperTextToSpeechAdapter
  -> LinuxAlsaSpeakerAdapter
  -> per-turn summary
  -> MultiTurnVoiceSessionResultV1
```

The stop-phrase hook is injected into the single-turn stage boundary after usable transcription and before command routing. A matched exact normalized phrase bypasses Brain execution and unrelated tools. Greeting and closing phrases are local configured output and use `SingleTurnVoicePipeline.run_local_output()` so the session manager never calls Piper or ALSA directly.

The explicit session states are `created`, `starting`, `greeting`, `listening`, `transcribing`, `checking_stop_phrase`, `processing`, `synthesizing`, `speaking`, `waiting_between_turns`, `stopping`, `completed`, `failed`, and `cancelled`. State transitions are validated and timestamped. Each turn has a correlation ID derived from the parent session ID.

Safe defaults are five turns, 180 seconds total, three consecutive failures, five seconds of capture per turn, a 0.75-second inter-turn delay, retry enabled for silence and blank transcription, and playback disabled. Exact default stop phrases are `stop listening`, `stop conversation`, `end conversation`, `goodbye Ares`, `goodbye`, `that is all`, and `exit conversation`. Case, punctuation, and repeated whitespace are normalized; substring matching is not used.

The session acquires its own light lifecycle/resource reservation, while each turn uses the existing heavy single-turn reservation and task slot. The reused `VoiceStageCoordinator` enforces microphone/speaker and Whisper/Piper mutual exclusion. Ctrl+C, total timeout, turn timeout, fatal component failure, turn limit, duration limit, and consecutive-failure limit all enter structured cleanup. Standard events omit raw transcripts and audio. Successful audio follows the configured cleanup policy; useful failure diagnostics may be retained.

Current collection after the bounded multi-turn checkpoint: 872 tests.

# Voice Activity Detection and End-of-Speech Capture

`core.VoiceActivityDetection` is a hardware-neutral foreground capture component. `RmsVoiceActivityCapture` consumes injected mono signed 16-bit PCM frames, so Linux/ALSA and subprocess details remain in `LinuxAlsaMicrophoneAdapter`. Brain, CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, calculator, Whisper, and Piper do not implement VAD rules.

```text
LinuxAlsaMicrophoneAdapter
  -> foreground arecord raw PCM stream (argument list, shell=False)
  -> RmsVoiceActivityCapture
  -> calibrate bounded ambient PCM frames
  -> derive start / continue / silence thresholds
  -> wait for consecutive speech frames
  -> preserve bounded pre-roll
  -> WAITING / SPEECH / POSSIBLE_SILENCE state machine
  -> retain short pauses only when speech resumes consecutively
  -> trim terminal silence
  -> atomically write and validate one mono PCM WAV
  -> SingleTurnVoicePipeline
  -> Whisper only when capture succeeded
```

The V1 boundary consists of `VoiceActivityCaptureRequestV1` and `VoiceActivityCaptureResultV1`. Requests carry sample format, frame size, calibration policy, three threshold bounds, consecutive start/resume/end rules, hangover duration, speech wait timeout, utterance limit, pre-roll, selected device, and correlation/session IDs. Results carry ambient mean/median/p90/peak/noise-floor statistics, all derived thresholds, speech/trailing frame counts, speech start/end offsets, the final status, WAV path, duration, peak/RMS levels, stop reason, timing, and bounded transition diagnostics.

Initial Raspberry Pi policy defaults are 16 kHz, mono, 16-bit PCM, 20 ms frames, 0.75 seconds of calibration, lower bounds of start RMS 200 / continue RMS 140 / silence RMS 80, three consecutive start and resume frames, five confirming low frames, 0.9 seconds of terminal hangover, a 10-second speech wait, a 15-second maximum utterance, and 0.25 seconds of pre-roll. Median and p90 statistics prevent one transient peak from defining the noise floor. Thresholds are clamped to configured minima/maxima. After speech starts, only sub-continue frames update the noise estimate, with bounded adaptation; actual speech cannot lift the ambient baseline indefinitely.

The end-of-speech states are:

1. `CALIBRATING`: sample ambient frames without treating them as an utterance.
2. `WAITING`: require consecutive frames above the derived start threshold.
3. `SPEECH`: accept frames above the continue threshold.
4. `POSSIBLE_SILENCE`: retain an internal pause only after consecutive resume frames; otherwise complete after bounded hangover and low-frame evidence.
5. `COMPLETE`: trim terminal pending frames and atomically validate the WAV.

This fixes the prior failure mode where every frame above one static silence threshold reset trailing silence. Post-speech noise below the continue threshold no longer extends capture indefinitely, and one click cannot resume speech. `maximum_utterance_seconds`, speech wait timeout, cancellation, bounded buffers, lifecycle/resource gates, and fixed-duration capture remain hard safety limits.

`SingleTurnVoicePipeline` selects calibrated `auto_stop`, calibration-disabled manual thresholds, or the preserved `fixed_duration` path through its existing request contract. No-speech or invalid-audio results stop before Whisper, Brain, Piper, and speaker execution. The bounded multi-turn session propagates the same capture settings per turn and applies its existing recoverable no-speech policy. `VoiceStageCoordinator` continues to enforce microphone/speaker and Whisper/Piper mutual exclusion.

# Canonical Linux Audio Capture Boundary

Real Raspberry Pi testing proved that a raw ALSA hardware device can accept a requested rate without supplying that rate. In the observed case, `hw:2,0` was requested at 16 kHz but `arecord` reported and wrote 44.1 kHz. The previous headerless streaming path sized 20 ms frames from the requested 16 kHz value, reinterpreted 44.1 kHz bytes at the wrong timing, and then labeled the output WAV as 16 kHz. That corrupted VAD timing and degraded Whisper input even though direct hardware recordings were clear.

The corrected boundary is:

```text
requested ALSA device
  -> resolve raw numeric hw:C,D to plughw:C,D for streaming VAD
  -> request S16_LE / mono / 16000 from ALSA plug conversion
  -> canonical PCM frames only
  -> RmsVoiceActivityCapture
  -> atomically finalized canonical WAV
  -> reopen and validate actual header
  -> Whisper
```

Fixed-duration capture retains explicit raw-device configurability. It records to a unique raw WAV, reads that file's actual header, validates complete PCM data, downmixes supported channel layouts, converts supported PCM widths, resamples supported rates to 16 kHz, and atomically writes a separate canonical WAV. It never reinterprets source bytes at a different rate. Normal production defaults use `plughw:2,0` for the verified card 2/device 0 microphone and `plughw:CARD=Device,DEV=0` for speaker output.

The canonical contract is 16 kHz, mono, signed 16-bit little-endian PCM in a valid RIFF/WAV envelope. `core.WavAudio` owns normalization and header validation. `LinuxAlsaMicrophoneAdapter` owns ALSA resolution and subprocesses. VAD accepts only canonical PCM, while `SingleTurnVoicePipeline` reopens the finalized normalized path and refuses noncanonical adapter output before Whisper. Brain, CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, and skills do not know ALSA devices, source rates, resampling details, or diagnostic paths.

Format diagnostics remain structured on the V1 capture/result boundary: requested and resolved device, requested rate, actual source rate/channels/width, canonical rate/channels/width, raw and normalized paths/durations, and final Whisper input path. `--diagnostic-audio` explicitly retains distinct raw/pre-VAD and final normalized WAVs. Normal operation does not retain the raw diagnostic capture. Unique temporary names, closed WAV writers, canonical revalidation, cancellation, lifecycle/resource gates, and microphone/speaker mutual exclusion remain mandatory.

# Transcript Normalization and Voice Calculator Routing

`core.TranscriptNormalization` owns deterministic STT cleanup before `VoiceCommandRouter`. The Brain, IntentParser, Planner, and CalculatorSkill remain unaware of Whisper formatting and model output.

```text
Whisper raw transcript
  -> TranscriptNormalizationRequestV1
  -> conservative adjacent-loop cleanup
  -> strict spoken-arithmetic parser when arithmetic is detected
  -> TranscriptNormalizationResultV1
  -> VoiceCommandRouter
  -> CoreService
  -> SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill
```

The result preserves `raw_transcript`, `cleaned_transcript`, and `normalized_command`. General commands are Unicode/whitespace/punctuation normalized. Arithmetic commands additionally support English number words zero through one thousand, negatives, decimals, plus/add, minus/subtract, times/multiplied-by, divide/over, and explicit spoken parentheses. The output is numeric/operator text such as `calculate 2 + 2`; the existing calculator still performs strict character validation, AST parsing, finite/bounded arithmetic, and never uses `eval()`.

Adjacent phrase blocks are collapsed only when they repeat beyond the configured limit. Thus `two plus two plus two` remains a legitimate three-term expression, while a longer exact adjacent Whisper loop is reduced and reported through `repetition_detected`, `repetitions_removed`, and `cleanup_rule`. Unsupported arithmetic words, malformed number grammar, and unsafe characters return a structured rejection before Brain execution. Unknown non-arithmetic requests remain unknown rather than being forced into calculator routing.

Natural-language calculator extraction is an anchored operation inside the same normalizer. The real Raspberry Pi transcript `I'll calculate 2 plus 2.` previously failed because arithmetic-candidate detection ran, but `I'll calculate` was not an approved removable prefix; the apostrophe and words then violated the arithmetic-source grammar. The V1 extractor now accepts only registered leading forms: direct calculator actions (`calculate`, `compute`, `solve`, `work out`), bounded polite action requests (`please`, `can/could/would you`), question forms (`what is`, `what's`, `how much is`), `tell me what ... is`, `I'll/I will calculate`, and an optional leading `Ares` vocative. Matching is case-insensitive after Unicode normalization and requires a word or punctuation boundary.

The extractor never removes arbitrary middle words. Only `tell me what <expression> is` may remove one exact trailing `is`. The anchored `I want you to calculate <expression>` form is also supported. The remainder must be one complete supported arithmetic expression. Additional commands, a second expression, identifiers, imports, function calls, attributes, assignments, paths, shell syntax, malformed operators, or unsupported words fail before routing. Successful or rejected wrapper extraction records `calculator_natural_language_wrapper`; repetition cleanup and wrapper extraction may both be represented. CalculatorSkill remains the sole arithmetic executor and its AST safety policy is unchanged.

## Production Calculator Routing Boundary

The text REPL, typed voice simulation, and real single-turn voice script now obtain built-in skills through one `create_builtin_skill_manager()` factory. This removes duplicated plugin-registration assembly and ensures the real voice path uses the same registered `CalculatorSkill`, IntentParser, Planner, and ExecutionPipeline as local text execution. Tests inject hardware adapters into the production `create_pipeline()` factory; they do not replace the calculator, command router, CoreService, planner, or execution layer.

```text
Whisper transcript
  -> TranscriptNormalizationRequestV1 / ResultV1
  -> VoiceCommandRouter
  -> CoreService voice.text_loop capability
  -> shared built-in SkillManager registry
  -> IntentParser
  -> ToolSelector candidate scoring
  -> Planner
  -> ExecutionPipeline
  -> registered CalculatorSkill
  -> structured text response
  -> Piper / ALSA only when explicitly requested
```

Normalization uses finite wrappers, complete token consumption, a 1024-character transcript bound, and a 256-character arithmetic-source bound. Capitalization, terminal punctuation, bounded whitespace, digits, English number words through one thousand, and approved spoken operators normalize deterministically. Remaining letters, code/import syntax, assignments, file paths, shell punctuation, malformed operators, or oversized arithmetic fail before intent routing. The existing CalculatorSkill continues to use its approved AST operators and numeric bounds; `eval()` is not used.

`SingleTurnVoiceResultV1` exposes raw, cleaned, and normalized text plus transcript cleanup rule, parsed intent, candidate-skill records, selected skill, planner decision, execution result, rejection reason, and bounded per-stage status. Candidate records include confidence, threshold, manifest registration/enabled state, capabilities, and the selection reason. Core classes do not print diagnostics; the owner-run script renders the fields only when `--diagnostic-routing` is requested.

The Linux ALSA manifest explicitly provides `voice.capture.activity`, consumes and produces the V1 VAD contracts, declares one task slot and a small logical resource reservation, and remains disabled by default. Fixed-duration capture remains available as an explicit fallback. No wake word, background listener, persistent transcript, GPT, cloud service, or autonomous capture was added.

The original VAD checkpoint collection was 912 tests. Adaptive calibration checkpoint collection was 959 tests. Production-registry routing checkpoint collection was 1002 tests. Natural-language calculator extraction checkpoint collection was 1058 tests. Current format-safe capture collection: 1079 tests.

# Architecture Hardening Checkpoint

This checkpoint comes after the simulated Phase 3 Voice City command pipeline and before real hardware/adapters.

Implemented:

- enforced module lifecycle
- versioned interface contracts
- capability manifests
- memory/database migrations
- health checks and adapter fallback
- measured resource budgets
- final integration, recovery, and safety regression checkpoint

Remaining hardening items:

- none. Architecture Hardening is complete before real Phase 3 voice hardware work.

Permanent rule: Every ARES ability must be independently installable, replaceable, disableable, health-checkable, version-compatible, and testable without modifying the Brain.

Permanent contract rule: No City, Skill, adapter, device, or service may exchange an unversioned public request or response across an ARES architectural boundary.

Permanent manifest rule: No independently loadable ARES module may start without a valid registered capability manifest.

Permanent memory rules:

- Durable ARES data may never be rewritten without validation and backup.
- Unknown future schema versions must never be silently downgraded.
- A failed load must never be interpreted as empty memory.
- Hardware-specific paths must not become part of the durable memory schema.

Permanent health/fallback rules:

- The Brain never selects concrete adapters.
- Automatic fallback is allowed only for explicitly retry-safe operations.
- A failed adapter must never cause unrelated Cities to activate.
- Fallback must never hide the original failure.
- Disabled or circuit-open adapters must not be selected.
- Health checks must not perform destructive actions.

Permanent resource rules:

- The Brain never manages RAM, CPU, adapters, or hardware.
- CoreService controls activation and resource reservations.
- No module activates before capacity is reserved.
- No failed operation may leak a reservation or task slot.
- Declared estimates must never be represented as exact measurements.
- Resource inspection must not activate inactive Cities.
- Dangerous actions must never be repeated because of eviction, retry, or cancellation.

# Final Integration, Recovery, And Safety Checkpoint

The final checkpoint proves complete internal routes across subsystem boundaries before real Phase 3 voice hardware work. It is not a new feature phase and does not add real microphone access, speech engines, GPT, internet access, background listeners, remote control, notifications, or new Cities.

Checkpoint pytest collection at this point was 630 tests.

Verified integration routes:

- Safe voice/text requests flow through mock microphone/audio, mock STT, Voice City, VoiceCommandRouter, IntentParser, Planner, ExecutionPipeline, the selected local skill/service, and mock output.
- Read-only PC status requests route through CoreService and PCService and return structured status without confirmation, shell commands, or unrelated adapter activation.
- Confirmation-gated device actions pause before execution, reject expired/malformed/reused/wrong confirmations, and execute exactly once after a valid confirmation.

Recovery order:

1. validate request
2. resolve capability
3. validate interface version
4. validate manifest
5. check health
6. reserve capacity
7. activate selected module
8. execute once
9. produce structured result
10. release task slot
11. retain or unload according to lifecycle policy
12. record bounded operational outcome

Fallback may occur only before destructive execution, only when the fallback satisfies the same contract, only when policy permits it, only when resource capacity permits it, and only when confirmation remains valid for the exact action.

Exactly-once destructive action protection is provided by `core.ExecutionGuard`. A confirmed destructive action receives a bounded local execution/idempotency token before the dangerous boundary is crossed. Completed tokens return the recorded structured result on duplicate submission; wrong-scope, uncertain, in-progress, malformed, expired, or reused confirmations fail closed. The guard stores no secrets or personal content and does not create automatic destructive retries.

Safety regression guarantees:

- The Brain cannot directly execute shell commands or select concrete adapters.
- Voice commands cannot bypass CoreService or confirmation gates.
- Fallback, retry, eviction, and cancellation cannot repeat dangerous actions.
- Disabled, incompatible, unknown, or malformed modules fail closed before activation.
- User-provided executable paths are rejected; app launch remains allowlist-only.
- Status, capability, resource, and event inspection do not activate inactive Cities.
- Operational events exclude API keys, tokens, passwords, transcripts, microphone audio, personal memory, and raw exception traces.
- Resource reservations and task slots are released on success and failure paths.
- Declared resource estimates are never reported as exact measured module usage.

# Next Project Block

After Architecture Hardening, Phase 3 real voice integration proceeds only with explicit owner approval. Completed checkpoints now include ALSA capture/playback, offline Whisper, offline Piper, validated voice profiles, the controlled single-turn pipeline, and the bounded owner-triggered multi-turn session. The next planned sequence is:

1. run and validate the bounded multi-turn command on Raspberry Pi hardware
2. measure per-turn timing, stop recognition, RMS thresholds, and cleanup from observed results
3. only later consider wake-word/background listening

This is a future implementation block. The current runtime still has no Vosk, wake word, GPT, internet access, background listener, daemon, scheduler, autonomous loop, unbounded conversation loop, boot-time microphone activation, or cloud TTS fallback.

# Measured Resource Budgets

`core.ResourceBudget` is the common resource-budget boundary for CoreService-managed modules. It enforces declared logical costs and task slots before lifecycle activation. It does not claim to measure exact per-module RAM or CPU.

Capability manifests can now declare resource metadata:

- `estimated_ram_mb`
- `estimated_cpu_weight`: `tiny`, `low`, `normal`, `high`, or `extreme`
- `startup_cost`: `instant`, `light`, `medium`, or `heavy`
- `shutdown_cost`: `instant`, `light`, `medium`, or `heavy`
- `heavy_module`
- `persistent_module`
- `inactivity_timeout_seconds`
- `maximum_concurrent_tasks`
- `task_priority`: `background`, `low`, `normal`, `high`, or `critical`
- `network_required`
- `hardware_acceleration_required`

`ResourcePolicy` defines global capacity rules. Local profiles are configuration data:

- `test`
- `raspberry_pi_5`
- `desktop`
- `future_orin`

The Raspberry Pi 5 profile keeps a conservative one-heavy-module limit. These profiles are declared logical budgets, not verified hardware benchmarks.

`ResourceManager` owns:

- activation checks with `can_activate()`
- reservations with `reserve()` and `release()`
- bounded task slots with acquire/release APIs
- activity tracking
- inactive-module discovery
- conservative eviction candidate selection
- cooperative cancellation tokens
- read-only current usage and reservation reports
- optional observed process-level metrics

CoreService integrates the resource gate into lazy routing:

request -> route to City/module -> inspect manifest -> validate compatibility and health/lifecycle prerequisites -> reserve capacity -> start lifecycle -> health-check selected module -> acquire task slot -> execute -> record activity -> release task slot -> retain or release reservation according to lifecycle/resource policy.

If reservation fails, the module does not start, unrelated Cities remain inactive, and the structured result explains the blocking limit. Failed startup, health failure, task-slot failure, and execution failure release task slots and newly created reservations safely.

Idle unloading is explicit only. `CoreService.run_resource_maintenance()` stops inactive non-persistent modules during owner/test-triggered ticks. No thread, scheduler, daemon, or background timer is added. Persistent modules and active tasks are not unloaded.

Eviction is optional and conservative. Candidates must be inactive, non-persistent, not processing a task, lower priority than the incoming request, and safe to stop. Critical-priority modules, active sessions, unsafe-stop modules, Brain/CoreService, and dangerous actions are not evicted automatically.

Observed metrics are process-level and read-only. ARES may report process uptime, CPU time, optional RSS when the platform exposes it, active module count, active heavy module count, active task count, declared reserved RAM, and loaded City count. Missing metrics remain unavailable instead of being faked.

Resource events can be stored in `EventHistoryStore`:

- `resource.reservation_created`
- `resource.reservation_released`
- `resource.activation_denied`
- `resource.heavy_module_limit_reached`
- `resource.idle_module_unloaded`
- `resource.eviction_performed`
- `resource.eviction_refused`
- `resource.task_slot_acquired`
- `resource.task_slot_released`
- `resource.task_cancelled`
- `resource.maintenance_completed`

Payloads are bounded operational metadata only. They must not include transcripts, secrets, personal memory contents, raw stack traces, or microphone recordings.

# Health Checks and Adapter Fallback

`core.Health` is the common health and fallback boundary for services, Cities, skills, and adapters. It normalizes existing `health_check()`, `get_status()`, and `status()` outputs behind one `HealthResult` shape instead of replacing stable local status contracts.

Health results include:

- component name
- component type
- status
- healthy/available/degraded booleans
- check timestamp
- optional latency
- error code
- safe message
- capabilities
- bounded non-secret metadata

Supported health statuses are:

- `healthy`
- `degraded`
- `unavailable`
- `failed`
- `disabled`
- `unknown`

Health checks are availability checks only. They must not start background listeners, open microphones, send notifications, call GPT, access arbitrary internet endpoints, mutate Brain state, modify persistent memory, execute PC actions, or activate unrelated Cities.

`AdapterFallbackPolicy` centralizes provider selection inside the owning City/service boundary. It accepts ordered `AdapterCandidate` objects, checks capability compatibility, interface-version compatibility, enabled state, health status, and circuit state, then selects the first compatible healthy adapter. Degraded adapters are selected only when policy explicitly allows degraded operation. Rejected candidates remain visible through structured rejection reasons.

Runtime fallback is separate from preflight selection. Runtime fallback is allowed only when the operation is explicitly marked `retry_safe`. It is bounded by `max_fallback_attempts`, preserves the original failure in the result/history, and reports which adapter ultimately handled the request. Retry-unsafe or unknown operations do not automatically fall back.

`CircuitBreaker` provides local bounded failure tracking with states:

- `closed`
- `open`
- `half_open`

Repeated failures open a circuit. Open adapters are skipped. After cooldown, a checked adapter moves to half-open for one probe. A successful probe closes the circuit; a failed probe reopens it. There is no daemon or background timer; state advances only when checked or used.

`HealthCache` supports optional short-lived health caching with TTL, forced refresh, and disabled-adapter invalidation. It is in-memory only.

CoreService health visibility is read-only:

- `get_service_health(name)`
- `list_service_health()`
- `get_capability_health(capability)`

These methods report known manifest, lifecycle, city status, and safe health state without activating every lazy City. Active probes are explicit and still must not execute business actions.

Voice City is the first integrated fallback boundary. The simulated `VoicePipeline` can use candidate lists for mock microphone selection and mock speech-to-text fallback. Default single-adapter behavior remains compatible. Text/mock fallback remains available in tests when the primary mock STT adapter is unavailable.

Operational health/fallback events can be stored in `EventHistoryStore`:

- `health.check_failed`
- `health.fallback_selected`
- `health.all_candidates_unavailable`
- `health.circuit_opened`
- `health.circuit_half_open_probe`
- `health.circuit_recovered`

Event payloads include only bounded operational fields such as city, capability, adapter name, status, error code, and attempt count. They must not store transcripts, API keys, raw personal data, or full exception traces.

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
- `VoiceActivityCaptureRequestV1`
- `VoiceActivityCaptureResultV1`
- `TranscriptNormalizationRequestV1`
- `TranscriptNormalizationResultV1`
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
- `SingleTurnVoiceRequestV1`
- `SingleTurnVoiceResultV1`
- `MultiTurnVoiceSessionRequestV1`
- `MultiTurnVoiceSessionResultV1`
- `EventPublicationEnvelopeV1`

`ContractRegistry` is the central compatibility registry. It can list known contracts, report supported versions, report the current version, identify consumers, and validate whether a requested contract is compatible. Duplicate incompatible registrations are rejected.

Compatibility validation is integrated into:

- VoicePipeline
- VoiceCommandRouter
- CoreService
- ModuleLifecycleManager
- microphone adapter boundary
- speech-to-text adapter boundary
- transcript normalization boundary
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

# Capability Manifests

`core.CapabilityManifest` is the declaration every independently loadable ARES module must provide before CoreService may activate it. Manifests make module compatibility explicit instead of relying on filenames, Python class names, or implicit behavior.

Supported module types are:

- `city`
- `skill`
- `adapter`
- `service`

Each manifest declares:

- identity: module name, module type, module version, manifest version, description, provider, and default enabled state
- capabilities: explicit capability strings such as `voice.capture`, `voice.transcribe`, `voice.command_route`, `voice.session`, `device.lock`, `device.sleep`, `weather.current`, and `market.quote`
- contract support: consumed and produced contract names with supported versions
- dependencies: required capabilities, optional capabilities, incompatible capabilities, required modules, and optional modules
- platform compatibility: supported operating systems, supported architectures, minimum Python version, and factual hardware flags such as microphone, speaker, camera, GPIO, GPU optional, and network optional
- permissions: declared permission identifiers such as `microphone.read`, `speaker.write`, `camera.read`, `network.outbound`, `filesystem.read`, `filesystem.write`, `process.launch`, `device.control`, and `gpio.control`
- lifecycle support: declared operations from `start`, `health_check`, `execute`, `stop`, and `recover`
- metadata: optional JSON-compatible metadata preserved through deterministic serialization

Any permission not declared is denied by policy. The current implementation validates declarations and policy compatibility; it does not implement OS-level permission enforcement yet.

`CapabilityManifestRegistry` is the central registry used by CoreService. It supports registering and unregistering manifests, retrieving manifests, listing enabled modules, listing modules by type or capability, finding providers for a capability, deterministic provider selection, and preferred-provider configuration through safe local policy.

Manifest validation covers:

- duplicate module names
- duplicate provider/version combinations
- malformed manifest versions
- unknown module types
- unknown or unsupported contract versions
- unknown required capabilities
- missing required modules
- incompatible capabilities
- platform mismatch
- undeclared permissions blocked by policy
- lifecycle declaration mismatch with the implementation

Provider selection returns all valid candidates, respects explicit preferred-provider configuration, and otherwise uses deterministic ordering. ARES must not silently select an incompatible provider. Controlled runtime fallback is handled by the centralized health/fallback policy and remains limited to explicitly retry-safe operations.

CoreService activation gate:

1. Locate the selected module manifest.
2. Verify the module is enabled.
3. Verify the requested capability is declared.
4. Verify contract compatibility.
5. Verify required capabilities and modules.
6. Verify platform compatibility.
7. Verify requested permissions are allowed by policy.
8. Verify lifecycle implementation compatibility.
9. Start the module only after every manifest check passes.

Failure before activation returns a structured manifest rejection, preserves the correlation id, does not start the module, does not alter unrelated city lifecycle state, and can record a `manifest.validation_failed` event-history entry when CoreService has an `EventHistoryStore` configured.

Current registered manifest coverage includes PCService, Voice City, mock microphone adapter, disabled-by-default Linux ALSA microphone adapter with `voice.capture.activity`, mock speech-to-text adapter, disabled-by-default Linux Whisper speech-to-text adapter, mock voice output adapter, VoiceCommandRouter, VoiceSessionSkill, and skill manifests registered by SkillRegistry. `config/modules.example.json` documents safe local configuration for enabled modules, preferred providers, VAD thresholds, and allowed permissions. It is not remote configuration and does not enable package downloads, dynamic imports, API keys, internet discovery, or automatic dependency installation.

Future modules must register manifests through the central registry before activation. A future V2 contract must be introduced by registering its new contract version and updating manifests explicitly; unknown versions must be rejected rather than reinterpreted.

# Memory Schema Migrations

`memory.schema_migrations` is the centralized migration framework for active JSON-backed persistent stores. Store modules call this shared layer instead of implementing ad hoc version checks.

Durable data classes:

- Durable identity/memory: `UserProfileStore`, `MemoryStore` short-term memory, `MemoryStore` long-term memory, `GoalsStore`, `NotesStore`, and `TasksStore`.
- Operational history: `EventHistoryStore`.
- Derived state: `ReminderScheduler` reads tasks and has no separate persisted file.
- Voice-session history: stored as event-history records; no separate voice-session store exists.
- Disposable cache: cache data is not identity memory and is not migrated as durable owner memory.
- Configuration: app allowlists, adapter examples, module examples, and other config files are configuration-backed durable state, not owner identity memory.
- Legacy/disconnected: `memory_manager.py` and `memory/memories.json` are legacy script-era formats. The active runtime uses `memory.v1.MemoryStore` and explicit store paths.

Current active schemas:

- `ares.user_profile`
- `ares.goals`
- `ares.notes`
- `ares.tasks`
- `ares.memory.short`
- `ares.memory.long`
- `ares.event_history`

Every active durable JSON store uses this envelope:

```json
{
  "schema_name": "ares.notes",
  "schema_version": 1,
  "created_at": "2026-07-11T00:00:00Z",
  "updated_at": "2026-07-11T00:00:00Z",
  "data": [],
  "metadata": {}
}
```

Schema versions are integer major versions. Missing schema versions are accepted only by explicit legacy importers for known structures. Unknown future versions fail closed. Downgrades are rejected. Migrations must be sequential; a future v1 -> v3 migration must run v1 -> v2 and then v2 -> v3. Current production schemas remain v1. A test fixture demonstrates v1 -> v2 behavior without inventing a production schema change.

`MigrationRegistry` supports:

- schema registration
- migration registration
- known schema lookup
- current-version lookup
- supported-version lookup
- migration-path calculation
- sequential migration execution
- dry-run mode
- duplicate edge rejection
- cycle rejection
- missing path rejection
- schema-specific legacy importers
- pre/post migration validation

File migration and writes follow this safety sequence:

1. Read and validate source.
2. Create a local backup under `.migration_backups`.
3. Migrate in memory.
4. Validate after every migration step.
5. Write a temporary file.
6. Flush safely where practical.
7. Atomically replace the original where practical.
8. Verify the final file can be loaded.

If a migration or write fails, the original file is preserved, the backup is preserved, incomplete temporary output is removed where possible, and the error is returned as structured data. Store load failures are not converted into empty memory. Where a store has an event bus, it publishes `storage.migration_failed`. Where an `EventHistoryStore` is provided, migration failures can also be recorded as local event-history records.

Inspection reports are read-only and include path, schema name, detected version, current target version, migration needed, migration path, latest backup, and validation state. They do not dump personal memory contents.

This migration layer is local-only. It does not add remote databases, cloud synchronization, distributed locking, PostgreSQL, Docker, automatic cloud backup, or hardware-specific paths. It prepares the future home-server model by keeping durable identity and memory upgradeable independently of Raspberry Pi, PC, phone, robot body, or other replaceable clients.

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

Voice City includes a safe service skeleton, mock session loops, adapter contracts, VoiceCommandRouter, the simulated VoicePipeline, an explicit controlled `SingleTurnVoicePipeline`, and a bounded owner-triggered `MultiTurnVoiceSession`. Mock/null adapters remain the default automated path; owner-run Linux adapters cover ALSA capture, offline Whisper transcription, offline Piper synthesis, and explicit ALSA playback. Future Voice City work may own wake-word or background-session behavior only after separate approval. The Brain receives structured text and responses; it contains no microphone, speaker, speech-engine, model-path, audio-driver, or session-hardware control code.

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
- Concrete adapter selection happens inside the owning City or service, never in the Brain.
- Automatic fallback is allowed only for explicitly retry-safe operations.
- Disabled, incompatible, unhealthy, or circuit-open adapters must not be selected.
- Health checks must not perform destructive actions.
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
