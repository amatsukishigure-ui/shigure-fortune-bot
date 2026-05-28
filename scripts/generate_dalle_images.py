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
    "Digital art, highly detailed cosmic fantasy illustration. "
    "Dark deep space background filled with nebula and stars. "
    "No text, no watermark, no border frame. Square 1:1 composition. "
    "Mystical, ethereal, Japanese fortune-telling aesthetic."
)

# ── 画像ごとのプロンプト ───────────────────────────────────────────────

IMAGES = {
    # ── 星座（動物・象徴が星雲として浮かび上がる構図） ──────────────────
    "zodiac/aries.jpg": (
        "A majestic cosmic ram formed from golden nebula and stardust fills the composition. "
        "Its powerful horns curve dramatically, radiating amber and gold light. "
        "The Aries constellation star pattern with glowing gold connecting lines is overlaid "
        "across the ram's body, each star a brilliant point of light. "
        "Deep indigo and violet space background with golden nebula clouds. "
        "The ram charges forward with fierce divine energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/taurus.jpg": (
        "A powerful cosmic bull emerges from emerald green nebula and stardust. "
        "Its massive head is lowered, eyes glowing with ancient earth energy. "
        "The Taurus constellation star pattern with glowing green connecting lines overlays "
        "the bull's body. The Pleiades star cluster shimmers above its shoulder. "
        "Deep forest-green cosmic background with jade and emerald nebula. "
        "Sense of unshakeable strength and natural abundance. "
        + STYLE_SUFFIX
    ),
    "zodiac/gemini.jpg": (
        "Two ethereal twin figures formed from violet and silver stardust face each other "
        "in a cosmic mirror. They hold hands, their forms merging into shared nebula energy. "
        "The Gemini constellation star pattern with purple glowing lines runs across both figures. "
        "Dark violet background with amethyst and lavender nebula swirling between the twins. "
        "Dual symmetry, the dance of light and shadow, yin and yang. "
        + STYLE_SUFFIX
    ),
    "zodiac/cancer.jpg": (
        "A luminous cosmic crab floats in moonlit space, its shell encrusted with pearl-blue stars. "
        "Silver claws reach forward, trailing threads of moonlight. "
        "The Cancer constellation pattern with silver-blue connecting lines is mapped across "
        "its body. The Beehive Cluster glows at the center of its shell. "
        "Deep midnight blue background with pearl and silver nebula shimmer. "
        "Gentle, protective, lunar energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/leo.jpg": (
        "A magnificent cosmic lion dominates the composition, its enormous mane radiating "
        "golden and amber stardust like a solar corona. Regulus blazes at its heart. "
        "The Leo constellation star pattern with brilliant gold connecting lines is overlaid "
        "across the lion's mane and body. The lion gazes forward with regal, ancient power. "
        "Deep crimson-black background with golden nebula clouds billowing outward. "
        "Fierce solar divinity, the king of the cosmic beasts. "
        + STYLE_SUFFIX
    ),
    "zodiac/virgo.jpg": (
        "A serene cosmic goddess figure formed from jade-green and white starlight. "
        "She holds a sheaf of wheat made of glowing stars, her robes flowing as nebula wisps. "
        "The Virgo constellation pattern with soft green connecting lines traces her silhouette. "
        "Spica gleams as a brilliant blue-white diamond near her hand. "
        "Dark olive-green background with mint and jade nebula. "
        "Pure, nurturing, harvest goddess energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/libra.jpg": (
        "A pair of cosmic scales floats in perfect equilibrium in deep space. "
        "The scales are forged from blue-violet starlight, their pans holding nebula clouds "
        "of equal weight — light and darkness perfectly balanced. "
        "The Libra constellation pattern with periwinkle connecting lines frames the scales. "
        "Behind the scales, a robed cosmic figure of justice is barely visible in nebula form. "
        "Navy blue background with silver and violet nebula. "
        + STYLE_SUFFIX
    ),
    "zodiac/scorpio.jpg": (
        "A massive cosmic scorpion emerges from dark crimson nebula, its segmented tail "
        "curving upward in a deadly arc, the stinger glowing red as a dying star. "
        "Antares blazes like a red supergiant at its heart. "
        "The Scorpio constellation pattern with deep red connecting lines maps its body. "
        "Blood-red and dark crimson background with shadowy nebula clouds. "
        "Intense, transformative, death-and-rebirth energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/sagittarius.jpg": (
        "A cosmic centaur archer draws a blazing bow aimed at the galactic center. "
        "Its arrow is a beam of pure cobalt-blue light shooting toward the Milky Way core. "
        "The Sagittarius constellation pattern with electric blue connecting lines overlays "
        "the centaur's body. The galactic center glows gold and blue behind the figure. "
        "Deep cobalt-black background with the full sweep of the Milky Way. "
        "Adventurous, expansive, truth-seeking energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/capricorn.jpg": (
        "A cosmic sea goat ascends from dark ocean depths into the stars above. "
        "Its lower fish tail is made of silver nebula, its upper goat form solid as mountain rock. "
        "The Capricorn constellation pattern with silver-grey connecting lines marks its form. "
        "Charcoal and dark grey background with subtle silver and gunmetal nebula. "
        "Ancient, disciplined, mountain-climbing-to-the-stars energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/aquarius.jpg": (
        "A luminous cosmic water bearer pours an endless stream of electric-blue starwater "
        "from a great urn. The water flows and becomes nebula, becomes galaxies. "
        "The Aquarius constellation pattern with cyan connecting lines flows along the "
        "water stream and across the figure. "
        "Deep teal background with electric blue and aquamarine nebula ripples. "
        "Visionary, humanitarian, future-seeing energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/pisces.jpg": (
        "Two luminous cosmic fish swim in opposite directions through deep space, "
        "connected by a shimmering silver cord of stardust that binds them eternally. "
        "Their scales are made of blue-violet nebula, their eyes hold the depth of oceans. "
        "The Pisces constellation pattern with sapphire connecting lines traces both fish. "
        "Deep sapphire background with flowing violet and blue nebula currents. "
        "Dreamy, spiritual, ocean-of-souls energy. "
        + STYLE_SUFFIX
    ),
    "zodiac/general.jpg": (
        "Twelve cosmic zodiac creatures arranged in a great celestial wheel in deep space. "
        "Each creature — ram, bull, twins, crab, lion, maiden, scales, scorpion, centaur, "
        "sea goat, water bearer, fish — glows in its own unique nebula color. "
        "A golden sacred geometry mandala connects them at the center. "
        "Dark purple cosmic background, the full wheel of the year and the cosmos. "
        + STYLE_SUFFIX
    ),
    # ── 方位（四神獣を用いた風水方位の守護神） ───────────────────────────
    "direction/north.jpg": (
        "Genbu, the Black Tortoise of the North — a massive cosmic black tortoise "
        "entwined with a serpent rises from dark polar waters into star-filled space. "
        "Its ancient shell is covered in star maps and constellation patterns. "
        "Glowing blue-black armor plates, serpent with silver scales coiling around it. "
        "Deep navy and black background with cold northern star light. "
        "The guardian of winter, water, and wisdom. "
        + STYLE_SUFFIX
    ),
    "direction/northeast.jpg": (
        "The northeastern direction guardian — where the Black Tortoise meets the Azure Dragon. "
        "A cosmic scene of dark water meeting the first light of dawn. "
        "Blue-black tortoise scales blend into teal dragon scales at the horizon. "
        "Aurora borealis in blue and teal sweeps across the background. "
        "A sense of transition, from darkness into the first light of morning. "
        + STYLE_SUFFIX
    ),
    "direction/east.jpg": (
        "Seiryu, the Azure Dragon of the East — a magnificent cosmic blue-green dragon "
        "soars eastward through golden dawn light, its scales shimmering like jade and sapphire. "
        "It brings the energy of spring, wood, and new beginnings. "
        "Its claws reach toward a rising star in the east. "
        "Deep azure background with golden sunrise rays breaking through teal nebula. "
        "The guardian of spring, wood energy, and the rising sun. "
        + STYLE_SUFFIX
    ),
    "direction/southeast.jpg": (
        "The southeastern direction — where the Azure Dragon meets the Vermilion Bird. "
        "A cosmic scene of teal dragon scales fading into warm amber and orange phoenix feathers. "
        "Dawn light warming into the heat of midday, spring blossoming into summer. "
        "Gold and amber gradient background with wisps of jade green and rose. "
        "Abundant, warm, growth energy at the threshold of fire and wood. "
        + STYLE_SUFFIX
    ),
    "direction/south.jpg": (
        "Suzaku, the Vermilion Bird of the South — a blazing cosmic phoenix spreads "
        "its enormous wings of crimson and gold fire across the sky. "
        "Each feather is a flame, each wingtip trails glowing embers and stardust. "
        "It embodies summer fire, fame, and passionate transformation. "
        "Deep vermilion and crimson background with golden flame-nebula erupting outward. "
        "The guardian of summer, fire, and the blazing noon sun. "
        + STYLE_SUFFIX
    ),
    "direction/southwest.jpg": (
        "The southwestern direction — where the Vermilion Bird meets the White Tiger. "
        "A cosmic scene of crimson phoenix feathers fading into white and gold tiger stripes. "
        "Fire cooling into late summer earth, the harvest season at hand. "
        "Amber and warm gold background with flickers of red and ivory. "
        "The energy of completion, harvest, and the earth's abundance. "
        + STYLE_SUFFIX
    ),
    "direction/west.jpg": (
        "Byakko, the White Tiger of the West — a great cosmic white tiger with blazing "
        "silver-white stripes stalks through autumn starfields. "
        "Its eyes are silver moons, its breath is cool autumn wind made visible as nebula. "
        "It embodies autumn, metal, and righteous authority. "
        "Deep grey-silver background with white and silver nebula, autumn leaves of light. "
        "The guardian of autumn, metal energy, and the setting sun. "
        + STYLE_SUFFIX
    ),
    "direction/northwest.jpg": (
        "The northwestern direction — where the White Tiger meets the Black Tortoise. "
        "A cosmic scene of white tiger fur fading into dark tortoise shell patterns. "
        "Twilight deepening into night, the last silver light before winter darkness. "
        "Silver and dark blue-grey background with white stars emerging in the west. "
        "The energy of completion, letting go, and the approach of rest. "
        + STYLE_SUFFIX
    ),
    "direction/compass.jpg": (
        "All four celestial guardians united in a cosmic compass rose: "
        "Black Tortoise in the north, Azure Dragon in the east, "
        "Vermilion Bird in the south, White Tiger in the west. "
        "Each guardian glows in its elemental color — black, azure, crimson, white. "
        "They form a great mandala of protection around a golden compass center. "
        "Deep midnight background, the complete feng shui guardian formation. "
        + STYLE_SUFFIX
    ),
    # ── 季節 ──────────────────────────────────────────────────────────
    "season/spring.jpg": (
        "A luminous Azure Dragon of spring soars through a storm of cherry blossom petals. "
        "Its jade-green scales are adorned with sakura flowers. "
        "Pink and white petals swirl around its coiling form in cosmic currents. "
        "Deep purple-rose background with soft pink nebula and spring starlight. "
        "The spirit of renewal, the dragon that brings the spring rains. "
        + STYLE_SUFFIX
    ),
    "season/summer.jpg": (
        "A blazing Vermilion Phoenix rises from a cosmic sun at the height of summer. "
        "The sun behind it erupts in sixteen great golden rays. "
        "The phoenix's wings spread wide, each feather a tongue of golden flame. "
        "Deep navy blue ocean below reflects the blazing summer sun above. "
        "Intense golden-white solar energy, the peak of yang power. "
        + STYLE_SUFFIX
    ),
    "season/autumn.jpg": (
        "A majestic White Tiger walks through a cosmic autumn forest of maple trees. "
        "The leaves are made of crimson, amber, and gold stardust, swirling in cosmic wind. "
        "The tiger's white coat glows against the warm autumn nebula. "
        "Deep amber-crimson background with falling leaves of light. "
        "The spirit of autumn harvest, bittersweet beauty, and graceful letting go. "
        + STYLE_SUFFIX
    ),
    "season/winter.jpg": (
        "A Black Tortoise rests at the bottom of a frozen cosmic sea beneath a field of stars. "
        "Its shell is a perfect snowflake crystal, each plate an intricate ice pattern. "
        "Aurora borealis in blue and green dances above the frozen surface. "
        "Deep midnight blue background with ice-crystal stars and silver snow. "
        "The deep silence of winter, dormant power, ancient wisdom waiting to emerge. "
        + STYLE_SUFFIX
    ),
    # ── 風水 ──────────────────────────────────────────────────────────
    "fengshui/general.jpg": (
        "A cosmic yin-yang symbol formed by a jade dragon and a black tiger circling each other. "
        "The dragon — luminous jade green — and the tiger — deep shadow black — "
        "chase each other's tails in perfect cosmic balance. "
        "Between them, the S-curve boundary glows with golden chi energy. "
        "Deep forest green and black background with flowing chi lines of light. "
        "The eternal dance of opposing forces in perfect harmony. "
        + STYLE_SUFFIX
    ),
    "fengshui/water.jpg": (
        "A great cosmic water dragon coils through deep ocean-blue space. "
        "Its body spirals in the shape of flowing water, leaving trails of aquamarine chi. "
        "Sacred water mandalas — concentric rings like ripples — surround it. "
        "Deep navy and teal background with flowing water-energy light patterns. "
        "The purifying, cleansing, wealth-bringing energy of the water element. "
        + STYLE_SUFFIX
    ),
    # ── 汎用 ──────────────────────────────────────────────────────────
    "general/fortune_1.jpg": (
        "A luminous celestial lotus opens at the center of a vast cosmic mandala. "
        "Each petal is a universe, each stamen a star. "
        "Intricate golden sacred geometry rings expand outward from the lotus. "
        "Deep indigo background with violet and gold nebula in perfect sacred symmetry. "
        "The mandala of destiny, the wheel of fortune, ancient divine order. "
        + STYLE_SUFFIX
    ),
    "general/fortune_2.jpg": (
        "The Flower of Life sacred geometry pattern fills cosmic space, "
        "each overlapping circle a world, a breath, a heartbeat of the universe. "
        "Cyan and teal bioluminescent light traces the pattern against dark space. "
        "Deep teal-black background with the eternal mathematical pattern of creation. "
        "Peaceful, unified, the hidden order beneath all things. "
        + STYLE_SUFFIX
    ),
    "general/fortune_3.jpg": (
        "An eight-petaled cosmic lotus mandala glows in deep amethyst and violet space. "
        "Each petal contains a galaxy. The center blazes with golden-white enlightenment light. "
        "Rings of sacred geometry — triangles, hexagons, circles — orbit the lotus. "
        "Deep purple-black background with violet and gold nebula petals. "
        "The mandala of spiritual awakening and higher consciousness. "
        + STYLE_SUFFIX
    ),
    "general/mystery_1.jpg": (
        "A great all-seeing eye floats within a radiant triangle of crimson fire in deep space. "
        "The eye is wise, ancient, and all-knowing — its iris a galaxy, its pupil a black hole. "
        "Twelve long rays of golden-red light radiate from the triangle's apex. "
        "Deep crimson and dark background with mysterious red-gold energy. "
        "The oracle, the prophet, the guardian who sees all destinies. "
        + STYLE_SUFFIX
    ),
    "general/mystery_2.jpg": (
        "A cosmic all-seeing eye within a triangle of silver-blue starlight in deep space. "
        "The eye gazes from beyond time — iris a swirling galaxy of blue and silver. "
        "Cool starlight rays radiate outward, illuminating ancient symbols in the darkness. "
        "Deep midnight blue-black background with silver-white cosmic energy. "
        "Omniscient awareness, the eye that watches the wheel of fate. "
        + STYLE_SUFFIX
    ),
    "general/ichi_gon.jpg": (
        "A single luminous golden dragon coils around one brilliant star at the center of space. "
        "The dragon is small yet contains infinite power — the essence of a single truth. "
        "Golden light radiates outward from star and dragon in perfect circles. "
        "Deep black-gold background with subtle dragon scale patterns in the nebula. "
        "The power of one word, one truth, one moment of clarity from the universe. "
        + STYLE_SUFFIX
    ),
    "general/menu.jpg": (
        "A silhouette of a mysterious fortune-teller in flowing robes stands against "
        "a vast cosmic backdrop, surrounded by swirling celestial symbols. "
        "Around the figure: dragon, stars, a crystal orb, zodiac symbols glowing softly. "
        "Warm deep amber and gold background with dragon scale texture in the nebula. "
        "Premium, authoritative, the presence of a true oracle. "
        + STYLE_SUFFIX
    ),
}


def load_openai_key() -> str:
    """環境変数 → .env ファイルの順で OPENAI_API_KEY を読み込む"""
    import os
    key = os.getenv("OPENAI_API_KEY", "")
    if key:
        return key
    # ローカル実行時: .env ファイルから読み込む（UTF-8/UTF-16 両対応）
    env_path = BASE_DIR / ".env"
    for enc in ("utf-8", "utf-16", "utf-8-sig"):
        try:
            for line in env_path.read_text(encoding=enc).splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            continue
    return ""


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
