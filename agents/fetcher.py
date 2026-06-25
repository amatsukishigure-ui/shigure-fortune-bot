"""
フェッチャーエージェント

投稿から約24時間後にThreads APIからメトリクスを取得し、
パフォーマンスデータを更新する。
"""

import json
import requests
from datetime import datetime, timedelta, timezone
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import THREADS_ACCESS_TOKEN, THREADS_USER_ID
from core.state import load_history, update_performance, load_performance


THREADS_API_BASE = "https://graph.threads.net/v1.0"

METRICS = ["views", "likes", "replies", "reposts", "quotes", "shares"]


def _fetch_metrics(post_id: str) -> dict | None:
    """Threads APIからメトリクスを取得する"""
    if not THREADS_ACCESS_TOKEN:
        # モック
        import random
        return {
            "views": random.randint(100, 5000),
            "likes": random.randint(5, 300),
            "replies": random.randint(0, 50),
            "reposts": random.randint(0, 20),
            "quotes": random.randint(0, 10),
            "shares": random.randint(0, 15),
        }

    url = f"{THREADS_API_BASE}/{post_id}/insights"
    params = {
        "metric": ",".join(METRICS),
        "access_token": THREADS_ACCESS_TOKEN,
    }
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return {item["name"]: item["values"][0]["value"] for item in data}
    except Exception as e:
        print(f"  メトリクス取得エラー [{post_id}]: {e}")
        return None


def _should_fetch(post: dict) -> bool:
    """フェッチ対象かどうか判定（投稿24時間以上経過 & まだ取得していない）"""
    if not post.get("threads_post_id"):
        return False
    if post.get("metrics_fetched"):
        return False

    posted_at = post.get("posted_at")
    if not posted_at:
        return False

    try:
        posted_time = datetime.fromisoformat(posted_at).replace(tzinfo=None)
        return datetime.now(timezone.utc).replace(tzinfo=None) - posted_time >= timedelta(hours=24)
    except ValueError:
        return False


def run() -> dict:
    """
    フェッチャーエージェントのメイン処理。

    Returns:
        {"fetched": int, "skipped": int}
    """
    history = load_history()
    existing_perf = {p["post_id"]: p for p in load_performance()}

    # history から threads_post_id → pattern_id の対照表を作成
    pattern_id_map = {
        h["threads_post_id"]: h.get("pattern_id", "")
        for h in history
        if h.get("threads_post_id")
    }

    fetched = 0
    skipped = 0

    for post in history:
        if not _should_fetch(post):
            skipped += 1
            continue

        post_id = post["threads_post_id"]

        # すでにフェッチ済みならスキップ
        if post_id in existing_perf and existing_perf[post_id].get("views"):
            skipped += 1
            continue

        metrics = _fetch_metrics(post_id)
        if metrics:
            # pattern_id を performance_data に記録してパターン別分析を可能にする
            pid = pattern_id_map.get(post_id, "")
            if pid:
                metrics["pattern_id"] = pid
            update_performance(post_id, metrics)
            fetched += 1
            print(f"📊 フェッチャー: [{post_id}] pattern={pid} views={metrics.get('views')} likes={metrics.get('likes')}")

    print(f"✅ フェッチャー: {fetched}件取得, {skipped}件スキップ")
    return {"fetched": fetched, "skipped": skipped}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
