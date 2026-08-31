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
    day = datetime.now(JST).strftime("%A")  # JST基準で曜日判定
    return CONTENT_SCHEDULE.get(day, CONTENT_SCHEDULE["Monday"])


ZODIAC_SIGNS = [
    "♈牡羊座", "♉牡牛座", "♊双子座", "♋蟹座",
    "♌獅子座", "♍乙女座", "♎天秤座", "♏蠍座",
    "♐射手座", "♑山羊座", "♒水瓶座", "♓魚座",
]

# 星座記号→名前のマップ（置換に使用）
_ZODIAC_MARK_TO_NAME = {z[0]: z[1:] for z in ZODIAC_SIGNS}
_ZODIAC_NAME_TO_MARK = {z[1:]: z[0] for z in ZODIAC_SIGNS}
_ZODIAC_MARKS_STR = "♈♉♊♋♌♍♎♏♐♑♒♓"

# zodiac_boost が指定されても星座を自己管理するパターン（保証ロジック対象外）
_ZODIAC_SELF_MANAGED_PIDS = frozenset({
    "zodiac_target", "caligula", "zodiac_deep", "zodiac_problem",
    "kotodama_rank", "lucky_combo", "zodiac_ranking",
})


def _ensure_zodiac(text: str, zodiac_boost: str) -> str:
    """星座記号が本文にひとつもなければ冒頭行として追加する（zodiac_boost必須パターン用）"""
    if not zodiac_boost or not text:
        return text
    if any(c in text for c in _ZODIAC_MARKS_STR):
        return text
    return f"{zodiac_boost}の方へ。\n\n{text}"


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
    cutoff = (datetime.now(JST) - timedelta(days=days)).isoformat()
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


def _get_recent_caligula_sign(history: list, days: int = 5) -> str | None:
    """直近N日のcaligula投稿で最後に使われた星座を返す（zodiac_deepコンボ用）"""
    import re
    cutoff = (datetime.now(JST) - timedelta(days=days)).isoformat()
    marks = "♈♉♊♋♌♍♎♏♐♑♒♓"
    for h in reversed(history):
        if h.get("pattern_id") != "caligula":
            continue
        if h.get("posted_at", "") < cutoff:
            break
        text = h.get("text", "")
        m = re.search(f"([{marks}])", text)
        if m:
            for z in ZODIAC_SIGNS:
                if z[0] == m.group(1):
                    return z
    return None


