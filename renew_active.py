#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Active two-account automation + Phenix Pterodactyl server 1-day monitor.

- Phenix Server: check every 1 day whether server c0fbebc6 is running, start it if offline/stopped.
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
SERVER_ID = (os.environ.get("ACTIVE_SERVER_ID") or os.environ.get("ENZONIC_SERVER_ID") or "2d24e7a5").strip() or "2d24e7a5"
FILES_URL = os.environ.get(
    "ACTIVE_FILES_URL",
    f"{BASE}/server/{SERVER_ID}/files/edit/server.properties/",
).strip()
VERSIONS_URL = os.environ.get(
    "ACTIVE_VERSIONS_URL",
    f"{BASE}/server/{SERVER_ID}/files/versions",
).strip()
EULA_URL = os.environ.get(
    "ACTIVE_EULA_URL",
    f"{BASE}/server/{SERVER_ID}/files/edit/eula.txt/",
).strip()
# Multiple visit targets.
_raw_visit_urls = (os.environ.get("ACTIVE_VISIT_URLS") or os.environ.get("ENZONIC_VISIT_URLS") or "").strip()
if _raw_visit_urls:
    VISIT_URLS = [u.strip() for part in _raw_visit_urls.splitlines() for u in part.split(",") if u.strip()]
else:
    VISIT_URLS = [FILES_URL, VERSIONS_URL, EULA_URL]
LOGIN_URL = f"{BASE}/login"
STATE_FILE = "active_state.json"
USER = (os.environ.get("ACTIVE_USER") or os.environ.get("ENZONIC_USER") or "").strip()
PASSWORD = (os.environ.get("ACTIVE_PASS") or os.environ.get("ENZONIC_PASS") or "").strip()
COOKIE = (os.environ.get("ACTIVE_COOKIE") or os.environ.get("ENZONIC_COOKIE") or "").strip()
PROXY = (os.environ.get("ACTIVE_PROXY") or os.environ.get("ENZONIC_PROXY") or os.environ.get("PROXY_SERVER") or "").strip()
FORCE_RUN = str(os.environ.get("FORCE_RUN", "")).lower() in ("1", "true", "yes", "y")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
API_KEY = (os.environ.get("ACTIVE_API_KEY") or os.environ.get("ENZONIC_API_KEY") or "").strip()

PHENIX_API_KEY = os.environ.get("PHENIX_API_KEY", "ptlc_RWFNcye9qasLUCaJpiY5YRGgzCYI7yRIhv7IUIcySOw").strip()
PHENIX_SERVER_UUID = os.environ.get("PHENIX_SERVER_UUID", "c0fbebc6").strip()


class ActiveError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def now_cn() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        elif os.path.exists("enzonic_state.json"):
            data = json.loads(Path("enzonic_state.json").read_text(encoding="utf-8"))
        else:
            data = {}
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
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


def is_due(state: dict, key: str, legacy_key: str, label: str, interval_days: int = 2) -> bool:
    if FORCE_RUN:
        log(f"⚡ FORCE_RUN=true，立即执行 Active {label}")
        return True
    next_ts = int(state.get(key) or (state.get(legacy_key) if legacy_key else 0) or 0)
    if not next_ts:
        log(f"🆕 未检测到 Active {label}时间，本次立即执行")
        return True
    current = int(time.time())
    if current < next_ts:
        hours = (next_ts - current) / 3600
        log(f"⏳ Active {label}尚未满 {interval_days} 天，约 {hours:.1f} 小时后")
        return False
    log(f"🎯 Active {label}已满 {interval_days} 天")
    return True


