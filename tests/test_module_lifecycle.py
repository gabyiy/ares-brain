from datetime import datetime

from core import (
    LIFECYCLE_BUSY,
    LIFECYCLE_DEGRADED,
    LIFECYCLE_FAILED,
    LIFECYCLE_READY,
    LIFECYCLE_STARTING,
    LIFECYCLE_STOPPED,
    LIFECYCLE_STOPPING,
    LIFECYCLE_UNLOADED,
    LifecyclePolicy,
    LifecycleRequest,
    ModuleLifecycleManager,
)


class LifecycleTestModule:
    def __init__(
        self,
        health_success=True,
        start_raises=False,
        execute_raises=False,
    ):
        self.health_success = health_success
        self.start_raises = start_raises
        self.execute_raises = execute_raises
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_raises:
            raise RuntimeError("start boom")
        return {"success": True, "status": "started"}

    def health_check(self):
        self.calls.append("health_check")
        if not self.health_success:
            return {
                "success": False,
                "status": "degraded",
                "error_message": "health boom",
            }
        return {"success": True, "status": "healthy"}

    def execute(self, request):
        self.calls.append(f"execute:{request.correlation_id}")
        if self.execute_raises:
            raise RuntimeError("execute boom")
        return {"handled": request.payload.get("text", "")}

    def stop(self):
        self.calls.append("stop")
        return {"success": True, "status": "stopped"}


def _request(operation="test", correlation_id="corr-1"):
    return LifecycleRequest(
        module_name="voice",
        operation=operation,
        payload={"text": "hello"},
        session_id="session-1",
        correlation_id=correlation_id,
    )


def _manager_with(module=None, policy=None):
    manager = ModuleLifecycleManager()
    manager.register_module("voice", module or LifecycleTestModule(), policy=policy)
    return manager


def _states(history):
    return [(item.from_state, item.to_state) for item in history]


def test_lifecycle_successful_unloaded_starting_ready():
    manager = _manager_with()

    result = manager.start("voice", _request("start"))

    assert result.success is True
    assert result.state == LIFECYCLE_READY
    assert _states(manager.history("voice")) == [
        (LIFECYCLE_UNLOADED, LIFECYCLE_STARTING),
        (LIFECYCLE_STARTING, LIFECYCLE_READY),
    ]


def test_lifecycle_ready_busy_ready_during_execution():
    manager = _manager_with()
    request = _request("execute")

    manager.start("voice", request)
    manager.health_check("voice", request)
    result = manager.execute("voice", request)

    assert result.success is True
    assert result.state == LIFECYCLE_READY
    assert result.data["response"] == {"handled": "hello"}
    assert _states(manager.history("voice"))[-2:] == [
        (LIFECYCLE_READY, LIFECYCLE_BUSY),
        (LIFECYCLE_BUSY, LIFECYCLE_READY),
    ]


def test_lifecycle_ready_stopping_stopped():
    manager = _manager_with()

    manager.start("voice", _request("start"))
    result = manager.stop("voice", _request("stop"))

    assert result.success is True
    assert result.state == LIFECYCLE_STOPPED
    assert _states(manager.history("voice"))[-2:] == [
        (LIFECYCLE_READY, LIFECYCLE_STOPPING),
        (LIFECYCLE_STOPPING, LIFECYCLE_STOPPED),
    ]


def test_lifecycle_idempotent_start():
    manager = _manager_with()
    manager.start("voice", _request("start"))
    transition_count = len(manager.history("voice"))

    result = manager.start("voice", _request("start"))

    assert result.success is True
    assert result.status == "already_ready"
    assert result.state == LIFECYCLE_READY
    assert len(manager.history("voice")) == transition_count


def test_lifecycle_idempotent_stop():
    manager = _manager_with()
    manager.start("voice", _request("start"))
    manager.stop("voice", _request("stop"))
    transition_count = len(manager.history("voice"))

    result = manager.stop("voice", _request("stop"))

    assert result.success is True
    assert result.status == "already_stopped"
    assert result.state == LIFECYCLE_STOPPED
    assert len(manager.history("voice")) == transition_count


def test_lifecycle_execution_rejected_before_start():
    manager = _manager_with()

    result = manager.execute("voice", _request("execute"))

    assert result.success is False
    assert result.status == "execution_rejected_not_ready"
    assert result.error_message == "module_not_ready"
    assert result.state == LIFECYCLE_UNLOADED
    assert manager.history("voice") == []


def test_lifecycle_health_check_failure_degrades_by_policy():
    module = LifecycleTestModule(health_success=False)
    manager = _manager_with(module)
    manager.start("voice", _request("start"))

    result = manager.health_check("voice", _request("health_check"))

    assert result.success is False
    assert result.status == "health_check_failed"
    assert result.state == LIFECYCLE_DEGRADED
    assert manager.status("voice").reason == "health boom"


