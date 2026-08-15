import asyncio
import contextlib
import csv
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import secrets
import sys
import uuid
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import redis.asyncio as aioredis
import uvicorn
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from httpx import AsyncClient
from qdrant_client import QdrantClient, models as qmodels
from telethon import TelegramClient, events
from publishing_studio import (
    configure as configure_publishing,
    evaluate_ingested_record,
    scheduler_loop as publishing_scheduler_loop,
    router as publishing_router,
)
from daily_pass import (
    configure_daily_pass,
    require_daily_pass_or_admin,
    router as daily_pass_router,
)
from prompt_ops_mcp import PromptOpsMCPBackend, configure_mcp, mcp, mcp_http_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

load_dotenv()

security = HTTPBasic()
app = FastAPI(title="Prompt Ops Control Tower")
app.include_router(publishing_router)
app.include_router(daily_pass_router)


@app.middleware("http")
async def prompt_hostname_router(request: Request, call_next: Any) -> Response:
    prompt_hosts = {
        host.strip().lower()
        for host in os.getenv("PROMPT_ONLY_HOSTS", "8.0x101.lol,08.0x101.lol").split(",")
        if host.strip()
    }
    raw_host = request.headers.get("host") or request.url.hostname or ""
    hostname = raw_host.split(":")[0].strip().lower()
    if hostname in prompt_hosts:
        path = request.url.path
        if path == "/":
            request.scope["path"] = "/prompts"
            request.scope["raw_path"] = b"/prompts"
        else:
            public_get = request.method == "GET" and (
                path == "/health"
                or path == "/api/prompts"
                or path == "/prompts"
                or path == "/lite"
                or path.startswith("/api/public/")
                or re.fullmatch(r"/api/prompts/P-\d{6}", path)
                or path.startswith("/api/daily-pass/")
            )
            public_export = request.method == "POST" and path == "/api/prompts/export"
            public_daily_pass = request.method == "POST" and path.startswith("/api/daily-pass/")
            protected_analysis = request.method == "POST" and path == "/api/prompts/analyze"
            mcp_request = path == "/mcp" or path.startswith("/mcp/")

            # Allow Publishing Studio & related management APIs (all guarded by HTTP Basic Auth)
            studio_request = (
                path == "/studio"
                or path.startswith("/api/channels")
                or path.startswith("/api/styles")
                or path.startswith("/api/post-drafts")
                or path.startswith("/api/publishing-rules")
                or path.startswith("/api/publishing/")
            )

            if not (
                public_get
                or public_export
                or public_daily_pass
                or protected_analysis
                or mcp_request
                or studio_request
            ):
                return JSONResponse({"detail": "Prompt-only surface"}, status_code=404)
    return await call_next(request)

ARTIFACTS_KEY = "promptops:artifacts:recent"
PROMPTS_HASH_KEY = "promptops:prompts:items"
PROMPTS_ORDER_KEY = "promptops:prompts:order"
PROMPTS_SERIAL_KEY = "promptops:prompts:serial"
PROMPTS_SERIAL_INDEX_KEY = "promptops:prompts:serial-index"
PROMPTS_BODY_INDEX_KEY = "promptops:prompts:body-index"
ALERTS_KEY = "promptops:alerts:recent"
SOURCE_CATALOG_KEY = "promptops:sources:catalog"
SOURCE_DELETED_KEY = "promptops:sources:deleted"
AI_PROVIDER_KEY = "promptops:ai:provider"
AI_USAGE_PREFIX = "promptops:ai:usage"
SEEN_PREFIX = "promptops:seen"
LAST_SYNC_KEY = "promptops:last_sync_at"
QDRANT_COLLECTION = "promptops_artifacts"
MAX_RECENT_ARTIFACTS = 500
MAX_ALERTS = 250
VECTOR_DIM = 128
DATE_FMT = "%Y-%m-%dT%H:%M:%S%z"

SOURCE_EXTENSIONS = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".env"}
EXCLUDE_DIRS = {".git", "__pycache__", "sessions", "node_modules", ".venv", ".mypy_cache"}
MAX_FILE_BYTES = 80_000
MAX_SNIPPET_CHARS = 1_400
MAX_PROMPT_ITEMS = 120
MAX_PROMPT_BODY_CHARS = 6_000
MAX_PROMPT_CATALOG = 1_000
DUPE_TTL_SECONDS = 60 * 60 * 24 * 14
GALLERY_CATEGORIES = {"Prompt", "System Prompt", "Image Prompt", "Video Prompt", "NotebookLM", "Distillate", "Pipeline", "Instruction", "Skill", "Agent", "Rule"}
PROMPT_CATEGORIES = {"Prompt", "System Prompt", "Image Prompt", "Video Prompt", "NotebookLM", "Distillate"}
PUBLIC_PROMPT_SOURCE_KINDS = {"prompt_csv", "rss", "github_atom", "web_page", "x_search"}

DEFAULT_SOURCE_BLUEPRINTS = [
    {
        "id": "prompts_chat_catalog",
        "name": "prompts.chat prompt catalog",
        "kind": "prompt_csv",
        "url": "https://raw.githubusercontent.com/f/awesome-chatgpt-prompts/main/prompts.csv",
        "enabled": True,
        "artifact_group": "General Prompts",
        "recommended_interval_seconds": 3600,
        "cadence_reason": "Публичный CC0 prompt dataset опрашивается раз в час и обрабатывается пакетами.",
        "csv_offset": 0,
        "csv_batch_size": 80,
    },
    {
        "id": "cursor_latest",
        "name": "Cursor Hot",
        "kind": "rss",
        "url": "https://forum.cursor.com/latest.rss",
        "enabled": True,
        "recommended_interval_seconds": 1800,
        "cadence_reason": "Forum с высокой частотой новых сообщений.",
    },
    {
        "id": "cursor_announcements",
        "name": "Cursor Announcements",
        "kind": "rss",
        "url": "https://forum.cursor.com/c/announcements/11.rss",
        "enabled": True,
        "recommended_interval_seconds": 3600,
        "cadence_reason": "Официальные объявления меняются реже, чем общий поток.",
    },
    {
        "id": "habr_articles",
        "name": "Habr Articles",
        "kind": "rss",
        "url": "https://habr.com/ru/rss/articles/?fl=ru",
        "enabled": True,
        "recommended_interval_seconds": 3600,
        "cadence_reason": "Habr публикует стабильную новостную ленту по AI и tooling.",
    },
    {
        "id": "agents_md_commits",
        "name": "agents.md commits",
        "kind": "github_atom",
        "repo": "agentsmd/agents.md",
        "branch": "main",
        "enabled": True,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "GitHub commit feed не требует частого опроса.",
    },
    {
        "id": "agent_rules_books_commits",
        "name": "agent-rules-books commits",
        "kind": "github_atom",
        "repo": "ciembor/agent-rules-books",
        "branch": "main",
        "enabled": True,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "GitHub commit feed лучше собирать в более мягком ритме.",
    },
    {
        "id": "prompts_chat_commits",
        "name": "awesome-chatgpt-prompts commits",
        "kind": "github_atom",
        "repo": "f/prompts.chat",
        "branch": "main",
        "enabled": True,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Репозиторий с промптами обновляется не каждую минуту.",
    },
    {
        "id": "promptengineering_reddit",
        "name": "PromptEngineering Reddit",
        "kind": "rss",
        "url": "https://old.reddit.com/r/PromptEngineering/.rss",
        "enabled": False,
        "recommended_interval_seconds": 14400,
        "cadence_reason": "Reddit часто режет RSS и требует редкого опроса.",
    },
    {
        "id": "chatgptpromptgenius_reddit",
        "name": "ChatGPTPromptGenius Reddit",
        "kind": "rss",
        "url": "https://old.reddit.com/r/ChatGPTPromptGenius/.rss",
        "enabled": False,
        "recommended_interval_seconds": 14400,
        "cadence_reason": "Rate limit на Reddit делает частый poll бессмысленным.",
    },
    {
        "id": "habr_ai",
        "name": "Habr: искусственный интеллект",
        "kind": "rss",
        "url": "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru",
        "enabled": False,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Тематический хаб обновляется реже общей ленты; двух часов достаточно.",
        "preset_group": "RU / Editorial",
    },
    {
        "id": "awesome_prompts_ai_boost",
        "name": "ai-boost / awesome-prompts",
        "kind": "github_atom",
        "repo": "ai-boost/awesome-prompts",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 14400,
        "cadence_reason": "Курируемый репозиторий: четырёхчасовой poll ловит изменения без лишних запросов.",
        "preset_group": "Prompts",
    },
    {
        "id": "awesome_claude_prompts",
        "name": "awesome-claude-prompts",
        "kind": "github_atom",
        "repo": "langgptai/awesome-claude-prompts",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 14400,
        "cadence_reason": "Claude-промпты обновляются пакетами, поэтому частый опрос не нужен.",
        "preset_group": "Prompts",
    },
    {
        "id": "awesome_agent_conventions",
        "name": "awesome-agent-conventions",
        "kind": "github_atom",
        "repo": "ItamarZand88/awesome-agent-conventions",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Курируемый индекс соглашений меняется редко; шесть часов экономят трафик.",
        "preset_group": "Agent rules",
    },
    {
        "id": "official_mcp_servers",
        "name": "Official MCP servers",
        "kind": "github_atom",
        "repo": "modelcontextprotocol/servers",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Официальный MCP-репозиторий активен, но двухчасового окна достаточно.",
        "preset_group": "MCP",
    },
    {
        "id": "awesome_mcp_servers",
        "name": "awesome-mcp-servers",
        "kind": "github_atom",
        "repo": "punkpeye/awesome-mcp-servers",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 10800,
        "cadence_reason": "Каталог обновляется регулярно; три часа дают хороший баланс свежести.",
        "preset_group": "MCP",
    },
    {
        "id": "github_mcp_server",
        "name": "GitHub MCP Server",
        "kind": "github_atom",
        "repo": "github/github-mcp-server",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Активный production-репозиторий, рекомендуемый cadence — два часа.",
        "preset_group": "MCP",
    },
    {
        "id": "jailbreak_llms_research",
        "name": "JailbreakLLMs research",
        "kind": "github_atom",
        "repo": "TrustAIRLab/JailbreakLLMs",
        "branch": "main",
        "enabled": False,
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Research dataset меняется редко; шестичасовой poll безопаснее и дешевле.",
        "preset_group": "Red team",
    },
    {
        "id": "mcpbook_ru",
        "name": "MCPbook RU",
        "kind": "web_page",
        "url": "https://mcpbook.ru/",
        "enabled": False,
        "recommended_interval_seconds": 43200,
        "cadence_reason": "Каталог без RSS: компактный snapshot страницы дважды в сутки.",
        "preset_group": "RU / MCP",
    },
    {
        "id": "mcpdb_ru",
        "name": "MCP Market RU",
        "kind": "web_page",
        "url": "https://mcpdb.ru/",
        "enabled": False,
        "recommended_interval_seconds": 43200,
        "cadence_reason": "Каталог без feed: двенадцатичасовой snapshot не создаёт лишнюю нагрузку.",
        "preset_group": "RU / MCP",
    },
    {
        "id": "mcp_catalog_ru",
        "name": "MCP Catalog RU",
        "kind": "web_page",
        "url": "https://mcp-catalog.ru/",
        "enabled": False,
        "recommended_interval_seconds": 43200,
        "cadence_reason": "Структурный каталог достаточно проверять дважды в сутки.",
        "preset_group": "RU / MCP",
    },
    {
        "id": "agents_md_site",
        "name": "agents.md specification",
        "kind": "web_page",
        "url": "https://agents.md/",
        "enabled": False,
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Спецификация меняется редко; одного snapshot в сутки достаточно.",
        "preset_group": "Agent rules",
    },
    {
        "id": "promptcentral_reddit",
        "name": "PromptCentral community",
        "kind": "rss",
        "url": "https://old.reddit.com/r/PromptCentral/.rss",
        "enabled": False,
        "artifact_group": "General Prompts",
        "recommended_interval_seconds": 14400,
        "cadence_reason": "Community RSS часто ограничивает запросы; четырёхчасовой poll снижает шум и rate-limit.",
    },
    {
        "id": "promptport_library",
        "name": "PromptPort library",
        "kind": "web_page",
        "url": "https://promptport.ai/",
        "enabled": False,
        "artifact_group": "Prompt Libraries",
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Каталог меняется пакетами; одного snapshot в сутки достаточно.",
    },
    {
        "id": "promptportal_library",
        "name": "PromptPortal library",
        "kind": "web_page",
        "url": "https://promptportal.io/",
        "enabled": False,
        "artifact_group": "Prompt Libraries",
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Витрину без RSS достаточно индексировать раз в сутки.",
    },
    {
        "id": "prompta_library",
        "name": "Prompta image + video",
        "kind": "web_page",
        "url": "https://prompta.co/en/",
        "enabled": False,
        "artifact_group": "Prompt Libraries",
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Каталог image/video-промптов обновляется заметно реже новостных лент.",
    },
    {
        "id": "nanobanana_x_prompts",
        "name": "Trending image prompts from X",
        "kind": "github_atom",
        "repo": "jau123/nanobanana-trending-prompts",
        "branch": "main",
        "enabled": False,
        "artifact_group": "Image / Visual",
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Курируемая выгрузка X обновляется пакетами; шесть часов сохраняют свежесть без прямого scraping.",
    },
    {
        "id": "veo_prompts",
        "name": "Veo prompts",
        "kind": "github_atom",
        "repo": "ishandutta2007/veo_prompts",
        "branch": "main",
        "enabled": False,
        "artifact_group": "Video / Motion",
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Видео-промпты меняются пакетами; шестичасовой cadence достаточен.",
    },
    {
        "id": "veo_prompting_guide",
        "name": "Veo 3 Prompting Guide",
        "kind": "github_atom",
        "repo": "snubroot/Veo-3-Prompting-Guide",
        "branch": "main",
        "enabled": False,
        "artifact_group": "Video / Motion",
        "recommended_interval_seconds": 43200,
        "cadence_reason": "Гайд меняется редко; два обновления в сутки достаточно.",
    },
    {
        "id": "notebooklm_usage_prompts",
        "name": "NotebookLM usage + steering prompts",
        "kind": "web_page",
        "url": "https://gist.github.com/rxctionzz/8ddb59f210dbeccbdae3fbaf135f5b5b",
        "enabled": False,
        "artifact_group": "NotebookLM",
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Пользовательский guide/gist достаточно проверять раз в сутки.",
    },
    {
        "id": "notebooklm_py_skill",
        "name": "NotebookLM automation skill",
        "kind": "github_atom",
        "repo": "teng-lin/notebooklm-py",
        "branch": "main",
        "enabled": False,
        "artifact_group": "NotebookLM",
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Активный automation toolkit: шестичасовой poll ловит новые prompt workflows.",
    },
    {
        "id": "user_prompt_library",
        "name": "User-refined prompt library",
        "kind": "github_atom",
        "repo": "shawnewallace/prompt-library",
        "branch": "main",
        "enabled": False,
        "artifact_group": "System Prompts",
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Пользовательские agents/instructions обновляются нерегулярно; шесть часов безопасны.",
    },
    {
        "id": "system_prompts_models",
        "name": "System prompts + AI models",
        "kind": "github_atom",
        "repo": "x1xhlol/system-prompts-and-models-of-ai-tools",
        "branch": "main",
        "enabled": False,
        "artifact_group": "System Prompts",
        "recommended_interval_seconds": 21600,
        "cadence_reason": "Коллекция системных промптов обновляется пакетами; шесть часов достаточно.",
    },
    {
        "id": "universal_prompt_distillation",
        "name": "Universal prompt distillation",
        "kind": "web_page",
        "url": "https://gist.github.com/bhagyeshsp/b2728f41ef96d14fff76f52607aca684",
        "enabled": False,
        "artifact_group": "Distillates",
        "recommended_interval_seconds": 86400,
        "cadence_reason": "Точечный distillation gist меняется редко; суточной проверки достаточно.",
    },
    {
        "id": "x_general_prompts",
        "name": "X: system + prompt engineering",
        "kind": "x_search",
        "query": '("system prompt" OR "prompt engineering") -is:retweet lang:en',
        "enabled": False,
        "artifact_group": "General Prompts",
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Recent Search имеет квоты; двухчасовой poll и дедупликация экономят API traffic.",
    },
    {
        "id": "x_image_prompts",
        "name": "X: image prompts",
        "kind": "x_search",
        "query": '("image prompt" OR "Midjourney prompt" OR "Flux prompt") -is:retweet lang:en',
        "enabled": False,
        "artifact_group": "Image / Visual",
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Двухчасовой poll даёт живой сигнал без постоянного расхода X API quota.",
    },
    {
        "id": "x_video_prompts",
        "name": "X: video prompts",
        "kind": "x_search",
        "query": '("Veo prompt" OR "Runway prompt" OR "Sora prompt") -is:retweet lang:en',
        "enabled": False,
        "artifact_group": "Video / Motion",
        "recommended_interval_seconds": 7200,
        "cadence_reason": "Видео-поток шумный: двухчасовое окно плюс semantic dedupe удерживают расход.",
    },
    {
        "id": "x_notebooklm_prompts",
        "name": "X: NotebookLM prompts",
        "kind": "x_search",
        "query": '("NotebookLM prompt" OR "Audio Overview prompt") -is:retweet lang:en',
        "enabled": False,
        "artifact_group": "NotebookLM",
        "recommended_interval_seconds": 10800,
        "cadence_reason": "Нишевой поток достаточно проверять каждые три часа.",
    },
]

SOURCE_GROUP_ORDER = [
    "General Prompts", "System Prompts", "Image / Visual", "Video / Motion",
    "NotebookLM", "Distillates", "Agent / Rules", "MCP / Tools",
    "Prompt Libraries", "Editorial / Discovery", "Red Team", "Workspace", "Other",
]

SOURCE_GROUP_BY_ID = {
    "cursor_latest": "Editorial / Discovery",
    "cursor_announcements": "Editorial / Discovery",
    "habr_articles": "Editorial / Discovery",
    "habr_ai": "Editorial / Discovery",
    "agents_md_commits": "Agent / Rules",
    "agent_rules_books_commits": "Agent / Rules",
    "awesome_agent_conventions": "Agent / Rules",
    "agents_md_site": "Agent / Rules",
    "prompts_chat_commits": "General Prompts",
    "promptengineering_reddit": "General Prompts",
    "chatgptpromptgenius_reddit": "General Prompts",
    "awesome_prompts_ai_boost": "General Prompts",
    "awesome_claude_prompts": "General Prompts",
    "official_mcp_servers": "MCP / Tools",
    "awesome_mcp_servers": "MCP / Tools",
    "github_mcp_server": "MCP / Tools",
    "mcpbook_ru": "MCP / Tools",
    "mcpdb_ru": "MCP / Tools",
    "mcp_catalog_ru": "MCP / Tools",
    "jailbreak_llms_research": "Red Team",
}


