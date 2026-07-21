#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitercloud (https://dashboard.bitercloud.lat) 服务器自动登录与续期脚本。
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
    log("⚡ Bitercloud 服务器自动登录续期")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    sb_kwargs = {"uc": True, "headless": HEADLESS, "xvfb": True}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
        log(f"🔗 挂载代理: {PROXY}")

    with SB(**sb_kwargs) as sb:
        # 1. 自动登录
        if USER and PASS:
            log(f"🔑 打开登录页面: {LOGIN_URL}")
            sb.open(LOGIN_URL)
            sb.wait_for_ready_state_complete()
            time.sleep(3)

            cur_url = sb.get_current_url()
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

                log("🖱️ 点击登录提交按钮...")
                if sb.is_element_visible('button[type="submit"]'):
                    sb.uc_click('button[type="submit"]')
                elif sb.is_element_visible('.btn-primary'):
                    sb.uc_click('.btn-primary')
                time.sleep(5)

            log("📍 登录完成，当前 URL:", sb.get_current_url())

        # 2. 打开服务器列表页面
        log(f"🌐 打开服务器列表页: {SERVERS_URL}")
        sb.open(SERVERS_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        # 提取页面上的关联按钮/链接
        inspect_res = sb.execute_script("""
            let items = [];
            document.querySelectorAll('form, button, a').forEach(el => {
                let action = el.getAttribute('action') || el.getAttribute('href') || '';
                let text = el.innerText || el.textContent || '';
                let method = el.getAttribute('method') || '';
                if (action || text) {
                    items.push({ tag: el.tagName, text: text.trim().substring(0, 50), action: action, method: method });
                }
            });
            return items;
        """)

        log("🔎 页面发现的交互元素 (前 20 个):")
        for idx, it in enumerate(inspect_res[:20]):
            log(f"   [{idx+1}] <{it.get('tag')}> text='{it.get('text')}' action='{it.get('action')}' method='{it.get('method')}'")

        renew_candidates = [
            f"/servers/{SERVER_ID}/renew",
            f"/servers/renew/{SERVER_ID}",
            f"/api/servers/{SERVER_ID}/renew"
        ]

        renew_success = False
        final_response = None

        for path in renew_candidates:
            log(f"🔄 尝试接口 path={path} (PATCH)...")
            res = sb.execute_script(f"""
                let cookies = document.cookie.split('; ');
                let xsrfCookie = cookies.find(row => row.startsWith('XSRF-TOKEN='));
                let xsrfValue = xsrfCookie ? decodeURIComponent(xsrfCookie.split('=')[1]) : '';

                return fetch('{path}', {{
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
            log(f"   结果 status={res.get('status')}, body={res.get('body', '')[:200]}")

            status_code = res.get("status")
            body = res.get("body", "")
            body_low = body.lower()

            if status_code == 200 or "renewed" in body_low or "success" in body_low or "ok" in body_low:
                renew_success = True
                final_response = body
                break

        if renew_success:
            msg = f"🎉 Bitercloud 服务器 ({SERVER_ID}) 自动续期成功！\n响应: {final_response}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        else:
            msg = f"ℹ️ Bitercloud 续期响应处理完成。具体日志见控制台。"
            log(msg)
            send_tg(msg)
            sys.exit(0)


if __name__ == "__main__":
    main()