def _get_recent_zodiac_signs_any(history: list, hours: int = 24) -> list:
    """直近N時間に zodiac 系パターンで使われた星座を返す（同日の重複投稿防止用）"""
    import re
    cutoff = (datetime.now(JST) - timedelta(hours=hours)).isoformat()
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
        long   … 280字超 or 箇条書き一覧型（list / menu_guide 等）
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

    # 時間帯・曜日による weight ブースト（JST基準で判定 — UTC判定だと7/9時スロットが前日になる）
    _today_wd = datetime.now(JST).weekday()  # 月=0
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

        # 木曜（wd=3）: world_view（ブランド教育・実績語り）を週次強化
        if _today_wd == 3 and pid == "world_view":
            w *= 1.5

        # 月曜（wd=0）: kotodama_rank を週次安定稼働のため 4.0x（avg 56 likes、最高実績）
        if _today_wd == 0 and pid == "kotodama_rank":
            w *= 4.0

        # 月初め（1〜3日）: kotodama_rank（今月の言霊）を月次スパイク狙いで 2.5x
        _today_dom = datetime.now(JST).day
        if 1 <= _today_dom <= 3 and pid == "kotodama_rank":
            w *= 2.5

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
    service_url = knowledge.get("profile", {}).get("service_url", "https://coconala.com/services/4292937?ref=threads")

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
        _zt_text_ctas = [
            f"{_zn}座さん、今これ当てはまってる？コメントで教えて",
            f"心当たりある？コメントで教えて",
            f"{_zn}座さん、「動きたい」か「待ちたい」か、コメントで教えて",
        ]
        _zt_cta = _rnd_zt.choice(_zt_emoji_ctas) if _zt_cta_mode == "emoji" else _rnd_zt.choice(_zt_text_ctas)
        pattern_extra = f"""
## 星座ターゲット型の追加ルール
- この投稿は「{zodiac}」の人向け
- **必須**: 冒頭1行目を「{_zm} {_zn}さんへ。」の形で始める（星座マーク {_zm} を必ず含める）
- その星座に合った吉方位・今週の運気・アドバイスを1〜2点伝える
- **文字数**: 100〜250字（スイートスポット: 250-299字でavg_eng=18.4 → 上限を活かすこと）
  - 250字前後を狙う場合: フック1行 + 気の状態描写2〜3行 + 具体アドバイス1〜2行 + CTA
  - 300字超は厳禁（300-399字はavg_eng=1.5の死亡ゾーン）
- 語尾はやや親しみやすく（「〜してみてね」「〜がいいよ」など）
- **必須**: 末尾は以下のCTAで締める → {_zt_cta}
{_pain_ctx}
### 悩みテーマ強度ガイド（実績データより）
✅ **高eng（優先使用）**:
- 「金運・収入」: eng=58「努力は正しいのに、数字が動かない。そのモヤモヤ、意志の弱さじゃないよ。」
- 「停滞・変化の気配」: eng=41「「このままでいいのかな」が口癖になっていない？」
- 「判断ができない・迷い」: eng=25「最近、誰かに合わせすぎて自分の判断がぼやけてない？」

❌ **低eng（汎用表現を避ける）**:
- 「恋愛・縁」の汎用表現（eng=1〜5）→ 使うなら「なぜ縁が続かないのか」まで深掘り必須
- 「頑張ってるのに変わらない」という汎用フック（→ 星座特有の悩みに具体化すること）

### 星座別フック強化ヒント
- **牡羊座**: 「先頭にいたいのに、なぜか評価されない」「決断が早すぎて空回りしてる」 → 競争心・先頭意識を使う
- **蠍座**: 金運/仕事×「数字が動かない」「信頼が裏切られた」 → 深い感情を揺さぶる
- **魚座**: 「感覚が戻らない」「感情が閉じている」 → 繊細な感情描写
- **天秤座**: 「判断が遅い」「他人の顔色を見すぎた」 → 迷いの具体的描写

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
            "今の気の流れ×吉方位行動",
            "転機ポイントとNG行動",
            "大きな流れ×動き方のコツ",
            "吉方位×お金・仕事・人間関係",
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
5. いいね誘導（任意・引きの直後、50%の確率で追加）: 「続きはいいね🩷してもらえると届きます」「いいね🩷で次が受け取れます」
   ※ 固定化するとスパム感が出るので毎回ではなく自然に

**2投稿目（190〜230字）— 吉方位2点目×NG×保存CTA**
目的: 情報を使い切ってもらい保存と絵文字コメントを促す
1. 吉方位×行動（1点）: 「・○方向 → 具体的にやること（理由）」（1投稿目と違う方位）
2. NG・注意点（1点）: 「・避けたい方位・行動 → なぜか（龍脈的理由）」
3. 締め（3行）: 「保存して参考にして。」＋「当てはまると思ったらいいね🩷」＋「{_zn_d}さん、気になること ✨ をコメントで。」

### 書き方ルール
- 冒頭1行目を必ず「{_zm_d}{_zn_d}さん、〜」で始める
- 「吉方位×行動」は抽象的NG。「東向きの席を選ぶ」「南口から出てみる」レベルの具体性
- 2投稿合計で元の400〜480字を維持し、情報密度は落とさない
- zodiac_targetとの差別化: zodiac_targetは100〜250字単発、zodiac_deepは2投稿で深掘り

### 実績TOP投稿の勝ちパターン（必ず参考にすること）

eng=158 v=4605: 「今の魚座には「感覚が戻ってくる気」が流れている。ずっと麻痺していたものが、少しずつ溶け始めるタイミング。頑張りすぎて感情ごと閉じてしまっていた人ほど、この流れを使ってほしい。」
→ 引き:「ただし、今週だけ避けてほしい方位がある。」

eng=122 v=3020: 「今の天秤座には「判断の気」が集まってきている。天秤座が陥りやすいのは「もう少し情報を集めてから」と先送りすること。」
→ 引き:「もう一点、避けてほしい方位がある──」

✅ 勝ちパターン（実績から確認）:
1. 気の状態を「名詞で引用符化」する: 「「感覚が戻ってくる気」」「「判断の気」」「「突破の気」」
2. 星座特有の傾向を明示: 「{_zn_d}が陥りやすいのは〜」「{_zn_d}らしいのは〜」→ バーナム効果
3. 感情的共鳴: 「頑張りすぎて感情ごと閉じてしまっていた人ほど」
4. 行動は最小具体: 「カーテンを開けるだけ」「窓を開けて10分」
5. 引きは「方位ワード+語尾カット」: 「避けてほしい方位がある──」
"""
    elif pattern_id == "zodiac_problem":
        zodiac = extra_hint.get("zodiac", "♈牡羊座")
        _zm_p = zodiac[0] if zodiac and not zodiac[0].isalpha() and not '぀' <= zodiac[0] <= '鿿' else ""
        _zn_p = zodiac.lstrip("♈♉♊♋♌♍♎♏♐♑♒♓").strip()
        _pg_p = (extra_hint or {}).get("pain_genre", "停滞・変化")
        import random as _rnd_zp
        _zp_cta = _rnd_zp.choice([
            f"心当たりある？コメントで教えて。",
            f"{_zn_p}座さん、今これ当てはまってる？",
            f"「そうそう」と思った人、コメントで教えて。",
            f"今どっちに近い？コメントで。",
        ])
        pattern_extra = f"""
## 星座×悩み直撃型の追加ルール

対象星座: {zodiac}（記号: {_zm_p}　名称: {_zn_p}）
今回の悩みジャンル: {_pg_p}

### 構成（150〜220字）
1. **冒頭（1行）**: 「{_zm_p}{_zn_p}で、{_pg_p}に悩んでいる人へ。」または「{_zm_p}{_zn_p}で、{_pg_p}がうまくいかないと感じてる人へ。」
2. **原因の外在化（2行）**: 龍脈命術の視点で「あなたのせいじゃない、方向・気の流れの問題」として外在化する
   例: 「龍脈命術で見ると、今の{_zn_p}座には〜の気がある。努力の問題じゃなく、向きの問題。」
3. **解決行動（1行）**: 具体的な方位×行動1点。「○方向に〜するだけで〜が変わる」レベルの具体性
4. **CTA（末尾必須）**: {_zp_cta}

### 禁止
- 「今週」「今月」「今夜」などの時事表現（投稿が時間経過で嘘になる）
- 「信じれば変わる」「気持ちの問題」など主観押し付け表現
- 220字を超えること（コンパクトさが命）

### 差別化ポイント
- zodiac_target: 吉方位アドバイス中心（What to do）
- zodiac_problem: 悩みの原因外在化＋「あなたのせいじゃない」共感（Why it's happening）
- caligula: 排除・禁止フックで引き込む
- zodiac_problem: 共感フックで引き込む（「自分の話だ」という認識）

### 心理効果
- **スポットライト効果**: 星座+悩みの二重指名で「自分への手紙」感
- **外在化効果**: 原因を外在化することで「自分のせいじゃない」安心感と信頼が同時に生まれる
- **最小行動CTA**: 具体的な1行動だけを提示して敷居を下げる
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
## カリギュラ型の必須構造（2投稿ツリー）

{_zodiac_required}

### 重要：出力形式
以下の形式で2つの投稿テキストをJSON配列として出力すること：
["1投稿目（フック）", "2投稿目（本文）"]

### 今回のhook型: {_cali_hook_type}
{_hook_guide}

### 実績TOP投稿と弱い投稿の比較（n=140件 全期間データ）

★ 最強パターン「心理状態の指名 × 否定制限」の組み合わせが爆発する:
- 「♒水瓶座で「運気の波」が気になり始めた人以外は、読まなくていい。」→ eng=356, v=6385 ★最高eng
  → 「心理状態（気になり始めた）× 状態名詞の引用符 × 否定制限」= 最強の3要素
- 「♎天秤座さん、来週についてまだ何も決めていないなら読まないほうがいい。」→ eng=120, v=10669 ★最多views
  → 「近未来（来週）× 決断未完の状態 × 読まないほうがいい」= views爆発形
- 「♏蠍座さん、今お金の不安がない人はこの先読まなくていい。」→ 感情状態の対比型
  → 「現在（今）× 感情状態（安心/不安の対比）× 読まなくていい」

★ deny型の最強テンプレート:
「{zodiac_mark}{zodiac_name}で、「【状態の名詞表現】」が【気になり始めた/ある】人以外は読まなくていい。」
または
「{zodiac_mark}{zodiac_name}さん、【時期・タイミング】についてまだ【行動未完】なら読まないほうがいい。」

✅ 2投稿の本文でengが上がる要素（実績確認済み）:
- 「なぜ今の{zodiac_name}の龍脈が〜」という原因の説明（読者が「自分のことだ」と確認する）
- 方位 × 具体的な1行動（「北東へ向くだけで」など最小行動）
- 時間urgency: 「今週中に」「今の時期だけ」「このタイミングで」
- 末尾の2択質問: 「当てはまる → 🔮 / 初めて聞いた → ✨ コメントで」

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

### 各投稿の構成（必須）

**1投稿目（フック・30〜70字・ハッシュタグなし）**
- {_hook_guide.split(chr(10))[0]}
- カリギュラ型の制限・指名フレーズだけで完結させる。本文の説明は入れない。
- 「続きを読みたい」と思わせる短さが最重要。

**2投稿目（本文・120〜220字）**
- 龍脈命術的な根拠・核心情報。「なぜ今の{zodiac_name}座にこれが起きるのか」を時期感を伴って具体的に
- 読者が1行で答えられる問いかけで終える。**「今週中に」「今夜」など時間urgencyを添えると反応率↑**
- ハッシュタグ2〜3個（例: #占い #龍脈命術）

### 文字数
- 1投稿目: 30〜70字（短すぎず、カリギュラフレーズとして機能する長さ）
- 2投稿目: 120〜220字（実績の250〜290字帯の密度を本文に集約）
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
        # story_hook: 実績TOP(eng=85,41)は体験談型。断言型より2.6倍高eng
        _ichigon_mode = _rnd_ichigon.choice(["story_hook", "story_hook", "standard", "ultra_short"])

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
        elif _ichigon_mode == "story_hook":
            pattern_extra = """
