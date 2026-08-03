#!/usr/bin/env python3
"""
Witchly.host AFK 挂机赚金币
用 Discord token 走 OAuth 登录, 通过 VLESS 代理绕过数据中心 IP 检测
持续运行, 每 100 秒访问一次 AFK 页面 (保持活跃)
"""
import json
import os
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLIENT_ID = "1463750742786572443"
REDIRECT_URI = "https://dash.witchly.host/api/auth/callback/discord"
SCOPE = "identify email guilds.join"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
PROXY = os.getenv("WITCHLY_PROXY", "")

# 持续时间 (秒), 默认 5.5 小时 (GHA 6h timeout 留 30 分钟余量)
RUN_DURATION = int(os.getenv("RUN_DURATION", "19800"))
# 访问间隔 (秒), AFK 每 120 秒 1 金币, 我们每 100 秒访问一次
PING_INTERVAL = 100


def log(msg):
    print(f"[witchly] {msg}", flush=True)


def login():
    """OAuth 登录, 返回 (opener, session_token) 或 (None, None)"""
    cj = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(cj)]
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    opener = urllib.request.build_opener(*handlers)

    # 1. CSRF
    req = urllib.request.Request("https://dash.witchly.host/api/auth/csrf")
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        csrf_token = json.loads(resp.read().decode())["csrfToken"]

    # 2. signin
    signin_data = urllib.parse.urlencode({
        "csrfToken": csrf_token, "callbackUrl": "/earn/afk", "json": "true",
    }).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/auth/signin/discord",
        data=signin_data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=30) as resp:
        redirect_url = json.loads(resp.read().decode())["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)["state"][0]

    # 3. authorize
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
    with opener.open(req, data=body, timeout=30) as resp:
        location = json.loads(resp.read().decode())["location"]

    # 4. callback
    req = urllib.request.Request(location)
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        final_url = resp.url
        if "error=" in final_url:
            m = re.search(r"error=(\w+)", final_url)
            err = m.group(1) if m else "unknown"
            log(f"❌ 登录被拒: {err}")
            return None, None

    # 5. session
    session_token = None
    for c in cj:
        if "witchly" in c.domain and "session" in c.name.lower():
            session_token = c.value
            break
    return opener, session_token


def ping_afk(opener):
    """访问 AFK 页面, 保持活跃"""
    req = urllib.request.Request("https://dash.witchly.host/earn/afk")
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        return resp.status


def check_status(opener):
    """查金币状态"""
    req = urllib.request.Request("https://dash.witchly.host/api/earn/status")
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            afk = data.get("afk", {})
            return afk.get("todayEarnings", "?"), afk.get("dailyCap", "?")
    except:
        return "?", "?"


def main():
    log("=== Witchly AFK start ===")
    log(f"运行时长: {RUN_DURATION}s ({RUN_DURATION//3600}h{(RUN_DURATION%3600)//60}m), 间隔: {PING_INTERVAL}s")

    if not DISCORD_TOKEN:
        log("ERROR: missing DISCORD_TOKEN")
        return 1

    if PROXY:
        log(f"使用代理: {PROXY}")

    # 登录
    log("登录中...")
    try:
        opener, session = login()
    except Exception as e:
        log(f"❌ 登录失败: {e}")
        return 1

    if not opener or not session:
        log("❌ 登录失败")
        return 1
    log(f"✅ 登录成功, session: {session[:20]}...")

    # 首次访问 AFK 页面
    try:
        ping_afk(opener)
        log("✅ AFK 页面已打开")
    except Exception as e:
        log(f"❌ 访问 AFK 失败: {e}")
        return 1

    # 查初始状态
    today, cap = check_status(opener)
    log(f"📊 今日金币: {today}/{cap}")

    # 持续循环
    start = time.time()
    ping_count = 0
    while time.time() - start < RUN_DURATION:
        time.sleep(PING_INTERVAL)
        elapsed = int(time.time() - start)
        ping_count += 1
        
        try:
            # 重新登录如果 session 过期 (每 30 分钟重新登录一次)
            if ping_count % 18 == 0:  # 18 * 100s = 30min
                log(f"🔄 {elapsed}s: 重新登录刷新 session...")
                opener, session = login()
                if not opener:
                    log("❌ 重新登录失败, 退出")
                    return 1
                log(f"✅ 重新登录成功")
            
            ping_afk(opener)
            
            # 每 10 次 ping 查一次金币 (约 17 分钟)
            if ping_count % 10 == 0:
                today, cap = check_status(opener)
                log(f"📊 {elapsed}s ({ping_count} pings): 今日金币 {today}/{cap}")
            else:
                log(f"💤 {elapsed}s ({ping_count} pings)")
        except Exception as e:
            log(f"⚠️ {elapsed}s: ping 失败: {e}, 重新登录...")
            try:
                opener, session = login()
                if opener:
                    log("✅ 重新登录成功")
                else:
                    log("❌ 重新登录失败")
                    return 1
            except Exception as e2:
                log(f"❌ 重新登录异常: {e2}")
                return 1

    # 最终状态
    today, cap = check_status(opener)
    log(f"=== done, 最终金币: {today}/{cap} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
