import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

load_dotenv()

# Health check for Render
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[health] Listening on 0.0.0.0:{port} for Render health checks.")
    return server

async def run_both_webhooks(student_app, admin_app):
    port = int(os.getenv("PORT", "10000"))
    render_url = os.getenv("RENDER_EXTERNAL_URL")  # set this in Render dashboard

    # Sanity check
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Remove polling
    await student_app.initialize()
    await admin_app.initialize()

    await student_app.start()
    await admin_app.start()

    # Setup webhook for each bot
    await student_app.bot.set_webhook(f"{render_url}/student/{student_app.bot.token}")
    await admin_app.bot.set_webhook(f"{render_url}/admin/{admin_app.bot.token}")

    # Start webhook servers
    await student_app.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=student_app.bot.token,
        webhook_url=f"{render_url}/student/{student_app.bot.token}"
    )
    await admin_app.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=admin_app.bot.token,
        webhook_url=f"{render_url}/admin/{admin_app.bot.token}"
    )

    try:
        await asyncio.Event().wait()
    finally:
        await student_app.updater.stop()
        await admin_app.updater.stop()
        await student_app.stop()
        await admin_app.stop()
        await student_app.shutdown()
        await admin_app.shutdown()

async def main():
    http_server = start_health_server()

    student_app = student_bot_main()
    admin_app = await admin_bot_main(student_app)

    try:
        await run_both_webhooks(student_app, admin_app)
    finally:
        try:
            http_server.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