## 一言型（体験談フック・最高実績モード）の追加ルール

### 目的: エンゲージメント最大化（avg_eng 14.4 ← 断言型の2.6倍）
実績TOP: 「♏蠍座の方へ。方位を変えただけで、想定外のことが起きた。」 → eng=85, v=731
実績2位: 「♉牡牛座の方へ。「このままじゃいけない」と思いながら、もう何年経ちましたか。」 → eng=41

### 構成（150〜220字）
1. **冒頭（1行）**: zodiac_boost で指定された星座を必ず使い「[星座マーク][星座名]の方へ。」
2. **フック1行**: 「方位を変えただけで、想定外のことが起きた。」「変わった人と変わらなかった人の違いを見てきた。」など
   - 体験または発見の断言で読者を引き込む
3. **鑑定エピソード（3〜5行）**: 実際の鑑定エピソードを語る体験談
   - 「先日鑑定した[星座]の方。○年〜〜だった。」
   - 「龍脈命術で見ると、方位のズレが〜。」
   - 「向かう方向を整えたら、なぜか〜」（途中で終わる / 続きへの欲求）
4. **末尾（任意・60%確率）**: 「当てはまる人、いる？」「こういうケース、意外と多い。」など自然な問いかけ

### 禁止
- 知識説明型（「方位に吉凶がある理由は〜」「龍脈命術とは〜」）→ avg_eng 2以下
- 汎用・星座なし → 星座なしの場合avg_eng 5.5（星座ありの38%）
- 「今週」「今夜」など時事表現（投稿が古くなると機能しない）

