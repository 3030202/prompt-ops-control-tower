import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from daily_pass import get_daily_pin, get_today_date_str

router = APIRouter()
security = HTTPBasic()
MOSCOW = ZoneInfo("Europe/Moscow")

CHANNELS_KEY = "promptops:publishing:channels"
STYLES_KEY = "promptops:publishing:styles"
RULES_KEY = "promptops:publishing:rules"
DRAFTS_KEY = "promptops:publishing:drafts"
EVENTS_KEY = "promptops:publishing:events"
PUBLIC_KEY = "promptops:publishing:public"
PUBLIC_ORDER_KEY = "promptops:publishing:public:order"
SCHEDULE_KEY = "promptops:publishing:schedule"
CANDIDATES_PREFIX = "promptops:publishing:candidates"
SENT_PREFIX = "promptops:publishing:sent"
RATE_PREFIX = "promptops:publishing:rate"
POST_MODES = {"step_by_step", "roast", "my_take", "artifact_from_source", "full_editorial"}
ARTIFACT_TYPES = {"prompt", "skill", "rule", "checklist", "pipeline"}

_app: Any = None
_load_records: Callable[[list[str]], Awaitable[list[dict[str, Any]]]] | None = None
_record_usage: Callable[..., Awaitable[None]] | None = None


def configure(app: Any, load_records: Callable[[list[str]], Awaitable[list[dict[str, Any]]]], record_usage: Callable[..., Awaitable[None]]) -> None:
    global _app, _load_records, _record_usage
    _app, _load_records, _record_usage = app, load_records, record_usage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def get_app() -> Any:
    global _app
    if _app is not None:
        return _app
    try:
        import prompt_ops_app
        return prompt_ops_app.app
    except Exception:
        return None


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    app_instance = get_app()
    if not app_instance or not hasattr(app_instance.state, "config"):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Config uninitialized")
    cfg = app_instance.state.config
    valid = secrets.compare_digest(credentials.username, cfg.dashboard_user) and secrets.compare_digest(credentials.password, cfg.dashboard_pass)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


async def get_item(key: str, item_id: str) -> dict[str, Any] | None:
    raw = await _app.state.redis.hget(key, item_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def list_items(key: str) -> list[dict[str, Any]]:
    result = []
    for raw in await _app.state.redis.hvals(key):
        try:
            result.append(json.loads(raw))
        except Exception:
            continue
    return result


async def save_item(key: str, item: dict[str, Any]) -> None:
    await _app.state.redis.hset(key, item["id"], json.dumps(item, ensure_ascii=False))


async def event(kind: str, **payload: Any) -> None:
    values = {"type": kind, "created_at": iso_now(), **payload}
    fields = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value) for key, value in values.items()}
    await _app.state.redis.xadd(EVENTS_KEY, fields, maxlen=5000, approximate=True)


def rule_matches(rule: dict[str, Any], record: dict[str, Any]) -> tuple[bool, list[str]]:
    checks: list[tuple[bool, str]] = []
    sources = set(map(str, rule.get("sources", [])))
    if sources:
        checks.append((record.get("source_id") in sources or record.get("source_name") in sources, "source"))
    types = set(map(str, rule.get("types", [])))
    if types:
        checks.append((record.get("type") in types, "type"))
    tags = {str(tag).lower() for tag in record.get("tags", [])}
    tags_any = {str(tag).lower() for tag in rule.get("tags_any", [])}
    tags_all = {str(tag).lower() for tag in rule.get("tags_all", [])}
    if tags_any:
        checks.append((bool(tags & tags_any), "tags_any"))
    if tags_all:
        checks.append((tags_all <= tags, "tags_all"))
    if rule.get("min_rating") is not None:
        checks.append((int(record.get("rating", 0)) >= int(rule["min_rating"]), "rating"))
    if rule.get("min_complexity") is not None:
        checks.append((int(record.get("complexity", 0)) >= int(rule["min_complexity"]), "complexity"))
    if rule.get("max_age_minutes"):
        minimum = utc_now().timestamp() - int(rule["max_age_minutes"]) * 60
        checks.append((float(record.get("published_ts", 0)) >= minimum, "age"))
    blob = " ".join([record.get("title", ""), record.get("summary", ""), record.get("raw", ""), " ".join(record.get("tags", []))])
    keywords = [str(value).lower() for value in rule.get("keywords", [])]
    if keywords:
        checks.append((any(value in blob.lower() for value in keywords), "keyword"))
    if rule.get("regex"):
        try:
            checks.append((bool(re.search(str(rule["regex"]), blob, re.IGNORECASE)), "regex"))
        except re.error:
            return False, ["invalid_regex"]
    if not checks:
        return False, ["no_conditions"]
    matched = all(value for value, _ in checks) if str(rule.get("operator", "AND")).upper() == "AND" else any(value for value, _ in checks)
    return matched, [name for value, name in checks if value]


