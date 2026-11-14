#!/usr/bin/env python3
"""L3chrane X Bots —— AFK 挂机赚币（l3chranexbots.oriaxen.com）

站点机制（2026-11-13 实测）
────────────────────────────────────────────────────────────────
· 托管平台：Discord bot 托管（Node.js / Python），免费档 512MB / 50% CPU / 600MB
· 货币：coin。部署一台服务器需 5 coins
· 官方 AFK：打开 /afk 页面，每 60 秒自动 +1 coin
  - 前端计时器**纯内存**（刷新即重置 60s），离开页面暂停
· 接口：POST /afk/claim  → {success, coins, newBalance, dailyEarned}
  ⚠️ 实测：**服务端不做任何间隔校验**——连续 POST 每次都 +1
     （1 秒内连刷 10 次全部成功，dailyEarned 无上限，maxDaily=0）
· 登录：Discord OAuth（scope=identify）→ Express session cookie `connect.sid`

本脚本
────────────────────────────────────────────────────────────────
1. 用 Discord 用户 token 走 OAuth 授权，换站点 connect.sid 会话
2. 循环 POST /afk/claim 攒币
3. 会话失效自动重登；状态写 l3chrane_state.json（CI 回写）

环境变量
  L3CH_DISCORD_TOKEN  Discord 用户 token（必填）
  L3CH_GOAL           目标余额（默认 50；够部署 10 台就停）
  L3CH_DELAY          ﻿每次 claim 间隔秒（默认 2.0，做个正常节奏免得被风控）
  L3CH_BASE           默认 https://l3chranexbots.oriaxen.com

用法
  python3 l3chrane_afk.py            # 攒到 goal 或会话/网络失败
  python3 l3chrane_afk.py --once     # 只打一次（连通性自检）
  python3 l3chrane_afk.py --status   # 只查余额
"""

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("L3CH_BASE", "https://l3chranexbots.oriaxen.com").rstrip("/")
DISCORD_TOKEN = os.environ.get("L3CH_DISCORD_TOKEN", "").strip()
GOAL = int(os.environ.get("L3CH_GOAL", "50"))
DELAY = float(os.environ.get("L3CH_DELAY", "2.0"))

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l3chrane_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

# 站点 OAuth client（从 /auth/discord 的 302 里解出来的公开值）
CLIENT_ID = "1519415022168571914"
REDIRECT = f"{BASE}/auth/discord/callback"


def log(m):
    print(m, flush=True)


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


