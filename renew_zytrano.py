#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zytrano (https://cp.zytrano.top) 服务器自动登录与续期脚本。
- 使用 SeleniumBase 在 Xvfb 虚拟显示屏中以真有头模式运行，完美过 Cloudflare Turnstile 验证码
- 自动填入账号密码登录 Zytrano 后端面板
- 登录成功后，通过 PATCH https://cp.zytrano.top/servers/renew/{server_id} 接口进行服务器续期
"""
import os
import sys
import time
import datetime
import requests

try:
    from seleniumbase import SB
except ImportError:
    sys.exit("缺少 seleniumbase 依赖，请先执行 pip install seleniumbase")

USER = os.environ.get("ZYTRANO_USER", "").strip()
PASS = os.environ.get("ZYTRANO_PASS", "").strip()
COOKIE = os.environ.get("ZYTRANO_COOKIE", "").strip()
SERVER_ID = (os.environ.get("ZYTRANO_SERVER_ID") or "nWUh0n4lbOYojO4M9ePOA").strip()

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

BASE_URL = "https://cp.zytrano.top"
LOGIN_URL = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"
RENEW_FULL_URL = f"{BASE_URL}/servers/renew/{SERVER_ID}"


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
    log("🚀 Zytrano 服务器自动登录续期启动")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    # 在 Xvfb 虚拟桌面下使用有头 UC 模式，能完美通过 Turnstile 验证码与 GUI 点击
    sb_kwargs = {"uc": True, "xvfb": True, "headless": False}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
        log(f"🔗 挂载代理: {PROXY}")

    with SB(**sb_kwargs) as sb:
        # Step 1: 静态 Cookie 逻辑（如有）
        if COOKIE and not (USER and PASS):
            log("🍪 写入静态 ZYTRANO_COOKIE...")
            sb.open(BASE_URL)
            sb.wait_for_ready_state_complete()
            for item in COOKIE.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    try:
                        sb.add_cookie({"name": k, "value": v, "domain": "cp.zytrano.top", "path": "/"})
                    except Exception as e:
                        log(f"添加 Cookie 提示 ({k}): {e}")
            sb.refresh()
            time.sleep(2)

        # Step 2: 填表登录与 Cloudflare Turnstile 处理
        if USER and PASS:
            log(f"🔑 导航至登录页面: {LOGIN_URL}")
            sb.open(LOGIN_URL)
            sb.wait_for_ready_state_complete()
            time.sleep(4)

            cur_url = sb.get_current_url()
            log("📍 当前页面 URL:", cur_url)

            if "login" in cur_url.lower():
                log(f"📝 自动填入账号 {USER[:3]}**** ...")
                if sb.is_element_visible('input[name="email"]'):
                    sb.type('input[name="email"]', USER, timeout=10)
                elif sb.is_element_visible('#email'):
                    sb.type('#email', USER, timeout=10)

                if sb.is_element_visible('input[name="password"]'):
                    sb.type('input[name="password"]', PASS, timeout=10)
                elif sb.is_element_visible('#password'):
                    sb.type('#password', PASS, timeout=10)

                # 处理 Cloudflare Turnstile 验证码
                log("🤖 尝试通过 Turnstile 验证码挑战...")
                try:
                    sb.uc_gui_click_captcha()
                    log("✅ 已尝试模拟点击 Cloudflare Turnstile 复选框")
                except Exception as e:
                    log(f"⚠️ Turnstile 交互提示: {e}")

                time.sleep(3)

                log("🖱️ 点击 Sign In 提交按钮...")
                if sb.is_element_visible('button[type="submit"]'):
                    sb.uc_click('button[type="submit"]')
                elif sb.is_element_visible('.btn-primary'):
                    sb.uc_click('.btn-primary')
                time.sleep(6)

            log("📍 登录完成，当前页面 URL:", sb.get_current_url())

        # Step 3: 导航到服务器列表页
        log(f"🌐 打开服务器列表页面: {SERVERS_URL}")
        sb.open(SERVERS_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        # Step 4: 在真浏览器环境中使用完整绝对路径发起 PATCH 续期请求
        log(f"🔄 发起 PATCH 续期请求 ({RENEW_FULL_URL})...")
        renew_res = sb.execute_script(f"""
            let cookies = document.cookie.split('; ');
            let xsrfCookie = cookies.find(row => row.startsWith('XSRF-TOKEN='));
            let xsrfValue = xsrfCookie ? decodeURIComponent(xsrfCookie.split('=')[1]) : '';

            return fetch('{RENEW_FULL_URL}', {{
                method: 'PATCH',
                credentials: 'include',
                headers: {{
                    'Accept': 'application/json, text/html, */*',
                    'Content-Type': 'application/json',
                    'X-XSRF-TOKEN': xsrfValue
                }}
            }}).then(async r => {{
                let txt = await r.text();
                return {{ status: r.status, body: txt }};
            }}).catch(err => ({{ status: 500, error: err.toString() }}));
        """)

        log("📊 续期 API 返回结果:", renew_res)

        status_code = renew_res.get("status") if isinstance(renew_res, dict) else 0
        body = renew_res.get("body", "") if isinstance(renew_res, dict) else str(renew_res)
        body_low = body.lower()

        if status_code in (200, 302) or "renewed" in body_low or "success" in body_low or "ok" in body_low:
            msg = f"🎉 Zytrano 服务器 ({SERVER_ID}) 全自动登录续期成功！\n响应: {body[:200]}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        elif status_code == 401 or "unauthenticated" in body_low:
            msg = f"❌ Zytrano 续期失败 (401 Unauthenticated)。未完成登录验证。"
            log(msg)
            send_tg(msg)
            sys.exit(1)
        else:
            msg = f"ℹ️ Zytrano 续期响应 (HTTP {status_code}): {body[:200]}"
            log(msg)
            send_tg(msg)
            sys.exit(0 if status_code and status_code < 500 else 1)


if __name__ == "__main__":
    main()