def source_artifact_group(source: dict[str, Any]) -> str:
    explicit = str(source.get("artifact_group", "")).strip()
    if explicit:
        return explicit
    source_id = str(source.get("id", ""))
    if source_id.startswith("workspace_") or source.get("kind") == "workspace":
        return "Workspace"
    return SOURCE_GROUP_BY_ID.get(source_id, "Other")



@dataclass(frozen=True)
class Config:
    dashboard_user: str
    dashboard_pass: str
    redis_host: str
    redis_port: int
    poll_tick_seconds: int
    scan_roots: list[str]
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_api_id: int | None
    telegram_api_hash: str | None
    target_channels: list[str]
    provider_name: str
    provider_kind: str
    provider_base_url: str
    provider_api_key: str
    provider_model: str
    monthly_token_limit: int
    monthly_budget_usd: float
    input_price_per_1m: float
    output_price_per_1m: float
    qdrant_url: str
    qdrant_api_key: str
    qdrant_collection: str = QDRANT_COLLECTION

    @property
    def has_telegram(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_bot_token != "your-bot-token-here"
            and self.telegram_chat_id
            and self.telegram_chat_id != "-1001234567890"
        )

    @property
    def has_telethon(self) -> bool:
        if not self.telegram_api_id or not self.telegram_api_hash:
            return False
        if self.telegram_api_id == 123456:
            return False
        if self.telegram_api_hash == "your_telethon_api_hash":
            return False
        return True

    @property
    def has_ai_provider(self) -> bool:
        return bool(self.provider_base_url and self.provider_api_key)


