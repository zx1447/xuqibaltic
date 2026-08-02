#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flarelax 服务器开机状态检测与自动续期（已停止获取 AFK 积分）。

- 检查服务器 8a4d1879 是否正在运行，如离线/停止则发送开机命令。
- 距离上次成功续期满 48 小时自动发起服务器续期（/api/server/8a4d1879/renew）。
- 移除了 350 分钟 AFK 积分获取循环，每次任务在 15-30 秒内极速完成。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.parse
from typing import Optional

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://free-dash.flarelax.com"
WARNING_URL = f"{BASE_URL}/auth/warning"
AUTH_URL = f"{BASE_URL}/auth/discord"
CALLBACK_URL = f"{BASE_URL}/auth/discord/callback"
SERVER_RENEW_URL = f"{BASE_URL}/api/server/8a4d1879/renew"
DISCORD_API = "https://discord.com/api/v10"

# 两次服务器续期间隔至少 48 小时
RENEW_INTERVAL_SECONDS = 2 * 24 * 60 * 60
STATE_FILE = "flarelax_state.json"

DISCORD_TOKEN = os.environ.get("FLARELAX_DISCORD_TOKEN", "").strip()
CUSTOM_PROXY = os.environ.get("FLARELAX_PROXY", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
SESSION_KEY = os.environ.get("FLARELAX_SESSION_KEY", "").strip()
SESSION_COOKIE_NAME = "connect.sid"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
UA = random.choice(USER_AGENTS)

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt",
]


class FlarelaxError(RuntimeError):
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
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception:
        pass


def normalize_proxy(address: str) -> str:
    if not address:
        return ""
    address = address.strip()
    if address.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return address
    return f"socks5h://{address}"


def make_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def get_oauth_parameters(session: requests.Session) -> tuple[str, str, str, str]:
    entry = session.get(AUTH_URL, allow_redirects=False, timeout=25)
    location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in location:
        raise FlarelaxError(f"OAuth 入口异常：HTTP {entry.status_code}")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    scope = query.get("scope", [""])[0]
    if not client_id or not redirect_uri:
        raise FlarelaxError("OAuth 地址缺少必要参数")
    return location, client_id, redirect_uri, scope


def discord_authorize(location: str, client_id: str, redirect_uri: str, scope: str) -> str:
    """直连 Discord，避免把用户 Token 交给公开代理。"""
    discord_session = make_session()
    response = discord_session.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "client_id": client_id,
        },
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
            "Referer": location,
            "Accept": "*/*",
        },
        allow_redirects=False,
        timeout=25,
    )
    if response.status_code == 401:
        raise FlarelaxError("Discord Token 无效或已失效")
    if response.status_code != 200:
        raise FlarelaxError(f"Discord OAuth 授权失败：HTTP {response.status_code}")
    callback = response.json().get("location", "")
    if not callback or not urllib.parse.parse_qs(urllib.parse.urlparse(callback).query).get("code"):
        raise FlarelaxError("Discord OAuth 没有返回有效 Code")
    return callback


def login_via_proxy(proxy: str) -> requests.Session:
    log(f"🔗 尝试节点：{proxy}")
    site_session = make_session(proxy)
    site_session.get(WARNING_URL, timeout=20)
    location, client_id, redirect_uri, scope = get_oauth_parameters(site_session)
    log(f"   ✅ 获取 OAuth 参数：client_id={client_id}")
    log("   🔐 直连 Discord 提交授权（Token 不经过代理）...")
    callback_url = discord_authorize(location, client_id, redirect_uri, scope)
    log("   ✅ Discord 授权 Code 获取成功，正在通过节点完成 Callback...")
    callback = site_session.get(callback_url, allow_redirects=True, timeout=25)
    if callback.status_code != 200 or "/login" in callback.url.lower():
        raise FlarelaxError(
            f"Callback 响应不为登录状态：HTTP {callback.status_code}，最终 URL：{callback.url}"
        )
    afk_page = site_session.get(f"{BASE_URL}/dashboard", timeout=20)
    if afk_page.status_code != 200 or "/login" in afk_page.url.lower():
        raise FlarelaxError(
            f"后台页面验证失败：HTTP {afk_page.status_code}，当前 URL：{afk_page.url}"
        )
    log(f"   🎉 登录并验证成功，最终页面：{afk_page.url}")
    return site_session


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(data: dict) -> None:
    temporary = f"{STATE_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def save_encrypted_session(state: dict, session: requests.Session, proxy: str) -> None:
    if not SESSION_KEY:
        log("⚠️ 未配置 FLARELAX_SESSION_KEY，本次不持久化 Flarelax 会话")
        return
    cookie_value = next(
        (cookie.value for cookie in session.cookies if cookie.name == SESSION_COOKIE_NAME),
        None,
    )
    if not cookie_value:
        log("⚠️ 当前 Flarelax 会话没有 connect.sid，无法持久化")
        return
    try:
        state["encrypted_session_cookie"] = Fernet(SESSION_KEY.encode()).encrypt(cookie_value.encode()).decode()
        state["last_session_proxy"] = proxy
        state["session_saved_time"] = now_str()
    except Exception as exc:
        log(f"⚠️ 加密 Flarelax 会话失败：{type(exc).__name__}")


