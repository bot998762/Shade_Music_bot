class CommandRegistry:
    def __init__(self): self._commands = {}
    def register(self, name: str, description: str, handler_func): self._commands[name] = {'description': description, 'handler': handler_func}
    def get_commands(self): return self._commands
registry = CommandRegistry()
