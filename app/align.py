"""Sentence alignment engine: cross-lingual semantic embeddings + dynamic
programming.

Each sentence is embedded, then a DP search over the two sentence lists finds
the globally optimal set of 1:1 / 1:n / n:1 correspondences by maximizing total
cosine similarity (with a small penalty on many-to-one merges).

Design principle (hard rule): sentences are immutable content units. The
alignment is expressed as *beads* — contiguous index ranges over the fixed
zh/en sentence lists. Editing in the UI only changes ranges, never text, so
neither the embedder nor any model can ever alter the corpus content.
"""
from __future__ import annotations

import json
import re
import time

import numpy as np
import requests

import config
from .clean import strip_markup
from .embedder import Embedder

# Alignment patterns the DP may use (zh_count : en_count). 中文意合长句常对应英文多句
# (一句中文=好几句 "We have…"),或多个中文短句=一句英文(团结还是分裂?…=一句),
# 故放宽到 1:5 / 5:1。合并惩罚(见 _dp_align)会压制无意义的过度合并。
_ALIGN_TYPES = [(1, 1), (1, 2), (2, 1), (1, 3), (3, 1), (2, 2),
                (1, 4), (4, 1), (2, 3), (3, 2), (1, 5), (5, 1)]


def _keep(sents: list[str], s: str, min_len: int, sep: str = "") -> None:
    """Append fragment s; a too-short fragment is glued onto the previous
    sentence instead of being silently dropped (永不丢句 also holds here)."""
    if not s:
        return
    if len(s) > min_len or not sents:
        sents.append(s)
    else:
        sents[-1] = sents[-1] + sep + s


def split_zh(text: str) -> list[str]:
    text = re.sub(r"\n{2,}", "\n", text)
    parts = re.split(r"((?:[。！？；]+[\"”」』]?))", text)
    sents, buf = [], ""
    for part in parts:
        buf += part
        if re.search(r"[。！？；]", part):
            _keep(sents, re.sub(r"\s+", "", buf.strip()), 1)
            buf = ""
    _keep(sents, re.sub(r"\s+", "", buf.strip()), 1)
    return sents


def split_en(text: str) -> list[str]:
    text = re.sub(r"\s*\n\s*", " ", text)
    for abbr in ["Mr.", "Mrs.", "Dr.", "U.S.", "etc.", "i.e.", "e.g.", "No.", "Vol.", "vs.", "St."]:
        text = text.replace(abbr, abbr.replace(".", "\x00"))
    # OCR/排版常把列表项或换行表示成句末后的「. – 」(下一句以破折号开头)。
    # 下面的切句前瞻要求下一句以大写字母/引号开头,破折号不满足 → 整段不切分,
    # 英文句数比中文少 → DP 把每句中文配上「本句+下句」的英文,通篇错位。
    # 先把「句末标点 + 破折号 + 大写」里的破折号去掉,让它能正常在此切句。
    text = re.sub(r"([.!?][”\"’')\]]?)\s+[–—-]\s+(?=[\"“'(A-Z])", r"\1 ", text)
    # Split on terminal . ! ? followed by whitespace + a capital/opening quote.
    # The terminal mark may be wrapped by a closing quote/bracket (dialogue ends
    # like  said." ) — without the second lookbehind those never split and the
    # whole side collapses into a few mega-sentences.
    sents = re.split(r"(?:(?<=[.!?])|(?<=[.!?][”\"’')\]]))\s+(?=[A-Z\"“'(])", text)
    out = []
    for s in sents:
        s = s.replace("\x00", ".").strip()
        s = re.sub(r"^[–—-]\s*", "", s)   # 去掉残留的行首破折号(列表项标记)
        _keep(out, s, 2, sep=" ")
    return out


def _merged_sim(emb_a, emb_b, a0, a1, b0, b1) -> float:
    a = emb_a[a0:a1].mean(axis=0)
    b = emb_b[b0:b1].mean(axis=0)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb + 1e-9))


def _semantic_scorer(emb_a, emb_b):
    """Bind the embeddings into the (a0,a1,b0,b1) -> score callable the DP wants."""
    return lambda a0, a1, b0, b1: _merged_sim(emb_a, emb_b, a0, a1, b0, b1)


