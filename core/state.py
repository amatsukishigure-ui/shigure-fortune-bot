"""
JSON状態管理モジュール

全エージェントが共有するJSONファイルの読み書きを管理する。
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    POST_QUEUE_FILE, PENDING_POSTS_FILE,
    POST_HISTORY_FILE, PERFORMANCE_FILE,
    RESEARCH_FILE, ERROR_LOG_FILE, KILL_SWITCH_FILE, DATA_DIR,
    X_QUEUE_FILE,
    FOLLOWER_HISTORY_FILE, REPLIED_COMMENTS_FILE, OUR_REPLY_IDS_FILE,
    HOURLY_STATS_FILE,
)


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any = None) -> Any:
    _ensure_data_dir()
    if not path.exists():
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Post Queue ────────────────────────────────────────────────

def load_queue() -> list:
    return load_json(POST_QUEUE_FILE, default=[])


def save_queue(queue: list) -> None:
    save_json(POST_QUEUE_FILE, queue)


def enqueue_post(post: dict) -> None:
    """キューに投稿を追加"""
    queue = load_queue()
    post["queued_at"] = datetime.now().isoformat()
    queue.append(post)
    save_queue(queue)


def dequeue_post() -> dict | None:
    """キューから次の投稿を取り出す"""
    queue = load_queue()
    if not queue:
        return None
    post = queue.pop(0)
    save_queue(queue)
    return post


# ── Pending Posts（承認待ち） ─────────────────────────────────

def load_pending() -> list:
    return load_json(PENDING_POSTS_FILE, default=[])


def save_pending(posts: list) -> None:
    save_json(PENDING_POSTS_FILE, posts)


def enqueue_pending(post: dict) -> None:
    """生成した投稿を承認待ちリストに追加する"""
    pending = load_pending()
    post["pending_at"] = datetime.now().isoformat()
    pending.append(post)
    save_pending(pending)


def approve_pending(indices: list = None) -> int:
    """
    承認待ちから指定インデックスをキューに移動。
    indices=None で全件承認。
    X対応投稿は X キューにも追加する。

    Returns: 承認した件数
    """
    from agents.x_poster import enqueue_x_post  # 循環インポート回避

    pending = load_pending()
    if not pending:
        return 0

    if indices is None:
        to_approve = list(enumerate(pending))
        remaining = []
    else:
        idx_set = set(indices)
        to_approve = [(i, p) for i, p in enumerate(pending) if i in idx_set]
        remaining = [p for i, p in enumerate(pending) if i not in idx_set]

    count = 0
    for _, post in to_approve:
        enqueue_post(post)
        count += 1
        # X対応パターンならXキューにも追加（一時停止中は追加しない）
        from config import X_POSTING_ENABLED
        if post.get("x_eligible") and X_POSTING_ENABLED:
            enqueue_x_post(post)

    save_pending(remaining)

    # 承認後にキューをインパクト順に並び替え
    # 高エンゲージパターン（caligula・zodiac_target・hoshi_betsu・ichi_gon）を先頭に
    _sort_queue_by_impact()

    return count


# 高インパクトパターン（avg views 実績順）
_HIGH_IMPACT_PATTERNS = {
    "caligula": 0,
    "ichi_gon": 1,
    "hoshi_betsu": 2,
    "zodiac_target": 3,
}


def _sort_queue_by_impact() -> None:
    """
    キューを高インパクトパターン優先に並び替える。

    ただし「今日」「明日」「曜日」「月日」を含む投稿は
    並び替え対象から除外する。
    これらは生成時の推定投稿時刻に基づいて書かれており、
    スロットがずれると曜日・日付が狂うため元の順序を保つ。
    """
    queue = load_queue()
    if len(queue) <= 1:
        return

    # 時刻・曜日に敏感な投稿かどうか判定
    _TIME_KEYWORDS = ("今日", "明日", "今週", "来週", "月曜", "火曜", "水曜",
                       "木曜", "金曜", "土曜", "日曜", "6月", "7月", "8月")

    def _is_time_sensitive(post: dict) -> bool:
        text = post.get("text", "")
        pattern = post.get("pattern_id", "")
        if pattern in ("kaiun_day", "yoru_kichi", "hoshi_betsu"):
            return True
        return any(kw in text for kw in _TIME_KEYWORDS)

    def _priority(post: dict) -> int:
        if _is_time_sensitive(post):
            return 50  # 時刻敏感な投稿は中間優先度（並び替えの影響を最小化）
        return _HIGH_IMPACT_PATTERNS.get(post.get("pattern_id", ""), 99)

    # stable sort: 高インパクトを先頭に、時刻敏感な投稿は元の相対順序を維持
    queue.sort(key=_priority)
    save_queue(queue)


def reject_pending(index: int) -> bool:
    """承認待ちから指定インデックスを削除する"""
    pending = load_pending()
    if index < 0 or index >= len(pending):
        return False
    removed = pending.pop(index)
    save_pending(pending)
    return True


# ── Post History ──────────────────────────────────────────────

def load_history(limit: int = None) -> list:
    history = load_json(POST_HISTORY_FILE, default=[])
    if limit:
        return history[-limit:]
    return history


def add_to_history(post: dict) -> None:
    history = load_json(POST_HISTORY_FILE, default=[])
    post["recorded_at"] = datetime.now().isoformat()
    history.append(post)
    # 最新1000件のみ保持
    if len(history) > 1000:
        history = history[-1000:]
    save_json(POST_HISTORY_FILE, history)


# ── Performance Data ──────────────────────────────────────────

def load_performance() -> list:
    return load_json(PERFORMANCE_FILE, default=[])


def update_performance(post_id: str, metrics: dict) -> None:
    perf = load_performance()
    for entry in perf:
        if entry.get("post_id") == post_id:
            entry.update(metrics)
            entry["fetched_at"] = datetime.now().isoformat()
            save_json(PERFORMANCE_FILE, perf)
            return
    # 新規追加
    metrics["post_id"] = post_id
    metrics["fetched_at"] = datetime.now().isoformat()
    perf.append(metrics)
    save_json(PERFORMANCE_FILE, perf)


# ── Research Data ─────────────────────────────────────────────

def load_research() -> dict:
    return load_json(RESEARCH_FILE, default={"topics": [], "last_updated": None})


def save_research(data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat()
    save_json(RESEARCH_FILE, data)


# ── Error Log ─────────────────────────────────────────────────

def log_error(agent: str, error: str) -> int:
    """エラーをログし、直近の連続エラー数を返す"""
    errors = load_json(ERROR_LOG_FILE, default=[])
    errors.append({
        "agent": agent,
        "error": error,
        "timestamp": datetime.now().isoformat(),
    })
    save_json(ERROR_LOG_FILE, errors)

    # 直近の連続エラー数をカウント（同エージェント）
    recent = [e for e in reversed(errors) if e["agent"] == agent]
    consecutive = 0
    for e in recent:
        if e["agent"] == agent:
            consecutive += 1
        else:
            break
    return consecutive


def clear_errors(agent: str) -> None:
    errors = load_json(ERROR_LOG_FILE, default=[])
    # 対象エージェントの直近エラーをクリア（リセット）
    save_json(ERROR_LOG_FILE, [e for e in errors if e["agent"] != agent])


# ── Kill Switch ───────────────────────────────────────────────

def is_killed() -> bool:
    return KILL_SWITCH_FILE.exists()


def activate_kill_switch(reason: str = "") -> None:
    KILL_SWITCH_FILE.write_text(
        f"KILLED at {datetime.now().isoformat()}\n{reason}"
    )
    print(f"⛔ KILL SWITCH activated: {reason}")


def deactivate_kill_switch() -> None:
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        print("✅ Kill switch deactivated")


# ── Follower History ──────────────────────────────────────────

def load_follower_history() -> list:
    return load_json(FOLLOWER_HISTORY_FILE, default=[])


def add_follower_snapshot(count: int) -> None:
    """フォロワー数のスナップショットを保存（1日1回）"""
    history = load_follower_history()
    today = datetime.now().date().isoformat()
    # 同日に既にある場合は上書き
    history = [h for h in history if h.get("date") != today]
    history.append({"count": count, "date": today})
    history = sorted(history, key=lambda x: x["date"])[-365:]
    save_json(FOLLOWER_HISTORY_FILE, history)


def get_follower_growth(days: int = 7) -> dict:
    """直近 N 日のフォロワー増減を返す"""
    history = load_follower_history()
    if len(history) < 2:
        return {"current": None, "delta": None, "days": days}
    current = history[-1]["count"]
    target_date_obj = datetime.now().date()
    from datetime import timedelta
    cutoff = (target_date_obj - timedelta(days=days)).isoformat()
    past = [h for h in history if h["date"] <= cutoff]
    baseline = past[-1]["count"] if past else history[0]["count"]
    return {"current": current, "delta": current - baseline, "days": days}


# ── Replied Comments ──────────────────────────────────────────

_REPLIED_RETENTION_DAYS = 90


def load_replied_comments() -> set:
    data = load_json(REPLIED_COMMENTS_FILE, default=[])
    if isinstance(data, list):
        return set(data)
    if isinstance(data, dict):
        return set(data.keys())
    return set()


def mark_comment_replied(comment_id: str) -> None:
    data = load_json(REPLIED_COMMENTS_FILE, default={})
    # 旧フォーマット（list）→ dict に移行（タイムスタンプは30日前で保持）
    if isinstance(data, list):
        placeholder = (datetime.now() - timedelta(days=30)).isoformat()[:16]
        data = {cid: placeholder for cid in data}
    data[comment_id] = datetime.now().isoformat()[:16]
    # 90日より古いエントリを削除
    cutoff = (datetime.now() - timedelta(days=_REPLIED_RETENTION_DAYS)).isoformat()[:16]
    data = {cid: ts for cid, ts in data.items() if ts >= cutoff}
    save_json(REPLIED_COMMENTS_FILE, data)


# ── Our Reply IDs（自分の返信ID追跡） ─────────────────────────

def load_our_reply_ids() -> dict:
    """自分が投稿した返信のIDとコンテキストを返す。{reply_id: {post_text, comment_text, replied_at}}"""
    return load_json(OUR_REPLY_IDS_FILE, default={})


def save_our_reply_id(reply_id: str, post_text: str, comment_text: str) -> None:
    """投稿した返信のIDを記録する"""
    our_replies = load_our_reply_ids()
    our_replies[reply_id] = {
        "post_text": post_text[:200],
        "comment_text": comment_text[:200],
        "replied_at": datetime.now().isoformat(),
    }
    # 最新500件のみ保持
    if len(our_replies) > 500:
        oldest_keys = sorted(our_replies, key=lambda k: our_replies[k].get("replied_at", ""))[:len(our_replies)-500]
        for k in oldest_keys:
            del our_replies[k]
    save_json(OUR_REPLY_IDS_FILE, our_replies)


# ── Hourly Stats ──────────────────────────────────────────────

def update_hourly_stats(hour_jst: int, views: int, likes: int) -> None:
    """投稿時間別のパフォーマンス集計を更新する"""
    stats = load_json(HOURLY_STATS_FILE, default={})
    key = str(hour_jst)
    if key not in stats:
        stats[key] = {"views_sum": 0, "likes_sum": 0, "count": 0}
    stats[key]["views_sum"] += views
    stats[key]["likes_sum"] += likes
    stats[key]["count"] += 1
    save_json(HOURLY_STATS_FILE, stats)


def get_best_hours(top_n: int = 6) -> list:
    """エンゲージメント率上位 N 時間帯を返す（JST）"""
    stats = load_json(HOURLY_STATS_FILE, default={})
    scored = []
    for hour_str, s in stats.items():
        if s["count"] < 3:
            continue  # サンプル数不足はスキップ
        avg_likes = s["likes_sum"] / s["count"]
        avg_views = s["views_sum"] / s["count"]
        score = avg_likes * 2 + avg_views * 0.1  # いいね重み付け
        scored.append((int(hour_str), score, avg_views, avg_likes, s["count"]))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]
