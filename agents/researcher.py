"""
リサーチャーエージェント（時雨 / 龍脈命術）

以下の3ソースを組み合わせて投稿ネタを収集する:
  1. ニュースRSS (NHK・Google Trends JP) — 今日本で話題のキーワード
  2. Claude生成リサーチ — 海外の文化・風土・グローバルトレンドを龍脈命術に変換
  3. テーマ補完 — 投稿履歴が少ないテーマを自動検出して埋める
"""

import json
import requests
from xml.etree import ElementTree as ET
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import anthropic
from config import (
    ANTHROPIC_API_KEY, MODEL_HEAVY,
    THEME_TREE_FILE, ACCOUNT_PROFILE_FILE,
)
from core.state import load_json, load_research, save_research, load_history
from core.kaiun_calendar import get_lucky_days


# ─────────────────────────────────────────
# 1. ニュースRSS 取得
# ─────────────────────────────────────────

RSS_SOURCES = [
    ("NHKニュース",          "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("Yahoo Japan ニュース", "https://news.yahoo.co.jp/rss/topics/top-picks.xml"),
    ("NHK社会",             "https://www3.nhk.or.jp/rss/news/cat1.xml"),
    ("NHKライフスタイル",   "https://www3.nhk.or.jp/rss/news/cat7.xml"),
]


def _fetch_news_rss(max_per_source: int = 8) -> list:
    """
    各RSSフィードからニュース・トレンドキーワードを取得する。
    取得失敗したソースはスキップ（フォールバック済み）。
    """
    all_items = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ShigureBot/1.0)"}

    for source_name, url in RSS_SOURCES:
        try:
            resp = requests.get(url, timeout=12, headers=headers)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")[:max_per_source]
            for item in items:
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()[:120]
                if title:
                    all_items.append({
                        "title": title,
                        "description": desc,
                        "source": source_name,
                    })
            print(f"  RSS [{source_name}]: {len(items)}件取得")
        except Exception as e:
            print(f"  RSS [{source_name}] 取得失敗: {e}")

    return all_items


# ─────────────────────────────────────────
# 2. 海外文化・グローバルトレンドリサーチ（Claude生成）
# ─────────────────────────────────────────

