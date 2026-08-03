#!/usr/bin/env python3
"""
Witchly.host AFK 挂机赚金币
POST /api/earn {duration: 秒数} 每 5 分钟上报一次
"""
import json, os, sys, re, time, urllib.error, urllib.parse, urllib.request, http.cookiejar

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLIENT_ID = "1463750742786572443"
REDIRECT_URI = "https://dash.witchly.host/api/auth/callback/discord"
SCOPE = "identify email guilds.join"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
PROXY = os.getenv("WITCHLY_PROXY", "")
RUN_DURATION = int(os.getenv("RUN_DURATION", "19800"))
REPORT_INTERVAL = 300  # 每 5 分钟上报一次 (跟前端 setInterval 一样)

def log(msg):
    print(f"[witchly] {msg}", flush=True)

def login():
    cj = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(cj)]
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    opener = urllib.request.build_opener(*handlers)

    # OAuth 登录
    req = urllib.request.Request("https://dash.witchly.host/api/auth/csrf")
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        csrf_token = json.loads(resp.read().decode())["csrfToken"]

    signin_data = urllib.parse.urlencode({"csrfToken": csrf_token, "callbackUrl": "/earn/afk", "json": "true"}).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/auth/signin/discord", data=signin_data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=30) as resp:
        redirect_url = json.loads(resp.read().decode())["url"]
    state = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)["state"][0]

    auth_url = f"https://discord.com/api/v9/oauth2/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&scope={urllib.parse.quote(SCOPE)}&state={state}"
    req = urllib.request.Request(auth_url, method="POST")
    req.add_header("Authorization", DISCORD_TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    body = json.dumps({"authorize": True, "permissions": "0", "integration_type": 0}).encode()
    with opener.open(req, data=body, timeout=30) as resp:
        location = json.loads(resp.read().decode())["location"]

    req = urllib.request.Request(location)
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        final_url = resp.url
        if "error=" in final_url:
            m = re.search(r"error=(\w+)", final_url)
            log(f"❌ 登录被拒: {m.group(1) if m else 'unknown'}")
            return None

    # 访问 AFK 页面 (初始化)
    req = urllib.request.Request("https://dash.witchly.host/earn/afk")
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        pass

    return opener

def report_earn(opener, duration):
    """POST /api/earn 上报在线时长"""
    data = json.dumps({"duration": duration, "final": False}).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/earn", data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", "https://dash.witchly.host/earn/afk")
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except:
            return e.code, {"error": body[:200]}
    except Exception as e:
        return -1, {"error": str(e)}

def check_status(opener):
    req = urllib.request.Request("https://dash.witchly.host/api/earn/status")
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except:
        return {}

def main():
    log("=== Witchly AFK start ===")
    log(f"运行: {RUN_DURATION}s, 上报间隔: {REPORT_INTERVAL}s")

    if not DISCORD_TOKEN:
        log("ERROR: missing DISCORD_TOKEN")
        return 1

    if PROXY:
        log(f"代理: {PROXY}")

    # 登录
    log("登录...")
    opener = login()
    if not opener:
        return 1
    log("✅ 登录成功")

    # 初始状态
    status = check_status(opener)
    afk = status.get("afk", {})
    log(f"📊 今日: {afk.get('todayEarnings', '?')}/{afk.get('dailyCap', '?')}")

    # 持续上报
    start = time.time()
    report_count = 0
    while time.time() - start < RUN_DURATION:
        time.sleep(REPORT_INTERVAL)
        elapsed = int(time.time() - start)
        report_count += 1

        # 每 30 分钟重新登录
        if report_count % 6 == 0:
            log(f"🔄 {elapsed}s: 重新登录...")
            opener = login()
            if not opener:
                log("❌ 重新登录失败")
                return 1

        # POST /api/earn
        status_code, result = report_earn(opener, REPORT_INTERVAL)
        
        if status_code == 200:
            # 看返回的金币
            coins = result.get("coins", "?")
            earned = result.get("earned", "?")
            log(f"📊 {elapsed}s ({report_count} reports): +{earned} coins, total={coins}")
        elif status_code == 403:
            code = result.get("code", "")
            error = result.get("error", "")
            if "ANTI_ALT" in code or "Anti-Alt" in error:
                log(f"❌ {elapsed}s: Anti-Alt 检测 (多账号), 停止")
                log("   确保只有一个实例在跑!")
                return 1
            elif "oracle_required" in error:
                log(f"⚠️ {elapsed}s: 需要 Turnstile 验证 (无法自动完成)")
            elif "IP_BANNED" in code:
                log(f"❌ {elapsed}s: IP 被封")
                return 1
            else:
                log(f"❌ {elapsed}s: 403 {error}")
        elif status_code == 429:
            log(f"⚠️ {elapsed}s: 请求太频繁, 等 60s")
            time.sleep(60)
        else:
            log(f"⚠️ {elapsed}s: HTTP {status_code}: {result}")

        # 每 30 分钟查状态
        if report_count % 6 == 0:
            status = check_status(opener)
            afk = status.get("afk", {})
            log(f"📊 状态: today={afk.get('todayEarnings', '?')}/{afk.get('dailyCap', '?')}")

    # 最后上报 (final=true)
    log("最终上报...")
    status_code, result = report_earn(opener, REPORT_INTERVAL)
    # 试 final
    data = json.dumps({"duration": REPORT_INTERVAL, "final": True}).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/earn", data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", "https://dash.witchly.host/earn/afk")
    try:
        with opener.open(req, timeout=30) as resp:
            log(f"最终: HTTP {resp.status}")
    except:
        pass

    status = check_status(opener)
    afk = status.get("afk", {})
    log(f"=== done, 今日: {afk.get('todayEarnings', '?')}/{afk.get('dailyCap', '?')} ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