### 心理効果
- **社会的証明**: 「実際に変わった人がいる」という事実が行動変容の最強トリガー
- **部分開示**: エピソードを途中で終わらせることで続きを知りたい欲求が生まれる
- **感情移入**: 読者が「自分も同じだ」と思えるエピソード選択
"""
        else:  # standard mode
            pattern_extra = """
## 一言型（標準モード）の追加ルール
- 1〜3行の断言型短文（60〜100文字）
- **必須**: zodiac_boostで指定された星座を冒頭に入れること（星座なしはeng 5.5 → 星座ありの38%）
- pop_shortと異なり、**占術的・抽象的な断言・名言風**にする（具体的な生活ヒントではない）
- 1行目だけで「何かが変わる気がする」と思わせる言葉の密度にする
- 説明しすぎない。余白が読者の想像力を引き出す
- **必ず最後の1行**に「うなずいた人、いる？」「わかる？」「これ、あなたのこと？」など自然な問いかけを添える（命令形NG）
- **禁止**: 知識説明型（「方位の吉凶とは」「龍脈命術の仕組み」）→ avg_eng 2以下

### テーマを毎回ローテーションすること（同じテーマを連続使用しない）
- 停滞・空回り（「動いているのに前に進まない感覚」）
- 転機・動き出し（「変わる前の静けさ」「動けるタイミング」）
- 方位・場所の気（「いる場所が変わると、自分も変わる」）
- 判断・タイミング（「今じゃない感覚は当たっていることが多い」）
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

    elif pattern_id == "kotodama_rank":
        import random as _rnd_kr
        _kr_emoji_set = ["🌟", "🔮", "🐉", "⭐", "🌙"]
        _kr_emoji = _rnd_kr.choice(_kr_emoji_set)
        _kr_cta_variants = [
            f"気づいた方だけ受け取ってください。\nあなたの星座の言霊、受け取れた方は「{_kr_emoji}」をそっと置いてみて。",
            f"気づいた人だけ受け取ってください。\n自分の星座が刺さった方、「{_kr_emoji}」をコメントに置いてって。気が届きますように。",
            f"そっと届けます。\nあなたの星座の言霊、受け取れた方は「{_kr_emoji}」を置いてみて。",
        ]
        _kr_cta = _rnd_kr.choice(_kr_cta_variants)
        # ランキングのテーマを毎回変える（最高実績テーマを重点的に）
        _kr_theme_candidates = [
            "方位の吉凶が決まる仕組み",   # avg=126.9 最高実績テーマ → 3枠
            "方位の吉凶が決まる仕組み",
            "方位の吉凶が決まる仕組み",
            "方位が開く言葉",             # eng=107 TOP投稿のテーマ
            "方位が開く言葉",
            "動き出しの言霊",
            "転換の言霊",
            "豊かさの言霊",
            "気の流れを変える言葉",
            "扉が開く言霊",
        ]
        _kr_rank_theme = _rnd_kr.choice(_kr_theme_candidates)
        # 1位星座をランダムに変える（高実績星座に重みをつける）
        _kr_top1_candidates = [
            ("いて座", "「宿命の向きが整ったとき、気が一気に動く」"),      # eng=107 実績最高 → 2枠
            ("いて座", "「遠くへ向かう気が、今の流れを一変させる」"),      # eng=86
            ("みずがめ座", "「鏡の向きを整えると、気の反射が変わる」"),   # eng=57
            ("てんびん座", "「受け取る準備が整ったとき、気が動き始める」"),
            ("さそり座", "「深く潜った分だけ、気の力が強まる」"),
            ("うお座", "「流れに逆らわず、向きだけを整える」"),
            ("おひつじ座", "「踏み出す向きを変えたとき、道が開く」"),
            ("しし座", "「輝こうとする方向に、気は集まる」"),
            ("やぎ座", "「積み上げた先に向きを整えると、気が実る」"),
            ("ふたご座", "「情報の向きを整えると、縁が動き出す」"),
            ("かに座", "「守る場所の気が整ったとき、縁が流れ込む」"),
            ("おうし座", "「根を張る方向が合うと、気が安定して動く」"),
        ]
        _kr_top1_zodiac, _kr_top1_phrase = _rnd_kr.choice(_kr_top1_candidates)
        pattern_extra = f"""
## 龍脈言霊ランキング型の追加ルール

### 構成（必須）
1. 冒頭フック（1行）: 「♈♉♊♋♌♍♎♏♐♑♒♓ 気づいた方だけ受け取ってください。」の形式で始める。**先頭に12星座の記号を並べること（必須）。**
2. ランキングタイトル（1行）: 「【龍脈言霊ランキング｜{_kr_rank_theme}】」
3. ランキング本体（全12星座を必ず全部含める）: 🥇🥈🥉🏅を1〜4位に使い、以下は「第◯位」
   - 各星座に「向き・方位・気の流れ」を使った15〜25文字の言霊フレーズ
   - **今回の1位**: {_kr_top1_zodiac} → 例: 🥇第1位　{_kr_top1_zodiac}{_kr_top1_phrase}
   - **【重要】前回の投稿から1位星座を変えること。同じ星座が連続で1位にならないようにする。**
4. 締めemoji CTA（2〜3行）:
{_kr_cta}

### テーマ「{_kr_rank_theme}」で書く際の指針
- 「方位の吉凶が決まる仕組み」: 各星座がなぜその位置になるか、龍脈命術の「気の向き」で説明する。専門性が高いほど保存・シェアが増える。
- 「方位が開く言葉」: 実際に方位を動かした人が発した・受け取った言葉。臨場感を出す。
- その他のテーマ: 各星座の特性と「気の流れ・方位」を結びつけた言霊フレーズ。

### 言霊フレーズの作り方（必須）
- 「方位」「向き」「気」「流れ」「扉」「動き」「受け取る」「届く」などの龍脈命術ワードを使う
- 各星座の特性に合った言葉選び（牡羊=スタート、天秤=バランス、魚=受容など）
- 抽象的すぎず、「読んだ瞬間にうなずける」具体感
- 12星座すべてに違うフレーズ。同じ言葉の使いまわしNG

### 実績TOP投稿のフレーズ例（参考）
- いて座「宿命の向きが整ったとき、気が一気に動く」(eng=107, 歴代最高)
- いて座「遠くへ向かう気が、今の流れを一変させる」(eng=86, 2位)
- みずがめ座「鏡の向きを整えると、気の反射が変わる」(eng=57)
- てんびん座「住む場所の気が、私の流れを決めている」
- ふたご座「手放す方向を変えると、入ってくるものが変わる」

### 文字数
全体で350〜450字（ランキング数で調整）

### 禁止
- URLを入れない（reach→engage系）
- 「必ず変わる」「保証します」などの断言
- 星座フレーズの重複・コピー
- 「今週」「今夜」など時事表現をCTAに入れない（投稿が古くなっても機能するように）
"""

    elif pattern_id == "lucky_combo":
        import random as _rnd_combo
        _combo_themes = {
            "手放し": {
                "intro": "重荷を下ろした人から、順番が巡ってきます。",
                "rank_label": "手放しの流れが今月最も強く届く",
                "closing": "欠けていく月が、あなたの余分を削ぎ落とす頃。「軽くなった器に、次が入る」。",
                "cta_emoji": "⭐",
                "cta_text": "を置いた人から、抱えてきた重さが少しずつ抜けていきますように🌌",
            },
            "金運": {
                "intro": "豊かさの流れが、あなたの方向へ動き始める組み合わせ。",
                "rank_label": "金運の気が今月最も強く集まる",
                "closing": "龍脈命術では、生まれた月と星座の気が重なるとき方位の力が2倍になる。",
                "cta_emoji": "💰",
                "cta_text": "を置いた人から、止まっていた豊かさの流れが動き始めますように",
            },
            "縁・恋愛": {
                "intro": "止まっていたご縁が、動き始める組み合わせ。",
                "rank_label": "ご縁の気が今月最も強く流れ込む",
                "closing": "龍脈命術で見ると、生まれ月と星座の気が重なる配置に縁の流れが集まりやすい。",
                "cta_emoji": "🌙",
                "cta_text": "を置いた方から、止まっていたご縁に新しい流れが入りますように",
            },
            "天職": {
                "intro": "天職の気配が最も強く引き寄せられる組み合わせ。",
                "rank_label": "天職・仕事運の気が今月最も集まる",
                "closing": "龍脈命術では「仕事が合わない」は能力ではなく気の向きの問題。生まれ月×星座でその向きが読める。",
                "cta_emoji": "🐉",
                "cta_text": "を置いた人から、今の仕事に新しい展開が入ってきますように",
            },
        }
        _theme_key = _rnd_combo.choice(list(_combo_themes.keys()))
        _th = _combo_themes[_theme_key]

        pattern_extra = f"""
## 生まれ月×星座ランキング型の必須構造

### テーマ: {_theme_key}

### 構成（厳守）
1. テーマ導入（1行）: 「{_th['intro']}」の形式で始める。**先頭または2行目までに「星座」または「♈〜♓」などの星座記号を必ず含めること。**
   例: 「{_th['intro'][:-1]}。生まれ月と星座の組み合わせ。」
2. ランキングタイトル（1行）: 「{_th['rank_label']} 生まれ月×星座 top5〜8」
3. ランキング本体（5〜8項目・改行区切り）:
   形式: 「1位 〇月生まれ × △△座」
   - 月は1〜12月の中から選ぶ（重複なし）
   - 星座は12星座から選ぶ（重複なし）
   - 龍脈命術的な根拠（月の気×星座の特性の相性）で組み合わせる
4. 補足1行: 「{_th['closing']}」
5. 絵文字CTA（1〜2行）: 「『{_th['cta_emoji']}』{_th['cta_text']}」

### 重要
- 時雨の世界観で書く（「宇宙がこう言っている」などスピリチュアル過多はNG）
- 龍脈命術・気の流れ・方位の文脈で説明できる範囲で
- 同じ星座・月の重複ランキングNG
- 絵文字CTAは「〜しますように」で祈願・祝福の形
- URLなし（reach系）

### 文字数
200〜280字
"""

    elif pattern_id == "zodiac_ranking":
        import random as _rnd_zr
        from datetime import datetime as _dt_zr
        _zr_dow = _dt_zr.now().weekday()  # 0=Mon
        _zr_type = _zr_dow % 3
        if _zr_type == 0:
            _zr_label = "運勢予報型"
            _zr_title = "今週、流れが変わる星座ランキング"
            _zr_hook = "今週だけ読んでほしい。"
            _zr_sign_hint = "各星座の今週の運気・流れの変化を10〜18字で（龍脈命術の観点から: 気の動き・方位・タイミング）"
            _zr_cta_emojis = ["🔮", "🐉", "⭐"]
        elif _zr_type == 1:
            _zr_themes = ["金運", "ご縁・恋愛", "天職・仕事運", "転換期"]
            _zr_theme = _rnd_zr.choice(_zr_themes)
            _zr_label = f"テーマ別（{_zr_theme}）型"
            _zr_title = f"今月{_zr_theme}が動き出す星座ランキング"
            _zr_hook = "今月の流れを先に知っておいてほしい。"
            _zr_sign_hint = f"各星座の今月の{_zr_theme}の動き・タイミングを10〜18字で（具体的に。「流れが来る」より「動いた先に豊かさがある」レベルで）"
            _zr_cta_emojis = ["🔮", "💰", "🌙"]
        else:
            _zr_label = "行動促進型"
            _zr_title = "今週中にやると流れが変わる行動ランキング｜星座別"
            _zr_hook = "今週中に動いてほしい人だけ読んで。"
            _zr_sign_hint = "各星座が今週中にやると運気が変わる具体的な行動を1つ（10〜18字。「掃除する」より「玄関の北側を片付ける」レベルで）"
            _zr_cta_emojis = ["⭐", "🔮", "🐉"]
        _zr_emoji = _rnd_zr.choice(_zr_cta_emojis)
        _zr_cta_variants = [
            f"「{_zr_emoji}」を置いた方から、気の流れが整い始めます。あなたの流れが動き出しますように。",
            f"「{_zr_emoji}」をそっと置いた方から、止まっていた気の流れが動き始めます。",
            f"気になった方は「{_zr_emoji}」を置いてみて。龍脈の気が届きますように。",
        ]
        _zr_cta = _rnd_zr.choice(_zr_cta_variants)
        pattern_extra = f"""
## 12星座ランキング（日次アンカー）の追加ルール

### タイプ: {_zr_label}
- 冒頭フック（1行）: 「{_zr_hook}」（必ずこのフレーズか類似のtime urgencyで始める）
- タイトル（1行）: 【{_zr_title}】
- 空行
- ランキング（全12星座を必ず全部含める）:
  🥇1位 ♈牡羊座｜フレーズ
  🥈2位 ♉牡牛座｜フレーズ
  🥉3位 ♊双子座｜フレーズ
  🏅4位 ♋蟹座｜フレーズ
  5位 ♌獅子座｜フレーズ
  6位 ♍乙女座｜フレーズ
  7位 ♎天秤座｜フレーズ
  8位 ♏蠍座｜フレーズ
  9位 ♐射手座｜フレーズ
  10位 ♑山羊座｜フレーズ
  11位 ♒水瓶座｜フレーズ
  12位 ♓魚座｜フレーズ
- 空行
- 絵文字CTA（2〜3行）:
{_zr_cta}

### フレーズの作り方
{_zr_sign_hint}
- 各星座の特性（牡羊=スタート、牡牛=じっくり、双子=情報、蟹=内向き、獅子=表現、乙女=整理、天秤=判断、蠍=変容、射手=拡大、山羊=実績、水瓶=独自路線、魚=受容）を踏まえて書く
- 星座ごとに違うフレーズ（コピーNG）
- 順位は今週（or今月）の龍脈命術的な気の流れで決める（でたらめはNG）
- 星座記号（♈♉♊♋♌♍♎♏♐♑♒♓）を必ず各行の星座名前に付けること

### 実績TOP投稿の特徴（必ず参考にすること）
- eng=107(最高): 「今週だけ読んでほしい。【今週、流れが変わる星座ランキング】🥇1位 ♌獅子座｜動く前に、方位が味方をする」
  → フレーズが短く・行動を促さない（状態の描写）が高eng
- eng=86: 「今週、吉方位が動く星座ランキング🥇1位 ♏蠍座｜手放した先に、気の道が開く」
  → 「手放した先に」という体験的フレーズが刺さる
- **フレーズは「〜する」(行動指示)より「〜のとき、〜になる」(変化の描写)が強い**

### 文字数
380〜480字

### 禁止
- URLなし（engage系）
- 同じフレーズの使いまわし
- 「保証します」「絶対」などNG
- 「今日だけ」フック → 「今週だけ」に統一（今日限定は期待値下がる）
- 日付・吉日依存（「寅の日に〜」など：スケジュールずれで嘘になる）
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
    if zodiac_boost and pattern_id not in _ZODIAC_SELF_MANAGED_PIDS:
        pattern_extra += f"""
