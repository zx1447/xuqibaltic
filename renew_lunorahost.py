#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LunoraHost Panel Discord OAuth 自动服务器续期。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken

APP_BASE = "https://app.lunorahost.xyz"
AUTH_URL = f"{APP_BASE}/auth/discord"
ME_URL = f"{APP_BASE}/api/auth/me"
RENEW_URL = f"{APP_BASE}/api/server/renew"
DISCORD_API = "https://discord.com/api/v10"
STATE_FILE = "lunorahost_state.json"
# Lunora 页面由站点返回续期冷却信息；每 4 天检查一次，状态文件避免重复 POST。
RENEW_INTERVAL_SECONDS = 4 * 24 * 60 * 60

DISCORD_TOKEN = os.environ.get("LUNORAHOST_DISCORD_TOKEN", "").strip()
PROXY = os.environ.get("LUNORAHOST_PROXY", "").strip()
SESSION_KEY = os.environ.get("LUNORAHOST_SESSION_KEY", "").strip()
SESSION_COOKIE_NAME = "lunora.sid"
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class LunoraHostError(RuntimeError):
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
        log(f"⚠️ Telegram 通知失败：{type(exc).__name__}: {exc}")


def make_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "application/json, text/plain, */*",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        log(f"🔗 使用 LUNORAHOST_PROXY：{proxy}")
    return session


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(data: dict) -> None:
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def save_encrypted_session(state: dict, session: requests.Session) -> None:
    """保存加密后的 lunora.sid；明文 Cookie 不进入仓库。"""
    if not SESSION_KEY:
        log("⚠️ 未配置 LUNORAHOST_SESSION_KEY，本次不持久化登录会话")
        return
    cookie_value = next(
        (cookie.value for cookie in session.cookies if cookie.name == SESSION_COOKIE_NAME),
        None,
    )
    if not cookie_value:
        log("⚠️ 当前会话没有 lunora.sid，无法持久化登录状态")
        return
    try:
        encrypted = Fernet(SESSION_KEY.encode()).encrypt(cookie_value.encode()).decode()
    except Exception as exc:
        log(f"⚠️ 加密登录会话失败：{type(exc).__name__}")
        return
    state["encrypted_session_cookie"] = encrypted
    state["session_saved_time"] = now_str()


def restore_encrypted_session(state: dict) -> requests.Session | None:
    """从仓库中的密文恢复 Cookie；密钥只存在 GitHub Secret。"""
    encrypted = state.get("encrypted_session_cookie")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookie_value = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session(PROXY)
    session.cookies.set(
        SESSION_COOKIE_NAME,
        cookie_value,
        domain="app.lunorahost.xyz",
        path="/",
    )
    return session


def session_is_valid(session: requests.Session) -> bool:
    try:
        response = session.get(ME_URL, timeout=20)
        if response.status_code != 200:
            return False
        data = response.json()
        return data.get("authenticated") is True
    except (requests.RequestException, ValueError):
        return False


def should_run() -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，忽略 4 天间隔，立即续期")
        return True
    state = load_state()
    last = int(state.get("last_renew_timestamp", 0) or 0)
    if last and time.time() - last < RENEW_INTERVAL_SECONDS:
        remaining = (RENEW_INTERVAL_SECONDS - (time.time() - last)) / 3600
        log(f"⏳ 距离上次 LunoraHost 续期不足最短间隔 4 天，跳过本次（约剩 {remaining:.1f} 小时）")
        return False
    return True


