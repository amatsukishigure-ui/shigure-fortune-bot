"""
開運日カレンダーモジュール

Claude API を使って日本の伝統暦を正確に計算し、
今月・来月の開運日リストをキャッシュする。

計算対象:
  - 日干支（60日周期）
  - 六曜（大安・友引・先勝・先負・仏滅・赤口）
  - 一粒万倍日（節気月×日支の組み合わせ）
  - 天赦日（季節×日干支の特定組み合わせ、年間約6回）
  - 寅の日・巳の日・戌の日（12日周期）
  - 天一天上（天一神が天上している16日間）
"""

import json
from datetime import datetime, timedelta, date
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import ANTHROPIC_API_KEY, MODEL_HEAVY, DATA_DIR

LUCKY_DAYS_FILE = DATA_DIR / "lucky_days_cache.json"
CACHE_DAYS = 7  # 7日ごとに再計算


def _load_cache() -> dict:
    if LUCKY_DAYS_FILE.exists():
        try:
            return json.loads(LUCKY_DAYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(data: dict) -> None:
    LUCKY_DAYS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_cache_fresh(cache: dict) -> bool:
    last = cache.get("calculated_at")
    if not last:
        return False
    try:
        delta = datetime.now() - datetime.fromisoformat(last)
        return delta.days < CACHE_DAYS
    except Exception:
        return False


def _weekday_jp(d: date) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]


def get_moon_phase(d: date = None) -> dict:
    """
    外部ライブラリ不要で月相を計算する。
    基準新月: 2000-01-06 (J2000近傍の観測値)
    朔望周期: 29.53058867日
    """
    if d is None:
        d = date.today()
    SYNODIC = 29.53058867
    reference = date(2000, 1, 6)
    days_since = (d - reference).days
    phase = (days_since % SYNODIC) / SYNODIC

    if phase < 0.0625:
        name, emoji, energy = "新月", "🌑", "リセット・新しい始まり"
    elif phase < 0.1875:
        name, emoji, energy = "三日月", "🌒", "意図を定める・準備"
    elif phase < 0.3125:
        name, emoji, energy = "上弦の月", "🌓", "行動・決断・前進"
    elif phase < 0.4375:
        name, emoji, energy = "十三夜", "🌔", "勢い・拡大・加速"
    elif phase < 0.5625:
        name, emoji, energy = "満月", "🌕", "完成・解放・感謝"
    elif phase < 0.6875:
        name, emoji, energy = "十六夜", "🌖", "収穫・見直し・内省"
    elif phase < 0.8125:
        name, emoji, energy = "下弦の月", "🌗", "手放し・整理・浄化"
    else:
        name, emoji, energy = "晦日月", "🌘", "静寂・完了・次への準備"

    # 新月・満月まであと何日かを計算
    days_to_new = round((1.0 - phase) * SYNODIC) if phase >= 0.5 else round((0.5 - phase) * SYNODIC)
    next_event = "新月" if phase >= 0.5 else "満月"

    return {
        "phase_name": name,
        "phase_emoji": emoji,
        "energy": energy,
        "phase_value": round(phase, 3),
        "days_to_next": int(days_to_new),
        "next_event": next_event,
    }


