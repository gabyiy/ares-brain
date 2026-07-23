from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
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
    analyze_wav_audio,
    write_audio_chunk_wav,
)
from memory.schema_migrations import MigrationError, StoreWriteLock  # noqa: E402


DEFAULT_MICROPHONE_DEVICE = "plughw:2,0"
DEFAULT_SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0"
DEFAULT_RECORD_SECONDS = 3
DIRECT_NATIVE_SAMPLE_RATE_HZ = 44_100
DIRECT_NATIVE_FRAME_BYTES = (
    DIRECT_NATIVE_SAMPLE_RATE_HZ
    * CANONICAL_PCM_FRAME_DURATION_MS
    // 1000
    * CANONICAL_CHANNELS
    * CANONICAL_SAMPLE_WIDTH_BYTES
)
DEFAULT_RUNTIME_LOCK_PATH = REPO_ROOT / "data" / "runtime" / "ares_standby_voice.runtime"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "runtime" / "pcm_integrity"
MINIMUM_DIAGNOSTIC_SPOKEN_RMS = 50.0
MINIMUM_DIAGNOSTIC_SPOKEN_PEAK = 200
MINIMUM_DIAGNOSTIC_RMS_RATIO = 2.0
MINIMUM_DIAGNOSTIC_RMS_DELTA = 25.0
MAXIMUM_DIAGNOSTIC_REPEATED_FRAME_PERCENTAGE = 50.0
MAXIMUM_DIAGNOSTIC_ZERO_SAMPLE_PERCENTAGE = 99.0
MINIMUM_PERSISTENT_TO_DIRECT_SPOKEN_RMS_RATIO = 0.20
MINIMUM_CROSS_PATH_RMS_RATIO_COMPARABILITY = 0.10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare direct arecord WAV capture with the exact persistent standby "
            "raw-PCM path before running wake/VAD reliability checks."
        )
    )
    parser.add_argument("--microphone-device", default=DEFAULT_MICROPHONE_DEVICE)
    parser.add_argument("--speaker-device", default=DEFAULT_SPEAKER_DEVICE)
    parser.add_argument(
        "--record-seconds",
        type=int,
        default=DEFAULT_RECORD_SECONDS,
        help=(
            "Seconds per quiet/spoken phase for each path (default: 3; the "
            "bounded comparison therefore records 6 seconds per path)."
        ),
    )
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
    direct_capture_func: Optional[Callable[..., Dict[str, Any]]] = None,
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
                direct_capture_func=(
                    direct_capture_func or _capture_direct_native_wav
                ),
                production_process_metadata_required=(
                    microphone_factory is LinuxAlsaMicrophoneAdapter
                ),
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
    direct_capture_func: Callable[..., Dict[str, Any]],
    production_process_metadata_required: bool,
) -> int:
    contract = canonical_pcm_contract()
    phase_seconds = int(args.record_seconds)
    direct_quiet_path = run_directory / "direct_quiet.wav"
    direct_spoken_path = run_directory / "direct_spoken.wav"
    persistent_quiet_path = run_directory / "persistent_quiet.wav"
    persistent_spoken_path = run_directory / "persistent_spoken.wav"
    report_path = run_directory / "pcm_integrity_report.json"
    persistent_device = str(
        resolve_alsa_capture_device(
            args.microphone_device,
            require_conversion=True,
        )
        or ""
    )
    direct_device = _native_direct_capture_device(args.microphone_device)
    adapter = microphone_factory(
        device=persistent_device or None,
        record_seconds=phase_seconds,
        sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
        channels=CANONICAL_CHANNELS,
        sample_format=CANONICAL_PCM_SAMPLE_FORMAT,
        timeout_seconds=float(phase_seconds + 5),
    )
    handle = None
    adapter_started = False
    persistent_quiet_frames: list[bytes] = []
    persistent_spoken_frames: list[bytes] = []
    persistent_pre_close_snapshot: Dict[str, Any] = {}
    persistent_post_close_snapshot: Dict[str, Any] = {}
    direct_processes: list[Dict[str, Any]] = []
    cleanup_failures: list[str] = []
    capture_error = ""
    capture_interrupted = False
    report: Dict[str, Any] = {
        "success": False,
        "protocol": {
            "quiet_seconds": phase_seconds,
            "spoken_seconds": phase_seconds,
            "direct_transport": "native_arecord_wav",
            "persistent_transport": "production_raw_pcm_stream",
        },
        "requested_device": args.microphone_device,
        "direct_native_device": direct_device,
        "persistent_conversion_device": persistent_device,
        "canonical_pcm_contract": contract,
        "direct_native_pcm_contract": {
            "sample_rate_hz": DIRECT_NATIVE_SAMPLE_RATE_HZ,
            "channels": CANONICAL_CHANNELS,
            "sample_format": CANONICAL_PCM_SAMPLE_FORMAT,
            "sample_width_bytes": CANONICAL_SAMPLE_WIDTH_BYTES,
            "frame_duration_ms": CANONICAL_PCM_FRAME_DURATION_MS,
            "frame_bytes": DIRECT_NATIVE_FRAME_BYTES,
        },
        "production_process_metadata_required": bool(
            production_process_metadata_required
        ),
        "direct_processes": direct_processes,
        "persistent_process": {},
        "direct": {},
        "persistent": {},
        "signal_comparison": {},
        "persistent_stream_counters": {},
        "persistent_post_close_counters": {},
        "files": {
            "direct_quiet_wav": str(direct_quiet_path),
            "direct_spoken_wav": str(direct_spoken_path),
            "persistent_quiet_wav": str(persistent_quiet_path),
            "persistent_spoken_wav": str(persistent_spoken_path),
            "json_report": str(report_path),
        },
    }
    output_func("ARES persistent PCM integrity diagnostic")
    output_func(f"Output directory: {run_directory}")
    output_func(
        "Direct native baseline: "
        f"{DIRECT_NATIVE_SAMPLE_RATE_HZ} Hz mono {CANONICAL_PCM_SAMPLE_FORMAT} "
        f"from {direct_device}."
    )
    output_func(
        "Canonical standby PCM: "
        f"{contract['sample_rate_hz']} Hz, {contract['channels']} channel, "
        f"{contract['sample_format']}, {contract['sample_width_bytes']} bytes/sample; "
        f"{contract['frame_duration_ms']} ms = {contract['samples_per_frame']} samples "
        f"= {contract['frame_bytes']} bytes."
    )

    try:
        for stage_name, output_path, prompt, instruction in (
            (
                "quiet",
                direct_quiet_path,
                "Direct native quiet capture is next. Press Enter when the room is quiet.",
                f"Direct native arecord: remain quiet for {phase_seconds} seconds.",
            ),
            (
                "spoken",
                direct_spoken_path,
                "Direct native spoken capture is next. Press Enter, then speak loudly.",
                f"Direct native arecord: SPEAK LOUDLY for {phase_seconds} seconds.",
            ),
        ):
            _wait_for_owner(input_func, prompt)
            output_func(instruction)
            process = dict(
                direct_capture_func(
                    output_path=output_path,
                    device=direct_device,
                    seconds=phase_seconds,
                    sample_rate_hz=DIRECT_NATIVE_SAMPLE_RATE_HZ,
                    channels=CANONICAL_CHANNELS,
                    sample_format=CANONICAL_PCM_SAMPLE_FORMAT,
                    timeout_seconds=float(phase_seconds + 5),
                )
                or {}
            )
            process["stage"] = stage_name
            direct_processes.append(process)
            _require_direct_capture_success(process, stage_name)
            pcm, header = _read_s16_wav_pcm(
                output_path,
                expected_sample_rate_hz=DIRECT_NATIVE_SAMPLE_RATE_HZ,
                expected_channels=CANONICAL_CHANNELS,
                expected_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                expected_duration_seconds=phase_seconds,
            )
            report["direct"][stage_name] = _stage_report(
                pcm,
                header=header,
                sample_rate_hz=DIRECT_NATIVE_SAMPLE_RATE_HZ,
                frame_bytes=DIRECT_NATIVE_FRAME_BYTES,
            )

        started = adapter.start()
        if not bool(getattr(started, "success", False)):
            detail = getattr(started, "error_message", "") or getattr(
                started,
                "status",
                "",
            )
            raise RuntimeError(f"microphone_preflight_failed:{str(detail)[:120]}")
        adapter_started = True
        _wait_for_owner(
            input_func,
            (
                "Persistent production-stream comparison is next: remain quiet "
                f"for {phase_seconds} seconds, then speak loudly for "
                f"{phase_seconds} seconds when prompted. Press Enter when ready."
            ),
        )
        handle = adapter.open_persistent_stream(
            owner="persistent_pcm_diagnostic",
            device=persistent_device or None,
        )
        report["resolved_persistent_device"] = str(
            getattr(handle, "resolved_device", "") or ""
        )
        report["persistent_arecord_command"] = list(
            getattr(handle, "command", ()) or ()
        )
        report["persistent_stream_id"] = str(
            getattr(handle, "stream_id", "") or ""
        )
        report["persistent_alsa_handle_id"] = str(
            getattr(handle, "alsa_handle_id", "") or ""
        )
        output_func(
            f"Persistent stream: remain quiet for {phase_seconds} seconds."
        )
        _capture_persistent_phase(
            handle.frame_source,
            seconds=phase_seconds,
            destination=persistent_quiet_frames,
        )
        persistent_quiet_pcm = concatenate_owned_pcm_frames(
            persistent_quiet_frames
        )
        _write_canonical_stage_wav(
            persistent_quiet_path,
            persistent_quiet_pcm,
            source="persistent_pcm_integrity_quiet",
        )
        output_func(
            f"Persistent stream: SPEAK LOUDLY NOW for {phase_seconds} seconds."
        )
        _capture_persistent_phase(
            handle.frame_source,
            seconds=phase_seconds,
            destination=persistent_spoken_frames,
        )
        persistent_spoken_pcm = concatenate_owned_pcm_frames(
            persistent_spoken_frames
        )
        _write_canonical_stage_wav(
            persistent_spoken_path,
            persistent_spoken_pcm,
            source="persistent_pcm_integrity_spoken",
        )
        for stage_name, path, pcm in (
            ("quiet", persistent_quiet_path, persistent_quiet_pcm),
            ("spoken", persistent_spoken_path, persistent_spoken_pcm),
        ):
            loaded_pcm, header = _read_s16_wav_pcm(
                path,
                expected_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                expected_channels=CANONICAL_CHANNELS,
                expected_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
                expected_duration_seconds=phase_seconds,
            )
            if loaded_pcm != pcm:
                raise RuntimeError(
                    f"persistent_{stage_name}_wav_pcm_mismatch"
                )
            report["persistent"][stage_name] = _stage_report(
                pcm,
                header=header,
                sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
                frame_bytes=CANONICAL_PCM_FRAME_BYTES,
            )
        persistent_pre_close_snapshot = _safe_source_snapshot(
            handle.frame_source
        )
    except BaseException as error:
        capture_error = f"{error.__class__.__name__}:{str(error)[:200]}"
        capture_interrupted = isinstance(error, KeyboardInterrupt)
        report["capture_error"] = capture_error
        report["failure_stage"] = _failure_stage(report)
    finally:
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
            persistent_post_close_snapshot = _safe_source_snapshot(
                handle.frame_source
            )
        if adapter_started:
            try:
                stopped = adapter.stop()
                if not bool(getattr(stopped, "success", False)):
                    cleanup_failures.append("microphone_stop_failed")
            except BaseException as error:
                cleanup_failures.append(
                    f"microphone_stop_error:{error.__class__.__name__}"
                )
        if handle is not None:
            persistent_post_close_snapshot = _safe_source_snapshot(
                handle.frame_source
            )

    if not persistent_pre_close_snapshot and handle is not None:
        persistent_pre_close_snapshot = _safe_source_snapshot(
            handle.frame_source
        )
    report["persistent_stream_counters"] = persistent_pre_close_snapshot
    report["persistent_post_close_counters"] = persistent_post_close_snapshot
    report["persistent_process"] = _persistent_process_report(
        handle,
        before_close=persistent_pre_close_snapshot,
        after_close=persistent_post_close_snapshot,
    )
    report["persistent_transport"] = {
        "short_read_count": int(
            persistent_pre_close_snapshot.get("partial_reads", 0) or 0
        ),
        "accumulated_partial_bytes": int(
            persistent_pre_close_snapshot.get(
                "accumulated_partial_bytes",
                0,
            )
            or 0
        ),
        "pending_partial_bytes": int(
            persistent_pre_close_snapshot.get("pending_partial_bytes", 0)
            or 0
        ),
        "consecutive_duplicate_frame_count": int(
            persistent_pre_close_snapshot.get(
                "maximum_consecutive_repeated_non_silent_frames",
                0,
            )
            or 0
        ),
        "process_pid": report["persistent_process"].get("process_pid"),
        "process_exit_status": report["persistent_process"].get(
            "exit_status_after_close"
        ),
        "exact_capture_command": list(
            report.get("persistent_arecord_command", []) or []
        ),
    }
    report["cleanup_failures"] = cleanup_failures
    try:
        _retain_partial_persistent_stage(
            report,
            path=persistent_quiet_path,
            frames=persistent_quiet_frames,
            stage_name="quiet",
        )
        _retain_partial_persistent_stage(
            report,
            path=persistent_spoken_path,
            frames=persistent_spoken_frames,
            stage_name="spoken",
        )
    except (OSError, RuntimeError, ValueError, wave.Error) as error:
        cleanup_failures.append(
            f"partial_stage_retention_error:{error.__class__.__name__}"
        )
    _populate_signal_comparison(report)
    structural_success = _render_report(report, output_func=output_func)
    direct_signal_success = bool(
        report["signal_comparison"].get("direct_signal_changed", False)
    )
    persistent_signal_success = bool(
        report["signal_comparison"].get("persistent_signal_changed", False)
    )
    comparability_success = bool(
        report["signal_comparison"].get("cross_path_comparable", False)
    )
    playback_success = True
    direct_audible: Optional[bool] = None
    persistent_audible: Optional[bool] = None
    if (
        args.playback
        and not capture_error
        and not cleanup_failures
        and direct_spoken_path.exists()
        and persistent_spoken_path.exists()
    ):
        playback_success = _play_recordings(
            speaker_factory=speaker_factory,
            speaker_device=args.speaker_device,
            paths=(direct_spoken_path, persistent_spoken_path),
            output_func=output_func,
        )
        if playback_success:
            direct_audible = _ask_owner_yes_no(
                input_func,
                "Was the direct spoken WAV clear and audible? [yes/no]",
            )
            persistent_audible = _ask_owner_yes_no(
                input_func,
                "Was the persistent spoken WAV clear and audible? [yes/no]",
            )
    elif args.playback:
        playback_success = False
    report["playback_requested"] = bool(args.playback)
    report["playback_success"] = bool(playback_success)
    report["direct_spoken_audible_confirmed"] = direct_audible
    report["persistent_spoken_audible_confirmed"] = persistent_audible
    audibility_success = bool(direct_audible and persistent_audible)
    report["audibility_confirmed"] = audibility_success
    report["success"] = bool(
        not capture_error
        and not cleanup_failures
        and structural_success
        and direct_signal_success
        and persistent_signal_success
        and comparability_success
        and playback_success
        and audibility_success
    )
    _write_json_report(report_path, report)
    output_func(f"Diagnostic report: {report_path}")
    if capture_interrupted:
        raise KeyboardInterrupt
    if capture_error or cleanup_failures:
        output_func(
            "FAIL: capture or cleanup failed; the failure JSON and any completed "
            "stage WAVs were retained."
        )
        return 3
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
    if not comparability_success:
        output_func(
            "FAIL: persistent speech amplitude/contrast is not reasonably "
            "comparable to the direct native baseline. Do not run wake reliability."
        )
        return 5
    if not playback_success or not audibility_success:
        output_func(
            "FAIL: direct and persistent spoken WAV audibility was not explicitly "
            "confirmed. Do not run wake reliability yet."
        )
        return 6
    output_func(
        "PASS: the native direct and exact production persistent captures are "
        "structurally valid, comparable, and explicitly confirmed audible."
    )
    return 0


