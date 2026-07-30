#!/usr/bin/env python3
"""VolyxHost AFK earning through the site's official Livewire actions."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://dash.volyxhost.com"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
EARN_URL = f"{BASE_URL}/coins/earn"
UPDATE_URL = f"{BASE_URL}/livewire-02cedd0c/update"
STATE_FILE = Path("volyxhost_state.json")
EMAIL = os.environ.get("VOLYXHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("VOLYXHOST_PASSWORD", "").strip()
SESSION_KEY = os.environ.get("VOLYXHOST_SESSION_KEY", "").strip()
RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
TICK_SECONDS = max(60, int(os.environ.get("TICK_SECONDS", "61")))
CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


class VolyxError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_text() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def save_encrypted_cookies(session: requests.Session, state: dict) -> None:
    if not SESSION_KEY:
        raise VolyxError("缺少 VOLYXHOST_SESSION_KEY")
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    if not cookies:
        raise VolyxError("没有可保存的 VolyxHost Cookie")
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


def session_is_valid(session: requests.Session) -> bool:
    try:
        response = session.get(DASHBOARD_URL, allow_redirects=False, timeout=25)
        return response.status_code == 200 and "VolyxHost FS" in response.text
    except requests.RequestException:
        return False


def get_component(session: requests.Session, path: str, component_name: str) -> tuple[str, str]:
    response = session.get(BASE_URL + path, timeout=30)
    if response.status_code != 200:
        raise VolyxError(f"GET {path} 失败：HTTP {response.status_code}")
    soup = BeautifulSoup(response.text, "html.parser")
    csrf_meta = soup.find("meta", attrs={"name": "csrf-token"})
    component = soup.find(attrs={"wire:name": component_name})
    if csrf_meta is None or component is None or not component.get("wire:snapshot"):
        raise VolyxError(f"页面 {path} 未找到 Livewire 组件 {component_name}")
    return str(component["wire:snapshot"]), str(csrf_meta["content"])


def livewire_call(
    session: requests.Session,
    snapshot: str,
    csrf: str,
    method: str,
    params: list | None = None,
    updates: dict | None = None,
    referer: str = EARN_URL,
) -> tuple[str, dict]:
    payload = {
        "_token": csrf,
        "components": [{
            "snapshot": snapshot,
            "updates": updates or {},
            "calls": [{"path": "", "method": method, "params": params or []}],
        }],
    }
    response = session.post(
        UPDATE_URL,
        json=payload,
        headers={
            "X-Livewire": "",
            "X-CSRF-TOKEN": csrf,
            "Origin": BASE_URL,
            "Referer": referer,
            "Content-Type": "application/json",
        },
        timeout=35,
    )
    if response.status_code != 200:
        raise VolyxError(f"Livewire {method} 失败：HTTP {response.status_code}｜{response.text[:300]}")
    try:
        result = response.json()["components"][0]
        return str(result["snapshot"]), result.get("effects") or {}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VolyxError(f"Livewire {method} 响应异常：{type(exc).__name__}") from exc


def snapshot_data(snapshot: str) -> dict:
    try:
        data = json.loads(snapshot).get("data", {})
        return data if isinstance(data, dict) else {}
    except ValueError as exc:
        raise VolyxError("Livewire snapshot 不是有效 JSON") from exc


def login(session: requests.Session) -> None:
    if not EMAIL or not PASSWORD:
        raise VolyxError("缺少 VOLYXHOST_EMAIL/VOLYXHOST_PASSWORD")
    snapshot, csrf = get_component(session, "/login", "auth.login")
    snapshot, effects = livewire_call(
        session,
        snapshot,
        csrf,
        "login",
        updates={"email": EMAIL, "password": PASSWORD, "remember": True},
        referer=LOGIN_URL,
    )
    redirect = str(effects.get("redirect", ""))
    if "/dashboard" not in redirect or not session_is_valid(session):
        html = str(effects.get("html", ""))
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        raise VolyxError(f"VolyxHost 登录失败：{text[:300] or '没有跳转到 Dashboard'}")
    log("✅ VolyxHost 登录成功")


def start_afk(session: requests.Session) -> tuple[str, str, dict]:
    snapshot, csrf = get_component(session, "/coins/earn", "coins.earn-coins")
    snapshot, _ = livewire_call(session, snapshot, csrf, "setTab", ["afk"])
    data = snapshot_data(snapshot)
    if not data.get("afkActive"):
        snapshot, _ = livewire_call(session, snapshot, csrf, "startAfk")
        data = snapshot_data(snapshot)
    if not data.get("afkActive"):
        raise VolyxError("点击 Start AFK Earn 后 afkActive 仍为 false")
    log("▶️ Start AFK Earn 按钮点击成功")
    return snapshot, csrf, data


def captcha_method(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        for attribute in ("wire:submit", "wire:click"):
            value = str(tag.get(attribute, ""))
            if "captcha" in value.lower() or "verify" in value.lower():
                match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", value)
                if match:
                    return match.group(1)
    return None


def solve_math_challenge(
    session: requests.Session, snapshot: str, csrf: str, effects: dict
) -> tuple[str, dict]:
    data = snapshot_data(snapshot)
    if not data.get("captchaPending"):
        return snapshot, data
    answer = data.get("captchaAnswer")
    if answer is None:
        first = data.get("captchaNum1")
        second = data.get("captchaNum2")
        if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
            raise VolyxError("出现验证问题，但无法计算答案")
        answer = first + second
    method = captcha_method(str(effects.get("html", ""))) or "verifyCaptcha"
    log(f"🧮 出现活跃验证，提交答案：{data.get('captchaNum1')} + {data.get('captchaNum2')}")
    snapshot, _ = livewire_call(
        session,
        snapshot,
        csrf,
        method,
        updates={"captchaInput": str(answer)},
    )
    data = snapshot_data(snapshot)
    if data.get("captchaPending"):
        raise VolyxError("活跃验证提交后仍未通过")
    log("✅ 活跃验证通过")
    return snapshot, data


def run_afk(session: requests.Session, state: dict) -> int:
    snapshot, csrf, data = start_afk(session)
    deadline = time.monotonic() + RUN_MINUTES * 60
    ticks = 0
    initial_coins = float(data.get("afkCoinsEarned", 0) or 0)
    last_coins = initial_coins
    state.update({
        "last_status": "earning",
        "last_start_time": now_text(),
        "afk_active": True,
        "session_coins": last_coins,
        "afk_seconds": int(data.get("afkSeconds", 0) or 0),
    })
    save_encrypted_cookies(session, state)
    save_state(state)

    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining < TICK_SECONDS:
                time.sleep(max(0, remaining))
                break
            time.sleep(TICK_SECONDS)
            snapshot, effects = livewire_call(session, snapshot, csrf, "tickAfk")
            data = snapshot_data(snapshot)
            ticks += 1
            if data.get("captchaPending"):
                snapshot, data = solve_math_challenge(session, snapshot, csrf, effects)
            last_coins = float(data.get("afkCoinsEarned", last_coins) or 0)
            state.update({
                "last_status": "earning",
                "last_tick_time": now_text(),
                "last_run_ticks": ticks,
                "session_coins": last_coins,
                "afk_seconds": int(data.get("afkSeconds", 0) or 0),
                "captcha_pending": bool(data.get("captchaPending")),
            })
            save_encrypted_cookies(session, state)
            save_state(state)
            log(f"🪙 AFK Tick #{ticks}｜本轮金币 {last_coins:.2f}｜活跃 {state['afk_seconds']} 秒")
    finally:
        try:
            data = snapshot_data(snapshot)
            if data.get("afkActive"):
                snapshot, _ = livewire_call(session, snapshot, csrf, "stopAfk")
                data = snapshot_data(snapshot)
                last_coins = float(data.get("afkCoinsEarned", last_coins) or 0)
                log("⏹️ 已点击 Stop Earning，正常结束 AFK Session")
        except Exception as exc:
            log(f"⚠️ 结束 AFK Session 提示：{type(exc).__name__}: {exc}")

    state.update({
        "last_status": "completed",
        "last_end_time": now_text(),
        "afk_active": False,
        "last_run_ticks": ticks,
        "last_run_coins": max(0.0, last_coins - initial_coins),
        "session_coins": last_coins,
    })
    save_encrypted_cookies(session, state)
    save_state(state)
    log(f"🏁 本轮完成：Tick {ticks} 次，增加金币 {state['last_run_coins']:.2f}")
    return 0


def main() -> int:
    log("=" * 60)
    log("🚀 VolyxHost AFK Earn 启动")
    log(f"🕐 北京时间：{now_text()}")
    log(f"⏳ 本轮运行：{RUN_MINUTES} 分钟")
    log("=" * 60)
    state = load_state()
    try:
        session = restore_session(state)
        if session is not None and session_is_valid(session):
            log("♻️ 复用 VolyxHost 登录会话")
        else:
            if session is not None:
                log("⌛ 保存的登录会话已失效，重新登录")
            session = make_session()
            login(session)
        save_encrypted_cookies(session, state)
        save_state(state)
        return run_afk(session, state)
    except Exception as exc:
        state.update({
            "last_status": "failed",
            "last_error": f"{type(exc).__name__}: {exc}",
            "last_error_time": now_text(),
        })
        save_state(state)
        log(f"❌ VolyxHost AFK 失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
