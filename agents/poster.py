"""
ポスターエージェント

キューから投稿を取り出してThreads APIで投稿する。
アフィリエイトリンクはコメント欄に自動配置する。

Threads API: https://developers.facebook.com/docs/threads
"""

import json
import time
import random
import requests
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    THREADS_ACCESS_TOKEN, THREADS_USER_ID,
    DAILY_POST_LIMIT, MIN_POST_INTERVAL_HOURS,
    MAX_CONSECUTIVE_ERRORS,
    IMAGE_MAP_FILE, IMAGE_ASSETS_DIR, GITHUB_RAW_BASE,
)
from core.state import (
    dequeue_post, load_history, add_to_history,
    is_killed, log_error, clear_errors, activate_kill_switch,
    load_json,
)


THREADS_API_BASE = "https://graph.threads.net/v1.0"


def _select_image(post: dict) -> str | None:
    """
    投稿のテーマ・パターン・テキストに合わせた画像URLを返す。
    ファイルが存在しない場合や確率外の場合は None を返す（テキストのみ投稿）。
    """
    image_map = load_json(IMAGE_MAP_FILE, default={})
    if not image_map:
        return None

    # 確率判定
    prob = image_map.get("image_post_probability", 0.4)
    if random.random() > prob:
        return None

    pattern_id = post.get("pattern_id", "")
    theme = post.get("theme", "")
    text = post.get("text", "")
    # 絵文字プレフィックスを除去（例: "♌獅子座" → "獅子座"）
    _raw_boost = post.get("zodiac_boost", "")
    zodiac_boost = _raw_boost.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓").strip() if _raw_boost else ""

    def resolve(filename: str | None) -> str | None:
        """ファイルが assets/images/ に実在すればGitHub Raw URLを返す"""
        if not filename:
            return None
        local = IMAGE_ASSETS_DIR / filename
        if local.exists():
            return f"{GITHUB_RAW_BASE}/{filename}"
        return None

    # 優先1: zodiac_boost — ただしテキストに星座名が実際に登場する場合のみ使用
    #   zodiac_boost はライターの「文体調整ヒント」を兼ねるため、
    #   テキストに星座名が出ていない投稿（list/trivia等のさりげないターゲット）に
    #   星座画像を貼るのは内容と合わないため除外する。
    if zodiac_boost and (zodiac_boost in text or zodiac_boost in theme):
        url = resolve(image_map.get("zodiac", {}).get(zodiac_boost))
        if url:
            return url

    # 優先2: パターンIDのデフォルト画像
    #   投稿タイプ（方位・風水・メニューなど）が最も確実な指標。
    #   例: kaiun_day（吉方位投稿）が「蟹座の方は…」と書いても
    #       パターンに紐づく方位画像を優先する。
    fname = image_map.get("pattern_defaults", {}).get(pattern_id)
    url = resolve(fname)
    if url:
        return url

    # 優先3: 星座キーワード
    #   パターンデフォルトがない投稿でのみ発動（汎用・共感系など）
    for kw, fname in image_map.get("zodiac", {}).items():
        if kw in text or kw in theme:
            url = resolve(fname)
            if url:
                return url

    # 優先4: 方位キーワード（複合語のみ — 「東京」「関西」には反応しない）
    for kw, fname in image_map.get("direction", {}).items():
        if kw.startswith("_"):   # _comment などのメタキーを無視
            continue
        if kw in text or kw in theme:
            url = resolve(fname)
            if url:
                return url

    # 優先5: 季節キーワード
    for kw, fname in image_map.get("season", {}).items():
        if kw in text or kw in theme:
            url = resolve(fname)
            if url:
                return url

    # 優先6: グローバルデフォルト（存在するものからランダム）
    available = [f for f in image_map.get("defaults", []) if resolve(f)]
    if available:
        return resolve(random.choice(available))

    return None


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