def load_config() -> Config:
    perplexity_api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if perplexity_api_key in {"pplx-your-key-here", "your-key-here"}:
        perplexity_api_key = ""
    ai_base_url = os.getenv("AI_PROVIDER_BASE_URL", "").strip()
    ai_api_key = os.getenv("AI_PROVIDER_API_KEY", "").strip()
    ai_name = os.getenv("AI_PROVIDER_NAME", "").strip()
    ai_kind = os.getenv("AI_PROVIDER_KIND", "openai_compatible").strip()
    ai_model = os.getenv("AI_PROVIDER_MODEL", "").strip()

    if not ai_base_url and perplexity_api_key:
        ai_base_url = "https://api.perplexity.ai"
        ai_api_key = perplexity_api_key
        ai_name = ai_name or "Perplexity"
        ai_kind = "openai_compatible"
        ai_model = ai_model or "xai/grok-4.5"

    scan_roots = [root.strip() for root in os.getenv("PROMPT_OPS_SCAN_ROOTS", "/app,/app/.codex,/app/.agents").split(",") if root.strip()]
    target_channels = [c.strip() for c in os.getenv("TARGET_TELEGRAM_CHANNELS", "").split(",") if c.strip()]
    return Config(
        dashboard_user=os.getenv("DASHBOARD_USER", "admin"),
        dashboard_pass=os.getenv("DASHBOARD_PASS", "admin"),
        redis_host=os.getenv("REDIS_HOST", "redis"),
        redis_port=int(os.getenv("REDIS_PORT", 6379)),
        poll_tick_seconds=max(15, int(os.getenv("POLL_TICK_SECONDS", 45))),
        scan_roots=scan_roots,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_api_id=int(os.getenv("TELEGRAM_API_ID", "0") or 0) or None,
        telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip() or None,
        target_channels=target_channels,
        provider_name=ai_name or "OpenAI-compatible",
        provider_kind=ai_kind,
        provider_base_url=ai_base_url,
        provider_api_key=ai_api_key,
        provider_model=ai_model or "gpt-4o-mini",
        monthly_token_limit=int(os.getenv("AI_MONTHLY_TOKEN_LIMIT", 500000)),
        monthly_budget_usd=float(os.getenv("AI_MONTHLY_BUDGET_USD", 25.0)),
        input_price_per_1m=float(os.getenv("AI_INPUT_PRICE_PER_1M", 2.0)),
        output_price_per_1m=float(os.getenv("AI_OUTPUT_PRICE_PER_1M", 8.0)),
        qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY", "").strip(),
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().strftime(DATE_FMT)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+0000"
        if len(cleaned) > 5 and cleaned[-3] == ":":
            cleaned = cleaned[:-3] + cleaned[-2:]
        return datetime.strptime(cleaned, DATE_FMT)
    except Exception:
        return None


def parse_iso_ts(value: str | None) -> float:
    parsed = parse_dt(value)
    return parsed.timestamp() if parsed else now_utc().timestamp()


def format_relative(dt_value: str | None) -> str:
    parsed = parse_dt(dt_value)
    if not parsed:
        return "-"
    delta = parsed - now_utc()
    minutes = int(delta.total_seconds() / 60)
    if abs(minutes) < 1:
        return "now"
    if minutes > 0:
        if minutes < 60:
            return f"in {minutes}m"
        return f"in {minutes // 60}h"
    minutes = abs(minutes)
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago"


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-zА-Яа-я0-9_@#./:-]+", text.lower())


def estimate_tokens(text: str) -> int:
    clean = normalize_ws(text)
    if not clean:
        return 0
    return max(1, math.ceil(len(clean) / 4))


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug or secrets.token_hex(6)


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()
    return digest[:24]


def clean_json_response(raw_content: str) -> str:
    content = raw_content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
    if content.endswith("```"):
        content = content.rsplit("\n", 1)[0]
    return content.strip()


def safe_json_loads(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(clean_json_response(raw))
    except Exception:
        return fallback


def dedupe_text(*parts: str) -> str:
    return hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()


def classify_artifact(path: str, text: str) -> tuple[str, int]:
    lowered_path = path.lower()
    lowered = text.lower()
    if "notebooklm" in lowered_path or "notebooklm" in lowered:
        return "NotebookLM", 87
    if any(token in lowered for token in ["video prompt", "veo prompt", "sora prompt", "runway prompt", "text-to-video", "image-to-video"]):
        return "Video Prompt", 86
    if any(token in lowered for token in ["image prompt", "midjourney prompt", "flux prompt", "text-to-image", "inpainting", "outpainting", "negative prompt"]):
        return "Image Prompt", 86
    if any(token in lowered for token in ["prompt distillation", "context distillation", "distill this prompt", "compress the prompt"]):
        return "Distillate", 89
    if any(token in lowered for token in ["system prompt", "system_prompt", "developer prompt", "custom instructions"]):
        return "System Prompt", 88
    if "skill" in lowered_path or "skill.md" in lowered_path or "skill" in lowered:
        return "Skill", 88
    if any(token in lowered for token in ["response_format", "messages", "temperature"]):
        return "Prompt", 84
    if any(token in lowered_path for token in ["dockerfile", "compose", "workflow", ".github/"]) or any(
        token in lowered for token in ["build", "deploy", "pipeline", "asyncio.create_task", "uvicorn"]
    ):
        return "Pipeline", 81
    if any(token in lowered for token in ["instruction", "must", "should", "usage", "example", "env"]):
        return "Instruction", 74
    if any(token in lowered for token in ["agent", "worker", "daemon", "telethon", "bot"]):
        return "Agent", 76
    if any(token in lowered for token in ["rule", "guardrail", "validate", "auth", "check", "sanitize"]):
        return "Rule", 79
    return "Noise", 10


def derive_tags(path: str, text: str, category: str) -> list[str]:
    lowered = f"{path}\n{text}".lower()
    tags: list[str] = [category.lower()]
    keyword_map = [
        ("prompt", "prompt"),
        ("system prompt", "system"),
        ("notebooklm", "notebooklm"),
        ("midjourney", "image"),
        ("text-to-image", "image"),
        ("inpainting", "image-edit"),
        ("veo", "video"),
        ("sora", "video"),
        ("text-to-video", "video"),
        ("distillation", "distillate"),
        ("template", "template"),
        ("workflow", "workflow"),
        ("pipeline", "pipeline"),
        ("instruction", "instruction"),
        ("skill", "skill"),
        ("agent", "agent"),
        ("rule", "rule"),
        ("guardrail", "guardrail"),
        ("check", "check"),
        ("validate", "validate"),
        ("docker", "docker"),
        ("telethon", "telethon"),
        ("fastapi", "fastapi"),
        ("qdrant", "vector"),
    ]
    for needle, tag in keyword_map:
        if needle in lowered:
            tags.append(tag)
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix:
        tags.append(suffix)
    if Path(path).name.lower() == "dockerfile":
        tags.append("dockerfile")
    if Path(path).name.lower() == "docker-compose.yml":
        tags.append("compose")
    return list(dict.fromkeys(tags))[:10]


def normalize_prompt_tag(value: Any) -> str:
    aliases = {
        "system": "system-prompt",
        "image": "image-generation",
        "video": "video-generation",
        "json": "structured-output",
        "analysis": "analytical-reasoning",
        "research": "research-workflow",
        "translation": "translation-workflow",
    }
    raw = str(value or "").strip().lower().replace("_", "-")
    tag = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:40]
    if not tag or len(tag) < 2:
        return ""
    return aliases.get(tag, tag)


def prompt_tags(title: str, body: str, category: str, source_tags: list[Any] | None = None) -> tuple[list[str], str]:
    """Return 3-5 English tags, prioritising reusable and narrow task labels."""
    text = f"{title}\n{body}".lower()
    raw_source_tags = source_tags if isinstance(source_tags, list) else []
    source = list(dict.fromkeys(filter(None, (normalize_prompt_tag(tag) for tag in raw_source_tags))))
    specific_rules = [
        (("faq", "frequently asked"), ["faq-generation", "question-coverage"]),
        (("code review", "review code", "ревью кода"), ["code-review", "defect-detection"]),
        (("compare", "comparison", "сравни"), ["comparative-analysis", "decision-support"]),
        (("summar", "сводк", "резюме"), ["content-summarization", "key-point-extraction"]),
        (("translat", "перевод"), ["translation-workflow", "meaning-preservation"]),
        (("audit", "аудит"), ["structured-audit", "risk-identification"]),
        (("research", "исслед"), ["research-synthesis", "evidence-analysis"]),
        (("image", "midjourney", "flux", "dall-e"), ["image-generation", "visual-direction"]),
        (("video", "veo", "sora", "shot list"), ["video-generation", "shot-planning"]),
        (("notebooklm", "audio overview"), ["notebooklm-workflow", "source-grounded-learning"]),
        (("distill", "compress the prompt", "сожми промпт"), ["prompt-distillation", "context-compression"]),
        (("tiktok", "reels", "short-form video"), ["short-form-video-script", "audience-engagement"]),
        (("script generator", "write a script", "screenplay", "сценарий"), ["script-generation", "narrative-structure"]),
        (("write code", "generate code", "implement", "напиши код", "реализуй"), ["code-generation", "implementation-planning"]),
    ]
    specific: list[str] = []
    for needles, candidates in specific_rules:
        if any(needle in text for needle in needles):
            specific.extend(candidates)
            break

    if not specific:
        stop_words = {
            "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
            "act", "assistant", "create", "creating", "generate", "generator", "prompt", "tool", "using",
        }
        title_words = [word for word in re.findall(r"[a-z0-9]+", title.lower()) if word not in stop_words and len(word) > 1]
        title_tag = normalize_prompt_tag("-".join(title_words[:3]))
        if title_tag:
            specific.append(title_tag)

    category_tags = {
        "System Prompt": ["system-prompt", "behavior-control"],
        "Image Prompt": ["image-generation", "visual-prompting"],
        "Video Prompt": ["video-generation", "motion-prompting"],
        "NotebookLM": ["notebooklm-workflow", "source-grounding"],
        "Distillate": ["prompt-distillation", "context-compression"],
        "Prompt": ["prompt-engineering", "task-instruction"],
    }.get(category, ["prompt-engineering", "task-instruction"])
    structural = []
    if re.search(r"\{\{?[_A-Za-z][^}\n]*\}\}?|\$\{[^}\n]+\}|<[_A-Za-z][^>\n]*>", body):
        structural.append("reusable-template")
    if any(token in text for token in ["json", "yaml", "xml", "markdown", "output format"]):
        structural.append("structured-output")
    if any(token in text for token in ["must", "never", "only", "constraint", "обязательно", "запрещено"]):
        structural.append("constraint-driven")
    if any(token in text for token in ["you are", "act as", "role:", "ты —", "роль:"]):
        structural.append("role-prompting")
    if any(token in text for token in ["step by step", "workflow", "pipeline", "по шагам"]):
        structural.append("multi-step-workflow")

    generated = list(dict.fromkeys(specific[:2] + category_tags + structural))
    tags = list(dict.fromkeys(specific[:2] + source + generated))
    fallbacks = ["prompt-engineering", "task-instruction", "response-design", "llm-workflow"]
    for tag in fallbacks:
        if len(tags) >= 3:
            break
        if tag not in tags:
            tags.append(tag)
    tags = tags[:5]
    used_generated = any(tag not in source for tag in tags)
    origin = "mixed" if source and used_generated else "source" if source else "generated"
    return tags, origin


def nonnegative_int(value: Any, fallback: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def prompt_text_list(value: Any, fallback: list[Any], limit: int = 8) -> list[str]:
    values = value if isinstance(value, list) else fallback
    return [normalize_ws(str(item))[:160] for item in values if normalize_ws(str(item))][:limit]


def prompt_learning_complexity(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else fallback
    level = str(raw.get("level", fallback.get("level", "средняя"))).lower()
    if level not in {"низкая", "средняя", "высокая"}:
        level = str(fallback.get("level", "средняя"))
    try:
        score = int(clamp(int(raw.get("score", fallback.get("score", 50))), 1, 100))
    except (TypeError, ValueError):
        score = int(fallback.get("score", 50))
    reason = normalize_ws(str(raw.get("reason", fallback.get("reason", ""))))[:300]
    return {"level": level, "score": score, "reason": reason}


def prompt_token_estimate(body: str, expected_output: str = "") -> dict[str, Any]:
    base = estimate_tokens(body)
    placeholders = len(re.findall(r"\{\{?[^}\n]+\}\}?|\$\{[^}\n]+\}|<[_A-Za-z][^>\n]*>", body))
    input_min = max(1, base + placeholders * 6)
    input_max = max(input_min, base + placeholders * 60 + 24)
    lowered = f"{body}\n{expected_output}".lower()
    if any(token in lowered for token in ["language detection", "название языка", "языковой код"]):
        output_min, output_max = 5, 30
    elif any(token in lowered for token in ["faq", "frequently asked"]):
        output_min, output_max = 800, 2200
    elif any(token in lowered for token in ["write code", "generate code", "implement", "код", "реализ"]):
        output_min, output_max = 600, 2600
    elif any(token in lowered for token in ["image", "midjourney", "flux", "dall-e"]):
        output_min, output_max = 100, 450
    elif any(token in lowered for token in ["video", "veo", "sora", "shot list", "tiktok", "reels", "script generator", "screenplay"]):
        output_min, output_max = 350, 1400
    elif any(token in lowered for token in ["summar", "summary", "сводк", "резюме"]):
        output_min, output_max = 250, 900
    elif any(token in lowered for token in ["report", "research", "audit", "отчёт", "исслед", "аудит"]):
        output_min, output_max = 800, 2800
    elif "json" in lowered:
        output_min, output_max = 350, 1400
    else:
        output_min, output_max = 300, 1200
    return {
        "input": {"min": input_min, "max": input_max},
        "output": {"min": output_min, "max": output_max},
        "total": {"min": input_min + output_min, "max": input_max + output_max},
        "method": "heuristic-v1",
    }


def derive_complexity(category: str, text: str) -> int:
    base = {"Prompt": 65, "System Prompt": 68, "Image Prompt": 62, "Video Prompt": 68, "NotebookLM": 66, "Distillate": 72, "Pipeline": 70, "Instruction": 52, "Skill": 76, "Agent": 68, "Rule": 58}.get(category, 18)
    base += min(25, len(text) // 120)
    return int(clamp(base, 1, 100))


def extract_entities(path: str, text: str) -> list[str]:
    entities = [path]
    for token in tokenize(text):
        clean = token.strip(",.;:()[]{}<>\"'`")
        if clean.startswith(("http://", "https://", "./", "/")):
            entities.append(clean)
        elif clean.isupper() and 2 <= len(clean) <= 24:
            entities.append(clean)
        elif clean.endswith((".py", ".yml", ".yaml", ".md", ".json", ".toml", ".xml", ".rss")):
            entities.append(clean)
    return list(dict.fromkeys(entities))[:12]


def make_title(path: str, text: str, category: str) -> str:
    for line in text.splitlines():
        clean = line.strip().lstrip("#-*> ")
        if clean:
            return clean[:96]
    name = Path(path).name if path else "workspace"
    return f"{category}: {name}"


def make_summary_from_text(text: str) -> str:
    clean = normalize_ws(text)
    return clean[:220] + ("..." if len(clean) > 220 else "")


def extract_prompt_body(raw_text: str) -> str:
    text = html.unescape(str(raw_text or "")).replace("\r\n", "\n").strip()
    fenced = re.findall(r"```(?:[\w.+-]+)?[ \t]*\n(.*?)```", text, flags=re.DOTALL)
    if fenced:
        candidate = max(fenced, key=len).strip()
        if candidate:
            return candidate[:MAX_PROMPT_BODY_CHARS]
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*(Title|Link|Repo|Path|Category|Source):\s*", line, flags=re.IGNORECASE):
            continue
        line = re.sub(r"<[^>]+>", " ", line)
        lines.append(line)
    candidate = "\n".join(lines).strip()
    return candidate[:MAX_PROMPT_BODY_CHARS]


def prompt_mechanics(title: str, body: str, complexity: int | None = None) -> dict[str, Any]:
    lowered = f"{title}\n{body}".lower()
    methods = []
    reasons = []

    if any(token in lowered for token in ["faq", "frequently asked"]):
        operation = "Генерирует FAQ для указанного продукта, сервиса, объекта, компании или мероприятия и распределяет вопросы по смысловым разделам"
        coverage = ["основные возможности", "использование", "ограничения", "типичные вопросы"]
    elif any(token in lowered for token in ["language detection", "detect language", "определи язык"]):
        operation = "Определяет язык переданного текста и возвращает название языка либо стандартизированный языковой код"
        coverage = ["основной язык", "языковой код", "смешанный текст", "уверенность определения"]
    elif any(token in lowered for token in ["storyboard", "storyboarding", "shot grid"]):
        operation = "Преобразует исходную идею или изображение в последовательность связанных кадров для раскадровки"
        coverage = ["последовательность кадров", "композиция", "действие", "визуальная связность"]
    elif any(token in lowered for token in ["tiktok", "reels", "short-form video", "script generator", "write a script", "screenplay", "сценарий"]):
        operation = "Генерирует сценарий короткого видео: выстраивает хук, последовательность сцен, реплики, визуальные подсказки и финальный призыв к действию"
        coverage = ["хук", "сцены и реплики", "визуальная подача", "призыв к действию"]
    elif any(token in lowered for token in ["style guide", "writing style", "tone of voice"]):
        operation = "Формализует правила стиля, тона и подачи, чтобы последующие материалы сохраняли единый голос"
        coverage = ["тон", "лексика", "структура", "разрешённые и запрещённые приёмы"]
    elif any(token in lowered for token in ["compare", "comparison", "сравни"]):
        operation = "Сопоставляет указанные сущности по заданным критериям, выявляет различия и помогает выбрать подходящий вариант"
        coverage = ["критерии сравнения", "сильные стороны", "ограничения", "итоговый выбор"]
    elif any(token in lowered for token in ["summar", "summary", "сводк", "резюме"]):
        operation = "Сжимает исходный материал, выделяет главные тезисы и отбрасывает второстепенные повторы"
        coverage = ["ключевые тезисы", "аргументы", "выводы", "пробелы контекста"]
    elif any(token in lowered for token in ["translat", "перевод"]):
        operation = "Переводит исходный материал, сохраняя смысл, терминологию, структуру и требуемый тон"
        coverage = ["смысл", "терминология", "тон", "форматирование"]
    elif any(token in lowered for token in ["audit", "analy", "аудит", "анализ", "проанализ"]):
        operation = "Разбирает входные данные по критериям, находит закономерности, риски и формирует проверяемые выводы"
        coverage = ["факты", "риски", "аномалии", "рекомендации"]
    elif any(token in lowered for token in ["write code", "generate code", "implement", "напиши код", "реализуй"]):
        operation = "Преобразует требования в план реализации и генерирует код с учётом ограничений и ожидаемого интерфейса"
        coverage = ["требования", "архитектура", "реализация", "проверка результата"]
    elif any(token in lowered for token in ["image", "midjourney", "flux", "dall-e"]):
        operation = "Собирает визуальную спецификацию: объект, композицию, стиль, свет, ракурс и ограничения изображения"
        coverage = ["сюжет", "композиция", "стиль", "свет и камера"]
    elif any(token in lowered for token in ["video", "veo", "sora", "shot list"]):
        operation = "Собирает видеоспецификацию: сцену, последовательность действий, движение камеры, ритм и визуальный стиль"
        coverage = ["сцена", "движение", "камера", "монтажный ритм"]
    elif "notebooklm" in lowered:
        operation = "Управляет обработкой загруженных источников в NotebookLM и задаёт форму учебного или аналитического результата"
        coverage = ["источники", "основные темы", "связи между материалами", "учебный результат"]
    elif any(token in lowered for token in ["distill", "compress the prompt", "сожми промпт"]):
        operation = "Сокращает исходный промпт, сохраняя его обязательные правила, рабочую логику и формат результата"
        coverage = ["цель", "ограничения", "ключевые инструкции", "формат выхода"]
    else:
        operation = f"Выполняет операцию «{title}»: принимает пользовательский контекст, последовательно обрабатывает его по инструкции и формирует требуемый результат"
        coverage = ["рабочий контекст", "основная задача", "ограничения", "формат результата"]

    if any(token in lowered for token in ["you are", "act as", "ты —", "роль:", "role:"]):
        methods.append("назначает модели специализированную роль")
        reasons.append("роль сужает допустимый тон, знания и способ рассуждения")
    else:
        methods.append("формулирует прямую задачу и рабочий контекст")
        reasons.append("явная цель уменьшает неоднозначность запроса")

    if re.search(r"\{\{?[_A-Za-z][^}\n]*\}\}?|\$\{[^}\n]+\}|<[_A-Za-z][^>\n]*>", body):
        methods.append("подставляет пользовательский контекст через переменные")
        reasons.append("шаблон можно переиспользовать без изменения основной логики")
    if any(token in lowered for token in ["must", "never", "only", "constraint", "обязательно", "только", "запрещено"]):
        methods.append("проверяет результат по явно заданным ограничениям")
        reasons.append("ограничения отсекают нежелательные варианты ответа")
    if any(token in lowered for token in ["example", "for example", "например", "input:", "output:"]):
        methods.append("сверяет ответ с примером")
        reasons.append("пример закрепляет ожидаемый паттерн продолжения")

    if any(token in lowered for token in ["faq", "frequently asked"]):
        output = "готовый FAQ, сгруппированный по темам и покрывающий основные вопросы выбранной сущности"
    elif any(token in lowered for token in ["language detection", "detect language", "определи язык"]):
        output = "название обнаруженного языка или его стандартизированный языковой код"
    elif any(token in lowered for token in ["storyboard", "storyboarding", "shot grid"]):
        output = "последовательная раскадровка с описанием композиции и действия в каждом кадре"
    elif any(token in lowered for token in ["tiktok", "reels", "short-form video", "script generator", "write a script", "screenplay", "сценарий"]):
        output = "готовый сценарий короткого видео с хуком, сценами, репликами, визуальными подсказками и CTA"
    elif "json" in lowered:
        output = "структурированный JSON, пригодный для дальнейшей машинной обработки"
    elif any(token in lowered for token in ["yaml", "xml"]):
        output = "структурированные данные в явно заданном формате"
    elif any(token in lowered for token in ["translate", "translation", "translator", "переведи", "перевод"]):
        output = "переведённый и адаптированный текст с сохранением исходного смысла"
    elif any(token in lowered for token in ["summarize", "summary", "synopsis", "резюме", "сводк", "краткое содержание"]):
        output = "сжатая сводка с основными тезисами исходного материала"
    elif any(token in lowered for token in ["analyze", "analysis", "audit", "проанализ", "анализ", "аудит"]):
        output = "структурированный аналитический разбор с выводами"
    elif "markdown" in lowered:
        output = "оформленный Markdown-документ"
    elif any(token in lowered for token in ["image", "midjourney", "flux", "dall-e", "stable diffusion"]):
        output = "описание или спецификация изображения в заданной стилистике"
    elif any(token in lowered for token in ["video", "veo", "sora", "camera movement", "shot list"]):
        output = "сценарий, shot list или спецификация видеогенерации"
    elif any(token in lowered for token in ["write code", "generate code", "implement code", "create a function", "write a script", "напиши код", "реализуй функцию", "создай скрипт"]):
        output = "код или техническая реализация с заданными требованиями"
    elif any(token in lowered for token in ["table", "таблиц"]):
        output = "сравнительная или аналитическая таблица"
    else:
        output = f"контекстный ответ в логике задачи «{title}»"

    structure = ["контекст и роль", "основная операция"]
    if re.search(r"\{\{?[_A-Za-z][^}\n]*\}\}?|\$\{[^}\n]+\}|<[_A-Za-z][^>\n]*>", body):
        structure.append("переменные пользователя")
    if any(token in lowered for token in ["must", "never", "only", "constraint", "обязательно", "только", "запрещено"]):
        structure.append("ограничения и критерии")
    if any(token in lowered for token in ["return", "output", "format", "верни", "формат"]):
        structure.append("формат результата")
    if any(token in lowered for token in ["example", "for example", "например", "input:", "output:"]):
        structure.append("пример")

    learning_score = int(complexity if complexity is not None else derive_complexity("Prompt", body))
    level = "низкая" if learning_score < 45 else "средняя" if learning_score < 75 else "высокая"
    learning_reason = (
        "можно применять почти без настройки" if level == "низкая" else
        "нужно заполнить контекст и проверить ограничения" if level == "средняя" else
        "требует понимания многоэтапной структуры, переменных и формата выхода"
    )
    return {
        "how_it_works": f"{operation}. Логика: {'; '.join(methods[:3])}.",
        "why_it_works": "; ".join(reasons[:3]) + ".",
        "structure": structure[:6],
        "coverage": coverage[:6],
        "expected_output": output,
        "learning_complexity": {"level": level, "score": learning_score, "reason": learning_reason},
    }


def prompt_mechanics_description(title: str, body: str) -> str:
    mechanics = prompt_mechanics(title, body)
    return (
        f"Как работает: {mechanics['how_it_works']} "
        f"Почему работает: {mechanics['why_it_works']} "
        f"На выходе: {mechanics['expected_output']}."
    )[:1000]

def prompt_literacy_score(body: str) -> int:
    lowered = body.lower()
    score = 42
    if any(token in lowered for token in ["you are", "act as", "ты —", "вы —", "роль:", "role:"]):
        score += 12
    if any(token in lowered for token in ["return", "output", "format", "верни", "формат", "структур"]):
        score += 12
    if any(token in lowered for token in ["must", "never", "only", "constraint", "обязательно", "только", "не добавляй"]):
        score += 10
    if any(token in body for token in ["{", "}", "<", ">", "${"]):
        score += 8
    if any(token in lowered for token in ["example", "например", "input:", "output:"]):
        score += 8
    if len(body) >= 500:
        score += 5
    if len(body) < 120:
        score -= 18
    return int(clamp(score, 1, 100))


def prompt_special_marks(body: str, category: str) -> list[str]:
    lowered = body.lower()
    marks = []
    if category == "System Prompt" or "system prompt" in lowered:
        marks.append("SYSTEM_LEVEL")
    if any(token in lowered for token in ["json", "yaml", "xml", "markdown", "code block"]):
        marks.append("STRUCTURED_OUTPUT")
    if re.search(r"\{\{?[_A-Za-z][^}\n]*\}\}?|\$\{[^}\n]+\}|<[_A-Za-z][^>\n]*>", body):
        marks.append("HAS_VARIABLES")
    if any(token in lowered for token in ["image", "midjourney", "flux", "video", "veo", "sora"]):
        marks.append("MULTIMODAL")
    if any(token in lowered for token in ["never", "must", "only", "обязательно", "запрещено", "только"]):
        marks.append("STRICT_CONSTRAINTS")
    return marks[:6]


def prompt_remarks(body: str) -> list[str]:
    lowered = body.lower()
    remarks = []
    if len(body) < 160:
        remarks.append("Короткий контекст: результат может сильно зависеть от модели.")
    if not any(token in lowered for token in ["return", "output", "format", "верни", "формат", "ответ"]):
        remarks.append("Не задан явный формат результата.")
    if not any(token in lowered for token in ["you are", "act as", "ты —", "роль:", "role:"]):
        remarks.append("Роль модели не зафиксирована явно.")
    if not any(token in lowered for token in ["must", "never", "only", "constraint", "обязательно", "только"]):
        remarks.append("Ограничения сформулированы слабо.")
    return remarks[:4]


def looks_like_prompt(category: str, body: str, explicit: Any = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    if category in PROMPT_CATEGORIES:
        return len(body.strip()) >= 40
    lowered = body.lower()
    signals = ["you are", "act as", "your task", "return only", "i want you to", "ты —", "твоя задача", "верни только", "создай", "сгенерируй"]
    return len(body.strip()) >= 80 and any(signal in lowered for signal in signals)


def build_prompt_projection(record: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any] | None:
    analysis = analysis or {}
    if record.get("source_kind") not in PUBLIC_PROMPT_SOURCE_KINDS:
        return None
    body = str(analysis.get("prompt_body") or record.get("prompt_body") or extract_prompt_body(record.get("raw", ""))).strip()
    category = str(record.get("type") or analysis.get("category") or "Prompt")
    if not looks_like_prompt(category, body, analysis.get("is_prompt")):
        return None
    complexity = int(clamp(int(analysis.get("complexity", record.get("complexity", derive_complexity(category, body)))), 1, 100))
    literacy = int(clamp(int(analysis.get("literacy_score", record.get("literacy_score", prompt_literacy_score(body)))), 1, 100))
    marks = list(dict.fromkeys(analysis.get("special_marks") or record.get("special_marks") or prompt_special_marks(body, category)))[:8]
    remarks = list(dict.fromkeys(analysis.get("remarks") or record.get("remarks") or prompt_remarks(body)))[:6]
    title = str(analysis.get("prompt_title") or record.get("title") or "Untitled prompt")[:160]
    tags, tag_origin = prompt_tags(title, body, category, analysis.get("tags") or record.get("tags", []))
    generated = prompt_mechanics(title, body, complexity)
    mechanics = {
        "how_it_works": str(analysis.get("how_it_works") or generated["how_it_works"])[:1000],
        "why_it_works": str(analysis.get("why_it_works") or generated["why_it_works"])[:800],
        "structure": prompt_text_list(analysis.get("structure"), generated["structure"]),
        "coverage": prompt_text_list(analysis.get("coverage"), generated["coverage"]),
        "expected_output": str(analysis.get("expected_output") or generated["expected_output"])[:500],
        "learning_complexity": prompt_learning_complexity(analysis.get("learning_complexity"), generated["learning_complexity"]),
    }
    description = (
        f"Как работает: {mechanics['how_it_works']} "
        f"Почему работает: {mechanics['why_it_works']} "
        f"На выходе: {mechanics['expected_output']}."
    )[:1500]
    description_origin = "source" if record.get("source_description") else "ai_enriched" if any(
        analysis.get(key) for key in ["description", "how_it_works", "expected_output"]
    ) else "reconstructed"
    return {
        "id": record.get("id"),
        "title": title,
        "prompt_body": body[:MAX_PROMPT_BODY_CHARS],
        "description": description,
        **mechanics,
        "tags": tags,
        "tag_origin": tag_origin,
        "description_origin": description_origin,
        "token_estimate": prompt_token_estimate(body, mechanics["expected_output"]),
        "complexity": complexity,
        "literacy_score": literacy,
        "special_marks": marks,
        "remarks": remarks,
        "prompt_type": category if category in PROMPT_CATEGORIES else "Prompt",
        "source_kind": record.get("source_kind", ""),
        "published_at": record.get("published_at") or iso_now(),
        "published_ts": float(record.get("published_ts") or now_utc().timestamp()),
        "updated_at": iso_now(),
        "schema_version": 2,
    }

def heuristic_analysis(raw_text: str, path: str = "") -> dict[str, Any]:
    category, score = classify_artifact(path, raw_text)
    if category == "Noise":
        lowered = raw_text.lower()
        if any(token in lowered for token in ["prompt", "instruction", "skill", "agent", "pipeline", "rule"]):
            score = 35
            category = "Instruction"
    tags = derive_tags(path or "workspace", raw_text, category)
    prompt_body = extract_prompt_body(raw_text)
    return {
        "anomaly_score": score,
        "category": category,
        "summary": "Автоматическая эвристическая оценка без внешней модели.",
        "entities": extract_entities(path or "workspace", raw_text),
        "tags": tags,
        "complexity": derive_complexity(category, raw_text),
        "killer_feature": "Локальная обработка без расходов на модель.",
        "should_disappear": "Дубли, лишний шум и boilerplate.",
        "is_prompt": looks_like_prompt(category, prompt_body),
        "prompt_body": prompt_body,
        "literacy_score": prompt_literacy_score(prompt_body),
        "special_marks": prompt_special_marks(prompt_body, category),
        "remarks": prompt_remarks(prompt_body),
    }


def render_prompt_merger(records: list[dict[str, Any]]) -> str:
    parts = []
    for idx, record in enumerate(records, start=1):
        parts.append(
            f"[{idx}] {record.get('title', 'Untitled')}\n"
            f"Type: {record.get('type', 'Unknown')}\n"
            f"Source: {record.get('source_name', '')}\n"
            f"Summary: {record.get('summary', '')}\n"
            f"Tags: {', '.join(record.get('tags', []))}\n"
            f"Content:\n{record.get('raw', record.get('snippet', ''))}"
        )
    return "\n\n---\n\n".join(parts)


def embed_text(text: str) -> list[float]:
    vector = [0.0] * VECTOR_DIM
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "big") % VECTOR_DIM
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        weight = 1.0 + min(len(token), 12) / 12.0
        vector[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return vector
    return [v / norm for v in vector]


def make_artifact_id(source_id: str, path: str, text: str) -> str:
    return stable_id(source_id, path, text[:2000])


def vector_point_id(artifact_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"promptops:{artifact_id}"))


def qdrant_record_payload(record: Any) -> dict[str, Any]:
    payload = dict(record.payload or {})
    payload.setdefault("id", str(record.id))
    payload["vector_id"] = str(record.id)
    return payload


def default_source_catalog(cfg: Config) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for blueprint in DEFAULT_SOURCE_BLUEPRINTS:
        item = dict(blueprint)
        item.setdefault("artifact_group", source_artifact_group(item))
        item.setdefault("manual_interval_seconds", None)
        item.setdefault("empty_streak", 0)
        item.setdefault("error_streak", 0)
        item.setdefault("paused", False)
        item.setdefault("last_attempt_at", None)
        item.setdefault("last_success_at", None)
        item.setdefault("next_refresh_at", None)
        item.setdefault("state", "idle")
        item.setdefault("detail", "waiting")
        item.setdefault("updated_at", iso_now())
        catalog.append(item)
    for root in cfg.scan_roots:
        catalog.append(
            {
                "id": f"workspace_{slugify(root)}",
                "name": f"Workspace: {root}",
                "kind": "workspace",
                "root": root,
                "artifact_group": "Workspace",
                "enabled": True,
                "recommended_interval_seconds": 900,
                "cadence_reason": "Локальный workspace меняется быстро и требует частого поллинга.",
                "manual_interval_seconds": None,
                "empty_streak": 0,
                "error_streak": 0,
                "paused": False,
                "last_attempt_at": None,
                "last_success_at": None,
                "next_refresh_at": None,
                "state": "idle",
                "detail": "waiting",
                "updated_at": iso_now(),
            }
        )
    return catalog


def merge_catalog(defaults: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in current}
    merged = []
    for item in defaults:
        merged.append({**item, **by_id.get(item["id"], {})})
    known = {item["id"] for item in defaults}
    for item in current:
        if item["id"] not in known:
            merged.append(item)
    return merged


def default_provider_state(cfg: Config) -> dict[str, Any]:
    return {
        "name": cfg.provider_name,
        "kind": cfg.provider_kind,
        "base_url": cfg.provider_base_url,
        "api_key": cfg.provider_api_key,
        "model": cfg.provider_model,
        "monthly_token_limit": cfg.monthly_token_limit,
        "monthly_budget_usd": cfg.monthly_budget_usd,
        "input_price_per_1m": cfg.input_price_per_1m,
        "output_price_per_1m": cfg.output_price_per_1m,
        "updated_at": iso_now(),
        "loaded_models": [],
    }


def api_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    if "api.perplexity.ai" in base:
        return base.removesuffix("/v1")
    return base if base.endswith("/v1") else f"{base}/v1"


def provider_is_configured() -> bool:
    provider = getattr(app.state, "provider_state", {})
    return bool(str(provider.get("base_url", "")).strip() and str(provider.get("api_key", "")).strip())


async def redis_get_json(redis_client: aioredis.Redis, key: str, default: Any) -> Any:
    raw = await redis_client.get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def redis_set_json(redis_client: aioredis.Redis, key: str, value: Any) -> None:
    await redis_client.set(key, json.dumps(value, ensure_ascii=False))


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    cfg: Config = app.state.config
    ok_user = secrets.compare_digest(credentials.username, cfg.dashboard_user)
    ok_pass = secrets.compare_digest(credentials.password, cfg.dashboard_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def ensure_qdrant_collection() -> None:
    cfg: Config = app.state.config
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if not client:
        app.state.vector_ready = False
        app.state.vector_status = "disabled"
        return

    def _prepare() -> None:
        exists = client.collection_exists(cfg.qdrant_collection)
        if not exists:
            client.create_collection(
                collection_name=cfg.qdrant_collection,
                vectors_config=qmodels.VectorParams(size=VECTOR_DIM, distance=qmodels.Distance.COSINE),
            )

    await asyncio.to_thread(_prepare)
    app.state.vector_ready = True
    app.state.vector_status = "ready"


async def qdrant_upsert(record: dict[str, Any]) -> None:
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if not client or not getattr(app.state, "vector_ready", False):
        return
    cfg: Config = app.state.config
    payload = dict(record)
    vector = embed_text(
        " ".join(
            [
                record.get("title", ""),
                record.get("summary", ""),
                record.get("raw", ""),
                " ".join(record.get("tags", [])),
                record.get("source_name", ""),
                record.get("type", ""),
            ]
        )
    )

    def _upsert() -> None:
        client.upsert(
            collection_name=cfg.qdrant_collection,
            points=[qmodels.PointStruct(id=vector_point_id(record["id"]), vector=vector, payload=payload)],
        )

    await asyncio.to_thread(_upsert)


async def qdrant_retrieve(ids: list[str]) -> list[dict[str, Any]]:
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if not client or not getattr(app.state, "vector_ready", False) or not ids:
        return []
    cfg: Config = app.state.config

    def _retrieve() -> list[dict[str, Any]]:
        records = client.retrieve(
            collection_name=cfg.qdrant_collection,
            ids=[vector_point_id(item_id) for item_id in ids],
            with_payload=True,
            with_vectors=False,
        )
        result: list[dict[str, Any]] = []
        for record in records:
            payload = qdrant_record_payload(record)
            result.append(payload)
        return result

    return await asyncio.to_thread(_retrieve)


def build_filter_conditions(params: dict[str, Any]) -> qmodels.Filter | None:
    must: list[Any] = []
    if params.get("types"):
        must.append(
            qmodels.FieldCondition(
                key="type",
                match=qmodels.MatchAny(any=[item for item in params["types"] if item]),
            )
        )
    if params.get("sources"):
        must.append(
            qmodels.FieldCondition(
                key="source_name",
                match=qmodels.MatchAny(any=[item for item in params["sources"] if item]),
            )
        )
    if params.get("tags"):
        must.append(
            qmodels.FieldCondition(
                key="tags",
                match=qmodels.MatchAny(any=[item for item in params["tags"] if item]),
            )
        )
    if params.get("min_rating") is not None or params.get("max_rating") is not None:
        must.append(
            qmodels.FieldCondition(
                key="rating",
                range=qmodels.Range(gte=params.get("min_rating"), lte=params.get("max_rating")),
            )
        )
    if params.get("min_complexity") is not None or params.get("max_complexity") is not None:
        must.append(
            qmodels.FieldCondition(
                key="complexity",
                range=qmodels.Range(gte=params.get("min_complexity"), lte=params.get("max_complexity")),
            )
        )
    if params.get("date_from_ts") is not None or params.get("date_to_ts") is not None:
        must.append(
            qmodels.FieldCondition(
                key="published_ts",
                range=qmodels.Range(gte=params.get("date_from_ts"), lte=params.get("date_to_ts")),
            )
        )
    if not must:
        return None
    return qmodels.Filter(must=must)


async def qdrant_search(
    query: str | None,
    filters: qmodels.Filter | None,
    limit: int = 120,
) -> list[dict[str, Any]]:
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if not client or not getattr(app.state, "vector_ready", False):
        return []
    cfg: Config = app.state.config

    def _run_search() -> list[dict[str, Any]]:
        if query:
            vector = embed_text(query)
            records = client.search(
                collection_name=cfg.qdrant_collection,
                query_vector=vector,
                query_filter=filters,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        else:
            records, _ = client.scroll(
                collection_name=cfg.qdrant_collection,
                scroll_filter=filters,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        result: list[dict[str, Any]] = []
        for record in records:
            payload = qdrant_record_payload(record)
            payload["search_score"] = getattr(record, "score", None)
            result.append(payload)
        result.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
        return result

    return await asyncio.to_thread(_run_search)


async def load_source_catalog() -> list[dict[str, Any]]:
    cfg: Config = app.state.config
    redis_client: aioredis.Redis = app.state.redis
    current = await redis_get_json(redis_client, SOURCE_CATALOG_KEY, [])
    if not current:
        catalog = default_source_catalog(cfg)
        await redis_set_json(redis_client, SOURCE_CATALOG_KEY, catalog)
        return catalog
    deleted = await redis_client.smembers(SOURCE_DELETED_KEY)
    catalog = merge_catalog(default_source_catalog(cfg), current)
    return [item for item in catalog if item.get("id") not in deleted]


async def save_source_catalog(catalog: list[dict[str, Any]]) -> None:
    redis_client: aioredis.Redis = app.state.redis
    await redis_set_json(redis_client, SOURCE_CATALOG_KEY, catalog)
    app.state.source_catalog = catalog


async def load_provider_state() -> dict[str, Any]:
    cfg: Config = app.state.config
    redis_client: aioredis.Redis = app.state.redis
    state = await redis_get_json(redis_client, AI_PROVIDER_KEY, default_provider_state(cfg))
    state.setdefault("loaded_models", [])
    state.setdefault("name", cfg.provider_name)
    state.setdefault("kind", cfg.provider_kind)
    state.setdefault("base_url", cfg.provider_base_url)
    state.setdefault("api_key", cfg.provider_api_key)
    state.setdefault("model", cfg.provider_model)
    state.setdefault("monthly_token_limit", cfg.monthly_token_limit)
    state.setdefault("monthly_budget_usd", cfg.monthly_budget_usd)
    state.setdefault("input_price_per_1m", cfg.input_price_per_1m)
    state.setdefault("output_price_per_1m", cfg.output_price_per_1m)
    return state


async def save_provider_state(state: dict[str, Any]) -> None:
    redis_client: aioredis.Redis = app.state.redis
    state["updated_at"] = iso_now()
    await redis_set_json(redis_client, AI_PROVIDER_KEY, state)
    app.state.provider_state = state


async def record_usage(
    provider_name: str,
    model_name: str,
    function_name: str,
    usage: dict[str, int],
    cost_usd: float,
) -> None:
    redis_client: aioredis.Redis = app.state.redis
    session_key = f"{AI_USAGE_PREFIX}:session:{app.state.session_id}"
    period_key = f"{AI_USAGE_PREFIX}:period:{now_utc().strftime('%Y-%m')}"
    model_key = f"{AI_USAGE_PREFIX}:model:{slugify(model_name)}"
    provider_key = f"{AI_USAGE_PREFIX}:provider:{slugify(provider_name)}"
    function_key = f"{AI_USAGE_PREFIX}:function:{slugify(function_name)}"
    total_key = f"{AI_USAGE_PREFIX}:total"

    async def _apply(key: str) -> None:
        await redis_client.hincrby(key, "prompt_tokens", int(usage.get("prompt_tokens", 0)))
        await redis_client.hincrby(key, "completion_tokens", int(usage.get("completion_tokens", 0)))
        await redis_client.hincrby(key, "total_tokens", int(usage.get("total_tokens", 0)))
        await redis_client.hincrby(key, "calls", 1)
        await redis_client.hincrbyfloat(key, "cost_usd", float(cost_usd))

    await asyncio.gather(
        _apply(total_key),
        _apply(session_key),
        _apply(period_key),
        _apply(model_key),
        _apply(provider_key),
        _apply(function_key),
    )


def estimated_cost(usage: dict[str, int]) -> float:
    provider = app.state.provider_state
    input_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    input_price = float(provider.get("input_price_per_1m", app.state.config.input_price_per_1m))
    output_price = float(provider.get("output_price_per_1m", app.state.config.output_price_per_1m))
    return (input_tokens / 1_000_000) * input_price + (completion_tokens / 1_000_000) * output_price


def normalize_usage(raw_usage: dict[str, Any] | None, prompt_text: str = "", completion_text: str = "") -> dict[str, int]:
    if raw_usage:
        prompt_tokens = int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0)
        completion_tokens = int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0)
        total_tokens = int(raw_usage.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(completion_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


async def get_telemetry_snapshot() -> dict[str, Any]:
    cfg: Config = app.state.config
    redis_client: aioredis.Redis = app.state.redis
    total = await redis_client.hgetall(f"{AI_USAGE_PREFIX}:total")
    session = await redis_client.hgetall(f"{AI_USAGE_PREFIX}:session:{app.state.session_id}")
    period = await redis_client.hgetall(f"{AI_USAGE_PREFIX}:period:{now_utc().strftime('%Y-%m')}")
    provider = app.state.provider_state
    tokens = int(total.get("total_tokens", 0) or 0)
    total_cost = float(total.get("cost_usd", 0.0) or 0.0)
    token_limit = int(provider.get("monthly_token_limit", cfg.monthly_token_limit))
    budget_usd = float(provider.get("monthly_budget_usd", cfg.monthly_budget_usd))
    remaining_tokens = max(0, token_limit - tokens)
    remaining_money = max(0.0, budget_usd - total_cost)

    indexed_count = await count_artifacts()
    last_sync = await redis_client.get(LAST_SYNC_KEY)
    return {
        "provider": provider.get("name", cfg.provider_name),
        "provider_kind": provider.get("kind", cfg.provider_kind),
        "model": provider.get("model", cfg.provider_model),
        "tokens_total": tokens,
        "cost_total": round(total_cost, 4),
        "remaining_tokens": remaining_tokens,
        "remaining_usd": round(remaining_money, 4),
        "session_tokens": int(session.get("total_tokens", 0) or 0),
        "period_tokens": int(period.get("total_tokens", 0) or 0),
        "session_cost": float(session.get("cost_usd", 0.0) or 0.0),
        "period_cost": float(period.get("cost_usd", 0.0) or 0.0),
        "token_limit": token_limit,
        "budget_usd": budget_usd,
        "indexed_artifacts": indexed_count,
        "vector_status": getattr(app.state, "vector_status", "unknown"),
        "last_sync": last_sync or "-",
        "current_session": app.state.session_id,
    }


def provider_messages(action: str, text: str, records: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    if action == "summary":
        system = (
            "Ты кратко резюмируешь артефакт в 1-2 предложениях. Верни только JSON: "
            '{"summary":"...","killer_feature":"...","should_disappear":"...","tags":["..."],"category":"...","complexity":0,"anomaly_score":0,"entities":["..."]}'
        )
        user = text
    elif action == "analysis":
        system = (
            "Ты проводишь экспресс-анализ артефакта. Верни только JSON: "
            '{"summary":"...","killer_feature":"...","should_disappear":"...","tags":["english-tag"],"category":"...","complexity":0,"anomaly_score":0,"entities":["..."],"is_prompt":true,"prompt_title":"...","prompt_body":"...","how_it_works":"...","why_it_works":"...","structure":["..."],"coverage":["..."],"expected_output":"...","learning_complexity":{"level":"низкая|средняя|высокая","score":0,"reason":"..."},"literacy_score":0,"special_marks":["..."],"remarks":["..."]}'
            " Поле how_it_works человеческим языком описывает выполняемую операцию и логику. Укажи структуру, полноту покрытия, сложность освоения и конкретный результат."
            " Верни 3-5 английских тегов в lowercase-kebab-case, включая 1-2 узких предметных тега. Не добавляй ссылки или references."
            " Если вход не содержит самостоятельного применимого промпта, поставь is_prompt=false и prompt_body пустым."
        )
        user = text
    elif action == "augment":
        system = (
            "Ты дополняешь артефакт практическими улучшениями. Верни только JSON: "
            '{"summary":"...","killer_feature":"...","should_disappear":"...","augmentation":"...","tags":["..."],"category":"...","complexity":0,"anomaly_score":0,"entities":["..."]}'
        )
        user = text
    elif action == "prune":
        system = (
            "Ты определяешь, что должно исчезнуть или быть удалено. Верни только JSON: "
            '{"summary":"...","killer_feature":"...","should_disappear":"...","anti_patterns":["..."],"tags":["..."],"category":"...","complexity":0,"anomaly_score":0,"entities":["..."]}'
        )
        user = text
    elif action == "prompt_register_analysis":
        system = (
            "Ты сравниваешь выбранный набор промптов как prompt engineer. Верни только JSON: "
            '{"summary":"...","patterns":["..."],"strengths":["..."],"weaknesses":["..."],"differences":["..."],"use_cases":["..."],"merge_recommendation":"..."}'
            " Анализируй конструкцию, ограничения, ожидаемый выход и пригодность к объединению. Не перепечатывай промпты целиком."
        )
        user = text
    else:
        merged = render_prompt_merger(records or [])
        system = (
            "Ты собираешь canvas-пакет из выбранных артефактов. Верни только JSON: "
            '{"title":"...","purpose":"...","summary":"...","merged_prompt":"...","instructions":"...","killer_feature":"...","should_disappear":"...","tags":["..."]}'
        )
        user = merged
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def fetch_provider_models() -> list[str]:
    provider = app.state.provider_state
    base_url = provider.get("base_url", "").strip()
    api_key = provider.get("api_key", "").strip()
    if not base_url or not api_key:
        return []
    url = f"{api_root(base_url)}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
    items = payload.get("data", payload if isinstance(payload, list) else [])
    models = []
    for item in items:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
            if model_id:
                models.append(str(model_id))
        elif isinstance(item, str):
            models.append(item)
    return sorted(list(dict.fromkeys(models)))


async def call_provider(action: str, text: str, records: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, int], str]:
    provider = app.state.provider_state
    base_url = provider.get("base_url", "").strip()
    api_key = provider.get("api_key", "").strip()
    model = provider.get("model", "").strip() or app.state.config.provider_model
    if not base_url or not api_key:
        raise RuntimeError("AI provider is not configured")
    url = f"{api_root(base_url)}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = provider_messages(action, text, records)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.15,
        "response_format": {"type": "json_object"},
    }
    async with AsyncClient(timeout=40.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    choices = data.get("choices", [])
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "") or ""
    usage = normalize_usage(data.get("usage"), json.dumps(messages, ensure_ascii=False), content)
    parsed = safe_json_loads(
        content,
        {
            "summary": make_summary_from_text(text),
            "killer_feature": "Найден живой сигнал в источнике.",
            "should_disappear": "Шум и лишние повторы.",
            "tags": [],
            "category": "Prompt",
            "complexity": 50,
            "anomaly_score": 50,
            "entities": [],
        },
    )
    return parsed, usage, content


def build_record(item: dict[str, Any], analysis: dict[str, Any], provider_name: str, model_name: str, function_name: str) -> dict[str, Any]:
    path = item.get("path", "")
    raw = item.get("text", "")
    category = str(analysis.get("category") or "Noise")
    rating = int(clamp(float(analysis.get("anomaly_score", 0) or 0), 0, 100))
    tags = list(dict.fromkeys((analysis.get("tags") or []) + derive_tags(path, raw, category)))[:12]
    record = {
        "id": make_artifact_id(item.get("source_id", item.get("source_name", "unknown")), path, raw),
        "title": item.get("title") or make_title(path, raw, category),
        "path": path,
        "source_id": item.get("source_id", ""),
        "source_name": item.get("source_name", ""),
        "source_kind": item.get("source_kind", ""),
        "artifact_group": item.get("artifact_group", "Other"),
        "source_url": item.get("source_url", ""),
        "type": category,
        "rating": rating,
        "complexity": int(clamp(int(analysis.get("complexity", derive_complexity(category, raw))), 1, 100)),
        "summary": analysis.get("summary") or make_summary_from_text(raw),
        "killer_feature": analysis.get("killer_feature", ""),
        "should_disappear": analysis.get("should_disappear", ""),
        "augmentation": analysis.get("augmentation", ""),
        "anti_patterns": analysis.get("anti_patterns", []),
        "is_prompt": bool(analysis.get("is_prompt", False)),
        "prompt_body": str(analysis.get("prompt_body", ""))[:MAX_PROMPT_BODY_CHARS],
        "literacy_score": int(clamp(int(analysis.get("literacy_score", prompt_literacy_score(raw))), 1, 100)),
        "special_marks": analysis.get("special_marks", []),
        "remarks": analysis.get("remarks", []),
        "entities": analysis.get("entities", extract_entities(path, raw)),
        "tags": tags,
        "raw": raw[:MAX_SNIPPET_CHARS],
        "search_blob": normalize_ws(
            " ".join([item.get("title", ""), path, raw, " ".join(tags), category, item.get("source_name", ""), item.get("source_kind", "")])
        ).lower(),
        "published_at": item.get("published_at") or iso_now(),
        "published_ts": item.get("published_ts") or now_utc().timestamp(),
        "updated_at": iso_now(),
        "provider_name": provider_name,
        "model_name": model_name,
        "function_name": function_name,
        "session_id": app.state.session_id,
        "origin": item.get("origin", ""),
    }
    return record


def prompt_body_hash(body: str) -> str:
    normalized = normalize_ws(html.unescape(str(body or ""))).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def prompt_record_priority(prompt: dict[str, Any]) -> tuple[int, int]:
    prompt_id = str(prompt.get("id", ""))
    original_artifact_id = 0 if re.fullmatch(r"[0-9a-f]{24}", prompt_id) else 1
    serial_match = re.fullmatch(r"P-(\d{6})", str(prompt.get("serial", "")))
    serial_number = int(serial_match.group(1)) if serial_match else sys.maxsize
    return original_artifact_id, serial_number


def dedupe_prompt_records(prompts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canonical: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for prompt in prompts:
        body_hash = prompt_body_hash(prompt.get("prompt_body", ""))
        if not body_hash:
            continue
        current = canonical.get(body_hash)
        if current is None:
            canonical[body_hash] = prompt
        elif prompt_record_priority(prompt) < prompt_record_priority(current):
            duplicates.append(current)
            canonical[body_hash] = prompt
        else:
            duplicates.append(prompt)
    kept_ids = {str(prompt.get("id")) for prompt in canonical.values()}
    kept = [prompt for prompt in prompts if str(prompt.get("id")) in kept_ids]
    return kept, duplicates


async def store_prompt_projection(record: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any] | None:
    prompt = build_prompt_projection(record, analysis)
    if not prompt:
        return None
    redis_client: aioredis.Redis = app.state.redis
    body_hash = prompt_body_hash(prompt.get("prompt_body", ""))
    canonical_id = await redis_client.hget(PROMPTS_BODY_INDEX_KEY, body_hash) if body_hash else None
    if canonical_id and canonical_id != str(prompt["id"]):
        canonical_raw = await redis_client.hget(PROMPTS_HASH_KEY, canonical_id)
        if canonical_raw:
            prompt["id"] = canonical_id
        else:
            await redis_client.hdel(PROMPTS_BODY_INDEX_KEY, body_hash)
    existing_raw = await redis_client.hget(PROMPTS_HASH_KEY, str(prompt["id"]))
    if existing_raw:
        existing = json.loads(existing_raw)
        prompt["serial"] = existing.get("serial")
    else:
        prompt["serial"] = f"P-{await redis_client.incr(PROMPTS_SERIAL_KEY):06d}"
    await redis_client.hset(PROMPTS_HASH_KEY, str(prompt["id"]), json.dumps(prompt, ensure_ascii=False))
    await redis_client.hset(PROMPTS_SERIAL_INDEX_KEY, str(prompt["serial"]), str(prompt["id"]))
    if body_hash:
        await redis_client.hset(PROMPTS_BODY_INDEX_KEY, body_hash, str(prompt["id"]))
    await redis_client.zadd(PROMPTS_ORDER_KEY, {str(prompt["id"]): prompt["published_ts"]})
    overflow = await redis_client.zcard(PROMPTS_ORDER_KEY) - MAX_PROMPT_CATALOG
    if overflow > 0:
        stale_ids = await redis_client.zrange(PROMPTS_ORDER_KEY, 0, overflow - 1)
        if stale_ids:
            stale_raw = await redis_client.hmget(PROMPTS_HASH_KEY, stale_ids)
            stale_prompts = [json.loads(raw) for raw in stale_raw if raw]
            stale_serials = [item.get("serial") for item in stale_prompts if item.get("serial")]
            stale_hashes = [prompt_body_hash(item.get("prompt_body", "")) for item in stale_prompts]
            await redis_client.zrem(PROMPTS_ORDER_KEY, *stale_ids)
            await redis_client.hdel(PROMPTS_HASH_KEY, *stale_ids)
            if stale_serials:
                await redis_client.hdel(PROMPTS_SERIAL_INDEX_KEY, *stale_serials)
            if stale_hashes:
                await redis_client.hdel(PROMPTS_BODY_INDEX_KEY, *stale_hashes)
    return prompt

async def load_prompt_catalog(limit: int = 200) -> list[dict[str, Any]]:
    redis_client: aioredis.Redis = app.state.redis
    ids = await redis_client.zrevrange(PROMPTS_ORDER_KEY, 0, max(0, min(limit, MAX_PROMPT_CATALOG) - 1))
    if not ids:
        return []
    raw_items = await redis_client.hmget(PROMPTS_HASH_KEY, ids)
    return [json.loads(raw) for raw in raw_items if raw]


async def backfill_prompt_catalog() -> None:
    records = await load_recent_artifacts(MAX_RECENT_ARTIFACTS)
    stored = 0
    for record in records:
        if await store_prompt_projection(record):
            stored += 1

    prompts = await load_prompt_catalog(MAX_PROMPT_CATALOG)
    prompts, duplicates = dedupe_prompt_records(prompts)
    if duplicates:
        duplicate_ids = [str(prompt["id"]) for prompt in duplicates if prompt.get("id")]
        duplicate_serials = [str(prompt["serial"]) for prompt in duplicates if prompt.get("serial")]
        if duplicate_ids:
            await app.state.redis.zrem(PROMPTS_ORDER_KEY, *duplicate_ids)
            await app.state.redis.hdel(PROMPTS_HASH_KEY, *duplicate_ids)
        if duplicate_serials:
            await app.state.redis.hdel(PROMPTS_SERIAL_INDEX_KEY, *duplicate_serials)

    description_updates = {}
    for prompt in prompts:
        title = prompt.get("title", "Untitled prompt")
        body = prompt.get("prompt_body", "")
        category = prompt.get("prompt_type", "Prompt")
        complexity = int(prompt.get("complexity", derive_complexity(category, body)))
        mechanics = prompt_mechanics(title, body, complexity)
        tags, tag_origin = prompt_tags(title, body, category, prompt.get("tags", []))
        prompt.update(mechanics)
        prompt["description"] = (
            f"Как работает: {mechanics['how_it_works']} "
            f"Почему работает: {mechanics['why_it_works']} "
            f"На выходе: {mechanics['expected_output']}."
        )[:1500]
        prompt["description_origin"] = prompt.get("description_origin", "reconstructed")
        prompt["tags"] = tags
        prompt["tag_origin"] = tag_origin
        prompt["token_estimate"] = prompt_token_estimate(body, mechanics["expected_output"])
        prompt["schema_version"] = 2
        description_updates[str(prompt["id"])] = json.dumps(prompt, ensure_ascii=False)
    if description_updates:
        await app.state.redis.hset(PROMPTS_HASH_KEY, mapping=description_updates)
        serial_index = {str(prompt["serial"]): str(prompt["id"]) for prompt in prompts if prompt.get("serial") and prompt.get("id")}
        body_index = {prompt_body_hash(prompt.get("prompt_body", "")): str(prompt["id"]) for prompt in prompts if prompt.get("id") and prompt_body_hash(prompt.get("prompt_body", ""))}
        if serial_index:
            await app.state.redis.hset(PROMPTS_SERIAL_INDEX_KEY, mapping=serial_index)
        await app.state.redis.delete(PROMPTS_BODY_INDEX_KEY)
        if body_index:
            await app.state.redis.hset(PROMPTS_BODY_INDEX_KEY, mapping=body_index)
    if stored or description_updates or duplicates:
        logging.info(
            "Backfilled %s prompts, refreshed %s descriptions, removed %s duplicates",
            stored, len(description_updates), len(duplicates),
        )


async def store_artifact(record: dict[str, Any]) -> None:
    redis_client: aioredis.Redis = app.state.redis
    await redis_client.lpush(ARTIFACTS_KEY, json.dumps(record, ensure_ascii=False))
    await redis_client.ltrim(ARTIFACTS_KEY, 0, MAX_RECENT_ARTIFACTS - 1)
    if int(record.get("rating", 0)) >= 70:
        await redis_client.lpush(ALERTS_KEY, json.dumps(record, ensure_ascii=False))
        await redis_client.ltrim(ALERTS_KEY, 0, MAX_ALERTS - 1)
    await redis_client.set(LAST_SYNC_KEY, iso_now())
    try:
        await qdrant_upsert(record)
    except Exception as exc:
        app.state.vector_status = "degraded"
        logging.error("Vector upsert failed for %s: %s", record.get("id"), exc)


async def process_item(item: dict[str, Any], function_name: str = "ingest") -> dict[str, Any] | None:
    redis_client: aioredis.Redis = app.state.redis
    cfg: Config = app.state.config
    source_id = item.get("source_id", item.get("source_name", "unknown"))
    raw = item.get("text", "")
    path = item.get("path", "")
    dupe_key = f"{SEEN_PREFIX}:{dedupe_text(source_id, path, raw)}"
    was_new = await redis_client.set(dupe_key, "1", ex=DUPE_TTL_SECONDS, nx=True)
    if not was_new:
        return None

    provider_name = app.state.provider_state.get("name", cfg.provider_name)
    model_name = app.state.provider_state.get("model", cfg.provider_model)
    is_prompt_csv = item.get("source_kind") == "prompt_csv"
    analysis = heuristic_analysis(raw, "" if is_prompt_csv else path)
    if is_prompt_csv:
        title = str(item.get("title") or "Untitled prompt")
        analysis.update({
            "category": "Prompt",
            "summary": prompt_mechanics_description(title, raw),
            "tags": derive_tags("", f"{title}\n{raw}", "Prompt"),
            "complexity": derive_complexity("Prompt", raw),
            "is_prompt": True,
        })
    if provider_is_configured() and not is_prompt_csv:
        try:
            parsed, usage, _ = await call_provider("analysis", raw)
            analysis = {
                **analysis,
                **parsed,
                "summary": parsed.get("summary") or analysis.get("summary"),
            }
            usage_normalized = normalize_usage(usage, raw, json.dumps(parsed, ensure_ascii=False))
            cost = estimated_cost(usage_normalized)
            await record_usage(provider_name, model_name, function_name, usage_normalized, cost)
        except Exception as exc:
            logging.warning("AI analysis fallback: %s", exc)
    record = build_record(item, analysis, provider_name, model_name, function_name)
    await store_artifact(record)
    await store_prompt_projection(record, analysis)
    try:
        await evaluate_ingested_record(record)
    except Exception as exc:
        logging.error("Publishing evaluation failed for %s: %s", record.get("id"), exc)
    return record


async def fetch_rss_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    response = await client.get(
        source["url"],
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Prompt-Ops-Control-Tower/1.0)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    response.raise_for_status()
    parsed = await asyncio.to_thread(feedparser.parse, response.text)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries[:12]:
        title = entry.get("title") or entry.get("summary") or entry.get("description") or "Untitled"
        summary = entry.get("summary", entry.get("description", ""))
        link = entry.get("link", source["url"])
        published = entry.get("published") or entry.get("updated") or iso_now()
        items.append(
            {
                "text": f"Title: {title}\nSummary: {summary}\nLink: {link}",
                "path": link,
                "source_id": source["id"],
                "source_name": source["name"],
                "source_kind": source["kind"],
                "artifact_group": source_artifact_group(source),
                "source_url": source["url"],
                "title": title,
                "summary": summary,
                "published_at": published,
                "published_ts": parse_iso_ts(published),
                "origin": source["kind"],
            }
        )
    return items


async def fetch_github_atom_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"https://github.com/{source['repo']}/commits/{source['branch']}.atom"
    response = await client.get(
        url,
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Prompt-Ops-Control-Tower/1.0)",
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    response.raise_for_status()
    parsed = await asyncio.to_thread(feedparser.parse, response.text)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries[:12]:
        title = entry.get("title") or entry.get("summary") or "Untitled"
        summary = entry.get("summary", "")
        link = entry.get("link", url)
        published = entry.get("published") or entry.get("updated") or iso_now()
        items.append(
            {
                "text": f"Title: {title}\nSummary: {summary}\nLink: {link}\nRepo: {source['repo']}",
                "path": source["repo"],
                "source_id": source["id"],
                "source_name": source["name"],
                "source_kind": source["kind"],
                "artifact_group": source_artifact_group(source),
                "source_url": url,
                "title": title,
                "summary": summary,
                "published_at": published,
                "published_ts": parse_iso_ts(published),
                "origin": source["kind"],
            }
        )
    return items


async def fetch_web_page_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    response = await client.get(
        source["url"],
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Prompt-Ops-Control-Tower/1.0)"},
    )
    response.raise_for_status()
    raw_html = response.text
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(normalize_ws(title_match.group(1))) if title_match else source["name"]
    cleaned = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = html.unescape(normalize_ws(re.sub(r"<[^>]+>", " ", cleaned)))[:MAX_SNIPPET_CHARS]
    return [{
        "text": f"Title: {title}\nSnapshot: {cleaned}\nLink: {source['url']}",
        "path": source["url"],
        "source_id": source["id"],
        "source_name": source["name"],
        "source_kind": source["kind"],
        "artifact_group": source_artifact_group(source),
        "source_url": source["url"],
        "title": title,
        "summary": cleaned[:220],
        "published_at": iso_now(),
        "published_ts": now_utc().timestamp(),
        "origin": "web_page",
    }]


async def read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


async def scan_workspace_root(root: str) -> list[dict[str, Any]]:
    root_path = Path(root)
    if not root_path.exists():
        return []
    items: list[dict[str, Any]] = []
    for current_root, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDE_DIRS]
        base = Path(current_root)
        for filename in filenames:
            path = base / filename
            if path.suffix.lower() not in SOURCE_EXTENSIONS and path.name not in {"Dockerfile", "docker-compose.yml"}:
                continue
            content = await read_text_file(path)
            if not content:
                continue
            category, _ = classify_artifact(str(path), content)
            excerpt = content.strip().replace("\r\n", "\n")[:MAX_SNIPPET_CHARS]
            items.append(
                {
                    "text": f"Path: {path}\nCategory: {category}\n\n{excerpt}",
                    "path": str(path),
                    "source_id": f"workspace_{slugify(str(root_path))}",
                    "source_name": f"Workspace: {root_path}",
                    "source_kind": "workspace",
                    "artifact_group": "Workspace",
                    "source_url": str(root_path),
                    "title": path.name,
                    "summary": excerpt[:200],
                    "published_at": iso_now(),
                    "published_ts": now_utc().timestamp(),
                    "origin": "workspace",
                }
            )
    return items


async def fetch_x_search_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    bearer_token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not bearer_token:
        raise RuntimeError("X_BEARER_TOKEN is required for x_search sources")
    response = await client.get(
        "https://api.x.com/2/tweets/search/recent",
        params={
            "query": source["query"],
            "max_results": 10,
            "tweet.fields": "created_at,author_id,lang",
        },
        headers={"Authorization": f"Bearer {bearer_token}"},
        timeout=20.0,
    )
    response.raise_for_status()
    items: list[dict[str, Any]] = []
    for tweet in response.json().get("data", []):
        tweet_id = str(tweet.get("id", ""))
        text = str(tweet.get("text", "")).strip()
        if not tweet_id or not text:
            continue
        link = f"https://x.com/i/web/status/{tweet_id}"
        published = tweet.get("created_at") or iso_now()
        items.append({
            "text": f"X post: {text}\nLink: {link}",
            "path": link,
            "source_id": source["id"],
            "source_name": source["name"],
            "source_kind": source["kind"],
            "artifact_group": source_artifact_group(source),
            "source_url": link,
            "title": normalize_ws(text)[:120],
            "summary": normalize_ws(text)[:220],
            "published_at": published,
            "published_ts": parse_iso_ts(published),
            "origin": "x_search",
        })
    return items


async def fetch_prompt_csv_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    csv.field_size_limit(min(sys.maxsize, 10_000_000))
    response = await client.get(source["url"], timeout=30.0, follow_redirects=True, headers={"User-Agent": "Prompt-Ops-Control-Tower/1.0"})
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    if not rows:
        return []
    offset = int(source.get("csv_offset", 0)) % len(rows)
    batch_size = int(clamp(int(source.get("csv_batch_size", 80)), 1, 200))
    selected = (rows + rows)[offset:offset + min(batch_size, len(rows))]
    source["csv_offset"] = (offset + len(selected)) % len(rows)
    items = []
    for index, row in enumerate(selected, start=offset):
        body = str(row.get("prompt") or row.get("Prompt") or "").strip()
        title = str(row.get("act") or row.get("title") or row.get("name") or f"Prompt {index + 1}").strip()
        if len(body) < 40:
            continue
        items.append({
            "text": body[:MAX_PROMPT_BODY_CHARS],
            "path": f"{source['url']}#row-{index + 2}",
            "source_id": source["id"],
            "source_name": source["name"],
            "source_kind": "prompt_csv",
            "artifact_group": source_artifact_group(source),
            "source_url": source["url"],
            "title": title[:160],
            "summary": f"Готовый промпт: {title}",
            "published_at": iso_now(),
            "published_ts": now_utc().timestamp() + index / 100_000,
            "origin": "prompt_csv",
        })
    return items


async def fetch_feed_items(client: AsyncClient, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not source.get("enabled", True) or source.get("paused"):
        return []
    kind = source["kind"]
    if kind == "prompt_csv":
        return await fetch_prompt_csv_items(client, source)
    if kind == "workspace":
        return await scan_workspace_root(source["root"])
    if kind == "rss":
        return await fetch_rss_items(client, source)
    if kind == "github_atom":
        return await fetch_github_atom_items(client, source)
    if kind == "web_page":
        return await fetch_web_page_items(client, source)
    if kind == "x_search":
        return await fetch_x_search_items(client, source)
    return []


def source_effective_interval(source: dict[str, Any]) -> int:
    manual = source.get("manual_interval_seconds")
    recommended = int(source.get("recommended_interval_seconds", 3600))
    if manual:
        return max(30, int(manual))
    empty_streak = int(source.get("empty_streak", 0))
    error_streak = int(source.get("error_streak", 0))
    backoff = 1.0
    if empty_streak:
        backoff *= min(4.0, 1.4 ** empty_streak)
    if error_streak:
        backoff *= min(8.0, 1.8 ** error_streak)
    return int(clamp(recommended * backoff, 60, 6 * 60 * 60))


def source_cadence_label(source: dict[str, Any]) -> str:
    interval = source_effective_interval(source)
    if interval < 1800:
        return "high"
    if interval < 7200:
        return "medium"
    return "slow"


async def mark_source_state(source_id: str, state: str, detail: str, **extra: Any) -> None:
    catalog = await load_source_catalog()
    updated = []
    for source in catalog:
        if source["id"] == source_id:
            source = {**source, "state": state, "detail": detail, "updated_at": iso_now(), **extra}
            if state == "running":
                source["last_attempt_at"] = iso_now()
            if state == "success":
                source["last_success_at"] = iso_now()
        updated.append(source)
    await save_source_catalog(updated)
    app.state.source_status[source_id] = {"state": state, "detail": detail, "updated_at": iso_now(), **extra}


async def refresh_source_schedule(source: dict[str, Any], success: bool, item_count: int) -> dict[str, Any]:
    next_interval = source_effective_interval(source)
    if success:
        if item_count == 0:
            source["empty_streak"] = int(source.get("empty_streak", 0)) + 1
        else:
            source["empty_streak"] = 0
        source["error_streak"] = 0
        next_interval = source_effective_interval(source)
    else:
        source["error_streak"] = int(source.get("error_streak", 0)) + 1
        next_interval = source_effective_interval(source)
    source["next_refresh_at"] = (now_utc() + timedelta(seconds=next_interval)).strftime(DATE_FMT)
    source["state"] = "success" if success else "error"
    source["detail"] = f"{item_count} items processed" if success else source.get("detail", "error")
    source["updated_at"] = iso_now()
    return source


def source_due(source: dict[str, Any], current: datetime) -> bool:
    if not source.get("enabled", True) or source.get("paused"):
        return False
    next_refresh_at = parse_dt(source.get("next_refresh_at"))
    if not next_refresh_at:
        return True
    return next_refresh_at <= current


async def fetch_due_sources(http_client: AsyncClient, redis_client: aioredis.Redis, sources: list[dict[str, Any]]) -> None:
    for source in sources:
        source_id = source["id"]
        await mark_source_state(source_id, "running", "scanning")
        try:
            items = await fetch_feed_items(http_client, source)
            processed = 0
            for item in items:
                record = await process_item(item)
                if record:
                    processed += 1
            updated = await refresh_source_schedule(source, True, processed)
            await mark_source_state(source_id, updated["state"], updated["detail"], next_refresh_at=updated["next_refresh_at"])
            catalog = await load_source_catalog()
            for idx, current in enumerate(catalog):
                if current["id"] == source_id:
                    catalog[idx] = {**current, **updated}
                    break
            await save_source_catalog(catalog)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error("Source %s failed: %s", source_id, exc)
            source["detail"] = str(exc)[:120]
            updated = await refresh_source_schedule(source, False, 0)
            updated["detail"] = source["detail"]
            await mark_source_state(source_id, "error", updated["detail"], next_refresh_at=updated["next_refresh_at"])
            catalog = await load_source_catalog()
            for idx, current in enumerate(catalog):
                if current["id"] == source_id:
                    catalog[idx] = {**current, **updated}
                    break
            await save_source_catalog(catalog)


async def poll_sources_loop(http_client: AsyncClient, redis_client: aioredis.Redis) -> None:
    while True:
        catalog = await load_source_catalog()
        current = now_utc()
        due = [source for source in catalog if source_due(source, current)]
        if due:
            await fetch_due_sources(http_client, redis_client, due[:4])
        next_refreshes = [parse_dt(item.get("next_refresh_at")) for item in catalog if item.get("enabled", True) and not item.get("paused")]
        future_slots = [slot for slot in next_refreshes if slot and slot > current]
        sleep_for = app.state.config.poll_tick_seconds
        if future_slots:
            sleep_for = min(sleep_for, max(15, int((min(future_slots) - current).total_seconds())))
        await asyncio.sleep(max(15, sleep_for))


async def vector_watchdog_loop() -> None:
    while True:
        if getattr(app.state, "qdrant", None) and not getattr(app.state, "vector_ready", False):
            try:
                await ensure_qdrant_collection()
            except Exception as exc:
                logging.warning("Qdrant watchdog retry failed: %s", exc)
        await asyncio.sleep(60)


async def reindex_recent_artifacts() -> None:
    if not getattr(app.state, "vector_ready", False):
        return
    redis_client: aioredis.Redis = app.state.redis
    raw_records = await redis_client.lrange(ARTIFACTS_KEY, 0, MAX_RECENT_ARTIFACTS - 1)
    indexed = 0
    for raw in raw_records:
        try:
            await qdrant_upsert(json.loads(raw))
            indexed += 1
        except Exception as exc:
            app.state.vector_status = "degraded"
            logging.warning("Reindex stopped after %s records: %s", indexed, exc)
            return
    if indexed:
        app.state.vector_status = "ready"
        logging.info("Reindexed %s recent artifacts", indexed)


async def start_telethon_userbot(http_client: AsyncClient, redis_client: aioredis.Redis) -> None:
    cfg: Config = app.state.config
    if not cfg.has_telethon:
        logging.info("Telethon disabled because credentials are missing")
        return
    os.makedirs("sessions", exist_ok=True)
    client = TelegramClient("sessions/userbot", cfg.telegram_api_id, cfg.telegram_api_hash)

    @client.on(events.NewMessage(chats=cfg.target_channels if cfg.target_channels else None))
    async def handler(event: Any) -> None:
        text = event.message.message
        if not text:
            return
        chat = await event.get_chat()
        chat_title = getattr(chat, "title", getattr(chat, "username", "Telegram Chat"))
        await process_item(
            {
                "text": text,
                "path": "telegram",
                "source_id": f"tg_{slugify(chat_title)}",
                "source_name": f"TG: {chat_title}",
                "source_kind": "telegram",
                "source_url": "",
                "title": chat_title,
                "summary": text[:160],
                "published_at": iso_now(),
                "published_ts": now_utc().timestamp(),
                "origin": "telegram",
            },
            function_name="telegram",
        )

    await client.start()
    app.state.telethon_client = client
    logging.info("Telethon userbot started")
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    finally:
        app.state.telethon_client = None


async def count_artifacts() -> int:
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if client and getattr(app.state, "vector_ready", False):
        cfg: Config = app.state.config

        def _count() -> int:
            return client.count(collection_name=cfg.qdrant_collection, exact=True).count

        try:
            return int(await asyncio.to_thread(_count))
        except Exception:
            pass
    redis_client: aioredis.Redis = app.state.redis
    return int(await redis_client.llen(ARTIFACTS_KEY))


async def load_recent_artifacts(limit: int = 80) -> list[dict[str, Any]]:
    client: QdrantClient | None = getattr(app.state, "qdrant", None)
    if client and getattr(app.state, "vector_ready", False):
        cfg: Config = app.state.config

        def _scroll() -> list[dict[str, Any]]:
            records, _ = client.scroll(
                collection_name=cfg.qdrant_collection,
                limit=max(limit, 200),
                with_payload=True,
                with_vectors=False,
            )
            items: list[dict[str, Any]] = []
            for record in records:
                payload = qdrant_record_payload(record)
                items.append(payload)
            items.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
            return items[:limit]

        try:
            return await asyncio.to_thread(_scroll)
        except Exception:
            pass
    redis_client: aioredis.Redis = app.state.redis
    raw = await redis_client.lrange(ARTIFACTS_KEY, 0, limit - 1)
    return [json.loads(item) for item in raw]


async def load_alerts(limit: int = 40) -> list[dict[str, Any]]:
    redis_client: aioredis.Redis = app.state.redis
    raw = await redis_client.lrange(ALERTS_KEY, 0, limit - 1)
    return [json.loads(item) for item in raw]


def apply_client_filters(artifacts: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    if not params:
        return artifacts
    query = normalize_ws(str(params.get("q", "") or "")).lower()
    semantic = normalize_ws(str(params.get("semantic", "") or "")).lower()
    search_text = semantic or query
    types = {item.strip() for item in params.get("types", []) if item.strip()}
    sources = {item.strip() for item in params.get("sources", []) if item.strip()}
    tags = {item.strip().lower() for item in params.get("tags", []) if item.strip()}
    min_rating = params.get("min_rating")
    max_rating = params.get("max_rating")
    min_complexity = params.get("min_complexity")
    max_complexity = params.get("max_complexity")
    date_from_ts = params.get("date_from_ts")
    date_to_ts = params.get("date_to_ts")

    filtered = []
    for item in artifacts:
        if types and item.get("type") not in types:
            continue
        if sources and item.get("source_name") not in sources and item.get("source_id") not in sources:
            continue
        if min_rating is not None and int(item.get("rating", 0)) < min_rating:
            continue
        if max_rating is not None and int(item.get("rating", 0)) > max_rating:
            continue
        if min_complexity is not None and int(item.get("complexity", 0)) < min_complexity:
            continue
        if max_complexity is not None and int(item.get("complexity", 0)) > max_complexity:
            continue
        if date_from_ts is not None and float(item.get("published_ts", 0)) < date_from_ts:
            continue
        if date_to_ts is not None and float(item.get("published_ts", 0)) > date_to_ts:
            continue
        if tags:
            item_tags = {str(tag).lower() for tag in item.get("tags", [])}
            if not (tags & item_tags):
                continue
        if search_text:
            blob = normalize_ws(
                " ".join(
                    [
                        item.get("title", ""),
                        item.get("summary", ""),
                        item.get("raw", ""),
                        " ".join(item.get("tags", [])),
                        item.get("source_name", ""),
                        item.get("source_id", ""),
                    ]
                )
            ).lower()
            if search_text not in blob and not any(search_text in str(tag).lower() for tag in item.get("tags", [])):
                continue
        filtered.append(item)
    filtered.sort(key=lambda item: item.get("published_ts", 0), reverse=True)
    return filtered


async def query_artifacts(params: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    query = params.get("semantic") or params.get("q")
    filters = build_filter_conditions(params)
    if query and getattr(app.state, "vector_ready", False):
        try:
            return await qdrant_search(query, filters, limit=limit)
        except Exception as exc:
            logging.warning("Vector search failed, falling back to recent list: %s", exc)
    if getattr(app.state, "vector_ready", False):
        try:
            return await qdrant_search(None, filters, limit=limit)
        except Exception:
            pass
    artifacts = await load_recent_artifacts(limit=MAX_PROMPT_ITEMS)
    return apply_client_filters(artifacts, params)


def parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_request_filters(request: Request) -> dict[str, Any]:
    qp = request.query_params
    params: dict[str, Any] = {
        "q": qp.get("q", ""),
        "semantic": qp.get("semantic", ""),
        "types": parse_csv(qp.get("types")),
        "sources": parse_csv(qp.get("sources")),
        "tags": parse_csv(qp.get("tags")),
    }
    for key in ["min_rating", "max_rating", "min_complexity", "max_complexity"]:
        raw = qp.get(key, "")
        if raw:
            try:
                params[key] = int(raw)
            except Exception:
                params[key] = None
    if qp.get("date_from"):
        try:
            start = datetime.strptime(qp.get("date_from"), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            params["date_from_ts"] = start.timestamp()
        except Exception:
            params["date_from_ts"] = None
    if qp.get("date_to"):
        try:
            end = datetime.strptime(qp.get("date_to"), "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            params["date_to_ts"] = end.timestamp()
        except Exception:
            params["date_to_ts"] = None
    return params


def render_badge(text: str, cls: str = "") -> str:
    return f'<span class="badge {cls}">{html.escape(text)}</span>'


def render_source_card(source: dict[str, Any]) -> str:
    state_raw = str(source.get("state", "idle"))
    state = state_raw if state_raw in {"success", "running", "error", "idle"} else "idle"
    marker = {"success": "●", "running": "◆", "error": "!", "idle": "·"}[state]
    source_id = html.escape(str(source.get("id", "")), quote=True)
    detail = html.escape(str(source.get("detail", "waiting")))
    cadence = html.escape(source_cadence_label(source))
    interval = int(source_effective_interval(source))
    next_refresh = html.escape(format_relative(source.get("next_refresh_at")))
    paused = bool(source.get("paused"))
    enabled = bool(source.get("enabled", True))
    action_state = "resume" if paused else "pause"
    return f"""
    <div class="source-row {state}" data-source-id="{source_id}" title="{detail}">
        <span class="state">{marker}</span>
        <span class="source-name">{html.escape(source.get('name', ''))}<small>{detail}</small></span>
        <span>{html.escape(source.get('kind', ''))}</span>
        <span>{cadence}</span>
        <span>{next_refresh}</span>
        <span class="source-actions">
            <input aria-label="interval" type="number" min="30" step="30" value="{interval}" data-source-interval="{source_id}">
            <button class="tinybtn" title="save interval" onclick="saveSourceInterval('{source_id}')">[s]</button>
            <button class="tinybtn" title="{action_state}" onclick="toggleSourcePaused('{source_id}', {str(paused).lower()})">[{action_state[0]}]</button>
            <button class="tinybtn" title="{'disable' if enabled else 'enable'}" onclick="toggleSourceEnabled('{source_id}', {str(not enabled).lower()})">[{'on' if enabled else 'off'}]</button>
            <button class="tinybtn" title="delete" onclick="deleteSource('{source_id}')">[x]</button>
        </span>
    </div>
    """


def render_source_groups(sources: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        grouped[source_artifact_group(source)].append(source)
    if not grouped:
        return '<div class="empty">NO_ACTIVE_SOURCES</div>'
    order = {name: index for index, name in enumerate(SOURCE_GROUP_ORDER)}
    sections = []
    for group_name in sorted(grouped, key=lambda name: (order.get(name, 999), name.lower())):
        group_sources = grouped[group_name]
        group_key = slugify(group_name)
        rows = "".join(render_source_card(source) for source in group_sources)
        enabled_count = sum(bool(source.get("enabled", True)) for source in group_sources)
        sections.append(
            f'<details class="source-group" data-source-group="{html.escape(group_key, quote=True)}" open>'
            f'<summary><span>{html.escape(group_name)}</span><small>{enabled_count}/{len(group_sources)} on</small></summary>'
            f'<div class="source-group-body">{rows}</div></details>'
        )
    return "".join(sections)


def render_artifact_card(item: dict[str, Any]) -> str:
    score = int(item.get("rating", 0) or 0)
    score_cls = "high" if score >= 70 else "mid" if score >= 40 else "low"
    artifact_id = str(item.get("id", ""))
    tags = " ".join(f"#{tag}" for tag in item.get("tags", [])[:5]) or "-"
    search_blob = normalize_ws(" ".join([
        item.get("title", ""), item.get("summary", ""), item.get("source_name", ""),
        item.get("type", ""), " ".join(item.get("tags", [])), item.get("raw", "")[:600],
    ])).lower()
    return f"""
    <div class="artifact-row" role="row" tabindex="-1" data-artifact-id="{html.escape(artifact_id, quote=True)}" data-search="{html.escape(search_blob, quote=True)}">
        <span class="mark cell">[ ]</span>
        <span class="cell">{html.escape(artifact_id[:8])}</span>
        <span class="score {score_cls} cell">{score:03d}</span>
        <span class="atype cell">{html.escape(item.get('type', 'Unknown'))}</span>
        <span class="tags cell">{html.escape(tags)}</span>
        <span class="cell">{html.escape(item.get('title', 'Untitled'))}</span>
    </div>
    """


def render_telemetry_bar(snapshot: dict[str, Any]) -> str:
    return f"""
    <div class="telemetry-bar">
        <div class="telemetry-left">
            <strong>{html.escape(snapshot['provider'])}</strong>
            <span>{html.escape(snapshot['model'])}</span>
            <span>vector: {html.escape(snapshot['vector_status'])}</span>
        </div>
        <div class="telemetry-mid">
            <span>tokens {snapshot['tokens_total']}/{snapshot['token_limit']}</span>
            <span>session {snapshot['session_tokens']}</span>
            <span>period {snapshot['period_tokens']}</span>
            <span>cost ${snapshot['cost_total']}</span>
        </div>
        <div class="telemetry-right">
            <span>remaining {snapshot['remaining_tokens']} tok</span>
            <span>${snapshot['remaining_usd']} left</span>
            <span>indexed {snapshot['indexed_artifacts']}</span>
            <span>sync {html.escape(format_relative(snapshot['last_sync']))}</span>
        </div>
    </div>
    """


def render_provider_panel(provider: dict[str, Any], models: list[str]) -> str:
    model_options = "".join(
        f'<option value="{html.escape(model)}" {"selected" if model == provider.get("model") else ""}>{html.escape(model)}</option>'
        for model in (models or [provider.get("model", "gpt-4o-mini")])
    )
    return f"""
    <div class="panel">
        <h2>AI Provider</h2>
        <div class="form-grid">
            <input id="providerName" value="{html.escape(provider.get('name', ''))}" placeholder="Provider name" />
            <input id="providerKind" value="{html.escape(provider.get('kind', 'openai_compatible'))}" placeholder="Provider kind" />
            <input id="providerBaseUrl" value="{html.escape(provider.get('base_url', ''))}" placeholder="Base URL" />
            <input id="providerApiKey" value="" type="password" autocomplete="new-password" placeholder="API key (оставьте пустым, чтобы не менять)" />
            <select id="providerModel">{model_options}</select>
            <input id="providerTokenLimit" type="number" value="{int(provider.get('monthly_token_limit', 500000))}" placeholder="Token limit" />
            <input id="providerBudget" type="number" step="0.01" value="{float(provider.get('monthly_budget_usd', 25.0))}" placeholder="Budget USD" />
            <input id="providerInputPrice" type="number" step="0.01" value="{float(provider.get('input_price_per_1m', 2.0))}" placeholder="Input $/1M" />
            <input id="providerOutputPrice" type="number" step="0.01" value="{float(provider.get('output_price_per_1m', 8.0))}" placeholder="Output $/1M" />
        </div>
        <div class="panel-actions">
            <button type="button" onclick="loadModels()">Load models</button>
            <button type="button" onclick="saveProvider()">Save provider</button>
        </div>
    </div>
    """


def render_source_form() -> str:
    options = """
        <option value="rss">rss</option>
        <option value="github_atom">github_atom</option>
        <option value="web_page">web_page</option>
        <option value="x_search">x_search</option>
        <option value="workspace">workspace</option>
    """
    return f"""
    <div class="panel">
        <h2>Add source</h2>
        <div class="form-grid">
            <input id="newSourceName" placeholder="Name" />
            <select id="newSourceKind">{options}</select>
            <input id="newSourceUrl" placeholder="RSS URL or workspace root" />
            <input id="newSourceRepo" placeholder="GitHub repo" />
            <input id="newSourceBranch" placeholder="Branch" value="main" />
            <input id="newSourceInterval" type="number" min="30" step="30" value="3600" placeholder="Interval seconds" />
            <input id="newSourceReason" placeholder="Cadence reason" />
            <input id="newSourceGroup" placeholder="Artifact group" value="General Prompts" />
            <input id="newSourceQuery" placeholder="X recent-search query" />
            <label class="toggle"><input id="newSourceEnabled" type="checkbox" checked /> enabled</label>
        </div>
        <div class="panel-actions">
            <button type="button" onclick="addSource()">Add source</button>
        </div>
    </div>
    """


def render_toolbar(filters: dict[str, Any], sources: list[dict[str, Any]], types: list[str]) -> str:
    sources_csv = ",".join(filters.get("sources", []))
    types_csv = ",".join(filters.get("types", []))
    tags_csv = ",".join(filters.get("tags", []))
    opened = "open" if any(value for key, value in filters.items() if key not in {"q", "semantic"}) else ""
    return f"""
    <form method="get" action="/">
        <div class="quick-filter"><span class="prompt">SEARCH&gt;</span><input id="quickSearch" name="q" value="{html.escape(filters.get('q', ''), quote=True)}" placeholder="type to filter; Enter runs server search"><span class="prompt">SEM&gt;</span><input name="semantic" value="{html.escape(filters.get('semantic', ''), quote=True)}" placeholder="semantic query"><button class="tinybtn" type="submit">[enter]</button><a class="tinybtn" href="/">[clear]</a></div>
        <details {opened}><summary>[+] advanced filters / combined AND</summary><div class="advanced">
            <input name="types" value="{html.escape(types_csv, quote=True)}" placeholder="types">
            <input name="sources" value="{html.escape(sources_csv, quote=True)}" placeholder="sources">
            <input name="tags" value="{html.escape(tags_csv, quote=True)}" placeholder="tags">
            <input name="min_rating" type="number" min="0" max="100" value="{filters.get('min_rating', '') if filters.get('min_rating') is not None else ''}" placeholder="rating min">
            <input name="max_rating" type="number" min="0" max="100" value="{filters.get('max_rating', '') if filters.get('max_rating') is not None else ''}" placeholder="rating max">
            <input name="min_complexity" type="number" min="0" max="100" value="{filters.get('min_complexity', '') if filters.get('min_complexity') is not None else ''}" placeholder="complexity min">
            <input name="max_complexity" type="number" min="0" max="100" value="{filters.get('max_complexity', '') if filters.get('max_complexity') is not None else ''}" placeholder="complexity max">
            <input name="date_from" type="date" value="{html.escape(filters.get('date_from', ''), quote=True)}">
            <input name="date_to" type="date" value="{html.escape(filters.get('date_to', ''), quote=True)}">
            <button type="submit">apply filters</button>
        </div></details>
    </form>
    """


async def summarize_selection(records: list[dict[str, Any]], action: str) -> dict[str, Any]:
    cfg: Config = app.state.config
    if provider_is_configured() and records:
        text = render_prompt_merger(records)
        try:
            parsed, usage, _ = await call_provider(action, text, records)
            cost = estimated_cost(usage)
            await record_usage(app.state.provider_state.get("name", cfg.provider_name), app.state.provider_state.get("model", cfg.provider_model), action, usage, cost)
            return parsed
        except Exception as exc:
            logging.warning("AI action %s fallback: %s", action, exc)
    merged = render_prompt_merger(records)
    return {
        "title": f"{action.title()} canvas",
        "purpose": "Сборка выбранных артефактов в единый рабочий пакет.",
        "summary": make_summary_from_text(merged),
        "merged_prompt": merged,
        "instructions": "Скопируй этот пакет в нужный агентский workflow и используй как источник контекста.",
        "killer_feature": "Единый пакет из живых артефактов с прозрачной структурой.",
        "should_disappear": "Разрозненные фрагменты без цели и без связи.",
        "tags": sorted({tag for record in records for tag in record.get("tags", [])})[:12],
    }


async def build_canvas_archive(records: list[dict[str, Any]], result: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    manifest = {
        "created_at": iso_now(),
        "count": len(records),
        "title": result.get("title", "Canvas"),
        "purpose": result.get("purpose", ""),
        "summary": result.get("summary", ""),
        "tags": result.get("tags", []),
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("canvas.md", build_canvas_markdown(records, result))
        archive.writestr("canvas.json", json.dumps(result, ensure_ascii=False, indent=2))
        archive.writestr("selected.json", json.dumps(records, ensure_ascii=False, indent=2))
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(
            "instructions.md",
            "# How to use\n\n"
            "1. Open the canvas bundle.\n"
            "2. Read the summary and purpose.\n"
            "3. Use the merged prompt as a single context block.\n"
            "4. Apply the instructions to the selected workflow.\n",
        )
    return buf.getvalue()


def build_canvas_markdown(records: list[dict[str, Any]], result: dict[str, Any]) -> str:
    sections = [
        f"# {result.get('title', 'Canvas')}",
        f"## Purpose\n{result.get('purpose', '')}",
        f"## Summary\n{result.get('summary', '')}",
        f"## Killer feature\n{result.get('killer_feature', '')}",
        f"## Should disappear\n{result.get('should_disappear', '')}",
        f"## Instructions\n{result.get('instructions', '')}",
        "## Selected artifacts",
    ]
    for idx, record in enumerate(records, start=1):
        sections.append(
            f"### {idx}. {record.get('title', 'Untitled')}\n"
            f"- Type: {record.get('type', '')}\n"
            f"- Source: {record.get('source_name', '')}\n"
            f"- Tags: {', '.join(record.get('tags', []))}\n"
            f"- Summary: {record.get('summary', '')}\n"
            f"- Content:\n\n```\n{record.get('raw', '')}\n```"
        )
    sections.append("## Merged prompt\n")
    sections.append("```\n" + result.get("merged_prompt", "") + "\n```")
    return "\n\n".join(sections)


async def load_selected_records(ids: list[str]) -> list[dict[str, Any]]:
    records = await qdrant_retrieve(ids)
    if records:
        return records
    all_records = await load_recent_artifacts(limit=MAX_RECENT_ARTIFACTS)
    by_id = {item["id"]: item for item in all_records if item.get("id")}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


@app.get("/health")
async def health() -> JSONResponse:
    redis_client: aioredis.Redis = app.state.redis
    await redis_client.ping()
    return JSONResponse({
        "status": "ok",
        "mcp": "configured" if os.getenv("MCP_API_KEY", "").strip() else "disabled",
    })


@app.get("/metrics")
async def metrics() -> JSONResponse:
    telemetry = await get_telemetry_snapshot()
    return JSONResponse(telemetry)


@app.get("/api/state")
async def api_state(username: str = Depends(authenticate)) -> JSONResponse:
    sources = await load_source_catalog()
    telemetry = await get_telemetry_snapshot()
    return JSONResponse({"sources": sources, "telemetry": telemetry, "user": username})


@app.get("/api/artifacts")
async def api_artifacts(
    request: Request,
    username: str = Depends(authenticate),
) -> JSONResponse:
    params = parse_request_filters(request)
    artifacts = await query_artifacts(params, limit=120)
    return JSONResponse({"items": artifacts, "count": len(artifacts), "user": username})


@app.get("/api/sources")
async def api_sources(username: str = Depends(authenticate)) -> JSONResponse:
    return JSONResponse({"items": await load_source_catalog(), "user": username})


@app.post("/api/sources")
async def add_source(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    catalog = await load_source_catalog()
    source_name = str(payload.get("name", "")).strip()
    kind = str(payload.get("kind", "rss")).strip()
    if not source_name:
        raise HTTPException(status_code=400, detail="Name is required")
    source_id = payload.get("id") or slugify(source_name)
    new_source = {
        "id": source_id,
        "name": source_name,
        "kind": kind,
        "artifact_group": str(payload.get("artifact_group", "Other")).strip() or "Other",
        "enabled": bool(payload.get("enabled", True)),
        "recommended_interval_seconds": int(payload.get("recommended_interval_seconds", 3600)),
        "manual_interval_seconds": int(payload["manual_interval_seconds"]) if payload.get("manual_interval_seconds") else None,
        "cadence_reason": str(payload.get("cadence_reason", "")),
        "paused": bool(payload.get("paused", False)),
        "empty_streak": 0,
        "error_streak": 0,
        "last_attempt_at": None,
        "last_success_at": None,
        "next_refresh_at": None,
        "state": "idle",
        "detail": "added",
        "updated_at": iso_now(),
    }
    if kind in {"rss", "web_page"}:
        url = str(payload.get("url", "")).strip()
        if not url:
            raise HTTPException(status_code=400, detail=f"URL is required for {kind}")
        new_source["url"] = url
    elif kind == "github_atom":
        repo = str(payload.get("repo", "")).strip()
        if not repo:
            raise HTTPException(status_code=400, detail="Repo is required for github_atom")
        new_source["repo"] = repo
        new_source["branch"] = str(payload.get("branch", "main")).strip() or "main"
    elif kind == "x_search":
        query = str(payload.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="Query is required for x_search")
        new_source["query"] = query
    elif kind == "workspace":
        root = str(payload.get("url", "")).strip()
        if not root:
            raise HTTPException(status_code=400, detail="Workspace root is required")
        new_source["root"] = root
    else:
        raise HTTPException(status_code=400, detail="Unsupported source kind")

    catalog = [item for item in catalog if item["id"] != source_id]
    await app.state.redis.srem(SOURCE_DELETED_KEY, source_id)
    catalog.insert(0, new_source)
    await save_source_catalog(catalog)
    return JSONResponse({"ok": True, "item": new_source, "user": username})


@app.patch("/api/sources/{source_id}")
async def update_source(source_id: str, payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    catalog = await load_source_catalog()
    updated_source = None
    for idx, source in enumerate(catalog):
        if source["id"] != source_id:
            continue
        source.update({k: v for k, v in payload.items() if v is not None})
        if "manual_interval_seconds" in payload and payload["manual_interval_seconds"] not in ("", None):
            source["manual_interval_seconds"] = int(payload["manual_interval_seconds"])
        if "enabled" in payload:
            source["enabled"] = bool(payload["enabled"])
        if "paused" in payload:
            source["paused"] = bool(payload["paused"])
        source["updated_at"] = iso_now()
        catalog[idx] = source
        updated_source = source
        break
    if not updated_source:
        raise HTTPException(status_code=404, detail="Source not found")
    await save_source_catalog(catalog)
    return JSONResponse({"ok": True, "item": updated_source, "user": username})


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: str, username: str = Depends(authenticate)) -> JSONResponse:
    catalog = await load_source_catalog()
    catalog = [item for item in catalog if item["id"] != source_id]
    await app.state.redis.sadd(SOURCE_DELETED_KEY, source_id)
    await save_source_catalog(catalog)
    return JSONResponse({"ok": True, "user": username})


@app.get("/api/provider/models")
async def api_provider_models(username: str = Depends(authenticate)) -> JSONResponse:
    models = await fetch_provider_models()
    provider = app.state.provider_state
    provider["loaded_models"] = models
    await save_provider_state(provider)
    return JSONResponse({"items": models, "user": username})


@app.post("/api/provider/select")
async def api_provider_select(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    provider = app.state.provider_state
    for key in [
        "name",
        "kind",
        "base_url",
        "api_key",
        "model",
        "monthly_token_limit",
        "monthly_budget_usd",
        "input_price_per_1m",
        "output_price_per_1m",
    ]:
        if key in payload and payload[key] not in (None, ""):
            provider[key] = payload[key]
    provider["monthly_token_limit"] = int(provider.get("monthly_token_limit", 500000))
    provider["monthly_budget_usd"] = float(provider.get("monthly_budget_usd", 25.0))
    provider["input_price_per_1m"] = float(provider.get("input_price_per_1m", 2.0))
    provider["output_price_per_1m"] = float(provider.get("output_price_per_1m", 8.0))
    provider["updated_at"] = iso_now()
    await save_provider_state(provider)
    return JSONResponse({"ok": True, "item": provider, "user": username})


@app.get("/api/provider/status")
async def api_provider_status(username: str = Depends(authenticate)) -> JSONResponse:
    provider = app.state.provider_state
    return JSONResponse({"item": provider, "user": username})


@app.post("/api/ai/action")
async def api_ai_action(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    action = str(payload.get("action", "summary")).strip()
    ids = [str(item) for item in payload.get("ids", []) if item]
    text = str(payload.get("text", "") or "")
    records = await load_selected_records(ids) if ids else []
    if not text:
        text = render_prompt_merger(records) if records else "No content"
    result = await summarize_selection(records or [{"title": "Manual input", "type": "Prompt", "source_name": "manual", "tags": [], "summary": "", "raw": text}], action)
    if text and not records:
        result.setdefault("summary", make_summary_from_text(text))
    return JSONResponse({"ok": True, "result": result, "user": username})


@app.post("/api/export")
async def api_export(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> Response:
    fmt = str(payload.get("format", "md")).strip().lower()
    ids = [str(item) for item in payload.get("ids", []) if item]
    records = await load_selected_records(ids)
    if fmt == "json":
        content = json.dumps(
            {
                "exported_at": iso_now(),
                "count": len(records),
                "items": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="promptops-export.json"'},
        )
    markdown = build_canvas_markdown(records, await summarize_selection(records, "canvas"))
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="promptops-export.md"'},
    )


@app.post("/api/canvas/preview")
async def api_canvas_preview(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    ids = [str(item) for item in payload.get("ids", []) if item]
    records = await load_selected_records(ids)
    result = await summarize_selection(records, "canvas")
    return JSONResponse({"ok": True, "canvas": result, "records": records, "user": username})


@app.post("/api/canvas/archive")
async def api_canvas_archive(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> Response:
    ids = [str(item) for item in payload.get("ids", []) if item]
    records = await load_selected_records(ids)
    result = await summarize_selection(records, "canvas")
    archive = await build_canvas_archive(records, result)
    safe_name = slugify(result.get("title", "canvas"))
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


def public_prompt_item(prompt: dict[str, Any]) -> dict[str, Any]:
    learning = prompt_learning_complexity(prompt.get("learning_complexity"), {"level": "средняя", "score": 50, "reason": ""})
    token_estimate = prompt.get("token_estimate") if isinstance(prompt.get("token_estimate"), dict) else {}
    return {
        "serial": prompt.get("serial", "P-000000"),
        "title": prompt.get("title", "Untitled prompt"),
        "prompt_body": prompt.get("prompt_body", ""),
        "description": prompt.get("description", ""),
        "how_it_works": prompt.get("how_it_works", ""),
        "why_it_works": prompt.get("why_it_works", ""),
        "structure": prompt_text_list(prompt.get("structure"), []),
        "coverage": prompt_text_list(prompt.get("coverage"), []),
        "expected_output": str(prompt.get("expected_output", ""))[:500],
        "learning_complexity": learning,
        "token_estimate": {
            key: {
                "min": nonnegative_int((token_estimate.get(key) or {}).get("min", 0)),
                "max": nonnegative_int((token_estimate.get(key) or {}).get("max", 0)),
            }
            for key in ["input", "output", "total"]
        } | {"method": str(token_estimate.get("method", "heuristic-v1"))[:32]},
        "tags": prompt.get("tags", []),
        "complexity": int(prompt.get("complexity", 0)),
        "literacy_score": int(prompt.get("literacy_score", 0)),
        "special_marks": prompt.get("special_marks", []),
        "remarks": prompt.get("remarks", []),
        "prompt_type": prompt.get("prompt_type", "Prompt"),
    }


def compact_prompt_item(prompt: dict[str, Any]) -> dict[str, Any]:
    return {
        "serial": prompt.get("serial", "P-000000"),
        "title": prompt.get("title", "Untitled prompt"),
        "tags": prompt.get("tags", []),
        "complexity": int(prompt.get("complexity", 0)),
        "literacy_score": int(prompt.get("literacy_score", 0)),
        "prompt_type": prompt.get("prompt_type", "Prompt"),
        "token_estimate": prompt.get("token_estimate", {}),
    }

def split_filter_values(raw: str) -> set[str]:
    return {value.strip().lower() for value in raw.split(",") if value.strip()}


def filter_prompt_items(
    prompts: list[dict[str, Any]],
    query: str = "",
    tags: set[str] | None = None,
    prompt_types: set[str] | None = None,
    min_complexity: int = 0,
    max_complexity: int = 100,
    min_literacy: int = 0,
    max_literacy: int = 100,
    complexity_buckets: set[str] | None = None,
    literacy_buckets: set[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_query = normalize_ws(query).lower()
    wanted_tags = tags or set()
    wanted_types = prompt_types or set()
    wanted_complexity = complexity_buckets or set()
    wanted_literacy = literacy_buckets or set()
    filtered = []
    for prompt in prompts:
        complexity = int(prompt.get("complexity", 0))
        literacy = int(prompt.get("literacy_score", 0))
        if not min_complexity <= complexity <= max_complexity or not min_literacy <= literacy <= max_literacy:
            continue
        complexity_bucket = "0-39" if complexity < 40 else "40-59" if complexity < 60 else "60-79" if complexity < 80 else "80-100"
        literacy_bucket = "0-49" if literacy < 50 else "50-69" if literacy < 70 else "70-84" if literacy < 85 else "85-100"
        if wanted_complexity and complexity_bucket not in wanted_complexity:
            continue
        if wanted_literacy and literacy_bucket not in wanted_literacy:
            continue
        item_tags = {str(value).lower() for value in prompt.get("tags", [])}
        if wanted_tags and not item_tags.intersection(wanted_tags):
            continue
        if wanted_types and str(prompt.get("prompt_type", "Prompt")).lower() not in wanted_types:
            continue
        blob = normalize_ws(" ".join([
            str(prompt.get("title", "")), str(prompt.get("prompt_body", "")),
            str(prompt.get("description", "")), " ".join(item_tags),
        ])).lower()
        if normalized_query and normalized_query not in blob:
            continue
        filtered.append(prompt)
    return filtered


def sort_prompt_items(prompts: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    sorters = {
        "oldest": (lambda item: float(item.get("published_ts", 0)), False),
        "serial": (lambda item: str(item.get("serial", "")), False),
        "title": (lambda item: str(item.get("title", "")).lower(), False),
        "complexity": (lambda item: int(item.get("complexity", 0)), True),
        "literacy": (lambda item: int(item.get("literacy_score", 0)), True),
        "newest": (lambda item: float(item.get("published_ts", 0)), True),
    }
    key, reverse = sorters.get(sort, sorters["newest"])
    return sorted(prompts, key=key, reverse=reverse)


def prompt_facets(prompts: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = defaultdict(int)
    tag_counts: dict[str, int] = defaultdict(int)
    complexity = {"0-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    literacy = {"0-49": 0, "50-69": 0, "70-84": 0, "85-100": 0}
    for prompt in prompts:
        type_counts[str(prompt.get("prompt_type", "Prompt"))] += 1
        for tag in prompt.get("tags", []):
            tag_counts[str(tag)] += 1
        cmp_score = int(prompt.get("complexity", 0))
        lit_score = int(prompt.get("literacy_score", 0))
        complexity["0-39" if cmp_score < 40 else "40-59" if cmp_score < 60 else "60-79" if cmp_score < 80 else "80-100"] += 1
        literacy["0-49" if lit_score < 50 else "50-69" if lit_score < 70 else "70-84" if lit_score < 85 else "85-100"] += 1
    return {
        "types": dict(sorted(type_counts.items())),
        "tags": dict(sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:60]),
        "complexity": complexity,
        "literacy": literacy,
    }


async def load_prompt_by_serial(serial: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"P-\d{6}", serial):
        return None
    redis_client: aioredis.Redis = app.state.redis
    prompt_id = await redis_client.hget(PROMPTS_SERIAL_INDEX_KEY, serial)
    if prompt_id:
        raw = await redis_client.hget(PROMPTS_HASH_KEY, prompt_id)
        if raw:
            return json.loads(raw)
        await redis_client.hdel(PROMPTS_SERIAL_INDEX_KEY, serial)
    for prompt in await load_prompt_catalog(MAX_PROMPT_CATALOG):
        if prompt.get("serial") == serial:
            await redis_client.hset(PROMPTS_SERIAL_INDEX_KEY, serial, str(prompt["id"]))
            return prompt
    return None


async def resolve_prompt_serials(serials: list[str], limit: int) -> list[dict[str, Any]]:
    clean = list(dict.fromkeys(str(serial).strip() for serial in serials if str(serial).strip()))
    if len(clean) > limit:
        raise HTTPException(status_code=400, detail=f"Maximum {limit} prompts per request")
    prompts = []
    for serial in clean:
        prompt = await load_prompt_by_serial(serial)
        if prompt:
            prompts.append(prompt)
    return prompts


def markdown_fence(body: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", body)), default=2)
    return "`" * max(3, longest + 1)


def build_prompt_register_export(registers: list[dict[str, Any]], format_name: str) -> tuple[str, str]:
    exported_at = iso_now()
    safe_registers = []
    for register in registers:
        safe_registers.append({
            "id": str(register.get("id", "register"))[:32],
            "name": str(register.get("name", "REGISTER"))[:80],
            "color": str(register.get("color", "#57ff8f"))[:16],
            "prompts": [public_prompt_item(prompt) for prompt in register.get("prompts", [])],
        })
    if format_name == "json":
        return json.dumps({"version": 1, "exported_at": exported_at, "registers": safe_registers}, ensure_ascii=False, indent=2), "application/json"
    sections = [f"# Prompt Register Export\n\nExported: {exported_at}"]
    for register in safe_registers:
        sections.append(f"## {register['name']} [{register['color']}]")
        for prompt in register["prompts"]:
            fence = markdown_fence(prompt["prompt_body"])
            tags = " ".join(f"#{tag}" for tag in prompt["tags"])
            sections.append(
                f"### {prompt['serial']} — {prompt['title']}\n\n"
                f"{prompt['description']}\n\n"
                f"CMP {prompt['complexity']} | LIT {prompt['literacy_score']} | {prompt['prompt_type']}\n\n"
                f"Structure: {' → '.join(prompt['structure']) or '-'}\n\n"
                f"Coverage: {', '.join(prompt['coverage']) or '-'}\n\n"
                f"Learning: {prompt['learning_complexity'].get('level', '-')} — {prompt['learning_complexity'].get('reason', '-')}\n\n"
                f"Tokens: IN ~{prompt['token_estimate'].get('input', {}).get('min', 0)}-{prompt['token_estimate'].get('input', {}).get('max', 0)} | OUT ~{prompt['token_estimate'].get('output', {}).get('min', 0)}-{prompt['token_estimate'].get('output', {}).get('max', 0)}\n\n"
                f"{tags}\n\n{fence}text\n{prompt['prompt_body']}\n{fence}\n\n"
                f"Marks: {', '.join(prompt['special_marks']) or '-'}\n\n"
                f"Remarks: {'; '.join(prompt['remarks']) or '-'}"
            )
    return "\n\n".join(sections) + "\n", "text/markdown"


@app.get("/api/prompts")
async def api_prompts(
    q: str = "", tag: str = "", tags: str = "", types: str = "",
    min_complexity: int = 0, max_complexity: int = 100,
    min_literacy: int = 0, max_literacy: int = 100,
    complexity_buckets: str = "", literacy_buckets: str = "",
    sort: str = "newest", offset: int = 0, limit: int = 200, view: str = "full",
    _auth: None = Depends(require_daily_pass_or_admin),
) -> JSONResponse:
    prompts = await load_prompt_catalog(MAX_PROMPT_CATALOG)
    wanted_tags = split_filter_values(tags)
    wanted_tags.update(split_filter_values(tag))
    filtered = filter_prompt_items(
        prompts, q, wanted_tags, split_filter_values(types),
        int(clamp(min_complexity, 0, 100)), int(clamp(max_complexity, 0, 100)),
        int(clamp(min_literacy, 0, 100)), int(clamp(max_literacy, 0, 100)),
        split_filter_values(complexity_buckets), split_filter_values(literacy_buckets),
    )
    ordered = sort_prompt_items(filtered, sort)
    facet_base = filter_prompt_items(
        prompts, q, set(), set(),
        int(clamp(min_complexity, 0, 100)), int(clamp(max_complexity, 0, 100)),
        int(clamp(min_literacy, 0, 100)), int(clamp(max_literacy, 0, 100)),
        split_filter_values(complexity_buckets), split_filter_values(literacy_buckets),
    )
    safe_offset = max(0, offset)
    safe_limit = max(1, min(limit, 500 if view != "compact" else 200))
    page = ordered[safe_offset:safe_offset + safe_limit]
    if view == "compact":
        return JSONResponse({
            "items": [compact_prompt_item(prompt) for prompt in page],
            "count": len(page), "total": len(ordered), "offset": safe_offset,
            "limit": safe_limit, "facets": prompt_facets(facet_base),
        })
    return JSONResponse({"items": [public_prompt_item(prompt) for prompt in page], "count": len(page)})


@app.get("/api/prompts/{serial}")
async def api_prompt_detail(serial: str, _auth: None = Depends(require_daily_pass_or_admin)) -> JSONResponse:
    prompt = await load_prompt_by_serial(serial)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return JSONResponse(public_prompt_item(prompt))


@app.post("/api/prompts/export")
async def api_prompt_export(payload: dict[str, Any] = Body(...), _auth: None = Depends(require_daily_pass_or_admin)) -> Response:
    format_name = str(payload.get("format", "md")).lower()
    if format_name not in {"md", "json"}:
        raise HTTPException(status_code=400, detail="Format must be md or json")
    raw_registers = payload.get("registers", [])
    if not isinstance(raw_registers, list) or not raw_registers:
        raise HTTPException(status_code=400, detail="At least one register is required")
    unique_serials = list(dict.fromkeys(
        str(serial) for register in raw_registers for serial in register.get("serials", [])
    ))
    if len(unique_serials) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 prompts per export")
    resolved = {prompt["serial"]: prompt for prompt in await resolve_prompt_serials(unique_serials, 200)}
    registers = []
    for register in raw_registers[:10]:
        registers.append({
            "id": register.get("id"), "name": register.get("name"), "color": register.get("color"),
            "prompts": [resolved[str(serial)] for serial in register.get("serials", []) if str(serial) in resolved],
        })
    content, media_type = build_prompt_register_export(registers, format_name)
    extension = "json" if format_name == "json" else "md"
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="prompt-register.{extension}"'})


@app.post("/api/prompts/analyze")
async def api_prompt_analyze(payload: dict[str, Any] = Body(...), username: str = Depends(authenticate)) -> JSONResponse:
    serials = [str(value) for value in payload.get("serials", [])]
    prompts = await resolve_prompt_serials(serials, 20)
    if not prompts:
        raise HTTPException(status_code=400, detail="Select at least one existing prompt")
    if not provider_is_configured():
        raise HTTPException(status_code=503, detail="AI provider is not configured")
    register_name = str(payload.get("register_name", "REGISTER"))[:80]
    chunks = []
    for prompt in prompts:
        chunks.append(
            f"[{prompt['serial']}] {prompt['title']}\n"
            f"Description: {prompt['description']}\n"
            f"Tags: {', '.join(prompt.get('tags', []))}\n"
            f"Prompt: {prompt.get('prompt_body', '')[:3000]}"
        )
    analysis_input = f"Register: {register_name}\n\n" + "\n\n---\n\n".join(chunks)
    try:
        result, usage, _ = await call_provider("prompt_register_analysis", analysis_input)
    except Exception as exc:
        logging.error("Prompt register analysis failed: %s", exc)
        raise HTTPException(status_code=502, detail="AI analysis failed") from exc
    normalized = normalize_usage(usage, analysis_input, json.dumps(result, ensure_ascii=False))
    cost = estimated_cost(normalized)
    provider = app.state.provider_state
    await record_usage(provider.get("name", "OpenAI-compatible"), provider.get("model", "unknown"), "prompt_register_analysis", normalized, cost)
    return JSONResponse({
        "ok": True, "result": result, "usage": normalized, "estimated_cost": round(cost, 6),
        "provider": provider.get("name"), "model": provider.get("model"),
        "prompt_count": len(prompts), "user": username,
    })


def mcp_tags_match(prompt: dict[str, Any], tags: list[str]) -> bool:
    wanted = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    available = {str(tag).strip().lower() for tag in prompt.get("tags", [])}
    return not wanted or wanted.issubset(available)


def mcp_types_match(prompt: dict[str, Any], prompt_types: list[str]) -> bool:
    wanted = {str(value).strip().lower() for value in prompt_types if str(value).strip()}
    return not wanted or str(prompt.get("prompt_type", "Prompt")).lower() in wanted


async def mcp_list_prompts_backend(
    query: str,
    tags: list[str],
    prompt_types: list[str],
    min_complexity: int,
    max_complexity: int,
    sort: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    prompts = await load_prompt_catalog(MAX_PROMPT_CATALOG)
    filtered = filter_prompt_items(
        prompts,
        query=query,
        min_complexity=min_complexity,
        max_complexity=max_complexity,
    )
    filtered = [
        prompt for prompt in filtered
        if mcp_tags_match(prompt, tags) and mcp_types_match(prompt, prompt_types)
    ]
    sort_name = {"complexity_desc": "complexity", "literacy_desc": "literacy"}.get(sort, sort)
    ordered = sort_prompt_items(filtered, sort_name)
    page = ordered[offset:offset + limit]
    return {
        "items": [compact_prompt_item(prompt) for prompt in page],
        "count": len(page),
        "total": len(ordered),
        "offset": offset,
        "limit": limit,
    }


async def mcp_get_prompt_backend(serial: str) -> dict[str, Any]:
    prompt = await load_prompt_by_serial(serial)
    if not prompt:
        raise ValueError(f"Prompt {serial} was not found")
    return public_prompt_item(prompt)


async def mcp_semantic_search_backend(
    query: str,
    tags: list[str],
    prompt_types: list[str],
    limit: int,
) -> dict[str, Any]:
    candidates = await qdrant_search(query, None, min(MAX_PROMPT_CATALOG, max(limit * 8, 80)))
    candidates.sort(key=lambda item: float(item.get("search_score") or 0), reverse=True)
    results = []
    for candidate in candidates:
        prompt_id = str(candidate.get("id", ""))
        raw = await app.state.redis.hget(PROMPTS_HASH_KEY, prompt_id)
        if not raw:
            continue
        prompt = json.loads(raw)
        if not mcp_tags_match(prompt, tags) or not mcp_types_match(prompt, prompt_types):
            continue
        item = compact_prompt_item(prompt)
        item["semantic_score"] = round(float(candidate.get("search_score") or 0), 6)
        results.append(item)
        if len(results) >= limit:
            break
    return {
        "query": query,
        "items": results,
        "count": len(results),
        "vector_status": getattr(app.state, "vector_status", "unknown"),
    }


async def mcp_export_prompts_backend(
    serials: list[str],
    format_name: str,
    register_name: str,
) -> dict[str, Any]:
    prompts = await resolve_prompt_serials(serials, 50)
    if not prompts:
        raise ValueError("No existing prompt serials were supplied")
    content, media_type = build_prompt_register_export(
        [{
            "id": "mcp",
            "name": register_name,
            "color": "#00ff66",
            "prompts": prompts,
        }],
        format_name,
    )
    return {
        "format": format_name,
        "media_type": media_type,
        "count": len(prompts),
        "content": content,
    }


async def mcp_catalog_stats_backend() -> dict[str, Any]:
    prompts = await load_prompt_catalog(MAX_PROMPT_CATALOG)
    return {
        "total": len(prompts),
        "facets": prompt_facets(prompts),
        "vector_status": getattr(app.state, "vector_status", "unknown"),
        "last_sync_at": await app.state.redis.get(LAST_SYNC_KEY),
    }


@app.get("/prompts", response_class=HTMLResponse)
async def prompt_catalog_page() -> HTMLResponse:
    return HTMLResponse((Path(__file__).resolve().parent / "prompts" / "index.html").read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request, username: str = Depends(authenticate)) -> HTMLResponse:
    filters = parse_request_filters(request)
    artifacts = await query_artifacts(filters, limit=MAX_PROMPT_ITEMS)
    sources = await load_source_catalog()
    provider = app.state.provider_state
    snapshot = await get_telemetry_snapshot()
    alerts = await load_alerts(16)
    models = provider.get("loaded_models", [])

    artifact_rows = "".join(render_artifact_card(item) for item in artifacts) or '<div class="empty">NO_ARTIFACTS // adjust filters or wait for sync</div>'
    healthy_sources = [source for source in sources if source.get("state") != "error"]
    error_sources = [source for source in sources if source.get("state") == "error"]
    source_rows = render_source_groups(healthy_sources)
    if error_sources:
        source_errors = f'<div class="source-errors" id="sourceErrors"><details class="error-log"><summary>ERROR_LOG [{len(error_sources)}]</summary>{render_source_groups(error_sources)}</details></div>'
    else:
        source_errors = '<div class="source-errors" id="sourceErrors"><details class="error-log"><summary>ERROR_LOG [0]</summary></details></div>'
    alert_rows = "".join(
        f'<div class="signal-row"><strong>{html.escape(item.get("type", "signal"))}</strong><span>{html.escape(item.get("summary", ""))}</span></div>'
        for item in alerts[:8]
    ) or '<div class="signal-row"><strong>signals</strong><span>quiet</span></div>'
    artifact_json = json.dumps(artifacts, ensure_ascii=False, separators=(",", ":")).replace("<", "\u003c")
    template = (Path(__file__).resolve().parent / "dashboard" / "index.html").read_text(encoding="utf-8")
    replacements = {
        "__USER__": html.escape(username),
        "__SESSION__": html.escape(app.state.session_id[:12]),
        "__PROVIDER__": html.escape(str(provider.get("name", "-"))),
        "__MODEL__": html.escape(str(provider.get("model", "-"))),
        "__TOKENS_TOTAL__": str(snapshot["tokens_total"]),
        "__BUDGET_LEFT__": str(snapshot["remaining_usd"]),
        "__VECTOR_STATUS__": html.escape(snapshot["vector_status"]),
        "__LAST_SYNC__": html.escape(format_relative(snapshot["last_sync"])),
        "__SOURCE_COUNT__": str(len(sources)),
        "__ARTIFACT_COUNT__": str(len(artifacts)),
        "__INDEXED_COUNT__": str(snapshot["indexed_artifacts"]),
        "__SOURCE_ROWS__": source_rows,
        "__SOURCE_ERRORS__": source_errors,
        "__ALERT_ROWS__": alert_rows,
        "__FILTER_BAR__": render_toolbar(filters, sources, sorted({item.get("type", "") for item in artifacts if item.get("type")})),
        "__ARTIFACT_ROWS__": artifact_rows,
        "__ARTIFACT_JSON__": artifact_json,
        "__SOURCE_FORM__": render_source_form(),
        "__PROVIDER_PANEL__": render_provider_panel(provider, models),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return HTMLResponse(content=template)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = load_config()
    app.state.config = cfg
    app.state.redis = aioredis.Redis(host=cfg.redis_host, port=cfg.redis_port, decode_responses=True)
    app.state.http_client = AsyncClient()
    app.state.background_tasks = []
    app.state.session_id = secrets.token_hex(8)
    app.state.vector_ready = False
    app.state.vector_status = "initializing"
    app.state.source_status = {}
    app.state.provider_state = default_provider_state(cfg)
    app.state.source_catalog = default_source_catalog(cfg)
    app.state.telethon_client = None
    await app.state.redis.ping()

    try:
        if not await app.state.redis.exists(SOURCE_CATALOG_KEY):
            await save_source_catalog(app.state.source_catalog)
        app.state.provider_state = await load_provider_state()
        app.state.source_catalog = await load_source_catalog()
    except Exception as exc:
        logging.warning("Seed state failed: %s", exc)

    if cfg.qdrant_url:
        try:
            app.state.qdrant = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=10.0)
            await ensure_qdrant_collection()
        except Exception as exc:
            logging.warning("Qdrant unavailable: %s", exc)
            app.state.qdrant = None
            app.state.vector_ready = False
            app.state.vector_status = "offline"
    else:
        app.state.qdrant = None
        app.state.vector_status = "disabled"

    for source in app.state.source_catalog:
        app.state.source_status[source["id"]] = {
            "state": source.get("state", "idle"),
            "detail": source.get("detail", "waiting"),
            "updated_at": source.get("updated_at", iso_now()),
        }

    configure_publishing(app, load_selected_records, record_usage)
    configure_daily_pass(app)
    configure_mcp(PromptOpsMCPBackend(
        list_prompts=mcp_list_prompts_backend,
        get_prompt=mcp_get_prompt_backend,
        semantic_search=mcp_semantic_search_backend,
        export_prompts=mcp_export_prompts_backend,
        catalog_stats=mcp_catalog_stats_backend,
    ))
    app.state.background_tasks.append(asyncio.create_task(reindex_recent_artifacts()))
    app.state.background_tasks.append(asyncio.create_task(backfill_prompt_catalog()))
    app.state.background_tasks.append(asyncio.create_task(publishing_scheduler_loop()))
    app.state.background_tasks.append(asyncio.create_task(poll_sources_loop(app.state.http_client, app.state.redis)))
    app.state.background_tasks.append(asyncio.create_task(vector_watchdog_loop()))
    if cfg.has_telethon:
        app.state.background_tasks.append(asyncio.create_task(start_telethon_userbot(app.state.http_client, app.state.redis)))

    async with mcp.session_manager.run():
        try:
            yield
        finally:
            for task in app.state.background_tasks:
                task.cancel()
            for task in app.state.background_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.http_client.aclose()
            await app.state.redis.aclose()


app.mount("/", mcp_http_app)
app.router.lifespan_context = lifespan


def main() -> None:
    uvicorn.run("prompt_ops_app:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
