# /// script
# requires-python = "==3.11.*"
# dependencies = [
#   "codewords-client==0.4.11",
#   "fastapi==0.116.1",
#   "anthropic==0.62.0",
#   "httpx==0.28.1",
#   "tzdata==2025.2"
# ]
# [tool.env-checker]
# env_vars = [
#   "PORT=8000",
#   "LOGLEVEL=INFO",
#   "CODEWORDS_API_KEY",
#   "CODEWORDS_RUNTIME_URI"
# ]
# ///
"""
GIRA WhatsApp Agent — answers GIRA (Pasta & Mare) customers on WhatsApp and takes their bookings.

How it behaves
--------------
* Customers write to the restaurant's WhatsApp number (any number, linked with /connect).
* The team answers first. If a customer's message stays unanswered by the team for
  `reply_after_minutes` (default 30), the agent answers it. 0 = the agent answers at once.
* When the team replies by hand from the phone, the waiting message counts as answered.
* The agent knows the menu, hours, address and house rules, checks real availability, books a
  table, finds or cancels a booking and adds guests to the waiting list — through GIRA's own API.
* It replies in the customer's language, in DMs only (never in groups).

Endpoints
---------
POST /            maintenance run: answers messages that waited too long + returns status
                  (the scheduler calls this every 5 minutes)
POST /connect     link a WhatsApp number (returns a pairing code) and route its messages here
POST /settings    change the delay, pause/resume the agent, change the GIRA site URL
POST /webhook     inbound WhatsApp events (called by the WhatsApp connection)
POST /chat        talk to the agent without WhatsApp (testing)
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from textwrap import dedent
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from anthropic import AsyncAnthropic
from codewords_client import AsyncCodewordsClient, logger, redis_client, run_service
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ──────────────────────────────────────────────────────────────────────────────
# Configuration defaults (all overridable at runtime through /settings)
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_GIRA_URL = "https://gira-lyart.vercel.app"
DEFAULT_REPLY_AFTER_MINUTES = 30
MODEL = "claude-sonnet-4-6"
TZ = ZoneInfo("Europe/Paris")

HISTORY_TTL_S = 48 * 3600          # conversations are forgotten after 48 h (privacy)
HISTORY_MAX = 30                   # messages kept per conversation
STALE_AFTER_S = 600                # inbound older than this = reconnect backlog, ignored
HANDOFF_TTL_S = 12 * 3600          # after "I want a human", the agent stays quiet 12 h
MAX_SWEEP_REPLIES = 20             # replies per maintenance run
MAX_AGENT_STEPS = 8

# ──────────────────────────────────────────────────────────────────────────────
# GIRA business facts (mirrors D:\GIRA\src\content\*.ts — update both together)
# ──────────────────────────────────────────────────────────────────────────────

TABLES = [
    {"id": "C1", "label": "Counter 1", "area": "counter", "size": 2},
    {"id": "C2", "label": "Counter 2", "area": "counter", "size": 2},
    {"id": "C3", "label": "Counter 3", "area": "counter", "size": 2},
    {"id": "1", "label": "Table 1", "area": "window", "size": 4},
    {"id": "2", "label": "Table 2", "area": "window", "size": 2},
    {"id": "3", "label": "Table 3", "area": "room", "size": 4},
    {"id": "4", "label": "Table 4", "area": "room", "size": 6},
    {"id": "5", "label": "Table 5", "area": "room", "size": 2},
    {"id": "6", "label": "Table 6", "area": "room", "size": 2},
    {"id": "7", "label": "Table 7", "area": "room", "size": 4},
    {"id": "8", "label": "Table 8", "area": "terrace", "size": 4},
    {"id": "9", "label": "Table 9", "area": "terrace", "size": 2},
    {"id": "10", "label": "Table 10", "area": "terrace", "size": 2},
]
AREAS = {
    "window": "Window (by the tall windows)",
    "room": "Main room (in the middle of it all)",
    "counter": "Chef's counter (watch the pasta being twirled)",
    "terrace": "Terrace (under the olive trees)",
}

BUSINESS_FACTS = dedent("""\
    RESTAURANT
    GIRA — Pasta & Mare. A small Italian kitchen: pasta rolled by hand every morning (12 shapes),
    seafood bought at the fish market at 6 a.m., twirled to order. 40 seats, never more.
    Address: 12 Rue de la Mer, 75000 Paris. Map: https://www.google.com/maps/search/?api=1&query=12+Rue+de+la+Mer+Paris
    Phone: +33 1 00 00 00 00 · Email: ciao@gira.example
    Website: {site}

    OPENING HOURS (Paris time)
    Monday: closed
    Tuesday–Thursday: 12:00–14:30 and 19:00–22:30
    Friday: 12:00–14:30 and 19:00–23:00
    Saturday: 12:00–15:00 and 19:00–23:00
    Sunday: 12:00–15:30 (lunch only)

    MENU (prices in EUR; the menu follows the market, a few dishes change every week)
    Pasta — made every morning, cooked to order
    - Spaghetti al pomodoro, 14 — San Marzano tomato, garlic confit, torn basil, a prawn on top
    - Linguine allo scoglio, 24 — mussels, prawns, cherry tomatoes, garlic. Our signature plate
    - Paccheri alla Norma, 16 — fried aubergine, slow tomato sugo, ricotta salata. Vegetarian
    - Tagliatelle ai frutti di mare, 22 — hand-cut ribbons, white ragù of the day's catch, lemon zest
    Mare — whatever the boats brought in this morning
    - Gamberi all'aglio, 18 — prawns seared with garlic, chilli and parsley, bread for the sauce
    - Cozze alla tarantina, 15 — mussels in a spicy tomato broth, grilled sourdough
    - Frittura di paranza, 19 — small fish of the day, lightly fried, lemon
    Antipasti — to share while the water boils
    - Pomodorini e burrata, 12 — cherry tomatoes, creamy burrata, basil oil. Vegetarian
    - Bruschetta al pomodoro, 8 — grilled bread, raw tomato, garlic, oregano. Vegan
    - Polpo e patate, 14 — tender octopus, crushed potatoes, olives, parsley
    Dolci
    - Tiramisù della casa, 8 — mascarpone, espresso, savoiardi, cocoa
    - Panna cotta agli agrumi, 7 — vanilla cream, Sicilian citrus
    - Cannolo siciliano, 6 — crisp shell, sheep's ricotta, pistachio

    CREA IL TUO (build your own plate): choose a pasta (linguine, spaghetti or paccheri), a sauce
    (pomodoro, bianco, nero di seppia or pesto), up to three treasures from the sea (prawns, mussels,
    clams…) and extras like burrata. Price and allergens update live: {site}/crea-il-tuo
    Plates built there can be pre-ordered with a booking from {site}/prenota/gestisci so the kitchen
    has them ready. Nothing is paid online — guests pay at the restaurant.

    BOOKING RULES
    - Parties of 1 to 6 guests. More than 6: ask them to call +33 1 00 00 00 00 for a long table.
    - Bookable up to 14 days ahead, never on Mondays. Times every 30 minutes; the last booking is
      60 minutes before closing. Today, only times at least 30 minutes from now.
    - A table is held for 2 hours.
    - Areas: window, main room, chef's counter (pairs of stools, 2 people), terrace.
    - Occasions the team prepares for: birthday, anniversary, business.
    - Every booking gets a code like GIRA-XXXXXXXX. With the code and the phone number used to book,
      guests can manage or cancel on {site}/prenota/gestisci (no account).
    - When a moment is full, guests can join the waiting list; they are emailed when a table frees up.
    - Personal details are deleted automatically after the evening.
    - Booking online: {site}/prenota
