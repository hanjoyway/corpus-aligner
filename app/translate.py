"""翻译辅助：用中心既有语料与术语库约束新文档的翻译，保证前后一致。

思路同专业 CAT 工具（Trados 那套「翻译记忆 + 术语库」），但多一层语义匹配：

  ① 切句
  ② 术语匹配   —— 纯本地子串扫描，0 次接口调用
  ③ 记忆库匹配 —— 先本地字面相似度（相当于 Trados 的模糊匹配率），
                  再用 bge-m3 语义匹配（Trados 做不到：说法不同、意思相同也能命中）
  ④ 批量翻译   —— 把②③作为「必须遵守的术语」和「本机构既往译法」一起喂给 LLM，
                  多句一次调用（一份 5000 字文档约 20 次调用，不是几百次）
  ⑤ 译后核验   —— 本地检查要求使用的术语是否真的出现在译文里，未照用的标出来

只处理中译英与英译中；记忆库只取 status=official 的条目（未经审定的不作范例）。
"""
from __future__ import annotations

import difflib
import json
import re

import config
from . import terms as term_lib

_ZH_END = "。！？；"
_BATCH = 12          # 每次请求翻译的句数：太大易串行错位，太小浪费调用
_TM_TOPK = 3         # 每句最多附带几条既往译法作范例


# ---------- 切句 ----------
def split_sentences(text: str, lang: str) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    out: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if lang == "zh":
            parts = re.split(r"(?<=[。！？；])", para)
        else:
            parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", para)
        for p in parts:
            p = p.strip()
            if p:
                out.append(p)
    return out


# ---------- 记忆库匹配 ----------
def _literal_ratio(a: str, b: str) -> float:
    """字面相似度——相当于 Trados 的模糊匹配率。纯本地，0 调用。"""
    return difflib.SequenceMatcher(None, a, b).ratio()


def tm_lookup(sent: str, src_lang: str, store, index, embedder,
              topk: int = _TM_TOPK) -> list[dict]:
    """在既有语料里找相似的旧译法。返回 [{zh,en,score,kind}]。

    kind: literal=字面相似（本地算出）/ semantic=语义相似（向量匹配）
    """
    src_key = "zh" if src_lang == "zh" else "en"
    cands: dict[int, dict] = {}

    # 1) 本地字面匹配：先按字符重合快速粗筛，避免对全库逐条算相似度
    probe = set(sent[:40])
    for p in store.pairs:
        if (p.status or "official") != "official":
            continue
        base = p.zh if src_key == "zh" else p.en
        if not base or not (p.zh and p.en):
            continue
        if len(probe & set(base[:60])) < 3:
            continue
        r = _literal_ratio(sent, base)
        if r >= 0.55:
            cands[p.id] = {"zh": p.zh, "en": p.en, "score": round(r, 3),
                           "kind": "literal"}

    # 2) 语义匹配：字面不像但意思相同的，Trados 匹配不到，这里能
    if index is not None and embedder is not None:
        try:
            hits, _total, _more = index.search(sent, embedder, store,
                                               top_k=topk,
                                               statuses={"official"})
            for h in hits:
                if not (h.get("zh") and h.get("en")):
                    continue
                prev = cands.get(h["id"])
                sc = float(h.get("score") or 0)
                if prev is None or sc > prev["score"]:
                    cands[h["id"]] = {"zh": h["zh"], "en": h["en"],
                                      "score": round(sc, 3), "kind": "semantic"}
        except Exception:      # 语义不可用（索引未建/接口异常）时降级为纯字面
            pass

    ranked = sorted(cands.values(), key=lambda x: -x["score"])
    return ranked[:topk]


# ---------- 组装提示词并调用 LLM ----------
# 机构名走配置（.env 的 TRANSLATE_ORG），代码里不写死具体客户——
# 这套代码也会打包成通用的单机版发出去。
_ORG = getattr(config, "TRANSLATE_ORG", "")
_SYS = (f"你是{_ORG}的专业译员。" if _ORG else "你是专业译员。") + (
        "请严格按要求翻译，只输出 JSON，不要任何解释或代码块标记。")


