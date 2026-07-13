from pathlib import Path

from core import MultiTurnVoiceSessionResultV1
from scripts import manual_verify_multi_turn_voice as manual


class StubSession:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.stop_reasons = []
        self.stop_calls = 0

    def run_session(self, request):
        self.requests.append(request)
        return replace_result_ids(self.result, request)

    def request_stop(self, reason):
        self.stop_reasons.append(reason)

    def stop(self, request=None):
        self.stop_calls += 1


def replace_result_ids(result, request):
    values = result.to_dict()
    values["correlation_id"] = request.correlation_id
    values["session_id"] = request.session_id
    return MultiTurnVoiceSessionResultV1.from_dict(values)


def _success_result():
    return MultiTurnVoiceSessionResultV1(
        success=True,
        status="completed",
        attempted_turns=2,
        successful_turns=1,
        stop_reason="owner_stop_phrase",
        total_duration_seconds=1.25,
        final_state="completed",
        resource_cleanup_status="completed",
    )


def test_script_entrypoint_import_is_safe():
    assert callable(manual.main)
    assert callable(manual.run_manual_verification)


def test_parser_uses_bounded_safe_defaults():
    args = manual.build_parser().parse_args([])

    assert args.max_turns == 5
    assert args.max_session_seconds == 180.0
    assert args.max_consecutive_failures == 3
    assert args.playback is False
    assert args.interactive_text is False


def test_text_turns_are_forwarded_to_versioned_session_request():
    output = []
    session = StubSession(_success_result())

    code = manual.run_manual_verification(
        [
            "--text-turn",
            "calculate 2 + 2",
            "--text-turn",
            "goodbye Ares",
            "--max-turns",
            "4",
            "--no-greeting",
            "--no-closing-phrase",
        ],
        output.append,
        session=session,
    )

    assert code == 0
    request = session.requests[0]
    assert request.simulated_text_turns == ["calculate 2 + 2", "goodbye Ares"]
    assert request.maximum_turns == 4
    assert request.playback_enabled is False


def test_interactive_and_fixed_text_modes_are_rejected_together():
    output = []
    session = StubSession(_success_result())

    code = manual.run_manual_verification(
        ["--interactive-text", "--text-turn", "hello"],
        output.append,
        session=session,
    )

    assert code == 2
    assert session.requests == []
    assert "cannot be used together" in "\n".join(output)


def test_non_verbose_output_is_concise_and_not_nested_json():
    output = []

    code = manual.run_manual_verification(
        ["--text-turn", "goodbye", "--no-greeting"],
        output.append,
        session=StubSession(_success_result()),
    )

    rendered = "\n".join(output)
    assert code == 0
    assert "Session completed" in rendered
    assert "Turns attempted: 2" in rendered
    assert '"turn_summaries"' not in rendered


def test_verbose_output_includes_structured_contract():
    output = []

    code = manual.run_manual_verification(
        ["--text-turn", "goodbye", "--verbose"],
        output.append,
        session=StubSession(_success_result()),
    )

    rendered = "\n".join(output)
    assert code == 0
    assert '"contract_name": "voice.conversation_session.result"' in rendered
    assert '"turn_summaries"' in rendered


def test_failed_session_returns_nonzero_exit_code():
    output = []
    failure = MultiTurnVoiceSessionResultV1(
        success=False,
        status="failed",
        stop_reason="fatal_component_failure",
        final_state="failed",
        error_stage="health_check",
        error_reason="microphone_unavailable",
    )

    code = manual.run_manual_verification([], output.append, session=StubSession(failure))

    assert code == 2
    assert "microphone_unavailable" in "\n".join(output)


def test_cancelled_session_returns_130():
    cancelled = MultiTurnVoiceSessionResultV1(
        success=False,
        status="cancelled",
        stop_reason="keyboard_interrupt",
        cancelled=True,
        final_state="cancelled",
    )

    code = manual.run_manual_verification([], lambda _: None, session=StubSession(cancelled))

    assert code == 130


def test_script_contains_no_direct_audio_or_model_subprocess_implementation():
    source = Path("scripts/manual_verify_multi_turn_voice.py").read_text(
        encoding="utf-8"
    ).lower()

    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "shell=true" not in source
    assert "arecord" not in source
    assert "aplay" not in source
    assert ".onnx" not in source


def test_request_preserves_explicit_devices_voice_and_limits():
    args = manual.build_parser().parse_args(
        [
            "--microphone-device",
            "hw:9,0",
            "--speaker-device",
            "plughw:CARD=Test,DEV=0",
            "--voice-profile",
            "en_US-hfc_male-medium",
            "--playback",
            "--max-session-seconds",
            "90",
            "--max-consecutive-failures",
            "2",
            "--stop-phrase",
            "finish now",
        ]
    )

    request = manual.request_from_args(args)

    assert request.microphone_device == "hw:9,0"
    assert request.speaker_device == "plughw:CARD=Test,DEV=0"
    assert request.tts_voice_profile == "en_US-hfc_male-medium"
    assert request.playback_enabled is True
    assert request.maximum_session_duration_seconds == 90
    assert request.maximum_consecutive_failures == 2
    assert request.stop_phrases == ["finish now"]
