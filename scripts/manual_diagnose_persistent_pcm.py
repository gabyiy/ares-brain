from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Optional, Sequence
import wave


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import (  # noqa: E402
    AudioChunk,
    LinuxAlsaMicrophoneAdapter,
    LinuxAlsaSpeakerAdapter,
    resolve_alsa_capture_device,
)
from core.PcmIntegrity import (  # noqa: E402
    CANONICAL_PCM_FRAME_BYTES,
    CANONICAL_PCM_FRAME_DURATION_MS,
    CANONICAL_PCM_SAMPLE_FORMAT,
    CANONICAL_PCM_SAMPLES_PER_FRAME,
    PcmIntegrityStatistics,
    analyze_s16_le_pcm_integrity,
    canonical_pcm_contract,
    concatenate_owned_pcm_frames,
)
from core.WavAudio import (  # noqa: E402
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE_HZ,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    validate_canonical_wav,
    write_audio_chunk_wav,
)
from memory.schema_migrations import MigrationError, StoreWriteLock  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0"
DEFAULT_RECORD_SECONDS = 4
DEFAULT_RUNTIME_LOCK_PATH = REPO_ROOT / "data" / "runtime" / "ares_standby_voice.runtime"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "runtime" / "pcm_integrity"
MINIMUM_DIAGNOSTIC_SPOKEN_RMS = 50.0
MINIMUM_DIAGNOSTIC_SPOKEN_PEAK = 200
MINIMUM_DIAGNOSTIC_RMS_RATIO = 2.0
MINIMUM_DIAGNOSTIC_RMS_DELTA = 25.0
MAXIMUM_DIAGNOSTIC_REPEATED_FRAME_PERCENTAGE = 99.0
MAXIMUM_DIAGNOSTIC_ZERO_SAMPLE_PERCENTAGE = 99.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare direct arecord WAV capture with the exact persistent standby "
            "raw-PCM path before running wake/VAD reliability checks."
        )
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument("--record-seconds", type=int, default=DEFAULT_RECORD_SECONDS)
    parser.add_argument("--playback", action="store_true")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument(
        "--runtime-lock-path",
        default=str(DEFAULT_RUNTIME_LOCK_PATH),
        help=argparse.SUPPRESS,
    )
    return parser


def run_pcm_diagnostic(
    argv: Optional[Sequence[str]] = None,
    *,
    output_func: Callable[[str], None] = print,
    input_func: Callable[[str], str] = input,
    microphone_factory: Callable[..., Any] = LinuxAlsaMicrophoneAdapter,
    speaker_factory: Callable[..., Any] = LinuxAlsaSpeakerAdapter,
) -> int:
    args = build_parser().parse_args(argv)
    issue = _validate_args(args)
    if issue:
        output_func(f"Configuration error: {issue}")
        return 2
    lock_path = Path(args.runtime_lock_path).expanduser()
    try:
        with StoreWriteLock(
            lock_path,
            recover_if_owner_dead=True,
            owner_kind="persistent_pcm_diagnostic",
        ):
            run_directory = _new_run_directory(Path(args.output_root))
            return _run_locked(
                args,
                run_directory=run_directory,
                output_func=output_func,
                input_func=input_func,
                microphone_factory=microphone_factory,
                speaker_factory=speaker_factory,
            )
    except KeyboardInterrupt:
        output_func("PCM diagnostic cancelled; ALSA ownership and runtime lock released.")
        return 130
    except (MigrationError, OSError, RuntimeError, TypeError, ValueError) as error:
        output_func(
            "PCM diagnostic failed safely: "
            f"{error.__class__.__name__}:{str(error)[:200]}"
        )
        return 3


