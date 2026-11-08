"""MCWorldHost coins & runtime management (check-in + renewal, shop-rule aware).

1. Claim the daily login reward.
2. Spend coins strictly per the shop rules - every boost is
   "one active at a time, repurchasable after expiry":
   - the "7 days extra runtime" offer (500 coins) is bought whenever the
     shop shows it in a purchasable state (the site only shows a live Buy
     button when the balance covers it);
   - while that boost is active the offer is locked, so coins are banked
     until it expires - never re-bought on top;
   - safety bridge: if the server runtime is critical (<= 2 days) while the
     7-day offer is locked, the cheaper "24h extra runtime" boost (100
     coins) is bought to keep the server alive until the 7-day offer
     unlocks again.

Set MCWH_DRY_RUN=1 to log the purchase decisions without clicking.
"""
import asyncio
import os
import re
import sys

import requests
from playwright.async_api import async_playwright

BASE = "https://mcworldhost.de"
RUNTIME_OFFER = "7 days extra runtime"
BRIDGE_OFFER = "24h extra runtime"
RUNTIME_COST = 500
BRIDGE_COST = 100
BRIDGE_RUNTIME_DAYS = 2.0  # buy the 24h bridge at/below this many days left


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


def read_runtime_days(session: requests.Session) -> float | None:
    """Parse the server runtime chip from the dashboard HTML."""
    try:
        html = session.get(f"{BASE}/dashboard/servers", timeout=25).text
    except Exception:
        return None
    match = re.search(
        r"(?:ds-hub-chip__v[^>]*>|<dt>Runtime</dt><dd[^>]*>)\s*(\d+)\s*([dhm])",
        html,
    )
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return {"d": float(value), "h": value / 24.0, "m": value / 1440.0}[unit]


async def offer_state(page, name: str) -> str:
    """Return 'buyable' | 'not_enough' | 'locked' | 'missing' for a shop offer."""
    offer = page.locator("article.ds-offer").filter(
        has=page.get_by_role("heading", name=re.compile(f"^{re.escape(name)}$", re.I))
    )
    if await offer.count() != 1:
        print(f"Could not uniquely identify the {name} offer.", file=sys.stderr)
        return "missing"
    button = offer.get_by_role("button")
    text = (await button.inner_text()).strip().lower()
    if "not enough" in text:
        return "not_enough"
    if await button.is_disabled() or text != "buy":
        return "locked"
    return "buyable"


async def buy_offer(page, name: str) -> bool:
    offer = page.locator("article.ds-offer").filter(
        has=page.get_by_role("heading", name=re.compile(f"^{re.escape(name)}$", re.I))
    )
    button = offer.get_by_role("button")
    await button.click()
    await page.wait_for_function(
        "() => document.body.innerText.includes('Purchase successful')",
        timeout=25000,
    )
    return True


async def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    dry_run = os.environ.get("MCWH_DRY_RUN", "") == "1"
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

    runtime_days = read_runtime_days(session)
    if runtime_days is None:
        print("Could not read the server runtime; purchase decisions degraded.", file=sys.stderr)
    else:
        print(f"Server runtime remaining: {runtime_days:.1f} days.")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(locale="en-US")
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

            stage = "shop-decision"
            await page.goto(f"{BASE}/dashboard/shop", wait_until="domcontentloaded")
            await page.locator("article.ds-offer").first.wait_for(timeout=20000)
            balance_text = await page.locator(".ds-wallet__value").first.inner_text()
            balance = int(re.sub(r"[^0-9]", "", balance_text) or "0")
            state7 = await offer_state(page, RUNTIME_OFFER)
            state24 = await offer_state(page, BRIDGE_OFFER)
            print(f"Shop: balance {balance}, 7-day offer {state7}, 24h offer {state24}.")

            runtime_low = runtime_days is not None and runtime_days <= BRIDGE_RUNTIME_DAYS

            if state7 == "buyable" and balance >= RUNTIME_COST:
                stage = "purchase-runtime-extension"
                if dry_run:
                    print(f"[dry-run] Would buy '{RUNTIME_OFFER}' for {RUNTIME_COST} coins.")
                    return 0
                await buy_offer(page, RUNTIME_OFFER)
                print(f"Purchased '{RUNTIME_OFFER}' for {RUNTIME_COST} coins "
                      f"(renewal window locked for 7 days; coins bank until expiry).")
                fresh = read_runtime_days(session)
                if fresh is not None:
                    print(f"Server runtime now: {fresh:.1f} days.")
                return 0

            if runtime_low and balance >= BRIDGE_COST and state24 == "buyable":
                stage = "purchase-bridge"
                if dry_run:
                    print(f"[dry-run] Runtime {runtime_days:.1f}d critical: "
                          f"would buy '{BRIDGE_OFFER}' for {BRIDGE_COST} coins as a bridge.")
                    return 0
                await buy_offer(page, BRIDGE_OFFER)
                print(f"Purchased '{BRIDGE_OFFER}' for {BRIDGE_COST} coins as a runtime bridge.")
                return 0

            if state7 == "locked":
                print(f"7-day offer locked (one-active rule); banking coins at {balance}.")
            elif state7 == "not_enough":
                print(f"Banking coins for the 7-day offer: {balance}/{RUNTIME_COST}.")
            else:
                print(f"7-day offer state '{state7}'; banking coins at {balance}.")
            return 0
        except Exception as exc:
            print(f"MCWorldHost automation failed at {stage}: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
