#!/usr/bin/env python3
"""FenixHost free-service renewal with encrypted Paymenter session reuse."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken

BASE_URL = "https://fenixhost.net"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
SERVICES_URL = f"{BASE_URL}/services"
UPDATE_URL = f"{BASE_URL}/paymenter/update"
STATE_FILE = Path("fenixhost_state.json")
EMAIL = os.environ.get("FENIXHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("FENIXHOST_PASSWORD", "").strip()
SESSION_KEY = os.environ.get("FENIXHOST_SESSION_KEY", "").strip()
PROXY = os.environ.get("FENIXHOST_PROXY", "").strip()
FORCE_RENEW = os.environ.get("FENIXHOST_FORCE_RENEW", "").strip().lower() in {
    "1", "true", "yes", "on"
}
RENEW_THRESHOLD_SECONDS = max(
    3600,
    int(os.environ.get("FENIXHOST_RENEW_THRESHOLD_SECONDS", str(2 * 86400)) or 2 * 86400),
)
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def timestamp_text(value: int | float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


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
            "domain": item.get("domain") or ".fenixhost.net",
            "path": item.get("path") or "/",
        }
        for item in cookies
        if item.get("name") and item.get("value")
    ]
    state["encrypted_cookies"] = Fernet(SESSION_KEY.encode()).encrypt(
        json.dumps(clean, separators=(",", ":")).encode()
    ).decode()
    state["session_saved_time"] = now_cn()


def cookie_list_from_session(session: requests.Session) -> list[dict]:
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]


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
            domain=item.get("domain") or ".fenixhost.net",
            path=item.get("path") or "/",
        )
    return session


def authenticated_session(cookies: list[dict]) -> tuple[requests.Session | None, str]:
    if not cookies:
        return None, ""
    session = make_session(cookies)
    try:
        response = session.get(DASHBOARD_URL, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        print(f"⚠️ 会话检查网络错误：{type(exc).__name__}", flush=True)
        return None, ""
    if response.status_code == 200 and "/login" not in response.url:
        return session, response.text
    return None, ""


def patch_seleniumbase_filelock() -> None:
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


def solve_turnstile(browser) -> None:
    if turnstile_token(browser):
        return
    print("🛡️ 使用 SCYED 分支同款 VLESS + SeleniumBase UC 处理 Turnstile", flush=True)
    methods = (
        lambda: browser.driver.uc_gui_click_cf(frame="#cf-turnstile", retry=False),
        lambda: browser.driver.uc_gui_click_captcha(),
        lambda: browser.driver.uc_gui_handle_cf(frame="#cf-turnstile"),
    )
    for index, method in enumerate(methods, 1):
        try:
            method()
        except Exception as exc:
            print(f"⚠️ Turnstile 第 {index} 种方式跳过：{type(exc).__name__}", flush=True)
        for _ in range(15):
            if turnstile_token(browser):
                print("✅ FenixHost Turnstile token 已生成", flush=True)
                return
            time.sleep(1)
    raise RuntimeError("Cloudflare Turnstile verification did not complete")


def browser_login() -> list[dict]:
    if not EMAIL or not PASSWORD:
        raise RuntimeError("missing FENIXHOST_EMAIL/FENIXHOST_PASSWORD")
    patch_seleniumbase_filelock()
    from seleniumbase import SB

    options = {
        "uc": True,
        "xvfb": True,
        "headless": False,
        "incognito": True,
        "locale": "en",
        "window_size": "1280,720",
        "host_resolver_rules": (
            "MAP *.challenges.cloudflare.com 104.18.94.41, EXCLUDE localhost"
        ),
    }
    if PROXY:
        options["proxy"] = PROXY
    with SB(**options) as browser:
        browser.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
        browser.wait_for_ready_state_complete()
        time.sleep(3)
        solve_turnstile(browser)
        browser.type("#email", EMAIL, timeout=15)
        browser.type("#password", PASSWORD, timeout=15)
        try:
            if not browser.is_selected("#remember"):
                browser.click("#remember")
        except Exception:
            pass
        browser.uc_click('button[type="submit"]')
        for _ in range(60):
            time.sleep(1)
            if "/login" not in browser.get_current_url():
                cookies = browser.get_cookies()
                print(f"✅ FenixHost 浏览器登录成功：{browser.get_current_url()}", flush=True)
                return cookies
        raise RuntimeError("FenixHost login remained on /login")


def service_urls(session: requests.Session) -> list[str]:
    response = session.get(SERVICES_URL, timeout=30)
    if response.status_code != 200 or "/login" in response.url:
        raise RuntimeError("services page is not authenticated")
    soup = BeautifulSoup(response.text, "html.parser")
    urls: list[str] = []
    for anchor in soup.select('a[href*="/services/"]'):
        url = urljoin(BASE_URL, anchor.get("href", ""))
        if re.fullmatch(r"https://fenixhost\.net/services/\d+", url) and url not in urls:
            urls.append(url)
    return urls


def parse_service_page(html_text: str, url: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    heading = soup.find("h1")
    name = " ".join(heading.get_text(" ", strip=True).split()) if heading else url.rsplit("/", 1)[-1]
    timer = soup.select_one(".countdown-timer[data-expires]")
    expiry = int(timer.get("data-expires")) if timer and str(timer.get("data-expires", "")).isdigit() else 0
    renew_button = soup.select_one('[wire\\:click="renewFree"]')
    component_snapshot = None
    for element in soup.select("[wire\\:snapshot]"):
        raw = element.get("wire:snapshot")
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if snapshot.get("memo", {}).get("name") == "services.show":
            component_snapshot = raw
            break
    csrf = soup.select_one('meta[name="csrf-token"]')
    return {
        "url": url,
        "service_id": url.rsplit("/", 1)[-1],
        "name": name,
        "expiry_timestamp": expiry,
        "expiry_time": timestamp_text(expiry),
        "renew_available": renew_button is not None,
        "snapshot": component_snapshot,
        "csrf": csrf.get("content") if csrf else None,
    }


def renew_service(session: requests.Session, info: dict) -> dict:
    if not info.get("snapshot") or not info.get("csrf"):
        raise RuntimeError("services.show Livewire snapshot or CSRF token missing")
    payload = {
        "_token": info["csrf"],
        "components": [
            {
                "snapshot": info["snapshot"],
                "updates": {},
                "calls": [{"path": "", "method": "renewFree", "params": []}],
            }
        ],
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Livewire": "",
        "X-CSRF-TOKEN": info["csrf"],
        "Origin": BASE_URL,
        "Referer": info["url"],
    }
    response = session.post(UPDATE_URL, headers=headers, json=payload, timeout=60)
    try:
        result = response.json()
    except ValueError:
        result = {"raw": response.text[:500]}
    if response.status_code != 200:
        raise RuntimeError(f"renewFree HTTP {response.status_code}: {str(result)[:300]}")
    errors = {}
    try:
        returned_snapshot = json.loads(result["components"][0]["snapshot"])
        errors = returned_snapshot.get("memo", {}).get("errors") or {}
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    if errors:
        raise RuntimeError(f"renewFree validation error: {errors}")
    time.sleep(2)
    refreshed = session.get(info["url"], timeout=30)
    after = parse_service_page(refreshed.text, info["url"])
    return {
        "http_status": response.status_code,
        "before_expiry_timestamp": info.get("expiry_timestamp", 0),
        "before_expiry_time": info.get("expiry_time"),
        "after_expiry_timestamp": after.get("expiry_timestamp", 0),
        "after_expiry_time": after.get("expiry_time"),
    }


def check_and_renew_services(session: requests.Session, state: dict) -> list[dict]:
    urls = service_urls(session)
    if not urls:
        raise RuntimeError("no FenixHost services found")
    results: list[dict] = []
    current = int(time.time())
    for url in urls:
        response = session.get(url, timeout=30)
        info = parse_service_page(response.text, url)
        remaining = max(0, int(info.get("expiry_timestamp", 0)) - current)
        result = {
            k: info.get(k)
            for k in ("service_id", "name", "url", "expiry_timestamp", "expiry_time", "renew_available")
        }
        result["remaining_seconds"] = remaining
        if not info.get("renew_available"):
            result["action"] = "no_free_renew_button"
            print(f"ℹ️ {info['name']} 没有免费续期按钮", flush=True)
        elif not FORCE_RENEW and remaining > RENEW_THRESHOLD_SECONDS:
            result["action"] = "waiting"
            print(
                f"⏳ {info['name']} 尚未进入续期窗口；到期：{info['expiry_time']}，"
                f"剩余约 {remaining / 86400:.1f} 天",
                flush=True,
            )
        else:
            print(f"🔄 调用 Paymenter renewFree：{info['name']}", flush=True)
            renewal = renew_service(session, info)
            result.update(renewal)
            result["action"] = "renewed"
            state["total_renew_success"] = int(state.get("total_renew_success", 0)) + 1
            state["last_renew_time"] = now_cn()
            print(
                f"✅ 免费服务续期成功；新到期时间：{renewal.get('after_expiry_time')}",
                flush=True,
            )
        results.append(result)
    return results


def main() -> int:
    print("🚀 FenixHost 免费服务自动续期启动", flush=True)
    print(f"🕐 北京时间：{now_cn()}", flush=True)
    print(f"🔗 Livewire：{UPDATE_URL}", flush=True)
    if not SESSION_KEY:
        print("❌ 缺少 FENIXHOST_SESSION_KEY", flush=True)
        return 1

    state = load_state()
    try:
        session, _ = authenticated_session(decrypt_cookies(state))
        login_source = "encrypted_session"
        if session is None:
            print("🔄 加密会话失效，使用 VLESS + UC 浏览器重新登录", flush=True)
            cookies = browser_login()
            session, _ = authenticated_session(cookies)
            if session is None:
                raise RuntimeError("browser login did not create an authenticated session")
            login_source = "vless_seleniumbase_uc"
        else:
            print("✅ FenixHost 加密登录会话有效", flush=True)

        results = check_and_renew_services(session, state)
        encrypt_cookies(cookie_list_from_session(session), state)
        state.update(
            {
                "last_check_time": now_cn(),
                "last_status": "success",
                "last_login_source": login_source,
                "paymenter_update_endpoint": UPDATE_URL,
                "renew_threshold_seconds": RENEW_THRESHOLD_SECONDS,
                "services": results,
            }
        )
        save_state(state)
        print("✅ FenixHost 检查完成", flush=True)
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
