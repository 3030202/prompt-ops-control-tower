import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

router = APIRouter(prefix="/api/daily-pass", tags=["daily-pass"])
security = HTTPBasic()
MOSCOW = ZoneInfo("Europe/Moscow")

DAILY_PASS_SECRET = os.getenv("DAILY_PASS_SECRET", "promptops-control-tower-daily-pin-secret-key-2026")
TELEGRAM_PUBLIC_CHANNEL = os.getenv("TELEGRAM_PUBLIC_CHANNEL", "https://t.me/desp0tat")
CUSTOM_PIN_KEY_PREFIX = "promptops:daily_pin:custom"
RATE_LIMIT_PREFIX = "promptops:daily_pin:rate"

_app: Any = None


def configure_daily_pass(app: Any) -> None:
    global _app
    _app = app


def get_moscow_now() -> datetime:
    return datetime.now(MOSCOW)


def get_today_date_str(offset_days: int = 0) -> str:
    now = get_moscow_now()
    if offset_days != 0:
        now = now + timedelta(days=offset_days)
    return now.strftime("%Y-%m-%d")


def seconds_until_midnight_msk() -> int:
    now = get_moscow_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Add 1 hour grace window so late-night visitors are not abruptly disconnected
    seconds = int((tomorrow - now).total_seconds()) + 3600
    return max(3600, seconds)


def compute_time_based_pin_prefix() -> str:
    """Returns the 3-digit time-based PIN prefix: HH (MSK hour) + tens-of-minutes.

    Example: at 16:49 MSK → '164'. Any 4th digit is accepted.
    The prefix changes every 10 minutes.
    """
    now = get_moscow_now()
    return f"{now.hour:02d}{now.minute // 10}"


async def get_current_pin_prefix(redis: Any) -> str:
    """Returns the current 3-digit PIN prefix (time-based), checking Redis override first.

    Redis override key: promptops:daily_pin:custom:YYYY-MM-DD  (full 4-digit PIN, takes priority).
    """
    if redis:
        try:
            target_date = get_today_date_str()
            custom = await redis.get(f"{CUSTOM_PIN_KEY_PREFIX}:{target_date}")
            if custom:
                custom_str = custom.decode("utf-8") if isinstance(custom, bytes) else str(custom)
                custom_str = custom_str.strip()
                # Custom override is a full 4-digit PIN — validate it exactly
                if len(custom_str) == 4 and custom_str.isdigit():
                    return custom_str  # will be compared as exact match in verify
        except Exception:
            pass
    return compute_time_based_pin_prefix()


