# mailgate

Self-hostable SMTP forwarder with a **Resend-compatible HTTP API**. Bring your own SMTP, get back a single endpoint that browsers and servers can call without exposing SMTP credentials.

MailGate is intentionally small. It does **one thing**: takes a fully-formed email object and forwards it through your SMTP. Body construction, form parsing, validation - all caller responsibility. Think of it as a Resend you can host.

- ✅ **JSON API** that mirrors Resend's `/emails` shape - drop-in for existing SDKs.
- ✅ **Browser-public-key safe**: per-key origin allowlist, recipient allowlist, sender allowlist, per-IP rate limit, IP blocklist, captcha (Turnstile / hCaptcha / reCaptcha).
- ✅ **Single small container** (~80 MB, no DB, no queue, no dashboard).
- ✅ **Prometheus metrics** at `/metrics`, all endpoints Bearer-auth protected.

## API

### `POST /emails`

Drop-in compatible with [Resend's `POST /emails`](https://resend.com/docs/api-reference/emails/send-email).

```http
POST /emails HTTP/1.1
Authorization: Bearer mg_xxxxx
Content-Type: application/json
X-Captcha-Token: <optional, if captcha configured for this client>

{
  "from": "Antikas <forms@forms.antikas.de>",
  "to": "info@antikas.de",
  "reply_to": "user@example.com",
  "subject": "new business registration",
  "html": "<p>Firma: Acme GmbH</p>",
  "text": "Firma: Acme GmbH",
  "attachments": [
    { "filename": "doc.pdf", "content": "JVBERi0xLjQg...", "content_type": "application/pdf" }
  ]
}
```

Field semantics match Resend's exactly:
- `from`, `to`, `cc`, `bcc`, `reply_to` accept either a single string or a list.
- `from` and `to` may be omitted if `default_from` / `default_to` are configured server-side for the client.
- At least one of `html` / `text` must be present.
- `attachments[].content` is base64-encoded.

Response on success: `{"id": "msg_..."}` (HTTP 200).

### `GET /health`

`{"status":"ok","version":"0.3.0"}`. **Requires Bearer auth** so the version isn't leaked publicly.

The bundled Docker `HEALTHCHECK` reads `MAILGATE_CLIENT_API_KEY` from env to authenticate from inside the container - works out of the box for env-based single-tenant deploys.

### `GET /metrics`

Prometheus exposition format. **Requires Bearer auth.** Configure your scraper with `bearer_token` (or `bearer_token_file` in Prometheus). Counters:

- `mailgate_emails_sent_total{client}`
- `mailgate_emails_failed_total{client,reason}`
- `mailgate_requests_blocked_total{reason}`
- `mailgate_send_duration_seconds{client}` (histogram)

### Errors

| Status | Code | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing or invalid api_key |
| 403 | `forbidden` | Origin / sender / recipient / captcha denied; IP blocked |
| 422 | `validation_error` | Bad request body (missing required field, etc.) |
| 429 | `rate_limited` | Per-key or per-IP minute or daily cap |
| 502 | `smtp_error` | SMTP unreachable / rejected |

## Configuration

Two modes - pick one (or combine for multi-tenant + default):

### Mode A: single-client via environment variables *(recommended for single-site deploys)*

#### Server settings (prefix `MAILGATE_`)

| Variable | Default | Required | Description |
|---|---|---|---|
| `MAILGATE_SMTP_HOST` | - | ✅ | SMTP server hostname |
| `MAILGATE_SMTP_PORT` | `587` | | SMTP port |
| `MAILGATE_SMTP_USER` | - | ✅ | SMTP username |
| `MAILGATE_SMTP_PASSWORD` | - | ✅ | SMTP password |
| `MAILGATE_SMTP_USE_TLS` | `true` | | STARTTLS on port 587 |
| `MAILGATE_SMTP_TIMEOUT` | `15` | | Per-send timeout (seconds) |
| `MAILGATE_HOST` | `0.0.0.0` | | Listen address |
| `MAILGATE_PORT` | `8080` | | Listen port |
| `MAILGATE_LOG_LEVEL` | `INFO` | | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

#### Client config (env-only single-client mode)

| Variable | Default | Description |
|---|---|---|
| `MAILGATE_CLIENT_NAME` | `default` | Logical name (used in metrics + logs) |
| `MAILGATE_CLIENT_API_KEY` | - *(req.)* | Bearer token. Min 16 chars, must start with `mg_` |
| `MAILGATE_CLIENT_ALLOWED_ORIGINS` | `""` (any) | Comma-separated `Origin` allowlist |
| `MAILGATE_CLIENT_ALLOWED_FROM_ADDRESSES` | `""` (any) | Comma-separated allowed `from:` addresses |
| `MAILGATE_CLIENT_ALLOWED_TO_ADDRESSES` | `""` (any) | **Strongly recommended.** Comma-separated allowed recipients |
| `MAILGATE_CLIENT_DEFAULT_FROM` | unset | Sender used when caller omits `from` |
| `MAILGATE_CLIENT_DEFAULT_TO` | `""` | Recipients used when caller omits `to` (comma-sep) |
| `MAILGATE_CLIENT_SUBJECT_PREFIX` | unset | Prepended to every subject (e.g. `[Antikas] `) |
| `MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE` | `10` | Per-key minute window |
| `MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE_PER_IP` | unset | Per-IP minute window (additional) |
| `MAILGATE_CLIENT_DAILY_LIMIT` | unset | Per-key daily cap |
| `MAILGATE_CLIENT_DAILY_LIMIT_PER_IP` | unset | Per-IP daily cap |
| `MAILGATE_CLIENT_IP_BLOCKLIST` | `""` | Comma-separated IPs / CIDR ranges to hard-reject |
| `MAILGATE_CLIENT_CAPTCHA_PROVIDER` | unset | `turnstile`, `hcaptcha`, or `recaptcha` |
| `MAILGATE_CLIENT_CAPTCHA_SECRET` | unset | Captcha-provider site secret |

### Mode B: multi-tenant via clients.json

Set `MAILGATE_CLIENTS_FILE=/path/to/clients.json`. Same field names without the `MAILGATE_CLIENT_` prefix. See `clients.example.json`.

### Public-key security model

MailGate accepts that for browser-hosted frontends, the `api_key` is effectively public. Security comes from **scope lockdown**, not key secrecy. Combine:

- `allowed_to_addresses` (lock to your own mailbox)
- `allowed_from_addresses` (lock to verified sender domain)
- `rate_limit_per_minute_per_ip` (limit per-IP burst)
- `daily_limit_per_ip` (cap per-IP daily)
- `captcha_provider` (Turnstile / hCaptcha / reCaptcha - bot resistance)

Worst case after a key leak: someone spams *your own inbox*, throttled by per-IP limits and gated by captcha. No worse than receiving spam through public SMTP, just rate-controlled.

This is the same design pattern as Mapbox publishable tokens, Algolia search-only keys, Stripe `pk_*` keys: public-by-design, scope-restricted.

## Local development

```bash
cd mailgate
python -m venv .venv && . .venv/Scripts/activate     # Windows
# python -m venv .venv && . .venv/bin/activate       # Linux/macOS
pip install -e ".[dev]"
cp .env.example .env       # edit SMTP_* and CLIENT_*
mailgate                  # http://localhost:8080
pytest                     # smoke tests
```

Generate a key:

```bash
python -c "import secrets; print('mg_'+secrets.token_urlsafe(24))"
```

## Docker

```bash
docker compose up -d
```

`.env` is read by docker-compose. Multi-stage Dockerfile builds a ~80 MB final image, runs as non-root, has healthcheck.

## Deploy on Coolify

1. New Application → Type: **Dockerfile** → connect this Git repo.
2. Exposed port: `8080`.
3. Custom domain (e.g. `forms.antikas.de`) - Coolify auto-provisions Let's Encrypt.
4. Healthcheck path: `/health`.
5. Paste env vars (replace placeholders):

   ```
   MAILGATE_SMTP_HOST=smtp-relay.brevo.com
   MAILGATE_SMTP_PORT=587
   MAILGATE_SMTP_USER=your-brevo-smtp-login
   MAILGATE_SMTP_PASSWORD=your-brevo-smtp-key
   MAILGATE_SMTP_USE_TLS=true

   MAILGATE_CLIENT_NAME=antikas
   MAILGATE_CLIENT_API_KEY=mg_GENERATED_RANDOM_TOKEN
   MAILGATE_CLIENT_ALLOWED_ORIGINS=https://www.antikas.de,https://antikas.de
   MAILGATE_CLIENT_ALLOWED_FROM_ADDRESSES=forms@forms.antikas.de
   MAILGATE_CLIENT_ALLOWED_TO_ADDRESSES=info@antikas.de
   MAILGATE_CLIENT_DEFAULT_FROM=Antikas Formular <forms@forms.antikas.de>
   MAILGATE_CLIENT_DEFAULT_TO=info@antikas.de
   MAILGATE_CLIENT_SUBJECT_PREFIX=[Antikas]
   MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE=10
   MAILGATE_CLIENT_RATE_LIMIT_PER_MINUTE_PER_IP=2
   MAILGATE_CLIENT_DAILY_LIMIT=200
   MAILGATE_CLIENT_DAILY_LIMIT_PER_IP=5
   ```

6. Deploy.

The CLI runs uvicorn with `--proxy-headers --forwarded-allow-ips '*'` so the real client IP is honored through Coolify's Traefik proxy (matters for `rate_limit_per_minute_per_ip` and `ip_blocklist`).

## Frontend example

The caller assembles the email object and POSTs JSON. MailGate just forwards.

```javascript
async function submit(form) {
  const data = new FormData(form);

  // Build attachments list (base64-encode any uploaded files)
  const attachments = [];
  for (const [key, val] of data.entries()) {
    if (val instanceof File && val.size > 0) {
      const buf = await val.arrayBuffer();
      const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
      attachments.push({ filename: val.name, content: b64, content_type: val.type });
      data.delete(key);
    }
  }

  // Build email body from the remaining text fields - your app decides the format
  const fields = Array.from(data.entries());
  const html = `<table>${fields.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>`;
  const text = fields.map(([k, v]) => `${k}: ${v}`).join('\n');

  const res = await fetch('https://forms.antikas.de/emails', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer mg_xxx',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      reply_to: data.get('email'),
      subject: 'neue Anfrage',
      html,
      text,
      attachments: attachments.length ? attachments : undefined,
    }),
  });

  if (res.ok) showSuccess();
  else showError(await res.json());
}
```

## Compatibility notes

The Resend Python / JS SDKs work if you point their base URL at your mailgate deploy. The wire format is identical.

## Why no `/forms` endpoint?

Earlier versions had a multipart `/forms` endpoint that auto-rendered an HTML body from form fields. It was removed in v0.3 because it conflated two concerns:

- **Forwarder** (MailGate's job): take a structured email and ship it via SMTP.
- **Form handler** (Web3Forms / Formspree / Basin): receive HTML form submissions and turn them into emails.

Resend, Postmark, Mailgun, SendGrid all do only the first. MailGate now does the same. If you need a form handler, run one in your frontend (10 lines of JS, see example above) or put a tiny purpose-built service in front of MailGate.

## License

Apache-2.0 - see [LICENSE](LICENSE).
