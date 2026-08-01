#!/usr/bin/env python3
"""BOT SERVER daily reward automation that strictly respects the site's cooldown."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://bot-server.site"
DAILY_URL = f"{BASE_URL}/dashboard/daily"
OAUTH_ENTRY = f"{BASE_URL}/auth/discord"
DISCORD_API = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_TOKEN = os.environ.get("BOTSERVER_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("BOTSERVER_SESSION_KEY", "").strip()
STATE_FILE = Path("botserver_state.json")
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
)


def log(message: str) -> None:
    print(message, flush=True)


def now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def timestamp_text(timestamp: float | int | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(float(timestamp), CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def decrypt_cookies(state: dict) -> list[dict]:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return []
    try:
        raw = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode())
        cookies = json.loads(raw.decode())
        return cookies if isinstance(cookies, list) else []
    except (InvalidToken, ValueError, TypeError):
        return []


def encrypt_cookies(session: requests.Session, state: dict) -> None:
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_cn()


def make_session(cookies: list[dict] | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    for item in cookies or []:
        session.cookies.set(
            item["name"],
            item["value"],
            domain=item.get("domain") or "bot-server.site",
            path=item.get("path") or "/",
        )
    return session


def get_daily_page(session: requests.Session) -> requests.Response:
    return session.get(DAILY_URL, allow_redirects=False, timeout=30)


def session_valid(session: requests.Session) -> tuple[bool, str]:
    try:
        response = get_daily_page(session)
    except requests.RequestException:
        return False, ""
    content_type = response.headers.get("Content-Type", "")
    valid = (
        response.status_code == 200
        and "text/html" in content_type
        and "Daily Reward" in response.text
        and "/auth/discord" not in response.url
    )
    return valid, response.text if valid else ""


def discord_login(session: requests.Session) -> str:
    if not DISCORD_TOKEN:
        raise RuntimeError("missing BOTSERVER_DISCORD_TOKEN")
    entry = session.get(OAUTH_ENTRY, allow_redirects=False, timeout=30)
    authorize_url = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or not authorize_url:
        raise RuntimeError(f"OAuth entry HTTP {entry.status_code}")

    query = dict(parse_qsl(urlsplit(authorize_url).query))
    api_url = f"{DISCORD_API}?{urlencode(query)}"
    headers = {
        "Authorization": DISCORD_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": "https://discord.com",
        "Referer": authorize_url,
    }
    response = requests.post(
        api_url,
        headers=headers,
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
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    callback = data.get("location") or ""
    if response.status_code != 200 or not callback:
        raise RuntimeError(
            f"Discord OAuth HTTP {response.status_code}: {str(data)[:250]}"
        )

    callback_response = session.get(callback, allow_redirects=True, timeout=60)
    if callback_response.status_code != 200 or "/dashboard" not in callback_response.url:
        raise RuntimeError(
            f"BOT SERVER OAuth callback failed: HTTP {callback_response.status_code} "
            f"URL {callback_response.url}"
        )
    valid, daily_html = session_valid(session)
    if not valid:
        raise RuntimeError("session remained unauthenticated after Discord OAuth")
    log("✅ BOT SERVER Discord OAuth 登录成功")
    return daily_html


def stat_value(soup: BeautifulSoup, label: str) -> int | None:
    for box in soup.select(".stat-box"):
        label_node = box.select_one(".stat-lbl")
        value_node = box.select_one(".stat-val")
        if not label_node or not value_node:
            continue
        if label.lower() == label_node.get_text(" ", strip=True).lower():
            match = re.search(r"-?\d+", value_node.get_text(" ", strip=True))
            return int(match.group(0)) if match else None
    return None


def parse_daily(html_text: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    end_match = re.search(r"\bvar\s+END\s*=\s*(\d{10,16})", html_text)
    next_claim_ms = int(end_match.group(1)) if end_match else 0
    next_claim_timestamp = next_claim_ms / 1000 if next_claim_ms else 0
    balance = stat_value(soup, "Balance")
    times_claimed = stat_value(soup, "Times Claimed")
    total_earned = stat_value(soup, "Total Earned")
    text = soup.get_text(" ", strip=True)
    cooldown_active = next_claim_timestamp > time.time() + 2
    return {
        "balance": balance,
        "times_claimed": times_claimed,
        "total_earned": total_earned,
        "next_claim_timestamp": int(next_claim_timestamp),
        "next_claim_time": timestamp_text(next_claim_timestamp),
        "cooldown_active": cooldown_active,
        "page_says_already_claimed": "already claimed" in text.lower(),
    }


def claim_once(session: requests.Session, before: dict) -> tuple[dict, bool]:
    response = session.post(
        DAILY_URL,
        headers={"Origin": BASE_URL, "Referer": DAILY_URL},
        allow_redirects=True,
        timeout=45,
    )
    if response.status_code != 200 or "/dashboard/daily" not in response.url:
        raise RuntimeError(
            f"daily claim HTTP {response.status_code}, final URL {response.url}"
        )
    after = parse_daily(response.text)
    before_balance = before.get("balance")
    after_balance = after.get("balance")
    before_count = before.get("times_claimed")
    after_count = after.get("times_claimed")
    claimed = (
        before_balance is not None
        and after_balance is not None
        and after_balance > before_balance
    ) or (
        before_count is not None
        and after_count is not None
        and after_count > before_count
    )
    return after, claimed


def main() -> int:
    log("🚀 BOT SERVER 正常冷却每日签到启动")
    log(f"🕐 北京时间：{now_cn()}")
    if not SESSION_KEY:
        log("❌ 缺少 BOTSERVER_SESSION_KEY")
        return 1

    state = load_state()
    session = make_session(decrypt_cookies(state))
    try:
        valid, daily_html = session_valid(session)
        login_source = "encrypted_session"
        if not valid:
            log("🔄 加密会话失效，使用 Discord OAuth 重新登录")
            session = make_session()
            daily_html = discord_login(session)
            login_source = "discord_oauth"
        else:
            log("✅ BOT SERVER 加密登录会话有效")

        before = parse_daily(daily_html)
        state.update(
            {
                "last_check_time": now_cn(),
                "last_login_source": login_source,
                "daily_url": DAILY_URL,
                "cooldown_policy": "respect_server_24_hour_cooldown",
                "before": before,
            }
        )

        if before["cooldown_active"]:
            state.update(
                {
                    "last_status": "waiting_for_cooldown",
                    "next_claim_timestamp": before["next_claim_timestamp"],
                    "next_claim_time": before["next_claim_time"],
                }
            )
            log(
                f"⏳ 仍在正常冷却中，不发送签到请求；"
                f"下次可签到时间：{before['next_claim_time']}"
            )
        else:
            log("🎁 冷却已结束，执行一次正常签到…")
            after, claimed = claim_once(session, before)
            state["after"] = after
            state["next_claim_timestamp"] = after["next_claim_timestamp"]
            state["next_claim_time"] = after["next_claim_time"]
            if claimed:
                state["last_status"] = "claim_success"
                state["last_claim_time"] = now_cn()
                state["total_claim_success"] = int(state.get("total_claim_success", 0)) + 1
                log(
                    f"✅ 每日签到成功：余额 {before.get('balance')} → "
                    f"{after.get('balance')}；下次：{after.get('next_claim_time')}"
                )
            else:
                state["last_status"] = "claim_not_confirmed"
                log(
                    "⚠️ 已按正常流程提交签到，但余额/次数未增加；"
                    f"服务器显示下次时间：{after.get('next_claim_time')}"
                )

        encrypt_cookies(session, state)
        save_state(state)
        return 0
    except Exception as exc:
        state.update(
            {
                "last_check_time": now_cn(),
                "last_status": f"ERROR: {type(exc).__name__}: {exc}",
            }
        )
        save_state(state)
        log(f"❌ BOT SERVER 失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
