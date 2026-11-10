"""Claim the daily login reward and buy the 7-day runtime extension if affordable.

This automation intentionally never opens the AFK page or loads ad slots.
"""
import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

BASE = "https://mcworldhost.de"
RUNTIME_OFFER = "7 days extra runtime"
RUNTIME_COST = 500


def create_session(email: str, password: str) -> requests.Session | None:
    session = requests.Session()
    try:
        response = session.post(
            f"{BASE}/api/auth/sign-in/email",
            json={"email": email, "password": password, "callbackURL": "/dashboard/coins"},
            headers={"Origin": BASE, "Referer": f"{BASE}/login"},
            timeout=25,
        )
    except requests.Timeout:
        print("Sign-in request timed out.", file=sys.stderr)
        return None
    except requests.RequestException as exc:
        print(f"Sign-in network error: {type(exc).__name__}.", file=sys.stderr)
        return None
    try:
        data = response.json()
    except ValueError:
        content_type = response.headers.get("content-type", "unknown").split(";")[0]
        via_cf = "yes" if response.headers.get("cf-ray") else "no"
        print(f"Sign-in returned non-JSON (HTTP {response.status_code}, type={content_type}, cloudflare={via_cf}).", file=sys.stderr)
        return None
    if response.status_code != 200 or not data.get("user"):
        content_type = response.headers.get("content-type", "unknown").split(";")[0]
        print(f"Sign-in rejected (HTTP {response.status_code}, type={content_type}).", file=sys.stderr)
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
        # Account/shop actions do not need ad resources.
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
            stage = "daily-claim"
            await page.get_by_text(re.compile(r"Daily login", re.I)).first.wait_for(timeout=20000)
            body = (await page.locator("body").inner_text()).lower()
            if "claimed today" in body:
                print("Daily login already claimed today.")
            else:
                claim = page.get_by_role("button", name=re.compile(r"daily\s*\(\+|täglich", re.I)).first
                await claim.wait_for(timeout=10000)
                if await claim.is_enabled():
                    await claim.click()
                    await page.wait_for_function(
                        "() => document.body.innerText.toLowerCase().includes('claimed today')",
                        timeout=20000,
                    )
                    print("Daily login reward claimed.")
                else:
                    print("Daily login is unavailable; continuing to check the shop.")

            stage = "check-runtime-offer"
            await page.goto(f"{BASE}/dashboard/shop", wait_until="domcontentloaded")
            await page.locator("article.ds-offer").first.wait_for(timeout=20000)
            balance_text = await page.locator(".ds-wallet__value").first.inner_text()
            balance = int(re.sub(r"[^0-9]", "", balance_text) or "0")
            if balance < RUNTIME_COST:
                print(f"Runtime extension not purchased: {balance}/{RUNTIME_COST} Coins.")
                return 0

            offer = page.locator("article.ds-offer").filter(
                has=page.get_by_role("heading", name=re.compile(r"^7 days extra runtime$", re.I))
            )
            if await offer.count() != 1:
                print("Could not uniquely identify the 7-day runtime offer; no purchase made.", file=sys.stderr)
                return 1
            buy_button = offer.get_by_role("button")
            if await buy_button.is_disabled():
                print("500 Coins available, but the 7-day offer is locked/active; no purchase made.")
                return 0
            if (await buy_button.inner_text()).strip().lower() != "buy":
                print("Offer is not in a purchasable state; no purchase made.")
                return 0

            stage = "purchase-runtime-extension"
            await buy_button.click()
            await page.wait_for_function(
                "() => document.body.innerText.includes('Purchase successful')",
                timeout=25000,
            )
            print("Purchased the 7-day runtime extension for 500 Coins.")
            return 0
        except Exception as exc:
            # Do not print page contents or credentials into Actions logs.
            print(f"MCWorldHost automation failed at {stage}: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
