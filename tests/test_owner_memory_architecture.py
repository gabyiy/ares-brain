import ast
import subprocess
import sys
from pathlib import Path

from core import CoreService, IntentParser
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
AUDIO_ADAPTER_FILES = (
    "core/LinuxAlsaMicrophone.py",
    "core/LinuxAlsaSpeaker.py",
    "core/LinuxPiperTextToSpeech.py",
    "core/LinuxWhisperSpeechToText.py",
)
FORBIDDEN_MEMORY_IMPORT_TOKENS = (
    "alsa",
    "whisper",
    "piper",
    "microphone",
    "speaker",
)


def test_events_first_import_does_not_cycle_through_owner_memory():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from events import EventHistoryStore; "
                "from memory import OwnerMemoryService, OwnerProfileStore; "
                "assert EventHistoryStore and OwnerMemoryService and OwnerProfileStore"
            ),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


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
        assert ".save_memory(" not in source, relative_path
        assert ".save_fact(" not in source, relative_path


def test_production_voice_launcher_does_not_write_owner_json_directly():
    source = (REPO_ROOT / "scripts/run_ares_voice.py").read_text(encoding="utf-8")

    assert "json.dump" not in source
    assert "json.dumps" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert "owner_profile.json" not in source
    assert "OwnerProfileStore" not in source
    assert "PendingOwnerMemoryActionStore" not in source
    assert "pending_owner_memory_action.json" not in source


def test_owner_memory_routing_precedes_tasks_and_tasks_do_not_own_profile_storage():
    rule_names = [rule.name for rule in IntentParser().rules]
    tasks_source = (REPO_ROOT / "skills/builtin/tasks.py").read_text(encoding="utf-8")

    assert rule_names.index("owner_memory") < rule_names.index("task")
    assert "OwnerMemoryService" not in tasks_source
    assert "OwnerProfileStore" not in tasks_source
    assert "owner_profile.json" not in tasks_source


def test_single_turn_pipeline_normalizes_transcript_before_brain_routing():
    tree = ast.parse(
        (REPO_ROOT / "core/SingleTurnVoiceStages.py").read_text(encoding="utf-8")
    )
    run_stages = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_run_stages"
    )
    calls = {
        node.func.attr: node.lineno
        for node in ast.walk(run_stages)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_normalize_transcription", "_brain_stage"}
    }

    assert calls["_normalize_transcription"] < calls["_brain_stage"]


def test_audio_adapters_do_not_import_or_write_owner_memory():
    for relative_path in AUDIO_ADAPTER_FILES:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")
        assert not any(module == "memory" or module.startswith("memory.") for module in imported_modules), relative_path
        assert "owner_profile" not in source.casefold(), relative_path


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


def test_only_one_production_python_default_pending_owner_action_path_exists():
    matches = []
    for folder in ("core", "memory", "skills", "interfaces", "scripts"):
        for path in (REPO_ROOT / folder).rglob("*.py"):
            if '"data" / "runtime" / "pending_owner_memory_action.json"' in path.read_text(encoding="utf-8"):
                matches.append(path.relative_to(REPO_ROOT).as_posix())
    assert matches == ["memory/pending_owner_memory.py"]


def test_tasks_and_voice_boundaries_do_not_own_pending_owner_memory_state():
    paths = [REPO_ROOT / "skills" / "builtin" / "tasks.py"]
    paths.extend(REPO_ROOT / relative for relative in VOICE_BOUNDARY_FILES)
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "PendingOwnerMemoryActionStore" not in source, path.name
        assert "pending_owner_memory_action.json" not in source, path.name
        assert "forget_memories_by_ids" not in source, path.name


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


def test_general_memory_text_and_voice_transports_share_structured_central_operation(tmp_path):
    path = tmp_path / "owner_profile.json"
    text_service = OwnerMemoryService(path, event_bus=EventBus())
    voice_service = OwnerMemoryService(path, event_bus=EventBus())
    text_manager = single_turn.create_skill_manager(
        CoreService(owner_memory_service=text_service),
        event_bus=EventBus(),
    )
    voice_manager = single_turn.create_skill_manager(
        CoreService(owner_memory_service=voice_service),
        event_bus=EventBus(),
    )

    text_response = text_manager.handle(
        "Remember in your long-term memory that I like going to the gym.",
        run_before_intents=True,
    )
    voice_response = single_turn.build_existing_brain_handler(voice_manager)(
        "What do you remember about the gym?"
    )

    text_diagnostics = text_response.metadata["owner_memory_diagnostics"]
    voice_diagnostics = voice_response.metadata["owner_memory_diagnostics"]
    assert text_response.skill == voice_response.skill == "owner_memory"
    assert text_diagnostics["memory_kind"] == voice_diagnostics["memory_kind"] == "general"
    assert text_diagnostics["persistence"] == voice_diagnostics["persistence"] == "long_term"
    assert text_response.text == "I will remember that you like going to the gym."
    assert voice_response.text == "You told me that you like going to the gym."


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
                if path.name in {
                    "manual_verify_general_owner_memory.py",
                    "manual_verify_general_long_term_memory.py",
                    "manual_verify_owner_memory_management.py",
                }:
                    continue
                source = path.read_text(encoding="utf-8", errors="ignore")
                declared.update(name for name in forbidden if name in source)
    assert declared == set()
