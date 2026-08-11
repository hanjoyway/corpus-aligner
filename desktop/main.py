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


def _log(msg: str) -> None:
    """打印到控制台（有的话）并always 写进日志文件。

    打包成无控制台的窗口程序后 sys.stdout 可能是 None，直接 print 会抛异常；
    出了问题也没地方看，所以始终往用户目录里写一份日志。
    """
    line = str(msg)
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except (OSError, AttributeError, ValueError):
        pass
    try:
        with open(settings.config_dir() / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _alert(title: str, msg: str) -> None:
    """没有控制台可看时，用系统原生对话框把话说给用户听。"""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e",
                            f'display dialog {msg!r} with title {title!r} buttons {{"好"}}'],
                           check=False)
    except Exception:                                   # noqa: BLE001
        pass


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


def _win_registry_browsers() -> list[str]:
    """从注册表里问 Windows 要浏览器的真实安装路径。

    写死 Program Files 那几条路径靠不住：Chrome 常被装进
    %LOCALAPPDATA%（用户级安装，不需要管理员），Edge 的位置也随版本变过。
    App Paths 是 Windows 官方登记程序位置的地方，问它最准。
    """
    if sys.platform != "win32":
        return []
    found = []
    try:
        import winreg
    except ImportError:
        return []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for name in ("msedge.exe", "chrome.exe", "chromium.exe"):
            try:
                key = winreg.OpenKey(
                    root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}")
                with key:
                    path, _ = winreg.QueryValueEx(key, "")
                if path:
                    found.append(path.strip('"'))
            except OSError:
                continue
    return found


def _app_window(url: str):
    """尽量开成独立应用窗口（无地址栏、无标签页，任务栏里独立一项）。

    返回浏览器进程对象，失败返回 None。用独立的 user-data-dir 有两个作用：
    一是这个进程不会被系统里已开着的浏览器接管（否则它一瞬间就退出，我们无从
    得知窗口何时关闭），二是关掉窗口即可退出整个程序——这才像个应用，
    而不是「关了窗口后台还留着一个不知道怎么关的东西」。
    """
    import shutil
    import subprocess

    candidates = _win_registry_browsers() + list(_APP_BROWSERS.get(sys.platform, []))
    if sys.platform == "win32":
        # 用户级安装的 Chrome / Edge 在这里，注册表若没登记还能兜住
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates += [
                os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(local, r"Chromium\Application\chrome.exe"),
            ]
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen = set()
    for exe in candidates:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        if not os.path.exists(exe):
            continue
        try:
            kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            profile = settings.config_dir() / "browser"
            proc = subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1400,900",
                 f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-features=Translate,ChromeWhatsNewUI"], **kwargs)
            _log(f"已用应用窗口模式打开：{exe}")
            return proc
        except OSError:
            continue
    _log("没找到 Chrome/Edge，退回默认浏览器标签页")
    return None


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
    proc = _app_window(url)
    if proc is not None:
        proc.wait()               # 窗口关了就退出整个程序，跟普通桌面应用一样
        _log("窗口已关闭，程序退出")
        os._exit(0)
        return
    # 退回浏览器标签页：此时没有窗口可等，只能提示用户怎么退出
    webbrowser.open(url)
    _alert("平行语料对齐工作台",
           f"程序已在本机启动：\n{url}\n\n"
           "本机未找到 Edge 或 Chrome，已改用默认浏览器打开。\n"
           "用完后请关闭本程序的图标或在任务管理器中结束 CorpusAligner。")


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

    _log("=" * 58)
    _log("  平行语料对齐工作台 · 单机版")
    _log(f"  界面地址：{url}")
    _log(f"  设置文件：{settings.settings_path()}")
    _log(f"  对齐模式：{'语义（bge-m3）' if config.SILICONFLOW_API_KEY else '离线（长度对齐）'}")
    _log("=" * 58)

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
