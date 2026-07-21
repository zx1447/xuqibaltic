import sys
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TreeMC Host (https://www.treemc.host) 自动续期脚本。
基于 Supabase Auth 及 Discord OAuth 机制，实现每 4 小时全自动续期与状态推送。
"""
import os
import re
import json
import base64
import time
import datetime
import urllib.request
import urllib.parse
import requests

# ============================================================
# 环境变量与配置
# ============================================================

DISCORD_TOKEN = os.environ.get("TREEMC_TOKEN", "").strip()
REFRESH_TOKEN = os.environ.get("TREEMC_REFRESH_TOKEN", "").strip()
COOKIE        = os.environ.get("TREEMC_COOKIE", "").strip()

TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN      = os.environ.get("TG_BOT_TOKEN", "").strip()
PROXY         = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

SUPABASE_URL  = "https://erxoposfedunywmraaja.supabase.co"
SUPABASE_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVyeG9wb3NmZWR1bnl3bXJhYWphIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAzNjg2OTksImV4cCI6MjA5NTk0NDY5OX0.vqaOEfAq4wJi0yMUfguqGSmYssuZfgGU9_JJAtWeZpk"
DISCORD_API   = "https://discord.com/api/v9"
CLIENT_ID     = "1511578910247358474"
REDIRECT_URI  = "https://erxoposfedunywmraaja.supabase.co/auth/v1/callback"
SITE_URL      = "https://www.treemc.host"
UA            = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def log(msg):
    print(msg, flush=True)


def now_str():
    utc_now = datetime.datetime.utcnow()
    bj_now  = utc_now + datetime.timedelta(hours=8)
    return bj_now.strftime('%Y-%m-%d %H:%M:%S')


def send_tg(result: str, time_left: str = "—"):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    msg = (
        f"🌲 TreeMC 自动续期通知\n"
        f"🕐 运行时间: {now_str()}\n"
        f"📅 利用期限: {time_left}\n"
        f"📊 续期结果: {result}"
    )
    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15):
            log("📨 Telegram 通知发送成功")
    except Exception as e:
        log(f"⚠️ Telegram 通知发送失败：{e}")


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "user-agent": UA,
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if PROXY:
        session.proxies.update({
            "http": PROXY,
            "https": PROXY,
        })
        log(f"🔗 挂载代理: {PROXY}")
    return session


def supabase_refresh(session: requests.Session, refresh_tok: str) -> dict:
    log("🔑 正在尝试用 Refresh Token 换取 Session...")
    resp = session.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        json={"refresh_token": refresh_tok},
        headers={
            "apikey": SUPABASE_ANON,
            "content-type": "application/json",
        },
        timeout=20,
    )
    if resp.status_code in (400, 401):
        raise RuntimeError(f"Refresh Token 无效/已过期 (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise RuntimeError(f"Supabase Refresh 请求失败 (HTTP {resp.status_code})")

    data = resp.json()
    if not data.get("access_token"):
        raise RuntimeError(f"响应中缺少 access_token: {data}")
    return data


def get_fresh_token_via_discord(session: requests.Session) -> dict:
    log("🔗 请求 Supabase 生成 OAuth State...")
    sb_resp = session.get(
        f"{SUPABASE_URL}/auth/v1/authorize",
        params={
            "provider": "discord",
            "redirect_to": f"{SITE_URL}/auth/callback",
        },
        headers={"apikey": SUPABASE_ANON},
        allow_redirects=False,
        timeout=20,
    )

    discord_oauth_url = sb_resp.headers.get("location", "")
    if not discord_oauth_url or "discord.com" not in discord_oauth_url:
        raise RuntimeError(f"未获取到 Discord OAuth URL，状态码: {sb_resp.status_code}")

    parsed       = urllib.parse.urlparse(discord_oauth_url)
    qs           = urllib.parse.parse_qs(parsed.query)
    state        = qs.get("state", [None])[0]
    redirect_uri = qs.get("redirect_uri", [REDIRECT_URI])[0]

    if not state:
        raise RuntimeError("未能从 OAuth URL 提取 State")

    log("🔐 成功获取 OAuth State，提交 Discord 授权...")
    redirect_uri_encoded = urllib.parse.quote(redirect_uri, safe="")

    resp = session.post(
        f"{DISCORD_API}/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri_encoded}"
        f"&scope=email%20identify"
        f"&state={state}",
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
            "accept": "*/*",
            "authorization": DISCORD_TOKEN,
            "content-type": "application/json",
            "origin": "https://discord.com",
            "referer": (
                f"https://discord.com/oauth2/authorize"
                f"?client_id={CLIENT_ID}"
                f"&redirect_uri={redirect_uri_encoded}"
                f"&response_type=code"
                f"&scope=email+identify"
            ),
        },
        timeout=20,
    )

    if resp.status_code == 401:
        raise RuntimeError("Discord Token 无效或失效")
    if resp.status_code != 200:
        raise RuntimeError(f"Discord OAuth 授权失败 (HTTP {resp.status_code})")

    location = resp.json().get("location", "")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [None])[0]
    if not code:
        raise RuntimeError("无法从 OAuth 响应提取 Code")

    log("🎫 成功获取 Code，向 Supabase Callback 换取 Session...")
    cb_resp = session.get(
        f"{REDIRECT_URI}?code={code}&state={state}",
        headers={
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "referer": "https://discord.com/",
        },
        allow_redirects=True,
        timeout=20,
    )

    final_url = cb_resp.url
    fragment  = urllib.parse.urlparse(final_url).fragment
    params    = urllib.parse.parse_qs(fragment)
    access_token  = params.get("access_token", [None])[0]
    refresh_token = params.get("refresh_token", [None])[0]

    if access_token and refresh_token:
        log("✅ 成功获取 Supabase Session Tokens")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "user": {},
        }

    raise RuntimeError(f"未在重定向中获得 Token，URL: {final_url}")


def build_sb_cookie(session: requests.Session, token_data: dict):
    cookie_payload = json.dumps({
        "access_token": token_data["access_token"],
        "token_type": "bearer",
        "expires_in": token_data.get("expires_in", 3600),
        "expires_at": token_data.get("expires_at"),
        "refresh_token": token_data["refresh_token"],
        "user": token_data.get("user", {}),
    }, separators=(',', ':'))

    encoded = "base64-" + base64.b64encode(cookie_payload.encode()).decode()
    mid     = len(encoded) // 2

    session.cookies.set(
        "sb-erxoposfedunywmraaja-auth-token.0", encoded[:mid],
        domain="www.treemc.host", path="/"
    )
    session.cookies.set(
        "sb-erxoposfedunywmraaja-auth-token.1", encoded[mid:],
        domain="www.treemc.host", path="/"
    )
    log("✅ Cookie 写入完成")


def fetch_time_left(session: requests.Session) -> str:
    try:
        resp = session.get(
            f"{SITE_URL}/api/pterodactyl/account",
            headers={
                "accept": "*/*",
                "referer": f"{SITE_URL}/dashboard",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return "—"
        data = resp.json()
        last_renewed_at = data.get("lastRenewedAt", "")
        if not last_renewed_at:
            return "—"
        
        dt_str = last_renewed_at.replace("+00:00", "").replace("Z", "")
        if "." in dt_str:
            dt = datetime.datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            dt = datetime.datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        expire_at = dt + datetime.timedelta(hours=12)
        now_utc   = datetime.datetime.utcnow()
        delta     = expire_at - now_utc
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "0h 0m"
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        return f"{h}h {m}m"
    except Exception as e:
        log(f"⚠️ 读取利用期限异常：{e}")
        return "—"


def do_renew(session: requests.Session):
    log("🔄 正在向 TreeMC 发起续期请求...")
    resp = session.post(
        f"{SITE_URL}/api/server/renew",
        headers={
            "accept": "*/*",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/dashboard",
        },
        timeout=20,
    )
    log(f"HTTP Status: {resp.status_code}, Response: {resp.text[:300]}")
    if resp.status_code != 200:
        raise RuntimeError(f"续期请求失败，状态码: {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"续期响应非 OK: {data}")
    log("🎉 续期操作成功！")


def main():
    log("=" * 50)
    log("🌲 TreeMC-Renew 启动")
    log(f"🕐 北京时间: {now_str()}")
    log("=" * 50)

    session = create_session()

    token_data = None

    # 1. 优先尝试静态直接 Cookie
    if COOKIE:
        log("🍪 优先使用 TREEMC_COOKIE 直连模式...")
        for item in COOKIE.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                session.cookies.set(k, v, domain="www.treemc.host", path="/")

    # 2. 若配置了 Refresh Token，先尝试 Refresh Token
    elif REFRESH_TOKEN:
        try:
            token_data = supabase_refresh(session, REFRESH_TOKEN)
            build_sb_cookie(session, token_data)
        except Exception as e:
            log(f"⚠️ Refresh Token 刷新失败: {e}")

    # 3. 兜底尝试 Discord OAuth
    if not COOKIE and token_data is None:
        if not DISCORD_TOKEN:
            log("❌ 缺少 TREEMC_TOKEN (Discord Token) 或 TREEMC_COOKIE！")
            sys.exit(1)
        token_data = get_fresh_token_via_discord(session)
        build_sb_cookie(session, token_data)

    try:
        do_renew(session)
        time_left = fetch_time_left(session)
        send_tg("✅ 续期成功", time_left)
        sys.exit(0)
    except Exception as e:
        log(f"❌ 续期失败: {e}")
        time_left = fetch_time_left(session)
        send_tg("❌ 续期失败", time_left)
        sys.exit(1)


if __name__ == "__main__":
    main()
