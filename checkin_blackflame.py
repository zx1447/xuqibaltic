#!/usr/bin/env python3
# BlackFlame Studio 自动签到（看广告领金币）
#
# 机制（2026-11 实测）：
#   Next.js Server Action 三步走 —— startAdWatch → 等 requiredSeconds(30s) → verifyAndRewardAdWatch
#   服务端只校验时间间隔；广告弹窗只是前端摆设。每轮 +5 coins。
#
# 会话链：Discord Token →（NextAuth OAuth 自动授权）→ 站点会话（约 6 小时短命，故每次现换）
#
# 环境变量：
#   BFLAME_DISCORD_TOKEN   必需  Discord 账号 token（MTxx... 格式）
#   BFLAME_RUN_MINUTES     可选  >0 = 持续循环跑多少分钟（一直运行模式）
#   BFLAME_WATCHES         可选  RUN_MINUTES=0 时的轮数，默认 1（每轮约 35 秒，+5 币）
#   BFLAME_BASE            可选  站点地址，默认 https://dashbord.blackflamestudio.us.ci
#
# 节奏：每轮 startAdWatch → 等 requiredSeconds(30s) → verify，+5 coins；
#       无冷却（实测连刷可用），循环模式下会话 4h 主动重登（会话 6h 过期）。
#
# 退出码：0 成功；2 有轮次失败；3 登录链失败

import json
import os
import re
import sys
import time
import http.cookiejar
import urllib.request
import urllib.parse

