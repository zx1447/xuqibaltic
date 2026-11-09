#!/usr/bin/env python3
"""
目标站 bot 自动续期 —— VLESS 家宽出口 + 真浏览器过 Turnstile。
（站点域名 / OAuth client_id / cookie 名等常量全部 base64 混淆，仓库内不留明文）

为什么这么做（2026-11-09 实测结论）：
  1. 新站续期接口 POST /api/bots/{id}/renew 必须携带 Cloudflare Turnstile token
     （空 body 会 403 {"code":"captcha_required"}），纯 HTTP 无法伪造。
  2. GitHub runner / 数据中心 IP 直连容易被盾盯上，走自家 VLESS 节点（家宽出口）稳。
  3. Turnstile 的挑战 iframe 位于 closed shadow root，SeleniumBase 的
     uc_gui_click_captcha 找不到它；必须用「页面几何换算 + pyautogui 真点击」。
  4. 无 GPU 环境要给 Chrome 加 --enable-unsafe-swiftshader，否则 WebGL 起不来，
     挑战会永远卡在加载（页面上 Three.js 场景也会崩）。

流程：
  a. 解析 VLESS URL（MWS_VLESS_URL，默认用内置的那条）-> 生成 xray 配置 -> 启动 xray
  b. requests 走 SOCKS5 验证出口 IP（家宽/住宅 IP 直接打日志，方便排查）
  c. 登录态：优先 mws_state.json 里加密保存的 cookie；失效则用 Discord token 走
     OAuth 链（auth/login -> discord authorize -> auth/callback -> /api/auth/exchange）
  d. 启动 undetected-chromedriver（挂 SOCKS5），CDP 注入 cookie
  e. 页面内显式渲染 Turnstile（300x65 固定容器，action=renew），等挑战 frame 就绪后
     pyautogui 真实点击复选框，轮询拿 token
  f. 页面内 fetch POST /api/bots/{id}/renew {turnstile_token} -> 200 即续期成功
  g. 对比续期前后 last_renewed_at，Telegram 通知，保存登录态

需要的环境变量（GitHub secrets）：
  MWS_DISCORD_TOKEN  Discord 用户 token（登录用，推荐）
  MWS_VLESS_URL      可选，覆盖内置 VLESS 节点（trycloudflare 隧道换域名后更新它）
  MWS_BOT_ID         可选，默认 60
  MWS_SESSION_KEY    可选，Fernet key；不配则不持久化登录态
  MWS_COOKIE         可选，直接注入的登录 cookie 值（备用）
  TG_BOT_TOKEN / TG_CHAT_ID  可选，Telegram 通知
  XRAY_BIN           可选，xray 可执行文件路径（默认从 PATH 找）
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

import requests

try:
    import socks  # noqa: F401  # pysocks，让 requests 支持 socks5h
except ImportError:
    pass

try:
    import undetected_chromedriver as uc
except ImportError:
    sys.exit("缺少 undetected-chromedriver，请 `pip install undetected-chromedriver`")

try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    pyautogui = None

# ---------------------------------------------------------------- 常量（站点相关全部 base64 混淆）
def _d(s):
    return base64.b64decode(s.encode()).decode()

DEFAULT_VLESS_URL = _d(
    "dmxlc3M6Ly82Yjg5Yzk0Mi1hNGU4LTQxZDgtYjdkYS0xMTFmMzRjZGZlMzFAcnNzLWVtcGxveW1lbnQtcm9nZXItcHJvc3BlY3RpdmUudHJ5Y2xvdWRmbGFyZS5jb206NDQzP2VuY3J5cHRpb249bm9uZSZzZWN1cml0eT10bHMmc25pPXJzcy1lbXBsb3ltZW50LXJvZ2VyLXByb3NwZWN0aXZlLnRyeWNsb3VkZmxhcmUuY29tJmZwPWNocm9tZSZ0eXBlPXdzJnBhdGg9JTJGc3RhdGljJTJGYXNzZXRzJTJGMmQ0M2YwMDk2ODg4LmpzI0NGLVZMRVNTJTIwbGV2"
)
FRONT = os.environ.get("MWS_FRONT", _d("aHR0cHM6Ly9jbG91ZC5tLXdzLmNj")).rstrip("/")
API = f"{FRONT}/api"
CLIENT_ID = _d("MTUwODAzNDA4NDM3NzQ2NDkwMw==")
REDIRECT_URI = _d("aHR0cHM6Ly9jbG91ZC1hcGkubS13cy5jYy9hdXRoL2NhbGxiYWNr")
AUTH_LOGIN = _d("aHR0cHM6Ly9jbG91ZC1hcGkubS13cy5jYy9hdXRoL2xvZ2lu")
COOKIE_NAME = _d("X19Ib3N0LW1ydGNsb3VkX3Rva2Vu")
STATE_FILE = "mws_state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")

SOCKS_PORT = 10808
XRAY_CFG = os.path.join(tempfile.gettempdir(), "mws_xray_config.json")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
VLESS_URL = os.environ.get("MWS_VLESS_URL", "").strip() or DEFAULT_VLESS_URL
DISCORD_TOKEN = os.environ.get("MWS_DISCORD_TOKEN", "").strip()
STATIC_COOKIE = os.environ.get("MWS_COOKIE", "").strip()
SESSION_KEY = os.environ.get("MWS_SESSION_KEY", "").strip()
BOT_ID = int(os.environ.get("MWS_BOT_ID", "60"))
XRAY_BIN = os.environ.get("XRAY_BIN", "").strip()
CHROMEDRIVER = os.environ.get("MWS_CHROMEDRIVER", "").strip()

PROXIES = {"http": f"socks5h://127.0.0.1:{SOCKS_PORT}",
           "https": f"socks5h://127.0.0.1:{SOCKS_PORT}"}


def log(msg):
    print(msg, flush=True)


def send_tg(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as exc:
        log(f"⚠️ Telegram 发送失败: {type(exc).__name__}: {exc}")


def die(message, code=1):
    log(f"❌ {message}")
    send_tg(f"🇯🇵 bot{BOT_ID} 续期失败\n\n{message}")
    sys.exit(code)


# ---------------------------------------------------------------- xray / VLESS
def parse_vless(url):
    """vless://uuid@host:port?params#name -> xray outbound 配置"""
    assert url.startswith("vless://"), "不是 vless:// 链接"
    body = url[len("vless://"):]
    frag = ""
    if "#" in body:
        body, frag = body.split("#", 1)
    userinfo, _, hostport_query = body.partition("@")
    query = ""
    if "?" in hostport_query:
        hostport, _, query = hostport_query.partition("?")
    else:
        hostport = hostport_query
    host, _, port = hostport.partition(":")
    q = dict(urllib.parse.parse_qsl(query))
    sni = q.get("sni") or host
    path = q.get("path") or "/"
    return {
        "uuid": userinfo,
        "host": host,
        "port": int(port or 443),
        "sni": sni,
        "path": path,
        "fp": q.get("fp") or "chrome",
        "name": urllib.parse.unquote(frag) or "vless",
    }