def freshness_score(record: dict[str, Any]) -> float:
    age_hours = max(0.0, (utc_now().timestamp() - float(record.get("published_ts", utc_now().timestamp()))) / 3600)
    return max(0.0, 100.0 - min(100.0, age_hours * 4.0))


def candidate_score(record: dict[str, Any], rule: dict[str, Any], confidence: float = 0.0) -> float:
    return round(
        0.45 * float(record.get("rating", 0))
        + 0.20 * freshness_score(record)
        + 0.15 * float(record.get("novelty", record.get("rating", 0)))
        + 0.10 * float(rule.get("source_weight", 50))
        + 0.10 * max(0.0, min(100.0, confidence * 100)),
        4,
    )


async def ai_json(system: str, user: str, function_name: str) -> tuple[dict[str, Any], dict[str, int], float]:
    provider = _app.state.provider_state
    base = str(provider.get("base_url", "")).rstrip("/")
    key, model = str(provider.get("api_key", "")), str(provider.get("model", ""))
    if not base or not key:
        raise RuntimeError("AI provider is not configured")
    root = base.removesuffix("/v1") if "api.perplexity.ai" in base else (base if base.endswith("/v1") else f"{base}/v1")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    response = await _app.state.http_client.post(
        f"{root}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.35, "response_format": {"type": "json_object"}},
        timeout=45.0,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    content = content.removeprefix("```json").removesuffix("```").strip()
    result = json.loads(content)
    raw_usage = payload.get("usage", {})
    prompt_tokens = int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or estimate_tokens(json.dumps(messages, ensure_ascii=False)))
    completion_tokens = int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or estimate_tokens(content))
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": int(raw_usage.get("total_tokens") or prompt_tokens + completion_tokens)}
    cost = (prompt_tokens * float(provider.get("input_price_per_1m", 0)) + completion_tokens * float(provider.get("output_price_per_1m", 0))) / 1_000_000
    if _record_usage:
        await _record_usage(str(provider.get("name", "provider")), model, function_name, usage, cost)
    return result, usage, cost


def fallback_post(record: dict[str, Any], mode: str, artifact_type: str | None) -> dict[str, str]:
    title, summary = record.get("title", "Новый сигнал"), record.get("summary", "")
    source = record.get("path") or record.get("source_url") or record.get("source_name", "")
    if mode == "step_by_step":
        text = f"{title}\n\nРазбираю по шагам.\n\n1. Что произошло: {summary}\n2. Почему я обратил внимание: рейтинг {record.get('rating', 0)}/100.\n3. Что проверю: источник, воспроизводимость и пользу.\n\nМой вывод: сохраняю как рабочий материал, а не как шум.\n\nИсточник: {source}"
    elif mode == "roast":
        text = f"{title}\n\nСначала прожарка. {summary}\n\nЧто здесь хорошо: идею можно проверить.\nЧто раздражает: без примеров это превращается в обёртку.\nЧто должно исчезнуть: повторы и неподтверждённые обещания.\n\nИсточник: {source}"
    elif mode == "my_take":
        text = f"{title}\n\nМои мысли: {summary}\n\nДля меня вопрос не в моде, а в том, какую реальную работу это убирает. Вывод сделаю после проверки.\n\nИсточник: {source}"
    elif mode == "artifact_from_source":
        kind = artifact_type or "prompt"
        text = f"{title}\n\nСобрал {kind} по мотивам источника.\n\n# {kind.title()}: {title}\nЦель: превратить сигнал в проверяемый процесс.\nШаги: проверить предпосылки, выполнить минимальный эксперимент, записать результат, удалить лишнее.\n\nИсточник: {source}"
    else:
        text = f"{title}\n\n{summary}\n\nКиллер-фича проявится там, где материал сокращает путь до проверяемого результата. Ритуалы, повторы и искусственная сложность должны исчезнуть.\n\nИсточник: {source}"
    return {"title": title, "text": text, "artifact_markdown": text if mode == "artifact_from_source" else ""}


