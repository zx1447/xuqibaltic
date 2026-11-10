#!/usr/bin/env python3
"""
Hostrix 免费服务器续期（账号密码登录 + 精准定时）
=================================================

面板规则（实测）：
  · 登录：POST /login  {csrf_token, username, password} → 302 /dashboard，无验证码
  · 续期：POST /servers/<id>/renew  {csrf_token}          → 免费时长 +24h
  · 冷却：两次之间 11 小时，期间点击被静默忽略（302 回原页，时长不变）
  · 页面只给整点小时（"⏰ 46h left" / "+24h in 11h"），无秒级时间源

精准策略：
  1. 每次真实点击时记录精确到秒的时间戳 last_click_epoch
  2. next_allowed = last_click_epoch + cooldown（默认 11h，可自校准）
  3. 若 next_allowed 落在本次 job 的可等待窗口内（HR_WAIT_MAX 秒），
     直接 sleep 到边界+几秒再点 —— 秒级精准，无需轮询
  4. 否则退出，由 15 分钟一次的 cron 接力
  5. 边界探测：若到点仍被拒，自动把冷却时间 +5 分钟并短重试，
     结果写回 state 供下轮沿用（自校准漂移）

环境变量：
  HOSTRIX_USER   账号（必填）
  HOSTRIX_PASS   密码（必填）
  HOSTRIX_SERVER 服务器 id，默认 cd86ccde4e34
  HR_WAIT_MAX    单次 job 最多等多少秒，默认 800
  HR_COOLDOWN_H  冷却小时数，默认 11（有 state 时以 state 为准）
  HR_FORCE=1     无视状态直接点（调试）

退出码：0 正常（含冷却未到）· 2 缺凭据 · 3 登录失败 · 4 页面解析失败
        5 点击后时长未变 · 6 会话失效
"""
import os, re, sys, json, time, calendar
import urllib.parse, urllib.request, urllib.error, http.cookiejar
from datetime import datetime, timezone, timedelta

BASE = "https://hostrix.website"
SID = os.environ.get("HOSTRIX_SERVER", "cd86ccde4e34")
USER = (os.environ.get("HOSTRIX_USER") or "").strip()
PASS = os.environ.get("HOSTRIX_PASS") or ""
WAIT_MAX = int(os.environ.get("HR_WAIT_MAX") or 800)
FORCE = os.environ.get("HR_FORCE") == "1"
COOLDOWN_H = float(os.environ.get("HR_COOLDOWN_H") or 11)
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "hostrix_state.json")
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def cst(epoch):
    return datetime.fromtimestamp(epoch, CST).strftime("%m-%d %H:%M:%S")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect())


def req(path, data=None, referer=None):
    h = {"User-Agent": UA}
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Referer"] = referer or BASE + path
    r = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        resp = op.open(r, timeout=30)
        return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location")


SUCCESS_RE = re.compile(r"(?i)(added|renewed|extended|topped up|\+\s*24)")
FAILURE_RE = re.compile(r"(?i)(can add more|can't add|already|too soon|cooldown|wait|not yet)")


def hours_from_flash(text):
    """Parse e.g. 'It has 3d 9h now' when the server-rendered timer is stale."""
    m = re.search(r"(?i)it has\s+(?:(\d+)\s*d\s*)?(\d+)\s*h(?:\s*(\d+)\s*m)?\s+now", text or "")
    if not m:
        return None
    return int(m.group(1) or 0) * 24 + int(m.group(2)) + (1 if int(m.group(3) or 0) else 0)


def cooldown_from_flash(text, fallback_seconds):
    """Use the cooldown explicitly reported by the service, if present."""
    m = re.search(r"(?i)you can add more in\s*(\d+)\s*(hours?|h)\b", text or "")
    return int(m.group(1)) * 3600 if m else fallback_seconds


def flashes(html):
    """精确解析 Flask flash：<div class="flash error">…</div>；退回扫描 toast 容器"""
    hits = re.findall(r'<div[^>]*class="flash(?:\s+([a-z]+))?"[^>]*>(.*?)</div>', html, re.S)
    outl = [(cat or "info", re.sub(r"<[^>]+>", " ", msg).strip()) for cat, msg in hits if msg.strip()]
    if not outl:
        hits = re.findall(r'<div[^>]*class="[^"]*\b(?:toast|alert|notice)\b[^"]*"[^>]*>(.*?)</div>', html, re.S)
        outl = [("info", re.sub(r"<[^>]+>", " ", m).strip()) for m in hits if m.strip()]
    return [(c, re.sub(r"\s+", " ", m)) for c, m in outl]


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    st["last_check_epoch"] = int(time.time())
    json.dump(st, open(STATE, "w"), indent=2, ensure_ascii=False)
    return st


