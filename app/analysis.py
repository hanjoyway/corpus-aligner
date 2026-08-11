"""Corpus-linguistics enrichment layer over a search result set.

Given the top-N Chinese sentences of a search, produce:
  - per-sentence stats: token count, content-word ratio, POS summary
  - aggregate stats: avg tokens, overall content ratio, POS distribution,
    verb-object collocations, key verbs, key objects

Chinese segmentation + POS tagging via jieba. All of this runs only on the
~20 returned sentences, so it stays cheap regardless of corpus size.
"""
from __future__ import annotations

from collections import Counter

import jieba.posseg as pseg

# Punctuation / whitespace tags excluded from the token count.
_PUNCT = {"x", "w"}
# Content (lexical) word tags: nouns, verbs, adjectives, idioms, time, place.
_CONTENT_PREFIX = ("n", "v", "a", "i", "j", "l", "s", "t", "z")
# Noun-like tags used as the "object" in verb-object collocations.
_NOUN = {"n", "nr", "ns", "nt", "nz", "nl", "ng", "l", "an"}
_VERB = {"v", "vn", "vd", "vg"}
# Light/auxiliary verbs carry little lexical content; drop from verb stats.
_VERB_STOP = {"要", "是", "有", "能", "会", "可以", "让", "使", "把", "被",
              "得", "去", "来", "做", "为", "等", "加", "上", "下", "进行"}


def _is_content(flag: str) -> bool:
    return flag.startswith(_CONTENT_PREFIX)


def _is_real_verb(word: str, flag: str) -> bool:
    return flag in _VERB and word not in _VERB_STOP


def analyze_sentence(text: str) -> dict:
    """Tokenize one Chinese sentence and return its tokens + per-sentence stats."""
    toks = [(w.word, w.flag) for w in pseg.cut(text) if w.flag not in _PUNCT and w.word.strip()]
    n = len(toks)
    content = sum(1 for _, f in toks if _is_content(f))
    pos = Counter(f for _, f in toks)
    return {
        "tokens": toks,
        "token_count": n,
        "content_ratio": round(content / n, 3) if n else 0.0,
        "pos": dict(pos.most_common()),
    }


def _collocations(toks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Heuristic verb-object pairs: a verb followed by a nearby noun (within 2)."""
    pairs = []
    for i, (w, f) in enumerate(toks):
        if _is_real_verb(w, f):
            for j in range(i + 1, min(i + 3, len(toks))):
                wj, fj = toks[j]
                if fj in _NOUN:
                    pairs.append((w, wj))
                    break
    return pairs


def analyze_set(sentences: list[str], *, top: int = 6) -> tuple[dict, list]:
    """Aggregate linguistic stats across a set of sentences (the result list).

    Returns (aggregate_stats, per_sentence_stats)."""
    per = [analyze_sentence(s) for s in sentences]
    all_toks = [t for p in per for t in p["tokens"]]
    total_tok = sum(p["token_count"] for p in per)
    content = sum(1 for _, f in all_toks if _is_content(f))

    pos_dist = Counter(f for _, f in all_toks)
    verbs = Counter(w for w, f in all_toks if _is_real_verb(w, f))
    objects = Counter(w for w, f in all_toks if f in _NOUN)
    collos = Counter(f"{v}→{o}" for p in per for v, o in _collocations(p["tokens"]))

    return {
        "count": len(sentences),
        "avg_tokens": round(total_tok / len(sentences), 2) if sentences else 0.0,
        "content_ratio": round(content / total_tok, 3) if total_tok else 0.0,
        "pos_dist": pos_dist.most_common(8),
        "collocations": collos.most_common(top),
        "key_verbs": verbs.most_common(top),
        "key_objects": objects.most_common(top),
    }, per


if __name__ == "__main__":
    sents = [
        "我们要推进经济体制改革，保护生态环境。",
        "坚持高质量发展，发展数字经济。",
        "完善社会保障，提高人民生活水平。",
    ]
    agg, per = analyze_set(sents)
    import json
    print(json.dumps(agg, ensure_ascii=False, indent=2))