async def generate_post(record: dict[str, Any], mode: str, style: dict[str, Any] | None, artifact_type: str | None) -> tuple[dict[str, Any], dict[str, int], float]:
    fallback = fallback_post(record, mode, artifact_type)
    style_card = json.dumps((style or {}).get("style_card", {}), ensure_ascii=False)
    system = f"Пиши Telegram-пост от первого лица по-русски. Не выдумывай факты. Режим только {mode}. Тип артефакта: {artifact_type or 'нет'}. Стиль: {style_card}. Верни JSON {{title,text,artifact_markdown}}."
    try:
        result, usage, cost = await ai_json(system, json.dumps(record, ensure_ascii=False), f"post_{mode}")
        return {**fallback, **result}, usage, cost
    except Exception as exc:
        logging.warning("Post generation fallback: %s", exc)
        return fallback, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0.0


async def create_draft(record: dict[str, Any], config: dict[str, Any], status_value: str = "review") -> dict[str, Any]:
    mode, artifact_type = str(config.get("mode", "my_take")), config.get("artifact_type") or None
    if mode not in POST_MODES:
        raise ValueError("Unsupported mode")
    if mode == "artifact_from_source" and artifact_type not in ARTIFACT_TYPES:
        raise ValueError("artifact_type is required")
    style = await get_item(STYLES_KEY, str(config.get("style_id", ""))) if config.get("style_id") else None
    generated, usage, cost = await generate_post(record, mode, style, artifact_type)
    draft = {
        "id": new_id("draft"), "artifact_id": record.get("id"), "channel_id": config.get("channel_id", ""),
        "rule_id": config.get("id", ""), "style_id": config.get("style_id", ""), "mode": mode,
        "artifact_type": artifact_type or "", "title": generated["title"], "text": generated["text"],
        "artifact_markdown": generated.get("artifact_markdown", ""), "status": status_value, "public": False,
        "scheduled_at": None, "telegram_message_id": None, "telegram_link": "", "tokens": usage,
        "cost_usd": round(cost, 6), "model": _app.state.provider_state.get("model", "fallback"),
        "source": {"id": record.get("id"), "title": record.get("title"), "summary": record.get("summary"), "source_name": record.get("source_name")},
        "history": [{"at": iso_now(), "action": "generated"}], "created_at": iso_now(), "updated_at": iso_now(),
    }
    await save_item(DRAFTS_KEY, draft)
    await event("draft_created", draft_id=draft["id"], artifact_id=record.get("id"), rule_id=config.get("id", ""))
    return draft


async def telegram_request(method: str, data: dict[str, Any], files: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _app.state.config.telegram_bot_token
    if not token or token == "your-bot-token-here":
        raise RuntimeError("Telegram bot token is not configured")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await _app.state.http_client.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=20.0)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload))
            return payload["result"]
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)
    raise RuntimeError(str(last_error))


async def rate_allowed(channel_id: str, kind: str) -> bool:
    now = utc_now().timestamp()
    window, limit = (3600, 10) if kind == "live" else (86400, 3)
    key = f"{RATE_PREFIX}:{kind}:{channel_id}"
    await _app.state.redis.zremrangebyscore(key, 0, now - window)
    if await _app.state.redis.zcard(key) >= limit:
        return False
    await _app.state.redis.zadd(key, {secrets.token_hex(8): now})
    await _app.state.redis.expire(key, window + 60)
    return True


async def send(channel: dict[str, Any], text: str, idem: str, kind: str, document: str = "") -> dict[str, Any]:
    idem_key = f"{SENT_PREFIX}:{hashlib.sha256(idem.encode()).hexdigest()}"
    if not await _app.state.redis.set(idem_key, "1", nx=True, ex=30 * 86400):
        raise RuntimeError("Duplicate publication blocked")
    try:
        result = await telegram_request("sendMessage", {"chat_id": channel["chat_id"], "text": text[:4096], "parse_mode": "HTML"})
        if document:
            await telegram_request("sendDocument", {"chat_id": channel["chat_id"], "caption": "Артефакт по мотивам источника"}, {"document": ("artifact.md", document.encode(), "text/markdown")})
        return result
    except Exception:
        await _app.state.redis.delete(idem_key)
        raise


def message_link(channel: dict[str, Any], message_id: int | None) -> str:
    username = str(channel.get("username", "")).lstrip("@")
    return f"https://t.me/{username}/{message_id}" if username and message_id else ""


async def make_public(draft: dict[str, Any]) -> None:
    item = {key: draft.get(key) for key in ("id", "title", "text", "mode", "artifact_type", "artifact_markdown", "telegram_link", "published_at")}
    item["source_name"] = draft.get("source", {}).get("source_name", "")
    await save_item(PUBLIC_KEY, item)
    await _app.state.redis.zadd(PUBLIC_ORDER_KEY, {item["id"]: utc_now().timestamp()})


