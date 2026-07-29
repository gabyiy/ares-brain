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
- own the central `BrainSessionManager` composition boundary
- expose `get_brain_session_snapshot()` without activating a module or City
- receive internal city events with `handle_event(event)`
- track city lifecycle states: `idle`, `active`, `failed`, and `disabled`
- register PCService as the default `pc` service
- register the Voice City placeholder service as the default `voice` service
- fail safely when a registered service does not expose required capability interfaces

CoreService does not implement device behavior itself. It discovers and routes to registered services.

# Central Brain Session Lifecycle

`core.BrainSessionManager` belongs to the Capital/Core Brain. It is not a removable City, adapter, skill, hardware provider, or `ModuleLifecycleManager` record. CoreService composes one manager and exposes a read-only snapshot; constructing CoreService does not boot the Brain or activate any registered service.

The versioned public boundary is `BrainSessionTransitionRequestV1` plus `BrainSessionSnapshotV1`. A snapshot reports current/previous/source/requested state, session and correlation identifiers, entered and last-activity timestamps, inactivity timeout/deadline state, consecutive and maximum failure counts, transition reason, structured status/errors, and bounded safe metadata.

Legal transitions are explicit:

```text
STOPPED -> BOOTING -> INITIALIZING -> STANDBY
STANDBY -> ACTIVE -> PROCESSING -> RESPONDING -> ACTIVE
ACTIVE | PROCESSING | RESPONDING -> RETURNING_TO_STANDBY -> STANDBY
any nonterminal state -> SHUTTING_DOWN -> STOPPED
operational state -> ERROR (only through explicit failure reporting)
ERROR -> RETURNING_TO_STANDBY (only explicit safe recovery) -> STANDBY
ERROR -> SHUTTING_DOWN -> STOPPED
```

An invalid transition returns `transition_rejected` with source state, requested target, and a bounded reason. State, session ID, timestamps, and transition history remain unchanged. Direct transition to `ERROR` is rejected; `report_failure()` owns failure counting and enters `ERROR` only for an explicitly unrecoverable failure or the configured consecutive-failure limit. Recovery requires `recovery_safe=True` and retains the required `RETURNING_TO_STANDBY` intermediate state.

Entering `ACTIVE` from `STANDBY` creates a unique session ID. Processing and response transitions preserve it. Completing the standby return clears it. `record_activity()` updates an injected-clock deadline, while `inactivity_expired()` only answers a question: it starts no timer, thread, listener, or transition. Defaults are 30 seconds and three consecutive failures; accepted bounds are 1-3600 seconds and 1-20 failures. `config/modules.example.json` records those values.

Lifecycle events are `brain_boot_started`, `brain_initialization_started`, `brain_standby_entered`, `brain_session_activated`, `brain_processing_started`, `brain_response_started`, `brain_session_activity_recorded`, `brain_returning_to_standby`, `brain_shutdown_started`, `brain_stopped`, `brain_state_transition_rejected`, and `brain_lifecycle_error`. They carry state, machine-safe reason, timestamp, correlation/session identifiers, timeout, and failure count only. Transcript text, owner-memory values, audio, secrets, and file contents are excluded. An injected `EventHistoryStore` may persist these bounded records synchronously. Event history is non-critical operational telemetry: append waits at most 0.05 seconds for its in-process lock, then warns once, increments the store's in-memory dropped-event counter, and leaves lifecycle execution live. The separate file-lock layer still recovers only an expired local lock whose PID is proven dead; a live owner is never stolen. This fail-open boundary does not apply to owner memory or other critical durable stores.

`BrainSessionManager` remains the sole lifecycle authority for the persistent foreground runtime. Future City activation may be requested by Capital/Core only after CoreService validates capability, health, contract, and capacity; Cities remain lifecycle-managed resources and will neither own nor mutate the central manager. The manager itself contains no microphone or wake logic. `BrainRuntime` may invoke the bounded wake adapter described below, but daemon/systemd startup and City activation remain unimplemented.

The bounded hardware probe `scripts/manual_diagnose_active_lifecycle_audio.py` and `scripts/run_ares_standby_voice.py` consume one authoritative production active-audio factory. That factory owns CLI-to-adapter mapping, the real ALSA microphone adapter, canonical ACTIVE request profile, constrained lifecycle-audio recognizer, and bounded Whisper helper. The diagnostic receives that production pipeline, observes the finalized-audio decision in compare-only mode, injects a nonpersistent event sink, constructs no `BrainRuntime`, and executes no lifecycle transition, CoreService request, skill, owner-memory operation, or production event-history write. It now captures only `goodbye Ares` and `shutdown Ares` until cancellation is physically verified.

`SingleTurnVoicePipeline` exposes one typed `finalized_audio_hook` after capture has stopped, the writer is closed, canonical WAV validation and minimum-RMS validation have passed, and before Whisper starts. The hook receives the existing `AudioChunk`; its `wav_path` and `final_whisper_input_path` identify the same closed 16 kHz mono signed 16-bit little-endian WAV. `SingleTurnFinalizedAudioDecision` has one exclusive outcome: handle the turn without Whisper, or continue to Whisper. The lifecycle recognizer does not copy or recapture audio. Production handles only high-confidence exact lifecycle evidence at this boundary. Medium, rejected, ordinary, low, unmatched, missing-confidence, timeout, and backend-failure decisions continue into the existing Whisper adapter with the same `AudioChunk`. This removes the invalid `uncertain/action=none/fallback=no` outcome. The diagnostic deliberately continues to Whisper for side-by-side reporting even when high constrained evidence would have bypassed it in production.

`core.ActiveLifecycleAudioRecognizer` is a pure evidence component. Production `VoskLifecycleGrammarBackend` owns one reusable spawned worker process that loads the configured local Vosk model once and creates a fresh constrained recognizer for each finalized candidate. The request deadline bounds child readiness, model loading, recognizer construction, native Vosk calls, PCM decoding, and result waiting. Parent-side `multiprocessing.Process.start()` and pipe `send()` remain synchronous setup calls and are not independently preempted by this helper. When a bounded child wait expires, cleanup terminates, kills if necessary, and requires the worker to be reaped; unproven cleanup fails closed and prevents a competing worker. A later turn can start a fresh worker only after cleanup is confirmed. The loaded grammar has exactly 42 standby expansions, 36 shutdown expansions, 49 bounded non-action competitors, and `[unk]` (128 alternatives). Its result preserves raw recognized text/tokens and separately reports the detected lifecycle-slot alias, prefix/suffix position, alias-canonicalized transcript, canonical phrase, confidence/tier, classification, selected action, fallback decision, and rejection reason. It has no microphone dependency, capture method, runtime/session reference, CoreService, memory service, or transition callback. Only `BrainSessionManager` may apply the action.

This boundary follows real hardware isolation, not a synthetic assumption. The repaired production-composed Raspberry Pi probe opened `plughw:2,0`, read nonzero complete canonical PCM, and finalized every requested candidate. A later owner run reached the second lifecycle phrase but blocked after starting Whisper and printing the timeout configuration. The current two-phrase diagnostic therefore accepts either high-confidence constrained authorization or a valid same-turn Whisper lifecycle fallback, and it cannot advance until the prior child/audio transaction is clean. Post-fix Raspberry Pi cancellation and completion acceptance remains required.

Production ACTIVE capture uses one bounded stream per command rather than the persistent standby transport. The diagnostic preserves that architecture: it verifies no live runtime or playback gate owns the microphone, reuses the production pipeline and adapter while creating and starting one fresh PCM stream for each phrase, reads only after open succeeds, closes after the final read for that phrase, releases diagnostic ownership in all exits, and never reuses a closed stream. A failure in one phrase is isolated from the next phrase's new ownership transaction. Valid live locks and processes are reported and never stolen or killed as stale merely to make the probe continue.

The diagnostic preflight is a structural audio-boundary check, not speech classification. It reports requested/resolved ALSA device, the canonical `16000 Hz / mono / signed S16_LE` contract, open/start/read state, the expected and actual 640-byte first frame, nonzero status, process and ALSA child identities, exact capture argv, bounded stderr, ownership, stream health, and cleanup. A complete all-zero or low-energy silent frame is structurally valid. The owner prompt begins only after preflight and per-phrase capture readiness, so preflight does not consume an advertised command.

Raw process exit is evidence, not the health decision. `SubprocessPcmFrameSource.close()` emits one immutable `PcmStreamStopResult` containing control intent, valid-frame evidence, exit code/signal, requested termination/escalation, stderr, reap and pipe-cleanup state, pre-stop failure state, ownership loss, final classification, and health effect. `controlled_stop` requires an explicit stop while the child was alive, at least one valid PCM frame, no active transport failure before the stop, complete reap and pipe cleanup, and an expected result. Expected results are exit zero, cleanup-time SIGINT/SIGTERM, bounded-escalation SIGKILL, or exit one only when stderr consists of the single exact `arecord` + `pcm_read` + `read error` + `Interrupted system call` diagnostic. This last case is the controlled interruption of a blocking read after ARES requested shutdown, not an ALSA failure during capture. A reaped SIGKILL escalation is `controlled_stop_degraded` with `degraded_reusable`; other negative signals, mixed/additional stderr, zero PCM, a child already dead at stop, active read/select/EOF failure, unrelated exit-one stderr, ownership loss, or incomplete cleanup is never masked and remains `unexpected_failure`/`cleanup_incomplete` with an unhealthy effect.

