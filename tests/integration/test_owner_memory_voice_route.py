import json

from core import (
    CoreService,
    MockMicrophoneAdapter,
    MockSpeechToTextAdapter,
    MockTextToSpeechAdapter,
    SpeakerPlaybackResult,
)
from events import EventBus, EventHistoryStore
from memory import (
    GoalsStore,
    MemoryStore,
    NotesStore,
    OwnerMemoryService,
    TasksStore,
    UserProfileStore,
)
from scripts import manual_verify_single_turn_voice as manual
from scripts import run_ares_voice


class CapturingTextToSpeech(MockTextToSpeechAdapter):
    def __init__(self):
        super().__init__()
        self.requests = []

    def synthesize(self, request):
        self.requests.append(request)
        return super().synthesize(request)


class NoAudioSpeaker:
    playing = False

    def __init__(self):
        self.play_count = 0
        self.played_paths = []

    def health_check(self):
        return SpeakerPlaybackResult(True, "healthy", "Mock speaker is healthy.")

    def start(self):
        return SpeakerPlaybackResult(True, "started", "Mock speaker started.")

    def stop(self):
        return SpeakerPlaybackResult(True, "stopped", "Mock speaker stopped.")

    def cancel_current(self):
        return None

    def play_wav(self, wav_path, *args, **kwargs):
        self.play_count += 1
        self.played_paths.append(str(wav_path))
        return SpeakerPlaybackResult(True, "played", "Mock playback completed.")


def _run_voice_turn(tmp_path, profile_path, text, *, playback=True, turn=1):
    event_bus = EventBus(max_history=500)
    core_service = CoreService(
        owner_memory_service=OwnerMemoryService(profile_path, event_bus=event_bus)
    )
    manager = manual.create_skill_manager(
        core_service,
        event_history_store=EventHistoryStore(tmp_path / f"manager_events_{turn}.json"),
        event_bus=event_bus,
        memory_store=MemoryStore(
            short_path=tmp_path / "short_memory.json",
            long_path=tmp_path / "long_memory.json",
            event_bus=event_bus,
        ),
        profile_store=UserProfileStore(tmp_path / "profile.json", event_bus=event_bus),
        goals_store=GoalsStore(tmp_path / "goals.json", event_bus=event_bus),
        notes_store=NotesStore(tmp_path / "notes.json", event_bus=event_bus),
        tasks_store=TasksStore(tmp_path / "tasks.json", event_bus=event_bus),
    )
    tts = CapturingTextToSpeech()
    speaker = NoAudioSpeaker()
    arguments = [
        "--text-input",
        text,
        "--recording-output",
        str(tmp_path / f"owner_memory_turn_{turn}.wav"),
    ]
    if playback:
        arguments.append("--playback")
    args = manual.build_parser().parse_args(arguments)
    history = EventHistoryStore(tmp_path / f"pipeline_events_{turn}.json")
    pipeline = manual.create_pipeline(
        args,
        output_func=lambda _: None,
        skill_manager=manager,
        event_history_store=history,
        microphone_adapter=MockMicrophoneAdapter(),
        speech_to_text_adapter=MockSpeechToTextAdapter(),
        text_to_speech_adapter=tts,
        speaker_adapter=speaker,
    )
    result = pipeline.run_once(manual.request_from_args(args))
    return result, manager, event_bus, history, tts, speaker, pipeline


def test_all_required_owner_memory_interactions_use_production_voice_route(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    interactions = (
        (
            "Remember that my favorite color is blue.",
            "I will remember that your favorite color is blue.",
            "created",
        ),
        (
            "What is my favorite color?",
            "Your favorite color is blue.",
            "recalled",
        ),
        (
            "Remember that my favorite color is red.",
            "I updated your favorite color to red.",
            "updated",
        ),
        (
            "What is my favorite color?",
            "Your favorite color is red.",
            "recalled",
        ),
        (
            "Forget my favorite color.",
            "I forgot your favorite color.",
            "forgotten",
        ),
        (
            "What is my favorite color?",
            "I do not know your favorite color yet.",
            "missing",
        ),
    )

    for turn, (text, expected, status) in enumerate(interactions, start=1):
        result, manager, _, _, tts, speaker, pipeline = _run_voice_turn(
            tmp_path,
            profile_path,
            text,
            playback=True,
            turn=turn,
        )

        assert result.success is True
        assert result.status == "completed"
        assert result.detected_intent == "owner_memory"
        assert result.routed_skill == "owner_memory"
        assert result.planner_decision == "1 step(s): owner_memory"
        assert result.execution_result == "success"
        assert result.brain_text_response == expected
        assert manager.last_plan.steps[0].target == "owner_memory"
        assert manager.last_execution.step_results[0].returned_data["metadata"][
            "storage_status"
        ] == status
        assert tts.requests[0].text == expected
        assert speaker.play_count == 1
        usage = pipeline.resource_manager.current_usage()
        assert usage["active_task_count"] == 0
        assert "single_turn_voice_pipeline" not in usage["reservation_names"]


def test_save_then_fresh_production_manager_recalls_persisted_value(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    saved, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "remember that my favorite color is blue",
        playback=False,
        turn=1,
    )
    recalled, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "what is my favorite color",
        playback=False,
        turn=2,
    )

    assert saved.brain_text_response == "I will remember that your favorite color is blue."
    assert recalled.brain_text_response == "Your favorite color is blue."


