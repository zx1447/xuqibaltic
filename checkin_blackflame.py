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
#   BFLAME_WATCHES         可选  本轮看广告次数，默认 1（每轮约 35 秒，+5 币）
#   BFLAME_BASE            可选  站点地址，默认 https://dashbord.blackflamestudio.us.ci
#
# 退出码：0 成功；2 签到失败；3 登录链失败

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


def req(url, method="GET", headers=None, data=None, timeout=30):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with opener.open(r, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


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


# ---------- 主流程 ----------
def main():
    if not TOKEN:
        log("❌ 缺环境变量 BFLAME_DISCORD_TOKEN")
        sys.exit(3)

    login()
    before = coins_now()
    log(f"\n== 签到 x{WATCHES}（起始 coins={before}）==")

    ok = 0
    for i in range(1, WATCHES + 1):
        log(f"-- 第 {i}/{WATCHES} 轮 --")
        st, txt = action(ACT_START)
        err = flight_error(txt)
        if err:
            # 可能有残留 watch 会话：先 cancel 再重试一次
            log(f"  startAdWatch 报错: {err}（尝试 cancel 后重试）")
            action(ACT_CANCEL)
            st, txt = action(ACT_START)
            err = flight_error(txt)
            if err:
                log(f"  ❌ 仍然失败: {err}")
                if "Ad Link" in err or "ad link" in err:
                    log("  （管理员没配广告链接，签不到，等站点配置）")
                continue
        m = re.search(r'"requiredSeconds"\s*:\s*(\d+)', txt)
        need = int(m.group(1)) if m else 30
        log(f"  startAdWatch ✅ 需等 {need}s")
        time.sleep(need + 2)

        st, txt = action(ACT_VERIFY)
        err = flight_error(txt)
        if err:
            if "wait" in err.lower() or "early" in err.lower() or "seconds" in err.lower():
                log(f"  服务端嫌早: {err}，补 5 秒重试")
                time.sleep(5)
                st, txt = action(ACT_VERIFY)
                err = flight_error(txt)
            if err:
                log(f"  ❌ verify 失败: {err}")
                continue
        ok += 1
        log("  verifyAndRewardAdWatch ✅ +5 coins")
        time.sleep(2)

    after = coins_now()
    log(f"\n== 完成 {ok}/{WATCHES} 轮 | coins: {before} → {after}（+{after - before}）==")

    # GitHub Actions 摘要
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## BlackFlame 签到结果\n\n")
            f.write(f"| 项目 | 值 |\n|---|---|\n")
            f.write(f"| 轮次 | {ok}/{WATCHES} 成功 |\n")
            f.write(f"| 金币 | {before} → {after}（+{after - before}）|\n")

    sys.exit(0 if ok == WATCHES else 2)


if __name__ == "__main__":
    main()
