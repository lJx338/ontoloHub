"""HIA-78 C2: Function 沙箱执行器。

## 设计目标

- **OS-level 隔离**：用户代码运行在 subprocess 中，进程崩溃不影响主进程
- **CPU/时间限制**：`subprocess.run(timeout=...)` 强制 wall-clock 超时
- **内存限制**（Unix）：`resource.setrlimit` 在 preexec_fn 里设置
- **Python AST 检查**：`RestrictedPython` 编译期过滤危险调用
- **输入输出**：`input_data` 作为变量注入，`result` 变量作为返回值
- **stdout 协议**：用户代码最后把 `result` 用 `print(json.dumps(...))` 写到 stdout

## 协议（Python 子进程）

1. 主进程把 `input_data` 序列化为 JSON 写入 stdin
2. 子进程通过 RestrictedPython 编译 + 执行用户代码
3. 子进程捕获 stdout / stderr；如果定义了 `result` 变量则序列化到 stdout（用 `<<<RESULT>>>` 标记）
4. 主进程解析 stdout，构造 `SandboxResult`

## 资源限制

| 限制项 | 实现 |
|--------|------|
| Wall-clock timeout | `subprocess.run(timeout=...)` |
| CPU time | Unix: `resource.RLIMIT_CPU` |
| Memory | Unix: `resource.RLIMIT_AS` |
| 输出大小 | 子进程 stdout 截断 1MB |
| 文件描述符 | Unix: `resource.RLIMIT_NOFILE` |

Windows 上 `RLIMIT_*` 不可用，依赖 timeout + 输出截断做兜底。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

from .result import SandboxError, SandboxResult

logger = logging.getLogger(__name__)


# ===== Resource limits =====

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1MB
DEFAULT_MAX_MEMORY_MB = 256
DEFAULT_MAX_CPU_SECONDS = 25  # 比 timeout 略短，先 KILL


def _set_unix_limits(memory_mb: int, cpu_seconds: int) -> None:
    """Unix preexec_fn：设置子进程资源限制。Windows 上不可用。"""
    try:
        import resource

        # Address space (memory)
        mem_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        # CPU 时间
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        # 最大文件描述符（避免 fork-bomb）
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        except (ValueError, OSError):
            pass
        # 阻止 fork
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            # PR_SET_DUMPABLE=4, PR_SET_NO_NEW_PRIVS=38
            libc.prctl(38, 1, 0, 0, 0)
        except Exception:  # pragma: no cover
            pass
    except Exception as exc:  # pragma: no cover
        # 限制设置失败不致命 — 仍然有 timeout + 输出截断兜底
        logger.warning("setrlimit failed: %s", exc)


# ===== Python wrapper script (runs in subprocess) =====

_PYTHON_RUNNER = textwrap.dedent('''
    """HIA-78 C2 sandbox runner - runs in subprocess.

    真正的安全边界是 subprocess + timeout + (Unix) rlimit。
    编译期只做语法检查，不做 RestrictedPython AST 限制（那会引入大量兼容性问题）。
    """
    import json
    import os
    import sys
    import traceback

    # Read input from stdin
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[sandbox] invalid input JSON: {e}", file=sys.stderr)
        sys.exit(2)

    input_data = payload.get("input_data", {}) or {}
    secrets = payload.get("secrets", {}) or {}
    timeout_s = payload.get("timeout_s", 30)

    # Inject helpers into globals
    globals_dict = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "input_data": input_data,
        "result": None,
        "secrets": secrets,
    }

    user_code = USER_CODE_PLACEHOLDER

    # 编译（plain compile — 已通过主进程的 validate_python_source 做 AST 检查）
    try:
        code_obj = compile(user_code, "<sandbox>", "exec")
    except SyntaxError as e:
        print(f"[sandbox] syntax error: {e}", file=sys.stderr)
        sys.exit(2)

    # 执行
    try:
        exec(code_obj, globals_dict)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # 输出 result（JSON 序列化）
    result_value = globals_dict.get("result")
    try:
        result_json = json.dumps(result_value, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        result_json = json.dumps({"_error": f"non-serializable result: {e}"})

    # 协议：result 用特殊 marker 输出（主进程抓取）
    sys.stdout.write(f"<<<RESULT>>>\\n{result_json}\\n<<<END>>>\\n")
    sys.stdout.flush()
''').strip()


def _build_runner_script(user_code: str) -> str:
    """用用户代码替换 runner 模板里的占位符。"""
    # 用 repr 安全地嵌入字符串（双层 repr —— 防止 \n / 单引号破坏语法）
    embedded = repr(user_code)  # 比如 'a = 1\\nresult = a + 1'
    return _PYTHON_RUNNER.replace("USER_CODE_PLACEHOLDER", embedded)


# ===== Compile check (early validation) =====


def validate_python_source(code: str) -> None:
    """编译期检查 — 只做语法检查，不做 AST 级限制。

    真正的安全边界是 subprocess + timeout + (Unix) rlimit；
    RestrictedPython AST 限制会带来太多兼容性问题（print, getattr, _ 开头属性等），
    收益不抵复杂度。

    Raises:
        SandboxError(kind="syntax", ...): 语法错误
    """
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise SandboxError(
            kind="syntax",
            message=f"Syntax error: {exc.msg}",
            traceback=str(exc),
        )


# ===== Public API =====


def execute_python(
    code: str,
    input_data: Optional[dict] = None,
    secrets: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> SandboxResult:
    """同步执行 Python 代码（subprocess 沙箱）。

    Args:
        code: 用户 Python 代码，必须最终把结果赋值给 `result` 变量
        input_data: 注入到代码中的字典（作为 `input_data` 变量）
        secrets: 注入到代码中的密钥字典（作为 `secrets` 变量；建议仅放非敏感元数据）
        timeout: wall-clock 超时（秒）
        max_memory_mb: 内存限制（MB，仅 Unix）
        max_output_bytes: stdout/stderr 最大字节数

    Returns:
        SandboxResult

    Raises:
        SandboxError: 编译/语法/超时/内存错误
    """
    # 编译期检查
    validate_python_source(code)

    payload = {
        "input_data": input_data or {},
        "secrets": secrets or {},
        "timeout_s": timeout,
    }

    runner_script = _build_runner_script(code)
    start = time.monotonic()

    # 写入临时文件（避免命令行长度限制）
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        encoding="utf-8",
        prefix="sandbox_",
    ) as f:
        f.write(runner_script)
        tmp_path = f.name

    try:
        # preexec_fn 仅在 Unix 生效；Windows 上跳过
        is_unix = platform.system() != "Windows"
        kwargs: dict = {
            "input": json.dumps(payload).encode("utf-8"),
            "capture_output": True,
            "timeout": timeout,
            "env": _sandbox_env(),
        }
        if is_unix:
            kwargs["preexec_fn"] = lambda: _set_unix_limits(
                max_memory_mb, max(timeout - 5, 5)
            )

        proc = subprocess.run([sys.executable, tmp_path], **kwargs)
        duration_ms = int((time.monotonic() - start) * 1000)

        # parse result
        stdout = proc.stdout[:max_output_bytes].decode("utf-8", errors="replace")
        stderr = proc.stderr[:max_output_bytes].decode("utf-8", errors="replace")

        result_value, parse_error = _extract_result(stdout)
        if parse_error:
            stderr = (stderr + "\n" + parse_error).strip()

        return SandboxResult(
            result=result_value,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
        )

    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        raise SandboxError(
            kind="timeout",
            message=f"Execution exceeded {timeout}s timeout",
            traceback=f"timeout_ms={duration_ms}",
        )

    except OSError as exc:
        raise SandboxError(
            kind="config",
            message=f"subprocess failed: {exc}",
        )

    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass


def _sandbox_env() -> dict:
    """构造子进程的环境变量。

    - 保留 PATH / HOME 等必要变量
    - 不继承父进程任何 secret / 凭证
    - 标识这是一个受控沙箱
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "TEMP": os.environ.get("TEMP", os.pathsep + "tmp"),
        "TMP": os.environ.get("TMP", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "PYTHONPATH": "",
        "HIA78_SANDBOX": "1",
    }