def _capture_direct_native_wav(
    *,
    output_path: Path,
    device: str,
    seconds: int,
    sample_rate_hz: int,
    channels: int,
    sample_format: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    arecord_path = shutil.which("arecord")
    if not arecord_path:
        return {
            "success": False,
            "command": [],
            "process_pid": None,
            "process_exit_status": None,
            "timed_out": False,
            "error": "arecord_missing",
        }
    command = [
        arecord_path,
        "-D",
        str(device),
        "-f",
        str(sample_format),
        "-r",
        str(int(sample_rate_hz)),
        "-c",
        str(int(channels)),
        "-d",
        str(int(seconds)),
        "-t",
        "wav",
        str(output_path),
    ]
    process: Any = None
    timed_out = False
    stderr = b""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
        )
        try:
            _, stderr = process.communicate(timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                _, stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=2.0)
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "success": False,
            "command": command,
            "process_pid": getattr(process, "pid", None),
            "process_exit_status": getattr(process, "returncode", None),
            "timed_out": timed_out,
            "stderr": "",
            "error": f"{error.__class__.__name__}:{str(error)[:160]}",
        }
    returncode = getattr(process, "returncode", None)
    stderr_text = bytes(stderr or b"").decode("utf-8", errors="replace")[:1000]
    return {
        "success": bool(
            not timed_out
            and returncode == 0
            and output_path.exists()
        ),
        "command": command,
        "process_pid": getattr(process, "pid", None),
        "process_exit_status": returncode,
        "timed_out": timed_out,
        "stderr": stderr_text,
        "error": (
            ""
            if not timed_out and returncode == 0
            else ("process_timeout" if timed_out else f"arecord_exit_{returncode}")
        ),
    }


