#!/usr/bin/env python3
"""
Witchly.host AFK 挂机赚金币
流程:
1. 用 Discord token 走 OAuth 登录 witchly (拿 session cookie)
2. 访问 /earn/afk 页面 (保持活跃, 每 120 秒 1 金币)
3. 收益每 5 分钟自动同步

环境变量:
  DISCORD_TOKEN  - Discord 用户 token (MTM... 格式)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLIENT_ID = "1463750742786572443"
REDIRECT_URI = "https://dash.witchly.host/api/auth/callback/discord"
SCOPE = "identify email guilds.join"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"


def log(msg):
    print(f"[witchly] {msg}", flush=True)


def main():
    log("=== Witchly AFK start ===")

    if not DISCORD_TOKEN:
        log("ERROR: missing DISCORD_TOKEN")
        return 1

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 1. 获取 CSRF token
    log("1. 获取 CSRF token...")
    req = urllib.request.Request("https://dash.witchly.host/api/auth/csrf")
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=30) as resp:
            csrf_token = json.loads(resp.read().decode())["csrfToken"]
        log("  ✅ CSRF token 获取成功")
    except Exception as e:
        log(f"  ❌ 获取 CSRF 失败: {e}")
        return 1

    # 2. 发起 Discord OAuth signin
    log("2. 发起 Discord OAuth...")
    signin_data = urllib.parse.urlencode({
        "csrfToken": csrf_token,
        "callbackUrl": "/earn/afk",
        "json": "true",
    }).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/auth/signin/discord",
        data=signin_data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            redirect_url = data.get("url", "")
        if not redirect_url:
            log(f"  ❌ 没拿到 OAuth URL: {data}")
            return 1
        state = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)["state"][0]
        log("  ✅ OAuth URL 获取成功")
    except Exception as e:
        log(f"  ❌ signin 失败: {e}")
        return 1

    # 3. 用 Discord token 自动授权
    log("3. Discord OAuth authorize...")
    auth_url = (f"https://discord.com/api/v9/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope={urllib.parse.quote(SCOPE)}"
        f"&state={state}")
    req = urllib.request.Request(auth_url, method="POST")
    req.add_header("Authorization", DISCORD_TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    body = json.dumps({"authorize": True, "permissions": "0", "integration_type": 0}).encode()
    try:
        with opener.open(req, data=body, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            location = data.get("location", "")
        if not location:
            log(f"  ❌ 没拿到 callback URL: {data}")
            return 1
        log("  ✅ Discord authorize 成功")
    except Exception as e:
        log(f"  ❌ authorize 失败: {e}")
        return 1

    # 4. 访问 callback 完成登录
    log("4. 完成 callback...")
    req = urllib.request.Request(location)
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=30) as resp:
            final_url = resp.url
        if "error=" in final_url:
            import re
            m = re.search(r"error=(\w+)", final_url)
            err = m.group(1) if m else "unknown"
            log(f"  ❌ 登录被拒: {err}")
            if err == "Datacenter":
                log("  ⚠️ IP 被识别为数据中心, 需要用住宅代理")
            return 1
        log(f"  ✅ 登录成功: {final_url}")
    except urllib.error.HTTPError as e:
        log(f"  ❌ callback HTTP {e.code}: {e.url}")
        return 1

    # 5. 检查 session
    session_token = None
    for c in cj:
        if "witchly" in c.domain and "session" in c.name.lower():
            session_token = c.value
            log(f"  ✅ session: {c.value[:30]}...")
            break
    if not session_token:
        log("  ❌ 没拿到 session token")
        return 1

    # 6. 访问 /earn/afk 页面
    log("5. 访问 /earn/afk...")
    req = urllib.request.Request("https://dash.witchly.host/earn/afk")
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode()
            if "Sign in with Discord" in body:
                log("  ❌ 还是登录页 (session 无效)")
                return 1
            log("  ✅ AFK 页面已打开")
            # 看金币信息
            import re
            for m in re.finditer(r"[^<>]{0,60}(?:coin|point|reward|earn|siphon)[^<>]{0,60}", body, re.I):
                t = m.group().strip()
                if t and len(t) > 10 and "<" not in t:
                    log(f"  📊 {t[:80]}")
    except Exception as e:
        log(f"  ❌ 访问 AFK 页面失败: {e}")
        return 1

    # 7. 看 API 有没有 claim/sync endpoint
    log("6. 查找 AFK API...")
    # Next.js 通常有 /api/ 开头的 API
    for path in ["/api/earn/afk", "/api/afk", "/api/earn", "/api/coins", "/api/earn/claim"]:
        req = urllib.request.Request(f"https://dash.witchly.host{path}")
        req.add_header("User-Agent", UA)
        try:
            with opener.open(req, timeout=10) as resp:
                body = resp.read().decode()[:500]
                log(f"  GET {path} → HTTP {resp.status}: {body[:200]}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200] if e.code < 500 else ""
            log(f"  GET {path} → HTTP {e.code}")
        except:
            pass

    log("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
