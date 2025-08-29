# main.py
import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

# ---------- Minimal HTTP server just to keep Render happy ----------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Always return 200 OK for / or any path
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Silence default request logging
    def log_message(self, fmt, *args):
        pass

def start_health_server():
    """
    Bind to $PORT (or 10000) on 0.0.0.0 in a daemon thread so Render sees
    an open port. Has zero impact on your bot logic.
    """
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[health] Listening on 0.0.0.0:{port} for Render health checks.")
    return server

# ---------- Run both bots with explicit lifecycle (no conflicts) ----------
async def run_both_polling(student_app, admin_app):
    # Clean slate: ensure webhooks are removed and pending updates dropped
    await student_app.bot.delete_webhook(drop_pending_updates=True)
    await admin_app.bot.delete_webhook(drop_pending_updates=True)

    # Sanity: tokens must be different
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Initialize and start both apps
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # Start polling for both (each has its own Updater)
    await student_app.updater.start_polling()
    await admin_app.updater.start_polling()

    # Keep running until cancelled by the platform
    try:
        await asyncio.Event().wait()
    finally:
        # Graceful shutdown (await everything to avoid warnings)
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
    # Start the tiny HTTP server first so Render sees an open port
    http_server = start_health_server()

    # Build apps (your factories)
    student_app = student_bot_main()
    # Admin factory may depend on student_app; keep your existing signature
    admin_app = await admin_bot_main(student_app)

    try:
        await run_both_polling(student_app, admin_app)
    finally:
        # Stop the HTTP server thread on shutdown
        try:
            http_server.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
