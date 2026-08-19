---
name: prompt-ops-deploy
description: >-
  Процедуры развертывания, управления Docker контейнерами, мониторинга логов
  и обслуживания сервисов Prompt Ops Control Tower (Redis, Qdrant, Cloudflared).
---

# Prompt Ops Deployment & Operations Guide

## 1. Стек сервисов Docker
- `osint_perplexity_grok_radar` (FastAPI / Uvicorn порт 8000)
- `osint_redis` (Redis 7 Alpine, порт 6379)
- `promptops_qdrant` (Qdrant Vector DB v1.13.4, порт 6333)
- `promptops_cloudflared` (Cloudflare Tunnel)

## 2. Управление контейнерами
- **Перезапуск основного сервиса после правок `.env`**:
  ```bash
  docker restart osint_perplexity_grok_radar
  ```
- **Просмотр логов в реальном времени**:
  ```bash
  docker logs -f --tail 100 osint_perplexity_grok_radar
  ```
- **Проверка состояния всех контейнеров**:
  ```bash
  docker ps
  ```

## 3. Проверка здоровья (Healthcheck)
- Health эндпоинт: `GET http://127.0.0.1:8000/health`
- Метрики: `GET http://127.0.0.1:8000/metrics`
- WebApp: `GET http://127.0.0.1:8000/webapp`
