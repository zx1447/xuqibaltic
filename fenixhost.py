#!/usr/bin/env python3
"""FenixHost Paymenter account keepalive with encrypted session reuse."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://fenixhost.net"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
UPDATE_URL = f"{BASE_URL}/paymenter/update"
STATE_FILE = Path("fenixhost_state.json")
EMAIL = os.environ.get("FENIXHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("FENIXHOST_PASSWORD", "").strip()
SESSION_KEY = os.environ.get("FENIXHOST_SESSION_KEY", "").strip()
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


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


def encrypt_cookies(cookies: list[dict], state: dict) -> None:
    if not SESSION_KEY:
        return
    clean = [
        {
            "name": item.get("name", ""),
            "value": item.get("value", ""),
            "domain": item.get("domain") or "fenixhost.net",
            "path": item.get("path") or "/",
        }
        for item in cookies
        if item.get("name") and item.get("value")
    ]
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(clean, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_cn()


def make_requests_session(cookies: list[dict] | None = None) -> requests.Session:
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
            domain=item.get("domain") or "fenixhost.net",
            path=item.get("path") or "/",
        )
    return session


def authenticated_page(cookies: list[dict]) -> tuple[bool, str, str]:
    if not cookies:
        return False, "", ""
    try:
        session = make_requests_session(cookies)
        response = session.get(DASHBOARD_URL, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        print(f"⚠️ 会话检查网络错误：{type(exc).__name__}", flush=True)
        return False, "", ""
    current = response.url
    body = response.text
    valid = response.status_code == 200 and "/login" not in current
    return valid, current, body


def compact_page_summary(body: str) -> list[str]:
    """Store a small non-sensitive dashboard summary in state."""
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(body, "html.parser").get_text("\n")
    except Exception:
        text = body
    lines: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or line in lines:
            continue
        if EMAIL and EMAIL.lower() in line.lower():
            continue
        lines.append(line[:240])
        if len(lines) >= 35:
            break
    return lines


def patch_seleniumbase_filelock() -> None:
    """Compatibility for recent filelock versions used by UC GUI clicks."""
    try:
        import filelock
        from seleniumbase.core import browser_launcher
        from seleniumbase.fixtures import page_actions

        def singleton_lock(path: str):
            return filelock.FileLock(path, is_singleton=True)

        browser_launcher.FileLock = singleton_lock
        page_actions.FileLock = singleton_lock
    except Exception:
        pass


def browser_login() -> tuple[list[dict], str, str]:
    patch_seleniumbase_filelock()
    from seleniumbase import SB

    last_error = "Cloudflare Turnstile verification did not complete"
    with SB(
        uc=True,
        xvfb=True,
        headless=False,
        locale="en",
        window_size="1366,768",
    ) as browser:
        for attempt in range(1, 4):
            browser.uc_open_with_reconnect(LOGIN_URL, 8)
            time.sleep(7)
            browser.wait_for_element_visible("#email", timeout=25)
            browser.type("#email", EMAIL)
            browser.type("#password", PASSWORD)
            try:
                if not browser.is_selected("#remember"):
                    browser.click("#remember")
            except Exception:
                pass

            print(f"🛡️ Cloudflare Turnstile 验证尝试 {attempt}/3", flush=True)
            try:
                browser.driver.uc_gui_click_cf(retry=False)
            except Exception as exc:
                print(f"⚠️ Turnstile GUI 点击异常：{type(exc).__name__}: {exc}", flush=True)

            token_ready = False
            for _ in range(20):
                time.sleep(1)
                try:
                    token = browser.execute_script(
                        "return document.querySelector('input[name=cf-turnstile-response]')?.value || ''"
                    )
                    if token:
                        token_ready = True
                        break
                except Exception:
                    pass
            print(f"🔐 Turnstile token：{'ready' if token_ready else 'not visible'}", flush=True)

            try:
                browser.click('button[type="submit"]')
            except Exception as exc:
                last_error = f"submit failed: {type(exc).__name__}: {exc}"
                continue

            for _ in range(35):
                time.sleep(1)
                current = browser.get_current_url()
                if "/login" not in current:
                    body = browser.get_page_source()
                    cookies = browser.get_cookies()
                    return cookies, current, body

            try:
                page_text = browser.get_text("body")
                if "CAPTCHA" in page_text:
                    last_error = "Cloudflare Turnstile was rejected"
                elif "credentials" in page_text.lower() or "incorrect" in page_text.lower():
                    last_error = "FenixHost rejected the credentials"
                else:
                    last_error = "login remained on /login"
            except Exception:
                pass

    raise RuntimeError(last_error)


def main() -> int:
    print("🚀 FenixHost 干净分支账号保活启动", flush=True)
    print(f"🕐 北京时间：{now_cn()}", flush=True)
    print(f"🔗 Paymenter 更新端点：{UPDATE_URL}", flush=True)
    if not EMAIL or not PASSWORD:
        print("❌ 缺少 FENIXHOST_EMAIL/FENIXHOST_PASSWORD", flush=True)
        return 1
    if not SESSION_KEY:
        print("❌ 缺少 FENIXHOST_SESSION_KEY", flush=True)
        return 1

    state = load_state()
    cookies = decrypt_cookies(state)
    valid, current, body = authenticated_page(cookies)
    login_source = "encrypted_session"

    try:
        if valid:
            print(f"✅ 加密登录会话有效：{current}", flush=True)
        else:
            print("🔄 没有有效会话，开始浏览器登录", flush=True)
            cookies, current, body = browser_login()
            login_source = "browser_login"
            valid, checked_url, checked_body = authenticated_page(cookies)
            if valid:
                current = checked_url
                body = checked_body
            elif "/login" in current:
                raise RuntimeError("browser login did not create an authenticated session")
            encrypt_cookies(cookies, state)
            print(f"✅ FenixHost 浏览器登录成功：{current}", flush=True)

        state.update(
            {
                "last_check_time": now_cn(),
                "last_status": "success",
                "last_login_source": login_source,
                "authenticated_url": current,
                "paymenter_update_endpoint": UPDATE_URL,
                "dashboard_summary": compact_page_summary(body),
            }
        )
        save_state(state)
        print("✅ FenixHost 账号保活完成", flush=True)
        return 0
    except Exception as exc:
        state.update(
            {
                "last_check_time": now_cn(),
                "last_status": f"ERROR: {type(exc).__name__}: {exc}",
                "paymenter_update_endpoint": UPDATE_URL,
            }
        )
        save_state(state)
        print(f"❌ FenixHost 失败：{type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
