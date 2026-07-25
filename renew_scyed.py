#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCYED 免费服务器 5~14 天随机续期。"""
from __future__ import annotations
import datetime as dt
import json, os, random, re, sys, time
from seleniumbase import SB
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
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

class ScyedError(RuntimeError): pass
def log(x): print(x, flush=True)
def now_str(): return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def send_tg(msg):
    if TG_TOKEN and TG_CHAT_ID:
        try: import requests; requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":msg},timeout=15)
        except: pass

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except (FileNotFoundError,ValueError,OSError):return {}
def save_state(st):
    tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_cookies(st,sb):
    if SESSION_KEY:
        c=sb.get_cookies()
        if c: st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(c,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore_cookies(st,sb):
    if not SESSION_KEY or not st.get("encrypted_cookies"):return False
    try:c=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken,ValueError,TypeError):return False
    try:
        sb.open(BASE);sb.wait_for_ready_state_complete()
        for x in c:
            try:sb.add_cookie({k:x[k] for k in ("name","value","domain","path","expiry","secure","httpOnly") if k in x})
            except:pass
        sb.refresh();sb.wait_for_ready_state_complete();time.sleep(2)
        return "login" not in sb.get_current_url().lower()
    except:return False

def should_run(st):
    if FORCE_RUN:return True
    nxt=int(st.get("next_renew_timestamp",0) or 0)
    if nxt and time.time()<nxt:
        log(f"⏳ SCYED 尚未到下一次随机续期时间：{st.get('next_renew_time','未知')}");return False
    return True

def pass_cf(sb):
    try:sb.uc_open_with_reconnect(LOGIN_URL,reconnect_time=6)
    except:sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete();time.sleep(6)
    if "just a moment" in sb.get_title().lower():
        try:sb.uc_gui_click_captcha();time.sleep(8)
        except:pass
    if "just a moment" in sb.get_title().lower():raise ScyedError("SCYED Cloudflare Challenge 未通过")

def login(sb):
    pass_cf(sb)
    if "login" not in sb.get_current_url().lower():return
    email_sel='input[type="email"], input[name="email"], input[name="username"], input[name="login"]'
    if not sb.is_element_present(email_sel):raise ScyedError("未找到 SCYED 账号输入框")
    sb.type(email_sel,USER,timeout=10)
    sb.type('input[type="password"], input[name="password"]',PASSWORD,timeout=10)
    if sb.is_element_visible('button[type="submit"]'):sb.uc_click('button[type="submit"]')
    elif sb.is_element_visible('input[type="submit"]'):sb.uc_click('input[type="submit"]')
    else:raise ScyedError("未找到 SCYED 登录按钮")
    time.sleep(6)
    if "login" in sb.get_current_url().lower():raise ScyedError("SCYED 登录后仍在登录页")
    log("✅ SCYED 登录成功")

def post_renew(sb):
    script="""
    const done=arguments[arguments.length-1];
    fetch(arguments[0],{method:'POST',credentials:'include',headers:{'Accept':'application/json, text/plain, */*','X-Requested-With':'XMLHttpRequest'}})
      .then(async r=>done({status:r.status,text:await r.text(),url:r.url})).catch(e=>done({error:String(e)}));
    """
    result=sb.execute_async_script(script,RENEW_URL)
    log(f"📡 POST {RENEW_URL} -> HTTP {result.get('status')}")
    log("📦 返回："+result.get('text','')[:500])
    if result.get('status')!=200:raise ScyedError(f"SCYED 续期失败：HTTP {result.get('status')}")

def main():
    log("🚀 SCYED 随机续期启动");log(f"🕐 北京时间：{now_str()}")
    if not USER or not PASSWORD:return log("❌ 缺少 SCYED_USER/SCYED_PASS") or 1
    st=load_state()
    if not should_run(st):return 0
    kw={"uc":True,"xvfb":True,"headless":False}
    if PROXY:kw["proxy"]=PROXY
    try:
        with SB(**kw) as sb:
            reused=restore_cookies(st,sb)
            if reused:log("♻️ 复用 SCYED 登录会话")
            else:login(sb)
            post_renew(sb)
            days=random.randint(5,14);next_ts=int(time.time()+days*86400)
            st.update({"last_renew_timestamp":int(time.time()),"last_renew_time":now_str(),"next_interval_days":days,"next_renew_timestamp":next_ts,"next_renew_time":dt.datetime.fromtimestamp(next_ts,dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")})
            save_cookies(st,sb);save_state(st)
            log(f"🎲 下次 SCYED 随机续期：{days} 天后")
            send_tg(f"✅ SCYED 续期成功\n🕐 {now_str()}\n🎲 下次：{days} 天后");return 0
    except Exception as e:
        log(f"❌ SCYED 续期失败：{type(e).__name__}: {e}");send_tg(f"❌ SCYED 续期失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
