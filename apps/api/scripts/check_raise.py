"""Verify sandbox raises on Python raise."""
from src.runtime.sandbox import execute_python, SandboxError

try:
    r = execute_python(code="raise ValueError('boom')", input_data={}, timeout=10)
    print("Returned:", r.to_dict())
except SandboxError as exc:
    print(f"SandboxError caught: kind={exc.kind}, message={exc.message}")
except Exception as exc:
    print(f"Other exception: {type(exc).__name__}: {exc}")
