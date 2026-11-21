from tests.conftest import csrf_from, inbound_payload, post_webhook, sign_in

GUEST = "393471234567"


async def test_pages_require_sign_in(client):
    for path in ("/admin", "/admin/conversations", "/admin/playground"):
        response = await client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"


async def test_wrong_password_is_refused(client):
    page = await client.get("/admin/login")
    response = await client.post(
        "/admin/login", data={"username": "admin", "password": "nope", "csrf": csrf_from(page.text)}
    )
    assert response.headers["location"] == "/admin/login"
    assert (await client.get("/admin")).status_code == 303


async def test_forms_need_the_csrf_token(client):
    response = await client.post("/admin/login", data={"username": "admin", "password": "correct horse battery"})
    assert response.status_code == 400


async def test_dashboard_and_inbox(client, clock):
    await post_webhook(client, inbound_payload(GUEST, "Ciao, siete aperti lunedì?", "wamid.1", clock.now, name="Marco"))
    await sign_in(client)

    dashboard = await client.get("/admin")
    assert dashboard.status_code == 200
    assert "The host is answering guests" in dashboard.text

    inbox = await client.get("/admin/conversations")
    assert "Marco" in inbox.text
    assert "🇮🇹" in inbox.text


async def test_team_reply_from_the_inbox_takes_the_chat(client, clock, sender, container):
    await post_webhook(client, inbound_payload(GUEST, "Ciao", "wamid.1", clock.now))
    await sign_in(client)
    conversation_id = (await container.conversations.summaries())[0].id
    page = await client.get(f"/admin/conversations/{conversation_id}")

    response = await client.post(
        f"/admin/conversations/{conversation_id}/reply",
        data={"text": "Ciao Marco, sono Giulia!", "csrf": csrf_from(page.text)},
    )
    assert response.status_code == 303
    assert sender.sent[-1] == (GUEST, "Ciao Marco, sono Giulia!")
    assert (await container.conversations.summaries())[0].status == "team"


async def test_settings_are_saved(client, container):
    await sign_in(client)
    page = await client.get("/admin")
    await client.post(
        "/admin/settings",
        data={"csrf": csrf_from(page.text), "enabled": "true", "reply_after_minutes": "20", "takeover_hours": "6"},
    )
    runtime = await container.runtime_settings.load()
    assert (runtime.enabled, runtime.reply_after_minutes, runtime.takeover_hours) == (True, 20, 6)


async def test_playground_talks_to_the_agent_without_whatsapp(client, sender, responder):
    await sign_in(client)
    page = await client.get("/admin/playground")
    await client.post(
        "/admin/playground",
        data={"text": "Bonjour !", "phone": "06 12 34 56 78", "csrf": csrf_from(page.text)},
    )
    page = await client.get("/admin/playground")
    assert "Bonjour !" in page.text
    assert responder.text in page.text
    assert sender.sent == []
    assert responder.calls[0]["guest"].phone == "+33612345678"