# --- offline scorer (no API key, no model) --------------------------------
# Gale & Church (1991) 的长度对齐：译文长度与原文长度近似成正比,偏差服从正态
# 分布,于是「这段中文对这段英文」的可信度可以纯靠字数算出来。桌面版在没有
# API key 时走这条路——质量不如语义向量,但零依赖、零联网、可写进方法学。
_ANCHOR_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)?|[A-Z]{2,}")
_GC_VAR = 6.8   # Gale-Church 的方差常数(按字符计)


def _offline_scorer(zh_sents: list[str], en_sents: list[str]):
    """Length-ratio scorer with numeric/acronym anchors, in the same 0~1 band as
    cosine so the DP's merge penalty stays calibrated."""
    zh_len = np.array([len(s) for s in zh_sents], dtype=np.float64)
    en_len = np.array([len(s) for s in en_sents], dtype=np.float64)
    # 比例由本文档自己估出来,比写死「1 汉字 ≈ 1.7 英文字符」稳——不同文体差别很大
    ratio = float(en_len.sum() / zh_len.sum()) if zh_len.sum() else 1.7
    zh_cum = np.concatenate([[0.0], np.cumsum(zh_len)])
    en_cum = np.concatenate([[0.0], np.cumsum(en_len)])
    # 锚点:两侧都出现的数字与全大写缩写(2022、WTO)。对齐对了它们就该同时出现。
    zh_anchor = [set(_ANCHOR_RE.findall(s)) for s in zh_sents]
    en_anchor = [set(_ANCHOR_RE.findall(s)) for s in en_sents]

    def score(a0: int, a1: int, b0: int, b1: int) -> float:
        la = zh_cum[a1] - zh_cum[a0]
        lb = en_cum[b1] - en_cum[b0]
        if la <= 0 or lb <= 0:
            return 0.0
        # 标准化偏差 δ,再用高斯映回 (0,1]。δ=0(长度完全符合比例)得 1.0
        delta = (lb - la * ratio) / np.sqrt(la * _GC_VAR * ratio + 1e-9)
        s = float(np.exp(-0.5 * min(delta * delta, 36.0)))
        a_set: set = set().union(*zh_anchor[a0:a1]) if a1 > a0 else set()
        b_set: set = set().union(*en_anchor[b0:b1]) if b1 > b0 else set()
        if a_set or b_set:
            hit = len(a_set & b_set) / max(len(a_set | b_set), 1)
            s += 0.15 * hit          # 锚点吻合是强证据,加分
        return min(s, 1.0)

    return score


def _dp_align(sim, n, m):
    NEG = -1e9
    dp = np.full((n + 1, m + 1), NEG)
    dp[0][0] = 0.0
    # Back-pointer stored as the *index* into _ALIGN_TYPES that reached each
    # cell (a single int8), not a Python (i, j) tuple. This keeps the matrix at
    # n*m bytes instead of n*m boxed objects — on a long document the tuple
    # version could allocate hundreds of MB and OOM-kill the worker (the client
    # then sees a non-JSON 502). -1 means "unreached".
    move = np.full((n + 1, m + 1), -1, dtype=np.int8)
    for i in range(n + 1):
        for j in range(m + 1):
            cur = dp[i, j]
            if cur == NEG:
                continue
            for k, (da, db) in enumerate(_ALIGN_TYPES):
                ni, nj = i + da, j + db
                if ni > n or nj > m:
                    continue
                s = sim(i, ni, j, nj)
                penalty = 0.0 if (da == 1 and db == 1) else -0.02 * (da + db - 2)
                score = cur + s + penalty
                if score > dp[ni, nj]:
                    dp[ni, nj] = score
                    move[ni, nj] = k
    beads = []
    i, j = n, m
    while (i > 0 or j > 0) and move[i, j] >= 0:
        da, db = _ALIGN_TYPES[move[i, j]]
        pi, pj = i - da, j - db
        beads.append({"zh": [pi, i], "en": [pj, j],
                      "type": f"{i - pi}:{j - pj}", "score": round(sim(pi, i, pj, j), 3)})
        i, j = pi, pj
    beads.reverse()
    return beads


_CJK = re.compile(r"[一-鿿]")
_PAGE_MARKER = re.compile(r"<<<\s*PAGE\s*\d+\s*>>>", re.IGNORECASE)

# The DP is O(n*m); past this product a single alignment would risk timing out
# or exhausting memory on a small server. Fail fast with a clear message
# instead, asking the user to split the document.
_MAX_DP_CELLS = 2_000_000  # ~1400 x 1400 sentences