def _run_locked(
    args: argparse.Namespace,
    *,
    run_directory: Path,
    output_func: Callable[[str], None],
    input_func: Callable[[str], str],
    microphone_factory: Callable[..., Any],
    speaker_factory: Callable[..., Any],
) -> int:
    contract = canonical_pcm_contract()
    direct_path = run_directory / "direct_arecord.wav"
    persistent_path = run_directory / "persistent_stream.wav"
    report_path = run_directory / "pcm_integrity_report.json"
    comparison_device = str(
        resolve_alsa_capture_device(
            args.microphone_device,
            require_conversion=True,
        )
        or ""
    )
    adapter = microphone_factory(
        device=comparison_device or None,
        record_seconds=args.record_seconds,
        sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
        channels=CANONICAL_CHANNELS,
        sample_format=CANONICAL_PCM_SAMPLE_FORMAT,
        timeout_seconds=float(args.record_seconds + 5),
    )
    handle = None
    output_func("ARES persistent PCM integrity diagnostic")
    output_func(f"Output directory: {run_directory}")
    output_func(
        "Canonical standby PCM: "
        f"{contract['sample_rate_hz']} Hz, {contract['channels']} channel, "
        f"{contract['sample_format']}, {contract['sample_width_bytes']} bytes/sample; "
        f"{contract['frame_duration_ms']} ms = {contract['samples_per_frame']} samples "
        f"= {contract['frame_bytes']} bytes."
    )
    started = adapter.start()
    if not bool(getattr(started, "success", False)):
        try:
            stopped = adapter.stop()
        except Exception as error:
            raise RuntimeError(
                "microphone_preflight_and_cleanup_failed:"
                f"{error.__class__.__name__}"
            ) from error
        if not bool(getattr(stopped, "success", False)):
            raise RuntimeError("microphone_preflight_cleanup_failed")
        output_func(
            "Microphone preflight failed: "
            f"{getattr(started, 'error_message', '') or getattr(started, 'status', '')}"
        )
        return 3
    direct_results: list[Any] = []
    direct_pcm = b""
    persistent_pcm = b""
    persistent_snapshot: Dict[str, Any] = {}
    persistent_command: list[str] = []
    resolved_device = ""
    try:
        _wait_for_owner(
            input_func,
            "Direct arecord room-silence capture is next. Press Enter when the room is quiet.",
        )
        output_func("Direct arecord: remain silent for 1 second.")
        direct_silence_path = run_directory / "direct_arecord_room_silence.wav"
        direct_silence_result = adapter.record_wav(
            direct_silence_path,
            seconds=1,
            device=comparison_device or None,
            timeout_seconds=6.0,
            overwrite=False,
            diagnostic_audio=True,
        )
        direct_results.append(direct_silence_result)
        _require_capture_success(direct_silence_result, "direct_room_silence")
        normalized_silence_pcm, direct_silence_wav = _read_canonical_wav_pcm(
            direct_silence_path
        )
        direct_silence_raw_path, direct_silence_pcm, direct_silence_raw_wav = (
            _retained_raw_direct_pcm(
                direct_silence_result,
                normalized_pcm=normalized_silence_pcm,
                stage_name="room_silence",
            )
        )
        _wait_for_owner(
            input_func,
            "Direct arecord spoken capture is next. Press Enter, then speak loudly.",
        )
        spoken_seconds = args.record_seconds - 1
        output_func(
            f"Direct arecord: SPEAK LOUDLY for {spoken_seconds} second(s)."
        )
        direct_spoken_path = run_directory / "direct_arecord_spoken.wav"
        direct_spoken_result = adapter.record_wav(
            direct_spoken_path,
            seconds=spoken_seconds,
            device=comparison_device or None,
            timeout_seconds=float(spoken_seconds + 5),
            overwrite=False,
            diagnostic_audio=True,
        )
        direct_results.append(direct_spoken_result)
        _require_capture_success(direct_spoken_result, "direct_spoken")
        normalized_spoken_pcm, direct_spoken_wav = _read_canonical_wav_pcm(
            direct_spoken_path
        )
        direct_spoken_raw_path, direct_spoken_pcm, direct_spoken_raw_wav = (
            _retained_raw_direct_pcm(
                direct_spoken_result,
                normalized_pcm=normalized_spoken_pcm,
                stage_name="spoken",
            )
        )
        direct_pcm = direct_silence_pcm + direct_spoken_pcm
        write_audio_chunk_wav(
            AudioChunk(
                data=direct_pcm,
                sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                channels=CANONICAL_CHANNELS,
                sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                source="direct_arecord_pcm_integrity_diagnostic",
                metadata={
                    "requested_device": args.microphone_device,
                    "comparison_device": comparison_device,
                    "room_silence_header": direct_silence_wav,
                    "spoken_header": direct_spoken_wav,
                    "raw_room_silence_header": direct_silence_raw_wav,
                    "raw_spoken_header": direct_spoken_raw_wav,
                },
            ),
            direct_path,
        )

        _wait_for_owner(
            input_func,
            "Persistent-stream capture is next. Press Enter when the room is quiet.",
        )
        handle = adapter.open_persistent_stream(
            owner="persistent_pcm_diagnostic",
            device=comparison_device or None,
        )
        persistent_command = list(getattr(handle, "command", ()) or ())
        resolved_device = str(getattr(handle, "resolved_device", "") or "")
        total_frames = _frame_count(args.record_seconds)
        silence_frames = _frame_count(1)
        output_func("Persistent stream: remain silent for 1 second.")
        frames: list[bytes] = []
        try:
            for frame_index in range(total_frames):
                if frame_index == silence_frames:
                    output_func("Persistent stream: SPEAK LOUDLY NOW.")
                source_frame = handle.frame_source.read_frame(
                    CANONICAL_PCM_FRAME_BYTES,
                    1.0,
                )
                actual_length = len(source_frame)
                immutable_frame = bytes(source_frame[:actual_length])
                if actual_length != CANONICAL_PCM_FRAME_BYTES:
                    raise RuntimeError(
                        "persistent_stream_returned_incomplete_pcm_frame:"
                        f"{actual_length}"
                    )
                frames.append(immutable_frame)
        except Exception as error:
            persistent_snapshot = _safe_source_snapshot(handle.frame_source)
            partial_pcm = concatenate_owned_pcm_frames(frames)
            partial_path: Optional[Path] = None
            if partial_pcm:
                partial_path = run_directory / "persistent_stream_partial.wav"
                write_audio_chunk_wav(
                    AudioChunk(
                        data=partial_pcm,
                        sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                        channels=CANONICAL_CHANNELS,
                        sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                        source="persistent_pcm_integrity_failure_diagnostic",
                    ),
                    partial_path,
                )
            failure_report = {
                "success": False,
                "failure_stage": "persistent_stream_capture",
                "error": f"{error.__class__.__name__}:{str(error)[:160]}",
                "requested_device": args.microphone_device,
                "comparison_device": comparison_device,
                "resolved_persistent_device": resolved_device,
                "canonical_pcm_contract": contract,
                "direct_arecord_commands": _direct_arecord_commands(
                    direct_results
                ),
                "direct_arecord_raw_wav_headers": [
                    direct_silence_raw_wav,
                    direct_spoken_raw_wav,
                ],
                "persistent_arecord_command": persistent_command,
                "persistent_stream_counters": persistent_snapshot,
                "expected_persistent_frames": total_frames,
                "captured_persistent_frames": len(frames),
                "captured_persistent_bytes": len(partial_pcm),
                "direct": {
                    name: statistics.to_dict()
                    for name, statistics in _stage_statistics(
                        direct_pcm,
                        args.record_seconds,
                    ).items()
                },
                "persistent_partial": _partial_stage_statistics(partial_pcm),
                "files": {
                    "direct_arecord_wav": str(direct_path),
                    "direct_raw_room_silence_wav": str(
                        direct_silence_raw_path
                    ),
                    "direct_raw_spoken_wav": str(direct_spoken_raw_path),
                    "persistent_partial_wav": (
                        str(partial_path) if partial_path is not None else ""
                    ),
                },
            }
            _write_json_report(report_path, failure_report)
            output_func(f"Diagnostic report: {report_path}")
            _print_failure_signal_statistics(
                failure_report,
                output_func=output_func,
            )
            _print_counter_snapshot(
                persistent_snapshot,
                output_func=output_func,
            )
            raise
        persistent_pcm = concatenate_owned_pcm_frames(frames)
        persistent_snapshot = _safe_source_snapshot(handle.frame_source)
        write_audio_chunk_wav(
            AudioChunk(
                data=persistent_pcm,
                sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                channels=CANONICAL_CHANNELS,
                sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                source="persistent_pcm_integrity_diagnostic",
                metadata={
                    "requested_device": args.microphone_device,
                    "resolved_device": resolved_device,
                    "canonical_pcm": True,
                },
            ),
            persistent_path,
        )
    finally:
        cleanup_failures: list[str] = []
        cleanup_interruption: Optional[BaseException] = None
        if handle is not None:
            try:
                closed = adapter.close_persistent_stream(
                    handle,
                    owner="persistent_pcm_diagnostic",
                )
                if not bool(getattr(closed, "success", False)):
                    cleanup_failures.append("persistent_stream_close_failed")
            except BaseException as error:
                cleanup_failures.append(
                    "persistent_stream_close_error:"
                    f"{error.__class__.__name__}"
                )
                if not isinstance(error, Exception):
                    cleanup_interruption = error
        try:
            stopped = adapter.stop()
            if not bool(getattr(stopped, "success", False)):
                cleanup_failures.append("microphone_stop_failed")
        except BaseException as error:
            cleanup_failures.append(
                f"microphone_stop_error:{error.__class__.__name__}"
            )
            if not isinstance(error, Exception) and cleanup_interruption is None:
                cleanup_interruption = error
        if cleanup_interruption is not None:
            raise cleanup_interruption
        if cleanup_failures:
            raise RuntimeError(";".join(cleanup_failures))

    loaded_direct_pcm, direct_wav = _read_canonical_wav_pcm(direct_path)
    if loaded_direct_pcm != direct_pcm:
        raise RuntimeError("direct_combined_wav_pcm_mismatch")
    persistent_wav = validate_canonical_wav(persistent_path)
    direct_stages = _stage_statistics(direct_pcm, args.record_seconds)
    persistent_stages = _stage_statistics(persistent_pcm, args.record_seconds)
    report = {
        "success": False,
        "requested_device": args.microphone_device,
        "comparison_device": comparison_device,
        "resolved_persistent_device": resolved_device,
        "canonical_pcm_contract": contract,
        "direct_arecord_commands": _direct_arecord_commands(direct_results),
        "persistent_arecord_command": persistent_command,
        "direct_wav": direct_wav,
        "direct_arecord_raw_wav_headers": [
            direct_silence_raw_wav,
            direct_spoken_raw_wav,
        ],
        "persistent_wav": persistent_wav,
        "direct": {
            name: statistics.to_dict()
            for name, statistics in direct_stages.items()
        },
        "persistent": {
            name: statistics.to_dict()
            for name, statistics in persistent_stages.items()
        },
        "persistent_stream_counters": persistent_snapshot,
        "files": {
            "direct_arecord_wav": str(direct_path),
            "direct_room_silence_wav": str(direct_silence_path),
            "direct_spoken_wav": str(direct_spoken_path),
            "direct_raw_room_silence_wav": str(direct_silence_raw_path),
            "direct_raw_spoken_wav": str(direct_spoken_raw_path),
            "persistent_stream_wav": str(persistent_path),
        },
    }
    structural_success = _render_report(report, output_func=output_func)
    direct_signal_success = _signal_changed(direct_stages)
    persistent_signal_success = _signal_changed(persistent_stages)
    signal_success = bool(direct_signal_success and persistent_signal_success)
    report["direct_speech_signal_changed"] = direct_signal_success
    report["persistent_speech_signal_changed"] = persistent_signal_success
    playback_success = True
    if args.playback:
        playback_success = _play_recordings(
            speaker_factory=speaker_factory,
            speaker_device=args.speaker_device,
            paths=(direct_path, persistent_path),
            output_func=output_func,
        )
    report["playback_requested"] = bool(args.playback)
    report["playback_success"] = bool(playback_success)
    report["success"] = bool(
        structural_success and signal_success and playback_success
    )
    _write_json_report(report_path, report)
    output_func(f"Diagnostic report: {report_path}")
    if not structural_success:
        output_func("FAIL: persistent PCM framing or ownership integrity did not pass.")
        return 4
    if not direct_signal_success:
        output_func(
            "FAIL: direct arecord spoken audio did not reach a realistic diagnostic "
            "amplitude. Check the selected device and recording before wake reliability."
        )
        return 5
    if not persistent_signal_success:
        output_func(
            "FAIL: persistent spoken audio did not reach a realistic diagnostic "
            "amplitude. Do not run wake reliability yet."
        )
        return 5
    if not playback_success:
        output_func(
            "FAIL: requested playback did not complete; audible comparison remains "
            "unconfirmed. Do not run wake reliability yet."
        )
        return 6
    output_func(
        "PASS: persistent PCM is structurally valid and spoken audio changes its "
        "signed S16_LE samples. Listen to both WAV files before wake reliability."
    )
    return 0