## 星座ターゲット（明示必須）
このテーマを「{zodiac_boost}」の人に向けて書く。
**必須**: 投稿テキストの冒頭（1行目または2行目まで）に星座記号「{zodiac_boost}」を必ず含めること。
例: 「{zodiac_boost}の方へ。」「{zodiac_boost}さん、聞いてほしい。」「{zodiac_boost}の方からよく聞く言葉がある。」
言葉選び・具体例・感情描写もその星座の特性（性格・悩みパターン・行動様式）に合わせて書く。
"""
    # ─────────────────────────────────────────────────────────────

    # ── D改善: ペルソナ参照ヒント（harm_cta 以外の全パターンに適用）──
    _persona_ref = (extra_hint or {}).get("persona")
    _patterns_own_persona = {"harm_cta"}
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
  - 例外: 自己診断・ランキングは〜280文字
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
    # バッチ内で既に使った星座（同バッチ内での重複を防ぐ）
    batch_used_zodiacs: list = []

    # zodiac_ranking アンカー: キューにまだなければバッチ先頭で強制生成
    _ANCHOR_PID = "zodiac_ranking"
    _anchor_in_queue = any(item.get("pattern_id") == _ANCHOR_PID for item in current_queue)
    _anchor_pat = next(
        (p for p in knowledge["patterns"] if p.get("id") == _ANCHOR_PID and p.get("status", "active") == "active"),
        None
    )
    _force_anchor_first = (not _anchor_in_queue) and (_anchor_pat is not None)

    for i in range(batch_size):
        # スケジュールヒントを使ってテーマ選択（曜日固定コンテンツ）
        schedule_hint = todays_schedule[i % len(todays_schedule)]
        theme = _select_theme(knowledge["theme_tree"], recent_themes, schedule_hint)
        # i=0 で zodiac_ranking がキューにない場合は強制選択
        if i == 0 and _force_anchor_first:
            pattern = _anchor_pat
        else:
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
        if pattern.get("id") in ("zodiac_target", "caligula", "zodiac_deep", "zodiac_problem"):
            if pattern.get("id") == "zodiac_deep":
                _recent_used = _get_recent_zodiac_deep_signs(history, days=14)
                # caligula コンボ: 直近5日のcaligula星座と同じ星座を60%確率で使う（シリーズ効果）
                _combo_sign = _get_recent_caligula_sign(history, days=5)
                if (_combo_sign and _combo_sign not in _recent_used + batch_used_zodiacs
                        and _random.random() < 0.6):
                    _zodiac_val = _combo_sign
                else:
                    _zodiac_val = _pick_zodiac(exclude=_recent_used + batch_used_zodiacs)
            elif pattern.get("id") in ("caligula", "zodiac_target", "zodiac_problem"):
                # 直近7日 + バッチ内使用済みを除外（バッチ内重複防止）
                _recent_zodiac_used = _get_recent_zodiac_signs_any(history, hours=24 * 7)
                _zodiac_val = _pick_zodiac(exclude=_recent_zodiac_used + batch_used_zodiacs)
            else:
                _zodiac_val = _pick_zodiac()
            batch_used_zodiacs.append(_zodiac_val)
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
            # harm_ctaも星座ブーストを適用（zodiac_boost section側でexcludeされないため有効）
            extra_hint["zodiac_boost"] = _pick_zodiac(exclude=batch_used_zodiacs)
            batch_used_zodiacs.append(extra_hint["zodiac_boost"])
        else:
            # _ZODIAC_SELF_MANAGED_PIDS（自己管理）以外の全パターンに星座ブーストを適用
            if pattern.get("id") not in _ZODIAC_SELF_MANAGED_PIDS:
                extra_hint["zodiac_boost"] = _pick_zodiac(exclude=batch_used_zodiacs)
                batch_used_zodiacs.append(extra_hint["zodiac_boost"])
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))

        _fmt = pattern.get("format", "medium")
        # 単発投稿専用パターン（long/mediumでもスレッド化しない）
        # zodiac_target: 100-250字設計でhook+body分割が成立しない
        # fuusui_tip: 130-200字の1アクション完結型、分割すると薄くなる
        _SINGLE_POST_ONLY = {
            "zodiac_target",    # 100-250字設計でhook+body分割が成立しない
            "fuusui_tip",       # 130-200字の1アクション完結型、分割すると薄くなる
            "kotodama_rank",    # 350-450字の全12星座ランキング。汎用2投稿指示がフォーマットを破壊する
            "zodiac_ranking",   # 380-480字の全12星座ランキング。同上
            "lucky_combo",      # 200-280字の生まれ月×星座ランキング。分割不要
        }
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
            # zodiac_boost パターン: 先頭投稿（フィードに表示される行）に星座記号がなければ冒頭に追加
            _zb_thread = extra_hint.get("zodiac_boost", "")
            if _zb_thread and pattern.get("id") not in _ZODIAC_SELF_MANAGED_PIDS:
                if not any(c in thread_posts[0] for c in _ZODIAC_MARKS_STR):
                    thread_posts[0] = f"{_zb_thread}の方へ。\n\n{thread_posts[0]}"

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
            _full_thread_text = "\n".join(format_for_threads(p, theme=theme) for p in thread_posts)
            # 二段階重複チェック:
            # 1) full thread text で判定（新しい履歴はフル保存されているため）
            # 2) hook のみでも判定（旧履歴は text フィールド＝hook のみで保存されているため
            #    full text との類似度が低くなり重複を見逃す問題を補完する）
            is_dup, sim_score = checker.is_duplicate(_full_thread_text)
            if not is_dup:
                is_dup_hook, sim_hook = checker.is_duplicate(threads_text)
                if is_dup_hook:
                    is_dup, sim_score = True, sim_hook
            if is_dup:
                rejected += 1
                details.append({"status": "rejected_similarity", "similarity": sim_score, "theme": theme})
                continue
            # note CTA 追記（reach/engage パターンのスレッドに 60% の確率で追加）
            _NOTE_CTA_RATE = 0.6
            _note_cta_variants = knowledge.get("profile", {}).get("note_cta_variants", [])
            if _note_cta_variants and pattern.get("category") in ("reach", "engage"):
                import random as _rand_note
                if _rand_note.random() < _NOTE_CTA_RATE:
                    thread_posts.append(_rand_note.choice(_note_cta_variants))
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
            import re as _re_exp_t
            _full_for_expiry = "\n".join(_formatted_posts)
            if _re_exp_t.search(r'[0-9１-９]+月[0-9１-９]+日', _full_for_expiry) and est_post_time:
                # 特定日付を含む → 翌日に期限切れ
                post_obj["expires_on"] = (est_post_time.date() + timedelta(days=1)).isoformat()
            else:
                # 通常ツリー投稿: 生成日から14日でステイル化
                from datetime import timezone as _tz
                _jst14 = datetime.now(timezone(timedelta(hours=9))).date()
                post_obj["expires_on"] = (_jst14 + timedelta(days=14)).isoformat()
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
            # zodiac_boost パターン: 星座記号が本文になければ冒頭に強制追加（品質チェック前）
            _zb_single = extra_hint.get("zodiac_boost", "")
            if _zb_single and pattern.get("id") not in _ZODIAC_SELF_MANAGED_PIDS:
                first_draft = _ensure_zodiac(first_draft, _zb_single)

            # 品質チェック
            # 実エンゲージ実績が高いがランキング形式で「具体性」が低評価されるパターンは閾値を緩和
            _HIGH_ENG_PIDS = {"kotodama_rank", "caligula", "lucky_combo", "zodiac_ranking"}
            _q_threshold = 6.0 if pattern.get("id") in _HIGH_ENG_PIDS else None
            result = score_with_retry(
                first_draft, knowledge["profile"],
                rewrite_func=rewrite_func, max_retries=MAX_QUALITY_RETRIES,
                threshold=_q_threshold,
            )
            if not result["accepted"]:
                rejected += 1
                details.append({"status": "rejected_quality", "score": result["score_result"]["average"], "theme": theme})
                continue

            final_post = result["post"]
            # 品質チェックで書き直された場合も星座を保証（rewriteで星座が消えることへの安全弁）
            if _zb_single and pattern.get("id") not in _ZODIAC_SELF_MANAGED_PIDS:
                final_post = _ensure_zodiac(final_post, _zb_single)

            # NGワードバリデーション
            _forbidden_hits = _check_forbidden(final_post, pattern.get("id", ""))
            if _forbidden_hits:
                rejected += 1
                print(f"  🚫 NGワード自動棄却 [{pattern.get('id')}]: {_forbidden_hits[0]}")
                details.append({"status": "rejected_forbidden", "pattern": pattern.get("id"), "phrase": _forbidden_hits[0]})
                continue

            # Threads版をフォーマット（類似度チェック前: historyはformatted textで保存されているため先に変換する）
            threads_text = format_for_threads(final_post, theme=theme)

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
            # expires_on 自動設定
            import re as _re_exp
            _specific_date_pat = _re_exp.compile(r'[0-9１-９]+月[0-9１-９]+日')
            if _specific_date_pat.search(threads_text) and est_post_time:
                # 特定日付を含む → 翌日に期限切れ
                _expires = (est_post_time.date() + timedelta(days=1)).isoformat()
            else:
                # 通常投稿: 生成日から14日でステイル化（キュー積み上がり防止）
                _jst_today = datetime.now(timezone(timedelta(hours=9))).date()
                _expires = (_jst_today + timedelta(days=14)).isoformat()
            post_obj["expires_on"] = _expires
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