def _native_direct_capture_device(device: Any) -> str:
    clean = str(device or "").strip()
    match = re.fullmatch(r"(?:plug)?hw:(\d+),(\d+)", clean)
    if match is None:
        raise ValueError(
            "direct native comparison requires hw:CARD,DEVICE or "
            "plughw:CARD,DEVICE"
        )
    return f"hw:{match.group(1)},{match.group(2)}"


def _require_direct_capture_success(
    process: Dict[str, Any],
    stage_name: str,
) -> None:
    if bool(process.get("success")):
        return
    detail = process.get("error") or process.get("process_exit_status")
    raise RuntimeError(
        f"direct_{stage_name}_capture_failed:{str(detail)[:120]}"
    )


def _read_s16_wav_pcm(
    path: Path,
    *,
    expected_sample_rate_hz: int,
    expected_channels: int,
    expected_sample_width_bytes: int,
    expected_duration_seconds: Optional[float] = None,
) -> tuple[bytes, Dict[str, Any]]:
    validation = analyze_wav_audio(path)
    if not validation.get("success"):
        raise ValueError(
            str(validation.get("error_message") or "diagnostic_wav_invalid")
        )
    actual_contract = (
        int(validation.get("sample_rate_hz", 0) or 0),
        int(validation.get("channels", 0) or 0),
        int(validation.get("sample_width_bytes", 0) or 0),
    )
    expected_contract = (
        int(expected_sample_rate_hz),
        int(expected_channels),
        int(expected_sample_width_bytes),
    )
    if actual_contract != expected_contract:
        raise ValueError(
            f"diagnostic_wav_format_mismatch:{actual_contract}:{expected_contract}"
        )
    if expected_duration_seconds is not None:
        actual_duration = float(validation.get("duration_seconds", 0.0) or 0.0)
        tolerance = 1.0 / max(1, int(expected_sample_rate_hz))
        if not math.isclose(
            actual_duration,
            float(expected_duration_seconds),
            abs_tol=tolerance,
        ):
            raise ValueError(
                "diagnostic_wav_duration_mismatch:"
                f"{actual_duration}:{expected_duration_seconds}"
            )
    with wave.open(str(path), "rb") as wav_file:
        pcm = wav_file.readframes(wav_file.getnframes())
    return bytes(pcm), validation


