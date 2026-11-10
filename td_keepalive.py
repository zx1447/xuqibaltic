#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teradata ClearScape 哪吒探针保活 + 自动唤醒 + 60 天到期自动重建（纯 HTTP，无浏览器依赖）

链路:
  1. Firebase Auth REST 密码登录 -> idToken
  2. Firestore 读 users/{email} 文档 -> 后端 API token
  3. GET /environments 检查环境状态
     - 环境不存在（到期被平台回收）        -> 重建
     - createDate + 60 天 <= 3 天          -> 主动重建（删旧建新，同配置）
     - state != RUNNING                    -> PATCH start 唤醒 + 等待
     - RUNNING                             -> Jupyter /api/status 探活
  4. 重建后自动重部署哪吒探针:
     - PUT 部署脚本到 Jupyter contents（token 认证）
     - kernel websocket 执行: 容器内自己从 GitHub 下载 agent + 配置 + 启动
     - 校验面板节点（uuid 固定, 名字不对则改名）

secrets:
  TD_EMAIL / TD_PASSWORD        Teradata 账号
  TD_ENV_PASSWORD               重建环境时设置的实例密码
  NZ_SECRET                     哪吒面板 agent 密钥
  NZ_PANEL_USER / NZ_PANEL_PASS 哪吒面板登录（改名兜底用）
用法:
  python td_keepalive.py            # 常规保活
  python td_keepalive.py --dry-run  # 只登录+读状态，不做任何变更
"""
import json
import os
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


def delete_env(tok, name):
    st, d = jhttp("DELETE", API + "/" + name, tok)
    log("DELETE %s -> HTTP %s %s" % (name, st, str(d)[:120]))
    return st


def create_env(tok, name, password, region):
    st, d = jhttp("POST", API, tok, {"name": name, "password": password, "region": region})
    log("POST create %s(%s) -> HTTP %s %s" % (name, region, st, str(d)[:200]))
    return st


def wait_running(tok, name, timeout=1200, interval=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            for e in list_envs(tok):
                if e.get("name") == name:
                    s = e.get("state")
                    log("环境 %s state=%s (等待中 %ds)" % (name, s, int(time.time() - t0)))
                    if s == "RUNNING":
                        return e
                    break
            else:
                log("环境 %s 尚未出现在列表 (等待 %ds)" % (name, int(time.time() - t0)))
        except Exception as ex:
            log("轮询异常: %s" % ex)
        time.sleep(interval)
    raise TimeoutError("等待 %s RUNNING 超时 %ds" % (name, timeout))


def jupyter_url(env):
    for s in env.get("services", []):
        if s.get("name") == "Jupyterlab":
            return s.get("url", "")
    return ""


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
    jurl = jupyter_url(env)
    if not jurl:
        raise RuntimeError("环境无 Jupyterlab service URL")
    base = jurl.split("?")[0]
    jt = urllib.parse.parse_qs(urllib.parse.urlparse(jurl).query).get("token", [""])[0]
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
        start_env(tok, name)
        env = wait_running(tok, name, timeout=900)

    # RUNNING → Jupyter 探活（顺带刷新 state 里的 jupyter token）
    jurl = jupyter_url(env)
    if jurl:
        base = jurl.split("?")[0]
        jt = urllib.parse.parse_qs(urllib.parse.urlparse(jurl).query).get("token", [""])[0]
        st, d = jupyter_api(base, jt, "GET", "/api/status", timeout=25)
        log("Jupyter 探活 %s -> HTTP %s" % (base, st))
        STATE["web_dns"] = urllib.parse.urlparse(base).hostname
        STATE["jupyter_token"] = jt

    # 面板侧核对（探针 uuid 固定，一般无需动作；last_active 观察输出）
    if DRY:
        log("DRY-RUN: 跳过面板校验")
        return
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
            if s.get("name") != NODE_NAME:
                if DRY:
                    log("DRY-RUN: 需要改名 %s -> %s（跳过）" % (s.get("name"), NODE_NAME))
                else:
                    panel_fix_name()
            break

    STATE["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE["last_result"] = "ok"
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