BASE = os.environ.get("BFLAME_BASE", "https://dashbord.blackflamestudio.us.ci").rstrip("/")
WATCHES = int(os.environ.get("BFLAME_WATCHES", "1"))
RUN_MINUTES = int(os.environ.get("BFLAME_RUN_MINUTES", "0"))
TOKEN = os.environ.get("BFLAME_DISCORD_TOKEN", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# Server Action ID（站点 chunk 0.deue4ydm.nr.js 内 createServerReference 硬编码；
# 站点重新构建后会变，脚本带自动重发现兜底）
ACT_START = "00479d0db00f9f4d9930771ac14c7b43ffdc76d8a0"      # startAdWatch
ACT_CANCEL = "0084c4cd6a11db20db67ae0bd128692e6eaa595612"     # cancelAdWatch
ACT_VERIFY = "0029bc5d72edbc78ce2734a359aeaab96793a69fe3"     # verifyAndRewardAdWatch

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def log(msg):
    print(msg, flush=True)


def req(url, method="GET", headers=None, data=None, timeout=30, retries=5):
    """网络请求带指数重试（WARP/网络 首连偶发 TLS 失败，重试即愈）"""
    last = None
    for i in range(retries):
        try:
            r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            with opener.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 —— 网络层一律重试
            last = e
            log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
            time.sleep(2 + i * 2)
    raise last


def site(path, method="GET", headers=None, data=None):
    h = {"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/dashboard"}
    h.update(headers or {})
    return req(BASE + path, method=method, headers=h, data=data)


def flight_error(text):
    """从 RSC flight 载荷提取 {"error":"..."}；$undefined 等引用值不是错误"""
    for m in re.finditer(r'"error"\s*:\s*"([^"]+)"', text):
        v = m.group(1)
        if not v.startswith("$"):
            return v
    return None


# ---------- 登录链 ----------
def login():
    log("== 登录链：Discord Token → 站点会话 ==")
    # 0) 验 Discord token
    st, txt = req("https://discord.com/api/v9/users/@me",
                  headers={"Authorization": TOKEN, "User-Agent": UA})
    if st != 200:
        log(f"❌ Discord token 无效（HTTP {st}）")
        sys.exit(3)
    me = json.loads(txt)
    log(f"  Discord: {me['username']} (id {me['id']})")

    # 1) CSRF
    st, txt = site("/api/auth/csrf")
    csrf = json.loads(txt)["csrfToken"]

    # 2) 拿 Discord 授权 URL
    body = urllib.parse.urlencode({
        "csrfToken": csrf, "callbackUrl": "/dashboard", "json": "true"}).encode()
    st, txt = site("/api/auth/signin/discord", method="POST",
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body)
    auth_url = json.loads(txt)["url"]

    # 3) Discord 侧自动批准
    st, txt = req(auth_url, method="POST",
                  headers={"Authorization": TOKEN, "User-Agent": UA,
                           "Content-Type": "application/json",
                           "Origin": "https://discord.com"},
                  data=json.dumps({"permissions": "0", "authorize": True,
                                   "integration_type": 0}).encode())
    grant = json.loads(txt)
    if "location" not in grant:
        log(f"❌ OAuth 自动授权失败: {json.dumps(grant, ensure_ascii=False)[:300]}")
        sys.exit(3)

    # 4) 回调换会话
    loc = urllib.parse.urlparse(grant["location"])
    site(loc.path + ("?" + loc.query if loc.query else ""))

    # 5) 校验
    st, txt = site("/api/auth/session")
    sess = json.loads(txt)
    if "user" not in sess:
        log(f"❌ 会话未建立: {txt[:200]}")
        sys.exit(3)
    log(f"  会话: {sess['user']['name']} | coins={sess['user']['coins']} "
        f"| expires={sess.get('expires')}")
    return sess


# ---------- Server Action ----------
def action(action_id):
    return site("/dashboard", method="POST",
                headers={"Next-Action": action_id,
                         "Content-Type": "text/plain;charset=UTF-8",
                         "Accept": "text/x-component"},
                data=b"[]")


def coins_now():
    st, txt = site("/api/auth/session")
    return json.loads(txt)["user"]["coins"]


# ---------- Server Action ID 自动重发现 ----------
def rediscover_ids():
    """站点重建后 chunk 名/Action ID 会变；从 dashboard 页 chunk 里重新挖。"""
    global ACT_START, ACT_CANCEL, ACT_VERIFY
    st, html = site("/dashboard")
    chunks = sorted(set(re.findall(r'/_next/static/chunks/[^"\'\\ )]+\.js', html)))
    for c in chunks:
        st, js = req(BASE + c, headers={"User-Agent": UA})
        if "createServerReference" not in js:
            continue
        found = dict(re.findall(
            r'createServerReference\)\("([0-9a-f]{40})",[\s\S]{0,140}?"([A-Za-z]+)"\)', js))
        # found = {id: name}，翻转
        names = {v: k for k, v in found.items()}
        if "startAdWatch" in names:
            ACT_START = names["startAdWatch"]
            ACT_CANCEL = names.get("cancelAdWatch", ACT_CANCEL)
            ACT_VERIFY = names.get("verifyAndRewardAdWatch", ACT_VERIFY)
            log(f"  Action ID 已重发现: start={ACT_START[:12]}… verify={ACT_VERIFY[:12]}…")
            return True
    return False


# ---------- 单轮 ----------
def one_round(idx):
    """一轮 start→wait→verify。返回 (ok, backoff_seconds)"""
    log(f"-- 第 {idx} 轮 --")
    st, txt = action(ACT_START)
    err = flight_error(txt)
    if err:
        log(f"  startAdWatch 报错: {err}（尝试 cancel 后重试）")
        action(ACT_CANCEL)
        st, txt = action(ACT_START)
        err = flight_error(txt)
        if err:
            log(f"  ❌ 仍然失败: {err}")
            if "Ad Link" in err or "ad link" in err:
                log("  （管理员没配广告链接，退避 300s）")
                return False, 300
            if any(k in err.lower() for k in ("sign", "login", "auth", "session", "unauthorized")):
                return False, -1          # 信号：需要重登
            return False, 30
    m = re.search(r'"requiredSeconds"\s*:\s*(\d+)', txt)
    need = int(m.group(1)) if m else 30
    log(f"  startAdWatch ✅ 需等 {need}s")
    time.sleep(need + 2)

    st, txt = action(ACT_VERIFY)
    err = flight_error(txt)
    if err:
        if any(k in err.lower() for k in ("wait", "early", "seconds")):
            log(f"  服务端嫌早: {err}，补 5 秒重试")
            time.sleep(5)
            st, txt = action(ACT_VERIFY)
            err = flight_error(txt)
        if err:
            log(f"  ❌ verify 失败: {err}")
            if any(k in err.lower() for k in ("sign", "login", "auth", "session", "unauthorized")):
                return False, -1
            return False, 30
    log("  verifyAndRewardAdWatch ✅ +5 coins")
    return True, 0


# ---------- 主流程 ----------
def main():
    if not TOKEN:
        log("❌ 缺环境变量 BFLAME_DISCORD_TOKEN")
        sys.exit(3)

    login()
    login_ts = time.time()
    before = coins_now()

    ok = 0
    rounds = 0

    if RUN_MINUTES > 0:
        # 一直运行模式：循环到截止（会话 4h 主动重登，防 6h 过期）
        deadline = time.time() + RUN_MINUTES * 60
        log(f"\n== 持续签到 {RUN_MINUTES} 分钟（起始 coins={before}，约 {(RUN_MINUTES * 60) // 35} 轮）==")
        while time.time() < deadline:
            if time.time() - login_ts > 4 * 3600:
                log("== 会话接近过期（4h），主动重登 ==")
                login()
                login_ts = time.time()
            rounds += 1
            try:
                good, backoff = one_round(rounds)
            except Exception as e:  # noqa: BLE001
                log(f"  ❌ 异常: {type(e).__name__}: {e}")
                good, backoff = False, 30
            if good:
                ok += 1
                time.sleep(2)
            elif backoff == -1:
                log("== 疑似会话失效，重登 ==")
                login()
                login_ts = time.time()
            else:
                time.sleep(backoff)
            if rounds % 25 == 0:
                cur = coins_now()
                elapsed = (time.time() - (deadline - RUN_MINUTES * 60)) / 60
                rate = (cur - before) / elapsed * 60 if elapsed > 0 else 0
                log(f"  …进度: {rounds} 轮 | coins={cur}（+{cur - before}，约 {rate:.0f} 币/小时）"
                    f" | 剩 {max(0, (deadline - time.time()) / 60):.0f} 分钟")
    else:
        # 定数模式
        log(f"\n== 签到 x{WATCHES}（起始 coins={before}）==")
        for i in range(1, WATCHES + 1):
            try:
                good, backoff = one_round(i)
            except Exception as e:  # noqa: BLE001
                log(f"  ❌ 异常: {type(e).__name__}: {e}")
                good = False
            rounds += 1
            if good:
                ok += 1
                time.sleep(2)
            else:
                time.sleep(max(backoff if backoff and backoff > 0 else 0, 0))

    after = coins_now()
    log(f"\n== 完成 {ok}/{rounds} 轮 | coins: {before} → {after}（+{after - before}）==")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## BlackFlame 签到结果\n\n")
            f.write(f"| 项目 | 值 |\n|---|---|\n")
            f.write(f"| 轮次 | {ok}/{rounds} 成功 |\n")
            f.write(f"| 金币 | {before} → {after}（+{after - before}）|\n")
            if RUN_MINUTES > 0:
                f.write(f"| 时长 | {RUN_MINUTES} 分钟（持续模式）|\n")

    sys.exit(0 if ok == rounds and rounds > 0 else (0 if RUN_MINUTES > 0 and ok > 0 else 2))


if __name__ == "__main__":
    main()