def update_schedule(state: dict, prefix: str, label: str, interval_days: int = 2) -> None:
    next_ts = int(time.time() + interval_days * 86400)
    next_time = dt.datetime.fromtimestamp(next_ts, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")
    state[f"next_{prefix}_timestamp"] = next_ts
    state[f"next_{prefix}_time"] = next_time
    log(f"📅 下次 Active {label}：{next_time}")


def api_headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def check_phenix_server(state: dict) -> None:
    """每过 1 天检测 Phenix Pterodactyl 服务器状态，没运行就启动。"""
    if not PHENIX_API_KEY:
        log("ℹ️ 未配置 PHENIX_API_KEY，跳过 Phenix 服务器 1 天检测")
        return

    if not is_due(state, "next_phenix_check_timestamp", "", "Phenix 服务器 1 天检测", interval_days=1):
        return

    base_url = "https://ptly1.hosting-phenix.com/api/client"
    headers = {
        "Authorization": f"Bearer {PHENIX_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Active-Server-Monitor/1.0",
    }

    log(f"🔍 开始检测 Phenix 服务器状态 ({PHENIX_SERVER_UUID})...")
    try:
        r = requests.get(f"{base_url}/servers/{PHENIX_SERVER_UUID}/resources", headers=headers, timeout=25)
        r.raise_for_status()
        res = r.json()
        attr = res.get("attributes", {})
        state_str = attr.get("current_state", "unknown")
        log(f"📊 当前服务器状态：{state_str}")

        action_taken = "none"
        if state_str in ("offline", "stopped"):
            log("🔴 检测到服务器没运行，正在发令启动...")
            p_res = requests.post(
                f"{base_url}/servers/{PHENIX_SERVER_UUID}/power",
                headers=headers,
                json={"signal": "start"},
                timeout=25,
            )
            p_res.raise_for_status()
            log("🟢 启动指令发送成功！")
            action_taken = "started"
        else:
            log("🟢 服务器正在运行或正常过渡中，无需操作。")
            action_taken = f"running_{state_str}"

        update_schedule(state, "phenix_check", "Phenix 服务器 1 天检测", interval_days=1)
        state.update({
            "last_phenix_check_timestamp": int(time.time()),
            "last_phenix_check_time": now_cn(),
            "last_phenix_server_state": state_str,
            "last_phenix_action": action_taken,
        })
        log("✅ Phenix 服务器 1 天检测完成")
    except Exception as exc:
        log(f"❌ Phenix 服务器检测异常：{type(exc).__name__}: {exc}")
        state.update({
            "last_phenix_error": str(exc),
            "last_phenix_error_time": now_cn(),
        })
        raise


def api_login_only() -> dict:
    if not API_KEY:
        raise ActiveError("缺少 ACTIVE_API_KEY/ENZONIC_API_KEY")
    response = requests.get(f"{BASE}/api/client", headers=api_headers(), timeout=25)
    if response.status_code != 200:
        detail = response.text[:300]
        raise ActiveError(f"API HTTP {response.status_code}: {detail}")
    payload = response.json()
    if payload.get("object") != "list" or not isinstance(payload.get("data"), list):
        raise ActiveError("API /api/client 响应不为正确的服务列表数据结构")
    servers = payload["data"]
    log(f"✅ Active API 登录成功，获取到 {len(servers)} 个服务器项")
    first = servers[0].get("attributes", {}) if servers and isinstance(servers[0], dict) else {}
    return {
        "server_count": len(servers),
        "first_server_id": first.get("identifier"),
        "first_server_name": first.get("name"),
    }


def add_cookie_header(sb) -> None:
    if not COOKIE:
        return
    log("🍪 载入会话 Cookie")
    sb.open(BASE)
    time.sleep(2)
    pairs = [part.strip().split("=", 1) for part in COOKIE.split(";") if "=" in part]
    for name, val in pairs:
        try:
            sb.add_cookie({"name": name.strip(), "value": val.strip(), "domain": ".enzonic.com", "path": "/"})
        except Exception:
            pass


def login_if_needed(sb) -> None:
    sb.open(f"{BASE}/dashboard")
    time.sleep(4)
    cur = sb.get_current_url()
    if "/login" not in cur.lower():
        log(f"✅ 会话直接访问后台成功：{cur}")
        return
    log("🔄 跳转了登录页，使用账号密码重新登录")
    if not USER or not PASSWORD:
        raise ActiveError("到达登录页但未配置 ACTIVE_USER 和 ACTIVE_PASS")
    sb.type("input[name='username'], input[type='text'], input[id*='user'], input[id*='email']", USER)
    sb.type("input[name='password'], input[type='password']", PASSWORD)
    sb.click("button[type='submit'], input[type='submit']")
    time.sleep(5)
    cur = sb.get_current_url()
    if "/login" in cur.lower():
        raise ActiveError(f"登录提交后停留在：{cur}")
    log(f"✅ 账号密码登录成功，最终页面：{cur}")


def visit_files_page(sb) -> str:
    target = random.choice(VISIT_URLS)
    log(f"🎲 本轮选中的访问地址：{target}")
    sb.open(target)
    time.sleep(6)
    cur = sb.get_current_url()
    try:
        title = sb.get_title()
    except Exception:
        title = ""
    log(f"✅ Active 页面访问完成：{cur} title={title[:120]}")
    if "/login" in cur.lower():
        raise ActiveError("访问 Active 页面时被重定向到登录页")
    return cur


def main() -> int:
    log("=" * 62)
    log("🚀 Active 多任务自动化（Web 访问 + API 检查 + Phenix 服务器 1 天检测）")
    log(f"🕐 北京时间：{now_cn()}")
    log("=" * 62)
    state = load_state()

    errors: list[str] = []

    # 1. Phenix Pterodactyl 服务器状态 1 天检查与自启
    try:
        check_phenix_server(state)
    except Exception as exc:
        message = f"Phenix 服务器检查失败：{type(exc).__name__}: {exc}"
        log(f"❌ {message}")
        errors.append(message)

    # 2. 原账号密码随机页面访问（2 天检查）
    browser_due = is_due(
        state, "next_browser_visit_timestamp", "next_visit_timestamp", "账号密码随机页面访问", interval_days=2
    )
    api_due = is_due(
        state, "next_api_login_timestamp", "next_login_timestamp", "独立 API 账号登录", interval_days=2
    )

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
            update_schedule(state, "browser_visit", "账号密码随机页面访问", interval_days=2)
            log("✅ Active 账号密码登录及随机页面访问完成")
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
            update_schedule(state, "api_login", "独立 API 账号登录", interval_days=2)
        except Exception as exc:
            message = f"API 账号失败：{type(exc).__name__}: {exc}"
            log(f"❌ {message}")
            state.update({"api_status": "failed", "api_error": str(exc)})
            errors.append(message)

    # 清理旧版冗余字段
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
        send_tg("❌ Active 任务有异常\n" + "\n".join(errors))
        return 1
    send_tg(f"✅ Active 任务成功\n🕐 {now_cn()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
