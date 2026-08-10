"""
Threads long-lived token を refresh する。

使い方（GH Actions から呼ばれる）:
  NEW_TOKEN=$(python scripts/refresh_token.py)

  成功: 新しいトークンのみ stdout に出力、exit 0
  失敗: エラーを stderr に出力、exit 1
  スキップ: トークン未設定など、何も出さず exit 0
"""
import os
import sys
import json
import pathlib
import datetime
import requests

token = os.environ.get("THREADS_ACCESS_TOKEN", "")
if not token:
    print("WARN: THREADS_ACCESS_TOKEN not set", file=sys.stderr)
    sys.exit(0)

resp = requests.get(
    "https://graph.threads.net/refresh_access_token",
    params={"grant_type": "th_refresh_token", "access_token": token},
    timeout=30,
)

if resp.ok:
    data = resp.json()
    new_token = data["access_token"]
    expires_in = data.get("expires_in", 0)
    days = expires_in // 86400
    print(f"✅ token refresh OK — {days}日延長", file=sys.stderr)
    print(new_token)  # stdout にトークンのみ
else:
    err = resp.json().get("error", {})
    msg = f"refresh失敗 code={err.get('code')} — {err.get('message', resp.text)}"
    print(f"❌ {msg}", file=sys.stderr)

    # エラーをログに記録
    log_path = pathlib.Path("data/error_log.json")
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log.append({
        "agent": "token_refresh",
        "error": msg,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(1)
