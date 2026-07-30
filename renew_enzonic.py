#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enzonic two-account automation.

- Username/password account: log in and randomly visit one of three file pages every 2 days.
- Separate API-key account: authenticate only with GET /api/client every 2 days.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys
import time

import requests

BASE = "https://cloud.panel.enzonic.com"
SERVER_ID = os.environ.get("ENZONIC_SERVER_ID", "2d24e7a5").strip() or "2d24e7a5"
FILES_URL = os.environ.get(
    "ENZONIC_FILES_URL",
    f"{BASE}/server/{SERVER_ID}/files/edit/server.properties/",
).strip()
VERSIONS_URL = os.environ.get(
    "ENZONIC_VERSIONS_URL",
    f"{BASE}/server/{SERVER_ID}/files/versions",
).strip()
EULA_URL = os.environ.get(
    "ENZONIC_EULA_URL",
    f"{BASE}/server/{SERVER_ID}/files/edit/eula.txt/",
).strip()
# Multiple visit targets. You can override with ENZONIC_VISIT_URLS as newline/comma separated URLs.
_raw_visit_urls = os.environ.get("ENZONIC_VISIT_URLS", "").strip()
if _raw_visit_urls:
    VISIT_URLS = [u.strip() for part in _raw_visit_urls.splitlines() for u in part.split(",") if u.strip()]
else:
    VISIT_URLS = [FILES_URL, VERSIONS_URL, EULA_URL]
LOGIN_URL = f"{BASE}/login"
STATE_FILE = "enzonic_state.json"
USER = os.environ.get("ENZONIC_USER", "").strip()
PASSWORD = os.environ.get("ENZONIC_PASS", "").strip()
COOKIE = os.environ.get("ENZONIC_COOKIE", "").strip()
PROXY = (os.environ.get("ENZONIC_PROXY") or os.environ.get("PROXY_SERVER") or "").strip()
FORCE_RUN = str(os.environ.get("FORCE_RUN", "")).lower() in ("1", "true", "yes", "y")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
API_KEY = os.environ.get("ENZONIC_API_KEY", "").strip()


class EnzonicError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def now_cn() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


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
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception:
        pass


def is_due(state: dict, key: str, legacy_key: str, label: str) -> bool:
    if FORCE_RUN:
        log(f"⚡ FORCE_RUN=true，立即执行 Enzonic {label}")
        return True
    next_ts = int(state.get(key) or state.get(legacy_key) or 0)
    if not next_ts:
        log(f"🆕 未检测到 Enzonic {label}时间，本次立即执行")
        return True
    current = int(time.time())
    if current < next_ts:
        hours = (next_ts - current) / 3600
        log(f"⏳ Enzonic {label}尚未满 2 天，约 {hours:.1f} 小时后")
        return False
    log(f"🎯 Enzonic {label}已满 2 天")
    return True


