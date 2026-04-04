"""
リサーチャーエージェント

テーマツリーを参照して不足テーマを特定し、
YouTube/X等からネタを収集してresearch_data.jsonに保存する。

※ 実際のYouTube/X API呼び出しはモック実装。
  本番環境ではAPIキーを設定してアンコメント。
"""

import json
import anthropic
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, MODEL_HEAVY,
    THEME_TREE_FILE, ACCOUNT_PROFILE_FILE,
)
from core.state import load_json, load_research, save_research, load_history


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

    # 使用回数が少ない順にソート
    return sorted(all_themes, key=lambda t: used_themes.get(t, 0))


def _mock_search_youtube(query: str) -> list:
    """
    YouTube検索のモック実装。
    本番: youtube_transcript_api + YouTube Data API v3 を使う。
    """
    return [
        {
            "title": f"【{query}】完全ガイド2025年版",
            "summary": f"{query}について解説。具体的な方法と注意点を紹介。",
            "source": "youtube_mock",
        },
        {
            "title": f"{query}で失敗する人の共通点",
            "summary": f"多くの人が{query}で陥るミスを分析。",
            "source": "youtube_mock",
        },
    ]


def _enrich_topics_with_claude(
    client_obj: anthropic.Anthropic,
    raw_topics: list,
    profile: dict,
) -> list:
    """Claudeでトピックを整理・要約する"""

    prompt = f"""以下のリサーチデータをThreads投稿ネタとして整理してください。

## アカウントプロフィール
{json.dumps(profile, ensure_ascii=False, indent=2)}

## 生データ
{json.dumps(raw_topics, ensure_ascii=False, indent=2)}

## 出力形式（JSONのみ）
[
  {{
    "title": "投稿ネタのタイトル",
    "theme": "テーマパス（例: 転職準備/書類作成）",
    "hook_idea": "1行目のフックアイデア",
    "key_points": ["ポイント1", "ポイント2", "ポイント3"],
    "summary": "2〜3文の要約",
    "source": "youtube/x/etc"
  }}
]

アカウントのターゲット層に刺さるネタだけを選別すること。"""

    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw_topics


def run(max_themes: int = 3) -> dict:
    """
    リサーチャーエージェントのメイン処理。

    Returns:
        {"researched_themes": list, "new_topics": int}
    """
    client_obj = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    theme_tree = load_json(THEME_TREE_FILE, default={})
    profile = load_json(ACCOUNT_PROFILE_FILE, default={})
    history = load_history(limit=200)
    research_data = load_research()

    if not theme_tree:
        print("⚠️ リサーチャー: テーマツリーなし")
        return {"researched_themes": [], "new_topics": 0}

    # 不足テーマを特定
    under_themes = _get_underrepresented_themes(theme_tree, history)[:max_themes]
    print(f"🔍 リサーチ対象テーマ: {under_themes}")

    raw_topics = []
    for theme in under_themes:
        # テーマの末尾ノード名でYouTube検索
        search_query = theme.split("/")[-1]
        results = _mock_search_youtube(search_query)
        for r in results:
            r["theme"] = theme
        raw_topics.extend(results)

    if not raw_topics:
        return {"researched_themes": under_themes, "new_topics": 0}

    # Claudeでネタを整理
    enriched = _enrich_topics_with_claude(client_obj, raw_topics, profile)

    # 既存データに追記（重複除外）
    existing_titles = {t.get("title") for t in research_data.get("topics", [])}
    new_topics = [t for t in enriched if t.get("title") not in existing_titles]

    research_data["topics"] = research_data.get("topics", []) + new_topics
    # 最新500件のみ保持
    if len(research_data["topics"]) > 500:
        research_data["topics"] = research_data["topics"][-500:]

    save_research(research_data)

    print(f"✅ リサーチャー: {len(new_topics)}件の新ネタ追加")
    return {
        "researched_themes": under_themes,
        "new_topics": len(new_topics),
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
