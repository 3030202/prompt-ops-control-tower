# Architecture

```mermaid
flowchart LR
  Sources[RSS / GitHub / Web / Telegram / Workspace] --> Ingest[Ingestion + normalization]
  Ingest --> Redis[(Redis AOF)]
  Ingest --> Qdrant[(Qdrant vector index)]
  Ingest --> Prompts[Prompt projection + strict allowlist]
  Prompts --> Register[8.0x101.lol Prompt Register]
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
- Prompt-only host middleware exposes only `/prompts`, `/api/prompts` and `/health`; workspace and Telegram records are rejected before projection.
- `/api/public/*` and `/lite` are intentionally public and return an allowlisted schema only.
- Telegram Bot API publishes; Telethon reads monitored/style channels.
- A live rule with an unavailable AI predicate fails closed and creates a review draft.
- Idempotency keys prevent repeated publication for 30 days.

## Persistence

Channels, styles, rules, drafts and public posts are Redis hashes. Scheduled drafts and candidates are sorted sets. The audit trail is a capped Redis stream. Redis AOF and the Qdrant volume survive container restarts.