""")


def system_prompt(site: str, customer_phone: str | None, customer_name: str | None) -> str:
    now = datetime.now(TZ)
    who = f"WhatsApp number {customer_phone}" if customer_phone else "a WhatsApp contact whose number is hidden"
    if customer_name:
        who += f", WhatsApp name \"{customer_name}\""
    return dedent("""\
        You are the WhatsApp host of GIRA — Pasta & Mare, answering customers on the restaurant's
        behalf. Warm, brief, a little Italian charm (a "ciao" or "grazie" is welcome), never pushy.

        Right now it is {now} in Paris ({weekday}). Today's date is {today}.
        You are talking with {who}.

        HOW TO ANSWER
        - Always reply in the customer's language (the language of their last message).
        - WhatsApp style: short paragraphs, *single asterisks* for bold, simple lists, plain URLs.
          No markdown headings, no tables. Usually under 80 words.
        - Only state facts from the RESTAURANT FACTS below or from your tools. If you don't know
          (allergies in detail, drinks, parking, private events, dietary specifics beyond the menu
          tags), say the team will confirm, and offer to pass the question on (handoff_to_staff).
        - Never mention, suggest or recommend wine or any alcohol.
        - Some earlier assistant turns may start with "[Staff]": those were written by a person from
          the team. Stay consistent with them.

        BOOKINGS
        - To book you need: date, time, number of guests, and a name. Optional: area preference,
          occasion, email (for a confirmation email), a note (allergies, high chair…).
        - Resolve relative dates ("tomorrow", "Saturday") to YYYY-MM-DD using today's date.
        - Always check_availability first, then propose real free times.
        - Before create_booking, summarise the booking (day, time, guests, name, area if any) and get
          an explicit yes. After booking, give the booking code and say it's needed at the door and
          to manage the booking.
        - The booking phone is the customer's WhatsApp number unless it is hidden — then ask for it.
        - To look up or cancel, ask for the booking code. Bookings can only be managed from the phone
          number they were made with. Before cancel_booking, get an explicit yes.
        - If the moment is full, offer other times or the waiting list (join_waitlist; an email is
          needed to be notified).
        - If the customer asks for a human, is upset, or needs something you cannot do, use
          handoff_to_staff and tell them someone from the team will reply here.

        RESTAURANT FACTS
        {facts}
    """).format(
        now=now.strftime("%H:%M"),
        weekday=now.strftime("%A"),
        today=now.strftime("%Y-%m-%d"),
        who=who,
        facts=BUSINESS_FACTS.format(site=site),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────


def wa_number(jid: str) -> str:
    """"336...:3@s.whatsapp.net" / "336...@lid" -> "336..." (digits only)."""
    return "".join(c for c in (jid or "").split("@")[0].split(":")[0] if c.isdigit())


def customer_phone_from_jid(jid: str) -> str | None:
    """The real phone number, in +E.164, when WhatsApp exposes it (not for hidden @lid contacts)."""
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@c.us"):
        digits = wa_number(jid)
        return f"+{digits}" if digits else None
    return None


def send_target(jid: str) -> str:
    """Where to send a reply: the bare number for normal chats, the full JID for hidden (@lid) ones."""
    return jid if jid.endswith("@lid") else wa_number(jid)


def mask(phone: str | None) -> str:
    return (phone[:5] + "****") if phone else "?"


async def load_settings() -> dict[str, str]:
    async with redis_client() as (redis, ns):
        raw = await redis.hgetall(f"{ns}:gira:settings") or {}
    settings = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }
    settings.setdefault("gira_url", DEFAULT_GIRA_URL)
    settings.setdefault("reply_after_minutes", str(DEFAULT_REPLY_AFTER_MINUTES))
    settings.setdefault("enabled", "1")
    return settings


async def send_whatsapp(phone_id: str, to: str, message: str) -> bool:
    logger.info("STEPLOG START send_whatsapp")
    async with AsyncCodewordsClient() as cw:
        resp = await cw.run(
            service_id="whatsapp_device_manager",
            path=f"/proxy/send/message?phone_id={phone_id}",
            inputs={"phone": to, "message": message},
        )
    ok = 200 <= resp.status_code < 300
    if not ok:
        logger.error("WhatsApp send failed", status=resp.status_code, to=mask(to))
    return ok


# ── Conversation memory (Redis, 48 h) ─────────────────────────────────────────


async def history_append(chat: str, role: str, text: str) -> None:
    async with redis_client() as (redis, ns):
        key = f"{ns}:gira:hist:{chat}"
        await redis.rpush(key, json.dumps({"role": role, "text": text[:2000], "at": int(time.time())}))
        await redis.ltrim(key, -HISTORY_MAX, -1)
        await redis.expire(key, HISTORY_TTL_S)


async def history_get(chat: str) -> list[dict[str, Any]]:
    async with redis_client() as (redis, ns):
        items = await redis.lrange(f"{ns}:gira:hist:{chat}", 0, -1) or []
    return [json.loads(i) for i in items]


def to_claude_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role turns and make sure the conversation starts with the customer."""
    msgs: list[dict[str, Any]] = []
    for h in history:
        role = "user" if h["role"] == "user" else "assistant"
        text = h["text"] if h["role"] != "staff" else f"[Staff] {h['text']}"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


