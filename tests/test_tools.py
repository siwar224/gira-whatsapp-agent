import json

import httpx
import pytest
import respx

from app.agent.tools import GuestContext, Toolbox
from app.booking.client import BookingClient

API = "https://booking.test"


@pytest.fixture
async def toolbox(profile):
    async with httpx.AsyncClient() as http:
        yield Toolbox(profile, BookingClient(API, http))


def availability(taken_at_2000: list[str]) -> dict:
    return {
        "date": "2026-11-28",
        "durationMinutes": 120,
        "slots": [
            {"time": "19:30", "meal": "dinner", "taken": []},
            {"time": "20:00", "meal": "dinner", "taken": taken_at_2000},
        ],
    }


@respx.mock
async def test_availability_counts_tables_that_suit_the_party(toolbox):
    respx.get(f"{API}/api/availability").respond(json=availability(["C1", "C2", "C3", "2", "5", "6", "9", "10"]))
    result = await toolbox.run(
        "check_availability", {"date": "2026-11-28", "guests": 2}, GuestContext(1, "+33612345678")
    )
    by_time = {t["time"]: t for t in result["times"]}
    # A couple fits 2- and 4-seat tables (never the 6-seat one): 13 tables minus the 6-seater.
    assert by_time["19:30"]["free_tables"] == 12
    # At 20:00 only the 4-seat tables 1, 3, 7 and 8 are left.
    assert by_time["20:00"]["free_tables"] == 4
    assert by_time["20:00"]["free_areas"] == ["room", "terrace", "window"]


@respx.mock
async def test_booking_prefers_the_area_and_retries_a_table_taken_meanwhile(toolbox):
    respx.get(f"{API}/api/availability").respond(json=availability([]))
    created = {
        "booking": {
            "code": "GIRA-ABCDEFGH",
            "date": "2026-11-28",
            "time": "20:00",
            "guests": 2,
            "name": "Lea",
            "emailSent": False,
        }
    }
    route = respx.post(f"{API}/api/bookings").mock(
        side_effect=[httpx.Response(409, json={"error": {"code": "table_taken"}}), httpx.Response(201, json=created)]
    )
    ctx = GuestContext(1, "+21620123456")
    result = await toolbox.run(
        "create_booking",
        {
            "date": "2026-11-28",
            "time": "20:00",
            "guests": 2,
            "name": "Lea",
            "area": "terrace",
            "phone": "+33 6 00 00 00 00",
        },
        ctx,
    )
    assert result["ok"] is True
    assert result["code"] == "GIRA-ABCDEFGH"
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first["tableId"] == "9" and second["tableId"] == "10"  # terrace two-tops, in order
    assert second["phone"] == "+21620123456"  # the WhatsApp number wins over a typed one
    assert route.calls[0].request.headers["Origin"] == API
    assert ctx.actions == ["create_booking"]


@respx.mock
async def test_unknown_number_must_give_a_valid_phone(toolbox):
    result = await toolbox.run("find_booking", {"code": "GIRA-ABCDEFGH", "phone": "123"}, GuestContext(1, None))
    assert result["code"] == "phone_needed"

    lookup = respx.post(f"{API}/api/bookings/lookup").respond(
        json={
            "booking": {
                "code": "GIRA-ABCDEFGH",
                "date": "2026-11-28",
                "time": "20:00",
                "guests": 2,
                "name": "Lea",
                "tableId": "9",
                "upcoming": True,
            }
        }
    )
    found = await toolbox.run(
        "find_booking", {"code": "gira-abcdefgh", "phone": "06 12 34 56 78"}, GuestContext(1, None)
    )
    assert found["table"] == "Table 9"
    assert json.loads(lookup.calls[0].request.content) == {"code": "GIRA-ABCDEFGH", "phone": "+33612345678"}


@respx.mock
async def test_api_errors_are_explained_to_the_agent(toolbox):
    respx.delete(f"{API}/api/bookings/GIRA-ABCDEFGH").respond(
        409, json={"error": {"code": "booking_past", "message": "This booking is in the past."}}
    )
    result = await toolbox.run("cancel_booking", {"code": "GIRA-ABCDEFGH"}, GuestContext(1, "+33612345678"))
    assert result == {"ok": False, "status": 409, "code": "booking_past", "message": "This booking is in the past."}


@respx.mock
async def test_unreachable_booking_system(toolbox):
    respx.get(f"{API}/api/availability").mock(side_effect=httpx.ConnectError("down"))
    result = await toolbox.run("check_availability", {"date": "2026-11-28", "guests": 2}, GuestContext(1, None))
    assert result["code"] == "booking_system_unreachable"


async def test_handoff_is_recorded(toolbox):
    ctx = GuestContext(1, "+33612345678")
    await toolbox.run("handoff_to_team", {"reason": "Complaint about last night"}, ctx)
    assert ctx.handoff_reason == "Complaint about last night"
