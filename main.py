# main.py
import os
import asyncio
import logging
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main")

PUBLIC_URL   = os.getenv("PUBLIC_URL")  # e.g. https://your-app.onrender.com
PORT         = int(os.getenv("PORT", "10000"))
STUDENT_TOKEN = os.getenv("STUDENT_BOT_TOKEN")
ADMIN_TOKEN   = os.getenv("ADMIN_BOT_TOKEN")

if not PUBLIC_URL or not STUDENT_TOKEN or not ADMIN_TOKEN:
    raise RuntimeError("Missing one of PUBLIC_URL, STUDENT_BOT_TOKEN, ADMIN_BOT_TOKEN env vars.")

async def _ensure_webhook(bot, want_url: str):
    """
    Set webhook only if it's not already pointing at want_url.
    Saves precious cold-start seconds on Render Free.
    """
    try:
        info = await bot.get_webhook_info()
        if info and info.url == want_url:
            logger.info("Webhook already set: %s", want_url)
            return
    except TelegramError as e:
        logger.warning("get_webhook_info failed (will try set_webhook): %s", e)

    await bot.set_webhook(want_url, max_connections=40)  # allow more parallel posts
    logger.info("Webhook set: %s", want_url)

async def make_app():
    # Build PTB Applications in webhook mode (disable Updater)
    student_app = student_bot_main(updater_none=True)
    admin_app   = await admin_bot_main(student_app, updater_none=True)

    # Initialize & start PTB apps
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # DO NOT delete webhooks on every boot (saves time). Just ensure they are correct.
    student_path = f"/{STUDENT_TOKEN}"
    admin_path   = f"/{ADMIN_TOKEN}"

    await _ensure_webhook(student_app.bot, f"{PUBLIC_URL}{student_path}")
    await _ensure_webhook(admin_app.bot,   f"{PUBLIC_URL}{admin_path}")

    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    async def handle_student(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            # Telegram always posts JSON; still guard to return quickly
            return web.Response(text="bad request", status=400)

        # Create Update and process *asynchronously* so we can ACK immediately
        try:
            update = Update.de_json(data, student_app.bot)
        except Exception:
            return web.Response(text="bad update", status=400)

        asyncio.create_task(student_app.process_update(update))
        return web.Response(text="OK")  # immediate ack to avoid Telegram timeout

    async def handle_admin(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.Response(text="bad request", status=400)

        try:
            update = Update.de_json(data, admin_app.bot)
        except Exception:
            return web.Response(text="bad update", status=400)

        asyncio.create_task(admin_app.process_update(update))
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_post(student_path, handle_student)
    app.router.add_post(admin_path, handle_admin)

    # For graceful shutdown
    app["student_app"] = student_app
    app["admin_app"] = admin_app
    app.on_shutdown.append(on_shutdown)

    logger.info("Webhook paths ready:")
    logger.info("  Student: %s%s", PUBLIC_URL, student_path)
    logger.info("  Admin  : %s%s", PUBLIC_URL, admin_path)
    return app

async def on_shutdown(app: web.Application):
    # Stop PTB apps cleanly (don’t delete webhooks; keep them set)
    student_app = app["student_app"]
    admin_app   = app["admin_app"]
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

def main():
    loop = asyncio.get_event_loop()
    aiohttp_app = loop.run_until_complete(make_app())
    web.run_app(aiohttp_app, host="0.0.0.0", port=PORT, shutdown_timeout=30)

if __name__ == "__main__":
    main()
