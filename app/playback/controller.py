import logging
from app.search.youtube import YouTubeSearch
from app.playback.cleanup import CleanupService
logger = logging.getLogger('PlaybackController')
class PlaybackController:
    def __init__(self, voice_mgr, state_mgr):
        self.search_engine = YouTubeSearch()
        self.voice = voice_mgr
        self.state = state_mgr
        self.cleanup_service = CleanupService()
    async def play(self, chat_id: int, query: str, user_name: str) -> str:
        self.state.set_state(chat_id, 'SEARCHING')
        track_info = await self.search_engine.search_and_resolve(query)
        self.state.set_state(chat_id, 'JOINING')
        await self.voice.play_stream(chat_id, track_info['stream_url'])
        self.state.set_state(chat_id, 'PLAYING')
        return f"🎶 **Now Playing:** [{track_info['title']}]({track_info['webpage_url']})\n👤 Requested by: {user_name}"
    async def advance(self, chat_id: int):
        await self.cleanup_service.cleanup(chat_id, self.voice, self.state)
