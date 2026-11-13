#!/usr/bin/env python3
# GemHost.pl 免费服守护：检测停止 → 自动拉起
#
# 机制（2026-11-13 实测）：
#   POST {base}/api/auth/login {"email","password"}
#     → Set-Cookie: gemhost_access(JWT,7天) + gemhost_csrf
#   GET  {base}/api/servers/list
#     → 名下全部服务器（id / name / plan / status）
#   GET  {base}/api/servers/<id>/status
#     → {status, is_suspended, free_session:{active,ends_at,remaining_ms,runtime_minutes:360,max_runtime_minutes:360}}
#   POST {base}/api/servers/<id>/power  {"action":"start|stop|restart|kill"}   （字段是 action，不是 signal）
#     → 拉起即获得新 6 小时会话（免费服会话耗尽自动停机，本脚本巡检拉起）
#   POST {base}/api/servers/<id>/free/extend-session → 需 captcha，弃用
#   变更类请求带 X-CSRF-Token: <gemhost_csrf cookie 值>
#
# 环境变量：
#   GEMHOST_EMAIL / GEMHOST_PASSWORD   登录凭据（必填）
#   GEMHOST_BASE                       默认 https://panel.gemhost.pl
#
# 状态文件 gemhost_state.json（cookie 缓存，7 天内免重登录）：
#   脚本只在 cookie 变化时改写，workflow 回写分支
#
# 退出码：0 全部正常/拉起成功；2 有失败；3 登录失败/无凭据

import http.cookiejar
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("GEMHOST_BASE", "https://panel.gemhost.pl").rstrip("/")
EMAIL = os.environ.get("GEMHOST_EMAIL", "").strip()
PASSWORD = os.environ.get("GEMHOST_PASSWORD", "").strip()
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemhost_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

# 视为"活着"的状态；stopping 等本轮放过（下轮自然变 offline 再拉）
OK_STATES = {"running", "starting"}
SKIP_STATES = {"stopping", "installing", "restoring"}


def log(msg):
    print(msg, flush=True)


class Session:
    """极简 cookie 会话（gemhost_access / gemhost_csrf 两个命名 cookie）"""

    def __init__(self):
        self.cookies = {}

    def request(self, path, method="GET", body=None, retries=3):
        url = f"{BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for i in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": BASE,
                "Referer": f"{BASE}/",
            })
            if self.cookies:
                req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
            if method != "GET" and self.cookies.get("gemhost_csrf"):
                req.add_header("X-CSRF-Token", self.cookies["gemhost_csrf"])
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._absorb(resp)
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                self._absorb(e)
                raw = e.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw) if raw else {"http_error": e.code}
                except Exception:  # noqa: BLE001
                    return {"http_error": e.code, "body": raw[:300]}
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
                time.sleep(2 + i * 2)
        raise last

    def _absorb(self, resp):
        # http.client 的 headers：get_all 拿全部 Set-Cookie；空值=清除，跳过
        hdrs = getattr(resp, "headers", None)
        if not hdrs:
            return
        for line in (hdrs.get_all("Set-Cookie") or []):
            name, _, rest = line.partition("=")
            value = rest.split(";")[0].strip()
            name = name.strip()
            if name and value:
                self.cookies[name] = value


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(sess, servers_seen):
    out = {"cookies": sess.cookies, "servers": servers_seen,
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def login(sess):
    log(f"== 登录 {EMAIL} ==")
    d = sess.request("/api/auth/login", method="POST",
                     body={"email": EMAIL, "password": PASSWORD})
    if "gemhost_access" not in sess.cookies:
        log(f"❌ 登录失败: {json.dumps(d, ensure_ascii=False)[:300]}")
        return False
    log(f"✅ {d.get('message', 'ok')}（user id {(d.get('user') or {}).get('id')}）")
    return True


def main():
    if not EMAIL or not PASSWORD:
        log("❌ 缺 GEMHOST_EMAIL / GEMHOST_PASSWORD")
        sys.exit(3)

    sess = Session()
    st = load_state()
    sess.cookies = dict(st.get("cookies") or {})

    # 先试缓存 cookie；401/无内容则重登录
    lst = sess.request("/api/servers/list")
    if not isinstance(lst, dict) or "servers" not in lst:
        log("缓存会话失效，重新登录")
        if not login(sess):
            sys.exit(3)
        lst = sess.request("/api/servers/list")

    servers = lst.get("servers") or []
    if not servers:
        log("⚠️ 名下没有服务器")
        save_state(sess, [])
        sys.exit(0)

    log(f"== GemHost 守护 x{len(servers)}（{BASE}）==")
    ok = 0
    rows = []
    seen = []
    for s in servers:
        sid, name = s["id"], s.get("name", "?")
        seen.append({"id": sid, "name": name})
        stt = sess.request(f"/api/servers/{sid}/status")
        status = (stt.get("status") or "?").lower()
        susp = stt.get("is_suspended")
        fs = stt.get("free_session") or {}
        ends = fs.get("ends_at") or "-"
        log(f"-- [{sid}] {name} | status={status} | suspended={susp} | 会话至 {ends}")

        if susp:
            log("  ⏭ 已挂起（suspended），跳过")
            rows.append((sid, name, status, "⏭ 挂起", ends))
            ok += 1
            continue
        if status in OK_STATES:
            log("  ✅ 运行中，无需动作")
            rows.append((sid, name, status, "—", ends))
            ok += 1
            continue
        if status in SKIP_STATES:
            log("  ⏭ 过渡状态，下轮复查")
            rows.append((sid, name, status, "⏭ 等下轮", ends))
            ok += 1
            continue

        log(f"  🚀 检测到停止 → 拉起")
        r = sess.request(f"/api/servers/{sid}/power", method="POST",
                         body={"action": "start"})
        log(f"     {json.dumps(r, ensure_ascii=False)[:200]}")
        time.sleep(8)
        stt2 = sess.request(f"/api/servers/{sid}/status")
        status2 = (stt2.get("status") or "?").lower()
        fs2 = stt2.get("free_session") or {}
        ends2 = fs2.get("ends_at") or "-"
        log(f"     复查: status={status2} | 新会话至 {ends2}")
        if status2 in OK_STATES:
            ok += 1
            rows.append((sid, name, status, "🚀 已拉起", ends2))
        else:
            rows.append((sid, name, status, f"❌ 拉起后 {status2}", ends2))
        time.sleep(1)

    save_state(sess, seen)
    log(f"\n== 完成 {ok}/{len(servers)} ==")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## GemHost 守护结果\n\n")
            f.write("| 服务器 | 名称 | 原状态 | 动作 | 会话至 |\n|---|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |\n")
    sys.exit(0 if ok == len(servers) else 2)


if __name__ == "__main__":
    main()
