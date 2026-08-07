class CleanupService:
    async def cleanup(self, chat_id: int, voice_mgr, state_mgr):
        if voice_mgr: await voice_mgr.leave(chat_id)
        state_mgr.set_state(chat_id, 'IDLE')
