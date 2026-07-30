#!/usr/bin/env python3
"""Enzonic API account login check every two days."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL = os.environ.get("ENZONIC_BASE_URL", "https://cloud.panel.enzonic.com").rstrip("/")
LOGIN_CHECK_URL = f"{BASE_URL}/api/client"
API_KEY = os.environ.get("ENZONIC_API_KEY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() in ("1", "true", "yes", "y")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
STATE_FILE = Path("enzonic_state.json")
LOGIN_INTERVAL_SECONDS = 2 * 24 * 60 * 60
CN_TZ = dt.timezone(dt.timedelta(hours=8))


def log(message: str) -> None:
    print(message, flush=True)


def now_text(timestamp: float | None = None) -> str:
    value = dt.datetime.fromtimestamp(timestamp, CN_TZ) if timestamp else dt.datetime.now(CN_TZ)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def send_telegram(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15,
        )
    except requests.RequestException:
        pass


def should_login(state: dict) -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，立即执行 Enzonic API 登录")
        return True
    next_timestamp = int(state.get("next_login_timestamp", 0) or 0)
    if not next_timestamp:
        log("🆕 尚无 API 登录记录，本次立即执行")
        return True
    current = int(time.time())
    if current < next_timestamp:
        remaining_hours = (next_timestamp - current) / 3600
        log(f"⏳ 尚未满 2 天；下次 API 登录：{state.get('next_login_time', now_text(next_timestamp))}，约 {remaining_hours:.1f} 小时后")
        return False
    log("🎯 已满 2 天，执行 Enzonic API 登录")
    return True


def main() -> int:
    log("=" * 58)
    log("🚀 Enzonic API 每 2 天登录检查")
    log(f"🕐 北京时间：{now_text()}")
    log("=" * 58)

    state = load_state()
    if not should_login(state):
        return 0
    if not API_KEY:
        log("❌ 缺少 ENZONIC_API_KEY")
        state.update({"last_status": "missing_api_key", "last_error_time": now_text()})
        save_state(state)
        return 1

    try:
        response = requests.get(
            LOGIN_CHECK_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Accept": "application/json",
                "User-Agent": "Enzonic-API-Login/1.0",
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        log(f"❌ Enzonic API 网络异常：{type(exc).__name__}: {exc}")
        state.update({
            "last_status": "network_error",
            "last_error": f"{type(exc).__name__}: {exc}",
            "last_error_time": now_text(),
        })
        save_state(state)
        return 1

    if response.status_code != 200:
        try:
            detail = json.dumps(response.json(), ensure_ascii=False)[:500]
        except ValueError:
            detail = response.text[:500]
        log(f"❌ Enzonic API 登录失败：HTTP {response.status_code}｜{detail}")
        state.update({
            "last_status": f"http_{response.status_code}",
            "last_error": detail,
            "last_error_time": now_text(),
        })
        save_state(state)
        return 1

    try:
        payload = response.json()
    except ValueError:
        log("❌ Enzonic API 返回的不是 JSON")
        state["last_status"] = "invalid_json"
        save_state(state)
        return 1

    if payload.get("object") != "list" or not isinstance(payload.get("data"), list):
        log(f"❌ Enzonic API 登录响应异常：{str(payload)[:500]}")
        state["last_status"] = "invalid_response"
        save_state(state)
        return 1

    servers = payload["data"]
    first = servers[0].get("attributes", {}) if servers and isinstance(servers[0], dict) else {}
    current = int(time.time())
    next_timestamp = current + LOGIN_INTERVAL_SECONDS
    state = {
        "last_status": "success",
        "last_login_timestamp": current,
        "last_login_time": now_text(current),
        "next_login_timestamp": next_timestamp,
        "next_login_time": now_text(next_timestamp),
        "interval_days": 2,
        "server_count": len(servers),
        "server_identifier": first.get("identifier"),
        "server_name": first.get("name"),
    }
    save_state(state)
    log(f"✅ Enzonic API 登录成功，服务器数量：{len(servers)}")
    log(f"📅 下次登录时间：{state['next_login_time']}")
    send_telegram(f"✅ Enzonic API 登录成功\n🕐 {state['last_login_time']}\n📅 下次：{state['next_login_time']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
