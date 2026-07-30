#!/usr/bin/env python3
"""DragonHost free server renewal: renew only during the final 24 hours."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = os.environ.get("DRAGONHOST_BASE_URL", "https://panel.dragonhost.ru").rstrip("/")
EMAIL = os.environ.get("DRAGONHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("DRAGONHOST_PASSWORD", "").strip()
SESSION_KEY = os.environ.get("DRAGONHOST_SESSION_KEY", "").strip()
GAME_ID = os.environ.get("DRAGONHOST_GAME_ID", "328").strip()
FORCE_ATTEMPT = os.environ.get("FORCE_ATTEMPT", "false").lower() == "true"
STATE_FILE = Path("dragonhost_state.json")
CN_TZ = timezone(timedelta(hours=8))

LOGIN_URL = f"{BASE_URL}/api/login"
SESSION_URL = f"{BASE_URL}/api/ses"
CSRF_URL = f"{BASE_URL}/api/csrf"
GAME_URL = f"{BASE_URL}/api/game/{GAME_ID}"
RENEW_INFO_URL = f"{GAME_URL}/renew"
RENEW_URL = RENEW_INFO_URL


def now_text(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(timestamp, CN_TZ) if timestamp else datetime.now(CN_TZ)
    return value.strftime("%Y-%m-%d %H:%M:%S")


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


def response_json(response: requests.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def error_message(response: requests.Response) -> str:
    data = response_json(response)
    error = data.get("error")
    if isinstance(error, dict) and error.get("msg"):
        return str(error["msg"])
    if data.get("msg"):
        return str(data["msg"])
    return response.text[:300]


def restore_session(session: requests.Session, state: dict) -> bool:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return False
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain"), path=cookie.get("path", "/"),
            )
        response = session.get(SESSION_URL, timeout=20)
        data = response_json(response)
        return response.status_code == 200 and data.get("success") is True
    except (InvalidToken, ValueError, TypeError, KeyError, requests.RequestException):
        return False


def save_session(session: requests.Session, state: dict) -> None:
    if not SESSION_KEY:
        return
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_text()


def main() -> int:
    print("🐉 DragonHost 最后一天自动续期检查", flush=True)
    print(f"🕐 北京时间：{now_text()}", flush=True)
    if not EMAIL or not PASSWORD:
        print("❌ 缺少 DRAGONHOST_EMAIL/DRAGONHOST_PASSWORD", flush=True)
        return 1

    state = load_state()
    state.update({"last_check_time": now_text(), "last_status": "checking"})
    save_state(state)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "DragonHost-Renew/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Lang": "en",
    })

    if restore_session(session, state):
        print("♻️ 复用 DragonHost 登录会话", flush=True)
    else:
        try:
            login = session.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD}, timeout=30)
        except requests.RequestException as exc:
            print(f"❌ 登录网络异常：{type(exc).__name__}: {exc}", flush=True)
            state["last_status"] = "login_network_error"
            save_state(state)
            return 1

        login_data = response_json(login)
        if login.status_code != 200 or not login_data.get("success"):
            message = error_message(login)
            print(f"❌ 登录失败：HTTP {login.status_code}｜{message}", flush=True)
            state.update({"last_status": "login_failed", "last_error": message})
            save_state(state)
            return 1
        print("✅ DragonHost 登录成功", flush=True)
        save_session(session, state)
        save_state(state)

    try:
        game_response = session.get(GAME_URL, timeout=25)
        game_data = response_json(game_response)
    except requests.RequestException as exc:
        print(f"❌ 获取服务器信息失败：{type(exc).__name__}: {exc}", flush=True)
        state["last_status"] = "game_info_network_error"
        save_state(state)
        return 1

    game = game_data.get("data") if isinstance(game_data.get("data"), dict) else {}
    if game_response.status_code != 200 or not game_data.get("success") or not game.get("expires"):
        message = error_message(game_response)
        print(f"❌ 服务器信息异常：HTTP {game_response.status_code}｜{message}", flush=True)
        state.update({"last_status": "game_info_failed", "last_error": message})
        save_state(state)
        return 1

    expires = int(game["expires"])
    remaining = expires - int(time.time())
    state.update({
        "game_id": GAME_ID,
        "server_status": game.get("status"),
        "expires_timestamp": expires,
        "expires_time": now_text(expires),
        "remaining_seconds": remaining,
    })
    print(f"📅 当前到期时间：{state['expires_time']}", flush=True)

    # DragonHost 只允许在最后一天续期。每 6 小时检查，进入最后 24 小时后才提交。
    if not FORCE_ATTEMPT and remaining > 24 * 3600:
        days = remaining / 86400
        print(f"ℹ️ 还有约 {days:.2f} 天，尚未进入最后一天，正常跳过", flush=True)
        state["last_status"] = "waiting_last_day"
        state.pop("last_error", None)
        save_state(state)
        return 0

    try:
        csrf_response = session.get(CSRF_URL, timeout=20)
        csrf_token = csrf_response.headers.get("X-CSRF-Token") or csrf_response.headers.get("x-csrf-token")
    except requests.RequestException as exc:
        print(f"❌ 获取 CSRF Token 失败：{type(exc).__name__}: {exc}", flush=True)
        state["last_status"] = "csrf_network_error"
        save_state(state)
        return 1
    if csrf_response.status_code != 200 or not csrf_token:
        print(f"❌ 获取 CSRF Token 失败：HTTP {csrf_response.status_code}", flush=True)
        state["last_status"] = "csrf_failed"
        save_state(state)
        return 1

    print("🔄 已进入最后一天，提交免费 5 天续期", flush=True)
    try:
        renewed = session.post(
            RENEW_URL,
            json={"method": "balance", "days": 5},
            headers={"X-CSRF-Token": csrf_token},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"❌ 续期网络异常：{type(exc).__name__}: {exc}", flush=True)
        state["last_status"] = "renew_network_error"
        save_state(state)
        return 1

    renewed_data = response_json(renewed)
    success = renewed.status_code in (200, 201, 202, 204) and (
        renewed.status_code == 204 or renewed_data.get("success") is True
    )
    if not success:
        message = error_message(renewed)
        # 服务端在“最后一天”边界之前会返回 HTTP 500；这不是账号或脚本故障。
        if "only renew on the last day" in message.lower():
            print(f"ℹ️ 服务端仍判定未到最后一天，等待下一次检查：{message}", flush=True)
            state.update({"last_status": "server_says_too_early", "last_error": message})
            save_state(state)
            return 0
        print(f"❌ 续期失败：HTTP {renewed.status_code}｜{message}", flush=True)
        state.update({"last_status": "renew_failed", "last_error": message})
        save_state(state)
        return 1

    # 重新读取到期日，确认确实延长。
    time.sleep(2)
    verify = session.get(GAME_URL, timeout=25)
    verify_data = response_json(verify)
    verify_game = verify_data.get("data") if isinstance(verify_data.get("data"), dict) else {}
    new_expires = int(verify_game.get("expires", 0) or 0)
    if new_expires <= expires:
        print("❌ 接口返回成功，但到期时间没有变化", flush=True)
        state["last_status"] = "renew_not_verified"
        save_state(state)
        return 1

    state.update({
        "last_status": "renewed",
        "last_renew_time": now_text(),
        "old_expires_time": now_text(expires),
        "expires_timestamp": new_expires,
        "expires_time": now_text(new_expires),
        "total_renew_success": int(state.get("total_renew_success", 0)) + 1,
    })
    state.pop("last_error", None)
    save_state(state)
    print(f"✅ 续期成功，新到期时间：{state['expires_time']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
