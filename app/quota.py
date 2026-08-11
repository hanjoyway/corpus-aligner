"""语料防抓取：按账号（未登录时按 IP）限制每天能取走的句对总量。

威胁模型不是"黑客攻进来"，而是**拿到合法账号的人写脚本把语料一条条翻走**。
纯 IP 限流挡不住他（他可以慢慢刷），所以这里按账号累计"取走了多少句对"，
超过日配额即拒绝——正常人工检索一天用不了几百条，脚本几分钟就会撞上限。

计数在内存（gunicorn 单 worker + 多线程，加锁即可）；进程重启后清零，
这对"拖慢批量抓取"的目的已经足够，不需要引入数据库。
"""
from __future__ import annotations

import threading
import time

import config

_lock = threading.Lock()
# {key: {"day": <epoch//86400>, "rows": 已取句对数, "reqs": 请求数, "warned": bool}}
_usage: dict[str, dict] = {}


def _today() -> int:
    return int(time.time()) // 86400


def _bucket(key: str) -> dict:
    b = _usage.get(key)
    day = _today()
    if b is None or b["day"] != day:
        b = {"day": day, "rows": 0, "reqs": 0, "warned": False}
        _usage[key] = b
    return b


def check(key: str) -> tuple[bool, int]:
    """检索前调用：返回 (是否放行, 今日剩余可取句对数)。"""
    if config.DAILY_ROW_QUOTA <= 0:          # 0 = 不限制
        return True, 10**9
    with _lock:
        b = _bucket(key)
        remaining = config.DAILY_ROW_QUOTA - b["rows"]
        return remaining > 0, max(remaining, 0)


def consume(key: str, rows: int) -> int:
    """检索后调用：累计本次取走的句对数，返回今日已用量。"""
    if config.DAILY_ROW_QUOTA <= 0:
        return 0
    with _lock:
        b = _bucket(key)
        b["rows"] += max(rows, 0)
        b["reqs"] += 1
        used = b["rows"]
        # 首次超限时打一条日志，便于 journalctl 里发现异常账号
        if used >= config.DAILY_ROW_QUOTA and not b["warned"]:
            b["warned"] = True
            print(f"[quota] 达到日配额: key={key} rows={used} reqs={b['reqs']}",
                  flush=True)
        return used


def snapshot() -> list[dict]:
    """当日各账号用量快照（管理后台查看，便于发现异常抓取）。"""
    day = _today()
    with _lock:
        return sorted(
            ({"key": k, "rows": v["rows"], "reqs": v["reqs"]}
             for k, v in _usage.items() if v["day"] == day),
            key=lambda x: -x["rows"],
        )
