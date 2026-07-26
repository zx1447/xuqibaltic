#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PidginHost cloud server renewal.

F12 renew request:
  POST https://www.pidginhost.ro/panel/cloud/servers/3853/

The site is Django-based, so a valid logged-in session cookie plus CSRF token
is required. Secrets are read from GitHub Actions environment variables; no
private token/cookie is written into the repository.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from http.cookies import SimpleCookie
from urllib.parse import urljoin

import requests

BASE = "https://www.pidginhost.ro"
SERVER_ID = os.environ.get("PIDGINHOST_SERVER_ID", "3853").strip() or "3853"
SERVER_URL = f"{BASE}/panel/cloud/servers/{SERVER_ID}/"
API_SERVER_URL = f"{BASE}/api/v1/cloud/servers/{SERVER_ID}/"
API_ACTIVITY_URL = f"{BASE}/api/v1/cloud/servers/{SERVER_ID}/activity/"
LOGIN_URL = f"{BASE}/panel/account/login?next=/panel/cloud/servers/{SERVER_ID}/"
STATE_FILE = "pidginhost_state.json"
INTERVAL_DAYS = int(os.environ.get("PIDGINHOST_INTERVAL_DAYS", "4") or "4")
FORCE = str(os.environ.get("FORCE_RUN", "")).lower() in ("1", "true", "yes", "y")

# Recommended: PIDGINHOST_COOKIE='sessionid=...; csrftoken=...'
# If only a raw token value is available, also set PIDGINHOST_COOKIE_NAME.
TOKEN = os.environ.get("PIDGINHOST_TOKEN", "").strip()
COOKIE = os.environ.get("PIDGINHOST_COOKIE", "").strip()
COOKIE_NAME = os.environ.get("PIDGINHOST_COOKIE_NAME", "").strip()
PROXY = os.environ.get("PIDGINHOST_PROXY", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class PidginHostError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def now_ts() -> int:
    return int(time.time())


def now_cn() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": BASE,
            "Referer": SERVER_URL,
        }
    )
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    apply_auth(s)
    return s


def set_cookie_header(s: requests.Session, cookie_header: str) -> None:
    c = SimpleCookie()
    c.load(cookie_header)
    for key, morsel in c.items():
        # Let requests handle domains where possible; these cookies are sent to pidginhost.ro.
        s.cookies.set(key, morsel.value, domain="www.pidginhost.ro", path="/")


def apply_auth(s: requests.Session) -> None:
    if COOKIE:
        set_cookie_header(s, COOKIE)
    elif TOKEN and ("=" in TOKEN or ";" in TOKEN):
        # User pasted a full Cookie header into PIDGINHOST_TOKEN.
        set_cookie_header(s, TOKEN)
    elif TOKEN and COOKIE_NAME:
        s.cookies.set(COOKIE_NAME, TOKEN, domain="www.pidginhost.ro", path="/")

    # PidginHost API tokens use DRF token auth, not Bearer/X-API-Key.
    # Verified: Authorization: Token <token> can read /api/v1/cloud/servers/<id>/.
    if TOKEN and "=" not in TOKEN and ";" not in TOKEN:
        s.headers.update({"Authorization": f"Token {TOKEN}"})


def is_login_redirect(resp: requests.Response) -> bool:
    loc = resp.headers.get("location", "")
    return resp.status_code in (301, 302, 303, 307, 308) and "/panel/account/login" in loc


def get_server_page(s: requests.Session) -> requests.Response:
    r = s.get(SERVER_URL, allow_redirects=False, timeout=25)
    if is_login_redirect(r):
        raise PidginHostError(
            "PidginHost 未登录：这个 POST 需要有效面板会话。请把 F12 里的完整 Cookie 放到 PIDGINHOST_COOKIE，"
            "或同时设置 PIDGINHOST_TOKEN + PIDGINHOST_COOKIE_NAME。"
        )
    if r.status_code != 200:
        raise PidginHostError(f"打开服务器页面失败：HTTP {r.status_code}")
    if "Autentificare" in r.text and "/panel/account/login" in r.text:
        raise PidginHostError("PidginHost 返回登录页，会话无效")
    return r


