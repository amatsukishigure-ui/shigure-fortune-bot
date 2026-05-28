"""
トラッカーエージェント（時雨 / 龍脈命術）

フォロワー数を毎日取得して記録する。
投稿時間別パフォーマンスの集計も更新する。
"""

import requests
from datetime import datetime, timezone, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import THREADS_ACCESS_TOKEN, THREADS_USER_ID
from core.state import (
    load_history, load_performance,
    add_follower_snapshot, update_hourly_stats, get_follower_growth, get_best_hours,
)


THREADS_API_BASE = "https://graph.threads.net/v1.0"
JST = timezone(timedelta(hours=9))


def _fetch_follower_count() -> int | None:
    """Threads API からフォロワー数を取得する"""
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        return None

    # まず insights API を試みる
    url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_insights"
    params = {
        "metric": "followers_count",
        "period": "day",
        "access_token": THREADS_ACCESS_TOKEN,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.ok:
            data = resp.json().get("data", [])
            for item in data:
                if item.get("name") == "followers_count":
                    values = item.get("values", [])
                    if values:
                        return values[-1].get("value")
    except Exception:
        pass

    # フォールバック: ユーザーフィールド
    url2 = f"{THREADS_API_BASE}/{THREADS_USER_ID}"
    params2 = {
        "fields": "followers_count",
        "access_token": THREADS_ACCESS_TOKEN,
    }
    try:
        resp = requests.get(url2, params=params2, timeout=10)
        if resp.ok:
            return resp.json().get("followers_count")
    except Exception as e:
        print(f"  フォロワー数取得エラー: {e}")

    return None


def _update_hourly_stats_from_history() -> int:
    """
    投稿履歴 × パフォーマンスデータから時間帯別統計を更新する。
    新規分（未集計分）のみ処理する。
    """
    history = load_history(limit=500)
    perf_list = load_performance()
    perf_map = {p["post_id"]: p for p in perf_list}

    updated = 0
    for h in history:
        tid = h.get("threads_post_id")
        posted_at = h.get("posted_at", "")
        # パフォーマンス集計済みフラグがあればスキップ
        if h.get("hourly_stats_recorded") or not tid or not posted_at:
            continue
        if tid not in perf_map:
            continue

        p = perf_map[tid]
        try:
            # posted_at は UTC（GitHub Actions）
            dt = datetime.fromisoformat(posted_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hour_jst = (dt.astimezone(JST)).hour
            update_hourly_stats(hour_jst, p.get("views", 0), p.get("likes", 0))
            h["hourly_stats_recorded"] = True
            updated += 1
        except Exception:
            pass

    # フラグ付きの履歴を保存（save_history は存在しないので直接）
    if updated > 0:
        from core.state import save_json, POST_HISTORY_FILE
        from config import POST_HISTORY_FILE as PHF
        full_history = []
        try:
            import json
            from pathlib import Path
            full_history = json.loads(PHF.read_text(encoding="utf-8"))
        except Exception:
            pass
        # フラグを反映
        h_map = {(h.get("threads_post_id"), h.get("posted_at")): h for h in history}
        for i, fh in enumerate(full_history):
            key = (fh.get("threads_post_id"), fh.get("posted_at"))
            if key in h_map and h_map[key].get("hourly_stats_recorded"):
                full_history[i]["hourly_stats_recorded"] = True
        save_json(PHF, full_history)

    return updated


def run() -> dict:
    """
    Returns:
        {"follower_count": int, "growth_7d": int, "hourly_updated": int}
    """
    # フォロワー数取得・記録
    count = _fetch_follower_count()
    if count is not None:
        add_follower_snapshot(count)
        print(f"  👥 フォロワー数: {count:,}人")
    else:
        print("  👥 フォロワー数: 取得できませんでした")

    growth = get_follower_growth(7)
    if growth["delta"] is not None:
        sign = "+" if growth["delta"] >= 0 else ""
        print(f"  📈 7日間の増減: {sign}{growth['delta']}人")

    # 時間帯別統計を更新
    hourly_updated = _update_hourly_stats_from_history()
    if hourly_updated:
        print(f"  ⏰ 時間帯別統計: {hourly_updated}件更新")

    # ベスト時間帯を表示
    best = get_best_hours(3)
    if best:
        best_str = "、".join(f"{h}時" for h, *_ in best)
        print(f"  ⭐ エンゲージ上位時間帯: {best_str}")

    return {
        "follower_count": count,
        "growth_7d": growth.get("delta"),
        "hourly_updated": hourly_updated,
    }


if __name__ == "__main__":
    import json
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
