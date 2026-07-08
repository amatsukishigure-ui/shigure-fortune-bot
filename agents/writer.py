"""
ライターエージェント（時雨 / 龍脈命術）

リサーチデータとアナリストのフィードバックを元に投稿を生成する。
品質スコア・類似度チェックを経て、Threads/X 両方のキューに追加する。
"""

import json
import anthropic
from datetime import datetime, timezone, timedelta, date
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, MODEL_HEAVY,
    MAX_QUALITY_RETRIES, MAX_SAME_PATTERN_CONSECUTIVE, MAX_SAME_THEME_CONSECUTIVE,
    ACCOUNT_PROFILE_FILE, POST_PATTERNS_FILE, THEME_TREE_FILE, HOOK_EXAMPLES_FILE,
    PERSONAS_FILE, HARM_FILE, CONTENT_SCHEDULE, DATA_DIR,
)
from core.state import (
    load_json, load_history, load_research, enqueue_post, enqueue_pending,
)
from core.similarity import SimilarityChecker
from core.quality import score_with_retry
from core.formatter import format_for_x, format_for_threads, get_x_hashtags_for_theme
from agents.x_poster import enqueue_x_post


def _load_knowledge() -> dict:
    personas_data = load_json(PERSONAS_FILE, default={})
    return {
        "profile": load_json(ACCOUNT_PROFILE_FILE, default={}),
        "patterns": load_json(POST_PATTERNS_FILE, default=[]),
        "theme_tree": load_json(THEME_TREE_FILE, default={}),
        "hook_examples": load_json(HOOK_EXAMPLES_FILE, default=[]),
        "personas": personas_data.get("personas", []),
        "harm": load_json(HARM_FILE, default={}),
    }


def _get_recent_patterns(history: list, n: int = MAX_SAME_PATTERN_CONSECUTIVE) -> list:
    return [h.get("pattern_id") for h in history[-n:] if h.get("pattern_id")]


def _get_recent_themes(history: list, n: int = MAX_SAME_THEME_CONSECUTIVE) -> list:
    return [h.get("theme") for h in history[-n:] if h.get("theme")]


def _get_todays_schedule() -> list:
    """今日の曜日に対応するコンテンツスケジュールを返す"""
    day = datetime.now().strftime("%A")  # 'Monday', 'Tuesday', ...
    return CONTENT_SCHEDULE.get(day, CONTENT_SCHEDULE["Monday"])


ZODIAC_SIGNS = [
    "♈牡羊座", "♉牡牛座", "♊双子座", "♋蟹座",
    "♌獅子座", "♍乙女座", "♎天秤座", "♏蠍座",
    "♐射手座", "♑山羊座", "♒水瓶座", "♓魚座",
]

# 星座記号→名前のマップ（置換に使用）
_ZODIAC_MARK_TO_NAME = {z[0]: z[1:] for z in ZODIAC_SIGNS}
_ZODIAC_NAME_TO_MARK = {z[1:]: z[0] for z in ZODIAC_SIGNS}