def out(**kw):
    """给 workflow 用"""
    p = os.environ.get("GITHUB_OUTPUT")
    if not p:
        return
    with open(p, "a") as f:
        for k, v in kw.items():
            if v is None:
                continue
            if "\n" in str(v):
                f.write(f"{k}<<__EOF__\n{v}\n__EOF__\n")
            else:
                f.write(f"{k}={v}\n")


# ───────────────────────── 登录 ─────────────────────────
def login():
    s, b, _ = req("/login")
    if s != 200:
        return False, f"GET /login HTTP {s}"
    m = re.search(r'name="csrf_token" value="([^"]+)"', b)
    if not m:
        return False, "登录页没有 csrf_token"
    body = urllib.parse.urlencode({
        "csrf_token": m.group(1), "username": USER, "password": PASS,
    }).encode()
    s, b, loc = req("/login", body, BASE + "/login")
    if s in (301, 302, 303) and loc and "login" not in loc:
        log(f"🔑 登录成功 → {loc}")
        return True, None
    msgs = flashes(b)
    err = " | ".join(m for c, m in msgs if c == "error") or (msgs[0][1] if msgs else "")
    return False, f"登录失败 HTTP {s} {loc or ''} {err}".strip()


# ───────────────────────── 解析服务器页 ─────────────────────────
def read_server():
    s, b, loc = req(f"/servers/{SID}")
    if s in (301, 302, 303, 307):
        return None, f"被重定向到 {loc}（会话失效）"
    if s != 200:
        return None, f"HTTP {s}"
    m = re.search(r'<form[^>]*action="([^"]*/renew)"[^>]*>(.*?)</form>', b, re.S)
    if not m:
        return None, "页面里找不到续期表单"
    action, inner = m.group(1), m.group(2)
    info = {"action": action}
    c = re.search(r'name="csrf_token" value="([^"]+)"', inner)
    info["csrf"] = c.group(1) if c else re.search(
        r'<meta name="csrf-token" content="([^"]+)"', b).group(1)

    span = re.search(r'<span class="time-left[^"]*"[^>]*>(.*?)</span>', inner, re.S)
    raw = re.sub(r"<[^>]+>", "", span.group(1)).strip() if span else ""
    info["left_raw"] = raw
    h = re.search(r"(\d+)\s*h", raw)
    info["hours_left"] = int(h.group(1)) if h else None
    bar = re.search(r'<i style="width:\s*([\d.]+)%', inner)
    info["bar_pct"] = float(bar.group(1)) if bar else None

    btn = re.search(r"<button([^>]*)>(.*?)</button>", inner, re.S)
    info["enabled"] = True
    info["btn_text"] = ""
    info["btn_title"] = ""
    if btn:
        attrs, txt = btn.group(1), re.sub(r"<[^>]+>", "", btn.group(2)).strip()
        info["enabled"] = "disabled" not in attrs
        info["btn_text"] = txt
        t = re.search(r'title="([^"]*)"', attrs)
        info["btn_title"] = t.group(1) if t else ""
    cd = re.search(r"in\s*(\d+)\s*h", info["btn_text"] + " " + info["btn_title"])
    info["cooldown_disp_h"] = int(cd.group(1)) if cd else 0

    msgs = flashes(b)
    info["flash"] = " | ".join(m for _, m in msgs)[:300]
    info["flash_cat"] = msgs[0][0] if msgs else ""

    st = re.search(r'id="status-pill"[^>]*>(.*?)<', b, re.S)
    info["power"] = re.sub(r"<[^>]+>", "", st.group(1)).strip() if st else "?"
    return info, None


def describe(i):
    if not i:
        return "-"
    return (f"剩余 {i['hours_left']}h · 按钮[{'可点' if i['enabled'] else '冷却'}] "
            f"{i['btn_text'] or '-'} · 电源 {i['power']}")


def click(info):
    """真点一次，返回 (ok, 说明)"""
    t0 = int(time.time())
    s, b, loc = req(info["action"], f"csrf_token={info['csrf']}".encode(),
                    BASE + f"/servers/{SID}")
    log(f"📡 POST renew → HTTP {s} → {loc} (t={cst(t0)})")
    after, err = read_server()
    if err:
        return False, f"点击后复查失败: {err}"
    before_h = info["hours_left"]
    after_h = after["hours_left"]
    grew = before_h is not None and after_h is not None and after_h > before_h
    fl = after.get("flash") or ""
    if fl:
        log(f"💬 服务端提示：{fl}")
    if grew:
        return True, f"+{after_h - before_h}h（{before_h}h → {after_h}h）· {fl}".strip(" ·")
    # 成功消息有时也会带下一次冷却提示，例如：
    # “Added 48 hours. It has 3d 9h now. You can add more in 12 hours.”
    # 成功关键词优先级必须高于后半句的 cooldown 文案。
    if SUCCESS_RE.search(fl):
        return True, f"服务端确认续期（页面 {before_h}h → {after_h}h）· {fl}"
    if FAILURE_RE.search(fl):
        return False, f"服务端拒绝：{fl}"
    return False, f"未确认（{before_h}h → {after_h}h，flash={fl or '空'}）"


