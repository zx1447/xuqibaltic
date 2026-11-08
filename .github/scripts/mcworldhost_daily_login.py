"""Claim MCWorldHost's daily login reward once per day.

This intentionally does not open the AFK page or load ad slots.
"""
import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

BASE = "https://mcworldhost.de"


def create_session(email: str, password: str) -> requests.Session | None:
    session = requests.Session()
    try:
        response = session.post(
            f"{BASE}/api/auth/sign-in/email",
            json={"email": email, "password": password, "callbackURL": "/dashboard/coins"},
            headers={"Origin": BASE, "Referer": f"{BASE}/login"},
            timeout=25,
        )
        data = response.json()
    except Exception:
        print("Sign-in request failed.", file=sys.stderr)
        return None
    if response.status_code != 200 or not data.get("user"):
        print(f"Sign-in rejected (HTTP {response.status_code}).", file=sys.stderr)
        return None
    return session


async def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    if not email or not password:
        print("Missing MCWH_EMAIL or MCWH_PASSWORD secret.", file=sys.stderr)
        return 2

    session = create_session(email, password)
    if session is None:
        return 1
    token = session.cookies.get("__Secure-better-auth.session_token")
    if not token:
        print("Sign-in did not return the expected session cookie.", file=sys.stderr)
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(locale="en-US")
        # The daily claim does not need advertising resources; keep this job limited to sign-in.
        await context.route("**/api/ad-slot**", lambda route: route.abort())
        await context.add_cookies([{
            "name": "__Secure-better-auth.session_token",
            "value": token,
            "domain": "mcworldhost.de",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }])
        page = await context.new_page()
        stage = "open-coins"
        try:
            await page.goto(f"{BASE}/dashboard/coins", wait_until="domcontentloaded")
            if "/login" in page.url:
                print("Site did not accept the authenticated session.", file=sys.stderr)
                return 1
            stage = "find-daily"
            await page.get_by_text(re.compile(r"Daily login", re.I)).first.wait_for(timeout=20000)
            body = (await page.locator("body").inner_text()).lower()
            if "claimed today" in body:
                print("Daily login was already claimed today; no action needed.")
                return 0

            stage = "find-claim-button"
            claim = page.get_by_role("button", name=re.compile(r"daily\s*\(\+|täglich", re.I)).first
            await claim.wait_for(timeout=10000)
            if not await claim.is_enabled():
                print("Daily claim is not available (possibly already claimed). No action taken.")
                return 0

            stage = "click-claim"
            await claim.click()
            stage = "verify-claim"
            await page.wait_for_function(
                "() => document.body.innerText.toLowerCase().includes('claimed today')",
                timeout=20000,
            )
            print("Daily login reward claimed successfully.")
            return 0
        except Exception as exc:
            # Do not print page contents or credentials into Actions logs.
            print(f"Daily login automation failed at {stage}: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
