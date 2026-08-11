"""Flask app: serves the UI and the search API.

Keyword/regex search works immediately over the full corpus. Semantic search
activates once a vector index exists and SILICONFLOW_API_KEY is set; otherwise
the endpoint returns a clear, non-fatal message.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import uuid
from datetime import timedelta

from flask import (Flask, jsonify, redirect, render_template, request,
                   session)

import config
from . import auth, mailer, quota, terms
from .analysis import analyze_set
from .semantic import SemanticIndex
from .store import CACHE, Store, load_store

RESULT_CAP = 20   # analyze and return the top 20 results

# 桌面版额外放行的非对齐路径（设置页与它的接口，见 desktop/webui.py）
_DESKTOP_PATHS = ("/favicon.svg", "/settings", "/api/settings", "/api/mode")

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def inject_version():
    """版本号即发布日期（CalVer），由部署脚本按提交自动写入 app/version.py。"""
    from .version import COMMIT, __version__
    return {"app_version": __version__, "app_commit": COMMIT,
            "desktop": config.DESKTOP}
app.permanent_session_lifetime = timedelta(days=30)


@app.before_request
def access_gate():
    """Site-wide access code (skipped entirely if ACCESS_CODE is unset).

    On ALIGN_HOST (a dedicated subdomain for people who only need the
    alignment tool, not corpus search), the site-wide code is bypassed
    entirely — only ALIGN_ACCESS_CODE (checked separately by align_gate)
    applies — and every non-align path is redirected/blocked so search is
    simply unreachable from that domain, regardless of a guessed URL.
    """
    if config.DESKTOP:
        # 单机版：进程只监听 127.0.0.1，使用者就是本机的人，门禁毫无意义。
        # 桌面版不带语料，检索/账号/后台这些路径一律不露出（与 ALIGN_HOST 同策略）。
        if _is_align_path(request.path) or request.path in _DESKTOP_PATHS:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "未找到 not found"}), 404
        return redirect("/align")
    host = (request.host or "").split(":")[0]
    if config.ALIGN_HOST and host == config.ALIGN_HOST:
        if request.path in ("/favicon.svg",) or _is_align_path(request.path):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "未找到 not found"}), 404
        return redirect("/align")
    # 主站门禁：只要建过账号就走账号登录；一个账号都没有时回退到站点访问码
    # ACCESS_CODE（向后兼容：部署新代码后、尚未建账号前，线上行为完全不变）。
    if auth.accounts_enabled():
        if auth.user_active(session.get("user")):
            return None
        if request.path in ("/login", "/logout", "/favicon.svg",
                            "/forgot", "/reset"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录 please log in"}), 401
        return render_template("login.html"), 401
    if not config.ACCESS_CODE or session.get("ok"):
        return None
    if request.path in ("/unlock", "/favicon.svg"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "未授权 unauthorized"}), 403
    return render_template("gate.html"), 401


@app.route("/unlock", methods=["POST"])
def unlock():
    if request.form.get("code", "") == config.ACCESS_CODE:
        session.permanent = True
        session["ok"] = True
        return redirect("/")
    return render_template("gate.html", error="访问码错误，请重试"), 401


# --- 账号登录（启用账号后主站的入口）--------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if auth.verify(username, password):
            session.permanent = True
            session["user"] = username
            return redirect("/")
        return render_template("login.html", error="用户名或密码错误，请重试"), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")


@app.get("/api/me")
def api_me():
    u = session.get("user")
    return jsonify({"user": u, "dept": auth.get_dept(u), "is_admin": auth.is_admin(u),
                    "email": auth.get_email(u), "mail_on": mailer.configured(),
                    "align_on": not config.DISABLE_ALIGN})


# --- 绑定邮箱（绑定后才能自助找回密码）------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


@app.post("/api/me/email/code")
def api_email_code():
    """向用户填写的邮箱发一个 6 位验证码，验证邮箱确实属于本人。"""
    u = session.get("user")
    if not auth.user_active(u):
        return jsonify({"error": "请先登录"}), 401
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip()
    if not _EMAIL_RE.match(email):
        return jsonify({"error": "邮箱格式不正确"}), 400
    other = auth.find_by_email(email)
    if other and other != u:
        return jsonify({"error": "该邮箱已被其他账号绑定"}), 400
    code = auth.make_email_code(u, email)
    ok, err = mailer.send(
        email, f"绑定邮箱验证码 · {config.MAIL_FROM_NAME}",
        f"你正在为账号「{u}」绑定此邮箱。\n\n验证码：{code}\n\n"
        f"验证码 10 分钟内有效。若非本人操作，请忽略本邮件。",
    )
    if not ok:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True})


@app.post("/api/me/email/verify")
def api_email_verify():
    u = session.get("user")
    if not auth.user_active(u):
        return jsonify({"error": "请先登录"}), 401
    code = (request.get_json(silent=True) or {}).get("code") or ""
    ok, val = auth.check_email_code(u, code)
    if not ok:
        return jsonify({"error": val}), 400
    auth.set_email(u, val)
    return jsonify({"ok": True, "email": val})


# --- 找回密码（需已绑定邮箱）----------------------------------------------
@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "GET":
        return render_template("forgot.html")
    ident = (request.form.get("ident") or "").strip()
    # 按用户名或邮箱找账号
    user = ident if auth.user_active(ident) else auth.find_by_email(ident)
    email = auth.get_email(user) if user else ""
    if user and email:
        token = auth.make_reset_token(user, config.RESET_TOKEN_TTL_MIN)
        link = f"{config.SITE_URL.rstrip('/')}/reset?token={token}"
        mailer.send(
            email, f"重置密码 · {config.MAIL_FROM_NAME}",
            f"账号「{user}」申请重置密码。\n\n请点击下面的链接设置新密码：\n{link}\n\n"
            f"链接 {config.RESET_TOKEN_TTL_MIN} 分钟内有效，只能使用一次。\n"
            f"若非本人操作，请忽略本邮件，你的密码不会被更改。",
        )
    # 无论账号是否存在都返回同样的提示——否则页面会变成"探测哪些账号存在"的工具
    return render_template("forgot.html", done=True)


@app.route("/reset", methods=["GET", "POST"])
def reset():
    token = (request.values.get("token") or "").strip()
    if request.method == "GET":
        if not auth.peek_reset_token(token):
            return render_template("reset.html", invalid=True), 400
        return render_template("reset.html", token=token)
    new = (request.form.get("password") or "").strip()
    if len(new) < 8:
        return render_template("reset.html", token=token, error="新密码至少 8 位"), 400
    user = auth.consume_reset_token(token)
    if not user:
        return render_template("reset.html", invalid=True), 400
    auth.set_password(user, new)
    return render_template("reset.html", done=True)


@app.post("/api/me/password")
def api_change_password():
    """用户自助修改密码：必须先验证当前密码，防止有人趁人离开电脑改掉密码。"""
    u = session.get("user")
    if not auth.user_active(u):
        return jsonify({"error": "请先登录"}), 401
    d = request.get_json(silent=True) or {}
    old = d.get("old") or ""
    new = (d.get("new") or "").strip()
    if not auth.verify(u, old):
        return jsonify({"error": "当前密码不正确"}), 403
    if len(new) < 8:
        return jsonify({"error": "新密码至少 8 位"}), 400
    if new == old:
        return jsonify({"error": "新密码不能与当前密码相同"}), 400
    auth.set_password(u, new)
    return jsonify({"ok": True})


# --- 账号管理后台（仅管理员）----------------------------------------------
def _gen_password() -> str:
    return secrets.token_urlsafe(9)


def _require_admin():
    u = session.get("user")
    if not (u and auth.is_admin(u)):
        return jsonify({"error": "需要管理员权限 admin only"}), 403
    return None


@app.get("/admin")
def admin_page():
    u = session.get("user")
    if not (u and auth.is_admin(u)):
        return redirect("/")
    return render_template("admin.html")


@app.get("/api/admin/usage")
def admin_usage():
    """当日各账号取走的句对数——用来发现异常抓取（谁在猛拉数据）。"""
    guard = _require_admin()
    if guard:
        return guard
    return jsonify({"usage": quota.snapshot(),
                    "daily_quota": config.DAILY_ROW_QUOTA,
                    "max_offset": config.MAX_SEARCH_OFFSET})


@app.get("/api/admin/users")
def admin_users():
    guard = _require_admin()
    if guard:
        return guard
    out = [{"username": x["username"], "dept": x.get("dept", ""),
            "email": x.get("email", ""),
            "active": x.get("active", True), "is_admin": x.get("is_admin", False)}
           for x in auth.list_users()]
    return jsonify({"users": out})


@app.post("/api/admin/users/email")
def admin_set_email():
    """管理员改/清除某账号的绑定邮箱（兜底：用户离职、邮箱失效、绑错了）。"""
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    email = (d.get("email") or "").strip()
    if email and not _EMAIL_RE.match(email):
        return jsonify({"error": "邮箱格式不正确"}), 400
    if auth.set_email(username, email):
        return jsonify({"ok": True})
    return jsonify({"error": f"无此用户：{username}"}), 404


@app.post("/api/admin/users/add")
def admin_add():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    dept = (d.get("dept") or "").strip()
    password = (d.get("password") or "").strip() or _gen_password()
    try:
        auth.add_user(username, password, dept, is_admin=bool(d.get("is_admin")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "username": username, "password": password})


@app.post("/api/admin/users/passwd")
def admin_passwd():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip() or _gen_password()
    if auth.set_password(username, password):
        return jsonify({"ok": True, "username": username, "password": password})
    return jsonify({"error": f"无此用户：{username}"}), 404


@app.post("/api/admin/users/active")
def admin_active():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    active = bool(d.get("active"))
    if username == session.get("user") and not active:
        return jsonify({"error": "不能停用自己的账号"}), 400
    if auth.set_active(username, active):
        return jsonify({"ok": True})
    return jsonify({"error": f"无此用户：{username}"}), 404


@app.post("/api/admin/users/role")
def admin_role():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    make_admin = bool(d.get("is_admin"))
    if username == session.get("user") and not make_admin:
        return jsonify({"error": "不能取消自己的管理员权限"}), 400
    if auth.set_admin(username, make_admin):
        return jsonify({"ok": True})
    return jsonify({"error": f"无此用户：{username}"}), 404


@app.post("/api/admin/users/remove")
def admin_remove():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    if username == session.get("user"):
        return jsonify({"error": "不能删除自己的账号"}), 400
    return jsonify({"ok": auth.remove_user(username)})


# --- 语料对齐工作台（/align）第二道访问码 ------------------------------
# 独立于站点门禁：过了站点门禁的人不一定能直接用 /align，还需再输一次
# ALIGN_ACCESS_CODE（置空则不启用，行为等同旧版）。同一个 session，记住 30 天。
_ALIGN_PREFIXES = ("/align", "/api/ocr", "/api/proof", "/api/align", "/api/clean")


def _is_align_path(path: str) -> bool:
    return path.startswith(_ALIGN_PREFIXES)


@app.before_request
def align_disabled_gate():
    """交付实例（DISABLE_ALIGN=1）：语料加工工具整体不可达，直接 404。
    比用访问码遮挡更彻底——不给客户留任何进入我方生产工具的口子。"""
    if config.DISABLE_ALIGN and _is_align_path(request.path):
        if request.path.startswith("/api/"):
            return jsonify({"error": "未找到 not found"}), 404
        return render_template("login.html"), 404
    return None


@app.before_request
def align_gate():
    if not config.ALIGN_ACCESS_CODE or session.get("align_ok"):
        return None
    if request.path in ("/align/unlock", "/favicon.svg"):
        return None
    if not _is_align_path(request.path):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "未授权：工作台需要单独访问码 unauthorized"}), 403
    return render_template("gate.html", title="语料对齐工作台",
                           sub="本工具需要单独的访问码", action="/align/unlock"), 401


@app.route("/align/unlock", methods=["POST"])
def align_unlock():
    if request.form.get("code", "") == config.ALIGN_ACCESS_CODE:
        session["align_ok"] = True
        return redirect("/align")
    return render_template("gate.html", title="语料对齐工作台",
                           sub="本工具需要单独的访问码", action="/align/unlock",
                           error="访问码错误，请重试"), 401

# 桌面版不带语料，也没有 data/ 目录可读——空库即可，检索路由在桌面版本来就不露出。
STORE = Store([]) if config.DESKTOP else load_store()
INDEX = SemanticIndex.load()       # None until scripts/build_index has run
_EMBEDDER = None                   # lazy: only built when semantic search is used
_EMBED_ERROR: str | None = None


def get_embedder():
    global _EMBEDDER, _EMBED_ERROR
    if _EMBEDDER is None and _EMBED_ERROR is None:
        try:
            from .embedder import default_embedder
            _EMBEDDER = default_embedder()
        except Exception as e:  # noqa: BLE001 - surface any setup error to the UI
            _EMBED_ERROR = str(e)
    return _EMBEDDER


@app.get("/translate")
def translate_page():
    return render_template("translate.html")


@app.post("/api/translate")
def api_translate():
    """用既有语料与术语库约束新文档的翻译，并核验术语是否照用。"""
    if not auth.user_active(session.get("user")) and auth.accounts_enabled():
        return jsonify({"error": "请先登录"}), 401
    if not config.DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置翻译模型 API key。"}), 400
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    src = d.get("src", "zh")
    if not text:
        return jsonify({"error": "请输入要翻译的内容。"}), 400
    if len(text) > 20000:
        return jsonify({"error": "单次最多 2 万字，请分批翻译。"}), 400
    # 计入语料取用配额：翻译会读取既往译法，同样属于语料使用
    qkey = session.get("user") or f"ip:{request.remote_addr}"
    allowed, _ = quota.check(qkey)
    if not allowed:
        return jsonify({"error": "今日用量已达上限，请明天再试。"}), 429

    from .translate import translate_document
    embedder = get_embedder()
    try:
        result = translate_document(text, src, STORE, INDEX, embedder)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"翻译失败：{e}"}), 502
    quota.consume(qkey, sum(len(s.get("tm_matches") or []) for s in result["segments"]))
    return jsonify(result)


@app.get("/api/terms")
def api_terms():
    """术语库检索：中心审定的标准译法（处室名 / 业务名 / 项目名）。"""
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang", "both")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 50, 0
    hits, total = terms.search(q, lang=lang, limit=limit, offset=offset)
    return jsonify({"terms": hits, "total": total, "offset": offset,
                    "has_more": offset + len(hits) < total})


# ---- 术语管理（仅管理员）----
@app.post("/api/admin/terms/add")
def admin_term_add():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    try:
        rec = terms.add(d.get("zh", ""), d.get("en", ""),
                        d.get("category", "综合"), d.get("status", "official"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "term": rec})


@app.post("/api/admin/terms/update")
def admin_term_update():
    guard = _require_admin()
    if guard:
        return guard
    d = request.get_json(silent=True) or {}
    try:
        tid = int(d.get("id", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "参数无效"}), 400
    if terms.update(tid, zh=d.get("zh"), en=d.get("en"),
                    category=d.get("category"), status=d.get("status")):
        return jsonify({"ok": True})
    return jsonify({"error": "无此术语"}), 404


@app.post("/api/admin/terms/remove")
def admin_term_remove():
    guard = _require_admin()
    if guard:
        return guard
    try:
        tid = int((request.get_json(silent=True) or {}).get("id", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "参数无效"}), 400
    return jsonify({"ok": terms.remove(tid)})


@app.get("/")
def home():
    return render_template("index.html")


_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#1c1917"/>'
    '<rect x="15" y="23" width="34" height="6.5" rx="3.25" fill="#b45830"/>'
    '<rect x="15" y="35" width="23" height="6.5" rx="3.25" fill="#d6a85f"/>'
    '<circle cx="45" cy="38.2" r="3" fill="#d6a85f"/>'
    '</svg>'
)


@app.get("/favicon.svg")
def favicon():
    return app.response_class(_FAVICON, mimetype="image/svg+xml")


@app.get("/api/corpora")
def corpora():
    indexed = set(INDEX.meta.get("corpora", [])) if INDEX else set()
    items = [{**c, "indexed": c["name"] in indexed} for c in STORE.corpora()]
    return jsonify({
        "corpora": items,
        "categories": STORE.categories(),
        "statuses": STORE.statuses(),
        "terms_count": terms.count(),
        "total_pairs": len(STORE.pairs),
        "semantic_ready": bool(INDEX),
        "semantic_count": INDEX.meta.get("count", 0) if INDEX else 0,
    })


@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "keyword")     # keyword | regex | semantic
    lang = request.args.get("lang", "both")
    case_sensitive = request.args.get("case", "") == "1"
    try:
        limit = min(int(request.args.get("limit", RESULT_CAP)), 100)
    except ValueError:
        limit = RESULT_CAP
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        offset = 0
    # --- 语料防抓取 -------------------------------------------------------
    # ① 限制翻页深度：不让脚本靠「显示更多」一路翻到底把整库倒出来
    if config.MAX_SEARCH_OFFSET and offset > config.MAX_SEARCH_OFFSET:
        return jsonify({"mode": mode, "hits": [], "count": 0, "has_more": False,
                        "error": f"为保护语料，单次检索最多查看前 {config.MAX_SEARCH_OFFSET} 条；"
                                 f"请缩小检索范围或换用更精确的检索词。"}), 403
    # ② 日配额：按账号（未登录时按 IP）累计取走的句对数
    qkey = session.get("user") or f"ip:{request.remote_addr}"
    allowed, remaining = quota.check(qkey)
    if not allowed:
        return jsonify({"mode": mode, "hits": [], "count": 0, "has_more": False,
                        "error": "今日检索量已达上限，请明天再试或联系管理员。"}), 429
    corpora_arg = request.args.get("corpora", "").strip()
    corpora = {c for c in corpora_arg.split("\n") if c} or None
    categories_arg = request.args.get("categories", "").strip()
    categories = {c for c in categories_arg.split("\n") if c} or None
    statuses_arg = request.args.get("statuses", "").strip()
    statuses = {c for c in statuses_arg.split("\n") if c} or None
    if not q:
        return jsonify({"mode": mode, "hits": [], "count": 0, "total": 0,
                        "offset": 0, "has_more": False})

    if mode == "semantic":
        if INDEX is None:
            return jsonify({"mode": mode, "hits": [], "count": 0,
                            "error": "语义索引尚未构建，请先运行 build_index。"})
        embedder = get_embedder()
        if embedder is None:
            return jsonify({"mode": mode, "hits": [], "count": 0,
                            "error": f"Embedding 不可用: {_EMBED_ERROR}"})
        # 语义检索的 total = 相似度达到相关阈值的条数（见 semantic.search 说明）
        hits, total, has_more = INDEX.search(q, embedder, STORE, top_k=limit,
                                             offset=offset, corpora=corpora,
                                             categories=categories, statuses=statuses)
    else:
        hits, total = STORE.keyword_search(
            q, lang=lang, regex=(mode == "regex"),
            case_sensitive=case_sensitive, corpora=corpora,
            categories=categories, statuses=statuses, offset=offset, limit=limit,
        )
        has_more = offset + len(hits) < total

    # 记入日配额（本次实际取走多少句对）
    quota.consume(qkey, len(hits))

    # Corpus-linguistics layer over the (top-N) Chinese side of the results.
    stats, per = analyze_set([h["zh"] for h in hits]) if hits else ({}, [])
    for h, p in zip(hits, per):
        h["ling"] = {"token_count": p["token_count"],
                     "content_ratio": p["content_ratio"], "pos": p["pos"]}
    return jsonify({"mode": mode, "query": q, "hits": hits,
                    "count": len(hits), "total": total, "offset": offset,
                    "has_more": has_more, "stats": stats,
                    "max_offset": config.MAX_SEARCH_OFFSET})


@app.get("/api/ask")
def ask():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"answer": "", "evidence": []})
    if INDEX is None:
        return jsonify({"error": "语义索引尚未构建，无法问答。"}), 400
    if not config.DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置 DeepSeek API key。"}), 400
    embedder = get_embedder()
    if embedder is None:
        return jsonify({"error": f"Embedding 不可用: {_EMBED_ERROR}"}), 400
    from .qa import ask as run_ask
    try:
        result = run_ask(q, STORE, INDEX, embedder)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"问答失败: {e}"}), 502
    return jsonify({"query": q, **result})


@app.get("/align")
def align_page():
    return render_template("align.html")


# --- OCR stage (async: upload PDF -> background GLM-OCR -> poll) -----------
OCR_JOBS: dict[str, dict] = {}


def _evict_finished(jobs: dict[str, dict], keep: int) -> None:
    """Drop oldest *finished* jobs beyond `keep`. Never evicts a running job —
    the old cap popped whatever was oldest, which could kill an in-progress job
    mid-poll (its client suddenly saw 404「无此任务」). Running jobs are bounded
    by actual concurrent use, so leaving them alone is safe."""
    finished = [k for k, v in jobs.items() if v.get("status") != "running"]
    for k in finished[:max(0, len(jobs) - keep)]:
        jobs.pop(k, None)


@app.post("/api/ocr/start")
def ocr_start():
    if not config.GLM_OCR_API_KEY:
        return jsonify({"error": "未配置 GLM-OCR key。"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "未上传 PDF。"}), 400
    pdf = f.read()
    try:
        start = max(1, int(request.form.get("start", 1)))
    except ValueError:
        start = 1
    end_s = request.form.get("end", "").strip()
    end = int(end_s) if end_s.isdigit() else None
    _evict_finished(OCR_JOBS, keep=8)      # 之前从不清理,文本任务会一直占内存
    job = uuid.uuid4().hex[:12]
    OCR_JOBS[job] = {"status": "running", "done": 0, "total": 0, "page": 0, "text": None, "error": None}

    def work():
        try:
            from .ocr import ocr_pdf
            def prog(d, t, p):
                OCR_JOBS[job].update(done=d, total=t, page=p)
            txt = ocr_pdf(pdf, start, end, progress=prog)
            OCR_JOBS[job].update(status="done", text=txt)
        except Exception as e:  # noqa: BLE001
            OCR_JOBS[job].update(status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job": job})


# --- OCR 图文对照校对(逐页:扫描原图 + OCR 文本) -----------------------
PROOF_JOBS: dict[str, dict] = {}


@app.post("/api/proof/start")
def proof_start():
    if not config.GLM_OCR_API_KEY:
        return jsonify({"error": "未配置 GLM-OCR key。"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "未上传 PDF。"}), 400
    pdf = f.read()
    try:
        start = max(1, int(request.form.get("start", 1)))
    except ValueError:
        start = 1
    end_s = request.form.get("end", "").strip()
    end = int(end_s) if end_s.isdigit() else None
    _evict_finished(PROOF_JOBS, keep=4)           # cap memory: drop oldest finished
    job = uuid.uuid4().hex[:12]
    PROOF_JOBS[job] = {"status": "running", "done": 0, "total": 0, "pages": [], "images": {}, "error": None}

    def work():
        try:
            from .ocr import glm_ocr, pdf_to_pages
            pages = pdf_to_pages(pdf, start, end, dpi=170)
            PROOF_JOBS[job]["total"] = len(pages)
            for k, (no, png) in enumerate(pages):
                PROOF_JOBS[job]["images"][no] = png
                try:
                    txt = glm_ocr(png)
                except Exception as e:  # noqa: BLE001
                    txt = f"(第 {no} 页 OCR 失败: {e})"
                PROOF_JOBS[job]["pages"].append({"no": no, "text": txt})
                PROOF_JOBS[job]["done"] = k + 1
            PROOF_JOBS[job]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            PROOF_JOBS[job].update(status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job": job})


@app.get("/api/proof/status")
def proof_status():
    j = PROOF_JOBS.get(request.args.get("job", ""))
    if not j:
        return jsonify({"error": "无此任务"}), 404
    # since=N:只回第 N 条之后新识别出的页。旧版每 1.5s 轮询都整包重发全部页文本,
    # 长书越到后面单次轮询越大;前端带上已渲染数即可增量拉取(不带则回全量,兼容旧行为)。
    try:
        since = max(0, int(request.args.get("since", 0)))
    except ValueError:
        since = 0
    return jsonify({"status": j["status"], "done": j["done"], "total": j["total"],
                    "pages": j["pages"][since:], "since": since, "error": j["error"]})


@app.post("/api/proof/retry")
def proof_retry():
    """单页重试:页图在 OCR 前已渲染并缓存,失败页(或识别质量差的页)可只重跑这一页,
    不必整篇重来。复用缓存页图,无需重新上传 PDF。"""
    data = request.get_json(silent=True) or {}
    j = PROOF_JOBS.get(data.get("job", ""))
    try:
        no = int(data.get("page", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "页码无效"}), 400
    if not j or no not in j.get("images", {}):
        return jsonify({"error": "该页原图已释放,请重新上传此 PDF 做 OCR。"}), 404
    from .ocr import glm_ocr, vlm_ocr, qwen_vl_ocr
    engine = (data.get("engine") or "auto").lower()
    png = j["images"][no]
    # 引擎链:智谱 OCR → 智谱 GLM-4V → 千问 Qwen2.5-VL(跨厂商,绕开智谱内容审核)
    ENGINES = {"ocr": glm_ocr, "vlm": vlm_ocr, "qwen": qwen_vl_ocr}
    if engine == "qwen":
        chain = ["qwen"]                 # 直接千问(智谱被敏感词拦时用)
    elif engine == "vlm":
        chain = ["vlm", "qwen"]          # GLM-4V → 千问
    else:
        chain = ["ocr", "vlm", "qwen"]   # auto:全链路依次兜底
    used, txt, errs = "", None, []
    for name in chain:
        try:
            txt, used = ENGINES[name](png), name
            break
        except Exception as e:  # noqa: BLE001 - 记下原因,继续下一个引擎
            errs.append(f"{name}:{e}")
    if txt is None:
        return jsonify({"ok": False, "no": no, "error": " ｜ ".join(errs)})
    for p in j["pages"]:
        if p["no"] == no:
            p["text"] = txt
            break
    return jsonify({"ok": True, "no": no, "text": txt, "engine": used})


@app.get("/api/proof/image")
def proof_image():
    j = PROOF_JOBS.get(request.args.get("job", ""))
    try:
        no = int(request.args.get("page", -1))
    except ValueError:
        return ("bad request", 400)
    if not j or no not in j["images"]:
        return ("not found", 404)
    from .ocr import img_mime
    return app.response_class(j["images"][no], mimetype=img_mime(j["images"][no]))


@app.post("/api/clean")
def api_clean():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"text": "", "report": {}})
    from .clean import clean_text
    cleaned, report = clean_text(text, data.get("opts") or None)
    return jsonify({"text": cleaned, "report": report})


@app.get("/api/ocr/status")
def ocr_status():
    j = OCR_JOBS.get(request.args.get("job", ""))
    if not j:
        return jsonify({"error": "无此任务"}), 404
    return jsonify(j)


@app.post("/api/align")
def api_align():
    data = request.get_json(silent=True) or {}
    if data.get("mode") == "single":
        from .align import separate_bilingual
        zh_text, en_text = separate_bilingual((data.get("text") or data.get("zh") or "").strip())
    else:
        zh_text = (data.get("zh") or "").strip()
        en_text = (data.get("en") or "").strip()
    if not zh_text or not en_text:
        return jsonify({"error": "未能得到中英两路文本（单文档模式请确保中英交替）。"}), 400
    embedder = get_embedder()
    if embedder is None and not config.DESKTOP:
        return jsonify({"error": f"Embedding 不可用: {_EMBED_ERROR}"}), 400
    # 桌面版没填 key 时 embedder 为 None，align() 自动改走离线长度对齐（不报错）。
    from .align import align as run_align
    try:
        result = run_align(zh_text, en_text, embedder)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"对齐失败: {e}"}), 502
    return jsonify(result)


@app.post("/api/align/ingest")
def api_align_ingest():
    """Add reviewed aligned pairs into the live corpus + vector index so they
    are immediately searchable on the platform.

    Both the in-memory store/index and their on-disk persistence are updated by
    *appending* the new rows, never by reloading the full corpus — this keeps
    the operation light enough for a small (1 GB) server.
    """
    global STORE, INDEX
    import numpy as np
    from .ingest import Pair
    from .semantic import IDS_PATH, META_PATH, VEC_PATH

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "对齐语料").strip()[:60] or "对齐语料"
    # 业务类别：入库时给这批句对打的类别标签。留空则按 config 映射兜底（回退语料库名）。
    category = config.category_of(name, (data.get("category") or "").strip()[:40])
    rows = [(p.get("zh", "").strip(), p.get("en", "").strip()) for p in (data.get("pairs") or [])]
    # 允许单语入库：只要有一侧有内容就保留（两侧都空才丢）。单语语料同样可检索，
    # 只是标明尚无对照译文，见 Pair.status。
    rows = [(z, e) for z, e in rows if z or e]
    if not rows:
        return jsonify({"error": "没有成对的句子可入库。"}), 400
    embedder = get_embedder()
    if embedder is None:
        return jsonify({"error": f"Embedding 不可用: {_EMBED_ERROR}"}), 400

    next_id = (max(STORE.by_id) + 1) if STORE.by_id else 0
    status = (data.get("status") or "official").strip() or "official"
    new_pairs = [Pair(next_id + i, name, name, i + 1, z, e,
                      category=category, status=status)
                 for i, (z, e) in enumerate(rows)]
    new_vecs = embedder.embed([(z or e) for z, e in rows])
    new_ids = np.asarray([p.id for p in new_pairs], dtype=np.int64)

    # 1) append to the corpus cache (line-delimited JSON; persists)
    with CACHE.open("a", encoding="utf-8") as f:
        for p in new_pairs:
            f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")

    # 2) append in memory, then persist the updated index from memory
    STORE.add_pairs(new_pairs)
    if INDEX is None:
        INDEX = SemanticIndex(new_vecs, new_ids,
                              {"model": config.EMBED_MODEL, "dim": config.EMBED_DIM,
                               "count": len(new_ids), "corpora": [name]})
    else:
        INDEX.append(new_vecs, new_ids, corpus=name)
    VEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(VEC_PATH, INDEX.vectors)
    np.save(IDS_PATH, INDEX.ids)
    META_PATH.write_text(json.dumps(INDEX.meta, ensure_ascii=False, indent=2))

    return jsonify({"ok": True, "added": len(rows), "corpus": name,
                    "total_pairs": len(STORE.pairs)})


@app.post("/api/align/export")
def api_align_export():
    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs") or []
    fmt = data.get("format", "tsv")
    if not pairs:
        return jsonify({"error": "无对齐数据。"}), 400
    import re as _re
    base = (data.get("name") or "aligned").strip()
    base = _re.sub(r"[\\/:*?\"<>|\n\r\t]", "", base)[:60] or "aligned"
    from .export import EXPORTERS
    exporter = EXPORTERS.get(fmt)
    if not exporter:
        return jsonify({"error": f"未知导出格式: {fmt}"}), 400
    content, mime, name = exporter(pairs, base)
    # HTTP 头只收 latin-1:中文文件名直接放 filename= 在部分服务器栈上会 500。
    # 标准做法(RFC 5987):ASCII 兜底名 + filename*=UTF-8'' 真名;前端 a.download 会再覆盖。
    from urllib.parse import quote
    ascii_name = _re.sub(r"[^A-Za-z0-9._-]", "_", name) or "export"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
    return app.response_class(content, mimetype=mime,
                              headers={"Content-Disposition": disp})


# --- AI 校验(后台任务:并发分批 → 轮询进度,判定增量返回)------------------
# 旧版是一个同步请求里顺序跑完所有批(每批间还 sleep 0.8s):几百行的文档要几分钟,
# 必撞 nginx/gunicorn 超时,整次校验作废。改成 OCR 同款 job+poll 模式。
CHECK_JOBS: dict[str, dict] = {}


@app.post("/api/align/check")
def api_align_check():
    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        return jsonify({"error": "无对齐数据。"}), 400
    if not config.DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置 DeepSeek API key。"}), 400
    _evict_finished(CHECK_JOBS, keep=6)
    job = uuid.uuid4().hex[:12]
    CHECK_JOBS[job] = {"status": "running", "done": 0, "total": len(pairs),
                       "verdicts": [None] * len(pairs), "error": None}

    def work():
        from .align import check_pairs
        j = CHECK_JOBS[job]

        def prog(done, total, verdicts):
            j.update(done=done, verdicts=list(verdicts))

        try:
            j["verdicts"] = check_pairs(pairs, progress=prog)
            j["status"] = "done"
            j["done"] = len(pairs)
        except Exception as e:  # noqa: BLE001
            j.update(status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job": job})


@app.get("/api/align/check/status")
def api_align_check_status():
    j = CHECK_JOBS.get(request.args.get("job", ""))
    if not j:
        return jsonify({"error": "无此任务"}), 404
    return jsonify(j)


@app.post("/api/align/llm")
def api_align_llm():
    """对选中的若干行用 LLM 重新对齐。把这些行的中/英文各自拼回整段、重新切句,
    交 DeepSeek 只决定分组(只输出编号),机器校验后按号回填原句。绝不改字。"""
    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        return jsonify({"error": "没有选中要重对齐的内容。"}), 400
    if not config.DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置 DeepSeek API key。"}), 400
    embedder = get_embedder()
    if embedder is None and not config.DESKTOP:
        return jsonify({"error": f"Embedding 不可用: {_EMBED_ERROR}"}), 400
    zh_text = "".join((p.get("zh") or "").strip() for p in pairs)
    en_text = " ".join((p.get("en") or "").strip() for p in pairs)
    from .align import llm_align
    try:
        d = llm_align(zh_text, en_text, embedder)
    except ValueError as e:                      # 校验不过 → 前端保留原对齐
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"AI 对齐失败: {e}"}), 502
    return jsonify(d)


@app.get("/api/ask_stream")
def ask_stream_route():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "empty"}), 400
    if INDEX is None:
        return jsonify({"error": "语义索引尚未构建。"}), 400
    if not config.DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置 DeepSeek API key。"}), 400
    embedder = get_embedder()
    if embedder is None:
        return jsonify({"error": f"Embedding 不可用: {_EMBED_ERROR}"}), 400

    from .qa import ask_stream

    def gen():
        try:
            for kind, payload in ask_stream(q, STORE, INDEX, embedder):
                yield f"data: {json.dumps({'kind': kind, 'payload': payload}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'kind': 'error', 'payload': str(e)})}\n\n"
        yield "data: {\"kind\": \"done\"}\n\n"

    r = app.response_class(gen(), mimetype="text/event-stream")
    r.headers["X-Accel-Buffering"] = "no"   # tell nginx not to buffer the stream
    r.headers["Cache-Control"] = "no-cache"
    return r


# --- 溯源：按语料条目取回它在原始材料中的位置与原件 ----------------------
# 语料的 src_ref 记录了出处（page:12 / frame:41@80s / time:204.5-209.2s）。
# 本端点据此返回原页图或视频帧，供人工核对 OCR、清洗、对齐是否有误。
# 音频类（time:）暂只返回时间信息，前端可据此定位播放位置。
@app.get("/api/source")
def api_source():
    """?id=<pair_id>  → 返回该条语料的溯源信息；&raw=1 时直接返回原件图片。"""
    try:
        pid = int(request.args.get("id", -1))
    except ValueError:
        return jsonify({"error": "参数无效"}), 400
    p = STORE.by_id.get(pid)
    if p is None:
        return jsonify({"error": "无此语料"}), 404
    ref = (p.src_ref or "").strip()
    info = {"id": pid, "corpus": p.corpus, "src_ref": ref,
            "kind": None, "image": None, "time": None}
    base = config.DATA_DIR / "sources" / p.corpus
    if ref.startswith("page:"):
        info["kind"] = "page"
        info["page"] = ref.split(":", 1)[1]
        f = base / f"page_{info['page']}.jpg"
        info["image"] = f"/api/source?id={pid}&raw=1" if f.exists() else None
    elif ref.startswith("frame:"):
        info["kind"] = "frame"
        body = ref.split(":", 1)[1]
        num = body.split("@")[0]
        info["frame"] = num
        info["time"] = body.split("@")[1] if "@" in body else None
        f = base / f"frame_{num}.jpg"
        info["image"] = f"/api/source?id={pid}&raw=1" if f.exists() else None
    elif ref.startswith("img:"):
        info["kind"] = "img"
        info["img"] = ref.split(":", 1)[1]
        f = base / f"img_{info['img']}.jpg"
        info["image"] = f"/api/source?id={pid}&raw=1" if f.exists() else None
    elif ref.startswith("time:"):
        info["kind"] = "time"
        info["time"] = ref.split(":", 1)[1]

    if request.args.get("raw") == "1":
        f = None
        if info["kind"] == "page":
            f = base / f"page_{info['page']}.jpg"
        elif info["kind"] == "frame":
            f = base / f"frame_{info['frame']}.jpg"
        elif info["kind"] == "img":
            f = base / f"img_{info['img']}.jpg"
        if f is None or not f.exists():
            return jsonify({"error": "原件不存在"}), 404
        from .ocr import img_mime
        data = f.read_bytes()
        return app.response_class(data, mimetype=img_mime(data))
    return jsonify(info)


@app.get("/api/context")
def context():
    try:
        pair_id = int(request.args.get("id", -1))
        radius = min(int(request.args.get("radius", 5)), 20)
    except ValueError:
        return jsonify({"error": "参数无效"}), 400
    # 上下文按 id 返回**原文顺序**的相邻句对，是比检索更好用的批量抓取通道
    # （循环 id 就能按顺序把整库倒出来），因此同样计入日配额。
    qkey = session.get("user") or f"ip:{request.remote_addr}"
    allowed, _ = quota.check(qkey)
    if not allowed:
        return jsonify({"lines": [],
                        "error": "今日检索量已达上限，请明天再试或联系管理员。"}), 429
    lines = STORE.context(pair_id, radius=radius)
    quota.consume(qkey, len(lines))
    return jsonify({"lines": lines})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
