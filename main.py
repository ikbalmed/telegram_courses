
# main.py
import os
import asyncio
import logging
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main")

PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))
STUDENT_TOKEN = os.getenv("STUDENT_BOT_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_BOT_TOKEN")

if not PUBLIC_URL:
    raise RuntimeError("Set PUBLIC_URL (https URL of your Render web service).")

async def make_app():
    # Build applications (factories only build; they don't start polling)
    student_app = student_bot_main(updater_none=True)   # disable Updater; webhook mode
    admin_app   = await admin_bot_main(student_app, updater_none=True)

    # Init & start PTB apps (without their own web servers)
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # Ensure clean slate then set webhooks to our shared web server
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # Use the tokens themselves as secret paths (simple & unique)
    student_path = f"/{STUDENT_TOKEN}"
    admin_path   = f"/{ADMIN_TOKEN}"

    await student_app.bot.set_webhook(f"{PUBLIC_URL}{student_path}")
    await admin_app.bot.set_webhook(f"{PUBLIC_URL}{admin_path}")

    # Build a single aiohttp app with two POST routes and a simple GET /
    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    async def handle_student(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, student_app.bot)
        await student_app.process_update(update)
        return web.Response(text="OK")

    async def handle_admin(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, admin_app.bot)
        await admin_app.process_update(update)
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_post(student_path, handle_student)
    app.router.add_post(admin_path, handle_admin)

    # Store for graceful shutdown
    app["student_app"] = student_app
    app["admin_app"] = admin_app
    app["student_path"] = student_path
    app["admin_path"] = admin_path

    logger.info("Webhook paths set:")
    logger.info(f"  Student: {PUBLIC_URL}{student_path}")
    logger.info(f"  Admin  : {PUBLIC_URL}{admin_path}")

    return app

async def on_shutdown(app: web.Application):
    # Gracefully stop PTB apps
    student_app = app["student_app"]
    admin_app   = app["admin_app"]
    try:
        await student_app.stop()
        await admin_app.stop()
    finally:
        try:
            await student_app.shutdown()
        except Exception:
            pass
        try:
            await admin_app.shutdown()
        except Exception:
            pass

def main():
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(make_app())
    web.run_app(app, host="0.0.0.0", port=PORT, shutdown_timeout=30)

if __name__ == "__main__":
    main()
