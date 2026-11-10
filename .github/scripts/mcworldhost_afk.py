"""MCWorldHost AFK coin farming over in-browser fetches (no ad slots).

Protocol (reverse-engineered from the /dashboard/afk Next.js client chunk):

1. Pass the Cloudflare challenge and sign in inside the same browser
   (python-requests is rejected by the edge even with a valid clearance).
2. GET /dashboard/afk -> the HTML references page-<hash>.js for the route.
3. Fetch that chunk (in-page) -> extract the two server-action IDs:
   - armAfkWatchAction        : starts/refreshes a watch session, returns
                                {ok, token, remainingSeconds, durationSeconds,
                                 reward, endsAtMs}
   - claimAfkWatchRewardAction: POST ["<token>"] after the watch window,
                                returns {ok, balance, reward, nextToken,
                                remainingSeconds, endsAtMs}
4. Loop: arm -> sleep ~62s -> claim -> use nextToken for the next round.
   All calls run as page.evaluate(fetch(...)) so they carry the browser's
   cookies, TLS fingerprint and clearance.

Server responses on failure (German locale):
  {"ok": false, "error": "Noch 60s warten"}   - account cooldown, 60s since
                                                the last credited reward
  {"ok": false, "error": "Fehlgeschlagen"}    - token expired/invalid -> re-arm
  {"ok": false, "code": "hour_limit"|"day_limit"|"rate_limit",
   "caps": {...}, "retryAfterSeconds": N}

Limits observed on the site: +3 coins per 60s claim, 150 per rolling hour,
500 per day. The script therefore exits (exit 0) on day_limit/hour_limit so
an hourly scheduled workflow can pick up the remainder in the next run.

This automation never loads ad iframes; it only replays the watch actions.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
AFK_PAGE = "/dashboard/afk"
CLAIM_INTERVAL = 62          # seconds between claims (60s window + jitter)
CLAIM_JITTER = 2
DEFAULT_MAX_MINUTES = 55     # matches the hourly GitHub Actions cadence
CHUNK_RE = re.compile(r"page-[0-9a-f]+\.js")
ACTION_RE = re.compile(r'createServerReference\)?\("([0-9a-f]{40,})"[^)]*"(\w+)"')


def log(msg: str) -> None:
    print(msg, flush=True)


def call_action(page, action_id: str, args: list):
    """POST a Next.js server action from inside the page (browser stack)."""
    body = json.dumps(args)
    text = page.evaluate(
        """async ([aid, body]) => {
            const r = await fetch(window.location.pathname, {
                method: 'POST',
                headers: {
                    'Accept': 'text/x-component',
                    'Next-Action': aid,
                    'Content-Type': 'application/json',
                },
                body: body,
            });
            return await r.text();
        }""",
        [action_id, body],
    )
    payloads = flight_payloads(text)
    return next((p for p in payloads if "ok" in p), None)


def flight_payloads(text: str) -> list:
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^[0-9a-f]+:(\{.*\})$", line.strip())
        if m:
            try:
                obj = json.loads(m.group(1))
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def resolve_action_ids(page) -> tuple:
    """Read the AFK page and its route chunk (in-page) for the action IDs."""
    page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
    if "/login" in page.url:
        raise RuntimeError("session rejected by the site (login redirect)")
    html = page.content()
    match = CHUNK_RE.search(html)
    if not match:
        raise RuntimeError("could not find the AFK route chunk in the page HTML")
    chunk_url = f"{BASE}/_next/static/chunks/app/(dashboard)/dashboard/afk/{match.group(0)}"
    js = page.evaluate("async (u) => await (await fetch(u)).text()", chunk_url)
    ids = {name: aid for aid, name in ACTION_RE.findall(js)}
    arm, claim = ids.get("armAfkWatchAction"), ids.get("claimAfkWatchRewardAction")
    if not arm or not claim:
        raise RuntimeError("AFK server actions missing from the route chunk")
    return arm, claim


def classify_failure(res) -> str:
    if not res:
        return "no_payload"
    if res.get("code") in ("hour_limit", "day_limit", "rate_limit"):
        return res["code"]
    error = str(res.get("error") or "")
    if "warten" in error.lower():
        return "cooldown"
    return "invalid_token"


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
            log(f"Sign-in failed: {type(exc).__name__}")
            return 1

        try:
            arm_id, claim_id = resolve_action_ids(page)
        except Exception as exc:
            log(f"AFK actions not resolved: {exc}")
            return 1
        log("AFK actions resolved.")

        earned = 0
        balance = None
        armed = False
        token = None
        while time.time() < deadline:
            if not armed:
                try:
                    res = call_action(page, arm_id, [])
                except Exception as exc:
                    log(f"arm network error: {type(exc).__name__}")
                    time.sleep(30)
                    continue
                kind = classify_failure(res)
                if res and res.get("ok"):
                    token = res["token"]
                    armed = True
                    log(f"armed watch token (reward {res.get('reward')} coins / "
                        f"{res.get('durationSeconds')}s)")
                elif kind == "day_limit":
                    log("AFK daily limit reached; stopping for today.")
                    return 0
                elif kind in ("hour_limit", "rate_limit"):
                    wait = int((res or {}).get("retryAfterSeconds", 600))
                    if time.time() + min(wait, 900) >= deadline:
                        log(f"{kind} for {wait}s; resuming in the next scheduled run.")
                        return 0
                    log(f"{kind}; waiting {wait}s within this run.")
                    time.sleep(wait + 5)
                else:
                    log(f"arm failed ({kind}); re-signing in and retrying.")
                    try:
                        sign_in(page, BASE, email, password)
                        time.sleep(10)
                        arm_id, claim_id = resolve_action_ids(page)
                    except Exception as exc:
                        log(f"re-sign-in failed: {type(exc).__name__}")
                        time.sleep(30)
                    continue

            time.sleep(CLAIM_INTERVAL + (CLAIM_JITTER * time.time() % 1))
            try:
                res = call_action(page, claim_id, [token])
            except Exception as exc:
                log(f"claim network error: {type(exc).__name__}; re-arming.")
                armed = False
                time.sleep(10)
                continue
            if res and res.get("ok"):
                balance = res.get("balance", balance)
                earned += int(res.get("reward", 3) or 3)
                armed = False
                next_token = res.get("nextToken")
                log(f"claimed +{res.get('reward')} coins (balance {balance}, "
                    f"+{earned} this run)")
                if next_token:
                    # keep the chain: next watch window is already armed
                    token = next_token
                    armed = True
                continue
            kind = classify_failure(res)
            armed = False
            if kind == "day_limit":
                log("AFK daily limit reached; stopping for today.")
                return 0
            if kind in ("hour_limit", "rate_limit"):
                log(f"{kind} hit; resuming in the next scheduled run.")
                return 0
            if kind == "cooldown":
                log("cooldown; re-arming in 20s.")
                time.sleep(20)
            else:
                log("claim failed (invalid/expired token); re-arming.")
                time.sleep(10)
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