def _stage_report(
    pcm: bytes,
    *,
    header: Dict[str, Any],
    sample_rate_hz: int,
    frame_bytes: int,
) -> Dict[str, Any]:
    statistics = analyze_s16_le_pcm_integrity(
        pcm,
        frame_bytes=frame_bytes,
        sample_rate_hz=sample_rate_hz,
        channels=CANONICAL_CHANNELS,
    ).to_dict()
    return {
        **statistics,
        "wav_header": dict(header),
        "complete": True,
    }


def _capture_persistent_phase(
    source: Any,
    *,
    seconds: int,
    destination: list[bytes],
) -> None:
    for _ in range(_frame_count(seconds)):
        source_frame = source.read_frame(CANONICAL_PCM_FRAME_BYTES, 1.0)
        if not isinstance(source_frame, (bytes, bytearray, memoryview)):
            raise TypeError("persistent_stream_returned_non_bytes_pcm")
        actual_length = len(source_frame)
        immutable_frame = bytes(source_frame[:actual_length])
        if actual_length != CANONICAL_PCM_FRAME_BYTES:
            raise RuntimeError(
                "persistent_stream_returned_incomplete_pcm_frame:"
                f"{actual_length}"
            )
        destination.append(immutable_frame)


def _write_canonical_stage_wav(
    path: Path,
    pcm: bytes,
    *,
    source: str,
) -> None:
    write_audio_chunk_wav(
        AudioChunk(
            data=bytes(pcm),
            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            channels=CANONICAL_CHANNELS,
            sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
            source=source,
        ),
        path,
    )


def _failure_stage(report: Dict[str, Any]) -> str:
    if len(report.get("direct_processes", [])) < 2:
        return "direct_native_capture"
    if not report.get("persistent_arecord_command"):
        return "persistent_stream_open"
    return "persistent_stream_capture"


def _persistent_process_report(
    handle: Any,
    *,
    before_close: Dict[str, Any],
    after_close: Dict[str, Any],
) -> Dict[str, Any]:
    source = getattr(handle, "frame_source", None) if handle is not None else None
    process = getattr(source, "process", None) if source is not None else None
    pid = before_close.get("process_pid")
    if pid is None:
        pid = getattr(process, "pid", None)
    before_status = before_close.get("process_exit_status")
    after_status = after_close.get("process_exit_status")
    if after_status is None:
        after_status = getattr(process, "returncode", None)
    before_alive = before_close.get("process_alive")
    after_alive = after_close.get("process_alive")
    return {
        "process_pid": pid,
        "alive_before_close": before_alive,
        "exit_status_before_close": before_status,
        "alive_after_close": after_alive,
        "exit_status_after_close": after_status,
        "metadata_available_before_close": all(
            key in before_close
            for key in (
                "process_pid",
                "process_exit_status",
                "process_alive",
            )
        ),
        "metadata_available_after_close": all(
            key in after_close
            for key in (
                "process_pid",
                "process_exit_status",
                "process_alive",
            )
        ),
    }