def _fix_zodiac(text: str, target_zodiac: str) -> str:
    """
    生成テキスト内の星座記号・名前を target_zodiac に統一する。
    モデルが指定外の星座を使った場合の事後補正。
    target_zodiac 例: "♍乙女座"
    """
    if not target_zodiac or not text:
        return text
    target_mark = target_zodiac[0] if target_zodiac[0] in _ZODIAC_MARK_TO_NAME else ""
    target_name = _ZODIAC_MARK_TO_NAME.get(target_mark, target_zodiac.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓"))
    if not target_mark or not target_name:
        return text

    import re
    # 他の星座記号をターゲット記号に置換
    wrong_marks = [m for m in _ZODIAC_MARK_TO_NAME if m != target_mark]
    for wm in wrong_marks:
        wn = _ZODIAC_MARK_TO_NAME[wm]
        # 「♊双子座」→「♍乙女座」
        text = text.replace(f"{wm}{wn}", f"{target_mark}{target_name}")
        # 記号だけ残っている場合: 「♊」→「♍」
        text = re.sub(re.escape(wm) + r"(?!" + "|".join(re.escape(n) for n in _ZODIAC_MARK_TO_NAME.values()) + ")", target_mark, text)
        # 名前だけ残っている場合: 「双子座」→「乙女座」
        text = text.replace(wn, target_name)
    return text


# GitHub Actionsのスケジュール（JST） — config.POST_SLOTS と必ず一致させること
from config import POST_SLOTS as SCHEDULED_HOURS_JST  # noqa: E402
JST = timezone(timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _get_jst_hour() -> int:
    return datetime.now(JST).hour


def _estimate_post_time(queue_length: int, batch_position: int) -> datetime:
    """
    現在のキュー長とバッチ内の位置から投稿予定時刻を推定する。
    スケジュールは SCHEDULED_HOURS_JST（JST）の順で1投稿/1スロット消費。

    queue_length には post_queue + pending_posts の合計を渡すこと。
    """
    now = datetime.now(JST)

    # 今後5日分のスケジュールスロットを生成（現在時刻より後のみ）
    # 3日だと夜遅い daily 実行時にスロット不足になるケースがあるため5日に拡張
    # ※ h=0（深夜0時）は「その日の00:00」ではなく「翌日の00:00」として扱う
    #   SCHEDULED_HOURS_JST = [..., 23, 0] の「0」は翌日深夜を意味するため
    upcoming_slots = []
    for delta_days in range(5):
        day = now.date() + timedelta(days=delta_days)
        for h in SCHEDULED_HOURS_JST:
            if h == 0:
                # 深夜0時スロット = 翌日 00:00（当日の0:00は00:00ではなく翌日扱い）
                slot_day = day + timedelta(days=1)
                slot = datetime(slot_day.year, slot_day.month, slot_day.day, 0, 0, tzinfo=JST)
            else:
                slot = datetime(day.year, day.month, day.day, h, 0, tzinfo=JST)
            if slot > now:
                upcoming_slots.append(slot)

    # h=0が先頭に処理される関係で時刻が前後するため昇順に並べ直す
    upcoming_slots.sort()

    # キュー＋承認待ちの既存投稿を先に消費し、その後のスロットにこの投稿が来る
    idx = queue_length + batch_position
    if idx < len(upcoming_slots):
        return upcoming_slots[idx]
    return upcoming_slots[-1] if upcoming_slots else now


def _format_datetime_jp(dt: datetime) -> str:
    """datetime を「M月D日（曜）HH:MM」形式に変換"""
    wd = WEEKDAY_JP[dt.weekday()]
    return f"{dt.month}月{dt.day}日（{wd}）{dt.strftime('%H:%M')}"


def _get_season_context(dt: datetime) -> str:
    """推定投稿日から季節・節気コンテキストを生成する（季節ズレ防止）"""
    m, d = dt.month, dt.day
    if m == 1:
        season = "冬（1月）"
        sekki = "小寒（1/5頃）→ 大寒（1/20頃）"
        notes = "年明け・寒の入り・受験シーズン"
        ng = "秋分・春分・夏至・立春はまだ先"
    elif m == 2:
        season = "冬〜早春（2月）"
        sekki = "立春（2/4頃）→ 雨水（2/19頃）"
        notes = "節分・立春・梅の季節"
        ng = "秋・夏の節気は使わないこと"
    elif m == 3:
        season = "春（3月）"
        sekki = "啓蟄（3/6頃）→ 春分（3/20頃）"
        notes = "春分・お彼岸・桜の季節"
        ng = "秋分・夏至・冬至は使わないこと"
    elif m == 4:
        season = "春（4月）"
        sekki = "清明（4/5頃）→ 穀雨（4/20頃）"
        notes = "桜・花見・新年度スタート"
        ng = "秋分・夏至・冬至は使わないこと"
    elif m == 5:
        season = "初夏（5月）"
        sekki = "立夏（5/5頃）→ 小満（5/21頃）" if d <= 21 else "小満（5/21）過ぎ → 芒種（6/6頃）へ"
        notes = "ゴールデンウィーク・初夏・梅雨前・新緑"
        ng = "秋分・春分・夏至・冬至は使わないこと（夏至は6月下旬）"
    elif m == 6:
        season = "初夏〜梅雨（6月）"
        sekki = "芒種（6/6頃）→ 夏至（6/21頃）"
        notes = "梅雨入り・夏至・紫陽花の季節"
        ng = "秋分・春分・冬至は使わないこと"
    elif m == 7:
        season = "夏・梅雨明け（7月）"
        sekki = "小暑（7/7頃）→ 大暑（7/23頃）"
        notes = "梅雨明け・七夕・盛夏"
        ng = "秋分・春分・冬至は使わないこと"
    elif m == 8:
        season = "真夏〜晩夏（8月）"
        sekki = "立秋（8/7頃）→ 処暑（8/23頃）"
        notes = "お盆・立秋・夏の終わり"
        ng = "春分・冬至は使わないこと（秋分は9月末）"
    elif m == 9:
        season = "初秋（9月）"
        sekki = "白露（9/8頃）→ 秋分（9/23頃）"
        notes = "敬老の日・秋分・お彼岸・夜長"
        ng = "春分・夏至・冬至は使わないこと"
    elif m == 10:
        season = "秋（10月）"
        sekki = "寒露（10/8頃）→ 霜降（10/23頃）"
        notes = "紅葉・秋の行楽・ハロウィン"
        ng = "春分・夏至は使わないこと"
    elif m == 11:
        season = "晩秋（11月）"
        sekki = "立冬（11/7頃）→ 小雪（11/22頃）"
        notes = "七五三・立冬・紅葉の見頃"
        ng = "春分・夏至・秋分は使わないこと"
    else:  # m == 12
        season = "冬（12月）"
        sekki = "大雪（12/7頃）→ 冬至（12/22頃）"
        notes = "冬至・クリスマス・年末・大掃除"
        ng = "春分・夏至・秋分は使わないこと"

    return (
        f"現在の季節: {season}\n"
        f"近い節気: {sekki}\n"
        f"季節メモ: {notes}\n"
        f"⚠️ NG節気（この時期に使ってはいけない節気・季節語）: {ng}"
    )


def _get_recent_zodiac_deep_signs(history: list, days: int = 14) -> list:
    """直近N日のzodiac_deep投稿で使われた星座のリストを返す"""
    import re
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    marks = "♈♉♊♋♌♍♎♏♐♑♒♓"
    used = []
    for h in history:
        if h.get("pattern_id") != "zodiac_deep":
            continue
        if h.get("posted_at", "") < cutoff:
            continue
        thread_posts = h.get("thread_posts", [])
        text = thread_posts[0] if thread_posts else h.get("text", "")
        m = re.search(f"([{marks}])", text)
        if m:
            for z in ZODIAC_SIGNS:
                if z[0] == m.group(1):
                    used.append(z)
                    break
    return used


def _get_recent_zodiac_signs_any(history: list, hours: int = 24) -> list:
    """直近N時間に zodiac 系パターンで使われた星座を返す（同日の重複投稿防止用）"""
    import re
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    marks = "♈♉♊♋♌♍♎♏♐♑♒♓"
    zodiac_patterns = {"zodiac_deep", "caligula", "zodiac_target"}
    used = []
    for h in history:
        if h.get("pattern_id") not in zodiac_patterns:
            continue
        posted = h.get("posted_at") or h.get("queued_at", "")
        if posted < cutoff:
            continue
        thread_posts = h.get("thread_posts", [])
        text = thread_posts[0] if thread_posts else h.get("text", "")
        m = re.search(f"([{marks}])", text)
        if m:
            for z in ZODIAC_SIGNS:
                if z[0] == m.group(1):
                    used.append(z)
                    break
    return used


def _pick_zodiac(exclude: list | None = None) -> str:
    import random
    available = [z for z in ZODIAC_SIGNS if z not in (exclude or [])]
    return random.choice(available if available else ZODIAC_SIGNS)


def _pick_persona(personas: list, recent_persona_ids: list = None) -> dict | None:
    """ペルソナリストからランダムに1件選ぶ（直近使用済みは避ける）"""
    import random
    if not personas:
        return None
    recent_ids = set(recent_persona_ids or [])
    fresh = [p for p in personas if p.get("id") not in recent_ids]
    pool = fresh if fresh else personas
    return random.choice(pool)


def _load_weight_adjustments() -> dict:
    """analyst が生成したパターン重み補正係数を読み込む（なければ空dict）"""
    from config import DATA_DIR
    import json
    adj_file = DATA_DIR / "pattern_weight_adjustments.json"
    try:
        if adj_file.exists():
            data = json.loads(adj_file.read_text(encoding="utf-8"))
            return data.get("adjustments", {})
    except Exception:
        pass
    return {}


def _select_pattern(
    patterns: list,
    recent_patterns: list,
    platform_hint: str = "threads",
    hour: int = 12,
    excluded_patterns: set = None,
    long_history_patterns: list = None,
) -> dict:
    """
    投稿パターンを重み付きランダムで選択する。

    pattern_id + daily_limit の仕組み:
        各パターンはバッチ内で daily_limit 回まで。
        run() が excluded_patterns を管理し、上限に達したパターンIDを渡す。

    long_history_patterns: 直近30件のpattern_idリスト。
        未出現パターンに starvation boost（2.0x）を掛けて偏りを補正する。

    format の種類（参考）:
        short  … 1〜4行、120字以内のひとこと投稿（ici_gon / emoji_comment 等）
        medium … 120〜280字の通常投稿（ほとんどのパターン）
        long   … 280字超 or 箇条書き一覧型（hoshi_betsu / list / menu_guide 等）
        tree   … 複数投稿ツリー（empathy_thread）
    """
    import random
    excluded_pids = excluded_patterns or set()
    blocked = set(recent_patterns)
    recent30 = set(long_history_patterns or [])

    # プラットフォーム対応 & daily_limit 制限を除外 & hold除外
    available = [
        p for p in patterns
        if p.get("status", "active") == "active"
        and p.get("id") not in blocked
        and platform_hint in p.get("platforms", ["threads"])
        and p.get("id", "") not in excluded_pids
    ]
    if not available:
        # recent_patterns ブロックを解除して再試行（全件消費ガード）
        available = [
            p for p in patterns
            if p.get("status", "active") == "active"
            and platform_hint in p.get("platforms", ["threads"])
            and p.get("id", "") not in excluded_pids
        ]
    if not available:
        # daily_limit 制限も解除（フォールバック）
        available = [
            p for p in patterns
            if p.get("status", "active") == "active"
            and platform_hint in p.get("platforms", ["threads"])
        ]

    # パフォーマンスフィードバックによる動的補正係数を取得
    perf_adjustments = _load_weight_adjustments()

    # 時間帯・曜日による weight ブースト
    from datetime import datetime, timezone
    _today_wd = datetime.now(timezone.utc).weekday()  # 月=0
    weights = []
    for p in available:
        w = p.get("weight", 1.0)
        pid = p.get("id", "")

        # 過去30日パフォーマンスによる動的補正（0.5〜2.0x）
        w *= perf_adjustments.get(pid, 1.0)

        # 直近30件に未登場のパターンに starvation boost（2.0x）
        # 重みが低いパターンを自然に底上げし、長期的な偏りを補正する
        if recent30 and pid not in recent30:
            w *= 2.0

        # preferred_hours ブースト: 最優先時間帯は 2.5x
        if hour in p.get("preferred_hours", []):
            w *= 2.5

        # 木曜（wd=3）: world_view Eモード（実績語り）と shigure_voice を週次強化
        if _today_wd == 3 and pid in ("world_view", "shigure_voice"):
            w *= 1.5

        # 月初め（1〜3日）: hoshi_betsu を月次スパイク狙いで 3.0x
        # 実績: 6月1日〜版 v=1022（全月トップ）— 月変わりのタイミングに強い
        _today_dom = datetime.now().day
        if 1 <= _today_dom <= 3 and pid == "hoshi_betsu":
            w *= 3.0

        weights.append(w)

    return random.choices(available, weights=weights, k=1)[0]


def _select_theme(theme_tree: dict, recent_themes: list, schedule_hint: str = "") -> str:
    import random

    def flatten(node, prefix=""):
        items = []
        for k, v in node.items():
            label = f"{prefix}/{k}" if prefix else k
            if isinstance(v, dict):
                items.extend(flatten(v, label))
            else:
                items.append(label)
        return items

    all_themes = flatten(theme_tree)

    # スケジュールヒントに近いテーマを優先
    if schedule_hint:
        hint_themes = [t for t in all_themes if any(
            keyword in t for keyword in schedule_hint.replace("・", "/").split("/")
        )]
        if hint_themes:
            return random.choice(hint_themes)

    recent_set = set(recent_themes)
    fresh = [t for t in all_themes if t not in recent_set]
    return random.choice(fresh if fresh else all_themes)


def _build_prompt(
    pattern: dict,
    theme: str,
    knowledge: dict,
    research_topics: list,
    analyst_feedback: str,
    feedback_for_rewrite: str = "",
    extra_hint: dict = None,
) -> str:
    """投稿生成プロンプトを構築する"""
    profile = knowledge["profile"]
    profile_summary = (
        f"占術家名: {profile.get('name')}\n"
        f"肩書き: {profile.get('title')}\n"
        f"独自メソッド: {profile.get('original_method')}\n"
        f"コアコンセプト: {profile.get('core_concept')}\n"
        f"口調: {profile.get('tone')}\n"
        f"NGワード: {', '.join(profile.get('ng_words', []))}\n"
        f"シグネチャ: 占術家 時雨（投稿の最後、世界観投稿・長め投稿のみ使用）"
    )

    import random as _rnd
    _hooks = list(knowledge["hook_examples"])
    _rnd.shuffle(_hooks)
    hooks_sample = "\n".join(f"- {h}" for h in _hooks[:10])

    def _fmt_topic(t: dict) -> str:
        parts = [f"- {t.get('title', '')}: {t.get('summary', '')}"]
        if t.get("cultural_angle"):
            parts.append(f"  （文化的視点: {t['cultural_angle']}）")
        if t.get("season_note"):
            parts.append(f"  （季節・天文: {t['season_note']}）")
        if t.get("hook_idea"):
            parts.append(f"  （フックアイデア: {t['hook_idea']}）")
        return "\n".join(parts)

    matched = [
        t for t in research_topics
        if any(k in t.get("theme", "") for k in theme.split("/"))
    ][:5]
    research_text = "\n".join(_fmt_topic(t) for t in matched) or "（関連リサーチなし）"

    if extra_hint is None:
        extra_hint = {}

    # 鑑定ページURL（メニュー誘導・ソフト誘導パターンで使用）
    service_url = knowledge.get("profile", {}).get("service_url", "https://shigurerooms.hp.peraichi.com")

    # ── 推定投稿時刻ブロック（季節コンテキストのみ。具体的日付は注入しない） ──
    # 注意: キューが変動するため「今日=○月○日」の日付注入は日付ズレを招く。
    # 季節・節気の正確さだけ維持し、日付は相対表現（今日/明日）のみを許可する。
    est_dt: datetime | None = extra_hint.get("estimated_post_time")
    if est_dt and isinstance(est_dt, datetime):
        est_date_str = est_dt.date().isoformat()
        season_ctx = _get_season_context(est_dt)
    else:
        now_jst = datetime.now(JST)
        est_date_str = now_jst.date().isoformat()
        season_ctx = _get_season_context(now_jst)
    post_time_block = (
        f"## 季節コンテキスト（必ず守ること）\n"
        f"### 実際の季節情報\n"
        f"{season_ctx}\n\n"
        f"⛔ 上記NG節気・季節語は絶対に使わないこと。\n"
        f"   正しい節気・季節のみを使い、季節と内容が一致しない投稿を生成しないこと。\n\n"
        f"⛔ **日付の数字書き禁止（最重要）**: 「今日（7/11）」「明日（7月12日）」「7月13日は」のように\n"
        f"   具体的な月日を投稿に書かないこと。\n"
        f"   「今日」「明日」「今週」「今夜」などの相対表現のみを使うこと。\n"
        f"   キューに溜まると実際の投稿日と必ずズレるため、日付の数字は絶対禁止。\n"
    )
    # ─────────────────────────────────────────────────────────────

    rewrite_block = ""
    if feedback_for_rewrite:
        rewrite_block = f"\n## 前回の添削フィードバック（必ず反映）\n{feedback_for_rewrite}\n"

    # パターン固有の追加ルール
    pattern_id = pattern.get("id", "")
    pattern_extra = ""
    if pattern_id == "pop_short":
        import random as _rnd_pop
        _pop_mode = _rnd_pop.choice(["question", "question", "tip"])  # question 2/3確率（実績最強）
        pattern_extra = f"""
## ポップ短文型の追加ルール

### 今回のモード: {"質問型+具体アクション（最強）" if _pop_mode == "question" else "ヒント型（シンプル情報提供）"}

### 実績から見た強い投稿 vs 弱い投稿（n=5件）
強い（eng=26）:
- 「クローゼットぎゅうぎゅうにしてない？気が詰まってると、チャンスの流れが入ってこない。週1だけでいい、一枚出してみて。」
→ 質問フック + 具体的な生活場面 + 「〜するだけでいい」アクション + カジュアル語尾

弱い（eng=3〜8）:
- 一般理論・哲学系（「吉方位とは何か」系）
- 「気の流れを理解することで〜」という解説系

### {"質問型+具体アクション の構成（必須）" if _pop_mode == "question" else "ヒント型 の構成"}
{"1. 質問フック（1行）: 生活の具体的な場面を質問形式で「〜してない？」「〜になってない？」で始める\\n2. 理由（1行）: 「気が〜」「方位的に〜」で短く根拠を添える\\n3. アクション（1行）: 「〜するだけでいい」「今日〜してみて」で具体的なひとつの行動" if _pop_mode == "question" else "1. 発見（1行）: 「〜は〜だよ」「〜をすると〜」という情報提供\\n2. 理由（1行）: 龍脈命術/気の観点で短く根拠\\n3. アクション（1行）: 「〜してみて」"}

### 生活場面トピック例（毎回違うトピックを選ぶ）
- クローゼット・収納 → 「ぎゅうぎゅうにしてない？」
- 財布・お金 → 「床に置いてない？」
- 玄関・靴 → 「靴がばらばらになってない？」
- 寝室・枕の向き → 「頭をどっちに向けて寝てる？」
- デスク・仕事スペース → 「机の上、物だらけになってない？」
- 冷蔵庫 → 「期限切れのもの入れっぱなしにしてない？」

### 文字数・語尾
- 80〜130文字（コンパクト、最後まで読ませる）
- 語尾: カジュアルに（「〜してみて」「〜してない？」「〜だよ」）
- 絵文字1〜2個使ってOK
"""
    elif pattern_id == "soft_discovery":
        import random as _rnd_sd
        _sd_topic = _rnd_sd.choice(["career", "moving", "relationship", "money", "stagnation"])
        _sd_guide = {
            "career":       "転職・昇進・キャリアの停滞から入る。「同じ努力なのに結果が出る人と出ない人がいる」という観察で始め、原因を「意志の強さ」でも「才能の差」でもなく「向いている方向の違い」として外在化する。",
            "moving":       "引越し・場所選びから入る。「引越しで人生が変わった人がいる」という普遍的な観察で始め、「住む場所が人を変える」という概念を「気の流れ」として後出しで紹介する。",
            "relationship": "人間関係・縁から入る。「なぜ同じような悩みを繰り返すのか」「縁が続く人と続かない人の差」という問いで始め、「いる場所・向かう方向が縁を変える」という概念を後出しで。",
            "money":        "お金・収入から入る。「同じ仕事量でも収入が伸びる人と止まる人がいる」という観察で始め、「頑張り方の問題じゃなく気の流れの問題」という外在化で関心を引く。",
            "stagnation":   "停滞・変われない感覚から入る。「何年も変わっていない感覚がある人へ」という呼びかけで始め、原因を「意志の弱さ」ではなく「向き・気の流れ」に外在化する。",
        }[_sd_topic]
        pattern_extra = f"""
## 新規発見型の追加ルール

### 役割（最重要）
**占いを知らない・信じていない新規層への入口コンテンツ**。
普遍的な悩みから入り、「向き・方向・気の流れ」という概念を占い文脈を「後出し」で届ける。
最初の1〜2行で「占い」「吉方位」を使わない。読んだ後に「これは実は龍脈命術の話だったのか」と気づかせる。

### 今回のテーマ: {_sd_topic}
切り口ガイド: {_sd_guide}

### 構成（140〜200字）
1. **普遍的な問い・観察（1〜2行）**: 占いの言葉を使わず、誰でも「確かに」と思える問いかけ
   例「同じ努力なのに結果が出る人と出ない人がいる。」
2. **外在化（1〜2行）**: 「運じゃない」「才能じゃない」→「向いてる方向が違うだけ」
   → 読者の自己否定を外在化して「あなたのせいじゃない」という安心感を先に作る
3. **ソフトな占術導入（1〜2行）**: 「方位、というと怪しく聞こえるかもしれないけど—」
   → 抵抗感を一言で認めてからサラッと導入。「龍脈命術では〜」は自然に1行で
4. **締め（1行）**: 「心当たりない？」「当てはまる？」「どう思う？」

### 禁止
- 最初の段落で「占い」「吉方位」「風水」を使う
- 「信じれば変わる」などの信仰を求める表現
- 鑑定URLを入れる（このパターンは新規認知が目的）

### 心理効果
- **ブリッジング効果**: 普遍的悩み → 方向性 → 占術、という橋渡しで抵抗感を段階的に下げる
- **外在化効果**: 「あなたのせいじゃない」という安心感が信頼感につながる
- **好奇心誘発**: 「怪しく聞こえるかもしれないけど」という先取りが逆に読み進める動機を作る
"""

    elif pattern_id == "zodiac_target":
        zodiac = extra_hint.get("zodiac", "♈牡羊座")
        # 絵文字マークを抽出（例: "♐射手座" → "♐"）
        _zm = zodiac[0] if zodiac and not zodiac[0].isalpha() and not '぀' <= zodiac[0] <= '鿿' else ""
        _zn = zodiac.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓").strip()
        _pg = (extra_hint or {}).get("pain_genre", "")
        _pain_ctx = f"""
### 今回の悩みジャンル: {_pg}
「{zodiac}で{_pg}に悩んでいる人」に特に刺さる内容にすること。
冒頭1行例: 「{_zm} {_zn}で今、{_pg}が気になっている人へ。」
吉方位・今の運気をこのジャンルと紐づけて具体的に伝える。
このジャンルで検索する人がヒットしやすいよう、本文中に自然にキーワードを入れる。
""" if _pg else ""
        import random as _rnd_zt
        _zt_cta_mode = _rnd_zt.choice(["save", "save", "emoji"])  # 2/3 save, 1/3 emoji
        _zt_emoji_ctas = [
            f"当たってた？ 🌟 をコメントに置いてみて。",
            f"自分の星座だった人、🌟 をひとつコメントで。",
            f"「そうそう」と思った人は 🌟 を置いてって。",
            f"読んだ {_zm} さん、🌟 でいいから置いてみて。",
        ]
        _zt_cta = _rnd_zt.choice(_zt_emoji_ctas) if _zt_cta_mode == "emoji" else ("保存してね" if _rnd_zt.random() > 0.5 else "心当たりある？")
        pattern_extra = f"""
## 星座ターゲット型の追加ルール
- この投稿は「{zodiac}」の人向け
- **必須**: 冒頭1行目を「{_zm} {_zn}さんへ。」の形で始める（星座マーク {_zm} を必ず含める）
- その星座に合った吉方位・今週の運気・アドバイスを1〜2点伝える
- 100〜200文字のコンパクトな内容
- 語尾はやや親しみやすく（「〜してみてね」「〜がいいよ」など）
- **必須**: 末尾は以下のCTAで締める → {_zt_cta}
{_pain_ctx}
### 心理効果
- **スポットライト効果**: 「{zodiac}さんへ。」の呼びかけが「自分だけに話しかけられている」感覚を作る
- **バーナム効果**: 運気・アドバイスは星座らしさを保ちながら「まさに今の自分だ」と感じる言葉選びにする
- **絵文字1文字CTA**: 🌟1文字だけでいいという最小行動原理でコメント摩擦を極限まで下げる
"""
    elif pattern_id == "zodiac_deep":
        zodiac = extra_hint.get("zodiac", "♈牡羊座")
        _zm_d = zodiac[0] if zodiac and not zodiac[0].isalpha() and not '぀' <= zodiac[0] <= '鿿' else ""
        _zn_d = zodiac.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓").strip()
        import random as _rnd_zd
        _zd_theme = _rnd_zd.choice([
            "今週の気の流れ×吉方位行動",
            "今週の転機ポイントとNG行動",
            "今月の大きな流れ×今週の動き方",
            "今週の吉方位×お金・仕事・人間関係",
        ])
        pattern_extra = f"""
## 星座週間深掘り型ツリー（2投稿）の追加ルール

対象星座: {zodiac}（絵文字: {_zm_d}　名称: {_zn_d}）
今回のテーマ軸: {_zd_theme}

### 重要：出力形式
以下の形式で2つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目"]

### 各投稿の構成

**1投稿目（160〜210字）— フック×気の傾向×吉方位1点**
目的: 「自分専用の情報だ」という引力を作り、2投稿目への期待を作る
1. 冒頭（1行）: 「{_zm_d}{_zn_d}さんへ。」または「今の{_zm_d}{_zn_d}さんへ。」
   【絶対禁止】「今週だけ読んでほしい」「今週の{_zn_d}」は使わない。時事表現は禁止。
2. 今の気の傾向（2〜3行）: 龍脈命術で読む{_zn_d}の現在の特徴。「〜の気が強まっている」「〜のタイミングにある」
3. 吉方位×行動（1点）: 「・○方向 → 具体的にやること（理由）」
4. 引き（1行・言いかけて止める型）: 吉方位2点目やNG注意点の断片を投げて切る
   例: 「ただし、避けてほしい方位がある。」
   例: 「もう一点、動き方に注意があって──」
   例: 「使える方角はもう1つ。ただし条件がある。」
   禁止: 「続けます。」「次に書きます。」「次で残りを話します。」

**2投稿目（190〜230字）— 吉方位2点目×NG×保存CTA**
目的: 情報を使い切ってもらい保存と絵文字コメントを促す
1. 吉方位×行動（1点）: 「・○方向 → 具体的にやること（理由）」（1投稿目と違う方位）
2. NG・注意点（1点）: 「・避けたい方位・行動 → なぜか（龍脈的理由）」
3. 締め（2行）: 「保存して今週の参考にして。」＋「{_zn_d}さん、今週気になること ✨ をコメントで。」

### 書き方ルール
- 冒頭1行目を必ず「{_zm_d}{_zn_d}さん、〜」で始める
- 「吉方位×行動」は抽象的NG。「東向きの席を選ぶ」「南口から出てみる」レベルの具体性
- 2投稿合計で元の400〜480字を維持し、情報密度は落とさない
- hoshi_betsuとの差別化: 12星座まとめではなく{_zn_d}だけを深掘り
- zodiac_targetとの差別化: zodiac_targetは100〜200字単発、zodiac_deepは2投稿で深掘り
"""
    elif pattern_id == "kaiun_day":
        from core.kaiun_calendar import get_moon_phase
        _moon = get_moon_phase(
            __import__('datetime').date.fromisoformat(est_date_str) if est_date_str else None
        )
        _moon_ref = f"{_moon['phase_emoji']} {_moon['phase_name']}（{_moon['energy']}）"

        import random as _rnd_kaiun
        _kaiun_topics = [
            ("一粒万倍日",
             "「一粒が万倍になって返る」という名前の通り、種まきに最適な日。新しいことを始めるエネルギーが高まる。"
             "龍脈命術では「気の流れが拡張方向に傾く日」と見る。"),
            ("天赦日",
             "暦の中で最も格が高い吉日。すべての障りが解消されやすく、何かを決断・スタートするのに向いている。"
             "龍脈命術では「気の抵抗がもっとも少ない日」と解釈する。"),
            ("大安",
             "六曜の最吉日。「大いに安し」の意味で、物事を整え・決める日に向いている。"
             "龍脈命術では方位と組み合わせると効果が上がると見る。"),
            ("寅の日",
             "寅は「出て必ず帰る」動物。旅や金運、財布の新調に縁があるとされる日。"
             "龍脈命術では金気が活性化するタイミングと見る。"),
            ("満月",
             "月のエネルギーが頂点に達するタイミング。感謝・完了・手放しのアクションに向く。"
             "龍脈命術では「気が外に放出されやすい時期」と見るため、お礼参りや整理に最適。"),
            ("新月",
             "月が消えてゼロに戻るタイミング。気のリセットと新しい始まりのエネルギーが高まる。"
             "龍脈命術では「気の受け皿が空になる時期」と見るため、願いを設定するのに向いている。"),
            ("上弦の月",
             "新月から満月へ向かう途中。行動・実行のエネルギーが高まる時期。"
             "龍脈命術では「気が内に向かって充填される時期」と見る。動き出しに向いている。"),
            ("下弦の月",
             "満月から新月へ向かう途中。手放し・整理・棚卸しに向く時期。"
             "龍脈命術では「気が外に流れる時期」と見るため、断捨離や関係の整理に適している。"),
        ]
        _topic_name, _topic_detail = _rnd_kaiun.choice(_kaiun_topics)

        pattern_extra = f"""
## 月のリズム・開運知識型の追加ルール

【月相参考】{_moon_ref}

### 今回のテーマ: 「{_topic_name}」
{_topic_detail}

### 絶対禁止（最重要）
⛔ 「今日は{_topic_name}！」「明日が天赦日です」「今月の大安は〜日」など
   **特定の日付・暦日を主張する表現はすべて禁止**
   → キューに溜まると投稿日と内容がズレて嘘になるため

✅ 代わりに「こういう日が来たら〜するといい」という普遍的な知識として書く

### 構成（150〜220字）
1. **フック（1行）**: 「知ってた？」「意外と知らない人が多い」などで興味を引く
2. **知識の核心（2〜3行）**: 「{_topic_name}とは何か」「なぜそう言われるのか」を龍脈命術の視点で
3. **活用法（1〜2行）**: 「こういう日が来たらこれをやって」「日常でどう使うか」
4. **締め（1行）**: 保存・コメント促進 or 「あなたは意識してる？」系の問いかけ

### スタイル
- 難しい暦の概念を「ふつうの言葉」で伝える
- 「龍脈命術では〜という理由がある」という独自視点を必ず加える
- 情報で終わらず、読者が実際に使える形で締める
"""
    elif pattern_id == "hoshi_betsu":
        # 公開時間帯による「今日 or 明日」の使い分け（日付は相対表現のみ、stale防止）
        _hb_is_evening = est_dt and est_dt.hour >= 20
        _hb_action_word = "明日" if _hb_is_evening else "今日"
        _hb_timing_note = (
            f"この投稿は夜（{est_dt.hour}時）に公開される。"
            f"読者は「今夜保存 → 明日使う」というフローになるため、"
            f"**行動日 = 明日** として書くこと。"
            f"冒頭フックも「今夜のうちに保存して、明日動くときに使って」系にする。"
        ) if _hb_is_evening else (
            f"この投稿は朝〜昼（{est_dt.hour if est_dt else '?'}時）に公開される。"
            f"**行動日 = 今日** として書くこと。"
        ) if est_dt else ""
        _hb_desc_mode = "long"  # short modeは低パフォーマンス（avg 13.0 vs long 19.1）のため廃止

        pattern_extra = f"""
## 星座別情報型の追加ルール

### 【重要】行動日の指定
{_hb_timing_note}
行動日: **{_hb_action_word}**

⚠️ **日付の括弧書き禁止**: 「今日（6/9）」「明日（6月10日）」のように具体的な日付を括弧で入れないこと。
   「今日」「明日」「今週末」等の相対表現のみを使う（投稿がキューに溜まると日付がズレるため）。

### 【絶対最優先ルール】12星座すべてを完結させること
⚠️ **♈から♓まで12星座すべてを必ず書き切ること。途中で止めることは絶対禁止。**
文字数の調整は「各星座の記述を短くする」ことで行う。星座リストを省略することで調整するのは禁止。

### 必須ルール
- **冒頭スクロール停止フック（1行）**: 「♈ 牡羊座 →」で絶対に始めない。
  必ずフック1行 → 空白行 → 星座一覧 の順で書く。

  実績TOP(n=27件)のフック分析:
  - 「【6月1日〜の星座別 運気の波】」→ eng174（1位）: 時期指定+全星座吉方位型
  - 「寝る向きひとつで、気の流れは変わる。」→ eng39: 一つのアクション型
  - 「朝の5分で、気の流れは変わる。」→ eng30: モーニングルーティン型
  → 強いフック: 「時期指定×吉方位」「一つの具体アクション×12星座」
  → 弱いフック(eng=3): 「鏡の置き場所」「哲学・信念系」「「動ける占いの意味」」

  禁止テーマ（実績でeng=3の底値）:
  - 哲学・信念系（「動ける占いとは」「届けたいのは次の一歩」）
  - 概念説明系（「龍脈命術の意味」「吉方位の誤解」だけで終わるもの）
  → これらはworld_view/shigure_voiceに任せる

- テーマを毎投稿で必ず変える（連続使用しない）:
  今週の吉方位×行動 / 寝る向き / モーニングアクション / 金運整え / 神社参拝方位 / 玄関整え / 今月のターニングポイント

- 各星座に必ず「具体的なアクション」を1つ入れる:
  NG: 「♈牡羊座 → 東方向が吉」（情報だけ）
  OK: 「♈牡羊座 → 東／動きたい衝動が来たら即行動していい週」（行動許可）
  OK: 「♉牡牛座 → 南西／財布の中を今日整えて」（アクション付き）

### 各星座の記述量（今回のモード）
{"**ショートモード**: 各星座は「方位 + 5〜10字のひと言」のみ（1行・完結）。12星座すべて完結後の合計が300字以下になるよう各行を調整すること。" if _hb_desc_mode == "short" else "**ロングモード**: 各星座は「方位 + 具体的なアクション・理由（15〜25字）」をセット（1行で収める）。12星座すべて完結後の合計が480字以下になるよう調整すること。"}

### フォーマット（必須）
12星座を4つずつ3グループに分け、グループ間に空白行を1行入れること。
各星座: 1行で完結させること（複数行禁止 ← これが途中切れの原因になる）

### 末尾（必須）
- 「自分の星座だけでもいいから保存して、{_hb_action_word}試してみて。」
- さらに: 「当てはまった星座、🌟 をコメントに置いてってほしい」を添える

### 心理効果
- **スポットライト効果**: 読者は「自分の星座」だけを探す。その瞬間「自分だけへの情報」と感じる
- **バーナム効果**: 各星座の記述は「その星座らしさ」を保ちながら普遍的な内容にする
- **保存行動誘発**: 全星座一覧は「後で見返したい」コンテンツ
"""
    elif pattern_id == "caligula":
        zodiac = extra_hint.get("zodiac", "")
        zodiac_name = zodiac.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓").strip() if zodiac else ""
        zodiac_mark = zodiac[0] if zodiac and not zodiac[0].isalpha() and not '぀' <= zodiac[0] <= '鿿' else ""
        # 星座は必ず割り当てられる（writer.run側で _pick_zodiac() を毎回呼ぶ）
        _zodiac_required = (
            f"⚠️ 【星座必須】冒頭1行目に必ず「{zodiac_mark} {zodiac_name}さん〜」または「{zodiac_mark}{zodiac_name}で〜」の形で星座マークを入れること。"
            f"星座指定なしの汎用型はエンゲージ6.4（星座型104.8の1/16）なので絶対禁止。"
        ) if zodiac else "⚠️ 星座が未指定だが、できるだけ星座を推定して冒頭に入れること"

        import random as _rnd_cali
        # deny=3/5確率: 実績データで否定制限型が上位を独占
        _cali_hook_type = _rnd_cali.choice(["deny","deny","deny","state","skeptic"])
        _hook_guide = {
            "deny":    f"否定制限型（最強）: 「{zodiac_mark}{zodiac_name}で〜な人以外は、読まなくていい。」「{zodiac_mark}{zodiac_name}さん、〜な人はこの先読まないほうがいい。」\n  感情・状態を具体的に指定する（「お金の不安がない人」「このままでいいのかが頭にある人」「運気の波が気になり始めた人」）",
            "state":   f"状態指名型: 「{zodiac_mark}{zodiac_name}で、〜という感覚がある人だけ読んで。」「{zodiac_mark}{zodiac_name}さん、今〜な人のための話をする。」\n  「感覚」「状態」「悩み」を読者が今まさに感じているレベルで具体化する",
            "skeptic": f"懐疑層向け（新規獲得型）: 「{zodiac_mark}{zodiac_name}さん、占いに興味ない人にこそ一度だけ聞いてほしいことがある。」\n  「信じなくていい」「試したくなければそれでいい」という姿勢で書く",
        }[_cali_hook_type]

        pattern_extra = f"""
## カリギュラ型の必須構造

{_zodiac_required}

### 今回のhook型: {_cali_hook_type}
{_hook_guide}

### 実績TOP投稿と弱い投稿の比較（n=18件 直近30日更新）

★ 最強パターン「時期感 × 状態 × 否定制限」の組み合わせが爆発する:
- 「♎天秤座さん、来週についてまだ何も決めていないなら読まないほうがいい。」→ v=2138
  → 「近未来（来週）× 決断未完の状態 × 読まなくていい」= 3要素が揃った最強形
- 「♏蠍座さん、今お金の不安がない人はこの先読まなくていい。」→ v=672
  → 「現在（今）× 感情状態（安心/不安の対比）× 読まなくていい」
- 「♒水瓶座で「運気の波」が気になり始めた人以外は、読まなくていい。」→ v=456
  → 「心理状態（気になり始めた）× 否定制限」

★ deny型の最強テンプレート:
「{zodiac_mark}{zodiac_name}で、【状況の具体描写】な人以外は読まなくていい。」
または
「{zodiac_mark}{zodiac_name}さん、【時期・タイミング】についてまだ【行動未完】なら読まないほうがいい。」

弱い冒頭（views=59〜132）:
- 「♌ 獅子座さんへ。」→ v=59（zodiac_targetの呼びかけ型は弱い・混同禁止）
- 「正直に言う。」→ v=59（冒頭に星座・感情指定なし）
- 「寝室の照明が「真っ白」な人は〜」→ v=59（星座なし・モノ系）
→ 共通した失敗: 星座なし OR 場所/モノ系 OR 呼びかけ型 OR 時期感なし

### 禁止（実績から確認済み）
- 星座マークなしで始める（汎用型 v=59 vs 星座型 avg=454 → 8倍差）
- 「♌ 獅子座さんへ。」のような単純な呼びかけ型（zodiac_targetとの混同）
- 場所系・モノ系hook（「財布に〜」「書斎が〜」「寝室の照明が〜」） ← 星座なしでは効かない
- 「正直に言う。」だけで冒頭にする（星座・感情指定がなければ機能しない）
- 300字超（エンゲージ激減）

### 構成（必須）
1. 冒頭（1行）: {_hook_guide.split(chr(10))[0]}
2. 中盤（3〜4行）: 龍脈命術的な根拠・核心情報。「なぜ今の{zodiac_name}座にこれが起きるのか」を時期感を伴って具体的に
3. 締め（1行）: 「わかる人は ✨ をコメントに」「当てはまった人 🌟 置いてって」など絵文字1文字CTA

### 文字数
250〜290文字（実績: 250〜299字帯 v=454avg > 200〜249字帯 v=132avg > 300字超=壊滅的）
"""
    elif pattern_id in ("menu_guide", "cta_soft"):
        # メニューをプロンプト用にフォーマット
        raw_menu = knowledge.get("profile", {}).get("menu", [])
        if isinstance(raw_menu, list):
            menu_lines = "\n".join(
                f"▷ {m['name']}　{m['price']:,}円\n　{m['description']}"
                for m in raw_menu
            )
        else:
            menu_lines = "（メニューデータなし）"

        if pattern_id == "menu_guide":
            import random as _rnd_menu
            _menu_format = _rnd_menu.choice(["menu", "line"])
            _line_url_menu = knowledge.get("profile", {}).get("line_url", "https://lin.ee/TJg5dru")
            if _menu_format == "menu":
                pattern_extra = f"""
## メニュー案内型ツリー（2投稿・Aフォーマット: 価値提供→メニュー）

### 重要：出力形式
以下の形式で2つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目"]

【リンク配置ルール】1投稿目にURLを含めない。URLは2投稿目のみ。

**1投稿目（140〜200字・URLなし）**
- 「こんな方に使ってほしい」という共感フックで始める
- 読者が「これは自分のためだ」と感じる状況・悩みを1〜2行で描写する
- 「龍脈命術では〜」という価値の匂わせを1行
- 引き（言いかけて止める型）: 「その方法を、今夜だけ書く。」「気になる方に向けて。」

**2投稿目（150〜220字・URLあり）**
- 必ず以下の正確なメニュー名・価格を使うこと（変更・省略・追加禁止）:
【鑑定メニュー】
{menu_lines}
- メニューは読者の状況に合わせて2〜3件に絞る
- 「詳細・お申し込みは {service_url}」で締める

### 禁止
- 1投稿目にURLを含める / 全メニューを列挙する / 「今すぐ」「急いで」などの強制表現
"""
            else:
                harm_data = knowledge.get("harm", {})
                _harm_cats = [k for k in harm_data if not k.startswith("_")]
                _harm_cat = _rnd_menu.choice(_harm_cats) if _harm_cats else ""
                _cat_info = harm_data.get(_harm_cat, {})
                _pains = _cat_info.get("pains", [])
                _sample_pain = _rnd_menu.choice(_pains) if _pains else "同じ悩みを繰り返している"
                pattern_extra = f"""
## メニュー案内型ツリー（2投稿・Bフォーマット: LINE誘導）

### 重要：出力形式
以下の形式で2つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目"]

【リンク配置ルール】1投稿目にURLを含めない。URLは2投稿目のみ。
今回の読者の痛みテーマ: 「{_sample_pain}」

**1投稿目（140〜190字・URLなし）**
- 上記テーマから読者の内言「」か「〜な人へ」で始める
- 痛みの共感を1〜2行
- 「まず話してほしいことがある。」という低コスト行動への誘い（URLなし）

**2投稿目（120〜160字・URLあり）**
- 「鑑定より前に、LINEで話してほしい」という文言で始める
- 費用なし・気軽・申込不要の3点を自然に伝える
- 「▷ LINE {_line_url_menu}」で締める

### 禁止
- 1投稿目にURLを含める / 「今すぐ」「急いで」「限定」などの強制表現
"""
        else:  # cta_soft
            pattern_extra = f"""
## ソフト誘導型の追加ルール
- 鑑定ページURL: {service_url}
- 本文で価値ある気づきを1つ提供してから誘導する（いきなり告知しない）
- 鑑定メニューに言及する場合は以下の正確な名称・価格を使うこと:
{menu_lines}
- 最後に「▷ 鑑定ページ {service_url}」を自然に添える
- 押しつけず「気になる方はどうぞ」のスタンスを保つ
- 150〜250文字を目安に

### 心理効果
- **フット・イン・ザ・ドア**: 価値ある情報を先に与えて「役に立つ人だ」という印象を作ってから誘導する
- **損失回避**: 「知らないでいると損をする」という示唆が行動意欲を高める
"""
    elif pattern_id == "follow_cta":
        # follow_cta は削除済み（出現した場合のフォールバック）
        pattern_extra = ""
    elif pattern_id == "free_reading":
        pattern_extra = f"""
## 無料鑑定誘導型の追加ルール
- 鑑定ページURL（無料メニューあり）: {service_url}
- 構成: ① 読者の「気になるけど一歩踏み出せない」心理に共感する冒頭 → ② 無料メニューの存在と内容（生年月日だけで分かること）→ ③ 低コスト行動への誘導
- 「まずは無料から」「感触だけでも」「試してみて」など心理的ハードルを下げる言葉を使う
- 有料の押しつけは一切しない。「気になったら」スタンスを保つ
- 最後は「▷ 鑑定ページ（無料メニューあり）{service_url}」の形で添える
- 150〜250文字程度。1行目は「〜な人へ」「〜のままにしていない？」など共感型フックで始める

### 心理効果
- **フット・イン・ザ・ドア**: 「無料」という最小コストの行動から始め、鑑定体験を通じて有料サービスへ自然につなげる
- **損失回避**: 「無料なのにやらないのは損」という感覚が背中を押す
- **決断の単純化**: 「生年月日だけでOK」という具体的な簡単さが行動ハードルを下げる
"""
    elif pattern_id == "persona_episode":
        # persona_episode は harm_cta の D パターンに統合済み。フォールバック。
        pattern_extra = ""

    elif pattern_id in ("emoji_comment", "type_quiz"):
        import random as _rnd_ec
        # type_quizはemoji_commentの3〜4択submode（パターン統合）
        # type_quizが指定された場合は必ず multi モード
        if pattern_id == "type_quiz":
            _ec_mode = "multi"
        else:
            # emoji_comment: 2択(binary)を高確率で選ぶ（実績R=7の最強フォーマット）
            _ec_mode = _rnd_ec.choice(["binary", "binary", "binary", "multi"])

        if _ec_mode == "binary":
            pattern_extra = """
## 絵文字コメント誘導型の追加ルール（2択モード）

### 設計原則
コメントへの障壁を限りなくゼロにする。絵文字1文字を置くだけでいい、という設計。

### 【最強公式】実績R=7の投稿はこれ一択
```
今、[感情A]の気持ちと、[感情B]の気持ち、どっちに近い？

[感情A・体感の描写] → [絵文字]
[感情B・体感の描写] → [絵文字]
```
実績最高例: 「今、動きたい気持ちと、なんか重い気持ち、どっちに近い？\n流れが来てる気がする → ✨\n梅雨みたいに気が重い → 🌧️」→ R=7（他は全てR=0）

**なぜこれが強いか**: 「今この瞬間の自分の感情・体感」を問うから。
NG例（R=0）: 「動く方位がある / 何から始めればいいか分からない」→ 行動選択肢は効果低い。

### 構成（60〜90字）
1. **問いかけ（1行）**: 「今、〇〇の気持ちと、〇〇の気持ち、どっちに近い？」— 必ず「今」を先頭に
2. **選択肢（2択）**: 各選択肢を「感情・体感の描写 → 絵文字」の形式で（行動ではなく感覚）
3. **余韻の締め（1行・省略可）**: 「どっちかな。」など穏やかな問いかけ

### 末尾の書き方（重要）
- OK: 「どっちかな。」「あなたはどっち？」「どちらに近い？」
- OK: 選択肢で終わらせて何も添えない（余白が自然にコメントを引き出す）
- NG: 「コメントして」「絵文字を置いて」などの直接的な行動指示

### 2択フォーマット（必須）
選択肢が3つ以上になるとコメント率が落ちる。必ず2択にする。

### 選択肢のテーマ例（毎回違うテーマを使うこと）
- 気の状態: 「整ってきた → 🔮」vs「詰まってる → 🌀」
- 行動意欲: 「動きたい → ✨」vs「止まってる → 🌙」
- 悩みの種類: 「仕事・キャリア → 🔥」vs「恋愛・人間関係 → 💧」
- 転機の感覚: 「転機が近い気がする → ⭐」vs「まだ底にいる気がする → 🌿」
- 季節×気の流れ: 「流れが変わってきた → 🌱」vs「なんか重い → 🌧️」
- 朝の調子: 「なんか今日いい感じ → ☀️」vs「エネルギー切れ → 💤」
- 決断のタイミング: 「もう決めた → 🔥」vs「もう少し待ちたい → 🌙」
- 今の住まい×気: 「今の場所が合ってる → 🌿」vs「なんかここじゃない感 → 🌀」
- 恋愛運の感覚: 「縁が来そうな予感 → 🌸」vs「縁が遠い感じ → 🌧️」
- 金運×行動: 「動いてお金を動かしたい → ✨」vs「まず整えてから → 🌿」

### 絵文字の選び方
絵文字は「その状態・感情を視覚的に翻訳したもの」を選ぶ。
2つの選択肢の絵文字は対になっていること（✨vs🌿、🔮vs🌀など）。
NG: 🔮 vs 🐉（どちらもブランドシンボルで対になっていない）

### 禁止
- 3択以上（コメント率が下がる）
- 鑑定URL・「コメントして」などの直接的な行動指示
"""
        else:  # multi モード（3〜4択・タイプ診断型）
            pattern_extra = """
## 絵文字コメント誘導型の追加ルール（タイプ診断モード・3〜4択）

### 設計原則
読者が自分のタイプを選ぶだけで参加できる自己診断型。
3〜4択で「自分はどのカテゴリか」を確認させることで自己分類欲求を刺激する。

### 構成（50〜100字）
1. **問いかけ（1行）**: 「今の自分、どのタイプ？」「あなたはどっちタイプ？」
2. **選択肢（3〜4択）**: 「A: 状況・感情 → 絵文字」の形式
3. **余韻の締め（1行）**: 「どれかな。」「どれに近い？」（省略可）

### テーマのバリエーション（毎回違うテーマを使う）
- 今の運気×行動パターン: A動く→🔥 / B整える→🌿 / C待つ→🌙 / D迷中→🌀
- 気の詰まり方: A仕事詰まり→📦 / B恋愛詰まり→💧 / C全部詰まり→🌀 / D意外と動いてる→✨
- 転機への姿勢: A絶対変える→🔥 / Bゆっくり→🌿 / Cサインを待つ→⭐ / Dまだ準備中→🌙
- 龍脈タイプ: A北の気（冷静判断）→🌙 / B南の気（直感行動）→🔥 / C東（新しい流れ）→✨ / D西（実りの時）→🌟
- 最近の自分: A上向き感→✨ / B変化の予感→⭐ / C停滞中→🌿 / D底打ち感→🌀

### 末尾の書き方
- OK: 「どれかな。」「どれに近い？」「あなたはどれ？」
- OK: 選択肢だけで終わらせて余白を作る
- NG: 「コメントして」などの直接的な行動指示

### 禁止
- 5択以上・鑑定URL・長い説明
"""

    elif pattern_id == "zodiac_rank_cta":
        import random as _rnd_zrc
        # カリギュラフックのバリエーション
        _zrc_hooks = [
            "今夜0:00までに、自分の星座を確認して。",
            "知りたくない人は読まないで。今夜の星座別 気の序列。",
            "正直に言う。今週、気の流れが止まっている星座がある。",
            "これ、明日になると意味がなくなる情報。今夜だけ。",
            "読んだ後に後悔するかもしれないけど、教えておく。",
            "今夜、気が動く星座と動かない星座が分かれている。",
            "0:00までに読んだほうがいい理由がある。",
        ]
        _zrc_hook = _rnd_zrc.choice(_zrc_hooks)

        # ティアのラベルバリエーション
        _zrc_tier_sets = [
            ("✨ 気が一番動く層", "⭐ 整いはじめている層", "🌙 今夜は静かに蓄える層"),
            ("🔥 今週ここが旬", "🌿 流れが整う", "🌀 内側を深める時期"),
            ("✨ 動くと変わる", "⭐ 準備が整う", "🌙 静観が吉"),
            ("🔮 気の流れが来ている", "⭐ 流れの途中", "🌿 充電期間"),
        ]
        _t1, _t2, _t3 = _rnd_zrc.choice(_zrc_tier_sets)

        # CTA バリエーション（神秘的予言型・時限儀式）
        _zrc_ctas = [
            "今夜0:00までに🌟を置いた人だけ、流れが変わる。",
            "今夜0:00までに🌟を置いた人だけ、何かが動く。",
            "23:59までに🌟を置いた人に、明日から流れが来る。",
            "この投稿を見た縁がある。0:00までに🌟を置いた人の流れが、今夜から動き始める。",
            "自分の星座を見つけた人は、0:00までに🌟を置いて。その人だけ、転機の予兆がある。",
            "今夜の境界線は0:00。🌟を置いた人の中から、気の流れが変わる人が出る。",
            "コメントに🌟を置いた人だけ、今夜気の窓が開く。0:00まで。",
            "見た縁を信じるなら、🌟を今夜0:00までに。置いた人だけ、光が入る隙間ができる。",
        ]
        _zrc_cta = _rnd_zrc.choice(_zrc_ctas)

        # タイトルバリエーション
        _zrc_titles = [
            "【今夜 気の流れ 星座ランキング】",
            "【今週の星座別 気の序列】",
            "【龍脈命術でみる 今夜の星座ティア】",
            "【今夜の星座別 気の勢いランキング】",
        ]
        _zrc_title = _rnd_zrc.choice(_zrc_titles)

        pattern_extra = f"""
## 星座ティアランキング＋時限CTA型の追加ルール

### 冒頭フック（使用するもの・そのまま使う）
「{_zrc_hook}」

### タイトル行（使用するもの）
「{_zrc_title}」

### ティアラベル（この3段階を使う）
- 1位層: 「{_t1}」
- 2位層: 「{_t2}」
- 3位層: 「{_t3}」

### 末尾CTA（そのまま使う）
「{_zrc_cta}」

### 必須フォーマット（この順で書く）
1. **冒頭フック（1行）**: 上記フックをそのまま
2. **タイトル（1行）**: 上記タイトルをそのまま
3. **空行**
4. **ティア1（2行）**: ラベル行 → 4星座を「♈ 牡羊座 / ♌ 獅子座 / ♐ 射手座 / ♎ 天秤座」の形式で1行
5. **空行**
6. **ティア2（2行）**: 同形式
7. **空行**
8. **ティア3（2行）**: 同形式
9. **空行**
10. **末尾CTA（1行）**: 上記CTAをそのまま

### 星座配分ルール
- 12星座すべてをティア1〜3に4つずつ振り分ける
- 今週・今夜の龍脈命術的な気の流れに合わせて配分すること（テーマ・季節を参考に）
- 同じ星座が毎回同じティアに入らないよう、龍脈的根拠を持って変化させる

### 心理設計
- **カリギュラ効果**: 冒頭フックで「読まずにはいられない」衝動を生む
- **スポットライト効果**: 自分の星座がどの層か即座に探したくなる
- **時限希少性**: 「0:00まで」「今夜だけ」が損失回避の動機を強化する
- **絵文字CTA（最重要）**: ✨1文字を置くだけ = emoji_comment型の最小行動原理。コメント率が大幅に上がる
- **FOMO**: 「✨ を集めてみる」という表現が読者を参加型にする

### 禁止
- hoshi_betsu のように全星座を縦並びフラット一覧にする（ティア構造が崩れる）
- ティアごとの星座数が4以外（必ず4/4/4）
- 末尾CTAを省略・変更する
- 「保存してね」で締める（このパターンはコメントCTAで締める）
"""

    elif pattern_id == "ritual_window":
        import random as _rnd_rw
        _rw_windows = [
            ("22:00〜23:59", "今夜0:00"),
            ("21:00〜23:59", "今夜0:00"),
            ("23:00〜0:00", "日付が変わる前"),
            ("20:00〜22:00", "今夜22:00"),
        ]
        _rw_window, _rw_deadline = _rnd_rw.choice(_rw_windows)
        _rw_phenomena = [
            "気の窓が開いている",
            "龍脈の流れが集まってくる時間帯",
            "方位の気が最も活性化する",
            "運気の切り替わりが起きやすい夜",
        ]
        _rw_rituals = [
            "窓を少しだけ開けて、外の空気を入れてみて",
            "深呼吸を3回、北か東に向かってやってみて",
            "今夜の自分に一言だけ声をかけてみて",
            "寝る前に財布か手帳を決まった場所に置いてみて",
        ]
        _rw_ctas = [
            f"今夜{_rw_deadline}までに🌟を置いた人だけ、道が拓ける。",
            f"{_rw_deadline}までに🌟を置いた人に、明日から光明が差し込む。",
            f"🌟を置いた人だけ、この窓から気が流れ込む。{_rw_deadline}まで。",
            f"今夜の気の窓に🌟を置いて。{_rw_deadline}までに置いた人だけ、流れが変わる。",
        ]
        _rw_hooks = [
            f"今夜{_rw_window}の間だけ、{_rnd_rw.choice(_rw_phenomena)}。",
            f"今この時間に読んでいる人へ（{_rw_window}限定の話）。",
            f"今夜{_rw_window}だけ開いている気の窓がある。",
            f"これを読んでいる時間（{_rw_window}）に意味がある。",
        ]
        _rw_hook = _rnd_rw.choice(_rw_hooks)
        _rw_ritual = _rnd_rw.choice(_rw_rituals)
        _rw_cta = _rnd_rw.choice(_rw_ctas)
        pattern_extra = f"""
## 気の窓型の追加ルール（時限儀式投稿）

### 今回の設定
- 時間窓: {_rw_window}
- フック: {_rw_hook}
- 今夜の儀式アクション: {_rw_ritual}

### 必須構成（この順番で）
1. **フック（1行）**: {_rw_hook}
2. **現象の説明（2〜3行）**: なぜこの時間帯に気の窓が開くのか、龍脈命術的な根拠を詩的かつ平易に語る
3. **今夜の儀式（1行）**: 「{_rw_ritual}」— 誰でも今すぐできる小さな一つの行動
4. **時限CTA（1行）**: {_rw_cta}

### ルール
- **150〜200文字**
- 時間窓（{_rw_window}）を必ず本文に入れる
- zodiac_rank_ctaとの差別化: 星座ランキングは入れない。純粋な「時間窓×儀式」
- 儀式は外出不要・体を大きく動かす必要がないシンプルなもの
- 締めはCTA1行のみ。説明不要

### 心理効果
- **時限儀式**: 締め切り + 神秘的報酬 = 行動に意味が生まれる
- **参加の証明**: 🌟コメントが「自分はその時間にいた」という証拠になる
- **排他性**: 「置いた人だけ」という限定がコメントを価値化する
"""

    elif pattern_id == "prophecy_serendipity":
        import random as _rnd_ps
        _ps_openings = [
            "今この投稿を見ているあなたへ。",
            "今夜、これを読んでいるあなたに。",
            "偶然ここに来たと思っている人へ。",
            "今このタイミングで見つけた人、聞いて。",
        ]
        _ps_fates = [
            "偶然ではないと思う。",
            "縁があって、ここに来たと思う。",
            "気の流れがここに連れてきた。",
            "このタイミングで読んでいることに、意味がある。",
        ]
        _ps_readings = [
            ("あなたに今、変化の予兆がある", "何かが動き始めようとしているサインだと思う"),
            ("何かを手放す時期に来ている", "執着を外すと、流れが来る。その直前にいる"),
            ("動くべきタイミングが近い", "迷っているなら、その迷いがタイミングのサインかもしれない"),
            ("止まっていたものが、動き始めようとしている", "その準備ができているから、この投稿に来た"),
        ]
        _ps_ctas = [
            "縁を感じた人は、🌟をひとつコメントに。確認しておきたい。",
            "「来た」と思った人は🌟を。あなたとの縁を記録しておきたい。",
            "ここまで読んだ縁を大事にしたい。🌟をひとつ置いていって。",
            "この投稿とあなたの縁を、🌟で教えてもらえると嬉しい。",
        ]
        _ps_opening = _rnd_ps.choice(_ps_openings)
        _ps_fate = _rnd_ps.choice(_ps_fates)
        _ps_reading_title, _ps_reading_body = _rnd_ps.choice(_ps_readings)
        _ps_cta = _rnd_ps.choice(_ps_ctas)
        pattern_extra = f"""
## 縁の予言型の追加ルール（この投稿を見た縁）

### 今回の設定
- 書き出し: {_ps_opening} {_ps_fate}
- 読み: {_ps_reading_title}
- 展開: {_ps_reading_body}

### 必須構成（この順番で）
1. **書き出し（1行）**: {_ps_opening} {_ps_fate}
2. **縁の読み（2〜3行）**: 「このタイミングで見ている人には〜がある」という龍脈的な読みを語る
   - テーマ: {_ps_reading_title}
   - 展開: {_ps_reading_body}
3. **時雨からのメッセージ（1〜2行）**: 「焦らなくていい」「このタイミングを無駄にしないでほしい」など個人に語りかける温かい一言
4. **CTA（1行）**: {_ps_cta}

### ルール
- **140〜190文字**
- 「あなたへ」という2人称で徹底的に個人に語りかける
- 龍脈的根拠を1行だけ添える（「龍脈命術で見ると〜」）
- soft_discoveryとの差別化: 概念説明は入れない。「あなたという個人との縁」に集中する
- 押しつけがましくなく「見てくれたことを喜んでいる」スタンスで

### 心理効果
- **パラソーシャル**: 「あなたに話しかけている」感覚が強い帰属意識を生む
- **バーナム効果**: 「このタイミングで読んでいる人」という設定が自己照合を促す
- **縁の権威**: 占術家が「縁がある」と言うことの重さが行動動機になる
"""

    elif pattern_id == "harm_revelation":
        import random as _rnd_hr
        _hr_hooks = [
            "本来は書くつもりがなかった。有料鑑定でしか話していないことを今夜だけ書く。",
            "鑑定の場でしか言わないことを、一つだけここに書く。",
            "有料鑑定を受けた方に毎回伝えていることを、今夜だけここに出す。",
            "普段は投稿に書かないことにしている。でも今夜は書く。",
        ]
        _hr_topics = [
            ("停滞の本当の原因", "頑張り方じゃなく、向いている方向の問題。その見極め方。"),
            ("鑑定でほぼ全員に言うこと", "動けない理由は意志ではなく環境の気の停滞だということ。"),
            ("運気が下がる前に出るサイン", "鑑定でよく見る、転落の3ヶ月前に現れる小さな変化。"),
            ("転機を見逃す人の共通点", "気の流れが変わる瞬間に気づけない理由と、気づく方法。"),
        ]
        _hr_hook = _rnd_hr.choice(_hr_hooks)
        _hr_topic, _hr_angle = _rnd_hr.choice(_hr_topics)
        _hr_ctas = [
            f"自分の状況が気になる方は鑑定で見ます。\n▷ {service_url}",
            f"続きが気になる方は、鑑定で話しましょう。\n▷ {service_url}",
            f"自分の方位を確かめたい方 → {service_url}",
        ]
        _hr_cta = _rnd_hr.choice(_hr_ctas)
        pattern_extra = f"""
## プレミアム開示型ツリー（3投稿）の追加ルール（有料内容の限定公開）

### 重要：出力形式
以下の形式で3つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目", "3投稿目"]

### 今回の設定
- 開示フック: {_hr_hook}
- トピック: {_hr_topic}
- 展開の角度: {_hr_angle}
- CTA: {_hr_cta}

### 各投稿の構成

**1投稿目（宣言＋テーマ投下）90〜120字**
目的: 「これは普通の投稿じゃない」と伝え、次への期待を作る
- {_hr_hook}
- 「今夜だけ」「一度だけ」の限定感を含める
- テーマ「{_hr_topic}」を宣言するが、答えは出さない
- 引き（必須・言いかけて止める型）: 答えの入口を見せて、そこで切る
  例: 「原因は、気の向きにある。」
  例: 「転機を逃す人には、ある共通した方位の問題がある。」
  例: 「止まっている理由は──方向だ。」
  禁止: 「続けます。」「次に話します。」「次で説明します。」

**2投稿目（核心の開示）130〜180字**
目的: 「確かにこれは有料で話す内容だ」と感じさせる密度
- 「前置きなしに即、核心を書く」スタンスで
- {_hr_angle}
- 「龍脈命術で〇〇人を見てきたが〜」など実績・具体性を含む
- 方位・気・空間の問題として具体的に語る（「どの方向」「どの場所」レベル）
- 締めは余韻を残す: 「これを知ってしまうと、止まっていた理由がわかる。」など

**3投稿目（余韻＋CTA）80〜110字**
目的: 「自分の状況を確かめたい」という行動意欲を生む
- 「知ってしまった以上、気づかなかったふりはできない。」などの余韻1行
- {_hr_cta}

### 禁止事項
- 投稿1・2で答えを出しきらない（引きの緊張感を保つ）
- 「続けます。」「次で話します。」などの内容ヒントのない移行文
- 表面的・抽象的な話（「心が大事」「前向きに」レベルは禁止）

### 心理効果
- 内部情報の解放: 「本来は非公開」という前提が情報の価値を2〜3倍に高める
- ツァイガルニク効果: 3投稿に渡って「未完」が続くことで読了衝動が増す
- 互恵性: 有料で話す内容を無料で出すことへの申し訳なさがCTAクリックを生む
"""

    elif pattern_id == "ichi_gon":
        import random as _rnd_ichigon
        _ichigon_mode = _rnd_ichigon.choice(["standard", "standard", "ultra_short"])

        if _ichigon_mode == "ultra_short":
            pattern_extra = """
## 一言型（超短文・発見特化モード）の追加ルール

### 目的: views の最大化（Threadsアルゴリズムの発見タブへの露出）
実績: 30字投稿で1167 views（全パターン中トップクラス）

### ルール（厳守）
- **20〜40文字のみ**（1〜2行・絶対超えない）
- 末尾の問いかけ・CTAは一切不要。余白こそが力。
- 説明・理由・補足を一切入れない。1つの断言だけで完結させる。

### 高パフォーマンスの構造
「[場所/もの] は、[本質的な定義]。\n[短い結論]。」

高実績例:
- 「寝室は、運気の充電器。\n枕の向きひとつで、朝の流れが変わる。」（30字・1167 views）
- 「今週の運気、星座じゃなくて「流れ」で見てほしい。\n向かい風の週こそ、方位を整えるだけで景色が変わるよ。」（52字・545 views）

### テーマ（毎回変えること）
- 場所・空間の気（寝室・玄関・窓・方位）
- 時間・タイミング（今週・転機・波）
- 感覚・直感（違和感・止まった感覚）
- 縁・人間関係

### 心理効果
- **完結効果**: 短文ほどスクロールなしに完読される → アルゴリズム的に「完読率」が高い
- **余白効果**: 短いほど読者の解釈空間が広がり「自分への言葉」になる
"""
        else:  # standard mode
            pattern_extra = """
## 一言型（標準モード）の追加ルール
- 1〜3行の断言型短文（60〜100文字）
- pop_shortと異なり、**占術的・抽象的な断言・名言風**にする（具体的な生活ヒントではない）
- 1行目だけで「何かが変わる気がする」と思わせる言葉の密度にする
- 説明しすぎない。余白が読者の想像力を引き出す
- **必ず最後の1行**に「うなずいた人、いる？」「わかる？」「これ、あなたのこと？」など自然な問いかけを添える（命令形NG）

### テーマを毎回ローテーションすること（同じテーマを連続使用しない）
- 停滞・空回り（「動いているのに前に進まない感覚」）
- 転機・動き出し（「変わる前の静けさ」「動けるタイミング」）
- 方位・場所の気（「いる場所が変わると、自分も変わる」）
- 判断・タイミング（「今じゃない感覚は当たっていることが多い」）
- 恋愛・縁（「縁は気の流れで変わる」）
- 仕事・キャリア（「停滞を実力のせいにしない」）
- 変化の前兆（「違和感はサインだ」）

### 心理効果
- **アンカリング**: 断言口調（「〜だ」「〜に動く」）が「真実」として脳に刻まれやすい
- **余白効果**: 短いほど読者が自分なりの解釈で補完し「自分に向けられた言葉」に変換される
- **完結効果**: 1〜3行で完結した文は記憶に残りやすく、スクショ・保存されやすい
"""
    elif pattern_id == "list":
        import random as _rnd_list
        _list_mode = _rnd_list.choice(["list", "list", "ranking"])  # list 2/3, ranking 1/3
        if _list_mode == "list":
            pattern_extra = """
## リスト型の追加ルール（Aフォーマット）

### 文字数ルール（最重要）
実績データ（n=23件）:
- 400〜500字: avg eng=68.0 ← 唯一の高パフォーマンス帯
- 200〜299字: avg eng=3.0 ← 最悪帯。絶対に避ける
- 300〜399字: avg eng=5.7 ← 低い
→ 目標: 400字以上。各項目の補足を充実させて必ず400字を超えること。「短くまとめよう」は禁止。

### 必須ルール
- タイトル行に数字を入れる: 「〇〇な人の特徴 3つ」「やってはいけない 3選」
- 3〜4項目に絞る（4項目で400字を目標）
- 末尾に「保存してね」+ コメント誘発

### 構成
1. タイトル行（1行）: 数字入りで「〜な人の3つの特徴」「〜するだけで変わる3選」
2. 各項目: 「・タイトル」1行 ＋「→ 理由・補足（40〜60字）」1〜2行、項目間は空白行
3. 締め（2行）: 「保存しておいてね」＋「いくつ当てはまった？」

### NG→改善例の活用
項目を「NG例」と「改善後」のセットで構成すると行動変容に直結する。
例: 「・財布を床に置く → 気はいつも低いところに滞る。財布の定位置を作るだけで、お金の流れが変わってくる。バッグの内ポケットか棚の上が吉。」← 理由と具体策両方を入れて40〜60字
"""
        else:
            pattern_extra = """
## リスト型の追加ルール（Bフォーマット: ランキング型）

### 必須ルール
- 3項目固定（「TOP3」「ワースト3」のみ）— 5項目以上は読み飛ばされる
- 冒頭はカリギュラフックで始める
- 300字以上（200字以内は低パフォーマンス帯のため禁止）

### 構成
1. カリギュラフック（1行）: 「知ると今すぐ変えたくなるので、覚悟して」「〇〇している人には早すぎる情報かもしれない」
2. ランキングタイトル（1行）: 「〇〇 ワースト3」「〜している人がやっていること TOP3」
3. 3項目: 各項目は「順位＋タイトル」1行目 ＋「→ 理由（40〜60字）」1〜2行、空白行で区切る
4. 締め（1行）: 「何位まで当てはまった？」
"""
    elif pattern_id == "shigure_voice":
        import random as _rnd_sv
        _sv_mode = _rnd_sv.choice(["discovery", "discovery", "confession", "question"])
        _sv_guide = {
            "discovery":  "「1000人以上鑑定してわかったこと」「最近よく思うこと」から始まる発見語り型。変わった人と変わらなかった人の違い・方位を整えた時に起きること・鑑定で一番多く見てきたパターンなど、時雨の目から見た真実を語る。具体的な観察ベース。数字（「1000人以上」「何百人」）を入れてリアリティを出す。",
            "confession": "「正直に言うと」「ずっと言いたかったけど」から始まる本音告白型。占い師として感じていること・信じてもらえない時の本音・龍脈命術に出会った経緯・懐疑的な人に向けて伝えたいこと。時雨のキャラクターへの信頼感を作る。弱さや迷いも正直に書いていい。",
            "question":   "「一つ聞いてもいい？」「ちょっと考えてほしいことがある」から始まる静かな問いかけ型。読者の当たり前・固定観念を3〜5行で揺さぶる問いを立てる。答えは提示しない。読者が自分で考えたくなる設計。",
        }[_sv_mode]
        pattern_extra = f"""
## 占術家の正直語り型の追加ルール

### 今回のモード: {_sv_mode}
{_sv_guide}

### 共通設計原則
- 「時雨」としての一人称視点を徹底する（「私は〜」「見てきた中で〜」「正直に言うと〜」）
- アドバイスでも教育でもなく「観察と本音の共有」スタンス
- 龍脈命術の名前を必ず入れる
- 押しつけ・強制・脅迫的表現は禁止

### 構成（150〜220字）
1. **個人的な出だし（1〜2行）**: 「正直に言うと。」「1000人以上鑑定してわかったこと。」「一つ聞いてもいい？」
2. **核心（3〜4行）**: 発見・本音・問い。具体的で地に足のついた内容。抽象論は避ける
3. **締め（1行）**: 「これ、しっくりきた？」「当てはまる人いる？」「どっちに近い？コメントで教えて」

### 禁止
- 鑑定URLを入れる（信頼醸成目的なのでCTAは不要。つけるとしてもほのめかす程度）
- 根拠のない断言（「必ず変わる」「100%」）
- 「みなさん」「皆様」などの書き言葉（時雨の語りはカジュアルな一人称）
"""

    elif pattern_id == "faq_myth":
        import random as _rnd_faq
        _faq_topics = [
            ("吉方位って引越しだけ？", "違う。通勤・買い物・散歩など日常の移動でも効く。「大きく動かないと変わらない」が一番の思い込み。"),
            ("占いは信じないと効かない？", "信じる・信じないは関係ない。方位の気の流れは客観的なもの。信じなくても動けば結果は出る。"),
            # 懐疑層向けQ&A（新規層への入口として重要）
            ("科学的根拠はあるの？", "厳密な意味での科学的証明はない。でも「東を向いて仕事したら集中できた」「引越しで縁が変わった」という体験は事実として積み上がる。信じるより試す方が早い。"),
            ("気のせいじゃないの？", "気のせいかもしれない。でも「気のせい」でも動いた人が変わっていく事実は変わらない。信じるかどうかより、試したかどうかが大事。"),
            ("他の占いと何が違うの？（初めて知った人へ）", "星座・タロット・数秘は「あなたの性格・運命」を読む。龍脈命術は「あなたが今いる場所・向かう方向」を読む。自分を変えるより向きを変える占い。"),
            ("吉方位に行ったけど何も変わらなかった", "1回では変わらない。方位は継続的に意識するもの。「行ったのに変わらない」は1回で諦めたケースが多い。"),
            ("龍脈命術って他の占いと何が違う？", "星座・数秘が「性質を読む」のに対し、龍脈命術は「今いる場所の気の流れ」を読む。自分を変えるより向きを変える占い。"),
            ("凶方位に住んでいたらもうダメ？", "ダメじゃない。凶方位に住んでいても、吉方位への日常的な移動で補える。住む場所だけが全てではない。"),
            ("鑑定を受けたほうがいい人は？", "同じ悩みを1年以上繰り返している・大きな決断を控えている人に特に向く。"),
            ("吉方位は毎月変わる？", "変わる。年間を通じた「大吉方位」と月ごとの「月吉方位」の2層がある。月で変わるのは月命星の動き。"),
            ("占いって当てにしていいの？", "「当てる」占いじゃなく「方向を示す」占い。当たる・外れるより、動いた後に変わったかどうかが大事。"),
            ("悪い鑑定結果が出たら怖い", "龍脈命術に「もうダメ」という結果は出ない。悪い流れも「今は整える時期」と読む。判断材料として使うもの。"),
        ]
        _faq = _rnd_faq.choice(_faq_topics)
        _q, _hint = _faq
        pattern_extra = f"""
## FAQ・誤解解消型の追加ルール

### 今回のFAQテーマ
Q: 「{_q}」
方向性のヒント: {_hint}

### フォーマット（2つのアプローチから選ぶ）
**Aアプローチ（直接Q&A型）**:
- 「よく聞かれること。」で始め、Qを1行で提示
- 「違います。」「答えは〇〇。」で直接回答
- 龍脈命術的な理由を2〜3行で説明
- 「試してみて」か「これだけ知っておいて」で締め

**Bアプローチ（誤解→真実型）**:
- 「よくある誤解があって。」で始め、誤解を1行で示す
- 「実際は〜」で真実を提示
- 「この違いを知ってると、行動が変わる」で締め

### ルール
- **130〜200文字**
- 難しい専門用語なし・読者を責めないトーン
- URLは不要（ブランド・教育系）
- 末尾に「これ、知ってた？」「当てはまった人いる？」などコメント誘発を添えてもよい

### 心理効果
- **誤解解消**: 「信じていない」「怖い」という心理的ハードルを下げる
- **権威性**: 誤解に正面から答えることで「知識がある信頼できる占術家」の印象を作る
- **教育マーケティング**: 知識を与えることでフォロー・保存が増える
"""
    elif pattern_id == "empathy_thread":
        pattern_extra = f"""
## ツリー型（3投稿・続きが読みたくなる構成）の追加ルール
- 鑑定ページURL: {service_url}

### 重要：出力形式
以下の形式で3つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目", "3投稿目"]

### 最重要ルール：「引き」は内容ヒット型のみ
各投稿（3投稿目以外）の末尾は「次に何が来るかを匂わせる一文」で締める。

【絶対禁止】以下の表現は使わない。「次で話す」と予告するだけでクリック動機が生まれない：
- 「続けます。」「続きます。」「次に続けます。」
- 「本当の理由を、次に話します。」「次に説明します。」「次で話します。」
- 「これで全部じゃない。」「もっと大事なことがある。」

【必須】引きは「言いかけて止める」型にする。答えの入口まで見せて、そこで切る：
- 「原因は──気の流れだ。」
- 「正体は、方位にある。」
- 「止まっている理由は、向きだ。」
- 「その人たちに全員、共通していたのは──空間だった。」
- 「意志の問題じゃない。これは方位の話だ。」
- 「龍脈命術では、この状態に名前がついている。」

コツ: 「──」「。」で文を完結させ、「なぜ？」「どういうこと？」を読者に思わせる。
「次に〇〇を話します」という予告ではなく、核心の断片を投げつけて終わる。

### 各投稿の構成

**1投稿目（ショックフック）60〜100字**
目的: スクロールを止め、「自分のことだ」と思わせる
- 読者の「言えなかった本音」を「」で引用して始める
- 一般的な「原因」の解釈を否定する（「〇〇のせいじゃない」）
- 末尾は「言いかけて止める」型の引きで締める。答えの断片を投げて切る
  例: 「原因は──方位だ。」「止まっている理由は、向きにある。」「正体は、空間だった。」
- 引き1行で投稿を終わらせ、3投稿目以外は完結させない

**2投稿目（核心の洞察）100〜160字**
目的: 「なるほど、だからか」という納得感を与える
- 「ほとんどの人が〇〇と思っている。違う。」という構造で始めてもよい
- 「龍脈命術で見てきた人に全員共通していたのは〜」など具体性・権威性を添える
- 方位・気の流れの観点から、具体的かつ意外性のある原因を明かす
- 解決策への期待を残す締め方にする（「これがわかると、動き方が変わる。」など）

**3投稿目（希望とCTA）80〜120字**
目的: 「自分にもできる」という確信とともに行動させる
- 「〇〇がわかれば、動ける」という希望の断言
- 「それだけで変わる」という簡単さの提示
- 「▷ まず確かめてみて {service_url}」で締める

### 心理設計
- ツァイガルニク効果: 未完情報は完結情報より記憶に残る。各投稿を「未完」で終わらせる
- 帰属の外在化: 「意志の問題じゃない、方位の問題」で自己否定から解放する
- 社会的証明: 「何人も見てきた」「全員に共通点がある」で信頼を積む
"""

    elif pattern_id == "harm_cta":
        import random as _rnd_harm
        harm_data = knowledge.get("harm", {})
        cta_action = f"▷ まず無料鑑定から {service_url}"

        # カテゴリ選択
        harm_cat = (extra_hint or {}).get("harm_category")
        harm_keys = [k for k in harm_data if not k.startswith("_")]
        if not harm_cat or harm_cat not in harm_data:
            harm_cat = _rnd_harm.choice(harm_keys) if harm_keys else ""
        cat = harm_data.get(harm_cat, {})
        cat_label = cat.get("label", harm_cat)
        pains = cat.get("pains", [])
        sample_pains = _rnd_harm.sample(pains, min(3, len(pains))) if pains else []
        pains_text = "\n".join(f"  - 「{p}」" for p in sample_pains) if sample_pains else "（ペインデータなし）"

        # ペルソナ取得
        persona_tags = cat.get("persona_tags", [])
        all_personas = knowledge.get("personas", [])
        matched = [p for p in all_personas if any(t in p.get("tags", []) for t in persona_tags)]
        persona = (extra_hint or {}).get("persona")
        if not persona and matched:
            persona = _rnd_harm.choice(matched)
        if persona:
            persona_block = (
                f"年齢・職業: {persona.get('age')}歳・{persona.get('occupation')}\n"
                f"悩み: {persona.get('struggle', '')[:120]}\n"
                f"龍脈的原因: {persona.get('revelation', '')[:120]}\n"
                f"変化後: {persona.get('result', '')[:100]}"
            )
        else:
            persona_block = "（ペルソナなし：実際の鑑定例として自由に作成）"

        # サブパターンをランダム選択: A=HARM法則型 / B=年数刺し型 / C=比較痛み型 / D=ペルソナ実録型
        _sub = _rnd_harm.choice(["A", "B", "C", "D"])
        _years_opts = ["1年以上", "2年近く", "3年以上", "4〜5年", "5年近く", "何年も"]
        _years = _rnd_harm.choice(_years_opts)

        # ── 共通: ツリー出力フォーマット ──
        _tree_header = f"""
### 重要：出力形式
以下の形式で2つの投稿テキストをJSON配列として出力すること：
["1投稿目", "2投稿目"]

【リンク配置ルール】1投稿目にURLを絶対に含めない。URLは2投稿目のみ。
1投稿目はリンクなしで拡散→2投稿目でCTA。これが最大のインプレッション戦略。

**2投稿目（共通・100〜130字）**
1. 希望の断言（1行）: 「向きがわかれば、動ける。」「あなたにも合う方位がある。」など
2. CTA（1〜2行）: {cta_action}

**1投稿目の末尾（引き・言いかけて止める型）**:
内容をほぼ言い切った上で、行動への橋渡しとなる一文で締める
例: 「方位でこれは変わる。」「向きを変えた瞬間に動いた人を何人も見てきた。」
例: 「意志の問題じゃなかった。」「気の流れが変われば──動ける。」
禁止: 「続けます。」「次に話します。」
"""

        if _sub == "A":
            pattern_extra = f"""
## 痛みCTA型ツリー — Aパターン（HARM法則型）
{_tree_header}
### 今回のHARMカテゴリ: {cat_label}
読者が抱えている痛みの例：
{pains_text}

### 参照ペルソナ
{persona_block}

### 1投稿目の構成（180〜230字・URLなし）
1. スクロール停止フック（1行）: 「〜の人へ」「正直に言う。〜」「ぶっちゃけ〜」から選ぶ
2. 痛みの受け止め（1行）: 「意志が弱いんじゃない」「あなたのせいじゃないかもしれない」
3. 龍脈命術的原因（2〜3行）: 方位・気の流れで具体的に。ペルソナの revelation を参考に
4. 引き（1行）: 「向きを変えた瞬間に動いた人を、何人も見てきた。」

### 禁止
- 1投稿目にURLを含める / 根拠のない励まし / 4行超の一段落
"""
        elif _sub == "B":
            pattern_extra = f"""
## 痛みCTA型ツリー — Bパターン（年数刺し型）
{_tree_header}
### 今回の年数指定: {_years}
冒頭は「同じ悩みを{_years}繰り返している人に、正直に言う。」で始めること。

### 参照ペルソナ（{_years}繰り返していた実例として活用）
{persona_block}

### 1投稿目の構成（190〜240字・URLなし）
1. 年数フック（1行）: 「同じ悩みを{_years}繰り返している人に、正直に言う。」
2. 否定（1行）: 「努力が足りないじゃない。運が悪いでもない。」
3. ペルソナ実例を匂わせる（1〜2行）: 悩み・原因・変化を匿名で（名前・個人情報は出さない）
4. 龍脈命術的原因（1〜2行）: 「動いている方向が気の流れと逆だった。」
5. 引き（1行）: 「向きを変えた瞬間に動いた人を、何人も見てきた。」

### 禁止
- 1投稿目にURLを含める / ペルソナの個人情報を特定できる形で記載
"""
        elif _sub == "C":
            pattern_extra = f"""
## 痛みCTA型ツリー — Cパターン（比較痛み型）
{_tree_header}
### 比較シチュエーション（毎回変える）
「仕事・昇進」「恋愛・結婚」「収入・お金」「転職成功」「起業・副業」など

### 今回のHARMカテゴリ参考
{pains_text}

### 1投稿目の構成（180〜230字・URLなし）
1. フック（1行）: 「「なんであの人だけ〇〇なんだろう」」（読者の内言カギカッコ）
2. 受け止め（1〜2行）: 「その感覚を持ったことある人へ。嫉妬の後の自己嫌悪、よくわかる。」
3. 外在化（2〜3行）: 「差は才能じゃなく、気の流れに乗れているかどうか」
4. 引き（1行）: 「あなたにも、合う流れがある。」「方位でこれは変わる。」

### 禁止
- 1投稿目にURLを含める / 嫉妬を煽る / 自己嫌悪を悪化させる表現
"""
        else:  # D: ペルソナ実録型
            _d_emotion_hooks = [
                "最終面接だけ、なぜか通らない。",
                "仕事を変えても、同じ壁にぶつかる。",
                "3年付き合って、また別れた。",
                "頑張っているのに、なぜか止まっている。",
                "引越したのに、状況が変わらなかった。",
            ]
            _d_hook = _rnd_harm.choice(_d_emotion_hooks)
            pattern_extra = f"""
## 痛みCTA型ツリー — Dパターン（ペルソナ実録型）
{_tree_header}
### 今回のHARMカテゴリ: {cat_label}
### 参照ペルソナ
{persona_block}

### 冒頭感情フレーズ（1行目に使う）
{_d_hook}

### 1投稿目の構成（200〜270字・URLなし）
1. 感情フック（1行）: {_d_hook}
2. 鑑定導入（1行）: 「先日、〜の方を鑑定しました」（ペルソナを匿名で紹介）
3. 悩みの描写（1〜2行）: 参照ペルソナの具体的な苦しさ。名前・個人情報は出さない
4. 鑑定の発見（1行）: 「原因は〇〇でした」（龍脈命術的な原因を一言で）
5. 変化（1行）: ペルソナの行動と変化を一文で
6. 引き（1行）: 「〇〇のせいじゃなかった。〇〇だっただけ。」

### 禁止
- 1投稿目にURLを含める / 個人特定できる情報 / 「人生が変わった」「奇跡」などの大げさな言葉
"""

    # ── convert カテゴリ: 末尾URL必須ルール（ツリーパターンは各投稿内で管理するため除外）──
    if (pattern.get("category") in ("convert", "conversion")
            and "https://" not in pattern_extra
            and pattern_id not in THREAD_PATTERNS):
        _line_url = knowledge.get("profile", {}).get("line_url", "https://lin.ee/TJg5dru")
        pattern_extra += f"""
## 【必須】集客系パターンの末尾URL
- 投稿の最後の1行に必ずURLを添えること
- 内容の流れに合わせてどちらかを選ぶ:
  - 鑑定へ誘導する場合: 「▷ 鑑定ページ {service_url}」
  - LINE相談へ誘導する場合: 「▷ LINE {_line_url}」
- 「気になったら」「まずは」などの余裕ある文言で自然に添える（押しつけNG）
"""
    # ─────────────────────────────────────────────────────────────────

    # ── 星座ブースト（if/elif チェーン完了後に適用） ─────────────────
    zodiac_boost = extra_hint.get("zodiac_boost") if extra_hint else None
    if zodiac_boost and pattern_id not in ("zodiac_target", "caligula", "hoshi_betsu", "empathy_thread", "harm_cta"):
        pattern_extra += f"""
## 星座ターゲット（ソフト適用）
このテーマを特に「{zodiac_boost}」の人に刺さるよう書く。
「{zodiac_boost}の方へ」と冒頭に書く必要はない。言葉選び・具体例・感情描写が自然にその星座の特性にフィットするようにする。
"""
    # ─────────────────────────────────────────────────────────────

    # ── D改善: ペルソナ参照ヒント（persona_episode・harm_cta 以外の全パターンに適用）──
    _persona_ref = (extra_hint or {}).get("persona")
    _patterns_own_persona = {"persona_episode", "harm_cta"}
    if _persona_ref and pattern_id not in _patterns_own_persona:
        _pg_ref = "女性" if _persona_ref.get("gender") == "female" else "男性"
        _tried_ref = "、".join(_persona_ref.get("tried", [])[:2])
        pattern_extra += f"""
## ターゲット読者の言葉（参考）
この投稿が最も響くと思われる読者タイプ（参考例）:
- {_persona_ref.get('age')}歳・{_pg_ref}・{_persona_ref.get('occupation')}
- 悩み: {_persona_ref.get('struggle', '')[:120]}
- 試したこと: {_tried_ref}
- 感情: {_persona_ref.get('emotion', '')[:80]}
→ この人が「あ、自分のことだ」と感じる言葉・状況・感情で書くこと。彼ら/彼女が実際に使う言葉・表現を本文に活かす。
"""
    # ────────────────────────────────────────────────────────────────

    _output_as_thread = (extra_hint or {}).get("output_as_thread", False)
    output_instruction = (
        "## 出力形式（必須）\n"
        "2投稿のJSON配列のみ出力すること:\n\n"
        "[\"フック投稿\", \"詳細投稿\"]\n\n"
        "- フック投稿: 30〜70文字。強い一言・問いかけ・宣言。ハッシュタグなし。読んだ瞬間に続きが気になる文\n"
        "- 詳細投稿: 120〜220文字。龍脈命術の視点で解説・体験談・行動指示 + 締め問いかけ + ハッシュタグ2〜3個\n"
        "- JSON配列以外のテキスト（前置き・説明・コードブロック記号）は不要"
    ) if _output_as_thread else (
        "## 出力\n投稿本文のみ。説明・メタ情報・マークダウン記法は不要。プレーンテキストで出力すること。"
    )

    return f"""あなたは占術家「時雨（しぐれ）」として、Threadsに投稿するテキストを1本生成してください。

{post_time_block}
## アカウント情報
{profile_summary}

## 投稿パターン
- パターン: {pattern.get('name')}
- 説明: {pattern.get('description')}
- 例: {pattern.get('example', '')}

## 投稿テーマ
{theme}

## 参考リサーチ
{research_text}

## アナリストフィードバック
{analyst_feedback or '（まだデータなし）'}

## バズった1行目の構造例
{hooks_sample}

## 心に刺さる場面（読者が「これ私だ」と感じる具体的な状況）
投稿に合う場面を1つ選んで、その人の気持ちをリアルに描写すること。

【仕事・キャリア】
- 頑張っているのに3年以上年収・ポジションが変わらない
- 転職したいのに動けないまま1年以上経っている
- 同期・後輩に追い抜かれた日の夜の感覚
- 面接で「惜しい」「もう少し」を繰り返されている
- 努力が正しいはずなのになぜか空回りしている

【恋愛・人間関係】
- 3年以上ひとり、マッチングアプリでも続かない
- 「なんで自分だけ」という嫉妬を感じる自己嫌悪
- 結婚を考えると「この人でいいのか」が頭をよぎる
- 友人が次々と幸せになっていくのを見ている側にいる

【人生全般】
- 「このままでいいのかな」が口癖になって何年も経つ
- やる気はあるのに体が動かない、決断できない
- 毎年「今年こそ変わる」と思ってまた同じ場所にいる
- 安定しているけど何かが足りない漠然とした不満
- 好きなことを仕事にしている人を見て自分と比べてしまう

{rewrite_block}{pattern_extra}
## 読者を動かす投稿の共通原則（全パターン必読）

**① 悩みの解像度を上げる**
「結果が出ない」より「3年間頑張ったのに後輩に抜かれた夜」のように、読者が「あ、自分だ」と感じる具体的な状況で描写する。

**② 原因を一言で言い切る**
「努力が足りないからじゃない、〇〇が原因だった」の形で龍脈命術の言葉（気の滞り・方位のズレ・向きの問題）として端的に示す。

**③ 今すぐ使える解決策を1つ入れる**
「北側の窓を開ける」「靴をそろえる」「枕の向きを変える」など今日からできる具体的な行動を「〜するだけ」の形で1行。難易度を下げることが再現性につながる。

**④ NG例→改善例で見せる（使えるなら）**
「やりがちなNG」と「改善後」を対比で示すと、読者が自分の状況に当てはめやすくなる。
例: NG「玄関に靴を出しっぱなし」→ 改善「帰ったら靴をそろえるだけ」

**⑤ 初心者に届く言葉を使う**
専門用語（龍脈命術・吉方位・気）を使う場合は直後に一言で言い換える。専門用語の連発は避け、高校生でも理解できる言葉を選ぶ。

**⑥ 小さな気づきを1〜2個散りばめる**
「え、そういうことだったの？」と感じさせる逆説・意外性の一文。
例:「動けないのは意志が弱いからじゃない。」「部屋が重い感じ、気のせいじゃない。」「向きを変えるだけで、努力の効き方が変わる。」

**⑦ 再現性を意識する**
「特別なことは何もない」「〜するだけでいい」「まず〜から」の言葉で「自分にもできそう」という感覚を作る。難しそうに見える方位・気の話も、具体的な行動に落とせば誰でも試せる。

**⑧ 常識・当たり前を一行で覆す**
読者が前提にしていることを崩す一文が強いフックになる。
例:「運がいい人は特別じゃない。気の流れを読んでいるだけ。」「頑張り方の問題じゃなく、向きの問題だった。」「努力より先に整えるべきものがある。」

**⑨ 次の小さな一歩を示す**
読んだ後に「じゃあ何をすれば？」を残さない。締めに今日できる一歩を添える。
例:「まず今日、北側の窓を少し開けてみて。」「今夜、枕の向きだけ変えてみよう。」

## 生成ルール
- **文字数（最重要）**: 基本80〜180文字。パターン固有のルールがあればそちら優先。長くなるなら削る。
  - 例外: 星座別一覧(hoshi_betsu)は〜350文字、自己診断・ランキングは〜280文字
  - 300文字を超える投稿は読まれない。常に「削れるか？」を意識する
- 口調は「時雨」として一貫させる
- **1行目は「自分のことだ」と思わせる書き出しにする（最重要）**
  - 読者の内言を「」で引用する（例：「なんか今じゃない気がする」）
  - または読者が今いるシチュエーションを具体的に描写する（例：転職・引越し・恋愛など）
  - または「〇〇な人へ」「〇〇しているあなたへ」と直接呼びかける
  - 豆知識・開運日・世界観投稿でも「情報を伝える」より先に「誰に届けるか」を示す
- 風水・吉方位・龍脈命術の世界観を自然に織り込む
- **【ブランド認知・教育の埋め込み】全投稿で龍脈命術の「何が違うか」を1文忍ばせる**:
  単に「龍脈命術では〜」と名前を出すだけでなく、**龍脈命術ならではの視点**を1行で自然に示すこと。
  目的: 読者が「龍脈命術 = 方位・気の流れで人生を動かす占い」と記憶する。
  埋め込みの型（いずれか1つ、文脈に合うものを選ぶ）:
  - 差別化型: 「星座占いが性格を読むのに対し、龍脈命術は今いる場所・向かう方向を読む」
  - 実践型: 「龍脈命術は『当てる』より『動ける』占い。方位を整えると、同じ行動の結果が変わる」
  - 原理型: 「龍脈命術の根拠は気の流れ。気は方位によって質が違い、向かう方向で人生の流れが変わる」
  - 体験型: 「龍脈命術を受けた人がよく言うのは『占いなのに行動できた』という感想」
  埋め込み禁止パターン（ブランド説明だけの投稿は読まれない）: 龍脈命術の説明だけで終わる。「龍脈命術とは〜です」という定義文から始まる。
  例外: emoji_comment・type_quiz・ichi_gon（短文で文字数制約がある場合）は不要
- **【エンゲージメント最重要】締め問いかけ必須**: CTA・変換系パターン（harm_cta・menu_guide・follow_cta・conversion系）以外は、最後の1行を問いかけで終わらせること
  - 必須例: 「これ、わかる？」「当てはまった？」「心当たりある？」「やってみた？」「どっちかな。」
  - 1語でも十分: 「わかる？」「当てはまる？」「どっち？」
  - 実績: ichi_gon で「これ、わかる？」を添えた投稿 → エンゲージ17.4%、なしだと3〜5%（約4倍差）
  - 例外: emoji_comment・type_quiz（選択肢自体が問いかけになっているため不要）
- **末尾は命令形を使わない**: 「いいねして」「フォローして」「コメントして」は直接的すぎる。問いかけか余韻で自然に終わらせる
  - NG: 「〜してください」「〜して」「いいねして」「コメントして」などの命令形
  - ※ 鑑定CTAのURL添付は別途許可されているパターンのみOK
- 語尾に少しポップさを持たせる（「〜だよ」「〜よね」「〜してみて」なども自然に使う）
- 最後の誘導文（「プロフィールから」等）は週1回のメニュー案内型のみ
- 短文型（一言型・ポップ短文型）は4行以内
- 「占術家 時雨」のシグネチャは世界観投稿・エピソード投稿のみ末尾に追加
- 絵文字は基本0〜1個（ポップ短文型のみ最大2個）
- NGワードは使わない
- 「今日」「明日」「今週」などの時間表現は必ず推定投稿時刻を基準にすること
- 季節・節気（秋分・春分・夏至・冬至など）は必ず上記「実際の季節情報」に合った内容のみ使用すること。NG節気は絶対に使わない
- **【絶対禁止】投稿本文にマークダウン記法を使わない**: `**太字**` `*斜体*` `# 見出し` `- 箇条書き` `` `コード` `` などのマークダウン記号は投稿文に一切含めない。SNS投稿はプレーンテキストで書くこと。箇条書きが必要な場合は「・」を使う。

{output_instruction}"""


# ツリー投稿パターン（format="tree"、複数投稿を連結するパターン）
THREAD_PATTERNS = {"empathy_thread", "harm_revelation", "zodiac_deep", "harm_cta", "menu_guide"}


_FORBIDDEN_PHRASES: dict = {
    "zodiac_deep": [
        "今週だけ読んでほしい",
        "今週の♈", "今週の♉", "今週の♊", "今週の♋",
        "今週の♌", "今週の♍", "今週の♎", "今週の♏",
        "今週の♐", "今週の♑", "今週の♒", "今週の♓",
        "今週だけ",
    ],
}


def _check_forbidden(text: str, pattern_id: str) -> list:
    """禁止フレーズチェック。違反フレーズのリストを返す（空なら合格）"""
    phrases = _FORBIDDEN_PHRASES.get(pattern_id, [])
    return [p for p in phrases if p in text]


def _strip_markdown(text: str) -> str:
    """投稿本文からマークダウン記法を除去する（AI的な**強調**などが混入した場合の後処理）"""
    import re
    # **太字** → 太字
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # *斜体* → 斜体（絵文字の * と混同しないよう2文字以上の中身のみ）
    text = re.sub(r'\*([^\*\n]{2,}?)\*', r'\1', text)
    # # 見出し → 除去（行頭の#記号のみ）
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    # ~~取消線~~ → テキストのみ
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # `コードスパン` → テキストのみ
    text = re.sub(r'`([^`]+)`', r'\1', text)
    return text


def _generate_post(
    client_obj: anthropic.Anthropic,
    pattern: dict,
    theme: str,
    knowledge: dict,
    research_topics: list,
    analyst_feedback: str,
    feedback_for_rewrite: str = "",
    extra_hint: dict = None,
) -> str:
    prompt = _build_prompt(
        pattern, theme, knowledge, research_topics,
        analyst_feedback, feedback_for_rewrite, extra_hint,
    )
    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "text":
            text = _strip_markdown(block.text.strip())
            if len(text) < 50:
                return ""  # 短すぎる場合は不完全な生成として棄却
            return text
    return ""


def _generate_thread(
    client_obj: anthropic.Anthropic,
    pattern: dict,
    theme: str,
    knowledge: dict,
    research_topics: list,
    analyst_feedback: str,
    extra_hint: dict = None,
) -> list:
    """
    ツリー型パターン用。Claude に JSON 配列を生成させ、投稿テキストのリストを返す。

    Returns:
        ["1投稿目", "2投稿目", "3投稿目"] または失敗時 []
    """
    prompt = _build_prompt(
        pattern, theme, knowledge, research_topics,
        analyst_feedback, "", extra_hint,
    )
    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    if not raw:
        return []

    # JSON配列のパース（コードブロックに包まれている場合も処理）
    import re
    # ```json ... ``` を除去
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", raw).strip()
    # 先頭の [ から末尾の ] までを抽出
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        cleaned = match.group(0)

    try:
        posts = json.loads(cleaned)
        if isinstance(posts, list) and all(isinstance(p, str) for p in posts):
            result = [_strip_markdown(p.strip()) for p in posts if p.strip()]
            # 2〜5投稿の範囲なら有効
            if 2 <= len(result) <= 5:
                return result
    except json.JSONDecodeError:
        pass

    # パース失敗時：改行区切りを試みる（フォールバック）
    lines = [_strip_markdown(l.strip()) for l in raw.split("\n\n") if len(l.strip()) > 30]
    if len(lines) >= 2:
        return lines[:4]

    return []


def run(batch_size: int = 5) -> dict:
    """
    ライターエージェントのメイン処理。
    生成した投稿をThreadsキューとXキューの両方に追加する。

    Returns:
        {"generated": int, "queued_threads": int, "queued_x": int, "rejected": int}
    """
    client_obj = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    knowledge = _load_knowledge()
    research_data = load_research()
    history = load_history(limit=100)

    # アナリストフィードバック
    feedback_file = DATA_DIR / "analyst_feedback.txt"
    analyst_feedback = ""
    if feedback_file.exists():
        analyst_feedback = feedback_file.read_text(encoding="utf-8")

    # 類似度チェッカー初期化
    checker = SimilarityChecker()
    # スレッド投稿は thread_posts 全文を結合して登録（hookのみでは類似度が不正確になるため）
    def _history_full_text(h: dict) -> str:
        tp = h.get("thread_posts", [])
        return "\n".join(tp) if len(tp) > 1 else h.get("text", "")
    checker.load_history([_history_full_text(h) for h in history if h.get("text") or h.get("thread_posts")])

    recent_patterns = _get_recent_patterns(history)
    recent_themes = _get_recent_themes(history)
    todays_schedule = _get_todays_schedule()
    jst_hour = _get_jst_hour()
    # starvation boost 用: 直近30件のパターンIDセット
    long_history_patterns = [h.get("pattern_id") for h in history[-30:] if h.get("pattern_id")]

    # 現在のキュー長を取得（投稿予定時刻の推定に使用）
    # pending_posts（承認待ち）も含めないと推定時刻が早すぎるズレが生じる
    current_queue = load_json(DATA_DIR / "post_queue.json", default=[])
    current_pending = load_json(DATA_DIR / "pending_posts.json", default=[])
    current_queue_len = len(current_queue) + len(current_pending)

    # ペルソナの直近使用IDを追跡（重複を避ける）
    recent_persona_ids = [
        h.get("persona_id") for h in history[-20:]
        if h.get("persona_id")
    ]

    queued_threads = 0
    queued_x = 0
    rejected = 0
    details = []

    # バッチ内パターン使用カウント（pattern_id + daily_limit の1日制限に使用）
    batch_pid_counts: dict = {}
    excluded_pids: set = set()

    for i in range(batch_size):
        # スケジュールヒントを使ってテーマ選択（曜日固定コンテンツ）
        schedule_hint = todays_schedule[i % len(todays_schedule)]
        theme = _select_theme(knowledge["theme_tree"], recent_themes, schedule_hint)
        pattern = _select_pattern(
            knowledge["patterns"], recent_patterns,
            hour=jst_hour, excluded_patterns=excluded_pids,
            long_history_patterns=long_history_patterns,
        )

        # 推定投稿時刻（キュー長 + バッチ内位置 から計算）
        est_post_time = _estimate_post_time(current_queue_len, i)

        # パターン固有のヒント（星座を使うパターンは対象星座を決定）
        import random as _random
        extra_hint = {"estimated_post_time": est_post_time}
        if pattern.get("id") in ("zodiac_target", "caligula", "zodiac_deep"):
            if pattern.get("id") == "zodiac_deep":
                _recent_used = _get_recent_zodiac_deep_signs(history, days=14)
                _zodiac_val = _pick_zodiac(exclude=_recent_used)
            elif pattern.get("id") in ("caligula", "zodiac_target"):
                # 直近24時間に同一星座が投稿済みの場合は別の星座を選ぶ（連続重複防止）
                _recent_zodiac_used = _get_recent_zodiac_signs_any(history, hours=24)
                _zodiac_val = _pick_zodiac(exclude=_recent_zodiac_used)
            else:
                _zodiac_val = _pick_zodiac()
            extra_hint["zodiac"] = _zodiac_val
            extra_hint["zodiac_boost"] = _zodiac_val  # 画像選択にも使う
            # B改善: 悩みジャンルで読者層を絞り込む（12星座×4ジャンル=精密ターゲット）
            extra_hint["pain_genre"] = _random.choice([
                "転職・キャリア", "恋愛・縁", "金運・収入", "停滞・変化"
            ])
            # D改善: ペルソナの言葉を参照して書く
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))
        elif pattern.get("id") in ("kyokan", "list", "trivia", "pop_short"):
            # 50%の確率で星座ブーストを適用（コンテンツを特定星座に自然にターゲット）
            if _random.random() < 0.5:
                extra_hint["zodiac_boost"] = _pick_zodiac()
            # D改善: pop_short/kyokan にもペルソナ参照を追加
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))
        elif pattern.get("id") == "soft_discovery":
            # D改善: ターゲット層の言葉を参照して普遍的な問いを書く
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))
        elif pattern.get("id") == "persona_episode":
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))
        elif pattern.get("id") == "harm_cta":
            # HARMカテゴリをランダム選択してペルソナをマッチング
            harm_data = knowledge.get("harm", {})
            harm_cats = [k for k in harm_data if not k.startswith("_")]
            if harm_cats:
                harm_cat = _random.choice(harm_cats)
                extra_hint["harm_category"] = harm_cat
                persona_tags = harm_data[harm_cat].get("persona_tags", [])
                matched = [
                    p for p in knowledge.get("personas", [])
                    if any(tag in p.get("tags", []) for tag in persona_tags)
                    and p.get("id") not in recent_persona_ids
                ]
                if not matched:
                    matched = [
                        p for p in knowledge.get("personas", [])
                        if any(tag in p.get("tags", []) for tag in persona_tags)
                    ]
                if matched:
                    persona = _random.choice(matched)
                    extra_hint["persona"] = persona
                    recent_persona_ids.append(persona.get("id"))

        _fmt = pattern.get("format", "medium")
        # 単発投稿専用パターン（long/mediumでもスレッド化しない）
        _SINGLE_POST_ONLY = {"hoshi_betsu", "zodiac_rank_cta"}
        # medium/long パターンも2投稿ツリー形式で生成（short・単発専用パターンを除く）
        is_thread_pattern = (
            pattern.get("id") in THREAD_PATTERNS
            or (_fmt in ("medium", "long") and pattern.get("id") not in _SINGLE_POST_ONLY)
        )

        if is_thread_pattern:
            # ── ツリー投稿 ──────────────────────────────────────────
            # 既存 tree パターン以外（medium/long）はJSONフォーマット出力を指示
            _thread_hint = dict(extra_hint or {})
            if pattern.get("id") not in THREAD_PATTERNS:
                _thread_hint["output_as_thread"] = True
            thread_posts = _generate_thread(
                client_obj, pattern, theme, knowledge,
                research_data.get("topics", []), analyst_feedback,
                extra_hint=_thread_hint,
            )
            if not thread_posts or len(thread_posts) < 2:
                rejected += 1
                details.append({"status": "rejected_thread_empty", "theme": theme})
                continue

            # 星座ターゲティングパターンの事後補正（指定星座と投稿内星座を統一）
            _target_z = extra_hint.get("zodiac", "")
            if _target_z and pattern.get("id") in ("zodiac_deep",):
                thread_posts = [_fix_zodiac(p, _target_z) for p in thread_posts]

            # NGワードバリデーション（ツリー全投稿をチェック）
            _pid_check = pattern.get("id", "")
            _forbidden_hits = []
            for _tp in thread_posts:
                _forbidden_hits.extend(_check_forbidden(_tp, _pid_check))
            if _forbidden_hits:
                rejected += 1
                print(f"  🚫 NGワード自動棄却 [{_pid_check}]: {_forbidden_hits[0]}")
                details.append({"status": "rejected_forbidden", "pattern": _pid_check, "phrase": _forbidden_hits[0]})
                continue

            # 先頭投稿を代表テキストとして扱う（品質・類似度チェック用）
            representative = thread_posts[0]
            threads_text = format_for_threads(representative, theme=theme)
            # 類似度チェックはスレッド全文で判定（hookのみだと短すぎて誤棄却が起きる）
            _full_thread_text = "\n".join(format_for_threads(p, theme=theme) for p in thread_posts)
            is_dup, sim_score = checker.is_duplicate(_full_thread_text)
            if is_dup:
                rejected += 1
                details.append({"status": "rejected_similarity", "similarity": sim_score, "theme": theme})
                continue
            _formatted_posts = [format_for_threads(p, theme=theme) for p in thread_posts]
            post_obj = {
                "text": threads_text,
                "thread_posts": _formatted_posts,
                "theme": theme,
                "pattern_id": pattern.get("id"),
                "quality_score": 0.0,
                "attempts": 1,
                "is_thread": True,
                "thread_count": len(thread_posts),
            }
            # 星座ブーストを記録（画像選択で使用）
            if extra_hint.get("zodiac_boost"):
                post_obj["zodiac_boost"] = extra_hint["zodiac_boost"]
            # ツリー投稿の expires_on 処理
            # X月X日を含む投稿は推定投稿日+1日で期限切れ
            import re as _re_exp_t
            _full_for_expiry = "\n".join(_formatted_posts)
            if _re_exp_t.search(r'[0-9１-９]+月[0-9１-９]+日', _full_for_expiry) and est_post_time:
                post_obj["expires_on"] = (est_post_time.date() + timedelta(days=1)).isoformat()
            enqueue_pending(post_obj)
            queued_threads += 1
            checker.add_to_corpus(_full_thread_text)  # 全文でコーパスに追加
            recent_patterns.append(pattern.get("id"))
            recent_themes.append(theme)
            # パターン使用カウントを更新（daily_limit 制御）
            _pid = pattern.get("id", "")
            if _pid:
                batch_pid_counts[_pid] = batch_pid_counts.get(_pid, 0) + 1
                if batch_pid_counts[_pid] >= pattern.get("daily_limit", 99):
                    excluded_pids.add(_pid)
            details.append({
                "status": "pending_thread",
                "theme": theme,
                "pattern": pattern.get("name"),
                "thread_count": len(thread_posts),
                "preview": threads_text[:50] + "...",
            })
        else:
            # ── 通常投稿 ─────────────────────────────────────────────
            def rewrite_func(post_text: str, feedback: str, _p=pattern, _eh=extra_hint) -> str:
                new_post = _generate_post(
                    client_obj, _p, theme, knowledge,
                    research_data.get("topics", []), analyst_feedback,
                    feedback_for_rewrite=feedback, extra_hint=_eh,
                )
                # hoshi_betsu: リライト後も12星座が揃っているか確認。欠落なら元のテキストを維持
                if _p.get("id") == "hoshi_betsu" and new_post:
                    _ZM = "♈♉♊♋♌♍♎♏♐♑♒♓"
                    if not all(z in new_post for z in _ZM):
                        print(f"  ⚠️ hoshi_betsu rewrite: 12星座不完全 → 元テキストを維持")
                        return post_text  # 欠落があれば元テキストを返す（品質チェックを再実行させる）
                return new_post

            # 初回生成
            first_draft = _generate_post(
                client_obj, pattern, theme, knowledge,
                research_data.get("topics", []), analyst_feedback,
                extra_hint=extra_hint,
            )
            if not first_draft:
                rejected += 1
                continue

            # 星座ターゲティングパターンの事後補正（指定星座と投稿内星座を統一）
            _target_z = extra_hint.get("zodiac", "")
            if _target_z and pattern.get("id") in ("caligula", "zodiac_target"):
                first_draft = _fix_zodiac(first_draft, _target_z)

            # hoshi_betsu: 12星座すべて含まれているか検証（途中切断チェック）
            if pattern.get("id") == "hoshi_betsu":
                _ZODIAC_MARKS = "♈♉♊♋♌♍♎♏♐♑♒♓"
                if not all(z in first_draft for z in _ZODIAC_MARKS):
                    print(f"  ⚠️ hoshi_betsu: 12星座不完全（再生成）")
                    first_draft = _generate_post(
                        client_obj, pattern, theme, knowledge,
                        research_data.get("topics", []), analyst_feedback,
                        extra_hint=extra_hint,
                    )
                    if not first_draft or not all(z in first_draft for z in _ZODIAC_MARKS):
                        rejected += 1
                        continue

            # 品質チェック
            result = score_with_retry(
                first_draft, knowledge["profile"],
                rewrite_func=rewrite_func, max_retries=MAX_QUALITY_RETRIES,
            )
            if not result["accepted"]:
                rejected += 1
                details.append({"status": "rejected_quality", "score": result["score_result"]["average"], "theme": theme})
                continue

            final_post = result["post"]

            # NGワードバリデーション
            _forbidden_hits = _check_forbidden(final_post, pattern.get("id", ""))
            if _forbidden_hits:
                rejected += 1
                print(f"  🚫 NGワード自動棄却 [{pattern.get('id')}]: {_forbidden_hits[0]}")
                details.append({"status": "rejected_forbidden", "pattern": pattern.get("id"), "phrase": _forbidden_hits[0]})
                continue

            # Threads版をフォーマット（類似度チェック前: historyはformatted textで保存されているため先に変換する）
            threads_text = format_for_threads(final_post, theme=theme)

            # hoshi_betsu: フォーマット後にも12星座が揃っているか検証（rewriteや truncation で消えるケース対策）
            if pattern.get("id") == "hoshi_betsu":
                _ZODIAC_MARKS = "♈♉♊♋♌♍♎♏♐♑♒♓"
                if not all(z in threads_text for z in _ZODIAC_MARKS):
                    print(f"  ⚠️ hoshi_betsu: フォーマット後に星座欠落 → スキップ (len={len(threads_text)})")
                    rejected += 1
                    continue

            # 類似度チェック（formatted textで比較: historyがformatted textを格納しているため整合性を保つ）
            is_dup, sim_score = checker.is_duplicate(threads_text)
            if is_dup:
                rejected += 1
                details.append({"status": "rejected_similarity", "similarity": sim_score, "theme": theme})
                continue

            hashtags = get_x_hashtags_for_theme(theme)
            x_text = format_for_x(final_post, hashtags)

            x_eligible = "x" in pattern.get("platforms", [])
            post_obj = {
                "text": threads_text,
                "x_text": x_text,
                "theme": theme,
                "pattern_id": pattern.get("id"),
                "quality_score": result["score_result"]["average"],
                "attempts": result["attempts"],
                "x_eligible": x_eligible,
            }
            # 時間限定コンテンツの expires_on 自動設定
            # 「今日」「明日」「今週」「今夜」を含む投稿は推定投稿日＋1日で期限切れに
            # 特定の月日数字を含む場合のみ expires_on を設定
            # （「今日」「今夜」は相対表現なので期限を設けない）
            import re as _re_exp
            _specific_date_pat = _re_exp.compile(r'[0-9１-９]+月[0-9１-９]+日')
            if _specific_date_pat.search(threads_text) and est_post_time:
                _expires = (est_post_time.date() + timedelta(days=1)).isoformat()
                post_obj["expires_on"] = _expires
            # 「今夜0:00まで」型パターンは当日中に期限切れ（翌日キューで陳腐化するため）
            if pattern.get("id") in ("zodiac_rank_cta", "ritual_window"):
                _today_exp = est_post_time.date().isoformat() if est_post_time else datetime.now(JST).date().isoformat()
                post_obj["expires_on"] = _today_exp
            # ペルソナIDを記録（重複防止のため）
            if extra_hint.get("persona"):
                post_obj["persona_id"] = extra_hint["persona"].get("id")
            # HARMカテゴリを記録
            if extra_hint.get("harm_category"):
                post_obj["harm_category"] = extra_hint["harm_category"]

            # 星座ブーストを記録（画像選択で使用）
            if extra_hint.get("zodiac_boost"):
                post_obj["zodiac_boost"] = extra_hint["zodiac_boost"]

            # 承認待ちに追加（承認後にポスターが投稿する）
            enqueue_pending(post_obj)
            queued_threads += 1
            if x_eligible:
                queued_x += 1

            checker.add_to_corpus(threads_text)  # formatted textをコーパスに追加（historyと同形式）
            recent_patterns.append(pattern.get("id"))
            recent_themes.append(theme)
            # パターン使用カウントを更新（daily_limit 制御）
            _pid = pattern.get("id", "")
            if _pid:
                batch_pid_counts[_pid] = batch_pid_counts.get(_pid, 0) + 1
                if batch_pid_counts[_pid] >= pattern.get("daily_limit", 99):
                    excluded_pids.add(_pid)

            details.append({
                "status": "pending",
                "score": result["score_result"]["average"],
                "theme": theme,
                "pattern": pattern.get("name"),
                "x_eligible": x_eligible,
                "preview": threads_text[:50] + "...",
            })

    return {
        "generated": batch_size,
        "queued_threads": queued_threads,
        "queued_x": queued_x,
        "rejected": rejected,
        "details": details,
    }


if __name__ == "__main__":
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    result = run(batch_size=batch)
    print(json.dumps(result, ensure_ascii=False, indent=2))
