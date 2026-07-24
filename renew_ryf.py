#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RYF Panel Discord OAuth 自动服务器续期。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

import requests
from cryptography.fernet import Fernet, InvalidToken

API_BASE = "https://edge-public.ryf.sh"
APP_BASE = "https://app.ryf.sh"
AUTH_URL = f"{API_BASE}/auth/discord"
ME_URL = f"{API_BASE}/auth/me"
SERVER_ID = "eef132a6-1001-40e5-9abc-281df97f3eed"
SERVER_URL = f"{API_BASE}/servers/{SERVER_ID}"
RENEW_URL = f"{API_BASE}/servers/{SERVER_ID}/renew"
POWER_URL = f"{API_BASE}/servers/{SERVER_ID}/power"
DISCORD_API = "https://discord.com/api/v10"
STATE_FILE = "ryf_state.json"
# 实测 RYF 接口返回 renew_by 约为 24 小时后；每 12 小时检查，最短间隔留 20 小时余量。
RENEW_INTERVAL_SECONDS = 20 * 60 * 60

DISCORD_TOKEN = os.environ.get("RYF_DISCORD_TOKEN", "").strip()
PROXY = os.environ.get("RYF_PROXY", "").strip()
SESSION_KEY = os.environ.get("RYF_SESSION_KEY", "").strip()
SESSION_COOKIE_NAME = "ryf_session"
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class RyfError(RuntimeError):
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
        log(f"⚠️ Telegram 通知失败：{type(exc).__name__}: {exc}")


def make_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "application/json, text/plain, */*",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        log(f"🔗 使用 RYF_PROXY：{proxy}")
    return session


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(data: dict) -> None:
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


def save_encrypted_session(state: dict, session: requests.Session) -> None:
    if not SESSION_KEY:
        log("⚠️ 未配置 RYF_SESSION_KEY，本次不持久化登录会话")
        return
    cookie_value = next(
        (cookie.value for cookie in session.cookies if cookie.name == SESSION_COOKIE_NAME),
        None,
    )
    if not cookie_value:
        log("⚠️ 当前 RYF 会话没有 session Cookie，无法持久化")
        return
    try:
        state["encrypted_session_cookie"] = Fernet(SESSION_KEY.encode()).encrypt(cookie_value.encode()).decode()
        state["session_saved_time"] = now_str()
    except Exception as exc:
        log(f"⚠️ 加密 RYF 登录会话失败：{type(exc).__name__}")


def restore_encrypted_session(state: dict) -> requests.Session | None:
    encrypted = state.get("encrypted_session_cookie")
    if not encrypted or not SESSION_KEY:
        return None
    try:
        cookie_value = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None
    session = make_session(PROXY)
    session.cookies.set(SESSION_COOKIE_NAME, cookie_value, domain="edge-public.ryf.sh", path="/")
    return session


def session_is_valid(session: requests.Session) -> bool:
    try:
        response = session.get(ME_URL, timeout=20)
        return response.status_code == 200 and response.json().get("authenticated", True) is not False
    except (requests.RequestException, ValueError):
        return False


def should_run() -> bool:
    if FORCE_RUN:
        log("⚡ FORCE_RUN=true，忽略 2 天间隔，立即续期")
        return True
    state = load_state()
    last = int(state.get("last_renew_timestamp", 0) or 0)
    if last and time.time() - last < RENEW_INTERVAL_SECONDS:
        remaining = (RENEW_INTERVAL_SECONDS - (time.time() - last)) / 3600
        log(f"⏳ 距离上次 RYF 续期不足最短间隔 20 小时，跳过本次（约剩 {remaining:.1f} 小时）")
        return False
    return True


