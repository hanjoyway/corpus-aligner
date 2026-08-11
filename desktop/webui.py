"""桌面版专属路由：设置页 + 运行模式查询。

挂在既有的 Flask app 上，服务器端一行都不用改——线上跑的时候
``config.DESKTOP`` 为假，这个模块根本不会被导入。
"""
from __future__ import annotations

from flask import jsonify, render_template, request

import config

from . import settings


def register(app) -> None:
    @app.get("/settings")
    def desktop_settings_page():
        return render_template("settings.html", fields=settings.FIELDS,
                               status=settings.status(),
                               path=settings.settings_path())

    @app.post("/api/settings")
    def desktop_settings_save():
        data = request.get_json(silent=True) or {}
        allowed = {f["env"] for f in settings.FIELDS}
        if data.get("_clear"):
            settings.save({k: "" for k in allowed})
            for k in allowed:
                setattr(config, k, "")
        else:
            vals = {k: v for k, v in data.items() if k in allowed and isinstance(v, str)}
            if not vals:
                return jsonify({"error": "没有可保存的内容"}), 400
            settings.save(vals)
        _reset_embedder()
        return jsonify({"ok": True, "status": settings.status()})

    @app.get("/api/mode")
    def desktop_mode():
        """工作台顶栏用它显示当前是语义对齐还是离线对齐。"""
        return jsonify({
            "semantic": bool(config.SILICONFLOW_API_KEY),
            "deepseek": bool(config.DEEPSEEK_API_KEY),
            "ocr": bool(config.GLM_OCR_API_KEY),
        })


def _reset_embedder() -> None:
    """改完 key 立刻生效：清掉 server 里缓存的 embedder 与上次的失败记录。"""
    from app import server
    server._EMBEDDER = None
    server._EMBED_ERROR = None
