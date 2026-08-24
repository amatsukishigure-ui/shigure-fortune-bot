"""
アナリストエージェント

過去投稿のパフォーマンスデータを分析し、
ライターへのフィードバックと次のバッチへの指示書を生成する。
"""

import json
import anthropic
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import ANTHROPIC_API_KEY, MODEL_HEAVY, ACCOUNT_PROFILE_FILE, DATA_DIR
from core.state import load_json, load_history, load_performance, save_json


FEEDBACK_FILE = DATA_DIR / "analyst_feedback.txt"
ANALYSIS_REPORT_FILE = DATA_DIR / "analysis_report.json"


def _calculate_engagement_score(metrics: dict) -> float:
    """エンゲージメントスコアを計算（絶対値: likes + replies×3）
    レートベース(likes/views)は高閲覧投稿を不当に低評価するため使わない。
    """
    likes = metrics.get("likes", 0)
    replies = metrics.get("replies", 0)
    return float(likes + replies * 3)


def _merge_history_and_performance(history: list, perf_data: list) -> list:
    """投稿履歴とパフォーマンスデータを結合する"""
    perf_map = {p["post_id"]: p for p in perf_data if "post_id" in p}
    merged = []
    for post in history:
        post_id = post.get("threads_post_id")
        if post_id and post_id in perf_map:
            entry = {**post, **perf_map[post_id]}
            entry["engagement_score"] = _calculate_engagement_score(perf_map[post_id])
            merged.append(entry)
    return merged


WEIGHT_ADJUSTMENTS_FILE = DATA_DIR / "pattern_weight_adjustments.json"


def compute_pattern_weight_adjustments(history: list, perf_data: list, lookback_days: int = 30) -> dict:
    """
    過去N日間の実パフォーマンスからパターン別の重み補正係数を計算する。
    スコアは絶対エンゲージメント: likes + replies*3
    （レートベースは高閲覧投稿を不当に低評価するため使わない）
    戻り値: {"caligula": 1.3, "kyokan": 0.7, ...} (0.5〜2.0の範囲)
    """
    from datetime import timedelta
    from collections import defaultdict

    cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    perf_map = {p["post_id"]: p for p in perf_data if "post_id" in p}

    pattern_scores: dict = defaultdict(list)
    for post in history:
        if post.get("posted_at", "") < cutoff:
            continue
        pid = post.get("pattern_id")
        post_id = post.get("threads_post_id")
        if not pid or not post_id:
            continue
        perf = perf_map.get(post_id, {})
        eng = perf.get("likes", 0) + perf.get("replies", 0) * 3
        pattern_scores[pid].append(eng)

    if not pattern_scores:
        return {}

    all_scores = [s for scores in pattern_scores.values() for s in scores]
    global_avg = sum(all_scores) / len(all_scores) if all_scores else 1.0
    if global_avg == 0:
        return {}

    adjustments = {}
    for pid, scores in pattern_scores.items():
        if len(scores) < 3:  # サンプル不足はスキップ（ノイズ防止）
            continue
        pattern_avg = sum(scores) / len(scores)
        ratio = pattern_avg / global_avg
        adjustments[pid] = round(max(0.5, min(2.0, ratio)), 3)

    return adjustments