async def publish_draft(draft: dict[str, Any], kind: str = "manual") -> dict[str, Any]:
    channel = await get_item(CHANNELS_KEY, draft.get("channel_id", ""))
    if not channel or not channel.get("enabled", True):
        raise RuntimeError("Destination channel is missing or disabled")
    if kind == "scheduled":
        if not await rate_allowed(channel["id"], "scheduled") or await _app.state.redis.exists(f"{RATE_PREFIX}:cooldown:{channel['id']}"):
            raise RuntimeError("Scheduled limit or cooldown reached")
    
    # Expand daily PIN & date macros
    redis = _app.state.redis if _app else None
    daily_pin = await get_daily_pin(redis)
    today_date = get_today_date_str()
    text = (draft.get("text", "") or "").replace("{daily_pin}", daily_pin).replace("{today_pin}", daily_pin).replace("{daily_date}", today_date)
    draft["text"] = text

    result = await send(channel, html.escape(text, quote=False), f"draft:{draft['id']}:{channel['id']}", kind, draft.get("artifact_markdown", ""))
    draft.update({"status": "published", "telegram_message_id": result.get("message_id"), "telegram_link": message_link(channel, result.get("message_id")), "published_at": iso_now(), "updated_at": iso_now()})
    draft["history"].append({"at": iso_now(), "action": "published", "kind": kind})
    await save_item(DRAFTS_KEY, draft)
    if kind == "scheduled":
        await _app.state.redis.setex(f"{RATE_PREFIX}:cooldown:{channel['id']}", 7200, "1")
    if draft.get("public"):
        await make_public(draft)
    await event("draft_published", draft_id=draft["id"], channel_id=channel["id"], kind=kind)
    return draft


async def review(record: dict[str, Any], rule: dict[str, Any], reason: str) -> None:
    draft = await create_draft(record, rule)
    draft["review_reason"] = reason
    await save_item(DRAFTS_KEY, draft)
    await event("live_review", draft_id=draft["id"], rule_id=rule["id"], reason=reason)


async def ai_match(rule: dict[str, Any], record: dict[str, Any]) -> tuple[bool, float, str]:
    result, _, _ = await ai_json("Верни JSON {matched:boolean,confidence:0..1,reason:string}.", json.dumps({"criterion": rule.get("ai_prompt"), "artifact": record}, ensure_ascii=False), "live_classifier")
    confidence = float(result.get("confidence", 0))
    return bool(result.get("matched")) and confidence >= float(rule.get("ai_threshold", 0.85)), confidence, str(result.get("reason", ""))


async def live_flash(record: dict[str, Any], rule: dict[str, Any], reasons: list[str], ai_reason: str) -> None:
    channel = await get_item(CHANNELS_KEY, rule.get("channel_id", ""))
    if not channel or not channel.get("enabled", True):
        await review(record, rule, "channel_missing_or_disabled")
        return
    if not await rate_allowed(channel["id"], "live"):
        await review(record, rule, "live_rate_limit")
        return
    source = record.get("path") or record.get("source_url") or record.get("source_name", "")
    why = ", ".join(reasons + ([ai_reason] if ai_reason else []))
    text = f"<b>LIVE / {html.escape(record.get('type', 'signal'))}</b>\n\n<b>{html.escape(record.get('title', 'Новый сигнал'))}</b>\n{html.escape(record.get('summary', ''))}\n\nПочему: <code>{html.escape(why)}</code>\nРейтинг: {record.get('rating', 0)}/100\nИсточник: {html.escape(str(source))}"
    try:
        result = await send(channel, text, f"live:{record.get('id')}:{rule['id']}:{channel['id']}", "live")
        await event("live_published", artifact_id=record.get("id"), rule_id=rule["id"], message_id=result.get("message_id"))
    except Exception as exc:
        await review(record, rule, f"telegram_error:{exc}")


async def evaluate_ingested_record(record: dict[str, Any]) -> None:
    if _app is None:
        return
    for rule in await list_items(RULES_KEY):
        if not rule.get("enabled"):
            continue
        matched, reasons = rule_matches(rule, record)
        if not matched:
            continue
        confidence, ai_reason = 0.0, ""
        if rule.get("ai_enabled"):
            try:
                matched, confidence, ai_reason = await ai_match(rule, record)
            except Exception as exc:
                if rule.get("live_enabled"):
                    await review(record, rule, f"ai_unavailable:{exc}")
                continue
            if not matched:
                continue
        if rule.get("live_enabled"):
            await live_flash(record, rule, reasons, ai_reason)
        if rule.get("scheduled_enabled"):
            score = candidate_score(record, rule, confidence)
            member = json.dumps({"artifact_id": record.get("id"), "queued_at": iso_now()}, ensure_ascii=False)
            await _app.state.redis.zadd(f"{CANDIDATES_PREFIX}:{rule['id']}", {member: score})
            await event("candidate_queued", artifact_id=record.get("id"), rule_id=rule["id"], score=score)


