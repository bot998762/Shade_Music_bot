class StateManager:
    def __init__(self): self._states = {}
    def set_state(self, chat_id: int, state: str): self._states[chat_id] = state
    def get_state(self, chat_id: int) -> str: return self._states.get(chat_id, 'IDLE')
