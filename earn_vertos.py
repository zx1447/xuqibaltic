#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vertos Hub AFK auto-earn.

Protocol reverse engineered from the panel bundle (AfkPage-*.js) and verified
against the live API:

    POST /api/auth/login      {"identifier": "<username>", "password": "..."}
      200 -> data.user; sets cookie vertos_hub_session (14 days)
      NOTE: identifier is the USERNAME, not the email address.
            The account's email zxy1715x@gmail.com logs in as username "anna".

    POST /api/afk/start       (no body)
      201 -> {"sessionId","sessionToken","expiresAt","startedAt","status"}
      409 -> SESSION_ALREADY_ACTIVE if another session is running

    POST /api/afk/heartbeat   {"sessionId": "...", "token": "..."}
      200 -> {"sessionSeconds","todaySeconds","todayCredits","todayEarnedCredits",
              "pendingFractionalCredits","lifetimeSeconds","lifetimeCredits",
              "fractionalSeconds","progressPercent","creditsAwarded","expiresAt"}
      403 -> INVALID_TOKEN
      410 -> SESSION_EXPIRED   (lease lapsed, must restart)

    POST /api/afk/stop        {"sessionId": "...", "token": "..."}  (on exit)

    GET  /api/afk/status      -> activeSession + progress
    GET  /api/credits         -> wallet balance

Measured behaviour
------------------
* lease is 90 s with a 60 s sliding window refreshed by each heartbeat,
  so a heartbeat every 30 s is the safe cadence (same as the web client)
* earning rate: 1 credit per 50 minutes, i.e. 0.02 credits/min
* creditsAwarded becomes non-zero only when a whole credit is minted

Environment
-----------
VERTOS_IDENTIFIER   username used to sign in (NOT the email)
VERTOS_PASSWORD     password
VERTOS_SESSION_COOKIE   optional: reuse an existing vertos_hub_session cookie
RUN_MINUTES         round length, default 355
HEARTBEAT_SECONDS   default 30
STOP_AFTER_CREDITS  optional: stop the round once todayCredits reaches this
TG_BOT_TOKEN/TG_CHAT_ID  optional notifications

Exit codes: 0 ok, 1 auth/config problem, 2 server unreachable
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

BASE = os.environ.get("VERTOS_BASE_URL", "https://hub.vertos.in").rstrip("/")
LOGIN_URL = f"{BASE}/api/auth/login"
ME_URL = f"{BASE}/api/auth/me"
START_URL = f"{BASE}/api/afk/start"
HEARTBEAT_URL = f"{BASE}/api/afk/heartbeat"
STOP_URL = f"{BASE}/api/afk/stop"
STATUS_URL = f"{BASE}/api/afk/status"
CREDITS_URL = f"{BASE}/api/credits"

IDENTIFIER = os.environ.get("VERTOS_IDENTIFIER", "").strip()
PASSWORD = os.environ.get("VERTOS_PASSWORD", "").strip()
COOKIE = os.environ.get("VERTOS_SESSION_COOKIE", "").strip()

RUN_MINUTES = max(1, min(355, int(os.environ.get("RUN_MINUTES", "355"))))
HEARTBEAT_SECONDS = max(15, int(os.environ.get("HEARTBEAT_SECONDS", "30")))
STOP_AFTER_CREDITS = int(os.environ.get("STOP_AFTER_CREDITS", "0") or 0)

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

STATE_FILE = Path("vertos_state.json")
CN_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class VertosError(RuntimeError):
    pass


class AuthError(VertosError):
    pass


class ServerError(VertosError):
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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/earn/afk",
        }
    )
    if COOKIE:
        session.headers["Cookie"] = COOKIE
    return session


