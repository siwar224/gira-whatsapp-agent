"""The conversation loop with Claude: read the chat, use tools, write the reply."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import anthropic

from app.agent.prompt import static_instructions, turn_context
from app.agent.tools import GuestContext, Toolbox, tool_schemas
from app.business.profile import BusinessProfile
from app.core.config import Settings
from app.db.models import Role

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_STEPS = 8
SORRY = "Thank you for your message! Someone from our team will get back to you here very soon."


class AgentUnavailable(RuntimeError):
    """Claude could not be reached or declined; the guest gets a polite holding message."""


@dataclass(frozen=True)
class Turn:
    role: str  # a Role value
    text: str


@dataclass
class AgentReply:
    text: str
    actions: list[str] = field(default_factory=list)
    handoff_reason: str | None = None


class Responder(Protocol):
    async def reply(
        self, turns: list[Turn], guest: GuestContext, now: datetime, stepping_in: bool = False
    ) -> AgentReply: ...


def to_messages(turns: list[Turn]) -> list[dict[str, Any]]:
    """Chat history as Claude messages: the guest is 'user'; the agent and the team are 'assistant'."""
    messages: list[dict[str, Any]] = []
    for turn in turns:
        role = "user" if turn.role == Role.CUSTOMER else "assistant"
        text = f"[Team] {turn.text}" if turn.role == Role.STAFF else turn.text
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


class ClaudeAgent:
    def __init__(
        self,
        settings: Settings,
        profile: BusinessProfile,
        toolbox: Toolbox,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self._settings = settings
        self._profile = profile
        self._toolbox = toolbox
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value() or None)
        self._tools = tool_schemas(profile)
        self._instructions = static_instructions(profile)

    async def _create(self, system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        params: dict[str, Any] = {
            "model": self._settings.anthropic_model,
            "max_tokens": self._settings.anthropic_max_tokens,
            "system": system,
            "tools": self._tools,
            "messages": messages,
            "output_config": {"effort": self._settings.anthropic_effort},
        }
        if self._settings.anthropic_refusal_fallback:
            return await self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
        return await self._client.messages.create(**params)

    async def reply(
        self, turns: list[Turn], guest: GuestContext, now: datetime, stepping_in: bool = False
    ) -> AgentReply:
        messages = to_messages(turns)
        if not messages:
            return AgentReply(text="")
        system = [
            {"type": "text", "text": self._instructions, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": turn_context(self._profile, now, guest.phone, guest.name, stepping_in)},
        ]
        for _ in range(MAX_STEPS):
            try:
                response = await self._create(system, messages)
            except anthropic.RateLimitError as exc:
                raise AgentUnavailable("Claude rate limit reached") from exc
            except anthropic.APIConnectionError as exc:
                raise AgentUnavailable("Claude could not be reached") from exc
            except anthropic.APIStatusError as exc:
                raise AgentUnavailable(f"Claude API error {exc.status_code}") from exc

            if response.stop_reason == "refusal":
                raise AgentUnavailable("Claude declined this conversation")

            messages.append(
                {"role": "assistant", "content": [block.model_dump(exclude_none=True) for block in response.content]}
            )
            calls = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason != "tool_use" or not calls:
                text = "".join(block.text for block in response.content if block.type == "text").strip()
                return AgentReply(text=text, actions=guest.actions, handoff_reason=guest.handoff_reason)

            results = await asyncio.gather(
                *(self._toolbox.run(call.name, dict(call.input or {}), guest) for call in calls)
            )
            for call, result in zip(calls, results, strict=True):
                log.info("Tool %s -> ok=%s", call.name, result.get("ok"))
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                        for call, result in zip(calls, results, strict=True)
                    ],
                }
            )
        raise AgentUnavailable("The agent needed too many steps")
