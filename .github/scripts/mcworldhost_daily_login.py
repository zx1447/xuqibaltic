"""Claim the daily login reward and buy the 7-day runtime extension if affordable.

This automation intentionally never opens the AFK page or loads ad slots.

Everything runs inside a headed Chromium on Xvfb: since 2026-11-08 the site
sits behind a Cloudflare managed challenge that (a) flags headless browsers
and (b) rejects python-requests even with a valid clearance cookie, so the
sign-in happens through the real login form in the browser.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
RUNTIME_OFFER = "7 days extra runtime"
RUNTIME_COST = 500


def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    if not email or not password:
        print("Missing MCWH_EMAIL or MCWH_PASSWORD secret.", file=sys.stderr)
        return 2

    print("Solving Cloudflare challenge (headed Chromium on Xvfb)...", flush=True)
    try:
        pw, browser, page = open_session(BASE)
    except Exception as exc:
        print(f"Cloudflare challenge failed: {exc}", file=sys.stderr)
        return 1

    stage = "sign-in"
    try:
        try:
            sign_in(page, BASE, email, password)
            print("Signed in.")
        except Exception as exc:
            print(f"Sign-in failed: {type(exc).__name__}", file=sys.stderr)
            return 1

        stage = "open-coins"
        page.goto(f"{BASE}/dashboard/coins", wait_until="domcontentloaded")
        if "/login" in page.url:
            print("Site did not accept the authenticated session.", file=sys.stderr)
            return 1

        stage = "daily-claim"
        page.get_by_text(re.compile(r"Daily login", re.I)).first.wait_for(timeout=20000)
        body = page.locator("body").inner_text().lower()
        if "claimed today" in body:
            print("Daily login already claimed today.")
        else:
            claim = page.get_by_role("button", name=re.compile(r"daily\s*\(\+|täglich", re.I)).first
            claim.wait_for(timeout=10000)
            if claim.is_enabled():
                claim.click()
                page.wait_for_function(
                    "() => document.body.innerText.toLowerCase().includes('claimed today')",
                    timeout=20000,
                )
                print("Daily login reward claimed.")
            else:
                print("Daily login is unavailable; continuing to check the shop.")

        stage = "check-runtime-offer"
        page.goto(f"{BASE}/dashboard/shop", wait_until="domcontentloaded")
        page.locator("article.ds-offer").first.wait_for(timeout=20000)
        balance_text = page.locator(".ds-wallet__value").first.inner_text()
        balance = int(re.sub(r"[^0-9]", "", balance_text) or "0")
        if balance < RUNTIME_COST:
            print(f"Runtime extension not purchased: {balance}/{RUNTIME_COST} Coins.")
            return 0

        offer = page.locator("article.ds-offer").filter(
            has=page.get_by_role("heading", name=re.compile(r"^7 days extra runtime$", re.I))
        )
        if offer.count() != 1:
            print("Could not uniquely identify the 7-day runtime offer; no purchase made.", file=sys.stderr)
            return 1
        buy_button = offer.get_by_role("button")
        if buy_button.is_disabled():
            print("500 Coins available, but the 7-day offer is locked/active; no purchase made.")
            return 0
        if buy_button.inner_text().strip().lower() != "buy":
            print("Offer is not in a purchasable state; no purchase made.")
            return 0

        stage = "purchase-runtime-extension"
        buy_button.click()
        page.wait_for_function(
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
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
