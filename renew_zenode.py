#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zenode 服务器活跃确认：接近到期时 POST confirm-activity。"""
from __future__ import annotations
import datetime as dt
import json, os, sys, time, urllib.parse
import requests
from cryptography.fernet import Fernet, InvalidToken

BASE="https://zenode.fr"
API_BASE=BASE
SERVER_ID="7acc4eeb-dc3f-4bd7-b90a-413b02d14223"
AUTH_URL=f"{BASE}/auth/discord"
SERVER_URL=f"{API_BASE}/api/hosting/servers/{SERVER_ID}"
CONFIRM_URL=f"{API_BASE}/api/hosting/servers/{SERVER_ID}/confirm-activity"
DISCORD_API="https://discord.com/api/v9"
STATE_FILE="zenode_state.json"
TOKEN=os.environ.get("ZENODE_DISCORD_TOKEN","").strip()
SESSION_KEY=os.environ.get("ZENODE_SESSION_KEY","").strip()
PROXY=os.environ.get("ZENODE_PROXY","").strip()
TG_TOKEN=os.environ.get("TG_BOT_TOKEN","").strip();TG_CHAT_ID=os.environ.get("TG_CHAT_ID","").strip()
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

class ZenodeError(RuntimeError):pass
def log(x):print(x,flush=True)
def now_str():return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def send_tg(m):
 if TG_TOKEN and TG_CHAT_ID:
  try:requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":m},timeout=15)
  except:pass
def sess():
 s=requests.Session();s.trust_env=False;s.headers.update({"User-Agent":UA,"Accept":"application/json, text/plain, */*","Origin":BASE,"Referer":f"{BASE}/app"})
 if PROXY:s.proxies.update({"http":PROXY,"https":PROXY})
 return s
def load():
 try:
  with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
 except:return {}
def save(st):
 tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_session(st,s):
 if SESSION_KEY:
  c={x.name:x.value for x in s.cookies}
  if c:st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(c,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore(st):
 if not SESSION_KEY or not st.get("encrypted_cookies"):return None
 try:c=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
 except (InvalidToken,ValueError,TypeError):return None
 s=sess()
 for k,v in c.items():s.cookies.set(k,v,domain="zenode.fr",path="/")
 return s
def valid(s):
 try:return s.get(SERVER_URL,timeout=20).status_code==200
 except:return False
def oauth():
 if not TOKEN:raise ZenodeError("缺少 ZENODE_DISCORD_TOKEN")
 s=sess();e=s.get(AUTH_URL,allow_redirects=False,timeout=20);loc=e.headers.get("location","")
 if e.status_code not in (301,302,303,307,308) or "discord.com" not in loc:raise ZenodeError(f"Zenode OAuth 入口失败：HTTP {e.status_code}")
 q=urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
 d=requests.Session();d.trust_env=False
 a=d.post(f"{DISCORD_API}/oauth2/authorize",params={k:v[0] for k,v in q.items()},json={"permissions":"0","authorize":True,"integration_type":0,"location_context":{"guild_id":"10000","channel_id":"10000","channel_type":10000}},headers={"Authorization":TOKEN,"Content-Type":"application/json","Origin":"https://discord.com","Referer":loc,"Accept":"*/*"},allow_redirects=False,timeout=25)
 if a.status_code!=200:raise ZenodeError(f"Discord OAuth 失败：HTTP {a.status_code}")
 cb=a.json().get("location","")
 if not cb:raise ZenodeError("OAuth 没有返回 Callback")
 s.get(cb,allow_redirects=True,timeout=25)
 if not valid(s):raise ZenodeError("Zenode Callback 后服务器 API 仍未授权")
 log("✅ Zenode Discord 登录成功");return s
def server(s):
 r=s.get(SERVER_URL,timeout=20)
 if r.status_code in (401,403):raise ZenodeError("Zenode 登录会话失效")
 if r.status_code!=200:raise ZenodeError(f"服务器状态失败：HTTP {r.status_code}")
 return r.json().get("server",r.json())
def confirm(s):
 r=s.post(CONFIRM_URL,json={},headers={"Accept":"application/json","Content-Type":"application/json"},timeout=20)
 log(f"📡 POST confirm-activity -> HTTP {r.status_code}")
 if r.status_code in (401,403):raise ZenodeError("Zenode 登录会话失效")
 if r.status_code!=200:raise ZenodeError(f"活跃确认失败：HTTP {r.status_code} {r.text[:300]}")
 data=r.json();log("📦 返回摘要："+json.dumps({k:data.get(k) for k in ("ok","message","activity_confirmation") if k in data},ensure_ascii=False)[:800]);return data
def main():
 log("🚀 Zenode 服务器活跃确认启动");log(f"🕐 北京时间：{now_str()}")
 st=load()
 try:
  s=restore(st)
  if s and valid(s):log("♻️ 复用 Zenode 登录会话")
  else:s=oauth();save_session(st,s)
  info=server(s);activity=info.get("activity_confirmation") or {};hours=activity.get("hours_remaining")
  days=activity.get("days_remaining")
  log(f"🕒 Zenode 剩余活跃时间：{days} 天 / {hours} 小时")
  # 只在最后 2 天确认，避免无意义重复请求。
  due=False
  if isinstance(hours,(int,float)):due=hours<=48
  elif isinstance(days,(int,float)):due=days<=2
  elif activity.get("expires_at"):
   try:due=(dt.datetime.fromisoformat(activity["expires_at"].replace("Z","+00:00"))-dt.datetime.now(dt.timezone.utc)).total_seconds()<=48*3600
   except:pass
  if due:
   confirm(s);st["last_confirm_time"]=now_str();st["last_confirm_timestamp"]=int(time.time())
  else:log("ℹ️ 还没到最后 2 天，暂不确认活跃")
  save_session(st,s);save(st);send_tg(f"✅ Zenode 检查完成\n🕐 {now_str()}\n📅 剩余：{days} 天");return 0
 except Exception as e:
  log(f"❌ Zenode 失败：{type(e).__name__}: {e}");send_tg(f"❌ Zenode 失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