def _render_report(
    report: Dict[str, Any],
    *,
    output_func: Callable[[str], None],
) -> bool:
    output_func("")
    output_func("Side-by-side signal statistics")
    for stage_name in ("room_silence", "spoken"):
        output_func(f"  Stage: {stage_name}")
        for source_name in ("direct", "persistent"):
            stats = dict(report[source_name][stage_name])
            output_func(
                f"    {source_name}: bytes={stats['byte_count']}; "
                f"samples={stats['sample_count']}; "
                f"min={stats['minimum_signed_sample']}; "
                f"max={stats['maximum_signed_sample']}; "
                f"mean_abs={stats['mean_absolute_amplitude']:.3f}; "
                f"rms={stats['rms']:.3f}; peak={stats['peak']}; "
                f"zero_samples={stats['zero_sample_percentage']:.3f}%; "
                f"repeated_frames={stats['repeated_frame_percentage']:.3f}%; "
                f"unique_frame_hashes={stats['unique_frame_hash_count']}"
            )
            output_func(
                "      first_32_signed_samples="
                f"{stats['first_32_decoded_signed_samples']}"
            )
    counters = dict(report["persistent_stream_counters"])
    _print_counter_snapshot(counters, output_func=output_func)
    direct_changed = _signal_changed_from_dict(report["direct"])
    persistent_changed = _signal_changed_from_dict(report["persistent"])
    direct_nonzero = _raw_bytes_nonzero(report["direct"])
    persistent_nonzero = _raw_bytes_nonzero(report["persistent"])
    expected_frames = int(report["persistent"]["all"]["frame_count"])
    valid_frames = int(counters.get("valid_full_pcm_frames", 0) or 0)
    expected_bytes = expected_frames * CANONICAL_PCM_FRAME_BYTES
    delivered_bytes = int(
        counters.get("valid_microphone_bytes_delivered_to_vad", 0) or 0
    )
    fresh_delivered_bytes = int(
        counters.get("fresh_microphone_bytes_delivered_to_vad", 0) or 0
    )
    direct_integrity = bool(
        _canonical_wav_metadata(report["direct_wav"])
        and len(report.get("direct_arecord_commands", [])) == 2
        and all(
            _canonical_arecord_command(
                command,
                report["comparison_device"],
                capture_type="wav",
            )
            for command in report.get("direct_arecord_commands", [])
        )
        and len(report.get("direct_arecord_raw_wav_headers", [])) == 2
        and all(
            _canonical_wav_metadata(header)
            for header in report.get("direct_arecord_raw_wav_headers", [])
        )
        and _stage_frame_variation_is_valid(report["direct"])
        and _stage_zero_density_is_valid(report["direct"])
    )
    persistent_integrity = bool(
        _canonical_wav_metadata(report["persistent_wav"])
        and _canonical_arecord_command(
            report.get("persistent_arecord_command", []),
            report["comparison_device"],
            capture_type="raw",
        )
        and report["resolved_persistent_device"] == report["comparison_device"]
        and valid_frames == expected_frames
        and delivered_bytes == expected_bytes
        and fresh_delivered_bytes == expected_bytes
        and int(counters.get("total_low_level_reads", 0) or 0)
        >= expected_frames
        and int(counters.get("empty_reads", 0) or 0) == 0
        and int(counters.get("read_errors", 0) or 0) == 0
        and int(counters.get("zero_filled_bytes", 0) or 0) == 0
        and int(counters.get("pending_partial_bytes", 0) or 0) == 0
        and int(counters.get("discarded_bytes", 0) or 0) == 0
        and int(counters.get("mutable_buffer_reuse_detected", 0) or 0) == 0
        and int(counters.get("replayed_frame_count", 0) or 0) == 0
        and _stage_frame_variation_is_valid(report["persistent"])
        and _stage_zero_density_is_valid(report["persistent"])
    )
    shared_process_path = _commands_share_executable(
        [
            *report.get("direct_arecord_commands", []),
            report.get("persistent_arecord_command", []),
        ]
    )
    structural_success = bool(
        direct_integrity and persistent_integrity and shared_process_path
    )
    direct_valid = bool(direct_integrity and direct_changed)
    persistent_valid = bool(
        persistent_integrity and shared_process_path and persistent_changed
    )
    report["direct_structural_integrity"] = direct_integrity
    report["persistent_structural_integrity"] = persistent_integrity
    report["same_arecord_process_path"] = shared_process_path
    output_func("")
    output_func("Diagnostic answers")
    for command_index, command in enumerate(
        report.get("direct_arecord_commands", []),
        start=1,
    ):
        output_func(
            f"  Direct arecord command {command_index}: "
            + " ".join(str(value) for value in command)
        )
    output_func(
        "  Persistent arecord command: "
        + " ".join(
            str(value) for value in report.get("persistent_arecord_command", [])
        )
    )
    output_func(f"  Direct canonical WAV bytes nonzero: {_yes_no(direct_nonzero)}")
    output_func(f"  Raw persistent ALSA bytes nonzero: {_yes_no(persistent_nonzero)}")
    output_func(
        "  Direct signed S16_LE samples change while speaking: "
        f"{_yes_no(direct_changed)}"
    )
    output_func(
        "  Persistent signed S16_LE samples change while speaking: "
        f"{_yes_no(persistent_changed)}"
    )
    output_func(
        "  Persistent source repeats pathologically within a stage: "
        f"{_yes_no(not _stage_frame_variation_is_valid(report['persistent']))}"
    )
    output_func(
        "  Direct valid while persistent is not: "
        f"{_yes_no(direct_valid and not persistent_valid)}"
    )
    output_func(
        "  Selected persistent device: "
        f"{report['resolved_persistent_device'] or '<default>'}"
    )
    output_func(
        "  Persistent requested transport format: "
        f"{CANONICAL_SAMPLE_RATE_HZ} Hz mono {CANONICAL_PCM_SAMPLE_FORMAT}; "
        "raw stdout is headerless, so the command, exact frame sizes, and saved "
        "WAV header are the available transport evidence."
    )
    for header_index, raw_header_value in enumerate(
        report.get("direct_arecord_raw_wav_headers", []),
        start=1,
    ):
        raw_header = dict(raw_header_value or {})
        output_func(
            f"  Direct arecord raw WAV header {header_index}: "
            f"{int(raw_header.get('sample_rate_hz', 0) or 0)} Hz / "
            f"{int(raw_header.get('channels', 0) or 0)} channel(s) / "
            f"{int(raw_header.get('sample_width_bytes', 0) or 0)} bytes/sample"
        )
    direct_wav = dict(report.get("direct_wav", {}) or {})
    persistent_wav = dict(report.get("persistent_wav", {}) or {})
    output_func(
        "  Saved direct/persistent WAV headers: "
        f"{int(direct_wav.get('sample_rate_hz', 0) or 0)}Hz/"
        f"{int(direct_wav.get('channels', 0) or 0)}ch/"
        f"{int(direct_wav.get('sample_width_bytes', 0) or 0)}B; "
        f"{int(persistent_wav.get('sample_rate_hz', 0) or 0)}Hz/"
        f"{int(persistent_wav.get('channels', 0) or 0)}ch/"
        f"{int(persistent_wav.get('sample_width_bytes', 0) or 0)}B"
    )
    output_func(
        "  Complete frame boundary: "
        f"{CANONICAL_PCM_FRAME_BYTES} bytes; expected={expected_frames}; "
        f"valid={valid_frames}; partial_low_level_reads="
        f"{int(counters.get('partial_reads', 0) or 0)}"
    )
    output_func(
        "  Mutable source buffer reused across a boundary: "
        f"{_yes_no(int(counters.get('mutable_buffer_reuse_detected', 0) or 0) > 0)}"
    )
    output_func(f"  Direct path integrity: {_yes_no(direct_integrity)}")
    output_func(f"  Persistent path integrity: {_yes_no(persistent_integrity)}")
    output_func(f"  Same arecord process path: {_yes_no(shared_process_path)}")
    output_func(f"  Structural PCM integrity: {_yes_no(structural_success)}")
    output_func(f"  Direct WAV: {report['files']['direct_arecord_wav']}")
    output_func(
        "  Retained raw direct WAV stages: "
        f"{report['files']['direct_raw_room_silence_wav']}; "
        f"{report['files']['direct_raw_spoken_wav']}"
    )
    output_func(f"  Persistent WAV: {report['files']['persistent_stream_wav']}")
    return structural_success


