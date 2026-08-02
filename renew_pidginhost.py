#!/usr/bin/env python3
"""
PidginHost server auto-renew via pure API (session cookie + CSRF).
不需要 Playwright/浏览器, 纯 urllib HTTP 请求。
"""
import json
import os
import sys
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.pidginhost.com"
SERVER_ID = os.getenv("PIDGINHOST_SERVER_ID", "3920")

# Session cookie + CSRF token (从 GitHub Actions secrets 读)
SESSION = os.getenv("PIDGINHOST_SESSION", "")
CSRF = os.getenv("PIDGINHOST_CSRF", "")

# API token (可选, 用于查状态)
API_TOKEN = os.getenv("PIDGINHOST_API_TOKEN", "")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def log(msg):
    print(f"[pidgin] {msg}", flush=True)


def api_check_status():
    """用 API token 查 server 状态 (如果有 token)"""
    if not API_TOKEN:
        return None
    log("🔄 用 API token 查 server 状态...")
    req = urllib.request.Request(
        f"{BASE}/api/v1/cloud/servers/{SERVER_ID}/",
        headers={"Authorization": f"Token {API_TOKEN}", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            log(f"  ✅ server {SERVER_ID}: status={data.get('status')} machine={data.get('machine',{}).get('status')}")
            return data
    except Exception as e:
        log(f"  ⚠️ API 查状态失败: {e}")
        return None


def panel_call(method, path, payload=None):
    """调用 PidginHost panel API (Django 表单 POST)"""
    url = f"{BASE}{path}"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"sessionid={SESSION}; csrftoken={CSRF}",
        "Referer": f"{BASE}/panel/cloud/servers/{SERVER_ID}/",
        "Origin": BASE,
        "X-CSRFToken": CSRF,
    }
    body = None
    if payload is not None:
        body = urllib.parse.urlencode(payload).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)


def check_session():
    """检查 session 是否有效"""
    log("🔄 检查 session 有效性...")
    status, text = panel_call("GET", f"/panel/cloud/servers/{SERVER_ID}/")
    if status == 200:
        if "extend 30 days" in text.lower() or "expires in" in text.lower():
            log(f"  ✅ session 有效")
            return True
        else:
            log(f"  ⚠️ HTTP 200 但页面异常")
            return True
    elif status in (401, 403):
        log(f"  ❌ session 失效 (HTTP {status})")
        log("   请重新登录 pidginhost.com, 更新 PIDGINHOST_SESSION 和 PIDGINHOST_CSRF secret")
        return False
    else:
        log(f"  ⚠️ HTTP {status}")
        return status == 200


def extend_renewal():
    """续期 30 天"""
    log(f"🔄 续期 server {SERVER_ID}...")
    payload = {
        "csrfmiddlewaretoken": CSRF,
        "action": "extend_renewal",
    }
    status, text = panel_call("POST", f"/panel/cloud/servers/{SERVER_ID}/", payload=payload)
    text_lower = text.lower()
    if "extended for 30 days" in text_lower:
        log("  ✅ 续期成功! (extended for 30 days)")
        return True
    elif "expires in 30 days" in text_lower:
        log("  ✅ 已是 30 天上限")
        return True
    elif "expires in" in text_lower:
        m = re.search(r"expires in (\d+) days", text_lower)
        if m:
            log(f"  ✅ 续期成功! (expires in {m.group(1)} days)")
            return True
    if status in (200, 302) and "extend 30 days" in text_lower:
        log(f"  ✅ 续期请求已提交 (HTTP {status})")
        return True
    if status == 403 and "CSRF" in text:
        log("  ❌ CSRF 验证失败, 请检查 PIDGINHOST_CSRF secret")
    else:
        log(f"  ❌ HTTP {status}: {text[:200]}")
    return False


def main():
    log("=== PidginHost auto-renew start (pure API) ===")

    if not SESSION:
        log("ERROR: missing PIDGINHOST_SESSION env")
        return 1
    if not CSRF:
        log("ERROR: missing PIDGINHOST_CSRF env")
        return 1

    # 1. 用 API token 查状态 (如果有)
    api_check_status()

    # 2. 检查 session
    if not check_session():
        return 1

    # 3. 续期
    ok = extend_renewal()

    log("=== done ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
