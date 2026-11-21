"""The actions the agent can take for a guest, and their schemas for Claude."""

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.booking.client import BookingClient, free_tables
from app.business.phone import normalize_phone
from app.business.profile import BusinessProfile

log = logging.getLogger(__name__)


@dataclass
class GuestContext:
    """Who the agent is talking to. The guest's own WhatsApp number is trusted for their bookings."""

    conversation_id: int
    phone: str | None
    name: str | None = None
    handoff_reason: str | None = None
    actions: list[str] = field(default_factory=list)


def tool_schemas(profile: BusinessProfile) -> list[dict[str, Any]]:
    areas = list(profile.booking.areas)
    occasions = ["none", *profile.booking.occasions]
    guests = {"type": "integer", "minimum": profile.booking.min_guests, "maximum": profile.booking.max_guests}
    hidden_phone = {
        "type": "string",
        "description": "Only when the guest's WhatsApp number is unknown: the phone they give, any format.",
    }
    return [
        {
            "name": "check_availability",
            "description": "List the bookable times of a day and how many tables suit the party at each time.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "guests": guests,
                    "meal": {"type": "string", "enum": ["lunch", "dinner"]},
                    "area": {"type": "string", "enum": areas},
                },
                "required": ["date", "guests"],
            },
        },
        {
            "name": "create_booking",
            "description": (
                "Book a table, only after the guest explicitly confirmed your summary. "
                "The best free table is chosen automatically."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM, a time returned by check_availability"},
                    "guests": guests,
                    "name": {"type": "string"},
                    "email": {"type": "string", "description": "For a confirmation email"},
                    "note": {"type": "string", "description": "Allergies, high chair, a surprise… (max 200)"},
                    "occasion": {"type": "string", "enum": occasions},
                    "area": {"type": "string", "enum": areas},
                    "phone": hidden_phone,
                },
                "required": ["date", "time", "guests", "name"],
            },
        },
        {
            "name": "find_booking",
            "description": "Look up one of the guest's bookings by its code (GIRA-XXXXXXXX).",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}, "phone": hidden_phone},
                "required": ["code"],
            },
        },
        {
            "name": "cancel_booking",
            "description": "Cancel one of the guest's bookings, only after they explicitly confirmed it.",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}, "phone": hidden_phone},
                "required": ["code"],
            },
        },
        {
            "name": "join_waitlist",
            "description": "Put the guest on the waiting list for a lunch or dinner that is full.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "meal": {"type": "string", "enum": ["lunch", "dinner"]},
                    "guests": guests,
                    "name": {"type": "string"},
                    "email": {"type": "string", "description": "Needed to be told when a table frees up"},
                    "phone": hidden_phone,
                },
                "required": ["date", "meal", "guests", "name"],
            },
        },
        {
            "name": "handoff_to_team",
            "description": (
                "Pass the chat to a person from the team: the guest asks for a human, complains, or needs "
                "something you cannot answer or do."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "One line for the team"}},
                "required": ["reason"],
            },
        },
    ]


