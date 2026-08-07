import asyncio
import logging
from app.bootstrap.startup import initialize_app
from app.bootstrap.lifecycle import start_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s")
logger = logging.getLogger("Main")

async def main():
    logger.info("Starting Phase 1 Live Validation Environment...")
    app_context = await initialize_app()
    await start_bot(app_context)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application stopped gracefully.")