def _print_counter_snapshot(
    counters: Dict[str, Any],
    *,
    output_func: Callable[[str], None],
) -> None:
    output_func("")
    output_func("Persistent low-level read counters")
    for key in (
        "total_low_level_reads",
        "valid_full_pcm_frames",
        "partial_reads",
        "empty_reads",
        "read_errors",
        "discarded_bytes",
        "zero_filled_bytes",
        "repeated_frame_hashes",
        "mutable_buffer_reuse_detected",
        "valid_microphone_bytes_delivered_to_vad",
        "fresh_microphone_bytes_delivered_to_vad",
        "pending_partial_bytes",
        "pending_discard_alignment_bytes",
    ):
        output_func(f"  {key}: {int(counters.get(key, 0) or 0)}")


def _print_failure_signal_statistics(
    report: Dict[str, Any],
    *,
    output_func: Callable[[str], None],
) -> None:
    output_func("")
    output_func("Partial side-by-side signal statistics")
    for stage_name in ("room_silence", "spoken"):
        output_func(f"  Stage: {stage_name}")
        for source_name, report_key in (
            ("direct", "direct"),
            ("persistent_partial", "persistent_partial"),
        ):
            statistics = dict(
                dict(report.get(report_key, {}) or {}).get(stage_name, {}) or {}
            )
            output_func(
                f"    {source_name}: bytes={int(statistics.get('byte_count', 0) or 0)}; "
                f"samples={int(statistics.get('sample_count', 0) or 0)}; "
                f"min={int(statistics.get('minimum_signed_sample', 0) or 0)}; "
                f"max={int(statistics.get('maximum_signed_sample', 0) or 0)}; "
                f"mean_abs={float(statistics.get('mean_absolute_amplitude', 0.0) or 0.0):.3f}; "
                f"rms={float(statistics.get('rms', 0.0) or 0.0):.3f}; "
                f"peak={int(statistics.get('peak', 0) or 0)}; "
                f"zero_samples={float(statistics.get('zero_sample_percentage', 0.0) or 0.0):.3f}%; "
                f"repeated_frames={float(statistics.get('repeated_frame_percentage', 0.0) or 0.0):.3f}%; "
                f"unique_frame_hashes={int(statistics.get('unique_frame_hash_count', 0) or 0)}"
            )
            output_func(
                "      first_32_signed_samples="
                f"{statistics.get('first_32_decoded_signed_samples', [])}"
            )


