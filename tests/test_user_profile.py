from events import EventBus
from memory import UserProfileStore, detect_profile_facts


def test_detect_profile_facts_supported_patterns():
    examples = {
        "My name is Gabi": ("name", "Gabi"),
        "I live in Madrid": ("location", "Madrid"),
        "My birthday is June 30": ("birthday", "June 30"),
        "My favorite tank is Leopard 2": ("favorite_tank", "Leopard 2"),
        "I own a Raspberry Pi": ("owned_items", "a Raspberry Pi"),
    }

    for text, expected in examples.items():
        facts = detect_profile_facts(text)
        assert [(fact.key, fact.value) for fact in facts] == [expected]


def test_user_profile_store_persists_facts_and_events(tmp_path):
    bus = EventBus(raise_handler_errors=True)
    profile = UserProfileStore(path=tmp_path / "profile.json", event_bus=bus)

    profile.learn_from_text("My name is Gabi")
    profile.learn_from_text("I live in Madrid")
    profile.learn_from_text("My birthday is June 30")
    profile.learn_from_text("My favorite tank is Leopard 2")
    profile.learn_from_text("I own a Raspberry Pi")
    profile.learn_from_text("I own a Raspberry Pi")

    reloaded = UserProfileStore(path=tmp_path / "profile.json", event_bus=bus)

    assert reloaded.get_value("name") == "Gabi"
    assert reloaded.get_value("location") == "Madrid"
    assert reloaded.get_value("birthday") == "June 30"
    assert reloaded.get_favorite("tank") == "Leopard 2"
    assert reloaded.get_value("owned_items") == ["a Raspberry Pi"]
    assert len(bus.history("profile.fact_saved")) == 6
