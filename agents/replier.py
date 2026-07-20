"""
リプライエージェント（時雨 / 龍脈命術）

直近の投稿に届いたコメントを取得し、時雨として返信する。
自分の返信に届いた再コメントにも soft closing 返信する。
返信済みコメントIDを記録して二重返信を防ぐ。
1日1回 daily 実行から呼ばれる。
"""

import json
import random
import time
import requests
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import anthropic
from config import (
    ANTHROPIC_API_KEY, MODEL_LIGHT,
    THREADS_ACCESS_TOKEN, THREADS_USER_ID,
    ACCOUNT_PROFILE_FILE,
)
from core.state import (
    load_history, load_json, load_replied_comments, mark_comment_replied,
    load_our_reply_ids, save_our_reply_id,
)


THREADS_API_BASE = "https://graph.threads.net/v1.0"

# 1回の実行で返信する最大件数（スパム防止）
MAX_REPLIES_PER_RUN = 10
# 何日以内の投稿のコメントを対象にするか
REPLY_TARGET_DAYS = 14

# 悩みキーワード（これを含むコメントは鑑定への自然な誘導を追加する）
_CONSULT_KEYWORDS = {
    "転職", "仕事", "引越", "引っ越", "恋愛", "停滞", "金運", "お金",
    "独立", "副業", "人間関係", "離婚", "結婚", "昇進", "辞め", "変えた",
}


def _has_consult_signal(text: str) -> bool:
    return any(kw in text for kw in _CONSULT_KEYWORDS)