Microphone health and speech-to-text health are independent component contracts. Microphone health checks device configuration/availability, idle stream and `VoiceRuntimeGate` ownership, prior process reap, and prior controlled-stop effect. STT health checks executable resolution/permission, model readability, temporary-output writability, command construction, current/prior child and process-group state, reap/handle cleanup, and cancellation state without opening the microphone or invoking Whisper. A cancelled and fully reaped prior request is historical healthy metadata; a live or unreaped child remains unhealthy. The production defect at this boundary was exact: `WhisperSubprocessRunner.__init__()` did not invoke `SafeSubprocessRunner.__init__()`, so `_runner` was absent and inherited `which()` raised `AttributeError: 'WhisperSubprocessRunner' object has no attribute '_runner'` during STT health. Calling the superclass constructor restores the resolver; it does not change the inference grammar, lifecycle parser, microphone, VAD, or confidence policy.

Failures retain typed categories including microphone open/health/cleanup, PCM read, invalid frame, no speech, VAD, WAV write, Whisper configuration/health/inference, empty transcript, lifecycle parse, and lifecycle-recognition mismatch; they also retain the exception class/message, failing adapter/method, requested format/device, stream state, process context, ALSA stderr, and cleanup outcome. Diagnostic-only UTC progress markers distinguish microphone-preflight start, valid PCM receipt, controlled stop, preflight pass, true capture readiness/start, actual finalized STT completion, microphone release, and temporary-file finalization. Completion is not printed merely because a pipeline call returned. An explicit diagnostic run may print a full traceback; normal production remains bounded and traceback-free. Deterministic tests can prove these contracts and branches, but only Raspberry Pi execution can prove the installed command/model permissions, real ALSA stop result, Whisper child behavior, owner transcript, and final lifecycle decisions.

The lightweight Raspberry Pi STT configuration preflight composes the production adapter but deliberately opens no microphone and requests no inference:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main

python scripts/manual_diagnose_active_lifecycle_audio.py \
  --microphone-device plughw:2,0 \
  --speaker-device plughw:CARD=Device,DEV=0 \
  --diagnostic-active-lifecycle-audio \
  --stt-preflight-only
```

It must end with executable/model/output/command checks passing, no current child, the previous child reaped, cancellation clear, and `STT configuration preflight result: passed`. The bounded two-phrase hardware gate then uses the same composition:

```bash
python scripts/manual_diagnose_active_lifecycle_audio.py \
  --microphone-device plughw:2,0 \
  --speaker-device plughw:CARD=Device,DEV=0 \
  --diagnostic-active-lifecycle-audio
```

Its key preflight evidence is a valid PCM frame, an explicit stop, complete reap/cleanup, no unexpected failure, and no final health effect. Exit code 1 plus the exact interrupted-`pcm_read` stderr is acceptable only beside that complete controlled-stop evidence. Phrase one must resolve to standby and phrase two to shutdown through high constrained evidence or exact same-WAV Whisper fallback; the diagnostic executes neither action and must finish with `both lifecycle phrases passed the bounded decision policy`.

Hardware-free verification:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/manual_verify_brain_session_manager.py
```

# Persistent Foreground Brain Runtime

Typed/simulated and fallback Whisper lifecycle matching remains deterministic and whole-phrase based. Real audio first uses the narrower constrained grammar and confidence policy below; a constrained rejection may then defer to the existing exact ACTIVE Whisper lifecycle parser before ordinary routing. Negated, descriptive, scheduled, embedded, empty, failed, or otherwise longer phrases do not execute lifecycle actions in either path.

The diagnostic-only production composition banner identifies the resolved runtime, lifecycle authority, standby listener, active input adapter, pipeline, ALSA recorder, constrained Vosk recognizer, Whisper adapter, shared gate, and routing revision. The explicitly gated lifecycle-audio probe also prints the exact loaded grammar. Each result reports raw constrained text and tokens, detected alias and position, alias-canonicalized text, confidence/tier/backend/classification/canonical/rejection, selected action, fallback decision, and the comparison Whisper result. These values remain terminal-only under diagnostic flags and are not written to events, memory, or operational logs. Normal production output does not dump the grammar.

`BrainSessionManager` is the lifecycle authority, including activation authority. `activate_session()` accepts only `STANDBY`; every other state returns `activation_not_allowed` while preserving state, session ID, transition history, and acknowledgement count. `BrainRuntime` also rejects any activation-category input that reaches its handler outside `STANDBY`. The ordinary ACTIVE parser is still responsible for returning typed, silent `attention_only` for `Ares`, `Aris`, `RS`, `Hey Ares`, and `Hello Ares`, but parser correctness is no longer the only guard against reactivation.

`core.BrainRuntime` belongs to Capital/Core and composes the existing `BrainSessionManager`, `CoreService`, one injected input adapter, one injected output adapter, and an injected production text-command handler. It does not implement skills, persistent memory, STT, TTS, hardware access, or City lifecycle. Runtime execution is serialized; no worker, daemon, or background timer thread is created.

The normal foreground flow is:

```text
STOPPED -> BOOTING -> INITIALIZING -> STANDBY
STANDBY -- exact activation --> ACTIVE
ACTIVE -> PROCESSING -> RESPONDING -> ACTIVE  (repeated commands, same session ID)
ACTIVE -- owner stop/inactivity --> RETURNING_TO_STANDBY -> STANDBY
supported state -- explicit shutdown --> SHUTTING_DOWN -> STOPPED
```

In `STANDBY`, ordinary commands are ignored and no session ID exists. Exact normalized `ares`, `hey ares`, `hello ares`, or `wake up ares`, with `ares`, `aris`, or `aries` in the final name slot, activates one session and emits the configured `Yes Gabi.` acknowledgement only after the lifecycle transition succeeds. Wake activation is never evaluated as an action from `ACTIVE`, `PROCESSING`, `RESPONDING`, `RETURNING_TO_STANDBY`, or `SHUTTING_DOWN`. While active, ordinary commands use the existing `TranscriptNormalization -> SkillManager -> IntentParser -> Planner -> ExecutionPipeline -> Skill` route bound to the same CoreService, so calculator and central owner-memory behavior are unchanged.

`core.LifecycleControl` retains its deterministic state-specific text entry points for simulated input, attention-only handling, diagnostics, and post-Whisper lifecycle fallback. Real ACTIVE audio authorization is ordered: accepted constrained evidence first; otherwise an exact valid result from the existing Whisper lifecycle normalizer; otherwise ordinary CoreService routing. A constrained rejection therefore cannot veto `Goodbye, Aris.` or `Shut down Aris.`, but failed, empty, negated, descriptive, or unmatched Whisper text cannot request a transition. Text normalization still reports raw, cleaned, command-body, canonical-name, removed-alias/type/position, negation, action, matched-phrase, and rejection fields. Arbitrary substrings are never rewritten and there is no fuzzy, edit-distance, semantic, or learned-alias path.

The constrained alias stage has one structural authority shared with grammar generation. Its assistant-name slot accepts only `ares`, `aris`, `aries`, `arris`, `rs`, or multi-token `r s`, then canonicalizes that complete slot to `ares`. Seven standby templates place the slot after `goodbye`, `good bye`, `bye`, `go standby`, `go to standby`, `standby`, or `sleep`. Six shutdown templates place it after `shutdown`, `shut down`, `turn off`, or `power off`, or before `shutdown` or `shut down`. Only these complete edge-slot structures are rewritten. Name-only input, `where is Ares`, `I spoke to Aris yesterday`, `Paris`, `Harris`, `the artist is here`, `remember that I like Ares`, `calculate two plus two`, `shut down the computer`, `goodbye everyone`, extra words, and substrings remain non-actions. The 49 bounded competitors and `[unk]` reduce forced positive decoding without becoming aliases.

Each constrained request gives child work a 5-second deadline followed by bounded terminate/kill/reap cleanup. Standby confidence tiers are high at `0.70` and medium at `0.50`; shutdown is stricter at `0.78` and `0.60`. Confidence is accepted only when backend tokens align with the normalized complete phrase. High exact evidence may transition. Medium evidence is explicitly non-authoritative and requires same-turn Whisper fallback; rejected constrained evidence does too. A complete valid Whisper lifecycle result may then transition, while failed, empty, negated, descriptive, or unmatched text remains ordinary. No second-turn confirmation state is created by this production decision path.

Exact ACTIVE name-only or greeting-plus-name input remains `attention_only`: it bypasses CoreService, keeps the same session ID and inactivity deadline, and is silent when the optional `already_active_acknowledgement` is empty. It never becomes standby or shutdown. Accepted constrained audio standby/shutdown actions transition through `BrainSessionManager` exactly as before: standby follows `ACTIVE -> RETURNING_TO_STANDBY -> STANDBY`, clears the session, and leaves the process alive; shutdown follows `ACTIVE -> SHUTTING_DOWN -> STOPPED` once. Negated, descriptive, scheduled, missing-name, extra-word, `[unk]`, low-confidence, and malformed candidates cannot execute. Cross-category collisions and malformed configuration fail closed.

The manager's injected clock and 30-second deadline provide inactivity semantics. The foreground input adapter performs bounded waits; only an adapter timeout at or after the exact deadline initiates standby. An input item returned at the boundary is processed serially and refreshes activity, so a successful input is not raced by a second timer. Clock rollback is clamped. No timeout is evaluated while `PROCESSING` or `RESPONDING`.

