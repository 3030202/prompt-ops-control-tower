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


def compute_deterministic_pin(date_str: str) -> str:
    """Computes a stable 4-digit PIN for a given YYYY-MM-DD using HMAC-SHA256."""
    h = hmac.new(DAILY_PASS_SECRET.encode("utf-8"), date_str.encode("utf-8"), hashlib.sha256).hexdigest()
    num = int(h[:8], 16) % 10000
    return f"{num:04d}"


async def get_daily_pin(redis: Any, date_str: Optional[str] = None) -> str:
    """Returns today's PIN, checking for manual Redis override first, then fallback to algorithm."""
    target_date = date_str or get_today_date_str()
    if redis:
        try:
            custom = await redis.get(f"{CUSTOM_PIN_KEY_PREFIX}:{target_date}")
            if custom:
                custom_str = custom.decode("utf-8") if isinstance(custom, bytes) else str(custom)
                custom_str = custom_str.strip()
                if len(custom_str) == 4 and custom_str.isdigit():
                    return custom_str
        except Exception:
            pass
    return compute_deterministic_pin(target_date)


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
    """Validates entered 4-digit PIN against today's PIN with rate-limiting."""
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

    today_str = get_today_date_str()
    correct_pin = await get_daily_pin(redis, today_str)

    if pin != correct_pin:
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
            detail="Неверный код доступа. Возьмите актуальный 4-значный код в Telegram-канале.",
        )

    # Valid PIN! Clear rate limit for this IP
    if redis:
        try:
            await redis.delete(rate_key)
        except Exception:
            pass

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
        "message": "Доступ успешно открыт на сегодня!",
    }


@router.get("/admin/today")
async def daily_pass_admin_today(_: str = Depends(authenticate_admin)) -> dict[str, Any]:
    """Admin endpoint: returns today's and upcoming PINs for Telegram posting."""
    redis = getattr(_app.state, "redis", None) if _app else None
    today_str = get_today_date_str()
    tomorrow_str = get_today_date_str(1)

    today_pin = await get_daily_pin(redis, today_str)
    tomorrow_pin = await get_daily_pin(redis, tomorrow_str)

    custom_today = None
    if redis:
        try:
            custom_today = await redis.get(f"{CUSTOM_PIN_KEY_PREFIX}:{today_str}")
        except Exception:
            pass

    upcoming = []
    for offset in range(7):
        d_str = get_today_date_str(offset)
        pin_val = await get_daily_pin(redis, d_str)
        upcoming.append({"date": d_str, "pin": pin_val, "offset": offset})

    return {
        "today_date": today_str,
        "today_pin": today_pin,
        "tomorrow_date": tomorrow_str,
        "tomorrow_pin": tomorrow_pin,
        "is_custom": bool(custom_today),
        "upcoming": upcoming,
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