# ──────────────────────────────────────────────────────────────────────────────
# GIRA API tools
# ──────────────────────────────────────────────────────────────────────────────


class Gira:
    """Thin client for GIRA's public booking API. Writes carry the site's own Origin header."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.headers = {"Origin": self.base, "Content-Type": "application/json"}

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.request(method, f"{self.base}{path}", json=body, headers=self.headers)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data


def fits(size: int, guests: int) -> bool:
    """Same rule as the website: a table fits without wasting more than two seats."""
    return size >= guests and size - guests <= 2


def error_of(status: int, data: dict[str, Any]) -> dict[str, Any]:
    err = data.get("error") or {}
    out = {"ok": False, "status": status, "code": err.get("code", "error"), "message": err.get("message", "Request failed.")}
    if err.get("fields"):
        out["fields"] = err["fields"]
    return out


async def tool_check_availability(gira: Gira, date: str, guests: int, meal: str | None = None,
                                  area: str | None = None, **_: Any) -> dict[str, Any]:
    status, data = await gira.call("GET", f"/api/availability?date={date}")
    if status != 200:
        return error_of(status, data)
    times = []
    for slot in data.get("slots", []):
        if meal and slot.get("meal") != meal:
            continue
        taken = set(slot.get("taken", []))
        free = [t for t in TABLES if fits(t["size"], guests) and t["id"] not in taken and (not area or t["area"] == area)]
        times.append({
            "time": slot["time"],
            "meal": slot["meal"],
            "free_tables": len(free),
            "free_areas": sorted({t["area"] for t in free}),
        })
    return {"ok": True, "date": date, "guests": guests, "times": times,
            "note": "Times with free_tables = 0 are full for this party."}


async def tool_create_booking(gira: Gira, ctx: dict[str, Any], date: str, time: str, guests: int, name: str,
                              phone: str | None = None, email: str | None = None, note: str | None = None,
                              occasion: str = "none", area: str | None = None, **_: Any) -> dict[str, Any]:
    phone = ctx.get("customer_phone") or phone
    if not phone:
        return {"ok": False, "code": "phone_needed", "message": "Ask the customer for their phone number."}
    status, data = await gira.call("GET", f"/api/availability?date={date}")
    if status != 200:
        return error_of(status, data)
    slot = next((s for s in data.get("slots", []) if s["time"] == time), None)
    if not slot:
        return {"ok": False, "code": "time_not_bookable", "message": f"{time} is not a bookable time on {date}."}
    taken = set(slot.get("taken", []))
    candidates = [t for t in TABLES if fits(t["size"], guests) and t["id"] not in taken]
    # Preferred area first, then the smallest table that fits.
    candidates.sort(key=lambda t: (0 if area and t["area"] == area else 1, t["size"]))
    if not candidates:
        return {"ok": False, "code": "full", "message": "No table fits this party at that time."}
    for table in candidates[:3]:  # a table can be taken between the check and the booking
        body = {"date": date, "time": time, "guests": guests, "occasion": occasion or "none",
                "tableId": table["id"], "name": name, "phone": phone}
        if email:
            body["email"] = email
        if note:
            body["note"] = note[:200]
        status, res = await gira.call("POST", "/api/bookings", body)
        if status == 201:
            b = res["booking"]
            return {"ok": True, "code": b["code"], "date": b["date"], "time": b["time"], "guests": b["guests"],
                    "table": table["label"], "area": AREAS[table["area"]], "name": b["name"],
                    "confirmation_email_sent": b.get("emailSent", False),
                    "area_preference_met": (not area) or table["area"] == area,
                    "manage_url": f"{gira.base}/prenota/gestisci"}
        if status != 409:
            return error_of(status, res)
    return {"ok": False, "code": "full", "message": "Those tables were just taken. Offer another time."}


async def tool_find_booking(gira: Gira, ctx: dict[str, Any], code: str, phone: str | None = None, **_: Any) -> dict[str, Any]:
    phone = ctx.get("customer_phone") or phone
    if not phone:
        return {"ok": False, "code": "phone_needed", "message": "Ask for the phone number used to book."}
    status, data = await gira.call("POST", "/api/bookings/lookup", {"code": code.strip().upper(), "phone": phone})
    if status != 200:
        return error_of(status, data)
    b = data["booking"]
    table = next((t for t in TABLES if t["id"] == b.get("tableId")), None)
    return {"ok": True, "code": b["code"], "date": b["date"], "time": b["time"], "guests": b["guests"],
            "name": b["name"], "occasion": b.get("occasion"), "table": table["label"] if table else b.get("tableId"),
            "upcoming": b.get("upcoming"), "has_preorder": bool(b.get("preorder"))}


async def tool_cancel_booking(gira: Gira, ctx: dict[str, Any], code: str, phone: str | None = None, **_: Any) -> dict[str, Any]:
    phone = ctx.get("customer_phone") or phone
    if not phone:
        return {"ok": False, "code": "phone_needed", "message": "Ask for the phone number used to book."}
    code = code.strip().upper()
    status, data = await gira.call("DELETE", f"/api/bookings/{code}", {"phone": phone})
    if status != 200:
        return error_of(status, data)
    return {"ok": True, "cancelled": True, "code": code}


async def tool_join_waitlist(gira: Gira, ctx: dict[str, Any], date: str, meal: str, guests: int, name: str,
                             email: str | None = None, phone: str | None = None, **_: Any) -> dict[str, Any]:
    phone = ctx.get("customer_phone") or phone
    if not phone:
        return {"ok": False, "code": "phone_needed", "message": "Ask the customer for their phone number."}
    body = {"date": date, "meal": meal, "guests": guests, "name": name, "phone": phone}
    if email:
        body["email"] = email
    status, data = await gira.call("POST", "/api/waitlist", body)
    if status != 201:
        return error_of(status, data)
    return {"ok": True, "waiting": True}


async def tool_handoff_to_staff(ctx: dict[str, Any], reason: str, **_: Any) -> dict[str, Any]:
    chat = ctx.get("chat")
    if chat and not ctx.get("test"):
        async with redis_client() as (redis, ns):
            await redis.set(f"{ns}:gira:human:{chat}", reason[:300], ex=HANDOFF_TTL_S)
        settings = ctx.get("settings", {})
        owner = settings.get("phone_number")
        if owner and settings.get("phone_id"):
            # A note in the restaurant's own "Message yourself" chat.
            who = ctx.get("customer_phone") or ctx.get("customer_name") or "a customer"
            await send_whatsapp(settings["phone_id"], wa_number(owner),
                                f"🔔 GIRA agent: {who} needs a person from the team.\nReason: {reason}")
    return {"ok": True, "message": "The team has been told and will reply in this chat. Stay quiet until then."}


TOOL_SCHEMAS = [
    {
        "name": "check_availability",
        "description": "List bookable times on a day and how many tables fit the party at each time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "guests": {"type": "integer", "minimum": 1, "maximum": 6},
                "meal": {"type": "string", "enum": ["lunch", "dinner"]},
                "area": {"type": "string", "enum": list(AREAS)},
            },
            "required": ["date", "guests"],
        },
    },
    {
        "name": "create_booking",
        "description": "Book a table. Only after the customer explicitly confirmed the summary. The best free table is chosen automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM, one of the times from check_availability"},
                "guests": {"type": "integer", "minimum": 1, "maximum": 6},
                "name": {"type": "string"},
                "phone": {"type": "string", "description": "Only if the customer's WhatsApp number is hidden. International format."},
                "email": {"type": "string"},
                "note": {"type": "string", "description": "Allergies, high chair, surprise… max 200 chars"},
                "occasion": {"type": "string", "enum": ["none", "birthday", "anniversary", "business"]},
                "area": {"type": "string", "enum": list(AREAS)},
            },
            "required": ["date", "time", "guests", "name"],
        },
    },
    {
        "name": "find_booking",
        "description": "Look up a booking by its code (GIRA-XXXXXXXX), for the customer's own phone number.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "phone": {"type": "string", "description": "Only if the WhatsApp number is hidden."}},
            "required": ["code"],
        },
    },
    {
        "name": "cancel_booking",
        "description": "Cancel a booking. Only after the customer explicitly confirmed they want to cancel.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "phone": {"type": "string", "description": "Only if the WhatsApp number is hidden."}},
            "required": ["code"],
        },
    },
    {
        "name": "join_waitlist",
        "description": "Add the customer to the waiting list for a full lunch or dinner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "meal": {"type": "string", "enum": ["lunch", "dinner"]},
                "guests": {"type": "integer", "minimum": 1, "maximum": 6},
                "name": {"type": "string"},
                "email": {"type": "string", "description": "Needed to be told when a table frees up"},
                "phone": {"type": "string", "description": "Only if the WhatsApp number is hidden."},
            },
            "required": ["date", "meal", "guests", "name"],
        },
    },
    {
        "name": "handoff_to_staff",
        "description": "Pass the conversation to a person from the team (customer asks for a human, complaint, question you cannot answer).",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "One line for the team"}},
            "required": ["reason"],
        },
    },
]


async def run_tool(name: str, args: dict[str, Any], gira: Gira, ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "check_availability":
            return await tool_check_availability(gira, **args)
        if name == "create_booking":
            return await tool_create_booking(gira, ctx, **args)
        if name == "find_booking":
            return await tool_find_booking(gira, ctx, **args)
        if name == "cancel_booking":
            return await tool_cancel_booking(gira, ctx, **args)
        if name == "join_waitlist":
            return await tool_join_waitlist(gira, ctx, **args)
        if name == "handoff_to_staff":
            return await tool_handoff_to_staff(ctx, **args)
        return {"ok": False, "message": f"Unknown tool {name}"}
    except (httpx.HTTPError, TypeError, KeyError) as e:
        logger.error("Tool failed", tool=name, error=f"{type(e).__name__}: {e}")
        return {"ok": False, "code": "tool_error", "message": "The booking system could not be reached. Apologise and suggest the website or calling."}


async def generate_reply(history: list[dict[str, Any]], ctx: dict[str, Any]) -> tuple[str, list[str]]:
    """Run the Claude tool loop over the conversation. Returns the reply text and the tools used."""
    logger.info("STEPLOG START generate_reply")
    settings = ctx["settings"]
    gira = Gira(settings["gira_url"])
    system = system_prompt(settings["gira_url"], ctx.get("customer_phone"), ctx.get("customer_name"))
    messages = to_claude_messages(history)
    if not messages:
        return "", []
    client = AsyncAnthropic()
    used: list[str] = []
    for _ in range(MAX_AGENT_STEPS):
        resp = await client.messages.create(model=MODEL, max_tokens=1024, system=system,
                                            messages=messages, tools=TOOL_SCHEMAS)
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return text, used
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                used.append(block.name)
                out = await run_tool(block.name, dict(block.input or {}), gira, ctx)
                logger.info("Tool used", tool=block.name, ok=out.get("ok"))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})
    return "Scusi — I need a moment on this one. Someone from the team will reply here shortly.", used


# ──────────────────────────────────────────────────────────────────────────────
# Answering a conversation
# ──────────────────────────────────────────────────────────────────────────────


async def answer_chat(chat: str, settings: dict[str, str]) -> str:
    """Generate and send the agent's reply for one conversation. Returns a status string."""
    async with redis_client() as (redis, ns):
        if await redis.get(f"{ns}:gira:human:{chat}"):
            return "handed_to_staff"
        meta_raw = await redis.get(f"{ns}:gira:meta:{chat}")
    meta = json.loads(meta_raw) if meta_raw else {}
    jid = meta.get("jid") or f"{chat}@s.whatsapp.net"
    ctx = {"settings": settings, "chat": chat, "customer_phone": customer_phone_from_jid(jid),
           "customer_name": meta.get("name")}
    history = await history_get(chat)
    if not history or history[-1]["role"] != "user":
        return "nothing_to_answer"
    try:
        reply, used = await generate_reply(history, ctx)
    except Exception as e:  # the LLM or network failed: don't leave the customer in silence
        logger.error("Agent failed", error=f"{type(e).__name__}: {e}")
        reply, used = "Grazie for your message! Our team will get back to you very soon. 🍝", []
    if not reply:
        return "empty_reply"
    if await send_whatsapp(settings["phone_id"], send_target(jid), reply):
        await history_append(chat, "assistant", reply)
        logger.info("Agent replied", chat=mask(chat), tools=used)
        return "sent"
    return "send_failed"


