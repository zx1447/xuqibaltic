#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enzonic Cloud random file-page visit.

- Daily workflow check.
- Actual login/visit interval is random: 1~4 days.
- Uses SeleniumBase UC browser because Enzonic/Pelican login is Livewire/Filament.
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
# Multiple visit targets. You can override with ENZONIC_VISIT_URLS as newline/comma separated URLs.
_raw_visit_urls = os.environ.get("ENZONIC_VISIT_URLS", "").strip()
if _raw_visit_urls:
    VISIT_URLS = [u.strip() for part in _raw_visit_urls.split("
") for u in part.split(",") if u.strip()]
else:
    VISIT_URLS = [FILES_URL, VERSIONS_URL]
LOGIN_URL = f"{BASE}/login"
STATE_FILE = "enzonic_state.json"
USER = os.environ.get("ENZONIC_USER", "").strip()
PASSWORD = os.environ.get("ENZONIC_PASS", "").strip()
COOKIE = os.environ.get("ENZONIC_COOKIE", "").strip()
PROXY = (os.environ.get("ENZONIC_PROXY") or os.environ.get("PROXY_SERVER") or "").strip()
FORCE_RUN = str(os.environ.get("FORCE_RUN", "")).lower() in ("1", "true", "yes", "y")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()


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


def should_run(state: dict) -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，立即执行 Enzonic 登录访问")
        return True
    next_ts = int(state.get("next_visit_timestamp") or 0)
    if not next_ts:
        log("🆕 未检测到下次访问时间，首次立即执行")
        return True
    now_ts = int(time.time())
    if now_ts < next_ts:
        hours = (next_ts - now_ts) / 3600
        log(f"⏳ 尚未到 Enzonic 随机访问时间：{state.get('next_visit_time')}，约 {hours:.1f} 小时后")
        return False
    log("🎯 已到 Enzonic 随机访问时间")
    return True


def update_next_schedule(state: dict) -> None:
    days = random.randint(1, 4)
    # Add hour/minute jitter to avoid a fixed daily pattern.
    hours = random.randint(0, 23)
    minutes = random.randint(0, 59)
    next_ts = int(time.time() + days * 86400 + hours * 3600 + minutes * 60)
    next_dt = dt.datetime.fromtimestamp(next_ts, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    )
    state["next_interval_days"] = days
    state["next_interval_hours"] = hours
    state["next_interval_minutes"] = minutes
    state["next_visit_timestamp"] = next_ts
    state["next_visit_time"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
    log(f"🎲 下次 Enzonic 随机访问：{days} 天 {hours} 小时 {minutes} 分钟后（{state['next_visit_time']}）")


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
    log("🚀 Enzonic 随机登录访问启动")
    log(f"🕐 北京时间：{now_cn()}")
    state = load_state()
    if not should_run(state):
        return 0

    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ 缺少 seleniumbase 依赖")
        return 1

    try:
        sb_kwargs = {"uc": True, "xvfb": True, "headless": False}
        if PROXY:
            sb_kwargs["proxy"] = PROXY
            log(f"🔗 使用代理：{PROXY}")
        with SB(**sb_kwargs) as sb:
            add_cookie_header(sb)
            login_if_needed(sb)
            final_url = visit_files_page(sb)

        state["last_visit_timestamp"] = int(time.time())
        state["last_visit_time"] = now_cn()
        state["server_id"] = SERVER_ID
        state["last_status"] = "success"
        state["last_url"] = final_url
        state["visit_urls_count"] = len(VISIT_URLS)
        state["visit_urls"] = VISIT_URLS
        update_next_schedule(state)
        save_state(state)
        send_tg(f"✅ Enzonic 随机访问成功\n🕐 {now_cn()}\n📌 下次：{state['next_visit_time']}")
        return 0
    except Exception as exc:
        log(f"❌ Enzonic 失败：{type(exc).__name__}: {exc}")
        state["last_visit_time"] = now_cn()
        state["last_status"] = f"ERROR: {exc}"
        save_state(state)
        send_tg(f"❌ Enzonic 失败\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
