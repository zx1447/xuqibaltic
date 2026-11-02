#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LemonHost AFK credits auto-claim.

Endpoint: POST https://app.lemonhost.me/api/client/account/afk/claim

LemonHost runs the Pterodactyl client panel, so the client API accepts either
an account API key (Bearer) or a browser session cookie. We prefer the API key:
it does not expire as fast, is not tied to a browser fingerprint and completely
sidesteps the Cloudflare Turnstile challenge on the login page.

Environment variables
---------------------
LEMONHOST_API_KEY       Pterodactyl client API key (ptlc_...). Preferred.
LEMONHOST_SESSION_COOKIE  Optional fallback, raw Cookie header from a browser.
LEMONHOST_BASE_URL      Default https://app.lemonhost.me
RUN_MINUTES             How long this round keeps claiming (default 340).
TICK_SECONDS            Seconds between claim attempts (default 300).
LEMONHOST_SESSION_KEY   Fernet key used to encrypt cookies kept in state.
DRY_RUN                 "true" -> probe endpoints, never claim.
TG_BOT_TOKEN/TG_CHAT_ID Optional Telegram notifications.

Exit codes
----------
0  round finished (claims attempted, possibly limited)
1  configuration / auth problem -- needs a human
2  endpoint unreachable / server side error -- retry later
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("missing dependency, run: pip install requests")

BASE_URL = os.environ.get("LEMONHOST_BASE_URL", "https://app.lemonhost.me").rstrip("/")
CLAIM_URL = f"{BASE_URL}/api/client/account/afk/claim"
ACCOUNT_URL = f"{BASE_URL}/api/client/account"