The V1 runtime contracts are `BrainRuntimeRequestV1`, `BrainRuntimeResultV1`, `BrainRuntimeSnapshotV1`, `BrainRuntimeCommandClassificationV1`, and `BrainRuntimeLoopResultV1`. Injected adapters are defined in `core.BrainRuntimeAdapters`: deterministic queue/collecting adapters support tests, while bounded console adapters support explicit foreground text verification. Runtime events are `brain_runtime_started`, `brain_runtime_input_received`, `brain_activation_requested`, `brain_activation_accepted`, `brain_activation_rejected`, `brain_runtime_command_started`, `brain_runtime_command_completed`, `brain_runtime_command_failed`, `brain_runtime_inactivity_expired`, `brain_runtime_standby_requested`, `brain_runtime_shutdown_requested`, and `brain_runtime_stopped`. They record category, state, lengths, timing, selected skill, status, and correlation/session IDs only; full input text, response values, owner-memory values, audio, secrets, and files are excluded. Production composition creates one `EventHistoryStore` and injects that exact object into runtime, session manager, CoreService/resource management, SkillManager, and voice pipeline; output adapters do not construct per-turn stores.

`scripts/run_ares_standby_voice.py` owns one process-exclusivity lock at the configured `data/runtime/ares_standby_voice.runtime` target. A second live process exits with `ARES is already running`. The lock is released by normal shutdown, Ctrl+C, and handled setup/runtime exits; verification scripts use isolated temporary runtime locks. Before every foreground exit the launcher reports an explicit outer terminal reason, such as `explicit_shutdown_command`, `owner_cancellation`, or `unrecoverable_failure`.

Hardware-free verification:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python scripts/manual_verify_brain_runtime.py
python scripts/run_ares_brain_runtime_text.py
```

The second command is the stable developer text interface. It is not a microphone path, service, or boot hook. The separately injected foreground wake adapter below does not alter this text mode; systemd/boot startup remains later and unimplemented.

# Foreground Standby Wake Runtime

Standby and ACTIVE audio have separate readiness semantics. The standby listener retains its established continuously pumped stream and wake profile. ACTIVE uses a fresh one-shot ALSA stream after TTS completion and the 0.35-second playback-settling gate. Pipeline startup then performs the existing 0.75-second ambient calibration. A synchronous VAD -> ALSA adapter -> single-turn pipeline -> runtime-input callback fires only when calibration transitions to `WAITING`; the owner prompt is printed from that callback, never before it. The next PCM frame therefore belongs to a real owner-speech window. The `active_command_v1` request wrapper preserves the configured thresholds, maximum duration, device, and canonical 16 kHz mono `S16_LE` format while enforcing at least 0.5 seconds of rolling pre-roll and 0.9 seconds of continuous terminal quiet. All retained frames are immutable, the first qualifying/start-evidence frame is appended once, speech beginning inside a 640-byte frame is preserved, and no leading candidate audio is trimmed before Whisper.

The old production ordering printed readiness immediately before `SingleTurnVoicePipeline.run_once()`. Its subsequent calibration correctly consumed and excluded the first 0.75 seconds; owner speech begun at the prompt could therefore lose an initial `goodbye`, `shutdown`, `calculate`, or `remember` and leave only later words. This was an ordering defect, not evidence for changing thresholds or mixer gain. The corrected callback also provides a production-composition test seam: `run_ares_standby_voice.py` is tested with the actual state-aware input adapter, central active normalizer, lifecycle authority, shared playback gate, active profile, and ready-after-calibration ordering.

`core.StandbyWakeListener` defines the bounded adapter contract used by Capital/Core while the lifecycle state is `STANDBY`. `core.LinuxStandbyWakeListener` is the Raspberry Pi/Linux implementation. It owns no lifecycle, skill, memory, or routing state. `BrainRuntime` starts, calls, cancels, and stops it; every activation still goes through `BrainSessionManager.activate_session()`.

The staged path is:

```text
BrainRuntime (STANDBY)
  -> LinuxStandbyWakeListener
  -> LinuxAlsaMicrophoneAdapter calibrated RMS candidate capture
  -> canonical 16 kHz mono signed 16-bit WAV
  -> WakeRecognizer interface
  -> VoskWakeRecognizer with a constrained local grammar and word confidence
  -> exact complete-phrase classification
  -> BrainSessionManager (ACTIVE)
  -> one "Yes Gabi." acknowledgement
  -> existing SingleTurnVoicePipeline transport
  -> LinuxWhisperSpeechToTextAdapter with the base English command model
  -> CoreService / SkillManager / IntentParser / Planner / ExecutionPipeline / Skill
  -> existing Piper and ALSA response output