def oauth_login() -> requests.Session:
    site = make_session(PROXY)
    entry = site.get(AUTH_URL, allow_redirects=False, timeout=25)
    location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in location:
        raise RyfError(f"RYF OAuth 入口异常：HTTP {entry.status_code}")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    scope = query.get("scope", [""])[0]
    state = query.get("state", [""])[0]
    if not client_id or not redirect_uri or not state:
        raise RyfError("RYF OAuth 地址缺少 client_id、redirect_uri 或 state")

    log(f"🔐 获取 RYF OAuth 参数：client_id={client_id}，scope={scope}，state 已获取")
    # Discord Token 只直连 Discord，不通过可选代理。
    discord = make_session()
    auth = discord.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "client_id": client_id,
            "state": state,
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
    if auth.status_code == 401:
        raise RyfError("Discord Token 无效或已失效")
    if auth.status_code != 200:
        raise RyfError(f"Discord OAuth 授权失败：HTTP {auth.status_code}")
    callback = auth.json().get("location", "")
    if not callback:
        raise RyfError("Discord OAuth 没有返回 Callback 地址")

    log("🎫 Discord 授权成功，正在请求 RYF Callback...")
    callback_response = site.get(callback, allow_redirects=True, timeout=30)
    if "/login" in callback_response.url.lower():
        raise RyfError(f"RYF Callback 后仍未登录：{callback_response.url}")

    me = site.get(ME_URL, timeout=25)
    if me.status_code == 401:
        raise RyfError("RYF Callback 后 /auth/me 仍返回 401")
    if me.status_code != 200:
        raise RyfError(f"RYF /auth/me 验证失败：HTTP {me.status_code}")
    try:
        profile = me.json()
    except ValueError as exc:
        raise RyfError("RYF /auth/me 返回非 JSON") from exc

    organizations = profile.get("organizations") or []
    organization_id = profile.get("personal_organization_id")
    if not organization_id and organizations:
        organization_id = organizations[0].get("id")
    if organization_id:
        site.headers.update({"X-Organization-ID": str(organization_id)})
        log("✅ RYF 登录成功，组织会话已建立")
    else:
        log("✅ RYF 登录成功，未发现组织 ID（继续使用个人会话）")
    return site


