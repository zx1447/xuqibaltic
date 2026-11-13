#!/usr/bin/env python3
# Xiaoxia（cn.xiaoxia.app）云端电脑自动唤醒
#
# 机制（2026-11-13 实测）：
#   登录 POST {base}/api/auth/login {"email","password"} → {"token":"<JWT 7天>"}
#   状态 GET  {base}/api/instance → {status, availability: ready|loading|offline,
#                                    health_status, quota:{today_tokens,effective_limit,...}}
#   唤醒 POST {base}/api/instance/start → 幂等（运行中回 {"status":"running"}，无副作用）
#   休眠规则：闲置"一段时间"自动休眠（availability=offline，数据保留）；
#            日 100 万 token 用完 → 停机，次日 0 点（UTC）自动恢复；
#            ⚠️ 休眠期 bot 消息不唤醒、不补收 —— 所以需要本脚本盯 offline 就拉起
#   付费唤醒令牌（/api/instance/wake）仅付费/VIP 可建，免费账号走本脚本的登录通道即可
#
# 环境变量：
#   XIAOXIA_EMAIL / XIAOXIA_PASSWORD   登录凭据（必填）
#   XIAOXIA_BASE                       默认 https://cn.xiaoxia.app
#
# 状态文件 xiaoxia_state.json（token 缓存，7 天内免重登录，变化时 workflow 回写）
#
# 哪吒监控联动（2026-11-13）：实例每次启动/唤醒后必须确保容器内哪吒 agent 也启动。
#   容器侧：OpenClaw cron 脚本任务「nezha-keepalive」每 5 分钟自检拉起（零 token）；
#   本侧：每轮巡检经网关 WS（/api/instance/agent/ws，纯 stdlib 实现）触发 cron.run
#         立即补检一遍；任务若被删则自动重建（cron.add）。双层保险。
#
# 退出码：0 正常/拉起成功；2 有失败；3 登录失败/缺凭据

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("XIAOXIA_BASE", "https://cn.xiaoxia.app").rstrip("/")
EMAIL = os.environ.get("XIAOXIA_EMAIL", "").strip()
PASSWORD = os.environ.get("XIAOXIA_PASSWORD", "").strip()
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xiaoxia_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

# availability: ready=可用 / loading=启动中 / offline=离线(休眠)
# status 侧兜底：stopped / hibernated 也拉起；loading 不动
WAKE_STATES = {"offline", "stopped", "hibernated", "sleeping"}


def log(msg):
    print(msg, flush=True)


def req(path, method="GET", body=None, token=None, retries=3):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(retries):
        r = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, (json.loads(raw) if raw else {})
            except Exception:  # noqa: BLE001
                return e.code, {"raw": raw[:300]}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
            time.sleep(2 + i * 2)
    raise last


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(token):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"token": token,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  f, ensure_ascii=False, indent=2)