async def sweep(settings: dict[str, str]) -> dict[str, int]:
    """Answer every conversation whose oldest unanswered message has waited long enough."""
    logger.info("STEPLOG START answer_overdue")
    delay_s = int(settings["reply_after_minutes"]) * 60
    now = time.time()
    counts = {"waiting": 0, "answered": 0, "skipped": 0}
    async with redis_client() as (redis, ns):
        pending = await redis.hgetall(f"{ns}:gira:pending") or {}
    due = []
    for k, v in pending.items():
        chat = k.decode() if isinstance(k, bytes) else k
        since = float(v.decode() if isinstance(v, bytes) else v)
        if now - since >= delay_s:
            due.append((since, chat))
        else:
            counts["waiting"] += 1
    for _, chat in sorted(due)[:MAX_SWEEP_REPLIES]:
        async with redis_client() as (redis, ns):
            # Delete before sending, so an overlapping run can't answer twice.
            if not await redis.hdel(f"{ns}:gira:pending", chat):
                continue
        status = await answer_chat(chat, settings)
        counts["answered" if status == "sent" else "skipped"] += 1
    return counts


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GIRA WhatsApp Agent",
    description="Answers GIRA — Pasta & Mare customers on WhatsApp and takes their bookings.",
    version="1.0.0",
)