def separate_bilingual(text: str) -> tuple[str, str]:
    """Single-doc mode: split interleaved bilingual text into a ZH stream and
    an EN stream by classifying each paragraph (CJK-majority -> Chinese).

    Page markers (``<<<PAGE n>>>``) carry no language of their own. Rather than
    drop them (which would lose the page provenance the UI uses to show the
    scan), a marker-only line is *held* and prepended to the next real
    paragraph, so it rides into that stream — exactly like the double-doc path —
    and is classified by the following content, never on its own.

    Structural HTML/markdown markup is stripped first, for the same reason an
    isolated tag would otherwise become a bogus sentence."""
    text = strip_markup(text)
    zh, en = [], []
    pending = ""  # a page-marker line waiting to attach to the next content
    for para in re.split(r"\n\s*\n|\n", text):
        p = para.strip()
        if not p:
            continue
        content = _PAGE_MARKER.sub("", p).strip()
        if not content:                       # line is only page marker(s)
            pending = f"{pending} {p}".strip() if pending else p
            continue
        if pending:
            p = f"{pending}\n{p}"
            pending = ""
        cjk = len(_CJK.findall(content))
        # 纯绝对阈值(≥3 个汉字就算中文)会把「Deng Xiaoping (邓小平) delivered…」这类
        # 只带中文人名/术语的英文长段整段划进中文流。绝对阈值只对短行生效,
        # 长段还须占比达标(>10%)才算中文;占比 >20% 一律算中文(中英混排的中文段)。
        if cjk > len(content) * 0.2 or (cjk >= 3 and cjk > len(content) * 0.1):
            zh.append(p)
        else:
            en.append(p)
    if pending:                               # trailing marker, no content after
        (zh if zh else en).append(pending)
    return "\n".join(zh), "\n".join(en)


def _fallback_beads(n: int, m: int) -> list[dict]:
    """Degraded alignment used only when the DP can't reach the end corner
    (the two sides are too imbalanced for the allowed merge ratios, or one side
    is empty). Pairs 1:1 positionally and leaves the surplus on the longer side
    as one-sided rows, so NO sentence is ever silently dropped. Scores are 0 so
    the rows flag as low-confidence and invite manual checking."""
    beads, i, j = [], 0, 0
    while i < n or j < m:
        if i < n and j < m:
            beads.append({"zh": [i, i + 1], "en": [j, j + 1], "type": "1:1", "score": 0.0}); i += 1; j += 1
        elif i < n:
            beads.append({"zh": [i, i + 1], "en": [j, j], "type": "1:0", "score": 0.0}); i += 1
        else:
            beads.append({"zh": [i, i], "en": [j, j + 1], "type": "0:1", "score": 0.0}); j += 1
    return beads


def align(zh_text: str, en_text: str, embedder: Embedder | None) -> dict:
    """Return fixed sentence lists + the aligned beads (index ranges).

    ``embedder`` may be None — the desktop build runs without an API key, and
    then falls back to the offline length-ratio scorer. ``mode`` in the result
    says which one ran, so the UI can be honest about it.

    ``fallback`` is True when the optimal DP alignment was unreachable and we
    fell back to positional 1:1 + one-sided leftover rows — the caller should
    warn the user, but no content is ever dropped.
    """
    zh_sents = split_zh(strip_markup(zh_text))
    en_sents = split_en(strip_markup(en_text))
    mode = "semantic" if embedder is not None else "offline"
    n, m = len(zh_sents), len(en_sents)
    if n == 0 and m == 0:
        return {"zh_sents": [], "en_sents": [], "beads": [], "fallback": False,
                "mode": mode}
    if n * m > _MAX_DP_CELLS:
        raise ValueError(
            f"文档过长（中文 {n} 句 × 英文 {m} 句），超出单次对齐上限，请分段后再对齐。")
    if n == 0 or m == 0:
        # one side has no sentences — keep the other side as unpaired rows
        return {"zh_sents": zh_sents, "en_sents": en_sents,
                "beads": _fallback_beads(n, m), "fallback": True, "mode": mode}
    if embedder is not None:
        sim = _semantic_scorer(embedder.embed(zh_sents), embedder.embed(en_sents))
    else:
        sim = _offline_scorer(zh_sents, en_sents)
    beads = _dp_align(sim, n, m)
    fallback = not beads          # DP couldn't reach the corner (extreme imbalance)
    if fallback:
        beads = _fallback_beads(n, m)
    return {"zh_sents": zh_sents, "en_sents": en_sents, "beads": beads,
            "fallback": fallback, "mode": mode}