def _stage_statistics(
    pcm: bytes,
    record_seconds: int,
) -> Dict[str, PcmIntegrityStatistics]:
    one_second_bytes = (
        CANONICAL_SAMPLE_RATE_HZ
        * CANONICAL_CHANNELS
        * CANONICAL_SAMPLE_WIDTH_BYTES
    )
    expected_bytes = record_seconds * one_second_bytes
    if len(pcm) != expected_bytes:
        raise ValueError(
            f"diagnostic_pcm_duration_mismatch:{len(pcm)}:{expected_bytes}"
        )
    silence = pcm[:one_second_bytes]
    spoken = pcm[one_second_bytes:]
    return {
        "room_silence": analyze_s16_le_pcm_integrity(silence),
        "spoken": analyze_s16_le_pcm_integrity(spoken),
        "all": analyze_s16_le_pcm_integrity(pcm),
    }


def _partial_stage_statistics(pcm: bytes) -> Dict[str, Dict[str, Any]]:
    one_second_bytes = (
        CANONICAL_SAMPLE_RATE_HZ
        * CANONICAL_CHANNELS
        * CANONICAL_SAMPLE_WIDTH_BYTES
    )
    room_silence = pcm[:one_second_bytes]
    spoken = pcm[one_second_bytes:]
    return {
        "room_silence": analyze_s16_le_pcm_integrity(room_silence).to_dict(),
        "spoken": analyze_s16_le_pcm_integrity(spoken).to_dict(),
        "all": analyze_s16_le_pcm_integrity(pcm).to_dict(),
    }