def restore_encrypted_session(state: dict, proxy: str) -> requests.Session | None:
    encrypted = state.get("encrypted_session_cookie")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookie_value = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session(proxy)
    session.cookies.set(SESSION_COOKIE_NAME, cookie_value, domain="free-dash.flarelax.com", path="/")
    return session


def session_is_valid(session: requests.Session) -> bool:
    try:
        page = session.get(f"{BASE_URL}/dashboard", allow_redirects=True, timeout=20)
        return page.status_code == 200 and "/login" not in page.url.lower() and "authentication required" not in page.text.lower()
    except requests.RequestException:
        return False


def maybe_renew_server(session: requests.Session) -> bool:
    """距离上次成功续期至少 48 小时时，POST 服务器续期。"""
    state = load_state()
    last_timestamp = int(state.get("last_server_renew_timestamp", 0) or 0)
    now_timestamp = int(time.time())
    if last_timestamp and now_timestamp - last_timestamp < RENEW_INTERVAL_SECONDS:
        remaining_hours = (RENEW_INTERVAL_SECONDS - (now_timestamp - last_timestamp)) / 3600
        log(f"⏳ 距离上次续期未满 48 小时（约 {remaining_hours:.1f} 小时后可再续期）")
        return False

    log("🔔 已达到 48 小时间隔，自动尝试发起服务器续期...")
    response = session.post(
        SERVER_RENEW_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/dashboard",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"   📡 POST /api/server/8a4d1879/renew -> HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise FlarelaxError(f"服务器续期返回非 JSON：{response.text[:300]}") from exc
    log("   📦 续期返回：" + json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    if response.status_code in (401, 403):
        raise FlarelaxError(f"续期会话失效：HTTP {response.status_code}")
    if response.status_code != 200:
        raise FlarelaxError(f"服务器续期失败：HTTP {response.status_code}")

    message = str(data.get("message", ""))
    if data.get("success") is False:
        if any(word in message.lower() for word in ("already", "cooldown", "wait", "again", "2 day")):
            log(f"ℹ️ 服务器仍在续期冷却期：{message}")
            state.update({
                "last_server_renew_timestamp": now_timestamp,
                "last_server_renew_time": now_str(),
                "last_server_renew_result": message,
            })
            save_state(state)
            return False
        raise FlarelaxError(f"服务器续期业务失败：{message or '未知原因'}")

    state.update({
        "last_server_renew_timestamp": now_timestamp,
        "last_server_renew_time": now_str(),
        "last_server_renew_result": message or "success",
    })
    save_state(state)
    log("🎉 服务器续期成功，已记录下次续期时间（至少 2 天后）")
    return True


def check_and_start_server(session: requests.Session, server_id: str = "8a4d1879") -> None:
    """检测后台服务器当前是否处于运行状态，如未运行则发令启动。"""
    info_url = f"{BASE_URL}/api/server/{server_id}"
    log(f"🔍 正在检查 Flarelax 服务器 {server_id} 开机状态：GET {info_url}")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/dashboard",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = session.get(info_url, headers=headers, timeout=20)
    log(f"   📡 状态查询 HTTP {r.status_code}")
    state_str = "unknown"
    is_online = False
    try:
        data = r.json()
        log("   📦 状态返回：" + json.dumps(data, ensure_ascii=False)[:300])
        if isinstance(data, dict):
            state_str = str(
                data.get("status")
                or data.get("state")
                or data.get("current_state")
                or (data.get("attributes", {}).get("current_state") if isinstance(data.get("attributes"), dict) else None)
                or "unknown"
            ).lower()
            is_online = state_str in ("online", "running", "starting") or data.get("online") is True
    except Exception as exc:
        log(f"   ⚠️ 解析状态 JSON 提示：{exc}；将尝试发令开机以防离线")

    if is_online:
        log(f"🟢 服务器 {server_id} 正在运行 ({state_str})，无需开机操作。")
        return

    log(f"🔴 服务器 {server_id} 处于未运行状态 ({state_str})，正在发送开机命令...")
    start_payloads = [
        ("POST", f"{BASE_URL}/api/server/{server_id}/power", {"signal": "start"}),
        ("POST", f"{BASE_URL}/api/server/{server_id}/power", {"action": "start"}),
        ("POST", f"{BASE_URL}/api/server/{server_id}/start", {}),
        ("GET", f"{BASE_URL}/api/server/{server_id}/start", None),
    ]
    for method, url, body in start_payloads:
        try:
            if method == "POST":
                res = session.post(url, headers=headers, json=body, timeout=20)
            else:
                res = session.get(url, headers=headers, timeout=20)
            log(f"   📡 {method} {url} -> HTTP {res.status_code}")
            if res.status_code in (200, 204):
                log("   🟢 启动开机命令发送成功！")
                break
        except Exception as e:
            log(f"   ⚠️ 请求 {url} 失败: {e}")


def build_proxy_list() -> list[str]:
    if CUSTOM_PROXY:
        log("🔧 检测到 FLARELAX_PROXY，优先尝试自定义节点")
        return [normalize_proxy(CUSTOM_PROXY)]

    log("🌐 正在抓取公开 SOCKS5 节点列表...")
    raw_proxies: set[str] = set()
    for url in PROXY_SOURCES:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                continue
            for line in response.text.splitlines():
                candidate = line.strip()
                if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}:\d{2,5}", candidate):
                    raw_proxies.add(f"socks5h://{candidate}")
        except Exception:
            continue

    proxies = list(raw_proxies)
    random.shuffle(proxies)
    log(f"📥 共发现 {len(proxies)} 个候选节点，挑选最多 40 个并发检测...")
    return proxies[:40]


def check_proxy(proxy: str) -> Optional[str]:
    session = make_session(proxy)
    try:
        check = session.get(WARNING_URL, timeout=12)
        if check.status_code in (400, 401, 404, 500) or "tokenerror" in check.text.lower():
            return proxy
        return None
    except Exception:
        return None


def find_accepted_proxies(candidates: list[str]) -> list[str]:
    accepted: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_map = {executor.submit(check_proxy, proxy): proxy for proxy in candidates}
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            if result:
                accepted.append(result)
                if len(accepted) >= 3:
                    break
    return accepted


def main() -> int:
    log("=" * 62)
    log("🚀 Flarelax 服务器开机状态检测与自动续期（已停止获取积分）")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 62)

    state = load_state()
    session: Optional[requests.Session] = None
    current_proxy = ""

    # 1. 优先尝试保存的代理和加密会话
    saved_proxy = state.get("last_session_proxy", "")
    if saved_proxy:
        log(f"♻️ 尝试复用上次 Flarelax 登录节点：{saved_proxy}")
        restored = restore_encrypted_session(state, saved_proxy)
        if restored is not None and session_is_valid(restored):
            session = restored
            current_proxy = saved_proxy
            log("✅ Flarelax Discord 会话有效，本次无需重新登录")
        else:
            log("⌛ 上次 Flarelax 会话或节点失效，开始通过节点池登录")

    if session is None:
        proxies = build_proxy_list()
        if not proxies:
            log("⚠️ 本次未获取到可用公开代理节点")
            return 0
        accepted = find_accepted_proxies(proxies)
        if not accepted:
            log("❌ 未找到能够通过 Flarelax 访问检测的节点")
            return 1
        for proxy in accepted:
            try:
                session = login_via_proxy(proxy)
                current_proxy = proxy
                break
            except Exception as exc:
                log(f"⚠️ 节点 {proxy} 登录失败：{exc}")

        if session is None:
            log("❌ 重新 OAuth 登录尝试了所有候选节点均失败")
            return 1
        save_encrypted_session(state, session, current_proxy)

    # 2. 检测服务器并开机
    try:
        check_and_start_server(session, "8a4d1879")
    except Exception as exc:
        log(f"⚠️ 服务器状态检测出现异常：{exc}")

    # 3. 距离上次续期满 48 小时自动发续期
    try:
        maybe_renew_server(session, 100)
    except Exception as exc:
        log(f"⚠️ 服务器续期尝试提示：{exc}")

    state["last_check_time"] = now_str()
    state["last_status"] = "success"
    save_state(state)
    log("🏁 Flarelax 检测与续期任务成功完结！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
