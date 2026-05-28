# -*- coding: utf-8 -*-
"""
gpt-image-1 による占い系素材画像 一括生成スクリプト（時雨 / 龍脈命術）

生成した画像は assets/images/ に保存し、
既存の Pillow 版画像を差し替えます。

使い方:
  python scripts/generate_dalle_images.py                          # 全35枚を生成（スキップあり）
  python scripts/generate_dalle_images.py --force                  # すでにある画像も上書き
  python scripts/generate_dalle_images.py --dry                    # プロンプトだけ確認（API非消費）
  python scripts/generate_dalle_images.py --push                   # 生成後に shigure-images へ自動 push
  python scripts/generate_dalle_images.py --force --push           # 上書き生成 + 自動 push
  python scripts/generate_dalle_images.py --images-repo PATH       # shigure-images のパスを手動指定

必要パッケージ:
  pip install openai pillow

事前準備:
  .env に OPENAI_API_KEY=sk-... を追記（config.py が自動読み込み）
"""

import io
import sys
import base64
import time
import shutil
import subprocess
import argparse
from pathlib import Path

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).parent.parent
ASSETS_DIR = BASE_DIR / "assets" / "images"

# ── 共通スタイル指示（全プロンプトに付加） ─────────────────────────────

STYLE_SUFFIX = (
    "Dark background, no text, no watermark, no border frame. "
    "Square 1:1 composition. High quality, artistic, mystical Japanese fortune-telling aesthetic. "
    "Suitable for social media post header image."
)

# ── 画像ごとのプロンプト ───────────────────────────────────────────────

