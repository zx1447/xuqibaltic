#!/usr/bin/env python3
"""
Witchly.host AFK 挂机赚金币
支持多个 VLESS 代理 (逗号分隔), 自动选能用的
"""
import json, os, sys, re, time, subprocess, urllib.error, urllib.parse, urllib.request, http.cookiejar

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
# 多个 VLESS URL (逗号分隔), 依次尝试
VLESS_URLS = os.getenv("VLESS_URLS", os.getenv("DP_VLESS_URL", "")).strip()
# 第二个 VLESS (硬编码, GitHub API 加不了 secret)
VLESS_URL_2 = "vless://77df559d-05f6-42a5-8a07-08118dc65a9f@demanding-assisted-berkeley-imposed.trycloudflare.com:443?type=ws&security=tls&path=%2Fstatic%2Fassets%2F34c1614493b4.js&sni=demanding-assisted-berkeley-imposed.trycloudflare.com&fp=chrome#CF-VLESS"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
PROXY_PORT = 10809
RUN_DURATION = int(os.getenv("RUN_DURATION", "19800"))
REPORT_INTERVAL = 300

def log(msg):
    print(f"[witchly] {msg}", flush=True)

def parse_vless(url):
    """解析 VLESS URL"""
    uuid = re.sub(r'^vless://([^@]+)@.*', r'\1', url)
    host_port = re.sub(r'^vless://[^@]+@([^/?]+).*', r'\1', url)
    host = host_port.split(':')[0]
    query = re.sub(r'.*\?([^#]*)#?.*', r'\1', url)
    path = '/'
    for part in query.split('&'):
        if part.startswith('path='):
            path = part[5:].replace('%2F', '/')
    return host, uuid, path

def start_xray(vless_url):
    """启动 xray 代理"""
    host, uuid, path = parse_vless(vless_url)
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag":"socks","port":10808,"listen":"127.0.0.1","protocol":"socks","settings":{"udp":True}},
            {"tag":"http","port":PROXY_PORT,"listen":"127.0.0.1","protocol":"http","settings":{}}
        ],
        "outbounds": [{
            "tag":"vless","protocol":"vless",
            "settings":{"vnext":[{"address":host,"port":443,"users":[{"id":uuid,"encryption":"none","flow":""}]}]},
            "streamSettings":{
                "network":"ws","security":"tls",
                "tlsSettings":{"serverName":host,"fingerprint":"chrome","allowInsecure":False},
                "wsSettings":{"path":path,"headers":{"Host":host}}
            }
        }, {"tag":"direct","protocol":"freedom","settings":{}}],
        "routing":{"rules":[{"type":"field","outboundTag":"direct","ip":["127.0.0.0/8","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]}]}
    }
    import tempfile
    config_path = "/tmp/xray-witchly.json"
    with open(config_path, 'w') as f:
        json.dump(config, f)
    
    os.system("pkill -9 -f xray 2>/dev/null; sleep 1")
    os.system(f"nohup /tmp/xray run -c {config_path} > /tmp/xray-witchly.log 2>&1 &")
    time.sleep(3)
    
    # 验证
    proxy_handler = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{PROXY_PORT}", "https": f"http://127.0.0.1:{PROXY_PORT}"})
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(urllib.request.Request("https://api.ipify.org", headers={"User-Agent": UA}), timeout=10) as resp:
            ip = resp.read().decode()
            log(f"  代理 IP: {ip}")
            return ip
    except:
        return None

def login(proxy):
    """OAuth 登录"""
    cj = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(cj)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

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

    auth_url = f"https://discord.com/api/v9/oauth2/authorize?client_id=1463750742786572443&response_type=code&redirect_uri={urllib.parse.quote('https://dash.witchly.host/api/auth/callback/discord')}&scope={urllib.parse.quote('identify email guilds.join')}&state={state}"
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
            return None, m.group(1) if m else "unknown"

    # 访问 AFK 页面
    req = urllib.request.Request("https://dash.witchly.host/earn/afk")
    req.add_header("User-Agent", UA)
    with opener.open(req, timeout=30) as resp:
        pass

    return opener, None

def report_earn(opener, duration):
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
    except:
        return -1, {"error": "network"}

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
    log(f"运行: {RUN_DURATION}s, 上报: {REPORT_INTERVAL}s")

    if not DISCORD_TOKEN:
        log("ERROR: missing DISCORD_TOKEN")
        return 1

    # 解析多个 VLESS URL
    vless_list = [u.strip() for u in VLESS_URLS.split(",") if u.strip()]
if VLESS_URL_2:
    vless_list.append(VLESS_URL_2)
    if not vless_list:
        log("ERROR: no VLESS URL")
        return 1
    log(f"VLESS 节点: {len(vless_list)} 个")

    # 依次尝试每个 VLESS, 找一个能登录的
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    opener = None
    for i, vless in enumerate(vless_list):
        host = parse_vless(vless)[0]
        log(f"尝试 VLESS {i+1}/{len(vless_list)}: {host}")
        ip = start_xray(vless)
        if not ip:
            log(f"  ❌ 代理不通")
            continue
        
        opener, err = login(proxy)
        if opener:
            log(f"  ✅ 登录成功 (IP: {ip})")
            break
        else:
            log(f"  ❌ 登录失败: {err}")
            if err == "Datacenter":
                log(f"     IP 是数据中心, 试下一个")
            elif err == "ANTI_ALT" or err == "Callback":
                # 可能是 Anti-Alt, 但登录本身成功了, 只是 earn 被 403
                # 还是用这个代理, 后面再处理
                log(f"     登录成功但可能 Anti-Alt, 继续用这个")
                opener, _ = login(proxy)
                if opener:
                    break
    
    if not opener:
        log("❌ 所有 VLESS 都失败")
        return 1

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
            new_opener, err = login(proxy)
            if new_opener:
                opener = new_opener
            else:
                log(f"⚠️ 重新登录失败: {err}")

        # POST /api/earn
        status_code, result = report_earn(opener, REPORT_INTERVAL)
        
        if status_code == 200:
            coins = result.get("coins", "?")
            earned = result.get("earned", "?")
            log(f"📊 {elapsed}s ({report_count}): +{earned} coins, total={coins}")
        elif status_code == 403:
            code = result.get("code", "")
            error = result.get("error", "")
            if "ANTI_ALT" in code:
                log(f"❌ {elapsed}s: Anti-Alt, 停止")
                return 1
            elif "oracle_required" in error:
                log(f"⚠️ {elapsed}s: 需要 Turnstile 验证")
            elif "IP_BANNED" in code:
                log(f"❌ {elapsed}s: IP 被封")
                return 1
            else:
                log(f"❌ {elapsed}s: 403 {error}")
        elif status_code == 429:
            log(f"⚠️ {elapsed}s: 限流, 等 60s")
            time.sleep(60)
        else:
            log(f"⚠️ {elapsed}s: HTTP {status_code}: {result}")

        if report_count % 6 == 0:
            status = check_status(opener)
            afk = status.get("afk", {})
            log(f"📊 状态: today={afk.get('todayEarnings', '?')}/{afk.get('dailyCap', '?')}")

    # 最终上报
    data = json.dumps({"duration": REPORT_INTERVAL, "final": True}).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/earn", data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", "https://dash.witchly.host/earn/afk")
    try:
        opener.open(req, timeout=30)
    except:
        pass

    status = check_status(opener)
    afk = status.get("afk", {})
    log(f"=== done, 今日: {afk.get('todayEarnings', '?')}/{afk.get('dailyCap', '?')} ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
