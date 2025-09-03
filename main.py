# main.py
import os
import asyncio
import logging
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError

from student_bot import main as student_bot_main, prewarm_clients as student_prewarm
from admin_bot import main as admin_bot_main, prewarm_clients as admin_prewarm

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

PUBLIC_URL    = os.getenv("PUBLIC_URL")   # e.g. https://your-app.onrender.com
PORT          = int(os.getenv("PORT", "10000"))
STUDENT_TOKEN = os.getenv("STUDENT_BOT_TOKEN")
ADMIN_TOKEN   = os.getenv("ADMIN_BOT_TOKEN")
SETUP_KEY     = os.getenv("SETUP_KEY", "")  # set a random secret for /setup

if not PUBLIC_URL or not STUDENT_TOKEN or not ADMIN_TOKEN:
    raise RuntimeError("Missing one of PUBLIC_URL, STUDENT_BOT_TOKEN, ADMIN_BOT_TOKEN")

# Secret-ish per-bot webhook paths
STUDENT_PATH = f"/{STUDENT_TOKEN}"
ADMIN_PATH   = f"/{ADMIN_TOKEN}"

# Global state for lazy init
student_app = None
admin_app = None
apps_ready = asyncio.Event()
apps_init_lock = asyncio.Lock()

async def _ensure_webhook(bot, want_url: str):
    """Set webhook only if different; saves cold-start time."""
    try:
        info = await bot.get_webhook_info()
        if info and info.url == want_url:
            log.info("Webhook already set: %s", want_url)
            return
    except TelegramError as e:
        log.warning("get_webhook_info failed; will set_webhook: %s", e)
    await bot.set_webhook(want_url, max_connections=40)
    log.info("Webhook set: %s", want_url)

async def init_apps():
    """Build & start PTB apps (webhook mode). Idempotent."""
    global student_app, admin_app
    if apps_ready.is_set():
        return
    async with apps_init_lock:
        if apps_ready.is_set():
            return
        log.info("Initializing PTB apps (webhook mode)...")
        # Build apps in webhook mode (disable Updater in factories)
        s_app = student_bot_main(updater_none=True)
        a_app = await admin_bot_main(s_app, updater_none=True)

        await s_app.initialize()
        await a_app.initialize()
        await s_app.start()
        await a_app.start()

        student_app = s_app
        admin_app = a_app
        apps_ready.set()
        log.info("PTB apps are ready.")

async def handle_health(_request: web.Request):
    return web.Response(text="ok")

async def handle_setup(request: web.Request):
    """
    Run once after your first deploy or when you change PUBLIC_URL:
    GET /setup?key=<SETUP_KEY>
    """
    if not SETUP_KEY:
        return web.Response(text="SETUP_KEY not configured", status=400)
    if request.query.get("key") != SETUP_KEY:
        return web.Response(text="forbidden", status=403)

    await init_apps()
    await _ensure_webhook(student_app.bot, f"{PUBLIC_URL}{STUDENT_PATH}")
    await _ensure_webhook(admin_app.bot,   f"{PUBLIC_URL}{ADMIN_PATH}")
    return web.Response(text="webhooks set")

async def _process_update(app, raw: dict):
    update = Update.de_json(raw, app.bot)
    await app.process_update(update)

async def handle_student(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad request", status=400)

    if not apps_ready.is_set():
        # Kick off init + prewarm, but ACK quickly to trigger Telegram retry
        asyncio.create_task(init_apps())
        # Prewarm Google clients right away in parallel
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, student_prewarm)
        loop.run_in_executor(None, admin_prewarm)
        try:
            await asyncio.wait_for(apps_ready.wait(), timeout=8)
        except asyncio.TimeoutError:
            return web.Response(text="warming up", status=503)

    asyncio.create_task(_process_update(student_app, data))
    return web.Response(text="OK")

async def handle_admin(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad request", status=400)

    if not apps_ready.is_set():
        asyncio.create_task(init_apps())
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, student_prewarm)
        loop.run_in_executor(None, admin_prewarm)
        try:
            await asyncio.wait_for(apps_ready.wait(), timeout=8)
        except asyncio.TimeoutError:
            return web.Response(text="warming up", status=503)

    asyncio.create_task(_process_update(admin_app, data))
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    # Start warming immediately so the first webhook is fast
    asyncio.create_task(init_apps())
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, student_prewarm)
    loop.run_in_executor(None, admin_prewarm)
    log.info("Server booted. Paths:\n  %s%s\n  %s%s",
             PUBLIC_URL, STUDENT_PATH, PUBLIC_URL, ADMIN_PATH)

async def on_shutdown(app: web.Application):
    if not apps_ready.is_set():
        return
    log.info("Shutting down PTB apps...")
    try:
        await student_app.stop()
    finally:
        try:
            await student_app.shutdown()
        except Exception:
            pass
    try:
        await admin_app.stop()
    finally:
        try:
            await admin_app.shutdown()
        except Exception:
            pass
    log.info("Shutdown complete.")

def main():
    app = web.Application()
    app.add_routes([
        web.get("/", handle_health),
        web.get("/setup", handle_setup),
        web.post(STUDENT_PATH, handle_student),
        web.post(ADMIN_PATH, handle_admin),
    ])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=int(PORT), shutdown_timeout=30)

if __name__ == "__main__":
    main()
