"""
Xポスターエージェント

キューからX用投稿を取り出してTwitter API v2で投稿する。
tweepyを使用。APIキー未設定時はモック動作。
"""

import json
import time
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET,
    X_BEARER_TOKEN, X_QUEUE_FILE, X_HISTORY_FILE,
    DAILY_POST_LIMIT, MIN_POST_INTERVAL_HOURS, MAX_CONSECUTIVE_ERRORS,
)
from core.state import is_killed, log_error, clear_errors, activate_kill_switch
from core.formatter import format_for_x, get_x_hashtags_for_theme


def _load_x_queue() -> list:
    from core.state import load_json
    return load_json(X_QUEUE_FILE, default=[])


def _save_x_queue(queue: list) -> None:
    from core.state import save_json
    save_json(X_QUEUE_FILE, queue)


def _load_x_history(limit: int = None) -> list:
    from core.state import load_json
    history = load_json(X_HISTORY_FILE, default=[])
    return history[-limit:] if limit else history


def _add_to_x_history(post: dict) -> None:
    from core.state import load_json, save_json
    history = load_json(X_HISTORY_FILE, default=[])
    post["recorded_at"] = datetime.now().isoformat()
    history.append(post)
    if len(history) > 1000:
        history = history[-1000:]
    save_json(X_HISTORY_FILE, history)


def _dequeue_x_post() -> dict | None:
    queue = _load_x_queue()
    if not queue:
        return None
    post = queue.pop(0)
    _save_x_queue(queue)
    return post


def _count_today_x_posts(history: list) -> int:
    today = datetime.now().date().isoformat()
    return sum(1 for h in history if h.get("posted_at", "").startswith(today))


def _get_last_x_post_time(history: list) -> datetime | None:
    posted = [h for h in history if h.get("posted_at")]
    if not posted:
        return None
    last = max(posted, key=lambda x: x["posted_at"])
    return datetime.fromisoformat(last["posted_at"])


def _can_post(history: list) -> tuple[bool, str]:
    if _count_today_x_posts(history) >= DAILY_POST_LIMIT:
        return False, f"本日の上限({DAILY_POST_LIMIT}件)に達しています"
    last_time = _get_last_x_post_time(history)
    if last_time:
        elapsed = datetime.now() - last_time
        min_interval = timedelta(hours=MIN_POST_INTERVAL_HOURS)
        if elapsed < min_interval:
            wait = (min_interval - elapsed).seconds // 60
            return False, f"最小投稿間隔未満（あと{wait}分）"
    return True, ""


def _post_to_x(text: str) -> dict:
    """
    Twitter API v2で投稿する。
    APIキー未設定時はモック動作。
    """
    if not X_API_KEY or not X_ACCESS_TOKEN:
        mock_id = f"x_mock_{int(time.time())}"
        print(f"  [X MOCK投稿] {text[:50]}...")
        return {"success": True, "tweet_id": mock_id, "mock": True}

    try:
        import tweepy

        client = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET,
        )
        response = client.create_tweet(text=text)
        tweet_id = response.data["id"]
        return {"success": True, "tweet_id": tweet_id, "mock": False}

    except ImportError:
        # tweepyが未インストールの場合はモック
        mock_id = f"x_mock_{int(time.time())}"
        print(f"  [X MOCK（tweepy未インストール）] {text[:50]}...")
        return {"success": True, "tweet_id": mock_id, "mock": True}

    except Exception as e:
        return {"success": False, "error": str(e), "mock": False}


def enqueue_x_post(post: dict) -> None:
    """X用キューに投稿を追加する（writer.pyから呼ぶ）"""
    queue = _load_x_queue()
    post["queued_at"] = datetime.now().isoformat()
    queue.append(post)
    _save_x_queue(queue)


def run() -> dict:
    """
    Xポスターエージェントのメイン処理（1投稿スロット分）。

    Returns:
        {"posted": bool, "tweet_id": str, "reason": str}
    """
    if is_killed():
        return {"posted": False, "reason": "Kill switch active"}

    history = _load_x_history(limit=100)
    can_post, reason = _can_post(history)
    if not can_post:
        print(f"⏸ Xポスター: {reason}")
        return {"posted": False, "reason": reason}

    post = _dequeue_x_post()
    if not post:
        print("⏸ Xポスター: キューが空です")
        return {"posted": False, "reason": "Queue empty"}

    # X用テキストを生成（フォーマッター経由）
    original_text = post.get("text", "")
    theme = post.get("theme", "")
    hashtags = get_x_hashtags_for_theme(theme)
    x_text = post.get("x_text") or format_for_x(original_text, hashtags)

    # 280字チェック
    if len(x_text) > 280:
        x_text = x_text[:277] + "..."

    try:
        result = _post_to_x(x_text)
        if not result["success"]:
            raise Exception(result.get("error", "投稿失敗"))

        tweet_id = result["tweet_id"]
        post["x_tweet_id"] = tweet_id
        post["x_text"] = x_text
        post["posted_at"] = datetime.now().isoformat()
        post["is_mock"] = result.get("mock", False)
        _add_to_x_history(post)
        clear_errors("x_poster")

        print(f"✅ Xポスター: 投稿成功 [{tweet_id}] {x_text[:40]}...")
        return {"posted": True, "tweet_id": tweet_id, "reason": ""}

    except Exception as e:
        consecutive = log_error("x_poster", str(e))
        print(f"❌ Xポスター: エラー ({consecutive}回連続) - {e}")
        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            activate_kill_switch(f"Xポスターエラー{consecutive}回連続: {e}")
        return {"posted": False, "reason": str(e)}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
