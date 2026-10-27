"""python -m session_manager —— 命令行入口（等价于直接运行 core.py）。"""
import sys

from .core import main

if __name__ == "__main__":
    sys.exit(main())