def test_general_owner_fact_uses_mock_voice_transport_and_central_brain_service(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    saved, saved_manager, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remember that my birthday is June 8.",
        playback=False,
        turn=1,
    )
    recalled, recalled_manager, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "When is my birthday?",
        playback=False,
        turn=2,
    )

    assert saved.routed_skill == "owner_memory"
    assert saved.brain_text_response == "I will remember that your birthday is June 8."
    assert recalled.routed_skill == "owner_memory"
    assert recalled.brain_text_response == "Your birthday is June 8."
    assert saved_manager.last_plan.steps[0].target == "owner_memory"
    assert recalled_manager.last_plan.steps[0].target == "owner_memory"
    assert saved_manager.owner_memory_service is saved_manager.core_service.owner_memory_service
    assert recalled_manager.owner_memory_service is recalled_manager.core_service.owner_memory_service


def test_general_long_term_memory_uses_production_voice_route_and_fresh_central_service(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    saved, saved_manager, _, _, saved_tts, saved_speaker, saved_pipeline = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remember in your long-term memory that I like going to the gym.",
        playback=True,
        turn=1,
    )
    recalled, recalled_manager, _, _, recalled_tts, recalled_speaker, recalled_pipeline = _run_voice_turn(
        tmp_path,
        profile_path,
        "What do you remember about the gym?",
        playback=True,
        turn=2,
    )

    assert saved.success is True
    assert saved.detected_intent == "owner_memory"
    assert saved.routed_skill == "owner_memory"
    assert saved.planner_decision == "1 step(s): owner_memory"
    assert saved.execution_result == "success"
    assert saved.brain_text_response == "I will remember that you like going to the gym."
    assert saved_manager.last_plan.steps[0].target == "owner_memory"
    assert saved_manager.last_execution.step_results[0].returned_data["metadata"]["storage_status"] == "created"
    assert saved_tts.requests[0].text == saved.brain_text_response
    assert saved_speaker.play_count == 1

    assert recalled.success is True
    assert recalled.detected_intent == "owner_memory"
    assert recalled.routed_skill == "owner_memory"
    assert recalled.brain_text_response == "You told me that you like going to the gym."
    assert recalled_manager.last_plan.steps[0].target == "owner_memory"
    assert recalled_manager.last_execution.step_results[0].returned_data["metadata"]["storage_status"] == "recalled"
    assert recalled_tts.requests[0].text == recalled.brain_text_response
    assert recalled_speaker.play_count == 1

    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert payload["schema_name"] == "ares.owner_profile"
    assert payload["schema_version"] == 3
    assert payload["data"]["facts"] == {}
    assert len(payload["data"]["memories"]) == 1
    assert payload["data"]["memories"][0]["canonical_text"] == "The owner likes going to the gym."
    assert saved_manager.owner_memory_service is saved_manager.core_service.owner_memory_service
    assert recalled_manager.owner_memory_service is recalled_manager.core_service.owner_memory_service
    for pipeline in (saved_pipeline, recalled_pipeline):
        usage = pipeline.resource_manager.current_usage()
        assert usage["active_task_count"] == 0
        assert "single_turn_voice_pipeline" not in usage["reservation_names"]


