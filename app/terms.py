"""术语库：中心审定的标准译法（处室名、业务名、项目名等）。

与句对语料分开存放，因为二者单位与用途都不同：
  · 句对 → 看一个说法在完整句子里怎么用
  · 术语 → 直接查"这个名称的标准英文是什么"
术语还会被翻译功能用作强制约束（见 app/translate.py）。

存储：data/terms.jsonl，一行一条。按 mtime 热加载，管理端改完即时生效。
"""
from __future__ import annotations

import json
import re
import threading

import config

TERMS_PATH = config.DATA_DIR / "terms.jsonl"
_lock = threading.Lock()
_cache: dict = {"mtime": None, "items": []}


def _load() -> list[dict]:
    mtime = TERMS_PATH.stat().st_mtime if TERMS_PATH.exists() else None
    if mtime != _cache["mtime"]:
        items = []
        if TERMS_PATH.exists():
            for line in TERMS_PATH.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        _cache["items"] = items
        _cache["mtime"] = mtime
    return _cache["items"]


def all_terms() -> list[dict]:
    return list(_load())


def count() -> int:
    return len(_load())


def search(query: str, *, lang: str = "both", limit: int = 50,
           offset: int = 0) -> tuple[list[dict], int]:
    """按中文或英文子串查术语（不区分大小写）。返回 (当前页, 总数)。"""
    items = _load()
    q = (query or "").strip().lower()
    if not q:
        # 空查询：返回全部（按中文排序），便于浏览整个术语表
        hits = sorted(items, key=lambda t: t.get("zh", ""))
    else:
        hits = []
        for t in items:
            zh = (t.get("zh") or "").lower()
            en = (t.get("en") or "").lower()
            if (lang in ("zh", "both") and q in zh) or \
               (lang in ("en", "both") and q in en):
                hits.append(t)
    return hits[offset:offset + limit], len(hits)


def match_in_text(text: str) -> list[dict]:
    """找出文本中出现的所有术语——翻译功能据此强制使用审定译法。

    纯本地子串匹配，不调任何接口。长术语优先（避免"认证处"盖住
    "国（境）外学历学位认证一处"），且已匹配区间不再重复命中。
    """
    if not text:
        return []
    found, taken = [], []
    for t in sorted(_load(), key=lambda x: -len(x.get("zh", ""))):
        zh = t.get("zh") or ""
        if len(zh) < 2 or not t.get("en"):
            continue
        for m in re.finditer(re.escape(zh), text):
            span = (m.start(), m.end())
            if any(span[0] < b and a < span[1] for a, b in taken):
                continue          # 与更长的术语重叠，跳过
            taken.append(span)
            found.append(t)
            break
    return found


# ---- 管理操作 ----------------------------------------------------------
def _write(items: list[dict]) -> None:
    TERMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TERMS_PATH.open("w", encoding="utf-8") as f:
        for t in items:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    _cache["mtime"] = None


def add(zh: str, en: str, category: str = "综合",
        status: str = "official", src_ref: str = "") -> dict:
    zh, en = (zh or "").strip(), (en or "").strip()
    if not zh or not en:
        raise ValueError("中英文都不能为空")
    with _lock:
        items = _load()
        if any((t.get("zh") or "").strip() == zh for t in items):
            raise ValueError(f"术语已存在：{zh}")
        nid = max((t.get("id", -1) for t in items), default=-1) + 1
        rec = {"id": nid, "zh": zh, "en": en, "category": category or "综合",
               "status": status or "official", "src_ref": src_ref}
        items = items + [rec]
        _write(items)
        return rec


def update(tid: int, **fields) -> bool:
    with _lock:
        items = _load()
        for t in items:
            if t.get("id") == tid:
                for k in ("zh", "en", "category", "status"):
                    if k in fields and fields[k] is not None:
                        t[k] = str(fields[k]).strip()
                _write(items)
                return True
    return False


def remove(tid: int) -> bool:
    with _lock:
        items = _load()
        kept = [t for t in items if t.get("id") != tid]
        if len(kept) == len(items):
            return False
        _write(kept)
        return True
