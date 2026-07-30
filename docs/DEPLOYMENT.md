# Deployment

## Первичное развёртывание

```bash
git clone https://github.com/3030202/prompt-ops-control-tower.git
cd prompt-ops-control-tower
cp .env.example .env
```

Заполните обязательные переменные и создайте отдельный MCP-токен:

```bash
openssl rand -hex 32
```

Запишите результат в `MCP_API_KEY` внутри `.env`. Затем:

```bash
mkdir -p logs sessions secrets
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
```

## Cloudflare Tunnel

Текущий production использует named tunnel. Public hostname направляется на:

```text
http://osint-radar:8000
```

Один origin обслуживает Prompt Register и MCP:

- `https://8.0x101.lol/`
- `https://8.0x101.lol/mcp`

Запуск:

```bash
docker compose --profile tunnel up -d --build
```

## Обновление

```bash
git pull --ff-only origin main
docker compose --profile tunnel up -d --build
docker compose ps
docker compose logs --no-color --tail=100 osint-radar
```

## Проверка MCP

Без авторизации:

```bash
curl -i https://8.0x101.lol/mcp
```

Ожидается `401`.

С авторизацией:

```bash
export MCP_API_KEY='token-from-production-env'
curl -sS https://8.0x101.lol/mcp \
  -X POST \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "MCP-Method: tools/list" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Дополнительно проверьте `tools/call` для `list_prompts`, `get_prompt` и `semantic_search_prompts`.

## Ротация токена

1. Создайте новый `openssl rand -hex 32`.
2. Замените `MCP_API_KEY` в production `.env`.
3. Выполните `docker compose up -d --force-recreate osint-radar`.
4. Обновите секрет в MCP-клиентах.
5. Убедитесь, что старый токен получает `401`.
