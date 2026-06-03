"""
プラットフォーム別テキストフォーマッター

Threads用（500字以内）とX用（280字以内）に
同じコンテンツを最適化する。
"""

import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import X_CHAR_LIMIT, THREADS_CHAR_LIMIT, X_HASHTAGS


THREADS_HASHTAGS = "#占い #龍脈命術"  # Threads投稿末尾に付加するタグ（発見性向上）
THREADS_HASHTAG_LEN = len("\n\n" + THREADS_HASHTAGS)


def format_for_threads(text: str) -> str:
    """
    Threads用に整形する（最大500字）。
    段落単位で収まる範囲まで含め、文の途中で切れないようにする。
    末尾にハッシュタグを追加して投稿の発見性を高める。
    """
    text = text.strip()

    # 既にハッシュタグが含まれていたら追加しない
    has_tag = "#占い" in text or "#龍脈命術" in text or "#吉方位" in text

    # ハッシュタグ付きで収まるかチェック
    body_limit = THREADS_CHAR_LIMIT - (THREADS_HASHTAG_LEN if not has_tag else 0)

    if len(text) <= body_limit:
        return text if has_tag else f"{text}\n\n{THREADS_HASHTAGS}"

    # 段落（空行区切り）単位で収まる範囲まで結合する
    blocks = text.split("\n\n")
    result = ""
    for block in blocks:
        candidate = (result + "\n\n" + block).strip() if result else block
        if len(candidate) <= body_limit:
            result = candidate
        else:
            break

    if result:
        return result if has_tag else f"{result}\n\n{THREADS_HASHTAGS}"

    # 1段落でも500字を超える場合は文末（。）で切る
    truncated = text[:body_limit - 1]
    last_period = truncated.rfind("。")
    if last_period > body_limit // 2:
        body = truncated[:last_period + 1]
        return body if has_tag else f"{body}\n\n{THREADS_HASHTAGS}"

    # 改行で切る
    last_newline = truncated.rfind("\n")
    if last_newline > body_limit // 2:
        body = truncated[:last_newline]
        return body if has_tag else f"{body}\n\n{THREADS_HASHTAGS}"

    return truncated if has_tag else f"{truncated}\n\n{THREADS_HASHTAGS}"


def format_for_x(text: str, hashtags: list = None) -> str:
    """
    X（Twitter）用に整形する（最大280字）。

    戦略:
    1. 一言型（短い）→ ハッシュタグを追加してそのまま
    2. 中尺（リスト・共感）→ 要点だけ残して圧縮
    3. 長尺（エピソード・星座一覧）→ 最初のブロックだけ使う

    Returns:
        X投稿用テキスト（280字以内）
    """
    if hashtags is None:
        hashtags = X_HASHTAGS[:3]  # デフォルトは3つ

    tag_text = " ".join(hashtags)
    available = X_CHAR_LIMIT - len(tag_text) - 2  # 改行2文字分

    text = text.strip()

    # シグネチャを除去（X版は短くする）
    text = re.sub(r'\n\n　　　占術家 時雨$', '', text)
    text = re.sub(r'\n\n　　　　　占術家 時雨$', '', text)
    text = text.strip()

    # すでに短い場合（一言型）
    if len(text) <= available:
        return f"{text}\n\n{tag_text}"

    # 星座別一覧は最初の4行だけ抜粋
    if "♈" in text or "♉" in text:
        lines = text.split("\n")
        header = lines[0] if lines else ""
        # 最初の4星座だけ
        zodiac_lines = [l for l in lines[1:] if l.strip().startswith(("♈", "♉", "♊", "♋"))][:4]
        tail = "他の星座はThreadsで👇"
        short = header + "\n" + "\n".join(zodiac_lines) + "\n" + tail
        if len(short) <= available:
            return f"{short}\n\n{tag_text}"

    # 改行ブロックで分割して最初のブロックだけ使う
    blocks = text.split("\n\n")
    result = ""
    for block in blocks:
        candidate = (result + "\n\n" + block).strip() if result else block
        if len(candidate) <= available:
            result = candidate
        else:
            break

    if not result:
        # それでも長い場合は単純に切る
        result = text[:available - 3] + "..."

    return f"{result}\n\n{tag_text}"


def get_x_hashtags_for_theme(theme: str) -> list:
    """テーマに合わせたハッシュタグを返す"""
    base = ["#占い", "#時雨"]
    theme_tags = {
        "吉方位": ["#吉方位", "#風水"],
        "風水": ["#風水", "#開運"],
        "星座": ["#星座占い", "#運勢"],
        "引越": ["#引越し", "#吉方位"],
        "転機": ["#龍脈命術", "#開運"],
        "金運": ["#金運", "#風水"],
        "恋愛": ["#恋愛運", "#星座占い"],
        "仕事": ["#仕事運", "#転職"],
        "対人": ["#対人運", "#開運"],
        "健康": ["#健康運", "#開運"],
        "引き寄せ": ["#引き寄せ", "#龍脈命術"],
        "神社": ["#神社", "#パワースポット"],
        "開運行動": ["#開運", "#風水"],
        "メッセージ": ["#星座占い", "#運勢"],
        "総合運": ["#運勢", "#星座占い"],
        "開運日": ["#開運日", "#一粒万倍日"],
        "一粒万倍": ["#一粒万倍日", "#開運"],
        "天赦": ["#天赦日", "#開運日"],
        "大安": ["#大安", "#開運日"],
        "寅の日": ["#寅の日", "#金運"],
        "巳の日": ["#巳の日", "#金運"],
        "インテリア": ["#風水インテリア", "#開運"],
        "暮らし": ["#吉方位", "#開運"],
        "季節": ["#開運", "#吉方位"],
        "天文": ["#星座占い", "#開運"],
        "方位の基礎": ["#吉方位", "#風水"],
    }
    for key, tags in theme_tags.items():
        if key in theme:
            return base + tags

    return base + ["#龍脈命術", "#吉方位"]
