import os
import json
import pathlib
import datetime
import requests

token = os.environ.get("THREADS_ACCESS_TOKEN", "")
if not token:
    print("WARN: THREADS_ACCESS_TOKEN not set")
    raise SystemExit(0)

resp = requests.get(
    "https://graph.threads.net/v1.0/me",
    params={"fields": "id", "access_token": token},
    timeout=10,
)

err_path = pathlib.Path("data/error_log.json")
log = json.loads(err_path.read_text(encoding="utf-8")) if err_path.exists() else []

if resp.ok:
    print(f"Token OK — user_id={resp.json().get('id')}")
else:
    err = resp.json().get("error", {})
    msg = f"TOKEN_INVALID: {err.get('message', 'unknown')} (code={err.get('code')})"
    print(f"ERROR: {msg}")
    log.append({
        "timestamp": datetime.datetime.now().isoformat()[:16],
        "type": "token_health",
        "message": msg,
    })
    err_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(1)
