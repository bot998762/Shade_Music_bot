import os
import logging
import importlib
from pyrogram import Client

logger = logging.getLogger("Startup")

def get_pytgcalls_class():
    import pytgcalls
    if hasattr(pytgcalls, "PyTgCalls"):
        return pytgcalls.PyTgCalls
    for mod_name in ["pytgcalls", "client", "pytgcalls.pytgcalls", "pytgcalls.client"]:
        try:
            m = importlib.import_module(mod_name)
            if hasattr(m, "PyTgCalls"):
                return m.PyTgCalls
        except Exception:
            continue
    raise ImportError("Could not locate PyTgCalls class in pytgcalls package")

async def initialize_app():
    logger.info("Initializing Pyrogram & PyTgCalls Clients...")
    
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    bot_token = os.getenv("BOT_TOKEN")
    session_string = os.getenv("SESSION_STRING") or os.getenv("ASSISTANT_SESSION_STRING")

    if not api_id or not api_hash or not bot_token:
        raise ValueError("Missing essential env vars: API_ID, API_HASH, or BOT_TOKEN")

    bot = Client("ShadeMusicBot", api_id=int(api_id), api_hash=api_hash, bot_token=bot_token)
    
    assistant = None
    if session_string:
        logger.info("Assistant Session detected. Initializing User Client...")
        assistant = Client("ShadeAssistant", api_id=int(api_id), api_hash=api_hash, session_string=session_string)
    else:
        logger.warning("No ASSISTANT_SESSION_STRING provided! Running Bot-only client.")

    PyTgCallsClass = get_pytgcalls_class()
    call_py = PyTgCallsClass(assistant if assistant else bot)
    
    from app.playback.controller import PlaybackController
    from app.playback.state import StateManager
    from app.streaming.voice import VoiceChatManager
    from app.handlers.registry import registry
    from app.handlers.start import start_handler
    from app.handlers.help import help_handler

    voice_mgr = VoiceChatManager()
    state_mgr = StateManager()
    controller = PlaybackController(voice_mgr, state_mgr)

    registry.register("start", "Start the bot", start_handler)
    registry.register("help", "Show help menu", help_handler)
    
    return {"bot": bot, "assistant": assistant, "call_py": call_py, "controller": controller}
