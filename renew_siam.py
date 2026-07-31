#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Siam Node 自动签到 - 纯 requests, 复用 session cookie.

策略:
1. 从 siam_state.json 读保存的 PHPSESSID cookie
2. 用 cookie 调签到 API
3. 如果 cookie 失效 (401/403/redirect to login), 用 Discord token 重新登录
4. 签到循环: 每次最多 6 次, 遇到 "limit/cooldown/no more" 停止
5. 保存新 cookie 到 state.json

环境变量:
  SIAM_DISCORD_TOKEN - Discord user token
"""
from __future__ import annotations
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import requests

BASE = "https://my.siam-node.cloud"
DISCORD_API = "https://discord.com/api/v9"
CLIENT_ID = "1415389053955739753"
REDIRECT_URI = f"{BASE}/DISCORDOAUTH2/process-oauth.php"
CHECKIN_URL = f"{BASE}/api/checkin.php"
PROFILE_URL = f"{BASE}/?p=profile"
HISTORY_URL = f"{BASE}/api/user_balance.php?action=history&limit=5"
STATE_FILE = "siam_state.json"

DISCORD_TOKEN = os.environ.get("SIAM_DISCORD_TOKEN", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"


class SiamError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    return s


def restore_session(state: dict) -> requests.Session | None:
    """从 state 恢复 session cookie."""
    phpsessid = state.get("phpsessid", "")
    if not phpsessid:
        return None
    s = make_session()
    s.cookies.set("PHPSESSID", phpsessid, domain="my.siam-node.cloud", path="/")
    log(f"♻️ 复用 session: PHPSESSID={phpsessid[:16]}...")
    return s


def save_session(state: dict, s: requests.Session) -> None:
    """保存 session cookie 到 state."""
    phpsessid = s.cookies.get("PHPSESSID", domain="my.siam-node.cloud")
    if phpsessid:
        state["phpsessid"] = phpsessid
        state["session_saved_time"] = now_str()
        log(f"💾 保存 session: PHPSESSID={phpsessid[:16]}...")


def is_session_valid(s: requests.Session) -> bool:
    """检查 session 是否有效 (访问 profile 页面, 看是否跳转到 login)."""
    try:
        r = s.get(PROFILE_URL, allow_redirects=False, timeout=15,
            headers={"Referer": f"{BASE}/"})
        # 如果 302 redirect to login, 说明 session 失效
        if r.status_code == 302 and "login" in r.headers.get("Location", "").lower():
            return False
        if r.status_code == 200 and "login" not in r.url.lower():
            return True
        return False
    except Exception:
        return False


def oauth_login() -> requests.Session:
    """用 Discord token 走 OAuth 登录 siam, 返回带 cookie 的 session."""
    if not DISCORD_TOKEN:
        raise SiamError("缺少 SIAM_DISCORD_TOKEN")
    log("🎫 Discord OAuth 登录...")
    s = make_session()
    # 先 warm up (拿 PHPSESSID)
    s.get(BASE, timeout=15)
    # 用 Discord token 获取 OAuth code
    oauth_params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "identify email",
        "prompt": "none",
    }
    r = requests.post(f"{DISCORD_API}/oauth2/authorize", params=oauth_params,
        json={"authorize": True, "integration_type": 0},
        headers={"Authorization": DISCORD_TOKEN, "Content-Type": "application/json",
            "User-Agent": UA, "Accept": "*/*"},
        timeout=25)
    if r.status_code != 200:
        raise SiamError(f"Discord OAuth 失败: HTTP {r.status_code} {r.text[:200]}")
    location = r.json().get("location", "")
    if not location or "code=" not in location:
        raise SiamError(f"Discord OAuth 没返回 code: {location}")
    log(f"✅ Discord code 获取成功")
    # 访问 callback (用 siam session, 会设 cookie)
    cb = s.get(location, allow_redirects=True, timeout=25)
    if cb.status_code >= 400:
        raise SiamError(f"Siam callback 失败: HTTP {cb.status_code}")
    log(f"✅ Siam callback 完成, final URL: {cb.url}")
    return s


def do_checkins(s: requests.Session, max_count: int = 6) -> int:
    """签到循环, 最多 max_count 次, 遇到 limit/cooldown 停止."""
    count = 0
    earned = 0
    for i in range(max_count):
        log(f"🖱️ 签到 #{i+1}/{max_count}...")
        try:
            r = s.post(CHECKIN_URL,
                headers={"Origin": BASE, "Referer": f"{BASE}/?p=topup",
                    "X-Requested-With": "XMLHttpRequest"},
                data={"action": "checkin"},
                timeout=20)
        except Exception as e:
            log(f"❌ 签到请求失败: {e}")
            break
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        log(f"📡 HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:200]}")
        # 检查 session 失效
        if r.status_code in (401, 403):
            raise SiamError(f"Session 失效: HTTP {r.status_code}")
        if r.status_code == 302 or (r.status_code == 200 and "login" in r.url.lower()):
            raise SiamError("Session 失效: redirect to login")
        if r.status_code != 200:
            log(f"⚠️ HTTP {r.status_code}, 停止")
            break
        status = data.get("status", "") if isinstance(data, dict) else ""
        if status == "error":
            msg = data.get("message", "")
            log(f"⚠️ 签到失败: {msg}")
            break
        if status == "success":
            amount = data.get("amount", 0)
            balance = data.get("balance", 0)
            count += 1
            earned += amount
            log(f"✅ +{amount} ฿ (余额: {balance} ฿)")
            # 如果有 remaining, 看是否 0
            remaining = data.get("remaining", data.get("⁹aining", -1))
            if remaining == 0:
                log("ℹ️ 今日签到次数用完, 停止")
                break
        # 签到间隔 (1 小时 cooldown, 但脚本每次跑只签 1 次, 不需要等)
        # 如果要连续签到, 需要 sleep 3600
        time.sleep(2)
    # 读余额历史
    try:
        hist = s.get(HISTORY_URL, headers={"Referer": f"{BASE}/?p=topup",
            "X-Requested-With": "XMLHttpRequest"}, timeout=15)
        log(f"📊 余额历史: {hist.text[:200]}")
    except Exception:
        pass
    log(f"🎉 签到完成: {count} 次, +{earned} ฿")
    return count


def main() -> int:
    log("=" * 50)
    log(f"🚀 Siam Node 签到启动 - {now_str()}")
    log("=" * 50)
    state = load_state()
    try:
        # 1. 尝试复用 session
        session = restore_session(state)
        if session and is_session_valid(session):
            log("✅ Session 有效, 复用")
        else:
            log("⚠️ Session 失效或不存在, 重新登录")
            session = oauth_login()
            if not is_session_valid(session):
                raise SiamError("登录后 session 仍无效")
            log("✅ 重新登录成功")
        # 2. 签到
        count = do_checkins(session, max_count=6)
        # 3. 保存 session
        save_session(state, session)
        state["last_checkin_timestamp"] = int(time.time())
        state["last_checkin_time"] = now_str()
        state["last_checkin_count"] = count
        save_state(state)
        send_tg(f"✅ Siam 签到完成\n🕐 {now_str()}\n📊 {count} 次")
        return 0
    except SiamError as e:
        log(f"❌ {e}")
        # 如果失败, 清除 session (下次重新登录)
        state.pop("phpsessid", None)
        state["last_error"] = str(e)
        state["last_error_time"] = now_str()
        save_state(state)
        send_tg(f"❌ Siam 签到失败\n🕐 {now_str()}\n📊 {e}")
        return 1
    except Exception as e:
        log(f"❌ 异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
