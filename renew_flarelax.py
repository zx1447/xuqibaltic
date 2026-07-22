#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flarelax AFK 自动领取脚本。

流程：
1. 使用 Discord 用户授权 Token 完成 Flarelax 的 Discord OAuth 登录；
2. 保留 OAuth 回调产生的 connect.sid 会话；
3. GET /api/afk/claim 完成 AFK claim。

注意：Flarelax 会拒绝 VPN/数据中心代理。默认不挂代理；如果你有获站点允许的
稳定住宅网络代理，可通过 FLARELAX_PROXY secret 传入 socks5/http URL。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse

import requests

BASE_URL = "https://free-dash.flarelax.com"
LOGIN_URL = f"{BASE_URL}/login"
WARNING_URL = f"{BASE_URL}/auth/warning"
AUTH_URL = f"{BASE_URL}/auth/discord"
CLAIM_URL = f"{BASE_URL}/api/afk/claim"
DISCORD_API = "https://discord.com/api/v10"

DISCORD_TOKEN = os.environ.get("FLARELAX_DISCORD_TOKEN", "").strip()
PROXY = os.environ.get("FLARELAX_PROXY", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
        log(f"⚠️ Telegram 通知发送失败：{type(exc).__name__}: {exc}")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
    )
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
        log("🔗 使用 FLARELAX_PROXY（代理是否被站点允许由站点决定）")
    else:
        log("🌐 直连 Flarelax（不使用 VPN/公共代理）")
    return session


def discord_oauth_login(session: requests.Session) -> None:
    log("1️⃣ 打开 Flarelax Discord OAuth 授权入口...")
    entry = session.get(AUTH_URL, allow_redirects=False, timeout=25)
    oauth_location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in oauth_location:
        raise RuntimeError(
            f"Flarelax OAuth 入口异常：HTTP {entry.status_code}，未获得 Discord 授权地址"
        )

    parsed = urllib.parse.urlparse(oauth_location)
    query = urllib.parse.parse_qs(parsed.query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    scope = query.get("scope", [""])[0]
    if not client_id or not redirect_uri:
        raise RuntimeError("Discord OAuth 地址缺少 client_id 或 redirect_uri")

    log(f"   ✅ 获取 OAuth 参数：client_id={client_id}，scope={scope}")
    log("2️⃣ 使用 Discord Token 提交授权...")
    authorize = session.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "client_id": client_id,
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
            "Referer": oauth_location,
            "Accept": "*/*",
        },
        allow_redirects=False,
        timeout=25,
    )
    if authorize.status_code == 401:
        raise RuntimeError("Discord Token 无效或已失效")
    if authorize.status_code != 200:
        raise RuntimeError(f"Discord OAuth 授权失败：HTTP {authorize.status_code}")

    try:
        callback_url = authorize.json().get("location", "")
    except ValueError as exc:
        raise RuntimeError("Discord OAuth 返回不是 JSON") from exc
    if not callback_url:
        raise RuntimeError("Discord OAuth 返回中没有 callback 地址")

    code = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query).get("code", [""])[0]
    if not code:
        raise RuntimeError("Discord OAuth callback 地址中没有 code")
    log("   ✅ Discord 授权 Code 获取成功（不会输出敏感值）")

    log("3️⃣ 请求 Flarelax OAuth Callback 建立登录会话...")
    callback = session.get(callback_url, allow_redirects=True, timeout=30)
    body_lower = callback.text.lower()
    if "vpns and proxies are strictly prohibited" in body_lower or "vpn" in body_lower and "proxies" in body_lower:
        raise RuntimeError(
            "Flarelax 拒绝了当前出口 IP：站点检测到 VPN/代理/数据中心网络。"
            "请使用本人正常住宅网络的自托管 Runner，或配置站点允许的稳定住宅代理。"
        )
    if "/login" in callback.url.lower():
        raise RuntimeError(f"OAuth 回调后仍在登录页：{callback.url}")
    if not session.cookies.get("connect.sid"):
        raise RuntimeError("OAuth 回调未生成 Flarelax connect.sid 会话")
    log(f"   ✅ 登录成功，当前 URL：{callback.url}")


def claim_afk(session: requests.Session) -> dict:
    log("4️⃣ 发起 AFK claim GET 请求...")
    response = session.get(
        CLAIM_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/dashboard/afk",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"   📡 GET {CLAIM_URL} -> HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"claim 返回非 JSON：{response.text[:300]}")

    log("   📦 返回：" + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if response.status_code == 401:
        raise RuntimeError("Flarelax 会话未生效，claim 返回 Authentication required")
    if response.status_code != 200:
        raise RuntimeError(f"claim 请求失败：HTTP {response.status_code}")
    if data.get("success") is False:
        message = data.get("message", "未知原因")
        # 已领取/冷却属于正常的定时检查结果，不让 Actions 误报故障。
        normalized = str(message).lower()
        if any(word in normalized for word in ("already", "cooldown", "wait", "claimed", "later")):
            log(f"ℹ️ 本次无需领取：{message}")
            return data
        raise RuntimeError(f"claim 业务失败：{message}")
    return data


def main() -> int:
    log("=" * 56)
    log("🚀 Flarelax AFK 自动登录与领取启动")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 56)
    if not DISCORD_TOKEN:
        log("❌ 缺少 FLARELAX_DISCORD_TOKEN GitHub Secret")
        return 1

    session = make_session()
    try:
        # 预热会话；warning 页面也是用户手动点击前看到的授权确认页。
        session.get(WARNING_URL, timeout=25)
        discord_oauth_login(session)
        result = claim_afk(session)
        message = result.get("message", "claim 请求成功")
        log(f"🎉 Flarelax AFK claim 完成：{message}")
        send_telegram(f"🎉 Flarelax AFK claim 成功\n🕐 {now_str()}\n📊 {message}")
        return 0
    except Exception as exc:
        log(f"❌ Flarelax 自动领取失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ Flarelax 自动领取失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
