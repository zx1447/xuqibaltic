#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host Ship Free 每 4 天自动续期。"""
from __future__ import annotations
import datetime as dt
import json, os, re, sys, time, urllib.parse
import requests
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://panel.host-ship.com"
SERVER_ID = "a3441a1f-30df-411e-9aff-775d182c60f9"
LOGIN_URL = f"{BASE}/auth/login"
CSRF_URL = "https://panel.host-ship.com/sanctum/csrf-cookie"
RENEW_URL = f"{BASE}/api/client/servers/{SERVER_ID}/renew"
STATE_FILE = "shiphsot_state.json"
INTERVAL = 4 * 24 * 60 * 60
USER = os.environ.get("SHIPHSOT_USER", "").strip()
PASSWORD = os.environ.get("SHIPHSOT_PASS", "").strip()
SESSION_KEY = os.environ.get("SHIPHSOT_SESSION_KEY", "").strip()
PROXY = os.environ.get("SHIPHSOT_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

class ShiphsotError(RuntimeError): pass

def log(x): print(x, flush=True)
def now_str(): return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def send_tg(msg):
    if TG_TOKEN and TG_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":msg},timeout=15)
        except: pass

def make_session():
    s=requests.Session();s.trust_env=False;s.headers.update({"User-Agent":UA,"Accept":"application/json, text/plain, */*","Origin":BASE,"Referer":BASE+"/"})
    if PROXY:s.proxies.update({"http":PROXY,"https":PROXY})
    return s

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except (FileNotFoundError,ValueError,OSError):return {}
def save_state(st):
    tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_session(st,s):
    if SESSION_KEY:
        cookies={c.name:c.value for c in s.cookies}
        if cookies:st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(cookies,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore_session(st):
    if not SESSION_KEY or not st.get("encrypted_cookies"):return None
    try:cookies=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken,ValueError,TypeError):return None
    s=make_session()
    for k,v in cookies.items():s.cookies.set(k,v,domain="panel.host-ship.com",path="/")
    return s

def refresh_csrf(s):
    s.get(CSRF_URL,timeout=20)
    page=s.get(LOGIN_URL,timeout=20)
    m=re.search(r'<meta name="csrf-token" content="([^"]+)"',page.text)
    if m:s.headers["X-CSRF-TOKEN"]=m.group(1)
    xsrf=s.cookies.get("XSRF-TOKEN")
    if xsrf:s.headers["X-XSRF-TOKEN"]=urllib.parse.unquote(xsrf)

def session_valid(s):
    try:
        refresh_csrf(s)
        r=s.get(f"{BASE}/api/client/account",allow_redirects=False,timeout=20)
        return r.status_code==200
    except: return False

def login():
    if not USER or not PASSWORD:raise ShiphsotError("缺少账号密码")
    s=make_session();refresh_csrf(s)
    r=s.post(LOGIN_URL,json={"user":USER,"password":PASSWORD,"g-recaptcha-response":""},headers={"Accept":"application/json","Content-Type":"application/json"},allow_redirects=False,timeout=25)
    if r.status_code!=200:raise ShiphsotError(f"Host Ship 登录失败：HTTP {r.status_code}")
    refresh_csrf(s)
    if not session_valid(s):raise ShiphsotError("Host Ship 登录后会话验证失败")
    log("✅ Host Ship 登录成功");return s

def renew(s):
    refresh_csrf(s)
    r=s.post(RENEW_URL,json={},headers={"Accept":"application/json","X-Requested-With":"XMLHttpRequest"},allow_redirects=False,timeout=25)
    log(f"📡 POST /api/client/servers/{SERVER_ID}/renew -> HTTP {r.status_code}")
    if r.status_code in (401,403):raise ShiphsotError("Host Ship 会话失效")
    if r.status_code==400 and "more than 30 days" in r.text.lower():
        log("ℹ️ Host Ship 当前剩余时间已超过 30 天，本次无需续期，等待下一个 4 天检查")
        return False
    if r.status_code!=204:raise ShiphsotError(f"续期失败：HTTP {r.status_code} {r.text[:300]}")
    log("🎉 Host Ship 续期成功（204 No Content）")
    return True

def main():
    log("🚀 Host Ship 每 4 天续期启动");log(f"🕐 北京时间：{now_str()}")
    st=load_state();last=int(st.get("last_renew_timestamp",0) or 0)
    if not FORCE_RUN and last and time.time()-last<INTERVAL:
        log("⏳ 尚未到 4 天续期时间，跳过");return 0
    try:
        s=restore_session(st)
        if s and session_valid(s):log("♻️ 复用 Host Ship 登录会话")
        else:s=login();save_session(st,s)
        try:renewed=renew(s)
        except ShiphsotError as e:
            if "会话失效" not in str(e):raise
            log("🔐 会话失效，重新登录后重试");s=login();save_session(st,s);renewed=renew(s)
        result_text="204 No Content" if renewed else "already exceeds 30 days"
        st.update({"last_renew_timestamp":int(time.time()),"last_renew_time":now_str(),"last_result":result_text});save_session(st,s);save_state(st)
        send_tg(f"✅ Host Ship 检查完成：{result_text}\n🕐 {now_str()}");return 0
    except Exception as e:
        log(f"❌ Host Ship 失败：{type(e).__name__}: {e}");send_tg(f"❌ Host Ship 失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
