#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitercloud (https://dashboard.bitercloud.lat) 服务器自动登录与续期脚本。
使用 SeleniumBase 真浏览器模式（结合 Cloudflare WARP 代理）自动填写账号密码登录并完成 PATCH 续期。
"""
import os
import sys
import time
import urllib.parse
import datetime
import requests

try:
    from seleniumbase import SB
except ImportError:
    sys.exit("缺少 seleniumbase 依赖，请先执行 pip install seleniumbase")

USER = os.environ.get("BITERCLOUD_USER", "").strip()
PASS = os.environ.get("BITERCLOUD_PASS", "").strip()
COOKIE = os.environ.get("BITERCLOUD_COOKIE", "").strip()
SERVER_ID = (os.environ.get("BITERCLOUD_SERVER_ID") or "Oh1vni0cCpJ1GHW-4qZMj").strip()

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

BASE_URL = "https://dashboard.bitercloud.lat"
LOGIN_URL = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"
RENEW_PATH = f"/servers/{SERVER_ID}/renew"


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
    log("⚡ Bitercloud 服务器自动登录续期")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    sb_kwargs = {"uc": True, "headless": HEADLESS, "xvfb": True}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
        log(f"🔗 挂载代理: {PROXY}")

    with SB(**sb_kwargs) as sb:
        # Step 1: 静态 Cookie 逻辑（如有）
        if COOKIE and not (USER and PASS):
            log("🍪 写入静态 BITERCLOUD_COOKIE...")
            sb.open(BASE_URL)
            sb.wait_for_ready_state_complete()
            for item in COOKIE.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    try:
                        sb.add_cookie({"name": k, "value": v, "domain": "dashboard.bitercloud.lat", "path": "/"})
                    except Exception as e:
                        log(f"添加 Cookie 提示 ({k}): {e}")
            sb.refresh()
            time.sleep(2)

        # Step 2: 自动填写账号密码登录
        if USER and PASS:
            log(f"🔑 打开登录页面: {LOGIN_URL}")
            sb.open(LOGIN_URL)
            sb.wait_for_ready_state_complete()
            time.sleep(3)

            cur_url = sb.get_current_url()
            log(f"📍 当前页面 URL: {cur_url}")

            # 检查是否在登录页
            if "login" in cur_url.lower():
                log(f"📝 填入账号 {USER[:3]}**** ...")
                try:
                    # 匹配常见的 email/username 输入框
                    if sb.is_element_visible('input[name="email"]'):
                        sb.type('input[name="email"]', USER, timeout=10)
                    elif sb.is_element_visible('#email'):
                        sb.type('#email', USER, timeout=10)
                    elif sb.is_element_visible('input[type="email"]'):
                        sb.type('input[type="email"]', USER, timeout=10)

                    if sb.is_element_visible('input[name="password"]'):
                        sb.type('input[name="password"]', PASS, timeout=10)
                    elif sb.is_element_visible('#password'):
                        sb.type('#password', PASS, timeout=10)
                    elif sb.is_element_visible('input[type="password"]'):
                        sb.type('input[type="password"]', PASS, timeout=10)

                    log("🖱️ 点击登录提交按钮...")
                    if sb.is_element_visible('button[type="submit"]'):
                        sb.uc_click('button[type="submit"]')
                    elif sb.is_element_visible('.btn-primary'):
                        sb.uc_click('.btn-primary')
                    time.sleep(5)
                except Exception as e:
                    log(f"⚠️ 登录表单填写提示: {e}")

            log("📍 登录尝试完成，当前 URL:", sb.get_current_url())

        # Step 3: 打开服务器管理页面
        log(f"🌐 打开服务器列表页: {SERVERS_URL}")
        sb.open(SERVERS_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        # Step 4: 在真浏览器环境中精准执行 PATCH 续期请求
        log(f"🔄 发起 PATCH 续期请求 ({RENEW_PATH})...")
        renew_res = sb.execute_script(f"""
            let cookies = document.cookie.split('; ');
            let xsrfCookie = cookies.find(row => row.startsWith('XSRF-TOKEN='));
            let xsrfValue = xsrfCookie ? decodeURIComponent(xsrfCookie.split('=')[1]) : '';

            return fetch('{RENEW_PATH}', {{
                method: 'PATCH',
                credentials: 'include',
                headers: {{
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-XSRF-TOKEN': xsrfValue
                }}
            }}).then(async r => {{
                let txt = await r.text();
                return {{ status: r.status, body: txt }};
            }}).catch(err => ({{ status: 500, error: err.toString() }}));
        """)

        log(f"📊 续期 API 返回结果: {renew_res}")

        status_code = renew_res.get("status") if isinstance(renew_res, dict) else 0
        body = renew_res.get("body", "") if isinstance(renew_res, dict) else str(renew_res)
        body_low = body.lower()

        if status_code == 200 or "renewed" in body_low or "success" in body_low or "ok" in body_low:
            msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 自动续期成功！\n响应: {body}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        elif status_code == 401 or "unauthenticated" in body_low:
            msg = f"❌ Bitercloud 续期失败 (401 Unauthenticated)。登录失效，请检查账号密码。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        elif status_code == 419 or "csrf" in body_low:
            msg = f"❌ Bitercloud 续期失败 (419 CSRF Mismatch)。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        else:
            msg = f"ℹ️ Bitercloud 续期响应 (HTTP {status_code}): {body}"
            log(msg)
            send_tg(msg)
            sys.exit(0 if status_code and status_code < 500 else 1)


if __name__ == "__main__":
    main()
