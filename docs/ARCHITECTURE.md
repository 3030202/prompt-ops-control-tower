# Architecture

```mermaid
flowchart LR
  Sources[RSS / GitHub / Web / Telegram / Workspace] --> Ingest[Ingestion + normalization]
  Ingest --> Redis[(Redis AOF)]
  Ingest --> Qdrant[(Qdrant vector index)]
  Ingest --> Prompts[Prompt projection + strict allowlist]
  Prompts --> Register[8.0x101.lol Prompt Register]
  Prompts --> MCP[Bearer-protected Streamable HTTP MCP]
  Qdrant --> MCP
  MCP --> Clients[AI clients / IDE agents]
  Ingest --> Rules[Publishing rule engine]
  Rules -->|live| Flash[Short live flash]
  Rules -->|scheduled| Queue[Candidate queue]
  Queue --> Scheduler[Europe/Moscow scheduler]
  Scheduler --> Drafts[Draft registry]
  Drafts --> Review[Human review]
  Drafts -->|explicit auto| Bot[Telegram Bot API]
  Review --> Bot
  Telethon[Telethon reader] --> Styles[Style profiles]
  Styles --> Drafts
  Bot --> Public[Sanitized public feed]
  Public --> Lite[Read-only Lite UI]
```

## Trust boundaries
 
- Administrative APIs and `/studio` use HTTP Basic authentication.
- Prompt-only hosts (`8.0x101.lol`, `08.0x101.lol`) expose `/prompts`, `/lite`, `/health`, `/api/prompts/*`, `/api/daily-pass/*`, `/api/public/*`, `/mcp` (Bearer token) and `/studio` (HTTP Basic Auth); internal Radar/OSINT dashboard and raw ingestion APIs are rejected with `404 Prompt-only surface`.
- `/mcp` requires a separate Bearer token and exposes only read-only tools backed by the public prompt allowlist.
- MCP transport validates `Host` and `Origin` against explicit production allowlists to prevent DNS rebinding.
- `/api/public/*` and `/lite` are intentionally public and return an allowlisted schema only.
- Telegram Bot API publishes; Telethon reads monitored/style channels.
- A live rule with an unavailable AI predicate fails closed and creates a review draft.
- Idempotency keys prevent repeated publication for 30 days.

## Persistence

Channels, styles, rules, drafts and public posts are Redis hashes. Scheduled drafts and candidates are sorted sets. The audit trail is a capped Redis stream. Redis AOF and the Qdrant volume survive container restarts.
