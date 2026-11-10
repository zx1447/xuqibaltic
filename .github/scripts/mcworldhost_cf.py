"""Solve the Cloudflare managed challenge on mcworldhost.de.

The site (since ~2026-11-08) answers non-browser sign-in requests with a
"cf-mitigated: challenge" interstitial. A headless Chromium gets flagged and
loops forever, but a HEADED Chromium on a virtual display (Xvfb) auto-passes
the challenge within seconds. This module launches such a browser, waits for
the challenge to clear, VERIFIES the clearance actually gets requests past
the edge (the cookie can take a few seconds to activate, and low-trust
clearances can be re-challenged), and hands back the clearance cookie plus
the exact User-Agent string (Cloudflare binds cf_clearance to IP + UA, so
both must be reused by every subsequent request).
"""
import os
import subprocess
import time

import requests


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


def _edge_ok(base: str, clearance: str, ua: str) -> bool:
    """True if a plain requests call with this clearance passes the edge."""
    try:
        r = requests.get(
            f"{base}/login",
            headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"},
            cookies={"cf_clearance": clearance},
            timeout=20,
            allow_redirects=False,
        )
        return r.status_code != 403
    except requests.RequestException:
        return True  # transport hiccup - do not treat as an edge block


def solve_challenge(base: str = "https://mcworldhost.de", wait_seconds: int = 75):
    """Return (cf_clearance, user_agent) or raise RuntimeError."""
    from playwright.sync_api import sync_playwright

    _ensure_display()
    last_title = ""
    with sync_playwright() as p:
        for attempt in (1, 2, 3):
            browser = p.chromium.launch(headless=False, args=["--no-sandbox"])
            context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            page = context.new_page()
            try:
                page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            deadline = time.time() + wait_seconds
            clearance = None
            ua = ""
            while time.time() < deadline:
                try:
                    last_title = page.title()
                except Exception:
                    last_title = ""
                if "just a moment" not in last_title.lower():
                    cookies = context.cookies(base)
                    clearance = next(
                        (c["value"] for c in cookies if c["name"] == "cf_clearance"), None
                    )
                    if clearance:
                        ua = page.evaluate("navigator.userAgent")
                        break
                time.sleep(3)
            browser.close()
            if not clearance:
                continue
            # The clearance can need a few seconds to activate at the edge.
            for settle in range(4):
                if _edge_ok(base, clearance, ua):
                    return clearance, ua
                print(f"clearance not active yet (check {settle + 1}); waiting...", flush=True)
                time.sleep(6)
            print("clearance rejected by the edge; re-solving...", flush=True)
            time.sleep(4)
    raise RuntimeError(
        f"Cloudflare challenge not solved (last title={last_title!r}); "
        "cf_clearance unavailable or not accepted."
    )