def _retain_partial_persistent_stage(
    report: Dict[str, Any],
    *,
    path: Path,
    frames: Sequence[bytes],
    stage_name: str,
) -> None:
    if stage_name in dict(report.get("persistent", {}) or {}) or not frames:
        return
    pcm = concatenate_owned_pcm_frames(frames)
    if not path.exists():
        _write_canonical_stage_wav(
            path,
            pcm,
            source=f"persistent_pcm_integrity_partial_{stage_name}",
        )
    loaded, header = _read_s16_wav_pcm(
        path,
        expected_sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
        expected_channels=CANONICAL_CHANNELS,
        expected_sample_width_bytes=CANONICAL_SAMPLE_WIDTH_BYTES,
    )
    if loaded != pcm:
        raise RuntimeError(f"persistent_partial_{stage_name}_wav_pcm_mismatch")
    stage = _stage_report(
        pcm,
        header=header,
        sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
        frame_bytes=CANONICAL_PCM_FRAME_BYTES,
    )
    stage["complete"] = False
    report["persistent"][stage_name] = stage


def _populate_signal_comparison(report: Dict[str, Any]) -> None:
    direct = dict(report.get("direct", {}) or {})
    persistent = dict(report.get("persistent", {}) or {})
    direct_changed = _signal_changed_from_dict(direct)
    persistent_changed = _signal_changed_from_dict(persistent)
    direct_ratio = _spoken_quiet_rms_ratio(direct)
    persistent_ratio = _spoken_quiet_rms_ratio(persistent)
    direct_spoken = float(
        dict(direct.get("spoken", {}) or {}).get("rms", 0.0) or 0.0
    )
    persistent_spoken = float(
        dict(persistent.get("spoken", {}) or {}).get("rms", 0.0) or 0.0
    )
    spoken_ratio = (
        persistent_spoken / direct_spoken
        if direct_spoken > 0.0
        else None
    )
    contrast_comparability = (
        min(persistent_ratio / direct_ratio, direct_ratio / persistent_ratio)
        if direct_ratio
        and persistent_ratio
        and direct_ratio > 0.0
        and persistent_ratio > 0.0
        else None
    )
    cross_path_comparable = bool(
        direct_changed
        and persistent_changed
        and spoken_ratio is not None
        and spoken_ratio >= MINIMUM_PERSISTENT_TO_DIRECT_SPOKEN_RMS_RATIO
        and contrast_comparability is not None
        and contrast_comparability
        >= MINIMUM_CROSS_PATH_RMS_RATIO_COMPARABILITY
    )
    report["signal_comparison"] = {
        "direct_signal_changed": direct_changed,
        "persistent_signal_changed": persistent_changed,
        "direct_spoken_quiet_rms_ratio": direct_ratio,
        "persistent_spoken_quiet_rms_ratio": persistent_ratio,
        "persistent_to_direct_spoken_rms_ratio": spoken_ratio,
        "cross_path_rms_ratio_comparability": contrast_comparability,
        "minimum_persistent_to_direct_spoken_rms_ratio": (
            MINIMUM_PERSISTENT_TO_DIRECT_SPOKEN_RMS_RATIO
        ),
        "minimum_cross_path_rms_ratio_comparability": (
            MINIMUM_CROSS_PATH_RMS_RATIO_COMPARABILITY
        ),
        "cross_path_comparable": cross_path_comparable,
    }
    report["direct_speech_signal_changed"] = direct_changed
    report["persistent_speech_signal_changed"] = persistent_changed


def _spoken_quiet_rms_ratio(stages: Dict[str, Any]) -> Optional[float]:
    quiet = dict(stages.get("quiet", {}) or {})
    spoken = dict(stages.get("spoken", {}) or {})
    quiet_rms = float(quiet.get("rms", 0.0) or 0.0)
    spoken_rms = float(spoken.get("rms", 0.0) or 0.0)
    if quiet_rms <= 0.0 or spoken_rms < 0.0:
        return None
    return round(spoken_rms / quiet_rms, 6)


def _ask_owner_yes_no(
    input_func: Callable[[str], str],
    prompt: str,
) -> bool:
    response = str(input_func(f"{prompt} ") or "").strip().lower()
    return response in {"y", "yes"}


def _direct_report_integrity(report: Dict[str, Any]) -> bool:
    phase_seconds = int(
        dict(report.get("protocol", {}) or {}).get("quiet_seconds", 0) or 0
    )
    device = str(report.get("direct_native_device", "") or "")
    processes = list(report.get("direct_processes", []) or [])
    stages = dict(report.get("direct", {}) or {})
    if (
        phase_seconds <= 0
        or not re.fullmatch(r"hw:\d+,\d+", device)
        or len(processes) != 2
    ):
        return False
    paths = dict(report.get("files", {}) or {})
    expected_paths = {
        "quiet": paths.get("direct_quiet_wav", ""),
        "spoken": paths.get("direct_spoken_wav", ""),
    }
    for process in processes:
        stage_name = str(process.get("stage", "") or "")
        if (
            stage_name not in expected_paths
            or not bool(process.get("success"))
            or int(process.get("process_pid", 0) or 0) <= 0
            or process.get("process_exit_status") != 0
            or bool(process.get("timed_out", False))
            or not _native_arecord_command(
                process.get("command", []),
                device=device,
                seconds=phase_seconds,
                output_path=expected_paths[stage_name],
            )
        ):
            return False
    return bool(
        _stage_contract_is_valid(
            stages,
            sample_rate_hz=DIRECT_NATIVE_SAMPLE_RATE_HZ,
            duration_seconds=phase_seconds,
            frame_bytes=DIRECT_NATIVE_FRAME_BYTES,
        )
        and _stage_frame_variation_is_valid(stages)
        and _stage_zero_density_is_valid(stages)
    )


