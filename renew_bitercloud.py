#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitercloud (https://dashboard.bitercloud.lat) 服务器全自动登录与续期脚本。
- 自动从网络池轮询可用代理节点绕过网关封锁 (/blocked)
- 使用账号密码自动登录，获取最新 Session 凭证
- 自动向 PATCH /servers/{server_id}/renew 接口发起续期请求
"""
import os
import sys
import re
import time
import datetime
import urllib.parse
import concurrent.futures
import requests

USER = os.environ.get("BITERCLOUD_USER", "").strip()
PASS = os.environ.get("BITERCLOUD_PASS", "").strip()
COOKIE = os.environ.get("BITERCLOUD_COOKIE", "").strip()
SERVER_ID = (os.environ.get("BITERCLOUD_SERVER_ID") or "Oh1vni0cCpJ1GHW-4qZMj").strip()

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
CUSTOM_PROXY = os.environ.get("CUSTOM_PROXY", "").strip()

BASE_URL = "https://dashboard.bitercloud.lat"
LOGIN_URL = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"
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


def get_public_proxies():
    log("🌐 正在获取公共代理节点列表...")
    proxy_sources = [
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
    ]
    proxies = []
    for url in proxy_sources:
        try:
            r = requests.get(url, timeout=5)
            lines = [l.strip() for l in r.text.strip().splitlines() if l.strip()]
            for line in lines:
                if "socks" not in line and "http" not in line:
                    prefix = "socks5://" if "socks5" in url else "http://"
                    proxies.append(f"{prefix}{line}")
                else:
                    proxies.append(line)
        except Exception:
            pass
    return list(dict.fromkeys(proxies))


def try_login_and_renew_via_proxy(proxy):
    s = requests.Session()
    s.proxies = {"http": proxy, "https": proxy}
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
    })

    try:
        # 1. 打开登录页面并提取 CSRF _token
        r_get = s.get(LOGIN_URL, allow_redirects=False, timeout=5)
        if r_get.status_code != 200:
            return None

        tokens = re.findall(r'name="_token"\s+value="([^"]+)"', r_get.text)
        token_val = tokens[0] if tokens else ""

        xsrf_raw = s.cookies.get("XSRF-TOKEN")
        if xsrf_raw:
            s.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf_raw)

        payload = {"_token": token_val, "email": USER, "password": PASS}

        # 2. 提交账号密码登录
        r_post = s.post(LOGIN_URL, data=payload, allow_redirects=False, timeout=8)
        if r_post.status_code not in (200, 302) or r_post.headers.get("Location") == f"{BASE_URL}/blocked":
            return None

        # 重新同步登录后生成的最新防伪 Token
        new_xsrf = s.cookies.get("XSRF-TOKEN")
        if new_xsrf:
            s.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(new_xsrf)

        s.headers["Accept"] = "application/json, text/html, */*"
        s.headers["Referer"] = SERVERS_URL

        # 3. 发起 PATCH 续期请求
        r_renew = s.patch(RENEW_URL, timeout=8)
        return (proxy, r_renew.status_code, r_renew.text)
    except Exception:
        return None


def main():
    log("=" * 50)
    log("⚡ Bitercloud 服务器自动登录续期启动")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    # 1. 优先使用 COOKIE 模式 (如有)
    if COOKIE:
        log("🍪 方式 1: 使用已配置的 BITERCLOUD_COOKIE 发起续期...")
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": SERVERS_URL,
            "Cookie": COOKIE
        })
        xsrf_token = None
        for item in COOKIE.split(";"):
            if "XSRF-TOKEN=" in item:
                xsrf_token = item.split("XSRF-TOKEN=", 1)[-1].strip()
                break
        if xsrf_token:
            s.headers["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf_token)

        try:
            resp = s.patch(RENEW_URL, json={}, timeout=20)
            log(f"HTTP Status: {resp.status_code}, Body: {resp.text[:300]}")
            if resp.status_code == 200 or "renewed" in resp.text.lower() or "ok" in resp.text.lower():
                msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 续期成功！\n响应: {resp.text[:200]}"
                log(msg)
                send_tg(msg)
                sys.exit(0)
            else:
                log("⚠️ Cookie 模式未通过，自动切换至账号密码全自动登录模式...")
        except Exception as e:
            log(f"⚠️ Cookie 模式执行异常: {e}")

    # 2. 账号密码全自动登录续期模式
    if USER and PASS:
        log(f"🔑 方式 2: 使用账号密码 ({USER[:3]}****) 进行全自动登录续期...")

        proxy_list = []
        if CUSTOM_PROXY:
            proxy_list.append(CUSTOM_PROXY)

        proxy_list.extend(get_public_proxies()[:300])

        log(f"🔍 正在并发轮询可用网络节点（池大小: {len(proxy_list)}）...")
        success = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
            results = executor.map(try_login_and_renew_via_proxy, proxy_list)
            for res in results:
                if res:
                    proxy, status_code, body = res
                    log(f"🎉 成功匹配可用节点 [{proxy}] 并完成全自动续期！")
                    log(f"   HTTP 状态码: {status_code}")
                    log(f"   响应内容: {body[:300]}")

                    if status_code == 200 or "renewed" in body.lower() or "ok" in body.lower() or "success" in body.lower():
                        msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 全自动登录续期成功！\n使用节点: {proxy}\n响应: {body[:200]}"
                        log(msg)
                        send_tg(msg)
                        success = True
                        break

        if success:
            sys.exit(0)
        else:
            msg = "❌ Bitercloud 自动续期失败：未找到有效代理节点。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
    else:
        log("❌ 缺失配置：请提供 BITERCLOUD_USER / BITERCLOUD_PASS 或 BITERCLOUD_COOKIE。")
        sys.exit(1)


if __name__ == "__main__":
    main()
