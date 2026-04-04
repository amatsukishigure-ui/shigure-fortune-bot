"""
メインオーケストレーター（時雨 / 龍脈命術）

Threads（shigure_fortune）と X（@Shigure_fortune）の両方に自動投稿する。

使い方:
  python main.py daily        # 毎朝1回の全体実行（分析→リサーチ→生成）
  python main.py post         # 1投稿スロット：Threads + X 両方に投稿
  python main.py post threads # Threadsのみ投稿
  python main.py post x       # Xのみ投稿
  python main.py status       # 状態確認
  python main.py kill         # 緊急停止
  python main.py revive       # Kill解除
"""

import sys
import io
import json
from datetime import datetime

# Windowsのcp932コンソールでもUTF-8の絵文字を出力できるようにする
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from core.state import is_killed


def cmd_daily():
    """毎朝の定期実行: 分析→リサーチ→生成（投稿はcronでcmd_postが担当）"""
    print(f"\n{'='*55}")
    print(f"🌙 占術家 時雨 - Daily Run")
    print(f"   Threads: shigure_fortune / X: @Shigure_fortune")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    if is_killed():
        print("⛔ Kill Switchがアクティブです。reviveコマンドで解除してください。")
        return

    from agents import supervisor, fetcher, analyst, researcher, writer

    print("🔍 [1/5] スーパーバイザー: 状態チェック")
    sup = supervisor.run(auto_kill=True)
    if sup["killed"]:
        print("⛔ 異常検知のため中断")
        return

    print("\n📊 [2/5] フェッチャー: メトリクス取得（Threads・X）")
    fetcher.run()

    print("\n📈 [3/5] アナリスト: パフォーマンス分析")
    analyst.run()

    print("\n🔍 [4/5] リサーチャー: ネタ収集（吉方位・風水・占い）")
    researcher.run(max_themes=3)

    print("\n✍️  [5/5] ライター: 投稿生成（時雨として）")
    result = writer.run(batch_size=5)
    print(
        f"   生成: {result['generated']}件 → "
        f"Threads: {result['queued_threads']}件, "
        f"X: {result['queued_x']}件, "
        f"棄却: {result['rejected']}件"
    )

    print(f"\n{'='*55}")
    print(f"✅ Daily Run 完了 - 龍の気が整いました")
    print(f"{'='*55}\n")


def cmd_post(platform: str = "both"):
    """
    1投稿スロット分の実行（cronから複数回/日呼ぶ）。

    platform: 'both' | 'threads' | 'x'
    """
    if is_killed():
        print("⛔ Kill Switchがアクティブです")
        return

    from agents import supervisor, poster
    from agents.x_poster import run as x_post_run

    # 監視チェック
    sup = supervisor.run(auto_kill=True)
    if sup["killed"]:
        return

    if platform in ("both", "threads"):
        t_result = poster.run()
        if t_result["posted"]:
            print(f"🧵 Threads: 投稿成功 [{t_result['post_id']}]")

    if platform in ("both", "x"):
        x_result = x_post_run()
        if x_result["posted"]:
            print(f"𝕏  X: 投稿成功 [{x_result['tweet_id']}]")


def cmd_status():
    """現在の状態を表示"""
    from agents import supervisor
    from core.state import load_queue, load_history, load_json
    from config import X_QUEUE_FILE, X_HISTORY_FILE

    sup = supervisor.run(auto_kill=False)
    threads_queue = load_queue()
    threads_history = load_history(limit=50)
    x_queue = load_json(X_QUEUE_FILE, default=[])
    x_history = load_json(X_HISTORY_FILE, default=[])

    today = datetime.now().date().isoformat()
    threads_today = [h for h in threads_history if h.get("posted_at", "").startswith(today)]
    x_today = [h for h in x_history if h.get("posted_at", "").startswith(today)]

    print(f"\n📊 時雨 / 龍脈命術 - 状態レポート")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Kill Switch: {'⛔ ON' if is_killed() else '✅ OFF'}")
    print(f"\n   [Threads: shigure_fortune]")
    print(f"     本日の投稿数: {len(threads_today)}件 / キュー残: {len(threads_queue)}件")
    print(f"\n   [X: @Shigure_fortune]")
    print(f"     本日の投稿数: {len(x_today)}件 / キュー残: {len(x_queue)}件")
    print(f"\n   エラー(直近1h): {sup['errors']['recent_error_count']}件")
    print(f"   ステータス: {sup['status']}")

    if threads_queue:
        print(f"\n   📋 Threadsキュー 次の3件:")
        for i, p in enumerate(threads_queue[:3]):
            score = p.get("quality_score", 0)
            print(f"    {i+1}. [{p.get('theme', '?')}] (⭐{score}) {p.get('text', '')[:45]}...")

    if x_queue:
        print(f"\n   📋 Xキュー 次の3件:")
        for i, p in enumerate(x_queue[:3]):
            print(f"    {i+1}. [{p.get('theme', '?')}] {p.get('x_text', '')[:45]}...")


def cmd_kill():
    from agents.supervisor import kill
    kill("CLIから手動停止")
    print("⛔ Kill Switch を有効化しました")


def cmd_revive():
    from agents.supervisor import revive
    revive()
    print("✅ Kill Switch を解除しました。龍脈が再び流れます。")


COMMANDS = {
    "daily": cmd_daily,
    "post": cmd_post,
    "status": cmd_status,
    "kill": cmd_kill,
    "revive": cmd_revive,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    func = COMMANDS.get(cmd)
    if func is None:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
    elif cmd == "post" and len(sys.argv) > 2:
        func(platform=sys.argv[2])
    else:
        func()
