"""The team takes a chat over; the agent steps back in only when a guest waits too long."""

from tests.conftest import echo_payload, inbound_payload, post_webhook

GUEST = "21620123456"


async def _conversation(container):
    return (await container.conversations.summaries())[0]


async def test_team_message_takes_the_chat_over(client, clock, sender, container):
    await post_webhook(client, inbound_payload(GUEST, "Une table pour 4 dimanche ?", "wamid.1", clock.now))
    assert len(sender.sent) == 1

    clock.advance(minutes=2)
    await post_webhook(client, echo_payload(GUEST, "Bonjour Lea, je m'en occupe !", "wamid.team-1", clock.now))
    assert (await _conversation(container)).status == "team"

    clock.advance(minutes=1)
    await post_webhook(client, inbound_payload(GUEST, "Merci ! Et en terrasse ?", "wamid.2", clock.now))
    assert len(sender.sent) == 1  # the agent stays out
    assert (await _conversation(container)).status == "waiting"


async def test_agent_steps_in_after_the_delay(client, clock, sender, responder, container):
    await post_webhook(client, echo_payload(GUEST, "Bonjour, ici l'équipe", "wamid.team-1", clock.now))
    clock.advance(minutes=1)
    await post_webhook(client, inbound_payload(GUEST, "Vous êtes là ?", "wamid.1", clock.now))
    assert sender.sent == []

    clock.advance(minutes=29)
    assert await container.conversations.sweep() == 0

    clock.advance(minutes=2)
    assert await container.conversations.sweep() == 1
    assert sender.sent == [(GUEST, responder.text)]
    assert responder.calls[-1]["stepping_in"] is True
    assert await container.conversations.sweep() == 0  # answered once only
    assert (await _conversation(container)).status == "team"


async def test_team_reply_in_time_cancels_the_safety_net(client, clock, sender, container):
    await post_webhook(client, echo_payload(GUEST, "Bonjour", "wamid.team-1", clock.now))
    await post_webhook(client, inbound_payload(GUEST, "Une question", "wamid.1", clock.now))
    clock.advance(minutes=10)
    await post_webhook(client, echo_payload(GUEST, "Oui ?", "wamid.team-2", clock.now))
    clock.advance(minutes=40)
    assert await container.conversations.sweep() == 0
    assert sender.sent == []


async def test_the_agents_own_messages_never_count_as_the_team(client, clock, sender, container):
    await post_webhook(client, inbound_payload(GUEST, "Hi", "wamid.1", clock.now))
    own_id = "wamid.out-1"  # the id FakeSender returned for the agent's reply
    await post_webhook(client, echo_payload(GUEST, "Ciao! How can I help?", own_id, clock.now))
    assert (await _conversation(container)).status == "agent"


async def test_chat_returns_to_the_agent_after_the_takeover_period(client, clock, sender, container):
    await post_webhook(client, echo_payload(GUEST, "Bonjour", "wamid.team-1", clock.now))
    clock.advance(hours=13)
    await post_webhook(client, inbound_payload(GUEST, "Re-bonjour", "wamid.1", clock.now))
    assert len(sender.sent) == 1


async def test_guest_asking_for_a_person_goes_to_the_team(client, clock, sender, responder, container):
    responder.handoff = "Wants to talk about a private event"
    await post_webhook(client, inbound_payload(GUEST, "I'd like to speak to someone", "wamid.1", clock.now))
    summary = await _conversation(container)
    assert summary.status == "attention"

    responder.handoff = None
    await post_webhook(client, inbound_payload(GUEST, "Hello?", "wamid.2", clock.now))
    assert len(sender.sent) == 1


async def test_hand_back_answers_a_waiting_guest(client, clock, sender, container):
    await post_webhook(client, echo_payload(GUEST, "Bonjour", "wamid.team-1", clock.now))
    await post_webhook(client, inbound_payload(GUEST, "Toujours là ?", "wamid.1", clock.now))
    conversation_id = (await _conversation(container)).id

    assert await container.conversations.hand_back(conversation_id) == "sent"
    assert len(sender.sent) == 1
    assert (await _conversation(container)).status == "agent"


async def test_old_conversations_are_forgotten(client, clock, container):
    await post_webhook(client, inbound_payload(GUEST, "Hi", "wamid.1", clock.now))
    clock.advance(hours=47)
    assert await container.conversations.purge() == 0
    clock.advance(hours=2)
    assert await container.conversations.purge() == 1
    assert await container.conversations.summaries() == []


async def test_internal_sweep_endpoint_needs_its_token(client):
    assert (await client.post("/internal/sweep")).status_code == 403
    ok = await client.post("/internal/sweep", headers={"X-Internal-Token": "internal-token"})
    assert ok.json() == {"answered": 0, "removed": 0}


async def test_team_message_delivered_late_still_leaves_the_guest_waiting(client, clock, sender, container):
    sender.configured = False  # the agent can't reply, so the guest's message stays unanswered
    sent_at = clock.now
    clock.advance(minutes=1)
    await post_webhook(client, inbound_payload(GUEST, "Et en terrasse ?", "wamid.1", clock.now))
    await post_webhook(client, echo_payload(GUEST, "Bonjour, ici Giulia", "wamid.team-1", sent_at))
    summary = await _conversation(container)
    assert summary.status == "waiting"
    assert summary.last_text == "Et en terrasse ?"  # messages are shown in the order they were sent


async def test_late_team_message_after_the_agent_answered(client, clock, sender, container):
    sent_at = clock.now
    clock.advance(minutes=1)
    await post_webhook(client, inbound_payload(GUEST, "Et en terrasse ?", "wamid.1", clock.now))
    await post_webhook(client, echo_payload(GUEST, "Bonjour, ici Giulia", "wamid.team-1", sent_at))
    summary = await _conversation(container)
    assert summary.status == "team"
    assert summary.last_role == "agent"


async def test_simultaneous_events_for_one_guest(container, clock):
    import asyncio

    from app.whatsapp.events import InboundMessage, TeamMessage

    team = TeamMessage(GUEST, "wamid.team-1", "Bonjour", clock.now)
    guest = InboundMessage(GUEST, "Lea", "wamid.1", "Merci", clock.now)
    results = await asyncio.gather(
        container.conversations.handle_events([team]), container.conversations.handle_events([guest])
    )
    assert results == [["team_took_over"], ["team_has_chat"]]


async def test_conversation_created_by_another_worker_is_reused(container):
    async with container.sessions() as session, session.begin():
        first = await container.conversations._insert_or_fetch(session, GUEST, "Lea", False)
    async with container.sessions() as session, session.begin():
        again = await container.conversations._insert_or_fetch(session, GUEST, "Lea", False)
    assert again.id == first.id


async def test_sweep_respects_pause(client, clock, sender, container):
    await post_webhook(client, echo_payload(GUEST, "Bonjour", "wamid.team-1", clock.now))
    await post_webhook(client, inbound_payload(GUEST, "Allô ?", "wamid.1", clock.now))
    runtime = await container.runtime_settings.load()
    runtime.enabled = False
    await container.runtime_settings.save(runtime)
    clock.advance(minutes=45)
    assert await container.conversations.sweep() == 0
    assert sender.sent == []