def sign_daily_token(date_str: str) -> str:
    """Signs a daily access token for a specific date."""
    signature = hmac.new(
        DAILY_PASS_SECRET.encode("utf-8"),
        f"daily_pass:{date_str}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"{date_str}.{signature}"


def verify_daily_token(token: Optional[str]) -> bool:
    """Verifies that the provided token is valid for today (or yesterday with grace window)."""
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    token_date, _ = parts
    today_date = get_today_date_str()

    # Check today's token
    if token_date == today_date and token == sign_daily_token(today_date):
        return True

    # Check yesterday's token if we are in early morning grace period (00:00 - 03:00 MSK)
    now = get_moscow_now()
    if now.hour < 3:
        yesterday_date = get_today_date_str(-1)
        if token_date == yesterday_date and token == sign_daily_token(yesterday_date):
            return True

    return False


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not _app or not getattr(_app.state, "config", None):
        return credentials.username
    cfg = _app.state.config
    valid = secrets.compare_digest(credentials.username, cfg.dashboard_user) and secrets.compare_digest(
        credentials.password, cfg.dashboard_pass
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_daily_pass_or_admin(request: Request) -> None:
    """Enforces valid Daily Pass token or admin/mcp authorization on protected endpoints."""
    # Check HTTP Basic Auth credentials for dashboard admin
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            if _app and getattr(_app.state, "config", None):
                cfg = _app.state.config
                import base64

                encoded = auth_header[6:].strip()
                decoded = base64.b64decode(encoded).decode("utf-8")
                username, password = decoded.split(":", 1)
                if secrets.compare_digest(username, cfg.dashboard_user) and secrets.compare_digest(
                    password, cfg.dashboard_pass
                ):
                    return
        except Exception:
            pass

    # Check MCP Bearer token
    if auth_header and auth_header.startswith("Bearer "):
        mcp_key = os.getenv("MCP_API_KEY", "").strip()
        bearer_token = auth_header[7:].strip()
        if mcp_key and secrets.compare_digest(bearer_token, mcp_key):
            return

    # Check Daily Pass cookie or custom header
    cookie_token = request.cookies.get("promptops_daily_pass")
    header_token = request.headers.get("X-Daily-Pass-Token")
    token = cookie_token or header_token

    if verify_daily_token(token):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Daily pass required. Введите актуальный 4-значный код дня из Telegram-канала для доступа.",
        headers={"WWW-Authenticate": "DailyPass"},
    )


@router.get("/status")
async def daily_pass_status(request: Request) -> dict[str, Any]:
    """Returns whether the client has an active, valid daily pass token."""
    cookie_token = request.cookies.get("promptops_daily_pass")
    header_token = request.headers.get("X-Daily-Pass-Token")
    token = cookie_token or header_token
    is_valid = verify_daily_token(token)
    return {
        "unlocked": is_valid,
        "date": get_today_date_str(),
        "channel_url": os.getenv("TELEGRAM_PUBLIC_CHANNEL", TELEGRAM_PUBLIC_CHANNEL),
    }


@router.get("/channel")
async def daily_pass_channel() -> dict[str, Any]:
    """Returns the configured Telegram channel link."""
    return {
        "channel_url": os.getenv("TELEGRAM_PUBLIC_CHANNEL", TELEGRAM_PUBLIC_CHANNEL),
        "date": get_today_date_str(),
    }


@router.post("/verify")
async def daily_pass_verify(
    request: Request, response: Response, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Validates entered 4-digit PIN using time-based logic with rate-limiting.

    PIN format:
      - Digits 1–2 : current MSK hour (00–23)
      - Digit 3    : tens digit of current MSK minutes (0–5)
      - Digit 4    : any digit (always accepted)

    A Redis custom override for today's date is a full 4-digit exact PIN.
    """
    pin = str(payload.get("pin", "")).strip()
    if not pin or len(pin) != 4 or not pin.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Код должен состоять ровно из 4 цифр",
        )

    ip = get_client_ip(request)
    redis = getattr(_app.state, "redis", None) if _app else None
    rate_key = f"{RATE_LIMIT_PREFIX}:{ip}"

    # Rate limiting: max 5 failed attempts per minute
    if redis:
        try:
            attempts = await redis.get(rate_key)
            if attempts and int(attempts) >= 5:
                ttl = await redis.ttl(rate_key)
                wait_sec = max(1, ttl)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Слишком много неверных попыток. Пожалуйста, подождите {wait_sec} сек.",
                )
        except HTTPException:
            raise
        except Exception:
            pass

    expected = await get_current_pin_prefix(redis)

    # Check whether the Redis override returned a full 4-digit PIN (exact match)
    # or the time-based 3-digit prefix (any 4th digit accepted)
    if len(expected) == 4:
        # Admin-set custom PIN: exact match required
        valid = pin == expected
    else:
        # Time-based: first 3 digits must match, 4th is free
        valid = pin[:3] == expected

    if not valid:
        if redis:
            try:
                pipe = redis.pipeline()
                pipe.incr(rate_key)
                pipe.expire(rate_key, 60)
                await pipe.execute()
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный код. Первые 3 цифры — время по МСК (ЧЧ + десятки минут).",
        )

    # Valid PIN — clear rate limit for this IP
    if redis:
        try:
            await redis.delete(rate_key)
        except Exception:
            pass

    today_str = get_today_date_str()
    token = sign_daily_token(today_str)
    max_age = seconds_until_midnight_msk()

    # Set cookie in response
    response.set_cookie(
        key="promptops_daily_pass",
        value=token,
        max_age=max_age,
        httponly=False,  # Allow frontend JS to read token
        samesite="lax",
        path="/",
    )

    return {
        "success": True,
        "token": token,
        "date": today_str,
        "expires_in_seconds": max_age,
        "message": "Доступ успешно открыт!",
    }


@router.get("/admin/today")
async def daily_pass_admin_today(_: str = Depends(authenticate_admin)) -> dict[str, Any]:
    """Admin endpoint: returns current time-based PIN info."""
    redis = getattr(_app.state, "redis", None) if _app else None
    today_str = get_today_date_str()
    now = get_moscow_now()

    current_prefix = await get_current_pin_prefix(redis)
    custom_today = None
    if redis:
        try:
            custom_today = await redis.get(f"{CUSTOM_PIN_KEY_PREFIX}:{today_str}")
        except Exception:
            pass

    # Show PIN prefix for each 10-minute slot of the current hour
    slots = []
    for tens in range(6):
        minute_start = tens * 10
        slots.append({
            "time_range": f"{now.hour:02d}:{minute_start:02d}–{now.hour:02d}:{minute_start + 9:02d}",
            "prefix": f"{now.hour:02d}{tens}",
            "is_current": tens == now.minute // 10,
        })

    return {
        "today_date": today_str,
        "current_time_msk": now.strftime("%H:%M"),
        "current_prefix": current_prefix,
        "pin_hint": f"{current_prefix}X  (X — любая цифра)",
        "is_custom": bool(custom_today),
        "slots_this_hour": slots,
        "channel_url": os.getenv("TELEGRAM_PUBLIC_CHANNEL", TELEGRAM_PUBLIC_CHANNEL),
    }


@router.post("/admin/custom")
async def daily_pass_admin_set_custom(
    payload: dict[str, Any] = Body(...),
    _: str = Depends(authenticate_admin),
) -> dict[str, Any]:
    """Admin endpoint: sets or clears a custom PIN for a given date."""
    target_date = str(payload.get("date", "")).strip() or get_today_date_str()
    custom_pin = str(payload.get("pin", "")).strip()
    redis = getattr(_app.state, "redis", None) if _app else None

    if not redis:
        raise HTTPException(status_code=500, detail="Redis connection unavailable")

    key = f"{CUSTOM_PIN_KEY_PREFIX}:{target_date}"
    if not custom_pin:
        # Clear custom override -> revert to deterministic
        await redis.delete(key)
        active_pin = compute_deterministic_pin(target_date)
        return {
            "success": True,
            "date": target_date,
            "pin": active_pin,
            "is_custom": False,
            "message": "Кастомный PIN сброшен. Используется детерминированный код дня.",
        }

    if len(custom_pin) != 4 or not custom_pin.isdigit():
        raise HTTPException(status_code=400, detail="PIN должен состоять из 4 цифр")

    await redis.setex(key, 86400 * 3, custom_pin)
    return {
        "success": True,
        "date": target_date,
        "pin": custom_pin,
        "is_custom": True,
        "message": f"Кастомный PIN {custom_pin} успешно установлен на {target_date}.",
    }


@router.post("/admin/broadcast")
async def daily_pass_admin_broadcast(
    payload: dict[str, Any] = Body(default={}),
    _: str = Depends(authenticate_admin),
) -> dict[str, Any]:
    """Admin endpoint: immediately broadcasts today's Daily PIN to all active channels."""
    force = bool(payload.get("force", True))
    import publishing_studio

    return await publishing_studio.broadcast_daily_pin_announcement(force=force)
