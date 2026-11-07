#!/usr/bin/env python3
"""
Hostrix 免费服务器续期
------------------------
面板规则（实测）：
  · POST /servers/<id>/renew  → 免费时长 +24h
  · 两次之间有 11 小时冷却（页面上显示 "+24h in Xh"）
  · 存量 46h，冷却期一次点击无效（302 回原页，时长不变）

Cookie：Flask 签名会话，服务端每次响应都 Set-Cookie 回写（Expires 30 天），
        每次请求都会滚动出新 cookie → 只要不长期闲置就能永久自动续。

环境变量：
  HOSTRIX_COOKIE   会话 cookie 值（必填）
  HOSTRIX_SERVER   服务器 id，默认 cd86ccde4e34
  HR_FORCE=1       无视倒计时直接点（仅调试用）
"""
import os, re, sys, json, time, urllib.request, urllib.error, http.cookiejar

BASE = "https://hostrix.website"
SID = os.environ.get("HOSTRIX_SERVER", "cd86ccde4e34")
COOKIE = (os.environ.get("HOSTRIX_COOKIE") or "").strip()
FORCE = os.environ.get("HR_FORCE") == "1"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hostrix_state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

if not COOKIE:
    log("❌ 缺少 HOSTRIX_COOKIE")
    sys.exit(2)

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect())

def req(path, data=None, referer=None):
    h = {"User-Agent": UA, "Cookie": f"session={COOKIE}"}
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Referer"] = referer or BASE + f"/servers/{SID}"
    r = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        resp = op.open(r, timeout=30)
        return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location")

def server_page():
    s, b, loc = req(f"/servers/{SID}")
    if s in (301, 302, 303, 307) and loc and "/login" in loc:
        return None, "会话失效（被重定向到登录页）"
    if s != 200:
        return None, f"HTTP {s}"
    info = {}
    m = re.search(r'<form method="post" action="([^"]*/renew)"[^>]*>(.*?)</form>', b, re.S)
    if not m:
        return None, "页面里找不到续期表单"
    inner = m.group(2)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', inner)
    info["action"] = m.group(1)
    info["csrf"] = csrf.group(1) if csrf else ""
    span = re.search(r'<span class="time-left[^"]*"[^>]*>(.*?)</span>', inner, re.S)
    info["left_raw"] = re.sub(r'<[^>]+>', '', span.group(1)).strip() if span else ""
    btn = re.search(r'<button([^>]*)>(.*?)</button>', inner, re.S)
    if btn:
        info["btn_disabled"] = "disabled" in btn.group(1)
        info["btn_text"] = re.sub(r'<[^>]+>', '', btn.group(2)).strip()
        t = re.search(r'title="([^"]*)"', btn.group(1))
        info["btn_title"] = t.group(1) if t else ""
    ttl = re.search(r'(\d+)h\s*left', info["left_raw"])
    cd = re.search(r'in\s*(\d+)h', info.get("btn_text", "") + " " + info.get("btn_title", ""))
    info["hours_left"] = int(ttl.group(1)) if ttl else None
    info["cooldown_h"] = int(cd.group(1)) if cd else 0
    # 抓页面 flash（成功续期时 Flask 会渲染一条提示）
    fl = re.findall(r'<(?:div|p|span)[^>]*class="[^"]*(?:flash|toast|alert|notice)[^"]*"[^>]*>(.*?)</(?:div|p|span)>', b, re.S)
    info["flash"] = " | ".join(re.sub(r'<[^>]+>', '', x).strip() for x in fl if x.strip())[:200]
    return info, None

def save_state(d):
    try:
        st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    except Exception:
        st = {}
    st.update(d)
    json.dump(st, open(STATE, "w"), indent=2, ensure_ascii=False)
    return st

def export_cookie():
    """把服务端滚动出的新 cookie 交给 workflow 写回 secret"""
    out = os.environ.get("GITHUB_OUTPUT")
    val = next((c.value for c in cj if c.name == "session"), None)
    if out and val:
        with open(out, "a") as f:
            f.write(f"cookie<<__EOF__\n{val}\n__EOF__\n")
        log(f"📤 已导出滚动后的 cookie（{len(val)} 字节）")

info, err = server_page()
if err:
    log(f"❌ {err}")
    save_state({"last_error": err, "last_check": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    export_cookie()
    sys.exit(3)

log(f"⏰ 剩余 {info['hours_left']}h · 冷却 {info['cooldown_h']}h · 按钮[{'禁用' if info.get('btn_disabled') else '可点'}] {info.get('btn_text','')}")

if info.get("btn_disabled") and not FORCE:
    log(f"⏳ 冷却中，{info['cooldown_h']}h 后可续 → 本轮不动")
    save_state({"hours_left": info["hours_left"], "cooldown_h": info["cooldown_h"],
                "last_check": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "last_action": "cooldown", "last_error": None})
    export_cookie()
    sys.exit(0)

log("🎯 冷却结束 → POST /renew")
s, b, loc = req(info["action"], data=f"csrf_token={info['csrf']}".encode())
log(f"📡 HTTP {s} → {loc}")

info2, err2 = server_page()
if err2:
    log(f"⚠️ 续期后复查失败: {err2}")
    save_state({"last_action": "renew_unknown", "last_error": err2})
    export_cookie()
    sys.exit(4)

before, after = info["hours_left"], info2["hours_left"]
grew = after is not None and before is not None and after > before
# 时长封顶时：数字不变，但冷却被重置成 11h（按钮重新变灰）→ 也算续期成功
capped = (not grew) and after == before and info2.get("btn_disabled") and info2["cooldown_h"] >= 10
ok = grew or capped
if grew:
    detail = f"成功 +{after - before}h"
elif capped:
    detail = f"已封顶（存量上限 ~{after}h），冷却已重置"
else:
    detail = "未变化"
log(f"{'✅' if ok else '⚠️'} 剩余时长 {before}h → {after}h（{detail}）· 下次冷却 {info2['cooldown_h']}h")
if info2.get("flash"):
    log(f"💬 {info2['flash']}")
save_state({"hours_left": after, "cooldown_h": info2["cooldown_h"],
            "last_check": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_action": "renew" if ok else "renew_nochange",
            "last_success": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None,
            "last_error": None})
export_cookie()
sys.exit(0 if ok else 5)