IMAGES = {
    # ── 星座 ──────────────────────────────────────────────────────────
    "zodiac/aries.jpg": (
        "Aries zodiac constellation on a deep indigo night sky. "
        "Golden star points connected by thin glowing lines forming the Ram constellation. "
        "Delicate golden sparkles and nebula dust around the stars. "
        "Warm golden and amber glow emanating from the center. "
        + STYLE_SUFFIX
    ),
    "zodiac/taurus.jpg": (
        "Taurus zodiac constellation on a deep forest-green night sky. "
        "Bright emerald-green star points connected by thin glowing lines forming the Bull. "
        "The Pleiades star cluster visible nearby, soft green mist. "
        "Lush, earth-energy aesthetic with jade and emerald tones. "
        + STYLE_SUFFIX
    ),
    "zodiac/gemini.jpg": (
        "Gemini zodiac constellation on a dark violet night sky. "
        "Twin columns of purple-white stars connected by glowing lines, two parallel paths of light. "
        "Amethyst and lavender nebula swirling between the twin figures. "
        "Dual symmetry with soft purple radiance. "
        + STYLE_SUFFIX
    ),
    "zodiac/cancer.jpg": (
        "Cancer zodiac constellation on a deep midnight blue night sky. "
        "Soft blue-white star points forming the Crab constellation, connected by silver threads. "
        "Moonlight shimmer, gentle silver and pearl-blue nebula mist. "
        "The Beehive Cluster as a bright central glow. "
        + STYLE_SUFFIX
    ),
    "zodiac/leo.jpg": (
        "Leo zodiac constellation on a deep crimson-black night sky. "
        "Bright golden-orange stars forming the Lion's sickle shape, Regulus blazing at the heart. "
        "Bold golden rays radiating from the brightest star. "
        "Fierce, regal energy with amber and gold glow. "
        + STYLE_SUFFIX
    ),
    "zodiac/virgo.jpg": (
        "Virgo zodiac constellation on a dark olive-green night sky. "
        "Soft green-white stars forming the Maiden, Spica glowing like a blue-white diamond. "
        "Delicate botanical elements, fern-like nebula wisps in jade and mint. "
        "Pure, earth-goddess energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/libra.jpg": (
        "Libra zodiac constellation on a dark navy blue night sky. "
        "Blue-violet stars forming the Scales of Justice, two pans balanced in equilibrium. "
        "Silver and periwinkle nebula haze, sense of perfect cosmic balance. "
        "Elegant and harmonious composition. "
        + STYLE_SUFFIX
    ),
    "zodiac/scorpio.jpg": (
        "Scorpio zodiac constellation on a deep blood-red night sky. "
        "Rich red-orange stars forming the Scorpion's curved tail, Antares blazing deep red. "
        "Dark crimson nebula clouds, intense and mysterious energy. "
        "The tail curves like a question mark trailing into darkness. "
        + STYLE_SUFFIX
    ),
    "zodiac/sagittarius.jpg": (
        "Sagittarius zodiac constellation on a dark cobalt-blue night sky. "
        "Bright blue-white stars forming the Archer's bow and arrow aimed at the galactic center. "
        "The Milky Way core glowing in the background with golden-blue stellar clouds. "
        "Adventurous, expansive energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/capricorn.jpg": (
        "Capricorn zodiac constellation on a charcoal-gray night sky. "
        "Silver-white stars forming the Sea Goat, precise geometric lines connecting them. "
        "Subtle silver and gunmetal nebula, austere and disciplined beauty. "
        "Crystalline, structured, ancient energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/aquarius.jpg": (
        "Aquarius zodiac constellation on a deep teal night sky. "
        "Bright cyan-blue stars forming the Water Bearer, with flowing wavy lines representing water. "
        "Electric blue and aquamarine ripples of light, futuristic and humanitarian. "
        "Twin zigzag lines of stars like water waves. "
        + STYLE_SUFFIX
    ),
    "zodiac/pisces.jpg": (
        "Pisces zodiac constellation on a deep sapphire night sky. "
        "Soft blue-violet stars forming Two Fish connected by a shimmering silver cord. "
        "Flowing ribbons of blue and violet light, dreamy and oceanic. "
        "The two fish swimming in opposite directions, spiritually connected. "
        + STYLE_SUFFIX
    ),
    "zodiac/general.jpg": (
        "All twelve zodiac symbols arranged in a sacred circle on dark purple space. "
        "Golden mandala geometry at the center, each sign glowing in its own color. "
        "Celestial wheel of the zodiac with ancient astronomical markings. "
        "Rich cosmic tapestry, mystical and grand. "
        + STYLE_SUFFIX
    ),
    # ── 方位 ──────────────────────────────────────────────────────────
    "direction/north.jpg": (
        "Ornate north compass point on deep navy blue background. "
        "Elegant N marker in gold filigree, pointing upward with glowing blue aurora behind it. "
        "Polar star Polaris shining at the tip. "
        "Arctic-blue and sapphire tones, precise and directional. "
        + STYLE_SUFFIX
    ),
    "direction/northeast.jpg": (
        "Northeast compass direction symbol on deep blue-slate background. "
        "NE arrow with golden filigree, dawn light breaking at the horizon. "
        "Pale gold meets arctic blue gradient, sense of new beginnings in the northeast direction. "
        + STYLE_SUFFIX
    ),
    "direction/east.jpg": (
        "East compass direction symbol on deep amber-black background. "
        "Golden sunrise ray emanating eastward, ornate E marker with morning star above. "
        "Warm amber, gold and sunrise orange tones. "
        "Energy of new beginnings and rising light. "
        + STYLE_SUFFIX
    ),
    "direction/southeast.jpg": (
        "Southeast compass direction symbol on dark warm background. "
        "SE marker with golden-orange filigree, blend of sunrise warmth and earth energy. "
        "Amber and terracotta tones, sense of harvest and abundance. "
        + STYLE_SUFFIX
    ),
    "direction/south.jpg": (
        "South compass direction symbol on deep vermillion-black background. "
        "Blazing S marker with phoenix-red filigree, flames rising upward. "
        "Intense crimson, scarlet and fire-orange tones. "
        "Passionate, active fire energy. "
        + STYLE_SUFFIX
    ),
    "direction/southwest.jpg": (
        "Southwest compass direction symbol on dark amber-brown background. "
        "SW marker with golden-bronze filigree, earth and harvest energy. "
        "Rich amber, burnt orange and autumn gold tones. "
        "Stable, grounding earth energy. "
        + STYLE_SUFFIX
    ),
    "direction/west.jpg": (
        "West compass direction symbol on dark forest-green background. "
        "Silver W marker with vine-like filigree, sunset over water in the distance. "
        "Deep jade, moss-green and sage tones. "
        "Reflective, introspective sunset energy. "
        + STYLE_SUFFIX
    ),
    "direction/northwest.jpg": (
        "Northwest compass direction symbol on deep teal-navy background. "
        "NW marker with silver-teal filigree, twilight sky blending blue and silver. "
        "Steel blue, silver and moonlit teal tones. "
        "Celestial, heavenly energy. "
        + STYLE_SUFFIX
    ),
    "direction/compass.jpg": (
        "Magnificent ornate 8-point compass rose on deep midnight blue background. "
        "Eight diamond-pointed directions in gold and silver, intricate filigree details. "
        "North point crowned with a polar star, concentric decorative rings. "
        "Reminiscent of antique navigational instruments, majestic and authoritative. "
        + STYLE_SUFFIX
    ),
    # ── 季節 ──────────────────────────────────────────────────────────
    "season/spring.jpg": (
        "Magical spring cherry blossoms floating in soft pink and purple mystical energy. "
        "Sakura petals swirling in a spiritual wind, glowing with inner light. "
        "Dark purple-rose gradient background, luminous pale pink flowers. "
        "Japanese spring spiritual energy, renewal and new beginnings. "
        + STYLE_SUFFIX
    ),
    "season/summer.jpg": (
        "Radiant summer sun symbol on deep navy ocean background. "
        "Sixteen golden rays alternating long and short, like an ancient sun deity symbol. "
        "Central sun disk glowing with intense golden-white light. "
        "Bright azure sky reflected in dark blue water below. "
        "Powerful, expansive solar energy. "
        + STYLE_SUFFIX
    ),
    "season/autumn.jpg": (
        "Maple leaves in autumn colors swirling in mystical wind energy on dark background. "
        "Rich crimson, orange and golden maple leaves in elegant spiral arrangement. "
        "Dark warmly lit background, leaves glowing with inner amber light. "
        "Japanese autumn wabi-sabi aesthetic, bittersweet beauty. "
        + STYLE_SUFFIX
    ),
    "season/winter.jpg": (
        "Intricate crystal snowflake on deep midnight blue winter background. "
        "Six-fold symmetry with complex geometric branches, each arm with three levels of structure. "
        "Ice blue and pearl white crystal formations, faint aurora borealis in background. "
        "Pure, crystalline winter silence. "
        + STYLE_SUFFIX
    ),
    # ── 風水 ──────────────────────────────────────────────────────────
    "fengshui/general.jpg": (
        "Perfect yin-yang taiji symbol in jade green and deep forest tones on dark background. "
        "The S-curve dividing light and shadow in perfect harmony. "
        "Jade green yang and deep forest shadow yin, glowing with chi energy. "
        "Ancient Chinese philosophy made visible. "
        + STYLE_SUFFIX
    ),
    "fengshui/water.jpg": (
        "Water element feng shui symbol: flowing mandala of concentric waves and ripples. "
        "Deep navy blue and aquamarine tones, sacred geometry of water movement. "
        "Central lotus-like pattern, rings of energy spreading outward like water drops. "
        "Calm, flowing, purifying water energy. "
        + STYLE_SUFFIX
    ),
    # ── 汎用 ──────────────────────────────────────────────────────────
    "general/fortune_1.jpg": (
        "Sacred geometric mandala for fortune and destiny on deep indigo background. "
        "Intricate concentric rings with petal patterns, inner star polygon at center. "
        "Purple and violet luminescence, ancient mystical symmetry. "
        "Powerful spiritual energy for divination. "
        + STYLE_SUFFIX
    ),
    "general/fortune_2.jpg": (
        "Flower of Life sacred geometry on deep teal-black background. "
        "Overlapping circles forming the ancient geometric pattern, glowing cyan and turquoise. "
        "Mathematical perfection of the universe encoded in light. "
        "Peaceful, unified field energy. "
        + STYLE_SUFFIX
    ),
    "general/fortune_3.jpg": (
        "Lotus mandala for spiritual fortune on dark purple-black background. "
        "Eight-petaled lotus in amethyst and violet with golden center. "
        "Sacred mandala rings extending outward, spiritual awakening energy. "
        "Deep meditation and higher consciousness aesthetic. "
        + STYLE_SUFFIX
    ),
    "general/mystery_1.jpg": (
        "Eye of Providence triangle on deep crimson-black background. "
        "All-seeing eye within a radiant triangle, twelve light rays emanating outward. "
        "Red and gold tones, mysterious and prophetic energy. "
        "Ancient oracle symbol, mystical and powerful. "
        + STYLE_SUFFIX
    ),
    "general/mystery_2.jpg": (
        "Eye of Providence on deep midnight blue-black background. "
        "All-seeing eye within a luminous equilateral triangle, silver-blue light rays. "
        "Cool silver and periwinkle tones, omniscient and cosmic energy. "
        "Deep cosmic awareness and spiritual sight. "
        + STYLE_SUFFIX
    ),
    "general/ichi_gon.jpg": (
        "A single golden word or symbol of wisdom on deep black-gold background. "
        "Ancient golden calligraphy energy in abstract form, no specific text. "
        "Rich amber and gold mandala rings around the central glow. "
        "One word of truth from the universe, powerful and concise. "
        + STYLE_SUFFIX
    ),
    "general/menu.jpg": (
        "Mystical fortune-telling service menu background on warm dark amber background. "
        "Flowing golden energy lines forming abstract decorative frame. "
        "Dragon scales pattern subtly in background, warm amber and deep gold tones. "
        "Premium, trustworthy aesthetic for professional fortune-telling services. "
        + STYLE_SUFFIX
    ),
}


def load_openai_key() -> str:
    """config.py 経由で .env から OPENAI_API_KEY を読み込む"""
    import sys
    import os
    sys.path.insert(0, str(BASE_DIR))
    from config import BASE_DIR as _BD
    from pathlib import Path
    env_path = Path(_BD) / ".env"
    key = ""
    for enc in ("utf-8", "utf-16", "utf-8-sig"):
        try:
            text = env_path.read_text(encoding=enc)
            for line in text.splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
            if key:
                break
        except Exception:
            continue
    if not key:
        key = os.getenv("OPENAI_API_KEY", "")
    return key


def generate_image(client, prompt: str, filename: str) -> bytes:
    """gpt-image-1 でプロンプトから画像を生成し PNG バイト列を返す"""
    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024",
        quality="high",
        n=1,
    )
    b64 = response.data[0].b64_json
    return base64.b64decode(b64)


def convert_to_jpeg(png_bytes: bytes, quality: int = 92) -> bytes:
    """PNG バイト列を JPEG に変換して返す"""
    from PIL import Image
    import io as _io
    img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def push_to_images_repo(images_repo: Path) -> bool:
    """
    assets/images/ の内容を shigure-images リポジトリにコピーして push する。
    Returns: True=成功, False=失敗
    """
    if not images_repo.exists():
        print(f"  ❌ shigure-images ディレクトリが見つかりません: {images_repo}")
        print(f"     --images-repo オプションでパスを指定してください。")
        return False

    print(f"\n  📂 shigure-images へコピー: {images_repo}")
    copied = 0
    for src in ASSETS_DIR.rglob("*.jpg"):
        if "_dalle_samples" in src.parts:
            continue
        rel = src.relative_to(ASSETS_DIR)
        dst = images_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    print(f"  {copied} ファイルをコピー")

    # git add / commit / push
    try:
        subprocess.run(
            ["git", "-C", str(images_repo), "add", "-A"],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", str(images_repo), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if result.returncode == 0:
            print("  ℹ️  差分なし — push をスキップ")
            return True

        subprocess.run(
            ["git", "-C", str(images_repo), "commit",
             "-m", "update: gpt-image-1 assets"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(images_repo), "push"],
            check=True, capture_output=True,
        )
        print("  ✅ shigure-images へ push しました")
        return True

    except subprocess.CalledProcessError as e:
        print(f"  ❌ git エラー: {e.stderr.decode(errors='replace') if e.stderr else e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="gpt-image-1 で占い素材画像を生成")
    parser.add_argument("--force", action="store_true", help="既存ファイルも上書き")
    parser.add_argument("--dry", action="store_true", help="プロンプト確認のみ（API非消費）")
    parser.add_argument("--only", type=str, default="", help="指定キーだけ生成 (例: zodiac/leo.jpg)")
    parser.add_argument("--push", action="store_true",
                        help="生成後に shigure-images リポジトリへ自動コピー＆push")
    parser.add_argument("--images-repo", type=str, default="",
                        help="shigure-images リポジトリのパス（省略時は ../shigure-images）")
    args = parser.parse_args()

    if args.dry:
        print("\n=== DRY RUN: プロンプト一覧 ===\n")
        for fname, prompt in IMAGES.items():
            print(f"[{fname}]")
            print(f"  {prompt[:120]}...")
            print()
        return

    key = load_openai_key()
    if not key:
        print("❌ OPENAI_API_KEY が見つかりません。")
        print("   .env に OPENAI_API_KEY=sk-... を追記してください。")
        return

    try:
        from openai import OpenAI
    except ImportError:
        print("❌ openai パッケージが見つかりません。")
        print("   pip install openai  を実行してください。")
        return

    client = OpenAI(api_key=key)

    targets = {k: v for k, v in IMAGES.items()
               if (not args.only or k == args.only)}
    total = len(targets)
    done = skipped = failed = 0

    print(f"\n{'='*60}")
    print(f"  gpt-image-1 画像生成  ({total}枚)")
    print(f"{'='*60}\n")

    for fname, prompt in targets.items():
        out_path = ASSETS_DIR / fname
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.exists() and not args.force:
            print(f"  SKIP [{fname}] (--force で上書き)")
            skipped += 1
            continue

        try:
            print(f"  生成中 [{fname}] ...", end=" ", flush=True)
            png_bytes = generate_image(client, prompt, fname)
            jpeg_bytes = convert_to_jpeg(png_bytes)
            out_path.write_bytes(jpeg_bytes)
            done += 1
            print(f"OK ({len(jpeg_bytes)//1024}KB)")

            # レート制限対策: 1枚ごとに少し待機
            if done < total - skipped:
                time.sleep(3)

        except Exception as e:
            msg = str(e)
            if "Billing hard limit" in msg or "billing_limit" in msg:
                print(f"\n  ❌ 課金上限エラー: OpenAI の課金上限に達しています。")
                print(f"     https://platform.openai.com/settings/organization/billing")
                print(f"     でチャージ後に再実行してください。")
                break
            elif "rate_limit" in msg.lower():
                print(f"  ⚠️  レート制限 — 60秒待機します...")
                time.sleep(60)
                failed += 1
            else:
                print(f"  NG: {msg}")
                failed += 1

    print(f"\n{'='*60}")
    print(f"  完了: {done}枚生成 / {skipped}枚スキップ / {failed}枚失敗")
    print(f"{'='*60}\n")

    if done > 0 and args.push:
        images_repo = (
            Path(args.images_repo) if args.images_repo
            else BASE_DIR.parent / "shigure-images"
        )
        push_to_images_repo(images_repo)
    elif done > 0:
        print("  ヒント: --push オプションを付けると shigure-images へ自動 push できます")
        print(f"  例: python scripts/generate_dalle_images.py --force --push\n")


if __name__ == "__main__":
    main()
