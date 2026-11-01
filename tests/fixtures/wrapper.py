import os, subprocess, sys
# 模拟 run_harness.py：把真正的干活进程作为孙/曾孙拉起来后等待
subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], check=False)