class Toolbox:
    def __init__(self, profile: BusinessProfile, booking: BookingClient):
        self._profile = profile
        self._booking = booking

    def _phone(self, ctx: GuestContext, given: str | None) -> str | None:
        return ctx.phone or normalize_phone(given, self._profile.country)

    async def run(self, name: str, args: dict[str, Any], ctx: GuestContext) -> dict[str, Any]:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return {"ok": False, "message": f"Unknown tool {name}."}
        try:
            result = await handler(ctx, **args)
        except TypeError as exc:
            return {"ok": False, "code": "invalid_input", "message": str(exc)}
        except httpx.HTTPError as exc:
            log.warning("Booking API unreachable during %s: %s", name, type(exc).__name__)
            return {
                "ok": False,
                "code": "booking_system_unreachable",
                "message": "The booking system can't be reached. Apologise and suggest the website or calling.",
            }
        ctx.actions.append(name)
        return result

    async def _check_availability(
        self, ctx: GuestContext, date: str, guests: int, meal: str | None = None, area: str | None = None
    ) -> dict[str, Any]:
        result = await self._booking.availability(date)
        if not result.ok:
            return result.error()
        times = []
        for slot in result.data.get("slots", []):
            if meal and slot.get("meal") != meal:
                continue
            free = free_tables(self._profile, set(slot.get("taken", [])), guests, area)
            times.append(
                {
                    "time": slot.get("time"),
                    "meal": slot.get("meal"),
                    "free_tables": len(free),
                    "free_areas": sorted({t.area for t in free}),
                }
            )
        return {
            "ok": True,
            "date": date,
            "guests": guests,
            "times": times,
            "note": "free_tables = 0 means that time is full for this party.",
        }

    async def _create_booking(
        self,
        ctx: GuestContext,
        date: str,
        time: str,
        guests: int,
        name: str,
        email: str | None = None,
        note: str | None = None,
        occasion: str = "none",
        area: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        guest_phone = self._phone(ctx, phone)
        if not guest_phone:
            return {"ok": False, "code": "phone_needed", "message": "Ask the guest for a valid phone number."}
        availability = await self._booking.availability(date)
        if not availability.ok:
            return availability.error()
        slot = next((s for s in availability.data.get("slots", []) if s.get("time") == time), None)
        if slot is None:
            return {"ok": False, "code": "time_not_bookable", "message": f"{time} can't be booked on {date}."}
        candidates = free_tables(self._profile, set(slot.get("taken", [])), guests)
        # Preferred area first, then the smallest table that fits.
        candidates.sort(key=lambda t: (0 if area and t.area == area else 1, t.seats))
        if not candidates:
            return {"ok": False, "code": "full", "message": "No table suits this party at that time."}
        for table in candidates[:3]:  # a table can be taken between the check and the booking
            body: dict[str, Any] = {
                "date": date,
                "time": time,
                "guests": guests,
                "occasion": occasion or "none",
                "tableId": table.id,
                "name": name,
                "phone": guest_phone,
            }
            if email:
                body["email"] = email
            if note:
                body["note"] = note[:200]
            result = await self._booking.create(body)
            if result.status == 201:
                booking = result.data.get("booking", {})
                return {
                    "ok": True,
                    "code": booking.get("code"),
                    "date": booking.get("date"),
                    "time": booking.get("time"),
                    "guests": booking.get("guests"),
                    "name": booking.get("name"),
                    "table": table.label,
                    "area": self._profile.area_label(table.area),
                    "area_preference_met": not area or table.area == area,
                    "confirmation_email_sent": booking.get("emailSent", False),
                    "manage_url": self._profile.links.manage_booking,
                }
            if result.status != 409:
                return result.error()
        return {"ok": False, "code": "full", "message": "Those tables were just taken. Offer another time."}

    async def _find_booking(self, ctx: GuestContext, code: str, phone: str | None = None) -> dict[str, Any]:
        guest_phone = self._phone(ctx, phone)
        if not guest_phone:
            return {"ok": False, "code": "phone_needed", "message": "Ask for the phone number used to book."}
        result = await self._booking.lookup(code.strip().upper(), guest_phone)
        if not result.ok:
            return result.error()
        booking = result.data.get("booking", {})
        table = next((t for t in self._profile.booking.tables if t.id == booking.get("tableId")), None)
        return {
            "ok": True,
            "code": booking.get("code"),
            "date": booking.get("date"),
            "time": booking.get("time"),
            "guests": booking.get("guests"),
            "name": booking.get("name"),
            "occasion": booking.get("occasion"),
            "table": table.label if table else booking.get("tableId"),
            "upcoming": booking.get("upcoming"),
            "has_preorder": bool(booking.get("preorder")),
        }

    async def _cancel_booking(self, ctx: GuestContext, code: str, phone: str | None = None) -> dict[str, Any]:
        guest_phone = self._phone(ctx, phone)
        if not guest_phone:
            return {"ok": False, "code": "phone_needed", "message": "Ask for the phone number used to book."}
        code = code.strip().upper()
        result = await self._booking.cancel(code, guest_phone)
        if not result.ok:
            return result.error()
        return {"ok": True, "cancelled": True, "code": code}

    async def _join_waitlist(
        self,
        ctx: GuestContext,
        date: str,
        meal: str,
        guests: int,
        name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        guest_phone = self._phone(ctx, phone)
        if not guest_phone:
            return {"ok": False, "code": "phone_needed", "message": "Ask the guest for a valid phone number."}
        body: dict[str, Any] = {"date": date, "meal": meal, "guests": guests, "name": name, "phone": guest_phone}
        if email:
            body["email"] = email
        result = await self._booking.join_waitlist(body)
        if not result.ok:
            return result.error()
        return {"ok": True, "waiting": True}

    async def _handoff_to_team(self, ctx: GuestContext, reason: str) -> dict[str, Any]:
        ctx.handoff_reason = reason[:300]
        return {
            "ok": True,
            "message": "The team has been told and will reply in this chat. Tell the guest so, briefly.",
        }
