#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BulkNodes AFK streak 自动访问脚本。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://dashboard.bulknodes.xyz"
AUTH_START_URL = f"{BASE_URL}/api/auth/discord/start?next=/dashboard"
AUTH_ME_URL = f"{BASE_URL}/api/auth/me"
STREAK_URL = f"{BASE_URL}/api/afk/streak"
DISCORD_API = "https://discord.com/api/v10"
STATE_FILE = "bulknodes_state.json"
VISIT_INTERVAL_SECONDS = 20 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 60
RELOGIN_AFTER_NO_INCREASE_SECONDS = 4 * 60

DISCORD_TOKEN = os.environ.get("BULKNODES_DISCORD_TOKEN", "").strip()
SESSION_KEY = os.environ.get("BULKNODES_SESSION_KEY", "").strip()
PROXY = os.environ.get("BULKNODES_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class BulkNodesError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as exc:
        log(f"⚠️ Telegram 通知失败：{type(exc).__name__}")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(data: dict) -> None:
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def save_browser_cookies(state: dict, sb) -> None:
    if not SESSION_KEY:
        log("⚠️ 未配置 BULKNODES_SESSION_KEY，本次不持久化 BulkNodes 会话")
        return
    cookies = sb.get_cookies()
    if not cookies:
        return
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(cookies, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_str()


def restore_browser_cookies(state: dict, sb) -> bool:
    encrypted = state.get("encrypted_cookies")
    if not encrypted or not SESSION_KEY:
        return False
    try:
        cookies = json.loads(Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return False
    try:
        sb.open(BASE_URL)
        sb.wait_for_ready_state_complete()
        for cookie in cookies:
            item = {k: cookie[k] for k in ("name", "value", "domain", "path", "expiry", "secure", "httpOnly") if k in cookie}
            if item.get("expiry") is None:
                item.pop("expiry", None)
            if isinstance(item.get("expiry"), float):
                item["expiry"] = int(item["expiry"])
            try:
                sb.add_cookie(item)
            except Exception:
                sb.add_cookie({"name": cookie["name"], "value": cookie["value"], "path": "/"})
        sb.refresh()
        sb.wait_for_ready_state_complete()
        time.sleep(2)
        return True
    except Exception:
        return False


def browser_fetch(sb, url: str, method: str = "GET") -> dict:
    """在已通过 Cloudflare 的浏览器上下文中执行同源 fetch。"""
    script = """
    const done = arguments[arguments.length - 1];
    fetch(arguments[0], {method: arguments[1], credentials: 'include', headers: {'Accept': 'application/json'}})
      .then(async r => done({status: r.status, url: r.url, text: await r.text()}))
      .catch(e => done({error: String(e)}));
    """
    result = sb.execute_async_script(script, url, method)
    return result if isinstance(result, dict) else {"error": "invalid browser result"}


def browser_auth_valid(sb) -> bool:
    result = browser_fetch(sb, AUTH_ME_URL)
    if result.get("status") != 200:
        return False
    try:
        return json.loads(result.get("text", "{}")).get("loggedIn") is True
    except ValueError:
        return False


def extract_points(text: str):
    """从 streak 返回中提取积分/余额字段，兼容不同 API 字段命名。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    keys = (
        "coins", "points", "afkPoints", "totalPoints", "balance",
        "totalCoins", "earnedToday", "earned", "score", "total",
    )
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def pass_cloudflare(sb) -> None:
    # GitHub Runner 的出口会触发 Cloudflare Managed Challenge，给浏览器足够时间完成 JS/Turnstile。
    try:
        sb.uc_open_with_reconnect(BASE_URL, reconnect_time=8)
    except Exception:
        sb.open(BASE_URL)
    sb.wait_for_ready_state_complete()
    for attempt in range(4):
        time.sleep(5)
        title = sb.get_title().lower()
        source = sb.get_page_source().lower()
        if "just a moment" not in title and "challenge-platform" not in source and "cf-chl-" not in source:
            return
        log(f"🛡️ Cloudflare Challenge 处理中（第 {attempt + 1}/4 次）...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as exc:
            log(f"⚠️ Cloudflare Challenge 点击提示：{type(exc).__name__}")
        time.sleep(8)
        try:
            sb.refresh()
            sb.wait_for_ready_state_complete()
        except Exception:
            pass
    raise BulkNodesError("Cloudflare Challenge 未通过")


def oauth_login(sb):
    if not DISCORD_TOKEN:
        raise BulkNodesError("缺少 BULKNODES_DISCORD_TOKEN")
    log("🔗 打开 BulkNodes Discord OAuth 入口...")
    sb.open(AUTH_START_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(2)
    oauth_location = sb.get_current_url()
    if "discord.com" not in oauth_location:
        raise BulkNodesError(f"未跳转到 Discord OAuth：{oauth_location}")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(oauth_location).query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    state = query.get("state", [""])[0]
    scope = query.get("scope", ["identify email"])[0]
    if not client_id or not redirect_uri or not state:
        raise BulkNodesError("BulkNodes OAuth 参数不完整")

    # Discord Token 直连 Discord，不经过可选代理。
    discord = requests.Session()
    discord.trust_env = False
    response = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        },
        json={
            "permissions": "0",
            "authorize": True,
            "integration_type": 0,
            "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000},
        },
        headers={
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": oauth_location,
            "Accept": "*/*",
        },
        allow_redirects=False,
        timeout=25,
    )
    if response.status_code == 401:
        raise BulkNodesError("Discord Token 无效或已失效")
    if response.status_code != 200:
        raise BulkNodesError(f"Discord OAuth 授权失败：HTTP {response.status_code}")
    callback = response.json().get("location", "")
    if not callback:
        raise BulkNodesError("Discord OAuth 未返回 Callback")

    log("🎫 Discord 授权成功，回到 BulkNodes Callback...")
    sb.open(callback)
    sb.wait_for_ready_state_complete()
    time.sleep(3)
    if not browser_auth_valid(sb):
        raise BulkNodesError("BulkNodes Callback 后仍未登录")
    log("✅ BulkNodes Discord 登录成功")


def main() -> int:
    log("=" * 62)
    log("🚀 BulkNodes AFK streak 真实浏览器持续模式启动")
    log(f"🕐 北京时间：{now_str()}")
    log("⏱️ 保持浏览器在线：每分钟发送 AFK heartbeat，每 1 小时刷新页面；4 分钟无积分增加则重新登录")
    log("=" * 62)

    try:
        run_minutes = max(1, int(os.environ.get("RUN_MINUTES", "350")))
    except ValueError:
        run_minutes = 350
    deadline = time.monotonic() + run_minutes * 60
    log(f"🛠️ 本次浏览器窗口：{run_minutes} 分钟")

    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ 缺少 seleniumbase 依赖")
        return 1

    state = load_state()
    sb_kwargs = {"uc": True, "xvfb": True, "headless": False}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
    streak_count = 0

    try:
        with SB(**sb_kwargs) as sb:
            pass_cloudflare(sb)
            reused = restore_browser_cookies(state, sb)
            if reused and browser_auth_valid(sb):
                log("♻️ BulkNodes Discord 会话仍有效，本次复用，不重新登录")
            else:
                if reused:
                    log("⌛ 已保存 BulkNodes 会话失效，重新 Discord OAuth 登录")
                oauth_login(sb)
                save_browser_cookies(state, sb)
                save_state(state)

            last_points = None
            last_points_increase = time.monotonic()
            last_page_refresh = 0.0

            while time.monotonic() < deadline:
                # 每分钟发送一次 heartbeat；页面每小时完整刷新一次。
                try:
                    now_mono = time.monotonic()
                    if now_mono - last_page_refresh >= 3600:
                        log("🔄 每小时刷新 BulkNodes 页面")
                        sb.open(BASE_URL)
                        sb.wait_for_ready_state_complete()
                        time.sleep(3)
                        if "just a moment" in sb.get_title().lower():
                            pass_cloudflare(sb)
                        last_page_refresh = time.monotonic()

                    if not browser_auth_valid(sb):
                        log("🔐 BulkNodes 会话失效，重新 Discord OAuth 登录")
                        oauth_login(sb)
                        save_browser_cookies(state, sb)
                        last_points = None
                        last_points_increase = time.monotonic()

                    result = browser_fetch(sb, STREAK_URL, "POST")
                    log(f"📡 POST /api/afk/streak -> HTTP {result.get('status')}")
                    log("📦 返回摘要：" + result.get("text", "")[:500])
                    if result.get("status") in (401, 403):
                        log("⚠️ AFK 接口返回未登录，下一轮重新登录")
                        state["session_invalid_at"] = now_str()
                    elif result.get("status") == 200:
                        streak_count += 1
                        points = extract_points(result.get("text", ""))
                        if points is not None and (last_points is None or points > last_points):
                            log(f"✅ 积分增加：{last_points} -> {points}")
                            last_points = points
                            last_points_increase = time.monotonic()
                        elif points is None:
                            log("⚠️ 本次返回没有识别到积分字段，继续观察 4 分钟")
                        else:
                            log(f"ℹ️ 本次积分未增加：仍为 {points}")

                        state["last_streak_timestamp"] = int(time.time())
                        state["last_streak_time"] = now_str()
                        if time.monotonic() - last_points_increase >= RELOGIN_AFTER_NO_INCREASE_SECONDS:
                            log("⛔ 连续 4 分钟积分没有增加，判定会话/AFK 已失效，重新 Discord OAuth 登录")
                            oauth_login(sb)
                            save_browser_cookies(state, sb)
                            last_points = None
                            last_points_increase = time.monotonic()
                    else:
                        log(f"⚠️ AFK 请求返回 HTTP {result.get('status')}，保持浏览器继续运行")

                    save_browser_cookies(state, sb)
                    save_state(state)
                except Exception as exc:
                    log(f"⚠️ 本轮浏览器访问异常，保持浏览器并等待下一分钟：{type(exc).__name__}: {exc}")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(HEARTBEAT_INTERVAL_SECONDS, remaining))

            log(f"🏁 BulkNodes 浏览器窗口结束，共成功请求 AFK streak {streak_count} 次")
            send_telegram(f"✅ BulkNodes AFK 窗口结束\n🕐 {now_str()}\n📊 成功请求：{streak_count} 次")
            return 0
    except Exception as exc:
        log(f"❌ BulkNodes 自动访问失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ BulkNodes 自动访问失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
