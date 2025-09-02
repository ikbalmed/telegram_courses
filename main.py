import os
import asyncio
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

PUBLIC_URL = os.getenv("PUBLIC_URL")
PORT = int(os.getenv("PORT", "10000"))

if not PUBLIC_URL or not PUBLIC_URL.startswith("https://"):
    raise RuntimeError("Set PUBLIC_URL to your Render HTTPS URL, e.g. https://your-app.onrender.com")

async def make_web_app(student_app, admin_app):
    s_token = os.getenv("STUDENT_BOT_TOKEN")
    a_token = os.getenv("ADMIN_BOT_TOKEN")
    if not s_token or not a_token:
        raise RuntimeError("Missing STUDENT_BOT_TOKEN or ADMIN_BOT_TOKEN")

    # Ensure webhooks are set (drop any pending updates)
    await student_app.bot.set_webhook(url=f"{PUBLIC_URL}/{s_token}", drop_pending_updates=True)
    await admin_app.bot.set_webhook(url=f"{PUBLIC_URL}/{a_token}", drop_pending_updates=True)

    async def health(_request):
        return web.Response(text="ok")

    async def handle_student(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, student_app.bot)
        await student_app.process_update(update)
        return web.Response(text="ok")

    async def handle_admin(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, admin_app.bot)
        await admin_app.process_update(update)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(f"/{s_token}", handle_student)
    app.router.add_post(f"/{a_token}", handle_admin)
    return app

async def main():
    # Build apps from your factories
    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)

    # Sanity check: different tokens/users
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps use the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Initialize & start both apps (JobQueue will run after start)
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # Create and start aiohttp server (one port for both webhooks)
    web_app = await make_web_app(student_app, admin_app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"Webhook server listening on 0.0.0.0:{PORT} — {PUBLIC_URL}")

    # Run forever until Render stops the service
    try:
        await asyncio.Event().wait()
    finally:
        # Graceful shutdown
        await runner.cleanup()
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

if __name__ == "__main__":
    asyncio.run(main())
