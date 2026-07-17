"""SmokeStack ERP — Telegram bot runtime (Render Background Worker).

This is a standalone long-polling bot process. It does NOT run inside FastAPI.
It reuses the existing ERP linking endpoints in app/routers/telegram.py over HTTP:

    POST /api/telegram/link/verify   {tg_id, code, device}  -> {ok, user}
    GET  /api/telegram/session/{tg_id}                       -> {linked, user}

Environment:
    TELEGRAM_BOT_TOKEN   (required)  BotFather token for @SmokkestakERP_bot
    SMOKESTACK_API_BASE  (optional)  default https://smokestack-api.onrender.com
"""
import os
import logging
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("smokestack-telegram")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("SMOKESTACK_API_BASE", "https://smokestack-api.onrender.com").rstrip("/")

HELP_TEXT = (
    "*SmokeStack ERP bot*\n\n"
    "/start – get started\n"
    "/help – show this help\n"
    "/link CODE – connect your ERP account (get the 6-digit CODE in the web app → Settings → Telegram)\n"
    "/me – show your linked account"
)


async def _api_get(path: str):
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(API_BASE + path)
        return r.status_code, (r.json() if r.content else {})


async def _api_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(API_BASE + path, json=body)
        return r.status_code, (r.json() if r.content else {})


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_user.first_name if update.effective_user else None) or "there"
    await update.message.reply_text(
        f"Hi {name}! \U0001F44B  I'm the SmokeStack ERP bot.\n\n"
        "To connect your account:\n"
        "1️⃣ Open the web app and sign in\n"
        "2️⃣ Go to Settings → Telegram and generate a 6-digit code\n"
        "3️⃣ Send me:  /link 123456\n\n"
        "Then /me shows who you are.  /help lists every command."
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage:  /link CODE\nGet the 6-digit code from the web app → Settings → Telegram."
        )
        return
    code = ctx.args[0].strip()
    u = update.effective_user
    tg_id = str(u.id)
    username = u.username or u.full_name or None
    try:
        status, data = await _api_post(
            "/api/telegram/link/verify",
            {"tg_id": tg_id, "code": code, "device": "Telegram", "username": username},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("link verify failed to reach API: %s", e)
        await update.message.reply_text("Couldn't reach the ERP server. Please try again shortly.")
        return
    if status == 200 and data.get("ok"):
        u = data.get("user", {})
        await update.message.reply_text(
            f"✅ Linked! You're signed in as *{u.get('name')}* ({u.get('role')}).",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "❌ That code is invalid or expired. Generate a new one in the web app → Settings → Telegram."
        )


async def me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    try:
        status, data = await _api_get(f"/api/telegram/session/{tg_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("session lookup failed to reach API: %s", e)
        await update.message.reply_text("Couldn't reach the ERP server. Please try again shortly.")
        return
    if data.get("linked"):
        u = data.get("user", {})
        branches = u.get("branches")
        br = ", ".join(branches) if branches else "all branches"
        await update.message.reply_text(
            f"You're linked as *{u.get('name')}* — {u.get('role')} ({br}).",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "You're not linked yet. Use  /link CODE  (get the code in the web app → Settings → Telegram)."
        )


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — configure it on the Render worker.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("me", me))
    log.info("SmokeStack Telegram bot starting (long polling); API_BASE=%s", API_BASE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