def run() -> dict:
    """
    アナリストエージェントのメイン処理。

    Returns:
        {"top_patterns": list, "top_themes": list, "feedback_saved": bool}
    """
    client_obj = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = load_history(limit=200)
    perf_data = load_performance()
    profile = load_json(ACCOUNT_PROFILE_FILE, default={})

    if not history or not perf_data:
        print("⚠️ アナリスト: データ不足のためスキップ")
        return {"top_patterns": [], "top_themes": [], "feedback_saved": False}

    merged = _merge_history_and_performance(history, perf_data)
    if not merged:
        print("⚠️ アナリスト: 結合データなし")
        return {"top_patterns": [], "top_themes": [], "feedback_saved": False}

    # パターン・テーマ別集計
    from collections import defaultdict
    pattern_scores = defaultdict(list)
    theme_scores = defaultdict(list)

    for post in merged:
        score = post.get("engagement_score", 0)
        if post.get("pattern_id"):
            pattern_scores[post["pattern_id"]].append(score)
        if post.get("theme"):
            theme_scores[post["theme"]].append(score)

    top_patterns = sorted(
        [(k, sum(v)/len(v)) for k, v in pattern_scores.items()],
        key=lambda x: x[1], reverse=True
    )[:5]

    top_themes = sorted(
        [(k, sum(v)/len(v)) for k, v in theme_scores.items()],
        key=lambda x: x[1], reverse=True
    )[:5]

    low_themes = sorted(
        [(k, sum(v)/len(v)) for k, v in theme_scores.items()],
        key=lambda x: x[1]
    )[:3]

    # Claudeに分析レポートを生成させる
    summary_data = {
        "投稿総数": len(merged),
        "上位パターン": top_patterns,
        "上位テーマ": top_themes,
        "低調テーマ": low_themes,
        "直近10件のエンゲージメント": [
            {
                "preview": p.get("text", "")[:40],
                "score": p.get("engagement_score", 0),
                "pattern": p.get("pattern_id"),
                "theme": p.get("theme"),
            }
            for p in sorted(merged, key=lambda x: x.get("recorded_at", ""))[-10:]
        ],
    }

    prompt = f"""以下のThreadsアカウントのパフォーマンスデータを分析し、
次のバッチでライターに渡すフィードバック指示書を作成してください。

## アカウント概要
{json.dumps(profile, ensure_ascii=False, indent=2)}

## パフォーマンスデータ
{json.dumps(summary_data, ensure_ascii=False, indent=2)}

## 出力形式
以下の形式で、ライターへの具体的な指示書を日本語で書いてください：

1. 積極的に使うべきパターン・テーマ（理由付き）
2. 控えるべきパターン・テーマ（理由付き）
3. 直近でバズった投稿から学んだ「型」の説明
4. 次のバッチで試すべき新しいアプローチ
5. 1行目（フック）の書き方の改善点

簡潔・具体的に書くこと。箇条書きで。"""

    response = client_obj.messages.create(
        model=MODEL_HEAVY,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    feedback_text = ""
    for block in response.content:
        if block.type == "text":
            feedback_text = block.text.strip()
            break

    # フィードバックをファイルに保存（ライターが読む）
    FEEDBACK_FILE.write_text(feedback_text, encoding="utf-8")

    # 分析レポートも保存
    report = {
        "generated_at": datetime.now().isoformat(),
        "top_patterns": top_patterns,
        "top_themes": top_themes,
        "low_themes": low_themes,
        "total_analyzed": len(merged),
        "feedback_preview": feedback_text[:200],
    }
    save_json(ANALYSIS_REPORT_FILE, report)

    # パターン重み補正係数を計算して保存（writer._select_patternが読む）
    adjustments = compute_pattern_weight_adjustments(history, perf_data, lookback_days=30)
    if adjustments:
        # 既存ファイルから locked リストと手動補正値を引き継ぐ
        existing_adj = {}
        locked: list = []
        try:
            if WEIGHT_ADJUSTMENTS_FILE.exists():
                existing = json.loads(WEIGHT_ADJUSTMENTS_FILE.read_text(encoding="utf-8"))
                existing_adj = existing.get("adjustments", {})
                locked = existing.get("locked", [])
        except Exception:
            pass

        # locked パターンは既存値を優先、それ以外は計算値で更新
        merged = {**adjustments}
        for pid in locked:
            if pid in existing_adj:
                merged[pid] = existing_adj[pid]

        adj_data = {
            "updated_at": datetime.now().isoformat(),
            "locked": locked,
            "lookback_days": 30,
            "adjustments": merged,
        }
        save_json(WEIGHT_ADJUSTMENTS_FILE, adj_data)
        print(f"   重み補正係数を更新: {len(merged)}パターン (locked: {locked})")

    print(f"✅ アナリスト: {len(merged)}件分析完了")
    return {
        "top_patterns": top_patterns,
        "top_themes": top_themes,
        "feedback_saved": True,
        "weight_adjustments": adjustments,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
