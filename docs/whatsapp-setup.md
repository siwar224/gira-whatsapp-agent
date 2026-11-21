# Connecting a WhatsApp number

The agent talks to guests through the official **WhatsApp Cloud API** from Meta. This guide
connects a restaurant number to it. It takes about 30 minutes; the business verification in step 1
can take longer.

## 1. Meta accounts

1. Create (or use) a **Meta Business portfolio** at [business.facebook.com](https://business.facebook.com).
2. At [developers.facebook.com](https://developers.facebook.com/apps), create an app of type
   **Business** and add the **WhatsApp** product.
3. Complete **business verification** in the Business portfolio when Meta asks for it. It raises
   the messaging limits and is required to go live.

## 2. The restaurant's number

In the app: **WhatsApp → API Setup → Add phone number**.

| Situation | What to do |
|---|---|
| A new number (SIM or landline) | Register it directly. It can't be used in the WhatsApp apps at the same time. |
| The number already runs on the **WhatsApp Business app** | Choose to connect the existing Business app number (*coexistence*), where Meta offers it for the number's country. The team keeps using the app, and every message they send from it reaches the agent as a team message. |
| The number runs on the personal WhatsApp app | Move it to the WhatsApp Business app first, or use another number. |

Any number from Tunisia (+216), Morocco (+212), France (+33), Italy (+39), Germany (+49) or Spain (+34)
works. Without coexistence, the team answers guests from the agent's **Inbox** instead of the app —
the takeover rules are the same.

Note the **Phone number ID** shown on the API Setup page → `WHATSAPP_PHONE_NUMBER_ID`.

## 3. A permanent access token

The temporary token on the API Setup page expires after 24 hours. For production:

1. Business settings → **Users → System users** → add a system user (role *Admin*).
2. **Assign assets**: the app (full control) and the WhatsApp account.
3. **Generate token** for the app with the permissions `whatsapp_business_messaging` and
   `whatsapp_business_management`, expiry *Never*.

→ `WHATSAPP_ACCESS_TOKEN`

The **App secret** is in the app's **Settings → Basic** → `WHATSAPP_APP_SECRET`.

## 4. The webhook

The service must be reachable over HTTPS (deployed, or locally through a tunnel such as
`cloudflared tunnel --url http://localhost:8000`).

1. Choose a long random string → `WHATSAPP_VERIFY_TOKEN`, and start the service with it.
2. In the app: **WhatsApp → Configuration → Webhook → Edit**
   - Callback URL: `https://<your-domain>/webhook`
   - Verify token: the same string
3. **Webhook fields** → subscribe to `messages`, and to `smb_message_echoes` when the number uses
   coexistence.

## 5. Check

- `GET https://<your-domain>/health` reports `"whatsapp": "configured"`.
- Send a message to the restaurant number from another phone: the reply arrives within seconds and
  the conversation appears in the admin **Inbox**.

## Good to know

- **24-hour window.** WhatsApp allows free-form replies within 24 hours of the guest's last
  message. The agent only ever replies to guests, so it is always inside the window; the Inbox
  disables the reply box when a chat is older than that.
- **Costs.** Replies to guests who wrote first are service conversations; check Meta's current
  WhatsApp pricing for each country.
- **Message ids.** Each reply the agent sends is stored with its WhatsApp id, so it is never
  mistaken for a team message.