async def sync_style_profile(style: dict[str, Any]) -> dict[str, Any]:
    client = getattr(_app.state, "telethon_client", None)
    if not client or not client.is_connected():
        raise RuntimeError("Telethon client is unavailable")
    messages = []
    async for message in client.iter_messages(style["source_channel"], limit=50):
        if message.message:
            messages.append(message.message)
    digest = hashlib.sha256("\n---\n".join(messages).encode()).hexdigest()
    if digest == style.get("messages_hash"):
        return style
    try:
        card, _, _ = await ai_json("Верни style card JSON с полями tone,length,structure,phrases,sharpness,headings,vocabulary,taboos. Не копируй тексты.", "\n---\n".join(messages), "style_sync")
    except Exception:
        if style.get("style_card"):
            raise
        card = {"tone": "прямой", "structure": "тезис → разбор → вывод", "taboos": ["канцелярит", "рекламные клише"]}
    style.update({"style_card": card, "messages_hash": digest, "sample_count": len(messages), "last_synced_at": iso_now(), "updated_at": iso_now()})
    await save_item(STYLES_KEY, style)
    await event("style_synced", style_id=style["id"], sample_count=len(messages))
    return style


async def broadcast_daily_pin_announcement(force: bool = False) -> dict[str, Any]:
    """Broadcasts today's Daily PIN to all active public channels in Studio."""
    redis = _app.state.redis if _app else None
    today_date = get_today_date_str()
    daily_pin = await get_daily_pin(redis, today_date)

    total_prompts = 0
    recent_24h = 0
    if redis:
        try:
            total_prompts = await redis.zcard("promptops:prompts:order")
            cutoff = utc_now().timestamp() - 86400
            recent_24h = await redis.zcount("promptops:prompts:order", cutoff, "+inf")
        except Exception:
            pass

    channels = await list_items(CHANNELS_KEY)
    active_channels = [c for c in channels if c.get("enabled", True)]

    if not active_channels:
        return {
            "success": False,
            "message": "Нет активных каналов для публикации в Publishing Studio",
            "date": today_date,
            "pin": daily_pin,
            "total_channels": 0,
            "results": [],
        }

    stats_line = f"📊 В каталоге: <b>{total_prompts}</b> промптов" + (f" (<b>+{recent_24h}</b> за 24ч)" if recent_24h > 0 else "")
    text = (
        f"🔑 <b>DAILY PASS // КОД ДНЯ: <code>{daily_pin}</code></b>\n\n"
        f"📅 Дата: <b>{today_date}</b> (MSK)\n"
        f"{stats_line}\n\n"
        f"Введите 4-значный код <code>{daily_pin}</code> в веб-интерфейсе для полного доступа к каталогу, "
        f"экспорту и AI-аналитике.\n\n"
        f"🌐 <b>TUI Каталог:</b> https://8.0x101.lol\n"
        f"⚡ <b>Lite витрина:</b> https://8.0x101.lol/lite\n\n"
        f"#DailyPass #PromptOps #AI"
    )

    results = []
    for ch in active_channels:
        idem = f"daily_pin:{today_date}:{ch['id']}" if not force else f"daily_pin:{today_date}:{ch['id']}:{secrets.token_hex(4)}"
        try:
            res = await send(ch, text, idem, "daily_pass_broadcast")
            results.append({"channel_id": ch["id"], "username": ch.get("username"), "status": "sent", "message_id": res.get("message_id")})
            await event("daily_pin_broadcast", channel_id=ch["id"], date=today_date, pin=daily_pin)
        except Exception as exc:
            results.append({"channel_id": ch["id"], "username": ch.get("username"), "status": "error", "error": str(exc)})

    return {
        "success": any(r.get("status") == "sent" for r in results),
        "date": today_date,
        "pin": daily_pin,
        "total_channels": len(active_channels),
        "results": results,
    }