def test_lifecycle_health_check_failure_can_fail_by_policy():
    module = LifecycleTestModule(health_success=False)
    manager = _manager_with(
        module,
        policy=LifecyclePolicy(health_failure_state=LIFECYCLE_FAILED),
    )
    manager.start("voice", _request("start"))

    result = manager.health_check("voice", _request("health_check"))

    assert result.success is False
    assert result.state == LIFECYCLE_FAILED
    assert manager.status("voice").reason == "health boom"


def test_lifecycle_startup_exception_causes_failed():
    module = LifecycleTestModule(start_raises=True)
    manager = _manager_with(module)

    result = manager.start("voice", _request("start"))

    assert result.success is False
    assert result.status == "startup_exception"
    assert result.state == LIFECYCLE_FAILED
    assert "RuntimeError: start boom" == result.error_message


def test_lifecycle_execution_exception_is_isolated():
    failing = LifecycleTestModule(execute_raises=True)
    healthy = LifecycleTestModule()
    manager = ModuleLifecycleManager()
    manager.register_module("voice", failing)
    manager.register_module("pc", healthy)

    manager.start("voice", LifecycleRequest("voice", "start"))
    failed = manager.execute("voice", LifecycleRequest("voice", "execute"))
    manager.start("pc", LifecycleRequest("pc", "start"))
    passed = manager.execute("pc", LifecycleRequest("pc", "execute"))

    assert failed.success is False
    assert failed.state == LIFECYCLE_FAILED
    assert manager.status("voice").state == LIFECYCLE_FAILED
    assert passed.success is True
    assert manager.status("pc").state == LIFECYCLE_READY


def test_lifecycle_illegal_state_transition_is_rejected():
    manager = _manager_with()

    result = manager.transition("voice", LIFECYCLE_BUSY, _request("transition"))

    assert result.success is False
    assert result.status == "illegal_transition"
    assert result.error_message == "illegal_transition:UNLOADED->BUSY"
    assert manager.status("voice").state == LIFECYCLE_UNLOADED
    assert manager.history("voice") == []


def test_lifecycle_explicit_recovery_from_failed():
    module = LifecycleTestModule(start_raises=True)
    manager = _manager_with(module)
    manager.start("voice", _request("start"))
    module.start_raises = False

    result = manager.recover("voice", _request("recover"))

    assert result.success is True
    assert result.status == "recovered"
    assert manager.status("voice").state == LIFECYCLE_READY
    assert (LIFECYCLE_FAILED, LIFECYCLE_STOPPING) in _states(manager.history("voice"))


def test_lifecycle_transition_history_is_correct():
    manager = _manager_with()
    request = _request("execute")

    manager.start("voice", request)
    manager.health_check("voice", request)
    manager.execute("voice", request)
    manager.stop("voice", request)

    assert _states(manager.history("voice")) == [
        (LIFECYCLE_UNLOADED, LIFECYCLE_STARTING),
        (LIFECYCLE_STARTING, LIFECYCLE_READY),
        (LIFECYCLE_READY, LIFECYCLE_BUSY),
        (LIFECYCLE_BUSY, LIFECYCLE_READY),
        (LIFECYCLE_READY, LIFECYCLE_STOPPING),
        (LIFECYCLE_STOPPING, LIFECYCLE_STOPPED),
    ]


def test_lifecycle_timestamps_are_monotonic():
    manager = _manager_with()
    request = _request("execute")

    manager.start("voice", request)
    manager.execute("voice", request)
    manager.stop("voice", request)

    timestamps = [
        datetime.fromisoformat(transition.timestamp.replace("Z", "+00:00"))
        for transition in manager.history("voice")
    ]
    assert timestamps == sorted(timestamps)


def test_lifecycle_correlation_id_survives_execution():
    manager = _manager_with()
    request = _request("execute", correlation_id="corr-stable")

    manager.start("voice", request)
    manager.health_check("voice", request)
    result = manager.execute("voice", request)

    assert result.request.correlation_id == "corr-stable"
    assert result.data["response"] == {"handled": "hello"}
    assert all(
        transition.correlation_id == "corr-stable"
        for transition in manager.history("voice")
    )


def test_lifecycle_query_returns_structured_state_and_health_information():
    manager = _manager_with()
    manager.start("voice", _request("start"))

    status = manager.status("voice").to_dict()

    assert status["module_name"] == "voice"
    assert status["state"] == LIFECYCLE_READY
    assert status["healthy"] is True
    assert status["policy"]["background_timer"] == "disabled"
    assert status["transition_count"] == 2