def _post_to_threads(text: str, image_url: str = None) -> dict:
    """
    Threads APIに投稿する（テキスト or 画像付きテキスト）。

    APIキーが未設定の場合はモックとして動作する。

    Returns:
        {"success": bool, "post_id": str, "has_image": bool, "error": str}
    """
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        mock_id = f"mock_{int(time.time())}"
        img_mark = " 🖼" if image_url else ""
        print(f"  [MOCK投稿{img_mark}] {text[:50]}...")
        return {"success": True, "post_id": mock_id, "has_image": bool(image_url), "mock": True}

    # Step 1: メディアコンテナ作成
    container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    data = {"text": text, "access_token": THREADS_ACCESS_TOKEN}

    if image_url:
        data["media_type"] = "IMAGE"
        data["image_url"] = image_url
    else:
        data["media_type"] = "TEXT"

    resp = requests.post(container_url, data=data)

    # 画像URLが使えない場合はテキストのみにフォールバック
    if not resp.ok and image_url:
        print(f"  ⚠️ 画像投稿失敗（{resp.status_code}）→ テキストのみで再試行")
        data = {"media_type": "TEXT", "text": text, "access_token": THREADS_ACCESS_TOKEN}
        resp = requests.post(container_url, data=data)
        image_url = None  # フォールバック成功後は画像なし扱い

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

    return {"success": True, "post_id": post_id, "has_image": bool(image_url), "mock": False}


def _post_thread(texts: list, lead_image_url: str = None) -> dict:
    """
    複数テキストをツリー形式（返信チェーン）で投稿する。
    lead_image_url が指定された場合、1枚目の投稿に画像を添付する。

    Returns:
        {"success": bool, "post_ids": [str, ...], "has_image": bool, "error": str}
    """
    if not texts:
        return {"success": False, "post_ids": [], "has_image": False, "error": "empty texts"}

    post_ids = []
    reply_to = None
    has_image = False

    for i, text in enumerate(texts):
        if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
            mock_id = f"mock_thread_{int(time.time())}_{i}"
            img_mark = " 🖼" if (i == 0 and lead_image_url) else ""
            print(f"  [MOCKツリー {i+1}/{len(texts)}{img_mark}] {text[:40]}...")
            post_ids.append(mock_id)
            reply_to = mock_id
            if i == 0 and lead_image_url:
                has_image = True
            continue

        # コンテナ作成
        container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
        data = {"text": text, "access_token": THREADS_ACCESS_TOKEN}
        if reply_to:
            data["reply_to_id"] = reply_to

        # 1枚目のみ画像付き
        if i == 0 and lead_image_url:
            data["media_type"] = "IMAGE"
            data["image_url"] = lead_image_url
        else:
            data["media_type"] = "TEXT"

        resp = requests.post(container_url, data=data)

        # 1枚目の画像投稿失敗 → テキストにフォールバック
        if not resp.ok and i == 0 and lead_image_url:
            print(f"  ⚠️ ツリー1枚目の画像投稿失敗 → テキストで再試行")
            data["media_type"] = "TEXT"
            del data["image_url"]
            resp = requests.post(container_url, data=data)
            lead_image_url = None

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
        if i == 0 and lead_image_url:
            has_image = True

        if i < len(texts) - 1:
            time.sleep(2)

    return {"success": True, "post_ids": post_ids, "has_image": has_image, "mock": not THREADS_ACCESS_TOKEN}


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

    # 画像URLを選択（存在しなければNone → テキストのみ）
    image_url = _select_image(post)

    try:
        if thread_posts and isinstance(thread_posts, list) and len(thread_posts) > 1:
            # ツリー投稿（1枚目のみ画像付き）
            result = _post_thread(thread_posts, lead_image_url=image_url)
            if not result["success"]:
                raise Exception("ツリー投稿失敗")
            post_id = result["post_ids"][0]
            img_mark = " 🖼" if result.get("has_image") else ""
            print(f"✅ ポスター: ツリー投稿成功{img_mark} [{post_id}] {len(thread_posts)}件 {text[:30]}...")
        else:
            # 通常投稿
            result = _post_to_threads(text, image_url=image_url)
            if not result["success"]:
                raise Exception("投稿失敗")
            post_id = result["post_id"]
            img_mark = " 🖼" if result.get("has_image") else ""
            print(f"✅ ポスター: 投稿成功{img_mark} [{post_id}] {text[:40]}...")

        # アフィリエイトリンクをコメント欄に投稿
        if affiliate_link:
            time.sleep(2)
            comment_text = f"▶️ 詳細・無料登録はこちら\n{affiliate_link}\n\n#PR"
            _post_comment(post_id, comment_text)

        # 履歴に記録
        post["threads_post_id"] = post_id
        post["posted_at"] = datetime.now().isoformat()
        post["is_mock"] = result.get("mock", False)
        post["has_image"] = result.get("has_image", False)
        if post["has_image"] and image_url:   # フォールバックでテキスト投稿になった場合は記録しない
            post["image_url"] = image_url
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
