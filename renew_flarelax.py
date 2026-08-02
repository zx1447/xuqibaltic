#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flarelax 服务器开机状态检测与自动续期（已停止获取 AFK 积分）。

- 检查服务器 8a4d1879 是否正在运行，如离线/停止则发送开机命令。
- 距离上次成功续期满 48 小时自动发起服务器续期（/api/server/8a4d1879/renew）。
- 移除了 350 分钟 AFK 积分获取循环，每次任务在 15-30 秒内极速完成。
- 使用 SeleniumBase UC 浏览器自动通过 Cloudflare Turnstile 验证，彻底告别不稳定的免费代理池。
"""

from __future__ import annotations

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
AUTH_URL = f"{BASE_URL}/auth/discord"
CALLBACK_URL = f"{BASE_URL}/auth/discord/callback"
SERVER_RENEW_URL = f"{BASE_URL}/api/server/8a4d1879/renew"
DISCORD_API = "https://discord.com/api/v10"

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


def make_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return session


def discord_authorize(location: str, client_id: str, redirect_uri: str, scope: str) -> str:
    """直连 Discord 完成 OAuth 授权并获取 callback 地址。"""
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


def click_turnstile_checkbox_precision(browser) -> bool:
    try:
        coords = browser.execute_script(
            """
            const el = document.querySelector(".g-recaptcha, .captcha-wrapper, iframe[src*='cloudflare'], iframe");
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return null;
            return [Math.round(rect.left + 30), Math.round(rect.top + rect.height / 2)];
            """
        )
        if coords and len(coords) == 2:
            x, y = coords[0], coords[1]
            log(f"   🖱️ 精准定位到 Turnstile 像素坐标: ({x}, {y})，点击复选框...")
            browser.driver.uc_gui_click_x_y(x, y)
            return True
    except Exception as exc:
        log(f"   ⚠️ 定位出错: {exc}")
    return False


def turnstile_token(browser) -> str:
    try:
        return browser.execute_script(
            """
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (el && el.value) return el.value;
            try { return window.turnstile?.getResponse?.() || ''; } catch (e) { return ''; }
            """
        ) or ""
    except Exception:
        return ""


def turnstile_solved(browser) -> bool:
    try:
        title = browser.get_title()
        if "checking" not in title.lower() and "just a moment" not in title.lower():
            return True
        for c in browser.get_cookies():
            if c.get("name") == "TOKEN" and c.get("value"):
                return True
    except Exception:
        pass
    return False


def solve_turnstile(browser) -> None:
    log("🛡️ 检查并处理 Flarelax Turnstile 验证...")
    for attempt in range(1, 35):
        if turnstile_solved(browser):
            log("   ✅ 当前页面非人机校验页或 Token 已生成")
            return
        if attempt in (1, 4, 8, 12, 16, 20):
            log(f"   🖱️ 尝试点击 Turnstile 人机验证框 (第 {attempt} 秒)...")
            clicked = click_turnstile_checkbox_precision(browser)
            if not clicked:
                try:
                    browser.driver.uc_gui_click_cf()
                except Exception:
                    pass
        time.sleep(1)

    log("⚠️ 无法自动通过 Turnstile，输出调试信息：")
    try:
        log(f"URL: {browser.get_current_url()} Title: {browser.get_title()}")
        iframes = browser.execute_script("return [...document.querySelectorAll('iframe')].map(f => ({src: f.src, title: f.title, rect: f.getBoundingClientRect().toJSON()}))")
        log(f"IFRAMES JS DUMP: {iframes}")
    except Exception as exc:
        log(f"debug log exc: {exc}")
    raise FlarelaxError("Cloudflare Turnstile 验证超时 (页面始终未跳转)")


def get_session_with_browser() -> requests.Session:
    from seleniumbase import SB

    log("🌐 启动 SeleniumBase UC 浏览器过 Cloudflare 并进行 Discord OAuth...")
    options = {
        "uc": True,
        "xvfb": True,
        "headless": False,
        "locale": "en",
        "window_size": "1280,720",
        "host_resolver_rules": "MAP *.challenges.cloudflare.com 104.18.94.41, EXCLUDE localhost",
    }
    if CUSTOM_PROXY:
        options["proxy"] = CUSTOM_PROXY
        log(f"   🔗 浏览器将通过代理：{CUSTOM_PROXY}")
    with SB(**options) as sb:
        log("   1. 访问 Flarelax 首页解动人机验证...")
        sb.uc_open_with_reconnect(BASE_URL, 6)
        time.sleep(4)
        solve_turnstile(sb)
        time.sleep(2)
        log("   2. 访问 Discord 授权入口...")
        sb.open(AUTH_URL)
        time.sleep(4)
        solve_turnstile(sb)
        time.sleep(3)
        cur = sb.get_current_url()
        if "discord.com" not in cur:
            raise FlarelaxError(f"未能重定向到 Discord OAuth 页面，当前 URL：{cur}")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(cur).query)
        client_id = query.get("client_id", [""])[0]
        redirect_uri = query.get("redirect_uri", [""])[0]
        scope = query.get("scope", [""])[0]
        if not client_id or not redirect_uri:
            raise FlarelaxError("Discord OAuth 地址缺少必要参数")
        log(f"   ✅ 成功获取 OAuth 参数：client_id={client_id}")
        log("   🔐 直连 Discord 提交授权...")
        callback_url = discord_authorize(cur, client_id, redirect_uri, scope)
        log("   ✅ Discord 授权 Code 获取成功，由浏览器打开 Callback...")
        sb.open(callback_url)
        for _ in range(30):
            time.sleep(1)
            u = sb.get_current_url()
            if "/login" not in u.lower() and "free-dash.flarelax.com" in u.lower():
                log(f"   🎉 后台登录成功，最终 URL：{u}")
                break
        else:
            raise FlarelaxError(f"等待 Dashboard 加载超时，当前 URL：{sb.get_current_url()}")

        cookies = sb.get_cookies()
        session = make_session()
        for c in cookies:
            try:
                session.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", "free-dash.flarelax.com"),
                    path=c.get("path", "/")
                )
            except Exception:
                pass
        return session


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


def save_encrypted_session(state: dict, session: requests.Session) -> None:
    if not SESSION_KEY:
        log("⚠️ 未配置 FLARELAX_SESSION_KEY，本次不持久化 Flarelax 会话")
        return
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    if not cookies:
        log("⚠️ 当前会话没有任何 Cookie，无法持久化")
        return
    try:
        state["encrypted_session_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
            json.dumps(cookies).encode()
        ).decode()
        state["session_saved_time"] = now_str()
    except Exception as exc:
        log(f"⚠️ 加密 Flarelax 会话失败：{type(exc).__name__}")


def restore_encrypted_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_session_cookies")
    if not encrypted or not SESSION_KEY:
        # 兼容旧版仅保存 connect.sid 的状态
        old_val = state.get("encrypted_session_cookie")
        if old_val and SESSION_KEY:
            try:
                cookie_value = Fernet(SESSION_KEY.encode()).decrypt(old_val.encode()).decode()
                session = make_session()
                session.cookies.set(SESSION_COOKIE_NAME, cookie_value, domain="free-dash.flarelax.com", path="/")
                return session
            except Exception:
                return None
        return None
    try:
        raw = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode())
        cookies = json.loads(raw.decode())
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session()
    for c in cookies:
        try:
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", "free-dash.flarelax.com"),
                path=c.get("path", "/")
            )
        except Exception:
            pass
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


def main() -> int:
    log("=" * 62)
    log("🚀 Flarelax 服务器开机状态检测与开机 + 48小时续期（已停止获取积分）")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 62)

    state = load_state()
    session = None

    # 1. 优先尝试保存的会话与 cf_clearance
    restored = restore_encrypted_session(state)
    if restored is not None and session_is_valid(restored):
        session = restored
        log("✅ 保存的 Flarelax 会话与 CF Clearance 仍有效，无需打开浏览器登录")
    else:
        log("⌛ 保存的会话失效或已被 Cloudflare 阻断，启动 SeleniumBase UC 浏览器重新登录")
        session = get_session_with_browser()
        save_encrypted_session(state, session)

    # 2. 检测服务器并启动 (若离线)
    try:
        check_and_start_server(session, "8a4d1879")
    except Exception as exc:
        log(f"⚠️ 服务器状态检测出现异常：{exc}")

    # 3. 距离上次续期满 48 小时自动发起服务器续期
    try:
        maybe_renew_server(session)
    except Exception as exc:
        log(f"⚠️ 服务器续期尝试提示：{exc}")

    state["last_check_time"] = now_str()
    state["last_status"] = "success"
    save_state(state)
    log("🏁 Flarelax 检测与续期任务成功完结！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
