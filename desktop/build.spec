# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：macOS 出 .app，Windows 出单文件 .exe。

    pyinstaller desktop/build.spec --noconfirm

刻意**不打包**的东西：.env（开发机的密钥）、data/（语料与账号）、汉英平行语料
库资源/。单机版的密钥由使用者自己在应用里填，见 desktop/settings.py。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# 只带桌面版会用到的两个模板。其余模板是交付实例的品牌页面（首页 / 登录 /
# 辅助翻译，含客户名称），桌面版一个都渲染不到，不该跟着程序包发出去。
datas = [
    (os.path.join(ROOT, "app", "templates", t), os.path.join("app", "templates"))
    for t in ("align.html", "settings.html")
]
datas += collect_data_files("jieba")        # 分词词典，缺了 analysis.py 直接报错

hiddenimports = [
    "zhipuai",          # OCR，只在函数里 import，静态分析容易漏
    "httpx", "anyio", "sniffio",
    "openpyxl", "docx",
]

a = Analysis(
    [os.path.join(ROOT, "desktop", "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "pytest", "IPython", "notebook",
        "pandas", "scipy", "torch", "transformers",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = None
if IS_MAC and os.path.exists(os.path.join(ROOT, "desktop", "icons", "app.icns")):
    icon = os.path.join(ROOT, "desktop", "icons", "app.icns")
elif IS_WIN and os.path.exists(os.path.join(ROOT, "desktop", "icons", "app.ico")):
    icon = os.path.join(ROOT, "desktop", "icons", "app.ico")

if IS_WIN:
    # Windows 交单个 exe 最省事（作业压缩包里就一个文件）。启动时解压到临时目录，
    # 头一次打开会慢几秒。
    # console=False：黑色控制台窗口会让人觉得这是个脚本而不是应用。程序改为
    # 用浏览器的应用窗口模式（无地址栏）呈现，关掉窗口即退出；启动信息写进
    # 用户目录的 run.log，出问题时还能查。
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="CorpusAligner",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
        runtime_tmpdir=None, console=False, icon=icon,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="CorpusAligner",
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=False, icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
                   name="CorpusAligner")
    if IS_MAC:
        app = BUNDLE(
            coll,
            name="CorpusAligner.app",
            icon=icon,
            bundle_identifier="cn.ai4language.corpusaligner",
            info_plist={
                "CFBundleDisplayName": "平行语料对齐工作台",
                "CFBundleName": "语料对齐",
                "NSHighResolutionCapable": True,
                # 本机自用的小服务，不走 https，得允许明文回环连接
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
                "LSMinimumSystemVersion": "11.0",
            },
        )
