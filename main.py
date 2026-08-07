import asyncio
import logging
import os
import sys
from aiohttp import web

sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Main")

async def health_check(request):
    return web.Response(text="Shade Music Bot is Live!", status=200)

async def start_web_server():
    port = int(os.getenv("PORT", os.getenv("APP_PORT", 8080)))
    logger.info(f"Starting Keep-Alive Web Server on port {port}...")
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Web Server successfully running!")

async def main():
    logger.info("Starting Phase 1 Live Validation Environment...")
    try:
        await start_web_server()
        from app.bootstrap.startup import initialize_app
        from app.bootstrap.lifecycle import start_bot
        
        app_context = await initialize_app()
        await start_bot(app_context)
    except Exception as e:
        logger.error(f"❌ CRITICAL RUNTIME ERROR: {str(e)}", exc_info=True)
        await asyncio.sleep(5)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application stopped gracefully.")
