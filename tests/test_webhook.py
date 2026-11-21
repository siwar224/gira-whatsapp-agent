from datetime import timedelta

from tests.conftest import inbound_payload, post_webhook


async def test_verification_handshake(client):
    ok = await client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
    )
    assert ok.status_code == 200
    assert ok.text == "42"

    wrong = await client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}
    )
    assert wrong.status_code == 403


async def test_rejects_unsigned_or_badly_signed_events(client, clock, sender):
    payload = inbound_payload("21620123456", "Hi", "wamid.1", clock.now)
    response = await post_webhook(client, payload, secret="someone-else")
    assert response.status_code == 403
    unsigned = await client.post("/webhook", json=payload)
    assert unsigned.status_code == 403
    assert sender.sent == []


async def test_guest_gets_an_instant_reply(client, clock, sender, responder):
    payload = inbound_payload("21620123456", "3aslema, famma blassa?", "wamid.1", clock.now)
    response = await post_webhook(client, payload)

    assert response.status_code == 200
    assert sender.read == ["wamid.1"]
    assert sender.sent == [("21620123456", responder.text)]
    guest = responder.calls[0]["guest"]
    assert guest.phone == "+21620123456"
    assert guest.name == "Lea"
    assert responder.calls[0]["turns"][-1].text == "3aslema, famma blassa?"


async def test_redelivered_message_is_answered_once(client, clock, sender):
    payload = inbound_payload("33612345678", "Bonjour", "wamid.same", clock.now)
    await post_webhook(client, payload)
    await post_webhook(client, payload)
    assert len(sender.sent) == 1


async def test_old_backlog_is_stored_but_not_answered(client, clock, sender, container):
    await post_webhook(client, inbound_payload("393471234567", "Ciao", "wamid.old", clock.now - timedelta(hours=2)))
    assert sender.sent == []
    assert (await container.conversations.summaries())[0].last_text == "Ciao"


async def test_delivery_statuses_and_other_numbers_are_ignored(client, clock, sender, responder):
    statuses = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID-1"},
                            "statuses": [{"id": "wamid.x", "status": "delivered"}],
                        },
                    }
                ]
            }
        ],
    }
    await post_webhook(client, statuses)
    await post_webhook(client, inbound_payload("34612345678", "Hola", "wamid.2", clock.now, phone_number_id="ANOTHER"))
    assert sender.sent == []
    assert responder.calls == []


async def test_agent_paused_records_without_replying(client, clock, sender, container):
    runtime = await container.runtime_settings.load()
    runtime.enabled = False
    await container.runtime_settings.save(runtime)

    await post_webhook(client, inbound_payload("4915123456789", "Hallo", "wamid.3", clock.now))
    assert sender.sent == []
    assert len(await container.conversations.summaries()) == 1


async def test_health(client):
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["whatsapp"] == "configured"
