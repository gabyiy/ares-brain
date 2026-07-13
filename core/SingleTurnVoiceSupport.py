from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.Contracts import (
    CONTRACT_SINGLE_TURN_VOICE_REQUEST,
    SingleTurnVoiceRequestV1,
    SingleTurnVoiceResultV1,
    VoiceActivityCaptureRequestV1,
    validate_contract,
)
from core.VoiceActivityDetection import (
    CAPTURE_MODE_AUTO_STOP,
    CAPTURE_MODES,
    validate_voice_activity_request,
)
from core.Microphone import AudioChunk
from core.ModuleLifecycle import LifecycleRequest, LifecycleResult
from core.VoiceCommandRouter import VoiceCommandRoutingResult


PIPELINE_CLEANUP_DELETE_ON_SUCCESS = "delete_on_success"
PIPELINE_CLEANUP_KEEP = "keep"
PIPELINE_CLEANUP_PRESERVE_ON_FAILURE = "preserve_on_failure"
PIPELINE_CLEANUP_POLICIES = {
    PIPELINE_CLEANUP_DELETE_ON_SUCCESS,
    PIPELINE_CLEANUP_KEEP,
    PIPELINE_CLEANUP_PRESERVE_ON_FAILURE,
}


class VoiceStageConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class SingleTurnPreBrainDecision:
    handled: bool = False
    status: str = "not_handled"
    response_text: str = ""
    continue_to_output: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


PreBrainHook = Callable[[str], Optional[SingleTurnPreBrainDecision]]


@dataclass
class VoiceStageCoordinator:
    capture_active: bool = False
    playback_active: bool = False
    heavy_stage: str = ""
    trace: List[Dict[str, str]] = field(default_factory=list)

    def begin_capture(self) -> None:
        if self.playback_active:
            raise VoiceStageConflict("speaker_playback_active_during_capture")
        self.capture_active = True
        self._trace("begin", "microphone_capture")

    def end_capture(self) -> None:
        if self.capture_active:
            self._trace("end", "microphone_capture")
        self.capture_active = False

    def begin_playback(self) -> None:
        if self.capture_active:
            raise VoiceStageConflict("microphone_capture_active_during_playback")
        self.playback_active = True
        self._trace("begin", "speaker_playback")

    def end_playback(self) -> None:
        if self.playback_active:
            self._trace("end", "speaker_playback")
        self.playback_active = False

    def begin_heavy(self, stage: str) -> None:
        clean_stage = str(stage or "").strip()
        if self.heavy_stage:
            raise VoiceStageConflict(
                f"heavy_speech_process_conflict:{self.heavy_stage}:{clean_stage}"
            )
        self.heavy_stage = clean_stage
        self._trace("begin", clean_stage)

    def end_heavy(self, stage: str) -> None:
        clean_stage = str(stage or "").strip()
        if self.heavy_stage == clean_stage:
            self._trace("end", clean_stage)
            self.heavy_stage = ""

    def reset(self) -> None:
        self.end_capture()
        self.end_playback()
        if self.heavy_stage:
            self._trace("end", self.heavy_stage)
        self.heavy_stage = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capture_active": self.capture_active,
            "playback_active": self.playback_active,
            "heavy_stage": self.heavy_stage,
            "trace": [dict(item) for item in self.trace],
        }

    def _trace(self, action: str, stage: str) -> None:
        self.trace.append({"action": action, "stage": stage})


@dataclass
class SingleTurnRunState:
    request: SingleTurnVoiceRequestV1
    started_at: float
    events: List[Dict[str, Any]] = field(default_factory=list)
    microphone_health_status: str = "not_checked"
    recording_status: str = "not_started"
    recorded_wav_path: str = ""
    recording_duration_seconds: float = 0.0
    peak_amplitude: int = 0
    rms_amplitude: float = 0.0
    transcription_status: str = "not_started"
    recognized_text: str = ""
    raw_transcript: str = ""
    cleaned_transcript: str = ""
    normalized_command: str = ""
    repetition_detected: bool = False
    repetitions_removed: int = 0
    transcript_cleanup_rule: str = "none"
    transcription_processing_time_seconds: float = 0.0
    brain_execution_status: str = "not_started"
    detected_intent: str = ""
    candidate_skills: List[Dict[str, Any]] = field(default_factory=list)
    routed_skill: str = ""
    planner_decision: str = ""
    execution_result: str = ""
    rejection_reason: str = ""
    routing_diagnostics: Dict[str, Any] = field(default_factory=dict)
    brain_text_response: str = ""
    brain_fallback_used: bool = False
    tts_status: str = "not_started"
    resolved_voice_profile: str = ""
    generated_speech_wav_path: str = ""
    tts_processing_time_seconds: float = 0.0
    playback_status: str = "not_requested"
    data: Dict[str, Any] = field(default_factory=dict)


