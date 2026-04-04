"""
JSON状態管理モジュール

全エージェントが共有するJSONファイルの読み書きを管理する。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (
    POST_QUEUE_FILE, POST_HISTORY_FILE, PERFORMANCE_FILE,
    RESEARCH_FILE, ERROR_LOG_FILE, KILL_SWITCH_FILE, DATA_DIR
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
