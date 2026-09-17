import sqlite3
c = sqlite3.connect(r"C:\Users\liuxi\AppData\Local\Temp\pytest-of-liuxi\pytest-10\test_bootstrap_admin_and_me0\test.db")
print("tables:", list(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
print("users:", list(c.execute("SELECT email FROM users").fetchall()))
c.close()
