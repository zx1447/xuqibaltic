#!/usr/bin/env python3
"""
Witchly AFK - Playwright 浏览器方式
用真实浏览器打开 AFK 页面, JS 自动 POST /api/earn
"""
import json, os, sys, re, time, subprocess, urllib.request, urllib.parse, http.cookiejar
from playwright.sync_api import sync_playwright

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
VLESS_URL = os.getenv("DP_VLESS_URL", "")
PROXY = "http://127.0.0.1:10809"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
CLIENT_ID = "1463750742786572443"
REDIRECT_URI = "https://dash.witchly.host/api/auth/callback/discord"
SCOPE = "identify email guilds.join"
RUN_DURATION = int(os.getenv("RUN_DURATION", "19800"))


def log(msg):
    print(f"[witchly] {msg}", flush=True)


def start_xray():
    if not VLESS_URL:
        log("❌ no DP_VLESS_URL")
        return None
    uuid = re.sub(r'^vless://([^@]+)@.*', r'\1', VLESS_URL)
    host = re.sub(r'^vless://[^@]+@([^/?]+).*', r'\1', host_port := re.sub(r'^vless://[^@]+@([^/?]+).*', r'\1', VLESS_URL)).split(':')[0]
    query = re.sub(r'.*\?([^#]*)#?.*', r'\1', VLESS_URL)
    path = '/'
    for part in query.split('&'):
        if part.startswith('path='):
            path = part[5:].replace('%2F', '/')
    config = {"log":{"loglevel":"warning"},"inbounds":[{"tag":"socks","port":10808,"listen":"127.0.0.1","protocol":"socks","settings":{"udp":True}},{"tag":"http","port":10809,"listen":"127.0.0.1","protocol":"http","settings":{}}],"outbounds":[{"tag":"vless","protocol":"vless","settings":{"vnext":[{"address":host,"port":443,"users":[{"id":uuid,"encryption":"none","flow":""}]}]},"streamSettings":{"network":"ws","security":"tls","tlsSettings":{"serverName":host,"fingerprint":"chrome","allowInsecure":False},"wsSettings":{"path":path,"headers":{"Host":host}}}},{"tag":"direct","protocol":"freedom","settings":{}}],"routing":{"rules":[{"type":"field","outboundTag":"direct","ip":["127.0.0.0/8","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"]}]}}
    with open("/tmp/xray-w.json", "w") as f:
        json.dump(config, f)
    os.system("pkill -9 -f xray 2>/dev/null; sleep 1")
    subprocess.Popen(["/tmp/xray", "run", "-c", "/tmp/xray-w.json"], stdout=open("/tmp/xray-w.log","w"), stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(3)
    try:
        ph = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
        op = urllib.request.build_opener(ph)
        with op.open(urllib.request.Request("https://api.ipify.org", headers={"User-Agent": UA}), timeout=10) as r:
            return r.read().decode()
    except:
        return None


def oauth_login():
    ph = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(ph, urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request("https://dash.witchly.host/api/auth/csrf", headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        csrf = json.loads(r.read().decode())["csrfToken"]
    sd = urllib.parse.urlencode({"csrfToken": csrf, "callbackUrl": "/earn/afk", "json": "true"}).encode()
    req = urllib.request.Request("https://dash.witchly.host/api/auth/signin/discord", data=sd, method="POST", headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req, timeout=30) as r:
        ru = json.loads(r.read().decode())["url"]
    st = urllib.parse.parse_qs(urllib.parse.urlparse(ru).query)["state"][0]
    au = f"https://discord.com/api/v9/oauth2/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&scope={urllib.parse.quote(SCOPE)}&state={st}"
    req = urllib.request.Request(au, method="POST", headers={"Authorization": DISCORD_TOKEN, "Content-Type": "application/json", "User-Agent": UA})
    body = json.dumps({"authorize": True, "permissions": "0", "integration_type": 0}).encode()
    with opener.open(req, data=body, timeout=30) as r:
        loc = json.loads(r.read().decode())["location"]
    req = urllib.request.Request(loc, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        if "error=" in r.url:
            m = re.search(r"error=(\w+)", r.url)
            return None, m.group(1) if m else "unknown"
    cookies = [{"name": c.name, "value": c.value, "domain": ".witchly.host", "path": "/", "secure": True} for c in cj if "witchly" in c.domain]
    return cookies, None


def main():
    log("=== Witchly AFK (Playwright) ===")
    if not DISCORD_TOKEN:
        log("ERROR: missing DISCORD_TOKEN")
        return 1
    log("1. 启动 xray...")
    ip = start_xray()
    if not ip:
        log("❌ 代理失败")
        return 1
    log(f"  IP: {ip}")
    log("2. OAuth 登录...")
    cookies, err = oauth_login()
    if not cookies:
        log(f"❌ 登录失败: {err}")
        return 1
    log(f"  ✅ {len(cookies)} cookies")
    log("3. Playwright 打开 AFK...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": PROXY}, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
        for c in cookies:
            try: ctx.add_cookies([c])
            except: pass
        page = ctx.new_page()
        earn_count = 0
        def on_resp(resp):
            nonlocal earn_count
            if "/api/earn" in resp.url and resp.request.method == "POST":
                earn_count += 1
                try: log(f"  📡 earn POST → {resp.status}: {resp.text()[:80]}")
                except: pass
        page.on("response", on_resp)
        page.goto("https://dash.witchly.host/earn/afk", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        if "blocked" in page.url:
            log(f"❌ blocked: {page.url}")
            browser.close()
            return 1
        log(f"  ✅ AFK 页面: {page.url}")
        st = page.evaluate("""async()=>{try{const r=await fetch('/api/earn/status');return await r.json()}catch(e){return{error:e.message}}}""")
        afk = st.get("afk", {})
        log(f"  📊 今日: {afk.get('todayEarnings','?')}/{afk.get('dailyCap','?')}")
        log("4. 持续挂机...")
        start = time.time()
        while time.time() - start < RUN_DURATION:
            time.sleep(300)
            el = int(time.time() - start)
            try:
                if "blocked" in page.url or "signin" in page.url:
                    log(f"❌ {el}s: {page.url}")
                    break
                st = page.evaluate("""async()=>{try{const r=await fetch('/api/earn/status');return await r.json()}catch(e){return{error:e.message}}}""")
                afk = st.get("afk", {})
                log(f"📊 {el}s: 今日 {afk.get('todayEarnings','?')}/{afk.get('dailyCap','?')}, earn POSTs: {earn_count}")
            except Exception as e:
                log(f"⚠️ {el}s: {e}")
                try:
                    page.goto("https://dash.witchly.host/earn/afk", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
                except:
                    break
        try:
            st = page.evaluate("""async()=>{const r=await fetch('/api/earn/status');return await r.json()}""")
            afk = st.get("afk", {})
            log(f"=== done, 今日: {afk.get('todayEarnings','?')}/{afk.get('dailyCap','?')}, earn POSTs: {earn_count} ===")
        except:
            log("=== done ===")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
