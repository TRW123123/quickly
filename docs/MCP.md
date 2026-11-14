# Model Context Protocol (MCP) — Quickly Leads

Quickly exposes a **remote MCP server** over **Streamable HTTP** so AI clients (Cursor, Claude Desktop, etc.) can call lead-related tools without running a local Python MCP process. The server is part of the main FastAPI app and uses the same authentication as the REST API.

---

## Endpoint

| Item | Value |
|------|--------|
| **URL** | `{BASE_URL}/api/mcp` |
| **Transport** | Streamable HTTP (use `mcp-remote` with `--transport http-only` when connecting from stdio-only clients) |
| **Auth** | `X-API-Key: <key>` and/or `Authorization: Bearer <jwt>` on every request (same as [API.md](API.md)) |

`BASE_URL` is the value you set in the environment (e.g. `https://quickly.example.com`). The UI **Settings → MCP (AI agents)** shows the resolved URL after login.

---

## Authentication

1. **API key (recommended for agents)** — Create a key under **Settings → API Keys**. Send it as:

   ```http
   X-API-Key: <your-api-key>
   ```

2. **JWT Bearer** — Same access token as the REST API (`Authorization: Bearer …`). Tokens expire (see API docs); API keys are usually simpler for long-lived MCP setups.

MCP requests are checked before the MCP session runs. Tool calls that proxy to `/api/leads` and `/api/campaigns/...` reuse the credentials from the **incoming MCP HTTP request**, so each user/session only accesses their own data.

---

## Tools

| Tool | Purpose |
|------|--------|
| `list_leads` | List leads (`q`, `status`, `bad_only`, `interest`) — same filters as `GET /api/leads`; all combine with AND when set |
| `get_lead` | `GET /api/leads/{id}` |
| `update_lead` | `PATCH /api/leads/{id}` (`name`, `enrollment_status`, `custom_data`) |
| `delete_lead` | `DELETE /api/leads/{id}` |
| `add_campaign_leads` | `POST /api/campaigns/{campaign_id}/leads` (bulk add / enroll) |

Standalone lead creation is not exposed as a separate MCP tool; use `add_campaign_leads` (same rules as the REST API).

### Campaign tools

These tools work directly on the database (same auth gate as above) and return JSON text.

| Tool | Parameters | Result |
|------|-----------|--------|
| `list_campaigns` | — | `id`, `name`, `status` (`active`/`paused`), `paused`, `leads` (enrolled), `sent` (emails sent), `sequence_steps`, `inbox_ids` |
| `create_campaign` | `name`, `steps?` = `[{subject, body, delay_days}]`, `inbox_ids?` | New campaign. **Always created paused (draft)** — a passed `paused` value is ignored. Empty `subject` on a later step = reply in the same thread. Unknown inbox ids → error, nothing created. |
| `add_leads` | `campaign_id`, `leads?` (JSON list of `{email, name?, custom_data?}` or plain emails; extra keys go into `custom_data`; a CSV string is accepted too), `csv?` (CSV text with an `email` header column, optional `name` and custom columns; headerless = one email per line) | `added`, `duplicates`, `blocked`, `invalid` — each `{count, emails}` (`duplicates.details` gives the reason: `duplicate_in_input`, `existing_lead`, `already_enrolled`), plus `errors`. Duplicates are checked against **all** existing leads (enrolled anywhere or not) and the blocklist. |
| `get_stats` | `campaign_id?` | One campaign or all: `leads`, `sent`, `opens`, `clicks`, `replies`, `replies_excluding_auto`, `bounces` and rates (`opens`/`clicks` = emails with ≥1 open/click; only tracked when enabled on the campaign). |
| `list_replies` | `campaign_id?`, `since?` (ISO date/datetime, UTC), `limit?` (default 20, max 200) | Newest first: lead, campaign, `replied_at`, `subject`, `snippet` (best effort from the synced Gmail/Office 365/SMTP mailbox, may be `null`), `classification` (AI interest status, if set). |
| `pause_campaign` | `campaign_id` | Sets `paused=true` and rebuilds the queue. No approval needed. |
| `start_campaign` | `campaign_id`, **`approved_by_safak`** (required) | Starts a draft/paused campaign. |
| `resume_campaign` | `campaign_id`, **`approved_by_safak`** (required) | Resumes a paused campaign. |

