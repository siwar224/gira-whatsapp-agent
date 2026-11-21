"""The Claude loop, against a stand-in for the Anthropic client."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agent.runner import AgentUnavailable, ClaudeAgent, Turn, to_messages
from app.agent.tools import GuestContext
from app.db.models import Role


class Block(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def create(self, **params):
        self.requests.append(copy.deepcopy(params))
        return self.responses.pop(0)


class FakeToolbox:
    def __init__(self):
        self.calls = []

    async def run(self, name, args, ctx):
        self.calls.append((name, args))
        ctx.actions.append(name)
        return {"ok": True, "times": [{"time": "20:00", "free_tables": 3}]}


def _client(responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


def test_history_becomes_claude_messages():
    turns = [
        Turn(Role.AGENT, "Leftover from earlier"),
        Turn(Role.CUSTOMER, "Hi"),
        Turn(Role.CUSTOMER, "Table for 2?"),
        Turn(Role.STAFF, "Sure, what time?"),
        Turn(Role.CUSTOMER, "20:00"),
    ]
    assert to_messages(turns) == [
        {"role": "user", "content": "Hi\n\nTable for 2?"},
        {"role": "assistant", "content": "[Team] Sure, what time?"},
        {"role": "user", "content": "20:00"},
    ]


async def test_tool_loop_and_request_shape(settings, profile):
    client, messages = _client(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    Block(
                        type="tool_use", id="tu_1", name="check_availability", input={"date": "2026-11-28", "guests": 2}
                    ),
                ],
            ),
            SimpleNamespace(stop_reason="end_turn", content=[Block(type="text", text="*20:00* is free! Your name?")]),
        ]
    )
    toolbox = FakeToolbox()
    agent = ClaudeAgent(settings, profile, toolbox, client=client)

    reply = await agent.reply(
        [Turn(Role.CUSTOMER, "Table for 2 on the 28th at 8pm?")],
        GuestContext(1, "+33612345678"),
        datetime(2026, 11, 21, 10, tzinfo=UTC),
    )

    assert reply.text == "*20:00* is free! Your name?"
    assert reply.actions == ["check_availability"]
    first, second = messages.requests
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu_1"


async def test_refusal_becomes_unavailable(settings, profile):
    client, _ = _client([SimpleNamespace(stop_reason="refusal", content=[])])
    agent = ClaudeAgent(settings, profile, FakeToolbox(), client=client)
    with pytest.raises(AgentUnavailable):
        await agent.reply([Turn(Role.CUSTOMER, "Hi")], GuestContext(1, None), datetime.now(UTC))


async def test_without_fallback_uses_the_standard_endpoint(settings, profile):
    settings.anthropic_refusal_fallback = False
    client, messages = _client([SimpleNamespace(stop_reason="end_turn", content=[Block(type="text", text="Ciao")])])
    agent = ClaudeAgent(settings, profile, FakeToolbox(), client=client)
    await agent.reply([Turn(Role.CUSTOMER, "Hi")], GuestContext(1, None), datetime.now(UTC))
    assert "fallbacks" not in messages.requests[0]
