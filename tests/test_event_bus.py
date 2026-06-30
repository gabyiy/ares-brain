import pytest

from events import EventBus


def test_event_bus_publish_subscribe_history_and_unsubscribe():
    bus = EventBus(raise_handler_errors=True)
    seen = []
    wildcard = []

    unsubscribe = bus.subscribe("alpha", lambda event: seen.append(event.payload["value"]))
    bus.subscribe("*", lambda event: wildcard.append(event.name))

    event = bus.publish("alpha", {"value": 42}, source="test")

    assert event.name == "alpha"
    assert event.source == "test"
    assert seen == [42]
    assert wildcard == ["alpha"]
    assert bus.history("alpha") == [event]

    unsubscribe()
    bus.publish("alpha", {"value": 7})

    assert seen == [42]
    assert wildcard == ["alpha", "alpha"]


def test_event_bus_can_raise_handler_errors():
    bus = EventBus(raise_handler_errors=True)

    def fail(_event):
        raise RuntimeError("handler failed")

    bus.subscribe("boom", fail)

    with pytest.raises(RuntimeError, match="handler failed"):
        bus.publish("boom")
