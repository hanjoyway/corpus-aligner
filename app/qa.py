"""Smart Q&A = retrieval-augmented generation (RAG).

Pipeline: semantically retrieve the most relevant bilingual sentence pairs as
evidence, then ask DeepSeek to answer the question grounded ONLY in that
evidence, citing pairs by number. Returns the answer plus the evidence list
(with which ones were cited) so the UI can highlight sources.
"""
from __future__ import annotations

import json
import re

import requests

import config
from .embedder import Embedder
from .semantic import SemanticIndex
from .store import Store

_SYSTEM = (
    "你是一个基于「汉英平行语料库」的问答助手。"
    "只能依据下面提供的语料片段回答用户的问题，不得使用语料之外的知识，也不得编造。"
    "回答要简洁、准确，并使用与用户提问相同的语言（中文问就用中文答，英文问就用英文答）。"
    "在引用语料片段时，用方括号标注其编号，例如 [1]、[2][3]。"
    "如果提供的语料不足以回答该问题，请直接说明「语料中没有足够信息回答这个问题」。"
)


def _build_user_prompt(question: str, evidence: list[dict]) -> str:
    lines = [f"问题：{question}", "", "语料片段："]
    for i, e in enumerate(evidence, 1):
        lines.append(f"[{i}] 中：{e['zh']}　英：{e['en']}")
    return "\n".join(lines)


def _cited_indices(answer: str, n: int) -> set[int]:
    """Parse [1], [2][3] style citations (1-based) found in the answer."""
    out = set()
    for m in re.findall(r"\[(\d+)\]", answer):
        idx = int(m)
        if 1 <= idx <= n:
            out.add(idx)
    return out


def ask(question: str, store: Store, index: SemanticIndex,
        embedder: Embedder, k: int | None = None) -> dict:
    k = k or config.QA_EVIDENCE_K
    evidence, _total, _more = index.search(question, embedder, store, top_k=k)
    if not evidence:
        return {"answer": "语料库中没有检索到相关内容。", "evidence": []}

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _build_user_prompt(question, evidence)},
    ]
    resp = requests.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
        json={"model": config.QA_MODEL, "messages": messages,
              "temperature": 0.2, "thinking": {"type": "disabled"}},
        timeout=90,
    )
    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"].strip()

    cited = _cited_indices(answer, len(evidence))
    for i, e in enumerate(evidence, 1):
        e["n"] = i
        e["cited"] = i in cited
    return {"answer": answer, "evidence": evidence}


def ask_stream(question: str, store: Store, index: SemanticIndex,
               embedder: Embedder, k: int | None = None):
    """Generator yielding (kind, payload): 'evidence' once, 'delta' per token,
    'cited' (list of cited numbers) at the end. For SSE streaming to the UI."""
    k = k or config.QA_EVIDENCE_K
    evidence, _total, _more = index.search(question, embedder, store, top_k=k)
    for i, e in enumerate(evidence, 1):
        e["n"] = i
    yield ("evidence", evidence)
    if not evidence:
        yield ("delta", "语料库中没有检索到相关内容。")
        return

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _build_user_prompt(question, evidence)},
    ]
    resp = requests.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
        json={"model": config.QA_MODEL, "messages": messages, "temperature": 0.2,
              "thinking": {"type": "disabled"}, "stream": True},
        timeout=90, stream=True,
    )
    resp.raise_for_status()
    parts: list[str] = []
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8")
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            delta = json.loads(data)["choices"][0]["delta"].get("content", "")
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
        if delta:
            parts.append(delta)
            yield ("delta", delta)
    yield ("cited", sorted(_cited_indices("".join(parts), len(evidence))))
