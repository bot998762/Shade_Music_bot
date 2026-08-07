import asyncio
import logging
from pyrogram import filters
from app.handlers.registry import registry
from app.handlers.play import play_handler

logger = logging.getLogger("Lifecycle")

async def start_bot(context):
    bot = context["bot"]
    assistant = context.get("assistant")
    call_py = context["call_py"]
    controller = context["controller"]

    logger.info("Starting Pyrogram Bot Client...")
    await bot.start()
    if assistant:
        logger.info("Starting Assistant Client...")
        await assistant.start()

    logger.info("Starting PyTgCalls Client...")
    await call_py.start()

    @bot.on_message(filters.command("start"))
    async def _start(client, message):
        cmd = registry.get_commands().get("start")
        if cmd: await cmd["handler"](client, message)

    @bot.on_message(filters.command("help"))
    async def _help(client, message):
        cmd = registry.get_commands().get("help")
        if cmd: await cmd["handler"](client, message)

    @bot.on_message(filters.command("play"))
    async def _play(client, message):
        await play_handler(client, message, controller)

    logger.info("✅ Shade Music Bot is Live and Listening!")
    await asyncio.Event().wait()