def login():
    log(f"== 登录 {EMAIL} ==")
    st, d = req("/api/auth/login", method="POST",
                body={"email": EMAIL, "password": PASSWORD})
    tok = d.get("token") if st == 200 else None
    if not tok:
        log(f"❌ 登录失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
        return None
    log(f"✅ 登录成功（JWT 已取）")
    return tok


def _ensure_nezha_job_params():
    """nezha-keepalive 定时任务参数（与容器侧 scripts/keepalive/nezha-keepalive.sh 配套）"""
    tools = ["ls", "read", "edit", "write", "apply_patch", "exec", "process",
             "skills_search", "skills_read", "dashboard", "nodes", "mobile_ui",
             "computer", "automations", "sessions", "theme", "terminal", "portal",
             "message", "tts", "github_identity_status", "transcripts", "presence",
             "gateway", "plugins", "openclaw", "agents_list", "get_goal",
             "create_goal", "update_goal", "skill_workshop", "progress_card",
             "secrets", "sessions_list", "sessions_history", "sessions_search",
             "conversations_list", "conversations_send", "conversations_turn",
             "sessions_send", "sessions_spawn", "sessions_yield", "subagents",
             "session_status", "web_search", "web_fetch", "view_image", "pdf",
             "browser", "memory_search", "memory_get", "intent", "tavily_search",
             "tavily_extract", "recent_session_context"]
    script = (
        'const r = await exec({ command: "bash /workspace/scripts/keepalive/nezha-keepalive.sh" });\n'
        'const out = String(r?.aggregated ?? "").trim();\n'
        'if (r?.status !== "completed" || r?.exitCode !== 0) {\n'
        '  throw new Error(`nezha keepalive failed (exit ${r?.exitCode ?? r?.status}): ${out.slice(-800)}`);\n'
        '}\n'
        'return { notify: out.split("\\n").slice(-1).join("\\n") || "哪吒在线" };'
    )
    return {
        "name": "nezha-keepalive",
        "displayName": "哪吒监控保活",
        "description": "每 5 分钟确保 nezha-agent 在运行；二进制+固定UUID配置常驻 /workspace/nezha，重启直接复用不重装。零 token 脚本任务。",
        "agentId": "main",
        "schedule": {"kind": "every", "everyMs": 300000},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {"kind": "script", "script": script, "toolsAllow": tools,
                    "toolsAllowIsDefault": True, "timeoutSeconds": 300, "toolBudget": 50},
        "delivery": {"mode": "none"},
        "failureAlert": {"after": 3, "cooldownMs": 7200000},
    }


class _MiniWS:
    """极简 WebSocket 客户端（RFC6455 文本帧，仅够网关 RPC 用；零第三方依赖）"""

    def __init__(self, wss_url, cookie, timeout=20):
        from urllib.parse import urlparse
        u = urlparse(wss_url)
        host, port = u.hostname, (u.port or 443)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Cookie: xx_hosting_token={cookie}\r\n"
               f"Origin: {BASE}\r\nUser-Agent: {UA}\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws handshake closed")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"ws upgrade fail: {head[:120]!r}")
        self._frag = b""

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise RuntimeError("ws closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, s):
        payload = s.encode()
        mask = os.urandom(4)
        hdr = bytes([0x81])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + masked)

    def recv_text(self, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            b0, b1 = self._recv_exact(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if (b1 & 0x80) else None
            data = self._recv_exact(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise RuntimeError("ws closed by server")
            if op in (0x1, 0x0):
                self._frag += data
                if fin:
                    out, self._frag = self._frag, b""
                    return out.decode("utf-8", "replace")
            # 0x9 ping / 0xA pong / 其它控制帧：忽略（会话短，无需应答）

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def ensure_nezha(token):
    """第二层保险：经网关 WS 触发容器内哪吒保活任务立即跑一遍（脚本任务零 token）。
    任务被删则自动重建。失败仅告警，不影响唤醒主流程。"""
    ws_base = BASE.replace("https://", "wss://").replace("http://", "ws://")
    ws = None
    try:
        ws = _MiniWS(f"{ws_base}/api/instance/agent/ws", token)
        seq = [0]

        def rpc(method, params, timeout=25):
            seq[0] += 1
            rid = f"req-{seq[0]}-{os.urandom(4).hex()}"
            ws.send_text(json.dumps({"type": "req", "id": rid,
                                     "method": method, "params": params}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                f = json.loads(ws.recv_text(timeout=max(1, deadline - time.time())))
                if f.get("type") == "res" and f.get("id") == rid:
                    return f
            return {"ok": False, "error": {"message": "rpc timeout"}}

        hello = rpc("connect", {
            "minProtocol": 4, "maxProtocol": 4,
            "client": {"id": "gateway-client", "version": "xx-workspace",
                       "platform": "web", "mode": "backend"},
            "role": "operator",
            "scopes": ["operator.read", "operator.write", "operator.admin"],
            "caps": ["tool-events"], "auth": {}})
        if not hello.get("ok"):
            log(f"  ⚠️ 网关 connect 失败: {json.dumps(hello, ensure_ascii=False)[:150]}")
            return False

        jobs = (rpc("cron.list", {}).get("payload") or {}).get("jobs") or []
        job = next((j for j in jobs if j.get("name") == "nezha-keepalive"
                    and j.get("enabled")), None)
        if job is None:
            log("  ⚠️ nezha-keepalive 任务不存在 → 重建")
            add = rpc("cron.add", _ensure_nezha_job_params())
            job = (add.get("payload") or {}) if add.get("ok") else None
            if not (job and job.get("id")):
                log(f"  ⚠️ 重建失败: {json.dumps(add, ensure_ascii=False)[:150]}")
                return False
        run = rpc("cron.run", {"id": job["id"]})
        payload = run.get("payload") or {}
        ok = bool(payload.get("enqueued") or payload.get("ok"))
        log(f"  {'✅' if ok else '⚠️'} 哪吒保活补检已派发 (job {job['id'][:8]}…) "
            f"{json.dumps(payload, ensure_ascii=False)[:120]}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠️ ensure_nezha 异常: {type(e).__name__}: {e}")
        return False
    finally:
        if ws is not None:
            ws.close()


def pass_once(token):
    """一轮巡检。返回 (ok?, new_token)"""
    st, d = req("/api/instance", token=token)
    if st != 200 or not isinstance(d, dict) or "availability" not in d:
        if isinstance(d, dict) and d.get("error") == "err_unauthorized":
            log("⚠️ token 失效，重新登录")
            token = login()
            if not token:
                return False, None
            st, d = req("/api/instance", token=token)
        else:
            log(f"❌ 取实例状态失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
            return False, token

    avail = (d.get("availability") or "?").lower()
    status = (d.get("status") or "?").lower()
    health = d.get("health_status")
    q = d.get("quota") or {}
    log(f"-- 实例 #{d.get('id')} | availability={avail} | status={status} | health={health} "
        f"| 今日 token {q.get('today_tokens', '?')}/{q.get('effective_limit', '?')} --")

    if avail == "loading":
        log("  ⏭ 启动中，下轮复查")
        return True, token

    if avail == "ready" and status not in WAKE_STATES:
        log("  ✅ 运行中，无需动作")
        ensure_nezha(token)  # 哪吒联动：补检容器内监控 agent
        return True, token

    # 休眠/停机 → 拉起
    log(f"  🚀 检测到休眠/停机 → 唤醒")
    st2, d2 = req("/api/instance/start", method="POST", token=token)
    log(f"     HTTP {st2} {json.dumps(d2, ensure_ascii=False)[:150]}")
    time.sleep(10)
    st3, d3 = req("/api/instance", token=token)
    avail2 = (d3.get("availability") or "?").lower()
    log(f"     复查: availability={avail2} | status={d3.get('status')}")
    if avail2 == "ready":
        ensure_nezha(token)  # 哪吒联动：每次启动/唤醒后立即补检
    return avail2 in ("ready", "loading"), token


def loop(minutes):
    """长驻守护：30 分钟一轮（防 GitHub schedule 调度饿死；接力由 workflow 负责）"""
    st = load_state()
    token = st.get("token") or ""
    deadline = time.time() + minutes * 60
    cycles = 0
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            ok, token = pass_once(token)
            if token is None:
                log("— [loop] 鉴权死，停止守护 —")
                return 3
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 本轮异常: {type(e).__name__}: {e}")
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1800, rest)
        log(f"— [loop] 休眠 {t / 60:.0f} 分钟后复查 —")
        time.sleep(t)
    if token:
        save_state(token)
    log("— [loop] 本轮预算用尽，交棒下一次运行 —")
    return 0


def main():
    if not EMAIL or not PASSWORD:
        log("❌ 缺 XIAOXIA_EMAIL / XIAOXIA_PASSWORD")
        return 3
    st = load_state()
    token = st.get("token") or ""
    log(f"🎯 小虾云端电脑唤醒巡检 · {BASE}")
    ok, token = pass_once(token)
    if token:
        save_state(token)
    return 0 if ok else 2


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            mins = float(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            mins = 330.0
        sys.exit(loop(mins))
    sys.exit(main())
