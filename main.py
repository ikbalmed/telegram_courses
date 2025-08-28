# main.py
import os
import asyncio
from dotenv import load_dotenv

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

async def run_both_polling(student_app, admin_app):
    # 1) Clean slate: drop webhooks & pending updates for both bots
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # 2) Sanity check: different tokens/users
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # 3) Initialize + start both apps
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # 4) Start polling for both (one updater per app)
    await student_app.updater.start_polling()
    await admin_app.updater.start_polling()

    # 5) Run forever until cancelled (Render sends SIGTERM on redeploy)
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        # 6) Graceful shutdown — IMPORTANT to await these to avoid warnings
        try:
            await student_app.updater.stop()
        except Exception:
            pass
        try:
            await admin_app.updater.stop()
        except Exception:
            pass
        try:
            await student_app.stop()
            await admin_app.stop()
        finally:
            # Ensure full cleanup (prevents "coroutine never awaited")
            try:
                await student_app.shutdown()
            except Exception:
                pass
            try:
                await admin_app.shutdown()
            except Exception:
                pass

async def main():
    # Build apps from your factories (they return Application instances)
    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)

    # POLLING mode for Render (simple & reliable)
    await run_both_polling(student_app, admin_app)

if __name__ == "__main__":
    # Use asyncio.run so the loop is created & closed by Python,
    # and we don't try to close a running loop ourselves.
    asyncio.run(main())