class StatusResponse(BaseModel):
    enabled: bool = Field(..., description="Whether the agent is answering")
    connected_number: str | None = Field(None, description="WhatsApp number the agent answers on (masked)")
    connection_status: str | None = Field(None, description="logged_in / logged_out")
    reply_after_minutes: int = Field(..., description="How long the team has to reply before the agent does")
    gira_url: str
    answered_now: int = Field(0, description="Overdue conversations answered during this run")
    still_waiting: int = Field(0, description="Conversations still inside the team's reply window")


class MaintenanceRequest(BaseModel):
    answer_overdue: bool = Field(True, description="Answer the messages the team left unanswered for too long")


@app.post("/", response_model=StatusResponse)
async def maintenance(req: MaintenanceRequest) -> StatusResponse:
    """Answer messages the team left unanswered for too long, and report the agent's status.

    The scheduler calls this every 5 minutes.
    """
    settings = await load_settings()
    counts = {"answered": 0, "waiting": 0}
    connection_status = None
    if settings.get("phone_id"):
        async with AsyncCodewordsClient() as cw:
            resp = await cw.run(service_id="whatsapp_device_manager",
                                path=f"/connections/{settings['phone_id']}", method="GET")
        if resp.status_code == 200:
            connection_status = resp.json().get("status")
        if settings["enabled"] == "1" and req.answer_overdue:
            counts = await sweep(settings)
    return StatusResponse(
        enabled=settings["enabled"] == "1",
        connected_number=mask(settings.get("phone_number")) if settings.get("phone_number") else None,
        connection_status=connection_status,
        reply_after_minutes=int(settings["reply_after_minutes"]),
        gira_url=settings["gira_url"],
        answered_now=counts.get("answered", 0),
        still_waiting=counts.get("waiting", 0),
    )


