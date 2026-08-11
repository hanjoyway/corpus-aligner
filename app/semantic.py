"""Semantic search over the prebuilt vector index.

Brute-force cosine via a single normalized matrix-vector product. At the
prototype's index size (tens of thousands of rows) this is sub-millisecond,
so no FAISS/Milvus needed yet.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import config
from .embedder import Embedder
from .store import Store

VEC_PATH = config.INDEX_DIR / "vectors.npy"
IDS_PATH = config.INDEX_DIR / "ids.npy"
META_PATH = config.INDEX_DIR / "meta.json"


class SemanticIndex:
    def __init__(self, vectors: np.ndarray, ids: np.ndarray, meta: dict):
        self.vectors = vectors          # (N, dim), L2-normalized
        self.ids = ids                  # (N,) pair ids
        self.meta = meta

    def append(self, vectors: np.ndarray, ids: np.ndarray,
               corpus: str | None = None) -> None:
        """Append new vectors/ids in place and update metadata (used by live
        ingest), avoiding a full reload of the index from disk."""
        self.vectors = np.vstack([self.vectors, vectors])
        self.ids = np.concatenate([self.ids, ids])
        self.meta["count"] = int(len(self.ids))
        if corpus and corpus not in self.meta.setdefault("corpora", []):
            self.meta["corpora"].append(corpus)

    @classmethod
    def load(cls) -> "SemanticIndex | None":
        if not (VEC_PATH.exists() and IDS_PATH.exists()):
            return None
        meta = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}
        return cls(np.load(VEC_PATH), np.load(IDS_PATH), meta)

    def search(
        self,
        query: str,
        embedder: Embedder,
        store: Store,
        *,
        top_k: int = 50,
        offset: int = 0,
        corpora: set[str] | None = None,
        categories: set[str] | None = None,
        statuses: set[str] | None = None,
    ) -> tuple[list[dict], int, bool]:
        """返回 (当前页, 相关结果总数, 是否还有更多)。

        语义检索会给全库每一条打分，没有"匹配/不匹配"之分，所以"总数"取
        **相似度 ≥ config.SEMANTIC_MIN_SCORE 的条数**——即"有多少条真正相关"。
        bge-m3 的分数基线偏高（无关句子也有 0.4 上下），阈值定低了会把半个库
        算成命中，故默认 0.6（实测：无关查询只剩几十条，相关查询几百到几千条）。
        """
        if not query.strip():
            return [], 0, False
        q = embedder.embed([query])[0]           # (dim,), normalized
        scores = self.vectors @ q                 # cosine similarity
        # --- 相关结果总数：分数 ≥ 阈值的条数（受语料库/类别过滤影响）---------
        thr = config.SEMANTIC_MIN_SCORE
        relevant = scores >= thr
        if corpora or categories or statuses:
            total = 0
            for row in np.flatnonzero(relevant):
                p = store.by_id.get(int(self.ids[row]))
                if p is None or (corpora and p.corpus not in corpora):
                    continue
                if categories and (p.category or p.corpus) not in categories:
                    continue
                if statuses and (p.status or "official") not in statuses:
                    continue
                total += 1
        else:
            total = int(relevant.sum())

        # --- 取当前页（按相关度排序，可翻页）--------------------------------
        need = offset + top_k
        mult = 6 if (corpora or categories or statuses) else 1
        pool = min(len(scores), max(need * mult, need + 1))
        cand = np.argpartition(-scores, pool - 1)[:pool]
        cand = cand[np.argsort(-scores[cand])]
        collected: list[tuple] = []
        for row in cand:
            p = store.by_id.get(int(self.ids[row]))
            if p is None or (corpora and p.corpus not in corpora):
                continue
            if categories and (p.category or p.corpus) not in categories:
                continue
            if statuses and (p.status or "official") not in statuses:
                continue
            collected.append((row, p))
            if len(collected) > need:            # 够本页 + 1 即可停
                break
        out: list[dict] = []
        for row, p in collected[offset:need]:
            hit = store._hit(p)
            hit["score"] = round(float(scores[row]), 4)
            out.append(hit)
        # 翻页以"相关结果"为界；候选池若已耗尽也不再声称还有更多
        has_more = (offset + len(out)) < total and len(collected) > need
        return out, total, has_more