def _calculate_lucky_days_via_claude(
    client_obj,
    start: date,
    days: int = 45,
) -> list:
    """
    Claude に日本の伝統暦を計算させ、開運日リストを返す。
    干支・六曜・一粒万倍日・天赦日などは数学的に決定論的なので
    Claude の計算で正確な結果が得られる。
    """
    end = start + timedelta(days=days - 1)

    # 日付リスト（最初の14日を明示）
    date_lines = "\n".join(
        f"- {(start + timedelta(days=i)).strftime('%Y年%m月%d日')}（{_weekday_jp(start + timedelta(days=i))}）"
        for i in range(min(14, days))
    )
    if days > 14:
        date_lines += f"\n（以降 {end.strftime('%m月%d日')} まで同様に計算）"

    prompt = f"""日本の伝統的な暦（こよみ）を正確に計算し、開運日を判定してください。

## 対象期間
{start.strftime('%Y年%m月%d日')} 〜 {end.strftime('%Y年%m月%d日')}（{days}日間）

## 計算対象日（代表）
{date_lines}

## 計算ルール（必ず守ること）

### 干支（日干支）
60日周期（十干×十二支）。甲子=0として日数から算出。

### 六曜
旧暦の月日から: (旧暦月 + 旧暦日) mod 6
0=先勝, 1=友引, 2=先負, 3=仏滅, 4=大安, 5=赤口
※月初めは必ず先勝からリセット（月ごとに起点が変わる）

### 一粒万倍日（節気月×日支の正式な組み合わせ）
節気月の「月支」と「日支」の組み合わせで決まる:
- 寅月(2月節)・午月(6月節): 子日・午日
- 卯月(3月節)・未月(7月節): 酉日・卯日
- 辰月(4月節)・申月(8月節): 子日・午日（※ 辰申月は子午）
- 巳月(5月節)・酉月(9月節): 卯日・酉日（※ 巳酉月は卯酉）
- 子月(11月節)・辰月(4月節): 寅日・申日 → ※諸説あり、最も一般的な説を使用
正確な「一粒万倍日一覧（2025年・2026年版）」に基づいて判定すること。

### 天赦日（年間約5〜6回）
季節による干支の組み合わせ:
- 春（立春〜立夏前）: 戊寅日
- 夏（立夏〜立秋前）: 甲午日
- 秋（立秋〜立冬前）: 戊申日
- 冬（立冬〜立春前）: 甲子日

### 寅の日・巳の日・戌の日（12日周期）
日支が「寅」「巳」「戌」の日。基準から12日ごとに繰り返す。

### 天一天上（16日間のサイクル）
天一神（てんいちじん）が天上にいる期間（全方位吉）。
戊子日〜癸巳日 または 甲午日〜己亥日 の16日間。

## 出力ルール
- 何らかの開運日に該当する日のみ出力（普通の日は除外）
- 大安・友引でも他の吉日と重なる場合のみ優先して含める
- JSON形式のみ出力（説明文・コメント不要）

## 出力形式
[
  {{
    "date": "YYYY-MM-DD",
    "date_jp": "M月D日",
    "weekday": "月",
    "kanshi": "甲子",
    "rokuyou": "大安",
    "lucky_types": ["天赦日", "大安", "一粒万倍日"],
    "lucky_score": 3,
    "summary": "天赦日×大安×一粒万倍日の最強トリプル開運日",
    "best_actions": ["新しいことを始める", "財布の新調", "引越し"],
    "kichi_houi": ["東", "南東"],
    "note": "この日に行動したことは万倍の力を持つとされる"
  }}
]

lucky_score は重なる開運日の数（1〜5）。スコアが高いほど希少な吉日。"""

    import anthropic
    client = client_obj or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model=MODEL_HEAVY,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    # コードブロック除去
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        result = json.loads(raw.strip())
        if not isinstance(result, list):
            return []
        # 自己補正・除外注記が含まれるエントリを除去
        filtered = [
            d for d in result
            if not any(
                kw in (d.get("summary", "") + d.get("note", ""))
                for kw in ["※補正", "除外", "ではありません", "誤り"]
            )
        ]
        return filtered
    except json.JSONDecodeError as e:
        print(f"  ⚠️ 開運日JSON解析失敗: {e}")
        return []


def get_lucky_days(client_obj=None, force_refresh: bool = False) -> list:
    """
    今月〜来月の開運日リストを返す。
    キャッシュが新鮮なら再計算しない。

    Returns:
        list[dict] - 開運日情報のリスト
    """
    cache = _load_cache()

    if not force_refresh and _is_cache_fresh(cache):
        print("  📅 開運日キャッシュ使用（再計算スキップ）")
        return cache.get("days", [])

    print("  📅 開運日を計算中（Claude API）...")
    today = date.today()
    # 今日から45日分計算
    days_list = _calculate_lucky_days_via_claude(client_obj, start=today, days=45)
    print(f"  {len(days_list)}件の開運日を取得")

    cache = {
        "calculated_at": datetime.now().isoformat(),
        "valid_days": 45,
        "start_date": today.isoformat(),
        "days": days_list,
    }
    _save_cache(cache)
    return days_list


def get_today_lucky_info() -> dict | None:
    """
    今日の開運日情報を返す。開運日でない場合は None。
    キャッシュから取得（API呼び出しなし）。
    """
    cache = _load_cache()
    today_str = date.today().isoformat()
    for d in cache.get("days", []):
        if d.get("date") == today_str:
            return d
    return None


def get_upcoming_lucky_days(n: int = 3) -> list:
    """
    今日以降の開運日を n 件返す（今日含む）。
    """
    cache = _load_cache()
    today_str = date.today().isoformat()
    upcoming = [
        d for d in cache.get("days", [])
        if d.get("date", "") >= today_str
    ]
    return sorted(upcoming, key=lambda x: x["date"])[:n]


def get_lucky_days_this_month() -> list:
    """今月の開運日を返す。"""
    cache = _load_cache()
    today = date.today()
    prefix = today.strftime("%Y-%m")
    return [
        d for d in cache.get("days", [])
        if d.get("date", "").startswith(prefix)
    ]


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    days = get_lucky_days(client, force_refresh=True)
    print(f"\n開運日 {len(days)}件:")
    for d in days[:10]:
        score = d.get("lucky_score", 1)
        stars = "★" * score
        print(f"  {d['date']} ({d.get('weekday','')}) [{d.get('kanshi','')}] {d.get('rokuyou','')} {stars}")
        print(f"    種別: {', '.join(d.get('lucky_types', []))}")
        print(f"    {d.get('summary', '')}")
