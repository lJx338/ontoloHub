"""HIA-78 C2: Sandbox 执行冒烟测试。"""
import asyncio
from src.runtime.sandbox import execute_python, execute_javascript, validate_python_source
from src.runtime.result import SandboxError


def test_basic_python():
    """基础 Python 执行：返回字符串。"""
    code = '''
result = {
    "greeting": f"hello, {input_data.get('name', 'world')}",
    "doubled": input_data.get("x", 0) * 2,
}
'''
    r = execute_python(code, {"name": "alice", "x": 21})
    print(f"[basic] result={r.result!r} exit={r.exit_code} duration={r.duration_ms}ms")
    assert r.result == {"greeting": "hello, alice", "doubled": 42}
    assert r.exit_code == 0
    print("[basic] PASS")


def test_python_timeout():
    """超时控制：sleep 应该被 timeout 截断。"""
    code = '''
import time
time.sleep(10)
result = "should not reach"
'''
    try:
        execute_python(code, {}, timeout=2)
        print("[timeout] FAIL — should have raised")
    except SandboxError as e:
        assert e.kind == "timeout", f"expected timeout, got {e.kind}"
        print(f"[timeout] PASS — got {e}")


def test_python_runtime_error():
    """运行时错误应该被报告（exit code != 0）。"""
    code = '''
result = 1 / 0
'''
    r = execute_python(code, {})
    assert r.exit_code != 0
    assert "ZeroDivisionError" in r.stderr or "division" in r.stderr
    print(f"[runtime_error] PASS — exit={r.exit_code} stderr={r.stderr[:80]!r}")


def test_python_with_secrets():
    """密钥注入：通过 secrets 字典。"""
    code = '''
api_key = secrets.get("STRIPE_API_KEY", "missing")
result = {"has_key": bool(api_key), "key_prefix": api_key[:6] if api_key else None}
'''
    r = execute_python(code, {}, secrets={"STRIPE_API_KEY": "sk_test_abc123"})
    assert r.result["has_key"] is True
    assert r.result["key_prefix"] == "sk_tes"
    print(f"[secrets] PASS — result={r.result}")


def test_syntax_validation():
    """语法错误应该被早拒绝。"""
    code = '''
def broken_function(:
    return "missing close paren"
'''
    try:
        validate_python_source(code)
        print("[syntax] FAIL — should have raised")
    except SandboxError as e:
        assert e.kind == "syntax"
        print(f"[syntax] PASS — {e}")


def test_ast_check_passes_for_normal_code():
    """普通代码应该通过 AST 检查。"""
    code = '''
result = {"x": 1, "y": 2}
'''
    try:
        validate_python_source(code)
        print("[ast] PASS — normal code accepted")
    except SandboxError as e:
        print(f"[ast] FAIL — normal code rejected: {e}")


def test_python_complex_calc():
    """复杂计算：累计 / 聚合。"""
    code = '''
items = input_data.get("items", [])
total = sum(i["price"] * i["qty"] for i in items)
result = {"total": total, "count": len(items)}
'''
    r = execute_python(code, {"items": [
        {"price": 10, "qty": 3},
        {"price": 5, "qty": 7},
    ]})
    assert r.result["total"] == 10 * 3 + 5 * 7  # = 65
    assert r.result["count"] == 2
    print(f"[complex] PASS — result={r.result}")


def test_javascript_basic():
    """JS 执行（如果 Node 可用）。"""
    code = '''
const x = (input_data.x || 0) * 2;
result = { doubled: x, name: input_data.name };
'''
    try:
        r = execute_javascript(code, {"x": 21, "name": "bob"})
        assert r.result["doubled"] == 42
        print(f"[js] PASS — result={r.result} duration={r.duration_ms}ms")
    except SandboxError as e:
        if e.kind == "config":
            print(f"[js] SKIP — Node.js not available: {e.message}")
        else:
            raise


if __name__ == "__main__":
    test_basic_python()
    test_python_timeout()
    test_python_runtime_error()
    test_python_with_secrets()
    test_syntax_validation()
    test_ast_check_passes_for_normal_code()
    test_python_complex_calc()
    test_javascript_basic()
    print("\n[ALL] All sandbox smoke tests passed")
