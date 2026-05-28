# -*- coding: utf-8 -*-
"""
占い系素材画像 一括生成スクリプト（時雨 / 龍脈命術）

テキストなし・グラデーション + 幾何学模様の神秘系デザイン。
assets/images/ 配下に 35 枚の JPEG 画像を生成する。

使い方:
  python scripts/generate_images.py
"""

import io
import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# Windows cp932 コンソールでも出力できるようにする
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SIZE = 1080
BASE_DIR = Path(__file__).parent.parent
ASSETS_DIR = BASE_DIR / "assets" / "images"


# ─── ユーティリティ ────────────────────────────────────────────────────────

def hex_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def make_gradient(c1: tuple, c2: tuple, style: str = "radial_in") -> Image.Image:
    """グラデーション背景を高速生成（numpy）"""
    y_idx, x_idx = np.ogrid[:SIZE, :SIZE]
    cx = cy = SIZE / 2

    if style == "radial_in":        # 中心が明るい
        dist = np.sqrt((x_idx - cx) ** 2 + (y_idx - cy) ** 2)
        t = np.clip(dist / (cx * 1.42), 0, 1)
    elif style == "radial_out":     # 中心が暗い
        dist = np.sqrt((x_idx - cx) ** 2 + (y_idx - cy) ** 2)
        t = 1 - np.clip(dist / (cx * 1.42), 0, 1)
    elif style == "diagonal":       # 左上→右下
        t = np.clip((x_idx + y_idx) / (SIZE * 1.5), 0, 1)
    else:                           # linear（上→下）
        t = y_idx / SIZE

    c1_arr = np.array(c1, dtype=float)
    c2_arr = np.array(c2, dtype=float)
    arr = (c1_arr * (1 - t[:, :, None]) + c2_arr * t[:, :, None]).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def add_glow(img: Image.Image, color: tuple, radius: int = 300, alpha: int = 70) -> Image.Image:
    """中央のソフトグロー（ガウシアンブラー）"""
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    cx = cy = SIZE // 2
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=(*color, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def add_vignette(img: Image.Image, strength: float = 0.55) -> Image.Image:
    """周辺減光（ビネット）でより深みを出す"""
    y_idx, x_idx = np.ogrid[:SIZE, :SIZE]
    cx = cy = SIZE / 2
    dist = np.sqrt((x_idx - cx) ** 2 + (y_idx - cy) ** 2)
    max_dist = math.sqrt(cx ** 2 + cy ** 2)
    vig = 1 - strength * (dist / max_dist) ** 1.8
    vig = np.clip(vig, 0, 1)
    arr = np.array(img).astype(float)
    arr = (arr * vig[:, :, None]).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def add_dots(img: Image.Image, color: tuple, count: int = 100,
             r_min: int = 1, r_max: int = 3, alpha_max: int = 230,
             seed: int = 0) -> Image.Image:
    """輝く点（星のような散点）"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    rng = random.Random(seed)
    margin = 60
    for _ in range(count):
        x = rng.randint(margin, SIZE - margin)
        y = rng.randint(margin, SIZE - margin)
        r = rng.randint(r_min, r_max)
        a = rng.randint(80, alpha_max)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(*color, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def add_rings(img: Image.Image, color: tuple, count: int = 4,
              alpha: int = 35) -> Image.Image:
    """同心円リング"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx = cy = SIZE // 2
    for i in range(1, count + 1):
        r = int(SIZE * 0.14 * i)
        w = max(1, 6 - i)
        a = max(8, alpha - i * 6)
        draw.ellipse([cx - r - w, cy - r - w, cx + r + w, cy + r + w],
                     outline=(*color, a), width=w)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def add_compass(img: Image.Image, color: tuple, alpha: int = 45) -> Image.Image:
    """方位盤・放射線"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx = cy = SIZE // 2
    reach = int(SIZE * 0.51)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x2 = cx + int(reach * math.cos(rad))
        y2 = cy + int(reach * math.sin(rad))
        draw.line([cx, cy, x2, y2], fill=(*color, alpha), width=2)
    # 中央の小さい円
    r = 18
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(*color, alpha + 20), width=3)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def add_arc_lines(img: Image.Image, color: tuple, alpha: int = 30) -> Image.Image:
    """流れるような曲線（風水・水イメージ）"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for i in range(4):
        offset = 120 * i
        bbox = [-offset, SIZE // 4 - offset, SIZE + offset, SIZE - SIZE // 4 + offset]
        a = max(10, alpha - i * 6)
        draw.arc(bbox, start=0, end=180, fill=(*color, a), width=2)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


# ─── 画像定義テーブル ───────────────────────────────────────────────────────
# (style, dark_color, light_color, decoration, accent_rgb)
IMAGES = {
    # 星座 ─────────────────────────────────────────────────────────────────
    "zodiac/aries.jpg":        ("radial_in",  "#0e0827", "#2c1b68", "stars",   (255, 190,  90)),
    "zodiac/taurus.jpg":       ("diagonal",   "#081808", "#1a4a2a", "rings",   ( 90, 210, 130)),
    "zodiac/gemini.jpg":       ("radial_in",  "#180818", "#4a1a78", "stars",   (200, 140, 255)),
    "zodiac/cancer.jpg":       ("linear",     "#08182e", "#184a6e", "rings",   (140, 190, 255)),
    "zodiac/leo.jpg":          ("radial_out", "#180800", "#6a2800", "stars",   (255, 170,  40)),
    "zodiac/virgo.jpg":        ("diagonal",   "#081808", "#385a28", "rings",   (140, 210, 170)),
    "zodiac/libra.jpg":        ("radial_in",  "#18182e", "#38387a", "stars",   (170, 170, 255)),
    "zodiac/scorpio.jpg":      ("linear",     "#180000", "#580a1a", "stars",   (255,  90,  90)),
    "zodiac/sagittarius.jpg":  ("diagonal",   "#08081a", "#1a1a58", "stars",   ( 90, 140, 255)),
    "zodiac/capricorn.jpg":    ("radial_in",  "#0a0a0a", "#28283a", "rings",   (170, 175, 195)),
    "zodiac/aquarius.jpg":     ("linear",     "#08182e", "#086a8e", "stars",   ( 80, 210, 255)),
    "zodiac/pisces.jpg":       ("radial_out", "#080a2e", "#182a7a", "rings",   (140, 170, 255)),
    "zodiac/general.jpg":      ("radial_in",  "#080a2e", "#281a5a", "stars",   (210, 190, 255)),

    # 方位 ─────────────────────────────────────────────────────────────────
    "direction/north.jpg":     ("radial_in",  "#08082e", "#082a6e", "compass", ( 90, 190, 255)),
    "direction/northeast.jpg": ("diagonal",   "#08082e", "#183a6e", "compass", (120, 200, 255)),
    "direction/east.jpg":      ("linear",     "#1a1a08", "#4a4808", "compass", (210, 195,  90)),
    "direction/southeast.jpg": ("diagonal",   "#1a0808", "#5a2808", "compass", (255, 160,  70)),
    "direction/south.jpg":     ("radial_out", "#280808", "#680808", "compass", (255, 110,  70)),
    "direction/southwest.jpg": ("diagonal",   "#281808", "#5a3808", "compass", (255, 150,  50)),
    "direction/west.jpg":      ("linear",     "#081808", "#1a4820", "compass", ( 90, 195, 140)),
    "direction/northwest.jpg": ("diagonal",   "#081a2e", "#183a5e", "compass", (110, 195, 215)),
    "direction/compass.jpg":   ("radial_in",  "#08081a", "#18184a", "compass", (170, 190, 255)),

    # 季節 ─────────────────────────────────────────────────────────────────
    "season/spring.jpg":       ("diagonal",   "#28083a", "#e0609a", "rings",   (255, 190, 215)),
    "season/summer.jpg":       ("radial_in",  "#08182e", "#087a9e", "rings",   ( 90, 215, 255)),
    "season/autumn.jpg":       ("diagonal",   "#3a1200", "#c05800", "rings",   (255, 175,  70)),
    "season/winter.jpg":       ("radial_in",  "#08082e", "#283a6e", "rings",   (195, 215, 255)),

    # 風水 ─────────────────────────────────────────────────────────────────
    "fengshui/general.jpg":    ("diagonal",   "#081808", "#1a4828", "arc",     ( 90, 195, 145)),
    "fengshui/water.jpg":      ("radial_in",  "#08082e", "#08285e", "arc",     ( 70, 170, 215)),

    # 汎用 ─────────────────────────────────────────────────────────────────
    "general/fortune_1.jpg":   ("radial_in",  "#08082e", "#280a5a", "stars",   (195, 170, 255)),
    "general/fortune_2.jpg":   ("diagonal",   "#081818", "#083a4a", "stars",   ( 90, 195, 215)),
    "general/fortune_3.jpg":   ("linear",     "#180818", "#481848", "stars",   (215, 140, 255)),
    "general/mystery_1.jpg":   ("radial_out", "#180808", "#58081a", "stars",   (255, 120, 120)),
    "general/mystery_2.jpg":   ("radial_in",  "#080808", "#18183a", "stars",   (140, 140, 255)),
    "general/ichi_gon.jpg":    ("diagonal",   "#181800", "#484800", "rings",   (250, 215,  90)),
    "general/menu.jpg":        ("linear",     "#180800", "#482800", "rings",   (255, 185,  90)),
}


# ─── 生成メイン ──────────────────────────────────────────────────────────────

def generate(filename: str, style: str, c1: str, c2: str,
             decoration: str, accent: tuple) -> Image.Image:
    seed = abs(hash(filename)) % 100000

    img = make_gradient(hex_rgb(c1), hex_rgb(c2), style=style)
    img = add_glow(img, accent, radius=320, alpha=55)

    if decoration == "stars":
        img = add_dots(img, accent, count=130, r_min=1, r_max=3, seed=seed)
        img = add_rings(img, accent, count=2, alpha=18)

    elif decoration == "rings":
        img = add_rings(img, accent, count=4, alpha=30)
        img = add_dots(img, accent, count=45, r_min=1, r_max=2, alpha_max=160, seed=seed)

    elif decoration == "compass":
        img = add_compass(img, accent, alpha=48)
        img = add_rings(img, accent, count=3, alpha=25)
        img = add_dots(img, accent, count=25, r_min=1, r_max=3, seed=seed)

    elif decoration == "arc":
        img = add_arc_lines(img, accent, alpha=30)
        img = add_rings(img, accent, count=3, alpha=22)
        img = add_dots(img, accent, count=50, r_min=1, r_max=2, seed=seed)

    img = add_vignette(img, strength=0.55)
    return img


def main():
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    total = len(IMAGES)
    done = 0

    for filename, (style, c1, c2, deco, accent) in IMAGES.items():
        out_path = ASSETS_DIR / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            img = generate(filename, style, c1, c2, deco, accent)
            img.save(out_path, "JPEG", quality=92, optimize=True)
            done += 1
            print(f"  ✅ [{done:02d}/{total}] {filename}")
        except Exception as e:
            print(f"  ❌ {filename}: {e}")

    print(f"\n✨ {done}/{total} 枚の画像を生成しました")
    print(f"   保存先: {ASSETS_DIR}")


if __name__ == "__main__":
    main()
