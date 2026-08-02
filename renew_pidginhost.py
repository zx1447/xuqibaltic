#!/usr/bin/env python3
"""
PidginHost 自动续期 + 自动刷新 session cookie

流程:
1. 用 Playwright + GitHub OAuth 登录 pidginhost.com, 拿新 session/CSRF cookie
2. 用新 cookie 调续期 API (POST action=extend_renewal)
3. 把新 cookie 写回 GitHub secret (下次 workflow 用新 session)

这样 session 永远是新鲜的, 不会过期。
需要 secrets:
- DP_GH_USER: GitHub 用户名
- DP_GH_PASS: GitHub 密码
- GH_TOKEN: GitHub PAT (有 repo 权限, 用于更新 secret)
- PIDGINHOST_SERVER_ID: 服务器 ID
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
REPO = os.getenv("GITHUB_REPOSITORY", "zx1447/xuqibaltic")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def log(msg):
    print(f"[pidgin] {msg}", flush=True)


def login_and_get_cookies():
    """用 Playwright + GitHub OAuth 登录, 返回 (sessionid, csrftoken)"""
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
            url = page.url
            if "github.com/login" in url:
                found_gh = True
                break
            if "pidginhost.com" in url and "/social/complete/" not in url and "/account/login" not in url:
                log("  已登录 (session 还有效)")
                cookies = ctx.cookies()
                session = next((c["value"] for c in cookies if c["name"] == "sessionid" and "pidginhost.com" in c["domain"]), "")
                csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken" and "pidginhost.com" in c["domain"]), "")
                browser.close()
                return session, csrf

        if not found_gh:
            log("  ❌ 没跳到 GitHub 登录页")
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
                log("  ❌ GitHub 要求设备验证 (异地登录)")
                browser.close()
                return None, None
            if "github.com/login/oauth/authorize" in url:
                try:
                    page.click("button:has-text('Authorize'), input[value='Authorize']", timeout=5000)
                    log("  点 Authorize")
                except Exception:
                    pass
            if "pidginhost.com" in url and "/social/complete/" not in url and "/account/login" not in url:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                log(f"  ✅ 登录成功: {url}")
                break
        else:
            log(f"  ❌ 登录超时, url={page.url}")
            browser.close()
            return None, None

        cookies = ctx.cookies()
        session = next((c["value"] for c in cookies if c["name"] == "sessionid" and "pidginhost.com" in c["domain"]), "")
        csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken" and "pidginhost.com" in c["domain"]), "")

        browser.close()
        if session and csrf:
            log(f"  ✅ 拿到新 cookie: session={session[:15]}... csrf={csrf[:15]}...")
            return session, csrf
        else:
            log(f"  ❌ 没拿到 cookie")
            return None, None


def api_call(session, csrf, method, path, payload=None):
    url = f"{BASE}{path}"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": f"sessionid={session}; csrftoken={csrf}",
        "Referer": f"{BASE}/panel/cloud/servers/{SERVER_ID}/",
        "Origin": BASE,
        "X-CSRFToken": csrf,
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


def extend_renewal(session, csrf):
    log(f"🔄 续期 server {SERVER_ID}...")
    payload = {"csrfmiddlewaretoken": csrf, "action": "extend_renewal"}
    status, text = api_call(session, csrf, "POST", f"/panel/cloud/servers/{SERVER_ID}/", payload)
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
    if not GH_TOKEN:
        log(f"  ⚠️ GH_TOKEN 未设置, 跳过更新 secret {name}")
        return False
    try:
        from nacl import public
    except ImportError:
        log(f"  ⚠️ pynacl 未安装, 跳过更新 secret {name}")
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
            log(f"  ✅ secret {name} 已更新 (HTTP {r.status})")
            return True
    except urllib.error.HTTPError as e:
        log(f"  ❌ 更新 secret 失败 HTTP {e.code}: {e.read().decode()[:200]}")
        return False


def main():
    log("=== PidginHost auto-renew + session refresh ===")

    if not GH_USER or not GH_PASS:
        log("ERROR: missing DP_GH_USER or DP_GH_PASS")
        return 1

    # 1. 登录拿新 cookie
    session, csrf = login_and_get_cookies()
    if not session or not csrf:
        log("FATAL: 登录失败")
        return 1

    # 2. 续期
    ok = extend_renewal(session, csrf)

    # 3. 验证
    if ok:
        log("🔄 验证 server 状态...")
        status, text = api_call(session, csrf, "GET", f"/api/v1/cloud/servers/{SERVER_ID}/")
        if status == 200:
            try:
                data = json.loads(text)
                log(f"  ✅ server {SERVER_ID} status={data.get('status')}")
            except Exception:
                pass

    # 4. 写回 secret
    log("🔄 写回新 cookie 到 GitHub secret...")
    update_github_secret("PIDGINHOST_SESSION", session)
    update_github_secret("PIDGINHOST_CSRF", csrf)

    log("=== done ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