def _get_replies(thread_id: str) -> list:
    """スレッドIDへの返信一覧を取得する（投稿にもコメントにも使える）"""
    if not THREADS_ACCESS_TOKEN:
        return []
    url = f"{THREADS_API_BASE}/{thread_id}/replies"
    params = {
        "fields": "id,text,username,timestamp",
        "access_token": THREADS_ACCESS_TOKEN,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if not resp.ok:
            body = resp.json() if resp.content else {}
            err = body.get("error", {})
            print(f"  返信取得失敗 [{thread_id}] HTTP{resp.status_code}: "
                  f"{err.get('message', resp.text[:120])}")
            if err.get("code") in (200, 10) or err.get("type") == "OAuthException":
                print("  ⚠️  アクセストークンに threads_read_replies 権限が必要です")
                return []
            return []
        return resp.json().get("data", [])
    except Exception as e:
        print(f"  返信取得エラー [{thread_id}]: {e}")
        return []


def _post_reply(comment_id: str, text: str) -> str | None:
    """
    コメントへの返信を投稿する。
    成功したら投稿されたスレッドID（str）を返す。失敗したら None。
    """
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        print(f"  [MOCKリプライ] → {text[:50]}...")
        return "mock_reply_id"

    container_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    try:
        resp = requests.post(container_url, data={
            "media_type": "TEXT",
            "text": text,
            "reply_to_id": comment_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=10)
        resp.raise_for_status()
        container_id = resp.json().get("id")

        publish_url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
        resp = requests.post(publish_url, data={
            "creation_id": container_id,
            "access_token": THREADS_ACCESS_TOKEN,
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as e:
        print(f"  リプライ投稿エラー: {e}")
        return None


_CARD_PICK_CONTEXT = {
    "A":  ("🌊", "流れに乗りたいが踏み出せていない状態",   "北東〜東（流れの気）"),
    "B":  ("🔥", "動き始めた・加速したい状態",             "南〜東南（躍動の気）"),
    "C":  ("🌿", "いったん整えてじっくり動きたい状態",     "西〜北西（受け取りの気）"),
    "🌊": ("🌊", "流れに乗りたいが踏み出せていない状態",   "北東〜東（流れの気）"),
    "🔥": ("🔥", "動き始めた・加速したい状態",             "南〜東南（躍動の気）"),
    "🌿": ("🌿", "いったん整えてじっくり動きたい状態",     "西〜北西（受け取りの気）"),
}

# 返信スタイルのバリエーション（毎回ランダムに選ぶ）
_REPLY_STYLES_CARD = [
    "相手の状態を受け止め→方位と今日できる1アクション→軽い問いかけ",
    "その選択肢を選んだ理由を想像して共感→龍脈的な見立て1行→続きを促す",
    "「その感覚、わかるな」という共鳴から入り→方位アドバイスを短く→何か変化があったら聞かせてという締め",
    "状態を一言で言い当てる（「ちょうど転換点にいる感じ」など）→龍脈的理由1行→問いかけ",
]


def _is_card_pick_comment(text: str) -> str | None:
    """A/B/C または 🌊🔥🌿 のカード回答か判定。該当キーを返す、なければ None"""
    t = text.strip().upper()
    for key in ("A", "B", "C", "🌊", "🔥", "🌿"):
        if t == key or t == key.upper():
            return key
    for key in ("A", "B", "C", "🌊", "🔥", "🌿"):
        if t.startswith(key) and len(t) <= len(key) + 5:
            return key
    return None


def _generate_card_pick_reply(choice_key: str, comment_text: str, profile: dict) -> str:
    ctx = _CARD_PICK_CONTEXT.get(choice_key, ("🔮", "気の流れを確認したい状態", "東〜南東"))
    emoji, state_desc, direction = ctx
    style = random.choice(_REPLY_STYLES_CARD)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
カード選択の投稿に「{comment_text}」というコメントが届きました。

【選ばれたカード】
絵文字: {emoji}
選んだ人の状態: {state_desc}
龍脈的な吉方位: {direction}

【今回の返信スタイル】
{style}

【ルール】
- 60〜110字で完結させる
- 「ありがとうございます」は使わない
- {emoji} の絵文字を冒頭1文字で使う（それ以外の絵文字は使わない）
- コメント内容に何か具体的な言葉があれば1語拾って受け止める（なければ状態を受け止める）
- 龍脈命術・方位・気の流れの視点を必ず含める（でも説明口調にしない）
- 末尾に「何か変化あったら教えてね」「どんな感じだった？」など軽い問いかけを入れる
- LINEへの誘導は不要（この返信では入れない）
- 語尾は「〜だよ」「〜ね」「〜かも」「〜かな」など柔らかく

返信文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


_HESITATION_CONTEXT = {
    "A": ("💰", "お金・料金の心配", "LINE相談は無料であること、申込み不要で気軽に話せること"),
    "B": ("🔮", "本当に変わるか・効果への不安", "変化した人の経験を自分の言葉で語り、変わらなくても正直に言うという誠実さ"),
    "C": ("💬", "何を話せばいいかわからない", "生年月日だけでいい・何も準備しなくていい・話しながら一緒に見えてくる"),
}

_REPLY_STYLES_HESITATION = [
    "不安を受け止め→その不安の解消を1行→LINEへの自然な誘い",
    "「その気持ち、わかる」から始め→ハードルを下げる具体的な一言→最後に誘導",
    "相手の不安を一言で言い当てる→「だから〇〇で大丈夫」という安心の根拠→LINE案内",
    "共感から入り→逆転の視点1行（不安な人ほど向いてる、など）→誘導",
]


def _is_hesitation_comment(text: str) -> str | None:
    """hesitation_break の A/B/C 回答コメントを検出。該当キーを返す、なければ None"""
    t = text.strip().upper()
    for key in ("A", "B", "C"):
        if t == key:
            return key
    for key in ("A", "B", "C"):
        if t.startswith(key) and len(t) <= len(key) + 5:
            return key
    # キーワード補完（より長い自然語コメント向け）
    if any(kw in text for kw in ("値段", "料金", "費用", "いくら")):
        return "A"
    if any(kw in text for kw in ("本当に変わる", "効果あるか", "変わるか不安")):
        return "B"
    if any(kw in text for kw in ("何を話", "話せばいい", "何を言")):
        return "C"
    return None


def _generate_hesitation_reply(choice_key: str, profile: dict) -> str:
    ctx = _HESITATION_CONTEXT.get(choice_key, ("🔮", "何となく踏み出せない気持ち", "話しながら一緒に見えてくることを伝える"))
    emoji, concern_desc, resolution_hint = ctx
    style = random.choice(_REPLY_STYLES_HESITATION)
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
「{concern_desc}」という不安を持つ人が選択肢を選びました。LINEへの相談に背中を押す返信を書いてください。

【今回の返信スタイル】
{style}

【伝えたいこと（自然に含める）】
{resolution_hint}

【ルール】
- 70〜120字で完結させる
- {emoji} の絵文字を冒頭1文字で使う
- 押しつけがましくなく、温かく、迷っている人に寄り添う
- 最後に「LINE {line_url}」へ自然に誘導する（URL丸出しでも可）
- 「ありがとうございます」は使わない
- 語尾は「〜だよ」「〜ね」「〜から」「〜していい」など柔らかく

返信文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _generate_reply(comment_text: str, post_text: str, profile: dict, pattern_id: str = "") -> str:
    """
    コメントへの時雨らしい返信を生成する。
    短く（60〜130字）、押しつけがましくなく、温かく。
    悩みキーワードがあれば自然に鑑定へ誘導する。
    """
    # card_pick: A/B/C コメントには専用返信
    if pattern_id == "card_pick":
        choice = _is_card_pick_comment(comment_text)
        if choice:
            return _generate_card_pick_reply(choice, comment_text, profile)

    # hesitation_break: 踏み切れない理由 A/B/C には専用返信
    if pattern_id == "hesitation_break":
        choice = _is_hesitation_comment(comment_text)
        if choice:
            return _generate_hesitation_reply(choice, profile)

    has_signal = _has_consult_signal(comment_text)
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")

    cta_rule = (
        f"- 返信の最後の1行に「もし詳しく話したい時はプロフィールのリンクから相談できるよ」"
        f"または「気になることがあれば LINE {line_url} でも聞けるよ」を自然に1行添える"
        f"（コメントの流れを壊さない程度にさりげなく）"
        if has_signal
        else "- 鑑定への直接誘導は不要（自然な会話の中で信頼を積む）"
    )

    _REPLY_STYLES = [
        "受け止め共感→龍脈の視点で状況を一言→短い問いかけ",
        "コメントの1語を引用して受け止め→方位・気の流れ解釈1行→余韻のある締め",
        "驚きや嬉しさを最初に→その人の状況に合った気の流れ解釈→「何か変化あったら教えてね」",
        "相手の状況を1行で言い当てる→龍脈命術的な読み→背中を押す一言",
        "シンプルな共感1行→吉方位ヒントを自然に→軽い問いかけ",
    ]
    style = random.choice(_REPLY_STYLES)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
Threadsへの投稿にコメントが届きました。時雨として返信してください。

【元の投稿】
{post_text[:200]}

【届いたコメント】
{comment_text}

【今回の返信スタイル（これに従う）】
{style}

【返信ルール】
- 60〜130字で返す
- 「ありがとうございます」は使わない（代わりに「嬉しい」「届いてよかった」など）
- コメントの具体的な言葉を1つ拾って受け止める（「〜って感じてるんだね」など）
- 龍脈命術・吉方位・気の流れの視点を必ず含める（説明口調は禁止）
- 龍脈命術の説明は文脈に合う場合のみ1行で自然に入れる（無理に入れない）
- 返信の末尾に軽い問いかけを入れてスレッドを続かせる
{cta_rule}
- 語尾は「〜だよ」「〜ね」「〜かも」など柔らかく
- 絵文字は0〜1個

返信文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _classify_nested_comment(text: str) -> str:
    """ネストコメントの種別を判定：high_interest / question / casual"""
    question_words = ["どうすれ", "どうやって", "なぜ", "なんで", "教えて", "知りたい", "気になる", "具体的", "詳しく", "相談"]
    casual_words = ["ありがとう", "感謝", "笑", "w", "🙏", "😊", "なるほど", "そうなんだ", "へえ"]
    high_words = ["やってみたい", "試してみたい", "お願い", "お願いしたい", "鑑定", "申し込み", "LINE"]

    text_lower = text.lower()
    if any(w in text for w in high_words):
        return "high_interest"
    if (any(w in text for w in question_words) and "？" in text) or text.endswith("？") or text.endswith("?"):
        return "question"
    if any(w in text_lower for w in casual_words) and len(text) < 30:
        return "casual"
    if "？" in text or "?" in text:
        return "question"
    if len(text) < 20:
        return "casual"
    return "high_interest"


def _generate_closing_reply(
    nested_comment: str,
    original_comment: str,
    post_text: str,
    profile: dict,
) -> str:
    """
    自分の返信に届いた再コメントへの返信を生成する。
    コメントの種別によってCTAを変える：
    - high_interest/question → 自然にLINEへ誘導
    - casual → 温かく受け止めてCTA不要
    """
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")
    comment_type = _classify_nested_comment(nested_comment)

    if comment_type == "casual":
        cta_instruction = "- LINEやプロフィールへの誘導は不要。会話を温かく受け止めて自然に締める。"
        length_rule = "- 30〜60字で完結させる（短く温かく）"
    elif comment_type == "question":
        cta_instruction = (
            f"- 質問に1行で答えたあと、「もっと詳しくはLINE（{line_url}）でも聞けるよ」と軽く添える"
        )
        length_rule = "- 60〜100字で完結させる"
    else:  # high_interest
        cta_instruction = (
            f"- 「気になることがあれば」「もっと話したいなら」など軽いきっかけを添えてLINE（{line_url}）へ案内する"
        )
        length_rule = "- 50〜90字で完結させる"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
Threadsでの会話が続いています。相手からまた返信が届きました。

【元の投稿】
{post_text[:150]}

【相手の最初のコメント】
{original_comment[:100]}

【相手の返信（今回届いたもの）】
{nested_comment}

【ルール】
{length_rule}
- 押しつけがましくなく、温かく、会話の自然な続きとして書く
{cta_instruction}
- 「ありがとうございます」は使わない
- 語尾は「〜だよ」「〜ね」「〜かも」など柔らかく
- 絵文字は0〜1個

返信文のみ出力してください。"""

    resp = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run() -> dict:
    """
    Returns:
        {"replied": int, "nested_replied": int, "skipped": int, "errors": int}
    """
    profile = load_json(ACCOUNT_PROFILE_FILE, default={})
    history = load_history(limit=200)
    replied_ids = load_replied_comments()

    # 対象投稿：直近 REPLY_TARGET_DAYS 日以内に投稿されたもの
    cutoff = (datetime.now() - timedelta(days=REPLY_TARGET_DAYS)).isoformat()
    target_posts = [
        h for h in history
        if h.get("threads_post_id")
        and h.get("posted_at", "") >= cutoff
        and not h.get("is_mock")
    ]

    replied = 0
    nested_replied = 0
    skipped = 0
    errors = 0

    # reply_magnet 投稿が今日のターゲットに含まれる場合は上限を拡大する
    today_str = datetime.now().strftime("%Y-%m-%d")
    has_reply_magnet_today = any(
        p.get("pattern_id") == "reply_magnet" and p.get("posted_at", "").startswith(today_str)
        for p in target_posts
    )
    effective_max = 30 if has_reply_magnet_today else MAX_REPLIES_PER_RUN

    own_username = profile.get("accounts", {}).get("threads", "").lstrip("@").lower()

    # ── フェーズ1: 投稿への新規コメントに返信 ────────────────

    for post in target_posts:
        if replied + nested_replied >= effective_max:
            break

        post_id = post["threads_post_id"]
        post_text = post.get("text", "")
        post_pattern_id = post.get("pattern_id", "")
        comments = _get_replies(post_id)

        for comment in comments:
            if replied + nested_replied >= effective_max:
                break

            cid = comment.get("id", "")
            comment_text = comment.get("text", "").strip()

            # 自分自身のコメント = ツリー2投稿目。そのIDへのユーザーコメントも返信対象にする
            commenter = comment.get("username", "").lstrip("@").lower()
            if own_username and commenter == own_username:
                sub_post_id = cid
                sub_post_text = comment_text
                sub_comments = _get_replies(sub_post_id)
                for sc in sub_comments:
                    if replied + nested_replied >= effective_max:
                        break
                    sc_id = sc.get("id", "")
                    sc_text = sc.get("text", "").strip()
                    sc_user = sc.get("username", "").lstrip("@").lower()
                    if own_username and sc_user == own_username:
                        continue
                    if sc_id in replied_ids or not sc_text:
                        continue
                    try:
                        reply_text = _generate_reply(sc_text, sub_post_text, profile, post_pattern_id)
                        reply_id = _post_reply(sc_id, reply_text)
                        if reply_id:
                            mark_comment_replied(sc_id)
                            save_our_reply_id(reply_id, sub_post_text, sc_text)
                            replied += 1
                            print(f"  💬 ツリー返信: 「{sc_text[:30]}」→ 「{reply_text[:40]}」")
                            time.sleep(3)
                        else:
                            errors += 1
                    except Exception as e:
                        print(f"  ツリーリプライ生成エラー: {e}")
                        errors += 1
                skipped += 1
                continue

            # 返信済みはスキップ
            if cid in replied_ids:
                skipped += 1
                continue

            # 空コメントはスキップ
            if not comment_text:
                skipped += 1
                continue

            try:
                reply_text = _generate_reply(comment_text, post_text, profile, post_pattern_id)
                reply_id = _post_reply(cid, reply_text)
                if reply_id:
                    mark_comment_replied(cid)
                    # 自分の返信IDを記録（ネスト返信検出用）
                    save_our_reply_id(reply_id, post_text, comment_text)
                    replied += 1
                    print(f"  💬 返信: 「{comment_text[:30]}」→ 「{reply_text[:40]}」")
                    time.sleep(3)
                else:
                    errors += 1
            except Exception as e:
                print(f"  リプライ生成エラー: {e}")
                errors += 1

    # ── フェーズ2: 自分の返信に届いたネストコメントへのクロージング ──

    our_reply_ids = load_our_reply_ids()
    replied_ids = load_replied_comments()  # フェーズ1後に再読み込み

    for our_reply_id, ctx in our_reply_ids.items():
        if replied + nested_replied >= effective_max:
            break

        nested_comments = _get_replies(our_reply_id)
        for nc in nested_comments:
            if replied + nested_replied >= effective_max:
                break

            nid = nc.get("id", "")
            nested_text = nc.get("text", "").strip()
            commenter = nc.get("username", "").lstrip("@").lower()

            # 自分自身のコメントはスキップ
            if own_username and commenter == own_username:
                skipped += 1
                continue

            # 返信済みはスキップ
            if nid in replied_ids:
                skipped += 1
                continue

            # 空コメントはスキップ
            if not nested_text:
                skipped += 1
                continue

            try:
                closing_text = _generate_closing_reply(
                    nested_text,
                    ctx.get("comment_text", ""),
                    ctx.get("post_text", ""),
                    profile,
                )
                reply_id = _post_reply(nid, closing_text)
                if reply_id:
                    mark_comment_replied(nid)
                    nested_replied += 1
                    print(f"  🔁 ネスト返信: 「{nested_text[:30]}」→ 「{closing_text[:40]}」")
                    time.sleep(3)
                else:
                    errors += 1
            except Exception as e:
                print(f"  ネストリプライ生成エラー: {e}")
                errors += 1

    return {
        "replied": replied,
        "nested_replied": nested_replied,
        "skipped": skipped,
        "errors": errors,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
