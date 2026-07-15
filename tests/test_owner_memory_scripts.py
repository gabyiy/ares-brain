import importlib
import json
from pathlib import Path
import subprocess

from scripts import manual_verify_owner_memory as manual
from scripts import verify_owner_memory_persistence as persistence


def test_manual_script_import_is_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    importlib.reload(manual)

    assert not list(tmp_path.rglob("owner_profile.json"))


def test_manual_text_command_uses_real_skill_path_and_isolated_profile(tmp_path):
    path = tmp_path / "owner_profile.json"

    result = manual.run_owner_memory_text(
        "remember that my favorite color is blue",
        path,
    )

    assert result["success"] is True
    assert result["parsed_intent"] == "owner_memory"
    assert result["selected_skill"] == "owner_memory"
    assert result["storage_status"] == "created"
    assert result["response"] == "I will remember that your favorite color is blue."
    assert result["file_existed_before"] is False
    assert result["profile_path"] == str(path.resolve())
    assert path.exists()


def test_manual_text_command_routes_observed_whisper_variation_to_owner_memory(tmp_path):
    path = tmp_path / "owner_profile.json"

    result = manual.run_owner_memory_text(
        "remember that modified white color is blue",
        path,
    )

    assert result["success"] is True
    assert result["parsed_intent"] == "owner_memory"
    assert result["selected_skill"] == "owner_memory"
    assert result["storage_status"] == "created"
    assert result["response"] == "I will remember that your favorite color is blue."


def test_manual_script_json_output_is_bounded_and_value_redacted(tmp_path):
    output = []

    code = manual.run_manual_verification(
        [
            "--profile-path",
            str(tmp_path / "owner_profile.json"),
            "--text",
            "remember that my favorite color is blue",
            "--json",
        ],
        output_func=output.append,
    )
    payload = json.loads(output[-1])

    assert code == 0
    assert payload["response"] == "I will remember that your favorite color is blue."
    assert payload["value_redacted"] is True
    assert "plan" not in payload
    assert "execution" not in payload


def test_protected_manual_command_never_emits_supplied_value(tmp_path):
    secret = "PROTECTED_VALUE_456"

    result = manual.run_owner_memory_text(
        f"remember that my password is {secret}",
        tmp_path / "owner_profile.json",
    )

    assert result["success"] is True
    assert result["protected_key_rejected"] is True
    assert result["storage_status"] == "rejected"
    assert secret not in json.dumps(result)


def test_persistence_script_runs_all_steps_in_fresh_processes():
    output = []

    success = persistence.verify_persistence(output_func=output.append, verbose=True)

    assert success is True
    assert any(line.startswith("PASS routing priority") for line in output)
    assert sum(line.startswith("PASS step") for line in output) == 7
    assert output[-1] == "PASS: verification used an isolated temporary profile only."


def test_persistence_script_fails_on_child_process_error():
    def fail_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 7, stdout="", stderr="safe failure")

    output = []
    success = persistence.verify_persistence(
        output_func=output.append,
        runner=fail_runner,
    )

    assert success is False
    assert "child process exited 7" in output[0]


def test_persistence_script_never_targets_real_profile(monkeypatch, tmp_path):
    seen_paths = []
    lifecycle_calls = 0

    def runner(command, **kwargs):
        nonlocal lifecycle_calls
        profile_path = Path(command[command.index("--profile-path") + 1])
        text = command[command.index("--text") + 1]
        seen_paths.append(profile_path)
        if text == persistence.ROUTING_PRIORITY_CHECK[0]:
            expected = persistence.ROUTING_PRIORITY_CHECK
        else:
            expected = persistence.EXPECTED_STEPS[lifecycle_calls]
            lifecycle_calls += 1
        payload = {
            "response": expected[1],
            "storage_status": expected[2],
            "parsed_intent": "owner_memory",
            "selected_skill": "owner_memory",
        }
        if lifecycle_calls == len(persistence.EXPECTED_STEPS):
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_name": "ares.owner_profile",
                        "data": {"owner_id": "primary_owner", "facts": {}},
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    assert persistence.verify_persistence(output_func=lambda _: None, runner=runner) is True
    assert len(seen_paths) == len(persistence.EXPECTED_STEPS) + 1
    assert len({path for path in seen_paths}) == 2
    assert all("ares_owner_memory_" in str(path.parent) for path in seen_paths)
    assert all(path != manual.DEFAULT_OWNER_PROFILE_PATH for path in seen_paths)


def test_isolated_verification_does_not_modify_production_profile(tmp_path):
    production = manual.DEFAULT_OWNER_PROFILE_PATH
    before_exists = production.exists()
    before_bytes = production.read_bytes() if before_exists else b""

    result = manual.run_owner_memory_text(
        "remember that my favorite color is blue",
        tmp_path / "owner_profile.json",
    )

    assert result["success"] is True
    assert production.exists() is before_exists
    if before_exists:
        assert production.read_bytes() == before_bytes