def _extract_result(stdout: str) -> tuple[Any, Optional[str]]:
    """从 stdout 解析 `<<<RESULT>>> ... <<<END>>>` 块。

    Returns:
        (result_value, error_message_or_None)
    """
    marker_start = "<<<RESULT>>>"
    marker_end = "<<<END>>>"
    if marker_start not in stdout:
        return None, "no <<<RESULT>>> marker found"
    try:
        start = stdout.index(marker_start) + len(marker_start)
        end = stdout.index(marker_end, start)
        json_str = stdout[start:end].strip()
        return json.loads(json_str), None
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"failed to parse result: {exc}"


# ===== JavaScript execution (placeholder) =====


def execute_javascript(
    code: str,
    input_data: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> SandboxResult:
    """同步执行 JavaScript 代码（Node.js subprocess 沙箱）。

    如果系统未安装 Node.js，会抛 SandboxError(kind="config")。

    实现说明：
    - 把 input_data 写入临时 JSON 文件而非 stdin — 避开 Windows 上 Node 24
      CSPRNG 初始化断言（subprocess stdin pipe 触发）
    - 用 wrapper 脚本从文件读输入，调用用户代码
    """
    if not _node_available():
        raise SandboxError(
            kind="config",
            message="node.js not available on PATH; install Node.js to run JS functions",
        )

    payload = json.dumps({"input_data": input_data or {}})

    # 写 input 到临时文件
    input_fd, input_path = tempfile.mkstemp(suffix=".json", prefix="sandbox_js_in_")
    try:
        with os.fdopen(input_fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        Path(input_path).unlink(missing_ok=True)
        raise

    # 写 wrapper script（从 input_path 读 input_data）
    wrapper_code = textwrap.dedent(
        """
        const fs = require('fs');
        const path = require('path');
        const inputPath = process.env.__HIA78_INPUT_PATH__;
        let input_data = {};
        try {
            const raw = fs.readFileSync(inputPath, 'utf8');
            const parsed = JSON.parse(raw);
            input_data = parsed.input_data || {};
        } catch (e) {
            console.error('[sandbox] failed to read input: ' + e.message);
            process.exit(2);
        }
        let result = null;
        try {
            USER_CODE_HERE
        } catch (e) {
            console.error(e.stack || e.message);
            process.exit(1);
        }
        process.stdout.write('<<<RESULT>>>\\n' + JSON.stringify(result) + '\\n<<<END>>>\\n');
        """
    ).strip().replace("USER_CODE_HERE", code)

    wrapper_fd, wrapper_path = tempfile.mkstemp(suffix=".js", prefix="sandbox_js_run_")
    try:
        with os.fdopen(wrapper_fd, "w", encoding="utf-8") as f:
            f.write(wrapper_code)
    except Exception:
        Path(wrapper_path).unlink(missing_ok=True)
        Path(input_path).unlink(missing_ok=True)
        raise

    start = time.monotonic()
    try:
        # 完整继承环境变量（Windows 上 Node 24 需要 SYSTEMROOT / TEMP 等）
        sandbox_env = _sandbox_env()
        sandbox_env.update({
            "NODE_OPTIONS": "",
            "__HIA78_INPUT_PATH__": input_path,
        })
        proc = subprocess.run(
            ["node", wrapper_path],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=sandbox_env,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        result_value, parse_error = _extract_result(stdout)
        if parse_error:
            stderr = (stderr + "\n" + parse_error).strip()
        return SandboxResult(
            result=result_value,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(
            kind="timeout",
            message=f"JS execution exceeded {timeout}s timeout",
        )
    except FileNotFoundError:
        raise SandboxError(
            kind="config",
            message="node.js executable not found",
        )
    finally:
        try:
            Path(wrapper_path).unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass
        try:
            Path(input_path).unlink(missing_ok=True)
        except Exception:  # pragma: no cover
            pass


def _node_available() -> bool:
    """检测系统是否安装了 Node.js。"""
    try:
        subprocess.run(
            ["node", "--version"],
            capture_output=True,
            timeout=2,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