def _safe_source_snapshot(source: Any) -> Dict[str, Any]:
    snapshot = getattr(source, "snapshot", None)
    if not callable(snapshot):
        return {"snapshot_error": "snapshot_unavailable"}
    try:
        value = snapshot()
    except Exception as error:
        return {"snapshot_error": f"{error.__class__.__name__}:{str(error)[:120]}"}
    if not isinstance(value, dict):
        return {"snapshot_error": "snapshot_not_a_mapping"}
    return dict(value)


def _direct_arecord_commands(results: Sequence[Any]) -> list[list[str]]:
    return [
        [
            str(value)
            for value in (
                dict(getattr(result, "data", {}) or {})
                .get("process", {})
                .get("args", [])
            )
        ]
        for result in results
    ]


def _read_canonical_wav_pcm(path: Path) -> tuple[bytes, Dict[str, Any]]:
    validation = validate_canonical_wav(path)
    if not validation.get("success"):
        raise ValueError(
            str(validation.get("error_message") or "diagnostic_wav_not_canonical")
        )
    with wave.open(str(path), "rb") as wav_file:
        pcm = wav_file.readframes(wav_file.getnframes())
    return bytes(pcm), validation


def _retained_raw_direct_pcm(
    result: Any,
    *,
    normalized_pcm: bytes,
    stage_name: str,
) -> tuple[Path, bytes, Dict[str, Any]]:
    data = dict(getattr(result, "data", {}) or {})
    raw_path_value = str(data.get("raw_wav_path", "") or "").strip()
    if not raw_path_value:
        raise RuntimeError(f"direct_{stage_name}_raw_wav_not_retained")
    raw_path = Path(raw_path_value)
    raw_pcm, raw_wav = _read_canonical_wav_pcm(raw_path)
    if raw_pcm != normalized_pcm:
        raise RuntimeError(f"direct_{stage_name}_raw_pcm_changed_by_normalization")
    reported_raw_wav = dict(data.get("raw_wav", {}) or {})
    if not _canonical_wav_metadata(reported_raw_wav):
        raise RuntimeError(f"direct_{stage_name}_raw_wav_not_canonical")
    for field_name in (
        "sample_rate_hz",
        "channels",
        "sample_width_bytes",
        "frames",
    ):
        if int(reported_raw_wav.get(field_name, 0) or 0) != int(
            raw_wav.get(field_name, 0) or 0
        ):
            raise RuntimeError(
                f"direct_{stage_name}_raw_header_mismatch:{field_name}"
            )
    return raw_path, raw_pcm, raw_wav


