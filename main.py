"""
Ofshore Commander Bot (@ogarniacz_ofshore_bot)
Unified control panel for the entire ofshore.dev ecosystem.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from aiohttp import web
from anthropic import AsyncAnthropic
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("commander")

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "8149345223"))
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
COOLIFY_URL = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
COOLIFY_TOKEN = os.environ["COOLIFY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
N8N_URL = os.environ.get("N8N_URL", "https://n8n.ofshore.dev")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8080"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ── App UUID map ──────────────────────────────────────────────────────────────
APPS: dict[str, str] = {
    "ai-control-center": "hokscgg48sowg44wwc044gk8",
    "autoheal":          "vcgk0g4sc4sck0kkc8k080gk",
    "brain-router":      "e88g00owoo84k8gw4co4cskw",
    "manus-brain":       "kssk4o48sgosgwwck8s8ws80",
    "openmanus":         "wcksgw80gsg4gg0w4w8sswcg",
    "sentinel":          "rs488c4ccg48w48gocgog8sg",
    "watchdog":          "g8csck0kw8c0sc0cosg0cw84",
    "guardian":          "qook8w0sw4o404swcoookg00",
    "claude-mcp":        "qkc0gocw4oogw4o888w44w4w",
    "integration-hub":   "s44sck0k0os0k4w0www00cg4",
    "ollama":            "rok4gc0o80wosk8cgks4k0sg",
    "english-teacher":   "d0800oks0g4gws0kw04ck00s",
    "commander":         "SELF",
}

APP_URLS: dict[str, str] = {
    "autoheal":          "https://autoheal.ofshore.dev",
    "watchdog":          "https://watchdog.ofshore.dev",
    "sentinel":          "https://sentinel.ofshore.dev",
    "ollama":            "https://ollama.ofshore.dev",
    "n8n":               "https://n8n.ofshore.dev",
    "coolify":           "https://coolify.ofshore.dev",
}

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# In-memory conversation store: chat_id → list of messages
# Persisted to Supabase async
_conversations: dict[int, list[dict]] = {}

# ── Supabase helpers ──────────────────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

async def sb_get(path: str, params: dict = None) -> list:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, params=params)
        return r.json() if r.is_success else []

async def sb_post(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, json=data)
        result = r.json()
        return result[0] if isinstance(result, list) and result else result

async def sb_upsert(path: str, data: dict, on_conflict: str = "id") -> dict:
    headers = {**SB_HEADERS, "Prefer": f"resolution=merge-duplicates,return=representation"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=data, params={"on_conflict": on_conflict})
        result = r.json()
        return result[0] if isinstance(result, list) and result else result

async def load_conversation(chat_id: int) -> list[dict]:
    """Load conversation history from Supabase."""
    if chat_id in _conversations:
        return _conversations[chat_id]
    rows = await sb_get("commander_threads", {"chat_id": f"eq.{chat_id}", "select": "messages"})
    msgs = rows[0]["messages"] if rows else []
    _conversations[chat_id] = msgs
    return msgs

async def save_conversation(chat_id: int, messages: list[dict]):
    """Save conversation history to Supabase."""
    _conversations[chat_id] = messages
    await sb_upsert(
        "commander_threads",
        {"chat_id": chat_id, "messages": messages, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="chat_id",
    )

async def ensure_tables():
    """Create commander_threads table if not exists (via RPC)."""
    sql = """
    CREATE TABLE IF NOT EXISTS commander_threads (
        id bigserial PRIMARY KEY,
        chat_id bigint UNIQUE NOT NULL,
        messages jsonb NOT NULL DEFAULT '[]',
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    );
    """
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(
            f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
            headers=SB_HEADERS,
            json={"sql": sql},
        )

# ── Coolify helpers ───────────────────────────────────────────────────────────
CF_HEADERS = {"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"}

async def coolify(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await getattr(c, method)(f"{COOLIFY_URL}/api/v1{path}", headers=CF_HEADERS, **kwargs)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "text": r.text}

def resolve_app(name: str) -> Optional[str]:
    name = name.lower().strip()
    if name in APPS:
        return APPS[name]
    for k, v in APPS.items():
        if name in k or k in name:
            return v
    return None

# ── Claude chat ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are Ofshore Commander — the AI brain of Maciej's self-hosted infrastructure at ofshore.dev.
You help Maciej manage, debug, and develop his entire ecosystem directly from Telegram.

Current ecosystem:
- Apps: {', '.join(APPS.keys())}
- Infrastructure: Digital Ocean, Coolify, Supabase (blgdhfcosqjzrutncbbr), Cloudflare
- Stack: React, FastAPI, Python, Node.js, PostgreSQL, Redis

Be concise and practical. Use markdown. When suggesting commands, use Telegram bot commands format (/command).
Respond in the same language as the user (PL/EN)."""

