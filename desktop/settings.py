"""桌面版的用户设置。

API key 存在**用户目录**，不在程序包里——这样打包出去的 .app/.exe 不含任何
凭据，谁拿到都不会用到别人的额度；每个人第一次打开时在应用内自己填。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

APP_NAME = "CorpusAligner"

# 三个可选的 key。全部留空也能用——对齐会走离线长度算法，只是 OCR 与 AI 校验
# 这两项本质上就是调别人的模型，没有 key 无从谈起。
FIELDS = [
    {
        "env": "SILICONFLOW_API_KEY",
        "label": "SiliconFlow（硅基流动）",
        "use": "语义对齐（bge-m3 向量）。不填则自动改用离线长度对齐。",
        "url": "https://cloud.siliconflow.cn",
    },
    {
        "env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek（深度求索）",
        "use": "AI 校验、AI 重对齐。不填则这两个按钮不可用。",
        "url": "https://platform.deepseek.com",
    },
    {
        "env": "GLM_OCR_API_KEY",
        "label": "智谱 GLM",
        "use": "扫描件 OCR 图文校对。不填则只能粘贴/导入已有文本。",
        "url": "https://open.bigmodel.cn",
    },
]


def config_dir() -> Path:
    """各平台放用户配置的常规位置——不要写进程序包，打包后那是只读临时目录。"""
    override = os.environ.get("CORPUS_ALIGNER_HOME")
    if override:                      # 自测用，免得动到使用者真正的设置
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "corpus-aligner"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load() -> dict:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save(values: dict) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    cur = load()
    cur.update({k: v for k, v in values.items()})
    p = settings_path()
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    try:                       # 里面是 key，别让同机其他用户读到
        p.chmod(0o600)
    except OSError:
        pass
    apply_env(cur)


def apply_env(values: dict) -> None:
    """把设置写进环境变量 + 已导入的 config 模块。

    config 在导入时读一次 env，所以运行中改 key 必须两处都动；各调用点都是
    在用的时候现读 ``config.XXX``，改完立即生效，不用重启。
    """
    for f in FIELDS:
        v = (values.get(f["env"]) or "").strip()
        if v:
            os.environ[f["env"]] = v
    if "config" in sys.modules:
        cfg = sys.modules["config"]
        for f in FIELDS:
            v = (values.get(f["env"]) or "").strip()
            if v:
                setattr(cfg, f["env"], v)


def bootstrap() -> dict:
    """在 import config 之前调用：铺好桌面版需要的环境变量。"""
    values = load()
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.environ["DESKTOP_MODE"] = "1"
    # 语料/术语等可写数据放用户目录；程序包在打包后是只读的临时解压目录。
    os.environ.setdefault("DESKTOP_DATA_DIR", str(d / "data"))
    # 会话签名密钥：本机自用，随机生成一次存下来即可。
    key = values.get("SECRET_KEY")
    if not key:
        key = secrets.token_hex(32)
        values["SECRET_KEY"] = key
        save(values)
    os.environ["SECRET_KEY"] = key
    # 桌面版没有门禁，显式清空，免得继承到开发机 .env 里的访问码。
    os.environ["ACCESS_CODE"] = ""
    os.environ["ALIGN_ACCESS_CODE"] = ""
    os.environ["DISABLE_ALIGN"] = ""
    apply_env(values)
    return values


def status() -> dict:
    """给设置页用：每个 key 填没填（**绝不回传 key 本身**）。"""
    values = load()
    return {f["env"]: bool((values.get(f["env"]) or "").strip()) for f in FIELDS}
