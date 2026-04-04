#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 占術家 時雨 - 自動投稿 cron 設定
# Threads: shigure_fortune / X: @Shigure_fortune
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# crontab -e で以下を追加する
# PYTHON=/path/to/venv/bin/python
# DIR=/path/to/threads_agent
# LOG=/path/to/logs
#
# ─── 毎朝 7:30 に全体実行（分析→リサーチ→生成）───
# 30 7 * * * $PYTHON $DIR/main.py daily >> $LOG/daily.log 2>&1
#
# ─── Threads + X 同時投稿（1日10スロット）───
# 0  8  * * * $PYTHON $DIR/main.py post both
# 0  10 * * * $PYTHON $DIR/main.py post both
# 0  12 * * * $PYTHON $DIR/main.py post both
# 0  14 * * * $PYTHON $DIR/main.py post both
# 0  16 * * * $PYTHON $DIR/main.py post both
# 0  18 * * * $PYTHON $DIR/main.py post both
# 0  20 * * * $PYTHON $DIR/main.py post both
# 0  21 * * * $PYTHON $DIR/main.py post both
# 0  22 * * * $PYTHON $DIR/main.py post both
# 0  23 * * * $PYTHON $DIR/main.py post both
#
# ─── Xだけ追加投稿したい場合（朝の一言など）───
# 0  7  * * * $PYTHON $DIR/main.py post x
# 0  13 * * * $PYTHON $DIR/main.py post x
#
# ─── 深夜の状態確認（ログ用）───
# 0  0  * * * $PYTHON $DIR/main.py status >> $LOG/status.log 2>&1
#
# ━━ 注意 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# X APIの無料プランは1日17ツイートまで（2024年時点）
# Threadsは制限が緩いが1日15件を上限に設定済み
# Kill Switchが入っている場合は全投稿が自動停止する
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo "cron_example.sh - このファイルをcronに設定してください"
