#!/usr/bin/env python3
"""
PidginHost 自动续期

两种模式:
1. 有 PIDGINHOST_API_TOKEN: 用 API token 查状态 (快, 不需要登录)
2. 续期: 必须用 Playwright + GitHub OAuth 登录 (panel 表单需要 session cookie)

流程:
1. 用 API token 查 server 状态 (如果有 token)
2. Playwright 登录拿 session cookie
3. 用 session cookie POST extend_renewal 续期
4. 把新 session cookie 写回 GitHub secret
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.pidginhost.com"
SERVER_ID = os.getenv("PIDGINHOST_SERVER_ID", "3920")
GH_USER = os.getenv("DP_GH_USER", "")
GH_PASS = os.getenv("DP_GH_PASS", "")
GH_TOKEN = os.getenv("GH_TOKEN", "")
API_TOKEN = os.getenv("PIDGINHOST_API_TOKEN", "")
REPO = os.getenv("GITHUB_REPOSITORY", "zx1447/xuqibaltic")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def log(msg):
    print(f"[pidgin] {msg}", flush=True)


def api_get(path):
    """用 API token GET (查状态)"""
    if not API_TOKEN:
        return None
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Token {API_TOKEN}", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log(f"  ⚠️ API GET {path} 失败: {e}")
        return None


def check_server_status():
    """用 API token 查 server 状态"""
    if not API_TOKEN:
        log("⚠️ PIDGINHOST_API_TOKEN 未设置, 跳过状态检查")
        return None
    log("🔄 用 API token 查 server 状态...")
    data = api_get(f"/api/v1/cloud/servers/{SERVER_ID}/")
    if data:
        log(f"  ✅ server {SERVER_ID}: status={data.get('status')} machine={data.get('machine',{}).get('status')}")
    return data


def login_and_get_cookies():
    """Playwright + GitHub OAuth 登录"""
    from playwright.sync_api import sync_playwright

    log("🔄 Playwright 登录 pidginhost.com...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()

        page.goto(f"{BASE}/panel/account/login", wait_until="domcontentloaded", timeout=30000)
        time.sleep(1)

        log("  点 GitHub 登录...")
        try:
            page.click('a[href*="/social/login/github/"]', timeout=15000)
        except Exception as e:
            log(f"  ❌ 找不到 GitHub 按钮: {e}")
            browser.close()
            return None, None

        found_gh = False
        for _ in range(15):
            time.sleep(2)
            if "github.com/login" in page.url:
                found_gh = True
                break
            if "pidginhost.com" in page.url and "/social/complete/" not in page.url and "/account/login" not in page.url:
                log("  已登录 (session 还有效)")
                cookies = ctx.cookies()
                session = next((c["value"] for c in cookies if c["name"] == "sessionid" and "pidginhost.com" in c["domain"]), "")
                csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken" and "pidginhost.com" in c["domain"]), "")
                browser.close()
                return session, csrf

        if not found_gh:
            browser.close()
            return None, None

        log("  填 GitHub 凭据...")
        try:
            page.fill("#login_field", GH_USER, timeout=10000)
            page.fill("#password", GH_PASS, timeout=10000)
            page.click('input[type="submit"]', timeout=10000)
        except Exception as e:
            log(f"  ❌ 填表失败: {e}")
            browser.close()
            return None, None

        log("  等跳回 pidginhost...")
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(2)
            url = page.url
            if "github.com/sessions/two-factor" in url:
                log("  ❌ GitHub 需要 2FA")
                browser.close()
                return None, None
            if "github.com/sessions/verified-device" in url:
                log("  ❌ GitHub 要求设备验证")
                browser.close()
                return None, None
            if "github.com/login/oauth/authorize" in url:
                try:
                    page.click("button:has-text('Authorize'), input[value='Authorize']", timeout=5000)
                except Exception:
                    pass
            if "pidginhost.com" in url and "/social/complete/" not in url and "/account/login" not in url:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                log(f"  ✅ 登录成功")
                break
        else:
            browser.close()
            return None, None

        cookies = ctx.cookies()
        session = next((c["value"] for c in cookies if c["name"] == "sessionid" and "pidginhost.com" in c["domain"]), "")
        csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken" and "pidginhost.com" in c["domain"]), "")
        browser.close()
        if session and csrf:
            log(f"  ✅ 拿到新 cookie")
            return session, csrf
        return None, None


def extend_renewal(session, csrf):
    """用 session cookie 调 panel 续期"""
    log(f"🔄 续期 server {SERVER_ID}...")
    payload = {"csrfmiddlewaretoken": csrf, "action": "extend_renewal"}
    url = f"{BASE}/panel/cloud/servers/{SERVER_ID}/"
    req = urllib.request.Request(url, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Cookie", f"sessionid={session}; csrftoken={csrf}")
    req.add_header("Referer", url)
    req.add_header("Origin", BASE)
    req.add_header("X-CSRFToken", csrf)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    body = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(req, data=body, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        status = e.code
    except Exception as e:
        log(f"  ❌ 网络错误: {e}")
        return False

    text_lower = text.lower()
    if "extended for 30 days" in text_lower:
        log("  ✅ 续期成功! (extended for 30 days)")
        return True
    elif "expires in 30 days" in text_lower:
        log("  ✅ 已是 30 天上限")
        return True
    elif "expires in" in text_lower:
        import re
        m = re.search(r"expires in (\d+) days", text_lower)
        if m:
            log(f"  ✅ 续期成功! (expires in {m.group(1)} days)")
            return True
    if status in (200, 302):
        log(f"  ✅ 续期请求已提交 (HTTP {status})")
        return True
    log(f"  ❌ 续期失败 HTTP {status}: {text[:200]}")
    return False


def update_github_secret(name, value):
    """把新 cookie 写回 GitHub secret"""
    if not GH_TOKEN:
        return False
    try:
        from nacl import public
    except ImportError:
        return False

    log(f"  🔄 更新 GitHub secret {name}...")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
        headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pk = json.load(r)
    except Exception as e:
        log(f"  ❌ 拿 public key 失败: {e}")
        return False

    pub_key = public.PublicKey(base64.b64decode(pk["key"]))
    sealed_box = public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(value.encode())
    encoded = base64.b64encode(encrypted).decode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/secrets/{name}",
        method="PUT",
        headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"},
        data=json.dumps({"encrypted_value": encoded, "key_id": pk["key_id"]}).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"  ✅ secret {name} 已更新")
            return True
    except urllib.error.HTTPError as e:
        log(f"  ❌ 更新 secret 失败: {e.read().decode()[:200]}")
        return False


def main():
    log("=== PidginHost auto-renew ===")

    if not GH_USER or not GH_PASS:
        log("ERROR: missing DP_GH_USER or DP_GH_PASS")
        return 1

    # 1. 用 API token 查状态 (如果有)
    check_server_status()

    # 2. Playwright 登录拿新 cookie
    session, csrf = login_and_get_cookies()
    if not session or not csrf:
        log("FATAL: 登录失败")
        return 1

    # 3. 续期
    ok = extend_renewal(session, csrf)

    # 4. 写回 secret
    log("🔄 写回新 cookie 到 GitHub secret...")
    update_github_secret("PIDGINHOST_SESSION", session)
    update_github_secret("PIDGINHOST_CSRF", csrf)

    log("=== done ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
