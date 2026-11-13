#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bkt 免费服自动续期（目标面板常量已 b64 混淆，公开仓库搜不到）
=============================================

机制（全部实测，2026-11-11）：
  · 服务器：面板上的免费 MC 服（Free 节点）
  · GET  /api/v1/servers/game/free/status
        → {secondsRemaining, canRenew, activeUntil, isExpired, deleteAfter}
  · 续期窗口：**到期前 2 小时内**或**过期之后**才允许（canRenew=true）
  · POST /api/v1/servers/game/free/renew   body: {"turnstileToken": "..."}
        → 成功一次 = **+8 小时**
  · Turnstile sitekey：脚本内置（b64），显式渲染，出现"我是人类"勾选框
  · token 一次性、300 秒内有效，解出后必须立刻消费
  · API Key（bh_pat_）**不能**用于续期：只能读状态/电源
  · 会话 cookie 不滑动：better-auth 会话 cookie 硬过期 7 天，
    需定期从浏览器重新抓取（脚本会在 <48h 时告警）

Turnstile 600010 实测结论（2026-11-11，重要）：
  600010 = "数学上解对了，但你看起来像机器人"。已经试过的全部死路：
    · Playwright chromium 无头/有头(Xvfb)，机房 IP 或家宽出口 —— 全部 600010
    · Firefox、Cloudflare WARP —— 600010
  唯一活路 = **家宽出口 + undetected-chromedriver 真浏览器 + 屏幕级真实点击**
  （m-ws 项目同款方案，已长期稳定）。本脚本 browser 解算器已切换到此方案：
    1. 拉家宽订阅接口（运行时拉取，节点轮换自动跟随）→ xray 起 SOCKS5
    2. UC Chrome 挂 SOCKS5 出口注入会话 cookie
    3. 页面内显式渲染 Turnstile → pyautogui 屏幕坐标真点勾选框 → 拿 token
    4. **在浏览器页面里 fetch 提交续期**（token 绑解算出口 IP，必须同出口提交）
    5. 直连复查 +8h 是否到账

解算器（BH_SOLVER 选择）：
  capsolver  付费打码平台 CapSolver（AntiTurnstileTaskProxyLess），任何网络可全自动
  2captcha   付费打码平台 2Captcha（TurnstileTaskProxyless）
  browser    UC 真浏览器 + 家宽订阅出口解算（推荐；BH_PROXY=0 时退化为直连，
             仅住宅网络机器可用）
  manual     只报告状态与窗口时间，不解算（默认兜底）
  auto       有 CapSolver key 用 capsolver，否则 2captcha，再否则 browser，最后 manual

环境变量：
  BH_COOKIE          会话 cookie（必填）。整行 Cookie 值，如
                     __Secure-better-auth.session_token=xxx
  BH_SOLVER          capsolver|2captcha|browser|manual|auto（默认 auto）
  BH_CAPSOLVER_KEY   CapSolver clientKey
  BH_2CAPTCHA_KEY    2Captcha clientKey
  BH_BASE            可选，覆盖默认面板地址（已 b64 内置）
  BH_FORCE=1         无视 canRenew 直接尝试（调试用，会被服务端拒）
  BH_VLESS_URL       可选，vless:// 节点直链（覆盖订阅）
  BH_SUB_URL         可选，家宽订阅接口（默认用脚本内置地址，已 b64）
  BH_PROXY           auto（默认，尽量走家宽出口）| 1（强制，失败即退出码 7）| 0
                     （直连，适合已在住宅网络的机器）
  XRAY_BIN           可选，xray 可执行文件路径（缺省会自动下载到脚本目录）

CLI：
  --gate             只做"会话+窗口探测"，输出 window_open=true/false 后退出
                     （workflow 用它决定要不要装浏览器全家桶，省时间）

退出码：
  0  正常（含"未到窗口，只报告"）
  2  缺环境变量      3  会话失效/快过期被拒      4  解算失败
  5  续期被服务端拒  6  续期后校验未通过        7  家宽出口不可用（BH_PROXY=1）
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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone, timedelta

