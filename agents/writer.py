"""
ライターエージェント（時雨 / 龍脈命術）

リサーチデータとアナリストのフィードバックを元に投稿を生成する。
品質スコア・類似度チェックを経て、Threads/X 両方のキューに追加する。
"""

import json
import anthropic
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, MODEL_HEAVY,
    MAX_QUALITY_RETRIES, MAX_SAME_PATTERN_CONSECUTIVE, MAX_SAME_THEME_CONSECUTIVE,
    ACCOUNT_PROFILE_FILE, POST_PATTERNS_FILE, THEME_TREE_FILE, HOOK_EXAMPLES_FILE,
    CONTENT_SCHEDULE, DATA_DIR,
)
from core.state import (
    load_json, load_history, load_research, enqueue_post,
)
from core.similarity import SimilarityChecker
from core.quality import score_with_retry
from core.formatter import format_for_x, format_for_threads, get_x_hashtags_for_theme
from agents.x_poster import enqueue_x_post


def _load_knowledge() -> dict:
    return {
        "profile": load_json(ACCOUNT_PROFILE_FILE, default={}),
        "patterns": load_json(POST_PATTERNS_FILE, default=[]),
        "theme_tree": load_json(THEME_TREE_FILE, default={}),
        "hook_examples": load_json(HOOK_EXAMPLES_FILE, default=[]),
    }


def _get_recent_patterns(history: list, n: int = MAX_SAME_PATTERN_CONSECUTIVE) -> list:
    return [h.get("pattern_id") for h in history[-n:] if h.get("pattern_id")]


def _get_recent_themes(history: list, n: int = MAX_SAME_THEME_CONSECUTIVE) -> list:
    return [h.get("theme") for h in history[-n:] if h.get("theme")]


def _get_todays_schedule() -> list:
    """今日の曜日に対応するコンテンツスケジュールを返す"""
    day = datetime.now().strftime("%A")  # 'Monday', 'Tuesday', ...
    return CONTENT_SCHEDULE.get(day, CONTENT_SCHEDULE["Monday"])


def _select_pattern(patterns: list, recent_patterns: list, platform_hint: str = "threads") -> dict:
    import random
    blocked = set(recent_patterns)
    # プラットフォームに対応するパターンのみ
    available = [
        p for p in patterns
        if p.get("id") not in blocked and platform_hint in p.get("platforms", ["threads"])
    ]
    if not available:
        available = [p for p in patterns if platform_hint in p.get("platforms", ["threads"])]

    weights = [p.get("weight", 1.0) for p in available]
    return random.choices(available, weights=weights, k=1)[0]


def _select_theme(theme_tree: dict, recent_themes: list, schedule_hint: str = "") -> str:
    import random

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

    # スケジュールヒントに近いテーマを優先
    if schedule_hint:
        hint_themes = [t for t in all_themes if any(
            keyword in t for keyword in schedule_hint.replace("・", "/").split("/")
        )]
        if hint_themes:
            return random.choice(hint_themes)

    recent_set = set(recent_themes)
    fresh = [t for t in all_themes if t not in recent_set]
    return random.choice(fresh if fresh else all_themes)


def _build_prompt(
    pattern: dict,
    theme: str,
    knowledge: dict,
    research_topics: list,
    analyst_feedback: str,
    feedback_for_rewrite: str = "",
) -> str:
    """投稿生成プロンプトを構築する"""
    profile = knowledge["profile"]
    profile_summary = (
        f"占術家名: {profile.get('name')}\n"
        f"肩書き: {profile.get('title')}\n"
        f"独自メソッド: {profile.get('original_method')}\n"
        f"コアコンセプト: {profile.get('core_concept')}\n"
        f"口調: {profile.get('tone')}\n"
        f"NGワード: {', '.join(profile.get('ng_words', []))}\n"
        f"シグネチャ: 占術家 時雨（投稿の最後、世界観投稿・長め投稿のみ使用）"
    )

    hooks_sample = "\n".join(f"- {h}" for h in knowledge["hook_examples"][:8])

    research_text = "\n".join(
        f"- {t.get('title', '')}: {t.get('summary', '')}"
        for t in research_topics[:5]
        if any(k in t.get("theme", "") for k in theme.split("/"))
    ) or "（関連リサーチなし）"

    rewrite_block = ""
    if feedback_for_rewrite:
        rewrite_block = f"\n## 前回の添削フィードバック（必ず反映）\n{feedback_for_rewrite}\n"

    return f"""あなたは占術家「時雨（しぐれ）」として、Threadsに投稿するテキストを1本生成してください。

## アカウント情報
{profile_summary}

## 投稿パターン
- パターン: {pattern.get('name')}
- 説明: {pattern.get('description')}
- 例: {pattern.get('example', '')}

## 投稿テーマ
{theme}

## 参考リサーチ
{research_text}

## アナリストフィードバック
{analyst_feedback or '（まだデータなし）'}

## バズった1行目の構造例
{hooks_sample}
{rewrite_block}
## 生成ルール
- 文字数: 80〜400文字（星座一覧は450文字まで可）。必ずこの範囲に収めること。
- 口調は「時雨」として一貫させる
- 1行目は必ずインパクトがある一文
- 風水・吉方位・龍脈命術の世界観を自然に織り込む
- 最後の誘導文（「プロフィールから」等）は週1回のメニュー案内型のみ
- 短文型（一言型）は3行以内
- 「占術家 時雨」のシグネチャは世界観投稿・エピソード投稿のみ末尾に追加
- 絵文字は控えめに（0〜2個）
- NGワードは使わない

## 出力
投稿本文のみ。説明・メタ情報は不要。"""


