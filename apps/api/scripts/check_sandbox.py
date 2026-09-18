"""Quick sandbox check"""
from src.runtime.sandbox import execute_python

code = "result = {'final': input_data['value'] + 1}"
r = execute_python(code=code, input_data={"value": 6}, timeout=10)
print("result:", r.to_dict())