def find_xray():
    if XRAY_BIN and os.path.isfile(XRAY_BIN):
        return XRAY_BIN
    found = shutil.which("xray")
    if found:
        return found
    for cand in ("./xray", "/usr/local/bin/xray", "/opt/xray/xray"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def start_xray():
    node = parse_vless(VLESS_URL)
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "socks-in", "listen": "127.0.0.1", "port": SOCKS_PORT,
            "protocol": "socks", "settings": {"udp": False},
        }],
        "outbounds": [
            {
                "tag": "proxy", "protocol": "vless",
                "settings": {"vnext": [{
                    "address": node["host"], "port": node["port"],
                    "users": [{"id": node["uuid"], "encryption": "none", "flow": ""}],
                }]},
                "streamSettings": {
                    "network": "ws", "security": "tls",
                    "tlsSettings": {"serverName": node["sni"], "fingerprint": node["fp"]},
                    "wsSettings": {
                        "path": node["path"],
                        "headers": {"Host": node["host"]},
                    },
                },
            },
            {"tag": "direct", "protocol": "freedom", "settings": {}},
        ],
        "routing": {"rules": [{"type": "field", "outboundTag": "direct",
                               "ip": ["geoip:private"]}]},
    }
    with open(XRAY_CFG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    binary = find_xray()
    if not binary:
        die("找不到 xray 可执行文件（workflow 会先装好；本地测试请 XRAY_BIN=/path/to/xray）")
    log(f"🔗 VLESS 节点: {node['name']} -> {node['host']}:{node['port']} (sni={node['sni']})")
    proc = subprocess.Popen(
        [binary, "run", "-c", XRAY_CFG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(2.5)
    if proc.poll() is not None:
        die("xray 启动即退出，请检查 VLESS 链接格式")
    return proc


def stop_xray(proc):
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            proc.terminate()


def check_exit_ip():
    """通过节点查出口 IP —— 家宽节点挂了立刻发现（trycloudflare 换域名）。"""
    for attempt in range(3):
        try:
            r = requests.get("https://www.cloudflare.com/cdn-cgi/trace",
                             proxies=PROXIES, timeout=20)
            fields = dict(line.split("=", 1) for line in r.text.splitlines() if "=" in line)
            ip, loc = fields.get("ip", "?"), fields.get("loc", "?")
            log(f"🌍 节点出口 IP: {ip} ({loc})  warp={fields.get('warp', '?')}")
            return ip
        except Exception as exc:
            log(f"⚠️ 出口检查第 {attempt + 1} 次失败: {type(exc).__name__}")
            time.sleep(3)
    die("VLESS 节点不通 —— trycloudflare 隧道可能已更换域名，"
        "请更新 MWS_VLESS_URL 后手动触发 workflow")


# ---------------------------------------------------------------- 登录态
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data):
    if not SESSION_KEY:
        log("⚠️ 未配置 MWS_SESSION_KEY，本次不持久化登录态")
        return
    try:
        from cryptography.fernet import Fernet
        encrypted = Fernet(SESSION_KEY.encode()).encrypt(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"encrypted_cookie": encrypted,
                       "saved_time": time.strftime("%Y-%m-%d %H:%M:%S")},
                      fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        log("💾 登录态已加密保存")
    except Exception as exc:
        log(f"⚠️ 保存登录态失败: {type(exc).__name__}: {exc}")


def decrypt_cookie(state):
    if not SESSION_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        encrypted = state.get("encrypted_cookie")
        if not encrypted:
            return None
        raw = Fernet(SESSION_KEY.encode()).decrypt(encrypted.encode()).decode()
        data = json.loads(raw)
        return data.get("cookie") if isinstance(data, dict) else None
    except Exception:
        return None


def api_headers(cookie):
    return {
        "User-Agent": UA, "Accept": "application/json",
        "Cookie": f"{COOKIE_NAME}={cookie}",
        "Referer": f"{FRONT}/", "Origin": FRONT,
    }


def api_me(cookie):
    try:
        r = requests.get(f"{API}/auth/me", headers=api_headers(cookie),
                         proxies=PROXIES, timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        log(f"⚠️ auth/me 异常: {type(exc).__name__}")
    return None


def oauth_login():
    """Discord token -> OAuth 全链 -> 新 cookie。2026-11-09 实测通过的顺序。"""
    if not DISCORD_TOKEN:
        return None
    log("🔑 使用 Discord token 走 OAuth 登录链 ...")
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    try:
        # 1) auth/login -> state
        r = s.get(AUTH_LOGIN,
                  allow_redirects=False, timeout=30)
        loc = r.headers.get("location", "")
        m = re.search(r"[?&]state=([^&]+)", loc)
        if r.status_code != 302 or not m:
            log(f"⚠️ auth/login 未按预期 302: {r.status_code}")
            return None
        state = urllib.parse.unquote(m.group(1))

        # 2) Discord authorize（参数放 query，body 固定 authorize:true —— 站方验证过的格式）
        auth_url = (f"https://discord.com/api/v9/oauth2/authorize"
                    f"?client_id={CLIENT_ID}&response_type=code"
                    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
                    f"&scope=identify&state={urllib.parse.quote(state, safe='')}")
        r = s.post(auth_url, data=json.dumps({"authorize": True, "integration_type": 0}),
                   headers={"Authorization": DISCORD_TOKEN,
                            "Content-Type": "application/json",
                            "Origin": "https://discord.com",
                            "Referer": "https://discord.com/"},
                   timeout=30)
        loc_final = r.headers.get("location", "") or ""
        try:
            loc_final = loc_final or (r.json() or {}).get("location", "")
        except ValueError:
            pass
        if not loc_final:
            log(f"⚠️ Discord authorize 失败: {r.status_code} {r.text[:200]}")
            return None
        code_m = re.search(r"[?&]code=([^&]+)", loc_final)
        if not code_m:
            log("⚠️ authorize 响应里没有 code（token 可能已失效）")
            return None

        # 3) callback -> 302 /auth/success?code=<exchange_code>
        if loc_final.startswith("/"):
            loc_final = "https://discord.com" + loc_final
        r = s.get(loc_final, allow_redirects=False, timeout=30)
        loc2 = r.headers.get("location", "")
        cm = re.search(r"[?&]code=([^&]+)", loc2)
        if not cm:
            log("⚠️ callback 未返回 exchange code")
            return None
        ex_code = urllib.parse.unquote(cm.group(1))

        # 4) exchange -> 登录 cookie（body 里同时带 token）
        r = s.post(f"{API}/auth/exchange", json={"code": ex_code},
                   headers={"Origin": FRONT, "Referer": f"{FRONT}/auth/success",
                            "Content-Type": "application/json"}, timeout=30)
        if r.status_code != 200:
            log(f"⚠️ exchange 失败: {r.status_code}")
            return None
        cookie = None
        try:
            cookie = (r.json() or {}).get("token")
        except ValueError:
            pass
        if not cookie:
            mc = re.search(re.escape(COOKIE_NAME) + r"=([^;]+)",
                           r.headers.get("set-cookie", "") or "")
            cookie = mc.group(1) if mc else None
        return cookie
    except Exception as exc:
        log(f"⚠️ OAuth 链异常: {type(exc).__name__}: {exc}")
        return None


def get_login_cookie():
    state = load_state()
    saved = decrypt_cookie(state)
    if saved:
        me = api_me(saved)
        if me:
            log(f"♻️ 复用保存的登录态（用户 {me.get('username')}）")
            return saved, me
        log("⚠️ 保存的登录态已失效，改走 Discord OAuth")
    if STATIC_COOKIE:
        me = api_me(STATIC_COOKIE)
        if me:
            log(f"🍪 使用注入的静态 cookie（用户 {me.get('username')}）")
            return STATIC_COOKIE, me
    cookie = oauth_login()
    if cookie:
        me = api_me(cookie)
        if me:
            log(f"✅ OAuth 登录成功（用户 {me.get('username')}）")
            return cookie, me
    die("没有可用的登录方式（Discord token / cookie 全部失败）")


# ---------------------------------------------------------------- 浏览器续期
JS_HOOK_NOOP = "var cb = arguments[arguments.length - 1]; cb('ok');"

JS_SETUP_WIDGET = """
var cb = arguments[arguments.length - 1];
(async () => {
  const out = {};
  try {
    const me = await (await fetch("/api/auth/me", {credentials: "include"})).json();
    if (!me || !me.username) { out.error = "browser: not logged in"; return cb(JSON.stringify(out)); }
    out.username = me.username;
    out.sitekey = me.turnstile_site_key;
    if (!out.sitekey) { out.error = "no sitekey"; return cb(JSON.stringify(out)); }

    if (!window.turnstile) {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
        s.onload = resolve;
        s.onerror = () => reject(new Error("turnstile api.js load failed"));
        document.head.appendChild(s);
      });
    }

    window.__tk = null; window.__tke = null;
    const box = document.createElement('div');
    box.id = '__mws_widget';
    const left = Math.max(10, Math.round((window.innerWidth - 300) / 2));
    const top = Math.max(10, Math.round((window.innerHeight - 65) / 2));
    box.style.cssText = `position:fixed;left:${left}px;top:${top}px;width:300px;height:65px;`
      + `z-index:2147483647;background:#fff;border:2px solid #e33`;
    document.body.appendChild(box);
    window.turnstile.render(box, {
      sitekey: out.sitekey,
      action: 'renew',
      callback: (t) => { window.__tk = t; },
      'error-callback': (c) => { window.__tke = String(c); },
      'expired-callback': () => { window.__tke = 'expired'; },
    });
    out.rendered = true;
    cb(JSON.stringify(out));
  } catch (e) {
    out.error = String(e && e.message || e);
    cb(JSON.stringify(out));
  }
})();
"""

JS_GEOMETRY = """
var cb = arguments[arguments.length - 1];
var box = document.getElementById('__mws_widget');
if (!box) { cb(JSON.stringify({error: 'widget box missing'})); return; }
var r = box.getBoundingClientRect();
cb(JSON.stringify({
  left: r.left, top: r.top, width: r.width, height: r.height,
  screenX: window.screenX, screenY: window.screenY,
  outerH: window.outerHeight, innerH: window.innerHeight
}));
"""

JS_PROBE_TOKEN = """
var cb = arguments[arguments.length - 1];
cb(JSON.stringify({t: window.__tk || null, e: window.__tke || null}));
"""

JS_SUBMIT_RENEW = """
var cb = arguments[arguments.length - 1];
(async () => {
  const res = await fetch("/api/bots/__BOT__/renew", {
    method: "POST",
    credentials: "include",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({turnstile_token: window.__tk}),
  });
  cb(JSON.stringify({status: res.status, body: await res.text()}));
})();
""".replace("__BOT__", str(BOT_ID))


def detect_chrome_major():
    """探测本机 Chrome 主版本 —— 好让 uc 下载匹配的 chromedriver（否则会拉最新版导致版本不匹配）。"""
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=15).stdout or ""
            m = re.search(r"(\d+)\.", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def browser_renew(cookie):
    """浏览器环节：注入 cookie -> 渲染 Turnstile -> 真点击 -> 提交续期。返回 (ok, info)。"""
    if pyautogui is None:
        return False, "缺少 pyautogui（pip install pyautogui python-xlib）"
    log("🌐 启动 undetected-chromedriver（挂 SOCKS5 家宽出口）...")
    opts = uc.ChromeOptions()
    opts.add_argument(f"--proxy-server=socks5://127.0.0.1:{SOCKS_PORT}")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--window-position=0,0")
    # 无 GPU 环境必须开软件 WebGL，否则 Turnstile 挑战永远卡加载
    opts.add_argument("--enable-unsafe-swiftshader")
    opts.add_argument("--use-angle=swiftshader")
    kwargs = {"options": opts}
    if CHROMEDRIVER:
        kwargs["driver_executable_path"] = CHROMEDRIVER
    else:
        major = detect_chrome_major()
        if major:
            log(f"    检测到 Chrome 主版本 {major}，chromedriver 按此版本下载")
            kwargs["version_main"] = major
    driver = uc.Chrome(**kwargs)
    try:
        driver.get(FRONT + "/login")
        time.sleep(3)
        driver.execute_cdp_cmd("Network.setCookie", {
            "name": COOKIE_NAME, "value": cookie,
            "url": FRONT + "/", "secure": True, "httpOnly": True, "path": "/",
        })
        driver.get(FRONT + "/")
        time.sleep(6)

        setup = json.loads(driver.execute_async_script(JS_SETUP_WIDGET, 45))
        log(f"    widget setup: {json.dumps(setup, ensure_ascii=False)[:200]}")
        if not setup.get("rendered"):
            return False, f"Turnstile 渲染失败: {setup.get('error', '未知')}"

        token = None
        st = {}
        for attempt in range(6):
            time.sleep(3)
            try:
                geom = json.loads(driver.execute_async_script(JS_GEOMETRY, 20))
            except Exception as exc:
                log(f"    geometry 读取失败: {type(exc).__name__}")
                time.sleep(4)
                continue
            chrome_top = max(0, geom["outerH"] - geom["innerH"])
            wx = int(geom["screenX"] + geom["left"] + 34)
            wy = int(geom["screenY"] + chrome_top + geom["top"] + 32)
            log(f"    🖱️ 第 {attempt + 1} 次点击复选框 ({wx}, {wy}) [chrome_top={chrome_top}]")
            pyautogui.click(wx, wy)
            for _ in range(5):
                time.sleep(3)
                st = json.loads(driver.execute_async_script(JS_PROBE_TOKEN, 20))
                if st.get("t"):
                    token = st["t"]
                    break
                if st.get("e"):
                    break
            if token or st.get("e"):
                break
        if not token:
            return False, f"未拿到 Turnstile token（widget error: {st.get('e', '无，疑似挑战未完成')}）"
        log(f"    ✅ Turnstile token 到手 (len={len(token)})")

        r2 = json.loads(driver.execute_async_script(JS_SUBMIT_RENEW, 30))
        log(f"    📩 续期响应: HTTP {r2.get('status')} {str(r2.get('body'))[:200]}")
        return r2.get("status") == 200, f"HTTP {r2.get('status')} {str(r2.get('body'))[:200]}"
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def fetch_timer(cookie):
    try:
        r = requests.get(f"{API}/bots", headers=api_headers(cookie),
                         proxies=PROXIES, timeout=25)
        data = r.json()
        for b in data.get("bots") or []:
            if int(b.get("id", -1)) == BOT_ID:
                return b.get("timer") or {}
    except Exception:
        pass
    return {}


def mask(s):
    if not s:
        return "?"
    if len(s) <= 8:
        return s[:2] + "****"
    return s[:6] + "****" + s[-4:]


def main():
    log("#" * 34)
    log(f"   bot{BOT_ID} 自动续期（VLESS 家宽 + 真浏览器）")
    log("#" * 34)

    xray_proc = start_xray()
    try:
        check_exit_ip()

        cookie, me = get_login_cookie()
        before = fetch_timer(cookie)
        log(f"🕒 续期前: last_renewed_at={before.get('last_renewed_at')} "
            f"remaining={before.get('remaining_hours')}h stop_at={before.get('stop_at')}")

        ok, info = browser_renew(cookie)
        if not ok:
            save_state({"cookie": cookie})
            die(f"浏览器续期环节失败: {info}")

        after = fetch_timer(cookie)
        renewed = (after.get("last_renewed_at") != before.get("last_renewed_at")
                   or (after.get("remaining_hours") or 0) >= (before.get("remaining_hours") or 0) + 160)
        if renewed:
            save_state({"cookie": cookie})
            msg = (f"🇯🇵 bot{BOT_ID} 续期成功\n\n"
                   f"👤 账户: {mask(me.get('username', ''))}\n"
                   f"📅 新到期: {after.get('stop_at')}\n"
                   f"⏱️ 续期时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"🔗 出口: VLESS 家宽节点\n{info}")
            log(msg)
            send_tg(msg)
            sys.exit(0)
        die(f"续期请求已发出但 timer 未变化: {info}")
    finally:
        stop_xray(xray_proc)


if __name__ == "__main__":
    main()
