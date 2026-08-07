import asyncio
import logging
import os
import sys
from aiohttp import web
from app.bootstrap.startup import initialize_app
from app.bootstrap.lifecycle import start_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Main")

async def health_check(request):
    return web.Response(text="Shade Music Bot is Live!", status=200)

async def start_web_server():
    port = int(os.getenv("APP_PORT", os.getenv("PORT", 8080)))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Dummy Web Server bound to port {port} for Render Web Service compliance.")

async def main():
    logger.info("Starting Phase 1 Live Validation Environment...")
    try:
        await start_web_server()
        app_context = await initialize_app()
        await start_bot(app_context)
    except Exception as e:
        logger.error(f"Fatal Startup Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application stopped gracefully.")
