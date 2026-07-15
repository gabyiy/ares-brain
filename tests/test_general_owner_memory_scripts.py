import json
from pathlib import Path
import subprocess
import sys

from events import EventBus
from memory import OwnerMemoryService
from scripts import inspect_owner_memory
from scripts import manual_verify_general_owner_memory
from scripts import manual_verify_general_long_term_memory
from scripts import manual_verify_owner_memory_management


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_inspection_script_reports_missing_profile_without_creating_it(tmp_path):
    path = tmp_path / "missing.json"
    output = []

    code = inspect_owner_memory.run_inspection(["--profile", str(path)], output.append)

    assert code == 0
    assert any("Saved facts: none" in line for line in output)
    assert not path.exists()


def test_inspection_script_lists_valid_facts_and_supports_json(tmp_path):
    path = tmp_path / "owner_profile.json"
    service = OwnerMemoryService(path, event_bus=EventBus())
    service._store.save_fact("birthday", "June 8")
    output = []

    code = inspect_owner_memory.run_inspection(
        ["--profile", str(path), "--key", "date of birth", "--json"],
        output.append,
    )
    payload = json.loads("\n".join(output))

    assert code == 0
    assert payload["schema_version"] == 3
    assert payload["facts"][0]["normalized_key"] == "birthday"
    assert payload["facts"][0]["value"] == "June 8"


def test_inspection_script_fails_closed_for_corrupt_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    path.write_text("{corrupt", encoding="utf-8")
    output = []

    code = inspect_owner_memory.run_inspection(["--profile", str(path)], output.append)

    assert code == 2
    assert path.read_text(encoding="utf-8") == "{corrupt"


def test_inspection_script_filters_general_memories_without_modifying_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    service = OwnerMemoryService(path, event_bus=EventBus())
    from core.OwnerLongTermMemory import classify_general_memory

    service._store.save_memory(classify_general_memory("I like going to the gym"))
    service._store.save_memory(classify_general_memory("I normally work long shifts"))
    before = path.read_bytes()
    output = []

    code = inspect_owner_memory.run_inspection(
        ["--profile", str(path), "--memories", "--topic", "gym", "--type", "preference"],
        output.append,
    )

    assert code == 0
    assert any("The owner likes going to the gym." in line for line in output)
    assert not any("works long shifts" in line for line in output)
    assert path.read_bytes() == before


def test_inspection_topic_gaming_matches_video_games_without_modifying_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    service = OwnerMemoryService(path, event_bus=EventBus())
    from core.OwnerLongTermMemory import classify_general_memory

    service._store.save_memory(classify_general_memory("I like video games"))
    before = path.read_bytes()
    output = []

    code = inspect_owner_memory.run_inspection(
        ["--profile", str(path), "--memories", "--topic", "gaming"],
        output.append,
    )

    assert code == 0
    assert any("The owner likes video games." in line for line in output)
    assert path.read_bytes() == before


def test_inspection_json_reports_v3_general_memory_metadata(tmp_path):
    path = tmp_path / "owner_profile.json"
    service = OwnerMemoryService(path, event_bus=EventBus())
    from core.OwnerLongTermMemory import classify_general_memory

    service._store.save_memory(classify_general_memory("I prefer wireless mice"))
    output = []

    code = inspect_owner_memory.run_inspection(
        ["--profile", str(path), "--memories", "--json"],
        output.append,
    )
    payload = json.loads("\n".join(output))

    assert code == 0
    assert payload["schema_version"] == 3
    assert payload["active_memory_count"] == 1
    assert payload["memories"][0]["memory_type"] == "preference"
    assert payload["memories"][0]["memory_id"].startswith("mem-")
    assert payload["memories"][0]["topics"]


def test_general_manual_verifier_uses_fresh_processes_and_isolated_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "manual_verify_general_owner_memory.py"),
            "--profile",
            str(path),
            "--reset",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "central Brain path across fresh processes" in completed.stdout
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert sorted(payload["data"]["facts"]) == ["birthday", "favorite_game"]


