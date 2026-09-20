"""HIA-78 C2: Function 沙箱执行结果。

执行完成后从 subprocess stdout 解析出：
- result: 用户代码 `result = ...` 的返回值（JSON 序列化）
- stdout/stderr: 捕获的标准输出 / 错误
- duration_ms: 执行耗时
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SandboxResult:
    """沙箱执行结果。"""

    result: Any = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    memory_mb_peak: float = 0.0
    timed_out: bool = False
    exit_code: int = 0

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "memory_mb_peak": self.memory_mb_peak,
            "timed_out": self.timed_out,
            "exit_code": self.exit_code,
        }


@dataclass
class SandboxError(Exception):
    """沙箱执行错误（含分类，便于上层做不同处理）。"""

    kind: str = "error"  # syntax | runtime | timeout | memory | permission | config
    message: str = ""
    traceback: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"
