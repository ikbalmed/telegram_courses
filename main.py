import os
import asyncio
from telegram.ext import Application, CommandHandler

TOKEN = os.getenv("TELEGRAM_TOKEN")

WEBHOOK_PATH = "/webhook/telegram_student"  # you can change this
WEBHOOK_URL = f"https://telegram-courses.onrender.com{WEBHOOK_PATH}"

async def start(update, context):
    await update.message.reply_text("Hello! Bot is working with webhook 🎉")

async def main():
    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))

    # Start webhook
    await application.bot.set_webhook(WEBHOOK_URL)

    # Run the webhook server
    await application.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8080)),
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
    )

if __name__ == "__main__":
    asyncio.run(main())
