import json
from pathlib import Path
import subprocess
import sys

from events import EventBus
from memory import OwnerMemoryService
from scripts import inspect_owner_memory
from scripts import manual_verify_general_owner_memory


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
    assert payload["schema_version"] == 2
    assert payload["facts"][0]["normalized_key"] == "birthday"
    assert payload["facts"][0]["value"] == "June 8"


def test_inspection_script_fails_closed_for_corrupt_profile(tmp_path):
    path = tmp_path / "owner_profile.json"
    path.write_text("{corrupt", encoding="utf-8")
    output = []

    code = inspect_owner_memory.run_inspection(["--profile", str(path)], output.append)

    assert code == 2
    assert path.read_text(encoding="utf-8") == "{corrupt"


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
    assert payload["schema_version"] == 2
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