def _build_prompt(items: list[dict], src_lang: str) -> str:
    tgt = "英文" if src_lang == "zh" else "中文"
    lines = [f"把下列句子逐句译成{tgt}。要求：",
             "1. 必须使用给定的术语译法，不得改写；",
             "2. 参考给出的本机构既往译法，保持用词与风格一致；",
             "3. 保持公文的正式语体，不增删信息。",
             "",
             '只输出 JSON 数组，形如 [{"n":1,"t":"译文"}, ...]，不要输出别的内容。',
             ""]
    for it in items:
        lines.append(f'原文 {it["n"]}：{it["src"]}')
        if it["terms"]:
            pairs = "；".join(f'{t["zh"]} = {t["en"]}' for t in it["terms"])
            lines.append(f'  必须使用的术语：{pairs}')
        for m in it["tm"][:2]:
            lines.append(f'  既往译法参考：{m["zh"]} → {m["en"]}')
        lines.append("")
    return "\n".join(lines)


def _call_llm(prompt: str) -> list[dict]:
    import requests
    r = requests.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
        json={"model": config.QA_MODEL,
              "messages": [{"role": "system", "content": _SYS},
                           {"role": "user", "content": prompt}],
              "temperature": 0.2, "stream": False},
        timeout=180,
    )
    r.raise_for_status()
    txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise ValueError("模型未返回可解析的 JSON")
    return json.loads(m.group(0))


# ---------- 译后核验 ----------
def verify_terms(translation: str, used_terms: list[dict], src_lang: str) -> list[dict]:
    """检查要求使用的术语是否真的出现在译文里。纯本地，0 调用。

    返回未照用的术语列表——这是 CAT 工具里的 QA Check，把「模型可能不听话」
    变成可见、可核的问题。
    """
    miss = []
    low = (translation or "").lower()
    for t in used_terms:
        want = (t["en"] if src_lang == "zh" else t["zh"]) or ""
        if not want:
            continue
        if src_lang == "zh":
            # 英文侧宽松比对：忽略大小写与多余空白；括号内缩写允许省略
            core = re.sub(r"\s*\([^)]*\)\s*", " ", want).strip().lower()
            ok = core in low or want.lower() in low
        else:
            ok = want in (translation or "")
        if not ok:
            miss.append(t)
    return miss


# ---------- 主入口 ----------
def translate_document(text: str, src_lang: str, store, index, embedder,
                       progress=None) -> dict:
    """翻译整篇文档，返回逐句结果与统计。"""
    sents = split_sentences(text, src_lang)
    if not sents:
        return {"segments": [], "stats": {}}

    # 逐句做本地匹配（不调接口）
    items = []
    for i, s in enumerate(sents, 1):
        matched = term_lib.match_in_text(s) if src_lang == "zh" else []
        items.append({"n": i, "src": s, "terms": matched, "tm": []})

    # 记忆库匹配（语义部分会调 embedding，按句进行）
    for it in items:
        it["tm"] = tm_lookup(it["src"], src_lang, store, index, embedder)

    # 分批翻译
    segments = []
    for start in range(0, len(items), _BATCH):
        chunk = items[start:start + _BATCH]
        if progress:
            progress(start, len(items))
        try:
            out = _call_llm(_build_prompt(chunk, src_lang))
            got = {int(o.get("n", 0)): (o.get("t") or "").strip() for o in out}
        except Exception as e:      # 整批失败不致命：标记后继续下一批
            got = {}
            for it in chunk:
                segments.append({**_seg(it, ""), "error": str(e)[:120]})
            continue
        for it in chunk:
            segments.append(_seg(it, got.get(it["n"], "")))

    for seg in segments:
        seg["missing_terms"] = [
            {"zh": t["zh"], "en": t["en"]}
            for t in verify_terms(seg["translation"], seg["_terms"], src_lang)
        ]
        seg.pop("_terms", None)

    stats = {
        "total": len(segments),
        "with_terms": sum(1 for s in segments if s["terms"]),
        "with_tm": sum(1 for s in segments if s["tm_matches"]),
        "term_violations": sum(1 for s in segments if s["missing_terms"]),
        "failed": sum(1 for s in segments if s.get("error")),
    }
    return {"segments": segments, "stats": stats}


def _seg(it: dict, translation: str) -> dict:
    return {
        "n": it["n"], "source": it["src"], "translation": translation,
        "terms": [{"zh": t["zh"], "en": t["en"]} for t in it["terms"]],
        "tm_matches": it["tm"],
        "_terms": it["terms"],
    }