def _generate_post(
    client_obj: anthropic.Anthropic,
    pattern: dict,
    theme: str,
    knowledge: dict,
    research_topics: list,
    analyst_feedback: str,
    feedback_for_rewrite: str = "",
) -> str:
    prompt = _build_prompt(
        pattern, theme, knowledge, research_topics,
        analyst_feedback, feedback_for_rewrite,
    )
    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def run(batch_size: int = 5) -> dict:
    """
    ライターエージェントのメイン処理。
    生成した投稿をThreadsキューとXキューの両方に追加する。

    Returns:
        {"generated": int, "queued_threads": int, "queued_x": int, "rejected": int}
    """
    client_obj = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    knowledge = _load_knowledge()
    research_data = load_research()
    history = load_history(limit=100)

    # アナリストフィードバック
    feedback_file = DATA_DIR / "analyst_feedback.txt"
    analyst_feedback = ""
    if feedback_file.exists():
        analyst_feedback = feedback_file.read_text(encoding="utf-8")

    # 類似度チェッカー初期化
    checker = SimilarityChecker()
    checker.load_history([h.get("text", "") for h in history if h.get("text")])

    recent_patterns = _get_recent_patterns(history)
    recent_themes = _get_recent_themes(history)
    todays_schedule = _get_todays_schedule()

    queued_threads = 0
    queued_x = 0
    rejected = 0
    details = []

    for i in range(batch_size):
        # スケジュールヒントを使ってテーマ選択（曜日固定コンテンツ）
        schedule_hint = todays_schedule[i % len(todays_schedule)]
        theme = _select_theme(knowledge["theme_tree"], recent_themes, schedule_hint)
        pattern = _select_pattern(knowledge["patterns"], recent_patterns)

        def rewrite_func(post_text: str, feedback: str) -> str:
            return _generate_post(
                client_obj, pattern, theme, knowledge,
                research_data.get("topics", []), analyst_feedback,
                feedback_for_rewrite=feedback,
            )

        # 初回生成
        first_draft = _generate_post(
            client_obj, pattern, theme, knowledge,
            research_data.get("topics", []), analyst_feedback,
        )
        if not first_draft:
            rejected += 1
            continue

        # 品質チェック
        result = score_with_retry(
            first_draft, knowledge["profile"],
            rewrite_func=rewrite_func, max_retries=MAX_QUALITY_RETRIES,
        )
        if not result["accepted"]:
            rejected += 1
            details.append({"status": "rejected_quality", "score": result["score_result"]["average"], "theme": theme})
            continue

        final_post = result["post"]

        # 類似度チェック
        is_dup, sim_score = checker.is_duplicate(final_post)
        if is_dup:
            rejected += 1
            details.append({"status": "rejected_similarity", "similarity": sim_score, "theme": theme})
            continue

        # Threads版・X版を生成
        threads_text = format_for_threads(final_post)
        hashtags = get_x_hashtags_for_theme(theme)
        x_text = format_for_x(final_post, hashtags)

        post_obj = {
            "text": threads_text,
            "x_text": x_text,
            "theme": theme,
            "pattern_id": pattern.get("id"),
            "quality_score": result["score_result"]["average"],
            "attempts": result["attempts"],
        }

        # Threadsキューに追加
        enqueue_post(post_obj)
        queued_threads += 1

        # X対応パターンならXキューにも追加
        if "x" in pattern.get("platforms", []):
            enqueue_x_post(post_obj)
            queued_x += 1

        checker.add_to_corpus(final_post)
        recent_patterns.append(pattern.get("id"))
        recent_themes.append(theme)

        details.append({
            "status": "queued",
            "score": result["score_result"]["average"],
            "theme": theme,
            "pattern": pattern.get("name"),
            "x_queued": "x" in pattern.get("platforms", []),
            "preview": threads_text[:50] + "...",
        })

    return {
        "generated": batch_size,
        "queued_threads": queued_threads,
        "queued_x": queued_x,
        "rejected": rejected,
        "details": details,
    }


if __name__ == "__main__":
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    result = run(batch_size=batch)
    print(json.dumps(result, ensure_ascii=False, indent=2))
