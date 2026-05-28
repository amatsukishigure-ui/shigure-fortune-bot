import os
from pathlib import Path
from dotenv import load_dotenv

# Windowsでファイルを作成するとUTF-16になる場合があるため両方試みる
_env_path = Path(__file__).parent / ".env"
for _enc in ("utf-8", "utf-16", "utf-8-sig"):
    try:
        load_dotenv(dotenv_path=_env_path, encoding=_enc, override=True)
        break
    except Exception:
        continue

BASE_DIR = Path(__file__).parent

# API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Threads API
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")

# X (Twitter) API v2
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")

# Models
MODEL_HEAVY = "claude-opus-4-6"    # ライター・アナリスト
MODEL_LIGHT = "claude-haiku-4-5"   # 品質スコア・類似度判定

# Quality thresholds
QUALITY_THRESHOLD = 7.0            # 合格ライン（10点満点の平均）
MAX_QUALITY_RETRIES = 2            # 書き直し最大回数

# Similarity
SIMILARITY_THRESHOLD = 0.85        # 類似度上限（これ以上は棄却）
SIMILARITY_HISTORY_SIZE = 100      # 比較対象の過去投稿数

# Posting schedule
DAILY_POST_LIMIT = 15
MIN_POST_INTERVAL_HOURS = 1
POST_SLOTS = [7, 12, 18, 20, 21, 22]  # 投稿時刻リスト（朝・昼・夜の峰に集中）

# Pattern rotation
MAX_SAME_PATTERN_CONSECUTIVE = 3   # 同パターン連続上限
MAX_SAME_THEME_CONSECUTIVE = 3     # 同テーマ連続上限

# Error handling
MAX_CONSECUTIVE_ERRORS = 3         # これ以上エラーが続いたら自動停止

# Paths
DATA_DIR = BASE_DIR / "data"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"

POST_QUEUE_FILE = DATA_DIR / "post_queue.json"
PENDING_POSTS_FILE = DATA_DIR / "pending_posts.json"   # 承認待ち投稿
POST_HISTORY_FILE = DATA_DIR / "post_history.json"
PERFORMANCE_FILE = DATA_DIR / "performance_data.json"
RESEARCH_FILE = DATA_DIR / "research_data.json"
ERROR_LOG_FILE = DATA_DIR / "error_log.json"
KILL_SWITCH_FILE = DATA_DIR / "KILL_SWITCH"
FOLLOWER_HISTORY_FILE = DATA_DIR / "follower_history.json"
REPLIED_COMMENTS_FILE = DATA_DIR / "replied_comments.json"
HOURLY_STATS_FILE = DATA_DIR / "hourly_stats.json"
EVENT_CALENDAR_FILE = KNOWLEDGE_DIR / "event_calendar.json"

ACCOUNT_PROFILE_FILE = KNOWLEDGE_DIR / "account_profile.json"
POST_PATTERNS_FILE = KNOWLEDGE_DIR / "post_patterns.json"
THEME_TREE_FILE = KNOWLEDGE_DIR / "theme_tree.json"
HOOK_EXAMPLES_FILE = KNOWLEDGE_DIR / "hook_examples.json"
PERSONAS_FILE = KNOWLEDGE_DIR / "personas.json"

# Platform settings
X_CHAR_LIMIT = 280
THREADS_CHAR_LIMIT = 500
X_HASHTAGS = ["#占い", "#吉方位", "#風水", "#龍脈命術", "#時雨"]
X_QUEUE_FILE = DATA_DIR / "x_post_queue.json"
X_HISTORY_FILE = DATA_DIR / "x_post_history.json"
X_PERFORMANCE_FILE = DATA_DIR / "x_performance_data.json"

# Content calendar (曜日×投稿スロットのテーマ固定)
CONTENT_SCHEDULE = {
    "Monday":    ["今週の運気", "星座別吉方位", "共感リスト", "鑑定エピソード", "一言"],
    "Tuesday":   ["運がいい人", "風水豆知識", "仕事運リスト", "世界観", "一言"],
    "Wednesday": ["龍脈メッセージ", "星座別恋愛運", "引越しエピソード", "吉方位使い方", "一言"],
    "Thursday":  ["運気の底", "風水豆知識", "転機のサイン", "鑑定エピソード", "一言"],
    "Friday":    ["週末の整え方", "週末吉方位", "金運リスト", "鑑定感想", "一言"],
    "Saturday":  ["今日動く人", "風水豆知識", "よくある相談", "時雨の世界観", "一言"],
    "Sunday":    ["来週の準備", "来週運気予報", "人生の転機", "メニュー案内", "一言"],
}