async def scheduler_tick() -> None:
    now = utc_now()
    for draft_id in await _app.state.redis.zrangebyscore(SCHEDULE_KEY, 0, now.timestamp()):
        await _app.state.redis.zrem(SCHEDULE_KEY, draft_id)
        draft = await get_item(DRAFTS_KEY, draft_id)
        if draft and draft.get("status") == "scheduled":
            try:
                await publish_draft(draft, "scheduled")
            except Exception as exc:
                draft.update({"status": "review", "review_reason": str(exc)})
                await save_item(DRAFTS_KEY, draft)
    local, slot = datetime.now(MOSCOW), datetime.now(MOSCOW).strftime("%H:%M")
    
    # 09:00 MSK Daily PIN broadcast
    if slot == "09:00":
        daily_broadcast_lock = f"promptops:publishing:daily_pin_broadcast:{local:%Y-%m-%d}"
        if await _app.state.redis.set(daily_broadcast_lock, "1", nx=True, ex=93600):
            try:
                await broadcast_daily_pin_announcement(force=False)
            except Exception as exc:
                logging.error("Daily PIN broadcast failed: %s", exc)

    for rule in await list_items(RULES_KEY):
        if not rule.get("enabled") or not rule.get("scheduled_enabled") or slot not in rule.get("schedule_slots", []):
            continue
        lock = f"promptops:publishing:slot:{rule['id']}:{local:%Y-%m-%d}:{slot}"
        if not await _app.state.redis.set(lock, "1", nx=True, ex=93600):
            continue
        popped = await _app.state.redis.zpopmax(f"{CANDIDATES_PREFIX}:{rule['id']}", 1)
        if not popped:
            continue
        candidate = json.loads(popped[0][0])
        records = await _load_records([candidate["artifact_id"]]) if _load_records else []
        if records:
            draft = await create_draft(records[0], rule)
            if rule.get("approval_mode") == "auto":
                try:
                    await publish_draft(draft, "scheduled")
                except Exception as exc:
                    draft["review_reason"] = str(exc)
                    await save_item(DRAFTS_KEY, draft)


