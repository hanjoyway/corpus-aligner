"""桌面版冒烟测试：不启服务器、不联网，直接用 Flask test client 走一遍关键路径。

CI 在打包**之前**跑它——打包要好几分钟，代码本身就跑不起来的话没必要等。

    python -m desktop.selftest
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows 控制台默认不是 UTF-8，打印中文会 UnicodeEncodeError，先强制过来
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

CASES: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CASES.append((name, bool(ok)))
    print(f"  {'通过' if ok else '失败'}  {name}{'  — ' + detail if detail else ''}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="corpus-aligner-selftest-")
    os.environ["CORPUS_ALIGNER_HOME"] = tmp     # 别碰使用者真实的设置文件

    from desktop import settings
    settings.bootstrap()

    import config
    from app.server import app
    from desktop import webui
    webui.register(app)

    print("桌面版冒烟测试")
    check("桌面模式已开启", config.DESKTOP)
    check("未读取开发机 .env", not config.SILICONFLOW_API_KEY,
          "打包版绝不能带着别人的 key 出门")

    c = app.test_client()

    r = c.get("/")
    check("根路径跳转到工作台", r.status_code == 302 and "/align" in r.headers.get("Location", ""))

    r = c.get("/align")
    body = r.get_data(as_text=True)
    check("工作台页面可打开", r.status_code == 200)
    check("模板渲染正常", "对齐工作台" in body)
    check("桌面版隐藏了入库按钮", 'data-op="ingest"' not in body)
    check("桌面版露出设置入口", 'href="/settings"' in body)

    check("设置页可打开", c.get("/settings").status_code == 200)
    check("检索接口不可达", c.get("/api/search?q=x").status_code == 404)
    check("管理后台不可达", c.get("/admin").status_code == 302)

    zh = ("世界贸易组织成立于1995年。该组织负责监督成员之间的贸易规则。"
          "截至2022年，其成员已超过一百六十个。")
    en = ("The World Trade Organization was founded in 1995. "
          "The organization oversees the rules of trade between its members. "
          "As of 2022, it had more than one hundred and sixty members.")
    r = c.post("/api/align", json={"zh": zh, "en": en})
    d = r.get_json() or {}
    beads = d.get("beads") or []
    check("无 key 时自动走离线对齐", d.get("mode") == "offline")
    check("离线对齐结果正确", len(beads) == 3 and all(b["type"] == "1:1" for b in beads),
          f"得到 {[b['type'] for b in beads]}")

    r = c.post("/api/align", json={"mode": "single", "text": zh + "\n\n" + en})
    check("单文档模式能自动分中英", len((r.get_json() or {}).get("beads") or []) > 0)

    pairs = [{"zh": "中心成立于1989年。", "en": "The Center was founded in 1989."}]
    for fmt in ("tmx", "xlsx", "docx", "rtf", "txt", "jianku"):
        r = c.post("/api/align/export", json={"format": fmt, "pairs": pairs})
        check(f"导出 {fmt}", r.status_code == 200 and len(r.get_data()) > 0)

    r = c.post("/api/settings", json={"SILICONFLOW_API_KEY": "sk-selftest"})
    check("保存密钥后立即切到语义档",
          r.status_code == 200 and (c.get("/api/mode").get_json() or {}).get("semantic") is True)
    check("设置页不回显密钥本身", "sk-selftest" not in c.get("/settings").get_data(as_text=True))
    c.post("/api/settings", json={"_clear": True})
    check("清空后退回离线档",
          (c.get("/api/mode").get_json() or {}).get("semantic") is False)

    bad = [n for n, ok in CASES if not ok]
    print(f"\n{len(CASES) - len(bad)}/{len(CASES)} 通过")
    if bad:
        print("失败项：" + "、".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
