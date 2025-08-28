# main.py
import asyncio
from dotenv import load_dotenv

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

async def run_both_polling(student_app, admin_app):
    # Clean slate
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # Sanity checks: tokens must be different
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Init & start
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # Start polling for both
    await student_app.updater.start_polling()
    await admin_app.updater.start_polling()

    # Keep running
    try:
        await asyncio.Event().wait()
    finally:
        # Graceful shutdown (await all!)
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

async def main():
    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)
    await run_both_polling(student_app, admin_app)

if __name__ == "__main__":
    asyncio.run(main())