class Site:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ── 基础请求 ──────────────────────────────────────────
    def req(self, path, method="GET", body=None, form=None, headers=None, retries=4, allow_redirect=True):
        url = path if path.startswith("http") else BASE + path
        last = None
        for attempt in range(retries):
            data = None
            h = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
            if headers:
                h.update(headers)
            if form is not None:
                data = urllib.parse.urlencode(form).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            elif body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            r = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with self.op.open(r, timeout=45) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return resp.status, raw, dict(resp.headers)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                return e.code, raw, dict(e.headers)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    time.sleep(4 * (attempt + 1))
        return 0, json.dumps({"error": last or "unreachable"}), {}

    def cookies(self):
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    # ── 登录：Discord OAuth ───────────────────────────────
    def login(self):
        """用 Discord 用户 token 拿 OAuth code → 换站点 session"""
        log("🔑 走 Discord OAuth 登录…")
        auth = {"Authorization": DISCORD_TOKEN}
        # 1) 问 Discord 这个应用是否已授权（顺便确认 token 有效）
        q = urllib.parse.urlencode({
            "client_id": CLIENT_ID, "scope": "identify",
            "response_type": "code", "redirect_uri": REDIRECT,
        })
        st, raw, _ = self.req(f"https://discord.com/api/oauth2/authorize?{q}", headers=auth)
        if st == 401:
            log("❌ Discord token 失效")
            return False
        # 2) 提交授权，拿回跳地址里的 code
        st, raw, _ = self.req(
            f"https://discord.com/api/v9/oauth2/authorize?{q}",
            method="POST", headers={**auth, "Content-Type": "application/json"},
            body={"permissions": "0", "authorize": True, "captcha_key": None,
                  "integration_type": 0, "guild_id": None},
        )
        try:
            loc = json.loads(raw).get("location", "")
        except Exception:  # noqa: BLE001
            log(f"❌ 授权失败 HTTP {st}: {raw[:200]}")
            return False
        m = re.search(r"[?&]code=([A-Za-z0-9]+)", loc)
        if not m:
            log(f"❌ 未拿到 OAuth code（返回：{raw[:200]}）")
            return False
        # 3) 回调换 session cookie
        st, raw, _ = self.req(f"/auth/discord/callback?code={m.group(1)}&iss=https%3A%2F%2Fdiscord.com")
        ck = [c for c in self.jar if c.name == "connect.sid"]
        if not ck:
            log(f"❌ 回调未拿到 session（HTTP {st}）")
            return False
        log("✅ 登录成功（connect.sid 已取）")
        return True

    def seed(self, sid):
        import http.cookiejar as cj
        host = urllib.parse.urlparse(BASE).hostname
        self.jar.set_cookie(cj.Cookie(
            0, "connect.sid", sid, None, False, host, True, False,
            "/", True, True, None, False, None, None, {}, False))

    def sid(self):
        for c in self.jar:
            if c.name == "connect.sid":
                return c.value
        return None

    # ── 业务 ──────────────────────────────────────────────
    def claim(self):
        st, raw, _ = self.req("/afk/claim", method="POST", body={})
        try:
            return st, json.loads(raw)
        except Exception:  # noqa: BLE001
            return st, {"raw": raw[:200]}

    def balance(self):
        """从 /afk 页面抓 Total Balance"""
        st, raw, _ = self.req("/afk")
        if st != 200:
            return None
        m = re.search(r'id="totalCoins"[^>]*>\s*(\d+)', raw)
        if m:
            return int(m.group(1))
        m = re.search(r"totalCoins\.textContent\s*=\s*(\d+)", raw)
        return int(m.group(1)) if m else None


def ensure_login(site):
    st = load_state()
    if st.get("sid"):
        site.seed(st["sid"])
        b = site.balance()
        if b is not None:
            return True
        log("⚠️ 缓存会话失效，重新登录")
    return site.login()


def pass_once(site, once=False, status_only=False):
    if not ensure_login(site):
        return False

    bal = site.balance()
    log(f"💰 当前余额：{bal if bal is not None else '未知'} coins｜目标 {GOAL}")

    if status_only:
        return True
    if bal is not None and bal >= GOAL:
        log(f"✅ 已达目标 {GOAL}，无需继续")
        return True

    round_ok = 0
    while True:
        st, d = site.claim()
        if st == 200 and (d or {}).get("success"):
            round_ok += 1
            nb = d.get("newBalance")
            log(f"  +1 coin → 余额 {nb}（本次 {round_ok} 次）")
            if isinstance(nb, int) and nb >= GOAL:
                log(f"🎯 达到目标 {GOAL} coins，停止")
                break
        elif st in (401, 403):
            log("⚠️ 会话失效，重新登录")
            if not site.login():
                return False
            continue
        else:
            log(f"⚠️ claim 失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:160]}")
            return False
        if once:
            break
        time.sleep(DELAY)
    save_state(sid=site.sid(), last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return True


def loop(minutes):
    site = Site()
    deadline = time.time() + minutes * 60
    cycles = 0
    fail = 0
    ok_any = False
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            ok = pass_once(site)
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 异常: {type(e).__name__}: {e}")
            ok = False
        if ok:
            fail = 0
            ok_any = True
        else:
            fail += 1
            if fail >= 3:
                log("— [loop] 连续 3 轮失败，退出交棒 —")
                return 2
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1800, rest)
        log(f"— [loop] 休眠 {t/60:.0f} 分钟 —")
        time.sleep(t)
    log(f"— [loop] 预算用尽（{cycles} 轮）—")
    return 0 if ok_any else 2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=0, metavar="MINUTES")
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()

    if not DISCORD_TOKEN:
        log("❌ 缺 L3CH_DISCORD_TOKEN")
        return 3
    log(f"🎯 L3chrane AFK · {BASE}")
    if a.loop:
        return loop(a.loop)
    site = Site()
    ok = pass_once(site, once=a.once, status_only=a.status)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