```

The Linux listener opens one raw canonical ALSA stream when `BrainRuntime` enters `STANDBY`; it does not reopen the device after each timeout or rejected candidate. Calibration samples one fixed three-second PCM window per standby epoch. The former policy required 30 uninterrupted 20 ms frames below the fixed pre-calibration speech-start RMS of 200 and erased all progress on any louder frame; constant USB hiss therefore caused `calibration_non_speech_window_not_found` despite a working device. The replacement `rms_percentile_bootstrap_v1` policy collects the bounded sample first, selects the quietest 25 percent of valid frames, and uses that subset's median instead of an absolute minimum or a perfect-silence window. A provisional floor plus bounded multiplier and absolute margin identifies high-energy speech bursts for exclusion. Quality gates reject insufficient frames, inadequate quiet coverage, speech-dominated input, severe clipping, all-zero PCM, invalid format, and noise above the configured usable limit. One likely speech-contaminated attempt gets one bounded retry after history/backlog reset; there is no indefinite retry and Vosk is never invoked during calibration.

The persistent raw boundary uses one canonical contract shared by capture and diagnostics: 16,000 Hz, one channel, signed 16-bit little-endian `S16_LE`, with each 20 ms frame containing 320 samples and 640 bytes. `SubprocessPcmFrameSource` copies every bytes-like low-level result before retaining it, accumulates arbitrary short reads losslessly, preserves extra bytes from multi-frame reads, and delivers only immutable complete frames. `ContinuousPcmFrameSource` is the sole stdout consumer and pumps those frames into a bounded latest-audio queue for the lifetime of the one owned process. The rolling source makes another immutable boundary copy before live history, pre-roll replay, or VAD delivery. Empty reads, odd-byte corruption, incomplete EOF, dead-process exits, and read errors fail without zero filling. Candidate reset advances a pump epoch, discards only queued or in-flight data from before that boundary, and never starts a competing descriptor reader.

Integrity observability is layered rather than inferred from one high-level duration. The source and rolling snapshots expose `total_low_level_reads`, `valid_full_pcm_frames`, `partial_reads`, `empty_reads`, `read_errors`, `discarded_bytes`, `zero_filled_bytes`, `repeated_frame_hashes`, `mutable_buffer_reuse_detected`, and `valid_microphone_bytes_delivered_to_vad`, alongside fresh/live and replay counters. Low-level reads need not equal full frames because a pipe read may be short. Equal consecutive frame hashes are signal evidence, not duplicate assembly; the existing frame-index duplicate check remains separate. VAD-delivery bytes include intentional pre-roll replay, while fresh live bytes are separately counted. Raw `arecord -t raw` has no WAV header, so the configured 16 kHz/mono/`S16_LE` values are reported as the requested canonical contract, not as an independently negotiated hardware measurement. Direct and serialized diagnostic WAV headers are independently validated where headers exist.

Successful calibration selects a wake-only threshold policy and leaves the stream open in `HEALTHY`. The validated policies are `conservative`, `normal` (default), and `sensitive`; they derive ordered start/continuation/silence gates from the greater of the robust floor and measured median using bounded multipliers plus absolute margins. Every profile enforces `start > continue > silence > ambient` and retains two-frame start evidence. This fixes the owner-observed case where ambient was roughly 389-430 RMS but the generic gate reached about 658.5 RMS and promoted no short wake speech. Active-command VAD does not use these policies. Calibration remains valid until the configured 300-second interval expires, an owner requests recalibration, the clock rolls back, the device changes, or stream recovery starts a new epoch. Failure attempts a bounded ALSA close and leaves the listener `FAILED`; only confirmed closure clears candidate/calibration state and releases capture ownership, while failed closure retains the same handle/gate for retry. Health data distinguishes a failed open, a retained failed-close handle, and an open that produced PCM and was deliberately closed during cleanup. Fresh calibration remains authoritative and this checkpoint does not use a cached noise profile. A bounded rolling source retains at most 100 frames and replays only the configured 0.3-second pre-roll at the next VAD window, so speech beginning at a foreground poll boundary is not discarded. Candidate completion resets rolling history and advances the bounded transport epoch so stale tails from recognition cannot enter the next candidate. The only added thread is the adapter-local bounded PCM transport pump (plus its isolated stderr drain); neither is a listener, lifecycle owner, recognizer, or command loop.

Wake VAD has three semantic phases. `WAITING_FOR_SPEECH` keeps the same calibrated stream live for up to 5 seconds and retains 0.3 seconds of pre-roll; replayed pre-roll and owner wait time do not consume the utterance safety budget. Two qualifying 20 ms frames enter `RECORDING_SPEECH`. A below-continuation frame enters `POSSIBLE_END_OF_SPEECH`; 0.9 seconds of continuous calibrated quiet completes the candidate, while one genuine continuation-level frame resumes recording and resets the terminal timer. Wake mode accepts as little as 0.08 seconds of speech evidence, retains 0.15 seconds of bounded post-speech grace, and starts its 4-second failsafe only when speech starts. The full-command VAD profile remains unchanged. Threshold validation enforces start greater than continue, continue greater than silence, and silence above the measured ambient reference, with independent minimum and maximum clamps. Frame-index assembly verifies that pre-roll, speech, and post-roll PCM are appended once. Results distinguish `speech_wait_timeout`, `completed_after_terminal_silence`, and `maximum_speech_duration_reached` instead of reporting pre-speech waiting as an utterance maximum. Beginning-clipping status is meaningful only after speech exists: no speech reports `beginning_clipped=false` with `not_applicable`, complete speech pre-roll reports `no`, and detected speech with incomplete pre-roll reports `yes`. Owner-facing mappings may express the same controls under `wake_capture` as `speech_wait_timeout_seconds`, `terminal_silence_seconds`, `maximum_speech_seconds`, `pre_roll_seconds`, `required_start_frames`, `required_continue_frames`, and `post_speech_grace_seconds`; these translate to the established internal fields, while unknown, malformed, or conflicting flat/nested definitions are rejected.

Before Vosk, a closed canonical 16 kHz mono signed 16-bit WAV is trimmed only beyond 0.24 seconds of leading and 0.20 seconds of trailing padding. The recognizer receives this validated file, not an open writer or unbounded raw capture. Calibration diagnostics report frame count/duration, RMS minimum/median/p20/p80/maximum, speech and non-speech counts, longest non-speech run, bootstrap threshold, quiet-sample coverage, selected floor, clipping/zero counts, bounded rolling summaries, threshold output, and quality reason. Diagnostic wake capture snapshots cumulative source/read sequences and live/returned byte counts before and after every poll. A bounded per-frame trace records sequence, timestamp, byte count, RMS, start decision, consecutive evidence, and VAD transition, proving whether the calibrated stream continued feeding the listener. The verifier classifies failures as absent post-calibration PCM, frames below the gate, candidate assembly failure, or recognizer rejection rather than collapsing all four into `no_speech_timeout`. Read-only ALSA diagnostics expose capture, microphone boost/input gain, and detectable AGC controls and may warn about extreme gain; they never mutate mixer state. The existing ALSA normalization boundary prevents 44.1 kHz hardware data from being relabeled as 16 kHz.

Whisper is not used in `STANDBY`. Real Raspberry Pi recordings showed that tiny Whisper could force a single spoken `Ares` into unrelated outputs such as `Alrighty`, `Okay`, or `Bye`; accepting those outputs as aliases would create dangerous false activations. `VoskWakeRecognizer` instead loads one local small English model at listener startup and constructs an activation-only grammar from shared exact aliases `ares`, `aris`, and `aries`, the empty/`hey`/`hello`/`okay`/`wake up` prefixes, and `[unk]`. Standby/shutdown controls remain absent from that STANDBY grammar. ACTIVE uses a separate `VoskLifecycleGrammarBackend` over the already-finalized command WAV and a separate, narrower lifecycle grammar; it does not invoke wake activation or open the standby stream. `Aries` remains a wake-only exact name slot and is not an ACTIVE lifecycle-audio alias. No model is downloaded at runtime. The capability manifest declares a 320 MB RAM estimate, normal recognition CPU weight, medium startup cost, serialized execution, and persistent model lifetime.

Wake recognition fails closed. Vosk word output is enabled and the minimum confidence across all returned words controls a two-tier policy derived from owner-observed short-word results. An exact wake at or above `0.55` activates immediately. An exact activation from `0.40` through less than `0.55` remains rejected until the same complete phrase is recognized twice within eight seconds; controls never use this medium-confidence path. Confidence below `0.40`, `[unk]`, wrong phrases, or extra words reject. Missing confidence is accepted only when Vosk returns exactly one permitted activation phrase and the listener marked the candidate valid after VAD speech evidence, canonical WAV validation, and duration checks; missing-confidence controls and malformed candidates reject. This exception is not available to unrestricted Whisper transcription. The complete punctuation/case/whitespace-normalized result must normally equal one configured grammar phrase. There is no substring check, edit distance, learned alias, arbitrary fuzzy correction, or standby Whisper fallback. `okay` alone, `bye`, `alrighty`, `areas`, `air`, partial words, zodiac questions, and ordinary sentences containing Ares are non-wake speech.

After endpoint correction, one narrowly bounded wake-only recovery handles a residual recognizer artifact: exactly two identical normalized configured alias tokens (`ares ares`, `aris aris`, or `aries aries`) may collapse to canonical `ares`. Both surface tokens must be the same alias, no unknown/prefix/other token may exist, the validated candidate must be nonzero and within the configured 4-second wake bound, and each token must have its own Vosk word/confidence entry that satisfies the existing policy. A single malformed Vosk word entry containing multiple normalized tokens fails closed before duplicate collapse. The decision uses the minimum confidence and diagnostics expose the original tokens, canonical tokens, minimum and mean values, collapse status, and the actual recognized surface alias even though the accepted identity is canonical `ares`. Mixed aliases such as `ares aris`, `[unk] aris`, three or more tokens, prefix duplication, missing-confidence duplicates, over-duration audio, and unrelated repeated words reject. This exception does not alter active-command text.

`WakeRecognizerRequestV1` and `WakeRecognizerResultV1` form the recognizer boundary. `LinuxStandbyWakeListener` depends on that protocol, not Vosk directly, and has no Whisper import or transcript parser. Result contracts expose classification, confidence availability, selected alias, status, timing, and safe metadata but no transcript or audio bytes. `WakeRecognizerLocalDiagnostics` can retain the raw Vosk JSON only in process for an explicitly enabled owner-terminal callback.

One completed wake candidate is represented by one frozen `WakeAttemptResult`. Its unique attempt and candidate IDs bind the ALSA stream instance/generation, capture validity and canonical audio metadata, recognizer invocation/result, classification, lifecycle before/after, infrastructure-failure flag, and cleanup outcome. The result is assembled only after candidate cleanup; runtime and diagnostic rendering retrieve it by exact attempt ID rather than reading independent mutable `last_result` and `last_diagnostics` snapshots. Contract invariants reject recognition text or confidence on invalid/empty audio, Vosk invocation on zero-duration/noncanonical audio, and wake acceptance without an invoked recognizer. A stream-generation mismatch clears the recognition data and fails the attempt as infrastructure, so recovery cannot relabel an older transcript as a newer candidate.

The persistent listener uses the explicit stream recovery states `CLOSED`, `OPENING`, `CALIBRATING`, `HEALTHY`, and `FAILED`. A stream is not healthy until calibration succeeds. Calibration or device recovery failure attempts to close the exact ALSA handle, clears rolling pre-roll, candidate history, and recognizer attempt state only after ownership is resolved, and returns a structured failure. A failed close is transactional: the same handle, calibration state, and capture-gate owner remain retained for retry, so another capture cannot enter while ALSA may still be locked. A successful reopen increments the generation; results from older generations cannot activate or appear in the new attempt diagnostics. Each Vosk candidate constructs a fresh constrained `KaldiRecognizer`, while the model remains loaded. No-speech, invalid-audio, calibration-failure, recovery, and exception paths explicitly reset ephemeral recognizer diagnostics.

Health is split by subsystem. Vosk model health constructs a constrained recognizer and feeds a deterministic silent PCM probe without opening or consuming the owner microphone. Microphone-adapter health, ALSA-device-open state, calibration state, stream state/generation, and last recovery reason are reported separately. Startup preserves the original component failure instead of replacing it with the generic `wake_recognizer_unhealthy` label.

`core.BrainRuntimeVoiceAdapters` reuses `SingleTurnVoicePipeline` as the active microphone/STT transport and as the TTS/speaker output boundary; it does not duplicate those subprocess paths. Acknowledgement, ordinary responses, standby confirmation, and shutdown speech use `run_local_output()`, an output-only synthesis/playback path that creates no fake input turn, emits no `voice_single_turn_started`, and never calls CoreService or skills. A shared `VoiceRuntimeGate` makes microphone ownership explicit. `standby_wake` owns capture only in `STANDBY`; the listener closes its stream before activation acknowledgement, and active-command capture cannot acquire the device during playback. After playback ends, the gate enforces the existing 0.35-second settling interval before a fresh one-shot active capture opens. The command adapter releases capture immediately after recording closes and finalizes the WAV, before Whisper inference. The gate rejects any second simultaneous capture owner. Normal operation never plays captured owner audio, so response PCM cannot become the next owner command.

After capture, the finalized-audio hook runs before Whisper. High-confidence exact constrained evidence is transported as a typed command and bypasses CoreService and Whisper. Medium/uncertain and rejected constrained evidence is non-authoritative and requires same-turn Whisper fallback on the same closed WAV. Its untouched transcript reaches the existing exact ACTIVE lifecycle normalizer before ordinary routing. A valid Whisper standby/shutdown result executes; ordinary, failed, or rejected text continues to CoreService. Completion of any bounded voice turn is transport-local, not completion of the persistent foreground runtime. Active-command EOF, empty or falsey transcription, no speech, recognizer/backend failure, timeout, lifecycle mismatch, and transcription failure remain nonterminal.

Deterministic tracing found that the previous `goodbye` misses came from absent `good bye` compound normalization plus lifecycle classification split downstream of generic transcript cleanup. The opaque post-`Transcribing command` exit had no retained old diagnostic, but the launcher did suppress the loop stop reason behind one generic clean-stop message; source-local EOF handling and the loop's negative-list success rule also admitted false-terminal or false-clean interpretations. Tests disproved completed `SingleTurnVoicePipeline.run_once()` as an outer-runtime terminal collision by itself. The repaired contracts make source-local EOF nonterminal, define successful outer termination positively as an explicit shutdown, and print the terminal reason, without claiming which old terminal path occurred on the Raspberry Pi turn whose reason was not reported.

Production terminal status makes this handoff visible with `ARES is waiting for your command...`, capture-start, speech-detected, command-captured, constrained recognition, optional transcribing, and processing messages. The waiting message is emitted only after the settle wait succeeds and `active_command` owns the capture gate; a shorter runtime poll that expires inside settling emits no prompt, eliminating duplicate messages for one phase. With diagnostic routing enabled, the active-command section reports constrained recognized text/tokens, confidence/tier/backend, classification/canonical phrase/rejection, confirmation requirement, selected action, whether Whisper fallback ran, the optional raw Whisper transcript, CoreService bypass, state/session before and after, pipeline status, runtime-terminal flag/reason, capture timing, canonical WAV metadata, routing, cleanup, and terminal-silence status. Wake-candidate and active-command diagnostics are separate; neither transcript enters events, memory, or persistent logs.

The owner-observed terminal stopping at `Transcribing command` established the boundary at which foreground progress disappeared, but it could not identify whether inference, child cleanup, or a following event append held control. Static tracing exposed two independent defects in that path. The former `WhisperSubprocessRunner` launched no new session, signalled only the leader, and could leave cleanup with `reaped=False` after descendants inherited its pipes. `EventHistoryStore` also acquired its in-process `RLock` without a deadline before reaching its already fail-open file-lock handling. The revised boundaries repair both defects without claiming that the Raspberry Pi log alone distinguished them.

Active command inference is bounded at the speech-engine process group. `SingleTurnVoiceRequestV1.transcription_timeout_seconds` reaches `LinuxWhisperSpeechToTextAdapter`; the Raspberry Pi launcher sets a 15-second inference timeout independently of its broader 300-second process budget. `WhisperSubprocessRunner` uses `Popen(..., shell=False, start_new_session=True)` on Linux, records PID/PGID, writes stdout/stderr to private bounded temporary files, and enforces wall time with parent-owned monotonic `poll()` checks rather than one blocking `wait()`. Timeout sends group `SIGTERM`, waits at most one second, escalates to group `SIGKILL`, then verifies exit, reaps, and closes handles within a three-second cleanup deadline. The same bounded process primitive protects one-shot `arecord`, Piper, and `aplay`; persistent `arecord` owns its own process group and bounded close path.

`ForegroundSignalCoordinator` is the sole SIGINT/SIGTERM owner for the production foreground launcher and active lifecycle diagnostic. A first signal requests cancellation from active speech/audio adapters and raises `ForegroundTerminationRequested` so the main stack reaches one idempotent final cleanup path. `SingleTurnVoicePipeline` and the runtime voice adapters never convert `KeyboardInterrupt` into fake completion. Cleanup may run twice safely. Before a diagnostic advances to phrase two it verifies that the prior Whisper PID and arecord stream are absent, the capture/playback gate is idle, cancellation is clear, and the next temporary path is unique. Diagnostic progress timestamps cover capture, Whisper start/completion/timeout, TERM/KILL, reap, microphone release, and file cleanup.

`transcription_timeout` is a typed, retryable active-input failure. The current audio is removed under the production delete-always policy, the microphone gate is released, raw text never reaches lifecycle classification or `CoreService`, and `BrainSessionManager` remains in `ACTIVE`. Nonzero exit, malformed/empty output, missing or header-only audio, and noncanonical PCM follow the same nonterminal safety rule. Only an exact successfully transcribed shutdown command can produce `explicit_shutdown_command`; cancellation and unrecoverable failure remain separately named.

`scripts/manual_diagnose_active_transcription.py` composes the production `LinuxAlsaMicrophoneAdapter`, canonical WAV boundary, `LinuxWhisperSpeechToTextAdapter`, and `WhisperSubprocessRunner` for one bounded hardware probe. It prints the finalized WAV header, exact command, PID/PGID, timeout, elapsed time, exit, transcript, signal escalation, reap, handle closure, microphone release, and exact-file cleanup. It does not construct command routing, lifecycle, or persistent owner-state services. Deterministic tests prove process-group escalation, bounded output transport, retry state, cleanup, and lock contention. Audible capture and timely completion with the installed Raspberry Pi Whisper binary remain owner-run hardware evidence.

Store write locks use versioned `ares.file_lock` metadata containing PID, hostname, creation time, owner kind, and a random owner token. Acquisition and release are context-managed; release removes only the caller's matching token. `scripts/inspect_store_lock.py` reports ownership and can recover only an expired local lock whose PID is proven dead. It never steals a live, remote, malformed, or unprovable legacy lock, and recovery uses an atomic filesystem replacement. A legacy timestamp lock from the previous implementation can be recovered automatically only when its timestamp predates the current Linux boot. If an event-history lock cannot safely be recovered, appends degrade with a warning while live Brain behavior continues; critical owner-memory transactions still fail closed.

`scripts/cleanup_ares_voice_processes.py` is the bounded emergency owner tool. It identifies ARES foreground runtime/diagnostic Python processes, follows only their allowlisted `arecord`, `whisper-cli`, Piper, and `aplay` descendants, displays PID/PGID/command ownership, sends TERM, escalates after a bound, and reports survivors. An orphan audio child is eligible only when its executable is allowlisted, its parent is init, and its working directory is the repository. Optional lock cleanup delegates to the same dead-local-PID and age policy. It never uses a broad process-name kill or removes a live owner's lock.

`scripts/inspect_ares_runtime_state.py` adds a read-only process-and-lock preflight for hardware verification. It lists matching ARES Python processes with PID, command line, and start time, then inspects both the foreground runtime lock and event-history lock with owner PID, age, liveness, and recovery eligibility. Optional recovery delegates to the same validated dead-local-owner policy; a live owner is never stolen. The bounded hardware verifier uses its own temporary event-history store and runtime lock, so it neither acquires nor leaves the production `data/event_history.json` lock.

The listener contracts are `WakeListenerRequestV1`, `WakeListenerResultV1`, `WakeDetectionResultV1`, `WakeListenerSnapshotV1`, and `StandbyListenResultV1`. Events contain state, status, classification, lengths, safe audio metadata, and correlation/session identifiers only. They exclude recognition text, owner-memory values, raw audio, file contents, and secrets. `WakeLocalDiagnostics` is deliberately ephemeral and may be sent only to an owner-injected terminal callback under `--diagnostic-wake`; it is never an event or persistence contract. Temporary candidate directories are unique and removed by default. Retention requires both `--diagnostic-wake` and `--retain-diagnostic-audio`, is bounded to the latest candidate by default, and only prints a manual playback command.

Foreground Raspberry Pi verification:

```bash
cd ~/ares-brain
source venv/bin/activate
git pull --ff-only origin main
python -m pip install -r requirements.txt
mkdir -p models/vosk
curl -fL https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o /tmp/vosk-model-small-en-us-0.15.zip
unzip -q /tmp/vosk-model-small-en-us-0.15.zip -d models/vosk
rm /tmp/vosk-model-small-en-us-0.15.zip
python scripts/manual_diagnose_persistent_pcm.py --microphone-device plughw:2,0 --speaker-device plughw:CARD=Device,DEV=0 --record-seconds 3 --playback
python scripts/manual_diagnose_wake_word.py --diagnostic-wake --vosk-model models/vosk/vosk-model-small-en-us-0.15
python scripts/manual_verify_standby_wake_hardware.py --diagnostic-wake --wake-vad-sensitive --wake-reliability-attempts 10
python scripts/manual_verify_standby_wake_hardware.py --verification-mode standby --attempts-per-test 3 --diagnostic-routing --diagnostic-wake --wake-vad-sensitive
python scripts/manual_verify_standby_wake_hardware.py --verification-mode shutdown --attempts-per-test 3 --diagnostic-routing --diagnostic-wake --wake-vad-sensitive
python scripts/run_ares_standby_voice.py
```

`scripts/manual_diagnose_persistent_pcm.py` is a mandatory prerequisite before any further wake-reliability run. It acquires the foreground runtime lock and performs four bounded stages: three quiet and three spoken seconds through native `hw:2,0` at 44.1 kHz, followed by three quiet and three spoken seconds through the exact production `plughw:2,0` 16 kHz persistent implementation. It saves four separate WAVs plus one JSON report. That report carries real headers, signed sample distributions, amplitude bands, direct and persistent spoken/quiet RMS ratios, cross-path comparability, partial/read-size/queue accounting, exact commands, PID/exit status, and terminal integrity state. `--playback` runs only after capture releases ALSA and success additionally requires explicit owner confirmation that both spoken WAVs are clear and audible. Local tests cannot establish Raspberry Pi amplitude.

The code-level root cause is classified: the prior persistent process was open continuously but its stdout was consumed only when calibration or VAD synchronously requested a frame. During prompt, recognition, and other consumer gaps, quiet PCM remained at the head of the pipe and `arecord` could block; later VAD reads could process that stale quiet backlog while the owner was speaking live. Direct capture did not exhibit the defect because `arecord` continuously wrote its WAV file. The separately piped but undrained ALSA stderr was a second backpressure risk. This precisely explains exact 640-byte frames with RMS 3-7, intermittent successful recognition, and zero-duration no-speech candidates without requiring a threshold or confidence change.

Only after that prerequisite passes, `scripts/manual_diagnose_wake_word.py` may capture exactly one candidate and the hardware verifier may proceed. The verifier first reports matching ARES processes and production lock ownership, refusing to compete with a live microphone owner. It then checks Vosk model, microphone adapter, ALSA open, and calibration health separately. The reliability phase prompts ten valid `Ares` attempts through one listener while the runtime remains in `STANDBY`; infrastructure failures are reported separately and do not masquerade as recognition misses or enter the recognition denominator. Before each attempt it clears only stale pre-prompt candidate history, prints `Ready for attempt N. Say 'Ares' now.`, keeps the existing stream live for a 0.6-second readiness interval, and starts the 5-second owner wait from that ready point. It leaves 0.5 seconds between every completed capture and the next prompt, including invalid-audio and other bounded infrastructure retries, without reopening or recalibrating ALSA. Each result is rendered from the exact immutable attempt before any infrastructure exclusion and includes IDs, stream generation, capture validity, raw/assembled/candidate duration, literal qualifying-speech duration, the complete post-start speech window, the first qualifying and last speech frames, speech wait/start timing, terminal confirmation/resets, PCM-integrity counters, recognizer invocation, original/canonical tokens and confidence in diagnostic mode, classification, lifecycle outcome, and cleanup. This phase must normally finish with one stream open and one calibration; explicit device recovery is generation-labelled. A medium-confidence exact candidate prints `Low-confidence wake detected. Say Ares once more.`, preserves recognizer confirmation state, and consumes the immediate second candidate before scoring that attempt.

Deterministic tests prove wake-state isolation, `attention_only` session/deadline stability, exact lifecycle-slot canonicalization, grammar coverage, false-positive rejection, high-confidence constrained bypass, mandatory medium/rejected Whisper fallback, ordinary calculator and owner-memory routing, session clearing/reactivation, inactivity, playback gating, timeout cleanup, event-lock degradation, and Ctrl+C cleanup. They cannot prove the Raspberry Pi child-process behavior or owner speech result. The owner-run two-phrase diagnostic and production acceptance sequence remain mandatory. `scripts/run_ares_standby_voice.py` intentionally remains foreground until an authorized shutdown control or Ctrl+C.

Real Raspberry Pi output exposed several independent defects. The latest ACTIVE lifecycle evidence is precise: Whisper returned `Goodbye, Aris.` and `Shut down Aris.` and the normal parser classified them correctly, while constrained Vosk emitted `goodbye rs` and `shutdown rs` and the old exact constrained table rejected them. The lifecycle-only slot fix admits `rs` without changing standby wake aliases, wake confidence, VAD, microphone gain, or ordinary transcript normalization. Earlier microphone-stream, calibration, wake-threshold, event-lock, and Whisper-process defects remain independently fixed; none is reopened by this change.

An earlier owner run proved persistent-stream ownership, one-time calibration, frame delivery, and Vosk recognition, but accepted only 6/10 wake prompts. Its timing defects were repaired by separating a 5-second pre-speech wait from a 4-second post-start failsafe and requiring 0.9 seconds of confirmed terminal quiet. A later hardware run exposed the lower transport regression described above: valid-size frames could still be stale because the pull-only stdout path backed up between consumers. The continuous pump repair does not change command-mode capture, Vosk grammar, confidence tiers, wake thresholds, or lifecycle ownership. Physical PCM amplitude/audibility and then 9/10 reliability remain owner-run Raspberry Pi proof.

The example listener configuration is enabled within its explicit configuration block but `enabled_modules.linux_standby_wake_listener` remains `false`. Its owner-facing `wake_capture` object uses the semantic phase names and defaults above; the loader continues to accept the established flat listener fields for backward compatibility, but refuses ambiguous duplicate definitions. The separate descriptive `voice_activity_capture.pcm_integrity` block records the fixed canonical contract and policies but is not parsed as a standby-listener override. This prevents strict wake configuration from accepting a second audio-format source and prevents generic module loading from starting capture. The foreground script opts in by constructing the adapter directly under `BrainRuntime` ownership.

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

VoiceService remains the boundary. The current real-audio surface includes explicit Linux ALSA capture through `LinuxAlsaMicrophoneAdapter`, constrained standby recognition through `VoskWakeRecognizer`, offline active-command transcription through `LinuxWhisperSpeechToTextAdapter`, offline Piper WAV generation through `LinuxPiperTextToSpeechAdapter`, explicit ALSA playback through `LinuxAlsaSpeakerAdapter`, controlled single-turn and bounded multi-turn pipelines, and the explicit foreground standby runtime. All providers remain replaceable and outside Brain business logic. Daemon/systemd startup, boot-time activation, GPT, cloud speech, and autonomous City activation remain later work.

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

## Production-Style Voice Launcher

`scripts/run_ares_voice.py` is an entry-point adapter around the existing production single-turn composition. It does not implement an alternate pipeline. It translates bounded CLI options into `SingleTurnVoiceRequestV1`, calls the same `scripts.manual_verify_single_turn_voice.create_pipeline()` factory, and invokes `SingleTurnVoicePipeline.run_once()`.

Before printing that ARES is listening, the launcher starts the lifecycle-managed pipeline, performs its side-effect-safe component health check, and stops that preflight reservation. Missing ALSA commands/devices, Whisper binary/model, Piper runtime/profile files, or voice configuration therefore fail before capture. The actual turn then follows the normal pipeline sequence and owns its own lifecycle/resource cleanup.

Repository-relative Whisper paths resolve from the script/repository location rather than the process working directory. Piper model/config resolution remains exclusively in `VoiceProfileRegistry`; the launcher knows only a profile identifier. Default response playback is enabled, while diagnostic WAV retention and captured-stage playback are independently disabled. `--diagnostic-routing` only prints bounded routing fields, `--retain-diagnostic-audio` only preserves intermediate files, and only `--play-diagnostic-audio` asks the existing speaker adapter to play captured stages. No option enables microphone monitoring.

The launcher is one foreground owner action and exits after one result. It does not add a wake word, loop, service, boot hook, transcript persistence, GPT, cloud fallback, or new hardware boundary.

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
  -> one foreground arecord raw PCM process (argument list, shell=False)
  -> continuous stdout pump reconstructs immutable exact PCM frames
  -> bounded latest-audio queue; separate bounded stderr drain
  -> RmsVoiceActivityCapture
  -> calibrate bounded ambient PCM frames
  -> derive start / continue / silence thresholds
  -> wait for consecutive speech frames
  -> preserve bounded pre-roll
  -> WAITING / SPEECH / POSSIBLE_SILENCE state machine
  -> append resumed speech and internal pauses to one ordered utterance
  -> trim only the confirmed consecutive terminal-silence suffix
  -> atomically write and validate one mono PCM WAV
  -> SingleTurnVoicePipeline
  -> Whisper only when capture succeeded
```

