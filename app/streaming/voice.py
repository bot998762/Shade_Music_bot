import logging
logger = logging.getLogger('VoiceManager')
class VoiceChatManager:
    async def play_stream(self, chat_id: int, stream_url: str):
        logger.info(f'Connecting PyTgCalls to chat {chat_id} with stream URL')
    async def leave(self, chat_id: int):
        logger.info(f'Leaving voice chat {chat_id}')
