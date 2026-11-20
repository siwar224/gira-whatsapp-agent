# GIRA WhatsApp Agent

A WhatsApp host for **GIRA — Pasta & Mare**. It answers customers' questions (menu, hours,
address, vegetarian options, Crea il tuo…) and **books, finds and cancels tables** and adds guests
to the waiting list, through the same API the GIRA website uses. It runs on
[CodeWords](https://codewords.agemo.ai) as a single Python service.

| | |
|---|---|
| CodeWords service | `gira_whatsapp_agent_4a536e8e` (private) |
| Source | [`gira_whatsapp_agent.py`](gira_whatsapp_agent.py) |
| Books on | `https://gira-lyart.vercel.app` (configurable) |
| Model | Claude Sonnet 4.6, through the CodeWords proxy (no API key needed) |

## How it behaves

- **The team answers first.** When a customer writes, the agent waits. If nobody from the team
  replies from the restaurant's phone within **30 minutes** (configurable), the agent answers.
  Set the delay to `0` for instant replies.
- A reply typed by the team on the phone counts as an answer: the timer for that chat stops, and
  the agent sees the team's message as context for later.
- It replies **in the customer's language**, in direct chats only (never in groups).
- Before booking or cancelling, it always summarises and asks for a clear "yes".
- Bookings are made with the customer's WhatsApp number, so they can also manage them on
  `/prenota/gestisci`. A booking can only be looked up or cancelled from the number that made it.
- "I want to talk to a person" or a complaint → the agent hands over: it stays quiet in that chat
  for 12 hours and drops a note in the restaurant's own "Message yourself" chat.
- Conversations are kept 48 hours, then forgotten. It never mentions wine or alcohol.

## Endpoints

| Path | What it does |
|---|---|
| `POST /` | Maintenance run: answers chats that waited past the delay, returns the status. Scheduled every 5 min. |
| `POST /connect` | Links a WhatsApp number (`{"phone_number": "+33…"}`) and returns a pairing code. Use it again to switch numbers. |
| `POST /settings` | `{"reply_after_minutes": 30, "enabled": true, "gira_url": "https://…"}` — no redeploy needed. |
| `POST /webhook` | Inbound WhatsApp events (called by the WhatsApp connection). |
| `POST /chat` | Talk to the agent without WhatsApp: `{"message": "…", "session": "demo"}`. Bookings it makes are real. |

## Setup (once)

1. **Link the restaurant's number.** Run `POST /connect` with the full international number:
   ```json
   { "phone_number": "+33612345678", "service_id": "gira_whatsapp_agent_4a536e8e" }
   ```
   Then on that phone, within ~30 seconds: **WhatsApp → Settings → Linked devices → Link a device →
   "Link with phone number instead"** and type the pairing code.
2. **Start the 5-minute schedule** for `POST /` (CodeWords → [Scheduled](https://codewords.agemo.ai/scheduled),
   or `schedule_runs /create` with the request id of a `POST /` run and `rate(5 minutes)`).
   Without it, delayed replies are never sent (instant mode, delay `0`, works without it).
3. Send a message to the number from another phone and check the reply.

To change number later, just run `/connect` with the new number (and unlink the old device on the
old phone). The WhatsApp link is a CodeWords beta feature; help: support@codewords.ai.

## Keeping it in sync with the website

The menu, hours, tables and rules are copied from the GIRA repo (`src/content/sections.ts` and
`src/content/booking.ts`) into `BUSINESS_FACTS` and `TABLES`. When the menu changes on the site,
update them here and redeploy.

## Known limits

- GIRA's API allows **5 new bookings per 10 minutes per IP**. All WhatsApp bookings come from
  CodeWords' servers, so a busy evening could hit it; the agent then apologises and points to the
  website. A dedicated agent key on the GIRA side would lift this.
- Voice notes and photos aren't read; the agent kindly asks the customer to write instead.
- Customers whose number WhatsApp hides (`@lid` contacts) are asked for their phone number before booking.