def _d(s):
    """b64 解码 —— 站点常量混淆用（防公开仓库/日志检索）。"""
    return base64.b64decode(s.encode()).decode()


BASE = (os.environ.get("BH_BASE") or _d("aHR0cHM6Ly9kYXNoLmJsYWNraG9zdC5wbA==")).rstrip("/")
COOKIE = (os.environ.get("BH_COOKIE") or "").strip()
PAT = (os.environ.get("BH_PAT") or "").strip()  # bh_pat_ API key：只读/电源可用，续期不行
SOLVER = (os.environ.get("BH_SOLVER") or "auto").strip().lower()
CAP_KEY = (os.environ.get("BH_CAPSOLVER_KEY") or "").strip()
TC_KEY = (os.environ.get("BH_2CAPTCHA_KEY") or "").strip()
FORCE = os.environ.get("BH_FORCE") == "1"
SITEKEY = _d("MHg0QUFBQUFBQmRyTW5yZ1lQMTl2QXpU")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "bkt_state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 家宽出口
# 家宽订阅接口（b64 内置，防检索；BH_SUB_URL 可覆盖）
DEFAULT_SUB_URL = _d("aHR0cDovLzgzLjE2OC45NC4yMzc6MzAyMDAvYXBpL3Byb3h5L3N1YnNjcmlwdGlvbi9mNGYzMDNiNTJiOWMyN2M1YmUzZjE0MTMzYjAxYjcyZWQ4YjQ2ZjY3M2JiMzFmNzcxZGEwZTg1NzczNWNkM2U0")
# 内置兜底节点（订阅拉不到时用；隧道轮换后可能过期，仅救急）
FALLBACK_VLESS_URL = _d("dmxlc3M6Ly81Y2ViNDFiYS1hNjQ3LTQ2ZjctOGM1MC04MjRlYTUyOTQxYTNAcmFwaWQtb3BlcmF0aW5nLWdyZWF0ZXItdGVybXMudHJ5Y2xvdWRmbGFyZS5jb206NDQzP3R5cGU9d3Mmc2VjdXJpdHk9dGxzJnBhdGg9JTJGc3RhdGljJTJGYXNzZXRzJTJGYzEwZGRkNTA2NTQ3LmpzJnNuaT1yYXBpZC1vcGVyYXRpbmctZ3JlYXRlci10ZXJtcy50cnljbG91ZGZsYXJlLmNvbSZmcD1jaHJvbWUjQW95b3UtUHJveHktVkxFU1M=")

