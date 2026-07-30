#!/usr/bin/env python3
"""CoCalc nezha probe keepalive.

策略:
1. 用 remember_me cookie 访问 cocalc 项目页面 (保持项目 active, 防 idle stop)
2. 检查 nezha 面板 cocalc entry 是否在线 (用公开 API)
3. 如果离线, 尝试通过 cocalc API 创建新 terminal (触发 .bashrc 自启 nezha agent)
4. 发 Telegram 告警 (可选)

环境变量:
  COCALC_PROJECT   - 项目 ID (e.g. f1438fae-073d-4fd7-b135-430ad46742e3)
  COCALC_COOKIE    - remember_me cookie (sha512$...)
  NEZHA_PANEL      - 面板 URL (e.g. https://nz.zxydk1715.dpdns.org)
  NEZHA_UUID       - cocalc 探针 UUID (e.g. a33d05ee-55b7-56c4-869e-c76aae75843b)
  TG_BOT_TOKEN     - (可选) Telegram bot token
  TG_CHAT_ID       - (可选) Telegram chat id
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse

COCALC_PROJECT = os.environ.get("COCALC_PROJECT", "").strip()
COCALC_COOKIE = os.environ.get("COCALC_COOKIE", "").strip()
NEZHA_PANEL = os.environ.get("NEZHA_PANEL", "https://nz.zxydk1715.dpdns.org").strip()
NEZHA_UUID = os.environ.get("NEZHA_UUID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

if not COCALC_PROJECT or not COCALC_COOKIE:
    print("[cocalc] ERROR: missing COCALC_PROJECT or COCALC_COOKIE")
    sys.exit(1)


def log(msg):
    print(f"[cocalc] {msg}", flush=True)


def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": f"[cocalc] {msg}"}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            data=data, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def fetch(url, method="GET", data=None, headers=None, timeout=30):
    """Fetch URL with cookie. Returns (status, body_text)."""
    h = {"User-Agent": UA, "Cookie": f"remember_me={COCALC_COOKIE}"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def ping_project():
    """访问 cocalc 项目页面, 保持 active."""
    url = f"https://cocalc.ai/projects/{COCALC_PROJECT}/files/"
    status, body = fetch(url)
    if status == 200:
        log(f"ping project OK (HTTP {status})")
        return True
    log(f"ping project FAIL (HTTP {status}): {body[:200]}")
    return False


def create_terminal():
    """通过 cocalc API 创建新 terminal (触发 .bashrc 自启 nezha agent).
    cocalc 用 POST /api/v2/project/{id}/open-create-file 创建文件
    """
    # cocalc API 是 websocket, 但有个 REST fallback
    # 试创建 .term 文件
    ts = time.strftime("%Y-%m-%d-%H%M%S")
    path = f"/home/user/{ts}.term"
    body = json.dumps({"path": path, "type": "file"}).encode()
    url = f"https://cocalc.ai/projects/{COCALC_PROJECT}/api/v2/files"
    status, resp = fetch(url, method="POST", data=body,
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
    if status == 200:
        log(f"created terminal file: {path}")
        return True
    log(f"create terminal FAIL (HTTP {status}): {resp[:200]}")
    return False


def check_nezha_online():
    """检查 nezha 面板 cocalc 是否在线.
    用公开 API: GET /api/v1/server (需要 token, 但有公开的 server list)
    nezha v1 公开 server list 在首页, 但需要登录看详情
    试: GET /api/v1/server-group (公开)
    """
    # nezha v1 公开 API
    url = f"{NEZHA_PANEL}/api/v1/server-group"
    status, body = fetch(url, headers={"Accept": "application/json"})
    if status != 200:
        log(f"nezha API FAIL (HTTP {status})")
        return None  # unknown
    try:
        data = json.loads(body)
        # 找 cocalc entry
        servers = []
        for g in data.get("data", {}).get("groups", []):
            for s in g.get("servers", []):
                servers.append(s)
        for s in servers:
            if s.get("uuid") == NEZHA_UUID or "cocalc" in (s.get("name") or "").lower():
                online = s.get("online_status") or s.get("last_active") or False
                # nezha v1 用 is_offline 字段
                if "is_offline" in s:
                    online = not s["is_offline"]
                return online
        # entry 不存在
        return None
    except Exception as e:
        log(f"nezha parse error: {e}")
        return None


def main():
    log("=== cocalc keepalive start ===")
    # 1. ping project (保持 active)
    ping_ok = ping_project()
    # 2. 检查 nezha (可选, 因为 API 可能不公开)
    if NEZHA_UUID:
        online = check_nezha_online()
        if online is True:
            log("nezha: cocalc ONLINE")
        elif online is False:
            log("nezha: cocalc OFFLINE")
            # 尝试创建 terminal 重启 agent
            log("trying to create terminal to restart agent...")
            create_terminal()
            send_tg("cocalc offline, trying to restart agent")
        else:
            log("nezha: cocalc entry not found or API unavailable")
    if not ping_ok:
        send_tg("cocalc project ping failed (cookie may be expired)")
    log("=== cocalc keepalive done ===")


if __name__ == "__main__":
    main()