The V1 boundary consists of `VoiceActivityCaptureRequestV1` and `VoiceActivityCaptureResultV1`. Requests carry sample format, frame size, calibration policy, three threshold bounds, consecutive start/resume/end rules, hangover duration, speech wait timeout, utterance limit, pre-roll, selected device, and correlation/session IDs. Results carry ambient mean/median/p90/peak/noise-floor statistics, all derived thresholds, speech/trailing frame counts, speech start/end offsets, the final status, WAV path, duration, peak/RMS levels, stop reason, timing, and bounded transition diagnostics.

`ContinuousPcmFrameSource` is transport infrastructure inside `LinuxAlsaMicrophoneAdapter`, not a second listener or lifecycle owner. Its sole producer continuously drains binary stdout from the already-owned `arecord` PID, accumulates arbitrary short reads and multi-frame reads, and publishes only immutable frames of the exact configured size. Its 50-frame queue keeps recent audio and drops/counts the oldest frame under consumer backpressure. Reset epochs remove only queued or in-flight frames from before an explicit prompt boundary; frames arriving afterward remain eligible for pre-roll and VAD. Stderr has an independent bounded drain and is never merged with PCM. PID, poll result, EOF class, read-size counts, partial bytes, queue drops, reset drops, tiny-signal runs, and non-silent duplicate runs are observable. Odd/incomplete terminal bytes, an alive-process stdout close, a dead process, zero fill, or a pathological repeated non-silent payload fail closed.

