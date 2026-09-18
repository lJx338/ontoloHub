"""HIA-78 C2: Function 运行时（Python/JS 沙箱 + 资源限制）。

子模块:
- sandbox: 同步执行（subprocess + RestrictedPython + timeout/memory 限制）
- function_pool: 异步任务执行（worker pool）
- secrets: 密钥注入
- result: 标准化执行结果
"""
