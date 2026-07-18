"""
パターン別 ENG% から weight 補正係数を自動計算して
data/pattern_weight_adjustments.json に書き出す。

writer.py の _load_weight_adjustments() が読み込むことで
パターン選択に自動フィードバックされる。

実行: python scripts/update_weights.py
"""

import json
import pathlib
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DATA_DIR = pathlib.Path(__file__).parent.parent / "data"


def run() -> dict:
    perf_path = DATA_DIR / "performance_data.json"
    history_path = DATA_DIR / "post_history.json"

    if not perf_path.exists():
        print("performance_data.json not found — skip")
        return {}

    perf_data = json.loads(perf_path.read_text(encoding="utf-8"))

    # 現行パターンのIDセットを取得（削除済みパターンを除外するため）
    patterns_path = pathlib.Path(__file__).parent.parent / "knowledge" / "post_patterns.json"
    active_pattern_ids: set = set()
    if patterns_path.exists():
        active_pattern_ids = {p["id"] for p in json.loads(patterns_path.read_text(encoding="utf-8"))}

    # post_history から threads_post_id → pattern_id マップを作成
    # performance_data は post_id フィールドを使う（threads_post_id と同値）
    id_to_pattern: dict = {}
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))
        for h in history:
            pid = h.get("threads_post_id") or h.get("post_id")
            pat = h.get("pattern_id")
            if pid and pat:
                id_to_pattern[str(pid)] = pat

    # パターン別 ENG% を集計（直近 LOOKBACK_DAYS 日のみ）
    LOOKBACK_DAYS = 30
    import datetime as _dt
    _cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    pattern_stats: dict = {}
    for rec in perf_data:
        if rec.get("fetched_at", "") < _cutoff:
            continue
        # performance_data は post_id キーを使う
        post_id = str(rec.get("post_id") or rec.get("threads_post_id") or rec.get("id") or "")
        pat = id_to_pattern.get(post_id)
        if not pat:
            continue
        views = rec.get("views", 0) or 0
        if views <= 0:
            continue
        likes    = rec.get("likes", 0) or 0
        replies  = rec.get("replies", 0) or 0
        reposts  = rec.get("reposts", 0) or 0
        # writer.py と同じ重み付き ENG 計算式（×1000 スケール）
        eng = (likes * 0.4 + replies * 0.35 + reposts * 0.25) / views * 1000

        if pat not in pattern_stats:
            pattern_stats[pat] = {"total_eng": 0.0, "count": 0}
        pattern_stats[pat]["total_eng"] += eng
        pattern_stats[pat]["count"] += 1

    if not pattern_stats:
        print("No joined records — skip")
        return {}

    # パターン別平均 ENG%（現行パターンのみ・削除済みパターンを除外）
    avg_engs = {
        pat: s["total_eng"] / s["count"]
        for pat, s in pattern_stats.items()
        if s["count"] >= 3  # 3件以上のデータがあるパターンのみ
        and (not active_pattern_ids or pat in active_pattern_ids)  # 現行パターンのみ
    }

    if not avg_engs:
        print("Not enough data per pattern — skip")
        return {}

    overall_avg = sum(avg_engs.values()) / len(avg_engs)
    print(f"Overall avg ENG%: {overall_avg:.3f}% ({len(avg_engs)} patterns)")

    # 補正係数: ENG%が全体平均の2倍→2.0x、半分以下→0.5x にクリップ
    adjustments = {}
    for pat, eng in sorted(avg_engs.items(), key=lambda x: -x[1]):
        ratio = eng / overall_avg if overall_avg > 0 else 1.0
        factor = max(0.5, min(2.0, ratio))
        adjustments[pat] = round(factor, 3)
        print(f"  {pat:25s} ENG={eng:.3f}% ratio={ratio:.2f} → {factor:.2f}x (n={pattern_stats[pat]['count']})")

    out = {
        "_updated": __import__("datetime").datetime.now().isoformat()[:16],
        "_overall_avg_eng": round(overall_avg, 4),
        "adjustments": adjustments,
    }
    out_path = DATA_DIR / "pattern_weight_adjustments.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved → {out_path}")
    return out


if __name__ == "__main__":
    run()