def _persistent_report_integrity(report: Dict[str, Any]) -> bool:
    protocol = dict(report.get("protocol", {}) or {})
    phase_seconds = int(protocol.get("quiet_seconds", 0) or 0)
    device = str(report.get("persistent_conversion_device", "") or "")
    resolved = str(report.get("resolved_persistent_device", "") or "")
    stages = dict(report.get("persistent", {}) or {})
    counters = dict(report.get("persistent_stream_counters", {}) or {})
    post_close = dict(report.get("persistent_post_close_counters", {}) or {})
    expected_frames = 2 * _frame_count(phase_seconds)
    expected_bytes = expected_frames * CANONICAL_PCM_FRAME_BYTES
    if not (
        phase_seconds > 0
        and resolved == device
        and _canonical_arecord_command(
            report.get("persistent_arecord_command", []),
            device,
            capture_type="raw",
        )
        and _stage_contract_is_valid(
            stages,
            sample_rate_hz=CANONICAL_SAMPLE_RATE_HZ,
            duration_seconds=phase_seconds,
            frame_bytes=CANONICAL_PCM_FRAME_BYTES,
        )
        and _stage_frame_variation_is_valid(stages)
        and _stage_zero_density_is_valid(stages)
        and int(counters.get("valid_full_pcm_frames", 0) or 0)
        >= expected_frames
        and int(
            counters.get("valid_microphone_bytes_delivered_to_vad", 0) or 0
        )
        == expected_bytes
        and int(
            counters.get("fresh_microphone_bytes_delivered_to_vad", 0) or 0
        )
        == expected_bytes
        and int(counters.get("read_errors", 0) or 0) == 0
        and int(counters.get("unexpected_eof_count", 0) or 0) == 0
        and not bool(counters.get("dead_process_detected", False))
        and int(counters.get("zero_filled_bytes", 0) or 0) == 0
        and int(counters.get("pending_partial_bytes", 0) or 0) == 0
        and int(counters.get("pending_discard_alignment_bytes", 0) or 0) == 0
        and int(counters.get("discarded_bytes", 0) or 0) == 0
        and int(counters.get("mutable_buffer_reuse_detected", 0) or 0) == 0
        and int(counters.get("replayed_frame_count", 0) or 0) == 0
        and not bool(counters.get("pathological_duplicate_frame_detected", False))
        and int(counters.get("queue_overflow_dropped_frames", 0) or 0) == 0
        and int(counters.get("queue_overflow_dropped_bytes", 0) or 0) == 0
        and int(counters.get("candidate_reset_discarded_frames", 0) or 0) == 0
        and int(counters.get("candidate_reset_discarded_bytes", 0) or 0) == 0
    ):
        return False
    if not bool(report.get("production_process_metadata_required", False)):
        return True
    process = dict(report.get("persistent_process", {}) or {})
    required_counter_fields = (
        "process_pid",
        "process_exit_status",
        "process_alive",
        "eof_count",
        "unexpected_eof_count",
        "dead_process_detected",
        "accumulated_partial_bytes",
        "pending_partial_bytes",
        "low_level_read_size_counts",
        "maximum_consecutive_repeated_non_silent_frames",
        "pathological_duplicate_frame_detected",
        "maximum_consecutive_tiny_rms_frames",
        "queue_overflow_dropped_frames",
        "queue_overflow_dropped_bytes",
        "candidate_reset_discarded_frames",
        "candidate_reset_discarded_bytes",
        "transport_argv",
        "stdout_transport_mode",
        "stderr_transport_mode",
        "expected_frame_bytes",
    )
    return bool(
        all(field in counters for field in required_counter_fields)
        and process.get("metadata_available_before_close") is True
        and process.get("metadata_available_after_close") is True
        and int(process.get("process_pid", 0) or 0) > 0
        and process.get("alive_before_close") is True
        and process.get("exit_status_before_close") is None
        and process.get("alive_after_close") is False
        and process.get("exit_status_after_close") is not None
        and list(counters.get("transport_argv", []) or [])
        == list(report.get("persistent_arecord_command", []) or [])
        and str(report.get("persistent_stream_id", "") or "")
        and str(report.get("persistent_alsa_handle_id", "") or "")
        == f"arecord-pid-{int(process.get('process_pid', 0) or 0)}"
        and counters.get("stdout_transport_mode")
        in {"raw_pcm_pipe", "raw_pcm_pipe_continuous_pump"}
        and counters.get("stderr_transport_mode") == "separate_bounded_pipe"
        and int(counters.get("expected_frame_bytes", 0) or 0)
        == CANONICAL_PCM_FRAME_BYTES
        and int(post_close.get("unexpected_eof_count", 0) or 0) == 0
        and not bool(post_close.get("dead_process_detected", False))
    )