def body_of(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {"_raw": response.text[:300]}
    return data if isinstance(data, dict) else {"_data": data}


def err(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return f"{error.get('code')}: {error.get('message')}"
    if error:
        return str(error)
    return body.get("_raw", "")[:200] or json.dumps(body, ensure_ascii=False)[:200]


def login(session: requests.Session, state: dict) -> dict:
    if COOKIE:
        response = session.get(ME_URL, timeout=30)
        body = body_of(response)
        if response.ok and body.get("success"):
            user = (body.get("data") or {}).get("user") or {}
            log(f"reusing provided cookie, user={user.get('username')}")
            return user
        log("provided cookie rejected, falling back to password login")

    if not (IDENTIFIER and PASSWORD):
        raise AuthError("VERTOS_IDENTIFIER / VERTOS_PASSWORD are not set")

    response = session.post(
        LOGIN_URL,
        json={"identifier": IDENTIFIER, "password": PASSWORD},
        timeout=30,
    )
    body = body_of(response)
    if response.status_code in (401, 403):
        raise AuthError(f"login rejected: {err(body)}")
    if response.status_code >= 500:
        raise ServerError(f"login {response.status_code}")
    if not response.ok or not body.get("success"):
        raise AuthError(f"login failed: {err(body)}")

    user = (body.get("data") or {}).get("user") or {}
    state["user_id"] = user.get("id")
    state["username"] = user.get("username")
    state["email"] = user.get("email")
    log(f"logged in as {user.get('username')} (id={user.get('id')})")
    return user


def start_session(session: requests.Session, state: dict) -> tuple[str, str]:
    """Always obtain a FRESH session.

    /api/afk/status exposes activeSession but WITHOUT sessionToken, so a
    reported active session cannot be resumed. Starting a new one is the only
    reliable path; the previous lease simply lapses (90 s).
    """
    last_error = ""
    for attempt in range(1, 5):
        response = session.post(START_URL, timeout=30)
        body = body_of(response)
        data = body.get("data") or {}

        if response.status_code in (401, 403):
            raise AuthError(f"start rejected: {err(body)}")
        if response.status_code >= 500:
            raise ServerError(f"start {response.status_code}")

        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else ""
        if response.status_code == 409 or code == "SESSION_ALREADY_ACTIVE":
            last_error = err(body)
            log(f"session already active (attempt {attempt}/4)")
            released = release_known_sessions(session, state)
            if released:
                log("released a stale session of ours, retrying immediately")
                time.sleep(3)
                continue
            log("no releasable session of ours; waiting 25s for the lease to lapse")
            time.sleep(25)
            continue

        sid, tok = data.get("sessionId"), data.get("sessionToken")
        if response.ok and body.get("success") and sid and tok:
            log(f"AFK session started, lease expires {data.get('expiresAt')}")
            # remember it so a later run (or a stale CI job) can release it
            prev = state.get("pending_session") or {}
            state["pending_session"] = {
                "sessionId": sid,
                "sessionToken": tok,
                "startedAt": data.get("startedAt"),
                "expiresAt": data.get("expiresAt"),
                "saved_at": now_text(),
                "prev": prev or None,
            }
            save_state(state)
            return sid, tok

        last_error = err(body) or f"no credentials in response: {body}"
        log(f"start attempt {attempt}/4 unusable: {last_error}")
        time.sleep(10)

    raise VertosError(f"could not start a session: {last_error}")


def fetch_status(session: requests.Session) -> dict | None:
    try:
        response = session.get(STATUS_URL, timeout=30)
        body = body_of(response)
        if response.ok and body.get("success"):
            return body.get("data") or {}
    except requests.RequestException:
        pass
    return None


def release_known_sessions(session: requests.Session, state: dict) -> bool:
    """Best-effort stop of sessions this script created but could not finish.

    A CI job that is killed leaves its AFK lease behind for 90 s; if a job even
    dies between start and stop, the next run would otherwise loop on 409.
    """
    released = False
    pending = state.get("pending_session")
    candidates = []
    while isinstance(pending, dict):
        candidates.append(pending)
        pending = pending.get("prev")
    for entry in candidates:
        sid, tok = entry.get("sessionId"), entry.get("sessionToken")
        if not (sid and tok):
            continue
        try:
            response = session.post(
                STOP_URL, json={"sessionId": sid, "token": tok}, timeout=20
            )
            if response.ok:
                log(f"released stale session {sid[:8]}...")
                released = True
        except requests.RequestException:
            continue
    if released:
        state.pop("pending_session", None)
        save_state(state)
    return released


def heartbeat(
    session: requests.Session, sid: str, tok: str
) -> tuple[str, dict]:
    """Returns (outcome, data). outcome: ok | expired | auth."""
    response = session.post(
        HEARTBEAT_URL, json={"sessionId": sid, "token": tok}, timeout=30
    )
    body = body_of(response)

    if response.status_code in (401, 403):
        code = ""
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or ""
        if code == "INVALID_TOKEN":
            return "expired", body
        raise AuthError(f"heartbeat auth: {err(body)}")
    if response.status_code == 410:
        return "expired", body
    if response.status_code >= 500:
        raise ServerError(f"heartbeat {response.status_code}: {err(body)}")
    if not response.ok or not body.get("success"):
        return "expired", body
    return "ok", body.get("data") or {}


def stop_session(
    session: requests.Session, sid: str, tok: str, state: dict | None = None
) -> None:
    try:
        session.post(STOP_URL, json={"sessionId": sid, "token": tok}, timeout=20)
        log("AFK session stopped cleanly")
        if state is not None:
            state.pop("pending_session", None)
            save_state(state)
    except requests.RequestException as exc:
        log(f"stop failed (ignored): {exc}")


def wallet_credits(session: requests.Session) -> float | None:
    try:
        response = session.get(CREDITS_URL, timeout=30)
        body = body_of(response)
        if response.ok and body.get("success"):
            coins = ((body.get("data") or {}).get("coins") or {})
            if coins.get("balance") is not None:
                return float(coins["balance"])
    except (requests.RequestException, TypeError, ValueError):
        pass
    return None


def main() -> int:
    log("=" * 60)
    log("Vertos Hub AFK starting")
    log(f"target : {HEARTBEAT_URL}")
    log(f"round  : {RUN_MINUTES} min, heartbeat {HEARTBEAT_SECONDS}s")
    if STOP_AFTER_CREDITS:
        log(f"stop   : when todayCredits >= {STOP_AFTER_CREDITS}")
    log("=" * 60)

    state = load_state()

    try:
        session = make_session()
        login(session, state)
        save_state(state)
    except AuthError as exc:
        log(f"FATAL auth: {exc}")
        state.update({"last_status": "auth_failed", "last_error": str(exc)})
        save_state(state)
        notify(f"Vertos AFK: 登录失败\n{exc}")
        return 1
    except (ServerError, requests.RequestException) as exc:
        log(f"transient failure during login: {exc}")
        state.update({"last_status": "unreachable", "last_error": str(exc)})
        save_state(state)
        return 2

    deadline = time.time() + RUN_MINUTES * 60
    beats = 0
    restarts = 0
    errors = 0
    stopped_reason = "time"

    try:
        while time.time() < deadline:
            try:
                sid, tok = start_session(session, state)
                state["session_id"] = sid
                save_state(state)

                # Heartbeat loop for as long as this lease survives.
                while time.time() < deadline:
                    outcome, data = heartbeat(session, sid, tok)

                    if outcome == "expired":
                        restarts += 1
                        log(f"session expired, restarting (restart #{restarts})")
                        state["last_status"] = "restarted"
                        state["restarts"] = restarts
                        save_state(state)
                        time.sleep(5)
                        break

                    beats += 1
                    if beats % 20 == 0 or data.get("creditsAwarded"):
                        log(
                            f"beat {beats} | session={data.get('sessionSeconds')}s "
                            f"today={data.get('todaySeconds')}s "
                            f"credits={data.get('todayCredits')} "
                            f"({data.get('todayEarnedCredits')}) "
                            f"awarded={data.get('creditsAwarded')}"
                        )

                    if data.get("creditsAwarded"):
                        balance = wallet_credits(session)
                        if balance is not None:
                            log(f"wallet balance now {balance}")
                            state["wallet_balance"] = balance

                    state.update(
                        {
                            "last_status": "active",
                            "session_seconds": data.get("sessionSeconds"),
                            "today_seconds": data.get("todaySeconds"),
                            "today_credits": data.get("todayCredits"),
                            "today_earned": data.get("todayEarnedCredits"),
                            "lifetime_seconds": data.get("lifetimeSeconds"),
                            "lifetime_credits": data.get("lifetimeCredits"),
                            "seconds_to_next_credit": data.get("secondsToNextCredit"),
                            "total_beats": int(state.get("total_beats", 0)) + 1,
                        }
                    )

                    if (
                        STOP_AFTER_CREDITS
                        and (data.get("todayCredits") or 0) >= STOP_AFTER_CREDITS
                    ):
                        log(
                            f"todayCredits reached {data.get('todayCredits')}, "
                            f"stopping as configured"
                        )
                        stopped_reason = "credit_target"
                        save_state(state)
                        notify(
                            f"Vertos AFK: 已达目标 {data.get('todayCredits')} credits，本轮停止"
                        )
                        stop_session(session, sid, tok, state)
                        save_state(state)
                        return 0

                    save_state(state)
                    time.sleep(HEARTBEAT_SECONDS)

                else:
                    # inner loop ended because the deadline passed, not a restart
                    stop_session(session, sid, tok, state)
                    break

            except AuthError as exc:
                log(f"FATAL auth mid-run: {exc}")
                state.update({"last_status": "auth_failed", "last_error": str(exc)})
                save_state(state)
                notify(f"Vertos AFK: 登录失效\n{exc}")
                return 1
            except (ServerError, VertosError, requests.RequestException) as exc:
                errors += 1
                state["last_error"] = str(exc)
                log(f"transient error {errors}: {exc}")
                if time.time() + 60 >= deadline:
                    stopped_reason = "errors"
                    break
                time.sleep(60)

    except KeyboardInterrupt:
        log("interrupted")

    final = fetch_status(session) or {}
    progress = final.get("progress") or {}
    state["last_status"] = "finished"
    state["finished_reason"] = stopped_reason
    save_state(state)

    log(
        f"round finished ({stopped_reason}): {beats} beats, "
        f"{restarts} restarts, {errors} errors"
    )
    log(
        f"progress: today={progress.get('todaySeconds')}s "
        f"credits={progress.get('todayCredits')} "
        f"earned={progress.get('todayEarnedCredits')} "
        f"lifetime={progress.get('lifetimeCredits')}"
    )
    notify(
        f"Vertos AFK 本轮结束（{stopped_reason}）\n"
        f"心跳 {beats} 次，会话重建 {restarts} 次，异常 {errors} 次\n"
        f"今日 {progress.get('todaySeconds')}s / {progress.get('todayCredits')} credits"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