def test_manual_verifier_refuses_to_reset_canonical_profile(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical.json"
    monkeypatch.setattr(manual_verify_general_owner_memory, "DEFAULT_OWNER_PROFILE_PATH", canonical)
    output = []

    code = manual_verify_general_owner_memory._run_sequence(
        canonical,
        True,
        False,
        output.append,
        subprocess.run,
    )

    assert code == 2
    assert any("refuses" in line for line in output)


def test_general_long_term_manual_verifier_uses_fresh_central_brain_processes(tmp_path):
    path = tmp_path / "owner_profile.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "manual_verify_general_long_term_memory.py"),
            "--profile",
            str(path),
            "--reset",
            "--verbose",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "general long-term memories persisted through the central Brain path" in completed.stdout
    assert "Initial active_memory_count: 0" in completed.stdout
    assert "Final active_memory_count: 2" in completed.stdout
    assert "locked-term normalization" in completed.stdout
    assert "no voice-specific memory file was created" in completed.stdout
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["data"]["facts"] == {}
    active = [memory for memory in payload["data"]["memories"] if memory["status"] == "active"]
    assert len(active) == 2
    assert {memory["object"] for memory in active} == {"going to the gym", "video games"}


def test_general_long_term_verifier_refuses_to_reset_canonical_profile(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical.json"
    monkeypatch.setattr(manual_verify_general_long_term_memory, "DEFAULT_OWNER_PROFILE_PATH", canonical)
    output = []

    code = manual_verify_general_long_term_memory._run_sequence(
        canonical,
        True,
        False,
        output.append,
        subprocess.run,
    )

    assert code == 2
    assert any("refuses" in line for line in output)


def test_inspection_script_reports_counts_facts_memories_and_pending_state(tmp_path):
    profile = tmp_path / "owner_profile.json"
    pending = tmp_path / "pending_owner_memory_action.json"
    from scripts.manual_verify_owner_memory import run_owner_memory_text

    run_owner_memory_text("Remember that my favorite color is red.", profile, pending)
    run_owner_memory_text("Remember that I like going to the gym.", profile, pending)
    run_owner_memory_text("Forget that I like going to the gym.", profile, pending)
    output = []

    code = inspect_owner_memory.run_inspection(
        [
            "--profile",
            str(profile),
            "--pending-state",
            str(pending),
            "--summary",
            "--facts",
            "--memories",
            "--pending",
        ],
        output.append,
    )

    assert code == 0
    assert "Keyed fact count: 1" in output
    assert "Active general memory count: 1" in output
    assert any("favorite color: red" in line for line in output)
    assert any("The owner likes going to the gym" in line for line in output)
    assert "Pending state: active" in output
    assert any("forget_specific / general_memory / 1 candidate" in line for line in output)


def test_inspection_script_json_includes_versioned_pending_state_without_modification(tmp_path):
    profile = tmp_path / "owner_profile.json"
    pending = tmp_path / "pending.json"
    from scripts.manual_verify_owner_memory import run_owner_memory_text

    run_owner_memory_text("Remember that I like tea.", profile, pending)
    run_owner_memory_text("Forget that I like tea.", profile, pending)
    profile_before = profile.read_bytes()
    pending_before = pending.read_bytes()
    output = []

    code = inspect_owner_memory.run_inspection(
        ["--profile", str(profile), "--pending-state", str(pending), "--json"],
        output.append,
    )
    payload = json.loads("\n".join(output))

    assert code == 0
    assert payload["pending_action_status"] == "active"
    assert payload["pending_action"]["operation"] == "forget_specific"
    assert profile.read_bytes() == profile_before
    assert pending.read_bytes() == pending_before


def test_owner_memory_management_manual_verifier_uses_isolated_fresh_processes(tmp_path):
    profile = tmp_path / "owner_profile.json"
    pending = tmp_path / "pending.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "manual_verify_owner_memory_management.py"),
            "--profile",
            str(profile),
            "--pending-state",
            str(pending),
            "--reset",
            "--verbose",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "cross-process confirmation" in completed.stdout
    assert "keyed/general separation" in completed.stdout
    assert json.loads(profile.read_text(encoding="utf-8"))["schema_version"] == 3


def test_management_verifier_refuses_to_reset_canonical_state(monkeypatch, tmp_path):
    profile = tmp_path / "canonical_profile.json"
    pending = tmp_path / "canonical_pending.json"
    monkeypatch.setattr(manual_verify_owner_memory_management, "DEFAULT_OWNER_PROFILE_PATH", profile)
    monkeypatch.setattr(
        manual_verify_owner_memory_management,
        "DEFAULT_PENDING_OWNER_MEMORY_ACTION_PATH",
        pending,
    )
    output = []

    code = manual_verify_owner_memory_management._run_sequence(
        profile,
        pending,
        True,
        False,
        output.append,
        subprocess.run,
    )

    assert code == 2
    assert any("refuses" in line for line in output)
