"""
ポスターエージェント

キューから投稿を取り出してThreads APIで投稿する。
アフィリエイトリンクはコメント欄に自動配置する。

Threads API: https://developers.facebook.com/docs/threads
"""

import json
import time
import requests
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    THREADS_ACCESS_TOKEN, THREADS_USER_ID,
    DAILY_POST_LIMIT, MIN_POST_INTERVAL_HOURS,
)
from core.state import (
    dequeue_post, load_history, add_to_history,
    is_killed, log_error, clear_errors, activate_kill_switch,
)
from config import MAX_CONSECUTIVE_ERRORS


THREADS_API_BASE = "https://graph.threads.net/v1.0"


def _count_today_posts(history: list) -> int:
    today = datetime.now().date().isoformat()
    return sum(
        1 for h in history
        if h.get("posted_at", "").startswith(today)
    )


def _get_last_post_time(history: list) -> datetime | None:
    posted = [h for h in history if h.get("posted_at")]
    if not posted:
        return None
    last = max(posted, key=lambda x: x["posted_at"])
    return datetime.fromisoformat(last["posted_at"])


def _can_post(history: list) -> tuple[bool, str]:
    """投稿可能かチェック"""
    today_count = _count_today_posts(history)
    if today_count >= DAILY_POST_LIMIT:
        return False, f"本日の上限({DAILY_POST_LIMIT}件)に達しています"

    last_time = _get_last_post_time(history)
    if last_time:
        elapsed = datetime.now() - last_time
        min_interval = timedelta(hours=MIN_POST_INTERVAL_HOURS)
        if elapsed < min_interval:
            wait = (min_interval - elapsed).seconds // 60
            return False, f"最小投稿間隔未満です（あと{wait}分）"

    return True, ""


def _post_to_threads(text: str) -> dict:
    """
    Threads APIに投稿する。

    APIキーが未設定の場合はモックとして動作する。

    Returns:
        {"success": bool, "post_id": str, "error": str}
    """
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        # モック動作（APIキー未設定時）
        mock_id = f"mock_{int(time.time())}"
        print(f"  [MOCK投稿] {text[:50]}...")
        return {"success": True, "post_id": mock_id, "mock": True}

    # Step 1: メディアコンテナ作成
    container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    resp = requests.post(container_url, data={
        "media_type": "TEXT",
        "text": text,
        "access_token": THREADS_ACCESS_TOKEN,
    })
    resp.raise_for_status()
    container_id = resp.json().get("id")

    # Step 2: 公開
    publish_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
    resp = requests.post(publish_url, data={
        "creation_id": container_id,
        "access_token": THREADS_ACCESS_TOKEN,
    })
    resp.raise_for_status()
    post_id = resp.json().get("id")

    return {"success": True, "post_id": post_id, "mock": False}


def _post_thread(texts: list) -> dict:
    """
    複数テキストをツリー形式（返信チェーン）で投稿する。

    Returns:
        {"success": bool, "post_ids": [str, ...], "error": str}
    """
    if not texts:
        return {"success": False, "post_ids": [], "error": "empty texts"}

    post_ids = []
    reply_to = None

    for i, text in enumerate(texts):
        if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
            mock_id = f"mock_thread_{int(time.time())}_{i}"
            print(f"  [MOCKツリー {i+1}/{len(texts)}] {text[:40]}...")
            post_ids.append(mock_id)
            reply_to = mock_id
            continue

        # コンテナ作成
        container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
        data = {
            "media_type": "TEXT",
            "text": text,
            "access_token": THREADS_ACCESS_TOKEN,
        }
        if reply_to:
            data["reply_to_id"] = reply_to

        resp = requests.post(container_url, data=data)
        resp.raise_for_status()
        container_id = resp.json().get("id")

        # 公開
        publish_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
        resp = requests.post(publish_url, data={
            "creation_id": container_id,
            "access_token": THREADS_ACCESS_TOKEN,
        })
        resp.raise_for_status()
        post_id = resp.json().get("id")
        post_ids.append(post_id)
        reply_to = post_id

        if i < len(texts) - 1:
            time.sleep(2)  # 連続投稿の間隔

    return {"success": True, "post_ids": post_ids, "mock": not THREADS_ACCESS_TOKEN}


def _post_comment(post_id: str, text: str) -> dict:
    """投稿のコメント欄に返信する（アフィリエイトリンク配置に使用）"""
    if not THREADS_ACCESS_TOKEN:
        print(f"  [MOCKコメント] {text[:50]}...")
        return {"success": True, "comment_id": f"mock_comment_{int(time.time())}"}

    # コメントもThreads APIで投稿（返信として）
    container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    resp = requests.post(container_url, data={
        "media_type": "TEXT",
        "text": text,
        "reply_to_id": post_id,
        "access_token": THREADS_ACCESS_TOKEN,
    })
    resp.raise_for_status()
    container_id = resp.json().get("id")

    publish_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
    resp = requests.post(publish_url, data={
        "creation_id": container_id,
        "access_token": THREADS_ACCESS_TOKEN,
    })
    resp.raise_for_status()
    return {"success": True, "comment_id": resp.json().get("id")}


def run() -> dict:
    """
    ポスターエージェントのメイン処理（1投稿スロット分）。

    Returns:
        {"posted": bool, "post_id": str, "reason": str}
    """
    if is_killed():
        return {"posted": False, "reason": "Kill switch active"}

    history = load_history(limit=100)
    can_post, reason = _can_post(history)
    if not can_post:
        print(f"⏸ ポスター: {reason}")
        return {"posted": False, "reason": reason}

    post = dequeue_post()
    if not post:
        print("⏸ ポスター: キューが空です")
        return {"posted": False, "reason": "Queue empty"}

    text = post.get("text", "")
    thread_posts = post.get("thread_posts")  # ツリー投稿用リスト
    affiliate_link = post.get("affiliate_link", "")

    try:
        if thread_posts and isinstance(thread_posts, list) and len(thread_posts) > 1:
            # ツリー投稿
            result = _post_thread(thread_posts)
            if not result["success"]:
                raise Exception("ツリー投稿失敗")
            post_id = result["post_ids"][0]  # 先頭投稿IDを代表IDとして使用
            print(f"✅ ポスター: ツリー投稿成功 [{post_id}] {len(thread_posts)}件 {text[:30]}...")
        else:
            # 通常投稿
            result = _post_to_threads(text)
            if not result["success"]:
                raise Exception("投稿失敗")
            post_id = result["post_id"]
            print(f"✅ ポスター: 投稿成功 [{post_id}] {text[:40]}...")

        # アフィリエイトリンクをコメント欄に投稿
        if affiliate_link:
            time.sleep(2)
            comment_text = f"▶️ 詳細・無料登録はこちら\n{affiliate_link}\n\n#PR"
            _post_comment(post_id, comment_text)

        # 履歴に記録
        post["threads_post_id"] = post_id
        post["posted_at"] = datetime.now().isoformat()
        post["is_mock"] = result.get("mock", False)
        add_to_history(post)
        clear_errors("poster")

        return {"posted": True, "post_id": post_id, "reason": ""}

    except Exception as e:
        consecutive = log_error("poster", str(e))
        print(f"❌ ポスター: エラー ({consecutive}回連続) - {e}")

        if consecutive >= MAX_CONSECUTIVE_ERRORS:
            activate_kill_switch(f"ポスターエラー{consecutive}回連続: {e}")

        return {"posted": False, "reason": str(e)}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
