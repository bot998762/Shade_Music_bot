class StateManager:
    def __init__(self): self._states = {}
    def set_state(self, chat_id: int, state: str): self._states[chat_id] = state
