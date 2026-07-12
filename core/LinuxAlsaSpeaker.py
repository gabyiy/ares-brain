from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from core.LinuxAlsaMicrophone import SafeProcessResult, SafeSubprocessRunner
from core.LinuxWhisperSpeechToText import analyze_wav_audio


ALSA_SPEAKER_STATUS_APLAY_MISSING = "aplay_missing"
ALSA_SPEAKER_STATUS_INVALID_DEVICE = "invalid_device"
ALSA_SPEAKER_STATUS_DEVICE_LIST_TIMEOUT = "playback_device_list_timeout"
ALSA_SPEAKER_STATUS_DEVICE_LIST_FAILED = "playback_device_list_failed"
ALSA_SPEAKER_STATUS_NO_PLAYBACK_DEVICE = "no_playback_device"
ALSA_SPEAKER_STATUS_INVALID_WAV = "invalid_wav"
ALSA_SPEAKER_STATUS_PLAYBACK_TIMEOUT = "playback_timeout"
ALSA_SPEAKER_STATUS_PLAYBACK_FAILED = "playback_failed"
ALSA_SPEAKER_STATUS_PLAYED = "played"
ALSA_SPEAKER_STATUS_DISABLED = "playback_disabled"


@dataclass(frozen=True)
class SpeakerPlaybackResult:
    success: bool
    status: str
    text: str = ""
    wav_path: str = ""
    device: str = ""
    duration_seconds: float = 0.0
    error_message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "text": self.text,
            "wav_path": self.wav_path,
            "device": self.device,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "data": dict(self.data),
            "metadata": dict(self.metadata),
        }


