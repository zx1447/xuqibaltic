#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCYED Hosting 5~14 天随机续期（真实浏览器自动过 CF 后登录）。"""
from __future__ import annotations
import datetime as dt
import json, os, random, sys, time
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://scyed.com"
LOGIN_URL = f"{BASE}/en/login"
RENEW_URL = f"{BASE}/en/gameserver/6100ef84/upgrade/freeServer"
STATE_FILE = "scyed_state.json"
USER = os.environ.get("SCYED_USER", "").strip()
PASSWORD = os.environ.get("SCYED_PASS", "").strip()
SESSION_KEY = os.environ.get("SCYED_SESSION_KEY", "").strip()
PROXY = os.environ.get("SCYED_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

class ScyedError(RuntimeError): pass

def log(x): print(x, flush=True)
def now_str(): return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def send_tg(msg):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":msg},timeout=15)
        except: pass

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except (FileNotFoundError,ValueError,OSError):return {}
def save_state(st):
    tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_browser_cookies(st,sb):
    if SESSION_KEY:
        cookies=sb.get_cookies()
        if cookies:
            st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(cookies,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore_browser_cookies(st,sb):
    if not SESSION_KEY or not st.get("encrypted_cookies"):return False
    try:cookies=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken,ValueError,TypeError):return False
    try:
        sb.open(BASE);sb.wait_for_ready_state_complete()
        for c in cookies:
            try:sb.add_cookie({k:c[k] for k in ("name","value","domain","path","expiry","secure","httpOnly") if k in c})
            except:pass
        sb.refresh();sb.wait_for_ready_state_complete();time.sleep(2)
        return "login" not in sb.get_current_url().lower() and "attention required" not in sb.get_title().lower()
    except:return False

def should_run(st):
    if FORCE_RUN:return True
    nxt=int(st.get("next_renew_timestamp",0) or 0)
    if nxt and time.time()<nxt:
        log(f"⏳ SCYED 尚未到随机续期时间：{st.get('next_renew_time','未知')}");return False
    return True

def wait_cf_auto(sb):
    try:sb.uc_open_with_reconnect(LOGIN_URL,reconnect_time=6)
    except:sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete()
    # SCYED 的 CF 盾会自动完成，不主动点击，只等待 JS 跳转。
    for i in range(4):
        time.sleep(6)
        title=sb.get_title().lower();source=sb.get_page_source().lower()
        if "just a moment" not in title and "attention required" not in title and "cf-chl-" not in source:
            log(f"✅ SCYED Cloudflare 自动验证通过（等待 {6*(i+1)} 秒）")
            return
        log(f"🛡️ 等待 SCYED Cloudflare 自动验证（第 {i+1}/4 次）")
    raise ScyedError("SCYED Cloudflare 自动验证未通过")

def first_selector(sb, selectors):
    for selector in selectors:
        try:
            if sb.is_element_present(selector) and sb.is_element_visible(selector): return selector
        except: pass
    return None

def login(sb):
    wait_cf_auto(sb)
    email=first_selector(sb,["input[type='email']","input[name='email']","#email","input[name='username']","#username","input[name='user']"])
    password=first_selector(sb,["input[type='password']","input[name='password']","#password"])
    if not email or not password:
        raise ScyedError("Cloudflare 已返回但未找到账号/密码输入框")
    log("🔑 Cloudflare 自动通过，开始填写 SCYED 登录表单")
    sb.type(email,USER,timeout=10);sb.type(password,PASSWORD,timeout=10)
    button=first_selector(sb,["button[type='submit']","input[type='submit']","button.btn-primary","button"])
    if not button:raise ScyedError("未找到 SCYED 登录按钮")
    log("🖱️ 点击 SCYED 登录按钮")
    sb.uc_click(button)
    time.sleep(6)
    if "login" in sb.get_current_url().lower():raise ScyedError("SCYED 登录后仍在登录页")
    log("✅ SCYED 登录成功")

def renew_in_browser(sb):
    sb.open(RENEW_URL);sb.wait_for_ready_state_complete();time.sleep(3)
    script="""
    const done=arguments[arguments.length-1];
    fetch(arguments[0],{method:'POST',credentials:'include',headers:{'Accept':'application/json, text/plain, */*','X-Requested-With':'XMLHttpRequest'}})
      .then(async r=>done({status:r.status,text:await r.text(),url:r.url})).catch(e=>done({error:String(e)}));
    """
    result=sb.execute_async_script(script,RENEW_URL)
    log(f"📡 POST {RENEW_URL} -> HTTP {result.get('status')}")
    log("📦 返回摘要："+result.get('text','')[:500])
    if result.get('status')==401:raise ScyedError("SCYED 登录会话失效")
    if result.get('status')!=200:raise ScyedError(f"SCYED 续期失败：HTTP {result.get('status')}")

def main():
    log("🚀 SCYED 真实浏览器随机续期启动");log(f"🕐 北京时间：{now_str()}")
    if not USER or not PASSWORD:return log("❌ 缺少 SCYED_USER/SCYED_PASS") or 1
    st=load_state()
    if not should_run(st):return 0
    try:
        from seleniumbase import SB
        kw={"uc":True,"xvfb":True,"headless":False}
        if PROXY:kw["proxy"]=PROXY
        with SB(**kw) as sb:
            reused=restore_browser_cookies(st,sb)
            if reused:log("♻️ 复用 SCYED 浏览器登录会话")
            else:login(sb)
            try:renew_in_browser(sb)
            except ScyedError as e:
                if "会话失效" not in str(e):raise
                log("🔐 确认会话失效，重新登录后重试")
                login(sb);renew_in_browser(sb)
            days=random.randint(5,14);nxt=int(time.time()+days*86400)
            st.update({"last_renew_timestamp":int(time.time()),"last_renew_time":now_str(),"next_interval_days":days,"next_renew_timestamp":nxt,"next_renew_time":dt.datetime.fromtimestamp(nxt,dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")})
            save_browser_cookies(st,sb);save_state(st)
            log(f"🎲 下次 SCYED 随机续期：{days} 天后");send_tg(f"✅ SCYED 续期成功\n🕐 {now_str()}\n🎲 下次：{days} 天后");return 0
    except Exception as e:
        log(f"❌ SCYED 续期失败：{type(e).__name__}: {e}");send_tg(f"❌ SCYED 续期失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