async def ask_claude(chat_id: int, user_message: str) -> str:
    """Send message to Claude with conversation history."""
    messages = await load_conversation(chat_id)
    messages.append({"role": "user", "content": user_message})

    # Keep last 40 messages to stay within context
    if len(messages) > 40:
        messages = messages[-40:]

    try:
        response = await anthropic.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        reply = response.content[0].text
        messages.append({"role": "assistant", "content": reply})
        await save_conversation(chat_id, messages)
        return reply
    except Exception as e:
        log.error(f"Claude error: {e}")
        return f"❌ Claude error: {e}"

# ── Formatting helpers ────────────────────────────────────────────────────────
def escape_md(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))

def status_emoji(status: str) -> str:
    s = str(status).lower()
    if any(x in s for x in ["running", "healthy", "ok", "200"]):
        return "🟢"
    if any(x in s for x in ["starting", "restarting", "building"]):
        return "🟡"
    return "🔴"

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("🔒 Access denied.")
        return

    text = (
        "🛸 *Ofshore Commander* online\\!\n\n"
        "Jeden bot do rządzenia całym ekosystemem\\.\n\n"
        "*Infra:*\n"
        "`/status` — status wszystkich apek\n"
        "`/deploy <app>` — redeploy\n"
        "`/restart <app>` — restart\n"
        "`/logs <app>` — ostatnie logi\n"
        "`/apps` — lista wszystkich aplikacji\n\n"
        "*AI & Agenci:*\n"
        "`/agents` — status agentów w Supabase\n"
        "`/alerts` — ostatnie alerty AutoHeal\n\n"
        "*Dane:*\n"
        "`/db <zapytanie>` — query Supabase\n"
        "`/n8n <id|name>` — trigger n8n workflow\n\n"
        "*Zarządzanie:*\n"
        "`/clear` — wyczyść historię rozmowy\n"
        "`/model <haiku|sonnet>` — zmień model\n\n"
        "💬 *Lub po prostu pisz* — rozmawiam jak Claude\\!"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_apps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    lines = ["📦 *Wszystkie aplikacje:*\n"]
    for name, uuid in APPS.items():
        url = APP_URLS.get(name, "")
        link = f" — [{name}\\.ofshore\\.dev]({url})" if url else ""
        lines.append(f"• `{escape_md(name)}`{link}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    msg = await update.message.reply_text("⏳ Sprawdzam status...")
    results = []

    try:
        data = await coolify("get", "/applications")
        if isinstance(data, list):
            for app in data:
                name = app.get("name", "?")
                status = app.get("status", "unknown")
                emoji = status_emoji(status)
                results.append(f"{emoji} `{escape_md(name)}` — {escape_md(status)}")
        else:
            results.append(f"⚠️ Coolify error: {escape_md(str(data))}")
    except Exception as e:
        results.append(f"❌ Coolify error: {escape_md(str(e))}")

    # Also ping key URLs
    ping_targets = [("autoheal", "https://autoheal.ofshore.dev/health"),
                    ("watchdog",  "https://watchdog.ofshore.dev/health"),
                    ("n8n",       "https://n8n.ofshore.dev/healthz")]
    for name, url in ping_targets:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url)
                results.append(f"{status_emoji(str(r.status_code))} `{name}` HTTP {r.status_code}")
        except Exception:
            results.append(f"🔴 `{escape_md(name)}` — timeout")

    text = "*📊 Status ekosystemu:*\n\n" + "\n".join(results)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/deploy <app_name>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    app_name = ctx.args[0]
    uuid = resolve_app(app_name)
    if not uuid or uuid == "SELF":
        await update.message.reply_text(f"❓ Nie znalazłem apki: `{escape_md(app_name)}`\nUżyj `/apps` by zobaczyć listę\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    msg = await update.message.reply_text(f"🚀 Deploying `{escape_md(app_name)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    result = await coolify("post", f"/applications/{uuid}/start")
    status = result.get("message", result.get("status", str(result)))
    await msg.edit_text(
        f"✅ Deploy `{escape_md(app_name)}` triggered\\!\n`{escape_md(str(status))}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/restart <app_name>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    app_name = ctx.args[0]
    uuid = resolve_app(app_name)
    if not uuid or uuid == "SELF":
        await update.message.reply_text(f"❓ Nie znalazłem apki: `{escape_md(app_name)}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    msg = await update.message.reply_text(f"🔄 Restarting `{escape_md(app_name)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    result = await coolify("post", f"/applications/{uuid}/restart")
    status = result.get("message", result.get("status", str(result)))
    await msg.edit_text(
        f"✅ Restart `{escape_md(app_name)}` triggered\\!\n`{escape_md(str(status))}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/logs <app_name>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    app_name = ctx.args[0]
    uuid = resolve_app(app_name)
    if not uuid or uuid == "SELF":
        await update.message.reply_text(f"❓ Nie znalazłem apki: `{escape_md(app_name)}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    msg = await update.message.reply_text(f"📋 Pobieram logi `{escape_md(app_name)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    result = await coolify("get", f"/applications/{uuid}/logs")

    if isinstance(result, dict) and "logs" in result:
        raw = result["logs"]
    elif isinstance(result, str):
        raw = result
    else:
        raw = str(result)

    # Last 50 lines
    lines = raw.strip().split("\n") if raw else ["(brak logów)"]
    tail = "\n".join(lines[-50:])
    if len(tail) > 3800:
        tail = tail[-3800:]

    await msg.edit_text(
        f"📋 *Logi: {escape_md(app_name)}*\n\n```\n{escape_md(tail)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    msg = await update.message.reply_text("🔔 Pobieram alerty...")
    rows = await sb_get(
        "autoheal_alerts",
        {"select": "created_at,app_name,level,message", "order": "created_at.desc", "limit": "15"},
    )
    if not rows:
        await msg.edit_text("✅ Brak alertów\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = ["*🔔 Ostatnie alerty AutoHeal:*\n"]
    for r in rows:
        level = str(r.get("level", "")).upper()
        emoji = "🔴" if level in ("ERROR", "CRITICAL") else "🟡" if level == "WARNING" else "ℹ️"
        ts = r.get("created_at", "")[:16].replace("T", " ")
        app = escape_md(r.get("app_name", "?"))
        msg_text = escape_md(str(r.get("message", ""))[:100])
        lines.append(f"{emoji} `{ts}` *{app}*\n   {msg_text}\n")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    msg = await update.message.reply_text("🤖 Sprawdzam agentów...")

    rows = await sb_get(
        "autonomous.agent_tasks",
        {"select": "agent_id,status,task_type,updated_at", "order": "updated_at.desc", "limit": "20"},
    )

    if not rows:
        # Try alternate table
        rows = await sb_get(
            "app_health_snapshots",
            {"select": "app_name,status,checked_at", "order": "checked_at.desc", "limit": "10"},
        )
        if rows:
            lines = ["*🤖 Health Snapshots:*\n"]
            for r in rows:
                emoji = status_emoji(r.get("status", ""))
                app = escape_md(r.get("app_name", "?"))
                ts = str(r.get("checked_at", ""))[:16].replace("T", " ")
                lines.append(f"{emoji} `{app}` — `{ts}`")
            await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
            return

        await msg.edit_text("ℹ️ Brak danych o agentach\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = ["*🤖 Aktywne zadania agentów:*\n"]
    for r in rows:
        status = r.get("status", "?")
        emoji = "🟢" if status == "completed" else "🔄" if status == "running" else "⏳"
        agent = escape_md(r.get("agent_id", "?"))
        task = escape_md(r.get("task_type", "?"))
        ts = str(r.get("updated_at", ""))[:16].replace("T", " ")
        lines.append(f"{emoji} `{agent}` — {task} `{ts}`")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/db <table>` lub `/db <table> select=col1,col2 limit=10`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    table = ctx.args[0]
    params = {}
    for arg in ctx.args[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            params[k] = v

    params.setdefault("limit", "10")
    msg = await update.message.reply_text(f"🗄️ Query: `{escape_md(table)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    rows = await sb_get(table, params)

    if not rows:
        await msg.edit_text(f"ℹ️ Brak wyników z `{escape_md(table)}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    result = json.dumps(rows, indent=2, default=str)
    if len(result) > 3500:
        result = result[:3500] + "\n... (truncated)"

    await msg.edit_text(
        f"🗄️ *{escape_md(table)}* \\({len(rows)} rows\\)\n```json\n{escape_md(result)}\n```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_n8n(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/n8n <webhook_path_or_id>`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    path = ctx.args[0]
    payload = {"source": "telegram_commander", "triggered_by": "maciej", "timestamp": datetime.now(timezone.utc).isoformat()}

    msg = await update.message.reply_text(f"⚡ Triggering n8n: `{escape_md(path)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            url = f"{N8N_URL}/webhook/{path}" if not path.startswith("http") else path
            r = await c.post(url, json=payload, headers={"X-N8N-API-KEY": N8N_API_KEY} if N8N_API_KEY else {})
            await msg.edit_text(
                f"✅ n8n triggered\\! Status: `{r.status_code}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception as e:
        await msg.edit_text(f"❌ n8n error: `{escape_md(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    chat_id = update.effective_chat.id
    _conversations[chat_id] = []
    await save_conversation(chat_id, [])
    await update.message.reply_text("🧹 Historia rozmowy wyczyszczona\\!", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    global CLAUDE_MODEL
    if not ctx.args:
        await update.message.reply_text(f"Aktualny model: `{escape_md(CLAUDE_MODEL)}`\nUżyj: `/model haiku` lub `/model sonnet`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    choice = ctx.args[0].lower()
    if "haiku" in choice:
        CLAUDE_MODEL = "claude-haiku-4-5-20251001"
    elif "sonnet" in choice:
        CLAUDE_MODEL = "claude-sonnet-4-6"
    else:
        await update.message.reply_text("❓ Dostępne: `haiku`, `sonnet`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await update.message.reply_text(f"✅ Model zmieniony na: `{escape_md(CLAUDE_MODEL)}`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ── Message handler (Claude chat) ─────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    # Show typing indicator
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_claude(chat_id, text)

    # Split long messages
    max_len = 4000
    if len(reply) <= max_len:
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    else:
        for i in range(0, len(reply), max_len):
            await update.message.reply_text(reply[i:i+max_len], parse_mode=ParseMode.MARKDOWN)


# ── Webhook server (for AutoHeal / Watchdog push alerts) ──────────────────────
bot_instance: Optional[Bot] = None

async def webhook_handler(request: web.Request) -> web.Response:
    """Receive push notifications from AutoHeal, Watchdog, etc."""
    global bot_instance
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    source = data.get("source", "unknown")
    level = str(data.get("level", "info")).upper()
    app = data.get("app_name", data.get("app", "?"))
    message = data.get("message", data.get("msg", "Alert received"))
    timestamp = data.get("timestamp", datetime.now(timezone.utc).isoformat())[:16].replace("T", " ")

    emoji_map = {"CRITICAL": "🚨", "ERROR": "🔴", "WARNING": "🟡", "INFO": "ℹ️", "RECOVERY": "✅"}
    emoji = emoji_map.get(level, "📢")

    text = (
        f"{emoji} *\\[{escape_md(source)}\\]* `{escape_md(level)}`\n"
        f"**App:** `{escape_md(str(app))}`\n"
        f"**Time:** `{escape_md(timestamp)}`\n"
        f"**Msg:** {escape_md(str(message)[:400])}"
    )

    if bot_instance:
        try:
            await bot_instance.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            log.error(f"Telegram send error: {e}")

    return web.Response(text="ok")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text=json.dumps({"status": "ok", "bot": "commander"}), content_type="application/json")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    global bot_instance

    log.info("Starting Ofshore Commander...")

    # Build Telegram app
    app = Application.builder().token(BOT_TOKEN).build()
    bot_instance = app.bot

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("apps", cmd_apps))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("db", cmd_db))
    app.add_handler(CommandHandler("n8n", cmd_n8n))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Aiohttp web server for push notifications
    web_app = web.Application()
    web_app.router.add_post("/alert", webhook_handler)
    web_app.router.add_post("/webhook", webhook_handler)
    web_app.router.add_get("/health", health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info(f"Webhook server on port {WEBHOOK_PORT}")

    # Start bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Commander bot polling started ✅")

    # Notify admin on startup
    try:
        await bot_instance.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🛸 *Ofshore Commander* uruchomiony\\!\nWpisz /help żeby zobaczyć komendy\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        log.warning(f"Startup notify failed: {e}")

    # Keep running
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
