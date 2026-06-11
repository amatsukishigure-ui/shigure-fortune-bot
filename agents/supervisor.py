"""
スーパーバイザーエージェント

全体の監視・異常検知・緊急停止を担当する。
"""

import json
from datetime import datetime, timedelta, timezone
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import DAILY_POST_LIMIT, DATA_DIR
from core.state import (
    load_json, load_history, load_queue,
    is_killed, activate_kill_switch, deactivate_kill_switch,
)


ERROR_LOG_FILE = DATA_DIR / "error_log.json"


def _check_posting_rhythm(history: list) -> dict:
    """投稿リズムの異常を検出する"""
    issues = []

    today = datetime.now(timezone.utc).date().isoformat()
    today_posts = [h for h in history if h.get("posted_at", "").startswith(today)]

    # 1分以内に複数投稿（bot判定リスク）
    if len(today_posts) >= 2:
        times = sorted([datetime.fromisoformat(p["posted_at"]).replace(tzinfo=None) for p in today_posts])
        for i in range(1, len(times)):
            diff = (times[i] - times[i-1]).seconds
            if diff < 60:
                issues.append(f"⚠️ 投稿間隔が{diff}秒しかありません（bot判定リスク）")

    # 1日の上限超え
    if len(today_posts) > DAILY_POST_LIMIT:
        issues.append(f"⚠️ 本日の投稿数が上限を超えています: {len(today_posts)}/{DAILY_POST_LIMIT}")

    return {"today_count": len(today_posts), "issues": issues}


def _check_error_log() -> dict:
    """エラーログをチェックする"""
    errors = load_json(ERROR_LOG_FILE, default=[])

    # 直近1時間のエラー
    one_hour_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    recent_errors = [
        e for e in errors
        if datetime.fromisoformat(e["timestamp"]).replace(tzinfo=None) > one_hour_ago
    ]

    # エージェント別の連続エラー数
    from collections import Counter
    agent_counts = Counter(e["agent"] for e in recent_errors)

    critical = {agent: count for agent, count in agent_counts.items() if count >= 3}
    return {
        "recent_error_count": len(recent_errors),
        "critical_agents": critical,
    }


def _check_queue_health() -> dict:
    """キューの健全性チェック"""
    queue = load_queue()
    issues = []

    if len(queue) == 0:
        issues.append("ℹ️ 投稿キューが空です")
    elif len(queue) > 50:
        issues.append(f"⚠️ キューが溜まりすぎています: {len(queue)}件")

    return {"queue_size": len(queue), "issues": issues}


def run(auto_kill: bool = True) -> dict:
    """
    スーパーバイザーエージェントのメイン処理。

    Returns:
        {"status": str, "rhythm": dict, "errors": dict, "queue": dict, "killed": bool}
    """
    killed = is_killed()
    history = load_history(limit=100)

    rhythm_result = _check_posting_rhythm(history)
    error_result = _check_error_log()
    queue_result = _check_queue_health()

    # 自動Kill判断
    newly_killed = False
    if auto_kill and not killed:
        critical = error_result.get("critical_agents", {})
        if critical:
            reason = f"エラー多発: {critical}"
            activate_kill_switch(reason)
            newly_killed = True

        rhythm_issues = rhythm_result.get("issues", [])
        critical_rhythm = [i for i in rhythm_issues if "⚠️" in i and "bot判定" in i]
        if critical_rhythm:
            activate_kill_switch(f"投稿リズム異常: {critical_rhythm[0]}")
            newly_killed = True

    # レポート出力
    all_issues = (
        rhythm_result.get("issues", []) +
        queue_result.get("issues", [])
    )

    status = "KILLED" if (killed or newly_killed) else (
        "WARNING" if all_issues else "OK"
    )

    report = {
        "status": status,
        "checked_at": datetime.now().isoformat(),
        "rhythm": rhythm_result,
        "errors": error_result,
        "queue": queue_result,
        "killed": killed or newly_killed,
        "issues": all_issues,
    }

    if status == "OK":
        print(f"✅ スーパーバイザー: 正常 (本日{rhythm_result['today_count']}件投稿)")
    elif status == "WARNING":
        print(f"⚠️ スーパーバイザー: 警告あり")
        for issue in all_issues:
            print(f"   {issue}")
    else:
        print(f"⛔ スーパーバイザー: KILLED")

    return report


def kill(reason: str = "手動停止") -> None:
    """手動でKill Switchを起動する"""
    activate_kill_switch(reason)


def revive() -> None:
    """Kill Switchを解除する"""
    deactivate_kill_switch()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "kill":
            kill(" ".join(sys.argv[2:]) or "手動停止")
        elif sys.argv[1] == "revive":
            revive()
    else:
        result = run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
