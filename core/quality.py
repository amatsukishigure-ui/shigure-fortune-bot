"""
品質スコアリングモジュール

Claudeが投稿を10項目で採点する。
各項目10点満点、平均7.0以上で合格。
"""

import json
import anthropic
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ANTHROPIC_API_KEY, MODEL_LIGHT, QUALITY_THRESHOLD


SCORING_CRITERIA = [
    ("フックの強さ", "1行目で読者を引き込む力。スクロールを止めさせるインパクトがあるか"),
    ("有益性", "読者にとって具体的な価値・学びがあるか"),
    ("具体性", "抽象論ではなく具体的な数字・事例・手順があるか"),
    ("テンポ感", "テキストのリズム。改行・句読点・文長のバランスが良いか"),
    ("ペルソナ一致度", "ターゲット読者層のニーズ・言語感覚に合っているか"),
    ("独自性", "他のアカウントでも見られる内容でなく、独自の視点・切り口があるか"),
    ("エンゲージメント誘発力", "いいね・コメント・保存を促す要素があるか"),
    ("信頼性", "嘘・誇張・不正確な情報がなく、信頼できる内容か"),
    ("読みやすさ", "短い文・箇条書き・適切な絵文字などで読みやすいか"),
    ("行動喚起力", "読者に何か行動・変化を促す力があるか"),
]


def score_post(post_text: str, account_profile: dict) -> dict:
    """
    投稿を10項目でスコアリングする。

    Returns:
        {
            "scores": {"フックの強さ": 8, ...},
            "average": 7.5,
            "passed": True,
            "feedback": "...",
        }
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    criteria_text = "\n".join(
        f"{i+1}. **{name}** ({desc}): 0〜10点"
        for i, (name, desc) in enumerate(SCORING_CRITERIA)
    )

    profile_text = json.dumps(account_profile, ensure_ascii=False, indent=2)

    prompt = f"""以下のThreads投稿を採点してください。

## アカウントプロフィール
{profile_text}

## 採点する投稿
---
{post_text}
---

## 採点基準（各0〜10点）
{criteria_text}

## 出力形式（JSONのみ、説明不要）
{{
  "scores": {{
    "フックの強さ": <0-10の整数>,
    "有益性": <0-10の整数>,
    "具体性": <0-10の整数>,
    "テンポ感": <0-10の整数>,
    "ペルソナ一致度": <0-10の整数>,
    "独自性": <0-10の整数>,
    "エンゲージメント誘発力": <0-10の整数>,
    "信頼性": <0-10の整数>,
    "読みやすさ": <0-10の整数>,
    "行動喚起力": <0-10の整数>
  }},
  "feedback": "<合格・不合格の理由と改善点を1〜2文で>"
}}"""

    response = client.messages.create(
        model=MODEL_LIGHT,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # JSONブロックを抽出
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw)
    scores = result["scores"]
    average = sum(scores.values()) / len(scores)

    return {
        "scores": scores,
        "average": round(average, 2),
        "passed": average >= QUALITY_THRESHOLD,
        "feedback": result.get("feedback", ""),
    }


def score_with_retry(
    post_text: str,
    account_profile: dict,
    rewrite_func=None,
    max_retries: int = 2,
) -> dict:
    """
    品質スコアが閾値未満なら rewrite_func で書き直してリトライする。

    Returns:
        {"post": str, "score_result": dict, "attempts": int, "accepted": bool}
    """
    current_post = post_text
    attempts = 0

    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        score_result = score_post(current_post, account_profile)

        if score_result["passed"]:
            return {
                "post": current_post,
                "score_result": score_result,
                "attempts": attempts,
                "accepted": True,
            }

        if attempt < max_retries and rewrite_func is not None:
            # フィードバックを渡して書き直し
            current_post = rewrite_func(current_post, score_result["feedback"])
        else:
            break

    return {
        "post": current_post,
        "score_result": score_result,
        "attempts": attempts,
        "accepted": False,
    }
