#!/usr/bin/env python3
"""Cross-site Jexactyl credit heartbeat; only the primary API account is renewed."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

PRIMARY_BASE_URL = os.environ.get(
    "JEXACTYL_PRIMARY_BASE_URL", "https://panel.cyrahost.xyz"
).rstrip("/")
SECONDARY_BASE_URL = os.environ.get(
    "JEXACTYL_SECONDARY_BASE_URL", "https://console.zraa.me"
).rstrip("/")
SERVER_UUID = os.environ.get(
    "JEXACTYL_SERVER_UUID", "1c8f44f1-a8b2-4abb-b5ad-3950ab451b30"
).strip()
PRIMARY_EARN_URL = f"{PRIMARY_BASE_URL}/api/client/store/earn"
SECONDARY_EARN_URL = f"{SECONDARY_BASE_URL}/api/client/store/earn"
RENEW_URL = f"{PRIMARY_BASE_URL}/api/client/servers/{SERVER_UUID}/renew"
API_KEY = os.environ.get("JEXACTYL_API_KEY", "").strip()
SECONDARY_EMAIL = os.environ.get("JEXACTYL_EMAIL", "").strip()
SECONDARY_PASSWORD = os.environ.get("JEXACTYL_PASSWORD", "").strip()
SESSION_KEY = os.environ.get("JEXACTYL_SESSION_KEY", "").strip()
RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
INTERVAL_SECONDS = max(60, int(os.environ.get("INTERVAL_SECONDS", "61")))
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
WAIT_AFTER_RUN_SECONDS = 2 * 60 * 60
RENEW_INTERVAL_SECONDS = 7 * 24 * 60 * 60
STATE_FILE = Path("jexactyl_state.json")
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


def make_primary_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Jexactyl-Primary-Heartbeat/1.0",
    })
    return session


def restore_secondary_session(state: dict) -> requests.Session | None:
    encrypted = state.get("secondary_encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return None
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Jexactyl-ZRAA-Secondary-Heartbeat/1.0",
        "Origin": SECONDARY_BASE_URL,
        "Referer": f"{SECONDARY_BASE_URL}/",
    })
    try:
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain"), path=cookie.get("path", "/"),
            )
    except (KeyError, TypeError):
        return None
    return session


def save_secondary_session(session: requests.Session, state: dict) -> None:
    if not SESSION_KEY:
        return
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    state["secondary_encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, separators=(",", ":")).encode()
    ).decode()
    state["secondary_session_saved_time"] = now_text()


def refresh_secondary_session(session: requests.Session) -> bool:
    try:
        response = session.get(f"{SECONDARY_BASE_URL}/", allow_redirects=False, timeout=25)
    except requests.RequestException:
        return False
    if response.status_code != 200 or "auth/login" in response.headers.get("Location", ""):
        return False
    if "window.JexactylUser" not in response.text and "Welcome," not in response.text:
        return False
    xsrf = session.cookies.get(
        "XSRF-TOKEN", domain=urllib.parse.urlparse(SECONDARY_BASE_URL).hostname
    )
    if not xsrf:
        # Requests may store the cookie for a parent domain; search by name as fallback.
        for cookie in session.cookies:
            if cookie.name == "XSRF-TOKEN":
                xsrf = cookie.value
                break
    if xsrf:
        session.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
    return True


def initialize_renew_schedule(state: dict) -> None:
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


def maybe_renew_primary(session: requests.Session, state: dict) -> bool:
    """Renew only the original API-key account. The password account is never renewed."""
    current = int(time.time())
    next_renew = int(state.get("next_renew_timestamp", 0) or 0)
    retry_at = int(state.get("renew_retry_timestamp", 0) or 0)
    if current < next_renew or current < retry_at:
        return True

    print(f"🔄 主 API 账号满 7 天，续期服务器 {SERVER_UUID[:8]}…", flush=True)
    try:
        response = session.post(RENEW_URL, timeout=25)
    except requests.RequestException as exc:
        state["last_renew_status"] = "network_error"
        state["renew_retry_timestamp"] = current + 300
        print(f"⚠️ 主账号续期网络异常：{type(exc).__name__}: {exc}", flush=True)
        return True

    state["last_renew_http_status"] = response.status_code
    state["last_renew_attempt_time"] = now_text()
    if response.status_code == 204:
        next_timestamp = current + RENEW_INTERVAL_SECONDS
        state.update({
            "last_renew_status": "success",
            "last_renew_time": now_text(current),
            "last_renew_source": "automatic_primary_only",
            "next_renew_timestamp": next_timestamp,
            "next_renew_time": now_text(next_timestamp),
            "total_renew_success": int(state.get("total_renew_success", 0)) + 1,
            "renew_retry_timestamp": 0,
        })
        print(f"✅ 主账号服务器续期成功；下次：{state['next_renew_time']}", flush=True)
        return True
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", "300"))
        state["renew_retry_timestamp"] = current + max(60, retry_after + 2)
        state["last_renew_status"] = "rate_limited"
        return True
    if response.status_code in (400, 409, 422):
        state["renew_retry_timestamp"] = current + 6 * 3600
        state["last_renew_status"] = f"not_ready_{response.status_code}"
        return True
    state["last_renew_status"] = f"http_{response.status_code}"
    print(f"⚠️ 主账号续期返回 HTTP {response.status_code}：{response.text[:200]}", flush=True)
    return response.status_code not in (401, 403)


def earn_once(session: requests.Session, earn_url: str, label: str) -> tuple[str, int]:
    try:
        response = session.post(earn_url, timeout=25)
    except requests.RequestException as exc:
        print(f"⚠️ {label}网络异常：{type(exc).__name__}", flush=True)
        return "failure", INTERVAL_SECONDS
    if response.status_code == 204:
        return "success", INTERVAL_SECONDS
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", INTERVAL_SECONDS))
        return "throttled", max(INTERVAL_SECONDS, retry_after + 2)
    if response.status_code == 419 and label.startswith("副账号"):
        if refresh_secondary_session(session):
            retry = session.post(earn_url, timeout=25)
            if retry.status_code == 204:
                return "success", INTERVAL_SECONDS
            response = retry
    print(f"⚠️ {label}积分返回 HTTP {response.status_code}：{response.text[:160]}", flush=True)
    return "unauthorized" if response.status_code in (401, 403, 419) else "failure", INTERVAL_SECONDS


def main() -> int:
    print("🚀 Jexactyl 跨站双账号积分心跳启动", flush=True)
    print(f"🕐 北京时间：{now_text()}", flush=True)
    print(f"⏳ 本轮 {RUN_MINUTES} 分钟；每 {INTERVAL_SECONDS} 秒请求积分接口", flush=True)
    print(f"🏠 主账号网站：{PRIMARY_BASE_URL}", flush=True)
    print(f"🏠 副账号网站：{SECONDARY_BASE_URL}", flush=True)
    print("🔒 只有主 API 账号参与续期；ZRAA 副账号绝不续期", flush=True)
    if not API_KEY:
        print("❌ 缺少 JEXACTYL_API_KEY", flush=True)
        return 1

    state = load_state()
    state.setdefault("primary_total_success", int(state.get("total_success", 0) or 0))
    state.setdefault("secondary_total_success", 0)
    state["primary_base_url"] = PRIMARY_BASE_URL
    state["secondary_base_url"] = SECONDARY_BASE_URL
    state["secondary_renew_enabled"] = False
    primary = make_primary_session()
    secondary = restore_secondary_session(state)
    secondary_valid = secondary is not None and refresh_secondary_session(secondary)
    state["secondary_account_email"] = SECONDARY_EMAIL or state.get("secondary_account_email")
    state["secondary_status"] = "ready" if secondary_valid else "session_invalid"
    if secondary_valid:
        save_secondary_session(secondary, state)
        print(
            f"✅ ZRAA 副账号登录会话有效："
            f"{state.get('secondary_account_email', 'password account')}",
            flush=True,
        )
    else:
        print("⚠️ ZRAA 副账号登录会话无效；本轮仅运行主 API 账号", flush=True)

    initialize_renew_schedule(state)
    if not maybe_renew_primary(primary, state):
        state["last_status"] = "primary_renew_unauthorized"
        save_state(state)
        return 1

    next_earn = int(state.get("next_earn_timestamp", 0) or 0)
    if not FORCE_RUN and next_earn and int(time.time()) < next_earn:
        print(f"⏳ 当前处于 2 小时等待期；下次积分轮次：{state.get('next_earn_time', now_text(next_earn))}", flush=True)
        save_state(state)
        return 0

    state.update({"last_start_time": now_text(), "last_status": "running"})
    save_state(state)
    deadline = time.monotonic() + RUN_MINUTES * 60
    primary_success = secondary_success = throttled = failures = 0

    while time.monotonic() < deadline:
        if not maybe_renew_primary(primary, state):
            return 1
        wait_seconds = INTERVAL_SECONDS

        result, wait = earn_once(primary, PRIMARY_EARN_URL, "主账号")
        wait_seconds = max(wait_seconds, wait)
        if result == "success":
            primary_success += 1
            state["primary_total_success"] += 1
            print(f"💓 主账号积分 #{primary_success}｜累计 {state['primary_total_success']}", flush=True)
        elif result == "throttled":
            throttled += 1
        else:
            failures += 1

        if secondary_valid and secondary is not None:
            result, wait = earn_once(
                secondary, SECONDARY_EARN_URL, "副账号（ZRAA，不续期）"
            )
            wait_seconds = max(wait_seconds, wait)
            if result == "success":
                secondary_success += 1
                state["secondary_total_success"] += 1
                state["secondary_last_success_time"] = now_text()
                print(f"💓 副账号积分 #{secondary_success}｜累计 {state['secondary_total_success']}", flush=True)
            elif result == "throttled":
                throttled += 1
            elif result == "unauthorized":
                secondary_valid = False
                state["secondary_status"] = "session_invalid"
                print("⚠️ 副账号登录失效，后续仅运行主账号", flush=True)
            else:
                failures += 1
            save_secondary_session(secondary, state)

        state.update({
            "last_status": "earning",
            "last_request_time": now_text(),
            "current_primary_success": primary_success,
            "current_secondary_success": secondary_success,
            "current_throttled": throttled,
            "current_failures": failures,
        })
        save_state(state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(wait_seconds, remaining))

    end_timestamp = int(time.time())
    next_timestamp = end_timestamp + WAIT_AFTER_RUN_SECONDS
    state.update({
        "last_end_time": now_text(end_timestamp),
        "last_status": "waiting_2_hours",
        "last_run_primary_success": primary_success,
        "last_run_secondary_success": secondary_success,
        "last_run_throttled": throttled,
        "last_run_failures": failures,
        "next_earn_timestamp": next_timestamp,
        "next_earn_time": now_text(next_timestamp),
    })
    if secondary is not None:
        save_secondary_session(secondary, state)
    save_state(state)
    print(f"🏁 本轮结束：主账号 {primary_success}，副账号 {secondary_success}，失败 {failures}", flush=True)
    print(f"🛌 等待 2 小时；下次：{state['next_earn_time']}", flush=True)
    return 0 if primary_success > 0 or secondary_success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
