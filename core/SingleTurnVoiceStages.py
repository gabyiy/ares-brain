from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from core.Contracts import SingleTurnVoiceRequestV1, SingleTurnVoiceResultV1, TextToSpeechRequestV1
from core.Health import RETRY_SAFE
from core.Microphone import AudioChunk, MicrophoneResult
from core.ResourceBudget import CancellationToken
from core.SingleTurnVoiceSupport import (
    PreBrainHook,
    SingleTurnPreBrainDecision,
    SingleTurnRunState,
    VoiceStageConflict,
    brain_timeout_routing,
    empty_audio_chunk,
    result_dict,
    result_error,
    result_success,
    safe_exception,
    speech_output_path,
)
from core.SpeechToText import TranscriptionResult
from core.WavAudio import analyze_wav_audio, read_audio_chunk_wav, write_audio_chunk_wav


class SingleTurnVoiceStageMixin:
    def _run_stages(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        cancellation_token: Optional[CancellationToken],
        pre_brain_hook: Optional[PreBrainHook] = None,
    ) -> SingleTurnVoiceResultV1:
        cancelled = self._cancelled(state, cancellation_token, "before_recording")
        if cancelled:
            return cancelled

        if request.text_input.strip():
            transcription = self._simulated_transcription(request, state)
        else:
            self._stage(2, "Recording", "running")
            recording, wav, audio_chunk = self._record_audio(request, state)
            if recording is not None:
                return recording
            self._stage(2, "Recording", "completed")
            if float(wav.get("rms_amplitude", 0.0)) < request.minimum_rms:
                state.recording_status = "silent_audio"
                return self._failure(
                    state,
                    "recording_validation",
                    "audio_below_rms_threshold",
                    "silent_audio",
                    data={"wav": wav, "minimum_rms": request.minimum_rms},
                )

            cancelled = self._cancelled(state, cancellation_token, "before_transcription")
            if cancelled:
                return cancelled
            transcription = self._transcription_stage(request, state, audio_chunk)
            if isinstance(transcription, SingleTurnVoiceResultV1):
                return transcription

        cancelled = self._cancelled(state, cancellation_token, "before_brain")
        if cancelled:
            return cancelled
        decision = self._apply_pre_brain_hook(state, pre_brain_hook)
        if isinstance(decision, SingleTurnVoiceResultV1):
            return decision
        if decision is None or not decision.handled:
            self._brain_stage(request, state, transcription)
        elif not decision.continue_to_output:
            result = self._result(
                state,
                success=True,
                status=decision.status or "intercepted_before_brain",
            )
            self._emit(
                state,
                self.EVENT_SINGLE_TURN_COMPLETED,
                "pipeline",
                result.status,
                True,
                {"pre_brain_intercepted": True},
            )
            return replace(result, events=[dict(event) for event in state.events])

        cancelled = self._cancelled(state, cancellation_token, "before_synthesis")
        if cancelled:
            return cancelled
        should_synthesize = not request.text_input.strip() or request.playback_enabled
        if should_synthesize:
            self._stage(5, "Synthesizing response", "running")
            synthesis_failure = self._synthesize(request, state)
            if synthesis_failure:
                return synthesis_failure
            self._stage(5, "Synthesizing response", "completed")
        else:
            state.tts_status = "skipped_simulated_input"
            self._stage(5, "Synthesizing response", "skipped")

        cancelled = self._cancelled(state, cancellation_token, "before_playback")
        if cancelled:
            return cancelled
        self._stage(6, "Playing response", "running" if request.playback_enabled else "skipped")
        if request.playback_enabled:
            playback_failure = self._playback(request, state)
            if playback_failure:
                return playback_failure
            self._stage(6, "Playing response", "completed")
        else:
            state.playback_status = "playback_disabled"
            self._emit(
                state,
                self.EVENT_PLAYBACK_COMPLETED,
                "playback",
                state.playback_status,
                True,
                {"playback_enabled": False},
            )

        if decision is not None and decision.handled:
            status = "completed_local_output"
        else:
            status = "completed_with_brain_fallback" if state.brain_fallback_used else "completed"
        result = self._result(state, success=not state.brain_fallback_used, status=status)
        self._emit(
            state,
            self.EVENT_SINGLE_TURN_COMPLETED,
            "pipeline",
            status,
            result.success,
            {"brain_fallback_used": state.brain_fallback_used},
        )
        return replace(result, events=[dict(event) for event in state.events])

    def _apply_pre_brain_hook(
        self,
        state: SingleTurnRunState,
        pre_brain_hook: Optional[PreBrainHook],
    ) -> Optional[SingleTurnPreBrainDecision | SingleTurnVoiceResultV1]:
        if pre_brain_hook is None:
            return None
        try:
            decision = pre_brain_hook(state.recognized_text)
        except Exception as error:
            return self._failure(
                state,
                "pre_brain_hook",
                safe_exception(error),
                "pre_brain_hook_failed",
            )
        if decision is None:
            return None
        if not isinstance(decision, SingleTurnPreBrainDecision):
            return self._failure(
                state,
                "pre_brain_hook",
                "invalid_pre_brain_decision",
                "pre_brain_hook_failed",
            )
        if not decision.handled:
            return decision
        if decision.continue_to_output and not decision.response_text.strip():
            return self._failure(
                state,
                "pre_brain_hook",
                "local_output_text_required",
                "pre_brain_hook_failed",
            )
        state.brain_execution_status = decision.status or "intercepted_before_brain"
        state.brain_text_response = decision.response_text.strip()
        state.data["pre_brain_decision"] = {
            "handled": True,
            "status": state.brain_execution_status,
            "continue_to_output": decision.continue_to_output,
            "data": dict(decision.data or {}),
        }
        self._emit(
            state,
            self.EVENT_BRAIN_EXECUTION_COMPLETED,
            "brain_execution",
            state.brain_execution_status,
            True,
            {
                "bypassed": True,
                "response_length": len(state.brain_text_response),
            },
        )
        return decision

    def _simulated_transcription(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
    ) -> TranscriptionResult:
        self._stage(2, "Recording", "skipped: simulated text input")
        state.recording_status = "skipped_simulated_input"
        self._emit(state, self.EVENT_RECORDING_STARTED, "recording", state.recording_status, True)
        self._emit(state, self.EVENT_RECORDING_COMPLETED, "recording", state.recording_status, True)
        self._stage(3, "Transcribing", "skipped: simulated text input")
        state.transcription_status = "simulated_text_input"
        state.recognized_text = request.text_input.strip()
        result = TranscriptionResult(
            success=True,
            status="simulated_text_input",
            text=state.recognized_text,
            confidence=1.0,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            data={"source": "single_turn_voice_pipeline", "simulated_input": True},
            metadata={"safe": True, "source": "single_turn_voice_pipeline"},
        )
        self._emit(
            state,
            self.EVENT_TRANSCRIPTION_COMPLETED,
            "transcription",
            result.status,
            True,
            {"text_length": len(result.text), "simulated_input": True},
        )
        return result

    def _transcription_stage(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        audio_chunk: AudioChunk,
    ) -> TranscriptionResult | SingleTurnVoiceResultV1:
        self._stage(3, "Transcribing", "running")
        started = self.clock()
        try:
            self.coordinator.begin_heavy("whisper")
            transcription = self._transcribe(audio_chunk)
        except VoiceStageConflict as error:
            return self._failure(state, "transcription", str(error), "stage_conflict")
        except Exception as error:
            return self._failure(
                state,
                "transcription",
                safe_exception(error),
                "transcription_failed",
            )
        finally:
            self.coordinator.end_heavy("whisper")
        if self._stage_timed_out(state, started, request.transcription_timeout_seconds):
            return self._failure(
                state,
                "transcription",
                "transcription_timeout",
                "transcription_timeout",
            )
        state.transcription_status = transcription.status
        state.recognized_text = transcription.text.strip()
        state.transcription_processing_time_seconds = float(
            transcription.data.get("processing_time_seconds", 0.0)
        )
        state.data["transcription"] = transcription.to_dict()
        if not transcription.success:
            return self._failure(
                state,
                "transcription",
                transcription.error_message or transcription.status,
                "transcription_failed",
            )
        if not state.recognized_text:
            return self._failure(
                state,
                "transcription",
                "blank_transcription",
                "blank_transcription",
            )
        self._emit(
            state,
            self.EVENT_TRANSCRIPTION_COMPLETED,
            "transcription",
            transcription.status,
            True,
            {
                "text_length": len(state.recognized_text),
                "processing_time_seconds": state.transcription_processing_time_seconds,
            },
        )
        self._stage(3, "Transcribing", "completed")
        return transcription

    def _brain_stage(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
        transcription: TranscriptionResult,
    ) -> None:
        self._stage(4, "Processing through ARES Brain", "running")
        started = self.clock()
        routing = self.command_router.route(
            transcription,
            session_id=request.session_id,
            correlation_id=request.correlation_id,
        )
        if self._stage_timed_out(state, started, request.brain_timeout_seconds):
            routing = brain_timeout_routing(request)
        self._apply_brain_result(state, routing)
        self._emit(
            state,
            self.EVENT_BRAIN_EXECUTION_COMPLETED,
            "brain_execution",
            state.brain_execution_status,
            routing.success,
            {
                "intent": state.detected_intent,
                "skill": state.routed_skill,
                "fallback_used": state.brain_fallback_used,
                "response_length": len(state.brain_text_response),
            },
        )
        self._stage(4, "Processing through ARES Brain", "completed")

    def _record_audio(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
    ) -> tuple[Optional[SingleTurnVoiceResultV1], Dict[str, Any], AudioChunk]:
        output_path = Path(request.recording_output_path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        state.recorded_wav_path = str(output_path)
        start_result = self._safe_adapter_call(self.microphone_adapter, "start")
        if not result_success(start_result):
            return (
                self._failure(
                    state,
                    "recording_start",
                    result_error(start_result, "microphone_start_failed"),
                    "recording_failed",
                ),
                {},
                empty_audio_chunk(),
            )
        self.coordinator.begin_capture()
        self._emit(state, self.EVENT_RECORDING_STARTED, "recording", "recording", True)
        started = self.clock()
        try:
            record_wav = getattr(self.microphone_adapter, "record_wav", None)
            if callable(record_wav):
                capture = record_wav(
                    output_path,
                    seconds=request.recording_duration_seconds,
                    device=request.microphone_device or None,
                    timeout_seconds=request.recording_timeout_seconds
                    or request.recording_duration_seconds + 5,
                    overwrite=True,
                )
            else:
                capture = self.microphone_adapter.read_chunk(
                    timeout_seconds=request.recording_timeout_seconds
                    or request.recording_duration_seconds + 5,
                )
                if capture.success and capture.chunk is not None:
                    write_audio_chunk_wav(capture.chunk, output_path)
        except Exception as error:
            capture = MicrophoneResult(
                success=False,
                status="recording_failed",
                text="Microphone recording failed safely.",
                error_message=safe_exception(error),
            )
        finally:
            self.coordinator.end_capture()
            stop_result = self._safe_adapter_call(self.microphone_adapter, "stop")
            state.data["microphone_stop"] = result_dict(stop_result)
        if self._stage_timed_out(state, started, request.recording_timeout_seconds):
            return (
                self._failure(state, "recording", "recording_timeout", "recording_timeout"),
                {},
                empty_audio_chunk(),
            )
        state.data["recording"] = capture.to_dict()
        state.recording_status = capture.status
        if not capture.success:
            return (
                self._failure(
                    state,
                    "recording",
                    capture.error_message or capture.status,
                    "recording_failed",
                ),
                {},
                empty_audio_chunk(),
            )
        wav = analyze_wav_audio(output_path)
        if not wav.get("success"):
            state.recording_status = "invalid_recording"
            return (
                self._failure(
                    state,
                    "recording_validation",
                    str(wav.get("error_message") or "invalid_wav"),
                    "invalid_recording",
                    data={"wav": wav},
                ),
                wav,
                empty_audio_chunk(),
            )
        state.recording_duration_seconds = float(wav.get("duration_seconds", 0.0))
        state.peak_amplitude = int(wav.get("peak_amplitude", 0))
        state.rms_amplitude = float(wav.get("rms_amplitude", 0.0))
        chunk = capture.chunk or read_audio_chunk_wav(output_path, "single_turn_voice_pipeline")
        if dict(chunk.metadata or {}).get("wav_path") != str(output_path):
            chunk = replace(chunk, metadata={**dict(chunk.metadata), "wav_path": str(output_path)})
        self._emit(
            state,
            self.EVENT_RECORDING_COMPLETED,
            "recording",
            state.recording_status,
            True,
            {
                "duration_seconds": state.recording_duration_seconds,
                "peak_amplitude": state.peak_amplitude,
                "rms_amplitude": state.rms_amplitude,
                "byte_count": int(wav.get("byte_count", 0)),
            },
        )
        return None, wav, chunk

    def _transcribe(self, audio_chunk: AudioChunk) -> TranscriptionResult:
        if self.fallback_policy is None or not self.speech_to_text_candidates:
            return self.speech_to_text_adapter.transcribe(audio_chunk)
        execution = self.fallback_policy.execute(
            self.speech_to_text_candidates,
            "voice.transcribe",
            lambda adapter: adapter.transcribe(audio_chunk),
            retry_safety=RETRY_SAFE,
            required_interface_version="v1",
        )
        data = dict(execution.data or {})
        return TranscriptionResult(
            success=execution.success and bool(data.get("success")),
            status=str(data.get("status") or execution.status),
            text=str(data.get("text") or ""),
            confidence=float(data.get("confidence", 0.0)),
            error_message=str(
                data.get("error_message")
                or execution.original_error
                or execution.error_message
                or ""
            ),
            data={
                **dict(data.get("data") or {}),
                "fallback_execution": execution.to_dict(),
            },
            metadata=dict(data.get("metadata") or {}),
        )

    def _apply_brain_result(self, state: SingleTurnRunState, routing: Any) -> None:
        state.brain_execution_status = routing.status
        command_result = dict(routing.data.get("command_result") or {})
        handler_response = dict(command_result.get("handler_response") or {})
        handler_metadata = dict(handler_response.get("metadata") or {})
        state.detected_intent = str(handler_metadata.get("detected_intent") or "")
        state.routed_skill = str(handler_response.get("skill") or "")
        state.data["brain_routing"] = routing.to_dict()
        if routing.success and routing.response_text.strip():
            state.brain_text_response = routing.response_text.strip()
            return
        state.brain_fallback_used = True
        state.brain_text_response = self.DEFAULT_BRAIN_FAILURE_RESPONSE
        state.brain_execution_status = f"{routing.status or 'failed'}_fallback"

    def _synthesize(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
    ) -> Optional[SingleTurnVoiceResultV1]:
        output_path = speech_output_path(request.recording_output_path)
        state.generated_speech_wav_path = str(output_path)
        start_result = self._safe_adapter_call(self.text_to_speech_adapter, "start")
        if not result_success(start_result):
            return self._failure(
                state,
                "synthesis_start",
                result_error(start_result, "tts_start_failed"),
                "tts_failed",
            )
        started = self.clock()
        try:
            self.coordinator.begin_heavy("piper")
            tts_result = self.text_to_speech_adapter.synthesize(
                TextToSpeechRequestV1(
                    text=state.brain_text_response,
                    language=request.language,
                    voice_profile_id=request.tts_voice_profile,
                    output_wav_path=str(output_path),
                    timeout_seconds=request.synthesis_timeout_seconds or request.timeout_seconds,
                    playback_enabled=False,
                    correlation_id=request.correlation_id,
                    session_id=request.session_id,
                    metadata={"source": "single_turn_voice_pipeline"},
                )
            )
        except Exception as error:
            return self._failure(state, "synthesis", safe_exception(error), "tts_failed")
        finally:
            self.coordinator.end_heavy("piper")
            self._safe_adapter_call(self.text_to_speech_adapter, "stop")
        if self._stage_timed_out(state, started, request.synthesis_timeout_seconds):
            return self._failure(state, "synthesis", "tts_timeout", "tts_timeout")
        state.tts_status = tts_result.status
        state.resolved_voice_profile = tts_result.resolved_voice_profile or tts_result.voice_id
        state.generated_speech_wav_path = tts_result.generated_audio_path or str(output_path)
        state.tts_processing_time_seconds = tts_result.processing_time_seconds
        state.data["tts"] = tts_result.to_dict()
        if not tts_result.success:
            return self._failure(
                state,
                "synthesis",
                tts_result.error_message or tts_result.status,
                "tts_failed",
            )
        self._emit(
            state,
            self.EVENT_SYNTHESIS_COMPLETED,
            "synthesis",
            tts_result.status,
            True,
            {
                "voice_profile": state.resolved_voice_profile,
                "processing_time_seconds": state.tts_processing_time_seconds,
                "duration_seconds": tts_result.duration_seconds,
            },
        )
        return None

    def _playback(
        self,
        request: SingleTurnVoiceRequestV1,
        state: SingleTurnRunState,
    ) -> Optional[SingleTurnVoiceResultV1]:
        if not state.generated_speech_wav_path:
            return self._failure(state, "playback", "speech_wav_missing", "playback_failed")
        if self.coordinator.capture_active:
            return self._failure(
                state,
                "playback",
                "microphone_capture_active_during_playback",
                "stage_conflict",
            )
        start_result = self._safe_adapter_call(self.speaker_adapter, "start")
        if not result_success(start_result):
            return self._failure(
                state,
                "playback_start",
                result_error(start_result, "speaker_start_failed"),
                "playback_failed",
            )
        started = self.clock()
        try:
            self.coordinator.begin_playback()
            playback = self.speaker_adapter.play_wav(
                state.generated_speech_wav_path,
                device=request.speaker_device or None,
                timeout_seconds=request.playback_timeout_seconds or request.timeout_seconds,
            )
        except Exception as error:
            return self._failure(state, "playback", safe_exception(error), "playback_failed")
        finally:
            self.coordinator.end_playback()
            self._safe_adapter_call(self.speaker_adapter, "stop")
        if self._stage_timed_out(state, started, request.playback_timeout_seconds):
            return self._failure(state, "playback", "playback_timeout", "playback_timeout")
        state.playback_status = playback.status
        state.data["playback"] = playback.to_dict()
        if not playback.success:
            return self._failure(
                state,
                "playback",
                playback.error_message or playback.status,
                "playback_failed",
            )
        self._emit(
            state,
            self.EVENT_PLAYBACK_COMPLETED,
            "playback",
            playback.status,
            True,
            {"device": playback.device, "duration_seconds": playback.duration_seconds},
        )
        return None
