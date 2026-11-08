"""Claim MCWorldHost's daily login reward once per day.

This intentionally does not open the AFK page or load ad slots.
"""
import asyncio
import os
import re
import sys
from playwright.async_api import async_playwright

BASE = "https://mcworldhost.de"


async def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    if not email or not password:
        print("Missing MCWH_EMAIL or MCWH_PASSWORD secret.", file=sys.stderr)
        return 2

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(locale="en-US")
        # The daily claim does not need advertising resources; keep this job limited to sign-in.
        await context.route("**/api/ad-slot**", lambda route: route.abort())
        page = await context.new_page()
        stage = "open-login"
        try:
            await page.goto(f"{BASE}/login?next=%2Fdashboard%2Fcoins", wait_until="domcontentloaded")
            if "/login" in page.url:
                stage = "fill-login"
                await page.locator("#email").fill(email)
                await page.locator("#password").fill(password)
                stage = "submit-login"
                await page.get_by_role("button", name=re.compile(r"sign in", re.I)).click()
                await page.wait_for_url(re.compile(r".*/dashboard(?:/.*)?$"), timeout=30000)

            stage = "open-coins"
            await page.goto(f"{BASE}/dashboard/coins", wait_until="domcontentloaded")
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
            print(f"Daily login automation failed at {stage}: {type(exc).__name__}; url={page.url.split('?')[0]}", file=sys.stderr)
            return 1
        finally:
            await browser.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
