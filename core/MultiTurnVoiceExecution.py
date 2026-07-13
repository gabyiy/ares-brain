from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from core.Contracts import (
    MultiTurnVoiceSessionRequestV1,
    MultiTurnVoiceSessionResultV1,
    SingleTurnVoiceRequestV1,
    SingleTurnVoiceResultV1,
    utc_contract_timestamp,
)
from core.MultiTurnVoiceRuntime import (
    EVENT_GREETING_COMPLETED,
    EVENT_GREETING_STARTED,
    EVENT_SESSION_CANCELLED,
    EVENT_SESSION_COMPLETED,
    EVENT_SESSION_FAILED,
    EVENT_SESSION_STARTED,
    EVENT_STOP_PHRASE,
    EVENT_STOP_REQUESTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
)
from core.MultiTurnVoiceSupport import (
    SESSION_CANCELLED,
    SESSION_CHECKING_STOP_PHRASE,
    SESSION_COMPLETED,
    SESSION_FAILED,
    SESSION_GREETING,
    SESSION_LISTENING,
    SESSION_SPEAKING,
    SESSION_STARTING,
    SESSION_STOPPING,
    SESSION_WAITING,
    StopPhraseMatcher,
    session_elapsed,
)
from core.ResourceBudget import CancellationToken
from core.SingleTurnVoiceSupport import SingleTurnPreBrainDecision