Microphone closure is transactional across the adapter, listener, and `VoiceRuntimeGate`. A failed process cleanup retains the same stream handle, calibration state, and logical capture owner for a bounded retry; it does not increment close counters, report ALSA released, or permit a competing capture. Only confirmed adapter closure clears the handle and releases the gate. `BrainSessionManager` remains the sole session/lifecycle authority.

Initial Raspberry Pi policy defaults are 16 kHz, mono, 16-bit PCM, 20 ms frames, 0.75 seconds of calibration, lower bounds of start RMS 200 / continue RMS 140 / silence RMS 80, three consecutive start and resume frames, five confirming low frames, 0.9 seconds of terminal hangover, a 10-second speech wait, a 15-second maximum utterance, and 0.25 seconds of pre-roll. Median and p90 statistics prevent one transient peak from defining the noise floor. Thresholds are clamped to configured minima/maxima. After speech starts, only sub-continue frames update the noise estimate, with bounded adaptation; actual speech cannot lift the ambient baseline indefinitely.

The end-of-speech states are:

1. `CALIBRATING`: sample ambient frames without treating them as an utterance.
2. `WAITING`: require consecutive frames above the derived start threshold.
3. `SPEECH`: accept frames above the continue threshold.
4. `POSSIBLE_SILENCE`: buffer every frame in order; consecutive resumed speech commits the whole pending block, while completion commits every frame before the final consecutive terminal-silence suffix.
5. `COMPLETE`: trim only that confirmed suffix, preserve the prior utterance, and atomically validate the WAV.

This fixes the prior failure mode where every frame above one static silence threshold reset trailing silence. Post-speech noise below the continue threshold no longer extends capture indefinitely, and one click cannot resume speech. `maximum_utterance_seconds`, speech wait timeout, cancellation, bounded buffers, lifecycle/resource gates, and fixed-duration capture remain hard safety limits.

`SingleTurnVoicePipeline` selects calibrated `auto_stop`, calibration-disabled manual thresholds, or the preserved `fixed_duration` path through its existing request contract. No-speech or invalid-audio results stop before Whisper, Brain, Piper, and speaker execution. The bounded multi-turn session propagates the same capture settings per turn and applies its existing recoverable no-speech policy. `VoiceStageCoordinator` continues to enforce microphone/speaker and Whisper/Piper mutual exclusion.

# Canonical Linux Audio Capture Boundary

