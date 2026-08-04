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
MODEL_HEAVY = "claude-sonnet-4-6"  # ライター・アナリスト（opus→sonnet: ~40%コスト削減）
MODEL_LIGHT = "claude-haiku-4-5"   # 品質スコア・類似度判定

# Quality thresholds
QUALITY_THRESHOLD = 7.0            # 合格ライン（10点満点の平均）
MAX_QUALITY_RETRIES = 1            # 書き直し最大回数（2→1: 最大2回生成に削減）

# Similarity
SIMILARITY_THRESHOLD = 0.72        # 類似度上限（0.85→0.72: 重複投稿を防ぐため引き下げ）
SIMILARITY_HISTORY_SIZE = 100      # 比較対象の過去投稿数

# Posting schedule
DAILY_POST_LIMIT = 20
MIN_POST_INTERVAL_HOURS = 0.5  # 1h→0.5h: 夜帯1時間おきcronでもスキップされないよう緩和
POST_SLOTS = [7, 11, 12, 16, 21, 22]  # 6スロット/日（2026-08-05: 9→11に変更。hourly_stats n=251: 11時avg15.9L/6.52% vs 9時avg5.2L/3.69%）
# 07=15.2L / 11=15.9L(新) / 12=12.9L / 16=22.0L / 21=10.4L / 22=16.6L（avg_likes実績）

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
OUR_REPLY_IDS_FILE = DATA_DIR / "our_reply_ids.json"
HOURLY_STATS_FILE = DATA_DIR / "hourly_stats.json"
EVENT_CALENDAR_FILE = KNOWLEDGE_DIR / "event_calendar.json"

IMAGE_MAP_FILE = KNOWLEDGE_DIR / "image_map.json"
IMAGE_ASSETS_DIR = BASE_DIR / "assets" / "images"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/amatsukishigure-ui/shigure-images/master"

ACCOUNT_PROFILE_FILE = KNOWLEDGE_DIR / "account_profile.json"
POST_PATTERNS_FILE = KNOWLEDGE_DIR / "post_patterns.json"
THEME_TREE_FILE = KNOWLEDGE_DIR / "theme_tree.json"
HOOK_EXAMPLES_FILE = KNOWLEDGE_DIR / "hook_examples.json"
PERSONAS_FILE = KNOWLEDGE_DIR / "personas.json"
HARM_FILE = KNOWLEDGE_DIR / "harm_pain_points.json"

# Platform settings
X_POSTING_ENABLED = False   # True に戻すと X への自動投稿が再開する
X_CHAR_LIMIT = 280
THREADS_CHAR_LIMIT = 500
X_HASHTAGS = ["#占い", "#吉方位", "#風水", "#龍脈命術", "#時雨"]
X_QUEUE_FILE = DATA_DIR / "x_post_queue.json"
X_HISTORY_FILE = DATA_DIR / "x_post_history.json"
X_PERFORMANCE_FILE = DATA_DIR / "x_performance_data.json"

# Content calendar (曜日×投稿スロットのテーマ固定)
# ブランドサイクル設計:
#   月: 認知・運気情報（今週の方向性を届ける）
#   火: ブランド認知（龍脈命術の差別化・原理）
#   水: ストーリー・信頼（鑑定エピソード・共感）
#   木: 実績・証拠（見てきた事実を共有）
#   金: 行動促進（週末の実践・吉方位活用）
#   土: 哲学・世界観（時雨という人物・信念）← world_view 2.0x ブースト
#   日: 統合・案内（来週準備＋メニュー）
CONTENT_SCHEDULE = {
    "Monday":    ["運気の流れ・吉方位", "星座別吉方位", "共感リスト", "鑑定エピソード", "一言"],
    "Tuesday":   ["龍脈命術とは何か", "他の占いとの違い", "気の流れと場所の仕組み", "方位が人生に影響する理由", "一言"],
    "Wednesday": ["転機のサイン", "星座別恋愛運", "引越し・移転エピソード", "吉方位の使い方", "一言"],
    "Thursday":  ["鑑定でわかったこと", "停滞する人の共通点", "動き出した人に共通すること", "転機が来る前の兆候", "一言"],
    "Friday":    ["気の整え方・ルーティン", "吉方位と行動タイミング", "金運・仕事運リスト", "よくある相談への答え", "一言"],
    "Saturday":  ["なぜ龍脈命術を使うのか", "占いへの想い", "時雨が大切にしていること", "動ける占いとは何か", "一言"],
    "Sunday":    ["気の整え方・準備", "運気予報・気の流れ読み", "人生の転機と方位", "メニュー案内", "一言"],
}
