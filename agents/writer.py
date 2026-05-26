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
    PERSONAS_FILE, CONTENT_SCHEDULE, DATA_DIR,
)
from core.state import (
    load_json, load_history, load_research, enqueue_post,
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

# GitHub Actionsのスケジュール（JST）
SCHEDULED_HOURS_JST = [10, 12, 14, 16, 18, 20, 22]
JST = timezone(timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _get_jst_hour() -> int:
    return datetime.now(JST).hour


def _estimate_post_time(queue_length: int, batch_position: int) -> datetime:
    """
    現在のキュー長とバッチ内の位置から投稿予定時刻を推定する。
    スケジュールは SCHEDULED_HOURS_JST（JST）の順で1投稿/1スロット消費。
    """
    now = datetime.now(JST)

    # 今後3日分のスケジュールスロットを生成（現在時刻より後のみ）
    upcoming_slots = []
    for delta_days in range(3):
        day = now.date() + timedelta(days=delta_days)
        for h in SCHEDULED_HOURS_JST:
            slot = datetime(day.year, day.month, day.day, h, 0, tzinfo=JST)
            if slot > now:
                upcoming_slots.append(slot)

    # キューにある既存の投稿を先に消費し、その後のスロットにこの投稿が来る
    idx = queue_length + batch_position
    if idx < len(upcoming_slots):
        return upcoming_slots[idx]
    return upcoming_slots[-1] if upcoming_slots else now


def _format_datetime_jp(dt: datetime) -> str:
    """datetime を「M月D日（曜）HH:MM」形式に変換"""
    wd = WEEKDAY_JP[dt.weekday()]
    return f"{dt.month}月{dt.day}日（{wd}）{dt.strftime('%H:%M')}"


def _pick_zodiac() -> str:
    import random
    return random.choice(ZODIAC_SIGNS)


def _pick_persona(personas: list, recent_persona_ids: list = None) -> dict | None:
    """ペルソナリストからランダムに1件選ぶ（直近使用済みは避ける）"""
    import random
    if not personas:
        return None
    recent_ids = set(recent_persona_ids or [])
    fresh = [p for p in personas if p.get("id") not in recent_ids]
    pool = fresh if fresh else personas
    return random.choice(pool)


def _select_pattern(patterns: list, recent_patterns: list, platform_hint: str = "threads", hour: int = 12) -> dict:
    import random
    blocked = set(recent_patterns)
    # プラットフォームに対応するパターンのみ
    available = [
        p for p in patterns
        if p.get("id") not in blocked and platform_hint in p.get("platforms", ["threads"])
    ]
    if not available:
        available = [p for p in patterns if platform_hint in p.get("platforms", ["threads"])]

    # 夜（20〜23時 JST）は yoru_kichi を3倍優先
    weights = []
    for p in available:
        w = p.get("weight", 1.0)
        if 20 <= hour <= 23 and p.get("id") == "yoru_kichi":
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

    hooks_sample = "\n".join(f"- {h}" for h in knowledge["hook_examples"][:8])

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

    # ── 推定投稿時刻ブロック ──────────────────────────────────────
    est_dt: datetime | None = extra_hint.get("estimated_post_time")
    if est_dt and isinstance(est_dt, datetime):
        est_date_str   = est_dt.date().isoformat()           # "2026-05-18"
        est_date_jp    = _format_datetime_jp(est_dt)         # "5月18日（月）10:00"
        est_tomorrow   = (est_dt.date() + timedelta(days=1)).isoformat()
        tm = est_dt + timedelta(days=1)
        est_tomorrow_jp = f"{tm.month}月{tm.day}日（{WEEKDAY_JP[tm.weekday()]}）"
        post_time_block = (
            f"## 推定投稿時刻（重要）\n"
            f"この投稿は **{est_date_jp} JST** に公開される予定です。\n"
            f"「今日」= {est_date_jp.split('（')[0]}、"
            f"「明日」= {est_tomorrow_jp}、"
            f"「昨日」= その前日 として投稿本文を書いてください。\n"
            f"生成時刻と投稿時刻はずれる場合があるため、必ずこの推定時刻を基準にすること。\n"
        )
    else:
        now_jst = datetime.now(JST)
        est_date_str  = now_jst.date().isoformat()
        est_tomorrow  = (now_jst.date() + timedelta(days=1)).isoformat()
        post_time_block = ""  # フォールバック：通常通り
    # ─────────────────────────────────────────────────────────────

    rewrite_block = ""
    if feedback_for_rewrite:
        rewrite_block = f"\n## 前回の添削フィードバック（必ず反映）\n{feedback_for_rewrite}\n"

    # パターン固有の追加ルール
    pattern_id = pattern.get("id", "")
    pattern_extra = ""
    if pattern_id == "pop_short":
        pattern_extra = """
## ポップ短文型の追加ルール
- 2〜5行に収める（80〜150文字）
- 語尾はカジュアルに（「〜だよ」「〜よね」「〜してみて」「〜かも」「〜ね」「〜！」）
- 絵文字を1〜3個使ってOK（末尾や強調部分に）
- 難しい専門用語より「ふとした気づき」として書く
- 読んで「あ、これ試せる」と思われる内容にする

### 心理効果
- **実行可能性効果**: 「今すぐできる」小さな行動提案が「試してみよう」を引き出す
- **親近感効果**: カジュアルな語尾・絵文字で距離感を縮め、フォローしたくなる人格を演出する
"""
    elif pattern_id == "zodiac_target":
        zodiac = extra_hint.get("zodiac", "♈牡羊座")
        pattern_extra = f"""
## 星座ターゲット型の追加ルール
- この投稿は「{zodiac}」の人向け
- 冒頭を「{zodiac}さんへ。」で始める
- その星座に合った吉方位・今週の運気・アドバイスを1〜2点伝える
- 100〜200文字のコンパクトな内容
- 語尾はやや親しみやすく（「〜してみてね」「〜がいいよ」など）

### 心理効果
- **スポットライト効果**: 「{zodiac}さんへ。」の呼びかけが「自分だけに話しかけられている」感覚を作る
- **バーナム効果**: 運気・アドバイスは星座らしさを保ちながら「まさに今の自分だ」と感じる言葉選びにする
"""
    elif pattern_id == "yoru_kichi":
        pattern_extra = f"""
## 夜の吉方位予告型の追加ルール
- この投稿は夜（20〜22時）に公開される。「明日」= {est_tomorrow_jp if est_dt else "翌日"} を指す
- 「明日（{est_tomorrow_jp if est_dt else "翌日"}）の吉方位」「{est_tomorrow_jp if est_dt else "明日"}動くなら」という内容にする
- 明日の具体的な方位（東・西・南・北・東南・西南など）を1〜2つ示す
- 朝の行動に活かせる具体的なアドバイスを添える（通勤・買い物・散歩など）
- 100〜200文字程度、夜に読んで翌朝試したくなる内容
- 語尾は「〜してみて」「〜がいいよ」などやさしいカジュアル調
"""
    elif pattern_id == "kaiun_day":
        # 推定投稿日を基準に開運日情報を取得（今日ではなく投稿される日）
        from core.kaiun_calendar import _load_cache
        cache = _load_cache()
        all_days = cache.get("days", [])

        # 推定投稿日に一致する開運日情報を検索
        target_info = next((d for d in all_days if d.get("date") == est_date_str), None)

        # 推定投稿日以降の直近開運日リスト
        upcoming = sorted(
            [d for d in all_days if d.get("date", "") >= est_date_str],
            key=lambda x: x["date"]
        )[:3]

        if target_info:
            lucky_types  = "・".join(target_info.get("lucky_types", []))
            best_actions = "、".join(target_info.get("best_actions", []))
            kichi_houi   = "・".join(target_info.get("kichi_houi", []))
            day_block = f"""
【投稿日（{est_date_jp if est_dt else est_date_str}）の開運情報】
- 日付: {target_info.get('date_jp', '')}（{target_info.get('weekday', '')}曜日）
- 干支: {target_info.get('kanshi', '')}
- 六曜: {target_info.get('rokuyou', '')}
- 開運日の種別: {lucky_types}
- おすすめ行動: {best_actions}
- 吉方位: {kichi_houi}
- まとめ: {target_info.get('summary', '')}
- 補足: {target_info.get('note', '')}"""
            day_label = "今日"
        else:
            # 投稿日は通常の日 → 直近の開運日を予告する形で紹介
            if upcoming:
                nxt = upcoming[0]
                lucky_types  = "・".join(nxt.get("lucky_types", []))
                best_actions = "、".join(nxt.get("best_actions", []))
                day_block = f"""
【直近の開運日（投稿日は通常の日）】
- 日付: {nxt.get('date_jp', '')}（{nxt.get('weekday', '')}曜日）
- 干支: {nxt.get('kanshi', '')} / 六曜: {nxt.get('rokuyou', '')}
- 開運日の種別: {lucky_types}
- おすすめ行動: {best_actions}
- まとめ: {nxt.get('summary', '')}"""
                day_label = f"{nxt.get('date_jp', '')}（{nxt.get('weekday', '')}）"
            else:
                day_block = "（開運日データ取得中）"
                day_label = "直近の開運日"

        upcoming_lines = "\n".join(
            f"  - {d.get('date_jp','')}（{d.get('weekday','')}）: {', '.join(d.get('lucky_types', []))} ★{d.get('lucky_score',1)}"
            for d in upcoming[:3]
        )

        pattern_extra = f"""
## 開運日型の追加ルール
{day_block}

【参考：直近の開運日一覧】
{upcoming_lines}

- 上記の正確な暦情報を必ず使って投稿を生成すること（日付・開運日種別・干支を正確に記載）
- 投稿日が開運日の場合は「今日は○○の日」で始める
- 投稿日が通常日の場合は「{day_label}は○○の日」として予告型で書く
- 開運日に行うべき具体的な行動を1〜2つ提示する
- 150〜250文字程度のテンポよい内容
- 語尾はカジュアルでOK（「〜してみて」「〜がいいよ」）

### 心理効果
- **希少性・時限性**: 「この日だけ」「今日中に動いて」という限定表現が行動を後押しする
- **損失回避**: 「この開運日を逃すと次は〇日後」という表現が「今動かないとまずい」感覚を生む
"""
    elif pattern_id == "caligula":
        zodiac = extra_hint.get("zodiac", "")
        zodiac_hint = f"今回のターゲット星座: {zodiac}" if zodiac else ""
        pattern_extra = f"""
## カリギュラ型の必須構造（3段構成）

### ① 制限ターゲット（冒頭）
特定の状況・タイプ・星座を絞り込む。
ただし「制限に根拠を持たせる」こと——読んだ人が
「なるほど、自分にはこれが必要な理由がわかる」と感じる制限にする。

良い絞り込み例:
- 「吉方位を知っているだけで動いていない人は、この先を読まないほうがいい（読むと動きたくなるから）」
- 「今うまくいっていると思っている人は読まなくていい（これは停滞を感じている人の話）」
- 「{zodiac}さんは、今週これを知ると動きたくなるから覚悟がある人だけ読んで」
- 「転職を考えているのに一歩踏み出せない人以外は読まなくていい」
{zodiac_hint}

### ② 制限の理由・根拠（中盤）
なぜその人だけに読ませたいのか、具体的な根拠を示す。
「この話は〇〇な状況の人にだけ刺さる」という理由が本文に自然に現れること。

例:
- 「運気の底にいる人ほど、次の行動を間違えやすい。それを防ぐ話をする」
- 「獅子座は今週、南の気が特に強く出ていて、方向を一つ間違えると空回りしやすい配置だから」
- 「知識だけある人が最も損をする理由が、龍脈命術の視点から説明できるから」

### ③ 核心の情報（本文）
制限の理由と一致した、具体的で価値ある情報を届ける。
「なるほど、だから自分向けだったのか」と読んだ人が納得できる内容にする。

## 構成チェックリスト
- [ ] 制限ターゲットと本文内容が論理的につながっているか
- [ ] 「なぜその人に向けているか」の理由が本文から読み取れるか
- [ ] 読んだ人が「これ、自分のことだ」と感じる具体性があるか
- 150〜250文字、改行を効果的に使う

### 心理効果
- **カリギュラ効果**: 「読まなくていい」という制限が逆に「読みたい」衝動を生む
- **好奇心ギャップ**: 制限することで「自分には隠された情報がある」という期待感を高める
- **ラベリング効果**: 「〜な人」と絞ることで、読者が「自分はその一人だ」と帰属意識を持つ
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
            pattern_extra = f"""
## メニュー案内型の追加ルール
- 鑑定ページURL: {service_url}
- 必ず以下の正確なメニュー名・価格・説明を使うこと（変更・省略・追加禁止）:

【鑑定メニュー】
─────────────
{menu_lines}

- 冒頭に一言「こんな方に使ってほしい」という導入を添えてから一覧を出す
- 最後は「詳細・お申し込みは {service_url}」で締める
- 200〜400文字を目安に
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
    elif pattern_id == "ranking":
        pattern_extra = f"""
## ランキング型の追加ルール

### 心理効果（必ず活用）
- **スポットライト効果**: 読者が「自分の順位は何位？」と自分事化するタイトル
- **ツァイガルニク効果**: 「続きが気になる」構造で最後まで読ませる
- **バーナム効果**: ランキング項目は誰でも1〜2つ当てはまる内容にする

### 構成
1. **タイトル行（1行）**: 「〇〇 TOP5」「絶対やってはいけない〇〇 ワースト3」「〜している人の特徴」など
2. **ランキングリスト（3〜5項目）**: 各項目に短い補足説明を添える（「なぜその順位か」が伝わると良い）
3. **コメント誘発（最終行）**: 「いくつ当てはまった？」「何位だった？コメントで教えて」「自分は〇位だと思う人はいいね」

### テーマ例（今回のテーマに合わせて選ぶ）
- 運気が上がる習慣ランキング / 金運を下げる行動ワースト / 吉方位の効果が出やすい行動ランキング
- 運気が下がり始めるサイン / 風水的に要注意な部屋の特徴 / 転換期に入る人の共通点

### 数・文字数
- ランキングは3〜5位（多すぎない）
- 全体200〜350文字
- 最後は必ず「コメントで教えて」「いくつ当てはまった？」などの問いかけで締める
"""
    elif pattern_id == "engagement_hook":
        pattern_extra = f"""
## 行動喚起型の追加ルール

### 心理効果（必ず活用）
- **バーナム効果**: 「なんとなく〜な感じ」「〜の気がする」という曖昧だが誰でも共感できる状態描写で「自分のことだ」と思わせる
- **ラベリング効果**: 「〜な人」「〜している人」と読者にラベルを貼り、帰属意識を生む
- **行動の明確化**: いいね・フォロー・保存のうち1つに絞って明確に指示する（複数指示は効果半減）

### 構成
1. **共感フック（1〜2行）**: 読者の内面状態を言語化。「言いたかったけど言えなかった感情」を代弁する
2. **龍脈命術的な視点（2〜3行）**: なぜその状態が起きているか、方位・気の観点から短く説明
3. **行動指示（1行）**: 「これ、わかる人はいいねして」「もっと知りたい人はフォローして」「保存して後で読み返して」のどれか1つ

### 使える行動指示の型
- いいね: 「わかる人はいいね」「うなずいた人はいいねして」
- フォロー: 「毎日こういう情報が欲しい人はフォローして待ってて」
- コメント: 「今の状況、〇か✕でコメントして」「数字で教えて（例：3つ当てはまった）」
- 保存: 「後で読み返したい人は保存して」（有益情報系に使う）

### 文字数・構成
- 120〜220文字
- 1行目は「」（カギカッコ）で読者の内言を引用するか、問いかけで始める
- 鑑定CTA は今回は不要（フォロー促進に集中）
"""
    elif pattern_id == "self_check":
        pattern_extra = f"""
## 自己診断型の追加ルール

### 心理効果（必ず活用）
- **バーナム効果**: チェック項目は「自分だけじゃなく多くの人が当てはまる」が「自分のことを言い当てられた」と感じる内容
- **ツァイガルニク効果**: 「3つ以上の人は〜」という結果を見るまでリストを読み続けさせる
- **自己開示効果**: 「自分のことをわかってもらえた」体験が信頼とフォローにつながる

### 構成
1. **タイトル行**: 「〇〇のサイン」「〇〇している人の特徴」など診断対象を明示
2. **チェックリスト（4〜6項目）**: □ 記号で始め、内面的な状態・行動・感覚を書く
3. **結果判定（2〜3段階）**: 当てはまる数ごとに意味を伝える
   - 例: 「1〜2個：気の流れが少し詰まっているかも」
   - 例: 「3〜4個：今が転換期の入口。動く方向を整えて」
   - 例: 「5個全部：鑑定で一度確認してみて → {service_url}」
4. **鑑定CTA（自然に埋め込む）**: 5個全部または3個以上の判定に「鑑定で確認を」を入れる

### チェック項目の書き方
- 「□ 〜している」「□ なんとなく〜な気がする」「□ 〜なのに〜できない」
- 行動より「内面の感覚」「ぼんやりした違和感」を書く（バーナム効果を高める）
- 「はっきりNO」と言える人が少ない表現を選ぶ

### 文字数
- 250〜400文字
- 最後の結果判定で必ず鑑定ページURLを自然に添える
- コメント誘発「いくつ当てはまった？」を最後に添えてもOK
"""
    elif pattern_id == "follow_cta":
        pattern_extra = """
## フォロー促進型の追加ルール
- 「このアカウントでは〇〇を届けています」という"何を提供しているか"を具体的に伝える
- 届けている情報の種類を3〜5つ箇条書きで示す（吉方位・開運日・星座別情報・風水ヒントなど）
- 「フォローしてね」「フォローして待っててね」など自然な一言でフォローを促す
- 強引な誘導や「絶対フォロー」「今すぐ」などの煽り文句は使わない
- 誰のためのアカウントか（転職・引越し・恋愛など岐路に立つ人）を1行で添える
- 150〜280文字、箇条書きを活かしてスキャンしやすい構成にする
- 語尾はやや柔らかく（「〜します」より「〜しています」「〜してね」）

### 心理効果
- **FOMO（見逃し恐怖）**: 「毎日届く情報を見逃したくない」という感覚がフォローを促す
- **価値の先出し**: フォローする「理由」を先に明示することで、フォローへの心理ハードルを下げる
"""
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
        persona = extra_hint.get("persona") if extra_hint else None
        if persona:
            gender_label = "女性" if persona.get("gender") == "female" else "男性"
            tags_str = "・".join(persona.get("tags", []))
            tried_str = "／".join(persona.get("tried", [])[:2])
            persona_block = f"""
【今回使用するペルソナ情報】
- 年齢・性別・職業: {persona.get('age')}歳・{gender_label}・{persona.get('occupation')}
- 悩み: {persona.get('struggle', '')}
- 試してきたこと: {tried_str}
- 感情: {persona.get('emotion', '')}
- きっかけ: {persona.get('trigger', '')}
- 鑑定で判明したこと: {persona.get('revelation', '')}
- 行動: {persona.get('action', '')}
- 結果: {persona.get('result', '')}
- タグ: {tags_str}"""
        else:
            persona_block = "（ペルソナデータなし：自由にエピソードを作成）"

        pattern_extra = f"""
## ペルソナ鑑定エピソード型の追加ルール
{persona_block}

### 構成（この順番で書く）
1. **導入（1〜2行）**: 「先日、○○の方を鑑定しました」または「こんな相談が来ました」で始める
2. **悩みの共感（2〜3行）**: 当事者が感じていた苦しさ・焦り・疑問を具体的に描写（「なのに」「なぜか」など逆接を使う）
3. **転機（1〜2行）**: 鑑定で何がわかったか。「その違和感の正体は〇〇でした」という形で明かす
4. **行動と結果（1〜2行）**: 取った行動とその後の変化を簡潔に
5. **メッセージ（1行）**: 読者への普遍的なメッセージ（「頑張りが足りないんじゃない。方向がずれていただけ」など）
6. **CTA（最後）**: 「▷ 鑑定ページ {service_url}」を添える

### ルール
- 250〜400文字（CTAのURL含む）
- 実名・特定できる個人情報は使わない（「30代のWebマーケターの方」のように匿名化）
- 大げさな言葉（「人生が変わった」「奇跡」）は避け、具体的な事実（数字・行動）で語る
- 読んだ人が「自分と同じかも」と感じる悩みの描写を丁寧に
- 鑑定を押しつけない。最後のCTAは「気になる方はどうぞ」スタンス

### 心理効果
- **社会的証明**: 「自分と似た状況の人が変わった」という具体的事例が信頼と行動意欲を高める
- **感情移入 × 代理体験**: 悩みの描写が「自分のこと」と一致するほど「相談したい」という欲求が高まる
- **解決可能性の提示**: 「この人も変われたなら自分も変われるかも」という希望が鑑定への動機になる
"""

    elif pattern_id == "ichi_gon":
        pattern_extra = """
## 一言型の追加ルール
- 1〜3行の断言型短文（80〜120文字）
- 1行目だけで「何かが変わる気がする」と思わせる言葉の密度にする
- 説明しすぎない。余白が読者の想像力を引き出す

### 心理効果
- **アンカリング**: 断言口調（「〜だ」「〜に動く」）が「真実」として脳に刻まれやすい
- **余白効果**: 短いほど読者が自分なりの解釈で補完し「自分に向けられた言葉」に変換される
- **完結効果**: 1〜3行で完結した文は記憶に残りやすく、スクショ・保存されやすい
"""
    elif pattern_id == "hoshi_betsu":
        pattern_extra = """
## 星座別情報型の追加ルール
- 12星座すべての吉方位または今週の運気を一覧で示す
- 各星座2〜3行で、方位と具体的な行動ヒントをセットで
- 最後に「自分の星座チェックして保存しておいて」でフォロー・保存を促す

### 心理効果
- **スポットライト効果**: 読者は一覧を全部読まず「自分の星座」だけを探す。その瞬間「自分だけへの情報」と感じる
- **バーナム効果**: 各星座の記述は「その星座らしさ」を保ちながら、誰でも当てはまる普遍的な内容にする
- **保存行動誘発**: 全星座一覧は「後で見返したい」コンテンツ。「保存して見返してね」を末尾に添える
"""
    elif pattern_id == "list":
        pattern_extra = """
## リスト型の追加ルール
- 「〇〇な人の特徴」「〇〇すること3つ」など箇条書きで3〜5項目
- 各項目に1行の補足説明を添えると信頼性が上がる
- 「いくつ当てはまった？」でコメントを誘発してOK

### 心理効果
- **バーナム効果**: 「〇〇な人の特徴」は、読者が1〜2個当てはまれば「自分のことだ」と感じる。完全一致は不要
- **ツァイガルニク効果**: 「これ自分かも」と思ったら最後まで読む。最初の項目を「あるある」で始めて引き込む
- **感情的対比**: 「やっている人 vs やっていない人」「上がる vs 下がる」対比構造が反応を生む
"""
    elif pattern_id == "episode":
        pattern_extra = """
## 鑑定エピソード型の追加ルール
- 「先日、〜の方を鑑定しました」で始める
- 相談内容・鑑定で判明したこと・行動・結果の流れで構成
- 150〜300文字。詳細を伏せて「続きは鑑定で」と示唆してもOK

### 心理効果
- **社会的証明**: 実際の相談事例（架空でも可）は「自分以外にも同じ悩みを持つ人がいる」という安心感を与える
- **感情移入 × 代理体験**: 相談者の悩みを「自分の悩みと一致する言葉」で描写し、読者が「自分も相談したい」と思う状態を作る
- **解決可能性の提示**: 「この人が変われたなら自分も変われるかも」という希望が鑑定への動機になる
"""
    elif pattern_id == "trivia":
        pattern_extra = """
## 豆知識型の追加ルール
- 「知らなかった！」「へえ」となる風水・占星術の豆知識を1つ深掘りする
- 「実は〜」「意外にも〜」で始めて驚きを演出する
- 最後に「これ知ってた？」など問いかけを添えてコメントを促してOK

### 心理効果
- **驚き効果（サプライズ）**: 「知らなかった！」という発見の瞬間が保存・シェアを誘発する
- **知識共有欲**: 「誰かに教えたい」と思わせる面白さ・実用性が拡散につながる
- **段階的開示**: 「なぜか」の答えを少し引っ張ってから明かすことで最後まで読ませる
"""
    elif pattern_id == "kyokan":
        pattern_extra = """
## 共感・問いかけ型の追加ルール
- 読者の内面状態を言語化して冒頭に置く（「」カギカッコで引用形式にするとより効果的）
- 共感から入り、龍脈命術的な視点で「だからそうなんだ」と解説する
- 末尾は「わかる人いる？」「これ、あなたのこと？」などの問いかけで締める

### 心理効果
- **バーナム効果**: 「なんとなく〜」「〜な気がする」という曖昧な表現が「自分のことだ」という感覚を生む
- **返報性の原理**: 問いかけられると「答えたい」という心理が働き、コメント・リプライが生まれやすい
- **帰属意識**: 「こういう人いる？」「わかる人だけわかればいい」でその感覚を持つ人の仲間意識を形成する
"""
    elif pattern_id == "world_view":
        pattern_extra = """
## 世界観投稿型の追加ルール
- 「なぜ私が占いをするのか」「龍脈命術とは何か」など時雨としての信念・想いを語る
- 説明しすぎず、少し「続きが気になる」部分を残して締める
- 末尾に「占術家 時雨」のシグネチャを使う

### 心理効果
- **自己開示効果**: 占術家・時雨の本音・葛藤・信念を語ることで「人間らしさ」が生まれ、信頼と親近感が高まる
- **差別化の明確化**: 「普通の占いとは違う」という対比が世界観への共感を深め、ファン化につながる
- **余白効果**: 語りすぎない、説明しきらない部分が「もっと知りたい」というフォロー動機を生む
"""

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
{rewrite_block}{pattern_extra}
## 生成ルール
- 文字数: 80〜400文字（星座一覧は450文字まで可）。必ずこの範囲に収めること。
- 口調は「時雨」として一貫させる
- 1行目は必ずインパクトがある一文
- 風水・吉方位・龍脈命術の世界観を自然に織り込む
- 語尾に少しポップさを持たせる（「〜だよ」「〜よね」「〜してみて」なども自然に使う）
- 最後の誘導文（「プロフィールから」等）は週1回のメニュー案内型のみ
- 短文型（一言型・ポップ短文型）は5行以内
- 「占術家 時雨」のシグネチャは世界観投稿・エピソード投稿のみ末尾に追加
- 絵文字は基本0〜2個（ポップ短文型のみ最大3個）
- NGワードは使わない
- 「今日」「明日」「今週」などの時間表現は必ず推定投稿時刻を基準にすること

## 出力
投稿本文のみ。説明・メタ情報は不要。"""


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
            text = block.text.strip()
            if len(text) < 50:
                return ""  # 短すぎる場合は不完全な生成として棄却
            return text
    return ""


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
    checker.load_history([h.get("text", "") for h in history if h.get("text")])

    recent_patterns = _get_recent_patterns(history)
    recent_themes = _get_recent_themes(history)
    todays_schedule = _get_todays_schedule()
    jst_hour = _get_jst_hour()

    # 現在のキュー長を取得（投稿予定時刻の推定に使用）
    current_queue = load_json(DATA_DIR / "post_queue.json", default=[])
    current_queue_len = len(current_queue)

    # ペルソナの直近使用IDを追跡（重複を避ける）
    recent_persona_ids = [
        h.get("persona_id") for h in history[-20:]
        if h.get("persona_id")
    ]

    queued_threads = 0
    queued_x = 0
    rejected = 0
    details = []

    for i in range(batch_size):
        # スケジュールヒントを使ってテーマ選択（曜日固定コンテンツ）
        schedule_hint = todays_schedule[i % len(todays_schedule)]
        theme = _select_theme(knowledge["theme_tree"], recent_themes, schedule_hint)
        pattern = _select_pattern(knowledge["patterns"], recent_patterns, hour=jst_hour)

        # 推定投稿時刻（キュー長 + バッチ内位置 から計算）
        est_post_time = _estimate_post_time(current_queue_len, i)

        # パターン固有のヒント（星座を使うパターンは対象星座を決定）
        extra_hint = {"estimated_post_time": est_post_time}
        if pattern.get("id") in ("zodiac_target", "caligula"):
            extra_hint["zodiac"] = _pick_zodiac()
        elif pattern.get("id") == "persona_episode":
            persona = _pick_persona(knowledge.get("personas", []), recent_persona_ids)
            if persona:
                extra_hint["persona"] = persona
                recent_persona_ids.append(persona.get("id"))

        def rewrite_func(post_text: str, feedback: str, _p=pattern, _eh=extra_hint) -> str:
            return _generate_post(
                client_obj, _p, theme, knowledge,
                research_data.get("topics", []), analyst_feedback,
                feedback_for_rewrite=feedback, extra_hint=_eh,
            )

        # 初回生成
        first_draft = _generate_post(
            client_obj, pattern, theme, knowledge,
            research_data.get("topics", []), analyst_feedback,
            extra_hint=extra_hint,
        )
        if not first_draft:
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

        # 類似度チェック
        is_dup, sim_score = checker.is_duplicate(final_post)
        if is_dup:
            rejected += 1
            details.append({"status": "rejected_similarity", "similarity": sim_score, "theme": theme})
            continue

        # Threads版・X版を生成
        threads_text = format_for_threads(final_post)
        hashtags = get_x_hashtags_for_theme(theme)
        x_text = format_for_x(final_post, hashtags)

        post_obj = {
            "text": threads_text,
            "x_text": x_text,
            "theme": theme,
            "pattern_id": pattern.get("id"),
            "quality_score": result["score_result"]["average"],
            "attempts": result["attempts"],
        }
        # ペルソナIDを記録（重複防止のため）
        if extra_hint.get("persona"):
            post_obj["persona_id"] = extra_hint["persona"].get("id")

        # Threadsキューに追加
        enqueue_post(post_obj)
        queued_threads += 1

        # X対応パターンならXキューにも追加
        if "x" in pattern.get("platforms", []):
            enqueue_x_post(post_obj)
            queued_x += 1

        checker.add_to_corpus(final_post)
        recent_patterns.append(pattern.get("id"))
        recent_themes.append(theme)

        details.append({
            "status": "queued",
            "score": result["score_result"]["average"],
            "theme": theme,
            "pattern": pattern.get("name"),
            "x_queued": "x" in pattern.get("platforms", []),
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
