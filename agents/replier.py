"""
リプライエージェント（時雨 / 龍脈命術）

直近の投稿に届いたコメントを取得し、時雨として返信する。
返信済みコメントIDを記録して二重返信を防ぐ。
1日1回 daily 実行から呼ばれる。
"""

import json
import time
import requests
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import anthropic
from config import (
    ANTHROPIC_API_KEY, MODEL_LIGHT,
    THREADS_ACCESS_TOKEN, THREADS_USER_ID,
    ACCOUNT_PROFILE_FILE,
)
from core.state import (
    load_history, load_json, load_replied_comments, mark_comment_replied,
)


THREADS_API_BASE = "https://graph.threads.net/v1.0"

# 1回の実行で返信する最大件数（スパム防止）
MAX_REPLIES_PER_RUN = 10
# 何日以内の投稿のコメントを対象にするか
REPLY_TARGET_DAYS = 7


def _get_post_replies(post_id: str) -> list:
    """投稿への返信一覧を取得する"""
    if not THREADS_ACCESS_TOKEN:
        return []
    url = f"{THREADS_API_BASE}/{post_id}/replies"
    params = {
        "fields": "id,text,username,timestamp",
        "access_token": THREADS_ACCESS_TOKEN,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if not resp.ok:
            body = resp.json() if resp.content else {}
            err = body.get("error", {})
            print(f"  コメント取得失敗 [{post_id}] HTTP{resp.status_code}: "
                  f"{err.get('message', resp.text[:120])}")
            # 権限不足（#200/#10/OAuthException）は早期リターン
            if err.get("code") in (200, 10) or err.get("type") == "OAuthException":
                print("  ⚠️  アクセストークンに threads_read_replies 権限が必要です")
                return []
            return []
        data = resp.json().get("data", [])
        return data
    except Exception as e:
        print(f"  コメント取得エラー [{post_id}]: {e}")
        return []


def _post_reply(comment_id: str, text: str) -> bool:
    """コメントへの返信を投稿する"""
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        print(f"  [MOCKリプライ] → {text[:50]}...")
        return True

    container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    try:
        resp = requests.post(container_url, data={
            "media_type": "TEXT",
            "text": text,
            "reply_to_id": comment_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=10)
        resp.raise_for_status()
        container_id = resp.json().get("id")

        publish_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
        resp = requests.post(publish_url, data={
            "creation_id": container_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  リプライ投稿エラー: {e}")
        return False


def _generate_reply(comment_text: str, post_text: str, profile: dict) -> str:
    """
    コメントへの時雨らしい返信を生成する。
    短く（50〜120字）、押しつけがましくなく、温かく。
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
Threadsへの投稿にコメントが届きました。時雨として返信してください。

【元の投稿】
{post_text[:200]}

【届いたコメント】
{comment_text}

【返信ルール】
- 50〜120字で短く返す
- 「ありがとうございます」は使わない（代わりに「嬉しい」「届いてよかった」など）
- 相手の言葉を受け止めて、龍脈命術・吉方位・気の流れの視点から一言添える
- 鑑定への誘導は不要（自然な会話として返す）
- 語尾は「〜だよ」「〜ね」「〜かも」など柔らかく
- 絵文字は0〜1個

返信文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run() -> dict:
    """
    Returns:
        {"replied": int, "skipped": int, "errors": int}
    """
    profile = load_json(ACCOUNT_PROFILE_FILE, default={})
    history = load_history(limit=200)
    replied_ids = load_replied_comments()

    # 対象投稿：直近 REPLY_TARGET_DAYS 日以内に投稿されたもの
    cutoff = (datetime.now() - timedelta(days=REPLY_TARGET_DAYS)).isoformat()
    target_posts = [
        h for h in history
        if h.get("threads_post_id")
        and h.get("posted_at", "") >= cutoff
        and not h.get("is_mock")
    ]

    replied = 0
    skipped = 0
    errors = 0

    own_username = profile.get("accounts", {}).get("threads", "").lstrip("@").lower()

    for post in target_posts:
        if replied >= MAX_REPLIES_PER_RUN:
            break

        post_id = post["threads_post_id"]
        post_text = post.get("text", "")
        replies = _get_post_replies(post_id)

        for comment in replies:
            if replied >= MAX_REPLIES_PER_RUN:
                break

            cid = comment.get("id", "")
            comment_text = comment.get("text", "").strip()

            # 自分自身のコメントはスキップ（@ なし・大小文字無視）
            commenter = comment.get("username", "").lstrip("@").lower()
            if own_username and commenter == own_username:
                skipped += 1
                continue

            # 返信済みはスキップ
            if cid in replied_ids:
                skipped += 1
                continue

            # 空コメントはスキップ
            if not comment_text:
                skipped += 1
                continue

            try:
                reply_text = _generate_reply(comment_text, post_text, profile)
                success = _post_reply(cid, reply_text)
                if success:
                    mark_comment_replied(cid)
                    replied += 1
                    print(f"  💬 返信: 「{comment_text[:30]}」→ 「{reply_text[:40]}」")
                    time.sleep(3)  # 連続投稿防止
                else:
                    errors += 1
            except Exception as e:
                print(f"  リプライ生成エラー: {e}")
                errors += 1

    return {"replied": replied, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