Real Raspberry Pi testing proved that a raw ALSA hardware device can accept a requested rate without supplying that rate. In the observed case, `hw:2,0` was requested at 16 kHz but `arecord` reported and wrote 44.1 kHz. The previous headerless streaming path sized 20 ms frames from the requested 16 kHz value, reinterpreted 44.1 kHz bytes at the wrong timing, and then labeled the output WAV as 16 kHz. That corrupted VAD timing and degraded Whisper input even though direct hardware recordings were clear.

The corrected boundary is:

```text
requested ALSA device
  -> resolve raw numeric hw:C,D to plughw:C,D for streaming VAD
  -> request S16_LE / mono / 16000 from ALSA plug conversion
  -> continuously drain raw stdout without relabeling source bytes
  -> accumulate and own exact canonical PCM frames only
  -> RmsVoiceActivityCapture
  -> atomically finalized canonical WAV
  -> reopen and validate actual header
  -> Whisper
```

Fixed-duration capture retains explicit raw-device configurability. It records to a unique raw WAV, reads that file's actual header, validates complete PCM data, downmixes supported channel layouts, converts supported PCM widths, resamples supported rates to 16 kHz, and atomically writes a separate canonical WAV. It never reinterprets source bytes at a different rate. Normal production defaults use `plughw:2,0` for ALSA conversion of the verified card 2/device 0 microphone and `plughw:CARD=Device,DEV=0` for speaker output. The integrity baseline deliberately uses native `hw:2,0` at 44.1 kHz and preserves that real header; those bytes are analyzed as 44.1 kHz and are never labeled 16 kHz.

The canonical contract is 16 kHz, mono, signed 16-bit little-endian PCM in a valid RIFF/WAV envelope. `core.WavAudio` owns normalization and header validation. `LinuxAlsaMicrophoneAdapter` owns ALSA resolution and subprocesses. VAD accepts only canonical PCM, while `SingleTurnVoicePipeline` reopens the finalized normalized path and refuses noncanonical adapter output before Whisper. Brain, CoreService, SkillManager, IntentParser, Planner, ExecutionPipeline, and skills do not know ALSA devices, source rates, resampling details, or diagnostic paths.

Format diagnostics remain structured on the V1 capture/result boundary: requested and resolved device, requested rate, actual source rate/channels/width, canonical rate/channels/width, raw/assembled/normalized paths and durations, frame/sample/byte counts, intentional leading/trailing trim, duration-invariant status, and final Whisper input path. `--preserve-diagnostic-audio` explicitly retains distinct raw capture, ordered VAD assembly, final normalized WAV, and transcript output in a unique per-turn directory without playing any of them. Legacy `--diagnostic-audio` has the same preservation-only meaning.

Diagnostic capture playback is a separate owner action. The manual single-turn verifier provides one flag per stage and `--play-diagnostic-audio` as an all-stage convenience. Those flags run only after capture has stopped and never enable microphone monitoring. Normal `--playback` is reserved for the generated Piper response WAV. Saving diagnostics, printing routing diagnostics, or enabling response playback cannot select a captured WAV. Unique temporary names, closed WAV writers, canonical revalidation, cancellation, lifecycle/resource gates, and microphone/speaker mutual exclusion remain mandatory.

# Post-VAD Utterance Assembly and Whisper Handoff

The original `POSSIBLE_SILENCE` implementation combined two different concepts: all frames seen since speech dropped below the continue threshold, and the consecutive terminal-silence suffix. It allowed the total pending-buffer length to satisfy the configured hangover while requiring only the smaller low-frame confirmation count. Completion then discarded the entire pending buffer. With repeated `SPEECH -> POSSIBLE_SILENCE -> SPEECH` transitions, low-energy words and internal pauses could accumulate in the final pending block and be removed together, even though ALSA and VAD had processed the complete stream.

The corrected assembly rules are:

1. Pre-roll is copied once when speech is confirmed.
2. Confirmed speech frames append once to one persistent utterance buffer.
3. `POSSIBLE_SILENCE` frames remain ordered in a separate pending block.
4. A confirmed resume appends that complete pending block once and clears it.
5. Completion requires the full configured count of consecutive frames below the silence threshold.
6. Only that final consecutive suffix is omitted; earlier pending speech and pauses append to the utterance.
7. Maximum-duration completion appends all pending frames rather than dropping them.

`core.WavAudio` calculates samples and duration from channels and sample width: `sample_count = pcm_bytes / (channels * sample_width_bytes)` and `duration = sample_count / sample_rate`. Already-canonical 16 kHz mono signed 16-bit PCM uses a lossless byte-for-byte normalization path. `LinuxAlsaMicrophoneAdapter` writes unique `raw_capture.wav`, `assembled_utterance.wav`, and `normalized_whisper_input.wav` stages, closes each writer before the next reader starts, and exposes the current turn's exact final path.

Before Whisper, `SingleTurnVoicePipeline` reopens the finalized normalized file and enforces `normalized_duration >= assembled_duration - duration_loss_tolerance_seconds`. The default tolerance is `0.05` seconds. An unexplained loss fails as `audio_duration_invariant_failed`; Whisper, Brain routing, Piper, and speaker playback do not run. This invariant is a corruption/truncation guard, not permission to remove speech.

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

The result preserves `raw_transcript`, `cleaned_transcript`, `extracted_calculator_expression`, and `normalized_command`. General commands are Unicode/whitespace/punctuation normalized. Arithmetic commands additionally support English number words zero through one thousand, negatives, decimals, plus/add/added-to, minus/subtract, times/multiplied-by, divide/over, and explicit spoken parentheses. The output is numeric/operator text such as `calculate 2 + 2`; the existing calculator still performs strict character validation, AST parsing, finite/bounded arithmetic, and never uses `eval()`.

Adjacent phrase blocks are collapsed only when they repeat beyond the configured limit. Thus `two plus two plus two` remains a legitimate three-term expression, while a longer exact adjacent Whisper loop is reduced and reported through `repetition_detected`, `repetitions_removed`, and `cleanup_rule`. Unsupported arithmetic words, malformed number grammar, and unsafe characters return a structured rejection before Brain execution. Unknown non-arithmetic requests remain unknown rather than being forced into calculator routing.

Natural-language calculator extraction is an anchored operation inside the same normalizer. Earlier Raspberry Pi transcripts exposed two finite-registry gaps: `I'll calculate ...` and natural `how much` variants. When a wrapper did not match, arithmetic-candidate detection still found the numbers/operator and the strict arithmetic parser correctly rejected the remaining wrapper word, including the observed `unsupported_arithmetic_word:much` result.

The V1 extractor accepts only registered forms: direct calculator actions (`calculate`, `compute`, `solve`, `work out`), bounded polite action requests, exact question forms (`what is`, `what's`, `how much is`, `what does ... equal`), exact nested forms (`tell me how much ... is`, `can you tell me how much ... is`), answer/result forms, first-person requests, and optional `Ares`, `Hello Ares`, `Hey Ares`, or `Hi Ares` address prefixes. Matching is case-insensitive after Unicode normalization and requires a word or punctuation boundary. Exact trailing `is`, `equal`, or `equals` is removed only for rules that declare that suffix.

The extractor never removes arbitrary middle words and never adds `much` to the arithmetic vocabulary. The remainder must be one complete supported arithmetic expression. Additional commands, a second instruction, identifiers, imports, function calls, attributes, assignments, paths, shell syntax, malformed operators, or unsupported words fail before routing. Non-arithmetic questions such as `How much is my house worth?` remain outside calculator routing. Successful or rejected wrapper extraction records `calculator_natural_language_wrapper`; repetition cleanup and wrapper extraction may both be represented. CalculatorSkill remains the sole arithmetic executor and its AST safety policy is unchanged.

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

The original VAD checkpoint collection was 912 tests. Adaptive calibration checkpoint collection was 959 tests. Production-registry routing checkpoint collection was 1002 tests. Natural-language calculator extraction checkpoint collection was 1058 tests. Format-safe capture checkpoint collection was 1079 tests. Current post-VAD assembly checkpoint collection: 1105 tests.

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

After Architecture Hardening, Phase 3 real voice integration proceeds only with explicit owner approval. Completed checkpoints now include ALSA capture/playback, offline Whisper, offline Piper, validated voice profiles, the controlled single-turn pipeline, its production-style one-command launcher, and the bounded owner-triggered multi-turn session. The next planned sequence is:

1. pull and run `python scripts/run_ares_voice.py` on Raspberry Pi hardware
2. record observed one-turn timing, stop reason, routing, response playback, and cleanup
3. continue bounded multi-turn hardware verification separately
4. only later consider wake-word/background listening

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

# General Explicit Long-Term Owner Memory

Persistent owner memory is a central Brain/CoreService capability. Voice is only one transport:

```text
voice / text / future client
  -> CoreService / Brain routing
  -> SkillManager -> IntentParser -> Planner -> ExecutionPipeline
  -> OwnerMemorySkill
  -> CoreService.execute_owner_memory(OwnerMemoryRequestV1)
  -> OwnerMemoryService
  -> OwnerProfileStore
  -> memory.schema_migrations
  -> data/memory/owner_profile.json
```

`CoreService` owns one `OwnerMemoryService`. `OwnerMemorySkill` consumes the versioned public service contract and does not open JSON. `OwnerMemoryService` is the only domain boundary that owns `OwnerProfileStore`. Voice launchers and voice pipeline modules neither import nor instantiate the store, and the memory package imports no ALSA, microphone, Whisper, Piper, or speaker implementation. Static architecture tests enforce those boundaries, reject voice-specific memory files, and verify production defines one canonical owner-profile path.