def _signal_changed(stages: Dict[str, PcmIntegrityStatistics]) -> bool:
    silence = stages["room_silence"]
    spoken = stages["spoken"]
    return bool(
        spoken.peak >= MINIMUM_DIAGNOSTIC_SPOKEN_PEAK
        and spoken.rms >= MINIMUM_DIAGNOSTIC_SPOKEN_RMS
        and spoken.rms >= silence.rms * MINIMUM_DIAGNOSTIC_RMS_RATIO
        and spoken.rms >= silence.rms + MINIMUM_DIAGNOSTIC_RMS_DELTA
        and spoken.maximum_signed_sample != spoken.minimum_signed_sample
    )


def _signal_changed_from_dict(stages: Dict[str, Dict[str, Any]]) -> bool:
    silence = stages["room_silence"]
    spoken = stages["spoken"]
    return bool(
        float(spoken["peak"]) >= MINIMUM_DIAGNOSTIC_SPOKEN_PEAK
        and float(spoken["rms"]) >= MINIMUM_DIAGNOSTIC_SPOKEN_RMS
        and float(spoken["rms"])
        >= float(silence["rms"]) * MINIMUM_DIAGNOSTIC_RMS_RATIO
        and float(spoken["rms"])
        >= float(silence["rms"]) + MINIMUM_DIAGNOSTIC_RMS_DELTA
        and int(spoken["maximum_signed_sample"])
        != int(spoken["minimum_signed_sample"])
    )


def _raw_bytes_nonzero(stages: Dict[str, Dict[str, Any]]) -> bool:
    all_stats = stages["all"]
    return bool(
        int(all_stats["peak"]) > 0
        and float(all_stats["zero_sample_percentage"]) < 100.0
    )


def _stage_frame_variation_is_valid(stages: Dict[str, Dict[str, Any]]) -> bool:
    for stage_name in ("room_silence", "spoken"):
        statistics = dict(stages.get(stage_name, {}) or {})
        frame_count = int(statistics.get("frame_count", 0) or 0)
        unique_count = int(
            statistics.get("unique_frame_hash_count", 0) or 0
        )
        repeated_percentage = float(
            statistics.get("repeated_frame_percentage", 0.0) or 0.0
        )
        if frame_count > 1 and (
            unique_count <= 1
            or repeated_percentage
            >= MAXIMUM_DIAGNOSTIC_REPEATED_FRAME_PERCENTAGE
        ):
            return False
    return True