def validated_single_turn_request(
    request: SingleTurnVoiceRequestV1 | Dict[str, Any],
) -> SingleTurnVoiceRequestV1:
    if isinstance(request, dict):
        request = SingleTurnVoiceRequestV1.from_dict(request)
    if not isinstance(request, SingleTurnVoiceRequestV1):
        raise ValueError("single_turn_request_required")
    compatibility = validate_contract(
        request,
        expected_contract_name=CONTRACT_SINGLE_TURN_VOICE_REQUEST,
    )
    if not compatibility.success:
        raise ValueError(compatibility.error_message or compatibility.status)
    if not 1 <= int(request.recording_duration_seconds) <= 60:
        raise ValueError("recording_duration_seconds_out_of_range")
    if float(request.minimum_rms) < 0:
        raise ValueError("minimum_rms_must_be_non_negative")
    if request.capture_mode not in CAPTURE_MODES:
        raise ValueError("invalid_capture_mode")
    if request.capture_mode == CAPTURE_MODE_AUTO_STOP:
        validate_voice_activity_request(
            VoiceActivityCaptureRequestV1(
                output_wav_path=request.recording_output_path,
                microphone_device=request.microphone_device,
                frame_duration_ms=request.frame_duration_ms,
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
                minimum_speech_start_rms=request.minimum_speech_start_rms,
                maximum_speech_start_rms=request.maximum_speech_start_rms,
                minimum_speech_continue_rms=request.minimum_speech_continue_rms,
                maximum_speech_continue_rms=request.maximum_speech_continue_rms,
                minimum_silence_rms=request.minimum_silence_rms,
                maximum_silence_rms=request.maximum_silence_rms,
                duration_loss_tolerance_seconds=(
                    request.duration_loss_tolerance_seconds
                ),
                frame_debug_enabled=request.frame_debug_enabled,
                correlation_id=request.correlation_id,
                session_id=request.session_id,
            )
        )
    if not 0.1 <= float(request.timeout_seconds) <= 1800:
        raise ValueError("timeout_seconds_out_of_range")
    for name in (
        "recording_timeout_seconds",
        "transcription_timeout_seconds",
        "brain_timeout_seconds",
        "synthesis_timeout_seconds",
        "playback_timeout_seconds",
    ):
        value = getattr(request, name)
        if value is not None and not 0.01 <= float(value) <= 1800:
            raise ValueError(f"{name}_out_of_range")
    if request.cleanup_policy not in PIPELINE_CLEANUP_POLICIES:
        raise ValueError("invalid_cleanup_policy")
    if not request.text_input.strip() and not str(request.recording_output_path or "").lower().endswith(".wav"):
        raise ValueError("recording_output_path_must_be_wav")
    return request


def contract_failure_result(request: Any, error_message: str) -> SingleTurnVoiceResultV1:
    payload = request if isinstance(request, dict) else getattr(request, "to_dict", lambda: {})()
    return SingleTurnVoiceResultV1(
        success=False,
        status="contract_rejected",
        correlation_id=str(dict(payload or {}).get("correlation_id") or ""),
        session_id=str(dict(payload or {}).get("session_id") or ""),
        error_stage="contract_validation",
        error_reason=error_message,
        metadata={"safe": True, "source": "single_turn_voice_pipeline"},
    )


def lifecycle_resource_failure(
    operation: str,
    decision: Any,
    request: Optional[SingleTurnVoiceRequestV1],
    module_name: str,
) -> LifecycleResult:
    return LifecycleResult(
        success=False,
        status="resource_denied",
        state="UNLOADED",
        text="Single-turn voice lifecycle was denied by resource policy.",
        error_message=str(decision.error_message or decision.status),
        data={"resource_decision": decision.to_dict()},
        request=LifecycleRequest(
            module_name=module_name,
            operation=operation,
            correlation_id=request.correlation_id if request else "",
            session_id=request.session_id if request else "",
        ),
        metadata={"safe": True, "source": "single_turn_voice_pipeline"},
    )


def brain_timeout_routing(request: SingleTurnVoiceRequestV1) -> VoiceCommandRoutingResult:
    return VoiceCommandRoutingResult(
        success=False,
        status="brain_timeout",
        text="ARES Brain execution timed out safely.",
        input_text="",
        error_message="brain_timeout",
        correlation_id=request.correlation_id,
        session_id=request.session_id,
        metadata={"safe": True, "source": "single_turn_voice_pipeline"},
    )


def speech_output_path(recording_path: str) -> Path:
    source = Path(recording_path).expanduser()
    return source.with_name(f"{source.stem}_response.wav")


def empty_audio_chunk() -> AudioChunk:
    return AudioChunk(data=b"", source="single_turn_voice_pipeline.empty")


def result_success(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


def result_error(result: Any, fallback: str) -> str:
    if isinstance(result, dict):
        return str(result.get("error_message") or result.get("status") or fallback)
    return str(
        getattr(result, "error_message", "")
        or getattr(result, "status", "")
        or fallback
    )


def result_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {
        "success": result_success(result),
        "status": str(getattr(result, "status", "") or ""),
        "error_message": str(getattr(result, "error_message", "") or ""),
    }


def safe_exception(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:200]


def elapsed(clock: Any, started_at: float) -> float:
    return round(max(0.0, float(clock()) - float(started_at)), 6)