# --- AI check (DeepSeek as judge; never edits content) --------------------
_CHECK_SYS = (
    "你是严谨但宽容的双语对齐校对员。给你若干「中英句对」,逐对判断中文与英文是否"
    "在内容上互相对应(互为翻译,或一方是另一方的合理翻译)。只判断,绝不改写或翻译。\n"
    "【关键:宽容对待断句差异】中英断句习惯不同,经常一句中文对应英文的一部分、"
    "或一句英文对应多句中文。只要双方讲的是同一件事就算对应(ok=true)。"
    "【绝不要】因为「英文比中文长或短」「看着像缺了半句」「不是逐字对照」「语序不同」"
    "就判错——这些是正常的翻译与切分差异,不是错位。\n"
    "【只在这两种情况判 ok=false】① 两句明显在讲不同的事(张冠李戴/内容无关);"
    "② 一侧是空的、完全没有对应内容。\n"
    "正例:中「双方已在中阿合作论坛框架内建立17项合作机制。」"
    "英「The two sides have established 17 cooperation mechanisms under the framework "
    "of the China-Arab States Cooperation Forum.」→ 讲的是同一件事,ok=true。\n"
    "正例:中「中阿互利共赢,树立南南合作典范。」"
    "英「China and Arab states have set an example for South-South cooperation in "
    "pursuing mutually beneficial collaboration.」→ 同一件事,ok=true(英文不算缺失)。\n"
    "【注】句尾若出现「…」是界面显示截断,不是原文不完整,绝不要据此判「英文截断/内容缺失」。\n"
    '严格输出 JSON 数组,顺序与输入一致,每项 {"i":序号,"ok":true/false,'
    '"note":"≤15字;ok=false 时简述哪里对不上,ok=true 时留空"}。'
)


def _deepseek(messages: list[dict], retries: int = 4) -> str:
    url = f"{config.DEEPSEEK_BASE_URL}/chat/completions"
    for attempt in range(retries):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                              json={"model": config.QA_MODEL, "messages": messages,
                                    "temperature": 0, "thinking": {"type": "disabled"}}, timeout=90)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return ""


def check_pairs(pairs: list[dict], batch: int = 8, workers: int = 3,
                progress=None) -> list[dict]:
    """pairs: [{"zh":..,"en":..}]. Returns [{"ok":bool,"note":str}] aligned by index.

    批之间用小线程池并发(workers 路),校验整篇长文从「分钟级」降到可等待;
    progress(done_rows, total_rows, verdicts) 每完成一批回调一次,便于后台任务
    增量把已出的判定推给前端。verdicts 中未完成的批为 None(前端跳过即可)。"""
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DeepSeek API key")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: list[dict | None] = [None] * len(pairs)

    def _clip(t, n=900):
        t = (t or "").strip()
        return t if len(t) <= n else t[:n] + "…"   # 仅极长句才截,并加省略号示意

    def _run_batch(s: int) -> None:
        chunk = pairs[s:s + batch]
        # 旧版把英文截到 160 字 → 长句被砍半,模型据此误判「英文截断/内容不完整」。
        # 放宽到 900 字(覆盖几乎所有真实句),真要截也加 … 让模型知道是显示截断、非原文缺失。
        listing = "\n".join(f'{i}. 中:{_clip(p["zh"])}　英:{_clip(p["en"])}'
                            for i, p in enumerate(chunk))
        res = [{"ok": True, "note": ""} for _ in chunk]
        try:
            txt = _deepseek([{"role": "system", "content": _CHECK_SYS},
                             {"role": "user", "content": listing}])
            m = re.search(r"\[.*\]", txt, re.S)
            arr = json.loads(m.group()) if m else []
            for item in arr:
                idx = item.get("i")
                if isinstance(idx, int) and 0 <= idx < len(chunk):
                    res[idx] = {"ok": bool(item.get("ok", True)),
                                "note": str(item.get("note", ""))[:30]}
        except Exception:  # noqa: BLE001 - one bad batch shouldn't kill the rest
            res = [{"ok": None, "note": "校验失败"} for _ in chunk]
        out[s:s + len(chunk)] = res

    starts = list(range(0, len(pairs), batch))
    done_rows = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_run_batch, s): s for s in starts}
        for fut in as_completed(futs):
            fut.result()
            done_rows += min(batch, len(pairs) - futs[fut])
            if progress:
                progress(done_rows, len(pairs), out)
    return [v if v is not None else {"ok": None, "note": "校验失败"} for v in out]


