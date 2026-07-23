#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CoredLab Hosting 每日访问保活脚本。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://host.coredlabgame.cloud"
AUTH_START_URL = f"{BASE_URL}/api/auth/discord/start?next=/dashboard/server"
ME_URL = f"{BASE_URL}/api/auth/me"
VISIT_URLS = [
    f"{BASE_URL}/dashboard",
    f"{BASE_URL}/dashboard/server?server=minecraft&tab=console",
]
DISCORD_API = "https://discord.com/api/v10"
STATE_FILE = "coredlab_state.json"
VISIT_INTERVAL_SECONDS = 20 * 60 * 60

DISCORD_TOKEN = os.environ.get("COREDLAB_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("COREDLAB_SESSION_KEY", "").strip()
PROXY = os.environ.get("COREDLAB_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class CoredLabError(RuntimeError):
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
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "application/json, text/plain, */*",
    })
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
        log("🔗 使用 COREDLAB_PROXY")
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
        log("⚠️ 未配置 COREDLAB_SESSION_KEY，本次不持久化登录会话")
        return
    cookies = {cookie.name: cookie.value for cookie in session.cookies}
    if not cookies:
        log("⚠️ 当前 CoredLab 没有可保存的 Cookie")
        return
    payload = json.dumps(cookies, separators=(",", ":"))
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(payload.encode()).decode()
    state["session_saved_time"] = now_str()


def restore_encrypted_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        payload = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode()
        cookies = json.loads(payload)
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="host.coredlabgame.cloud", path="/")
    return session


def session_is_valid(session: requests.Session) -> bool:
    try:
        response = session.get(ME_URL, timeout=20)
        return response.status_code == 200 and response.json().get("loggedIn") is True
    except (requests.RequestException, ValueError):
        return False


def should_visit() -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，立即执行 CoredLab 每日访问")
        return True
    last = int(load_state().get("last_visit_timestamp", 0) or 0)
    if last and time.time() - last < VISIT_INTERVAL_SECONDS:
        remaining = (VISIT_INTERVAL_SECONDS - (time.time() - last)) / 3600
        log(f"⏳ 距离上次 CoredLab 访问不足 20 小时，跳过（约剩 {remaining:.1f} 小时）")
        return False
    return True


def oauth_login() -> requests.Session:
    site = make_session()
    entry = site.get(AUTH_START_URL, allow_redirects=False, timeout=25)
    location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in location:
        raise CoredLabError(f"CoredLab OAuth 入口异常：HTTP {entry.status_code}")

    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    state = query.get("state", [""])[0]
    scope = query.get("scope", ["identify email"])[0]
    if not client_id or not redirect_uri or not state:
        raise CoredLabError("CoredLab OAuth 地址缺少必要参数")

    log(f"🔐 获取 CoredLab OAuth 参数：client_id={client_id}，state 已获取")
    discord = requests.Session()
    discord.trust_env = False
    auth = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "prompt": query.get("prompt", ["consent"])[0],
        },
        json={
            "permissions": "0",
            "authorize": True,
            "integration_type": 0,
            "location_context": {
                "guild_id": "10000",
                "channel_id": "10000",
                "channel_type": 10000,
            },
        },
        headers={
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": location,
            "Accept": "*/*",
        },
        allow_redirects=False,
        timeout=25,
    )
    if auth.status_code == 401:
        raise CoredLabError("Discord Token 无效或已失效")
    if auth.status_code != 200:
        raise CoredLabError(f"Discord OAuth 授权失败：HTTP {auth.status_code}")
    callback = auth.json().get("location", "")
    if not callback:
        raise CoredLabError("Discord OAuth 没有返回 Callback")

    log("🎫 Discord 授权成功，正在请求 CoredLab Callback...")
    site.get(callback, allow_redirects=True, timeout=30)
    if not session_is_valid(site):
        raise CoredLabError("CoredLab Callback 后 /api/auth/me 仍未登录")
    log("✅ CoredLab Discord 登录成功")
    return site


def visit_dashboard(session: requests.Session) -> None:
    for url in VISIT_URLS:
        log(f"🌐 访问 CoredLab 页面：{url}")
        response = session.get(url, allow_redirects=False, timeout=25)
        if response.status_code in (301, 302, 303, 307, 308) and "discord/start" in response.headers.get("Location", ""):
            raise CoredLabError("CoredLab 登录会话已失效")
        if response.status_code != 200:
            raise CoredLabError(f"CoredLab 页面访问失败：HTTP {response.status_code}，URL={url}")
        log(f"✅ 页面访问成功：{url}")


def main() -> int:
    log("=" * 58)
    log("🚀 CoredLab Hosting 每日访问启动")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 58)
    if not should_visit():
        return 0

    state = load_state()
    try:
        session = restore_encrypted_session(state)
        if session is not None and session_is_valid(session):
            log("♻️ Discord 登录会话仍有效，本次复用，不重新登录")
        else:
            if session is not None:
                log("⌛ 已保存的 CoredLab 会话失效，重新 Discord OAuth 登录")
            if not DISCORD_TOKEN:
                raise CoredLabError("缺少 COREDLAB_DISCORD_TOKEN，且无可复用会话")
            session = oauth_login()
            save_encrypted_session(state, session)

        try:
            visit_dashboard(session)
        except CoredLabError as visit_error:
            if "会话已失效" not in str(visit_error) or not DISCORD_TOKEN:
                raise
            log("🔐 访问确认会话失效，重新登录一次后重试")
            session = oauth_login()
            save_encrypted_session(state, session)
            visit_dashboard(session)

        state["last_visit_timestamp"] = int(time.time())
        state["last_visit_time"] = now_str()
        save_encrypted_session(state, session)
        save_state(state)
        log("🎉 CoredLab 每日访问完成")
        send_telegram(f"✅ CoredLab 每日访问成功\n🕐 {now_str()}")
        return 0
    except Exception as exc:
        log(f"❌ CoredLab 自动访问失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ CoredLab 自动访问失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
