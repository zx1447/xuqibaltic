#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PortalMine login + server_state checker + auto coins.

Confirmed from F12 / JS:
- Login API: POST /api/auth.php?action=login
- Session check: GET /api/auth.php?action=me
- Server state: GET /server-v16132.php?action=server_state
- Coins (ad): GET /api/coins.php?action=ad_begin -> returns {ok, ad_id, duration, ...}
             GET /api/coins.php?action=ad_claim -> returns {ok, coins, total, ...}

Auto coins flow (when PORTALMINE_AUTO_COINS=1):
  1. login
  2. GET /api/coins.php?action=ad_begin (start ad session)
  3. sleep duration (default 60s, from response or PORTALMINE_AD_SECONDS)
  4. GET /api/coins.php?action=ad_claim (claim reward)
  5. record coins delta to state

Note: dashboard has a 60-second "Earn coins" reward button.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from http.cookies import SimpleCookie

import requests

BASE = "https://portalmine.com"
LOGIN_URL = f"{BASE}/api/auth.php?action=login"
ME_URL = f"{BASE}/api/auth.php?action=me"
STATE_URL = f"{BASE}/server-v16132.php?action=server_state"
COINS_API = f"{BASE}/api/coins.php"
STATE_FILE = "portalmine_state.json"
USER = os.environ.get("PORTALMINE_USER", "").strip()
PASSWORD = os.environ.get("PORTALMINE_PASS", "").strip()
COOKIE = os.environ.get("PORTALMINE_COOKIE", "").strip()
POLL_COUNT = int(os.environ.get("PORTALMINE_POLL_COUNT", "1") or "1")
POLL_SECONDS = int(os.environ.get("PORTALMINE_POLL_SECONDS", "60") or "60")
# Auto coins config
AUTO_COINS = os.environ.get("PORTALMINE_AUTO_COINS", "").strip().lower() in {"1", "true", "yes", "on"}
AD_SECONDS = int(os.environ.get("PORTALMINE_AD_SECONDS", "60") or "60")
AD_MAX_ROUNDS = int(os.environ.get("PORTALMINE_AD_MAX_ROUNDS", "1") or "1")
# Run forever (until workflow timeout). Set PORTALMINE_AD_FOREVER=1 to enable.
# When enabled, ignores AD_MAX_ROUNDS and keeps claiming until the process is killed.
AD_FOREVER = os.environ.get("PORTALMINE_AD_FOREVER", "").strip().lower() in {"1", "true", "yes", "on"}
# Max runtime in seconds (safety cap, default 5h to fit GitHub Actions 6h timeout with buffer)
AD_MAX_RUNTIME_SECONDS = int(os.environ.get("PORTALMINE_AD_MAX_RUNTIME", "18000") or "18000")
# PortalMine server id (from dashboard F12: x-portalmine-server-id header)
SERVER_ID = os.environ.get("PORTALMINE_SERVER_ID", "").strip()
# Reward zone (from dashboard-v16132.js: PM_REWARDED_ZONE_NAME)
REWARD_ZONE = "portalmine_idle_reward_v10900"
REWARD_PROVIDER = "internal"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

class PortalMineError(RuntimeError):
    pass

def log(msg: str) -> None:
    print(msg, flush=True)

def now_cn() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def send_tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception:
        pass

def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"})
    if COOKIE:
        c = SimpleCookie()
        try:
            c.load(COOKIE)
            for k, m in c.items():
                if k.lower() not in {"path", "expires", "max-age", "httponly", "samesite"}:
                    s.cookies.set(k, m.value, domain="portalmine.com", path="/")
        except Exception:
            for item in COOKIE.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    if k.lower() not in {"path", "expires", "max-age", "httponly", "samesite"}:
                        s.cookies.set(k, v, domain="portalmine.com", path="/")
    return s

def parse_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}