def _stage_zero_density_is_valid(stages: Dict[str, Dict[str, Any]]) -> bool:
    for stage_name in ("room_silence", "spoken"):
        statistics = dict(stages.get(stage_name, {}) or {})
        if int(statistics.get("sample_count", 0) or 0) <= 0:
            return False
        if (
            float(statistics.get("zero_sample_percentage", 100.0) or 0.0)
            >= MAXIMUM_DIAGNOSTIC_ZERO_SAMPLE_PERCENTAGE
        ):
            return False
    return True


def _canonical_wav_metadata(value: Any) -> bool:
    metadata = dict(value or {}) if isinstance(value, dict) else {}
    return bool(
        metadata.get("success")
        and int(metadata.get("sample_rate_hz", 0) or 0)
        == CANONICAL_SAMPLE_RATE_HZ
        and int(metadata.get("channels", 0) or 0) == CANONICAL_CHANNELS
        and int(metadata.get("sample_width_bytes", 0) or 0)
        == CANONICAL_SAMPLE_WIDTH_BYTES
    )


def _canonical_arecord_command(
    command: Any,
    device: Any,
    *,
    capture_type: str,
) -> bool:
    values = [str(value) for value in (command or [])]
    expected = {
        "-f": CANONICAL_PCM_SAMPLE_FORMAT,
        "-c": str(CANONICAL_CHANNELS),
        "-r": str(CANONICAL_SAMPLE_RATE_HZ),
        "-D": str(device or ""),
        "-t": str(capture_type),
    }
    for flag, expected_value in expected.items():
        try:
            actual_value = values[values.index(flag) + 1]
        except (ValueError, IndexError):
            return False
        if actual_value != expected_value:
            return False
    if capture_type == "raw" and (not values or values[-1] != "-"):
        return False
    return True


def _commands_share_executable(commands: Sequence[Any]) -> bool:
    executables = {
        str(command[0])
        for command in commands
        if isinstance(command, (list, tuple)) and command
    }
    return len(executables) == 1 and len(commands) > 0


def _play_recordings(
    *,
    speaker_factory: Callable[..., Any],
    speaker_device: str,
    paths: Sequence[Path],
    output_func: Callable[[str], None],
) -> bool:
    speaker = speaker_factory(device=speaker_device, timeout_seconds=30.0)
    try:
        started = speaker.start()
    except Exception as error:
        output_func(f"Playback unavailable: {error.__class__.__name__}")
        try:
            speaker.stop()
        except Exception:
            pass
        return False
    if not bool(getattr(started, "success", False)):
        output_func(
            "Playback unavailable: "
            f"{getattr(started, 'error_message', '') or getattr(started, 'status', '')}"
        )
        try:
            speaker.stop()
        except Exception:
            pass
        return False
    success = True
    try:
        for path in paths:
            output_func(f"Playing {path.name}...")
            played = speaker.play_wav(path, device=speaker_device)
            if not bool(getattr(played, "success", False)):
                success = False
                output_func(
                    f"Playback failed for {path.name}: "
                    f"{getattr(played, 'error_message', '') or getattr(played, 'status', '')}"
                )
    except Exception as error:
        success = False
        output_func(f"Playback error: {error.__class__.__name__}")
    finally:
        try:
            stopped = speaker.stop()
            if not bool(getattr(stopped, "success", False)):
                success = False
                output_func("Playback cleanup failed: speaker_stop_failed")
        except Exception as error:
            success = False
            output_func(
                f"Playback cleanup failed: {error.__class__.__name__}"
            )
    return success


def _require_capture_success(result: Any, stage_name: str) -> None:
    if bool(getattr(result, "success", False)):
        return
    detail = getattr(result, "error_message", "") or getattr(
        result,
        "status",
        "",
    )
    raise RuntimeError(f"{stage_name}_capture_failed:{str(detail)[:120]}")


def _write_json_report(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _wait_for_owner(input_func: Callable[[str], str], prompt: str) -> None:
    input_func(f"{prompt} ")


def _new_run_directory(output_root: Path) -> Path:
    root = output_root.expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = root / f"pcm-{stamp}-{os.getpid()}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _frame_count(seconds: int) -> int:
    return int(seconds * 1000 // CANONICAL_PCM_FRAME_DURATION_MS)


def _validate_args(args: argparse.Namespace) -> str:
    if isinstance(args.record_seconds, bool) or not 2 <= args.record_seconds <= 10:
        return "--record-seconds must be between 2 and 10"
    for label, value in (
        ("microphone", args.microphone_device),
        ("speaker", args.speaker_device),
    ):
        clean = str(value or "").strip()
        if not clean or len(clean) > 128:
            return f"{label} device is invalid"
    return ""


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


if __name__ == "__main__":
    raise SystemExit(run_pcm_diagnostic())
