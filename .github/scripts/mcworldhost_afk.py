"""MCWorldHost AFK coin farming via the real UI (headed browser on Xvfb).

The site (2026-11 build) credits +3 coins per 60s while the Earn AFK tab is
open with its ad slots visible ("Keep tab open · +3 coins every 60s · reload
every 5 min"), and the old server-action IDs no longer live in the page
chunk (the build splits them into loader + sibling chunks now). So we simply
keep the page open like a user and reload it every few minutes to re-arm the
watch window, tracking the balance text for progress.

All interaction happens in the same headed Chromium that solved the
Cloudflare challenge and signed in - python-requests is rejected by the edge
even with a valid clearance cookie, so the browser stays the only channel.

Caps observed: +3/60s, 150 per rolling hour, 500 per day.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
AFK_PAGE = "/dashboard/afk"
RELOAD_EVERY = 240          # site says "reload every 5 min"; 4 min is safe
DEFAULT_MAX_MINUTES = 55    # matches the hourly GitHub Actions cadence
DAY_CAP = 500


def log(msg: str) -> None:
    print(msg, flush=True)


def read_counters(page):
    """Return (balance, today_earned) parsed from the AFK page text."""
    try:
        text = page.locator("body").inner_text(timeout=10000)
    except Exception:
        return None, None
    m = re.search(r"Balance\s+(\d+)", text)
    balance = int(m.group(1)) if m else None
    m = re.search(r"Today\s+(\d+)\s*/\s*%d" % DAY_CAP, text)
    today = int(m.group(1)) if m else None
    return balance, today


def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    max_minutes = float(os.environ.get("MCWH_AFK_MAX_MINUTES", DEFAULT_MAX_MINUTES))
    if not email or not password:
        log("Missing MCWH_EMAIL or MCWH_PASSWORD secret.")
        return 2

    deadline = time.time() + max_minutes * 60
    log("Solving Cloudflare challenge (headed Chromium on Xvfb)...")
    try:
        pw, browser, page = open_session(BASE)
    except Exception as exc:
        log(f"Cloudflare challenge failed: {exc}")
        return 1

    try:
        try:
            sign_in(page, BASE, email, password)
            log("Signed in.")
        except Exception as exc:
            log(f"Sign-in failed: {type(exc).__name__}: {str(exc)[:180]}")
            return 1

        page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
        if "/login" in page.url:
            log("session rejected by the site (login redirect).")
            return 1
        try:
            page.wait_for_selector("text=Balance", timeout=20000)
        except Exception:
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_selector("text=Balance", timeout=15000)
            except Exception:
                log("AFK page did not render a Balance readout.")
                return 1

        balance, today = read_counters(page)
        log(f"AFK watch started: balance={balance}, today={today}/{DAY_CAP}.")
        if today is not None and today >= DAY_CAP:
            log("AFK daily cap already reached; stopping for today.")
            return 0

        earned = 0
        while time.time() < deadline:
            time.sleep(RELOAD_EVERY)
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            if "/dashboard/afk" not in page.url:
                # an ad navigation or logout kicked us off the page
                log("page left the AFK route; navigating back.")
                try:
                    page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
                except Exception as exc:
                    log(f"navigate-back failed: {type(exc).__name__}")
                    continue
            new_balance, new_today = read_counters(page)
            if new_balance is None:
                log("balance readout missing after reload; retrying next cycle.")
                continue
            if last := balance:
                delta = new_balance - balance
                if delta > 0:
                    earned += delta
                    log(f"balance {balance} -> {new_balance} (+{delta}, +{earned} this run)")
                elif delta == 0:
                    log(f"balance unchanged at {new_balance} (cooldown/cap?)")
                else:
                    log(f"balance dropped {balance} -> {new_balance} (purchase elsewhere?)")
            balance = new_balance
            if new_today is not None and new_today >= DAY_CAP:
                log(f"AFK daily cap reached ({new_today}/{DAY_CAP}); stopping for today.")
                return 0
        log(f"watch window over: +{earned} coins this run (balance {balance}).")
        return 0
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
