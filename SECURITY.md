# Security Policy

## Reporting

Do not open a public issue for credentials, authentication bypasses, Telegram session exposure, SSRF, or data leaks. Send a private GitHub security advisory to the repository owner.

## Secret handling

- Never commit `.env`, Telethon `.session` files, logs, exported private artifacts, Redis data, or Qdrant data.
- Use a dedicated Telegram bot with only the permissions required for selected channels.
- Keep the dashboard behind HTTPS and strong authentication when exposed outside localhost.
- Treat source text and AI output as untrusted. Review generated posts before enabling `approval_mode=auto`.

## Supported version

Only the latest revision on `main` receives security fixes during the initial development phase.