def oauth_login() -> requests.Session:
    site = make_session(PROXY)
    entry = site.get(AUTH_URL, allow_redirects=False, timeout=25)
    location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in location:
        raise LunoraHostError(f"LunoraHost OAuth 入口异常：HTTP {entry.status_code}")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    scope = query.get("scope", [""])[0]
    state = query.get("state", [""])[0]
    if not client_id or not redirect_uri or not state:
        raise LunoraHostError("LunoraHost OAuth 地址缺少 client_id、redirect_uri 或 state")

    log(f"🔐 获取 LunoraHost OAuth 参数：client_id={client_id}，scope={scope}，state 已获取")
    # Discord Token 只直连 Discord，不通过可选代理。
    discord = make_session()
    auth = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "client_id": client_id,
            "state": state,
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
        raise LunoraHostError("Discord Token 无效或已失效")
    if auth.status_code != 200:
        raise LunoraHostError(f"Discord OAuth 授权失败：HTTP {auth.status_code}")
    callback = auth.json().get("location", "")
    if not callback:
        raise LunoraHostError("Discord OAuth 没有返回 Callback 地址")

    log("🎫 Discord 授权成功，正在请求 LunoraHost Callback...")
    callback_response = site.get(callback, allow_redirects=True, timeout=30)
    if "/login" in callback_response.url.lower():
        raise LunoraHostError(f"LunoraHost Callback 后仍未登录：{callback_response.url}")

    me = site.get(ME_URL, timeout=25)
    if me.status_code == 401:
        raise LunoraHostError("LunoraHost Callback 后 /auth/me 仍返回 401")
    if me.status_code != 200:
        raise LunoraHostError(f"LunoraHost /auth/me 验证失败：HTTP {me.status_code}")
    try:
        profile = me.json()
    except ValueError as exc:
        raise LunoraHostError("LunoraHost /auth/me 返回非 JSON") from exc

    organizations = profile.get("organizations") or []
    organization_id = profile.get("personal_organization_id")
    if not organization_id and organizations:
        organization_id = organizations[0].get("id")
    if organization_id:
        site.headers.update({"X-Organization-ID": str(organization_id)})
        log("✅ LunoraHost 登录成功，组织会话已建立")
    else:
        log("✅ LunoraHost 登录成功，未发现组织 ID（继续使用个人会话）")
    return site


def renew_server(session: requests.Session) -> dict:
    log(f"🔄 发起 LunoraHost 服务器续期 POST：{RENEW_URL}")
    response = session.post(
        RENEW_URL,
        headers={
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/dashboard",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"📡 POST /api/server/renew -> HTTP {response.status_code}")
    if response.status_code in (401, 403):
        raise LunoraHostError(f"LunoraHost 登录会话失效：HTTP {response.status_code}")
    if response.status_code != 200:
        raise LunoraHostError(f"LunoraHost 续期请求失败：HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LunoraHostError(f"LunoraHost 续期返回非 JSON：{response.text[:300]}") from exc
    safe_summary = {
        key: data.get(key)
        for key in ("message", "id", "name", "status", "renew_by")
        if key in data
    }
    log("📦 续期返回摘要：" + json.dumps(safe_summary, ensure_ascii=False, separators=(",", ":")))
    if data.get("success") is False:
        raise LunoraHostError(f"LunoraHost 续期业务失败：{data.get('message', '未知原因')}")
    return data


def main() -> int:
    log("=" * 56)
    log("🚀 LunoraHost 服务器自动续期启动")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 56)
    state = load_state()
    if not should_run():
        return 0

    try:
        # 续期到期时优先复用上次加密保存的 lunora.sid。
        session = restore_encrypted_session(state)
        if session is not None and session_is_valid(session):
            log("♻️ Discord 登录会话仍有效，复用现有会话，不重新 OAuth 登录")
        else:
            if session is not None:
                log("⌛ 已保存的 Discord 会话已失效，重新进行 OAuth 登录")
            if not DISCORD_TOKEN:
                raise LunoraHostError("缺少 LUNORAHOST_DISCORD_TOKEN，且没有可复用的有效会话")
            session = oauth_login()
            save_encrypted_session(state, session)

        try:
            result = renew_server(session)
        except LunoraHostError as renew_exc:
            # 仅在续期接口明确返回登录失效时重新 OAuth 一次。
            if "登录会话失效" not in str(renew_exc) or not DISCORD_TOKEN:
                raise
            log("🔐 续期请求确认会话失效，重新 OAuth 登录一次后重试")
            session = oauth_login()
            save_encrypted_session(state, session)
            result = renew_server(session)

        state.update({
            "last_renew_timestamp": int(time.time()),
            "last_renew_time": now_str(),
            "last_result": result.get("message", "success"),
        })
        save_encrypted_session(state, session)
        save_state(state)
        message = result.get("message", "LunoraHost 续期请求成功")
        log(f"🎉 LunoraHost 服务器续期成功：{message}")
        send_telegram(f"🎉 LunoraHost 服务器续期成功\n🕐 {now_str()}\n📊 {message}")
        return 0
    except Exception as exc:
        log(f"❌ LunoraHost 自动续期失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ LunoraHost 自动续期失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