class ConnectRequest(BaseModel):
    phone_number: str = Field(..., description="The restaurant's WhatsApp number, international format", example="+33612345678")
    service_id: str | None = Field(None, description="This service's id (only if the platform doesn't provide SERVICE_ID)")
    force_repoint: bool = Field(False, description="Take the number over even if it already feeds another bot")


class ConnectResponse(BaseModel):
    status: str
    pair_code: str | None = Field(None, description="Enter in WhatsApp → Linked devices → Link with phone number instead")
    message: str


@app.post("/connect", response_model=ConnectResponse)
async def connect(req: ConnectRequest) -> ConnectResponse:
    """Link a WhatsApp number to the agent (or switch to another number)."""
    logger.info("STEPLOG START connect_number")
    number = "".join(c for c in req.phone_number if c.isdigit() or c == "+")
    if not number.startswith("+") or len(number) < 9:
        raise HTTPException(status_code=400, detail="Use the full international number, e.g. +33612345678.")
    service_id = req.service_id or os.environ.get("SERVICE_ID")
    if not service_id:
        raise HTTPException(status_code=400, detail="Pass service_id (this service's id).")
    service_path = f"{service_id}/webhook"

    async with AsyncCodewordsClient() as cw:
        resp = await cw.run(service_id="whatsapp_device_manager", path="/connections",
                            inputs={"phone_number": number})
        data = resp.json() if resp.content else {}
        if resp.status_code == 409:
            raise HTTPException(status_code=409,
                                detail=f"This number is already linked ({json.dumps(data.get('detail', data))[:200]}). Nothing changed.")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Could not connect the number ({resp.status_code}).")
        phone_id = data["phone_id"]

        current = await cw.run(service_id="whatsapp_device_manager", path=f"/connections/{phone_id}", method="GET")
        existing = (current.json() or {}).get("service_path", "") if current.status_code == 200 else ""
        if existing and existing != service_path and not req.force_repoint:
            raise HTTPException(status_code=409,
                                detail="This number already feeds another bot. Re-run with force_repoint=true to switch it here.")
        sub = await cw.run(service_id="whatsapp_device_manager", path=f"/connections/{phone_id}/subscribe",
                           method="PUT", inputs={"service_path": service_path})
        if sub.status_code != 200:
            raise HTTPException(status_code=502,
                                detail=f"Paired, but routing messages failed ({sub.status_code}). Run /connect again.")

    async with redis_client() as (redis, ns):
        await redis.hset(f"{ns}:gira:settings", mapping={"phone_id": phone_id, "phone_number": number})
    logger.info("WhatsApp number linked", number=mask(number))
    return ConnectResponse(status="pair_code_issued", pair_code=data.get("pair_code"),
                           message="On the restaurant's phone: WhatsApp → Settings → Linked devices → Link a device → "
                                   "'Link with phone number instead', then type this code (it expires in ~30 s).")