class MultiTurnVoiceExecutionMixin:
    def _run_session(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        token: CancellationToken,
    ) -> MultiTurnVoiceSessionResultV1:
        started_at = self.clock()
        started_timestamp = utc_contract_timestamp()
        self._reset_run_state(request)
        matcher = StopPhraseMatcher(request.stop_phrases)
        counters = {
            "attempted": 0,
            "successful": 0,
            "failed": 0,
            "silent": 0,
            "blank": 0,
            "consecutive_failures": 0,
        }
        summaries: List[Dict[str, Any]] = []
        stop_reason = ""
        matched_stop_phrase = ""
        maximum_turns_reached = False
        maximum_duration_reached = False
        fallback_used = False
        fatal_error_stage = ""
        fatal_error_reason = ""

        self._transition(SESSION_STARTING, "session_starting")
        self._emit(EVENT_SESSION_STARTED, "session", "started", True)
        self._notify("session_started", {"session_id": request.session_id})

        if request.greeting_enabled:
            self._transition(SESSION_GREETING, "configured_greeting")
            self._emit(EVENT_GREETING_STARTED, "greeting", "started", True)
            greeting = self._run_local_phrase(request, request.greeting_text, "greeting", token)
            greeting_success = greeting is None or greeting.success
            self._emit(
                EVENT_GREETING_COMPLETED,
                "greeting",
                "completed" if greeting_success else "failed",
                greeting_success,
                {"playback_enabled": request.playback_enabled},
            )
            if not greeting_success:
                fatal_error_stage = greeting.error_stage or "greeting"
                fatal_error_reason = greeting.error_reason or greeting.status
                stop_reason = "fatal_component_failure"

        for turn_number in range(1, request.maximum_turns + 1):
            if stop_reason:
                break
            if token.requested or self._stop_requested:
                stop_reason = token.reason or self._stop_reason or "stop_requested"
                break
            remaining = self._remaining_session_seconds(request, started_at)
            if remaining <= 0:
                maximum_duration_reached = True
                stop_reason = "maximum_session_duration"
                break
            if remaining < self._minimum_turn_budget(request):
                maximum_duration_reached = True
                stop_reason = "insufficient_remaining_session_time"
                break
            if turn_number > 1:
                if not self._pipeline_idle():
                    fatal_error_stage = "resource_lock"
                    fatal_error_reason = "single_turn_pipeline_not_idle"
                    stop_reason = "fatal_resource_lock_failure"
                    break
                delay = min(request.inter_turn_delay_seconds, remaining)
                if delay > 0:
                    try:
                        self.sleeper(delay)
                    except KeyboardInterrupt:
                        token.cancel("keyboard_interrupt")
                        stop_reason = "keyboard_interrupt"
                        break
                if not self._pipeline_idle():
                    fatal_error_stage = "resource_lock"
                    fatal_error_reason = "single_turn_pipeline_not_idle_after_delay"
                    stop_reason = "fatal_resource_lock_failure"
                    break
                if self._remaining_session_seconds(request, started_at) < self._minimum_turn_budget(request):
                    maximum_duration_reached = True
                    stop_reason = "insufficient_remaining_session_time"
                    break

            try:
                simulated_text, input_available = self._next_simulated_text(request, turn_number)
            except KeyboardInterrupt:
                token.cancel("keyboard_interrupt")
                stop_reason = "keyboard_interrupt"
                break
            if not input_available:
                stop_reason = "input_exhausted"
                break

            counters["attempted"] += 1
            self._current_turn_number = turn_number
            self._transition(SESSION_LISTENING, f"turn_{turn_number}_started")
            turn_correlation_id = self._turn_correlation_id(request, turn_number)
            self._emit(
                EVENT_TURN_STARTED,
                "turn",
                "started",
                True,
                {
                    "turn_number": turn_number,
                    "turn_correlation_id": turn_correlation_id,
                    "simulated_input": simulated_text is not None,
                },
            )
            self._notify(
                "turn_started",
                {"turn_number": turn_number, "simulated_input": simulated_text is not None},
            )

            if simulated_text is not None and not simulated_text.strip():
                turn_result = self._empty_simulated_turn(request, turn_number)
            else:
                matched: Dict[str, str] = {"phrase": ""}

                def before_brain(text: str) -> SingleTurnPreBrainDecision:
                    self._transition(
                        SESSION_CHECKING_STOP_PHRASE,
                        f"turn_{turn_number}_check_stop_phrase",
                    )
                    phrase = matcher.match(text)
                    if not phrase:
                        return SingleTurnPreBrainDecision()
                    matched["phrase"] = phrase
                    self._emit(
                        EVENT_STOP_PHRASE,
                        "stop_phrase",
                        "detected",
                        True,
                        {
                            "turn_number": turn_number,
                            "turn_correlation_id": turn_correlation_id,
                            "matched_stop_phrase": phrase,
                            "text_length": len(text),
                        },
                    )
                    self._notify(
                        "stop_phrase_detected",
                        {"turn_number": turn_number, "matched_stop_phrase": phrase},
                    )
                    return SingleTurnPreBrainDecision(
                        handled=True,
                        status="owner_stop_phrase",
                        continue_to_output=False,
                        data={"matched_stop_phrase": phrase},
                    )

                turn_request = self._single_turn_request(
                    request,
                    turn_number,
                    simulated_text or "",
                    remaining,
                )
                self._observing_turn = True
                try:
                    turn_result = self.single_turn_pipeline.run_once(
                        turn_request,
                        cancellation_token=token,
                        pre_brain_hook=before_brain,
                    )
                finally:
                    self._observing_turn = False
                matched_stop_phrase = matched["phrase"]

            summary = self._turn_summary(turn_number, turn_result)
            summaries.append(summary)
            recording = dict(turn_result.data.get("recording") or {})
            self._notify(
                "turn_result",
                {
                    "turn_number": turn_number,
                    "recognized_text": turn_result.recognized_text,
                    "raw_transcript": turn_result.raw_transcript,
                    "cleaned_transcript": turn_result.cleaned_transcript,
                    "normalized_command": turn_result.normalized_command,
                    "response_text": turn_result.brain_text_response,
                    "status": turn_result.status,
                    "detected_intent": turn_result.detected_intent,
                    "routed_skill": turn_result.routed_skill,
                    "planner_decision": turn_result.planner_decision,
                    "execution_result": turn_result.execution_result,
                    "rejection_reason": turn_result.rejection_reason,
                    "capture_mode": request.capture_mode,
                    "capture_stop_reason": recording.get("stop_reason") or recording.get("status", ""),
                    "ambient_rms": float(recording.get("ambient_rms", 0.0)),
                    "speech_rms": float(recording.get("speech_rms", 0.0)),
                    "peak_amplitude": int(recording.get("peak_amplitude", 0)),
                    "speech_start_rms": float(recording.get("derived_speech_start_rms", 0.0)),
                    "speech_continue_rms": float(
                        recording.get("derived_speech_continue_rms", 0.0)
                    ),
                    "silence_rms": float(recording.get("derived_silence_rms", 0.0)),
                },
            )

            if matched_stop_phrase:
                stop_reason = "owner_stop_phrase"
                self._emit(
                    EVENT_TURN_COMPLETED,
                    "turn",
                    "stop_phrase",
                    True,
                    {
                        "turn_number": turn_number,
                        "turn_correlation_id": turn_correlation_id,
                        "text_length": len(turn_result.recognized_text),
                    },
                )
                break

            classification = self._classify_turn(turn_result, request)
            fallback_used = fallback_used or turn_result.brain_fallback_used
            if classification["logical_success"]:
                counters["successful"] += 1
                counters["consecutive_failures"] = 0
                self._emit(
                    EVENT_TURN_COMPLETED,
                    "turn",
                    turn_result.status,
                    True,
                    self._turn_event_metadata(turn_number, turn_result, classification),
                )
            else:
                counters["failed"] += 1
                counters["consecutive_failures"] += 1
                if classification["category"] == "silence":
                    counters["silent"] += 1
                if classification["category"] == "blank_transcription":
                    counters["blank"] += 1
                self._emit(
                    EVENT_TURN_FAILED,
                    "turn",
                    turn_result.status,
                    False,
                    self._turn_event_metadata(turn_number, turn_result, classification),
                )

            if classification["fatal"]:
                fatal_error_stage = turn_result.error_stage or classification["category"]
                fatal_error_reason = turn_result.error_reason or turn_result.status
                stop_reason = classification["stop_reason"]
                break
            if classification["category"] == "silence" and not request.silence_retry_enabled:
                stop_reason = "silence_retry_disabled"
                break
            if (
                classification["category"] == "blank_transcription"
                and not request.blank_transcription_retry_enabled
            ):
                stop_reason = "blank_transcription_retry_disabled"
                break
            if counters["consecutive_failures"] >= request.maximum_consecutive_failures:
                stop_reason = "maximum_consecutive_failures"
                break
            if turn_number >= request.maximum_turns:
                maximum_turns_reached = True
                stop_reason = "maximum_turns"
                break
            self._transition(SESSION_WAITING, f"turn_{turn_number}_completed")

        cancelled = bool(token.requested or self._stop_reason == "keyboard_interrupt")
        if cancelled and not stop_reason:
            stop_reason = token.reason or self._stop_reason or "cancelled"
        if not stop_reason:
            maximum_turns_reached = counters["attempted"] >= request.maximum_turns
            stop_reason = "maximum_turns" if maximum_turns_reached else "session_completed"

        clean_completion = not fatal_error_reason and not cancelled
        closing_result = None
        if request.closing_phrase_enabled and clean_completion:
            if request.playback_enabled:
                self._transition(SESSION_SPEAKING, "configured_closing_phrase")
            closing_result = self._run_local_phrase(
                request,
                request.closing_phrase_text,
                "closing",
                token,
            )
            if closing_result is not None and not closing_result.success:
                fatal_error_stage = closing_result.error_stage or "closing"
                fatal_error_reason = closing_result.error_reason or closing_result.status
                stop_reason = "closing_output_failed"
                clean_completion = False

        self._transition(SESSION_STOPPING, stop_reason)
        self._emit(
            EVENT_STOP_REQUESTED,
            "session",
            stop_reason,
            True,
            {
                "attempted_turns": counters["attempted"],
                "matched_stop_phrase": matched_stop_phrase,
            },
        )
        if cancelled:
            final_state = SESSION_CANCELLED
            final_status = "cancelled"
            success = False
            self._transition(final_state, stop_reason)
            self._emit(EVENT_SESSION_CANCELLED, "session", final_status, False)
        elif fatal_error_reason or stop_reason in {
            "maximum_consecutive_failures",
            "silence_retry_disabled",
            "blank_transcription_retry_disabled",
        }:
            final_state = SESSION_FAILED
            final_status = "failed"
            success = False
            self._transition(final_state, fatal_error_reason or stop_reason)
            self._emit(
                EVENT_SESSION_FAILED,
                "session",
                final_status,
                False,
                {"failure_category": fatal_error_stage or stop_reason},
            )
        else:
            final_state = SESSION_COMPLETED
            success = True
            final_status = "completed_with_partial_failures" if counters["failed"] else "completed"
            self._transition(final_state, stop_reason)
            self._emit(
                EVENT_SESSION_COMPLETED,
                "session",
                final_status,
                True,
                {
                    "attempted_turns": counters["attempted"],
                    "successful_turns": counters["successful"],
                    "failed_turns": counters["failed"],
                    "stop_reason": stop_reason,
                },
            )

        completed_timestamp = utc_contract_timestamp()
        return MultiTurnVoiceSessionResultV1(
            success=success,
            status=final_status,
            correlation_id=request.correlation_id,
            session_id=request.session_id,
            started_at=started_timestamp,
            completed_at=completed_timestamp,
            total_duration_seconds=session_elapsed(self.clock, started_at),
            attempted_turns=counters["attempted"],
            successful_turns=counters["successful"],
            failed_turns=counters["failed"],
            silent_turns=counters["silent"],
            blank_transcription_turns=counters["blank"],
            stop_reason=stop_reason,
            recognized_stop_phrase=matched_stop_phrase,
            maximum_turns_reached=maximum_turns_reached,
            maximum_duration_reached=maximum_duration_reached,
            cancelled=cancelled,
            fallback_responses_used=fallback_used,
            resource_cleanup_status="pending_lifecycle_stop",
            final_state=final_state,
            turn_summaries=summaries,
            state_history=self._state_history(),
            error_stage=fatal_error_stage,
            error_reason=fatal_error_reason,
            data={
                "owner_triggered_only": True,
                "background_listening": False,
                "raw_audio_persisted": request.cleanup_policy == "keep",
                "event_history_failures": list(self._event_history_failures),
                "closing_status": closing_result.status if closing_result is not None else "skipped",
            },
            events=[dict(event) for event in self._session_events],
            metadata={
                "safe": True,
                "source": "multi_turn_voice_session",
                "owner_triggered_only": True,
                "bounded": True,
            },
        )

    def _run_local_phrase(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        text: str,
        purpose: str,
        token: CancellationToken,
    ) -> Optional[SingleTurnVoiceResultV1]:
        if not request.playback_enabled:
            return None
        remaining = max(0.1, request.per_turn_timeout_seconds)
        local_request = self._single_turn_request(
            request,
            0,
            text,
            remaining,
            purpose=purpose,
        )
        self._observing_turn = False
        return self.single_turn_pipeline.run_local_output(
            local_request,
            text,
            cancellation_token=token,
        )

    def _single_turn_request(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        turn_number: int,
        text_input: str,
        remaining_seconds: float,
        purpose: str = "turn",
    ) -> SingleTurnVoiceRequestV1:
        timeout = max(0.1, min(request.per_turn_timeout_seconds, remaining_seconds))
        output_directory = Path(request.recording_output_directory).expanduser()
        safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.session_id)[:80]
        suffix = f"turn_{turn_number:03d}" if turn_number else purpose
        output_path = output_directory / f"{safe_session}_{suffix}.wav"
        correlation_id = (
            self._turn_correlation_id(request, turn_number)
            if turn_number
            else f"{request.correlation_id}:{purpose}"
        )
        return SingleTurnVoiceRequestV1(
            microphone_device=request.microphone_device,
            recording_duration_seconds=request.recording_duration_seconds,
            recording_output_path=str(output_path),
            language=request.language,
            whisper_executable_path=request.whisper_executable_path,
            whisper_model_profile=request.whisper_model_profile,
            minimum_rms=request.minimum_rms,
            capture_mode=request.capture_mode,
            calibration_enabled=request.calibration_enabled,
            calibration_duration_seconds=request.calibration_duration_seconds,
            speech_start_rms=request.speech_start_rms,
            speech_continue_rms=request.speech_continue_rms,
            silence_rms=request.silence_rms,
            required_speech_frames=request.required_speech_frames,
            required_continue_frames=request.required_continue_frames,
            required_silence_frames=request.required_silence_frames,
            silence_duration_seconds=request.silence_duration_seconds,
            speech_wait_timeout_seconds=request.speech_wait_timeout_seconds,
            maximum_utterance_seconds=request.maximum_utterance_seconds,
            pre_roll_seconds=request.pre_roll_seconds,
            frame_duration_ms=request.frame_duration_ms,
            minimum_speech_start_rms=request.minimum_speech_start_rms,
            maximum_speech_start_rms=request.maximum_speech_start_rms,
            minimum_speech_continue_rms=request.minimum_speech_continue_rms,
            maximum_speech_continue_rms=request.maximum_speech_continue_rms,
            minimum_silence_rms=request.minimum_silence_rms,
            maximum_silence_rms=request.maximum_silence_rms,
            frame_debug_enabled=request.frame_debug_enabled,
            tts_voice_profile=request.tts_voice_profile,
            speaker_device=request.speaker_device,
            playback_enabled=request.playback_enabled,
            timeout_seconds=timeout,
            recording_timeout_seconds=min(
                timeout,
                (
                    request.speech_wait_timeout_seconds
                    + request.maximum_utterance_seconds
                    + 5.0
                    if request.capture_mode == "auto_stop"
                    else float(request.recording_duration_seconds) + 5.0
                ),
            ),
            transcription_timeout_seconds=timeout,
            brain_timeout_seconds=min(timeout, 30.0),
            synthesis_timeout_seconds=timeout,
            playback_timeout_seconds=timeout,
            cleanup_policy=request.cleanup_policy,
            text_input=text_input,
            correlation_id=correlation_id,
            session_id=request.session_id,
            metadata={
                "source": "multi_turn_voice_session",
                "parent_correlation_id": request.correlation_id,
                "turn_number": turn_number,
                "purpose": purpose,
                "simulated_input": bool(text_input),
            },
        )

    def _next_simulated_text(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        turn_number: int,
    ) -> tuple[Optional[str], bool]:
        if request.simulated_text_turns:
            index = turn_number - 1
            if index >= len(request.simulated_text_turns):
                return None, False
            return request.simulated_text_turns[index], True
        if request.interactive_text:
            if self.text_input_provider is None:
                return None, False
            value = self.text_input_provider(turn_number)
            return ("" if value is None else str(value)), value is not None
        return None, True

    def _classify_turn(
        self,
        result: SingleTurnVoiceResultV1,
        request: MultiTurnVoiceSessionRequestV1,
    ) -> Dict[str, Any]:
        if result.success or result.brain_fallback_used:
            return {
                "category": "success",
                "logical_success": True,
                "fatal": False,
                "stop_reason": "",
            }
        if result.status == "silent_audio":
            return {
                "category": "silence",
                "logical_success": False,
                "fatal": False,
                "stop_reason": "",
            }
        if result.status == "blank_transcription":
            return {
                "category": "blank_transcription",
                "logical_success": False,
                "fatal": False,
                "stop_reason": "",
            }
        if result.status == "cancelled" or result.error_stage == "cancellation":
            return {
                "category": "cancellation",
                "logical_success": False,
                "fatal": True,
                "stop_reason": "cancelled",
            }
        fatal = result.error_stage in {
            "lifecycle_start",
            "health_check",
            "resource_reservation",
            "recording_start",
            "resource_lock",
            "pre_brain_hook",
        }
        fatal = fatal or result.status in {
            "stage_conflict",
            "not_ready",
            "health_not_verified",
            "resource_denied",
        }
        if request.playback_enabled and result.status == "tts_failed":
            fatal = True
        category = (
            "playback_failure"
            if result.status in {"playback_failed", "playback_timeout"}
            else "transcription_failure"
            if result.status in {"transcription_failed", "transcription_timeout"}
            else "component_failure"
        )
        return {
            "category": category,
            "logical_success": False,
            "fatal": fatal,
            "stop_reason": "fatal_component_failure" if fatal else "",
        }

    def _turn_summary(
        self,
        turn_number: int,
        result: SingleTurnVoiceResultV1,
    ) -> Dict[str, Any]:
        recording = dict(result.data.get("recording") or {})
        return {
            "turn_number": turn_number,
            "correlation_id": result.correlation_id,
            "success": result.success,
            "status": result.status,
            "recognized_text": result.recognized_text,
            "raw_transcript": result.raw_transcript,
            "cleaned_transcript": result.cleaned_transcript,
            "normalized_command": result.normalized_command,
            "detected_intent": result.detected_intent,
            "routed_skill": result.routed_skill,
            "planner_decision": result.planner_decision,
            "execution_result": result.execution_result,
            "rejection_reason": result.rejection_reason,
            "brain_text_response": result.brain_text_response,
            "brain_fallback_used": result.brain_fallback_used,
            "recording_status": result.recording_status,
            "transcription_status": result.transcription_status,
            "tts_status": result.tts_status,
            "playback_status": result.playback_status,
            "total_processing_time_seconds": result.total_processing_time_seconds,
            "error_stage": result.error_stage,
            "error_reason": result.error_reason,
            "simulated_input": result.simulated_input,
            "recorded_wav_path": result.recorded_wav_path,
            "generated_speech_wav_path": result.generated_speech_wav_path,
            "capture_stop_reason": recording.get("stop_reason") or recording.get("status", ""),
            "ambient_rms": float(recording.get("ambient_rms", 0.0)),
            "speech_rms": float(recording.get("speech_rms", 0.0)),
            "peak_amplitude": int(recording.get("peak_amplitude", 0)),
            "capture_thresholds": {
                "speech_start_rms": recording.get("derived_speech_start_rms", 0.0),
                "speech_continue_rms": recording.get("derived_speech_continue_rms", 0.0),
                "silence_rms": recording.get("derived_silence_rms", 0.0),
            },
        }

    def _turn_event_metadata(
        self,
        turn_number: int,
        result: SingleTurnVoiceResultV1,
        classification: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "turn_number": turn_number,
            "turn_correlation_id": result.correlation_id,
            "text_length": len(result.recognized_text),
            "response_length": len(result.brain_text_response),
            "intent": result.detected_intent,
            "status": result.status,
            "failure_category": classification["category"],
            "duration_seconds": result.total_processing_time_seconds,
        }

    def _empty_simulated_turn(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        turn_number: int,
    ) -> SingleTurnVoiceResultV1:
        return SingleTurnVoiceResultV1(
            success=False,
            status="silent_audio",
            correlation_id=self._turn_correlation_id(request, turn_number),
            session_id=request.session_id,
            recording_status="skipped_empty_simulated_input",
            transcription_status="not_started",
            simulated_input=True,
            error_stage="simulated_input",
            error_reason="empty_simulated_input",
            metadata={"safe": True, "source": "multi_turn_voice_session"},
        )

    def _remaining_session_seconds(
        self,
        request: MultiTurnVoiceSessionRequestV1,
        started_at: float,
    ) -> float:
        limit = min(
            request.maximum_session_duration_seconds,
            request.total_session_timeout_seconds,
        )
        return max(0.0, float(limit) - session_elapsed(self.clock, started_at))

    def _minimum_turn_budget(self, request: MultiTurnVoiceSessionRequestV1) -> float:
        if request.simulated_text_turns or request.interactive_text:
            return 0.01
        return float(request.recording_duration_seconds)