def extract_csrf(s: requests.Session, html: str) -> str:
    m = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)', html)
    if not m:
        m = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrfmiddlewaretoken["\']', html)
    if m:
        return m.group(1)
    token = s.cookies.get("csrftoken")
    if token:
        return token
    raise PidginHostError("未找到 CSRF Token，请补充 F12 的请求载荷和 Cookie")


def extract_hidden_fields(html: str) -> dict:
    fields = {}
    for name, value in re.findall(
        r'<input[^>]+type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\'][^>]*>',
        html,
        flags=re.I,
    ):
        fields[name] = value
    return fields



def api_server_status(s: requests.Session) -> dict:
    r = s.get(API_SERVER_URL, headers={"Accept": "application/json"}, timeout=25)
    log(f"📡 GET {API_SERVER_URL} -> HTTP {r.status_code}")
    if r.status_code == 401:
        raise PidginHostError("PidginHost API Token 无效或未按 Authorization: Token 传递")
    if r.status_code != 200:
        raise PidginHostError(f"PidginHost API 读取服务器失败：HTTP {r.status_code} {r.text[:200]}")
    data = r.json()
    log(f"✅ API Token 有效：server={data.get('id')} hostname={data.get('hostname')} status={data.get('status')}")
    return data


def api_recent_renew_log(s: requests.Session) -> str | None:
    try:
        r = s.get(API_ACTIVITY_URL, headers={"Accept": "application/json"}, timeout=25)
        if r.status_code != 200:
            return None
        logs = r.json().get("logs", [])
        for item in logs:
            msg = str(item.get("message", ""))
            if "Free VM renewal extended" in msg:
                return f"{item.get('date')} - {msg}"
    except Exception:
        return None
    return None

def renew(s: requests.Session) -> requests.Response:
    page = get_server_page(s)
    csrf = extract_csrf(s, page.text)
    data = extract_hidden_fields(page.text)
    data.setdefault("csrfmiddlewaretoken", csrf)
    headers = {
        "Referer": SERVER_URL,
        "Origin": BASE,
        "X-CSRFToken": csrf,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = s.post(SERVER_URL, data=data, headers=headers, allow_redirects=False, timeout=25)
    log(f"📡 POST {SERVER_URL} -> HTTP {r.status_code}")
    if r.status_code == 403 and "CSRF" in r.text.upper():
        raise PidginHostError("POST 被 CSRF 拦截：需要完整登录 Cookie，尤其是 csrftoken/session cookie")
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("location", "")
        if "/panel/account/login" in loc:
            raise PidginHostError("POST 后跳到登录页，会话失效")
        log(f"✅ PidginHost 返回重定向，按 F12 记录视为续期提交成功：{loc or '(无 Location)'}")
        return r
    if 200 <= r.status_code < 300:
        log("✅ PidginHost 返回 2xx，续期请求已提交")
        return r
    raise PidginHostError(f"续期失败：HTTP {r.status_code} {r.text[:300]}")


def main() -> int:
    log("🚀 PidginHost 4 天续期启动")
    log(f"🕐 北京时间：{now_cn()}")
    st = load_state()
    try:
        last = int(st.get("last_renew_timestamp") or 0)
        elapsed = now_ts() - last if last else None
        min_interval = INTERVAL_DAYS * 86400
        if last and not FORCE and elapsed is not None and elapsed < min_interval:
            remain_h = round((min_interval - elapsed) / 3600, 1)
            log(f"ℹ️ 距上次成功未满 {INTERVAL_DAYS} 天，剩余约 {remain_h} 小时，跳过")
            st["last_check_time"] = now_cn()
            save_state(st)
            return 0

        if not (TOKEN or COOKIE):
            raise PidginHostError("缺少 PIDGINHOST_TOKEN 或 PIDGINHOST_COOKIE Secret")

        s = make_session()
        if TOKEN:
            api_server_status(s)
            latest = api_recent_renew_log(s)
            if latest:
                log(f"🧾 最近续期日志：{latest}")

        if COOKIE or (TOKEN and ("=" in TOKEN or ";" in TOKEN)) or COOKIE_NAME:
            renew(s)
        else:
            raise PidginHostError(
                "API Token 已验证有效，但 PidginHost 的免费 VM 续期 F12 是面板 POST，"
                "当前 v1 API schema 没有 renew/free-renew 端点；需要完整 Cookie/CSRF 或新的 F12 API 续期请求。"
            )

        st["last_check_time"] = now_cn()
        st["last_renew_time"] = now_cn()
        st["last_renew_timestamp"] = now_ts()
        st["server_id"] = SERVER_ID
        save_state(st)
        send_tg(f"✅ PidginHost 续期完成\n🕐 {now_cn()}\n🖥️ Server: {SERVER_ID}")
        return 0
    except Exception as e:
        log(f"❌ PidginHost 续期失败：{type(e).__name__}: {e}")
        st["last_check_time"] = now_cn()
        st["last_error"] = str(e)[:500]
        save_state(st)
        send_tg(f"❌ PidginHost 续期失败\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
