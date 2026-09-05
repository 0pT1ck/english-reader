"""Launcher.

Exists because the user starts this by double-clicking a .bat file, not from a
shell, so three things have to happen before uvicorn takes over:

* **Check the port first.** uvicorn's own failure for an occupied port is an
  English traceback that scrolls past the useful part, and the usual cause is a
  previous window still running — worth saying plainly.
* **Print where to go.** The console URL and where the password lives.
* **Keep all Chinese text on this side.** A .bat file is parsed by cmd using the
  system code page *before* `chcp` can take effect, so non-ASCII characters in
  the batch file itself get mangled and executed as commands. Python's stdout is
  already reconfigured to UTF-8 (see core.logging), so text printed here is safe.
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.core.config import ENV_FILE, get_settings  # noqa: E402


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    dev = "--dev" in sys.argv
    settings = get_settings()
    host, port = settings.host, settings.port
    url = f"http://{host}:{port}"

    if port_in_use(host, port):
        print("=" * 58)
        print(f"  端口 {port} 已经被占用，服务无法启动。")
        print()
        print("  最常见的原因是上一个启动窗口还开着。")
        print("  关掉那个窗口后重试；或者在 .env 里改 ER_PORT 换一个端口。")
        print("=" * 58)
        input("\n按回车退出…")
        return 1

    print("=" * 58)
    print("  English Reader")
    print()
    print(f"  管理控制台   {url}/admin")
    print(f"  接口文档     {url}/docs")
    print()
    print(f"  管理密码在   {ENV_FILE}")
    print("               变量名 ER_ADMIN_SECRET")
    if dev:
        print()
        print("  开发模式：改代码自动重载，DEBUG 日志直接落盘")
    print()
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)
    print()

    # Give the server a moment to bind before opening the browser, so the first
    # request does not hit a connection refused.
    threading.Timer(1.5, lambda: webbrowser.open(f"{url}/admin")).start()

    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=dev,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