def check_me(s: requests.Session) -> dict:
    r = s.get(ME_URL, timeout=20)
    data = parse_json(r)
    log(f"📡 GET auth me -> HTTP {r.status_code}; ok={data.get('ok')}")
    return data

def login(s: requests.Session) -> dict:
    if not USER or not PASSWORD:
        raise PortalMineError("缺少 PORTALMINE_USER/PORTALMINE_PASS")
    r = s.post(
        LOGIN_URL,
        data={"login_input": USER, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE, "Referer": BASE + "/"},
        timeout=30,
    )
    data = parse_json(r)
    log(f"📡 POST auth login -> HTTP {r.status_code}; ok={data.get('ok')}; remembered={data.get('remembered')}")
    if r.status_code != 200 or not data.get("ok"):
        raise PortalMineError(f"登录失败：{str(data)[:300]}")
    user = data.get("user") or {}
    log(f"✅ PortalMine 登录成功：user={user.get('username')} coins={user.get('coins')}")
    return data

def get_server_state(s: requests.Session) -> dict:
    r = s.get(STATE_URL, timeout=25)
    data = parse_json(r)
    log(f"📡 GET server_state -> HTTP {r.status_code}; ok={data.get('ok')}; state={data.get('state') or data.get('current_state')}")
    if r.status_code != 200 or not data.get("ok"):
        raise PortalMineError(f"server_state 失败：{str(data)[:300]}")
    return data

