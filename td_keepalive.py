#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teradata ClearScape 哪吒探针保活 + 自动唤醒 + 到期重建 + 进程保活（纯 HTTP，无浏览器依赖）

链路:
  1. Firebase Auth REST 密码登录 -> idToken
  2. Firestore 读 users/{email} 文档 -> 后端 API token
  3. GET /environments 检查环境状态
     - 环境不存在（到期被平台回收）        -> 重建
     - createDate + 60 天 <= 3 天          -> 主动重建（删旧建新，同配置）
     - STOPPING/STOPPED                    -> 等待 STOPPED 后 PATCH start（幂等重发）
     - RUNNING                             -> Jupyter /api/status 探活
  4. 面板上报确认:
     - 已上报   -> ensure_probe_processes 轻量确认（缺 watchdog 则拉起）
     - 未上报   -> ensure_probe_processes（kernel exec 拉起 watchdog/agent/v4proxy）
                   等 2 分钟仍无上报 -> 环境 stop→start 重建
                   重建 RUNNING 后再次 ensure_probe_processes（容器重启进程全清，必须重拉）
  5. 重建后自动重部署哪吒探针（kernel websocket 执行部署脚本）
  6. Vantage 活动: 每轮 kernel 执行真实 SQL（SELECT + keepalive_log 写入）并
     PUT notebook 文件 —— 清零平台「48h 无 Vantage 查询 / 2h 无 Jupyter 活动」
     两个空闲计数器，防止被 Teradata 强制暂停（2026-11-11 nzprobe 被暂停的根因）

教训（2026-11-10 掉线复盘）:
  - 环境状态机: STOPPING 状态 PATCH start 会 400；必须等 STOPPED 再 start，
    且 wait_running 里要在 STOPPED 时幂等重发 start（修 20:53 卡死 900s 超时）。
  - stop→start 重建会重启容器、清空所有进程；旧代码重建后从不重拉探针，
    导致"环境 RUNNING 但面板零上报"无限循环，每轮重建都白做。

secrets:
  TD_EMAIL / TD_PASSWORD        Teradata 账号
  TD_ENV_PASSWORD               重建环境时设置的实例密码
  NZ_SECRET                     哪吒面板 agent 密钥
  NZ_PANEL_USER / NZ_PANEL_PASS 哪吒面板登录（改名兜底用）
用法:
  python td_keepalive.py            # 常规保活
  python td_keepalive.py --dry-run  # 只登录+读状态，不做任何变更
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

import websocket  # websocket-client

FB_KEY = "AIzaSyCV9siLw4jczQC09_hW7bTOCbIW3JJce7A"
FB_SIGNIN = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + FB_KEY
FIRESTORE = ("https://firestore.googleapis.com/v1/projects/clearscape-production"
             "/databases/(default)/documents/users/")
API = "https://api.trial.teradata.com/environments"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "td_state.json")
ASSETS = os.path.join(HERE, "td_assets")

ENV_TTL_DAYS = 60
REBUILD_AHEAD_DAYS = 3
AGENT_VER = "v2.4.0"
AGENT_MD5 = "c0ed36cf91888b2ac1a7be500903b368"

PANEL_ADDR = "nz.zxydk1715.dpdns.org:443"
PANEL_HTTP = "https://nz.zxydk1715.dpdns.org"
NODE_NAME = "Teradata 免费PaaS"