def _stage_contract_is_valid(
    stages: Dict[str, Any],
    *,
    sample_rate_hz: int,
    duration_seconds: int,
    frame_bytes: int,
) -> bool:
    for stage_name in ("quiet", "spoken"):
        stage = dict(stages.get(stage_name, {}) or {})
        header = dict(stage.get("wav_header", {}) or {})
        if not (
            stage.get("complete") is True
            and bool(header.get("success"))
            and int(header.get("sample_rate_hz", 0) or 0) == sample_rate_hz
            and int(header.get("channels", 0) or 0) == CANONICAL_CHANNELS
            and int(header.get("sample_width_bytes", 0) or 0)
            == CANONICAL_SAMPLE_WIDTH_BYTES
            and math.isclose(
                float(header.get("duration_seconds", 0.0) or 0.0),
                float(duration_seconds),
                abs_tol=1.0 / sample_rate_hz,
            )
            and math.isclose(
                float(stage.get("duration_seconds", 0.0) or 0.0),
                float(duration_seconds),
                abs_tol=1.0 / sample_rate_hz,
            )
            and int(stage.get("partial_frame_bytes", -1)) == 0
            and int(stage.get("frame_count", 0) or 0)
            == _frame_count(duration_seconds)
            and int(stage.get("byte_count", 0) or 0)
            == int(duration_seconds)
            * sample_rate_hz
            * CANONICAL_CHANNELS
            * CANONICAL_SAMPLE_WIDTH_BYTES
        ):
            return False
        if int(frame_bytes) <= 0:
            return False
    return True


def _native_arecord_command(
    command: Any,
    *,
    device: str,
    seconds: int,
    output_path: Any,
) -> bool:
    values = [str(value) for value in (command or [])]
    expected = {
        "-D": str(device),
        "-f": CANONICAL_PCM_SAMPLE_FORMAT,
        "-r": str(DIRECT_NATIVE_SAMPLE_RATE_HZ),
        "-c": str(CANONICAL_CHANNELS),
        "-d": str(int(seconds)),
        "-t": "wav",
    }
    for flag, expected_value in expected.items():
        try:
            actual = values[values.index(flag) + 1]
        except (ValueError, IndexError):
            return False
        if actual != expected_value:
            return False
    return bool(values and values[-1] == str(output_path))


def _format_ratio(value: Any) -> str:
    if value is None:
        return "not_available"
    return f"{float(value):.3f}"


def _render_report(
    report: Dict[str, Any],
    *,
    output_func: Callable[[str], None],
) -> bool:
    if not report.get("signal_comparison"):
        _populate_signal_comparison(report)
    output_func("")
    output_func("Side-by-side signal statistics")
    for stage_name in ("quiet", "spoken"):
        output_func(f"  Stage: {stage_name}")
        for source_name in ("direct", "persistent"):
            stats = dict(
                dict(report.get(source_name, {}) or {}).get(stage_name, {}) or {}
            )
            output_func(
                f"    {source_name}: bytes={int(stats.get('byte_count', 0) or 0)}; "
                f"samples={int(stats.get('sample_count', 0) or 0)}; "
                f"duration={float(stats.get('duration_seconds', 0.0) or 0.0):.3f}s; "
                f"min={int(stats.get('minimum_signed_sample', 0) or 0)}; "
                f"max={int(stats.get('maximum_signed_sample', 0) or 0)}; "
                f"mean_abs={float(stats.get('mean_absolute_amplitude', 0.0) or 0.0):.3f}; "
                f"rms={float(stats.get('rms', 0.0) or 0.0):.3f}; "
                f"peak={int(stats.get('peak', 0) or 0)}; "
                f"nonzero={int(stats.get('nonzero_sample_count', 0) or 0)}; "
                f"distinct={int(stats.get('distinct_sample_count', 0) or 0)}; "
                f"repeated_frames={int(stats.get('repeated_frame_count', 0) or 0)}"
            )
            output_func(
                "      |sample| above "
                f"100={float(stats.get('absolute_sample_percentage_above_100', 0.0) or 0.0):.3f}%; "
                f"300={float(stats.get('absolute_sample_percentage_above_300', 0.0) or 0.0):.3f}%; "
                f"1000={float(stats.get('absolute_sample_percentage_above_1000', 0.0) or 0.0):.3f}%; "
                f"3000={float(stats.get('absolute_sample_percentage_above_3000', 0.0) or 0.0):.3f}%"
            )
    counters = dict(report.get("persistent_stream_counters", {}) or {})
    _print_counter_snapshot(counters, output_func=output_func)
    direct_integrity = _direct_report_integrity(report)
    persistent_integrity = _persistent_report_integrity(report)
    commands = [
        *[
            process.get("command", [])
            for process in report.get("direct_processes", [])
            if isinstance(process, dict)
        ],
        report.get("persistent_arecord_command", []),
    ]
    shared_process_path = _commands_share_executable(commands)
    structural_success = bool(
        direct_integrity
        and persistent_integrity
        and shared_process_path
        and not report.get("cleanup_failures")
    )
    report["direct_structural_integrity"] = direct_integrity
    report["persistent_structural_integrity"] = persistent_integrity
    report["same_arecord_process_path"] = shared_process_path
    report["structural_integrity"] = structural_success
    output_func("")
    output_func("Diagnostic answers")
    for process in report.get("direct_processes", []):
        output_func(
            f"  Direct {process.get('stage', 'unknown')} arecord command: "
            + " ".join(str(value) for value in process.get("command", []))
        )
    output_func(
        "  Persistent arecord command: "
        + " ".join(
            str(value) for value in report.get("persistent_arecord_command", [])
        )
    )
    comparison = dict(report.get("signal_comparison", {}) or {})
    output_func(
        "  Direct spoken/quiet RMS ratio: "
        f"{_format_ratio(comparison.get('direct_spoken_quiet_rms_ratio'))}"
    )
    output_func(
        "  Persistent spoken/quiet RMS ratio: "
        f"{_format_ratio(comparison.get('persistent_spoken_quiet_rms_ratio'))}"
    )
    output_func(
        "  Persistent/direct spoken RMS ratio: "
        f"{_format_ratio(comparison.get('persistent_to_direct_spoken_rms_ratio'))}"
    )
    output_func(
        "  Cross-path ratio comparability: "
        f"{_format_ratio(comparison.get('cross_path_rms_ratio_comparability'))}; "
        f"pass={_yes_no(bool(comparison.get('cross_path_comparable', False)))}"
    )
    output_func(
        "  Persistent process: "
        f"pid={report.get('persistent_process', {}).get('process_pid')}; "
        f"alive_before_close={report.get('persistent_process', {}).get('alive_before_close')}; "
        f"exit_after_close={report.get('persistent_process', {}).get('exit_status_after_close')}"
    )
    output_func(f"  Direct path integrity: {_yes_no(direct_integrity)}")
    output_func(f"  Persistent path integrity: {_yes_no(persistent_integrity)}")
    output_func(f"  Same arecord process path: {_yes_no(shared_process_path)}")
    output_func(f"  Structural PCM integrity: {_yes_no(structural_success)}")
    for key, value in dict(report.get("files", {}) or {}).items():
        output_func(f"  {key}: {value}")
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
        "accumulated_partial_bytes",
        "empty_reads",
        "eof_count",
        "unexpected_eof_count",
        "read_errors",
        "discarded_bytes",
        "zero_filled_bytes",
        "repeated_frame_hashes",
        "maximum_consecutive_repeated_non_silent_frames",
        "pathological_duplicate_frame_detected",
        "maximum_consecutive_tiny_rms_frames",
        "mutable_buffer_reuse_detected",
        "valid_microphone_bytes_delivered_to_vad",
        "fresh_microphone_bytes_delivered_to_vad",
        "pending_partial_bytes",
        "pending_discard_alignment_bytes",
        "queue_overflow_dropped_frames",
        "queue_overflow_dropped_bytes",
        "candidate_reset_discarded_frames",
        "candidate_reset_discarded_bytes",
    ):
        value = counters.get(key, 0)
        if isinstance(value, bool):
            output_func(f"  {key}: {_yes_no(value)}")
        else:
            output_func(f"  {key}: {int(value or 0)}")
    output_func(
        "  low_level_read_size_counts: "
        f"{dict(counters.get('low_level_read_size_counts', {}) or {})}"
    )


