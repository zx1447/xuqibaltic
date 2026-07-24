#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arkya 免费服务器每 3 天自动续期。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

import requests
from cryptography.fernet import Fernet, InvalidToken

API_BASE = "https://api.arkya.gg"
APP_BASE = "https://arkya.gg"
SERVER_ID = "ef0897ab-919d-4f82-ad72-a178d5aaa6e7"
LOGIN_URL = f"{API_BASE}/api/auth/sign-in/email"
SESSION_URL = f"{API_BASE}/api/auth/get-session"
RENEW_URL = f"{API_BASE}/api/free-servers/{SERVER_ID}/renew"
STATE_FILE = "arkya_state.json"
RENEW_INTERVAL_SECONDS = 3 * 24 * 60 * 60

USER = os.environ.get("ARKYA_USER", "").strip()
PASSWORD = os.environ.get("ARKYA_PASS", "").strip()
SESSION_KEY = os.environ.get("ARKYA_SESSION_KEY", "").strip()
PROXY = os.environ.get("ARKYA_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class ArkyaError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as exc:
        log(f"⚠️ Telegram 通知失败：{type(exc).__name__}")


def make_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Origin": APP_BASE,
        "Referer": f"{APP_BASE}/",
        "Accept": "application/json",
    })
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
        log("🔗 使用 ARKYA_PROXY")
    return session


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(data: dict) -> None:
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def save_encrypted_session(state: dict, session: requests.Session) -> None:
    if not SESSION_KEY:
        log("⚠️ 未配置 ARKYA_SESSION_KEY，本次不持久化 Arkya 会话")
        return
    cookies = {cookie.name: cookie.value for cookie in session.cookies}
    if not cookies:
        log("⚠️ Arkya 登录没有产生 Cookie")
        return
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_str()


def restore_encrypted_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="api.arkya.gg", path="/")
    return session


def session_is_valid(session: requests.Session) -> bool:
    try:
        response = session.get(SESSION_URL, timeout=20)
        return response.status_code == 200 and bool(response.json().get("user"))
    except (requests.RequestException, ValueError):
        return False


def should_run(state: dict) -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，忽略 3 天间隔，立即续期")
        return True
    last = int(state.get("last_renew_timestamp", 0) or 0)
    if last and time.time() - last < RENEW_INTERVAL_SECONDS:
        remaining = (RENEW_INTERVAL_SECONDS - (time.time() - last)) / 3600
        log(f"⏳ 距离上次 Arkya 续期不足 3 天，跳过本次（约剩 {remaining:.1f} 小时）")
        return False
    return True


def login_with_password() -> requests.Session:
    if not USER or not PASSWORD:
        raise ArkyaError("缺少 ARKYA_USER 或 ARKYA_PASS")
    session = make_session()
    log(f"🔑 使用账号密码登录 Arkya：{USER[:2]}****")
    response = session.post(
        LOGIN_URL,
        json={
            "email": USER,
            "password": PASSWORD,
            "callbackURL": f"{APP_BASE}/clientarea/servers",
        },
        headers={"Content-Type": "application/json"},
        timeout=25,
    )
    if response.status_code != 200:
        raise ArkyaError(f"Arkya 登录失败：HTTP {response.status_code} {response.text[:300]}")
    if not session_is_valid(session):
        raise ArkyaError("Arkya 登录返回成功但会话验证失败")
    log("✅ Arkya 登录成功，会话已建立")
    return session


def renew_server(session: requests.Session) -> dict:
    log(f"🔄 发起 Arkya 免费服务器续期：{RENEW_URL}")
    response = session.post(
        RENEW_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/clientarea/servers",
        },
        timeout=25,
    )
    log(f"📡 POST /api/free-servers/{SERVER_ID}/renew -> HTTP {response.status_code}")
    if response.status_code in (401, 403):
        raise ArkyaError(f"Arkya 登录会话失效：HTTP {response.status_code}")
    if response.status_code not in (200, 201):
        raise ArkyaError(f"Arkya 续期失败：HTTP {response.status_code} {response.text[:300]}")
    try:
        data = response.json()
    except ValueError:
        data = {"status": response.status_code, "message": response.text[:300]}
    safe = {k: data.get(k) for k in ("message", "status", "freeExpiresAt", "renewBy") if k in data}
    log("📦 返回摘要：" + json.dumps(safe or {"status": response.status_code}, ensure_ascii=False, separators=(",", ":")))
    return data


def main() -> int:
    log("=" * 58)
    log("🚀 Arkya 免费服务器自动续期启动")
    log(f"🕐 北京时间：{now_str()}")
    log(f"🖥 服务器 ID：{SERVER_ID}")
    log("=" * 58)
    state = load_state()
    if not should_run(state):
        return 0

    try:
        session = restore_encrypted_session(state)
        if session is not None and session_is_valid(session):
            log("♻️ Arkya 登录会话仍有效，复用现有会话，不重新登录")
        else:
            if session is not None:
                log("⌛ Arkya 已保存会话失效，重新账号密码登录")
            session = login_with_password()
            save_encrypted_session(state, session)

        try:
            result = renew_server(session)
        except ArkyaError as exc:
            if "会话失效" not in str(exc):
                raise
            log("🔐 Arkya 确认会话失效，重新账号密码登录一次后重试")
            session = login_with_password()
            save_encrypted_session(state, session)
            result = renew_server(session)

        state["last_renew_timestamp"] = int(time.time())
        state["last_renew_time"] = now_str()
        state["last_result"] = result.get("message", "201 Created") if isinstance(result, dict) else "201 Created"
        save_encrypted_session(state, session)
        save_state(state)
        log("🎉 Arkya 免费服务器续期成功")
        send_telegram(f"✅ Arkya 服务器续期成功\n🕐 {now_str()}")
        return 0
    except Exception as exc:
        log(f"❌ Arkya 自动续期失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ Arkya 自动续期失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
