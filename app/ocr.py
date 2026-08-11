"""OCR stage: scanned PDF -> page images -> GLM-OCR -> text with page markers.

Produces text where each page is prefixed with `<<<PAGE NNN>>>`, preserving
page provenance for later cleaning / segmentation. GLM-OCR (智谱) does the
recognition; PyMuPDF renders the pages.
"""
from __future__ import annotations

import base64

import config


def img_mime(img_bytes: bytes) -> str:
    """Detect page-image mime from magic bytes (we only ever produce JPEG/PNG)."""
    return "image/jpeg" if img_bytes[:2] == b"\xff\xd8" else "image/png"


def glm_ocr(img_bytes: bytes) -> str:
    """Recognize one page image via 智谱 GLM-OCR, return its markdown text."""
    import httpx
    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=config.GLM_OCR_API_KEY)
    headers = dict(client.auth_headers)
    headers["Content-Type"] = "application/json"
    b64 = base64.b64encode(img_bytes).decode()
    r = httpx.post(config.GLM_OCR_URL, headers=headers,
                   json={"model": "glm-ocr", "file": f"data:{img_mime(img_bytes)};base64,{b64}"},
                   timeout=150)
    r.raise_for_status()
    return (r.json().get("md_results") or "").strip()


_VLM_PROMPT = (
    "请把这张书页图片里的全部文字逐字转写出来,严格照搬原文,不要翻译、不要改写、不要纠错、"
    "不要总结。按原文的段落和换行还原;包含页眉/页脚/页码等所有可见文字。"
    "只输出文字本身,不要任何解释、不要 markdown 代码块、不要额外标记。"
)


def vlm_ocr(png_bytes: bytes) -> str:
    """视觉模型兜底转写:走智谱 GLM-4V 视觉对话端点(同一个 key),
    用于 layout_parsing OCR 接口失败(如偶发 400)时的备选。

    图片字段格式各家不一:智谱原生 image_url.url 收「裸 base64」,
    OpenAI 风格收「data:image/png;base64,...」。先试裸 base64,失败再试 data URI,
    免去对具体版本的猜测。"""
    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=config.GLM_OCR_API_KEY)
    b64 = base64.b64encode(png_bytes).decode()

    def _call(image_url_value):
        resp = client.chat.completions.create(
            model=config.GLM_VLM_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url_value}},
                {"type": "text", "text": _VLM_PROMPT},
            ]}],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip()

    try:
        out = _call(b64)
    except Exception:  # noqa: BLE001 - 退到 data URI 格式再试一次
        out = _call(f"data:{img_mime(png_bytes)};base64,{b64}")
    if out.startswith("```"):
        import re
        out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
    return out


def qwen_vl_ocr(png_bytes: bytes) -> str:
    """跨厂商兜底转写:千问 Qwen2.5-VL,走 SiliconFlow(OpenAI 兼容端点)。
    智谱整套(layout OCR + GLM-4V)若因政治敏感词触发内容审核被拦(400),
    换到千问这套不同的审核体系通常能正常识别。复用 embedding 的 SiliconFlow key。"""
    import httpx
    b64 = base64.b64encode(png_bytes).decode()
    url = config.SILICONFLOW_BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": config.QWEN_VL_MODEL, "temperature": 0.1,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{img_mime(png_bytes)};base64,{b64}"}},
            {"type": "text", "text": _VLM_PROMPT},
        ]}],
    }
    r = httpx.post(url, headers={"Authorization": f"Bearer {config.SILICONFLOW_API_KEY}",
                                 "Content-Type": "application/json"},
                   json=body, timeout=150)
    r.raise_for_status()
    out = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if out.startswith("```"):
        import re
        out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
    return out


def pdf_to_pages(pdf_bytes: bytes, start: int = 1, end: int | None = None, dpi: int = 180):
    """Render PDF pages [start, end] (1-based) to image bytes; returns a list of
    (page_no, img_bytes) tuples. JPEG(q85) 优先——比 PNG 小 5-10 倍,整本书的页图
    常驻内存(校对任务)在 1GB 服务器上才扛得住;老版 PyMuPDF 不支持 jpg 时退回 PNG。"""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.needs_pass:
        if not doc.authenticate(""):   # owner-locked but no user password -> opens
            doc.close()
            raise RuntimeError("PDF 已加密且需要密码，无法打开。")
    n = doc.page_count
    if start > n:
        doc.close()
        raise RuntimeError(f"起始页 {start} 超出 PDF 总页数({n} 页)。")
    end = min(end or n, n)
    zoom = dpi / 72.0
    pages = []
    for i in range(start - 1, end):
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        try:
            img = pix.tobytes("jpeg", jpg_quality=85)
        except Exception:  # noqa: BLE001 - PyMuPDF too old for jpeg
            img = pix.tobytes("png")
        pages.append((i + 1, img))
    doc.close()
    return pages


def ocr_pdf(pdf_bytes: bytes, start: int = 1, end: int | None = None,
            dpi: int = 180, progress=None) -> str:
    """OCR a page range; returns text with <<<PAGE NNN>>> markers per page."""
    pages = pdf_to_pages(pdf_bytes, start, end, dpi)
    parts = []
    for k, (pno, png) in enumerate(pages):
        try:
            txt = glm_ocr(png)
        except Exception as e:  # noqa: BLE001 - keep going, mark the failed page
            txt = f"(第 {pno} 页 OCR 失败: {e})"
        parts.append(f"<<<PAGE {pno:03d}>>>\n{txt}")
        if progress:
            progress(k + 1, len(pages), pno)
    return "\n\n".join(parts)
