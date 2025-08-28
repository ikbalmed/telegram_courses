from dotenv import load_dotenv
import os
import asyncio

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g. https://your-service.onrender.com

async def run_polling_both(student_app, admin_app):
    # make sure no stale webhooks exist (and clear pending updates)
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # sanity check: tokens must be different
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Run both bots concurrently (each manages its own Updater internally)
    await asyncio.gather(
        student_app.run_polling(drop_pending_updates=True),
        admin_app.run_polling(drop_pending_updates=True),
    )

async def run_webhooks_both(student_app, admin_app):
    # webhook mode avoids getUpdates conflicts entirely
    # use token as a unique secret path (simple & effective)
    s_token = os.getenv("STUDENT_BOT_TOKEN")
    a_token = os.getenv("ADMIN_BOT_TOKEN")
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL must be set for webhook mode.")

    # clear old webhooks and pending updates
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")
    print(f"Webhook URL base: {PUBLIC_URL} (PORT={PORT})")

    await asyncio.gather(
        student_app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=s_token,
            webhook_url=f"{PUBLIC_URL}/{s_token}",
            drop_pending_updates=True,
        ),
        admin_app.run_webhook(
            listen="0.0.0.0",
            port=PORT,  # both can share the same port; different paths
            url_path=a_token,
            webhook_url=f"{PUBLIC_URL}/{a_token}",
            drop_pending_updates=True,
        ),
    )

async def main():
    # Build applications (your existing factories)
    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)

    # Choose polling or webhook
    if USE_WEBHOOK:
        await run_webhooks_both(student_app, admin_app)
    else:
        await run_polling_both(student_app, admin_app)

if __name__ == "__main__":
    asyncio.run(main())
