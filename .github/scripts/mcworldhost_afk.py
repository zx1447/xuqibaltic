"""MCWorldHost AFK coin farming over pure HTTP (no browser, no ad slots).

Protocol (reverse-engineered from the /dashboard/afk Next.js client chunk):

1. Sign in via /api/auth/sign-in/email -> __Secure-better-auth.session_token.
2. GET /dashboard/afk -> the HTML references page-<hash>.js for the route.
3. GET that chunk -> extract the two server-action IDs:
   - armAfkWatchAction        : starts/refreshes a watch session, returns
                                {ok, token, remainingSeconds, durationSeconds,
                                 reward, endsAtMs}
   - claimAfkWatchRewardAction: POST ["<token>"] after the watch window,
                                returns {ok, balance, reward, nextToken,
                                remainingSeconds, endsAtMs}
4. Loop: arm -> sleep ~62s -> claim -> use nextToken for the next round.

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

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import solve_challenge  # noqa: E402

BASE = "https://mcworldhost.de"
AFK_PAGE = "/dashboard/afk"
CLAIM_INTERVAL = 62          # seconds between claims (60s window + jitter)
CLAIM_JITTER = 2
DEFAULT_MAX_MINUTES = 55     # matches the hourly GitHub Actions cadence
CHUNK_RE = re.compile(r"page-[0-9a-f]+\.js")
ACTION_RE = re.compile(r'createServerReference\)?\("([0-9a-f]{40,})"[^)]*"(\w+)"')


def log(msg: str) -> None:
    print(msg, flush=True)


def new_session(email: str, password: str, clearance: str = "", ua: str = ""):
    """Sign in over HTTP. The cf_clearance cookie (+ matching UA) satisfies the
    Cloudflare edge; the session then carries it for all later calls."""
    session = requests.Session()
    if ua:
        session.headers["User-Agent"] = ua
    if clearance:
        session.cookies.set("cf_clearance", clearance, domain="mcworldhost.de")
    else:
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        )
    try:
        response = session.post(
            f"{BASE}/api/auth/sign-in/email",
            json={"email": email, "password": password, "callbackURL": AFK_PAGE},
            headers={"Origin": BASE, "Referer": f"{BASE}/login"},
            timeout=25,
        )
        data = response.json()
    except Exception:
        log("sign-in request failed")
        return None
    if response.status_code != 200 or not data.get("user"):
        log(f"sign-in rejected (HTTP {response.status_code})")
        return None
    return session


def flight_payloads(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^[0-9a-f]+:(\{.*\})$", line.strip())
        if m:
            try:
                obj = json.loads(m.group(1))
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def resolve_action_ids(session: requests.Session) -> tuple[str, str] | None:
    """Fetch the AFK page and its route chunk to find the current action IDs."""
    resp = session.get(f"{BASE}{AFK_PAGE}", timeout=25)
    html = resp.text
    if "/login" in resp.url or "Sign in" in html[:4000]:
        log("session rejected by the site (login redirect).")
        return None
    match = CHUNK_RE.search(html)
    if not match:
        log("could not find the AFK route chunk in the page HTML")
        return None
    chunk_url = f"{BASE}/_next/static/chunks/app/(dashboard)/dashboard/afk/{match.group(0)}"
    js = session.get(chunk_url, timeout=25).text
    ids = {name: aid for aid, name in ACTION_RE.findall(js)}
    arm, claim = ids.get("armAfkWatchAction"), ids.get("claimAfkWatchRewardAction")
    if not arm or not claim:
        log("AFK server actions missing from the route chunk")
        return None
    return arm, claim


def call_action(session: requests.Session, action_id: str, args: list, page_url: str):
    response = session.post(
        page_url,
        json=args,
        headers={"Accept": "text/x-component", "Next-Action": action_id},
        timeout=25,
    )
    payloads = flight_payloads(response.text)
    return next((p for p in payloads if "ok" in p), None), response.status_code


def classify_failure(res: dict | None) -> str:
    if not res:
        return "unknown"
    if res.get("code"):
        return str(res["code"])
    error = str(res.get("error", ""))
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
    log("Solving Cloudflare challenge (headed Chromium on Xvfb)...", flush=True)
    try:
        clearance, ua = solve_challenge(BASE)
        log("Cloudflare challenge solved; clearance cookie obtained.")
    except Exception as exc:
        log(f"Cloudflare challenge failed: {exc}")
        return 1
    session = new_session(email, password, clearance, ua)
    if session is None:
        return 1
    ids = resolve_action_ids(session)
    if ids is None:
        log("session or page state rejected; aborting")
        return 1
    arm_id, claim_id = ids
    page_url = f"{BASE}{AFK_PAGE}"
    log("AFK actions resolved.")

    earned = 0
    balance = None
    armed = False
    token = None
    while time.time() < deadline:
        if not armed:
            try:
                res, status = call_action(session, arm_id, [], page_url)
            except requests.RequestException as exc:
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
                session = new_session(email, password, clearance, ua) or session
                time.sleep(20)
            continue

        time.sleep(CLAIM_INTERVAL + (CLAIM_JITTER * time.time() % 1))
        try:
            res, status = call_action(session, claim_id, [token], page_url)
        except requests.RequestException as exc:
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
        log(f"claim failed ({kind}); re-arming.")
        time.sleep(5)

    log(f"time budget ({max_minutes:.0f} min) exhausted; earned {earned} coins this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
