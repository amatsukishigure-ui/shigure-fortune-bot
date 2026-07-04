"""
コンバージョン相関レポート

data/conversion_log.json に手動で記録した週次コンバージョン数（LINE追加・Coconala購入）と
data/post_history.json の投稿パターンを照合し、どのパターンが転換に寄与しているか推定する。

使い方:
  python scripts/conversion_report.py          # 全期間サマリー
  python scripts/conversion_report.py --add    # 今週のデータを追加

conversion_log.json の entry 形式:
  {
    "week_start": "2026-07-07",   # 月曜日の日付
    "line_adds": 3,               # その週のLINE友だち追加数
    "coconala_orders": 1,         # その週のCoconala注文数
    "note": "任意メモ"
  }
"""

import json
import pathlib
import sys
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

BASE = pathlib.Path(__file__).parent.parent
DATA = BASE / "data"


def load_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else {}


def add_entry():
    log_path = DATA / "conversion_log.json"
    log = load_json(log_path, {"entries": []})

    today = datetime.now()
    # 直近月曜を算出
    days_since_monday = today.weekday()
    week_start = (today - timedelta(days=days_since_monday)).strftime("%Y-%m-%d")

    # 既存エントリ確認
    existing = next((e for e in log["entries"] if e.get("week_start") == week_start), None)
    if existing:
        print(f"  週 {week_start} のエントリは既にあります: LINE追加={existing.get('line_adds',0)}, Coconala={existing.get('coconala_orders',0)}")
        overwrite = input("  上書きしますか？ (y/N): ").strip().lower()
        if overwrite != "y":
            print("  スキップしました。")
            return

    print(f"\n── {week_start} 週のコンバージョンを入力 ──")
    try:
        line_adds = int(input("  LINE 友だち追加数: ").strip() or "0")
        coconala_orders = int(input("  Coconala 注文数: ").strip() or "0")
        note = input("  メモ（任意、Enterでスキップ）: ").strip()
    except (ValueError, EOFError):
        print("  入力をキャンセルしました。")
        return

    entry = {
        "week_start": week_start,
        "line_adds": line_adds,
        "coconala_orders": coconala_orders,
    }
    if note:
        entry["note"] = note

    # 上書きまたは追加
    log["entries"] = [e for e in log["entries"] if e.get("week_start") != week_start]
    log["entries"].append(entry)
    log["entries"].sort(key=lambda e: e.get("week_start", ""))

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  保存しました → {log_path}")


def report():
    log_path = DATA / "conversion_log.json"
    log = load_json(log_path, {"entries": []})
    entries = log.get("entries", [])

    if not entries:
        print("conversion_log.json にデータがありません。")
        print("  python scripts/conversion_report.py --add  でデータを入力してください。")
        return

    history_path = DATA / "post_history.json"
    history = load_json(history_path, [])

    print("\n═══ コンバージョン週次サマリー ═══\n")
    total_line = sum(e.get("line_adds", 0) for e in entries)
    total_coconala = sum(e.get("coconala_orders", 0) for e in entries)
    print(f"  記録週数     : {len(entries)} 週")
    print(f"  LINE 追加累計: {total_line}")
    print(f"  Coconala 累計: {total_coconala}")
    print()

    # パターン別投稿数を週ごとに集計
    # 週ごとに投稿されたパターンを特定し、コンバージョンと紐付ける
    pattern_conv: dict = defaultdict(lambda: {"line": 0, "coconala": 0, "weeks": 0})

    for entry in entries:
        week_start_str = entry.get("week_start", "")
        if not week_start_str:
            continue
        week_start_dt = datetime.strptime(week_start_str, "%Y-%m-%d")
        week_end_dt = week_start_dt + timedelta(days=7)

        line_adds = entry.get("line_adds", 0)
        coconala_orders = entry.get("coconala_orders", 0)

        # その週に投稿されたパターンを収集（重複なし）
        week_patterns: set = set()
        for h in history:
            posted_at = h.get("posted_at", "")
            if not posted_at:
                continue
            try:
                posted_dt = datetime.fromisoformat(posted_at[:19])
            except ValueError:
                continue
            if week_start_dt <= posted_dt < week_end_dt:
                pat = h.get("pattern_id")
                if pat:
                    week_patterns.add(pat)

        for pat in week_patterns:
            pattern_conv[pat]["line"] += line_adds
            pattern_conv[pat]["coconala"] += coconala_orders
            pattern_conv[pat]["weeks"] += 1

    if not pattern_conv:
        print("  投稿履歴とコンバージョン週のマッチングができませんでした。")
        print("  post_history.json の posted_at フィールドを確認してください。")
        return

    # スコアリング: LINE×1 + Coconala×3（購入の方が価値が高い）
    scored = sorted(
        pattern_conv.items(),
        key=lambda x: x[1]["line"] + x[1]["coconala"] * 3,
        reverse=True,
    )

    print("── パターン別 コンバージョン相関 ──\n")
    print(f"  {'パターン':<28} {'出現週':>6} {'LINE追加':>8} {'Coconala':>8}  スコア")
    print(f"  {'-'*28} {'-'*6} {'-'*8} {'-'*8}  {'-'*5}")
    for pat, vals in scored[:15]:
        score = vals["line"] + vals["coconala"] * 3
        print(f"  {pat:<28} {vals['weeks']:>6}週 {vals['line']:>8} {vals['coconala']:>8}  {score:>5}")

    print()
    if scored:
        top_pat = scored[0][0]
        print(f"  → 最も転換に関連するパターン: {top_pat}")
        print("  ※ 相関であり因果ではありません。複数パターンが同じ週に出ます。")

    # 週次推移
    print("\n── 週次推移 ──\n")
    print(f"  {'週':>12}  {'LINE':>6}  {'Coconala':>8}")
    print(f"  {'-'*12}  {'-'*6}  {'-'*8}")
    for entry in entries[-8:]:  # 直近8週
        w = entry.get("week_start", "")
        l = entry.get("line_adds", 0)
        c = entry.get("coconala_orders", 0)
        print(f"  {w}  {l:>6}  {c:>8}")

    print()


def main():
    parser = argparse.ArgumentParser(description="コンバージョン相関レポート")
    parser.add_argument("--add", action="store_true", help="今週のコンバージョンデータを追加")
    args = parser.parse_args()

    if args.add:
        add_entry()
    else:
        report()


if __name__ == "__main__":
    main()
