"""Embedding backends.

The rest of the app only depends on the `Embedder` interface, so swapping
SiliconFlow's hosted bge-m3 for a locally-run bge-m3 later is a one-file
change. Vectors are L2-normalized, so cosine similarity == dot product.
"""
from __future__ import annotations

import time
from typing import Protocol

import numpy as np
import requests

import config


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (len(texts), dim) float32 array of normalized vectors."""
        ...


def _normalize(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype(np.float32)


class SiliconFlowEmbedder:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, batch_size: int = 32):
        self.api_key = api_key or config.SILICONFLOW_API_KEY
        self.model = model or config.EMBED_MODEL
        self.base_url = (base_url or config.SILICONFLOW_BASE_URL).rstrip("/")
        self.batch_size = batch_size
        self._cache: dict[str, np.ndarray] = {}   # single-query embedding cache
        if not self.api_key:
            raise RuntimeError(
                "SILICONFLOW_API_KEY is not set. Put it in .env or the environment."
            )

    def _post(self, batch: list[str], retries: int = 7) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model, "input": batch}
        for attempt in range(retries):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()["data"]
                return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
            except (requests.RequestException, KeyError) as e:
                if attempt == retries - 1:
                    raise
                # 1,2,4,8,16,30s —— 实测网络抖动可持续数十秒，短退避不够
                time.sleep(min(2 ** attempt, 30))
        return []

    def embed(self, texts: list[str]) -> np.ndarray:
        # Serve a single cached query instantly (repeat searches cost nothing).
        if len(texts) == 1 and texts[0] in self._cache:
            return self._cache[texts[0]][None, :]
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i:i + self.batch_size]))
        vecs = _normalize(np.asarray(out, dtype=np.float32))
        if len(texts) == 1:
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[texts[0]] = vecs[0]
        return vecs


def default_embedder() -> Embedder:
    return SiliconFlowEmbedder()
