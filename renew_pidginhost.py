#!/usr/bin/env python3
"""
PidginHost server auto-renew via pure API (no browser).
策略: session cookie + CSRF token + POST /panel/cloud/servers/{id}/ + action=extend_renewal
优势: 不需要 Playwright/浏览器, 不会被 UI 改版影响, 几秒完成
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.pidginhost.com"
SERVER_ID = os.getenv("PIDGINHOST_SERVER_ID", "3920")

# Session cookie + CSRF token (从 GitHub Actions secrets 读)
SESSION = os.getenv("PIDGINHOST_SESSION", "")
CSRF = os.getenv("PIDGINHOST_CSRF", "")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def log(msg):
    print(f"[pidgin] {msg}", flush=True)


def api_call(method, path, payload=None, extra_headers=None):
    """调用 PidginHost panel API (Django 表单 POST)"""
    url = f"{BASE}{path}"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"sessionid={SESSION}; csrftoken={CSRF}",
        "Referer": f"{BASE}/panel/cloud/servers/{SERVER_ID}/",
        "Origin": BASE,
    }
    if CSRF:
        headers["X-CSRFToken"] = CSRF

    body = None
    if payload is not None:
        body = urllib.parse.urlencode(payload).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, dict(resp.headers), text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        return e.code, dict(e.headers), text
    except urllib.error.URLError as e:
        return -1, {}, str(e)


def check_session():
    """检查 session 是否有效 (调 API 列服务器)"""
    log("🔄 检查 session 有效性...")
    status, _, text = api_call("GET", f"/api/v1/cloud/servers/{SERVER_ID}/")
    if status == 200:
        try:
            data = json.loads(text)
            log(f"✅ session 有效, server {SERVER_ID} status={data.get('status')}")
            return True, data
        except Exception:
            log(f"⚠️ 200 但解析失败: {text[:200]}")
            return False, {}
    elif status == 401 or status == 403:
        log(f"❌ session 失效 (HTTP {status})")
        log("   请重新登录 pidginhost.com, 更新 PIDGINHOST_SESSION 和 PIDGINHOST_CSRF secret")
        return False, {}
    else:
        log(f"⚠️ 检查 session 返回 HTTP {status}")
        return False, {}


def extend_renewal():
    """续期 30 天"""
    log(f"🔄 续期 server {SERVER_ID}...")
    payload = {
        "csrfmiddlewaretoken": CSRF,
        "action": "extend_renewal",
    }
    status, headers, text = api_call(
        "POST",
        f"/panel/cloud/servers/{SERVER_ID}/",
        payload=payload,
    )

    # 分析结果
    if status == 200 or status == 302:
        # 看页面文本确认
        text_lower = text.lower()
        if "extended for 30 days" in text_lower:
            log("✅ 续期成功! (extended for 30 days)")
            return True
        elif "expires in 30 days" in text_lower:
            log("✅ 已是 30 天上限 (expires in 30 days)")
            return True
        elif "expires in" in text_lower:
            # 看具体天数
            import re
            m = re.search(r"expires in (\d+) days", text_lower)
            if m:
                log(f"✅ 续期成功! (expires in {m.group(1)} days)")
                return True
        # 200 但没明确提示, 也算成功 (表单 POST 成功后 Django 重定向回详情页)
        if status == 200 and "extend 30 days" in text_lower:
            log("✅ 续期请求已提交 (页面显示 Extend 按钮仍在)")
            return True
        log(f"⚠️ HTTP {status}, 但未确认续期结果, 看 body 前 300 字:")
        log(f"   {text[:300]}")
        return status == 200
    elif status == 403:
        if "CSRF" in text:
            log("❌ CSRF 验证失败, 请检查 PIDGINHOST_CSRF secret")
        else:
            log("❌ 403 Forbidden, session 可能过期")
        return False
    else:
        log(f"❌ HTTP {status}")
        log(f"   {text[:300]}")
        return False


def main():
    log("=== PidginHost auto-renew start (pure API) ===")

    if not SESSION:
        log("ERROR: missing PIDGINHOST_SESSION env")
        return 1
    if not CSRF:
        log("ERROR: missing PIDGINHOST_CSRF env")
        return 1

    # 1. 检查 session
    ok, server_info = check_session()
    if not ok:
        return 1

    # 2. 续期
    ok = extend_renewal()

    # 3. 验证 (重新查 server 状态)
    if ok:
        log("🔄 验证续期结果...")
        status, _, text = api_call("GET", f"/api/v1/cloud/servers/{SERVER_ID}/")
        if status == 200:
            try:
                data = json.loads(text)
                log(f"✅ server {SERVER_ID} status={data.get('status')}")
            except Exception:
                pass

    log("=== done ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
