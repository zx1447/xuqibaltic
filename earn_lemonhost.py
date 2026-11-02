#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LemonHost AFK credit auto-claim.

Verified against the live panel (see README for the probe transcript):

    POST https://app.lemonhost.me/api/client/account/afk/claim
    body: {"sessionId": "<any string>"}

    200 -> {"success":true,"credits":N,"afk_credits":N,"reward":1,
            "claimed_today":N,"daily_limit":1000}
    429 -> {"error":"AFK reward already claimed. Wait for the timer to complete."}
    400 -> {"error":"Session ID is required"}   (body key must be camelCase sessionId)

Measured cooldown is about 60 seconds and the reward is 1 credit per claim,
capped at daily_limit (1000) per account per day.

Auth: Pterodactyl client API key (ptlc_...) via Bearer. No browser session and
no Cloudflare Turnstile involved.

Environment
-----------
LEMONHOST_API_KEY        Pterodactyl client API key. Required.
LEMONHOST_BASE_URL       Default https://app.lemonhost.me
RUN_MINUTES              Round length in minutes (default 340).
TICK_SECONDS             Delay between claims (default 70, cooldown is ~60).
DRY_RUN                  "true" -> one probe claim only, then exit.
TG_BOT_TOKEN/TG_CHAT_ID  Optional Telegram notifications.

Exit codes
----------
0  round finished normally (may have hit the daily limit)
1  configuration / auth problem -- needs a human
2  endpoint unreachable / server error -- retry later
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
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
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

RUN_MINUTES = max(1, min(355, int(os.environ.get("RUN_MINUTES", "355"))))
TICK_SECONDS = max(30, int(os.environ.get("TICK_SECONDS", "65")))

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

STATE_FILE = Path("lemo_state.json")
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# The panel replies 429 while the timer is still running; that is not an error.
COOLDOWN_RETRY_SECONDS = 25

# If 429 persists this long the day is treated as capped, so the round stops
# instead of burning the remaining runner minutes for nothing.
CAP_STALL_MINUTES = int(os.environ.get("CAP_STALL_MINUTES", "30"))


class LemonHostError(RuntimeError):
    pass


class AuthError(LemonHostError):
    pass


class ConfigError(LemonHostError):
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
    except Exception as exc:
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


def session_id(state: dict) -> str:
    """Stable per-account session id, generated once and kept in state."""
    existing = state.get("session_id")
    if isinstance(existing, str) and existing:
        return existing
    fresh = f"afk-{uuid.uuid4().hex}"
    state["session_id"] = fresh
    return fresh


def make_session() -> requests.Session:
    if not API_KEY:
        raise AuthError("LEMONHOST_API_KEY is not set")
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return session


def parse_body(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {"_raw": response.text[:300]}
    return data if isinstance(data, dict) else {"_data": data}


def error_text(body: dict) -> str:
    for key in ("error", "detail", "message", "_raw"):
        if body.get(key):
            return str(body[key])
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("detail") or first.get("code") or first)
        return str(first)
    return json.dumps(body, ensure_ascii=False)[:200]


def fetch_account(session: requests.Session) -> dict:
    response = session.get(ACCOUNT_URL, timeout=30)
    body = parse_body(response)
    if response.status_code in (401, 403):
        raise AuthError(f"auth rejected ({response.status_code}): {error_text(body)}")
    if response.status_code >= 500:
        raise ServerError(f"account endpoint {response.status_code}")
    if not response.ok:
        raise ServerError(f"account endpoint {response.status_code}: {error_text(body)}")
    return body.get("attributes") or body


def claim(session: requests.Session, session_id_value: str) -> tuple[str, dict]:
    """Returns (outcome, body). outcome: credited | cooldown | limit | unknown."""
    response = session.post(
        CLAIM_URL, json={"sessionId": session_id_value}, timeout=30
    )
    body = parse_body(response)

    if response.status_code in (401, 403):
        raise AuthError(f"claim rejected ({response.status_code}): {error_text(body)}")
    if response.status_code == 400 and "session id" in error_text(body).lower():
        raise ConfigError(f"payload rejected: {error_text(body)}")
    if response.status_code == 429:
        return "cooldown", body
    if response.status_code >= 500:
        raise ServerError(f"claim {response.status_code}: {error_text(body)}")
    if response.ok and body.get("success"):
        if body.get("claimed_today") is not None and body.get("daily_limit") is not None:
            if int(body["claimed_today"]) >= int(body["daily_limit"]):
                return "limit", body
        return "credited", body
    return "unknown", body


