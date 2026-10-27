#!/usr/bin/env python3
"""开发入口：起网页界面并打开浏览器（打包成 exe 时也用它）。

    python run.py                 # 默认 http://127.0.0.1:8765
    python run.py --port 8899     # 换端口（被占用会自动往后找）
    python run.py --no-browser    # 只起服务

等价的命令行入口：
    python -m session_manager list | show <id> | del <id> | clean | trash | stats
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from session_manager import server  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = 8765
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print("--port 需要数字，已改用默认端口 %d" % port)
    return server.serve(None, port, "--no-browser" not in argv)


if __name__ == "__main__":
    sys.exit(main())
