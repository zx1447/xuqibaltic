#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Siam Node 每日 Discord OAuth 登录并循环签到获取积分。"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://my.siam-node.cloud"
DISCORD_API = "https://discord.com/api/v9"
CLIENT_ID = "1415389053955739753"
REDIRECT_URI = f"{BASE}/DISCORDOAUTH2/process-oauth.php"
CHECKIN_URL = f"{BASE}/api/checkin.php"
HISTORY_URL = f"{BASE}/api/user_balance.php?action=history&limit=5"
STATE_FILE = "siam_state.json"
VISIT_INTERVAL_SECONDS = 20 * 60 * 60

DISCORD_TOKEN = os.environ.get("SIAM_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("SIAM_SESSION_KEY", "").strip()
PROXY = os.environ.get("SIAM_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class SiamError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_tg(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": message}, timeout=15)
    except Exception:
        pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
        log("🔗 使用 SIAM_PROXY")
    return s


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
            return x if isinstance(x, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def save_session(state: dict, s: requests.Session) -> None:
    if not SESSION_KEY:
        return
    cookies = {c.name: c.value for c in s.cookies}
    if cookies:
        state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(json.dumps(cookies, separators=(",", ":")).encode()).decode()
        state["session_saved_time"] = now_str()


def restore_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return None
    s = make_session()
    for name, value in cookies.items():
        s.cookies.set(name, value, domain="my.siam-node.cloud", path="/")
    return s


def should_run(state: dict) -> bool:
    if FORCE_RUN:
        return True
    last = int(state.get("last_checkin_timestamp", 0) or 0)
    if last and time.time() - last < VISIT_INTERVAL_SECONDS:
        log("⏳ Siam 今日已签到，跳过本次")
        return False
    return True


def oauth_login() -> requests.Session:
    if not DISCORD_TOKEN:
        raise SiamError("缺少 SIAM_DISCORD_TOKEN")
    site = make_session()
    # 先建立 Siam 会话；站点可能要求使用可访问其面板的网络出口。
    warm = site.get(BASE, allow_redirects=False, timeout=20)
    log(f"🌐 Siam 登录入口预热：HTTP {warm.status_code}")
    oauth_url = "https://discord.com/api/v9/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "identify email",
    })
    discord = requests.Session(); discord.trust_env = False
    auth = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": "identify email"},
        json={"permissions": "0", "authorize": True, "integration_type": 0, "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000}},
        headers={"Authorization": DISCORD_TOKEN, "Content-Type": "application/json", "Origin": "https://discord.com", "Referer": oauth_url, "Accept": "*/*"},
        allow_redirects=False,
        timeout=25,
    )
    if auth.status_code != 200:
        raise SiamError(f"Discord OAuth 失败：HTTP {auth.status_code}")
    callback = auth.json().get("location", "")
    if not callback:
        raise SiamError("Discord OAuth 没返回 callback")
    log("🎫 Discord Code 获取成功，访问 Siam Callback...")
    cb = site.get(callback, allow_redirects=True, timeout=25)
    if cb.status_code >= 400:
        raise SiamError(f"Siam Callback 被拒绝：HTTP {cb.status_code}")
    log("✅ Siam Callback 完成")
    return site


def parse_json(response: requests.Response) -> dict | list:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}


def is_no_more_reward(data: object) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower()
    return any(x in text for x in ("already", "cooldown", "too soon", "limit", "no reward", "cannot", "not available", "今日", "已签到"))


def do_checkins(session: requests.Session) -> int:
    count = 0
    # 每日最多尝试 20 次；遇到“已签到/冷却/无奖励”立即停止。
    for i in range(20):
        response = session.post(CHECKIN_URL, headers={"Origin": BASE, "Referer": f"{BASE}/", "Accept": "application/json, text/plain, */*"}, timeout=20)
        data = parse_json(response)
        log(f"📡 第 {i + 1} 次签到：HTTP {response.status_code}，返回 {json.dumps(data, ensure_ascii=False)[:400]}")
        if response.status_code in (401, 403):
            raise SiamError(f"Siam 登录会话失效：HTTP {response.status_code}")
        if response.status_code != 200:
            raise SiamError(f"Siam 签到请求失败：HTTP {response.status_code}")
        count += 1
        if is_no_more_reward(data):
            log("ℹ️ Siam 返回无法继续获取积分，停止本日签到")
            break
        time.sleep(1)
    try:
        history = session.get(HISTORY_URL, headers={"Referer": f"{BASE}/"}, timeout=20)
        log(f"📊 积分历史：HTTP {history.status_code}，{history.text[:400]}")
    except Exception as exc:
        log(f"⚠️ 读取积分历史失败：{type(exc).__name__}")
    return count


def main() -> int:
    log("=" * 56); log("🚀 Siam Node 每日签到启动"); log(f"🕐 北京时间：{now_str()}"); log("=" * 56)
    state = load_state()
    if not should_run(state):
        return 0
    try:
        session = restore_session(state)
        if session is not None:
            log("♻️ 尝试复用 Siam 登录会话")
        else:
            session = oauth_login()
        count = do_checkins(session)
        state["last_checkin_timestamp"] = int(time.time())
        state["last_checkin_time"] = now_str()
        state["checkin_count"] = count
        save_session(state, session); save_state(state)
        log(f"🎉 Siam 每日签到完成，共请求 {count} 次")
        send_tg(f"✅ Siam 签到完成\n🕐 {now_str()}\n📊 请求：{count} 次")
        return 0
    except SiamError as exc:
        log(f"❌ Siam 自动签到失败：{exc}")
        send_tg(f"❌ Siam 签到失败\n🕐 {now_str()}\n📊 {exc}")
        return 1
    except Exception as exc:
        log(f"❌ Siam 异常：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