The skill manifest declares keyed-fact and general-memory remember, update, recall, forget, and list capabilities. `IntentParser` evaluates explicit owner-memory candidates before note/task/reminder rules. `remember that I like going to the gym` and `remember in long-term memory that I prefer wireless mice` select owner memory; `remember to buy milk`, `remember my task to buy a video game`, `remind me tomorrow to buy food`, and `create a task to go to the gym` remain task operations. An ordinary statement such as `I went to the gym today` never enters the owner profile.

`core.OwnerMemory` retains bounded keyed-fact parsing and aliases. `core.OwnerLongTermMemory` owns deterministic general-memory trigger cleanup, classification, validation, signatures, topic extraction, and retrieval scoring. Accepted activation families include explicit remember/do-not-forget forms and explicit save/store/keep/add/note long-term forms. The trigger boundary recognizes `long term`, `long time`, `long memory`, `permanent`, `persistent`, `lifetime`, and the observed Whisper substitutions `locked term`, `lock term`, and `long turn memory`. It also canonicalizes leading `remembering ... memory that` forms. A variant is rewritten only when it occurs near a leading memory verb and before the fact-introducing `that`; arbitrary occurrences of `locked` inside a fact are unchanged.

Trigger normalization runs before intent routing and preserves the raw/cleaned transcript separately. For example, `Remember in your locked term memory that I love going to the gym` becomes the routing command `remember longterm that I love going to the gym`; extraction stores only `I love going to the gym`. `Remembering a long term memory that I like video games` follows the same path. Structured diagnostics expose the canonical trigger, extracted fact, selected action, and routing reason without making the malformed trigger part of the profile. General content is classified into a finite set: preference, dislike, routine, personal fact, relationship, possession, goal, biographical fact, or instruction preference. Unclear but valid facts use `personal_fact`; no fact is invented.

The public `OwnerMemoryRequestV1` / `OwnerMemoryResultV1` boundary was evolved compatibly with optional structured memory, query, persistence, explicitness, confirmation, and diagnostics fields. Correlation/session IDs survive execution. The service returns created, duplicate, updated, recalled, forgotten, missing, listed, confirmation-required, rejected, or storage-failed outcomes. Spoken formatting remains in `OwnerMemorySkill`; the store never owns interface-specific wording.

`OwnerProfileStore` uses `ares.owner_profile` schema v3. Its `data` contains `owner_id`, the existing keyed `facts`, structured `memories`, and the legacy bounded `pending_delete_all` field retained for v3 compatibility. General entries contain `memory_id`, `memory_type`, subject/predicate/object, canonical and owner-spoken text, topics, persistence, source, confidence, timestamps, and active/superseded state. The registered migration path is sequential: v1 -> v2 enriches keyed facts; v2 -> v3 preserves every fact and adds empty general-memory and legacy confirmation fields. Current CRUD confirmation state is not authoritative owner memory and is stored separately. A valid v3 profile is read directly. Unknown future versions and malformed/corrupt stores fail closed and are never interpreted as empty memory.

Deterministic retrieval uses normalized key/type/topic/token and subject/predicate/object/canonical-text overlap. It returns at most five ordered matches and refuses weak one-token coincidences. Exact normalized signatures avoid repeat records; an explicit correction may supersede a matching active record, with at most 20 inactive revisions retained. This is bounded lexical matching, not semantic equivalence, embeddings, vector search, or an LLM.

`OwnerMemoryService` centrally implements list, inspect, count, specific-delete request, topic-delete request, all-general-delete request, keyed-fact-delete request, confirmation, and cancellation. `IntentParser` resolves these commands before generic task/device deletion only when they are explicitly memory-shaped. `remind me to delete the file tomorrow`, `remember to remove the rubbish`, and `create a task to clear the desk` remain task/reminder requests.

All destructive owner-memory operations are two-step. An exact specific match creates a pending request for one memory id. Multiple matches return clarification and create no pending request. Topic and all-general requests snapshot only the matching active general-memory ids; keyed facts are reported and preserved. Keyed-fact deletion stores the normalized key plus a revision digest, so a value changed after the prompt cannot be deleted by stale confirmation. The confirm path deletes only the stored snapshot: general memories added later are untouched.

Cross-process confirmation uses `PendingOwnerMemoryActionStore`, not the durable owner profile. Its single canonical production path is repository-root `data/runtime/pending_owner_memory_action.json`, with explicit isolated override `ARES_PENDING_OWNER_MEMORY_ACTION_PATH`. The versioned `ares.pending_owner_memory_action` v1 record stores an action id, owner id, operation, target kind, bounded target ids or keyed-fact key/revision, topic, candidate count, human-readable summary, timestamps, status, and the normalized request. It never stores raw audio or a full raw transcript. The default TTL is 60 seconds and validation rejects intervals above 300 seconds.

The transient record uses the shared write lock, deterministic UTF-8 JSON, flush/fsync where supported, and atomic replacement. A valid pending action intentionally survives the one-turn launcher process. `Yes, delete it`, operation-specific bounded confirmations, and cancellation phrases are considered before ordinary skill routing only when central pending state exists. Unrelated commands preserve pending state until expiry; explicit cancellation, successful confirmation, expiry, invalid state, and corrupt state clear or refuse it without profile mutation. A new destructive request replaces the old pending action. Vague requests such as `forget it`, `delete my memory`, and `remove everything` never create a target.

The profile limits keyed facts to 100, active general memories to 100, inactive history to 20, owner memory text to 320 characters, canonical text to 360 characters, topics to 8 at 48 characters each, retrieval/spoken output to 5 records, and serialized data to 65,536 bytes. Nested objects, binary/control content, path-like payloads, executable/import syntax, system-instruction changes, and protected credential/recovery categories fail before storage. Temporary `right now` facts return clarification instead of silently becoming durable memory. Operational events contain bounded action/status metadata, never fact values or raw transcripts.

The canonical path is defined once in production code as repository-root `data/memory/owner_profile.json`; `ARES_OWNER_PROFILE_PATH` is an explicit test/manual override. Mutations acquire a per-profile transaction lock and shared migration write lock, validate before replacement, retain one last-known-good backup, write and flush a unique temporary file, atomically replace, and reload the final profile. A failed mutation preserves the old file and cleans incomplete temporary output. Recovery is explicit: ARES never auto-restores or overwrites corruption evidence.

`scripts/inspect_owner_memory.py` is read-only and can report summary/counts, keyed facts, all general memories, one topic, one memory type, transient pending status, or deterministic sanitized JSON. `scripts/manual_verify_general_long_term_memory.py` uses an isolated profile and fresh Python processes to prove both observed Whisper transcripts create preferences, combined/type/topic recall works, repeats do not duplicate records, restart persistence holds, ordinary speech remains non-persistent, task-shaped `remember to` language remains a task, and no voice-owned memory file appears. `scripts/manual_verify_owner_memory_management.py` adds isolated cross-process request/confirm, cancellation, expiry, corrupt-pending, specific/topic/all-general/keyed separation, and no-mutation-before-confirmation checks.

Owner memory excludes inferred facts, automatic transcript/audio/conversation storage, semantic or episodic memory, temporary notes, embeddings, vectors, GPT memory, autonomous learning, and cloud synchronization. `ConversationContextManager`, legacy `UserProfileStore`, general `MemoryStore`, operational `EventHistoryStore`, and the central owner profile retain distinct responsibilities.

# Memory Schema Migrations

`memory.schema_migrations` is the centralized migration framework for active JSON-backed persistent stores. Store modules call this shared layer instead of implementing ad hoc version checks.

Durable data classes:

- Durable identity/memory: `UserProfileStore`, explicit `OwnerProfileStore`, `MemoryStore` short-term memory, `MemoryStore` long-term memory, `GoalsStore`, `NotesStore`, and `TasksStore`.
- Operational history: `EventHistoryStore`.
- Derived state: `ReminderScheduler` reads tasks and has no separate persisted file.
- Voice-session history: stored as event-history records; no separate voice-session store exists.
- Disposable cache: cache data is not identity memory and is not migrated as durable owner memory.
- Configuration: app allowlists, adapter examples, module examples, and other config files are configuration-backed durable state, not owner identity memory.
- Legacy/disconnected: `memory_manager.py` and `memory/memories.json` are legacy script-era formats. The active runtime uses `memory.v1.MemoryStore` and explicit store paths.

Current active schemas:

- `ares.user_profile`
- `ares.owner_profile`
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

Schema versions are integer major versions. Missing schema versions are accepted only by explicit legacy importers for known structures. Unknown future versions fail closed. Downgrades are rejected. Migrations must be sequential; `ares.owner_profile` now demonstrates the production v1 -> v2 -> v3 path while preserving every keyed fact. Other current production schemas remain at v1, and a separate test fixture demonstrates generic multi-step migration behavior.

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

This module lifecycle is intentionally distinct from `BrainSessionManager`: modules move through load/readiness states, while the Capital Brain moves through session states. A City cannot replace or own the Brain session manager.

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
- Owner facts are persisted only after an explicit bounded owner-memory command.
- Transcripts, microphone recordings, and complete conversations are not owner-profile data.
- Protected credentials and recovery material must never enter owner memory or operational diagnostics.
- A failed owner-profile load must never be interpreted as an empty profile or overwrite recovery evidence.
- Secrets are never stored in committed config.
- Real API integrations stay gated by config and environment variables.
- Tests must pass before merge.

# Long-Term Vision

ARES is intended to become an extensible personal AI operating system. It starts as a Raspberry Pi assistant, but the architecture should allow it to grow into a larger system, then into a robot body, and eventually into a humanoid robot without losing its identity.

The Brain is the continuity layer. Cities can be added, replaced, upgraded, or retired. The Brain keeps the owner relationship, memory, goals, history, personality, reasoning, and planning stable while the body and tools evolve around it.
