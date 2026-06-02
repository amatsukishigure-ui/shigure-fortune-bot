"""
メインオーケストレーター（時雨 / 龍脈命術）

Threads（shigure_fortune）と X（@Shigure_fortune）の両方に自動投稿する。

使い方:
  python main.py daily        # 毎朝1回の全体実行（分析→リサーチ→生成）
  python main.py post         # 1投稿スロット：Threads + X 両方に投稿
  python main.py post threads # Threadsのみ投稿
  python main.py post x       # Xのみ投稿
  python main.py status       # 状態確認（フォロワー数・ベスト時間帯含む）
  python main.py review       # 承認待ち投稿を確認
  python main.py approve      # 全件承認してキューへ
  python main.py approve 0 2  # 指定番号だけ承認
  python main.py reject 1     # 指定番号を削除
  python main.py optimize     # 投稿時間帯の最適化レポート
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
    researcher.run(max_themes=5)

    print("\n📡 [+] トラッカー: フォロワー数・時間帯統計を更新")
    from agents import tracker
    tracker.run()

    print("\n💬 [+] リプライヤー: コメント自動返信")
    from agents import replier
    r_result = replier.run()
    if r_result["replied"] > 0:
        print(f"   {r_result['replied']}件返信しました")

    if datetime.now().weekday() == 0:  # 月曜日
        print("\n♻️  [+] リポスター: 人気投稿の再掲（週1回）")
        from agents import reposter
        reposter.run()

    print("\n✍️  [5/5] ライター: 投稿生成（時雨として）")
    result = writer.run(batch_size=12)
    print(
        f"   生成: {result['generated']}件 → "
        f"承認待ち Threads: {result['queued_threads']}件, "
        f"X: {result['queued_x']}件, "
        f"棄却: {result['rejected']}件"
    )
    print(f"   📋 python main.py review  で内容を確認 → approve で承認")

    print(f"\n{'='*55}")
    print(f"✅ Daily Run 完了 - 龍の気が整いました")
    print(f"{'='*55}\n")


def cmd_post(platform: str = "threads"):
    """
    1投稿スロット分の実行（cronから複数回/日呼ぶ）。

    platform: 'threads' | 'both' | 'x'
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
    from core.state import (
        load_queue, load_history, load_json, load_pending,
        get_follower_growth, get_best_hours,
    )
    from config import X_QUEUE_FILE, X_HISTORY_FILE

    sup = supervisor.run(auto_kill=False)
    threads_queue = load_queue()
    threads_history = load_history(limit=50)
    x_queue = load_json(X_QUEUE_FILE, default=[])
    x_history = load_json(X_HISTORY_FILE, default=[])
    pending = load_pending()

    today = datetime.now().date().isoformat()
    threads_today = [h for h in threads_history if h.get("posted_at", "").startswith(today)]
    x_today = [h for h in x_history if h.get("posted_at", "").startswith(today)]

    print(f"\n📊 時雨 / 龍脈命術 - 状態レポート")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Kill Switch: {'⛔ ON' if is_killed() else '✅ OFF'}")

    # フォロワー情報
    growth = get_follower_growth(7)
    if growth["current"] is not None:
        sign = "+" if (growth["delta"] or 0) >= 0 else ""
        delta_str = f"（7日間: {sign}{growth['delta']}人）" if growth["delta"] is not None else ""
        print(f"   👥 フォロワー数: {growth['current']:,}人 {delta_str}")

    # ベスト時間帯
    best = get_best_hours(3)
    if best:
        best_str = "、".join(f"{h}時" for h, *_ in best)
        print(f"   ⭐ エンゲージ上位時間帯: {best_str}")

    print(f"\n   [Threads: shigure_fortune]")
    print(f"     本日の投稿数: {len(threads_today)}件 / キュー残: {len(threads_queue)}件 / 承認待ち: {len(pending)}件")
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


def cmd_review():
    """承認待ち投稿を一覧表示する"""
    from core.state import load_pending

    pending = load_pending()
    if not pending:
        print("\n📋 承認待ちの投稿はありません。")
        print("   python main.py daily  を実行して投稿を生成してください。")
        return

    print(f"\n{'='*60}")
    print(f"📋 承認待ち投稿一覧  ({len(pending)}件)")
    print(f"{'='*60}")

    for i, p in enumerate(pending):
        pattern = p.get("pattern_id", "?")
        theme = p.get("theme", "?")
        score = p.get("quality_score", 0)
        x_mark = " [X対応]" if p.get("x_eligible") else ""
        thread_count = len(p.get("thread_posts", []))
        is_thread = thread_count > 1

        print(f"\n┌─ [{i}] {pattern}{x_mark}{'  🌳ツリー'+str(thread_count)+'件' if is_thread else ''}")
        print(f"│  テーマ: {theme}")
        if score:
            print(f"│  品質スコア: {score:.1f}")
        print(f"│")

        if is_thread:
            for j, tp in enumerate(p.get("thread_posts", [])):
                prefix = "│  1投稿目" if j == 0 else f"│  {j+1}投稿目"
                print(f"{prefix}: {tp}")
                if j < thread_count - 1:
                    print(f"│  　↓")
        else:
            text = p.get("text", "")
            for line in text.splitlines():
                print(f"│  {line}")

        print(f"└{'─'*58}")

    print(f"\n操作コマンド:")
    print(f"  python main.py approve          # 全件承認してキューへ")
    print(f"  python main.py approve 0 2 4    # 指定番号だけ承認")
    print(f"  python main.py reject 1         # 指定番号を削除")


