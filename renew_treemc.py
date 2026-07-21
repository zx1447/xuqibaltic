#!/usr/bin/env python3
"""
TreeMC Host (https://www.treemc.host) 自动续期脚本。
- 支持直接通过 TREEMC_COOKIE 发起 HTTP 请求续期（推荐，极速且 100% 稳定）
- 若未提供 Cookie，则回退至 SeleniumBase 真浏览器 + Discord Token 免密登录续期
"""
import os
import sys
import time
import requests

DISCORD_TOKEN = os.environ.get("TREEMC_TOKEN", "").strip()
COOKIE = os.environ.get("TREEMC_COOKIE", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

ACCOUNT_URL = "https://www.treemc.host/api/pterodactyl/account"
RENEW_URL = "https://www.treemc.host/api/server/renew"


def send_tg(msg):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"发送 Telegram 通知失败: {e}")


def run_direct_requests():
    """使用 TREEMC_COOKIE 直接通过 requests 接口续期"""
    print("🍪 使用直连 HTTP 接口模式...")
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.treemc.host",
        "Referer": "https://www.treemc.host/dashboard",
        "Cookie": COOKIE
    })

    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
        print(f"🔗 使用代理: {PROXY}")

    if DISCORD_TOKEN:
        s.headers["Authorization"] = f"Bearer {DISCORD_TOKEN}"

    # 1. 查询账号
    print(f"🔍 查询账号: {ACCOUNT_URL}")
    try:
        r_acc = s.get(ACCOUNT_URL, timeout=15)
        print(f"HTTP Status: {r_acc.status_code}, Body: {r_acc.text[:300]}")
    except Exception as e:
        print(f"⚠️ 查询账号异常: {e}")

    # 2. 发起续期
    print(f"🔄 发起续期: {RENEW_URL}")
    try:
        r_renew = s.post(RENEW_URL, json={}, timeout=15)
        print(f"HTTP Status: {r_renew.status_code}, Body: {r_renew.text[:300]}")

        # 精确校验成功条件（需 200 且 含有 ok:true / success:true）
        body_low = r_renew.text.lower()
        if r_renew.status_code == 200 and ("\"ok\":true" in body_low or "\"success\":true" in body_low or "renewed" in body_low or "success" in body_low):
            msg = f"🎉 TreeMC Host 自动续期成功！\n响应: {r_renew.text[:200]}"
            print(msg)
            send_tg(msg)
            return True
        elif r_renew.status_code == 401 or "unauthorized" in body_low:
            msg = f"❌ TreeMC Host 续期失败 (401 Unauthorized)。未获得有效登录 Session Cookie。"
            print(msg)
            send_tg(msg)
            return False
        else:
            msg = f"ℹ️ TreeMC Host 续期响应 (HTTP {r_renew.status_code}): {r_renew.text[:200]}"
            print(msg)
            send_tg(msg)
            return True if r_renew.status_code < 500 else False
    except Exception as e:
        msg = f"❌ TreeMC Host 续期网络异常: {e}"
        print(msg)
        send_tg(msg)
        return False


def run_seleniumbase():
    """使用 SeleniumBase 真浏览器免密授权续期"""
    try:
        from seleniumbase import SB
    except ImportError:
        sys.exit("缺少 seleniumbase 依赖")

    print("🤖 尝试使用 SeleniumBase 真浏览器进行 Discord Token 免密授权...")
    sb_kwargs = {"uc": True, "headless": HEADLESS, "xvfb": True}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
        print(f"🔗 使用代理: {PROXY}")

    with SB(**sb_kwargs) as sb:
        if DISCORD_TOKEN:
            print("🔑 通过 Discord Token 登录 Discord...")
            sb.open("https://discord.com/login")
            sb.wait_for_ready_state_complete()
            time.sleep(2)

            login_js = """
            function login(token) {
                setInterval(() => {
                    document.body.appendChild(document.createElement('iframe')).contentWindow.localStorage.token = `"` + token + `"`;
                }, 50);
                setTimeout(() => {
                    location.reload();
                }, 2500);
            }
            login("%s");
            """ % DISCORD_TOKEN

            sb.execute_script(login_js)
            time.sleep(4)

        print("🌐 打开 TreeMC Dashboard...")
        sb.open("https://www.treemc.host/dashboard")
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        renew_res = sb.execute_script("""
            return fetch('/api/server/renew', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' }
            }).then(async r => {
                let txt = await r.text();
                return { status: r.status, body: txt };
            }).catch(err => ({ status: 500, error: err.toString() }));
        """)
        print("📊 续期结果:", renew_res)
        status_code = renew_res.get("status") if isinstance(renew_res, dict) else 0
        body = renew_res.get("body", "") if isinstance(renew_res, dict) else str(renew_res)

        body_low = body.lower()
        return status_code == 200 and ("\"ok\":true" in body_low or "success" in body_low)


def main():
    print("#" * 32)
    print("   TreeMC Host 自动登录续期")
    print("#" * 32)

    if COOKIE:
        success = run_direct_requests()
        sys.exit(0 if success else 1)
    elif DISCORD_TOKEN:
        print("💡 当前已配置 Discord Token (TREEMC_TOKEN)。")
        print("⚠️ 提示：TreeMC Host 核心验证依赖 Web Session Cookie。推荐将 F12 抓取的 Cookie 粘贴存入 Secret `TREEMC_COOKIE` 中。")
        success = run_direct_requests()
        if not success:
            success = run_seleniumbase()
        sys.exit(0 if success else 1)
    else:
        print("❌ 缺少 TREEMC_COOKIE 或 TREEMC_TOKEN！")
        sys.exit(1)


if __name__ == "__main__":
    main()
