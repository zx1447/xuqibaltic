#!/usr/bin/env python3
"""CyraHost: earn credits every minute and renew the server every 7 days."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = os.environ.get("CYRAHOST_BASE_URL", "https://panel.cyrahost.xyz").rstrip("/")
SERVER_UUID = os.environ.get(
    "CYRAHOST_SERVER_UUID", "1c8f44f1-a8b2-4abb-b5ad-3950ab451b30"
).strip()
EARN_URL = f"{BASE_URL}/api/client/store/earn"
RENEW_URL = f"{BASE_URL}/api/client/servers/{SERVER_UUID}/renew"
API_KEY = os.environ.get("CYRAHOST_API_KEY", "").strip()
RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
INTERVAL_SECONDS = max(60, int(os.environ.get("INTERVAL_SECONDS", "61")))
RENEW_INTERVAL_SECONDS = 7 * 24 * 60 * 60
STATE_FILE = Path("cyrahost_state.json")
CN_TZ = timezone(timedelta(hours=8))


def now_text(timestamp: float | None = None) -> str:
    moment = datetime.fromtimestamp(timestamp, CN_TZ) if timestamp else datetime.now(CN_TZ)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


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


def initialize_renew_schedule(state: dict) -> None:
    """The user just renewed manually (HTTP 204), so first automatic renewal is 7 days later."""
    if int(state.get("next_renew_timestamp", 0) or 0) > 0:
        return
    current = int(time.time())
    next_renew = current + RENEW_INTERVAL_SECONDS
    state.update({
        "last_renew_time": now_text(current),
        "last_renew_source": "manual_before_automation",
        "next_renew_timestamp": next_renew,
        "next_renew_time": now_text(next_renew),
    })
    print(f"📅 首次自动续期安排在：{state['next_renew_time']}", flush=True)


def maybe_renew(session: requests.Session, state: dict) -> bool:
    current = int(time.time())
    next_renew = int(state.get("next_renew_timestamp", 0) or 0)
    retry_at = int(state.get("renew_retry_timestamp", 0) or 0)
    if current < next_renew or current < retry_at:
        return True

    print(f"🔄 已满 7 天，开始续期服务器 {SERVER_UUID[:8]}…", flush=True)
    try:
        response = session.post(RENEW_URL, timeout=25)
    except requests.RequestException as exc:
        state["last_renew_status"] = "network_error"
        state["renew_retry_timestamp"] = current + 300
        print(f"⚠️ 续期网络异常：{type(exc).__name__}: {exc}", flush=True)
        return True

    state["last_renew_http_status"] = response.status_code
    state["last_renew_attempt_time"] = now_text()
    if response.status_code == 204:
        next_timestamp = current + RENEW_INTERVAL_SECONDS
        state.update({
            "last_renew_status": "success",
            "last_renew_time": now_text(current),
            "last_renew_source": "automatic",
            "next_renew_timestamp": next_timestamp,
            "next_renew_time": now_text(next_timestamp),
            "total_renew_success": int(state.get("total_renew_success", 0)) + 1,
            "renew_retry_timestamp": 0,
        })
        print(f"✅ 服务器续期成功；下次：{state['next_renew_time']}", flush=True)
        return True

    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", "300"))
        state["renew_retry_timestamp"] = current + max(60, retry_after + 2)
        state["last_renew_status"] = "rate_limited"
        print(f"⏱️ 续期接口限流，{max(60, retry_after + 2)} 秒后重试", flush=True)
        return True

    if response.status_code in (400, 409, 422):
        state["renew_retry_timestamp"] = current + 6 * 3600
        state["last_renew_status"] = f"not_ready_{response.status_code}"
        print(f"ℹ️ 当前暂不可续期（HTTP {response.status_code}），6 小时后重试", flush=True)
        return True

    state["last_renew_status"] = f"http_{response.status_code}"
    print(f"⚠️ 续期返回 HTTP {response.status_code}：{response.text[:200]}", flush=True)
    return response.status_code not in (401, 403)


def main() -> int:
    print("🚀 CyraHost 积分心跳与七天续期启动", flush=True)
    print(f"🕐 北京时间：{now_text()}", flush=True)
    print(f"⏳ 本轮 {RUN_MINUTES} 分钟；积分接口每 {INTERVAL_SECONDS} 秒请求一次", flush=True)
    if not API_KEY:
        print("❌ 缺少 CYRAHOST_API_KEY", flush=True)
        return 1

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "CyraHost-Heartbeat-Renew/2.0",
    })

    state = load_state()
    state["last_start_time"] = now_text()
    state["last_status"] = "running"
    initialize_renew_schedule(state)
    save_state(state)

    deadline = time.monotonic() + RUN_MINUTES * 60
    run_success = 0
    run_throttled = 0
    run_failures = 0
    consecutive_failures = 0

    while time.monotonic() < deadline:
        if not maybe_renew(session, state):
            state["last_status"] = "renew_unauthorized"
            save_state(state)
            return 1

        wait_seconds = INTERVAL_SECONDS
        try:
            # API Key 请求不发送 Origin/Referer，避免被 Laravel Sanctum 当成网页 CSRF 请求。
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
                print(f"💓 积分成功 #{run_success}｜累计 {state['total_success']}｜{now_text()}", flush=True)
            elif status == 429:
                run_throttled += 1
                retry_after = int(response.headers.get("Retry-After", INTERVAL_SECONDS))
                wait_seconds = max(INTERVAL_SECONDS, retry_after + 2)
                state["last_status"] = "rate_limited"
                print(f"⏱️ 积分接口限流，{wait_seconds} 秒后继续", flush=True)
            elif status in (401, 403):
                state["last_status"] = "unauthorized"
                save_state(state)
                print(f"❌ API Key 或当前出口被拒绝：HTTP {status}｜{response.text[:200]}", flush=True)
                return 1
            else:
                run_failures += 1
                consecutive_failures += 1
                state["last_status"] = f"http_{status}"
                print(f"⚠️ 积分返回 HTTP {status}：{response.text[:200]}", flush=True)
        except requests.RequestException as exc:
            run_failures += 1
            consecutive_failures += 1
            state["last_status"] = "network_error"
            print(f"⚠️ 网络异常：{type(exc).__name__}: {exc}", flush=True)

        state.update({
            "current_run_success": run_success,
            "current_run_throttled": run_throttled,
            "current_run_failures": run_failures,
        })
        save_state(state)

        if consecutive_failures >= 10:
            print("❌ 连续失败 10 次，本轮提前结束", flush=True)
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
    print(f"🏁 本轮结束：积分成功 {run_success}，限流 {run_throttled}，失败 {run_failures}", flush=True)
    return 0 if run_success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