# --- LLM 对齐(只决定分组,绝不生成/改写文字)----------------------------
# 思路:把已切句的中英各自编号给模型,它只回「哪些编号归一组」的 JSON,一个正文字都不吐。
# 拿回编号后做严格机器校验(两侧编号必须恰好、按序、不重不漏地覆盖),再回原句数组按号取文
# 拼成句对。任何编号错乱一律拒绝、保留原对齐。文字永远=用户输入切出来的原句,逐字节一致。
_ALIGN_SYS = (
    "你是中英句子对齐器。下面给你【已编号】的中文句子与英文句子(各自独立编号)。"
    "任务:把它们按「互为翻译」配成若干组,使每组中文与英文意思对应。\n"
    "【铁律1:只输出编号,绝不输出、改写、翻译、补全任何句子文字。】\n"
    "【铁律2:每个中文编号、每个英文编号都必须恰好用一次;整体按原顺序,不跳号、不重复、不遗漏。】\n"
    "【铁律3:一组可 1 句中文配多句英文,或多句中文配 1 句英文(中文意合长句、英文一句一意时常见);"
    "同一组内编号必须连续;每组中英两侧各至少 1 个编号。】\n"
    "例:中文 C10「团结还是分裂?」C11「和平还是冲突?」C12「合作还是对抗?」C13「再次成为时代之问。」"
    "对应英文 E13「Unity or split, peace or conflict… raised again by our times.」"
    '→ 一组 {"zh":[10,11,12,13],"en":[13]}。\n'
    '严格只输出 JSON 数组,每项 {"zh":[编号,…],"en":[编号,…]},从前到后排列。无任何解释、无代码围栏。'
)


def llm_align(zh_text: str, en_text: str, embedder: Embedder | None) -> dict:
    """用 DeepSeek 只做分组决策,返回与 align() 同结构的结果(beads 索引指向重新切出的原句)。
    校验不过 → raise ValueError(调用方应保留原对齐)。"""
    zh_sents = split_zh(strip_markup(zh_text))
    en_sents = split_en(strip_markup(en_text))
    n, m = len(zh_sents), len(en_sents)
    if n == 0 or m == 0:
        return {"zh_sents": zh_sents, "en_sents": en_sents,
                "beads": _fallback_beads(n, m), "fallback": True, "source": "llm"}
    user = ("【中文句子】\n" + "\n".join(f"C{i+1}. {s}" for i, s in enumerate(zh_sents))
            + "\n\n【英文句子】\n" + "\n".join(f"E{i+1}. {s}" for i, s in enumerate(en_sents)))
    txt = _deepseek([{"role": "system", "content": _ALIGN_SYS},
                     {"role": "user", "content": user}])
    mobj = re.search(r"\[.*\]", txt, re.S)
    if not mobj:
        raise ValueError("AI 对齐返回格式异常,已保留原对齐。")
    groups = json.loads(mobj.group())
    zh_seq, en_seq, ranges = [], [], []
    for g in groups:
        zi = [int(x) for x in (g.get("zh") or [])]
        ei = [int(x) for x in (g.get("en") or [])]
        if not zi or not ei:
            raise ValueError("AI 对齐出现空组,已保留原对齐。")
        zh_seq += zi; en_seq += ei
        ranges.append((min(zi) - 1, max(zi), min(ei) - 1, max(ei)))
    # 铁律2 校验:两侧编号必须恰好、按序、不重不漏覆盖 1..n / 1..m
    if zh_seq != list(range(1, n + 1)) or en_seq != list(range(1, m + 1)):
        raise ValueError(f"AI 对齐编号不完整或错乱(中文需 1-{n}、英文需 1-{m}),已放弃并保留原对齐。")
    # embedder 只用来给分组打个展示用的分数；桌面版没有 key 时用离线打分器代替,
    # 分组本身出自 DeepSeek,不受影响。
    sim = (_semantic_scorer(embedder.embed(zh_sents), embedder.embed(en_sents))
           if embedder is not None else _offline_scorer(zh_sents, en_sents))
    beads = [{"zh": [a0, a1], "en": [b0, b1], "type": f"{a1 - a0}:{b1 - b0}",
              "score": round(sim(a0, a1, b0, b1), 3)}
             for (a0, a1, b0, b1) in ranges]
    return {"zh_sents": zh_sents, "en_sents": en_sents, "beads": beads,
            "fallback": False, "source": "llm"}
