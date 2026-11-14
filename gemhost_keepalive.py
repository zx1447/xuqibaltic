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
#   POST {base}/api/servers/<id>/power {"action":"restart"}
#     → 实测（2026-11-14）rewrite 免费会话：restart 会把 ends_at 重置为「现在+6h」，
#       即 restart 等价于「换一个新会话」，不必等停机
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
#
# 新增（2026-11-14）：主动续期
#   免费会话 6h 一到会硬停机，只靠 offline 才拉起会有断档窗口。
#   现改为：剩余时间 <= RENEW_BEFORE_MIN 时**主动 restart**，把 ends_at 重置为满 6h，
#   做到无缝续期、永不硬停。
#   注意巡检间隔必须小于触发阈值，否则两轮之间就漏了 →
#   因此同时加了 next_wake_seconds 输出，workflow 用它做自适应休眠。

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
# 剩余会话时间低于此值就主动 restart 续期（分钟）
RENEW_BEFORE_MIN = int(os.environ.get("RENEW_BEFORE_MIN", "15"))
# restart 后等待服务回到 running 的最长秒数
RENEW_WAIT_SECONDS = int(os.environ.get("RENEW_WAIT_SECONDS", "90"))

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


def wait_until_up(sess, sid, limit=RENEW_WAIT_SECONDS):
    """轮询到服务回到 OK_STATES，返回 (status, free_session)。"""
    waited = 0
    stt = {}
    while waited < limit:
        time.sleep(6)
        waited += 6
        stt = sess.request(f"/api/servers/{sid}/status")
        st = (stt.get("status") or "?").lower()
        if st in OK_STATES:
            return st, stt.get("free_session") or {}
    return (stt.get("status") or "?").lower(), stt.get("free_session") or {}


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
    # 本轮观测到的最小会话剩余时间（分钟），用于算下一轮该睡多久
    min_remain_min = None
    for s in servers:
        sid, name = s["id"], s.get("name", "?")
        seen.append({"id": sid, "name": name})
        stt = sess.request(f"/api/servers/{sid}/status")
        status = (stt.get("status") or "?").lower()
        susp = stt.get("is_suspended")
        fs = stt.get("free_session") or {}
        ends = fs.get("ends_at") or "-"
        remain_ms = fs.get("remaining_ms")
        remain_min = round(remain_ms / 60000, 1) if isinstance(remain_ms, (int, float)) else None
        log(f"-- [{sid}] {name} | status={status} | suspended={susp} | 会话至 {ends}"
            + (f" | 剩余 {remain_min}min" if remain_min is not None else ""))
        if remain_min is not None and (min_remain_min is None or remain_min < min_remain_min):
            min_remain_min = remain_min

        if susp:
            log("  ⏭ 已挂起（suspended），跳过")
            rows.append((sid, name, status, "⏭ 挂起", ends))
            ok += 1
            continue

        if status in OK_STATES:
            # ── 主动续期：剩余不够就医治，避免 6h 到点硬停机
            if not fs.get("active") or remain_min is None:
                log("  ✅ 运行中（无会话信息），无需动作")
                rows.append((sid, name, status, "—", ends))
                ok += 1
                continue
            if remain_min > RENEW_BEFORE_MIN:
                log(f"  ✅ 运行中，剩余 {remain_min}min（>{RENEW_BEFORE_MIN}min 阈值），无需动作")
                rows.append((sid, name, status, "—", ends))
                ok += 1
                continue

            # 到达阈值 → 主动 restart 换新会话
            log(f"  🔄 剩余仅 {remain_min}min ≤ {RENEW_BEFORE_MIN}min → 主动 restart 续期")
            r = sess.request(f"/api/servers/{sid}/power", method="POST",
                             body={"action": "restart"})
            log(f"     {json.dumps(r, ensure_ascii=False)[:200]}")
            status2, fs2 = wait_until_up(sess, sid)
            ends2 = fs2.get("ends_at") or "-"
            remain2 = fs2.get("remaining_ms")
            remain2_min = round(remain2 / 60000, 1) if isinstance(remain2, (int, float)) else None
            log(f"     复查: status={status2} | 新会话至 {ends2}"
                + (f" | 剩余 {remain2_min}min" if remain2_min is not None else ""))
            if status2 in OK_STATES:
                ok += 1
                rows.append((sid, name, status, f"🔄 已续期(原剩{remain_min}min)", ends2))
                log(f"     ✅ 续期成功，会话已重置为满额")
            else:
                rows.append((sid, name, status, f"❌ 续期后 {status2}", ends2))
                log(f"     ❌ 续期失败，状态 {status2}")
            time.sleep(1)
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

    # ── 自适应唤醒：算出下一轮应该在多久之后检查 ──
    # 目标：在「剩余时间掉到 RENEW_BEFORE_MIN 以下」之前就醒来，
    # 否则巡检间隔(30min) > 阈值(15min) 会漏掉整个窗口。
    next_wake_seconds = None
    if min_remain_min is not None:
        # 距离阈值还有多少分钟；留 2 分钟余量；最少 60s，最多 30min
        until_threshold = min_remain_min - RENEW_BEFORE_MIN
        if until_threshold <= 0:
            wake_min = 1          # 已经到阈值，下一轮立刻复查
        else:
            wake_min = max(1, int(until_threshold) - 2)
        next_wake_seconds = min(wake_min * 60, 1800)
        log(f"[wake] 最小剩余 {min_remain_min}min，阈值 {RENEW_BEFORE_MIN}min"
            f" → 下一轮 {next_wake_seconds}s 后唤醒")
    else:
        log("[wake] 未取到会话剩余时间，沿用默认 30min")

    # 供 workflow 读取（写进 $GITHUB_OUTPUT）
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"next_wake_seconds={next_wake_seconds or 1800}\n")
            f.write(f"min_remain_min={min_remain_min if min_remain_min is not None else ''}\n")
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