def test_real_whisper_memory_variants_use_production_route_and_preserve_keyed_facts(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    bootstrap = OwnerMemoryService(profile_path)
    expected_facts = {
        "birthday": "June 8th",
        "city": "Madrid",
        "favorite_color": "red",
        "favorite_game": "EVE Online",
    }
    for key, value in expected_facts.items():
        bootstrap._store.save_fact(key, value)
    assert bootstrap.inspect(include_values=True)["memory_count"] == 0

    gym, gym_manager, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remember in your locked term memory that I love going to the gym",
        playback=False,
        turn=1,
    )
    games, games_manager, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remembering a long term memory that I like video games",
        playback=False,
        turn=2,
    )
    recalled, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "What do I like?",
        playback=False,
        turn=3,
    )
    gym_duplicate, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remember in your locked term memory that I love going to the gym",
        playback=False,
        turn=4,
    )
    games_duplicate, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remembering a long term memory that I like video games",
        playback=False,
        turn=5,
    )

    assert gym.raw_transcript == "Remember in your locked term memory that I love going to the gym"
    assert gym.normalized_command == "remember longterm that I love going to the gym"
    assert gym.detected_intent == gym.routed_skill == "owner_memory"
    assert gym.brain_text_response == "I will remember that you love going to the gym."
    assert gym_manager.last_plan.steps[0].target == "owner_memory"
    assert not (tmp_path / "tasks.json").exists()
    task_candidate = next(candidate for candidate in gym.candidate_skills if candidate["skill"] == "tasks")
    assert task_candidate["selected"] is False
    gym_diagnostics = gym.routing_diagnostics["owner_memory"]
    assert gym_diagnostics["normalized_memory_trigger"] == "remember longterm that"
    assert gym_diagnostics["extracted_fact_text"] == "I love going to the gym"
    assert gym_diagnostics["routing_reason"] == "explicit_owner_memory_storage_request"

    assert games.raw_transcript == "Remembering a long term memory that I like video games"
    assert games.normalized_command == "remember longterm that I like video games"
    assert games.detected_intent == games.routed_skill == "owner_memory"
    assert games.brain_text_response == "I will remember that you like video games."
    assert games_manager.last_plan.steps[0].target == "owner_memory"
    assert "going to the gym" in recalled.brain_text_response
    assert "video games" in recalled.brain_text_response
    assert gym_duplicate.routing_diagnostics["owner_memory"]["operation_result"] == "duplicate"
    assert games_duplicate.routing_diagnostics["owner_memory"]["operation_result"] == "duplicate"

    report = OwnerMemoryService(profile_path).inspect(include_values=True)
    facts = {fact["normalized_key"]: fact["value"] for fact in report["facts"]}
    active = [memory for memory in report["memories"] if memory["status"] == "active"]
    assert report["memory_count"] == 2
    assert len(active) == 2
    assert {memory["object"] for memory in active} == {"going to the gym", "video games"}
    assert facts == expected_facts


def test_ordinary_voice_statement_does_not_persist_general_owner_memory(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"

    result, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "I went to the gym today.",
        playback=False,
        turn=1,
    )

    assert result.routed_skill != "owner_memory"
    assert not profile_path.exists()


