"""
Minimal stub for loguru.logger used in test-sandbox (no pip access).
Injected via sys.modules before any app import.
"""
import sys
import types

class _StubLogger:
    def debug(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def critical(self, *a, **kw): pass
    def exception(self, *a, **kw): pass

_mod = types.ModuleType("loguru")
_mod.logger = _StubLogger()
sys.modules["loguru"] = _mod