**Starting a campaign.** Quickly has no separate status column: a campaign sends when `paused=false` (the scheduler and send job skip paused campaigns). `start_campaign` and `resume_campaign` therefore both set `paused=false` and trigger the same global queue recalculation as the UI toggle. `start_campaign` and `resume_campaign` only act when `approved_by_safak` is the JSON boolean `true`; anything else (`false`, `"true"`, `1`, `null`) is rejected without effect. Both also refuse campaigns without sequence steps or without an inbox. Omitting the parameter fails schema validation before the tool runs.

**Audit log.** Every start/resume attempt that reaches the tool — approved or rejected — is written to the table `campaign_start_log` (`campaign_id`, `action` = `start`/`resume`, `approved_by_safak` (`NULL` if a non-boolean was sent), `api_key_id` and `user` of the caller when resolvable, `result` such as `started`, `resumed`, `unchanged: campaign already active`, `rejected: …`, `created_at`).

### Blocklist

A global do-not-contact list lives in the table `blocklist` (`id`, `value` lowercase and unique, `kind` = `email` | `domain`, `reason`, `created_at`). A domain entry blocks every address whose part after `@` equals the domain exactly (no subdomain matching).

Every way leads enter a campaign skips blocked addresses and reports them: `POST /api/campaigns/{id}/leads` (and therefore `add_campaign_leads`) and `POST /api/campaigns/{id}/leads/import` return `blocked` (count) and `blocked_leads`, with per-row status `blocked`; preview mode (`confirm_only`) lists them under `flagged.blocked`; `add_leads` returns them under `blocked`. Lead recovery (changing a lead's email) refuses blocked addresses.

REST (same auth as other routers):

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/blocklist?q=&kind=&limit=&offset=` | `{total, items}` |
| `POST` | `/api/blocklist` | One object `{value, kind?, reason?}` or a list of objects/strings. `kind` is detected from the value when omitted (`@example.com` → domain). Returns `{added, existing, invalid}`. |
| `DELETE` | `/api/blocklist/{id}` | 404 if unknown |

---

## Cursor and `mcp-remote`

Most MCP clients only support **stdio**. Use **[mcp-remote](https://www.npmjs.com/package/mcp-remote)** so the editor runs a small Node bridge to Quickly’s HTTPS endpoint.

### From the UI

1. Log in → **Settings → MCP (AI agents)**.
2. Create an **API key** if needed.
3. Click **Copy MCP fragment** and merge the `mcpServers` entry into your MCP config (Cursor: `~/.cursor/mcp.json`).

The fragment uses environment substitution so secrets are not embedded in JSON with awkward escaping:

```json
{
  "mcpServers": {
    "quickly-leads": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://your-domain.com/api/mcp",
        "--header",
        "X-API-Key:${QUICKLY_MCP_API_KEY}",
        "--transport",
        "http-only"
      ],
      "env": {
        "QUICKLY_MCP_API_KEY": "your-key-here"
      }
    }
  }
}
```

### JWT instead of an API key

Use a header without spaces after the colon (Cursor/Windows caveat per mcp-remote docs), and put the full `Bearer …` value in `env`:

```json
"args": [
  "-y",
  "mcp-remote",
  "https://your-domain.com/api/mcp",
  "--header",
  "Authorization:${QUICKLY_MCP_AUTH}",
  "--transport",
  "http-only"
],
"env": {
  "QUICKLY_MCP_AUTH": "Bearer eyJ..."
}
```

### Local HTTP (development only)

If Quickly is only on `http://localhost:8000`, add **`--allow-http`** after the URL in `args` (see mcp-remote README). Do not use this on untrusted networks.

---

## Setup helper API

`GET /api/settings/mcp-setup` (authenticated) returns:

| Field | Description |
|-------|-------------|
| `api_base_url` | Configured app base URL |
| `mcp_http_url` | Full MCP endpoint (`…/api/mcp`) |
| `cursor_mcp_fragment` | Ready-to-merge `mcpServers` object for `npx mcp-remote` |

See [API.md — Settings](API.md#settings) for the full Settings API.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| **404 / 405** on `/api/mcp` | Old deployment without the dedicated MCP routes; upgrade Quickly. |
| **401** | Missing or invalid `X-API-Key` / Bearer token. |
| **421 Misdirected Request** / “Invalid Host header” | Fixed in current Quickly: MCP DNS rebinding checks are disabled for the public Host your reverse proxy sends. Upgrade if you still see this. |
| **Connection / TLS errors** | Check `BASE_URL`, certificate, and that the client can reach the host. |

---

## Related documentation

- [API.md](API.md) — REST reference (leads, campaigns, auth)
- [README.md](../README.md) — Install and overview