def update_schedule(state: dict, prefix: str, label: str) -> None:
    next_ts = int(time.time() + 2 * 86400)
    next_time = dt.datetime.fromtimestamp(next_ts, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    state[f"next_{prefix}_timestamp"] = next_ts
    state[f"next_{prefix}_time"] = next_time
    log(f"📅 下次 Enzonic {label}：{next_time}")


def api_headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def api_get_json(path: str, timeout: int = 15) -> dict:
    url = path if path.startswith("http") else BASE + path
    resp = requests.get(url, headers=api_headers(), timeout=timeout)
    log(f"📡 API GET {path} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise EnzonicError(f"API 请求失败 HTTP {resp.status_code}: {str(data)[:300]}")
    return data


def api_login_only() -> dict:
    """Authenticate the separate API account; do not visit random API endpoints."""
    if not API_KEY:
        raise EnzonicError("缺少 ENZONIC_API_KEY")
    data = api_get_json("/api/client", timeout=20)
    servers = data.get("data")
    if data.get("object") != "list" or not isinstance(servers, list):
        raise EnzonicError(f"Enzonic API 登录响应异常：{str(data)[:300]}")
    first = servers[0].get("attributes", {}) if servers and isinstance(servers[0], dict) else {}
    result = {
        "server_count": len(servers),
        "server_identifier": first.get("identifier"),
        "server_name": first.get("name"),
    }
    log("✅ Enzonic 独立 API 账号登录成功：" + json.dumps(result, ensure_ascii=False))
    return result


def add_cookie_header(sb) -> None:
    if not COOKIE:
        return
    log("🍪 写入 ENZONIC_COOKIE")
    sb.open(BASE)
    sb.wait_for_ready_state_complete()
    for item in COOKIE.split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        if not name:
            continue
        try:
            sb.add_cookie({"name": name, "value": value, "domain": "cloud.panel.enzonic.com", "path": "/"})
        except Exception as exc:
            log(f"⚠️ 添加 Cookie {name} 提示：{type(exc).__name__}")
    sb.refresh()
    time.sleep(2)


def login_if_needed(sb) -> None:
    sb.open(FILES_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)
    cur = sb.get_current_url()
    log(f"📍 初始页面：{cur}")
    if "/login" not in cur.lower():
        log("✅ 已有 Enzonic 登录态")
        return

    if not USER or not PASSWORD:
        raise EnzonicError("缺少 ENZONIC_USER/ENZONIC_PASS，且没有有效 Cookie")

    log("🔑 填写 Enzonic 账号密码")
    # Filament/Pelican login fields.
    selectors_user = ['input[id="form.login"]', 'input[wire\\:model="data.login"]', 'input[type="text"]']
    selectors_pass = ['input[id="form.password"]', 'input[wire\\:model="data.password"]', 'input[type="password"]']

    user_filled = False
    for sel in selectors_user:
        try:
            if sb.is_element_visible(sel):
                sb.type(sel, USER, timeout=10)
                user_filled = True
                break
        except Exception:
            pass
    pass_filled = False
    for sel in selectors_pass:
        try:
            if sb.is_element_visible(sel):
                sb.type(sel, PASSWORD, timeout=10)
                pass_filled = True
                break
        except Exception:
            pass
    if not user_filled or not pass_filled:
        raise EnzonicError("未找到 Enzonic 登录输入框")

    # Remember me is optional.
    try:
        if sb.is_element_visible('input[id="form.remember"]'):
            sb.click('input[id="form.remember"]')
    except Exception:
        pass

    log("🖱️ 点击 Sign in")
    try:
        if sb.is_element_visible('button[type="submit"]'):
            sb.uc_click('button[type="submit"]')
        else:
            sb.press_keys('input[id="form.password"]', "\n")
    except Exception:
        sb.press_keys('input[id="form.password"]', "\n")
    time.sleep(8)

    sb.open(FILES_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(4)
    cur = sb.get_current_url()
    log(f"📍 登录后访问 Files：{cur}")
    if "/login" in cur.lower():
        text = ""
        try:
            text = sb.get_text("body")[:500]
        except Exception:
            pass
        raise EnzonicError(f"Enzonic 登录失败，仍在登录页。页面摘要：{text}")


def visit_files_page(sb) -> str:
    # Randomly visit one target from the configured Enzonic pages.
    targets = [u for u in VISIT_URLS if u]
    if not targets:
        targets = [FILES_URL]
    target = random.choice(targets)
    log(f"🎲 本次从 {len(targets)} 个 Enzonic 地址中随机选择访问")
    log(f"🌐 随机访问 Enzonic 页面：{target}")
    sb.open(target)
    sb.wait_for_ready_state_complete()
    time.sleep(random.randint(6, 12))
    cur = sb.get_current_url()
    title = ""
    try:
        title = sb.get_title()
    except Exception:
        pass
    log(f"✅ Enzonic 页面访问完成：{cur} title={title[:120]}")
    if "/login" in cur.lower():
        raise EnzonicError("访问 Enzonic 页面时被重定向到登录页")
    return cur


def main() -> int:
    log("=" * 62)
    log("🚀 Enzonic 双账号每 2 天任务")
    log(f"🕐 北京时间：{now_cn()}")
    log("=" * 62)
    state = load_state()
    browser_due = is_due(
        state, "next_browser_visit_timestamp", "next_visit_timestamp", "账号密码随机页面访问"
    )
    api_due = is_due(
        state, "next_api_login_timestamp", "next_login_timestamp", "独立 API 账号登录"
    )
    if not browser_due and not api_due:
        return 0

    errors: list[str] = []

    if browser_due:
        try:
            from seleniumbase import SB
            sb_kwargs = {"uc": True, "xvfb": True, "headless": False}
            if PROXY:
                sb_kwargs["proxy"] = PROXY
                log(f"🔗 使用代理：{PROXY}")
            with SB(**sb_kwargs) as sb:
                add_cookie_header(sb)
                login_if_needed(sb)
                final_url = visit_files_page(sb)

            current = int(time.time())
            state.update({
                "last_browser_visit_timestamp": current,
                "last_browser_visit_time": now_cn(),
                "last_browser_url": final_url,
                "browser_server_id": SERVER_ID,
                "browser_visit_urls_count": len(VISIT_URLS),
                "browser_visit_urls": VISIT_URLS,
                "browser_status": "success",
            })
            update_schedule(state, "browser_visit", "账号密码随机页面访问")
            log("✅ Enzonic 账号密码登录及随机页面访问完成")
        except Exception as exc:
            message = f"浏览器账号失败：{type(exc).__name__}: {exc}"
            log(f"❌ {message}")
            state.update({"browser_status": "failed", "browser_error": str(exc)})
            errors.append(message)

    if api_due:
        try:
            api_result = api_login_only()
            current = int(time.time())
            state.update({
                "last_api_login_timestamp": current,
                "last_api_login_time": now_cn(),
                "last_api_login_result": api_result,
                "api_status": "success",
            })
            update_schedule(state, "api_login", "独立 API 账号登录")
        except Exception as exc:
            message = f"API 账号失败：{type(exc).__name__}: {exc}"
            log(f"❌ {message}")
            state.update({"api_status": "failed", "api_error": str(exc)})
            errors.append(message)

    # 删除旧版的随机 API/单一计划字段，避免两个账号再次混在一起。
    for key in (
        "next_visit_timestamp", "next_visit_time", "next_login_timestamp", "next_login_time",
        "last_api_result", "last_url", "visit_urls", "visit_urls_count",
        "next_interval_days", "next_interval_hours", "next_interval_minutes",
    ):
        state.pop(key, None)

    state["last_check_time"] = now_cn()
    state["last_status"] = "success" if not errors else "partial_failure"
    save_state(state)
    if errors:
        send_tg("❌ Enzonic 双账号任务失败\n" + "\n".join(errors))
        return 1
    send_tg(f"✅ Enzonic 双账号任务成功\n🕐 {now_cn()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
