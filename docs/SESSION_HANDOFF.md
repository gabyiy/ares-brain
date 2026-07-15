ARES Session Handoff

Last Updated: 2026-07-15

Current Version

ARES v2.00 - Constrained Vosk Standby Wake Recognition

---

Current Status

ARES is at the completed Architecture Hardening foundation plus a central deterministic Capital/Core Brain session manager, persistent foreground runtime, and injected staged Raspberry Pi standby wake listener. Verified components already include ALSA input/output, constrained Vosk standby recognition, offline Whisper active-command STT, offline Piper TTS, configurable voice profiles, controlled single-turn and bounded multi-turn pipelines, adaptive RMS end-of-speech capture, complete ordered utterance assembly, duration-checked canonical 16 kHz mono PCM normalization, production natural-language calculator routing, and central general explicit long-term owner memory with confirmation-gated CRUD shared by text and voice through CoreService. Real Raspberry Pi evidence showed tiny Whisper was unsuitable for the isolated name: it forced `Ares` audio into unrelated words such as `Alrighty`, `Okay`, and `Bye`. Standby now uses a strict local Vosk grammar; the implementation is verified with deterministic adapters on Windows, while real Raspberry Pi Vosk activation remains owner-run.

Checkpoint root causes and fixes:

- ARES previously had strict lifecycle state for removable modules and bounded one-turn voice orchestration, but no Capital-owned lifecycle authority for a future persistent Brain process. `BrainSessionManager` now owns the central state machine and CoreService exposes a read-only snapshot without booting or activating any City.
- V1 transition/snapshot contracts, injected-clock inactivity checks, unique active-session IDs, bounded failure escalation, explicit safe recovery, lock-protected access, transition history, and privacy-bounded lifecycle events are implemented. The manager contains no microphone, Whisper, Piper, memory-store, skill, GPT, network, listener, or runtime-loop behavior.
- Checkpoint 2 adds `core.BrainRuntime` as the Capital-owned foreground process controller while keeping `BrainSessionManager` as the sole state authority. The runtime boots once to standby, recognizes only exact bounded text phrases, keeps one session ID across serial production skill commands, returns to standby at the exact manager inactivity deadline or an owner stop phrase, and exits only on explicit shutdown/cancellation/end-of-input/unsafe failure.
- Runtime input/output are injected. Queue/collecting adapters drive deterministic verification and bounded console adapters preserve the explicit text interface. Runtime events store category, lengths, status, timing, and correlation/session IDs, never full input, owner-memory values, response content, audio, secrets, or files.
- Checkpoint 3 adds one injected `StandbyWakeListener` to the Capital-owned runtime. Linux standby uses calibrated RMS VAD as the low-cost first stage, canonical 16 kHz mono PCM, and an injected `WakeRecognizer` only for a bounded candidate. Non-wake speech remains silent in standby and never reaches CoreService.
- Active voice input/output reuse `SingleTurnVoicePipeline`. A shared capture/playback gate and post-playback delay prevent simultaneous input/output and self-wake. Candidate files are unique and removed by default, events contain no transcript/audio content, and shutdown cancels adapters without hidden worker threads. No City activation, systemd, boot hook, daemon, GPT, cloud, network listener, or barge-in was added.
- `VoskWakeRecognizer` loads the configured local model once and constrains decoding to `ares`, `aries`, `hey ares`, `hey aries`, `okay ares`, `okay aries`, exact standby/shutdown controls, and `[unk]`. The complete normalized result must match one phrase; there is no substring, repetition, edit-distance, or fuzzy fallback.
- Word output is mandatory. Missing/invalid confidence, any word below the conservative 0.8 default, `[unk]`, extra words, `okay`, `bye`, `alrighty`, `areas`, `air`, and sentences that merely mention Ares all reject activation. Every accepted wake resolves to canonical `ares`.
- `WakeRecognizerRequestV1` and `WakeRecognizerResultV1` contain classification/confidence metadata but no recognition text. Raw Vosk JSON and normalized text remain available only in explicit local `--diagnostic-wake` output and never enter events or owner memory.
- Wake capture now uses 0.25-second pre-roll, 0.7-second terminal silence, two-frame speech evidence, calibrated 160/120 continue/silence RMS floors, and a two-second active candidate cap. This explains and removes the previous 3.3-second output shape of three seconds plus 0.3-second pre-roll. Full-command VAD defaults remain unchanged.
- `--diagnostic-wake` exposes the local raw/cleaned/normalized transcript and classification only in the owner foreground terminal. Operational events, contracts, owner memory, and normal output remain transcript-free. Retention additionally requires `--retain-diagnostic-audio`, keeps one latest candidate by default, and never plays it automatically.
- Wake candidate duration comes from the finalized canonical WAV rather than total listener wall time. Raw stream, assembled, normalized, recognizer-input, recognition, and overall processing durations are reported separately, and an over-limit or noncanonical WAV is rejected before Vosk.

- End-of-speech could reach `maximum_duration_reached` because the previous detector cleared all trailing-silence evidence for any frame above one static silence threshold. Adaptive calibration now derives three bounded thresholds, and `POSSIBLE_SILENCE` resumes only after consecutive frames above the continue threshold.
- Voice arithmetic reached IntentParser as number words and Whisper formatting, so digit/operator intent rules returned `unknown`. The versioned transcript normalizer now preserves raw text and converts only strict supported arithmetic into the unchanged safe calculator route.
- The owner subsequently verified clear Raspberry Pi USB capture, end-of-speech completion, and base English Whisper output `Calculate 2 plus 2.`; the remaining observed `unknown` result was therefore inside routing.
- Routing tests had manually registered CalculatorSkill instead of constructing the same built-in registry used by the real script. Registration is now centralized, the production script factory is integration-tested, and structured diagnostics identify parser, candidate, planner, execution, or rejection failures independently.
- The next real Raspberry Pi run produced `I'll calculate 2 plus 2.` and failed at transcript normalization. Candidate detection saw arithmetic, but `I'll calculate` was not an approved removable prefix, so its apostrophe and letters correctly triggered `unsupported_arithmetic_text`.
- The shared normalizer now applies one anchored `calculator_natural_language_wrapper` registry and permits only a finite set of direct, polite, question, first-person, `Ares`, and `tell me what ... is` forms. It never removes arbitrary middle words and leaves CalculatorSkill validation unchanged.
- Windows CI verifies this extraction checkpoint with injected hardware adapters and does not claim the final post-pull Raspberry Pi calculator response.
- Real ALSA diagnostics then proved `hw:2,0` did not honor a requested 16 kHz rate: it supplied 44.1 kHz. The old headerless VAD stream nevertheless used 16 kHz frame sizes and WAV metadata, which explains growling, repetition, and unstable end-of-speech/routing despite clear direct recordings.
- Auto-stop now resolves numeric raw devices through `plughw`, fixed capture reads the actual WAV header, and both routes enforce one centralized 16 kHz mono signed 16-bit PCM boundary before VAD/Whisper. No raw PCM bytes are relabeled at another rate.
- The owner then proved a separate post-capture defect: ALSA/VAD processed 3.42 seconds through frame 171, but the normalized Whisper input contained exactly 1.0 second. `POSSIBLE_SILENCE` used total pending-buffer length to satisfy hangover, required only the smaller consecutive-low-frame count, and discarded the whole pending block at completion. Low-energy words and internal pauses could therefore disappear together.
- The VAD now preserves one persistent ordered utterance, appends each resumed pending block exactly once, requires the full configured consecutive terminal-silence duration, and trims only that suffix. Canonical normalization is lossless for already-canonical PCM, and a 0.05-second duration invariant blocks Whisper when unexplained truncation occurs.
- The owner verified the correction on Raspberry Pi: raw capture `7.060s`, assembled/normalized/Whisper input `5.400s`, `duration_consistent`, and `completed_after_silence`. This checkpoint does not alter the now-correct VAD/audio-duration path.
- The next failure, `unsupported_arithmetic_word:much`, occurred when an arithmetic candidate used a natural question shape not represented by the finite anchored wrapper/suffix table. Strict token validation was correct; the extraction registry was incomplete.
- The registry now recognizes explicit `how much`, `what does ... equal`, nested `tell me how much ... is`, answer/result, polite, first-person, and optional ARES-address forms. It records the extracted expression and never treats `much` as an arithmetic token or removes arbitrary middle words.
- The previous manual command explicitly selected all capture stages with `--playback-debug-stages`, which is why the owner heard raw, assembled, and normalized speech. Normal `--playback` now has one documented purpose: play the generated Piper response. Preservation and each diagnostic stage are independently owner-controlled.
- `scripts/run_ares_voice.py` now supplies the verified Raspberry Pi defaults and delegates to the existing production factory and `SingleTurnVoicePipeline`. It performs lifecycle/component health preflight before capture, resolves repository-relative Whisper paths from the script location, reads the default Piper profile from `config/voice_profiles.json`, plays only the generated response by default, and exits after one turn.
- Owner memory is now a central Brain/CoreService capability. The production path is `input -> CoreService -> SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> OwnerMemorySkill -> CoreService.execute_owner_memory() -> OwnerMemoryService -> OwnerProfileStore`; voice code neither imports nor instantiates the JSON store.
- `OwnerMemoryRequestV1` and `OwnerMemoryResultV1` carry explicit remember, update, recall, forget, and list actions across the skill/CoreService boundary. The same service is used by direct text, injected voice transcripts, and fresh Python processes.
- The canonical `ares.owner_profile` schema is now v3 at repository-root-relative `data/memory/owner_profile.json`. Registered sequential migration runs v1 -> v2 -> v3, preserves every keyed fact, and adds structured general memories. Current destructive confirmation state is transient and separate from authoritative owner memory.
- Mutations use an owner transaction lock plus the shared store-write lock, bounded schema/value validation, one retained last-known-good backup, deterministic UTF-8 JSON, a flushed temporary file, atomic replacement, and final reload validation. Corrupt or future-version data fails closed and is never replaced by an empty profile.
- Facts may be bounded strings, integers, finite floats, booleans, or short scalar lists. Protected credentials, executable/instruction content, unsafe keys, control characters, excessive counts/sizes, and recovery material are rejected; ordinary transcripts, recordings, complete conversations, and inferred facts are not persisted.
- Raspberry Pi exposed that `Remember that modified white color is blue.` did not match the narrow owner parser and then fell through to the generic `remember` task rule. The task store ran, so no owner profile was created.
- Owner-memory candidates now run before generic task/device rules, and recognized malformed candidates cannot fall through. A narrow exact alias handles that observed favorite-color Whisper substitution; no fuzzy or broad semantic matching was added.
- Explicit save, update, recall, delete/forget, and list requests share the production owner-memory route. Deterministic forms cover birthday, city, favorite game, pet name, work schedule, and bounded custom facts; a finite alias map prevents spelling variants such as `favourite colour` from creating duplicates.
- Routing priority remains narrow: `remember that my birthday is June 8` is owner memory, while `remember to buy milk`, `remind me tomorrow to buy food`, and `save a task to buy food` remain task/reminder operations. Normal conversation never creates owner memory.
- The canonical owner profile resolves from the repository root regardless of current working directory. Routing diagnostics report action, normalized key, candidate reason, selected skill, path, prior file state, operation result, and bounded rejection details without exposing protected values.
- Explicit general-memory triggers now pass through `IntentParser -> Planner -> OwnerMemorySkill -> CoreService.execute_owner_memory() -> OwnerMemoryService`; `run_ares_voice.py` remains a transport and contains no persistence code.
- Bounded triggers cover remember/do-not-forget and explicit long-term save/store/keep/add/note forms, including conservative `long time memory` / `long memory` Whisper variants. Ordinary conversation and `remember to` task language remain non-owner-memory paths.
- Real Raspberry Pi transcripts then exposed a second routing gap. Whisper emitted `Remember in your locked term memory that I love going to the gym`, which was not recognized as owner memory and fell through to the generic `remember` task rule. It also emitted `Remembering a long term memory that I like video games`, which no save pattern accepted and therefore routed unknown. Schema v3 was healthy; the write route never ran.
- Trigger normalization now recognizes bounded `locked term`, `lock term`, `locked-term`, `long turn`, `long time`, `long memory`, `lifetime`, `permanent`, and `persistent` variants only near an explicit leading memory verb and before the fact clause. Leading `remembering ... memory that` is canonicalized to `remember longterm that`. Arbitrary `locked` text inside a fact is not rewritten.
- IntentParser already evaluates owner memory before tasks; canonicalizing these candidates before routing makes that priority effective. `remember ... memory ... that <fact>` selects owner memory, while `remember to <action>`, `remember my task ...`, and reminder time/action structures remain tasks. Diagnostics preserve raw and cleaned transcripts while adding canonical trigger, extracted fact, selected action, and routing reason.
- Deterministic production-factory tests seed birthday, city, favorite color, and favorite game, then prove the two observed transcripts add exactly two active preferences, combined `What do I like?` recall returns both, repeats are duplicates, the keyed facts remain byte-for-byte equivalent at the value boundary, and no task store is created. Post-pull Raspberry Pi verification remains owner-run.
- General records use one of nine finite types, deterministic subject/predicate/object extraction, bounded topics, exact normalized duplicate signatures, lexical retrieval, explicit superseding updates, and active/superseded state. No embeddings, vector database, GPT, or autonomous extraction was added.
- Owner-memory list, inspect, count, specific deletion, topic deletion, all-general deletion, keyed-fact deletion, confirmation, and cancellation now share the central CoreService route. Nothing destructive mutates the profile on the first request.
- Cross-process confirmation uses one atomic `ares.pending_owner_memory_action` v1 record at `data/runtime/pending_owner_memory_action.json`, not the owner profile. It expires after 60 seconds by default, contains only bounded targets and normalized request metadata, and stores no raw transcript or audio.
- Specific deletion requires exactly one high-confidence match. Topic and all-general operations snapshot only general-memory ids and preserve keyed facts. Keyed-fact confirmation binds the key and a revision digest. Expired, corrupt, changed-target, ambiguous, or missing pending state cannot execute.
- Valid pending state survives the one-turn voice process and unrelated commands until expiry. Confirmation or cancellation is considered before ordinary routing only while central pending state exists; a new destructive request replaces the old one.

Confirmed Phase 3 foundation:

- Voice City skeleton
- Manual Voice City text loop simulation
- Lazy city routing through CoreService capability metadata
- Internal `core.EventBus`
- Local `events.EventHistoryStore`
- Read-only `skills.EventHistorySkill`
- Voice City audio adapter contracts
- Voice City adapter-backed single-turn loop
- Voice City multi-turn mock session
- Voice Session Skill
- Voice Session event logging
- Voice Session status query
- Microphone adapter abstraction
- Speech-to-text adapter abstraction
- Voice Command Router
- Simulated VoicePipeline
- Enforced module lifecycle
- Versioned interface contracts
- Capability manifests
- Memory/schema migrations
- Health checks and controlled adapter fallback
- Measured resource budgets
- Final integration, recovery, and safety regression checkpoint
- Linux ALSA microphone adapter for explicit Raspberry Pi capture tests
- Offline Whisper speech-to-text adapter for explicit Raspberry Pi WAV transcription
- Raspberry Pi whisper.cpp runtime setup and verification scripts
- Hardened speech-input verification and ALSA monitoring helper
- Reliable English-mode Whisper verification defaults for `ggml-tiny.en.bin`
- Text-to-speech adapter abstraction
- Linux Piper offline TTS adapter for explicit Raspberry Pi WAV generation
- Linux ALSA speaker adapter for explicit USB speaker playback
- Raspberry Pi Piper setup and manual TTS verification scripts
- Correct V1/nested speaker health evaluation in the Raspberry Pi TTS verifier
- Selected ALSA playback-device validation through `aplay -l`
- Verified direct Piper WAV generation and audible USB speaker playback on Raspberry Pi 5
- Validated `VoiceProfile` / `VoiceProfileRegistry` boundary
- Official `en_US-hfc_male-medium` configured as the default ARES voice
- Previously verified `en_US-amy-low` retained as an optional profile
- Profile-aware Piper installer, health checks, result metadata, and manual verifier/listing
- Owner-confirmed real Raspberry Pi playback of the default `en_US-hfc_male-medium` profile
- Versioned controlled single-turn voice request/result contracts
- Lifecycle/resource-gated microphone -> Whisper -> Brain -> Piper -> speaker orchestration
- Owner-run `scripts/manual_verify_single_turn_voice.py` with real and simulated-text modes
- Versioned bounded multi-turn session request/result contracts
- `MultiTurnVoiceSession` orchestration that reuses `SingleTurnVoicePipeline`
- Owner-run `scripts/manual_verify_multi_turn_voice.py` with real, fixed text-turn, and bounded interactive-text modes
- Versioned `VoiceActivityCaptureRequestV1` / `VoiceActivityCaptureResultV1` contracts
- Hardware-neutral `RmsVoiceActivityCapture` with start/silence hysteresis, consecutive speech frames, pre-roll, and trimmed terminal silence
- Linux ALSA foreground raw-PCM streaming for automatic end-of-speech capture with `shell=False`
- Auto-stop integration in single-turn and bounded multi-turn pipelines with fixed-duration fallback preserved
- Owner-run `scripts/manual_verify_voice_activity_capture.py` for Raspberry Pi threshold calibration
- Bounded ambient RMS calibration with mean/median/p90/peak/noise-floor diagnostics
- Three-threshold start/continue/silence hysteresis with consecutive resume/end evidence
- Versioned raw/cleaned/normalized transcript contracts
- Strict spoken arithmetic conversion into the unchanged safe CalculatorSkill path
- Conservative adjacent Whisper repetition cleanup and concise manual routing traces
- Shared `create_builtin_skill_manager()` construction for text REPL, typed voice simulation, and real single-turn voice
- Production-factory integration through the real registered CalculatorSkill
- Versioned candidate-skill and per-stage routing diagnostics plus `--diagnostic-routing`
- Anchored natural-language calculator wrapper extraction with one-expression-only safety
- ALSA raw-device resolution to `plughw` for canonical streaming capture
- Actual-header WAV normalization before VAD/Whisper with atomic finalization
- Distinct per-turn raw/assembled/normalized WAV diagnostics plus transcript output through owner-requested `--preserve-diagnostic-audio`
- Pre-Whisper assembled/normalized duration invariant with fail-closed truncation handling
- Versioned extracted-calculator-expression routing diagnostics
- Independent diagnostic preservation, per-stage capture playback, and response playback controls
- Production-style `scripts/run_ares_voice.py` owner launcher with health preflight, repository-root path resolution, verified Raspberry Pi defaults, and response-only playback
- Core-owned `OwnerMemoryService` with versioned request/result contracts and one canonical `OwnerProfileStore`
- `ares.owner_profile` v3 keyed-fact plus structured general-memory persistence with sequential v1 -> v2 -> v3 migration, atomic writes, one retained backup, bounded validation, protected-key rejection, and isolated path override
- Registered `OwnerMemorySkill` through CoreService, IntentParser, Planner, ExecutionPipeline, and SkillManager
- Hardware-free keyed-fact verifier, general long-term fresh-process verifier, six-fresh-process compatibility verifier, and read-only fact/topic/type inspection tool
- Bounded Whisper-trigger normalization and owner-memory-before-task production regressions for the observed `locked term` and `remembering ... memory` transcripts
- Central owner-memory list/count/inspect and confirmation-gated specific/topic/all-general/keyed deletion
- Atomic cross-process `ares.pending_owner_memory_action` v1 state with expiry, cancellation, corruption rejection, and exact-target snapshots
- Central versioned `BrainSessionManager` state machine with strict transition rejection, deterministic inactivity checks, failure/recovery handling, and safe lifecycle events
- Capital/Core `BrainRuntime` foreground loop with exact text activation, same-session multi-command routing, manager-owned inactivity standby, owner standby, explicit shutdown, injected adapters, and privacy-safe events
- Versioned standby-wake listener contracts, strict wake configuration, exact phrase classification, and a Core-owned listener capability manifest
- Linux staged wake adapter using existing ALSA/calibrated-VAD/canonical-WAV boundaries plus constrained Vosk recognition without standby Whisper
- Active runtime voice adapters that reuse `SingleTurnVoicePipeline` and enforce capture/playback exclusion plus post-playback settling
- Exact configurable `Ares`/`Aries` grammar phrases with complete-candidate confidence and `[unk]` false-positive protection
- Terminal-only wake diagnostics, one-attempt diagnosis, bounded latest-candidate retention, and finalized-WAV duration guards
- Foreground `scripts/run_ares_standby_voice.py`, deterministic wake verifier, and bounded per-stage Raspberry Pi wake hardware helper
- Architecture Hardening Checkpoint before real hardware/adapters

Current pytest collection: 1847 tests.

Hardware-free Raspberry Pi verification after pulling:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/manual_verify_brain_session_manager.py
python scripts/manual_verify_brain_runtime.py
python scripts/manual_verify_standby_wake_runtime.py
python scripts/run_ares_brain_runtime_text.py
```

The manual verifiers use fake clocks and deterministic injected adapters; they require no Raspberry Pi hardware. The text process remains unchanged. The standby-wake verifier injects microphone/Vosk standby results plus Whisper active-command, Piper, and speaker behavior while exercising the real runtime, manager, single-turn transport, CoreService, calculator, and owner-memory routes. Windows verification passed; post-pull Raspberry Pi Vosk wake execution remains owner-run.

Raspberry Pi standby wake verification:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python -m pip install -r requirements.txt
mkdir -p models/vosk
curl -fL https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o /tmp/vosk-model-small-en-us-0.15.zip
unzip -q /tmp/vosk-model-small-en-us-0.15.zip -d models/vosk
rm /tmp/vosk-model-small-en-us-0.15.zip
python scripts/manual_diagnose_wake_word.py \
  --microphone-device plughw:2,0 \
  --speaker-device plughw:CARD=Device,DEV=0 \
  --vosk-model models/vosk/vosk-model-small-en-us-0.15 \
  --wake-min-confidence 0.8 \
  --diagnostic-wake
python scripts/manual_verify_standby_wake_hardware.py --diagnostic-wake
python scripts/run_ares_standby_voice.py
```

The first script captures one candidate and exits. The hardware helper runs six named tests with three attempts per test by default, exits nonzero on failure, and never replays owner capture. The production command stays in the foreground until `shutdown Ares` or Ctrl+C. Defaults are microphone `plughw:2,0`, speaker `plughw:CARD=Device,DEV=0`, `vosk-model-small-en-us-0.15` with 0.8 minimum confidence for standby, base English Whisper for active commands, male `en_US-hfc_male-medium`, 30-second inactivity, and no diagnostic output or retention.

Raspberry Pi post-pull verification:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/inspect_owner_memory.py --memories
```

Run a fresh `python scripts/run_ares_voice.py` process for each of these phrases: `Remember in your long-term memory that I love going to the gym.`, `Remember in long-term memory that I like playing video games.`, `What do I like?`, `What do you remember about the gym?`, and `What do you remember about video games?`. Then inspect with `python scripts/inspect_owner_memory.py --memories`, `--type preference`, `--topic gym`, `--topic gaming`, and `--json`. Existing keyed facts must remain and repeats must not increase the active general-memory count. This has not been claimed as post-fix Raspberry Pi verification from Windows.

The real audio surface now includes explicit ALSA fixed-duration or VAD-bounded capture, canonical WAV conversion, constrained local Vosk standby recognition, offline Whisper active-command transcription, profile-resolved Piper, explicit ALSA playback, controlled single/multi-turn paths, and the foreground staged standby-wake runtime. Adaptive calibration stays in the VAD boundary; transcript normalization stays between active STT and routing. `config/voice_profiles.json` remains the single voice source, while Brain/CoreService know none of the hardware, model, conversion, or subprocess details. Owner memory remains a separate central service reached through the normal skill route. Systemd, boot-time microphone activation, daemonization, notifications, GPT, internet runtime access, automatic transcript memory writes, barge-in, and real device/event automation remain disabled.

`skills.VoiceSessionSkill` now starts a bounded mock voice session from text commands: "start voice session", "start mock voice", and "run voice test". It uses only `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, and `VoiceSessionLoop`, returns a transcript summary, and is wired through IntentParser, Planner, ExecutionPipeline, SkillManager, and the REPL path.

Voice sessions now write safe operational event records to `EventHistoryStore` when `SkillContext.event_history_store` is configured. The current event types are `voice_session.started`, `voice_session.stopped`, `voice_session.adapter_failure`, and `voice_session.max_turns_reached`. Event payloads avoid mock transcript text and store only operational metadata such as status, turn count, max-turn limit, and adapter failure details.

ARES can now answer "what happened in voice session", "show last voice session", and "voice session status" by reading the latest local voice-session event group. This is read-only and returns started/stopped/failure/max_turns summary lines without starting a new mock session.

Microphone adapter abstraction has been added.

Microphone behavior:

- New model: `core.AudioChunk`.
- New result: `core.MicrophoneResult`.
- New interface: `core.MicrophoneAdapter`.
- New safe test adapter: `core.MockMicrophoneAdapter`.
- `MicrophoneAdapter` defines `start()`, `stop()`, `read_chunk(timeout_seconds, cancel_requested)`, `get_status()`, and `get_capabilities()`.
- `MockMicrophoneAdapter` covers lifecycle, queued chunk reads, timeout handling, cancellation support, structured status/capability data, and safe failure paths without hardware access.
- `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept microphone adapters through dependency injection.
- Voice City can swap a future microphone implementation without changing the Brain, CoreService, skills, or current text loops.
- Tests cover audio chunk serialization and validation, start/stop, read-before-start, queued chunk reads, timeout handling, cancellation callable/event support, failure modes, structured status/capabilities, and Voice City injection.
- No Whisper, Vosk, Piper, wake word, real STT, speaker access, GPT, internet, or background listener was added.

Linux ALSA microphone adapter has been added.

Linux ALSA behavior:

- New module: `core.LinuxAlsaMicrophone`.
- New adapter: `core.LinuxAlsaMicrophoneAdapter`.
- New safe subprocess boundary: `SafeSubprocessRunner`.
- The adapter implements the existing `MicrophoneAdapter` contract.
- It checks whether `arecord` is available, lists capture devices with `arecord -l`, validates optional selected devices such as `hw:1,0`, performs `health_check()`, records bounded WAV samples, validates missing/empty/invalid WAV output, and returns structured `MicrophoneResult` data.
- `SafeSubprocessRunner` uses argument lists with `shell=False`; no user text becomes a shell command.
- `linux_alsa_microphone_adapter` is registered as a disabled-by-default capability manifest provider.
- Manual script: `scripts/manual_verify_linux_alsa_microphone.py`.
- Manual Raspberry Pi commands:

```bash
python scripts/manual_verify_linux_alsa_microphone.py
python scripts/manual_verify_linux_alsa_microphone.py --record --seconds 3 --output /tmp/ares_mic_test.wav
python scripts/manual_verify_linux_alsa_microphone.py --device hw:1,0 --record --seconds 3 --output /tmp/ares_mic_hw_1_0.wav
```

- Tests mock subprocess and filesystem behavior and do not require microphone hardware.
- At the ALSA microphone checkpoint, no Whisper, Vosk, Piper, wake word, real STT, speaker/TTS, GPT, internet, or background listener was added.

Offline Whisper speech-to-text adapter has been added.

Offline Whisper behavior:

- New module: `core.LinuxWhisperSpeechToText`.
- New adapter: `core.LinuxWhisperSpeechToTextAdapter`.
- It implements the existing `SpeechToTextAdapter` contract.
- It accepts WAV files recorded by `LinuxAlsaMicrophoneAdapter` and `AudioChunk` input.
- It uses a local Whisper/whisper.cpp-style executable with `shell=False`.
- It requires a local model file; recommended first Raspberry Pi model is `ggml-tiny.en.bin`.
- It returns structured `TranscriptionResult` data with recognized text, processing time, requested/detected language metadata, status, and safe errors.
- It handles missing binary, missing model, invalid audio, timeout, non-zero process exit, and no-transcription results safely.
- `linux_whisper_speech_to_text_adapter` is registered as a disabled-by-default capability manifest provider.
- Manual script: `scripts/manual_verify_linux_whisper_stt.py`.
- Manual Raspberry Pi commands:

```bash
python scripts/manual_verify_linux_whisper_stt.py --model models/whisper/ggml-tiny.en.bin --whisper-command whisper-cli

python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command whisper-cli \
  --output /tmp/ares_whisper_test.wav

python scripts/manual_verify_linux_whisper_stt.py \
  --record \
  --device hw:1,0 \
  --seconds 3 \
  --model models/whisper/ggml-tiny.en.bin \
  --whisper-command /path/to/whisper-cli \
  --output /tmp/ares_whisper_hw_1_0.wav
```

- Tests mock subprocess and filesystem behavior and do not require Raspberry Pi hardware, a real microphone, Whisper, or a real model.
- No wake word, continuous/background listening, GPT, internet, speaker/TTS, autonomous loop, or conversation loop was added.

Raspberry Pi Whisper runtime preparation has been added.

Whisper runtime preparation behavior:

- New setup script: `scripts/install_whisper_cpp_raspberry_pi.py`.
- New runtime verifier: `scripts/verify_whisper_cpp_runtime.py`.
- The setup script clones `https://github.com/ggml-org/whisper.cpp.git` into `external/whisper.cpp` if missing.
- It builds `external/whisper.cpp/build/bin/whisper-cli` with CMake.
- It downloads the recommended `tiny.en` GGML model into `models/whisper/ggml-tiny.en.bin` by default.
- It verifies both the executable and model exist before reporting PASS.
- The runtime verifier locates `whisper-cli`, locates the GGML model, transcribes an existing recorded WAV sample, and prints PASS/FAIL diagnostics with recognized text and timing.
- Generated local samples, the cloned whisper.cpp checkout, and downloaded GGML model binaries are ignored by git.
- The scripts are import-safe and do not run installation, download, recording, or transcription on import.
- Tests mock subprocess and filesystem behavior and do not require network, Raspberry Pi hardware, real Whisper, or a real model.
- No wake word, continuous/background listening, GPT, internet runtime path, speaker/TTS, autonomous loop, or conversation loop was added.

Recommended Raspberry Pi commands:

```bash
sudo apt update
sudo apt install -y git cmake build-essential curl
python scripts/install_whisper_cpp_raspberry_pi.py
python scripts/manual_verify_linux_alsa_microphone.py --record --seconds 3 --output /tmp/ares_mic_test.wav
python scripts/verify_whisper_cpp_runtime.py --wav /tmp/ares_mic_test.wav
```

Explicit runtime verifier paths:

```bash
python scripts/verify_whisper_cpp_runtime.py \
  --whisper-command external/whisper.cpp/build/bin/whisper-cli \
  --model models/whisper/ggml-tiny.en.bin \
  --wav /tmp/ares_mic_test.wav \
  --language en
```

Raspberry Pi speech-input verification has been hardened.

Root cause addressed:

- The previous verifier accepted any non-empty Whisper transcript as success.
- Whisper's `[BLANK_AUDIO]` marker is non-empty text, so it could be reported as a successful transcription.
- The verification scripts used `--language auto` while the recommended installed model is the English-only `ggml-tiny.en.bin`; reliable direct manual Whisper runs use English mode.
- The previous verifier did not print enough WAV signal diagnostics or exact process diagnostics to distinguish wrong-file selection, silence, near-silence, or Whisper output parsing.

Current hardened behavior:

- `LinuxWhisperSpeechToTextAdapter` measures WAV file size, duration, sample rate, channels, sample width, peak amplitude, and RMS amplitude.
- Silent WAVs fail before `whisper-cli` runs.
- Near-silent WAVs fail when below the configured RMS threshold.
- `[BLANK_AUDIO]`, `<|nospeech|>`, `<|no_speech|>`, `(no speech)`, `[SILENCE]`, and `[NO_SPEECH]` style output is treated as `no_usable_speech`.
- English-only GGML model filenames resolve automatic language configuration to effective language `en`.
- `scripts/verify_whisper_cpp_runtime.py` and `scripts/manual_verify_linux_whisper_stt.py` default to `--language en` for the recommended `ggml-tiny.en.bin` model.
- `scripts/verify_whisper_cpp_runtime.py` prints selected WAV diagnostics, exact `whisper-cli` command, requested/effective language, exit code, and stdout/stderr previews on failures.
- `scripts/manual_verify_linux_whisper_stt.py` records, validates, and transcribes the exact WAV, and only plays it with `aplay` when `--playback` is explicitly provided.
- `scripts/configure_linux_alsa_monitoring.py` mutes mic playback monitoring while preserving mic capture and speaker playback mixer controls.

Hardened Raspberry Pi commands:

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

Explicit playback only:

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

Disable USB mic monitoring while preserving capture:

```bash
python scripts/configure_linux_alsa_monitoring.py --card 1 --apply
```

Equivalent ALSA commands:

```bash
amixer -c 1 sset Mic 0% mute
amixer -c 1 sset Capture 80% cap
amixer -c 1 sset Speaker 70% unmute
```

Text-to-speech adapter abstraction and offline Piper output have been added.

TTS behavior:

- New module: `core.TextToSpeech`.
- New contracts: `TextToSpeechRequestV1` and `TextToSpeechResultV1`.
- New interface: `core.TextToSpeechAdapter`.
- New safe test adapter: `core.MockTextToSpeechAdapter`.
- New Linux runtime adapter: `core.LinuxPiperTextToSpeechAdapter`.
- New speaker boundary: `core.SpeakerOutputAdapter`.
- New Linux speaker adapter: `core.LinuxAlsaSpeakerAdapter`.
- `LinuxPiperTextToSpeechAdapter` generates WAV files from explicit text through a local Piper executable with `shell=False`.
- `core.VoiceProfiles` provides the immutable profile model and strict profile registry used to resolve all Piper model/config files.
- `config/voice_profiles.json` configures `en_US-hfc_male-medium` as the one enabled default and retains `en_US-amy-low` as optional.
- Requested unknown or disabled profiles fail with structured errors; the adapter never silently selects a different voice.
- `LinuxAlsaSpeakerAdapter` validates an explicitly selected device through `aplay -l` and plays WAV files through `aplay` only when playback is explicitly requested.
- `scripts/manual_verify_linux_tts.py` checks the V1 result's `success` and `status` fields plus nested speaker health, validates generated WAV metadata, and reports deterministic process exit codes.
- The verifier prints resolved paths, selected device, exact Piper and requested `aplay` commands, WAV duration/sample rate/channels/sample width/file size, and raw stdout/stderr when a subprocess fails.
- Playback failure preserves the generated WAV for diagnosis.
- Playback is disabled by default and no microphone monitoring is enabled.
- `linux_piper_text_to_speech_adapter` and `linux_alsa_speaker_adapter` are disabled by default in module config and have capability manifests/resource metadata.
- Tests mock subprocess and filesystem behavior and do not require Raspberry Pi hardware, Piper, a real model, or a speaker.
- No GPT, wake word, background listener, automatic microphone activation, memory writes based on voice, cloud fallback, autonomous loop, or conversation loop was added.

Recommended Raspberry Pi Piper setup:

```bash
sudo apt update
sudo apt install -y curl tar alsa-utils
python scripts/install_piper_raspberry_pi.py
```

The installer now resolves the default from the registry. It also supports `--voice en_US-hfc_male-medium` and `--voice en_US-amy-low`, validates downloaded files, and skips valid existing files. It does not install anything during normal runtime.

Default local paths:

- Piper runtime: `external/piper/`
- Voice registry: `config/voice_profiles.json`
- Default model: `models/piper/en_US-hfc_male-medium.onnx`
- Default config: `models/piper/en_US-hfc_male-medium.onnx.json`
- Optional Amy model/config: `models/piper/en_US-amy-low.onnx` and `models/piper/en_US-amy-low.onnx.json`
- Generated samples: `data/manual_tts_samples/`