class SettingsRequest(BaseModel):
    reply_after_minutes: int | None = Field(None, ge=0, le=1440, description="Minutes the team has to reply first (0 = agent answers at once)")
    enabled: bool | None = Field(None, description="Pause (false) or resume (true) the agent")
    gira_url: str | None = Field(None, description="GIRA site the agent books on, e.g. https://gira-lyart.vercel.app")


@app.post("/settings")
async def update_settings(req: SettingsRequest) -> dict[str, Any]:
    """Change how the agent behaves, without redeploying."""
    changes: dict[str, str] = {}
    if req.reply_after_minutes is not None:
        changes["reply_after_minutes"] = str(req.reply_after_minutes)
    if req.enabled is not None:
        changes["enabled"] = "1" if req.enabled else "0"
    if req.gira_url:
        changes["gira_url"] = req.gira_url.rstrip("/")
    if changes:
        async with redis_client() as (redis, ns):
            await redis.hset(f"{ns}:gira:settings", mapping=changes)
    s = await load_settings()
    return {"enabled": s["enabled"] == "1", "reply_after_minutes": int(s["reply_after_minutes"]),
            "gira_url": s["gira_url"], "connected_number": mask(s.get("phone_number")) if s.get("phone_number") else None}


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, str]:
    """Inbound WhatsApp events for the restaurant's number."""
    logger.info("STEPLOG START receive_message")
    body = await request.json()
    data = body.get("payload") or {}
    if body.get("event") != "message":
        return {"status": "skipped", "reason": "non-message event"}
    chat_id = data.get("chat_id", "") or ""
    if chat_id.endswith("@g.us") or chat_id.endswith("@newsletter") or chat_id.endswith("@broadcast"):
        return {"status": "skipped", "reason": "not a direct chat"}
    chat = wa_number(chat_id)
    text = (data.get("body") or "").strip()
    if not chat:
        return {"status": "skipped", "reason": "no chat"}

    msg_id = data.get("id", "")
    async with redis_client() as (redis, ns):
        if msg_id and not await redis.set(f"{ns}:gira:seen:{msg_id}", "1", nx=True, ex=86400):
            return {"status": "skipped", "reason": "duplicate delivery"}
    ts = data.get("timestamp", "")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
            if age > STALE_AFTER_S:
                return {"status": "skipped", "reason": "stale backlog message"}
        except ValueError:
            pass

    settings = await load_settings()
    owner = wa_number(body.get("device_id", "")) or wa_number(settings.get("phone_number", ""))

    # The team typed on the restaurant's phone.
    if data.get("is_from_me"):
        if chat == owner:
            return {"status": "skipped", "reason": "owner's own notes"}
        if text:
            await history_append(chat, "staff", text)
        async with redis_client() as (redis, ns):
            await redis.hdel(f"{ns}:gira:pending", chat)  # the customer got a human answer
        return {"status": "recorded", "reason": "team replied"}

    if chat == owner:
        return {"status": "skipped", "reason": "self chat"}

    if not text:
        if any(data.get(k) for k in ("audio", "image", "video", "document")):
            text = "[The customer sent a voice note, photo or file that you can't open. Kindly ask them to write it.]"
        else:
            return {"status": "skipped", "reason": "no text"}

    await history_append(chat, "user", text)
    async with redis_client() as (redis, ns):
        await redis.set(f"{ns}:gira:meta:{chat}", json.dumps({"jid": data.get("from") or chat_id,
                                                               "name": data.get("from_name")}), ex=HISTORY_TTL_S)
        human = await redis.get(f"{ns}:gira:human:{chat}")
        if not human:
            # The timer starts at the first unanswered message.
            await redis.hsetnx(f"{ns}:gira:pending", chat, str(time.time()))

    if settings["enabled"] != "1" or not settings.get("phone_id"):
        return {"status": "recorded", "reason": "agent paused or not connected"}
    if human:
        return {"status": "recorded", "reason": "handed to staff"}
    if int(settings["reply_after_minutes"]) == 0:
        async with redis_client() as (redis, ns):
            await redis.hdel(f"{ns}:gira:pending", chat)
        return {"status": await answer_chat(chat, settings)}
    return {"status": "waiting", "reason": f"team has {settings['reply_after_minutes']} min to reply"}


class ChatRequest(BaseModel):
    message: str = Field(..., description="What the customer writes", example="Hi! Do you have a table for 2 tomorrow at 8pm?")
    phone: str = Field("+33600000000", description="The customer's phone number, international format")
    session: str = Field("test", description="Conversation id — reuse it to continue the same chat")
    reset: bool = Field(False, description="Start this conversation over")


class ChatResponse(BaseModel):
    reply: str
    tools_used: list[str]


@app.post("/chat", response_model=ChatResponse)
async def chat_test(req: ChatRequest) -> ChatResponse:
    """Talk to the agent without WhatsApp (bookings it makes are real)."""
    logger.info("STEPLOG START test_chat")
    chat = f"test-{req.session}"
    if req.reset:
        async with redis_client() as (redis, ns):
            await redis.delete(f"{ns}:gira:hist:{chat}")
    await history_append(chat, "user", req.message)
    settings = await load_settings()
    ctx = {"settings": settings, "chat": chat, "customer_phone": req.phone, "customer_name": None, "test": True}
    reply, used = await generate_reply(await history_get(chat), ctx)
    await history_append(chat, "assistant", reply)
    return ChatResponse(reply=reply, tools_used=used)


if __name__ == "__main__":
    run_service(app)