# ───────────────────────── 主流程 ─────────────────────────
if not USER or not PASS:
    log("❌ 缺少 HOSTRIX_USER / HOSTRIX_PASS")
    sys.exit(2)

log(f"👤 账号 {USER} · 服务器 {SID}")
ok, err = login()
if not ok:
    log(f"❌ {err}")
    save_state(last_error=err, last_action="login_failed")
    out(status="login_failed", error=err)
    sys.exit(3)

info, err = read_server()
if err:
    log(f"❌ {err}")
    save_state(last_error=err, last_action="read_failed")
    out(status="read_failed", error=err)
    sys.exit(4)

st = load_state()
cooldown_s = int(float(st.get("cooldown_h", COOLDOWN_H)) * 3600)
log(f"📊 {describe(info)}")
if info.get("flash"):
    log(f"💬 {info['flash']}")

# 推算精确的"下次可点"时刻
now0 = int(time.time())
last_click = st.get("last_click_epoch")
if info["enabled"]:
    next_allowed = now0
elif last_click:
    # 有真实点击记录 → 秒级精确边界
    next_allowed = int(last_click) + cooldown_s
elif info["cooldown_disp_h"]:
    # 没有记录，页面只给整点小时（"in 11h" 是向下取整）→ 取保守上界
    next_allowed = now0 + info["cooldown_disp_h"] * 3600
else:
    next_allowed = now0 + cooldown_s

if next_allowed > time.time():
    log(f"🎯 下次可点：{cst(next_allowed)}（还有 {int(next_allowed - time.time())}s）")

# ── 需要等待吗 ──
if not info["enabled"] and not FORCE:
    wait = next_allowed - time.time()
    if wait > WAIT_MAX:
        log(f"⏳ 超出本次等待窗口（{int(wait)}s > {WAIT_MAX}s）→ 交给 cron 接力")
        save_state(hours_left=info["hours_left"], cooldown_h=cooldown_s / 3600,
                   next_allowed_epoch=next_allowed, last_action="wait_next_tick")
        out(status="cooldown", hours_left=info["hours_left"],
            next_allowed=next_allowed, action="none")
        sys.exit(0)
    log(f"😴 精准等待 {max(0, int(wait)) + 5}s → 边界 {cst(next_allowed)}")
    time.sleep(max(0, wait) + 5)
    info, err = read_server()
    if err:
        log(f"⚠️ 边界复查失败: {err}（交给 cron）")
        save_state(last_action="probe_error", last_error=err)
        out(status="probe_error", error=err, action="none")
        sys.exit(0)
    log(f"🔍 边界复查：{describe(info)}")
    if not info["enabled"]:
        # 说明服务端冷却比我们的估算长（首次无点击记录时会出现）
        # 按当前页面的整点提示重新取上界，交给下一次 cron，不再盲目空转
        nxt = int(time.time()) + max(1, info["cooldown_disp_h"]) * 3600
        log(f"⏳ 仍在冷却 → 下次尝试 {cst(nxt)}（cron 接力）")
        save_state(hours_left=info["hours_left"], next_allowed_epoch=nxt,
                   last_action="probe_still_cooling")
        out(status="cooldown", hours_left=info["hours_left"],
            next_allowed=nxt, action="none")
        sys.exit(0)

# ── 点击 ──
log("🎯 冷却已过 → 执行续期")
ok, msg = click(info)
now = int(time.time())
if ok:
    after, _ = read_server()
    flash_hours = hours_from_flash(msg)
    page_hours = after["hours_left"] if after else None
    # Prefer an increased page reading; if the page is stale, trust the success flash.
    final_hours = page_hours if page_hours is not None and page_hours > (info["hours_left"] or 0) else (flash_hours if flash_hours is not None else page_hours)
    actual_cooldown_s = cooldown_from_flash(msg, cooldown_s)
    log(f"✅ 续期成功：{msg}")
    save_state(hours_left=final_hours if final_hours is not None else info["hours_left"],
               last_click_epoch=now, next_allowed_epoch=now + actual_cooldown_s,
               cooldown_h=actual_cooldown_s / 3600, last_action="renew",
               last_success_epoch=now, last_error=None)
    log(f"🎯 下一次可点：{cst(now + actual_cooldown_s)}（冷却 {actual_cooldown_s // 3600}h）")
    out(status="renewed", hours_left=final_hours if final_hours is not None else "",
        next_allowed=now + actual_cooldown_s, action="renew", detail=msg)
    sys.exit(0)
else:
    log(f"⚠️ {msg}")
    save_state(hours_left=info["hours_left"], last_action="renew_nochange",
               next_allowed_epoch=now + 900, last_error=msg)
    out(status="nochange", hours_left=info["hours_left"],
        next_allowed=now + 900, action="renew", detail=msg)
    sys.exit(5)
