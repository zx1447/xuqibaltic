#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitercloud (https://dashboard.bitercloud.lat) 服务器自动续期脚本。

续期规则：
- 目标接口: PATCH https://dashboard.bitercloud.lat/servers/{server_id}/renew
- 必须带 bitercloud_session 和 XSRF-TOKEN Cookie
- 动态在请求头传递 X-XSRF-TOKEN (对 XSRF-TOKEN 提取并 url-decode 解码)
"""
import os
import sys
import json
import time
import datetime
import urllib.parse
import requests

COOKIE = os.environ.get("BITERCLOUD_COOKIE", "").strip()
SERVER_ID = (os.environ.get("BITERCLOUD_SERVER_ID") or "Oh1vni0cCpJ1GHW-4qZMj").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

BASE_URL = "https://dashboard.bitercloud.lat"
RENEW_URL = f"{BASE_URL}/servers/{SERVER_ID}/renew"


def log(msg):
    print(msg, flush=True)


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
        log("❌ 错误: 未设置 BITERCLOUD_COOKIE 环境变量！请在 GitHub Secrets 中配置 BITERCLOUD_COOKIE。")
        sys.exit(1)

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/servers",
        "Cookie": COOKIE
    })

    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
        log(f"🔗 挂载代理: {PROXY}")

    xsrf_token = None
    for item in COOKIE.split(";"):
        if "XSRF-TOKEN=" in item:
            xsrf_token = item.split("XSRF-TOKEN=", 1)[-1].strip()
            break

    if xsrf_token:
        decoded_xsrf = urllib.parse.unquote(xsrf_token)
        s.headers["X-XSRF-TOKEN"] = decoded_xsrf
        log("🔑 已解析并自动填入 X-XSRF-TOKEN 请求头")

    log(f"🔄 发起 PATCH 续期请求: {RENEW_URL}")
    try:
        resp = s.patch(RENEW_URL, json={}, timeout=20)
        log(f"HTTP Status: {resp.status_code}")
        log(f"Response Body: {resp.text[:300]}")

        body_low = resp.text.lower()
        if resp.status_code == 200 or "renewed" in body_low or "success" in body_low or "ok" in body_low:
            msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 续期成功！\n响应: {resp.text[:200]}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        elif resp.status_code == 401 or "unauthenticated" in body_low:
            msg = f"❌ Bitercloud 续期失败 (401 Unauthenticated)。Cookie 已失效，请在 Secrets 中更新 BITERCLOUD_COOKIE。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        elif resp.status_code == 419 or "csrf" in body_low:
            msg = f"❌ Bitercloud 续期失败 (419 Page Expired / CSRF Token Mismatch)。请重新提取完整的 Cookie。"
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
