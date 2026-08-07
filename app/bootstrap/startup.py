import os
import logging
from pyrogram import Client

# Robust multi-version import handling for PyTgCalls
try:
    from pytgcalls import PyTgCalls
except ImportError:
    try:
        from pytgcalls.pytgcalls import PyTgCalls
    except ImportError:
        from pytgcalls.client import PyTgCalls

from app.playback.controller import PlaybackController
from app.playback.state import StateManager
from app.streaming.voice import VoiceChatManager
from app.handlers.registry import registry
from app.handlers.start import start_handler
from app.handlers.help import help_handler

logger = logging.getLogger("Startup")

async def initialize_app():
    logger.info("Initializing clients and services...")
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    bot_token = os.getenv("BOT_TOKEN")
    session_string = os.getenv("SESSION_STRING") or os.getenv("ASSISTANT_SESSION_STRING")

    if not all([api_id, api_hash, bot_token]):
        raise ValueError("Missing essential env vars: API_ID, API_HASH, or BOT_TOKEN")

    bot = Client("ShadeMusicBot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
    
    assistant = None
    if session_string:
        logger.info("Assistant Session detected. Initializing Client...")
        assistant = Client("ShadeAssistant", api_id=int(api_id), api_hash=api_hash, session_string=session_string)

    call_py = PyTgCalls(assistant if assistant else bot)
    voice_mgr = VoiceChatManager()
    state_mgr = StateManager()
    controller = PlaybackController(voice_mgr, state_mgr)

    registry.register("start", "Start the bot", start_handler)
    registry.register("help", "Show help menu", help_handler)
    
    return {"bot": bot, "assistant": assistant, "call_py": call_py, "controller": controller}
