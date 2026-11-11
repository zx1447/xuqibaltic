"""MCWorldHost AFK coin farming via the real UI (headed browser on Xvfb).

The site (2026-11 build) credits +3 coins per 60s ONLY while the Earn AFK tab
has its ad slots actually rendering (the page waits for a "mcwh-ad-status"
ok=true postMessage from the same-origin ad wrapper). If the ads never load,
it shows a "No ad blocker" warning and keeps the 60s ring paused - no coins.

Observed on GitHub runners (and our sandbox): the ad network Adsterra
(highperformanceformat.com) returns 403 for real-browser UA requests from
datacenter IPs, so the wrapper never reports ok -> crediting never starts.
Nothing on our side can honestly change that; faking the postMessage would be
ad fraud and is NOT done. So the run is adaptive:

  - ads blocked  -> one "Check again" retry, then exit early (no wasted minutes)
  - ads healthy  -> keep the page open like a user, reload every 4 min to
    re-arm the watch window, track the balance text for progress

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


def ads_unavailable(page) -> bool:
    """True when the page shows the 'No ad blocker' warning (= crediting paused)."""
    try:
        page.wait_for_selector("text=No ad blocker", timeout=8000)
        return True
    except Exception:
        return False


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
        page.wait_for_timeout(8000)  # give the ad wrapper time to report status

        # —— 广告可用性判定: Adsterra 拒绝数据中心 IP 时站点显示警告、计币暂停 ——
        if ads_unavailable(page):
            try:
                page.get_by_role("button", name=re.compile(r"check again", re.I)).first.click(timeout=8000)
                log("clicked 'Check again'; re-checking ad status in 20s...")
                page.wait_for_timeout(20000)
            except Exception:
                pass
            if ads_unavailable(page):
                balance, today = read_counters(page)
                log(f"ads are NOT served to this egress (Adsterra 403 for datacenter IPs) - "
                    f"site keeps crediting paused (balance={balance}, today={today}/{DAY_CAP}).")
                log("Nothing honest to automate here; exiting early instead of idling 52 min.")
                return 0
        log("ad status ok - crediting engine should be running.")

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
