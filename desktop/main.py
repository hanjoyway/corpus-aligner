"""单机桌面版入口：起一个只监听本机的 Flask，然后把浏览器打开到它上面。

界面还是那套网页工作台，但进程跑在使用者自己的电脑上——不联网也能做对齐与
导出，不需要访问码，不需要服务器。打包成 .app / .exe 后双击即用。

开发时直接跑： python -m desktop.main
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 打包后 sys.path 由 PyInstaller 铺好；源码运行时确保项目根目录在 path 上，
# 这样 `import config` / `import app.server` 与线上完全一致。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desktop import settings  # noqa: E402


def _free_port(preferred: int = 5099) -> int:
    """先试固定端口（书签、刷新才稳定），被占了再让系统随便给一个。"""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


# Chromium 系的「应用模式」：开一个没有地址栏、没有标签页、没有书签栏的独立窗口，
# 任务栏/程序坞里也是独立一项。看起来就是个普通桌面程序，而不是「又开了个网页」。
# 找不到 Chrome/Edge 才退回普通浏览器标签页——功能一样，只是外观差一点。
_APP_BROWSERS = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path.home()) + "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "win32": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}


def _app_window(url: str) -> bool:
    """尽量开成独立应用窗口。成了返回 True。"""
    import shutil
    import subprocess

    candidates = list(_APP_BROWSERS.get(sys.platform, []))
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for exe in candidates:
        if not os.path.exists(exe) and not os.path.isabs(exe):
            continue
        if not os.path.exists(exe):
            continue
        try:
            subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1400,900",
                 "--disable-features=Translate"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            continue
    return False


def _open_when_up(url: str, timeout: float = 20.0) -> None:
    """等服务真起来再开窗口——早开会看到「无法连接」，用户以为程序坏了。"""
    port = url.rsplit(":", 1)[1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                break
        time.sleep(0.15)
    if not _app_window(url):
        webbrowser.open(url)


def main() -> int:
    # 同上：Windows 上不强制 UTF-8，启动横幅里的中文会让程序直接崩
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    settings.bootstrap()          # 必须在 import config 之前：config 在导入时读 env

    import config                                        # noqa: E402
    from app.server import app                           # noqa: E402
    from desktop import webui                            # noqa: E402

    webui.register(app)

    port = int(os.environ.get("DESKTOP_PORT") or _free_port())
    url = f"http://127.0.0.1:{port}"

    print("=" * 58)
    print("  平行语料对齐工作台 · 单机版")
    print("=" * 58)
    print(f"  界面地址：{url}")
    print(f"  设置文件：{settings.settings_path()}")
    print(f"  对齐模式：{'语义（bge-m3）' if config.SILICONFLOW_API_KEY else '离线（长度对齐）'}")
    print()
    print("  浏览器会自动打开。用完关掉这个窗口即可退出。")
    print("=" * 58, flush=True)

    if not os.environ.get("DESKTOP_NO_BROWSER"):
        threading.Thread(target=_open_when_up, args=(url,), daemon=True).start()

    # 藏掉 werkzeug 那句「不要用于生产环境」的红字警告：这里本来就不是服务器，
    # 只服务本机一个人，那行字只会让使用者以为自己装错了东西。
    import logging

    from flask import cli
    cli.show_server_banner = lambda *a, **k: None
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # 逐条请求日志也别刷屏

    try:
        # debug 关掉：reloader 在打包后会重新执行 exe，起两个进程
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