def main() -> int:
    log("=" * 60)
    log("LemonHost AFK claim starting")
    log(f"target : {CLAIM_URL}")
    log(f"round  : {RUN_MINUTES} min, tick {TICK_SECONDS}s")
    if DRY_RUN:
        log("DRY_RUN: enabled")
    log("=" * 60)

    state = load_state()

    try:
        session = make_session()
        account = fetch_account(session)
        state["username"] = account.get("username", "unknown")
        log(
            f"authenticated as {state['username']} | "
            f"credits={account.get('credits')} afk={account.get('afk_credits')}"
        )
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

    sid = session_id(state)
    save_state(state)

    if DRY_RUN:
        outcome, body = claim(session, sid)
        log(f"dry run -> {outcome}: {error_text(body) if outcome != 'credited' else body}")
        save_state(state)
        return 0

    deadline = time.time() + RUN_MINUTES * 60
    credited = 0
    cooldowns = 0
    errors = 0
    daily = None
    cooldown_since = None  # first 429 of the current streak

    while True:
        try:
            outcome, body = claim(session, sid)

            if outcome == "credited":
                credited += 1
                cooldown_since = None
                daily = body.get("claimed_today")
                state.update(
                    {
                        "last_status": "credited",
                        "last_claim_time": now_text(),
                        "last_claim_timestamp": int(time.time()),
                        "total_claims": int(state.get("total_claims", 0)) + 1,
                        "credits": body.get("credits"),
                        "afk_credits": body.get("afk_credits"),
                        "claimed_today": daily,
                        "daily_limit": body.get("daily_limit"),
                    }
                )
                log(
                    f"credited x{credited} | credits={body.get('credits')} "
                    f"today={daily}/{body.get('daily_limit')}"
                )

            elif outcome == "cooldown":
                cooldowns += 1
                state["last_status"] = "cooldown"
                if cooldown_since is None:
                    cooldown_since = time.time()
                stalled = (time.time() - cooldown_since) / 60
                if stalled >= CAP_STALL_MINUTES:
                    state["last_status"] = "daily_cap_reached"
                    state["cap_detected_at"] = now_text()
                    log(
                        f"429 for {stalled:.0f} min straight -- treating the day as "
                        f"capped, stopping this round"
                    )
                    save_state(state)
                    notify(
                        f"LemonHost AFK: 冷却已持续 {stalled:.0f} 分钟，判定今日已满额，本轮停止\n"
                        f"今日已领 {daily}"
                    )
                    break
                log(f"cooldown, waiting {COOLDOWN_RETRY_SECONDS}s")
                save_state(state)
                remaining = deadline - time.time()
                if remaining <= COOLDOWN_RETRY_SECONDS:
                    break
                time.sleep(COOLDOWN_RETRY_SECONDS)
                continue

            elif outcome == "limit":
                daily = body.get("claimed_today")
                state["last_status"] = "daily_limit_reached"
                log(f"daily limit reached ({daily}/{body.get('daily_limit')}), stopping")
                save_state(state)
                break

            else:
                errors += 1
                state["last_status"] = "unknown_response"
                state["last_error"] = error_text(body)
                log(f"unexpected response: {error_text(body)}")

            save_state(state)

        except ConfigError as exc:
            log(f"FATAL config: {exc}")
            state.update({"last_status": "config_error", "last_error": str(exc)})
            save_state(state)
            notify(f"LemonHost AFK: 请求格式被拒绝\n{exc}")
            return 1
        except AuthError as exc:
            log(f"FATAL auth mid-run: {exc}")
            state.update({"last_status": "auth_failed", "last_error": str(exc)})
            save_state(state)
            notify(f"LemonHost AFK: 认证失效，需要更新 API Key\n{exc}")
            return 1
        except (ServerError, requests.RequestException) as exc:
            errors += 1
            state["last_error"] = str(exc)
            log(f"transient error {errors}: {exc}")
            if time.time() + 60 >= deadline:
                break
            time.sleep(60)
            continue

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(TICK_SECONDS, remaining))

    state["last_status"] = state.get("last_status") or "ok"
    save_state(state)
    log(f"round finished: {credited} claim(s), {cooldowns} cooldown(s), {errors} error(s)")
    notify(
        f"LemonHost AFK 本轮结束\n"
        f"成功领取 {credited} 次（+{credited} credits）\n"
        f"冷却等待 {cooldowns} 次，异常 {errors} 次\n"
        f"今日进度 {daily}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
