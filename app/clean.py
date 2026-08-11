"""Stage ② — text cleaning.

Strips non-content markup from OCR'd / raw text before alignment: page
markers, repeated running headers, footnote reference marks, standalone page
numbers. Every rule is toggleable and reports exactly what it removed, so the
user can see and trust it. It removes *markup only* — never rewrites sentence
content.
"""
from __future__ import annotations

import re
from collections import Counter

DEFAULT_OPTS = {
    "page_markers": True,    # <<<PAGE 001>>>
    "headers": True,         # repeated short running headers
    "footnote_marks": True,  # 〔1〕 [1] reference markers
    "asterisks": True,       # * footnote anchors
    "page_numbers": True,    # standalone "123" / "第94页"
    "markdown": True,        # GLM-OCR markdown: # headings, <div>, ![](), $...$
}

_PAGE_MARKER = re.compile(r"<<<\s*PAGE\s*\d+\s*>>>", re.IGNORECASE)
_MD_DIV = re.compile(r"</?div[^>]*>")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_CIRCLED = re.compile(r"\$\s*\\textcircled\{[^}]*\}\s*\$")
# 只删「真·LaTeX 行内标记」——$...$ 内必须含 \ ^ _ { } 之一才算(如 $ ^{*} $ / $ ^{[1]} $)。
# 旧规则 \$[^$]*\$ 会把两个美元号之间的内容整段吞掉:US$300 billion; …reaching US$30 billion
# → 只剩「US30 billion」,英文句子被吃,直接导致中英句数失衡、对齐崩成兜底。
_MD_INLINE_MATH = re.compile(r"\$[^$]*[\\^_{}][^$]*\$")
_MD_HEADING = re.compile(r"^\s{0,3}#+\s*", re.MULTILINE)
_FOOTNOTE = re.compile(r"[〔\[]\s*\d{1,3}\s*[〕\]]")
_PAGENUM_LINE = re.compile(r"^\s*(?:第\s*\d+\s*页|\d{1,4})\s*$")
_SENT_END = re.compile(r"[。！？.!?]")


def strip_markup(text: str) -> str:
    """Remove structural HTML / markdown markup (GLM-OCR artifacts: ``<div>``,
    ``![](...)`` image refs, ``# headings``, inline ``$...$`` math) without
    touching sentence content — heading *text* is kept, only the ``#`` marker
    goes. Used to sanitize text before alignment so these tags don't become
    bogus sentences or skew the language split."""
    text = _MD_DIV.sub("", text)
    text = _MD_IMG.sub("", text)
    text = _MD_CIRCLED.sub("", text)
    text = _MD_INLINE_MATH.sub("", text)
    text = _MD_HEADING.sub("", text)
    return text


def clean_text(text: str, opts: dict | None = None) -> tuple[str, dict]:
    o = {**DEFAULT_OPTS, **(opts or {})}
    report = {k: 0 for k in DEFAULT_OPTS}

    if o["page_markers"]:
        report["page_markers"] = len(_PAGE_MARKER.findall(text))
        text = _PAGE_MARKER.sub("\n", text)

    if o["markdown"]:
        report["markdown"] = (len(_MD_DIV.findall(text)) + len(_MD_IMG.findall(text))
                              + len(_MD_INLINE_MATH.findall(text)) + len(_MD_HEADING.findall(text)))
        text = _MD_DIV.sub("", text)
        text = _MD_IMG.sub("", text)              # drop figure placeholders
        text = _MD_CIRCLED.sub("", text)          # 圈数字脚注
        text = _MD_INLINE_MATH.sub("", text)      # 其余行内 LaTeX 标记
        text = _MD_HEADING.sub("", text)          # 标题标记，保留标题文字

    lines = text.split("\n")

    # Detect repeated running headers: short, punctuation-free lines that recur.
    header_set: set[str] = set()
    if o["headers"]:
        freq = Counter(ln.strip() for ln in lines if ln.strip())
        for ln, cnt in freq.items():
            if cnt >= 4 and len(ln) <= 16 and not _SENT_END.search(ln):
                header_set.add(ln)

    out_lines = []
    for ln in lines:
        s = ln.strip()
        if o["headers"] and s in header_set:
            report["headers"] += 1
            continue
        if o["page_numbers"] and _PAGENUM_LINE.match(s):
            report["page_numbers"] += 1
            continue
        out_lines.append(ln)
    text = "\n".join(out_lines)

    if o["footnote_marks"]:
        report["footnote_marks"] = len(_FOOTNOTE.findall(text))
        text = _FOOTNOTE.sub("", text)

    if o["asterisks"]:
        report["asterisks"] = text.count("*")
        text = text.replace("*", "")

    # tidy whitespace (collapse blank runs); never touch in-line content
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, report


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    raw = open(path, encoding="utf-8").read()
    cleaned, rep = clean_text(raw)
    print(f"文件: {path}")
    print(f"原始: {len(raw):,} 字符 / {raw.count(chr(10))+1:,} 行")
    print(f"清洗: {len(cleaned):,} 字符 / {cleaned.count(chr(10))+1:,} 行")
    print(f"去除: {rep}")
    print("\n--- 清洗后前 12 行 ---")
    for ln in cleaned.split("\n")[:12]:
        print("  " + ln[:70])