async def scheduler_loop() -> None:
    ticks = 0
    while True:
        try:
            await scheduler_tick()
            if ticks % 20160 == 0:
                for style in await list_items(STYLES_KEY):
                    last = datetime.fromisoformat(style["last_synced_at"]) if style.get("last_synced_at") else None
                    if not last or utc_now() - last >= timedelta(days=7):
                        try:
                            await sync_style_profile(style)
                        except Exception as exc:
                            logging.warning("Style sync failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("Publishing scheduler error: %s", exc)
        ticks += 1
        await asyncio.sleep(30)


def normalize_rule(payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    item = {**(current or {}), **payload}
    defaults = {"id": new_id("rule"), "name": "Publishing rule", "enabled": False, "operator": "AND", "sources": [], "types": [], "tags_any": [], "tags_all": [], "keywords": [], "min_rating": 70, "min_complexity": None, "max_age_minutes": 1440, "regex": "", "ai_enabled": False, "ai_prompt": "", "ai_threshold": 0.85, "live_enabled": False, "scheduled_enabled": False, "schedule_slots": ["10:00", "15:00", "20:00"], "approval_mode": "review", "mode": "my_take", "artifact_type": "", "source_weight": 50, "channel_id": "", "style_id": "", "created_at": iso_now()}
    for key, value in defaults.items():
        item.setdefault(key, value)
    if item["mode"] not in POST_MODES:
        raise ValueError("Unsupported mode")
    if item["mode"] == "artifact_from_source" and item.get("artifact_type") not in ARTIFACT_TYPES:
        raise ValueError("artifact_type is required")
    if item.get("live_enabled") and not item.get("channel_id"):
        raise ValueError("channel_id is required for live")
    item["updated_at"] = iso_now()
    return item


@router.get("/api/channels")
async def channels(_: str = Depends(authenticate)) -> JSONResponse:
    return JSONResponse({"items": await list_items(CHANNELS_KEY)})


@router.post("/api/channels")
async def add_channel(payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    if not payload.get("name") or not payload.get("chat_id"):
        raise HTTPException(400, "name and chat_id are required")
    item = {"id": str(payload.get("id") or new_id("channel")), "name": str(payload["name"]), "chat_id": str(payload["chat_id"]), "username": str(payload.get("username", "")), "purpose": str(payload.get("purpose", "publishing")), "enabled": bool(payload.get("enabled", True)), "test_mode": bool(payload.get("test_mode", False)), "style_source_channel": str(payload.get("style_source_channel", "")), "created_at": iso_now(), "updated_at": iso_now()}
    await save_item(CHANNELS_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.patch("/api/channels/{item_id}")
async def edit_channel(item_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(CHANNELS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Channel not found")
    item.update({key: value for key, value in payload.items() if key not in {"id", "created_at"}})
    item["updated_at"] = iso_now()
    await save_item(CHANNELS_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.delete("/api/channels/{item_id}")
async def remove_channel(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    await _app.state.redis.hdel(CHANNELS_KEY, item_id)
    return JSONResponse({"ok": True})


@router.post("/api/channels/{item_id}/test")
async def test_channel(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    channel = await get_item(CHANNELS_KEY, item_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    result = await telegram_request("sendMessage", {"chat_id": channel["chat_id"], "text": "Prompt Ops: тест канала успешен."})
    return JSONResponse({"ok": True, "message_id": result.get("message_id")})


@router.get("/api/styles")
async def styles(_: str = Depends(authenticate)) -> JSONResponse:
    return JSONResponse({"items": await list_items(STYLES_KEY)})


@router.post("/api/styles")
async def add_style(payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    if not payload.get("name") or not payload.get("source_channel"):
        raise HTTPException(400, "name and source_channel are required")
    item = {"id": str(payload.get("id") or new_id("style")), "name": str(payload["name"]), "source_channel": str(payload["source_channel"]), "style_card": payload.get("style_card", {}), "messages_hash": "", "sample_count": 0, "last_synced_at": None, "created_at": iso_now(), "updated_at": iso_now()}
    await save_item(STYLES_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.patch("/api/styles/{item_id}")
async def edit_style(item_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(STYLES_KEY, item_id)
    if not item:
        raise HTTPException(404, "Style not found")
    item.update({key: value for key, value in payload.items() if key not in {"id", "created_at"}})
    item["updated_at"] = iso_now()
    await save_item(STYLES_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.delete("/api/styles/{item_id}")
async def remove_style(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    await _app.state.redis.hdel(STYLES_KEY, item_id)
    return JSONResponse({"ok": True})


@router.post("/api/styles/{item_id}/sync")
async def sync_style(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(STYLES_KEY, item_id)
    if not item:
        raise HTTPException(404, "Style not found")
    return JSONResponse({"ok": True, "item": await sync_style_profile(item)})


@router.get("/api/styles/{item_id}/preview")
async def preview_style(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(STYLES_KEY, item_id)
    if not item:
        raise HTTPException(404, "Style not found")
    return JSONResponse({"id": item_id, "name": item.get("name"), "style_card": item.get("style_card", {}), "sample_count": item.get("sample_count", 0), "last_synced_at": item.get("last_synced_at")})


@router.get("/api/post-drafts")
async def drafts(_: str = Depends(authenticate)) -> JSONResponse:
    items = await list_items(DRAFTS_KEY)
    return JSONResponse({"items": sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)})


@router.post("/api/post-drafts")
async def add_draft(payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    records = await _load_records([str(payload.get("artifact_id", ""))]) if _load_records else []
    if not records:
        raise HTTPException(404, "Artifact not found")
    try:
        item = await create_draft(records[0], payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse({"ok": True, "item": item})


@router.get("/api/post-drafts/{item_id}")
async def get_draft(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    return JSONResponse(item)


@router.patch("/api/post-drafts/{item_id}")
async def edit_draft(item_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    changes = {key: value for key, value in payload.items() if key in {"title", "text", "channel_id", "style_id", "public", "artifact_markdown"}}
    item.update(changes)
    item["updated_at"] = iso_now()
    item["history"].append({"at": iso_now(), "action": "edited", "fields": sorted(changes)})
    await save_item(DRAFTS_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/post-drafts/{item_id}/regenerate")
async def regenerate(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    records = await _load_records([item["artifact_id"]]) if item and _load_records else []
    if not item or not records:
        raise HTTPException(404, "Draft or artifact not found")
    style = await get_item(STYLES_KEY, item.get("style_id", "")) if item.get("style_id") else None
    generated, usage, cost = await generate_post(records[0], item["mode"], style, item.get("artifact_type") or None)
    item.update({**generated, "tokens": usage, "cost_usd": cost, "updated_at": iso_now()})
    item["history"].append({"at": iso_now(), "action": "regenerated"})
    await save_item(DRAFTS_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/post-drafts/{item_id}/approve")
async def approve(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    item.update({"status": "approved", "updated_at": iso_now()})
    item["history"].append({"at": iso_now(), "action": "approved"})
    await save_item(DRAFTS_KEY, item)
    await event("draft_approved", draft_id=item_id)
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/post-drafts/{item_id}/publish")
async def publish(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    try:
        item = await publish_draft(item)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/post-drafts/{item_id}/schedule")
async def schedule(item_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    try:
        when = datetime.fromisoformat(str(payload["scheduled_at"]).replace("Z", "+00:00"))
    except Exception as exc:
        raise HTTPException(400, "Invalid scheduled_at") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=MOSCOW)
    when = when.astimezone(timezone.utc)
    item.update({"scheduled_at": when.isoformat(), "status": "scheduled", "updated_at": iso_now()})
    await save_item(DRAFTS_KEY, item)
    await _app.state.redis.zadd(SCHEDULE_KEY, {item_id: when.timestamp()})
    return JSONResponse({"ok": True, "item": item})


@router.post("/api/post-drafts/{item_id}/reject")
async def reject(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    item = await get_item(DRAFTS_KEY, item_id)
    if not item:
        raise HTTPException(404, "Draft not found")
    item.update({"status": "rejected", "updated_at": iso_now()})
    item["history"].append({"at": iso_now(), "action": "rejected"})
    await save_item(DRAFTS_KEY, item)
    await _app.state.redis.zrem(SCHEDULE_KEY, item_id)
    return JSONResponse({"ok": True, "item": item})


@router.get("/api/publishing-rules")
async def rules(_: str = Depends(authenticate)) -> JSONResponse:
    return JSONResponse({"items": await list_items(RULES_KEY)})


@router.post("/api/publishing-rules")
async def add_rule(payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    try:
        item = normalize_rule(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await save_item(RULES_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.patch("/api/publishing-rules/{item_id}")
async def edit_rule(item_id: str, payload: dict[str, Any] = Body(...), _: str = Depends(authenticate)) -> JSONResponse:
    current = await get_item(RULES_KEY, item_id)
    if not current:
        raise HTTPException(404, "Rule not found")
    try:
        item = normalize_rule({**payload, "id": item_id}, current)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await save_item(RULES_KEY, item)
    return JSONResponse({"ok": True, "item": item})


@router.delete("/api/publishing-rules/{item_id}")
async def remove_rule(item_id: str, _: str = Depends(authenticate)) -> JSONResponse:
    await _app.state.redis.hdel(RULES_KEY, item_id)
    await _app.state.redis.delete(f"{CANDIDATES_PREFIX}:{item_id}")
    return JSONResponse({"ok": True})


@router.post("/api/publishing-rules/{item_id}/dry-run")
async def dry_run(item_id: str, payload: dict[str, Any] = Body(default={}), _: str = Depends(authenticate)) -> JSONResponse:
    rule = await get_item(RULES_KEY, item_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    records = await _load_records(list(map(str, payload.get("artifact_ids", [])))) if _load_records else []
    items = []
    for record in records:
        matched, reasons = rule_matches(rule, record)
        items.append({"artifact_id": record.get("id"), "matched": matched, "reasons": reasons, "score": candidate_score(record, rule) if matched else 0})
    return JSONResponse({"items": items})


@router.get("/api/publishing/events")
async def events(limit: int = 100, _: str = Depends(authenticate)) -> JSONResponse:
    items = []
    for event_id, fields in await _app.state.redis.xrevrange(EVENTS_KEY, count=min(max(limit, 1), 500)):
        item = {"id": event_id}
        for key, value in fields.items():
            try:
                item[key] = json.loads(value)
            except Exception:
                item[key] = value
        items.append(item)
    return JSONResponse({"items": items})


@router.post("/api/publishing/broadcast-daily-pin")
async def api_broadcast_daily_pin(_: str = Depends(authenticate)) -> JSONResponse:
    """Admin endpoint to broadcast today's Daily PIN to all active channels."""
    result = await broadcast_daily_pin_announcement(force=True)
    return JSONResponse(result)


@router.get("/api/public/feed")
async def public_feed(q: str = "", mode: str = "") -> JSONResponse:
    ids = await _app.state.redis.zrevrange(PUBLIC_ORDER_KEY, 0, 199)
    items = [item for item in [await get_item(PUBLIC_KEY, item_id) for item_id in ids] if item]
    if q:
        items = [item for item in items if q.lower() in f"{item.get('title')} {item.get('text')} {item.get('source_name')}".lower()]
    if mode:
        items = [item for item in items if item.get("mode") == mode]
    return JSONResponse({"items": items, "count": len(items)})


@router.get("/api/public/posts/{item_id}")
async def public_post(item_id: str) -> JSONResponse:
    item = await get_item(PUBLIC_KEY, item_id)
    if not item:
        raise HTTPException(404, "Post not found")
    return JSONResponse(item)


@router.get("/studio", response_class=HTMLResponse)
async def studio(_: str = Depends(authenticate)) -> HTMLResponse:
    return HTMLResponse(Path("studio/index.html").read_text(encoding="utf-8"))


@router.get("/lite", response_class=HTMLResponse)
async def lite() -> HTMLResponse:
    return HTMLResponse(Path("lite/index.html").read_text(encoding="utf-8"))
