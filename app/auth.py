"""账号系统：用户存于 data/users.json（不进 git），密码 pbkdf2 加盐哈希。

设计要点：只要存在任一启用中的用户，就视为「启用账号登录」——此时主站检索平台
要求用户名 + 密码登录；若一个用户都没有，则回退到站点访问码 ACCESS_CODE（向后
兼容：部署代码后、尚未建账号前，线上行为与旧版完全一致）。

users.json 变更会按文件 mtime 自动热加载，新增/改密后无需重启进程即可生效。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

import config

USERS_PATH = config.DATA_DIR / "users.json"
_ITER = 200_000          # pbkdf2 迭代次数
_ALGO = "sha256"

# 内存缓存：按 users.json 的 mtime 决定是否重新读盘。
_cache: dict = {"mtime": None, "users": {}}


def _read_raw() -> dict:
    if not USERS_PATH.exists():
        return {"users": []}
    try:
        return json.loads(USERS_PATH.read_text(encoding="utf-8") or "{}") or {"users": []}
    except json.JSONDecodeError:
        return {"users": []}


def _users() -> dict[str, dict]:
    """返回 {用户名: 记录}，文件变动时自动重载（不需重启进程）。"""
    mtime = USERS_PATH.stat().st_mtime if USERS_PATH.exists() else None
    if mtime != _cache["mtime"]:
        data = _read_raw()
        _cache["users"] = {u["username"]: u for u in data.get("users", [])}
        _cache["mtime"] = mtime
    return _cache["users"]


def accounts_enabled() -> bool:
    """是否启用账号登录：存在至少一个启用中的用户。"""
    return any(u.get("active", True) for u in _users().values())


def _hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                               bytes.fromhex(salt_hex), _ITER).hex()


def verify(username: str, password: str) -> bool:
    """校验用户名 + 密码。恒定时间比较，避免时序侧信道。"""
    u = _users().get(username)
    if not u or not u.get("active", True):
        return False
    return hmac.compare_digest(_hash(password, u["salt"]), u["hash"])


def user_active(username: str | None) -> bool:
    """会话校验用：该用户仍存在且启用中（管理员删号后旧会话即失效）。"""
    if not username:
        return False
    u = _users().get(username)
    return bool(u and u.get("active", True))


def get_dept(username: str | None) -> str:
    if not username:
        return ""
    return (_users().get(username) or {}).get("dept", "")


def get_email(username: str | None) -> str:
    if not username:
        return ""
    return (_users().get(username) or {}).get("email", "")


def find_by_email(email: str) -> str | None:
    """按邮箱反查用户名（找回密码用）。邮箱不区分大小写。"""
    e = (email or "").strip().lower()
    if not e:
        return None
    for u in _users().values():
        if (u.get("email") or "").lower() == e and u.get("active", True):
            return u["username"]
    return None


def set_email(username: str, email: str) -> bool:
    email = (email or "").strip()
    users = list_users()
    for u in users:
        if u["username"] == username:
            u["email"] = email
            _write(users)
            return True
    return False


def is_admin(username: str | None) -> bool:
    if not username:
        return False
    u = _users().get(username)
    return bool(u and u.get("active", True) and u.get("is_admin", False))


# ---- 邮箱验证码 / 找回密码令牌 ------------------------------------------------
# 都放内存（单 worker）+ 短时效 + 一次性 + 限尝试次数；重启失效可接受。
_codes: dict[str, dict] = {}    # 绑定邮箱验证码 {username: {...}}
_resets: dict[str, dict] = {}   # 找回密码令牌 {token: {...}}


def make_email_code(username: str, email: str, ttl_sec: int = 600) -> str:
    code = f"{secrets.randbelow(1000000):06d}"
    _codes[username] = {"code": code, "email": email.strip(),
                        "exp": time.time() + ttl_sec, "tries": 0}
    return code


def check_email_code(username: str, code: str) -> tuple[bool, str]:
    """校验绑定验证码，成功则返回 (True, 邮箱)。"""
    rec = _codes.get(username)
    if not rec:
        return False, "请先获取验证码"
    if time.time() > rec["exp"]:
        _codes.pop(username, None)
        return False, "验证码已过期，请重新获取"
    rec["tries"] += 1
    if rec["tries"] > 5:
        _codes.pop(username, None)
        return False, "尝试次数过多，请重新获取验证码"
    if not hmac.compare_digest(rec["code"], (code or "").strip()):
        return False, "验证码不正确"
    _codes.pop(username, None)
    return True, rec["email"]


def make_reset_token(username: str, ttl_min: int) -> str:
    token = secrets.token_urlsafe(32)
    _resets[token] = {"user": username, "exp": time.time() + ttl_min * 60}
    return token


def peek_reset_token(token: str) -> str | None:
    """只看令牌是否有效（渲染重置页用），不消费。"""
    rec = _resets.get(token or "")
    if not rec or time.time() > rec["exp"]:
        _resets.pop(token or "", None)
        return None
    return rec["user"]


def consume_reset_token(token: str) -> str | None:
    """校验并作废令牌（一次性），返回用户名。"""
    user = peek_reset_token(token)
    if user:
        _resets.pop(token, None)
    return user


# ---- 管理操作（供 scripts/user_admin.py 调用）---------------------------------
def list_users() -> list[dict]:
    return _read_raw().get("users", [])


def _write(users: list[dict]) -> None:
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_PATH.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _cache["mtime"] = None  # 迫使下次读盘重载


def add_user(username: str, password: str, dept: str = "",
             is_admin: bool = False) -> None:
    username = username.strip()
    if not username or not password:
        raise ValueError("用户名和密码都不能为空")
    users = list_users()
    if any(u["username"] == username for u in users):
        raise ValueError(f"用户已存在：{username}")
    salt = os.urandom(16).hex()
    users.append({
        "username": username,
        "dept": dept.strip(),
        "salt": salt,
        "hash": _hash(password, salt),
        "active": True,
        "is_admin": bool(is_admin),
        "created": int(time.time()),
    })
    _write(users)


def set_admin(username: str, admin: bool) -> bool:
    users = list_users()
    for u in users:
        if u["username"] == username:
            u["is_admin"] = bool(admin)
            _write(users)
            return True
    return False


def set_password(username: str, password: str) -> bool:
    if not password:
        raise ValueError("密码不能为空")
    users = list_users()
    for u in users:
        if u["username"] == username:
            u["salt"] = os.urandom(16).hex()
            u["hash"] = _hash(password, u["salt"])
            _write(users)
            return True
    return False


def set_active(username: str, active: bool) -> bool:
    users = list_users()
    for u in users:
        if u["username"] == username:
            u["active"] = active
            _write(users)
            return True
    return False


def remove_user(username: str) -> bool:
    users = list_users()
    kept = [u for u in users if u["username"] != username]
    if len(kept) == len(users):
        return False
    _write(kept)
    return True
