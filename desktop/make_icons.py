"""生成应用图标（.ico / .icns），图形与网页版顶栏的 logo 一致。

直接用 Pillow 画，不依赖 SVG 渲染器——图形本身就是两个圆角矩形加一个圆点，
没必要为此引入 cairosvg 那一串系统库。

    python -m desktop.make_icons
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"

BG = (51, 48, 43)          # #33302b 深棕底
EDGE = (74, 68, 61)        # #4a443d 描边
BAR1 = (180, 88, 48)       # #b45830 赭石（长条 = 原文）
BAR2 = (214, 168, 95)      # #d6a85f 金（短条 = 译文）


def draw(size: int) -> Image.Image:
    """按 76×76 的原始比例放大绘制。用 4 倍超采样把圆角磨平。"""
    s = size * 4
    u = s / 76.0                       # 原图 1 单位对应的像素
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2 * u, 2 * u, 74 * u, 74 * u], radius=18 * u,
                        fill=BG, outline=EDGE, width=max(1, int(1.5 * u)))
    d.rounded_rectangle([18 * u, 27 * u, 58 * u, 34 * u], radius=3.5 * u, fill=BAR1)
    d.rounded_rectangle([18 * u, 42 * u, 46 * u, 49 * u], radius=3.5 * u, fill=BAR2)
    r = 3.2 * u
    d.ellipse([54 * u - r, 45.5 * u - r, 54 * u + r, 45.5 * u + r], fill=BAR2)
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # Windows：.ico 内嵌多个尺寸，系统按场景挑
    sizes = [16, 24, 32, 48, 64, 128, 256]
    draw(256).save(OUT / "app.ico", sizes=[(n, n) for n in sizes])
    draw(512).save(OUT / "app.png")
    print(f"写出 {OUT/'app.ico'}")
    print(f"写出 {OUT/'app.png'}")

    # macOS：.icns 要靠系统的 iconutil，只能在 Mac 上生成（生成一次提交进仓库，
    # Windows 的 CI 用不到它）
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "app.iconset"
            iconset.mkdir()
            for n in (16, 32, 64, 128, 256, 512):
                draw(n).save(iconset / f"icon_{n}x{n}.png")
                draw(n * 2).save(iconset / f"icon_{n}x{n}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(OUT / "app.icns")], check=True)
        print(f"写出 {OUT/'app.icns'}")
    else:
        print("跳过 .icns（需要 macOS 的 iconutil）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
