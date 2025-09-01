import os
import asyncio
from aiohttp import web
from telegram.ext import Application
from admin_bot import admin_handlers
from student_bot import student_handlers

# Telegram tokens
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
STUDENT_BOT_TOKEN = os.getenv("STUDENT_BOT_TOKEN")

# Webhook config
WEBHOOK_HOST = "https://telegram-courses.onrender.com"
ADMIN_PATH = "/webhook/telegram_admin"
STUDENT_PATH = "/webhook/telegram_student"
PORT = int(os.getenv("PORT", "8080"))

async def init_app():
    # Create apps for each bot
    admin_app = Application.builder().token(ADMIN_BOT_TOKEN).build()
    student_app = Application.builder().token(STUDENT_BOT_TOKEN).build()

    # Register handlers (your existing handlers)
    admin_handlers(admin_app)
    student_handlers(student_app)

    # Create aiohttp web app
    app = web.Application()

    # Add webhook update listeners
    app.router.add_post(ADMIN_PATH, admin_app.webhook_handler)
    app.router.add_post(STUDENT_PATH, student_app.webhook_handler)

    # Set webhook URLs
    await admin_app.bot.set_webhook(url=f"{WEBHOOK_HOST}{ADMIN_PATH}")
    await student_app.bot.set_webhook(url=f"{WEBHOOK_HOST}{STUDENT_PATH}")

    # Start the dispatcher (background processing)
    await admin_app.initialize()
    await student_app.initialize()
    await admin_app.start()
    await student_app.start()

    return app

def main():
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(init_app())
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
