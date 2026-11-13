#!/usr/bin/env python3
# Ajolote Hosting（ajolote.lat）免费服自动续期
#
# 机制（2026-11-13 实测）：
#   登录 NextAuth：GET /api/auth/csrf → POST /api/auth/callback/credentials
#     （form: csrfToken/email/password/json=true）→ Set-Cookie
#     __Secure-authjs.session-token=<JWT>（30 天）
#   状态 GET  /api/servers/{id}/renew → {free, renewedAt, expiresAt, expired, deleteAt}
#   续期 = 看一个广告（站内自有视频，provider=placeholder）：
#     POST /api/servers/{id}/renew/start   → {token, requiredMs≈40s, commercial}
#     等满 requiredMs（不足会被拒）
#     POST /api/servers/{id}/renew/complete {"token":…} → {ok:true, expiresAt:now+7d}
#   周期：每次续期 +7 天；到期停机不能启动，+30 天（deleteAt）删档连世界一起删。
#   另：每次开机也自动续（Cada vez que lo enciendes también se renueva）。
#
# 环境变量：
#   AJOLOTE_EMAIL / AJOLOTE_PASSWORD   登录凭据（必填）
#   AJOLOTE_BASE                       默认 https://ajolote.lat
#   AJOLOTE_SERVER                     默认 cmwem4vv00002w8fo5kvc11pf（Ajolote Free）
#
# 用法：
#   python3 renew_ajolote.py               # 单趟：临期(<3天)才续
#   python3 renew_ajolote.py --force       # 强制续一次（验证链路）
#   python3 renew_ajolote.py --loop 330    # 长驻守护（30 分钟一轮，bkt 装甲）
#
# 状态文件 ajolote_state.json（session cookie 缓存 + 最近续期记录，变化时 workflow 回写）
#
# 退出码：0 正常/续期成功；2 有失败；3 登录失败/缺凭据

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = os.environ.get("AJOLOTE_BASE", "https://ajolote.lat").rstrip("/")
EMAIL = os.environ.get("AJOLOTE_EMAIL", "").strip()
PASSWORD = os.environ.get("AJOLOTE_PASSWORD", "").strip()
SERVER = os.environ.get("AJOLOTE_SERVER", "cmwem4vv00002w8fo5kvc11pf").strip()
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ajolote_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

# 剩余 < 3 天就续（每次续期 +7 天，到期停机、+30 天删档）
RENEW_BEFORE_MS = 3 * 86400 * 1000
SESSION_COOKIE = "__Secure-authjs.session-token"


def log(msg):
    print(msg, flush=True)