def cmd_approve(*args):
    """
    承認待ち投稿をキューに移す。
    引数なし → 全件承認
    数字を指定 → その番号だけ承認（例: approve 0 2 4）
    """
    from core.state import load_pending, approve_pending

    pending = load_pending()
    if not pending:
        print("\n📋 承認待ちの投稿はありません。")
        return

    if args:
        try:
            indices = [int(a) for a in args]
        except ValueError:
            print("❌ 番号を数字で指定してください（例: approve 0 2）")
            return
        out_of_range = [i for i in indices if i < 0 or i >= len(pending)]
        if out_of_range:
            print(f"❌ 範囲外の番号があります: {out_of_range}（0〜{len(pending)-1}）")
            return
        count = approve_pending(indices)
        print(f"\n✅ {count}件の投稿を承認してキューに追加しました（番号: {indices}）")
    else:
        count = approve_pending()
        print(f"\n✅ 全{count}件の投稿を承認してキューに追加しました")

    from core.state import load_queue
    print(f"   Threadsキュー残数: {len(load_queue())}件")


def cmd_reject(*args):
    """承認待ちから指定番号の投稿を削除する（例: reject 2）"""
    from core.state import load_pending, reject_pending

    if not args:
        print("❌ 削除する番号を指定してください（例: reject 2）")
        return

    try:
        index = int(args[0])
    except ValueError:
        print("❌ 番号を数字で指定してください（例: reject 2）")
        return

    pending = load_pending()
    if index < 0 or index >= len(pending):
        print(f"❌ 範囲外の番号です（0〜{len(pending)-1}）")
        return

    post = pending[index]
    reject_pending(index)
    print(f"\n🗑  [{index}] {post.get('pattern_id','?')} を削除しました")
    print(f"   承認待ち残数: {len(pending)-1}件")


def cmd_optimize_schedule():
    """
    時間帯別パフォーマンスデータから最適な投稿スケジュールを提案する。
    エンゲージメントが高い時間帯 TOP6 を表示。
    """
    from core.state import get_best_hours, load_json
    from config import HOURLY_STATS_FILE

    stats = load_json(HOURLY_STATS_FILE, default={})
    if not stats:
        print("\n📊 時間帯統計データがまだありません。")
        print("   しばらく運用すると自動的にデータが蓄積されます。")
        return

    print(f"\n{'='*55}")
    print(f"📊 投稿時間帯の最適化レポート")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    best = get_best_hours(top_n=24)  # 全時間帯を取得

    print(f"   時間帯別エンゲージメント（データあり時間帯）:\n")
    has_data = False
    for hour, score, avg_views, avg_likes, count in best:
        if count == 0:
            continue
        has_data = True
        bar = "█" * min(int(score / 3), 20)
        print(f"   {hour:02d}時  {bar:<20} score:{score:.1f}  views:{avg_views:.0f}  likes:{avg_likes:.2f}  ({count}投稿)")

    if not has_data:
        print("   データなし")
        return

    from config import POST_SLOTS
    current_str = " ".join(f"{h:02d}:00" for h in POST_SLOTS)
    top6 = [(h, sc, v, l, c) for h, sc, v, l, c in best if c > 0][:6]
    if top6:
        hours_str = " ".join(f"{h:02d}:00" for h, *_ in top6)
        print(f"\n   ✅ データ推奨 TOP6: {hours_str}")
        print(f"   現在のスケジュール: {current_str}")
        print(f"\n   ※ .github/workflows/post.yml の cron と config.py の POST_SLOTS を")
        print(f"     上記推奨時間に合わせると最大エンゲージメントが期待できます。")

    print(f"\n{'='*55}\n")


def cmd_reply():
    """コメントへの自動返信（毎投稿スロットで呼ばれる）"""
    if is_killed():
        return
    from agents import replier
    result = replier.run()
    if result["replied"] > 0:
        print(f"💬 リプライヤー: {result['replied']}件返信")
    elif result.get("errors", 0) > 0:
        print(f"⚠️  リプライヤー: エラー {result['errors']}件")


def cmd_kill():
    from agents.supervisor import kill
    kill("CLIから手動停止")
    print("⛔ Kill Switch を有効化しました")


def cmd_revive():
    from agents.supervisor import revive
    revive()
    print("✅ Kill Switch を解除しました。龍脈が再び流れます。")


COMMANDS = {
    "daily":    cmd_daily,
    "post":     cmd_post,
    "reply":    cmd_reply,
    "status":   cmd_status,
    "review":   cmd_review,
    "approve":  cmd_approve,
    "reject":   cmd_reject,
    "optimize": cmd_optimize_schedule,
    "kill":     cmd_kill,
    "revive":   cmd_revive,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    func = COMMANDS.get(cmd)
    if func is None:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
    elif cmd == "post" and len(sys.argv) > 2:
        func(platform=sys.argv[2])
    elif cmd in ("approve", "reject") and len(sys.argv) > 2:
        func(*sys.argv[2:])
    else:
        func()