SUB_URL = (os.environ.get("BH_SUB_URL") or "").strip() or DEFAULT_SUB_URL
VLESS_URL = (os.environ.get("BH_VLESS_URL") or "").strip()
PROXY_MODE = (os.environ.get("BH_PROXY") or "auto").strip().lower()
SOCKS_PORT = 10808
XRAY_BIN_ENV = os.environ.get("XRAY_BIN", "").strip()
XRAY_CFG = os.path.join(tempfile.gettempdir(), "bkt_xray_config.json")
XRAY_PROC = None


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def cst(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(CST).strftime("%m-%d %H:%M:%S")
    except Exception:
        return str(iso)


def out(**kw):
    p = os.environ.get("GITHUB_OUTPUT")
    if not p:
        return
    with open(p, "a") as f:
        for k, v in kw.items():
            if v is None:
                continue
            f.write(f"{k}<<__EOF__\n{v}\n__EOF__\n" if "\n" in str(v) else f"{k}={v}\n")


def mask(s):
    if not s:
        return "?"
    return s[:2] + "***"


def mask_ip(ip):
    parts = (ip or "").split(".")
    return ".".join(parts[:2]) + ".x.x" if len(parts) == 4 else "***"


def mask_host(host):
    parts = (host or "").split(".")
    if len(parts) >= 3:
        return parts[0][:4] + "***." + ".".join(parts[-2:])
    return "***"


def api(path, method="GET", body=None, cookie=COOKIE, use_pat=False):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if use_pat and PAT:
        h["Authorization"] = "Bearer " + PAT
    elif cookie:
        h["Cookie"] = cookie
    if data is not None:
        h["Content-Type"] = "application/json"
        h["Origin"] = BASE
        h["Referer"] = BASE + "/"
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        x = urllib.request.urlopen(r, timeout=30)
        raw = x.read().decode("utf-8", "replace")
        return x.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def save_state(**kw):
    try:
        st = {}
        if os.path.exists(STATE):
            st = json.load(open(STATE))
        st.update(kw)
        st["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        json.dump(st, open(STATE, "w"), indent=2, ensure_ascii=False)
    except Exception:
        pass


# ───────────────────────── 会话 ─────────────────────────
def check_session():
    s, b = api("/api/auth/get-session")
    sess = (b or {}).get("session") or {}
    exp = sess.get("expiresAt")
    user = ((b or {}).get("user") or {}).get("email")
    if not exp:
        return None, None
    left = datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now(timezone.utc)
    log(f"🔑 会话有效 · {user} · 到期 {cst(exp)}（剩 {left.total_seconds()/3600:.1f}h）")
    if left < timedelta(hours=48):
        log("⚠️ 会话不足 48 小时，请尽快从浏览器重新抓取 BH_COOKIE！")
    return exp, user


# ───────────────────────── 状态 ─────────────────────────
def get_status():
    # 有 API key 就走 key（省会话配额），没有就用 cookie
    s, b = api("/api/v1/servers/game/free/status", use_pat=bool(PAT))
    if s != 200 or not b.get("success"):
        s, b = api("/api/v1/servers/game/free/status")
    if s != 200 or not b.get("success"):
        return None, f"HTTP {s}: {b}"
    return b.get("freeServer") or {}, None


# ───────────────────────── 家宽出口（订阅 → xray → SOCKS5）────────
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
    if XRAY_BIN_ENV and os.path.isfile(XRAY_BIN_ENV):
        return XRAY_BIN_ENV
    found = shutil.which("xray")
    if found:
        return found
    exe = "xray.exe" if sys.platform.startswith("win") else "xray"
    for cand in (os.path.join(HERE, "xray_bin", exe),
                 "./xray", "/usr/local/bin/xray", "/opt/xray/xray"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def ensure_xray_binary():
    """找不到 xray 时从官方 release 下载到脚本目录（CI workflow 通常已装好）。"""
    b = find_xray()
    if b:
        return b
    if sys.platform.startswith("win"):
        plat, exe = "windows-64", "xray.exe"
    elif sys.platform.startswith("darwin"):
        plat, exe = "macos-64", "xray"
    else:
        plat, exe = "linux-64", "xray"
    dest_dir = os.path.join(HERE, "xray_bin")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, exe)
    if os.path.isfile(dest) and os.access(dest, os.X_OK):
        return dest
    url = f"https://github.com/XTLS/Xray-core/releases/latest/download/Xray-{plat}.zip"
    zpath = dest + ".zip"
    log(f"📦 下载 xray（{plat}）…")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(zpath, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.namelist():
                if os.path.basename(member) == exe:
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break
        os.remove(zpath)
        if sys.platform.startswith("linux") or sys.platform.startswith("darwin"):
            os.chmod(dest, 0o755)
        log("📦 xray 就绪")
    except Exception as exc:
        log(f"⚠️ xray 下载失败: {type(exc).__name__}")
        return None
    return dest if os.path.isfile(dest) else None


def fetch_subscription():
    """拉家宽订阅（base64 节点列表）。地址已 b64 内置，日志中不出现。"""
    raw = None
    for attempt in range(4):
        log(f"📡 拉取家宽订阅 (第 {attempt + 1}/4 次) ...")
        try:
            req = urllib.request.Request(SUB_URL, headers={"User-Agent": "clash-verge/1.6.6"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
            if text:
                raw = text
                break
            log("⚠️ 订阅响应为空")
        except Exception as exc:
            log(f"⚠️ 订阅拉取失败: {type(exc).__name__}")
        time.sleep(5)
    if raw is None:
        return None
    cand = raw.replace("-", "+").replace("_", "/")
    cand += "=" * (-len(cand) % 4)
    try:
        text = base64.b64decode(cand).decode("utf-8")
    except Exception:
        text = raw  # 有些订阅直接吐明文节点列表
    nodes = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("vless://")]
    if nodes:
        log(f"📡 订阅解析成功：{len(nodes)} 条 vless 节点，选用第 1 条")
    else:
        log("⚠️ 订阅里没有 vless:// 节点")
    return nodes or None


def resolve_node():
    """节点解析顺序：BH_VLESS_URL 直链 → 订阅接口第一条 → 内置兜底节点。"""
    if VLESS_URL.startswith("vless://"):
        return VLESS_URL
    if VLESS_URL.startswith(("http://", "https://")):
        global SUB_URL
        SUB_URL = VLESS_URL
    nodes = fetch_subscription()
    if nodes:
        return nodes[0]
    log("⚠️ 订阅连续拉取失败，退回内置兜底节点")
    return FALLBACK_VLESS_URL


def start_xray(node):
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
    log(f"🔗 VLESS 节点: {node['name']} -> {mask_host(node['host'])}:{node['port']}")
    proc = subprocess.Popen(
        [find_xray(), "run", "-c", XRAY_CFG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid if not sys.platform.startswith("win") else None,
    )
    time.sleep(2.5)
    if proc.poll() is not None:
        return None
    return proc


def stop_xray(proc):
    if proc and proc.poll() is None:
        try:
            if sys.platform.startswith("win"):
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass


def check_exit_ip():
    """通过节点查出口 IP（curl 走 SOCKS5）。家宽节点挂了立刻发现。"""
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "20",
                 "--socks5-hostname", f"127.0.0.1:{SOCKS_PORT}",
                 "https://www.cloudflare.com/cdn-cgi/trace"],
                capture_output=True, text=True, timeout=30)
            fields = dict(l.split("=", 1) for l in (r.stdout or "").splitlines()
                          if "=" in l)
            if fields.get("ip"):
                ip, loc = fields["ip"], fields.get("loc", "?")
                log(f"🌍 节点出口 IP: {mask_ip(ip)} ({loc})")
                return ip
        except Exception as exc:
            log(f"⚠️ 出口检查第 {attempt + 1} 次失败: {type(exc).__name__}")
        time.sleep(3)
    return None


def setup_proxy():
    """建立家宽出口。成功返回 True；失败返回 False（BH_PROXY=1 时调用方退出 7）。"""
    global XRAY_PROC
    if PROXY_MODE == "0":
        log("🔒 BH_PROXY=0，直连模式（适合已在住宅网络的机器）")
        return False
    node_url = resolve_node()
    try:
        node = parse_vless(node_url)
    except Exception as exc:
        log(f"⚠️ 节点解析失败: {type(exc).__name__}")
        return False
    if not find_xray() and not ensure_xray_binary():
        log("⚠️ xray 不可用，退回直连")
        return False
    proc = start_xray(node)
    if not proc:
        log("⚠️ xray 启动即退出（节点格式异常），退回直连")
        return False
    XRAY_PROC = proc
    ip = check_exit_ip()
    if not ip:
        stop_xray(XRAY_PROC)
        XRAY_PROC = None
        log("⚠️ 节点不通（隧道可能已轮换且订阅未更新），退回直连")
        return False
    return True


# ───────────────────────── 付费解算器（直连 POST，与打码平台配套）────────
def solve_capsolver():
    log(f"🧠 CapSolver 解算中…（sitekey {SITEKEY[:8]}***）")
    payload = {
        "clientKey": CAP_KEY,
        "task": {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": BASE + "/",
            "websiteKey": SITEKEY,
        },
    }
    req = urllib.request.Request(
        "https://api.capsolver.com/createTask",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        tid = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("taskId")
    except Exception as e:
        log(f"❌ createTask 失败: {e}")
        return None
    if not tid:
        log("❌ CapSolver 未返回 taskId")
        return None
    for i in range(60):
        time.sleep(3)
        req = urllib.request.Request(
            "https://api.capsolver.com/getTaskResult",
            data=json.dumps({"clientKey": CAP_KEY, "taskId": tid}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            res = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            log(f"⚠️ getTaskResult: {e}")
            continue
        st = res.get("status")
        if st == "ready":
            tok = (res.get("solution") or {}).get("token")
            log(f"✅ 解出 token（{len(tok or '')} 字符）")
            return tok
        if st == "failed":
            log(f"❌ CapSolver 失败: {res}")
            return None
    log("❌ CapSolver 超时（180s）")
    return None


def solve_2captcha():
    log(f"🧠 2Captcha 解算中…（sitekey {SITEKEY[:8]}***）")
    payload = {
        "clientKey": TC_KEY,
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": BASE + "/",
            "websiteKey": SITEKEY,
        },
    }
    req = urllib.request.Request(
        "https://api.2captcha.com/createTask",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        tid = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("taskId")
    except Exception as e:
        log(f"❌ createTask 失败: {e}")
        return None
    if not tid:
        log("❌ 2Captcha 未返回 taskId")
        return None
    for i in range(60):
        time.sleep(3)
        req = urllib.request.Request(
            "https://api.2captcha.com/getTaskResult",
            data=json.dumps({"clientKey": TC_KEY, "taskId": tid}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            res = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            log(f"⚠️ getTaskResult: {e}")
            continue
        st = res.get("status")
        if st == "ready":
            tok = (res.get("solution") or {}).get("token")
            log(f"✅ 解出 token（{len(tok or '')} 字符）")
            return tok
        if st == "failed":
            log(f"❌ 2Captcha 失败: {res}")
            return None
    log("❌ 2Captcha 超时（180s）")
    return None


def renew(token):
    body = {"turnstileToken": token} if token else {}
    s, b = api("/api/v1/servers/game/free/renew", "POST", body)
    ok = (s == 200 and isinstance(b, dict) and b.get("success"))
    msg = b.get("error") or b.get("message") or b if isinstance(b, dict) else b
    return ok, s, msg, b


# ───────────────────────── UC 真浏览器解算 ─────────────────────────
JS_SETUP_WIDGET = """
var cb = arguments[arguments.length - 1];
(async () => {
  const out = {};
  try {
    const me = await (await fetch("/api/auth/get-session", {credentials: "include"})).json();
    if (!me || !me.user) { out.error = "browser: not logged in"; return cb(JSON.stringify(out)); }
    out.user = me.user.email || me.user.name || "user";
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
    box.id = '__bh_widget';
    const left = Math.max(10, Math.round((window.innerWidth - 300) / 2));
    const top = Math.max(10, Math.round((window.innerHeight - 65) / 2));
    box.style.cssText = `position:fixed;left:${left}px;top:${top}px;width:300px;height:65px;`
      + `z-index:2147483647;background:#fff;border:2px solid #e33`;
    document.body.appendChild(box);
    window.turnstile.render(box, {
      sitekey: "__SITEKEY__",
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
""".replace("__SITEKEY__", SITEKEY)

JS_GEOMETRY = """
var cb = arguments[arguments.length - 1];
var box = document.getElementById('__bh_widget');
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

# token 绑解算出口 IP —— 必须在同一个浏览器会话里（同 cookie + 同 IP）提交
JS_SUBMIT_RENEW = """
var cb = arguments[arguments.length - 1];
(async () => {
  const res = await fetch("/api/v1/servers/game/free/renew", {
    method: "POST",
    credentials: "include",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({turnstileToken: window.__tk}),
  });
  cb(JSON.stringify({status: res.status, body: await res.text()}));
})();
"""

SESSION_COOKIE_NAME = _d("X19TZWN1cmUtYmV0dGVyLWF1dGguc2Vzc2lvbl90b2tlbg==")


def session_cookie_value():
    """从整行 Cookie 里精确提取 session_token；若整行就是 name=value 也能兜住。"""
    m = re.search(re.escape(SESSION_COOKIE_NAME) + r"=([^;\s]+)", COOKIE)
    if m:
        return m.group(1)
    name, _, val = COOKIE.partition("=")
    return val.strip() if val.strip() else COOKIE


def detect_chrome_major():
    """探测本机 Chrome 主版本 —— 好让 uc 下载匹配的 chromedriver。"""
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium",
                 "chromium-browser"):
        path = shutil.which(name)
        if not path:
            continue
        try:
            outp = subprocess.run([path, "--version"], capture_output=True,
                                  text=True, timeout=15).stdout or ""
            m = re.search(r"(\d+)\.", outp)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def browser_renew_uc():
    """UC 真浏览器（家宽出口）：解 Turnstile + 页面内提交续期。返回 (ok, info)。"""
    try:
        import undetected_chromedriver as uc
    except Exception:
        return False, "缺 undetected-chromedriver（pip install undetected-chromedriver）"
    try:
        import pyautogui
    except Exception:
        return False, "缺 pyautogui（pip install pyautogui python-xlib）"
    log("🌐 启动 undetected-chromedriver（挂 SOCKS5 家宽出口）...")
    opts = uc.ChromeOptions()
    opts.add_argument(f"--proxy-server=socks5://127.0.0.1:{SOCKS_PORT}")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--window-position=0,0")
    # 无 GPU 环境必须开软件 WebGL，否则 Turnstile 挑战永远卡加载
    opts.add_argument("--enable-unsafe-swiftshader")
    opts.add_argument("--use-angle=swiftshader")
    kwargs = {"options": opts}
    major = detect_chrome_major()
    if major:
        log(f"    Chrome 主版本 {major}，chromedriver 按此版本下载")
        kwargs["version_main"] = major
    driver = uc.Chrome(**kwargs)
    try:
        driver.get(BASE + "/")
        time.sleep(3)
        cval = session_cookie_value()
        driver.execute_cdp_cmd("Network.setCookie", {
            "name": SESSION_COOKIE_NAME, "value": cval,
            "url": BASE + "/", "secure": True, "httpOnly": True, "path": "/",
        })
        driver.get(BASE + "/")
        time.sleep(6)

        setup = json.loads(driver.execute_async_script(JS_SETUP_WIDGET, 45))
        slog = dict(setup)
        if slog.get("user"):
            slog["user"] = mask(str(slog["user"]))
        log(f"    widget setup: {json.dumps(slog, ensure_ascii=False)[:200]}")
        if not setup.get("rendered"):
            return False, f"Turnstile 渲染失败: {setup.get('error', '未知')}（cookie 失效?）"

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


def pick_solver():
    if SOLVER == "capsolver":
        return "capsolver" if CAP_KEY else None
    if SOLVER == "2captcha":
        return "2captcha" if TC_KEY else None
    if SOLVER in ("browser", "manual"):
        return SOLVER
    # auto
    if CAP_KEY:
        return "capsolver"
    if TC_KEY:
        return "2captcha"
    try:
        import undetected_chromedriver  # noqa: F401
        return "browser"
    except Exception:
        return "manual"


# ───────────────────────── 主流程 ─────────────────────────
def main():
    gate_only = "--gate" in sys.argv
    if not COOKIE:
        log("❌ 缺少 BH_COOKIE（浏览器 F12 → 请求标头 → Cookie 整行）")
        return 2
    log(f"🎯 bkt 免费服续期 · solver={SOLVER} · proxy={PROXY_MODE}")
    exp, user = check_session()
    if not exp:
        log("❌ 会话无效（get-session 无返回）——BH_COOKIE 过期或抄错")
        return 3

    fs, err = get_status()
    if err:
        log(f"❌ 取状态失败: {err}")
        return 3
    secs = fs.get("secondsRemaining")
    can = fs.get("canRenew")
    exp_at = fs.get("activeUntil")
    expired = fs.get("isExpired")
    log(f"📊 {mask(str(fs.get('name') or '?'))} · 状态 {fs.get('status')}"
        f" · 剩余 {secs/3600:.2f}h · 到期 {cst(exp_at)}"
        f" · canRenew={can} · expired={expired}")

    if gate_only:
        window = bool(can) or FORCE
        out(window_open="true" if window else "false",
            seconds_remaining=secs, active_until=exp_at)
        if not window:
            open_at = cst((datetime.fromisoformat(exp_at.replace("Z", "+00:00"))
                           - timedelta(hours=2)).astimezone(CST))
            log(f"⏳ [gate] 未到续期窗口 —— 北京时间 {open_at} 开窗，本轮不装浏览器")
            save_state(action="gate_wait", seconds_remaining=secs,
                       active_until=exp_at, can_renew=False)
        else:
            log("✅ [gate] 续期窗口已开，开始装浏览器并解算")
            save_state(action="gate_window", seconds_remaining=secs,
                       active_until=exp_at, can_renew=True)
        return 0

    if not can and not FORCE:
        open_at = cst((datetime.fromisoformat(exp_at.replace("Z", "+00:00"))
                       - timedelta(hours=2)).astimezone(CST))
        log(f"⏳ 未到续期窗口 —— 窗口在北京时间 {open_at} 打开（到期前 2h），届时再来")
        save_state(action="wait", seconds_remaining=secs, active_until=exp_at,
                   can_renew=False)
        out(action="wait", seconds_remaining=secs, active_until=exp_at)
        return 0

    solver = pick_solver()
    if solver == "manual" or solver is None:
        log("⏳ 已到续期窗口，但没有可用解算器（BH_SOLVER=manual 或无 key/无浏览器）")
        log("   → 全自动请配 BH_CAPSOLVER_KEY / BH_2CAPTCHA_KEY；"
            "或 pip install undetected-chromedriver pyautogui 后跑 BH_SOLVER=browser；"
            "或手动去面板点续期")
        save_state(action="window_open_no_solver", seconds_remaining=secs,
                   active_until=exp_at, can_renew=True)
        out(action="window_open_no_solver", seconds_remaining=secs,
            active_until=exp_at)
        return 0

    # ---- capsolver / 2captcha：打码平台出 token，直连 POST（旧通道）----
    if solver in ("capsolver", "2captcha"):
        solve = {"capsolver": solve_capsolver, "2captcha": solve_2captcha}[solver]
        token = solve()
        if not token:
            save_state(action="solve_failed", solver=solver, can_renew=True)
            out(action="solve_failed", solver=solver)
            return 4
        ok, code, msg, raw = renew(token)
        if not ok:
            log(f"❌ 续期被拒 HTTP {code}: {msg}")
            save_state(action="renew_rejected", solver=solver, error=str(msg))
            out(action="renew_rejected", error=str(msg))
            return 5
        log(f"🎉 续期请求成功：{json.dumps(raw, ensure_ascii=False)[:200]}")
        time.sleep(4)
        fs2, err2 = get_status()
        if not err2:
            secs2 = fs2.get("secondsRemaining")
            gain = (secs2 - secs) / 3600 if isinstance(secs2, (int, float)) else None
            log(f"✅ 校验：剩余 {secs/3600:.2f}h → {secs2/3600:.2f}h"
                f"（+{gain:.2f}h）· 新到期 {cst(fs2.get('activeUntil'))}")
            save_state(action="renewed", solver=solver, before_seconds=secs,
                       after_seconds=secs2, active_until=fs2.get("activeUntil"),
                       gain_hours=gain, last_renew=datetime.now(timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"))
            out(action="renewed", solver=solver, gain_hours=gain,
                active_until=fs2.get("activeUntil"), seconds_remaining=secs2)
            return 0
        save_state(action="renewed_unverified", solver=solver)
        out(action="renewed_unverified", solver=solver)
        return 0

    # ---- browser：UC 真浏览器 + 家宽出口，解算与提交都在浏览器里 ----
    proxy_ok = False
    try:
        proxy_ok = setup_proxy()
        if (not proxy_ok) and PROXY_MODE == "1":
            log("❌ BH_PROXY=1 且家宽出口建立失败 —— 按要求中止")
            save_state(action="proxy_failed", solver="browser")
            out(action="proxy_failed", solver="browser")
            return 7
        if not proxy_ok:
            log("⚠️ 无家宽出口，退回直连 —— 机房 IP 大概率 600010")
        ok, info = browser_renew_uc()
        if not ok:
            save_state(action="solve_failed", solver="browser", proxy=proxy_ok,
                       error=info)
            out(action="solve_failed", solver="browser", error=info)
            return 4
        log(f"🎉 续期请求成功：{info}")
        time.sleep(4)
        fs2, err2 = get_status()
        if not err2:
            secs2 = fs2.get("secondsRemaining")
            gain = (secs2 - secs) / 3600 if isinstance(secs2, (int, float)) else None
            log(f"✅ 校验：剩余 {secs/3600:.2f}h → {secs2/3600:.2f}h"
                f"（+{gain:.2f}h）· 新到期 {cst(fs2.get('activeUntil'))}")
            save_state(action="renewed", solver="browser", proxy=proxy_ok,
                       before_seconds=secs, after_seconds=secs2,
                       active_until=fs2.get("activeUntil"), gain_hours=gain,
                       last_renew=datetime.now(timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"))
            out(action="renewed", solver="browser", gain_hours=gain,
                active_until=fs2.get("activeUntil"), seconds_remaining=secs2)
            return 0
        save_state(action="renewed_unverified", solver="browser", proxy=proxy_ok)
        out(action="renewed_unverified", solver="browser")
        return 0
    finally:
        if XRAY_PROC:
            stop_xray(XRAY_PROC)
            globals()["XRAY_PROC"] = None


def sync_state_git():
    """把 bkt_state.json 推到 bkt 分支（2026-11-13 加）。

    背景：--loop 一轮守 5.5 小时，而 state 只在循环**结束**时由 workflow 推送，
    中途所有续期结果都不落远端 —— 面板上看到的永远是 5 小时前的旧快照，
    万一收尾那次 push 失败，整轮战绩连记录都没有。
    这里每轮循环结束就同步一次：fetch + rebase + push，失败只告警不中断守护。
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return  # 本地跑不动 git，跳过
    import subprocess

    def run(*args):
        return subprocess.run(args, capture_output=True, text=True)

    try:
        if not os.path.exists(STATE):
            return
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        run("git", "add", os.path.basename(STATE))
        if run("git", "diff", "--cached", "--quiet").returncode == 0:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run("git", "commit", "-m", f"Update bkt state [skip ci] {ts}")
        for i in range(1, 4):
            run("git", "fetch", "-q", "origin", "bkt")
            run("git", "rebase", "origin/bkt")
            r = run("git", "push", "origin", "HEAD:bkt")
            if r.returncode == 0:
                log(f"💾 [loop] state 已同步到 bkt（第 {i} 次尝试）")
                return
            run("git", "rebase", "--abort")
            time.sleep(i * 2)
        log("⚠️ [loop] state 同步失败（已重试 3 次），下一轮再试")
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ [loop] state 同步异常: {type(e).__name__}: {e}")


def loop(minutes):
    """长驻守护：每 20 分钟复查一次，窗口一开立即解算续期。

    背景（2026-11-13 事故）：GitHub schedule 调度饿死——20 分钟一轮的 cron
    实际只送进来 ~4 次/天，窗口开了没人来，服务器眼睁睁过期。故改长驻循环：
    一次触发守 ~5.5 小时，结束由 workflow 自派发接力（workflow_dispatch
    不受 schedule 饿死影响），schedule 只作兜底。
    """
    deadline = time.time() + minutes * 60
    cycles = 0
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            rc = main()
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 本轮异常: {type(e).__name__}: {e}")
            rc = 9
        sync_state_git()
        if rc in (2, 3):
            log("— [loop] 鉴权/配置类错误，停止守护 —")
            return rc
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1200, rest)
        log(f"— [loop] 休眠 {t / 60:.0f} 分钟后复查 —")
        time.sleep(t)
    log("— [loop] 本轮预算用尽，交棒下一次运行 —")
    return 0


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            mins = float(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            mins = 330.0
        sys.exit(loop(mins))
    sys.exit(main())