def test_imperfect_whisper_owner_phrase_outranks_tasks_in_production_route(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"

    result, manager, _, _, _, _, _ = _run_voice_turn(
        tmp_path,
        profile_path,
        "Remember that modified white color is blue.",
        playback=False,
        turn=1,
    )

    assert result.success is True
    assert result.normalized_command == "remember that my favorite color is blue"
    assert result.transcript_cleanup_rule == "owner_memory_whisper_alias_v1"
    assert result.detected_intent == "owner_memory"
    assert result.routed_skill == "owner_memory"
    assert result.brain_text_response == (
        "I will remember that your favorite color is blue."
    )
    assert manager.last_plan.steps[0].target == "owner_memory"
    assert profile_path.exists()
    assert not (tmp_path / "tasks.json").exists()
    owner_diagnostics = result.routing_diagnostics["owner_memory"]
    assert owner_diagnostics["action"] == "save"
    assert owner_diagnostics["normalized_key"] == "favorite_color"
    assert owner_diagnostics["profile_path"] == str(profile_path.resolve())
    assert owner_diagnostics["file_existed_before"] is False
    assert owner_diagnostics["operation_result"] == "created"
    task_candidate = next(
        candidate
        for candidate in result.candidate_skills
        if candidate["skill"] == "tasks"
    )
    assert task_candidate["selected"] is False


def test_declarative_update_and_delete_forms_use_owner_memory_pipeline(tmp_path):
    profile_path = tmp_path / "memory" / "owner_profile.json"
    interactions = (
        (
            "My favorite color is blue.",
            "I will remember that your favorite color is blue.",
            "created",
        ),
        (
            "Update my favorite color to red.",
            "I updated your favorite color from blue to red.",
            "updated",
        ),
        (
            "Delete my favorite color.",
            "I forgot your favorite color.",
            "forgotten",
        ),
    )

    for turn, (text, expected_response, expected_status) in enumerate(
        interactions,
        start=1,
    ):
        result, manager, *_ = _run_voice_turn(
            tmp_path,
            profile_path,
            text,
            playback=False,
            turn=turn,
        )

        assert result.success is True
        assert result.detected_intent == "owner_memory"
        assert result.routed_skill == "owner_memory"
        assert result.brain_text_response == expected_response
        assert manager.last_execution.step_results[0].returned_data["metadata"][
            "storage_status"
        ] == expected_status


def test_production_skill_construction_uses_canonical_profile_override_across_runs(
    tmp_path,
    monkeypatch,
):
    profile_path = tmp_path / "isolated" / "owner_profile.json"
    tasks_path = tmp_path / "tasks.json"
    monkeypatch.setenv("ARES_OWNER_PROFILE_PATH", str(profile_path))

    def build_manager(turn):
        event_bus = EventBus(max_history=500)
        return manual.create_skill_manager(
            CoreService(),
            event_history_store=EventHistoryStore(
                tmp_path / f"events_{turn}.json"
            ),
            event_bus=event_bus,
            memory_store=MemoryStore(
                short_path=tmp_path / "short_memory.json",
                long_path=tmp_path / "long_memory.json",
                event_bus=event_bus,
            ),
            profile_store=UserProfileStore(
                tmp_path / "user_profile.json",
                event_bus=event_bus,
            ),
            goals_store=GoalsStore(tmp_path / "goals.json", event_bus=event_bus),
            notes_store=NotesStore(tmp_path / "notes.json", event_bus=event_bus),
            tasks_store=TasksStore(tasks_path, event_bus=event_bus),
        )

    first = manual.build_existing_brain_handler(build_manager(1))(
        "remember that my favorite color is blue"
    )
    second = manual.build_existing_brain_handler(build_manager(2))(
        "what is my favorite color"
    )

    assert first.skill == "owner_memory"
    assert first.text == "I will remember that your favorite color is blue."
    assert second.skill == "owner_memory"
    assert second.text == "Your favorite color is blue."
    assert profile_path.exists()
    assert not tasks_path.exists()


def test_voice_response_tts_and_speaker_remain_explicitly_opt_in(tmp_path):
    profile_path = tmp_path / "owner_profile.json"
    disabled, _, _, _, disabled_tts, disabled_speaker, _ = _run_voice_turn(
        tmp_path,
        profile_path,
        "remember that my favorite color is blue",
        playback=False,
        turn=1,
    )
    enabled, _, _, _, enabled_tts, enabled_speaker, _ = _run_voice_turn(
        tmp_path,
        profile_path,
        "what is my favorite color",
        playback=True,
        turn=2,
    )

    assert disabled.playback_status == "playback_disabled"
    assert disabled_tts.synthesis_count == 0
    assert disabled_speaker.play_count == 0
    assert enabled_tts.requests[0].text == "Your favorite color is blue."
    assert enabled_speaker.play_count == 1


def test_protected_voice_request_redacts_transcript_result_and_events(tmp_path):
    secret = "VOICE_SECRET_789"
    profile_path = tmp_path / "owner_profile.json"

    result, _, event_bus, history, _, speaker, _ = _run_voice_turn(
        tmp_path,
        profile_path,
        f"remember that my API key is {secret}",
        playback=False,
        turn=1,
    )

    assert result.success is True
    assert result.routed_skill == "owner_memory"
    assert result.raw_transcript == "[REDACTED]"
    assert result.cleaned_transcript == "[REDACTED]"
    assert result.normalized_command == "[REDACTED]"
    assert secret not in json.dumps(result.to_dict())
    assert secret not in json.dumps([event.payload for event in event_bus.history()])
    assert secret not in json.dumps([record.to_dict() for record in history.recent()])
    assert not profile_path.exists()
    assert speaker.play_count == 0


def test_profile_persists_only_normalized_fact_not_voice_artifacts(tmp_path):
    profile_path = tmp_path / "owner_profile.json"
    result, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "remember that my favorite color is blue",
        playback=False,
        turn=1,
    )
    serialized = profile_path.read_text(encoding="utf-8")

    assert result.success is True
    assert "favorite_color" in serialized
    assert "blue" in serialized
    assert "remember that my" not in serialized
    assert ".wav" not in serialized
    assert "transcript" not in serialized


def test_calculator_and_unknown_routes_remain_unchanged_with_owner_store(tmp_path):
    profile_path = tmp_path / "owner_profile.json"
    calculator, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "calculate 2 + 2",
        playback=False,
        turn=1,
    )
    unknown, *_ = _run_voice_turn(
        tmp_path,
        profile_path,
        "explain quantum painting",
        playback=False,
        turn=2,
    )

    assert calculator.routed_skill == "calculator"
    assert calculator.brain_text_response == "Result: 4"
    assert unknown.routed_skill == "unknown"
    assert unknown.brain_text_response == "I cannot handle that request yet."


def test_run_ares_voice_hardware_defaults_are_unchanged():
    args = run_ares_voice.build_parser().parse_args([])

    assert args.microphone_device == "plughw:2,0"
    assert args.speaker_device == "plughw:CARD=Device,DEV=0"
    assert args.whisper_command == "external/whisper.cpp/build/bin/whisper-cli"
    assert args.whisper_model == "models/whisper/ggml-base.en.bin"
    assert args.voice_profile == "en_US-hfc_male-medium"
    assert args.timeout == 300.0