class SpeakerOutputAdapter:
    """Interface for local speaker playback adapters."""

    def start(self) -> SpeakerPlaybackResult:
        raise NotImplementedError

    def stop(self) -> SpeakerPlaybackResult:
        raise NotImplementedError

    def play_wav(
        self,
        wav_path: str | Path,
        device: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> SpeakerPlaybackResult:
        raise NotImplementedError

    def health_check(self) -> SpeakerPlaybackResult:
        raise NotImplementedError

    def get_status(self) -> SpeakerPlaybackResult:
        raise NotImplementedError

    def get_capabilities(self) -> SpeakerPlaybackResult:
        raise NotImplementedError


class LinuxAlsaSpeakerAdapter(SpeakerOutputAdapter):
    """Linux ALSA speaker adapter backed by aplay.

    The adapter performs explicit one-shot playback only. It never monitors a
    microphone, starts a background listener, or plays audio unless play_wav is
    called.
    """

    def __init__(
        self,
        device: Optional[str] = None,
        aplay_command: str = "aplay",
        timeout_seconds: float = 120.0,
        runner: Optional[SafeSubprocessRunner] = None,
        source: str = "linux_alsa_speaker_adapter",
    ):
        self.device = _normalize_optional_device(device)
        self.aplay_command = str(aplay_command or "aplay").strip()
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.runner = runner or SafeSubprocessRunner()
        self.source = source
        self.started = False
        self.playing = False
        self.play_count = 0
        self.audio_hardware_accessed = False

    def start(self) -> SpeakerPlaybackResult:
        health = self.health_check()
        if not health.success:
            return health
        self.started = True
        return self._success(
            "started",
            "Linux ALSA speaker adapter is ready for explicit playback.",
            data={"health": health.to_dict()},
        )

    def stop(self) -> SpeakerPlaybackResult:
        self.started = False
        self.playing = False
        return self._success(
            "stopped",
            "Linux ALSA speaker adapter stopped. No playback process is running.",
        )

    def health_check(self) -> SpeakerPlaybackResult:
        if self.playing:
            return self._failure(
                ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
                "Linux ALSA speaker already has an active playback operation.",
                "speaker_playback_already_active",
                device=self.device or "",
            )
        aplay_path = self._find_aplay()
        if not aplay_path:
            return self._failure(
                ALSA_SPEAKER_STATUS_APLAY_MISSING,
                "Linux ALSA speaker adapter could not find aplay.",
                "aplay_missing",
            )
        if self.device and not _safe_alsa_device(self.device):
            return self._failure(
                ALSA_SPEAKER_STATUS_INVALID_DEVICE,
                "Selected ALSA playback device is unsafe.",
                "invalid_playback_device",
                device=self.device,
            )
        device_data: Dict[str, Any] = {
            "device_available": None,
            "devices": [],
        }
        if self.device:
            listed = self._list_playback_devices(aplay_path)
            if not listed.success:
                return listed
            devices = listed.data.get("devices", [])
            if not _speaker_device_available(self.device, devices):
                return self._failure(
                    ALSA_SPEAKER_STATUS_INVALID_DEVICE,
                    "Selected ALSA playback device is not available.",
                    "alsa_playback_device_not_found",
                    device=self.device,
                    data={
                        "selected_device": self.device,
                        "available_devices": _available_device_names(devices),
                        "devices": devices,
                        "process": listed.data.get("process", {}),
                    },
                )
            device_data = {
                "device_available": True,
                "devices": devices,
                "process": listed.data.get("process", {}),
            }
        return self._success(
            "healthy",
            "Linux ALSA speaker health check passed.",
            data={
                "aplay_available": True,
                "aplay_path": aplay_path,
                "selected_device": self.device or "",
                **device_data,
            },
        )

    def get_status(self) -> SpeakerPlaybackResult:
        aplay_path = self._find_aplay()
        return self._success(
            "started" if self.started else "stopped",
            "Linux ALSA speaker adapter status discovered.",
            data={
                "source": self.source,
                "started": self.started,
                "playing": self.playing,
                "aplay_available": bool(aplay_path),
                "aplay_path": aplay_path or "",
                "selected_device": self.device or "",
                "play_count": self.play_count,
                "speaker": "explicit_playback_only",
                "microphone_monitoring": "not_managed_here",
            },
        )

    def get_capabilities(self) -> SpeakerPlaybackResult:
        return self._success(
            "capabilities",
            "Linux ALSA speaker adapter capabilities discovered.",
            data={
                "supported_modes": ["aplay_wav_playback"],
                "supports_device_selection": True,
                "playback_default": "disabled_until_explicit_request",
                "background_playback": "disabled",
                "microphone_monitoring": "not_enabled",
            },
        )

    def play_wav(
        self,
        wav_path: str | Path,
        device: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> SpeakerPlaybackResult:
        self.play_count += 1
        path = Path(wav_path).expanduser()
        selected_device = _normalize_optional_device(device) or self.device
        aplay_path = self._find_aplay()
        if not aplay_path:
            return self._failure(
                ALSA_SPEAKER_STATUS_APLAY_MISSING,
                "Linux ALSA speaker adapter could not find aplay.",
                "aplay_missing",
                wav_path=str(path),
                device=selected_device or "",
            )
        if selected_device and not _safe_alsa_device(selected_device):
            return self._failure(
                ALSA_SPEAKER_STATUS_INVALID_DEVICE,
                "Selected ALSA playback device is unsafe.",
                "invalid_playback_device",
                wav_path=str(path),
                device=selected_device,
            )
        if self.playing:
            return self._failure(
                ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
                "A WAV playback operation is already active.",
                "speaker_playback_already_active",
                wav_path=str(path),
                device=selected_device or "",
            )

        wav = analyze_wav_audio(path)
        if not wav.get("success") or int(wav.get("byte_count", 0)) <= 0:
            return self._failure(
                ALSA_SPEAKER_STATUS_INVALID_WAV,
                "Playback WAV is missing, empty, or invalid.",
                str(wav.get("error_message", "invalid_wav")),
                wav_path=str(path),
                device=selected_device or "",
                data={"wav": wav},
            )

        command = [aplay_path]
        if selected_device:
            command.extend(["-D", selected_device])
        command.append(str(path))
        self.playing = True
        try:
            result = self.runner.run(
                command,
                timeout_seconds=_positive_timeout(timeout_seconds or self.timeout_seconds),
            )
        finally:
            self.playing = False
        self.audio_hardware_accessed = True
        if result.timed_out:
            return self._failure(
                ALSA_SPEAKER_STATUS_PLAYBACK_TIMEOUT,
                "Timed out while playing WAV.",
                "aplay_timeout",
                wav_path=str(path),
                device=selected_device or "",
                duration_seconds=float(wav.get("duration_seconds", 0.0)),
                data={"wav": wav, "process": _safe_process_data(result)},
            )
        if result.returncode != 0:
            return self._failure(
                ALSA_SPEAKER_STATUS_PLAYBACK_FAILED,
                "aplay failed while playing WAV.",
                f"aplay_exit_{result.returncode}",
                wav_path=str(path),
                device=selected_device or "",
                duration_seconds=float(wav.get("duration_seconds", 0.0)),
                data={"wav": wav, "process": _safe_process_data(result)},
            )
        return self._success(
            ALSA_SPEAKER_STATUS_PLAYED,
            "WAV playback completed.",
            wav_path=str(path),
            device=selected_device or "",
            duration_seconds=float(wav.get("duration_seconds", 0.0)),
            data={"wav": wav, "process": _safe_process_data(result)},
        )

    def _find_aplay(self) -> str:
        found = self.runner.which(self.aplay_command)
        if found:
            return str(found)
        path = Path(self.aplay_command).expanduser()
        try:
            executable = os.access(path, os.R_OK)
            if os.name == "posix":
                executable = executable and os.access(path, os.X_OK)
            if (
                path.exists()
                and path.is_file()
                and path.stat().st_size > 0
                and executable
            ):
                return str(path)
        except OSError:
            return ""
        return ""

    def _list_playback_devices(self, aplay_path: str) -> SpeakerPlaybackResult:
        result = self.runner.run(
            [aplay_path, "-l"],
            timeout_seconds=min(self.timeout_seconds, 10.0),
        )
        process = _safe_process_data(result)
        if result.timed_out:
            return self._failure(
                ALSA_SPEAKER_STATUS_DEVICE_LIST_TIMEOUT,
                "Timed out while listing ALSA playback devices.",
                "aplay_device_list_timeout",
                device=self.device or "",
                data={"process": process},
            )
        if result.returncode != 0:
            return self._failure(
                ALSA_SPEAKER_STATUS_DEVICE_LIST_FAILED,
                "aplay failed while listing playback devices.",
                f"aplay_exit_{result.returncode}",
                device=self.device or "",
                data={"process": process},
            )
        devices = parse_aplay_playback_devices(result.stdout)
        if not devices:
            return self._failure(
                ALSA_SPEAKER_STATUS_NO_PLAYBACK_DEVICE,
                "aplay is available, but no ALSA playback devices were found.",
                "no_playback_device",
                device=self.device or "",
                data={"devices": [], "process": process},
            )
        return self._success(
            "devices",
            f"Detected {len(devices)} ALSA playback device(s).",
            device=self.device or "",
            data={"devices": devices, "process": process},
        )

    def _success(
        self,
        status: str,
        text: str,
        wav_path: str = "",
        device: str = "",
        duration_seconds: float = 0.0,
        data: Optional[Dict[str, Any]] = None,
    ) -> SpeakerPlaybackResult:
        return SpeakerPlaybackResult(
            success=True,
            status=status,
            text=text,
            wav_path=wav_path,
            device=device,
            duration_seconds=duration_seconds,
            data={"source": self.source, **dict(data or {})},
            metadata=self._metadata(),
        )

    def _failure(
        self,
        status: str,
        text: str,
        error_message: str,
        wav_path: str = "",
        device: str = "",
        duration_seconds: float = 0.0,
        data: Optional[Dict[str, Any]] = None,
    ) -> SpeakerPlaybackResult:
        return SpeakerPlaybackResult(
            success=False,
            status=status,
            text=text,
            wav_path=wav_path,
            device=device,
            duration_seconds=duration_seconds,
            error_message=error_message,
            data={"source": self.source, **dict(data or {})},
            metadata=self._metadata(),
        )

    def _metadata(self) -> Dict[str, Any]:
        return {
            "safe": True,
            "source": self.source,
            "audio_hardware_accessed": self.audio_hardware_accessed,
            "subprocess_shell": False,
            "microphone_monitoring": "not_enabled",
        }


def _normalize_optional_device(device: Optional[str]) -> str:
    clean = str(device or "").strip()
    return clean


def _safe_alsa_device(device: str) -> bool:
    return bool(device) and bool(re.match(r"^[A-Za-z0-9_.,:=+-]+$", device))


def parse_aplay_playback_devices(output: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"^card\s+(?P<card_index>\d+):\s*"
        r"(?P<card_id>[^\[]+)\[(?P<card_name>[^\]]+)\],\s*"
        r"device\s+(?P<device_index>\d+):\s*"
        r"(?P<device_id>[^\[]+)\[(?P<device_name>[^\]]+)\]"
    )
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = pattern.match(line)
        if not match:
            continue
        card_index = int(match.group("card_index"))
        device_index = int(match.group("device_index"))
        card_id = match.group("card_id").strip()
        aliases = [
            f"hw:{card_index},{device_index}",
            f"plughw:{card_index},{device_index}",
            f"hw:CARD={card_id},DEV={device_index}",
            f"plughw:CARD={card_id},DEV={device_index}",
        ]
        devices.append(
            {
                "card_index": card_index,
                "card_id": card_id,
                "card_name": match.group("card_name").strip(),
                "device_index": device_index,
                "device_id": match.group("device_id").strip(),
                "device_name": match.group("device_name").strip(),
                "alsa_devices": aliases,
                "raw_line": line,
            }
        )
    return devices


def _speaker_device_available(device: str, devices: List[Dict[str, Any]]) -> bool:
    selected = str(device or "").strip().lower()
    return any(
        selected == str(alias).lower()
        for item in devices
        for alias in item.get("alsa_devices", [])
    )


def _available_device_names(devices: List[Dict[str, Any]]) -> List[str]:
    return sorted(
        {
            str(alias)
            for item in devices
            for alias in item.get("alsa_devices", [])
        }
    )


def _positive_timeout(value: float) -> float:
    parsed = float(value)
    if parsed <= 0 or parsed > 900:
        raise ValueError("timeout_seconds must be > 0 and <= 900")
    return parsed


def _safe_process_data(result: SafeProcessResult) -> Dict[str, Any]:
    return {
        "args": list(result.args),
        "command": " ".join(str(arg) for arg in result.args),
        "returncode": result.returncode,
        "stdout": str(result.stdout or ""),
        "stderr": str(result.stderr or ""),
        "stdout_preview": str(result.stdout or "")[:4000],
        "stderr_preview": str(result.stderr or "")[:4000],
        "timed_out": result.timed_out,
        "error_message": result.error_message,
    }