DRY = "--dry-run" in sys.argv
STATE = json.load(open(STATE_FILE))
AGENT_UUID = STATE["agent_uuid"]
REBUILD_EPOCH = ""


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def http(method, url, headers=None, body=None, timeout=40, raw=False):
    """返回 (status, text_or_bytes)；HTTPError 不抛出"""
    h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        b = r.read()
        return r.status, (b if raw else b.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        b = e.read()
        return e.code, (b if raw else b.decode("utf-8", "replace"))


def jhttp(method, url, jwt, body=None, timeout=40):
    st, txt = http(method, url, {"Authorization": "Bearer " + jwt}, body, timeout)
    try:
        return st, json.loads(txt)
    except Exception:
        return st, txt


# ---------------------------------------------------------------- 登录链
def login_api_token(email, pwd):
    st, r = http("POST", FB_SIGNIN,
                 {"Content-Type": "application/json"},
                 {"email": email, "password": pwd, "returnSecureToken": True})
    if st != 200:
        raise RuntimeError("Firebase login HTTP %s: %s" % (st, str(r)[:200]))
    idt = json.loads(r)["idToken"]
    log("Firebase 登录 OK (idToken %d chars)" % len(idt))

    st, d = http("GET", FIRESTORE + urllib.parse.quote(email.lower()),
                 {"Authorization": "Bearer " + idt})
    if st != 200:
        raise RuntimeError("Firestore read HTTP %s: %s" % (st, str(d)[:200]))
    fields = json.loads(d).get("fields", {})
    tok = fields.get("token", {}).get("stringValue", "")
    if not tok:
        raise RuntimeError("Firestore users/%s 无 token 字段: keys=%s" % (email, list(fields)[:10]))
    meta = {k: (next(iter(v.values())) if isinstance(v, dict) and v else v)
            for k, v in fields.items() if k in ("envCount", "currentConsumption", "resetDate", "profileType")}
    log("Firestore token OK (len=%d) 用户信息: %s" % (len(tok), json.dumps(meta, ensure_ascii=False)))
    return tok


# ---------------------------------------------------------------- 环境操作
def list_envs(tok):
    st, d = jhttp("GET", API, tok)
    if st != 200:
        raise RuntimeError("list environments HTTP %s: %s" % (st, str(d)[:200]))
    return d if isinstance(d, list) else d.get("items", [])


def start_env(tok, name):
    st, d = jhttp("PATCH", API + "/" + name, tok, {"operation": "start"})
    log("PATCH start %s -> HTTP %s %s" % (name, st, str(d)[:120]))
    return st


def stop_env(tok, name):
    st, d = jhttp("PATCH", API + "/" + name, tok, {"operation": "stop"})
    log("PATCH stop %s -> HTTP %s %s" % (name, st, str(d)[:120]))
    return st


def delete_env(tok, name):
    st, d = jhttp("DELETE", API + "/" + name, tok)
    log("DELETE %s -> HTTP %s %s" % (name, st, str(d)[:120]))
    return st


def create_env(tok, name, password, region):
    st, d = jhttp("POST", API, tok, {"name": name, "password": password, "region": region})
    log("POST create %s(%s) -> HTTP %s %s" % (name, region, st, str(d)[:200]))
    return st


def wait_running(tok, name, timeout=1200, interval=30, auto_start=True):
    """等待 RUNNING。状态机: STOPPING -> (等) -> STOPPED -> (auto_start 时幂等重发 start)
    -> STARTING -> RUNNING。auto_start 幂等: 仅在 STOPPED 时发送，STARTING/RUNNING 不重发。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            for e in list_envs(tok):
                if e.get("name") == name:
                    s = e.get("state")
                    log("环境 %s state=%s (等待中 %ds)" % (name, s, int(time.time() - t0)))
                    if s == "RUNNING":
                        return e
                    if s == "STOPPED" and auto_start:
                        start_env(tok, name)
                    break
            else:
                log("环境 %s 尚未出现在列表 (等待 %ds)" % (name, int(time.time() - t0)))
        except Exception as ex:
            log("轮询异常: %s" % ex)
        time.sleep(interval)
    raise TimeoutError("等待 %s RUNNING 超时 %ds" % (name, timeout))


def wait_stopped(tok, name, timeout=600, interval=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            cur = next((e for e in list_envs(tok) if e.get("name") == name), None)
        except Exception:
            cur = None
        if cur:
            log("环境 %s state=%s (等待停止 %ds)" % (name, cur.get("state"), int(time.time() - t0)))
            if cur.get("state") == "STOPPED":
                return cur
        time.sleep(interval)
    raise TimeoutError("等待 %s STOPPED 超时 %ds" % (name, timeout))


def jupyter_url(env):
    for s in env.get("services", []):
        if s.get("name") == "Jupyterlab":
            return s.get("url", "")
    return ""


def jupyter_endpoint(env):
    """从 env services 提取 (站点根 base, token)。API 路径必须相对站点根拼接，
    不能带 /lab/tree/... 路径（否则 POST 落到静态路径上 405）。"""
    jurl = jupyter_url(env)
    if not jurl:
        raise RuntimeError("环境无 Jupyterlab service URL")
    p = urllib.parse.urlparse(jurl)
    base = "%s://%s" % (p.scheme, p.netloc)
    jt = urllib.parse.parse_qs(urllib.parse.urlparse(jurl).query).get("token", [""])[0]
    if not jt:
        raise RuntimeError("Jupyterlab service URL 无 token 参数")
    return base, jt


# ---------------------------------------------------------------- Jupyter (token 认证)
def jupyter_api(base, jt, method, path, body=None, timeout=40):
    """base=https://web-xxx.env.trial.teradata.com  jt=?token= 值"""
    url = base + path + ("&" if "?" in (base + path) else "?") + "token=" + urllib.parse.quote(jt)
    h = {"Authorization": "token " + jt}
    st, txt = http(method, url, h, body, timeout)
    try:
        return st, json.loads(txt)
    except Exception:
        return st, txt


def kernel_execute(base, jt, code, timeout=600):
    """起临时 kernel 跑 python 代码，返回拼接输出"""
    st, sess = jupyter_api(base, jt, "POST", "/api/sessions",
                           {"name": "nz-deploy", "path": "td-deploy.ipynb",
                            "type": "notebook", "kernel": {"name": "python3"}})
    if st not in (200, 201):
        raise RuntimeError("start session HTTP %s: %s" % (st, str(sess)[:300]))
    sid, kid = sess["id"], sess["kernel"]["id"]
    try:
        ws = websocket.create_connection(
            "%s/api/kernels/%s/channels?token=%s" % (base.replace("https://", "wss://"), kid, urllib.parse.quote(jt)),
            header=["Authorization: token " + jt], timeout=20, suppress_origin=True)
        mid = uuidlib.uuid4().hex
        hdr = {"msg_id": mid, "username": "nz", "session": uuidlib.uuid4().hex,
               "msg_type": "execute_request", "version": "5.3",
               "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        req = {"header": hdr, "parent_header": {}, "metadata": {},
               "content": {"code": code, "silent": False, "store_history": False,
                           "user_expressions": {}, "allow_stdin": False, "stop_on_error": True}}
        ws.send(json.dumps(req))
        ws.settimeout(timeout)
        out, idle, t0 = [], False, time.time()
        while True:
            if time.time() - t0 > timeout:
                out.append("[TIMEOUT after %ds]" % timeout)
                break
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if idle:
                    break
                continue
            m = json.loads(raw)
            if m.get("parent_header", {}).get("msg_id") != mid:
                continue
            mt = m.get("header", {}).get("msg_type")
            if mt == "stream":
                out.append(m["content"]["text"])
            elif mt == "execute_result":
                out.append(m["content"]["data"].get("text/plain", ""))
            elif mt == "error":
                out.append("[ERROR] %s: %s\n%s" % (
                    m["content"].get("ename"), m["content"].get("evalue"),
                    "\n".join(m["content"].get("traceback", [])[-6:])))
            elif mt == "status" and m["content"].get("execution_state") == "idle":
                idle = True
                break
        return "".join(out)
    finally:
        try:
            jupyter_api(base, jt, "DELETE", "/api/sessions/" + sid)
        except Exception:
            pass


# ---------------------------------------------------------------- 探针部署
DEPLOY_SH = r"""
set -e
NZ=/home/jovyan/JupyterLabRoot/nz
mkdir -p $NZ && cd $NZ
echo "=== 1) nezha-agent __AGENT_VER__ ==="
if [ ! -f nezha-agent ] || ! ./nezha-agent -v >/dev/null 2>&1; then
  rm -f agent.zip
  for i in 1 2 3; do
    curl -sL -m 300 -o agent.zip "https://github.com/nezhahq/agent/releases/download/__AGENT_VER__/nezha-agent_linux_amd64.zip" && break
    echo "retry $i"; sleep 5
  done
  python3 -c "import zipfile; zipfile.ZipFile('agent.zip').extractall('.')"
  chmod +x nezha-agent && rm -f agent.zip
fi
./nezha-agent -v || true
md5sum nezha-agent
echo "=== 2) config.yml ==="
cat > config.yml <<'CFG'
client_secret: __NZ_SECRET__
debug: false
disable_auto_update: false
disable_command_execute: false
disable_force_update: false
disable_nat: false
disable_send_query: false
gpu: false
insecure_tls: false
ip_report_period: 1800
report_delay: 3
self_update_period: 0
server: __PANEL_ADDR__
skip_connection_count: false
skip_procs_count: false
temperature: false
tls: true
use_atomgit_to_upgrade: false
use_gitee_to_upgrade: false
use_ipv6_country_code: false
uuid: __AGENT_UUID__
CFG
echo "=== 3) v4proxy.py ==="
cat > v4proxy.py <<'V4P'
__V4PROXY_SRC__
V4P
echo "=== 4) watchdog.sh ==="
cat > watchdog.sh <<'WD'
#!/bin/bash
P=/home/jovyan/JupyterLabRoot/nz
LOG=$P/watchdog.log
echo "[PID $$] watchdog started $(date '+%F %T')" >> $LOG
(
  # 环境内自我 ping，保持 web 层活跃（防闲置挂起第一道防线）
  while true; do
    curl -sS -m 20 -o /dev/null "https://__WEB_DNS__/" || true
    sleep 300
  done
) &
(
  while true; do
    pgrep -f "python3 .*v4proxy" >/dev/null || {
      echo "[$(date '+%F %T')] start v4proxy" >> $LOG
      setsid nohup python3 $P/v4proxy.py >> $P/v4proxy.log 2>&1 < /dev/null &
    }
    # agent 年龄 >=4h 无条件重启（防休眠唤醒后 TCP 断开进程僵死）
    APID=$(pgrep -x nezha-agent | head -1)
    if [ -n "$APID" ]; then
      AGE=$(( $(date +%s) - $(stat -c %Y /proc/$APID 2>/dev/null || date +%s) ))
      if [ $AGE -ge 14400 ]; then
        echo "[$(date '+%F %T')] agent age ${AGE}s >= 4h -> kill (respawn next loop)" >> $LOG
        kill $APID 2>/dev/null || true
      fi
    fi
    pgrep -x nezha-agent >/dev/null || {
      echo "[$(date '+%F %T')] start nezha-agent" >> $LOG
      setsid nohup env NZ_SERVER=__PANEL_ADDR__ NZ_TLS=true NZ_CLIENT_SECRET=__NZ_SECRET__ \
        HTTPS_PROXY=http://127.0.0.1:3128 $P/nezha-agent > $P/agent.log 2>&1 < /dev/null &
    }
    sleep 60
  done
) &
wait
WD
chmod +x watchdog.sh v4proxy.py nezha-agent
echo "=== 5) 清理旧进程并启动 ==="
pkill -x nezha-agent 2>/dev/null || true
pkill -f "python3 .*v4proxy" 2>/dev/null || true
sleep 1
pkill -f "bash .*watchdog.sh" 2>/dev/null || true
setsid nohup bash watchdog.sh > watchdog.out 2>&1 < /dev/null &
sleep 4
echo "=== 6) 进程验证 ==="
pgrep -x nezha-agent && echo AGENT_OK || echo AGENT_FAIL
pgrep -f "python3 .*v4proxy" && echo PROXY_OK || echo PROXY_FAIL
pgrep -f "bash .*watchdog.sh" && echo WD_OK || echo WD_FAIL
echo "=== agent.log tail ==="
tail -3 agent.log 2>/dev/null || echo "(empty)"
echo "DEPLOY_DONE_MARKER"
"""


def build_deploy_sh(web_dns):
    v4src = open(os.path.join(ASSETS, "v4proxy.py")).read().rstrip()
    sh = DEPLOY_SH
    sh = sh.replace("__AGENT_VER__", AGENT_VER)
    sh = sh.replace("__NZ_SECRET__", os.environ["NZ_SECRET"])
    sh = sh.replace("__PANEL_ADDR__", PANEL_ADDR)
    sh = sh.replace("__AGENT_UUID__", AGENT_UUID)
    sh = sh.replace("__WEB_DNS__", web_dns)
    sh = sh.replace("__V4PROXY_SRC__", v4src)
    return sh


def deploy_probe(env):
    """新环境里完整部署探针，返回 (base, deploy输出)"""
    base, jt = jupyter_endpoint(env)
    log("Jupyter: %s token=%s..." % (base, jt[:12]))

    # 0) API 就绪确认
    for i in range(30):
        st, _ = jupyter_api(base, jt, "GET", "/api/status", timeout=20)
        log("api/status -> %s" % st)
        if st == 200:
            break
        time.sleep(20)
    else:
        raise RuntimeError("Jupyter API 一直不可用")

    # 1) 上传部署 shell
    sh = build_deploy_sh(urllib.parse.urlparse(base).hostname)
    st, r = jupyter_api(base, jt, "PUT", "/api/contents/td_deploy.sh",
                        {"type": "file", "format": "text", "content": sh}, timeout=60)
    if st not in (200, 201):
        raise RuntimeError("PUT td_deploy.sh HTTP %s: %s" % (st, str(r)[:300]))
    log("部署脚本已上传 (%d chars)" % len(sh))

    # 2) kernel 执行
    code = ("import subprocess\n"
            "r = subprocess.run('bash /home/jovyan/JupyterLabRoot/td_deploy.sh', "
            "shell=True, capture_output=True, text=True, timeout=560)\n"
            "print(r.stdout[-4000:])\n"
            "print('STDERR:', r.stderr[-800:])\n")
    out = kernel_execute(base, jt, code, timeout=600)
    print(out, flush=True)
    if "DEPLOY_DONE_MARKER" not in out or "AGENT_OK" not in out:
        raise RuntimeError("部署脚本执行异常（未看到完成标记）")
    return base, jt


# ---------------------------------------------------------------- 进程保活（kernel exec 轻量路径）
ENSURE_CODE = r'''
import subprocess as sp, time as T
def sh(c, t=25):
    try:
        r = sp.run(c, shell=True, capture_output=True, text=True, timeout=t)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return "EXC " + str(e)
P = "/home/jovyan/JupyterLabRoot/nz"
wd_ok = sp.run("pgrep -f 'bash .*watchdog.sh'", shell=True, capture_output=True).returncode == 0
if not wd_ok:
    print("watchdog 缺失 -> 清理并拉起")
    print(sh("pkill -x nezha-agent 2>/dev/null; pkill -f 'python3 .*v4proxy' 2>/dev/null; sleep 1; "
             "cd %s && setsid nohup bash watchdog.sh > watchdog.out 2>&1 < /dev/null & sleep 8; echo started" % P, t=30))
else:
    print("watchdog 已在运行")
T.sleep(6)
print("== 进程验证 ==")
print("agent:", sh("pgrep -ax nezha-agent || echo NO_AGENT"))
print("v4proxy:", sh("pgrep -f 'python3 .*v4proxy' >/dev/null && echo PROXY_OK || echo NO_PROXY"))
print("== agent.log tail ==")
print(sh("tail -6 %s/agent.log 2>/dev/null || echo NOLOG" % P))
print("ENSURE_DONE_MARKER")
'''


def ensure_probe_processes(env):
    """kernel exec 确保容器内 watchdog 在跑（watchdog 会拉起 v4proxy+agent）。
    返回 True=确认 agent 进程存在；False=执行失败或 agent 未起来。"""
    try:
        base, jt = jupyter_endpoint(env)
    except Exception as ex:
        log("kernel 路径不可用（取 Jupyter endpoint 失败）: %s" % str(ex)[:150])
        return False
    try:
        out = kernel_execute(base, jt, ENSURE_CODE, timeout=180)
        print(out, flush=True)
        if "ENSURE_DONE_MARKER" in out and "NO_AGENT" not in out:
            return True
        return False
    except Exception as ex:
        log("kernel exec 失败（runner POST 可能被 WAF 拦）: %s" % str(ex)[:200])
        return False


# ------------------------------------------ 平台空闲判定对抗（Vantage 查询 + Jupyter 活动）
# Teradata 平台空闲策略（2026-11-11 官方邮件实锤，nzprobe 已被强制暂停一次）:
#   - 48h 未对 Vantage 发起查询 -> 环境被平台暂停（agent 跟着掉线）
#   - 近 2h Jupyter 无活动       -> 计入空闲
# kernel exec 起会话/执行本身就是 Jupyter 活动；这里再跑一条真实 SQL，
# 把「Vantage 查询」计数器也清零。SQL 失败不阻断保活主流程。
VANTAGE_CODE_TMPL = r'''
import base64, json
res = {"steps": [], "ok": False, "host": None, "user": None}
creds = json.loads(base64.b64decode("__CREDS_B64__").decode())
import teradatasql
con = None
err = None
for u, p in creds:
    if not p:
        continue
    try:
        con = teradatasql.connect(json.dumps({"host": "__DBC_HOST__", "user": u, "password": p}))
        res["user"] = u
        res["host"] = "__DBC_HOST__"
        break
    except Exception as e:
        err = repr(e)[:160]
        res["steps"].append(["connect_" + u, repr(e)[:90]])
if con is None:
    res["error"] = "connect failed: " + str(err)
else:
    try:
        cur = con.execute("SELECT CURRENT_TIMESTAMP, DATABASE, USER")
        res["steps"].append(["select", str(cur.fetchall())])
        res["ok"] = True
    except Exception as e:
        res["steps"].append(["select", repr(e)[:110]])
    try:
        con.execute("CREATE TABLE demo_user.keepalive_log (ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, note VARCHAR(64))")
        res["steps"].append(["create_table", "ok"])
    except Exception as e:
        s = repr(e)[:110]
        res["steps"].append(["create_table", "exists" if ("exists" in s.lower() or "already" in s.lower()) else s])
    try:
        con.execute("INSERT INTO demo_user.keepalive_log (note) VALUES ('nz-keepalive')")
        res["steps"].append(["insert", "ok"])
    except Exception as e:
        res["steps"].append(["insert", repr(e)[:110]])
    try:
        cur = con.execute("SELECT TOP 1 * FROM demo_user.keepalive_log ORDER BY ts DESC")
        res["steps"].append(["last", str(cur.fetchall())])
    except Exception as e:
        res["steps"].append(["last", repr(e)[:110]])
    try:
        con.close()
    except Exception:
        pass
print("VANTAGE_JSON::" + json.dumps(res))
'''


def vantage_db_host(env):
    """从环境 services 里解析 Vantage DBC 主机（bteq .logon {host}/{user}）"""
    try:
        for s in env.get("services", []):
            if s.get("name") == "Vantage":
                m = re.search(r"logon\s+([A-Za-z0-9.\-]+)/", s.get("url", ""))
                if m:
                    return m.group(1)
    except Exception:
        pass
    return (STATE.get("env_name") or "nzprobe") + ".env.trial.teradata.com"


def build_vantage_code(dbc_host):
    """生成 SQL 活动代码；实例密码只进 kernel 代码，绝不写日志。"""
    pwd = os.environ.get("TD_ENV_PASSWORD", "") or ""
    creds = [("demo_user", pwd), ("dbc", pwd)] if pwd else [("demo_user", "demo_user"), ("dbc", "dbc")]
    return (VANTAGE_CODE_TMPL
            .replace("__DBC_HOST__", dbc_host)
            .replace("__CREDS_B64__", base64.b64encode(json.dumps(creds).encode()).decode()))


def vantage_activity(env, attempts=3):
    """环境内执行真实 Vantage SQL（清零平台 48h 空闲查询计数器）。
    顺带 PUT 一个 notebook 文件加强 Jupyter 活动信号。返回查询是否成功。"""
    try:
        base, jt = jupyter_endpoint(env)
    except Exception as ex:
        log("vantage_activity: 取 Jupyter endpoint 失败: %s" % str(ex)[:150])
        STATE["last_sql"] = "skip"
        STATE["last_sql_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return False
    # 文件级 Jupyter 活动（contents API 写入，信号双保险）
    try:
        nb = {"cells": [{"cell_type": "code", "metadata": {}, "source": ["# nz keepalive touch"],
                         "outputs": [], "execution_count": None}],
              "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        st, r = jupyter_api(base, jt, "PUT", "/api/contents/keepalive_touch.ipynb",
                            {"type": "notebook", "format": "json", "content": nb}, timeout=30)
        log("Jupyter 文件活动 touch -> HTTP %s" % st)
    except Exception as ex:
        log("Jupyter touch 异常（不阻断）: %s" % str(ex)[:120])
    dbc_host = vantage_db_host(env)
    code = build_vantage_code(dbc_host)
    for i in range(attempts):
        try:
            out = kernel_execute(base, jt, code, timeout=300)
            marker = "VANTAGE_JSON::"
            idx = out.rfind(marker)
            parsed = None
            if idx >= 0:
                try:
                    parsed = json.loads(out[idx + len(marker):].strip().splitlines()[0])
                except Exception:
                    parsed = None
            if parsed and parsed.get("ok"):
                log("Vantage SQL 活动 ✓ host=%s user=%s steps=%s" % (
                    parsed.get("host"), parsed.get("user"),
                    json.dumps(parsed.get("steps"), ensure_ascii=False)[:400]))
                STATE["last_sql"] = "ok"
                STATE["last_sql_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                return True
            log("Vantage SQL 第 %d/%d 次未成功: %s" % (i + 1, attempts, (out[-400:] if out else "无输出")))
        except Exception as ex:
            log("vantage_activity 第 %d/%d 次异常: %s" % (i + 1, attempts, str(ex)[:200]))
        time.sleep(30)
    STATE["last_sql"] = "fail"
    STATE["last_sql_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    log("Vantage SQL 活动 ✗（本轮放弃，下一轮 workflow 重试）")
    return False


# ---------------------------------------------------------------- 面板校验/改名
def panel_token():
    st, txt = http("POST", PANEL_HTTP + "/api/v1/login",
                   body={"username": os.environ.get("NZ_PANEL_USER", "admin"),
                         "password": os.environ.get("NZ_PANEL_PASS", "")})
    if st != 200:
        raise RuntimeError("面板登录 HTTP %s: %s" % (st, str(txt)[:150]))
    d = json.loads(txt).get("data")
    if isinstance(d, dict):
        return d.get("token", "")
    return json.loads(txt).get("token") or (d or "")


def panel_fix_name():
    """节点 uuid 固定 → 面板条目应自动对应；名字不对则改名（带 X-Csrf-Token）"""
    try:
        tok = panel_token()
    except Exception as ex:
        log("面板登录失败(跳过改名校验): %s" % ex)
        return
    st, txt = http("GET", PANEL_HTTP + "/api/v1/server",
                   {"Authorization": "Bearer " + tok})
    if st != 200:
        log("面板节点列表 HTTP %s" % st)
        return
    try:
        r = json.loads(txt)
    except Exception:
        log("面板节点列表非 JSON: %s" % txt[:150])
        return
    hit = None
    for s in (r.get("data") or []):
        if str(s.get("uuid", "")).lower() == AGENT_UUID.lower():
            hit = s
            break
    if not hit:
        log("⚠️ 面板无 uuid=%s 节点（agent 可能尚未连上，等下一轮校正）" % AGENT_UUID)
        return
    if hit.get("name") == NODE_NAME:
        log("面板节点名正确: %s (last_active=%s)" % (hit.get("name"), hit.get("last_active")))
        return
    # 改名: 需要 X-Csrf-Token = nz-csrf cookie
    from http.cookiejar import CookieJar
    cj = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(PANEL_HTTP + "/api/v1/login",
                                 data=json.dumps({"username": os.environ.get("NZ_PANEL_USER", "admin"),
                                                  "password": os.environ.get("NZ_PANEL_PASS", "")}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    op.open(req, timeout=20)
    csrf = next((c.value for c in cj if c.name == "nz-csrf"), "")
    st, r2 = http("PATCH", PANEL_HTTP + "/api/v1/server/%s" % hit["id"],
                  {"Authorization": "Bearer " + tok, "X-Csrf-Token": csrf},
                  {"name": NODE_NAME})
    log("面板改名 -> HTTP %s %s" % (st, str(r2)[:150]))


# ---------------------------------------------------------------- 重建
def rebuild(tok, reason):
    global REBUILD_EPOCH
    name = STATE["env_name"]
    region = STATE["region"]
    pwd = os.environ["TD_ENV_PASSWORD"]
    log("========== 重建开始: %s ==========" % reason)
    # 旧环境还在则先删（释放 envCount）
    envs = list_envs(tok)
    for e in envs:
        if e.get("name") == name:
            delete_env(tok, name)
            t0 = time.time()
            while time.time() - t0 < 300:
                time.sleep(20)
                if all(x.get("name") != name for x in list_envs(tok)):
                    break
                log("等待旧环境删除...")
            break
    # 建新环境（同配置: 同名同区域）
    st, _ = create_env(tok, name, pwd, region)
    if st not in (200, 201, 202):
        raise RuntimeError("创建环境失败 HTTP %s（密码复杂度要求: 8+ 位含大小写数字符号）" % st)
    env = wait_running(tok, name, timeout=1500)
    REBUILD_EPOCH = reason
    log("新环境 RUNNING: createDate=%s webDns=%s" % (env.get("createDate"), env.get("webDnsName")))

    # 探针部署
    base, jt = deploy_probe(env)
    time.sleep(15)
    panel_fix_name()

    # 新环境立即跑一次真实 Vantage SQL，重置平台空闲计数（否则 48h 后又被暂停）
    try:
        vantage_activity(env)
    except Exception as ex:
        log("重建后 SQL 活动异常（不阻断）: %s" % str(ex)[:150])

    # 更新 state（域名/创建时间/部署信息）→ workflow 会 commit 回分支
    STATE["web_dns"] = env.get("webDnsName") or urllib.parse.urlparse(base).hostname
    STATE["create_date"] = env.get("createDate")
    STATE["jupyter_token"] = jt
    STATE["last_rebuild"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_rebuild_reason"] = reason
    json.dump(STATE, open(STATE_FILE, "w"), indent=1, ensure_ascii=False)
    log("td_state.json 已更新")


# ---------------------------------------------------------------- 主流程
def days_left(create_date_str):
    if not create_date_str:
        return None
    try:
        cd = datetime.fromisoformat(create_date_str.replace("Z", "+00:00"))
        return ENV_TTL_DAYS - (datetime.now(timezone.utc) - cd).total_seconds() / 86400.0
    except Exception:
        return None


def main():
    email = os.environ["TD_EMAIL"]
    pwd = os.environ["TD_PASSWORD"]
    tok = login_api_token(email, pwd)

    envs = list_envs(tok)
    name = STATE["env_name"]
    env = next((e for e in envs if e.get("name") == name), None)
    log("当前环境列表: %s" % [(e.get("name"), e.get("state")) for e in envs])

    if env is None:
        if DRY:
            log("DRY-RUN: 环境不存在，跳过重建")
            return
        rebuild(tok, "环境不存在（到期被回收或被删除）")
        return

    left = days_left(env.get("createDate"))
    log("环境 %s state=%s, createDate=%s, 剩余≈%.1f 天" %
        (name, env.get("state"), env.get("createDate"), left if left is not None else -1))

    if left is not None and left <= REBUILD_AHEAD_DAYS:
        if DRY:
            log("DRY-RUN: 应触发到期重建（剩余 %.1f 天）" % left)
            return
        rebuild(tok, "60 天到期重建（剩余 %.1f 天，提前重建保连续）" % left)
        return

    if env.get("state") != "RUNNING":
        if DRY:
            log("DRY-RUN: 环境未运行，将 PATCH start（跳过）")
            return
        # STOPPING 状态直接 start 会 400；wait_running(auto_start) 会在 STOPPED 时幂等重发
        if env.get("state") != "STOPPING":
            start_env(tok, name)
        env = wait_running(tok, name, timeout=1200)

    # RUNNING → Jupyter 探活（顺带刷新 state 里的 jupyter token）
    try:
        jbase, jt = jupyter_endpoint(env)
        st, d = jupyter_api(jbase, jt, "GET", "/api/status", timeout=25)
        log("Jupyter 探活 %s -> HTTP %s" % (jbase, st))
        STATE["web_dns"] = urllib.parse.urlparse(jbase).hostname
        STATE["jupyter_token"] = jt
    except Exception as ex:
        log("Jupyter 探活异常（不阻断）: %s" % str(ex)[:150])

    # 平台空闲判定对抗: 每轮真实 Vantage SQL + Jupyter 活动（见 vantage_activity 注释）
    if DRY:
        log("DRY-RUN: 跳过 Vantage SQL 活动")
    else:
        vantage_activity(env)

    if DRY:
        log("DRY-RUN: 跳过探针刷新与面板上报确认")
        # 面板侧核对（探针 uuid 固定，一般无需动作；last_active 观察输出）
        st, txt = http("GET", PANEL_HTTP + "/api/v1/server",
                       {"Authorization": "Bearer " + panel_token()})
        try:
            r = json.loads(txt)
        except Exception:
            log("面板节点列表非 JSON: %s" % txt[:150])
            return
        for s in (r.get("data") or []):
            if str(s.get("uuid", "")).lower() == AGENT_UUID.lower():
                log("面板节点: %s | last_active=%s" % (s.get("name"), s.get("last_active")))
                break
        return

    # —— 面板上报确认；修复梯度: ① kernel exec 进程保活 ② 环境 stop→start 重建 ——
    # 背景: 容器休眠/重启会清空所有进程（watchdog/agent/v4proxy），环境状态仍是
    # RUNNING 但面板零上报。旧代码此时直接 stop→start 重建，且重建后从不重拉探针
    # 进程，形成"重建成功却零上报"死循环（2026-11-10 掉线复盘）。
    # 新梯度: 先 kernel exec 拉起 watchdog（最轻、不动环境）；仍无上报才重建，
    # 且重建后必须再次 ensure（容器重启进程全清）。
    def panel_reported(ptok_):
        st_, txt_ = http("GET", PANEL_HTTP + "/api/v1/server",
                         {"Authorization": "Bearer " + ptok_})
        try:
            for s_ in (json.loads(txt_).get("data") or []):
                if str(s_.get("uuid", "")).lower() == AGENT_UUID.lower():
                    la_ = str(s_.get("last_active") or "")
                    return bool(la_) and not la_.startswith("0001-01-01")
        except Exception:
            pass
        return False

    ptok = panel_token()
    confirmed = panel_reported(ptok)
    if not confirmed:
        for _ in range(8):
            time.sleep(15)
            if panel_reported(ptok):
                confirmed = True
                break

    fix_note = ""
    if confirmed:
        log("面板上报确认 ✓（探针健康）")
        # 轻量确认: watchdog 万一不在就补拉（正常时输出"已在运行"）
        if ensure_probe_processes(env):
            log("进程保活轻量确认 ✓")
    else:
        log("⚠️ 面板 2 分钟未见上报——尝试 kernel exec 进程保活（拉起 watchdog/agent）")
        if ensure_probe_processes(env):
            for _ in range(8):
                time.sleep(15)
                if panel_reported(ptok):
                    confirmed = True
                    fix_note = "kernel 进程保活修复（未动环境）"
                    break
            if confirmed:
                log("进程保活修复成功 ✓")
            else:
                log("进程保活后仍无上报，转入环境重建")
        else:
            log("kernel 保活不可用，直接转入环境重建")
        if not confirmed:
            log("执行环境 stop→start 重建")
            try:
                stop_env(tok, name)
                wait_stopped(tok, name, timeout=600)
                env = wait_running(tok, name, timeout=1200, auto_start=True)
                # 容器已重启、进程全清——必须重新拉起探针
                if ensure_probe_processes(env):
                    log("重建后探针进程已拉起，等待上报")
                else:
                    log("⚠️ 重建后 kernel 拉起失败（探针进程可能缺失）")
                confirmed = False
                for _ in range(20):
                    time.sleep(15)
                    if panel_reported(ptok):
                        confirmed = True
                        fix_note = "stop→start 重建 + 进程重拉修复"
                        break
                log("重建后结果: %s" % ("面板上报确认 ✓（探针已恢复）" if confirmed
                                       else "⚠️ 仍未上报，等下一轮"))
            except Exception as e:
                log("重建异常（不阻断，等下一轮）: %s" % str(e)[:200])

    # 面板侧核对（探针 uuid 固定，名字不对则改名）
    st, txt = http("GET", PANEL_HTTP + "/api/v1/server",
                   {"Authorization": "Bearer " + ptok})
    try:
        r = json.loads(txt)
    except Exception:
        log("面板节点列表非 JSON: %s" % txt[:150])
        return
    for s in (r.get("data") or []):
        if str(s.get("uuid", "")).lower() == AGENT_UUID.lower():
            log("面板节点: %s | last_active=%s" % (s.get("name"), s.get("last_active")))
            if s.get("name") != NODE_NAME:
                panel_fix_name()
            break

    STATE["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_result"] = ("ok: " + fix_note) if (confirmed and fix_note) else \
                           ("ok" if confirmed else "warn: 环境已恢复但面板未见上报")
    json.dump(STATE, open(STATE_FILE, "w"), indent=1, ensure_ascii=False)
    log("保活完成 ✓")


if __name__ == "__main__":
    try:
        main()
        sys.stdout.write("::notice::teradata keepalive ok" + os.linesep)
    except Exception as e:
        log("FAILED: %s" % e)
        STATE["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE["last_result"] = "fail: %s" % str(e)[:300]
        json.dump(STATE, open(STATE_FILE, "w"), indent=1, ensure_ascii=False)
        sys.exit(1)