class Api:
    """带 cookie jar 的极简 HTTP 客户端（cookie jar 用于登录双提交）"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, method="GET", body=None, form=None, retries=3):
        url = f"{BASE}{path}"
        last = None
        for _ in range(retries):
            data = None
            headers = {
                "User-Agent": UA,
                "Accept": "application/json, text/x-component, */*",
                "Origin": BASE,
                "Referer": f"{BASE}/es/dashboard/servers/{SERVER}",
            }
            if form is not None:
                data = urllib.parse.urlencode(form).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            r = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self.opener.open(r, timeout=30) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    try:
                        return resp.status, json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        return resp.status, {"_raw": raw[:300]}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    d = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    d = {"_raw": raw[:300]}
                last = (e.code, d)
                if e.code in (429, 500, 502, 503):
                    time.sleep(3)
                    continue
                return e.code, d
            except Exception as e:  # noqa: BLE001
                last = (0, {"error": f"{type(e).__name__}: {e}"})
                time.sleep(3)
        return last if last else (0, {"error": "unreachable"})

    def seed_session(self, token):
        """把缓存的 session token 注入 jar"""
        import http.cookiejar as cj
        c = cj.Cookie(
            version=0, name=SESSION_COOKIE, value=token, port=None,
            port_specified=False, domain="ajolote.lat", domain_specified=True,
            domain_initial_dot=False, path="/", path_specified=True,
            secure=True, expires=None, discard=False, comment=None,
            comment_url=None, rest={}, rfc2109=False)
        self.jar.set_cookie(c)

    def session_token(self):
        for c in self.jar:
            if c.name == SESSION_COOKIE:
                return c.value
        return None


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(api, extra=None):
    st = load_state()
    tok = api.session_token()
    if tok:
        st["cookie"] = tok
    st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if extra:
        st.update(extra)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def login(api):
    log(f"== 登录 {EMAIL} ==")
    st, d = api.req("/api/auth/csrf")
    csrf = (d or {}).get("csrfToken")
    if not csrf:
        log(f"❌ 取 csrf 失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
        return False
    st2, d2 = api.req("/api/auth/callback/credentials", method="POST",
                      form={"csrfToken": csrf, "email": EMAIL,
                            "password": PASSWORD, "json": "true"})
    # 成功 = 302 被 opener 跟随 → 200/302 均可能；失败返回 {"url":.../error}
    if api.session_token():
        log("✅ 登录成功（session token 已取）")
        return True
    log(f"❌ 登录失败 HTTP {st2}: {json.dumps(d2, ensure_ascii=False)[:200]}")
    return False


def ensure_session(api):
    st = load_state()
    if st.get("cookie"):
        api.seed_session(st["cookie"])
        code, _ = api.req(f"/api/servers/{SERVER}/renew")
        if code == 200:
            return True
        log("⚠️ 缓存 session 失效，重新登录")
    return login(api)


def ad_renew(api):
    """看广告续期：start → 等 requiredMs → complete。返回 (ok, info)"""
    log("  📺 开始续期流程（看广告）…")
    st, d = api.req(f"/api/servers/{SERVER}/renew/start", method="POST", body={})
    if st == 409:
        log("  ⏭ 409：已在续期/刚续过，跳过")
        return True, {"skipped": "409"}
    if st != 200:
        err = (d or {}).get("error", "")
        if err == "rate_limited":
            log("  ⚠️ rate_limited：稍后下一轮再试")
            return False, d
        log(f"  ❌ renew/start 失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
        return False, d
    token = (d or {}).get("token")
    required = int((d or {}).get("requiredMs") or 40000)
    if not token:
        log(f"  ❌ 无 token: {json.dumps(d, ensure_ascii=False)[:200]}")
        return False, d
    log(f"  ⏱ 广告时长 {required / 1000:.0f}s（provider={d.get('provider')}）…")
    time.sleep(required / 1000 + 3)
    st2, d2 = api.req(f"/api/servers/{SERVER}/renew/complete", method="POST",
                      body={"token": token})
    if st2 == 200 and (d2 or {}).get("ok"):
        log(f"  ✅ 续期成功 → expiresAt={d2.get('expiresAt')}")
        return True, d2
    if (d2 or {}).get("error") == "already_running":
        log("  ⏭ already_running：视为已续")
        return True, d2
    log(f"  ❌ renew/complete 失败 HTTP {st2}: {json.dumps(d2, ensure_ascii=False)[:200]}")
    return False, d2


def pass_once(api, force=False):
    """一轮巡检。返回 ok?"""
    if not ensure_session(api):
        return False

    st, d = api.req(f"/api/servers/{SERVER}/renew")
    if st != 200 or not (d or {}).get("free"):
        if st in (401, 403):
            log("⚠️ session 失效，重新登录")
            if not login(api):
                return False
            st, d = api.req(f"/api/servers/{SERVER}/renew")
        else:
            log(f"❌ 取续期状态失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
            return False

    expires = (d or {}).get("expiresAt") or ""
    delete_at = (d or {}).get("deleteAt") or ""
    expired = bool((d or {}).get("expired"))
    remain_ms = 0
    try:
        remain_ms = (time.mktime(time.strptime(expires, "%Y-%m-%dT%H:%M:%S.%fZ"))
                     - time.time()) * 1000 if "." in expires else \
                    (time.mktime(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ"))
                     - time.time()) * 1000
    except Exception:  # noqa: BLE001
        pass
    days = remain_ms / 86400000
    log(f"-- 服 {SERVER} | 到期 {expires}（剩 {days:.1f} 天）| expired={expired} "
        f"| 删档线 {delete_at} --")

    if not force and not expired and remain_ms > RENEW_BEFORE_MS:
        log("  ✅ 充足，无需动作")
        return True

    if expired:
        log("  🚨 已过期（服务器停机）→ 立即续期")
    elif force:
        log("  🔧 --force 强制续期")
    else:
        log(f"  ⏰ 剩余 < {RENEW_BEFORE_MS // 86400000} 天 → 自动续期")

    ok, info = ad_renew(api)
    if ok and not info.get("skipped"):
        save_state(api, {"last_renewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "last_expires_at": info.get("expiresAt")})
    return ok


def loop(minutes, force=False):
    """长驻守护：30 分钟一轮（bkt 装甲，接力由 workflow 负责）"""
    api = Api()
    deadline = time.time() + minutes * 60
    cycles = 0
    ok_all = True
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            ok = pass_once(api, force=(force and cycles == 1))
            ok_all = ok_all and ok
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 本轮异常: {type(e).__name__}: {e}")
            ok_all = False
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1800, rest)
        log(f"— [loop] 休眠 {t / 60:.0f} 分钟后复查 —")
        time.sleep(t)
    save_state(api)
    log("— [loop] 本轮预算用尽，交棒下一次运行 —")
    return 0 if ok_all else 2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, default=0, metavar="MINUTES")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    if not EMAIL or not PASSWORD:
        log("❌ 缺 AJOLOTE_EMAIL / AJOLOTE_PASSWORD")
        return 3

    log(f"🎯 Ajolote 免费服续期巡检 · {BASE} · 服 {SERVER}")
    if args.loop:
        return loop(args.loop, force=args.force)

    api = Api()
    try:
        ok = pass_once(api, force=args.force)
    except Exception as e:  # noqa: BLE001
        log(f"❌ 异常: {type(e).__name__}: {e}")
        ok = False
    save_state(api)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