def coins_api(s: requests.Session, action: str, body: dict | None = None, timeout: int = 20) -> dict:
    """Call /api/coins.php?action=<action>.
    If body is provided -> POST with JSON body; otherwise GET.
    Adds x-portalmine-server-id header (required for ad_begin/ad_claim).
    """
    ts = int(time.time() * 1000)
    url = f"{COINS_API}?action={action}&_={ts}"
    headers = {
        "Referer": f"{BASE}/dashboard.html",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    if SERVER_ID:
        headers["x-portalmine-server-id"] = SERVER_ID
    if body is not None:
        headers["Content-Type"] = "application/json"
        r = s.post(url, json=body, timeout=timeout, headers=headers)
    else:
        r = s.get(url, timeout=timeout, headers=headers)
    data = parse_json(r)
    return data

def coins_ad_begin(s: requests.Session) -> dict:
    """Start an ad session.
    POST /api/coins.php?action=ad_begin
    body: {zone, provider, required_steps}
    Returns {ok, token, seconds, reward, step_seconds, required_steps, eligible_at, ...}
    """
    body = {
        "zone": REWARD_ZONE,
        "provider": REWARD_PROVIDER,
        "required_steps": 1,
    }
    data = coins_api(s, "ad_begin", body=body)
    log(f"📡 coins ad_begin -> ok={data.get('ok')}; token={str(data.get('token',''))[:16]}...; seconds={data.get('seconds')}; reward={data.get('reward')}; full={json.dumps(data, ensure_ascii=False)[:300]}")
    return data

def coins_ad_claim(s: requests.Session, token: str) -> dict:
    """Claim the ad reward.
    POST /api/coins.php?action=ad_claim
    body: {token, completed_steps, required_steps, provider}
    Returns {ok, coins, reward, ...} or {ok:false,error}
    """
    body = {
        "token": token,
        "completed_steps": 1,
        "required_steps": 1,
        "provider": REWARD_PROVIDER,
    }
    data = coins_api(s, "ad_claim", body=body)
    log(f"📡 coins ad_claim -> ok={data.get('ok')}; coins={data.get('coins')}; reward={data.get('reward')}; error={data.get('error')}; full={json.dumps(data, ensure_ascii=False)[:300]}")
    return data

def run_auto_coins(s: requests.Session, state: dict) -> dict:
    """Run ad_begin -> wait -> ad_claim loop. Returns summary dict.
    If AD_FOREVER is set, loops until AD_MAX_RUNTIME_SECONDS (default 5h) or killed.
    Otherwise loops AD_MAX_ROUNDS times.
    """
    summary = {"rounds_attempted": 0, "rounds_ok": 0, "coins_earned": 0, "errors": [], "last_claim": None, "started_at": int(time.time()), "stopped_reason": None}
    start_ts = time.time()
    if AD_FOREVER:
        max_rounds = 999999  # effectively unlimited
        log(f"💰 AD_FOREVER 启用：将循环领取金币直到超时 (max_runtime={AD_MAX_RUNTIME_SECONDS}s)")
    else:
        max_rounds = max(1, min(AD_MAX_ROUNDS, 100))
        log(f"💰 将跑 {max_rounds} 轮金币领取")
    i = 0
    while True:
        # check runtime cap
        if AD_FOREVER:
            elapsed_total = time.time() - start_ts
            if elapsed_total >= AD_MAX_RUNTIME_SECONDS:
                summary["stopped_reason"] = f"max_runtime {AD_MAX_RUNTIME_SECONDS}s reached"
                log(f"⏹️ 达到最大运行时间 {AD_MAX_RUNTIME_SECONDS}s，停止")
                break
            remaining = AD_MAX_RUNTIME_SECONDS - elapsed_total
            # if less than 90s left (not enough for one more round), stop
            if remaining < 90:
                summary["stopped_reason"] = f"only {int(remaining)}s left, not enough for another round"
                log(f"⏹️ 剩余 {int(remaining)}s 不足以再跑一轮，停止")
                break
        else:
            if i >= max_rounds:
                summary["stopped_reason"] = f"max_rounds {max_rounds} reached"
                break
        i += 1
        summary["rounds_attempted"] += 1
        if AD_FOREVER:
            log(f"💰 Auto coins round {i} (elapsed {int(time.time()-start_ts)}s): ad_begin")
        else:
            log(f"💰 Auto coins round {i}/{max_rounds}: ad_begin")
        begin = coins_ad_begin(s)
        if not begin.get("ok"):
            err = begin.get("error") or begin.get("msg") or str(begin)[:200]
            summary["errors"].append(f"ad_begin: {err}")
            log(f"⚠️ ad_begin failed: {err}")
            # common errors: COOLDOWN, ALREADY_ACTIVE, RATE_LIMIT -> wait and retry
            err_up = str(err).upper()
            if any(k in err_up for k in ("COOLDOWN", "ALREADY", "RATE", "LIMIT", "NO_AD", "EXHAUSTED")):
                if AD_FOREVER:
                    log(f"⏳ cooldown/rate_limit, waiting 60s before retry...")
                    time.sleep(60)
                    continue
                else:
                    break
            # transient error: small backoff then retry
            time.sleep(5)
            continue
        # duration from response or fallback to AD_SECONDS
        duration = begin.get("seconds") or begin.get("step_seconds") or begin.get("duration") or AD_SECONDS
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = AD_SECONDS
        duration = max(5, min(duration, 300))  # clamp 5-300s
        # token from ad_begin response (needed for ad_claim)
        token = begin.get("token") or begin.get("ad_token") or begin.get("session_token")
        log(f"⏳ Ad session started (token={str(token)[:16] if token else 'None'}...), waiting {duration}s...")
        # wait the full duration (server tracks eligible_at; no heartbeat needed)
        time.sleep(duration + 2)  # +2s buffer to ensure eligible_at passed
        log(f"💰 Auto coins round {i}: ad_claim")
        claim = coins_ad_claim(s, token=token)
        if not claim.get("ok"):
            err = claim.get("error") or claim.get("msg") or str(claim)[:200]
            summary["errors"].append(f"ad_claim: {err}")
            log(f"⚠️ ad_claim failed: {err}")
            continue
        earned = claim.get("reward") or claim.get("coins_earned") or claim.get("amount") or 0
        try:
            earned = int(earned)
        except (TypeError, ValueError):
            earned = 0
        summary["rounds_ok"] += 1
        summary["coins_earned"] += earned
        summary["last_claim"] = claim
        total_coins = claim.get("coins") or claim.get("coins_total") or "?"
        log(f"✅ ad_claim ok: +{earned} coins (total={total_coins}, rounds_ok={summary['rounds_ok']}, earned={summary['coins_earned']})")
        # save state after each successful claim (so progress is preserved if killed)
        state["auto_coins"] = summary
        state["last_auto_coins_time"] = now_cn()
        try:
            save_state(state)
        except Exception:
            pass
        # small gap between rounds
        if not AD_FOREVER and i < max_rounds:
            time.sleep(3)
        elif AD_FOREVER:
            time.sleep(2)  # smaller gap in forever mode
    state["auto_coins"] = summary
    state["last_auto_coins_time"] = now_cn()
    summary["duration_seconds"] = int(time.time() - start_ts)
    return summary

def summarize_state(data: dict) -> dict:
    keys = [
        "state", "current_state", "state_label", "install_status", "name", "version", "type", "platform",
        "display_address", "account_level", "slots", "limit_memory_mb", "limit_disk_mb", "limit_cpu",
        "cycle_left", "uptime", "cpu", "memory", "disk", "players_online", "coins",
    ]
    return {k: data.get(k) for k in keys if k in data}

def main() -> int:
    log("🚀 PortalMine server_state 检测启动")
    log(f"🕐 北京时间：{now_cn()}")
    state = load_state()
    try:
        s = make_session()
        me = check_me(s)
        if not me.get("ok"):
            login(s)
            me = check_me(s)
        if not me.get("ok"):
            raise PortalMineError("登录后 auth me 仍未授权")

        poll_count = max(1, min(POLL_COUNT, 10))
        last = None
        for i in range(poll_count):
            if i:
                log(f"⏳ 等待 {POLL_SECONDS} 秒后再次检查 server_state ({i+1}/{poll_count})")
                time.sleep(max(1, min(POLL_SECONDS, 300)))
            last = get_server_state(s)

        # Auto coins (ad_begin -> wait -> ad_claim)
        coins_summary = None
        if AUTO_COINS:
            log(f"💰 AUTO_COINS 启用，开始自动领取金币 (max_rounds={AD_MAX_ROUNDS})")
            try:
                coins_summary = run_auto_coins(s, state)
            except Exception as e:
                log(f"⚠️ 自动金币异常: {type(e).__name__}: {e}")
                state["auto_coins_error"] = f"{type(e).__name__}: {e}"
        else:
            log("💡 提示：设置 PORTALMINE_AUTO_COINS=1 可自动领取金币 (ad_begin/ad_claim)")

        summary = summarize_state(last or {})
        state.update({
            "last_check_timestamp": int(time.time()),
            "last_check_time": now_cn(),
            "last_status": "success",
            "server_state": summary,
            "poll_count": poll_count,
        })
        save_state(state)
        tg_msg = f"✅ PortalMine 检测成功\n🕐 {now_cn()}\n📌 {summary.get('state_label') or summary.get('state')}\n🖥️ {summary.get('display_address', '')}"
        if coins_summary:
            tg_msg += f"\n💰 金币: +{coins_summary['coins_earned']} ({coins_summary['rounds_ok']}/{coins_summary['rounds_attempted']} 轮)"
            if coins_summary.get("errors"):
                tg_msg += f"\n⚠️ 错误: {coins_summary['errors'][-1]}"
        send_tg(tg_msg)
        log("✅ PortalMine 检测完成：" + json.dumps(summary, ensure_ascii=False))
        if coins_summary:
            log(f"💰 自动金币: +{coins_summary['coins_earned']} ({coins_summary['rounds_ok']}/{coins_summary['rounds_attempted']} 轮)")
        return 0
    except Exception as exc:
        log(f"❌ PortalMine 失败：{type(exc).__name__}: {exc}")
        state.update({"last_check_time": now_cn(), "last_status": f"ERROR: {exc}"})
        save_state(state)
        send_tg(f"❌ PortalMine 失败\n{exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
