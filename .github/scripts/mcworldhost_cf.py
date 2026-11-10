"""Pass the Cloudflare managed challenge on mcworldhost.de and sign in.

Since ~2026-11-08 the site answers non-browser requests with a
"cf-mitigated: challenge" interstitial. Headless Chromium is flagged and
loops forever, but a HEADED Chromium on a virtual display (Xvfb) auto-passes
within seconds. The clearance cookie only validates for the browser's own
network stack on stricter edges (python-requests can still get 403 with a
valid clearance), so ALL site interaction must happen inside this browser:
prefer page navigation/clicks, or page.evaluate(fetch(...)) for API calls -
never plain requests.Session.
"""
import os
import re
import subprocess
import time


def _ensure_display() -> None:
    if os.environ.get("DISPLAY"):
        return
    try:
        subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError("DISPLAY is not set and Xvfb is not installed (apt-get install xvfb).")
    os.environ["DISPLAY"] = ":99"
    time.sleep(2)


def open_session(base: str = "https://mcworldhost.de", wait_seconds: int = 90):
    """Launch a headed Chromium, wait out the challenge, return (pw, browser, page).

    The caller owns the browser: finish with browser.close(); pw.stop().
    """
    from playwright.sync_api import sync_playwright

    _ensure_display()
    pw = sync_playwright().start()
    last_title = ""
    try:
        browser = pw.chromium.launch(headless=False, args=["--no-sandbox"])
    except Exception:
        pw.stop()
        raise
    context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
    page = context.new_page()
    try:
        page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            last_title = page.title()
        except Exception:
            last_title = ""
        if "just a moment" not in last_title.lower():
            try:
                # a real (non-challenge) page must render actual content
                page.wait_for_selector("h1", timeout=8000)
                return pw, browser, page
            except Exception:
                pass
        time.sleep(3)
    browser.close()
    pw.stop()
    raise RuntimeError(f"Cloudflare challenge not cleared (last title={last_title!r}).")


def sign_in(page, base: str, email: str, password: str, timeout: int = 45000) -> None:
    """Submit the login form; raises on failure. Never logs credentials."""
    page.goto(f"{base}/login", wait_until="domcontentloaded")
    for attempt in (1, 2):
        page.get_by_role("textbox", name=re.compile(r"mail", re.I)).first.wait_for(timeout=timeout)
        page.get_by_role("textbox", name=re.compile(r"mail", re.I)).first.fill(email)
        page.get_by_role("textbox", name=re.compile(r"pass(wort)?", re.I)).first.fill(password)
        page.get_by_role("button", name=re.compile(r"sign\s*in|anmelden", re.I)).first.click()
        try:
            page.wait_for_url("**/dashboard/**", timeout=timeout)
            return
        except Exception:
            body = ""
            try:
                body = page.locator("body").inner_text()[:2000].lower()
            except Exception:
                pass
            if "invalid email" in body or "ungültig" in body:
                raise RuntimeError("invalid credentials")
            # transient (challenge hold or click race) -> one more attempt
    raise RuntimeError("login did not reach the dashboard after 2 attempts")
