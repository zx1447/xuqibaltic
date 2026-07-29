#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open Hosting AFK 保活：Discord OAuth 登录 -> 挂住 wss://dash.openhosting.site/ws 心跳连接。

Open Hosting 的保活机制是「保持 AFK 页的 WebSocket 连接打开」，服务器按连接时长发放
hosting coins 并保持服务器活跃。本脚本登录成功后连上 /ws 并长时间挂住，断线自动重连，
会话失效自动重新 OAuth 登录。配合 GitHub Actions 的「跑完自然结束再自我触发」实现 7x24 常驻。
"""
from __future__ import annotations
import datetime as dt
import json, os, sys, time, urllib.parse
import requests
import websocket  # websocket-client
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://dash.openhosting.site"
WS_URL = "wss://dash.openhosting.site/ws"
DISCORD_API = "https://discord.com/api/v9"
STATE_FILE = "openhosting_state.json"

TOKEN = os.environ.get("OH_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("OH_SESSION_KEY", "").strip()
PROXY = os.environ.get("OH_PROXY", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
# 单轮挂多久（分钟）。GitHub Actions 单 job 上限 6h，留余量。
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "340"))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class OHError(RuntimeError):
    pass


def log(x):
    print(x, flush=True)


def now_str():
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def send_tg(m):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": m}, timeout=15)
        except Exception:
            pass


def http_session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    return s


# ---------- state ----------
def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(st):
    tmp = STATE_FILE + ".tmp"
    open(tmp, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE_FILE)


def save_cookies(st, cookies: dict):
    if SESSION_KEY and cookies:
        st["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
            json.dumps(cookies, separators=(",", ":")).encode()).decode()
        st["session_saved_time"] = now_str()


def restore_cookies(st) -> dict | None:
    if not SESSION_KEY or not st.get("encrypted_cookies"):
        return None
    try:
        return json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return None


# ---------- login ----------
def discord_oauth_cookies() -> dict:
    """走完 Discord OAuth，返回 Open Hosting 的会话 cookies（含 connect.sid）。"""
    if not TOKEN:
        raise OHError("缺少 OH_DISCORD_TOKEN")
    s = http_session()
    e = s.get(f"{BASE}/login", allow_redirects=False, timeout=25)
    loc = e.headers.get("location", "")
    if "discord.com" not in loc:
        raise OHError(f"Open Hosting 登录入口异常：HTTP {e.status_code}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    params = {k: v[0] for k, v in q.items()}
    d = requests.Session(); d.trust_env = False
    a = d.post(f"{DISCORD_API}/oauth2/authorize", params=params,
               json={"permissions": "0", "authorize": True, "integration_type": 0,
                     "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000}},
               headers={"Authorization": TOKEN, "Content-Type": "application/json",
                        "Origin": "https://discord.com", "Referer": loc, "Accept": "*/*"},
               allow_redirects=False, timeout=30)
    if a.status_code != 200:
        raise OHError(f"Discord OAuth 失败：HTTP {a.status_code} {a.text[:200]}")
    cb = a.json().get("location", "")
    if not cb:
        raise OHError("OAuth 未返回 callback")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(cb).query).get("code", [""])[0]
    if not code:
        raise OHError("OAuth callback 缺少 code")
    # /submitlogin?code=... 设置 connect.sid
    r = s.get(f"{BASE}/submitlogin", params={"code": code}, allow_redirects=True, timeout=30)
    cookies = {c.name: c.value for c in s.cookies}
    if "connect.sid" not in cookies:
        raise OHError(f"登录后未拿到 connect.sid（最终 URL: {r.url}）")
    log("✅ Open Hosting Discord 登录成功")
    return cookies


def ws_connect(cookies: dict):
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    ws = websocket.create_connection(
        WS_URL,
        header={"Cookie": cookie_str, "User-Agent": UA, "Origin": BASE},
        timeout=30,
    )
    return ws


def ws_alive(cookies: dict) -> bool:
    """快速验证：能否用当前 cookies 连上 /ws。"""
    try:
        ws = ws_connect(cookies)
        ok = ws.connected
        ws.close()
        return ok
    except Exception:
        return False


# ---------- main loop ----------
def hold(cookies: dict, deadline: float, st: dict) -> dict:
    """挂住 ws 直到 deadline；断线重连；返回更新后的 cookies（可能重新登录过）。"""
    reconnects = 0
    while time.time() < deadline:
        ws = None
        try:
            ws = ws_connect(cookies)
            log(f"🔌 已连接 {WS_URL}，保持心跳中… {now_str()}")
            ws.settimeout(30)
            last_ping = time.time()
            while time.time() < deadline:
                # 定期 ping 保活 + 探测死连接
                if time.time() - last_ping > 25:
                    ws.ping()
                    last_ping = time.time()
                try:
                    ws.recv()  # 服务器静默，通常超时
                except websocket.WebSocketTimeoutException:
                    pass
                if not ws.connected:
                    raise ConnectionError("connection closed by server")
        except Exception as e:
            msg = str(e)
            log(f"⚠️ 连接中断：{type(e).__name__}: {msg[:160]}")
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            # 判断是否会话失效（401/403/拒绝握手）-> 重新登录
            low = msg.lower()
            if any(x in low for x in ("401", "403", "unauthorized", "forbidden", "handshake", "redirect")):
                log("🔐 会话可能失效，重新 Discord OAuth 登录")
                try:
                    cookies = discord_oauth_cookies()
                    save_cookies(st, cookies); save(st)
                except Exception as le:
                    log(f"❌ 重新登录失败：{le}")
                    time.sleep(15)
            reconnects += 1
            if time.time() < deadline:
                time.sleep(2)  # 与前端 onclose 2s 重连一致
    log(f"🏁 本轮挂住结束，重连次数：{reconnects}")
    st["last_reconnects"] = reconnects
    return cookies


def main():
    log("🚀 Open Hosting AFK 保活启动"); log(f"🕐 北京时间：{now_str()}")
    log(f"⏳ 本轮计划挂住 {RUN_MINUTES} 分钟（之后自然结束，由 workflow 重新触发下一轮）")
    st = load()
    try:
        cookies = restore_cookies(st)
        if cookies and ws_alive(cookies):
            log("♻️ 复用已保存的 Open Hosting 会话")
        else:
            if cookies:
                log("⌛ 已保存会话失效，重新登录")
            cookies = discord_oauth_cookies()
            save_cookies(st, cookies); save(st)

        st["last_start_time"] = now_str(); save(st)
        send_tg(f"🟢 Open Hosting AFK 保活已启动\n🕐 {now_str()}\n⏳ 本轮 {RUN_MINUTES} 分钟")

        deadline = time.time() + RUN_MINUTES * 60
        cookies = hold(cookies, deadline, st)

        save_cookies(st, cookies)
        st["last_end_time"] = now_str()
        save(st)
        send_tg(f"✅ Open Hosting AFK 本轮结束\n🕐 {now_str()}\n🔁 等待 workflow 触发下一轮")
        return 0
    except Exception as e:
        log(f"❌ Open Hosting 失败：{type(e).__name__}: {e}")
        send_tg(f"❌ Open Hosting AFK 失败\n{type(e).__name__}: {e}")
        st["last_error"] = f"{type(e).__name__}: {e}"; st["last_error_time"] = now_str(); save(st)
        return 1


if __name__ == "__main__":
    sys.exit(main())
