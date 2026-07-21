#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitercloud (https://dashboard.bitercloud.lat) 服务器自动续期脚本。

规则说明：
- 续期接口：PATCH https://dashboard.bitercloud.lat/servers/{server_id}/renew
- 核心鉴权：需要在 Request Header 中携带包含 bitercloud_session 和 XSRF-TOKEN 的 Cookie
- 防伪标头：程序会自动从 Cookie 中提取 XSRF-TOKEN 并解码填入 X-XSRF-TOKEN 请求头
"""
import os
import sys
import time
import datetime
import urllib.parse
import requests

COOKIE = os.environ.get("BITERCLOUD_COOKIE", "").strip()
USER = os.environ.get("BITERCLOUD_USER", "").strip()
PASS = os.environ.get("BITERCLOUD_PASS", "").strip()
SERVER_ID = (os.environ.get("BITERCLOUD_SERVER_ID") or "Oh1vni0cCpJ1GHW-4qZMj").strip()

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

BASE_URL = "https://dashboard.bitercloud.lat"
RENEW_URL = f"{BASE_URL}/servers/{SERVER_ID}/renew"


def log(*args):
    print(" ".join(str(arg) for arg in args), flush=True)


def now_str():
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    bj_now = utc_now + datetime.timedelta(hours=8)
    return bj_now.strftime('%Y-%m-%d %H:%M:%S')


def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log(f"⚠️ Telegram 通知发送失败：{e}")


def main():
    log("=" * 50)
    log("⚡ Bitercloud 服务器自动续期启动")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    if not COOKIE:
        log("❌ 错误: 未设置 BITERCLOUD_COOKIE 环境变量！")
        log("💡 提示：由于 Bitercloud 登录页面防爬网关拦截，请在 F12 的 Network 请求集中复制 Cookie 值并填入 Secret `BITERCLOUD_COOKIE` 中。")
        sys.exit(1)

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/servers",
        "Cookie": COOKIE
    })

    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
        log(f"🔗 挂载代理: {PROXY}")

    # 提取 XSRF-TOKEN 自动生成 X-XSRF-TOKEN 请求头
    xsrf_token = None
    for item in COOKIE.split(";"):
        if "XSRF-TOKEN=" in item:
            xsrf_token = item.split("XSRF-TOKEN=", 1)[-1].strip()
            break

    if xsrf_token:
        decoded_xsrf = urllib.parse.unquote(xsrf_token)
        s.headers["X-XSRF-TOKEN"] = decoded_xsrf
        log("🔑 成功解析并注入 X-XSRF-TOKEN 防伪 Header")

    log(f"🔄 正在向 Bitercloud 发起 PATCH 续期请求...")
    log(f"📍 目标 URL: {RENEW_URL}")

    try:
        resp = s.patch(RENEW_URL, json={}, timeout=20)
        log(f"HTTP 状态码: {resp.status_code}")
        log(f"响应内容: {resp.text[:300]}")

        body_low = resp.text.lower()
        if resp.status_code == 200 or "renewed" in body_low or "success" in body_low or "ok" in body_low:
            msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 自动续期成功！\n响应内容: {resp.text[:200]}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        elif resp.status_code == 401 or "unauthenticated" in body_low:
            msg = f"❌ Bitercloud 续期失败 (401 Unauthenticated)。Cookie 已失效，请前往 GitHub Secrets 更新 BITERCLOUD_COOKIE。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        elif resp.status_code == 419 or "csrf" in body_low:
            msg = f"❌ Bitercloud 续期失败 (419 Page Expired / CSRF Token Mismatch)。请重新在 F12 中提取完整 Cookie 填入 BITERCLOUD_COOKIE。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        else:
            msg = f"ℹ️ Bitercloud 续期响应 (HTTP {resp.status_code}): {resp.text[:200]}"
            log(msg)
            send_tg(msg)
            sys.exit(0 if resp.status_code < 500 else 1)

    except Exception as e:
        msg = f"❌ Bitercloud 续期网络或脚本异常: {e}"
        log(msg)
        send_tg(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
