import os
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

from student_bot import main as student_bot_main
from admin_bot import main as admin_bot_main

from aiohttp import web

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
    # Health server runs on a different port so Render can ping it
    port = int(os.getenv("HEALTH_PORT", "10001"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[health] Listening on 0.0.0.0:{port} for Render health checks.")
    return server

async def run_both_webhooks(student_app, admin_app):
    port = int(os.getenv("PORT", "10000"))  # Render gives only ONE port
    render_url = os.getenv("RENDER_EXTERNAL_URL")  # Set this in Render dashboard

    # Sanity check: different tokens
    s_me = await student_app.bot.get_me()
    a_me = await admin_app.bot.get_me()
    assert s_me.id != a_me.id, (
        f"Both apps are using the same bot token (@{s_me.username}). "
        "Set separate STUDENT_BOT_TOKEN and ADMIN_BOT_TOKEN."
    )
    print(f"Student bot: @{s_me.username} | Admin bot: @{a_me.username}")

    # Init bots
    await student_app.initialize()
    await admin_app.initialize()
    await student_app.start()
    await admin_app.start()

    # Register webhooks with Telegram
    await student_app.bot.set_webhook(f"{render_url}/student")
    await admin_app.bot.set_webhook(f"{render_url}/admin")

    # aiohttp server handling both bots on different routes
    async def handle_student(request):
        update = await request.json()
        await student_app.update_queue.put(update)
        return web.Response(status=200)

    async def handle_admin(request):
        update = await request.json()
        await admin_app.update_queue.put(update)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/student", handle_student)
    app.router.add_post("/admin", handle_admin)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Webhook server running on port {port}")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
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
