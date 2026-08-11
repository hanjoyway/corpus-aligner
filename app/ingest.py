"""Load the 汉英平行语料库资源 folder into normalized sentence pairs.

Each corpus lives in its own sub-folder and ships as line-aligned text:
the N-th line of `<name>.ZH.txt` is the translation of the N-th line of
`<name>.EN.txt`. Source files are GBK/GB18030 encoded, so we transcode to
UTF-8 on read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

# Matches "<base>.ZH.txt" and also the stray double extension "<base>.EN.txt.txt"
_LANG_RE = re.compile(r"^(?P<base>.+?)\.(?P<lang>ZH|EN)(?:\.txt)+$", re.IGNORECASE)

# Try these encodings in order. gb18030 is a superset of GBK/GB2312 and decodes
# almost any simplified-Chinese legacy file; utf-8 first in case some are clean.
_ENCODINGS = ("utf-8-sig", "gb18030", "latin-1")


@dataclass
class Pair:
    id: int
    corpus: str   # corpus folder name, e.g. "中国共产党党章"
    doc: str      # base filename within the corpus, e.g. "2018年政府工作报告"
    line: int     # 1-based line number inside the doc (alignment anchor)
    zh: str
    en: str
    category: str = ""  # 业务类别；空则由 config.category_of 兜底解析（向后兼容旧缓存）
    # 译文状态：official=中心审定的官方对照 / draft=我方补译待审 / none=暂无译文（单语）
    # 单语材料允许 zh 或 en 为空——这类语料照样可检索，只是标明尚无对照译文。
    status: str = "official"
    # 溯源引用：指回这条语料在原始材料中的位置，供"查看原文"核对清洗/对齐是否有误。
    #   PDF/图片 → "page:12"       （原页图）
    #   视频画面 → "frame:41@80s"  （抽帧序号与秒数）
    #   音视频语音 → "time:204.5-209.2s"（起止秒）
    # 空字符串表示该条暂无溯源信息（不影响检索）。
    src_ref: str = ""
    # 自动对齐的置信度（中英两侧的语义相似度，0~1）。低于阈值的条目会被降级为
    # 待核对，人工审核时可按此值排序，最可疑的排最前。空值表示未评估（如单语条目）。
    align_sim: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _read_lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        except UnicodeDecodeError:
            continue
    # Last resort: never raise, just lossy-decode so ingestion keeps going.
    return raw.decode("gb18030", errors="replace").split("\n")


def _group_files(corpus_dir: Path) -> dict[str, dict[str, Path]]:
    """Group a corpus folder's files into {base: {"ZH": path, "EN": path}}."""
    groups: dict[str, dict[str, Path]] = {}
    for f in corpus_dir.iterdir():
        if not f.is_file():
            continue
        m = _LANG_RE.match(f.name)
        if not m:
            continue
        base = m.group("base")
        lang = m.group("lang").upper()
        groups.setdefault(base, {})[lang] = f
    return groups


def load_pairs(root: str | Path) -> list[Pair]:
    """Walk every corpus folder and return aligned, non-empty sentence pairs."""
    root = Path(root)
    pairs: list[Pair] = []
    next_id = 0
    for corpus_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for base, langs in sorted(_group_files(corpus_dir).items()):
            if "ZH" not in langs or "EN" not in langs:
                continue  # unpaired file, skip
            zh_lines = _read_lines(langs["ZH"])
            en_lines = _read_lines(langs["EN"])
            for i in range(min(len(zh_lines), len(en_lines))):
                zh = zh_lines[i].strip()
                en = en_lines[i].strip()
                if not zh or not en:
                    continue  # drop blanks / one-sided lines
                pairs.append(Pair(next_id, corpus_dir.name, base, i + 1, zh, en))
                next_id += 1
    return pairs


if __name__ == "__main__":
    import sys
    from collections import Counter

    src = sys.argv[1] if len(sys.argv) > 1 else "汉英平行语料库资源"
    ps = load_pairs(src)
    by_corpus = Counter(p.corpus for p in ps)
    print(f"total pairs: {len(ps):,}  across {len(by_corpus)} corpora\n")
    for corpus, n in by_corpus.most_common():
        print(f"{n:>7,}  {corpus}")
    if ps:
        s = ps[0]
        print(f"\nsample [{s.corpus} · {s.doc} · line {s.line}]")
        print(f"  ZH: {s.zh[:60]}")
        print(f"  EN: {s.en[:60]}")
