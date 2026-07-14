import ast
from pathlib import Path

from core import CoreService
from events import EventBus
from memory import OwnerMemoryService
from scripts import manual_verify_single_turn_voice as single_turn


REPO_ROOT = Path(__file__).resolve().parent.parent
VOICE_BOUNDARY_FILES = (
    "scripts/run_ares_voice.py",
    "scripts/manual_verify_single_turn_voice.py",
    "scripts/manual_verify_multi_turn_voice.py",
    "scripts/manual_voice_text_loop.py",
    "core/SingleTurnVoicePipeline.py",
    "core/MultiTurnVoiceSession.py",
    "core/VoicePipeline.py",
)
FORBIDDEN_MEMORY_IMPORT_TOKENS = (
    "alsa",
    "whisper",
    "piper",
    "microphone",
    "speaker",
)


def test_voice_boundaries_do_not_construct_or_import_concrete_owner_store():
    for relative_path in VOICE_BOUNDARY_FILES:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "OwnerProfileStore" not in source, relative_path
        assert "owner_profile.json" not in source, relative_path
        assert "memory.owner_profile" not in imported_names, relative_path


def test_memory_package_has_no_audio_or_voice_hardware_imports():
    for path in (REPO_ROOT / "memory").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        lowered = " ".join(modules).casefold()
        assert not any(token in lowered for token in FORBIDDEN_MEMORY_IMPORT_TOKENS), path.name


def test_only_one_production_python_default_owner_profile_path_exists():
    matches = []
    for folder in ("core", "memory", "skills", "interfaces", "scripts"):
        for path in (REPO_ROOT / folder).rglob("*.py"):
            if '"data" / "memory" / "owner_profile.json"' in path.read_text(encoding="utf-8"):
                matches.append(path.relative_to(REPO_ROOT).as_posix())
    assert matches == ["memory/owner_profile.py"]


def test_voice_manager_uses_core_service_owned_memory(tmp_path):
    service = OwnerMemoryService(tmp_path / "owner_profile.json", event_bus=EventBus())
    core = CoreService(owner_memory_service=service)

    manager = single_turn.create_skill_manager(core, event_bus=EventBus())
    response = manager.handle("remember that my birthday is June 8", run_before_intents=True)

    assert manager.owner_memory_service is service
    assert response.skill == "owner_memory"
    assert service.profile_path.exists()


def test_voice_and_text_transports_produce_same_central_memory_result(tmp_path):
    path = tmp_path / "owner_profile.json"
    text_core = CoreService(owner_memory_service=OwnerMemoryService(path, event_bus=EventBus()))
    voice_core = CoreService(owner_memory_service=OwnerMemoryService(path, event_bus=EventBus()))
    text_manager = single_turn.create_skill_manager(text_core, event_bus=EventBus())
    voice_manager = single_turn.create_skill_manager(voice_core, event_bus=EventBus())

    text_response = text_manager.handle("remember that my birthday is June 8", run_before_intents=True)
    voice_response = single_turn.build_existing_brain_handler(voice_manager)("when is my birthday")

    assert text_response.text == "I will remember that your birthday is June 8."
    assert voice_response.text == "Your birthday is June 8."
    assert voice_response.skill == "owner_memory"


def test_no_voice_specific_owner_memory_files_are_defined():
    forbidden = {
        "voice_owner_profile.json",
        "speech_memory.json",
        "microphone_memory.json",
        "voice_facts.json",
    }
    declared = set()
    for folder in ("core", "memory", "skills", "interfaces", "scripts", "config"):
        for path in (REPO_ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json"}:
                if path.name == "manual_verify_general_owner_memory.py":
                    continue
                source = path.read_text(encoding="utf-8", errors="ignore")
                declared.update(name for name in forbidden if name in source)
    assert declared == set()