The official [Piper voice catalog](https://github.com/rhasspy/piper/blob/master/VOICES.md) and [`hfc_male` model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/hfc_male/medium/MODEL_CARD) identify the selected default as a single-speaker U.S. English male, medium-quality, 22,050 Hz voice. The official model is approximately 63 MB, making it a bounded local default for Raspberry Pi 5.

List profiles:

```bash
python scripts/manual_verify_linux_tts.py --list-voices
```

Manual TTS verification without playback:

```bash
python scripts/manual_verify_linux_tts.py \
  --text "Hello Gabriel. I am Ares, and my new voice is working."
```

Manual TTS verification with explicit USB speaker playback:

```bash
python scripts/manual_verify_linux_tts.py \
  --text "Hello Gabriel. I am Ares, and my new voice is working." \
  --voice-profile en_US-hfc_male-medium \
  --playback \
  --device plughw:CARD=Device,DEV=0
```

Verified Raspberry Pi result supplied by the owner:

- Piper runtime installed successfully.
- The `en_US-amy-low` model loaded successfully.
- Piper generated a valid WAV.
- Direct ALSA playback through `plughw:CARD=Device,DEV=0` succeeded.
- The generated voice was clearly audible through the USB speaker.
- The configured `en_US-hfc_male-medium` default loaded and played successfully; the owner audibly confirmed the male voice.

False-health root cause: the old manual verifier called `to_dict()` on a healthy `TextToSpeechResultV1` and then queried `health.get("healthy")`. That key is not part of the V1 contract, so it returned `None` and forced failure even though the contract contained `success=true`, `status=healthy`, and healthy nested speaker data. The corrected verifier evaluates those explicit structured fields.

Controlled single-turn voice pipeline has been added.

Single-turn behavior:

- New contracts: `SingleTurnVoiceRequestV1` and `SingleTurnVoiceResultV1`.
- New orchestration modules: `core.SingleTurnVoicePipeline`, `core.SingleTurnVoiceStages`, and `core.SingleTurnVoiceSupport`.
- New common WAV boundary: `core.WavAudio`.
- Route: microphone -> local Whisper -> VoiceCommandRouter -> CoreService `voice.text_loop` -> SkillManager -> IntentParser -> Planner/ExecutionPipeline -> selected skill -> local Piper -> ALSA speaker.
- The script never implements subprocess logic; ALSA, Whisper, and Piper remain inside their existing adapters.
- One heavy pipeline reservation and one task slot cover the full turn; the reservation/task slot are released in success, failure, timeout, and cancellation paths.
- `VoiceStageCoordinator` prevents microphone/speaker overlap and Whisper/Piper overlap.
- Silence and corrupt WAVs stop before Whisper; blank transcription stops before Brain; Brain failure uses a local response; TTS/playback failure preserves useful diagnostics.
- Operational events are bounded and exclude raw audio/transcript text.
- Text simulation uses the real SkillManager path while skipping microphone and Whisper; without `--playback` it also skips TTS/speaker.
- Single-turn checkpoint pytest collection: 812 tests.

Exact commands:

```bash
git pull --ff-only origin main
python scripts/manual_verify_single_turn_voice.py --text-input "calculate 2 + 2"
python scripts/manual_verify_single_turn_voice.py \
  --microphone-device plughw:2,0 \
  --speaker-device plughw:CARD=Device,DEV=0 \
  --auto-stop \
  --auto-calibration \
  --calibration-seconds 0.75 \
  --speech-start-rms 200 \
  --speech-continue-rms 160 \
  --silence-rms 120 \
  --silence-seconds 0.9 \
  --speech-wait-timeout 10 \
  --max-utterance-seconds 15 \
  --pre-roll-seconds 0.25 \
  --frame-ms 20 \
  --duration-loss-tolerance 0.05 \
  --language en \
  --whisper-command external/whisper.cpp/build/bin/whisper-cli \
  --whisper-model models/whisper/ggml-base.en.bin \
  --voice-profile en_US-hfc_male-medium \
  --diagnostic-routing \
  --timeout 300 \
  --playback
```

Say `How much is two plus two?` The expected trace is cleaned transcript `how much is two plus two`, extracted expression `two plus two`, normalized command `calculate 2 + 2`, cleanup rule `calculator_natural_language_wrapper`, intent `calculate`, selected registered skill `calculator`, planner target `calculator`, execution `success`, response `Result: 4`, and no rejection reason. The automated Windows production-factory test produced this result through CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, and the real CalculatorSkill. With `--playback`, only the generated Piper response WAV is sent to the speaker.

To preserve unique `raw_capture.wav`, `assembled_utterance.wav`, `normalized_whisper_input.wav`, and `whisper_transcript.txt` files without playing them, use the same command without `--playback` and add `--preserve-diagnostic-audio`. To hear captured stages explicitly, add one of `--play-diagnostic-capture`, `--play-diagnostic-assembled`, or `--play-diagnostic-normalized`; `--play-diagnostic-audio` selects all three once. These flags never enable live microphone monitoring. Final Raspberry Pi execution after pulling this checkpoint remains owner-run verification.

Controlled bounded multi-turn voice session has been added.

Session behavior:

- New contracts: `MultiTurnVoiceSessionRequestV1` and `MultiTurnVoiceSessionResultV1`.
- New orchestration modules: `core.MultiTurnVoiceSession`, `core.MultiTurnVoiceExecution`, `core.MultiTurnVoiceRuntime`, and `core.MultiTurnVoiceSupport`.
- Every normal turn invokes `SingleTurnVoicePipeline.run_once()`; greeting/closing output invokes its local-output path.
- Real route: ALSA microphone -> Whisper -> stop-phrase gate -> VoiceCommandRouter -> CoreService -> SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill -> Piper -> ALSA speaker.
- States: `created`, `starting`, `greeting`, `listening`, `transcribing`, `checking_stop_phrase`, `processing`, `synthesizing`, `speaking`, `waiting_between_turns`, `stopping`, `completed`, `failed`, and `cancelled`.
- Defaults: five turns, 180 seconds, three consecutive failures, five-second captures, 0.75-second inter-turn delay, retry for silence/blank transcription, and playback disabled.
- Exact normalized stop phrases: `stop listening`, `stop conversation`, `end conversation`, `goodbye Ares`, `goodbye`, `that is all`, and `exit conversation`.
- Stop phrases bypass Brain routing. Unsupported Brain requests remain recoverable and may continue.
- Each turn gets a child correlation ID linked to the parent session ID.
- `VoiceStageCoordinator` continues to prevent microphone/speaker and Whisper/Piper overlap.
- Ctrl+C, timeouts, limits, and fatal failures enter structured lifecycle/resource cleanup.
- Standard events contain bounded status, timing, turn, intent, and failure metadata; they omit raw speech and audio.
- Successful audio is deleted by default; `--keep-audio` retains it, while failure diagnostics may be preserved.
- Bounded multi-turn checkpoint collection: 872 tests.

Exact bounded-session commands:

```bash
git pull --ff-only origin main
python scripts/manual_verify_multi_turn_voice.py \
  --text-turn "calculate 2 + 2" \
  --text-turn "goodbye Ares" \
  --no-greeting \
  --no-closing-phrase

python scripts/manual_verify_multi_turn_voice.py \
  --interactive-text \
  --max-turns 5 \
  --max-session-seconds 180 \
  --no-greeting \
  --no-closing-phrase

python scripts/manual_verify_multi_turn_voice.py \
  --microphone-device plughw:2,0 \
  --speaker-device plughw:CARD=Device,DEV=0 \
  --auto-stop \
  --auto-calibration \
  --calibration-seconds 0.75 \
  --speech-start-rms 200 \
  --speech-continue-rms 160 \
  --silence-rms 120 \
  --silence-seconds 0.9 \
  --speech-wait-timeout 10 \
  --max-utterance-seconds 15 \
  --pre-roll-seconds 0.25 \
  --frame-ms 20 \
  --language en \
  --whisper-command external/whisper.cpp/build/bin/whisper-cli \
  --whisper-model models/whisper/ggml-base.en.bin \
  --voice-profile en_US-hfc_male-medium \
  --diagnostic-audio \
  --playback \
  --max-turns 5 \
  --max-session-seconds 180 \
  --max-consecutive-failures 3 \
  --inter-turn-delay 0.75 \
  --timeout 300
```

The fixed two-turn simulation was executed on Windows and returned calculator `Result: 4`; `goodbye Ares` was detected before Brain routing. Real Raspberry Pi auto-stop multi-turn execution remains the next owner-run hardware verification.

Adaptive voice activity calibration and robust calculator voice routing have been added on top of the original automatic end-of-speech checkpoint.

Capture behavior:

- New contracts: `VoiceActivityCaptureRequestV1` and `VoiceActivityCaptureResultV1`.
- New hardware-neutral component: `core.RmsVoiceActivityCapture`.
- The Linux ALSA adapter owns the foreground `arecord` raw-PCM subprocess and passes bounded frames to the injected detector; the detector has no ALSA or subprocess dependency.
- Default format is 16 kHz, mono, signed 16-bit PCM in 20 ms frames.
- Adaptive calibration samples 0.75 seconds of ambient audio and records mean, median, p90, peak, and robust noise-floor RMS.
- Bounded start/continue/silence thresholds, consecutive start/resume/end evidence, and `POSSIBLE_SILENCE` hangover replace the old one-threshold reset behavior.
- Sub-continue post-speech noise cannot extend capture indefinitely; isolated clicks cannot resume speech.
- Terminal silence is omitted from the final validated WAV, while pauses with consecutive resumed speech are retained.
- `POSSIBLE_SILENCE` now distinguishes the complete ordered pending block from its truly consecutive terminal-silence suffix. Resume commits the whole pending block once; completion commits everything before the suffix and trims only the suffix.
- Canonical input uses a lossless PCM-copy normalization path. Frame/sample/byte math is explicit, and the pipeline rejects unexplained assembled-to-normalized duration loss before Whisper.
- Auto-stop uses unique per-turn raw, assembled, and normalized filenames. Owner-requested diagnostics can also preserve the Whisper transcript and play each finalized WAV stage after capture stops.
- No-speech and invalid-audio results stop before Whisper, Brain, Piper, and speaker execution.
- `TranscriptNormalizationRequestV1` / `TranscriptNormalizationResultV1` preserve raw, cleaned, and normalized forms.
- Spoken arithmetic is converted deterministically into numeric/operator text before the existing IntentParser/Planner/ExecutionPipeline/CalculatorSkill path. Calculator character and AST safety validation are unchanged and `eval()` is not used.
- Exact adjacent Whisper loops beyond the configured limit are collapsed conservatively; legitimate `two plus two plus two` is preserved.
- `--fixed-duration --record-seconds 5` preserves the previous capture path as an explicit fallback.
- The manifest advertises `voice.capture.activity`, V1 contract support, one task slot, and bounded logical resource metadata.
- Original VAD checkpoint collection: 912 tests. Current collection after adaptive calibration and routing hardening: 959 tests.

Exact Raspberry Pi threshold-calibration command:

```bash
git pull --ff-only origin main
python scripts/manual_verify_voice_activity_capture.py \
  --microphone-device plughw:2,0 \
  --auto-calibration \
  --calibration-seconds 0.75 \
  --speech-start-rms 200 \
  --speech-continue-rms 160 \
  --silence-rms 120 \
  --required-speech-frames 3 \
  --required-continue-frames 3 \
  --required-silence-frames 5 \
  --silence-seconds 0.9 \
  --speech-wait-timeout 10 \
  --max-utterance-seconds 15 \
  --pre-roll-seconds 0.25 \
  --frame-ms 20 \
  --diagnostic-audio \
  --frame-debug \
  --verbose
```

The calibration script runs no output audio by default. Add `--transcribe` for local Whisper diagnostics or `--transcribe --route` for the existing local Brain route. Use the controlled single-turn script with `--playback` for an audible response.

The calibration command prints measured ambient RMS, speech RMS, peak amplitude, thresholds, selected device, exact argument-list `arecord` command, output path, duration, and stop reason. Without the optional flags it does not run Whisper or Brain, and it never runs TTS or playback. Thresholds remain hardware-specific calibration values and require owner verification on the Raspberry Pi.

Speech-to-text adapter abstraction exists.

Speech-to-text behavior:

- New model: `core.TranscriptionResult`.
- New interface: `core.SpeechToTextAdapter`.
- New safe test adapter: `core.MockSpeechToTextAdapter`.
- `TranscriptionResult` stores transcription text, status, error details, and bounded confidence values.
- `SpeechToTextAdapter` defines `transcribe(audio_chunk)`, `get_status()`, and `get_capabilities()`.
- `MockSpeechToTextAdapter` converts `AudioChunk` objects into deterministic mock text without calling Whisper, Vosk, internet services, GPT, or a real speech engine.
- The mock adapter handles successful transcription, empty audio, no transcription, low confidence, and safe adapter failure.
- `PlaceholderVoiceService`, `NullVoiceInput`, and `MockVoiceInputAdapter` accept speech-to-text adapters through dependency injection.
- Voice City can swap a future transcription implementation without changing the Brain, CoreService, skills, or current text loops.
- Tests cover success, empty audio, low confidence, adapter failure, no transcription, confidence clamping, structured status/capabilities, and Voice City injection.
- No Whisper, Vosk, wake word, hardware-specific code, real microphone access, real STT, speaker access, GPT, internet, or background listener was added.

Voice Command Router has been added.

Voice command routing behavior:

- New module: `core.VoiceCommandRouter`.
- New result model: `core.VoiceCommandRoutingResult`.
- New metrics model: `core.VoiceCommandRouterMetrics`.
- `VoiceCommandRouter.route(transcription)` accepts `TranscriptionResult` objects.
- Empty transcriptions are ignored safely.
- Low-confidence transcriptions are rejected before command handling.
- Transcription adapter failures are propagated as structured routing failures.
- Valid text routes through CoreService's `voice.text_loop` capability.
- Unknown commands return safe structured `unknown_command` results.
- Metrics track total, routed, rejected, unknown, and failed command counts.
- Routed and rejected commands emit `voice_command.routed` and `voice_command.rejected` events.
- Tests cover successful routing, empty transcription, low-confidence rejection, unknown command handling, transcription failure propagation, metrics, and event-bus publication.
- No Whisper, Vosk, GPT, wake word, internet, hardware access, real microphone, real STT, or background listener was added.

Simulated VoicePipeline has been added.

Voice pipeline behavior:

- New module: `core.VoicePipeline`.
- New result model: `core.VoicePipelineResult`.
- `VoicePipeline.run_once()` accepts audio through an injected `MicrophoneAdapter`.
- Audio is transcribed through an injected `SpeechToTextAdapter`.
- The resulting `TranscriptionResult` is passed through `VoiceCommandRouter`.
- Valid commands route through CoreService's `voice.text_loop` capability.
- Only the required city is activated; unrelated cities remain idle.
- Final response text is sent through an injected `VoiceOutputAdapter`.
- Session ids and correlation ids are preserved through stage data and all pipeline events.
- Structured events are recorded for audio captured, transcription accepted/rejected, command routed/rejected, city activated, execution completed/failed, and output produced.
- Failure at microphone, STT, routing, target city, or output stages returns a safe structured result and does not corrupt later turns in the same session.
- Tests cover successful complete command, empty audio, microphone failure, STT failure, empty transcription, low confidence, unknown command, CoreService routing failure, target city failure, output failure, reusable session state after failure, requested-city activation only, stable correlation ids, and unrelated city idleness.
- No concrete microphone/STT/output adapters were imported into the Brain or CoreService.
- No real microphone access, Whisper, Vosk, Piper, GPT, wake-word detection, internet access, background listening, daemon/service installation, or guessed RAM/CPU limits were added.

Architecture Hardening Checkpoint

This checkpoint comes after the simulated Phase 3 Voice City command pipeline and before real hardware/adapters.

Implemented:

- enforced module lifecycle
- versioned interface contracts
- capability manifests
- memory/database migrations
- health checks and adapter fallback
- measured resource budgets

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

Final integration, recovery, and safety regression checkpoint has been added.

Checkpoint behavior:

- Focused integration tests prove the complete mock voice/text route reaches VoicePipeline, VoiceCommandRouter, IntentParser, Planner, ExecutionPipeline, the selected local skill/service, and mock output.
- Read-only PC status requests route through CoreService and PCService without dangerous confirmation, arbitrary shell command use, or unrelated adapter activation.
- Confirmation-gated device actions pause before execution, reject expired/malformed/reused/wrong confirmations, and execute exactly once after valid confirmation.
- `core.ExecutionGuard` provides bounded local exactly-once tokens for confirmed destructive actions. Duplicate tokens return the recorded structured result or fail closed without executing again.
- Recovery tests cover microphone, STT, lifecycle, health, resource, execution, output, event-history, manifest, contract, disabled-city, unknown-city, cancellation, and fallback failures.
- Safety regression tests cover confirmation bypass prevention, allowlist-only app launch, disabled/incompatible module fail-closed behavior, operational event redaction, resource/task cleanup, migration compatibility, and resource-estimate wording.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, background listener, remote control, notifications, scheduler, daemon, or new product feature was added.

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

Measured resource budgets have been added.

Resource budget behavior:

- New module: `core.ResourceBudget`.
- New models: `ResourceDeclaration`, `ResourcePolicy`, `ResourceManager`, `ResourceReservation`, `ResourceDecision`, and `CancellationToken`.
- Capability manifests now include optional validated resource declarations.
- Declared resource fields include estimated RAM, CPU weight, startup cost, shutdown cost, heavy module flag, persistent module flag, inactivity timeout, maximum concurrent tasks, task priority, network requirement, hardware acceleration requirement, and safe-to-stop metadata.
- Platform profiles are `test`, `raspberry_pi_5`, `desktop`, and `future_orin`.
- The Raspberry Pi 5 profile keeps a conservative one-heavy-module policy as declared config data, not as an exact benchmark.
- CoreService now reserves capacity before lifecycle start, acquires a bounded task slot before execution, releases task slots on every path, records activity after successful execution, and releases newly created reservations after failed activation or failed execution.
- CoreService exposes `get_resource_status()`, `get_module_resource_status(name)`, `list_loaded_modules()`, `list_resource_reservations()`, `explain_activation(name)`, and `run_resource_maintenance()`.
- Idle unloading runs only during explicit maintenance ticks and never through a background timer.
- Persistent modules and active tasks are not idle-unloaded.
- Optional eviction selects only inactive, non-persistent, lower-priority, safe-to-stop modules.
- Critical modules, active tasks, unsafe-stop modules, Brain/CoreService, and dangerous actions are not automatic eviction candidates.
- Cooperative cancellation is token-based and releases task slots only when cancellation is supported.
- Observed process metrics are read-only process-level fields: uptime, CPU time, optional RSS when available, active module/task counts, loaded City count, and declared reserved RAM.
- Declared estimates are kept separate from observed process metrics.
- EventHistoryStore can record resource reservation, release, activation denial, heavy-limit, idle-unload, eviction, task-slot, cancellation, and maintenance events without transcripts, secrets, personal memory, or raw exception traces.
- Tests cover manifest resource declarations, budget rejections, heavy-module limits, task limits, idle unloading, maintenance ticks, eviction selection, cancellation, metrics, event payload safety, CoreService routing gates, VoicePipeline compatibility, and health/fallback compatibility.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, background listener, threads, Docker, remote telemetry, distributed scheduler, OS process killing, real hardware benchmarking, or exact per-module memory measurement was added.

Enforced module lifecycle has been added.

Lifecycle behavior:

- New module: `core.ModuleLifecycle`.
- New manager: `core.ModuleLifecycleManager`.
- New structured models: `LifecycleRequest`, `LifecycleResult`, `LifecycleStatus`, and `LifecycleTransition`.
- Required states: `UNLOADED`, `STARTING`, `READY`, `BUSY`, `DEGRADED`, `STOPPING`, `STOPPED`, and `FAILED`.
- Required operations: `start()`, `health_check()`, `execute(request)`, and `stop()`.
- CoreService registers every service with the lifecycle manager.
- CoreService `route_by_capability()` starts and health-checks only the selected module before execution.
- Execution is rejected unless the selected module is `READY`.
- Starting an already `READY` module and stopping an already `STOPPED` module are idempotent.
- Startup exceptions leave the module in `FAILED`.
- Health-check failures move the module to `DEGRADED` or `FAILED` according to policy.
- Execution exceptions mark only the selected module `FAILED`; unrelated modules remain usable.
- Failed/degraded modules require explicit `recover_service()` before retry.
- Inactivity policy metadata is present, but no background lifecycle timer was added.
- Lifecycle transitions record timestamps, operation names, reasons, session ids, and correlation ids.
- CoreService exposes `get_lifecycle_status()` and `get_lifecycle_history()`.
- Voice City is integrated with lifecycle gating, and the simulated VoicePipeline still passes.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, background listening, background timers, daemon/service installation, process spawning, Docker, or guessed resource limits were added.

Versioned interface contracts have been added.

Contract behavior:

- New module: `core.Contracts`.
- Current runtime version format: integer major versions such as `v1`.
- Every public request/result contract exposes `contract_name`, `contract_version`, `correlation_id`, optional `session_id`, `created_at`, and `metadata`.
- `ContractRegistry` lists known contracts, supported versions, current versions, and contract consumers.
- Unsupported major versions are rejected before execution and are not silently reinterpreted.
- Optional metadata is preserved through deterministic dictionary serialization.
- CoreService rejects unsupported core execution contracts before city lookup or activation.
- ModuleLifecycleManager rejects unsupported lifecycle contracts before state transitions.
- VoicePipeline and VoiceCommandRouter validate V1 contracts across microphone, STT, command routing, lifecycle, and CoreService execution boundaries.
- Event envelopes are versioned for future city event routing and event-history storage.
- CoreService can record compatibility rejection entries in `EventHistoryStore` when one is configured.
- Rejected contracts preserve correlation ids where available, do not activate unrelated cities, do not alter lifecycle state, and do not corrupt later VoicePipeline turns.

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

No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, background listeners, remote registries, plugin downloads, dynamic code loading, database migrations, or guessed resource limits were added.

Capability manifests have been added.

Manifest behavior:

- New module: `core.CapabilityManifest`.
- New models: `CapabilityManifest`, `ManifestDependencies`, `PlatformCompatibility`, `ManifestPolicy`, `ManifestValidationResult`, and `ProviderSelectionResult`.
- New registry: `CapabilityManifestRegistry`.
- Manifests declare module identity, module type, module version, manifest version, description, provider, enabled state, explicit capabilities, consumed and produced contract versions, dependencies, platform compatibility, permissions, lifecycle support, and metadata.
- Supported module types are `city`, `skill`, `adapter`, and `service`.
- Supported permissions are `microphone.read`, `speaker.write`, `camera.read`, `network.outbound`, `filesystem.read`, `filesystem.write`, `process.launch`, `device.control`, and `gpio.control`.
- Any permission not declared is denied by policy.
- The registry validates duplicate module names, duplicate provider/version combinations, malformed manifest versions, unknown module types, unsupported contract versions, unknown required capabilities, missing required modules, incompatible capabilities, platform mismatch, permission policy, and lifecycle declaration compatibility.
- Provider selection respects explicit preferred-provider policy and otherwise uses deterministic ordering.
- CoreService validates the selected module manifest before lifecycle start.
- Manifest rejection happens before module activation, preserves correlation ids, leaves lifecycle state unchanged, keeps unrelated cities idle, and can record `manifest.validation_failed` entries in `EventHistoryStore`.
- Voice City, mock microphone adapter, mock speech-to-text adapter, mock voice output adapter, VoiceCommandRouter, and VoiceSessionSkill have registered manifests.
- SkillRegistry now registers skill manifests from explicit skill metadata without changing skill execution behavior.
- `config/modules.example.json` documents safe local enable/disable flags, preferred providers, and allowed permissions.
- Tests cover manifest validation, dependency handling, permission policy, provider selection, skill manifest registration, CoreService manifest rejection, VoicePipeline compatibility, and CoreService usability after manifest failures.
- No real microphone access, Whisper, Vosk, Piper, wake word, GPT, internet, background listeners, automatic dependency installation, dynamic plugin loading, database migrations, runtime provider fallback, Docker, daemon installation, or guessed hardware limits were added.

Versioned memory schema migrations have been added.

Migration behavior:

- New module: `memory.schema_migrations`.
- New models: `SchemaEnvelope`, `MigrationResult`, `StoreInspectionReport`, and `MigrationError`.
- New registry: `MigrationRegistry`.
- Every active durable JSON store now uses an envelope with `schema_name`, `schema_version`, `created_at`, `updated_at`, `data`, and optional `metadata`.
- Active durable schemas are `ares.user_profile`, `ares.owner_profile`, `ares.goals`, `ares.notes`, `ares.tasks`, `ares.memory.short`, `ares.memory.long`, and `ares.event_history`.
- Durable identity/memory stores are user profile, explicit owner profile, short/long memory, goals, notes, and tasks.
- Operational history is event history.
- `ReminderScheduler` has no separate persistence; it derives due/upcoming reminders from tasks.
- Voice session persistence is event-history based; there is no separate voice-session store.
- Disposable caches are not migrated as identity data.
- Configuration files are treated as configuration-backed durable state, not owner memory.
- Legacy `memory_manager.py` and `memory/memories.json` are documented as legacy/disconnected from the active `MemoryStore` path.
- Known legacy unversioned JSON formats import into v1 only when the target store structure matches exactly.
- Most production schemas remain at v1. `ares.owner_profile` is now at v3 through its registered preserving v1 -> v2 -> v3 migration.
- A controlled test fixture also demonstrates generic v1 -> v2 migration support without inventing destructive changes for other production schemas.
- Migrations validate before migration and after every step.
- Migration writes create local backups under `.migration_backups`, write temporary files, flush safely where practical, atomically replace where practical, and verify the final file can be loaded.
- Invalid JSON, truncated files, malformed envelopes, wrong root types, wrong schema names, future schema versions, downgrade attempts, missing migration paths, failed migration steps, post-migration validation failure, and concurrent write attempts fail closed without resetting memories.
- Store load failures publish `storage.migration_failed` on the configured event bus where available.
- Migration failure can be recorded in `EventHistoryStore` via `record_migration_failure`.
- Inspection reports show store path, schema name, detected version, target version, migration needed, path, latest backup, and validation state without dumping personal memory contents.
- Tests use temporary directories only and do not touch real user data.
- No real microphone access, Whisper, Vosk, Piper, wake word, GPT, internet, remote databases, cloud synchronization, distributed locking, PostgreSQL, Docker, automatic cloud backup, health fallback, or guessed resource limits were added.

Health checks and controlled adapter fallback have been added.

Health/fallback behavior:

- New module: `core.Health`.
- New common model: `HealthResult`.
- New policy/config models: `HealthPolicyConfig`, `AdapterCandidate`, `AdapterFallbackPolicy`, and `FallbackExecutionResult`.
- New local resilience helpers: `CircuitBreaker` and `HealthCache`.
- Health statuses are `healthy`, `degraded`, `unavailable`, `failed`, `disabled`, and `unknown`.
- Retry safety is explicit: `retry_safe`, `retry_unsafe`, or `unknown`.
- CoreService exposes read-only health visibility with `get_service_health(name)`, `list_service_health()`, and `get_capability_health(capability)`.
- Lazy health visibility reports manifest, lifecycle, and city state without activating every City.
- Active health probes are explicit and call only safe health/status methods.
- Adapter fallback checks enabled state, declared capability, interface version, health status, degraded-mode policy, and circuit state before selection.
- Degraded adapters are used only when policy explicitly allows them.
- Runtime fallback is bounded by `max_fallback_attempts` and allowed only for explicitly retry-safe operations.
- Runtime fallback preserves the original failure in the execution result/history.
- Circuit breaker states are `closed`, `open`, and `half_open`, with deterministic clock injection for tests and no background timer.
- Health cache supports TTL reuse, expiration, forced refresh, and disabled-adapter invalidation.
- `MockMicrophoneAdapter`, `MockSpeechToTextAdapter`, `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, `ToolAdapter`, `PCService`, and `PlaceholderVoiceService` expose safe health checks or status-backed health behavior.
- `VoicePipeline` can use optional candidate lists for mock microphone selection and mock STT fallback while preserving its default single-adapter path.
- EventHistoryStore can record safe operational events for health check failures, fallback selections, all-unavailable decisions, circuit opened, half-open probes, and circuit recovery.
- Tests cover health normalization, selection, rejection reasons, degraded policy, bounded fallback attempts, retry-safety, circuit breaker transitions, cache behavior, event-history safety, CoreService lazy health visibility, VoicePipeline compatibility, and STT fallback.
- No real microphone, Whisper, Vosk, Piper, wake word, GPT, internet, real weather/market calls, notifications, automatic PC actions, background listeners, threads, Docker, remote telemetry, distributed scheduler, operating-system process killing, real hardware benchmarking, or exact per-module memory measurement was added.

Phase 3 Voice Checkpoint

This checkpoint is documentation-only and freezes the current Voice City foundation before real audio work.

Checkpoint status:

- Test count: 391 tests
- Voice City skeleton
- Audio adapter contracts
- Single-turn loop
- Multi-turn mock session
- `VoiceSessionSkill`
- Voice session event logging
- Voice session status query

No real microphone access, speaker output, wake word detection, real STT, real TTS, background listener, notifications, GPT, internet access, or real device/event automation was added for this checkpoint.

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
Real voice/audio work has not started. Voice City currently has safe placeholder service contracts, mock/local audio adapter contracts, and a manual text loop simulation.

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
- Context-aware planner
- Action confirmation layer
- Adapter config and SecretsGuard
- RealWeatherAdapter skeleton
- RealWeatherAdapter HTTP logic
- RealMarketAdapter skeleton
- ExecutionPipeline
- ToolChain
- ToolAdapter
- WeatherSkill
- MarketSkill
- CalendarSkill
- DeviceAction framework
- DeviceActionSkill
- CoreService
- Confirmed Windows lock device action
- Confirmed Windows sleep device action
- Device app launcher skeleton
- Confirmed Windows app launcher
- Manual calculator launch verification script
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
- Currently routes `TimeDateSkill`, `MemoryRecallSkill`, `OwnerMemorySkill`, `CalculatorSkill`, `GoalsSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, `MarketSkill`, `CalendarSkill`, and `DeviceActionSkill`.

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
- Reads `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` through existing safe interfaces when those stores are injected.
- Resolves context requests such as `remind me about my main goal tomorrow`, `what should I do next for my goals`, and `show my goals and notes about gym`.
- Returns deterministic empty-context responses when required local context is missing.
- Serializes plans and steps for tests, events, and REPL display.
- Planner never executes skills.
- Planner does not read data files directly.
- Planner does not write memory, goals, notes, or tasks.
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
- Pauses before destructive or important actions and returns a confirmation request.
- Executes conversation memory steps through MemoryStore.
- Executes internal `planner_context` response steps for deterministic context-only answers.
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

Context-aware Planner foundation has been added.

New context-aware behavior:

- `SkillManager` injects its existing `UserProfileStore`, `GoalsStore`, `NotesStore`, and `TasksStore` handles into `ToolSelector`'s Planner.
- Planner reads context only through public store methods such as `get_favorite`, `list`, and `search`.
- Planner resolves main-goal reminders into task steps when goal context exists.
- Planner resolves favorite-profile reminders into task steps when profile context exists.
- Planner resolves notes-about requests into note search steps when matching notes exist.
- Planner answers next-goal questions from related open tasks, milestones, or deterministic fallback guidance.
- Missing context returns safe local `planner_context` responses instead of direct file access, GPT, or external calls.
- REPL integration verifies `what should I do next for my goals?` through the live path.
- No GPT, internet access, real APIs, voice, notifications, or background automation were added.

Action Confirmation Layer has been added.

New confirmation modules:

- `core.ConfirmationRequest`
- `core.ConfirmationDecision`
- `core.ConfirmationManager`

Confirmation behavior:

- `ExecutionPipeline` pauses before destructive or important `PlanStep` actions.
- One pending confirmation id is kept in memory for the active runtime.
- `SkillManager` handles `yes` and `confirm` as approval for the pending request.
- `SkillManager` handles `no` and `cancel` as cancellation.
- Missing pending confirmation returns a safe local failure message.
- Confirmed actions rerun with a confirmation-approved marker so they do not ask again.
- Multi-step plans stop safely at the confirmation step and do not run later steps until the user explicitly confirms the protected action.
- Protected actions include note deletion, delete-all notes, task deletion, clear-completed tasks, goal delete/pause/complete, and future external adapter write/delete actions.
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

- `core.ExternalAdapterConfig`
- `core.SecretsGuard`
- `core.SecretValidationError`
- `core.ToolAdapter`
- `core.ToolRequest`
- `core.ToolResponse`
- `core.ToolAdapterRegistry`
- `core.MockWeatherAdapter`
- `core.RealWeatherAdapter`
- `core.MockMarketAdapter`
- `core.RealMarketAdapter`
- `core.MockCalendarAdapter`

ToolAdapter behavior:

- Validates future adapter config before execution.
- Tracks adapter enabled state, mode, API key environment variable name, base URL, and timeout.
- Rejects raw-looking secrets in adapter config payloads.
- Registers and looks up local adapters by name.
- Finds adapters by capability.
- Exposes adapter metadata: name, description, capabilities, `requires_network`, `requires_auth`, and `supports_real_mode`.
- Returns clear missing-adapter and unsupported-capability responses.
- Provides offline mock weather, market, and calendar adapters for tests only.
- Provides a real-weather-capable adapter that runs HTTP only after explicit real-mode config and env-key gates pass.
- Provides a real-market-capable adapter that runs HTTP only after explicit real-mode config and env-key gates pass.
- Planner accepts an optional ToolAdapterRegistry for future adapter-aware planning.
- ExecutionPipeline can execute explicit `tool_adapter` PlanSteps through an injected registry.
- No default real APIs, API keys, GPT, voice, calendar integration, web adapter, or ungated network calls were added.

Device Action Framework skeleton has been added.

New device action modules:

- `core.DeviceAction`
- `core.AppLaunchConfig`
- `core.AppAllowlistLoader`
- `core.AppAllowlistConfigError`
- `core.DeviceActionResult`
- `core.DeviceActionSafetyDecision`
- `core.DeviceActionConfirmationRequest`
- `core.DeviceActionRegistry`
- `core.LocalDeviceActionAdapter`

Device action behavior:

- Provides stable local action metadata and execution result models.
- Registers safe local actions by name.
- Returns safe failures for unknown actions.
- Exposes safe built-in actions: `echo`, `system_status_mock`, `list_actions`, and `list_apps`.
- Exposes confirmation-gated real actions: `lock_pc` and `sleep_pc`.
- Exposes confirmation-gated Windows app launcher action: `open_app`.
- Classifies device actions as `safe`, `confirmation_required`, or `forbidden`.
- Marks shutdown and restart as confirmation-required placeholders.
- Marks `lock_pc` as confirmation-required and implemented only after approval.
- Marks `sleep_pc` as confirmation-required and implemented only after approval.
- Marks `open_app` as confirmation-required and implemented only as an enabled allowlist Windows launcher after approval.
- Marks run command, delete, and arbitrary shell as forbidden placeholders.
- Returns stable confirmation-required or forbidden results without executing those actions.
- `system_status_mock` returns deterministic mock data and does not inspect the host.
- Future dangerous actions must require explicit confirmation before execution.
- No shutdown/restart, arbitrary app launching, arbitrary shell command execution, Telegram, voice, internet, GPT, remote control, notifications, or dangerous device automation was added.

DeviceActionSkill safe live path has been added.

New skill module:

- `skills.builtin.DeviceActionSkill`

Live path behavior:

- IntentParser recognizes `device_action` intents for safe device commands and unsafe device phrases.
- ToolSelector selects DeviceActionSkill through structured intent matching.
- Planner creates `device_action` plan steps.
- ExecutionPipeline executes the plan through the registered DeviceActionSkill.
- SkillManager carries `LocalDeviceActionAdapter` through SkillContext.
- Text REPL can execute safe device actions through the normal router flow.
- Supported commands are `echo <text>`, `list device actions`, `list apps`, `system status`, and confirmation-gated `lock pc`/`sleep pc`/`open app <app_id>`.
- Shutdown, restart, and unapproved `lock_pc`/`sleep_pc`/`open_app` return stable confirmation-required responses.
- Run command, delete, and arbitrary shell return stable forbidden responses.
- Confirmation-required device actions are never executed directly unless they are explicitly implemented and confirmed; currently `lock_pc`, `sleep_pc`, and allowlisted Windows `open_app` meet that rule.
- Forbidden device actions are never executed.
- No shutdown/restart, arbitrary app launching, arbitrary shell, Telegram, voice, internet, GPT, remote access, notifications, or background jobs were added.

Device dangerous-action confirmation gate has been added.

Danger gate behavior:

- Device action danger classification is centralized in `core.DeviceAction`.
- `DeviceActionSkill` uses the classification before calling `LocalDeviceActionAdapter`.
- Confirmation-required results include a stable `device-action-confirmation:<action>` token.
- Confirmation-required metadata records that the action was not executed.
- Forbidden metadata records that the action was not executed.
- Planner preserves the classification fields on `device_action` plan steps.
- The text REPL displays confirmation-required and forbidden responses safely through the normal assistant path.
- No real OS commands, shutdown/restart, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, or background jobs were added.

Confirmed Windows lock device action has been added.

Lock behavior:

- `lock_pc` is the first real OS-backed device action.
- `lock_pc` requires explicit confirmation through `core.ConfirmationManager`.
- `ExecutionPipeline` pauses the `lock_pc` step before execution and stores a pending confirmation.
- `yes` or `confirm` reruns the step with `confirmation_approved`.
- `LocalDeviceActionAdapter` calls the Windows lock implementation only after confirmation approval.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows lock implementation; no test locks the workstation.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; mocked app launching arrived later in v1.30 and confirmed allowlisted Windows app launching arrived later in v1.31.

Confirmed Windows sleep device action has been added.

Sleep behavior:

- `sleep_pc` is the second real OS-backed device action.
- `sleep_pc` requires explicit confirmation through `core.ConfirmationManager`.
- `ExecutionPipeline` pauses the `sleep_pc` step before execution and stores a pending confirmation.
- `yes` or `confirm` reruns the step with `confirmation_approved`.
- `LocalDeviceActionAdapter` calls the Windows sleep implementation only after confirmation approval.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows sleep implementation; no test puts the workstation to sleep.
- Shutdown, restart, arbitrary app launching, run command, delete, arbitrary shell, Telegram, voice, GPT, internet, notifications, remote access, and background jobs were not added; mocked app launching arrived later in v1.30 and confirmed allowlisted Windows app launching arrived later in v1.31.

Device app launcher skeleton has been added.

App launcher behavior:

- `AppLaunchConfig` models app allowlist entries with `app_id`, `display_name`, `command_placeholder`, `enabled`, and `requires_confirmation`.
- `LocalDeviceActionAdapter` lists allowlisted apps with the safe `list_apps` action.
- `open_app <app_id>` requires explicit confirmation before execution.
- In this skeleton phase, confirmed `open_app` calls only the injected mocked launcher for enabled allowlisted apps.
- Unknown app ids and disabled app ids are rejected before the launcher callback is called.
- Command-like app ids are normalized and rejected if they are not allowlisted.
- IntentParser, Planner, ExecutionPipeline, SkillManager, DeviceActionSkill, and the text REPL all preserve the app id and confirmation gate.
- No real apps were launched in this skeleton phase, and no arbitrary shell commands, shutdown/restart/delete, Telegram, voice, GPT, internet, remote access, notifications, or background jobs were added.

Confirmed Windows app launcher has been added.

Confirmed app launcher behavior:

- `open_app <app_id>` still requires explicit confirmation through `core.ConfirmationManager`.
- Built-in allowlist examples originally started disabled by default: `notepad`, `calculator`, and `browser`.
- Confirmed `open_app` launches only enabled apps from the allowlist config.
- User input can only select an allowlisted `app_id`; user-provided paths and shell-like app ids are rejected.
- The Windows launcher uses the configured allowlist command with `shell=False`.
- Non-Windows platforms return a safe unsupported response.
- Tests mock the Windows launcher and do not open real apps.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

App launcher allowlist config has been added.

Allowlist config behavior:

- `config/apps.json` stores approved app definitions outside runtime code.
- The tracked examples originally started disabled by default: `notepad`, `calculator`, and `browser`.
- `AppAllowlistLoader` validates required `app_id`, `display_name`, command/path, `enabled`, and `requires_confirmation` fields.
- Invalid config, missing required fields, non-boolean flags, and duplicate normalized app ids fail safely before the adapter builds its runtime allowlist.
- `LocalDeviceActionAdapter` loads the config-backed allowlist by default and still accepts injected test allowlists for isolated tests.
- Confirmed `open_app` uses only the configured allowlist command; user-supplied command or path parameters are ignored and never become launch commands.
- Tests cover valid config loading, invalid config rejection, duplicate app id rejection, disabled and unknown app rejection, confirmed enabled launch through a mocked launcher, and user-supplied path isolation.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

Calculator app allowlist enablement has been added.

Calculator app behavior:

- `calculator` is the only enabled app in `config/apps.json`.
- `notepad` and `browser` remain disabled and fail safely before launch.
- `open_app calculator` still requires explicit confirmation through `core.ConfirmationManager`.
- Confirmed calculator launch uses only the existing safe Windows launcher path and configured allowlist command.
- User-supplied paths and shell-like app ids remain rejected.
- Unknown apps and disabled apps fail safely.
- Non-Windows platforms return unsupported safely.
- Tests mock the launcher and do not open Calculator during verification.
- No shutdown, restart, delete, Telegram, voice, GPT, internet, remote access, notifications, arbitrary shell commands, or arbitrary app launching was added.

Manual calculator launch verification has been added.

Manual verification behavior:

- New script: `scripts/manual_verify_calculator_launch.py`.
- The script prints a warning before any action.
- The script shows `App id: calculator`.
- It requires the exact typed confirmation `YES_OPEN_CALCULATOR`.
- It refuses every other input and does not build the device action adapter on refusal.
- After exact confirmation, it calls `LocalDeviceActionAdapter.execute("open_app", {"app_id": "calculator", "confirmation_approved": True})`.
- Tests inject a fake adapter and never open Calculator.
- No new apps, shutdown, restart, delete, shell commands, Telegram, voice, GPT, internet, remote access, or notifications were added.

PCService abstraction has been added.

PC service behavior:

- New module: `core/PCService.py`.
- `PCService` defines the dedicated interface for future PC operations: `lock()`, `sleep()`, `open_app(app_id)`, and `status()`.
- `PCServiceResult` defines the service-layer result shape.
- `PCStatus` defines the structured safe status object.
- `PCCapabilities` defines the structured safe capability object.
- `WindowsPCService` contains the Windows-specific implementation behind the service boundary.
- `LocalDeviceActionAdapter` delegates status, lock, sleep, and open-app behavior through PCService instead of directly calling Windows helpers.
- Existing lock, sleep, app launcher, confirmation, app allowlist, and test injection behavior remains compatible.
- Tests verify DeviceAction delegates status, lock, sleep, and open-app calls through PCService.
- No new device actions, shutdown, restart, delete, shell commands, Telegram, voice, GPT, internet, remote access, notifications, or behavior changes were added.

PCService status provider has been added.

Status provider behavior:

- `PCService.get_status()` returns structured safe status through `PCStatus`.
- Safe fields include operating system, hostname, current user, Python version, optional uptime, and available actions.
- `PCService.status()` remains a compatibility wrapper around `get_status()`.
- DeviceAction `system status` obtains status data only through PCService.
- Tests cover direct PCService status structure, the compatibility wrapper, DeviceAction status routing, and SkillManager pipeline status output.
- No network access, hardware telemetry, process enumeration, remote control, internet, GPT, voice, or new device actions were added.

PCService capability discovery has been added.

Capability discovery behavior:

- `PCService.get_capabilities()` returns structured safe capability data through `PCCapabilities`.
- Capability data includes supported device actions, supported applications, available status providers, available services, and explicit safeguards.
- DeviceAction `list device actions` and `list apps` obtain discovery data through PCService instead of direct hardcoded lists.
- Tests cover direct PCService capability structure, DeviceAction action discovery through PCService, and DeviceAction app discovery through PCService.
- No internet, GPT, network, remote execution, process enumeration, hardware telemetry, or new device actions were added.

CoreService orchestration layer has been added.

New orchestration modules:

- `core.CoreService`
- `core.CoreServiceResult`

CoreService behavior:

- Owns local/external service registration.
- Registers `PCService` as the `pc` service by default.
- Exposes `get_service(name)` for service lookup.
- Exposes `list_services()` for stable service metadata.
- Exposes `get_capabilities()` to aggregate capability data from every registered service.
- `SkillManager` carries the active CoreService for device action paths.
- `LocalDeviceActionAdapter` obtains PCService through CoreService where practical.
- Tests cover default PCService registration, service lookup, service listing, capability aggregation, capability failure reporting, DeviceAction discovery through CoreService, and SkillManager/CoreService handoff.
- No behavior changes, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Phase 2 architecture stabilization has been completed.

Phase 2 Complete architecture baseline:

- `PC_SERVICE_NAME` centralizes the default CoreService PC registration key.
- `SkillManager` carries the active `CoreService`.
- `SkillContext` now exposes that same `core_service` reference for consistent Brain-to-service access.
- `CoreService` remains the service registration, lookup, listing, and capability aggregation boundary.
- `PCService` remains the dedicated local PC operation/status/capability boundary.
- Services that participate in CoreService capability aggregation expose `get_capabilities()`.
- PC services expose `get_status()` and the compatibility `status()` wrapper.
- Missing service capability interfaces fail safely through CoreService capability aggregation.
- Tests cover default PCService status/capability interfaces, safe missing-capability reporting, and SkillManager/CoreService context propagation.
- No behavior changes, new functionality, new cities, GPT, internet, network calls, remote execution, hardware additions, or new device actions were added.

Permanent architecture reference has been created in `docs/ARCHITECTURE.md`.

Architecture reference contents:

- ARES Philosophy centered on the Brain as the stable identity layer.
- Capital City Architecture with Brain -> CoreService -> Registered Services -> Skills -> Adapters -> Devices.
- Current Services covering CoreService and PCService responsibilities.
- Device Action Pipeline from Brain to Skill to DeviceAction to PCService to Windows.
- Capability Discovery through `get_capabilities()`.
- Future City placeholders for Voice, Vision, Weather, Stocks, Codex, Home, and Robot cities.
- Upgrade Philosophy for replacing hardware, speech engines, AI models, cameras, and providers without changing the Brain.
- Design Rules for service boundaries, structured data, discovery, small modules, and tests before merge.
- Long-term Vision for ARES growing from Raspberry Pi into a humanoid robot without losing identity.
- This was documentation-only and did not add runtime behavior.

Voice City foundation has been added.

Voice City skeleton behavior:

- `core.VoiceService` defines the Voice City service interface.
- `core.PlaceholderVoiceService` provides safe placeholder status and capability responses.
- `core.VoiceStatus` reports that audio hardware access, microphone, speaker, background listening, internet, and GPT are disabled, and STT/TTS/wake word are not configured.
- `core.VoiceCapabilities` reports no supported voice actions, no input modes, no output modes, and explicit safeguards for microphone, speaker, STT, TTS, wake word, background listening, internet, and GPT.
- `CoreService` registers the placeholder VoiceService as `voice` by default alongside `pc`.
- CoreService capability aggregation now includes both PCService and VoiceService by default.
- Tests cover VoiceService registration through CoreService, capabilities, placeholder status, PC plus Voice capability aggregation, and no audio hardware access.
- No real microphone, speaker, Whisper, Vosk, Piper, STT, TTS, wake word, GPT, internet, or background listening was added.

Voice City STT/TTS contracts have been added.

Voice input/output contract behavior:

- `core.VoiceInput` defines `listen_once()`, `get_status()`, and `get_capabilities()`.
- `core.VoiceOutput` defines `speak(text)`, `get_status()`, and `get_capabilities()`.
- `core.NullVoiceInput` returns a safe placeholder listen result with no microphone access, no STT, no wake word, and no background listening.
- `core.NullVoiceOutput` accepts text as a safe placeholder and performs no speaker output or TTS.
- `PlaceholderVoiceService` owns the input/output components.
- VoiceService status includes input and output status blocks.
- VoiceService capabilities expose `voice_input: placeholder` and `voice_output: placeholder` plus component capability data.
- CoreService aggregation includes the updated VoiceService capability shape.
- Tests cover component ownership, NullVoiceInput placeholder behavior, NullVoiceOutput placeholder behavior, VoiceService status aggregation, CoreService capability aggregation, and no audio hardware access.
- No real microphone, speaker, Whisper, Vosk, Piper, STT, TTS, wake word, GPT, internet, or background listening was added.

Voice City text loop foundation has been added.

Voice loop behavior:

- `core.VoiceLoop` defines a one-shot Voice City text loop.
- `VoiceLoop.run_once()` calls `VoiceInput.listen_once()` once.
- Empty or missing input returns a safe no-input result and does not call the text handler.
- Recognized text is passed to an injected existing text/planner/execution handler.
- Final response text is passed to `VoiceOutput.speak(text)`.
- Default components remain `NullVoiceInput` and `NullVoiceOutput`.
- Safe failures are returned for input, handler, and output errors with explicit error messages.
- Tests cover empty input, recognized text reaching a mocked planner/execution handler, output text reaching `NullVoiceOutput`, and safe error paths.
- No microphone, speaker, wake word, background loop, real STT, real TTS, GPT, internet, new skills, or behavior changes outside Voice City were added.

Manual Voice City text simulation has been added.

Manual voice text simulation behavior:

- New script: `scripts/manual_voice_text_loop.py`.
- The script is owner-run and text-only.
- It prints a warning that no microphone or speaker will be used.
- It accepts one typed input line.
- It wraps that line in a safe typed-text `VoiceInput`.
- It passes the text through `VoiceLoop`.
- Non-empty text uses the existing local SkillManager planner/execution path.
- The final response is printed as text.
- Output uses `NullVoiceOutput`.
- Empty input exits safely.
- Tests cover import safety, mocked VoiceLoop handoff, real local calculator routing through the existing path, empty input behavior, and no audio hardware access.
- No microphone, speaker, wake word, background loop, real STT, real TTS, GPT, internet, real audio, or unconfirmed device action execution was added.

City lifecycle and lazy capability routing have been added.

City lifecycle behavior:

- CoreService tracks city states: `idle`, `active`, `failed`, and `disabled`.
- CoreService service metadata includes city status and registered capabilities.
- `CoreService.route_by_capability()` routes one request to one matching idle city.
- Only the selected city becomes active during the route.
- Unused cities remain idle and are not called.
- Disabled cities are skipped.
- A failed route marks only the selected city as `failed`.
- `CoreService.get_capabilities()` includes capability registry metadata and city status data.
- Event Bus city activation is documented as future-only. No event-driven city wakeup runtime was added.
- Tests prove unused cities are not called, disabled cities are not routed, and failed routes are reported safely.
- No real audio, GPT, internet, new APIs, external calls, notifications, or background activation was added.

Internal Core EventBus skeleton has been added.

Internal event bus behavior:

- New module: `core.EventBus`.
- `core.Event` stores source, type, priority, payload, and timestamp.
- Supported priorities are `low`, `normal`, `high`, and `critical`.
- `EventBus.publish()` creates an event and dispatches to subscribers for that event type.
- `EventBus.subscribe()` registers in-process handlers and returns an unsubscribe callback.
- Publishing without subscribers is safe.
- Event history is returned in priority order.
- This is future-use infrastructure for cities reporting important events without Brain polling.
- No real camera, notifications, background daemon, internet, GPT, new APIs, or event-driven city activation was added.

CoreService Event Bus integration has been added.

Internal event decision behavior:

- `CoreService.handle_event(event)` receives internal `core.Event` objects.
- Event decisions are stable: `ignored`, `recorded`, and `escalated`.
- Low and normal priority events are recorded only.
- High and critical priority events are marked escalated.
- Unknown source events fail safely with an ignored decision.
- Disabled source events fail safely with an ignored decision.
- `CoreService.event_decisions()` returns recorded/escalated event decisions and can filter by decision.
- This is internal routing metadata only.
- No notifications, background listeners, real devices, internet, GPT, new APIs, or daemon behavior was added.

Local Event History Store has been added.

Internal event history behavior:

- New module: `events.EventHistoryStore`.
- New record model: `EventHistoryRecord`.
- Event decisions/results are persisted locally to `data/event_history.json` by default.
- `data/event_history.json` is ignored by git.
- Stored records include source, type, priority, decision, event data, result data, and timestamp.
- Recent history can be queried by source, type, and priority.
- Stored history is bounded by a configurable max-record limit.
- The store does not subscribe to events, start listeners, send notifications, run devices, call GPT, or access the internet.
- This is internal memory/logging only.

CoreService Event History persistence has been added.

Internal event persistence behavior:

- `CoreService` accepts an optional `EventHistoryStore`.
- `CoreService.handle_event(event)` stores handled decisions/results when the store is configured.
- Low and normal priority events are stored as `recorded`.
- High and critical priority events are stored as `escalated`.
- Unknown source events are stored as safe `ignored` records.
- Disabled source events are stored as safe `ignored` records.
- The `failed` decision value is available for future failed event-handling paths.
- This is synchronous internal memory/logging only.
- No notifications, background daemon, real devices, GPT, internet, new APIs, or external calls were added.

Event History Skill has been added.

Read-only event history query behavior:

- New skill: `skills.EventHistorySkill`.
- Skill name and planner target: `event_history`.
- Supported phrases are `what happened recently`, `show recent events`, and `show critical events`.
- `IntentParser` recognizes event-history requests.
- `Planner` creates read-only `event_history` steps.
- `SkillManager`, `ExecutionPipeline`, and the text REPL route these requests through the normal live path.
- The skill reads `EventHistoryStore` through `SkillContext.event_history_store`.
- Empty history returns a safe local response.
- No notifications, background daemon, real devices, GPT, internet, new APIs, external calls, or write actions were added.

Phase 3 foundation checkpoint has been documented.

Checkpoint status:

- Current version: ARES v1.52 - Phase 3 Foundation Checkpoint.
- Checkpoint pytest collection before audio adapter contracts: 351 tests.
- Implemented and verified foundation: Voice City skeleton, manual Voice City text loop simulation, lazy city routing, internal `core.EventBus`, local `events.EventHistoryStore`, and read-only `skills.EventHistorySkill`.
- This checkpoint is documentation-only and made no runtime changes.
- Real microphone access, speaker output, wake word detection, real STT, real TTS, background listening, notifications, GPT, internet access, and real device/event automation remain disabled until explicitly approved.

Voice City audio adapter contracts have been added.

Voice adapter behavior:

- New interface: `VoiceInputAdapter`.
- New interface: `VoiceOutputAdapter`.
- New safe input adapter: `MockVoiceInputAdapter`.
- New safe output adapter: `MockVoiceOutputAdapter`.
- `NullVoiceInput` and `NullVoiceOutput` now delegate through adapters while preserving placeholder no-audio behavior.
- `PlaceholderVoiceService` accepts injected input/output adapters for test and future provider wiring.
- Manual Voice City text simulation uses `MockVoiceInputAdapter` for typed text and still uses `NullVoiceOutput`.
- `VoiceLoop` reports adapter failures safely and still ignores empty input safely.
- Tests cover input capture, output speak, empty input, adapter injection, adapter failure, and no audio hardware access.
- Phase pytest collection at this point was 357 tests.
- Real Whisper, Vosk, Piper, microphone, speaker, wake word, background listener, GPT, internet, and real audio hardware access remain future work.

Voice City adapter-backed single-turn loop has been added.

Single-turn loop behavior:

- New model: `VoiceTextRequest`.
- New loop: `VoiceSingleTurnLoop`.
- `VoiceInputAdapter.capture()` is now the explicit one-turn capture entry point; `capture_input()` remains compatible.
- Flow: `MockVoiceInputAdapter.capture()` -> `VoiceTextRequest` -> injected existing text/CoreService handler -> `MockVoiceOutputAdapter.speak(response)`.
- Normal one-turn mock input/output returns a completed result.
- Empty input returns a safe no-op.
- Input adapter failures fail safely before text handling.
- Output adapter failures fail safely after response generation.
- Tests cover normal one-turn input/output, empty input, input adapter failure, output adapter failure, and no real microphone/speaker access.
- Phase pytest collection at this point: 370 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Voice City multi-turn mock session has been added.

Multi-turn session behavior:

- New loop: `VoiceSessionLoop`.
- New result model: `VoiceSessionResult`.
- New turn model: `VoiceSessionTurn`.
- Sessions process queued mock inputs in sequence through the existing adapter-backed VoiceLoop path.
- `max_turns` bounds every session.
- Stop phrases are `stop`, `exit`, and `goodbye`.
- Empty inputs are recorded as safe no-op turns.
- Session results include structured turns, transcript, and history output.
- Input adapter failures stop the session safely before text handling.
- Output adapter failures stop the session safely after response generation.
- Tests cover multi-turn flow, stop phrase handling, max-turn limiting, empty input handling, input failure, output failure, and no real microphone/speaker access.
- Phase pytest collection at this point: 370 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Voice Session Skill has been added.

Voice session skill behavior:

- New skill: `skills.VoiceSessionSkill`.
- Supported phrases: `start voice session`, `start mock voice`, and `run voice test`.
- `IntentParser` emits `voice_session` intents.
- `Planner` creates `voice_session.start` steps.
- `ToolSelector`, `ExecutionPipeline`, `SkillManager`, and the text REPL route the command through the normal live skill path.
- The skill uses only `MockVoiceInputAdapter`, `MockVoiceOutputAdapter`, and `VoiceSessionLoop`.
- Sessions are bounded by `max_turns`, capped at the existing safe session limit.
- Responses include transcript summaries for normal starts, stop phrases, max-turn limits, and empty sessions.
- Tests cover parser phrases, max-turn entity extraction, planner steps, ToolSelector routing, direct skill start, stop phrase handling, max-turn limiting, empty session behavior, and SkillManager live-path execution.
- Phase pytest collection at this point: 379 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, or real audio hardware access was added.

Voice Session event logging has been added.

Voice session event logging behavior:

- `VoiceSessionSkill` writes safe local operational events to `EventHistoryStore` when `SkillContext.event_history_store` is present.
- Recorded event types are `voice_session.started`, `voice_session.stopped`, `voice_session.adapter_failure`, and `voice_session.max_turns_reached`.
- Adapter failures are recorded as high-priority escalated records for local history only.
- Event payloads include status, turn counts, max-turn limit, success state, and adapter failure details.
- Event payloads do not store mock transcript content.
- `EventHistorySkill` can show these events through `show recent events`.
- Tests cover start logging, stop logging, adapter failure logging, max-turn logging, live SkillManager logging, and EventHistorySkill display.
- Phase pytest collection at this point: 384 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, notifications, or real audio hardware access was added.

Voice Session status query has been added.

Voice session status behavior:

- Supported phrases: `what happened in voice session`, `show last voice session`, and `voice session status`.
- `IntentParser` emits a `voice_session` intent with `action=status`.
- `Planner` creates a `voice_session.status` step.
- `VoiceSessionSkill` reads the latest local `voice_session.*` event group from `EventHistoryStore`.
- The response summarizes whether the latest mock voice session started, stopped, failed, or reached max turns.
- No-session queries return `No voice session events found.`
- Tests cover no session, normal stopped session, failed session, max-turn session, parser routing, planner routing, and SkillManager live path.
- Phase pytest collection at this point was 391 tests.
- No microphone, speaker, wake word, background listener, real STT, real TTS, GPT, internet, notifications, or real audio hardware access was added.

ARES Behavior Schematic has been documented in README and `docs/ARCHITECTURE.md`.

Behavior schematic summary:

- Capital = Brain identity.
- City Hall = CoreService.
- Cities = major services.
- Districts = sub-services.
- Villages = adapters.
- Houses = concrete devices, APIs, files, models.
- Brain stores identity-level knowledge only: long-term memory, short-term context, user profile, known people and friends, learned preferences, goals, personality, relationship history, and decision history.
- Replaceable services own implementation details, such as Weather City for weather APIs, Voice City for STT/TTS/wake word, Vision City for camera and face recognition, PC City for Windows/device actions, Codex City for GitHub/testing, and Home City for smart plugs and home devices.
- Examples document that the Brain can remember "Gabriel wants morning weather reports" while Weather City chooses the provider, and the Brain can remember "Andrei is a known friend" while Vision City handles face embeddings and matching.
- Design rule added: the Brain must never directly know API keys, Windows commands, camera internals, model internals, hardware commands, or provider-specific parsing.
- This was documentation-only and did not add GPT, internet, voice, camera, notifications, background listening, or runtime behavior.

External Adapter Config and SecretsGuard foundation has been added.

New config modules and files:

- `core.AdapterConfig`
- `core.ExternalAdapterConfig`
- `core.SecretsGuard`
- `core.SecretValidationError`
- `config/adapters.example.json`

Adapter config behavior:

- Config fields include `enabled`, `mode`, `api_key_env_name`, `base_url`, and `timeout_seconds`.
- Supported modes are `mock`, `local`, and `real`.
- `ToolAdapterRegistry` enforces config before adapter execution.
- Mock/local mode preserves existing offline WeatherSkill, MarketSkill, and CalendarSkill behavior.
- Real mode fails safely when an env key is missing, when the env-key name is only a placeholder, or when real execution is not implemented.
- Fake placeholders are allowed only as placeholders.
- Raw-looking API keys and tokens are rejected.
- Local/private adapter config files are ignored by git.
- No real API keys, real API calls, internet access, GPT, voice, notifications, or background automation were added.

Real Weather Adapter skeleton has been added.

New weather adapter module:

- `core.RealWeatherAdapter`

Real weather skeleton behavior:

- Uses explicit adapter name `real_weather`.
- Supports `weather.current` and `weather.forecast` capabilities.
- Marks `requires_network` and `requires_auth` as true.
- Is not registered in the default SkillManager adapter registry.
- Reads API keys only from the configured environment variable name.
- Fails safely when the env key is missing.
- Does not hardcode or store raw secrets.
- Does not expose raw env values in responses.
- `config/adapters.example.json` keeps `real_weather` disabled and mock-mode by default with fake placeholders.
- Existing WeatherSkill behavior stays on `mock_weather` unless a structured intent explicitly selects `real_weather`.
- No real weather API key, real weather API call, GPT, voice, calendar write, notification, or background job was added.

Real Weather Adapter HTTP logic has been added.

HTTP behavior:

- Runs only when adapter config mode is `real`.
- Requires the configured API key environment variable to exist before any HTTP call.
- Reads the API key value only from the environment variable.
- Does not store raw keys in config.
- Does not return raw env values in adapter responses.
- Passes configured timeout seconds to the HTTP client.
- Normalizes supported weather API payloads into ARES weather data with location, condition, temperature C, period, capability, and source.
- Returns safe deterministic errors for HTTP timeout, HTTP status errors, invalid JSON, and unrecognized weather payloads.
- Tests mock HTTP and make no real network calls.
- Default WeatherSkill behavior remains `mock_weather`.
- No real API key, default real mode, GPT, voice, calendar write, stocks real API, notification, or background job was added.

Real Market Adapter skeleton has been added.

New market adapter module:

- `core.RealMarketAdapter`

Real market skeleton behavior:

- Uses explicit adapter name `real_market`.
- Supports `market.quote` and `market.summary` capabilities.
- Marks `requires_network` and `requires_auth` as true.
- Is not registered in the default SkillManager adapter registry.
- Reads API keys only from the configured environment variable name.
- Fails safely when the env key is missing.
- Does not hardcode or store raw secrets.
- Does not expose raw env values in responses.
- `config/adapters.example.json` keeps `real_market` disabled and mock-mode by default with fake placeholders.
- Existing MarketSkill behavior stays on `mock_market` unless a structured intent explicitly selects `real_market`.
- No real market API key, default real mode, GPT, voice, calendar write, notification, or background job was added.

Real Market Adapter HTTP logic has been added.

HTTP behavior:

- Runs only when adapter config mode is `real`.
- Requires the configured API key environment variable to exist before any HTTP call.
- Reads the API key value only from the environment variable.
- Does not store raw keys in config.
- Does not return raw env values in adapter responses.
- Passes configured timeout seconds to the HTTP client.
- Normalizes supported market API payloads into ARES market data with symbol, price, currency, capability, source, and optional name/change fields.
- Returns safe deterministic errors for HTTP timeout, HTTP status errors, invalid JSON, and unrecognized market payloads.
- Tests mock HTTP and make no real network calls.
- Default MarketSkill behavior remains `mock_market`.
- No real API key, default real mode, GPT, voice, calendar write, notification, or background job was added.

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

Long-term city model architecture has been documented.

City model summary:

- ARES Brain is the capital city.
- The capital owns identity, memory, profile, goals, planning, decisions, and history with the owner.
- Specialized cities connect to the capital through explicit bridges.
- Planned specialized cities are Voice City, Vision City, Device/PC City, Weather City, Market City, Calendar City, Home City, Robot Body City, and Codex City.
- Core Services City provides scheduler, permissions, logging, configuration, health monitoring, plugin manager, secrets guard, and confirmation layer.
- Codex City is a future maintenance city that can check the ARES GitHub repository, pull latest code, run tests, check compile, check docs freshness, report problems, and suggest fixes.
- Codex City must never auto-edit without owner approval.

Future ARES home architecture vision has been documented.

Future architecture summary:

- Brain owns identity, memory, reasoning, planning, goals, personality, owner history, and decision history.
- CoreService is the future intent and capability router between the Brain and registered services.
- Cities are major ability modules.
- Adapters are hardware or API connectors owned by cities.
- Devices are physical bodies and interfaces.
- ARES Home Server is the future continuity point for the Brain, master memory, user profile, goals, relationship history, sync service, and backups.
- Raspberry Pi, Jetson Orin, Windows PC, phone, and future robot bodies act as replaceable clients or bodies.
- Hard rule: hardware-specific code must never enter the Brain.

Boundary:

- Documentation only.
- The home-server/client-body model is future vision, not implemented runtime behavior.
- No scheduler implementation was added.
- No GitHub API integration was added.
- No self-modifying behavior was added.
- No GPT, real voice/audio implementation, internet access, real APIs, or notifications were added.

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

The built-in skill plugin currently registers `MemoryRecallSkill`, `OwnerMemorySkill`, `CalculatorSkill`, `CalendarSkill`, `DeviceActionSkill`, `GoalsSkill`, `MarketSkill`, `NotesSkill`, `TasksSkill`, `WeatherSkill`, and `TimeDateSkill`.
The REPL priority skill path currently covers legacy profile recall, explicit bounded owner-memory commands, calculator arithmetic, goal commands, note commands, task commands, weather commands, stock/market commands, calendar/schedule commands, and safe device action commands.
`SkillManager` parses text into `core.Intent` before `ToolSelector` selects a local skill.
`ToolSelector` builds a `core.Plan` or `core.MultiStepPlan` before selection, and its Planner can use safe injected store interfaces for local context.
`SkillManager` owns a `CoreService` for local/external service registration and capability aggregation where practical.
`SkillContext` carries the same `core_service` reference used by SkillManager.
`SkillManager` handles confirmation decisions through `core.ConfirmationManager` before normal intent parsing.
`SkillManager` validates executable plan steps through `core.ToolChain`, and accepted chains execute through `core.ExecutionPipeline`.
`ExecutionPipeline` can execute weather skill PlanSteps, market skill PlanSteps, calendar skill PlanSteps, device action PlanSteps, and explicit `tool_adapter` PlanSteps through `core.ToolAdapterRegistry`.
`ExecutionPipeline` can execute internal `planner_context` PlanSteps for context-only responses.
`ExecutionPipeline` pauses destructive actions with `ConfirmationRequest` instead of executing them immediately.
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

Install the explicit Vosk dependency and local small English model, then run the one-attempt wake diagnostic. Record its raw local recognizer result, normalized exact phrase, word confidence, finalized candidate duration, and rejection reason. Then run the bounded hardware helper and foreground standby runtime to record no-speech, unrelated-speech rejection, one acknowledgement, active Whisper calculator routing, standby, second activation, and clean shutdown evidence before considering systemd. Runtime ownership must remain in Capital/Core. Do not add boot startup, daemonization, barge-in, GPT, cloud services, autonomous City activation, or a second lifecycle/timer system.

Next technical choices:

- Pull latest `main`, install `requirements.txt`, place `vosk-model-small-en-us-0.15` under `models/vosk`, run the deterministic wake verifier, run `manual_diagnose_wake_word.py --diagnostic-wake`, then run `manual_verify_standby_wake_hardware.py --diagnostic-wake` with the local Vosk/base-Whisper models and known ALSA devices.
- Inspect owner state only through `python scripts/inspect_owner_memory.py --summary --pending` or its focused flags; malformed durable or transient data must fail closed rather than be reset and executed.
- Keep microphone monitoring disabled with `scripts/configure_linux_alsa_monitoring.py` if the USB sound device loops mic playback to speaker.
- An exact high-confidence `ares` or `aries` result must resolve to canonical `ares`. `[unk]`, unrelated words, missing confidence, or confidence below 0.8 must reject; do not add output-driven aliases, substring matching, or fuzzy matching.
- Tune only validated wake adapter thresholds/durations if real hardware evidence requires it; keep exact phrase policy and Capital/Core lifecycle ownership.
- Keep systemd/boot startup for the next separately reviewed checkpoint after hardware stability is demonstrated.
- Keep GPT, embeddings, semantic/vector search, autonomous fact extraction, external weather/stocks/calendar APIs, real scheduling, and notifications out of scope until explicitly approved.
- Keep additional STT engines, internet-backed adapters, notifications, and automatic PC actions out of scope until separately approved.
- Keep explicit owner facts distinct from legacy `UserProfileStore`, general `MemoryStore`, operational event history, and RAM-only conversation context.

---

Future Roadmap

1. Phase 3 Real Voice Integration
2. Controlled single-turn voice pipeline completed
3. Controlled bounded multi-turn voice session completed
4. RMS VAD and automatic end-of-speech capture completed
5. Raspberry Pi USB capture, end-of-speech, and base English Whisper transcription verified by owner
6. Production voice calculator routing hardening completed in CI
7. Anchored natural-language calculator wrapper extraction completed in CI
8. Canonical ALSA/WAV normalization before VAD and Whisper completed in CI
9. Complete ordered post-VAD assembly and duration-safe Whisper handoff in CI
10. Production voice calculator and short launcher verified by owner on Raspberry Pi
11. Explicit favorite-color owner-memory persistence completed and owner-verified across fresh Raspberry Pi voice processes
12. Central general owner facts, v1-to-v2 migration, inspection, and fresh-process verification completed in deterministic tests
13. General explicit long-term owner memory, v2-to-v3 migration, lexical retrieval, confirmation-gated broad deletion, and fresh-process verification completed in deterministic tests
14. Whisper-tolerant explicit-memory routing for `locked term memory` and `remembering ... memory that` completed in deterministic production-path tests
15. Safe central list/count/inspect and confirmation-gated specific/topic/all-general/keyed deletion completed in deterministic production-path tests
16. Central deterministic Brain Session Manager completed in CI
17. Persistent foreground Brain Runtime completed in CI with exact text activation, multi-command sessions, inactivity standby, and explicit shutdown
18. Run the hardware-free runtime verifier and foreground text interface after pulling on Raspberry Pi
19. Add one bounded real microphone wake activation adapter without changing Capital/Core runtime ownership. Completed in deterministic verification; Raspberry Pi hardware proof remains owner-run.
20. Verify constrained Vosk foreground wake stability on Raspberry Pi, then separately review systemd/boot startup
21. GPT fallback integration
22. Raspberry Pi deployment
23. Robot body / sensors
24. Vision
25. Robotics
26. Jetson Orin migration
27. Autonomous ARES

Verification Notes

- `scripts/verify_phase2_events_memory.py` verifies router event publication and memory turn storage with temporary memory files.
- Run it with `python scripts/verify_phase2_events_memory.py`.
- Automated tests run with `py -m pytest`.
- Current pytest collection: 1847 tests.
- Phase 3 skill package compiles with `py -m compileall skills`.
- `SkillManager` was manually checked with the built-in time/date skill.
- Text REPL was verified with `hello`, `what time is it`, `what date is it`, and `quit`.
- Long-term profile recall was verified through the text REPL with name, location, birthday, favorite tank, and owned item facts.
- Current verification passed:
  - `py -m pytest`
  - `py -m compileall core interfaces events memory skills scripts`
  - `py scripts\verify_phase2_events_memory.py`
  - `py scripts\manual_verify_brain_session_manager.py`
  - `py scripts\manual_verify_brain_runtime.py`
  - `py scripts\manual_verify_standby_wake_runtime.py`
- Standby-wake tests cover versioned listener/recognizer contracts, grammar and alias normalization, shutdown/standby collisions, exact `Ares`/`Aries` matching, `[unk]`, low/missing confidence, unrelated-word and partial-word rejection, queued/Linux listener lifecycle, wake-only and unchanged command VAD profiles, actual-header duration guards, canonical-WAV-only recognition, constrained grammar construction, in-process timeout/cancellation, terminal-only diagnostics, bounded latest-candidate retention, missing dependency/model failures before capture, capability metadata, one-session/one-acknowledgement runtime activation, active Whisper calculator and owner-memory routing, standby/inactivity/reactivation/shutdown, self-wake exclusion, output isolation, event and memory privacy, CLI defaults, one-attempt diagnosis, and bounded hardware verification.
- GitHub Actions CI runs the same verification suite on Windows with Python 3.13 for `main` pushes and pull requests.
- GitHub Actions should be checked after push for the latest `main` commit.
- Tool selection tests cover current TimeDate/MemoryRecall/Calculator/Goals/Notes/Tasks selection.
- Calculator tests cover simple arithmetic, precedence, parentheses, decimals, bounded powers, unsafe input rejection, and the REPL routing path.
- Notes tests cover add, list, search, delete, duplicate note text, empty note rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Tasks tests cover add, list, mark done, delete, empty task rejection, persistence after reload, ToolSelector routing, and the REPL routing path.
- Goals tests cover add, list, show, complete, pause, delete, add milestone, persistence after reload, ToolSelector routing, IntentParser routing, Planner path, ExecutionPipeline path, ToolChain goal chains, SkillManager path, REPL lifecycle commands, and the REPL routing path.
- ToolAdapter tests cover adapter registration, lookup, missing adapter responses, mock weather responses, mock market responses, no-network/no-auth metadata, Planner registry wiring, and ExecutionPipeline adapter execution.
- CoreService tests cover service registration, lifecycle metadata, capability registry metadata, lazy route-by-capability behavior, unused city idle behavior, disabled city routing prevention, and failed route state handling.
- ModuleLifecycle tests cover UNLOADED -> STARTING -> READY startup, READY -> BUSY -> READY execution, READY -> STOPPING -> STOPPED shutdown, idempotent start/stop, execution rejection before start, health-check DEGRADED/FAILED policy behavior, startup failure, execution failure isolation, illegal transition rejection, explicit recovery, transition history, monotonic timestamps, correlation id preservation, and lifecycle status queries.
- CoreService lifecycle tests cover lifecycle health gating, explicit recovery, unrelated-module failure isolation, lifecycle status/history queries, and Voice City activation without activating PC City.
- Health fallback tests cover HealthResult normalization, healthy/degraded/unavailable/failed/disabled states, primary/secondary adapter selection, disabled/capability/version rejection, degraded strict/permissive policy, all-unavailable failure, rejection reasons, bounded fallback attempts, retry-safe fallback, retry-unsafe no-fallback, circuit open/half-open/recovery behavior, deterministic clocks, health cache TTL/refresh/invalidations, malformed/timeout/exception health failures, event-history safety, lazy CoreService health visibility, active probes without lifecycle activation, VoicePipeline compatibility, and mock STT fallback.
- Resource budget tests cover manifest resource declarations, invalid resource values, RAM budget rejection, network/hardware policy rejection, heavy-module limits, reservation release, CoreService budget gates, failed activation cleanup, execution exception cleanup, global and per-module task limits, idle detection, maintenance unloading, persistent/active unload prevention, eviction candidate selection, eviction-disabled behavior, cooperative cancellation, process metrics availability, status inspection without activation, resource event safety, health/fallback compatibility, and simulated VoicePipeline compatibility.
- Core EventBus tests cover event dataclass normalization, publish, subscribe, unsubscribe, no-subscriber safety, priority ordering, invalid priority rejection, and stable priority levels.
- CoreService event decision tests cover low, normal, high, critical, unknown-source, and disabled-source event handling.
- EventHistoryStore tests cover add, query by source/type/priority, bounded max size, empty history, persistence after reload, invalid priority rejection, and zero-size history.
- CoreService event-history integration tests cover stored low, normal, high, critical, unknown-source, and disabled-source decisions.
- EventHistorySkill tests cover recent events, critical events, empty history, parser phrases, planner steps, and SkillManager live path.
- VoicePipeline tests cover successful complete simulated command execution, empty audio, microphone failure, STT failure, empty transcription, low-confidence rejection, unknown command, CoreService routing failure, target city failure, output adapter failure, session reuse after failure, requested-city activation only, stable correlation ids, and unrelated city idleness.
- VoiceService tests cover CoreService registration, safe placeholder capabilities, safe placeholder status, VoiceInput/VoiceOutput ownership, VoiceInputAdapter/VoiceOutputAdapter mock implementations, adapter injection, NullVoiceInput listen placeholders, NullVoiceOutput speak placeholders, mock input capture and capture compatibility, mock output speak, empty mock input, CoreService aggregation of PCService and VoiceService, VoiceTextRequest conversion, VoiceLoop defaults, VoiceSingleTurnLoop normal input/output, VoiceSessionLoop multi-turn flow, stop phrase handling, max-turn limiting, empty no-op turns, input adapter failure, output adapter failure, transcript/history output, no-input behavior, recognized text routing to a mocked planner/execution handler, response handoff to NullVoiceOutput, safe input/handler/output/adapter failures, and no audio hardware access.
- VoiceSessionSkill tests cover parser phrases, max-turn entity extraction, status query phrases, Planner step creation, ToolSelector routing, direct skill start, stop phrase handling, max-turn limiting, empty session behavior, transcript summaries, mock-adapter-only safeguards, safe local event history records for start/stop/adapter failure/max-turn completion, latest voice-session status summaries for no-session/stopped/failed/max-turn cases, EventHistorySkill display, and SkillManager live-path execution.
- DeviceAction tests cover registry registration/listing, app allowlist config loading, calculator enabled state, invalid config rejection, duplicate app id rejection, unknown action safe failure, echo, list actions, list apps, structured PCService status, structured PCService capability discovery, CoreService-backed service registration/capability aggregation, default PCService status/capability interfaces, safe missing-capability reporting, stable result formatting, PCService delegation for status/lock/sleep/open-app calls, CoreService-backed action/app discovery, danger classification, confirmation-required placeholders, forbidden placeholders, unapproved `lock_pc`/`sleep_pc`/`open_app`, confirmed mocked Windows lock/sleep, confirmed Windows calculator launch through a mocked launcher, unknown/disabled app rejection, notepad/browser disabled handling, arbitrary path rejection, shell-like input rejection, user-supplied path isolation, non-Windows unsupported handling, shutdown/restart remaining non-executable, and not-executed dangerous results.
- Manual calculator launch verification tests cover refusal without exact confirmation, the exact open_app device action path with mocked adapter, and safe adapter failure reporting without opening Calculator.
- Manual Voice City text simulation tests cover import safety, typed text reaching VoiceLoop through `MockVoiceInputAdapter`, real local calculator routing through the existing SkillManager planner/execution path, empty input safe exit, and no audio hardware access.
- DeviceActionSkill tests cover echo, list actions, list apps, structured system status, shutdown/restart confirmation-required responses, unapproved `lock_pc`/`sleep_pc`/`open_app`, confirmed `lock_pc`/`sleep_pc`/`open_app` through SkillManager, run command/delete forbidden responses, unknown action safe failure, ToolSelector routing, Planner routing, SkillManager/CoreService handoff, SkillContext CoreService propagation, SkillManager/ExecutionPipeline confirmation-required handling, and text REPL display.
- Adapter config guard tests cover mock mode, real-mode missing-env failure, placeholder handling, raw-secret rejection, example config loading, read-only mock adapter behavior, and confirmation-layer compatibility.
- RealWeatherAdapter tests cover default mock weather behavior, real adapter instantiation, real-mode missing-env failure, mocked HTTP success, timeout safe errors, bad API response safe errors, HTTP status safe errors, normalized output stability, env-key-name-only config, raw env value non-exposure, safe WeatherSkill adapter failure handling, raw-looking key rejection, and SecretsGuard example-config compatibility.
- RealMarketAdapter tests cover default mock market behavior, real adapter instantiation, real-mode missing-env failure, mocked HTTP success, timeout safe errors, bad API response safe errors, HTTP status safe errors, normalized output stability, env-key-name-only config, raw env value non-exposure, safe MarketSkill adapter failure handling, raw-looking key rejection, and SecretsGuard example-config compatibility.
- WeatherSkill tests cover weather intent parsing, mock adapter calls, WeatherSkill responses, ToolSelector routing, planner weather steps, execution pipeline weather steps, REPL routing, full live path into `MockWeatherAdapter`, ToolChain loop prevention for repeated weather steps, and missing adapter errors.
- MarketSkill tests cover market intent parsing, mock adapter calls, MarketSkill responses, ToolSelector routing, planner market steps, execution pipeline market steps, REPL routing, and missing adapter errors.
- CalendarSkill tests cover calendar intent parsing, mock adapter calls, CalendarSkill responses, ToolSelector routing, planner calendar steps, execution pipeline calendar steps, REPL routing, and missing adapter errors.
- ReminderScheduler tests cover tomorrow parsing, relative minutes/hours, clock time parsing, due task detection, upcoming task ordering, and invalid due text handling.
- Planner tests cover single-step plans, two-step plans, mixed notes/calculator, mixed task/memory, goal steps, invalid plans, ordering, serialization, ToolSelector plan attachment, SkillManager execution, and REPL plan display.
- Multi-step planner tests cover single-step compatibility, weather plus reminder, goals plus calendar, three-step plans, planner ordering, execution ordering, partial failure recovery, and REPL integration.
- Context-aware planner tests cover goal context, profile favorite context, notes context, task context, missing context, multi-step context plans, partial failure recovery, and REPL integration.
- Confirmation tests cover delete note/task/goal confirmation, confirm execution, cancel behavior, missing pending confirmation, unaffected weather/market/calendar paths, future external write confirmation, clear completed tasks, goal status changes, and multi-step confirmation pause behavior.
- ExecutionPipeline tests cover single-step execution, multi-step execution, notes plus calculator, task plus memory, unrecoverable failure, recoverable partial failure, execution ordering, execution logging, rollback hooks, SkillManager integration, REPL execution display, live REPL multi-step planning, live REPL pipeline execution, live REPL partial failure reporting, and live-path component usage.
- ToolChain tests cover memory plus calculator, note plus memory, task/reminder plus memory, ordering, max depth enforcement, repeated-step loop prevention, and REPL chain display/history.
- Conversation context tests cover history ordering, max history size, clear, retrieval APIs, SkillManager integration, and REPL integration.
- Intent parser tests cover intent detection, confidence values, entity extraction, goal commands, ambiguous local phrasing, unknown intent, ToolSelector integration, SkillManager integration, live REPL parser use, and the REPL task path.
- Contract tests cover V1 request/result acceptance, unsupported V2 rejection, missing/malformed contract headers, wrong contract type rejection, correlation id preservation, metadata round-trip, deterministic serialization, registry discovery, duplicate registration rejection, CoreService pre-activation rejection, lifecycle state preservation, VoicePipeline session reuse after rejection, and successful V1 VoicePipeline execution.
- Capability manifest tests cover schema validation, deterministic serialization, duplicate rejection, capability/provider lookup, disabled modules, unsupported contracts, dependency validation, incompatible capabilities, platform mismatch, permission policy, lifecycle declaration mismatch, provider preference, skill manifest registration, CoreService manifest rejection, VoicePipeline compatibility, and CoreService usability after manifest failures.
- Schema migration tests cover current schema load, known legacy import into v1, non-guessing malformed legacy inputs, v1 -> v2 fixture migration, multi-step order, future-version rejection, missing paths, duplicate/cyclic registration, dry-run behavior, backup creation, original preservation, temporary-file cleanup, malformed/truncated JSON rejection, wrong schema rejection, post-migration validation failure, metadata preservation, deterministic serialization, store usability after migration, unrelated city idleness, CoreService usability, event-history recording, concurrent lock failure, and content-safe inspection reports.
- Final integration checkpoint tests cover the complete mock voice/text route, read-only PC status routing, confirmation-gated device actions, exactly-once destructive action protection, fallback boundaries, resource/task cleanup, unrelated-City idleness, disabled/incompatible fail-closed behavior, operational event redaction, migration/resource safety regressions, and recovery from isolated subsystem failures.
- Linux ALSA microphone adapter tests cover capture-device parsing, missing `arecord`, no devices, invalid selected devices, valid WAV recording, `read_chunk()` through the adapter contract, timeout, non-zero arecord exits, invalid device process errors, missing/empty WAV output, unsafe device identifier rejection, structured status/capabilities, and manual script no-record/explicit-record behavior.
- Offline Whisper STT tests cover health checks, missing binary, missing model, WAV transcription metadata, stdout parsing, no-transcription results, invalid/missing audio, timeout, non-zero process exit, `AudioChunk` transcription with and without existing WAV metadata, structured status/capabilities, mocked Raspberry Pi ALSA-to-Whisper integration, and manual script record/transcribe behavior.
- Whisper runtime setup tests cover clone/build/download/verification success, existing checkout/model reuse, missing dependency failure, build failure, import safety, runtime verifier success, missing command, missing model, missing WAV sample, transcription failure, empty transcription, and PASS/FAIL output paths without real network, Whisper, or Raspberry Pi hardware.
- Speech-input hardening tests cover valid speech WAV diagnostics, silent WAV rejection, near-silent RMS rejection, corrupt WAV rejection, no-speech marker handling, English-only model language resolution, missing model/binary failures before subprocess execution, exact command diagnostics, runtime verifier diagnostics, runtime verifier `--language en` defaults, playback disabled by default, explicit playback opt-in, ALSA monitoring command planning, dry-run behavior, apply behavior, missing `amixer`, and preserving capture while muting mic playback monitoring.
- VAD tests cover deterministic PCM no-speech, immediate/short/long utterances, pre-roll and first-syllable preservation, short pauses, threshold boundaries, hysteresis, maximum duration, cancellation, device/timeout/invalid-PCM failures, lifecycle, raw ALSA argument-list streaming, fixed-duration fallback, pipeline short-circuiting, single-turn/multi-turn routing, mutual exclusion, calibration script behavior, and hardware-free synthetic PCM integration through the real local Brain route.
- Adaptive VAD tests cover quiet/noisy ambient calibration, a transient spike, derived bounds, start/continue/silence hysteresis, repeated internal pauses, post-speech sub-continue noise, normal trailing-silence completion, no-speech, maximum duration, cancellation, and calibration-disabled fallback.
- Transcript normalization tests cover raw/cleaned/normalized preservation, number words zero through one thousand, negatives, decimals, spoken operators and parentheses, anchored direct/polite/question/first-person/vocative wrappers, contraction and punctuation handling, ambiguity and multiple-expression rejection, conservative Whisper-loop cleanup, versioned round trips, no `eval()`, and real Brain/CalculatorSkill routing to `Result: 4`.
- Production voice calculator tests cover the shared runtime registry, exact script factory, `I'll calculate 2 plus 2.`, `What is two plus two?`, all approved wrapper categories, unsafe and ambiguous input rejection, bounded lengths, empty transcript/audio, safe unknown handling, cleanup/candidate/manifest diagnostics, Planner/ExecutionPipeline evidence, V1 round trips, TTS handoff, and lifecycle/resource cleanup without a mock calculator or GPT fallback.
- Production launcher tests cover verified Raspberry Pi defaults, repository-root path resolution, delegation to the existing factory and single-turn pipeline, versioned request metadata, automatic/fixed capture selection, response-only playback, diagnostic opt-in, dependency and construction failures before capture, stable exit codes, and the absence of duplicated microphone/Whisper/Piper/speaker implementation.
- Owner-memory tests cover v2-to-v3 migration, existing-fact preservation, structured preference/dislike/routine/goal/personal/instruction memories, explicit trigger variants, ordinary-conversation non-persistence, exact/topic/type recall, duplicate control, explicit superseding updates, list/count/inspect, confirmation-gated exact/topic/all-general/keyed deletion, cancellation, expiry, corrupt transient state, normalized aliases, limits, protected-key redaction, malformed-profile rejection, backup/atomic failure handling, fresh-process persistence, production IntentParser/Planner/ExecutionPipeline/OwnerMemorySkill routing, calculator/task/note collisions, voice/text parity, and isolation from transcript/audio persistence.
- `git diff --check` passed after the automated test changes.
- Runtime Python checks may not be available in some Windows sessions if `python`/`py` are not installed, only Microsoft Store aliases are present.
- Example configuration now documents bounded transcript-routing defaults and keeps diagnostic output disabled unless explicitly requested.

Production Launcher Raspberry Pi Check

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/run_ares_voice.py
```

Say `How much is two plus two?` The expected route selects the registered calculator, returns `Result: 4`, speaks only that Piper response through `plughw:CARD=Device,DEV=0`, and exits. Add `--diagnostic-routing --retain-diagnostic-audio` to preserve files and print bounded routing fields without replaying capture. Captured-stage playback remains a separate explicit `--play-diagnostic-audio` action.

General Explicit Long-Term Owner Memory Check

Implementation and Windows/hardware-free checks are complete. Existing keyed facts are preserved and the management flows still require owner-run Raspberry Pi verification after pulling. Do not describe CI mocks as hardware verification.

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/manual_verify_general_long_term_memory.py \
  --profile /tmp/ares_general_long_term_memory.json \
  --reset \
  --verbose
python scripts/manual_verify_owner_memory_management.py \
  --profile /tmp/ares_owner_memory_management.json \
  --pending-state /tmp/ares_pending_owner_memory_action.json \
  --reset \
  --verbose
python scripts/inspect_owner_memory.py --summary --pending
```

The general-memory verifier creates fresh child processes for save, recall, list, duplicate, update, persistence, non-memory, and reminder collision checks. The management verifier separately proves no mutation before confirmation, cross-process exact confirmation, ambiguity refusal, topic deletion, cancellation, expiry, all-general/keyed separation, and corrupt transient-state refusal. Both use isolated files, never modify the production profile, and fail if a voice-specific memory file appears or if the route bypasses the central Brain service.

For real voice verification, run `python scripts/run_ares_voice.py` separately for every phrase. A request and its confirmation are separate foreground processes:

1. Say `What do I like?` and note the existing preferences.
2. Say `Forget that I like going on works.` Expect a candidate prompt, with no deletion yet.
3. Say `No, cancel.` Expect `Deletion cancelled. I kept the memory.`
4. Ask `What do I like?` and confirm the incorrectly transcribed memory remains.
5. Repeat `Forget that I like going on works.`, then in a fresh process say `Yes, delete it.` Expect the exact memory to be deleted while gym/video-game memories remain.
6. Say `Forget that I love video games.`, then `Yes, delete it.` Confirm no unrelated memory is removed.
7. Say `Remember in your long-term memory that I like playing video games.` and confirm a repeat does not duplicate it.
8. Ask `How many memories do you have about me?`, `List my general long-term memories.`, and `List my saved facts.`
9. Say `Forget everything about gaming.`, then `Never mind.` Confirm no gaming memory or keyed favorite-game fact is removed.

The phrase `remember to buy milk` remains a task, not owner memory. `Remind me to delete the file tomorrow` remains a task. `I went to the gym today` persists nothing. `Forget it`, `delete my memory`, and `remove everything` are too vague to create a pending deletion. Explicit temporary statements return clarification rather than silently becoming durable facts.

Inspect the versioned UTF-8 JSON without editing it:

```bash
python scripts/inspect_owner_memory.py --summary
python scripts/inspect_owner_memory.py --facts
python scripts/inspect_owner_memory.py --memories
python scripts/inspect_owner_memory.py --topic gym
python scripts/inspect_owner_memory.py --type preference
python scripts/inspect_owner_memory.py --count
python scripts/inspect_owner_memory.py --pending
python scripts/inspect_owner_memory.py --json
```

The inspector is read-only and exits nonzero for corrupt or unsupported state. It reports schema v3, keyed facts, general memory ids/types/canonical text/topics/timestamps/status, the centrally resolved profile path, and bounded transient pending metadata. It never confirms, cancels, deletes, or repairs state. Recovery is explicit: validate the active file and retained backup before replacing anything; ARES never silently resets or auto-restores owner memory.

Latest Commits

- `78baf61` Add foreground standby wake runtime
- `1744506` Implement safe owner memory management
- `f63385d` Implement general explicit owner memory
- `2e8a9fb` Implement central general owner memory
- `d006422` Harden owner memory routing and persistence
- `14ddf38` Add explicit persistent owner memory
- `1f3294d` Add production single-turn voice launcher
- `e9103d3` Document calculator wrappers and playback isolation
- `bfdcff7` Harden spoken calculator routing and playback isolation
- `6377d12` Document complete VAD utterance handoff
- `188174d` Preserve complete VAD utterances for Whisper
- `7934987` Document format-safe Raspberry Pi audio capture
- `e8a881b` Normalize ALSA capture before voice processing
- `419cb7d` Support safe natural-language calculator wrappers
- `40255ad` Document production voice calculator routing
- `45cfb9c` Harden production voice calculator routing
- `fcb47f1` Document adaptive voice capture and calculator routing
- `fcc8a45` Harden calibrated voice capture and calculator routing
- `a27575b` Document automatic end-of-speech capture
- `9c57bdd` Implement automatic end-of-speech voice capture
- `4b14766` Document controlled multi-turn voice sessions
- `bbd0380` Add controlled multi-turn voice sessions
- `6cdfc51` Document controlled single-turn voice checkpoint
- `305117a` Add controlled single-turn voice pipeline
- `3ea316e` Add configurable Piper voice profiles
- `58915cb` Document reliable Raspberry Pi TTS verification
- `73dd9d6` Fix Raspberry Pi TTS verification
- `93ea0eb` Document modular offline TTS checkpoint
- `cdfbbea` Add modular offline Linux TTS output
- `f5b4d1f` Fix Whisper speech input verification
- `34e6bfa` Harden Raspberry Pi speech input verification
- `67942dc` Add Raspberry Pi whisper runtime setup scripts
- `cb510c9` Add offline Whisper speech to text adapter
- `692f3d2` Add Linux ALSA microphone adapter
- `f8c5c95` Add final integration safety checkpoint
- `6636b78` Add measured resource budgets
- `ebaff5a` Add health checks and adapter fallback
- `a269ff2` Document memory schema migrations
- `dc2526d` Add versioned memory schema migrations
- `80df88b` Add capability manifest foundation
- `98d8dea` Document versioned interface contracts
- `2c61d59` Add versioned interface contracts
- `35da573` Add enforced module lifecycle foundation
- `9365d76` Add simulated voice command pipeline
- `20930a1` Add voice command router
- `b02fc6a` Add speech-to-text adapter abstraction
- `36a534b` Add microphone adapter abstraction
- `3061509` Document Phase 3 voice checkpoint
- `9315b73` Add Voice Session status query
- `147248b` Log Voice Session events
- `31e0baa` Add Voice Session skill
- `2094bf2` Add Voice City multi-turn mock session
- `a0408cc` Document Voice City single-turn loop
- `ea3bbe2` Add Voice City adapter-backed single-turn loop
- `6e20daf` Document Voice City audio adapter contracts
- `0cf439d` Add Voice City audio adapter contracts
- `d2732bf` Document Phase 3 foundation checkpoint
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
- `9ac2de7` Add manual Voice City text loop simulation
- `b2b7a48` Add Voice City text loop foundation
- `fa98b30` Add Voice City input output contracts
- `6245ef4` Add Voice City service skeleton
- `432a70a` Create permanent architecture reference
- `5726813` Document Phase 2 architecture completion
- `8049fab` Stabilize core service architecture
- `ade5197` Document core service orchestration layer
- `78ced08` Add core service orchestration layer
- `9740e6d` Document PC service capability discovery
- `aa281b7` Add PC service capability discovery
- `44f7289` Document PC service status provider
- `b4af64d` Add structured PC service status provider
- `ab9ea2f` Add PC service abstraction for device actions
- `0af1e5c` Add manual calculator launch verification script
- `090043e` Enable calculator app allowlist entry
- `3486663` Add config-backed app allowlist loader
- `d15a528` Add confirmed Windows app launcher
- `8b6e7fc` Add safe device app launcher skeleton
- `6bab5a2` Add confirmed Windows sleep device action
- `65660aa` Add confirmed Windows lock device action
- `5a789bf` Add device action danger confirmation gate
- `63e4a6e` Add device action skill live path
- `694da9e` Add safe device action framework skeleton
- `357f984` Implement gated real market HTTP adapter
- `88e739e` Add real market adapter skeleton
- `d95ace8` Implement gated real weather HTTP adapter
- `4fbb734` Add real weather adapter skeleton
- `40ea8e5` Add external adapter config guard
- `fbf4492` Add action confirmation layer
- `79f7e4a` Add context-aware planner support
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

- Architecture hardening before real hardware/adapters is complete: lifecycle, contracts, manifests, migrations, health/fallback, and measured resource budgets are implemented.
- Phase 3 now includes the Capital-owned foreground standby wake runtime with a constrained Vosk grammar, owner-local wake diagnostics, confidence gates, and finalized-WAV duration guards in deterministic verification, alongside owner-verified ALSA, active-command Whisper, Piper, speaker, voice profiles, single-turn voice, and calculator routing.
- Pull `main` on Raspberry Pi, install the dependency/model documented above, and run `python scripts/manual_diagnose_wake_word.py --diagnostic-wake`, then `python scripts/manual_verify_standby_wake_hardware.py --diagnostic-wake`. Verify silence and unrelated speech remain in standby, exact high-confidence `Ares`/`Aries` produces one acknowledgement, calculator and owner-memory commands use active Whisper under one session, `goodbye Ares` returns to standby, and `shutdown Ares` cleans up.
- Then run `python scripts/run_ares_standby_voice.py` in the foreground. Do not claim hardware wake verification until the owner records this evidence.
- Keep CI green before merging or pushing further changes.
- Prefer feature branch -> local verification -> PR -> CI -> merge for future work.
- Do not enable default real weather/market API behavior, Google Calendar integration, GPT, embeddings, vision, scheduling, notifications, or background automation yet.
- Do not add systemd/boot startup, daemonization, cloud speech, barge-in, an unbounded hidden loop, automatic transcript memory writes, semantic/vector memory, autonomous fact extraction, embeddings, or cloud synchronization yet.
