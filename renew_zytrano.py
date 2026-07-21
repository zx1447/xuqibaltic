#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zytrano (https://cp.zytrano.top) 服务器自动登录与续期脚本。
- 使用 SeleniumBase (uc + Xvfb) 自动完成账号密码登录与 Cloudflare Turnstile 验证
- 登录完成后，构建标准 Laravel PATCH 伪造表单提交续期请求
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
RENEW_ACTION_URL = f"{BASE_URL}/servers/renew/{SERVER_ID}"


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

        # Step 2: 填表登录与 Turnstile 处理
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

                log("🤖 尝试通过 Turnstile 验证码挑战...")
                try:
                    sb.uc_gui_click_captcha()
                    log("✅ 已尝试模拟点击 Turnstile 复选框")
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

        # Step 3: 打开服务器管理页面
        log(f"🌐 打开服务器列表页面: {SERVERS_URL}")
        sb.open(SERVERS_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        # 检查页面上是否有既有的 Renew 按钮并直接点击
        try:
            renew_btns = sb.find_elements(f"form[action*='renew/{SERVER_ID}'] button, button[form*='{SERVER_ID}'], a[href*='renew/{SERVER_ID}']")
            if renew_btns:
                log("🖱️ 在页面发现对应服务器的 Renew 按钮，直接模拟点击...")
                sb.uc_click(renew_btns[0])
                time.sleep(5)
        except Exception as e:
            log(f"寻找既有 Renew 按钮提示: {e}")

        # Step 4: 提交标准 Laravel POST + _method=PATCH 续期表单
        log(f"🔄 提交 Laravel PATCH 方法伪造表单 ({RENEW_ACTION_URL})...")
        sb.execute_script(f"""
            let form = document.createElement('form');
            form.method = 'POST';
            form.action = '{RENEW_ACTION_URL}';

            let mInput = document.createElement('input');
            mInput.type = 'hidden';
            mInput.name = '_method';
            mInput.value = 'PATCH';
            form.appendChild(mInput);

            let tInput = document.createElement('input');
            tInput.type = 'hidden';
            tInput.name = '_token';
            let csrfMeta = document.querySelector('meta[name="csrf-token"]');
            tInput.value = csrfMeta ? csrfMeta.content : '';
            form.appendChild(tInput);

            document.body.appendChild(form);
            form.submit();
        """)

        time.sleep(6)
        after_renew_url = sb.get_current_url()
        page_src = sb.get_page_source().lower()

        log("📍 续期表单提交后 URL:", after_renew_url)

        if "login" not in after_renew_url.lower():
            msg = f"🎉 Zytrano 服务器 ({SERVER_ID}) 全自动登录续期成功！\n当前页面: {after_renew_url}"
            log(msg)
            send_tg(msg)
            sys.exit(0)
        else:
            msg = f"❌ Zytrano 续期失败：登录已失效，跳转至 {after_renew_url}"
            log(msg)
            send_tg(msg)
            sys.exit(1)


if __name__ == "__main__":
    main()
