from dotenv import load_dotenv
import asyncio
from datetime import timedelta

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

# Backstop import: run the reminder job once shortly after startup
# (won't interfere with your in-file scheduling; it's just a safety net)
from student_bot import check_subscriptions_and_send_reminders as run_student_reminders

load_dotenv()

async def main():
    # Build apps
    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)

    # Make sure we're not in webhook mode and clear any stale webhooks
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # Sanity check: these must be different bots/tokens
    student_me = await student_app.bot.get_me()
    admin_me = await admin_app.bot.get_me()
    assert student_me.id != admin_me.id, (
        f"Both apps are using the same bot token (@{student_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{student_me.username} | Admin bot: @{admin_me.username}")

    # Initialize
    await student_app.initialize()
    await admin_app.initialize()

    # Start apps
    await student_app.start()
    await admin_app.start()

    # Start polling (one stream per app)
    await student_app.updater.start_polling()
    await admin_app.updater.start_polling()

    # --- Backstop: ensure reminders run once shortly after startup ---
    # If the in-file scheduling used `when=0`, it can misfire during startup.
    # This extra one-time run (5s later) with a grace window guarantees it fires.
    if not student_app.job_queue.get_jobs_by_name("reminder_boot_backstop"):
        student_app.job_queue.run_once(
            run_student_reminders,
            when=timedelta(seconds=5),
            name="reminder_boot_backstop",
            job_kwargs={"misfire_grace_time": 600},  # 10 min grace
        )

    try:
        # Keep running
        await asyncio.Future()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        # Stop gracefully
        await student_app.stop()
        await admin_app.stop()

if __name__ == '__main__':
    asyncio.run(main())
