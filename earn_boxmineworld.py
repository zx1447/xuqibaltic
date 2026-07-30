#!/usr/bin/env python3
"""BoxMineWorld AFK earning via Discord OAuth and the official WebSocket protocol."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import websocket
from cryptography.fernet import Fernet, InvalidToken

WEB_ORIGIN = "https://afk.boxmineworld.com"
API_BASE = "https://afkapi.boxmineworld.com"
ME_URL = f"{API_BASE}/auth/me"
WS_URL = "wss://afkapi.boxmineworld.com/earn"
DISCORD_API = "https://discord.com/api/v9"
DISCORD_AUTH_URL = (
    "https://discord.com/api/oauth2/authorize"
    "?client_id=722536749217087538"
    "&redirect_uri=https%3A%2F%2Fafkapi.boxmineworld.com%2Fauth%2Fcallback%2Fdiscord"
    "&response_type=code&scope=identify%20guilds"
)
STATE_FILE = Path("boxmineworld_state.json")
DISCORD_TOKEN = os.environ.get("BOXMINEWORLD_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("BOXMINEWORLD_SESSION_KEY", "").strip()
RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class BoxMineWorldError(RuntimeError):
    pass


def now_text() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(message, flush=True)


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


def make_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": WEB_ORIGIN,
    })
    return session


def save_encrypted_cookies(session: requests.Session, state: dict) -> None:
    if not SESSION_KEY:
        raise BoxMineWorldError("缺少 BOXMINEWORLD_SESSION_KEY")
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    if not cookies:
        raise BoxMineWorldError("没有可保存的 BoxMineWorld Cookie")
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_text()


def restore_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session()
    try:
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain"), path=cookie.get("path", "/"),
            )
    except (KeyError, TypeError):
        return None
    return session


def validate_session(session: requests.Session) -> str | None:
    try:
        response = session.get(ME_URL, timeout=20)
        if response.status_code != 200:
            return None
        data = response.json()
        return str(data.get("name")) if data.get("name") else None
    except (requests.RequestException, ValueError):
        return None


def discord_login() -> tuple[requests.Session, str]:
    if not DISCORD_TOKEN:
        raise BoxMineWorldError("缺少 BOXMINEWORLD_DISCORD_TOKEN")
    parsed = urllib.parse.urlparse(DISCORD_AUTH_URL)
    params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
    discord = requests.Session()
    discord.trust_env = False
    response = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params=params,
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
        headers={
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": DISCORD_AUTH_URL,
            "User-Agent": UA,
        },
        timeout=30,
    )
    if response.status_code == 401:
        raise BoxMineWorldError("Discord Token 无效或已失效")
    if response.status_code != 200:
        raise BoxMineWorldError(f"Discord OAuth 失败：HTTP {response.status_code}")
    callback = response.json().get("location", "")
    if not callback:
        raise BoxMineWorldError("Discord OAuth 未返回 Callback")

    site = make_session()
    callback_response = site.get(callback, allow_redirects=False, timeout=30)
    if callback_response.status_code not in (301, 302, 303, 307, 308):
        raise BoxMineWorldError(f"BoxMineWorld Callback 异常：HTTP {callback_response.status_code}")
    username = validate_session(site)
    if not username:
        raise BoxMineWorldError("Callback 后 /auth/me 仍未登录")
    return site, username


def cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)


def connect_websocket(session: requests.Session) -> websocket.WebSocket:
    return websocket.create_connection(
        WS_URL,
        cookie=cookie_header(session),
        origin=WEB_ORIGIN,
        header=[f"User-Agent: {UA}"],
        timeout=12,
        enable_multithread=True,
        http_proxy_host=None,
        http_proxy_port=None,
    )


def run_afk(session: requests.Session, state: dict) -> int:
    deadline = time.monotonic() + RUN_MINUTES * 60
    connections = 0
    messages = 0
    activity_responses = 0
    reconnects = 0
    connected_once = False

    while time.monotonic() < deadline:
        ws = None
        try:
            ws = connect_websocket(session)
            connected_once = True
            connections += 1
            subscription_id = str(uuid.uuid4())
            ws.send(json.dumps({
                "type": "subscribe",
                "path": "/earn",
                "subscriptionId": subscription_id,
            }))
            log(f"🔌 AFK WebSocket 已连接（第 {connections} 次）")
            state.update({"last_status": "connected", "last_connected_time": now_text()})
            save_state(state)
            last_ping = 0.0

            while time.monotonic() < deadline:
                if time.monotonic() - last_ping >= 20:
                    ws.send(json.dumps({"type": "ping"}))
                    last_ping = time.monotonic()
                ws.settimeout(3)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise websocket.WebSocketConnectionClosedException("empty WebSocket frame")
                message = json.loads(raw)
                messages += 1
                if message.get("subscriptionId"):
                    subscription_id = str(message["subscriptionId"])

                if message.get("type") == "activity_check" and message.get("checkId"):
                    ws.send(json.dumps({
                        "type": "activity_check_response",
                        "checkId": message["checkId"],
                        "path": "/earn",
                        "subscriptionId": subscription_id,
                    }))
                    activity_responses += 1
                    log(f"💓 已回应活跃检测 #{activity_responses}")
                    continue

                error = message.get("error")
                if isinstance(error, dict) and error.get("device"):
                    state.update({"last_status": "device_conflict", "last_error_time": now_text()})
                    save_state(state)
                    raise BoxMineWorldError("账号正在另一台设备上赚取积分")
                if error:
                    log(f"⚠️ AFK 返回错误：{error}")
                    continue

                data = message.get("data")
                if isinstance(data, dict):
                    earned = data.get("earned")
                    maximum = data.get("max_earn")
                    interval = data.get("interval")
                    cooldown = bool(data.get("cooldown"))
                    reset_at = data.get("reset_at", 0)
                    limit_reached = cooldown or (
                        isinstance(earned, (int, float))
                        and isinstance(maximum, (int, float))
                        and maximum > 0
                        and earned >= maximum
                    )
                    state.update({
                        "last_status": "daily_limit_reached" if limit_reached else "earning",
                        "last_message_time": now_text(),
                        "session_earned": earned,
                        "max_earn": maximum,
                        "earn_interval_ms": interval,
                        "cooldown": cooldown,
                        "reset_at": reset_at,
                    })
                    state.pop("last_error", None)
                    save_state(state)
                    log(f"🪙 AFK 状态：{earned}/{maximum}｜间隔 {interval}ms｜cooldown={cooldown}")
                    if limit_reached:
                        log(f"✅ 今日已获取 {earned}/{maximum} 个积分，立即结束本轮 Workflow")
                        return 0

        except BoxMineWorldError:
            raise
        except (websocket.WebSocketException, OSError, ValueError, json.JSONDecodeError) as exc:
            reconnects += 1
            state.update({
                "last_status": "reconnecting",
                "last_error": f"{type(exc).__name__}: {exc}",
                "last_error_time": now_text(),
                "reconnects": reconnects,
            })
            save_state(state)
            log(f"⚠️ WebSocket 断开，5 秒后重连：{type(exc).__name__}")
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    state.update({
        "last_status": "completed",
        "last_end_time": now_text(),
        "last_run_connections": connections,
        "last_run_messages": messages,
        "last_run_activity_responses": activity_responses,
        "last_run_reconnects": reconnects,
    })
    state.pop("last_error", None)
    save_state(state)
    log(f"🏁 本轮结束：连接 {connections} 次，消息 {messages} 条，活跃回应 {activity_responses} 次")
    return 0 if connected_once else 1


def main() -> int:
    log("=" * 60)
    log("🚀 BoxMineWorld AFK Earn 启动")
    log(f"🕐 北京时间：{now_text()}")
    log(f"⏳ 本轮运行：{RUN_MINUTES} 分钟")
    log("=" * 60)
    state = load_state()
    reset_at = int(state.get("reset_at", 0) or 0)
    if state.get("cooldown") and reset_at > int(time.time() * 1000):
        reset_text = datetime.fromtimestamp(reset_at / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        earned = state.get("session_earned", 0)
        maximum = state.get("max_earn", 8)
        state["last_status"] = "daily_limit_reached"
        state.pop("last_error", None)
        save_state(state)
        log(f"✅ 今日已获取 {earned}/{maximum} 个积分，不再启动 AFK 流")
        log(f"⏳ 每日额度预计重置时间：{reset_text}")
        return 0

    state.update({"last_start_time": now_text(), "last_status": "starting"})
    save_state(state)

    try:
        session = restore_session(state)
        username = validate_session(session) if session is not None else None
        if username:
            log(f"♻️ 复用 BoxMineWorld 登录会话：{username}")
        else:
            if session is not None:
                log("⌛ 已保存会话失效，重新 Discord OAuth 登录")
            session, username = discord_login()
            save_encrypted_cookies(session, state)
            state["username"] = username
            save_state(state)
            log(f"✅ BoxMineWorld Discord 登录成功：{username}")
        return run_afk(session, state)
    except Exception as exc:
        state.update({
            "last_status": "failed",
            "last_error": f"{type(exc).__name__}: {exc}",
            "last_error_time": now_text(),
        })
        save_state(state)
        log(f"❌ BoxMineWorld AFK 失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
