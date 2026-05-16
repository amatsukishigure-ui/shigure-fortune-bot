"""
ライターエージェント（時雨 / 龍脈命術）

リサーチデータとアナリストのフィードバックを元に投稿を生成する。
品質スコア・類似度チェックを経て、Threads/X 両方のキューに追加する。
"""

import json
import anthropic
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    ANTHROPIC_API_KEY, MODEL_HEAVY,
    MAX_QUALITY_RETRIES, MAX_SAME_PATTERN_CONSECUTIVE, MAX_SAME_THEME_CONSECUTIVE,
    ACCOUNT_PROFILE_FILE, POST_PATTERNS_FILE, THEME_TREE_FILE, HOOK_EXAMPLES_FILE,
    CONTENT_SCHEDULE, DATA_DIR,
)
from core.state import (
    load_json, load_history, load_research, enqueue_post,
)
from core.similarity import SimilarityChecker
from core.quality import score_with_retry
from core.formatter import format_for_x, format_for_threads, get_x_hashtags_for_theme
from agents.x_poster import enqueue_x_post


def _load_knowledge() -> dict:
    return {
        "profile": load_json(ACCOUNT_PROFILE_FILE, default={}),
        "patterns": load_json(POST_PATTERNS_FILE, default=[]),
        "theme_tree": load_json(THEME_TREE_FILE, default={}),
        "hook_examples": load_json(HOOK_EXAMPLES_FILE, default=[]),
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


def _get_jst_hour() -> int:
    from datetime import timezone, timedelta
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).hour


def _pick_zodiac() -> str:
    import random
    return random.choice(ZODIAC_SIGNS)


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
"""
    elif pattern_id == "yoru_kichi":
        pattern_extra = """
## 夜の吉方位予告型の追加ルール
- 「明日の吉方位」「明日動くなら」という夜向けの内容にする
- 明日の具体的な方位（東・西・南・北・東南・西南など）を1〜2つ示す
- 朝の行動に活かせる具体的なアドバイスを添える（通勤・買い物・散歩など）
- 100〜200文字程度、夜に読んで翌朝試したくなる内容
- 語尾は「〜してみて」「〜がいいよ」などやさしいカジュアル調
"""
    elif pattern_id == "kaiun_day":
        # キャッシュから今日・直近の開運日情報を取得
        from core.kaiun_calendar import get_today_lucky_info, get_upcoming_lucky_days
        today_info = get_today_lucky_info()
        upcoming   = get_upcoming_lucky_days(n=3)

        if today_info:
            lucky_types = "・".join(today_info.get("lucky_types", []))
            best_actions = "、".join(today_info.get("best_actions", []))
            kichi_houi   = "・".join(today_info.get("kichi_houi", []))
            today_block  = f"""
【今日の開運情報（正確な暦に基づく）】
- 日付: {today_info.get('date_jp', '')}（{today_info.get('weekday', '')}曜日）
- 干支: {today_info.get('kanshi', '')}
- 六曜: {today_info.get('rokuyou', '')}
- 開運日の種別: {lucky_types}
- おすすめ行動: {best_actions}
- 吉方位: {kichi_houi}
- まとめ: {today_info.get('summary', '')}
- 補足: {today_info.get('note', '')}"""
        else:
            # 今日は開運日でない場合→直近の開運日を紹介
            if upcoming:
                nxt = upcoming[0]
                lucky_types  = "・".join(nxt.get("lucky_types", []))
                best_actions = "、".join(nxt.get("best_actions", []))
                today_block  = f"""
【直近の開運日（今日は通常の日）】
- 日付: {nxt.get('date_jp', '')}（{nxt.get('weekday', '')}曜日）
- 干支: {nxt.get('kanshi', '')} / 六曜: {nxt.get('rokuyou', '')}
- 開運日の種別: {lucky_types}
- おすすめ行動: {best_actions}
- まとめ: {nxt.get('summary', '')}"""
            else:
                today_block = "（開運日データ取得中）"

        upcoming_lines = "\n".join(
            f"  - {d.get('date_jp','')}（{d.get('weekday','')}）: {', '.join(d.get('lucky_types', []))} ★{d.get('lucky_score',1)}"
            for d in upcoming[:3]
        )

        pattern_extra = f"""
## 開運日型の追加ルール
{today_block}

【直近の開運日（参考）】
{upcoming_lines}

- 上記の正確な暦情報を必ず使って投稿を生成すること（日付・開運日種別・干支を正確に記載）
- 「今日は○○の日」または「○日は○○の日」という形で開始する
- 開運日に行うべき具体的な行動を1〜2つ提示する
- 「今日中に動いて」「この日だけは試して」という即時行動を促す
- 150〜250文字程度のテンポよい内容
- 語尾はカジュアルでOK（「〜してみて」「〜がいいよ」）
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
"""
    elif pattern_id in ("menu_guide", "cta_soft"):
        pattern_extra = f"""
## 鑑定誘導型の追加ルール
- 鑑定ページURL: {service_url}
- 本文で価値ある気づきを1つ提供してから誘導する（いきなり告知しない）
- 最後に「▷ 鑑定ページ {service_url}」を自然に添える
- 押しつけず「気になる方はどうぞ」のスタンスを保つ
- cta_soft は150〜250文字、menu_guide は200〜350文字を目安に
- 「無料」「初回無料」という言葉は使ってよい
"""

    return f"""あなたは占術家「時雨（しぐれ）」として、Threadsに投稿するテキストを1本生成してください。

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

    queued_threads = 0
    queued_x = 0
    rejected = 0
    details = []

    for i in range(batch_size):
        # スケジュールヒントを使ってテーマ選択（曜日固定コンテンツ）
        schedule_hint = todays_schedule[i % len(todays_schedule)]
        theme = _select_theme(knowledge["theme_tree"], recent_themes, schedule_hint)
        pattern = _select_pattern(knowledge["patterns"], recent_patterns, hour=jst_hour)

        # パターン固有のヒント（星座を使うパターンは対象星座を決定）
        extra_hint = {}
        if pattern.get("id") in ("zodiac_target", "caligula"):
            extra_hint["zodiac"] = _pick_zodiac()

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
