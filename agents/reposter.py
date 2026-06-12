"""
リポスターエージェント（時雨 / 龍脈命術）

30〜60日前の高パフォーマンス投稿を再生成してキューに追加する。
日付・時期表現を今の季節に合わせて書き換える。
週1回程度（月曜の daily 実行時）に動作する。
"""

import re
from datetime import datetime, timezone, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import anthropic
from config import ANTHROPIC_API_KEY, MODEL_LIGHT, ACCOUNT_PROFILE_FILE
from core.state import (
    load_history, load_performance, load_json, enqueue_pending,
)
from agents.writer import _get_season_context, JST, WEEKDAY_JP


# リポスト対象の条件
REPOST_MIN_DAYS_AGO = 30      # 最低 30 日前以降の投稿
REPOST_MAX_DAYS_AGO = 90      # 最大 90 日前まで遡る
REPOST_MIN_LIKES = 3          # いいね 3 以上
REPOST_MIN_VIEWS = 100        # views 100 以上
REPOST_MAX_PER_RUN = 2        # 1回の実行で最大 2 件追加
# リポストしないパターン（時事性が高すぎるもの）
SKIP_PATTERNS = {"kaiun_day", "yoru_kichi", "menu_guide", "follow_cta"}


def _update_date_expressions(text: str, profile: dict) -> str:
    """
    投稿文中の「今週」「今月」「今日」などの時間表現を
    Claude を使って現在時点に合わせて書き換える。
    """
    now_jst = datetime.now(JST)
    season_ctx = _get_season_context(now_jst)
    wd = WEEKDAY_JP[now_jst.weekday()]
    today_jp = f"{now_jst.month}月{now_jst.day}日（{wd}）"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""以下の投稿文にある時間表現（「今週」「今月」「今日」「この季節」「春分」「秋分」など）を、
現在の時点（{today_jp}、{now_jst.year}年）に合わせて自然に書き換えてください。

【現在の季節情報】
{season_ctx}

【書き換えルール】
- 日付・曜日・季節が現在と矛盾しないようにする
- NG節気（上記に記載）は使わない
- 文章の構成・トーン・文字数はできる限り保つ（大幅な書き換えは不要）
- 書き換えが不要な箇所はそのまま残す

【投稿文】
{text}

書き換えた投稿文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _find_repost_candidates() -> list:
    """リポスト候補を履歴 × パフォーマンスから抽出する"""
    history = load_history(limit=500)
    perf_list = load_performance()
    perf_map = {p["post_id"]: p for p in perf_list}

    now = datetime.now(timezone.utc)
    cutoff_near = (now - timedelta(days=REPOST_MIN_DAYS_AGO)).isoformat()
    cutoff_far  = (now - timedelta(days=REPOST_MAX_DAYS_AGO)).isoformat()

    candidates = []
    for h in history:
        tid = h.get("threads_post_id")
        posted_at = h.get("posted_at", "")
        pattern_id = h.get("pattern_id", "")

        if not tid or not posted_at:
            continue
        if pattern_id in SKIP_PATTERNS:
            continue
        if h.get("is_repost"):
            continue  # リポスト済みは除外

        # 投稿日が対象範囲内か
        if not (cutoff_far <= posted_at <= cutoff_near):
            continue

        if tid not in perf_map:
            continue

        p = perf_map[tid]
        likes = p.get("likes", 0)
        views = p.get("views", 0)

        if likes >= REPOST_MIN_LIKES and views >= REPOST_MIN_VIEWS:
            score = likes * 3 + views * 0.05
            candidates.append({
                "history": h,
                "perf": p,
                "score": score,
            })

    # スコア降順で返す
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def run() -> dict:
    """
    Returns:
        {"reposted": int, "candidates_found": int}
    """
    profile = load_json(ACCOUNT_PROFILE_FILE, default={})
    candidates = _find_repost_candidates()

    if not candidates:
        print("  ♻️  リポスト候補なし（条件を満たす投稿がありません）")
        return {"reposted": 0, "candidates_found": 0}

    print(f"  ♻️  リポスト候補: {len(candidates)}件 → 上位{REPOST_MAX_PER_RUN}件を再生成")

    reposted = 0
    for cand in candidates[:REPOST_MAX_PER_RUN]:
        h = cand["history"]
        original_text = h.get("text", "")
        if not original_text:
            continue

        try:
            updated_text = _update_date_expressions(original_text, profile)
            post_obj = {
                "text": updated_text,
                "theme": h.get("theme", ""),
                "pattern_id": h.get("pattern_id", ""),
                "quality_score": h.get("quality_score", 0),
                "attempts": 1,
                "x_eligible": h.get("x_eligible", False),
                "is_repost": True,
                "original_post_id": h.get("threads_post_id"),
            }
            enqueue_pending(post_obj)
            reposted += 1
            print(f"  ♻️  リポスト追加: [{h.get('pattern_id')}] {updated_text[:40]}...")
        except Exception as e:
            print(f"  ♻️  リポスト生成エラー: {e}")

    return {"reposted": reposted, "candidates_found": len(candidates)}


if __name__ == "__main__":
    import json
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
