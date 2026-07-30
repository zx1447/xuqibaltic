#!/usr/bin/env python3
"""CyraHost Jexactyl credit heartbeat via Client API key."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = os.environ.get("CYRAHOST_BASE_URL", "https://panel.cyrahost.xyz").rstrip("/")
EARN_URL = f"{BASE_URL}/api/client/store/earn"
ACCOUNT_URL = f"{BASE_URL}/api/client/account"
API_KEY = os.environ.get("CYRAHOST_API_KEY", "").strip()
PROXY_URL = os.environ.get("CYRAHOST_PROXY", "").strip()
RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
INTERVAL_SECONDS = max(61, int(os.environ.get("INTERVAL_SECONDS", "62")))
STATE_FILE = Path("cyrahost_state.json")
CN_TZ = timezone(timedelta(hours=8))


def now_text() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def mask_account(value: str) -> str:
    if "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:2]}***@{domain}"
    return f"{value[:2]}***{value[-2:]}" if len(value) > 4 else "***"


def main() -> int:
    print("🚀 CyraHost Earn 心跳启动", flush=True)
    print(f"🕐 北京时间：{now_text()}", flush=True)
    print(f"⏳ 本轮运行 {RUN_MINUTES} 分钟，每 {INTERVAL_SECONDS} 秒请求一次", flush=True)
    if not API_KEY:
        print("❌ 缺少 CYRAHOST_API_KEY", flush=True)
        return 1

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "CyraHost-Earn-Heartbeat/1.0",
    })
    if PROXY_URL:
        session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
        print("🔗 使用 VLESS 转换的 SOCKS5 代理出口", flush=True)

    try:
        response = session.get(ACCOUNT_URL, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        account = response.json().get("attributes", {})
        identity = account.get("email") or account.get("username") or "unknown"
        print(f"✅ API Key 有效，账户：{mask_account(str(identity))}", flush=True)
    except Exception as exc:
        print(f"❌ API Key 验证失败：{type(exc).__name__}: {exc}", flush=True)
        return 1

    state = load_state()
    state["last_start_time"] = now_text()
    state["last_status"] = "running"
    save_state(state)

    deadline = time.monotonic() + RUN_MINUTES * 60
    run_success = 0
    run_throttled = 0
    run_failures = 0
    consecutive_failures = 0

    while time.monotonic() < deadline:
        wait_seconds = INTERVAL_SECONDS
        try:
            # 不发送 Origin/Referer，避免 Laravel Sanctum 将 API Key 请求误判为网页 CSRF 请求。
            response = session.post(EARN_URL, timeout=25)
            status = response.status_code
            state["last_http_status"] = status
            state["last_request_time"] = now_text()

            if status == 204:
                run_success += 1
                consecutive_failures = 0
                state["total_success"] = int(state.get("total_success", 0)) + 1
                state["last_success_time"] = now_text()
                state["last_status"] = "earning"
                print(f"💓 Earn 成功 #{run_success}｜累计 {state['total_success']}｜{now_text()}", flush=True)
            elif status == 429:
                run_throttled += 1
                retry_after = int(response.headers.get("Retry-After", INTERVAL_SECONDS))
                wait_seconds = max(INTERVAL_SECONDS, retry_after + 2)
                state["last_status"] = "rate_limited"
                print(f"⏱️ 触发频率限制，{wait_seconds} 秒后继续", flush=True)
            elif status in (401, 403):
                state["last_status"] = "unauthorized"
                save_state(state)
                print(f"❌ API Key 被拒绝：HTTP {status}", flush=True)
                return 1
            else:
                run_failures += 1
                consecutive_failures += 1
                state["last_status"] = f"http_{status}"
                print(f"⚠️ Earn 返回 HTTP {status}：{response.text[:200]}", flush=True)
        except requests.RequestException as exc:
            run_failures += 1
            consecutive_failures += 1
            state["last_status"] = "network_error"
            print(f"⚠️ 网络异常：{type(exc).__name__}: {exc}", flush=True)

        state["current_run_success"] = run_success
        state["current_run_throttled"] = run_throttled
        state["current_run_failures"] = run_failures
        save_state(state)

        if consecutive_failures >= 10:
            print("❌ 连续失败 10 次，本轮提前结束，交给下一轮重试", flush=True)
            state["last_status"] = "too_many_failures"
            save_state(state)
            return 1

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(wait_seconds, remaining))

    state.update({
        "last_end_time": now_text(),
        "last_status": "completed",
        "last_run_success": run_success,
        "last_run_throttled": run_throttled,
        "last_run_failures": run_failures,
    })
    save_state(state)
    print(f"🏁 本轮结束：成功 {run_success}，限流 {run_throttled}，失败 {run_failures}", flush=True)
    return 0 if run_success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
