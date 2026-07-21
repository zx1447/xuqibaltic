#!/usr/bin/env python3
"""
TreeMC Host (https://www.treemc.host) 自动续期脚本。
规则：Free 账户需要每 12 小时续期一次，本脚本可通过 GitHub Actions 定时任务（每 4 小时）自动调用续期接口。

环境变量：
  TREEMC_TOKEN   TreeMC Host / Discord 鉴权 Token
  TREEMC_COOKIE  可选，浏览器 Cookie
  TG_BOT_TOKEN   可选，Telegram 通知 Bot Token
  TG_CHAT_ID     可选，Telegram Chat ID
  PROXY_SERVER   可选，代理地址（如 socks5://127.0.0.1:40001）
"""
import os
import sys
import json
import requests

TOKEN = os.environ.get("TREEMC_TOKEN", "").strip()
COOKIE = os.environ.get("TREEMC_COOKIE", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
PROXY = os.environ.get("PROXY_SERVER", "").strip()

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


def get_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.treemc.host",
        "Referer": "https://www.treemc.host/dashboard",
    })

    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
        print(f"🔗 使用代理: {PROXY}")

    if TOKEN:
        s.headers["Authorization"] = f"Bearer {TOKEN}" if not TOKEN.startswith("Bearer ") else TOKEN
        s.headers["x-discord-token"] = TOKEN
        s.headers["x-auth-token"] = TOKEN

    cookies_dict = {}
    if COOKIE:
        for item in COOKIE.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                cookies_dict[k] = v
    elif TOKEN:
        cookies_dict["next-auth.session-token"] = TOKEN
        cookies_dict["__Secure-next-auth.session-token"] = TOKEN
        cookies_dict["discord_token"] = TOKEN

    if cookies_dict:
        s.cookies.update(cookies_dict)

    return s


def main():
    print("#" * 32)
    print("   TreeMC Host 自动续期脚本")
    print("#" * 32)

    if not TOKEN and not COOKIE:
        print("❌ 缺少 TREEMC_TOKEN 或 TREEMC_COOKIE 环境变量！")
        sys.exit(1)

    session = get_session()

    print(f"🔍 检查账号信息: {ACCOUNT_URL}")
    try:
        resp_acc = session.get(ACCOUNT_URL, timeout=15)
        print(f"HTTP 状态码: {resp_acc.status_code}")
        print(f"响应内容: {resp_acc.text[:500]}")
    except Exception as e:
        print(f"⚠️ 查询账号信息时发生异常: {e}")

    print(f"🔄 发送续期请求: {RENEW_URL}")
    try:
        resp_renew = session.post(RENEW_URL, json={}, timeout=15)
        print(f"HTTP 状态码: {resp_renew.status_code}")
        print(f"响应内容: {resp_renew.text[:500]}")

        if resp_renew.status_code == 200:
            msg = f"✅ TreeMC Host 服务器续期请求成功！\n响应: {resp_renew.text[:200]}"
            print(msg)
            send_tg(msg)
            sys.exit(0)
        elif "Unauthorized" in resp_renew.text or resp_renew.status_code == 401:
            msg = f"❌ TreeMC Host 续期失败：未授权 (401 Unauthorized)。请更新 TREEMC_TOKEN 或 Session Cookie。"
            print(msg)
            send_tg(msg)
            sys.exit(1)
        else:
            msg = f"⚠️ TreeMC Host 续期响应状态码 ({resp_renew.status_code})：{resp_renew.text[:200]}"
            print(msg)
            send_tg(msg)
            sys.exit(0 if resp_renew.status_code < 500 else 1)

    except Exception as e:
        msg = f"❌ TreeMC Host 续期出现网络或脚本异常: {e}"
        print(msg)
        send_tg(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
