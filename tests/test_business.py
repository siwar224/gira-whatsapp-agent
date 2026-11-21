import pytest

from app.agent.prompt import static_instructions, turn_context
from app.agent.tools import tool_schemas
from app.business.countries import SUPPORTED_COUNTRIES
from app.business.phone import country_of, normalize_phone
from app.business.profile import BusinessProfile


@pytest.mark.parametrize(
    ("country", "typed", "expected"),
    [
        ("TN", "20 123 456", "+21620123456"),
        ("MA", "06 12 34 56 78", "+212612345678"),
        ("FR", "06 12 34 56 78", "+33612345678"),
        ("IT", "347 123 4567", "+393471234567"),
        ("DE", "0151 23456789", "+4915123456789"),
        ("ES", "612 34 56 78", "+34612345678"),
        ("FR", "0049 151 23456789", "+4915123456789"),
        ("DE", "+216 20 123 456", "+21620123456"),
    ],
)
def test_phone_numbers_from_every_supported_country(country, typed, expected):
    assert normalize_phone(typed, country) == expected


@pytest.mark.parametrize("typed", ["", "123", "hello", "+33 1"])
def test_invalid_phone_numbers(typed):
    assert normalize_phone(typed, "FR") is None


def test_country_of_a_number():
    assert country_of("+21620123456").name == "Tunisia"
    assert country_of("+212612345678").name == "Morocco"
    assert country_of("+14155550123") is None


def test_each_country_has_a_valid_time_zone(profile):
    for code, country in SUPPORTED_COUNTRIES.items():
        data = profile.model_dump()
        data.update(country=code, timezone=None)
        assert BusinessProfile.model_validate(data).timezone == country.timezone


def test_unsupported_country_is_rejected(profile):
    data = profile.model_dump()
    data["country"] = "US"
    with pytest.raises(ValueError, match="Unsupported country"):
        BusinessProfile.model_validate(data)


def test_profile_renders_menu_hours_and_rules(profile):
    facts = profile.render_facts()
    assert "Linguine allo scoglio, 24 (Signature)" in facts
    assert "Monday: closed" in facts
    assert "Sunday: 12:00–15:30" in facts
    assert "Parties of 1 to 6 guests" in facts


def test_instructions_cover_languages_and_house_rules(profile):
    text = static_instructions(profile)
    assert "wine or any alcoholic drink" in text
    assert "French" in text


def test_tunisian_restaurant_expects_derja(profile):
    data = profile.model_dump()
    data.update(country="TN", timezone=None)
    tunis = BusinessProfile.model_validate(data)
    assert "Derja" in static_instructions(tunis)
    assert tunis.timezone == "Africa/Tunis"


def test_turn_context_uses_the_restaurant_clock(profile):
    from datetime import UTC, datetime

    context = turn_context(profile, datetime(2026, 11, 21, 18, 30, tzinfo=UTC), "+33612345678", "Lea", False)
    assert "Saturday 2026-11-21 19:30" in context  # Paris is UTC+1 in November
    assert "+33612345678" in context


def test_tool_schemas_follow_the_profile(profile):
    tools = {tool["name"]: tool for tool in tool_schemas(profile)}
    assert set(tools) == {
        "check_availability",
        "create_booking",
        "find_booking",
        "cancel_booking",
        "join_waitlist",
        "handoff_to_team",
    }
    area = tools["create_booking"]["input_schema"]["properties"]["area"]
    assert area["enum"] == ["window", "room", "counter", "terrace"]
