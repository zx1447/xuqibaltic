#!/usr/bin/env python3
"""
TreeMC Host (https://www.treemc.host) 自动登录与续期脚本（SeleniumBase 真浏览器版）。
"""
import os
import sys
import time
import requests

try:
    from seleniumbase import SB
except ImportError:
    sys.exit("缺少 seleniumbase 依赖，请先执行 pip install seleniumbase")

DISCORD_TOKEN = os.environ.get("TREEMC_TOKEN", "").strip()
COOKIE = os.environ.get("TREEMC_COOKIE", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()


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


def main():
    print("#" * 32)
    print("   TreeMC Host 自动登录续期")
    print("#" * 32)

    sb_kwargs = {"uc": True, "headless": HEADLESS, "xvfb": True}
    if PROXY:
        sb_kwargs["proxy"] = PROXY
        print(f"🔗 使用代理: {PROXY}")

    with SB(**sb_kwargs) as sb:
        # Step 1: 写入静态 Session Cookie（如有）
        if COOKIE:
            print("🍪 注入 TREEMC_COOKIE 静态 Session...")
            sb.open("https://www.treemc.host")
            sb.wait_for_ready_state_complete()
            for item in COOKIE.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    try:
                        sb.add_cookie({
                            "name": k,
                            "value": v,
                            "domain": ".treemc.host",
                            "path": "/",
                        })
                    except Exception as e:
                        print(f"添加 Cookie 提示 ({k}): {e}")
            sb.refresh()
            time.sleep(2)

        # Step 2: 通过 Discord Token 进行免密登录
        if DISCORD_TOKEN:
            print("🔑 正在通过 Discord Token 进行免密授权登录...")
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
            print("✅ Discord Token 注入完成，当前 Discord URL:", sb.get_current_url())

        # Step 3: 打开 TreeMC Dashboard
        print("🌐 导航到 TreeMC Host...")
        sb.open("https://www.treemc.host/dashboard")
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        print("📍 当前页面 URL:", sb.get_current_url())

        # 如果跳转到了 Discord 授权页面，自动点击 Authorize
        if "discord.com/oauth2/authorize" in sb.get_current_url():
            print("🖱️ 处于 Discord 授权界面，点击 Authorize 按钮...")
            time.sleep(3)
            try:
                if sb.is_element_visible("button[type='submit']"):
                    sb.uc_click("button[type='submit']")
                elif sb.is_element_visible("button:contains('Authorize')"):
                    sb.uc_click("button:contains('Authorize')")
                time.sleep(5)
            except Exception as e:
                print(f"授权点击提示: {e}")

        # 如果在 TreeMC 且有 Login / Discord 关联按钮
        page_source = sb.get_page_source()
        if ("login" in sb.get_current_url().lower() or "login" in page_source.lower()) and "discord.com" not in sb.get_current_url():
            print("🖱️ 尝试寻找并点击 Discord 登录/授权按钮...")
            try:
                if sb.is_element_visible("a[href*='discord']"):
                    sb.uc_click("a[href*='discord']")
                elif sb.is_element_visible("button:contains('Login')"):
                    sb.uc_click("button:contains('Login')")
                elif sb.is_element_visible("a:contains('Login')"):
                    sb.uc_click("a:contains('Login')")
                time.sleep(5)
            except Exception as e:
                print(f"点击登录按钮提示: {e}")

        # 再检测一次授权
        if "discord.com/oauth2/authorize" in sb.get_current_url():
            print("🖱️ 处于 Discord 授权界面，点击 Authorize 按钮...")
            time.sleep(3)
            try:
                if sb.is_element_visible("button[type='submit']"):
                    sb.uc_click("button[type='submit']")
                elif sb.is_element_visible("button:contains('Authorize')"):
                    sb.uc_click("button:contains('Authorize')")
                time.sleep(5)
            except Exception as e:
                print(f"授权点击提示: {e}")

        print("📍 处理后页面 URL:", sb.get_current_url())

        # 先查账号信息
        acc_res = sb.execute_script("""
            return fetch('/api/pterodactyl/account', {
                method: 'GET',
                credentials: 'include'
            }).then(async r => {
                let txt = await r.text();
                return { status: r.status, body: txt };
            }).catch(err => ({ status: 500, error: err.toString() }));
        """)
        print("🔍 账号接口返回:", acc_res)

        # 触发续期 API
        print("🔄 发起续期 API 请求...")
        renew_res = sb.execute_script("""
            return fetch('/api/server/renew', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json'
                }
            }).then(async r => {
                let txt = await r.text();
                return { status: r.status, body: txt };
            }).catch(err => ({ status: 500, error: err.toString() }));
        """)

        print("📊 续期 API 返回结果:", renew_res)

        status_code = renew_res.get("status") if isinstance(renew_res, dict) else 0
        body = renew_res.get("body", "") if isinstance(renew_res, dict) else str(renew_res)

        if status_code == 200 or "success" in body.lower() or "renewed" in body.lower():
            msg = f"🎉 TreeMC Host 自动续期成功！\n返回结果: {body}"
            print(msg)
            send_tg(msg)
            sys.exit(0)
        elif status_code == 401 or "unauthorized" in body.lower():
            msg = f"❌ TreeMC Host 续期失败 (401 Unauthorized)。未获得有效登录状态。\n账号响应: {acc_res}"
            print(msg)
            send_tg(msg)
            sys.exit(1)
        else:
            msg = f"ℹ️ TreeMC Host 续期响应 (HTTP {status_code}): {body}"
            print(msg)
            send_tg(msg)
            sys.exit(0 if status_code and status_code < 500 else 1)


if __name__ == "__main__":
    main()
