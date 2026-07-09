import pytest

from core import (
    EVENT_PRIORITIES,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    Event,
    EventBus,
)


def test_core_event_dataclass_normalizes_payload_and_priority():
    event = Event(
        source="voice_city",
        type="voice.input_received",
        priority=PRIORITY_HIGH,
        payload={"text": "hello"},
    )

    assert event.source == "voice_city"
    assert event.type == "voice.input_received"
    assert event.priority == PRIORITY_HIGH
    assert event.payload == {"text": "hello"}
    assert event.timestamp.endswith("Z")
    assert event.to_dict()["payload"] == {"text": "hello"}


def test_core_event_bus_publish_subscribe_and_unsubscribe():
    bus = EventBus()
    seen = []

    unsubscribe = bus.subscribe("city.important", lambda event: seen.append(event))

    event = bus.publish(
        source="vision_city",
        type="city.important",
        priority=PRIORITY_NORMAL,
        payload={"object": "door"},
    )

    assert seen == [event]
    assert bus.history("city.important") == [event]

    unsubscribe()
    bus.publish(source="vision_city", type="city.important", payload={"object": "window"})

    assert seen == [event]


def test_core_event_bus_publish_with_no_subscribers_is_safe():
    bus = EventBus()

    event = bus.publish(
        source="weather_city",
        type="weather.changed",
        priority=PRIORITY_LOW,
        payload={"condition": "clear"},
    )

    assert event.type == "weather.changed"
    assert event.priority == PRIORITY_LOW
    assert bus.history() == [event]


def test_core_event_bus_priority_ordering():
    bus = EventBus()

    low = bus.publish(source="notes_city", type="note.saved", priority=PRIORITY_LOW)
    critical = bus.publish(source="pc_city", type="device.safety", priority=PRIORITY_CRITICAL)
    normal = bus.publish(source="calendar_city", type="calendar.event", priority=PRIORITY_NORMAL)
    high = bus.publish(source="voice_city", type="voice.command", priority=PRIORITY_HIGH)

    assert bus.history() == [critical, high, normal, low]


def test_core_event_bus_rejects_invalid_priority():
    bus = EventBus()

    with pytest.raises(ValueError, match="Invalid event priority"):
        bus.publish(source="voice_city", type="voice.command", priority="urgent")


def test_core_event_bus_priority_levels_are_stable():
    assert EVENT_PRIORITIES == (
        PRIORITY_LOW,
        PRIORITY_NORMAL,
        PRIORITY_HIGH,
        PRIORITY_CRITICAL,
    )