def fetch_server(session: requests.Session) -> dict:
    response = session.get(
        SERVER_URL,
        headers={"Origin": APP_BASE, "Referer": f"{APP_BASE}/servers/{SERVER_ID}"},
        timeout=25,
    )
    if response.status_code in (401, 403):
        raise RyfError(f"RYF 登录会话失效：HTTP {response.status_code}")
    if response.status_code != 200:
        raise RyfError(f"RYF 服务器状态请求失败：HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RyfError("RYF 服务器状态返回非 JSON") from exc
    return data.get("server", data) if isinstance(data, dict) else {}


def ensure_server_started(session: requests.Session) -> str:
    server = fetch_server(session)
    status = str(
        server.get("status")
        or server.get("state")
        or server.get("power_status")
        or server.get("powerStatus")
        or "unknown"
    ).lower()
    log(f"🖥 RYF 当前服务器状态：{status}")
    running = {"running", "online", "active", "starting", "installing", "provisioning", "ready"}
    blocked = {"suspended", "expired", "deleted", "pending_deletion"}
    if status in running:
        return status
    if status in blocked:
        log(f"⚠️ 服务器状态为 {status}，不能自动启动")
        return status

    log("▶️ 检测到服务器未运行，发送 power=start 启动指令...")
    response = session.post(
        POWER_URL,
        json={"signal": "start"},
        headers={
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/servers/{SERVER_ID}",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"📡 POST /servers/{SERVER_ID}/power (start) -> HTTP {response.status_code}")
    if response.status_code in (401, 403):
        raise RyfError(f"RYF 登录会话失效：HTTP {response.status_code}")
    if response.status_code not in (200, 202, 204):
        raise RyfError(f"RYF 启动服务器失败：HTTP {response.status_code} {response.text[:200]}")
    log("✅ RYF 启动指令已发送")
    return "starting"


def renew_server(session: requests.Session) -> dict:
    log(f"🔄 发起 RYF 服务器续期 POST：{RENEW_URL}")
    response = session.post(
        RENEW_URL,
        headers={
            "Origin": APP_BASE,
            "Referer": f"{APP_BASE}/servers/{SERVER_ID}",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"📡 POST /servers/{SERVER_ID}/renew -> HTTP {response.status_code}")
    if response.status_code in (401, 403):
        raise RyfError(f"RYF 登录会话失效：HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RyfError(f"RYF 续期返回非 JSON：{response.text[:300]}") from exc
    if response.status_code != 200:
        error_code = str(data.get("error", "")).lower()
        if "cooldown" in error_code or "not_renew" in error_code or "later" in str(data).lower():
            log(f"ℹ️ RYF 当前不可续期，保留会话继续下一小时检查：{data}")
            return data
        raise RyfError(f"RYF 续期请求失败：HTTP {response.status_code}")
    safe_summary = {
        key: data.get(key)
        for key in ("message", "id", "name", "status", "renew_by")
        if key in data
    }
    log("📦 续期返回摘要：" + json.dumps(safe_summary, ensure_ascii=False, separators=(",", ":")))
    if data.get("success") is False:
        raise RyfError(f"RYF 续期业务失败：{data.get('message', '未知原因')}")
    return data


def main() -> int:
    log("=" * 62)
    log("🚀 RYF 服务器持续检测与自动续期启动")
    log(f"🕐 北京时间：{now_str()}")
    log(f"🖥 服务器 ID：{SERVER_ID}")
    log("⏱️ 工作流保持运行，每小时检查续期并确认服务器是否启动")
    log("=" * 62)

    try:
        run_minutes = max(1, int(os.environ.get("RUN_MINUTES", "350")))
    except ValueError:
        run_minutes = 350
    deadline = time.monotonic() + run_minutes * 60
    state = load_state()

    try:
        session = restore_encrypted_session(state)
        if session is not None and session_is_valid(session):
            log("♻️ Discord 登录会话仍有效，复用现有会话，不重新 OAuth 登录")
        else:
            if session is not None:
                log("⌛ 已保存的 Discord 会话失效，重新进行 OAuth 登录")
            if not DISCORD_TOKEN:
                raise RyfError("缺少 RYF_DISCORD_TOKEN，且没有可复用的有效会话")
            session = oauth_login()
            save_encrypted_session(state, session)
            save_state(state)

        check_count = 0
        renew_count = 0
        start_count = 0
        last_error = None

        while time.monotonic() < deadline:
            try:
                status = ensure_server_started(session)
                if status == "starting":
                    start_count += 1

                result = renew_server(session)
                check_count += 1
                if result.get("renew_by") or result.get("message"):
                    renew_count += 1
                state["last_check_time"] = now_str()
                state["last_server_status"] = status
                state["last_renew_result"] = result.get("message", result.get("error", "checked"))
                save_encrypted_session(state, session)
                save_state(state)
                log(f"✅ 第 {check_count} 次每小时检查完成：状态={status}")
            except RyfError as exc:
                last_error = exc
                log(f"⚠️ 本轮 RYF 检查异常：{exc}")
                if "登录会话失效" in str(exc):
                    if not DISCORD_TOKEN:
                        break
                    log("🔐 仅因确认会话失效，重新 OAuth 登录一次")
                    session = oauth_login()
                    save_encrypted_session(state, session)
                else:
                    # 续期冷却/临时 API 错误不重新登录，下一小时继续检查。
                    pass
            except requests.RequestException as exc:
                last_error = exc
                log(f"⚠️ RYF 网络异常，保留会话等待下一小时：{type(exc).__name__}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            log("⏳ RYF 工作流保持运行，1 小时后进行下一次检查")
            time.sleep(min(3600, remaining))

        log("=" * 62)
        log(f"🏁 RYF 持续窗口结束：检查 {check_count} 次，续期响应 {renew_count} 次，启动指令 {start_count} 次")
        if last_error:
            log(f"ℹ️ 最后一次异常：{last_error}")
        send_telegram(
            f"✅ RYF 持续窗口结束\n🕐 {now_str()}\n"
            f"📊 检查：{check_count} 次\n🔄 续期响应：{renew_count} 次\n▶️ 启动指令：{start_count} 次"
        )
        return 0
    except Exception as exc:
        log(f"❌ RYF 持续任务失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ RYF 持续任务失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
