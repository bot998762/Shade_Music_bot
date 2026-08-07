import logging
logger = logging.getLogger('StreamMonitor')
class StreamMonitor:
    def __init__(self, controller):
        self._controller = controller
    async def on_stream_end(self, chat_id: int):
        logger.info(f'Stream ended for chat {chat_id}')
        await self._controller.advance(chat_id)
