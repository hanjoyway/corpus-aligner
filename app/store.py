"""In-memory corpus store + keyword/regex search.

On first run it ingests the raw corpus and caches it to data/corpus.jsonl so
later starts are instant. Keyword search is a linear scan: at ~260k short
strings it stays well under 200ms, which is fine for a prototype.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import config
from .ingest import Pair, load_pairs

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "汉英平行语料库资源"
CACHE = ROOT / "data" / "corpus.jsonl"


class Store:
    def __init__(self, pairs: list[Pair]):
        self.pairs = pairs
        self.by_id = {p.id: p for p in pairs}
        # doc -> ordered list of pairs, for KWIC context windows
        self._by_doc: dict[tuple[str, str], list[Pair]] = {}
        for p in pairs:
            self._by_doc.setdefault((p.corpus, p.doc), []).append(p)

    def add_pairs(self, pairs: list[Pair]) -> None:
        """Append already-id'd pairs in place (used by live ingest), avoiding a
        full reload of the corpus."""
        self.pairs.extend(pairs)
        for p in pairs:
            self.by_id[p.id] = p
            self._by_doc.setdefault((p.corpus, p.doc), []).append(p)

    # ---- corpus listing -------------------------------------------------
    def corpora(self) -> list[dict]:
        counts: dict[str, int] = {}
        for p in self.pairs:
            counts[p.corpus] = counts.get(p.corpus, 0) + 1
        return [{"name": k, "count": v} for k, v in sorted(counts.items())]

    # ---- business-category listing --------------------------------------
    def categories(self) -> list[dict]:
        """业务类别及其句对数，按句对数降序（多条件检索的「按类别」维度）。"""
        counts: dict[str, int] = {}
        for p in self.pairs:
            c = p.category or p.corpus
            counts[c] = counts.get(c, 0) + 1
        return [{"name": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

    def statuses(self) -> list[dict]:
        """各译文状态的条数（官方对照 / 待审译文 / 暂无译文）。"""
        counts: dict[str, int] = {}
        for p in self.pairs:
            k = p.status or "official"
            counts[k] = counts.get(k, 0) + 1
        order = ["official", "draft", "none"]
        return [{"key": k, "count": counts[k]} for k in order if k in counts]

    # ---- keyword / regex search ----------------------------------------
    def keyword_search(
        self,
        query: str,
        *,
        lang: str = "both",      # "zh" | "en" | "both"
        regex: bool = False,
        case_sensitive: bool = False,
        corpora: set[str] | None = None,
        categories: set[str] | None = None,
        statuses: set[str] | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> tuple[list[dict], int]:
        """返回 (当前页命中, 全库真实命中总数)。total 用于前端显示真实条数、
        offset/limit 用于「显示更多」分页——即使只展示 20 条，total 也是全部匹配数。"""
        if not query:
            return [], 0
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                pat = re.compile(query, flags)
            except re.error:
                return [], 0
            match = lambda s: bool(pat.search(s))
        else:
            # 模糊匹配：多个关键词用空格分隔，全部命中即可（不要求相邻、顺序无关），
            # 单个词内支持 * / ? 通配符。单个连写词退化为原来的精确子串匹配。
            prep = (lambda s: s) if case_sensitive else (lambda s: s.lower())
            terms = query.split() or [query]
            if not case_sensitive:
                terms = [t.lower() for t in terms]
            flags = 0 if case_sensitive else re.IGNORECASE
            matchers = []
            for t in terms:
                if "*" in t or "?" in t:
                    rx = re.compile(re.escape(t).replace(r"\*", ".*").replace(r"\?", "."), flags)
                    matchers.append(lambda s, p=rx: bool(p.search(s)))
                else:
                    matchers.append(lambda s, t=t: t in prep(s))
            match = lambda s: all(m(s) for m in matchers)

        check_zh = lang in ("zh", "both")
        check_en = lang in ("en", "both")
        lo, hi = offset, offset + limit
        out: list[dict] = []
        total = 0
        for p in self.pairs:
            if corpora and p.corpus not in corpora:
                continue
            if categories and (p.category or p.corpus) not in categories:
                continue
            if statuses and (p.status or "official") not in statuses:
                continue
            if (check_zh and p.zh and match(p.zh)) or (check_en and p.en and match(p.en)):
                if lo <= total < hi:      # 只把当前页窗口内的收进结果
                    out.append(self._hit(p))
                total += 1                # 但总数继续累计到全库扫完
        return out, total

    # ---- KWIC context window -------------------------------------------
    def context(self, pair_id: int, radius: int = 5) -> list[dict]:
        p = self.by_id.get(pair_id)
        if p is None:
            return []
        doc = self._by_doc[(p.corpus, p.doc)]
        idx = next(i for i, q in enumerate(doc) if q.id == pair_id)
        lo, hi = max(0, idx - radius), min(len(doc), idx + radius + 1)
        return [{**self._hit(q), "focus": q.id == pair_id} for q in doc[lo:hi]]

    @staticmethod
    def _hit(p: Pair) -> dict:
        return {"id": p.id, "corpus": p.corpus, "doc": p.doc,
                "line": p.line, "zh": p.zh, "en": p.en,
                "category": p.category or p.corpus,
                "status": p.status or "official",
                "src_ref": p.src_ref or ""}


def _build_cache() -> list[Pair]:
    pairs = load_pairs(RAW_DIR)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
    return pairs


def load_store() -> Store:
    if CACHE.exists():
        pairs = [Pair(**json.loads(line)) for line in CACHE.open(encoding="utf-8")]
    else:
        pairs = _build_cache()
    # 解析业务类别：显式标注（新入库）优先，其余按 config 映射兜底。存量缓存里
    # 没有 category 字段，这一步把它们归入对应业务类别，无需重建缓存。
    for p in pairs:
        p.category = config.category_of(p.corpus, p.category)
    return Store(pairs)


if __name__ == "__main__":
    store = load_store()
    print(f"loaded {len(store.pairs):,} pairs, {len(store.corpora())} corpora")
    for q, opts in [("改革开放", {}), ("Belt and Road", {"lang": "en"})]:
        hits, total = store.keyword_search(q, limit=3, **opts)
        print(f"\n'{q}' -> {total} hits (showing {min(3, len(hits))})")
        for h in hits[:3]:
            print(f"  [{h['corpus']}] {h['zh'][:40]} || {h['en'][:40]}")
