"""
リプライエージェント（時雨 / 龍脈命術）

直近の投稿に届いたコメントを取得し、時雨として返信する。
自分の返信に届いた再コメントにも soft closing 返信する。
返信済みコメントIDを記録して二重返信を防ぐ。
1日1回 daily 実行から呼ばれる。
"""

import json
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


_CARD_PICK_MESSAGES = {
    "A": ("🌊", "流れに乗りたい気持ちがある時期だね。北東か東の方位を意識して動き出してみて。小さな一歩でいい。"),
    "B": ("🔥", "加速の気が来ている。今いる場所から南か東南へ意識を向けると、その勢いがさらに増す。"),
    "C": ("🌿", "整える時期にいる。西か北西に静かな場所を作って、そこでゆっくり考えると答えが見えてくる。"),
    "🌊": ("🌊", "流れに乗りたい気持ちがある時期だね。北東か東の方位を意識して動き出してみて。"),
    "🔥": ("🔥", "加速の気が来ている。南か東南へ意識を向けると、その勢いがさらに増す。"),
    "🌿": ("🌿", "整える時期にいる。西か北西に静かな場所を作ると、答えが見えてくる。"),
}


def _is_card_pick_comment(text: str) -> str | None:
    """A/B/C または 🌊🔥🌿 のカード回答か判定。該当キーを返す、なければ None"""
    t = text.strip().upper()
    for key in ("A", "B", "C", "🌊", "🔥", "🌿"):
        if t == key or t == key.upper():
            return key
    # 「Aです」「Bかな」など軽い付加語も拾う
    for key in ("A", "B", "C", "🌊", "🔥", "🌿"):
        if t.startswith(key) and len(t) <= len(key) + 5:
            return key
    return None


def _generate_card_pick_reply(choice_key: str, profile: dict) -> str:
    emoji, msg = _CARD_PICK_MESSAGES.get(choice_key, ("🔮", "気の流れを確認してみて。"))
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")
    return f"{emoji} {msg}\n詳しく知りたい人は LINE {line_url} へ。"


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
            return _generate_card_pick_reply(choice, profile)

    has_signal = _has_consult_signal(comment_text)
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")

    cta_rule = (
        f"- 返信の最後の1行に「もし詳しく話したい時はプロフィールのリンクから相談できるよ」"
        f"または「気になることがあれば LINE {line_url} でも聞けるよ」を自然に1行添える"
        f"（コメントの流れを壊さない程度にさりげなく）"
        if has_signal
        else "- 鑑定への直接誘導は不要（自然な会話の中で信頼を積む）"
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
Threadsへの投稿にコメントが届きました。時雨として返信してください。

【元の投稿】
{post_text[:200]}

【届いたコメント】
{comment_text}

【返信ルール】
- 60〜130字で返す
- 「ありがとうございます」は使わない（代わりに「嬉しい」「届いてよかった」など）
- コメントの具体的な言葉を1つ拾って受け止める（「〜って感じてるんだね」など）
- 龍脈命術・吉方位・気の流れの視点から、その人の状況に合った一言を添える
- 返信の末尾に軽い問いかけを入れてスレッドを続かせる（「どんなこと感じてた？」「何か変化があったら教えてね」など）
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


def _generate_closing_reply(
    nested_comment: str,
    original_comment: str,
    post_text: str,
    profile: dict,
) -> str:
    """
    自分の返信に届いた再コメントへのソフトクロージング返信を生成する。
    50〜90字、自然にLINEまたはプロフィールへ誘導。
    """
    line_url = profile.get("line_url", "https://lin.ee/TJg5dru")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたは占術家「時雨（しぐれ）」です。
Threadsでの会話が続いています。相手からまた返信が届きました。
自然な流れで「より詳しく話したい場合はLINEかプロフィールへ」とソフトに案内してください。

【元の投稿】
{post_text[:150]}

【相手の最初のコメント】
{original_comment[:100]}

【相手の返信（今回届いたもの）】
{nested_comment}

【ルール】
- 50〜90字で完結させる
- 押しつけがましくなく、温かく、会話の自然な続きとして書く
- 「もっと話したいなら」「気になることがあれば」など軽いきっかけを添えてLINE（{line_url}）またはプロフィールのリンクへ案内する
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

    own_username = profile.get("accounts", {}).get("threads", "").lstrip("@").lower()

    # ── フェーズ1: 投稿への新規コメントに返信 ────────────────

    for post in target_posts:
        if replied + nested_replied >= MAX_REPLIES_PER_RUN:
            break

        post_id = post["threads_post_id"]
        post_text = post.get("text", "")
        post_pattern_id = post.get("pattern_id", "")
        comments = _get_replies(post_id)

        for comment in comments:
            if replied + nested_replied >= MAX_REPLIES_PER_RUN:
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
                    if replied + nested_replied >= MAX_REPLIES_PER_RUN:
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
        if replied + nested_replied >= MAX_REPLIES_PER_RUN:
            break

        nested_comments = _get_replies(our_reply_id)
        for nc in nested_comments:
            if replied + nested_replied >= MAX_REPLIES_PER_RUN:
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
