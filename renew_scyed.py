#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCYED Hosting 5~14 天随机续期（账号密码 API 模式）。"""
from __future__ import annotations
import datetime as dt
import json, os, random, sys, time
import requests
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://scyed.com"
LOGIN_URL = f"{BASE}/api/auth/sign-in/email"
SESSION_URL = f"{BASE}/api/auth/get-session"
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
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":msg},timeout=15)
        except: pass

def make_session():
    s=requests.Session();s.trust_env=False
    s.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36","Accept":"application/json, text/plain, */*","Origin":BASE,"Referer":f"{BASE}/en/login"})
    if PROXY:s.proxies.update({"http":PROXY,"https":PROXY});log("🔗 使用 SCYED_PROXY")
    return s

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except (FileNotFoundError,ValueError,OSError):return {}
def save_state(st):
    tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_session(st,s):
    if SESSION_KEY:
        c={x.name:x.value for x in s.cookies}
        if c:st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(c,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore_session(st):
    if not SESSION_KEY or not st.get("encrypted_cookies"):return None
    try:c=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken,ValueError,TypeError):return None
    s=make_session()
    for k,v in c.items():s.cookies.set(k,v,domain="scyed.com",path="/")
    return s

def session_valid(s):
    try:
        r=s.get(SESSION_URL,timeout=20)
        return r.status_code==200 and bool(r.json().get("user") or r.json().get("session"))
    except (requests.RequestException,ValueError):return False

def should_run(st):
    if FORCE_RUN:return True
    nxt=int(st.get("next_renew_timestamp",0) or 0)
    if nxt and time.time()<nxt:
        log(f"⏳ SCYED 尚未到随机续期时间：{st.get('next_renew_time','未知')}");return False
    return True

def login():
    if not USER or not PASSWORD:raise ScyedError("缺少 SCYED_USER/SCYED_PASS")
    s=make_session();log(f"🔑 使用账号密码请求 SCYED 登录：{USER[:2]}****")
    r=s.post(LOGIN_URL,json={"email":USER,"password":PASSWORD,"callbackURL":f"{BASE}/en/gameserver/6100ef84/upgrade/freeServer"},headers={"Content-Type":"application/json","Accept":"application/json"},timeout=25)
    if r.status_code!=200:raise ScyedError(f"SCYED 登录失败：HTTP {r.status_code} {r.text[:300]}")
    if not session_valid(s):raise ScyedError("SCYED 登录返回 200 但 /api/auth/get-session 未登录")
    log("✅ SCYED API 登录成功");return s

def renew(s):
    r=s.post(RENEW_URL,headers={"Accept":"application/json, text/plain, */*","X-Requested-With":"XMLHttpRequest"},allow_redirects=False,timeout=25)
    log(f"📡 POST {RENEW_URL} -> HTTP {r.status_code}")
    if r.status_code in (401,403):raise ScyedError("SCYED 登录会话失效")
    if r.status_code!=200:raise ScyedError(f"SCYED 续期失败：HTTP {r.status_code} {r.text[:300]}")
    log("📦 返回摘要："+r.text[:500]);return r

def main():
    log("🚀 SCYED API 随机续期启动");log(f"🕐 北京时间：{now_str()}")
    st=load_state()
    if not should_run(st):return 0
    try:
        s=restore_session(st)
        if s and session_valid(s):log("♻️ 复用 SCYED API 登录会话")
        else:
            if s:log("⌛ SCYED 已保存会话失效，重新账号密码登录")
            s=login();save_session(st,s)
        try:renew(s)
        except ScyedError as e:
            if "会话失效" not in str(e):raise
            log("🔐 SCYED 会话失效，重新登录后重试");s=login();save_session(st,s);renew(s)
        days=random.randint(5,14);nxt=int(time.time()+days*86400)
        st.update({"last_renew_timestamp":int(time.time()),"last_renew_time":now_str(),"next_interval_days":days,"next_renew_timestamp":nxt,"next_renew_time":dt.datetime.fromtimestamp(nxt,dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")});save_session(st,s);save_state(st)
        log(f"🎲 下次 SCYED 随机续期：{days} 天后");send_tg(f"✅ SCYED 续期成功\n🕐 {now_str()}\n🎲 下次：{days} 天后");return 0
    except Exception as e:
        log(f"❌ SCYED 续期失败：{type(e).__name__}: {e}");send_tg(f"❌ SCYED 续期失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