def _generate_cultural_research(
    client_obj: anthropic.Anthropic,
    target_themes: list,
    profile: dict,
    trend_keywords: list,
) -> list:
    """
    Claudeを使って、海外の文化・風土・季節情報を
    龍脈命術の投稿ネタとして生成する。
    """
    now = datetime.now()
    month_name = now.strftime("%B")   # 英語月名
    month_jp   = f"{now.month}月"
    day_str    = now.strftime("%Y年%m月%d日")

    # 二十四節気の目安（月→節気）
    SEKKI = {
        1: "小寒・大寒", 2: "立春・雨水", 3: "啓蟄・春分",
        4: "清明・穀雨", 5: "立夏・小満", 6: "芒種・夏至",
        7: "小暑・大暑", 8: "立秋・処暑", 9: "白露・秋分",
        10: "寒露・霜降", 11: "立冬・小雪", 12: "大雪・冬至",
    }
    sekki = SEKKI.get(now.month, "")

    trend_block = ""
    if trend_keywords:
        keywords = [t.get("title", "") for t in trend_keywords[:10]]
        trend_block = f"""
## 今日本で話題のキーワード（RSS取得）
{', '.join(keywords)}
※ 占い・方位・開運と自然につながるキーワードがあれば投稿ネタに組み込んでください。
"""

    prompt = f"""占術家「時雨（龍脈命術）」のThreads投稿用リサーチ情報を生成してください。

## 現在の日時・季節
- 日付: {day_str}
- 月: {month_jp}（{month_name}）
- 二十四節気の目安: {sekki}
{trend_block}
## リサーチ対象テーマ
{json.dumps(target_themes, ensure_ascii=False)}

## アカウント情報
- メソッド: 龍脈命術（西洋占星術・風水・吉方位・タロット・霊視の統合）
- ターゲット: 転職・引越し・恋愛・人生の岐路に立つ20〜40代
- コンセプト: 「動ける占い」を届ける

## 生成してほしいリサーチの観点

### A) 海外文化・風土の視点（必ず含める）
以下の文化圏で、風水・方位・開運に関連する慣習・信仰・最新トレンドを調べ、
龍脈命術の世界観に自然に結びつく投稿ネタを作成してください：
- 中国・台湾（本場の風水・玄空飛星・八宅派など）
- 韓国（四柱命理・奇門遁甲の現代活用）
- 東南アジア（タイ・インドネシア・ベトナムの土地の気の考え方）
- インド（ヴァースツ・シャーストラ：方位と建築の関係）
- 西洋（ケルトの方位信仰、北欧ルーン、西洋占星術の方位論）
- 日本国内（今月注目されている開運スポット・パワースポット・行事）

### B) 季節・天文イベント（必ず含める）
{month_jp}の天文・季節イベント（月相・惑星・星座の動き・二十四節気）と、
吉方位・気の流れへの影響を投稿ネタとして生成してください。

### C) グローバル開運トレンド
海外のSNS・メディアで今注目されている「スピリチュアル・開運・方位」のトレンドを
日本の読者向けに翻案した投稿ネタを生成してください。

## 出力形式（JSONのみ、説明不要）
[
  {{
    "title": "投稿ネタのタイトル",
    "theme": "{target_themes[0] if target_themes else '吉方位・風水'}",
    "hook_idea": "1行目のフックアイデア（インパクト重視）",
    "key_points": ["ポイント1", "ポイント2", "ポイント3"],
    "summary": "2〜3文の要約（海外文化や季節情報を具体的に含める）",
    "source": "cultural_research",
    "cultural_angle": "参照した文化・地域・トレンド（例: 中国風水/インドヴァースツ/ケルト方位）",
    "season_note": "季節・天文との関連（あれば）"
  }}
]

各観点（A/B/C）から最低2件ずつ、合計8〜12件生成してください。"""

    response = client_obj.messages.create(
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
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        print("  ⚠️ 文化リサーチJSON解析失敗")
        return []


# ─────────────────────────────────────────
# 3. テーマ不足チェック
# ─────────────────────────────────────────

def _get_underrepresented_themes(theme_tree: dict, history: list) -> list:
    """投稿履歴が少ないテーマを特定する"""
    from collections import Counter

    def flatten(node, prefix=""):
        items = []
        for k, v in node.items():
            label = f"{prefix}/{k}" if prefix else k
            if isinstance(v, dict):
                items.extend(flatten(v, label))
            else:
                items.append(label)
        return items

    all_themes = flatten(theme_tree)
    used_themes = Counter(h.get("theme", "") for h in history)
    return sorted(all_themes, key=lambda t: used_themes.get(t, 0))


# ─────────────────────────────────────────
# 4. Claudeによるトピック整理・エンリッチ
# ─────────────────────────────────────────

def _enrich_topics_with_claude(
    client_obj: anthropic.Anthropic,
    raw_topics: list,
    profile: dict,
    trend_context: list = None,
) -> list:
    """
    生のリサーチデータをClaude で整理し、
    龍脈命術の投稿ネタとして最適化する。
    """
    trend_block = ""
    if trend_context:
        keywords = [t.get("title", "") for t in trend_context[:8]]
        trend_block = f"""
## 今日本で話題（RSS取得済み）
{', '.join(keywords)}
※ 関連するトレンドがあれば投稿ネタに自然に組み込むこと。
"""

    prompt = f"""以下のリサーチデータを、Threads投稿ネタとして整理してください。

## アカウントプロフィール
{json.dumps(profile, ensure_ascii=False, indent=2)}
{trend_block}
## 生データ
{json.dumps(raw_topics, ensure_ascii=False, indent=2)}

## 出力形式（JSONのみ）
[
  {{
    "title": "投稿ネタのタイトル",
    "theme": "テーマパス（例: 吉方位・風水/今月の吉方位）",
    "hook_idea": "1行目のフックアイデア",
    "key_points": ["ポイント1", "ポイント2", "ポイント3"],
    "summary": "2〜3文の要約",
    "source": "cultural_research / news_rss / etc",
    "cultural_angle": "参照した文化・地域（あれば）",
    "season_note": "季節・天文との関連（あれば）"
  }}
]

ターゲット層（転職・引越し・恋愛・岐路に立つ20〜40代）に刺さるネタのみ選別し、
龍脈命術（吉方位・風水・気の流れ）の世界観に自然につながるものだけ残すこと。"""

    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        result = json.loads(raw.strip())
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return raw_topics


# ─────────────────────────────────────────
# メイン実行
# ─────────────────────────────────────────

def run(max_themes: int = 5) -> dict:
    """
    リサーチャーエージェントのメイン処理。

    Returns:
        {"researched_themes": list, "new_topics": int, "rss_count": int, "cultural_count": int}
    """
    client_obj = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    theme_tree = load_json(THEME_TREE_FILE, default={})
    profile    = load_json(ACCOUNT_PROFILE_FILE, default={})
    history    = load_history(limit=200)
    research_data = load_research()

    if not theme_tree:
        print("  ⚠️ リサーチャー: テーマツリーなし")
        return {"researched_themes": [], "new_topics": 0, "rss_count": 0, "cultural_count": 0}

    # 不足テーマを特定
    under_themes = _get_underrepresented_themes(theme_tree, history)[:max_themes]
    print(f"  📋 リサーチ対象テーマ: {under_themes}")

    # ── ステップ0: 開運日カレンダー計算（7日ごとにキャッシュ更新）──
    print("  📅 開運日カレンダー更新中...")
    try:
        lucky_days = get_lucky_days(client_obj)
        print(f"  開運日: {len(lucky_days)}件（45日分）")
    except Exception as e:
        print(f"  ⚠️ 開運日計算失敗: {e}")

    # ── ステップ1: ニュースRSS取得 ──
    print("  🌐 ニュースRSS取得中...")
    rss_items = _fetch_news_rss()
    print(f"  合計 {len(rss_items)}件のRSSアイテム取得")

    # ── ステップ2: 海外文化・トレンドリサーチ（Claude生成）──
    print("  🌏 海外文化・グローバルトレンドリサーチ生成中...")
    cultural_topics = _generate_cultural_research(
        client_obj, under_themes, profile, rss_items
    )
    print(f"  {len(cultural_topics)}件の文化リサーチ生成")

    # ── ステップ3: RSS + 文化リサーチをまとめてエンリッチ ──
    all_raw = cultural_topics  # 文化リサーチはすでにClaude生成なのでそのまま使う
    if rss_items:
        # RSSはさらにエンリッチして龍脈命術ネタに変換
        print("  ✨ RSSネタを龍脈命術向けに変換中...")
        rss_enriched = _enrich_topics_with_claude(
            client_obj, rss_items, profile, trend_context=rss_items
        )
        all_raw = cultural_topics + rss_enriched

    # ── ステップ4: 既存データに追記（重複除外）──
    existing_titles = {t.get("title", "") for t in research_data.get("topics", [])}
    new_topics = [t for t in all_raw if t.get("title", "") not in existing_titles]

    research_data["topics"] = research_data.get("topics", []) + new_topics
    research_data["last_updated"] = datetime.now().isoformat()
    research_data["last_rss_count"] = len(rss_items)
    research_data["last_cultural_count"] = len(cultural_topics)

    # 最新500件のみ保持
    if len(research_data["topics"]) > 500:
        research_data["topics"] = research_data["topics"][-500:]

    save_research(research_data)

    print(f"  ✅ リサーチャー完了: {len(new_topics)}件の新ネタ追加")
    return {
        "researched_themes": under_themes,
        "new_topics": len(new_topics),
        "rss_count": len(rss_items),
        "cultural_count": len(cultural_topics),
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
