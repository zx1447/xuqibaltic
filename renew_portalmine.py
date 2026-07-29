#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PortalMine login + server_state checker.

Confirmed from F12 / JS:
- Login API: POST /api/auth.php?action=login
- Session check: GET /api/auth.php?action=me
- Server state: GET /server-v16132.php?action=server_state

Note: dashboard has a 60-second "Earn coins" reward button. This script does
not automate rewarded/ad coin sessions; it only logs in and checks server state.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from http.cookies import SimpleCookie

import requests

BASE = "https://portalmine.com"
LOGIN_URL = f"{BASE}/api/auth.php?action=login"
ME_URL = f"{BASE}/api/auth.php?action=me"
STATE_URL = f"{BASE}/server-v16132.php?action=server_state"
STATE_FILE = "portalmine_state.json"
USER = os.environ.get("PORTALMINE_USER", "").strip()
PASSWORD = os.environ.get("PORTALMINE_PASS", "").strip()
COOKIE = os.environ.get("PORTALMINE_COOKIE", "").strip()
POLL_COUNT = int(os.environ.get("PORTALMINE_POLL_COUNT", "1") or "1")
POLL_SECONDS = int(os.environ.get("PORTALMINE_POLL_SECONDS", "60") or "60")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

class PortalMineError(RuntimeError):
    pass

def log(msg: str) -> None:
    print(msg, flush=True)

def now_cn() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def send_tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception:
        pass

def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"})
    if COOKIE:
        c = SimpleCookie()
        try:
            c.load(COOKIE)
            for k, m in c.items():
                if k.lower() not in {"path", "expires", "max-age", "httponly", "samesite"}:
                    s.cookies.set(k, m.value, domain="portalmine.com", path="/")
        except Exception:
            for item in COOKIE.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    if k.lower() not in {"path", "expires", "max-age", "httponly", "samesite"}:
                        s.cookies.set(k, v, domain="portalmine.com", path="/")
    return s

def parse_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}

def check_me(s: requests.Session) -> dict:
    r = s.get(ME_URL, timeout=20)
    data = parse_json(r)
    log(f"📡 GET auth me -> HTTP {r.status_code}; ok={data.get('ok')}")
    return data

def login(s: requests.Session) -> dict:
    if not USER or not PASSWORD:
        raise PortalMineError("缺少 PORTALMINE_USER/PORTALMINE_PASS")
    r = s.post(
        LOGIN_URL,
        data={"login_input": USER, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE, "Referer": BASE + "/"},
        timeout=30,
    )
    data = parse_json(r)
    log(f"📡 POST auth login -> HTTP {r.status_code}; ok={data.get('ok')}; remembered={data.get('remembered')}")
    if r.status_code != 200 or not data.get("ok"):
        raise PortalMineError(f"登录失败：{str(data)[:300]}")
    user = data.get("user") or {}
    log(f"✅ PortalMine 登录成功：user={user.get('username')} coins={user.get('coins')}")
    return data

def get_server_state(s: requests.Session) -> dict:
    r = s.get(STATE_URL, timeout=25)
    data = parse_json(r)
    log(f"📡 GET server_state -> HTTP {r.status_code}; ok={data.get('ok')}; state={data.get('state') or data.get('current_state')}")
    if r.status_code != 200 or not data.get("ok"):
        raise PortalMineError(f"server_state 失败：{str(data)[:300]}")
    return data

def summarize_state(data: dict) -> dict:
    keys = [
        "state", "current_state", "state_label", "install_status", "name", "version", "type", "platform",
        "display_address", "account_level", "slots", "limit_memory_mb", "limit_disk_mb", "limit_cpu",
        "cycle_left", "uptime", "cpu", "memory", "disk", "players_online", "coins",
    ]
    return {k: data.get(k) for k in keys if k in data}

def main() -> int:
    log("🚀 PortalMine server_state 检测启动")
    log(f"🕐 北京时间：{now_cn()}")
    state = load_state()
    try:
        s = make_session()
        me = check_me(s)
        if not me.get("ok"):
            login(s)
            me = check_me(s)
        if not me.get("ok"):
            raise PortalMineError("登录后 auth me 仍未授权")

        poll_count = max(1, min(POLL_COUNT, 10))
        last = None
        for i in range(poll_count):
            if i:
                log(f"⏳ 等待 {POLL_SECONDS} 秒后再次检查 server_state ({i+1}/{poll_count})")
                time.sleep(max(1, min(POLL_SECONDS, 300)))
            last = get_server_state(s)

        summary = summarize_state(last or {})
        state.update({
            "last_check_timestamp": int(time.time()),
            "last_check_time": now_cn(),
            "last_status": "success",
            "server_state": summary,
            "poll_count": poll_count,
        })
        save_state(state)
        send_tg(f"✅ PortalMine 检测成功\n🕐 {now_cn()}\n📌 {summary.get('state_label') or summary.get('state')}\n🖥️ {summary.get('display_address', '')}")
        log("✅ PortalMine 检测完成：" + json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        log(f"❌ PortalMine 失败：{type(exc).__name__}: {exc}")
        state.update({"last_check_time": now_cn(), "last_status": f"ERROR: {exc}"})
        save_state(state)
        send_tg(f"❌ PortalMine 失败\n{exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