API_KEY = os.environ.get("LEMONHOST_API_KEY", "").strip()
COOKIE_RAW = os.environ.get("LEMONHOST_SESSION_COOKIE", "").strip()
SESSION_KEY = os.environ.get("LEMONHOST_SESSION_KEY", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

RUN_MINUTES = max(1, min(345, int(os.environ.get("RUN_MINUTES", "340"))))
TICK_SECONDS = max(30, int(os.environ.get("TICK_SECONDS", "300")))

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

STATE_FILE = Path("lemo_state.json")
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 401/403 -> credentials are wrong, retrying cannot help.
FATAL_STATUS = {401, 403}
# 405 means the route exists but rejects our verb: a real bug, fail loudly.
FATAL_OTHER = {404, 405}


class LemonHostError(RuntimeError):
    pass


class AuthError(LemonHostError):
    pass


class ServerError(LemonHostError):
    pass


def now_text() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def notify(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as exc:  # notification must never break the run
        log(f"telegram notify failed: {exc}")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    state["updated_at"] = now_text()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    if API_KEY:
        session.headers["Authorization"] = f"Bearer {API_KEY}"
    elif COOKIE_RAW:
        session.headers["Cookie"] = COOKIE_RAW
    else:
        raise AuthError(
            "need LEMONHOST_API_KEY (preferred) or LEMONHOST_SESSION_COOKIE"
        )
    return session


def parse_body(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {"_raw": response.text[:300]}
    return data if isinstance(data, dict) else {"_data": data}


def describe_error(body: dict) -> str:
    """Pull Pterodactyl's error detail out of a response body."""
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("detail") or first.get("code") or first)
        return str(first)
    for key in ("detail", "message", "error", "_raw"):
        if body.get(key):
            return str(body[key])
    return json.dumps(body, ensure_ascii=False)[:200]


def verify_auth(session: requests.Session) -> dict:
    """Cheap authenticated GET so we fail fast on a bad key."""
    response = session.get(ACCOUNT_URL, timeout=30)
    body = parse_body(response)
    if response.status_code in FATAL_STATUS:
        raise AuthError(
            f"auth rejected ({response.status_code}): {describe_error(body)}"
        )
    if response.status_code >= 500:
        raise ServerError(f"account endpoint {response.status_code}")
    if response.ok:
        user = body.get("attributes") or body
        ident = user.get("username") or user.get("email") or "unknown"
        log(f"authenticated as {ident}")
        return {"username": ident}
    log(f"account endpoint returned {response.status_code} (continuing)")
    return {}


def claim_once(session: requests.Session) -> tuple[bool, str, dict]:
    """Fire one claim. Returns (credited, message, body)."""
    response = session.post(CLAIM_URL, json={}, timeout=30)
    body = parse_body(response)

    if response.status_code in FATAL_STATUS:
        raise AuthError(
            f"claim rejected ({response.status_code}): {describe_error(body)}"
        )
    if response.status_code in FATAL_OTHER:
        raise LemonHostError(
            f"claim endpoint looks wrong ({response.status_code}): "
            f"{describe_error(body)}"
        )
    if response.status_code == 429:
        return False, "rate limited", body
    if response.status_code >= 500:
        raise ServerError(f"claim {response.status_code}: {describe_error(body)}")
    return response.ok, describe_error(body) if not response.ok else "ok", body


def already_claimed_recently(state: dict) -> bool:
    """Skip duplicate claims inside the same minute window."""
    last = state.get("last_claim_timestamp")
    if not isinstance(last, int):
        return False
    return (time.time() - last) < (TICK_SECONDS - 10)


def main() -> int:
    log("=" * 60)
    log("LemonHost AFK claim starting")
    log(f"target      : {CLAIM_URL}")
    log(f"round       : {RUN_MINUTES} min, tick {TICK_SECONDS}s")
    mode = "API key" if API_KEY else ("session cookie" if COOKIE_RAW else "none")
    log(f"mode        : {mode}")
    if DRY_RUN:
        log("DRY_RUN     : enabled, no claim will be sent")
    log("=" * 60)

    state = load_state()

    try:
        session = make_session()
        info = verify_auth(session)
        if info.get("username"):
            state["username"] = info["username"]
    except AuthError as exc:
        log(f"FATAL auth: {exc}")
        state.update({"last_status": "auth_failed", "last_error": str(exc)})
        save_state(state)
        notify(f"LemonHost AFK: 认证失败\n{exc}")
        return 1
    except (ServerError, requests.RequestException) as exc:
        log(f"transient failure during auth check: {exc}")
        state.update({"last_status": "unreachable", "last_error": str(exc)})
        save_state(state)
        return 2

    if DRY_RUN:
        ok, message, _ = claim_once(session)
        log(f"dry run claim -> ok={ok} message={message}")
        save_state(state)
        return 0

    deadline = time.time() + RUN_MINUTES * 60
    claimed = 0
    errors = 0
    status = "ok"

    while True:
        try:
            if already_claimed_recently(state):
                log(f"claimed {TICK_SECONDS}s ago, waiting for the next window")
            else:
                ok, message, _ = claim_once(session)
                if ok:
                    claimed += 1
                    state["last_status"] = "claimed"
                    state["last_claim_timestamp"] = int(time.time())
                    state["last_claim_time"] = now_text()
                    state["total_claims"] = int(state.get("total_claims", 0)) + 1
                    log(f"claim #{claimed} credited")
                else:
                    state["last_status"] = message
                    log(f"claim not credited: {message}")
                save_state(state)
        except AuthError as exc:
            log(f"FATAL auth mid-run: {exc}")
            state.update({"last_status": "auth_failed", "last_error": str(exc)})
            save_state(state)
            notify(f"LemonHost AFK: 认证失效，需要更新 token\n{exc}")
            return 1
        except (ServerError, requests.RequestException) as exc:
            errors += 1
            log(f"transient error {errors}: {exc}")
            state["last_error"] = str(exc)
            status = "transient_error"
            # A short backoff, then keep trying until the round ends.
            if time.time() + 60 >= deadline:
                break
            time.sleep(60)
            continue

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(TICK_SECONDS, remaining))

    log(f"round finished: {claimed} credited claim(s), {errors} transient error(s)")
    notify(
        f"LemonHost AFK 本轮结束\n"
        f"成功领取 {claimed} 次\n"
        f"临时错误 {errors} 次\n"
        f"状态 {status}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
