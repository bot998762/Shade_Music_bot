import os
import logging
from pyrogram import Client
from pytgcalls import PyTgCalls
from app.playback.controller import PlaybackController
from app.playback.state import StateManager
from app.streaming.voice import VoiceChatManager
from app.handlers.registry import registry
from app.handlers.start import start_handler
from app.handlers.help import help_handler

logger = logging.getLogger("Startup")

async def initialize_app():
    logger.info("Initializing Pyrogram & PyTgCalls Clients...")
    
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    bot_token = os.getenv("BOT_TOKEN")
    session_string = os.getenv("SESSION_STRING") or os.getenv("ASSISTANT_SESSION_STRING")

    if not api_id or not api_hash or not bot_token:
        raise ValueError(f"Missing Essential Vars! API_ID={bool(api_id)}, API_HASH={bool(api_hash)}, BOT_TOKEN={bool(bot_token)}")

    bot = Client("ShadeMusicBot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
    
    assistant = None
    if session_string:
        logger.info("Assistant Session String detected. Initializing User Client...")
        assistant = Client("ShadeAssistant", api_id=int(api_id), api_hash=api_hash, session_string=session_string)
    else:
        logger.warning("No ASSISTANT_SESSION_STRING provided! Running Bot-only mode.")

    call_py = PyTgCalls(assistant if assistant else bot)
    voice_mgr = VoiceChatManager()
    state_mgr = StateManager()
    controller = PlaybackController(voice_mgr, state_mgr)

    registry.register("start", "Start the bot", start_handler)
    registry.register("help", "Show help menu", help_handler)
    
    return {"bot": bot, "assistant": assistant, "call_py": call_py, "controller": controller}