def _stage_statistics(
    pcm: bytes,
    phase_seconds: int,
) -> Dict[str, PcmIntegrityStatistics]:
    one_second_bytes = (
        CANONICAL_SAMPLE_RATE_HZ
        * CANONICAL_CHANNELS
        * CANONICAL_SAMPLE_WIDTH_BYTES
    )
    expected_bytes = 2 * phase_seconds * one_second_bytes
    if len(pcm) != expected_bytes:
        raise ValueError(
            f"diagnostic_pcm_duration_mismatch:{len(pcm)}:{expected_bytes}"
        )
    split_bytes = phase_seconds * one_second_bytes
    quiet = pcm[:split_bytes]
    spoken = pcm[split_bytes:]
    return {
        "quiet": analyze_s16_le_pcm_integrity(quiet),
        "spoken": analyze_s16_le_pcm_integrity(spoken),
        "all": analyze_s16_le_pcm_integrity(pcm),
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


def _signal_changed(stages: Dict[str, PcmIntegrityStatistics]) -> bool:
    quiet = stages.get("quiet")
    spoken = stages.get("spoken")
    if quiet is None or spoken is None:
        return False
    return bool(
        spoken.peak >= MINIMUM_DIAGNOSTIC_SPOKEN_PEAK
        and spoken.rms >= MINIMUM_DIAGNOSTIC_SPOKEN_RMS
        and spoken.rms >= quiet.rms * MINIMUM_DIAGNOSTIC_RMS_RATIO
        and spoken.rms >= quiet.rms + MINIMUM_DIAGNOSTIC_RMS_DELTA
        and spoken.maximum_signed_sample != spoken.minimum_signed_sample
    )


def _signal_changed_from_dict(stages: Dict[str, Dict[str, Any]]) -> bool:
    quiet = dict(stages.get("quiet", {}) or {})
    spoken = dict(stages.get("spoken", {}) or {})
    if not quiet or not spoken:
        return False
    return bool(
        float(spoken.get("peak", 0.0) or 0.0)
        >= MINIMUM_DIAGNOSTIC_SPOKEN_PEAK
        and float(spoken.get("rms", 0.0) or 0.0)
        >= MINIMUM_DIAGNOSTIC_SPOKEN_RMS
        and float(spoken.get("rms", 0.0) or 0.0)
        >= float(quiet.get("rms", 0.0) or 0.0)
        * MINIMUM_DIAGNOSTIC_RMS_RATIO
        and float(spoken.get("rms", 0.0) or 0.0)
        >= float(quiet.get("rms", 0.0) or 0.0)
        + MINIMUM_DIAGNOSTIC_RMS_DELTA
        and int(spoken.get("maximum_signed_sample", 0) or 0)
        != int(spoken.get("minimum_signed_sample", 0) or 0)
    )


def _stage_frame_variation_is_valid(stages: Dict[str, Dict[str, Any]]) -> bool:
    for stage_name in ("quiet", "spoken"):
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
    for stage_name in ("quiet", "spoken"):
        statistics = dict(stages.get(stage_name, {}) or {})
        if int(statistics.get("sample_count", 0) or 0) <= 0:
            return False
        if (
            float(statistics.get("zero_sample_percentage", 100.0) or 0.0)
            >= MAXIMUM_DIAGNOSTIC_ZERO_SAMPLE_PERCENTAGE
        ):
            return False
    return True


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
    if not re.fullmatch(
        r"(?:plug)?hw:\d+,\d+",
        str(args.microphone_device or "").strip(),
    ):
        return (
            "microphone device must be a numeric hw:CARD,DEVICE or "
            "plughw:CARD,DEVICE identifier for the native comparison"
        )
    return ""


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


if __name__ == "__main__":
    raise SystemExit(run_pcm_diagnostic())
