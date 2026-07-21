#!/usr/bin/env python3
"""
BalticHost / hostingmitherz 面板自动续期脚本（SeleniumBase 真浏览器版）。

为什么用真浏览器：
  面板有反爬防护盾（M.E.O.W），裸 requests 从数据中心 IP（如 GitHub 云端 runner）
  会被直接拦成一张反爬 HTML 页，连登录请求都发不出去。用 SeleniumBase 的
  uc（undetected-chromedriver）模式启动一个真 Chrome，能像真人浏览器一样
  通过防护盾 / Turnstile，从而完成登录与续期。

鉴权（二选一）：
  A. 账号密码（推荐）：设置 BLINKY_USER / BLINKY_PASS，脚本自动填表登录。
  B. 静态 cookie（旧）：设置 SESSION_COOKIE="session=...."，注入后直接进续期页。

续期规则：
  仅到期前 7 天内开放续期按钮；窗口外脚本会优雅跳过（退出 0）。

环境变量：
  BLINKY_USER     面板登录用户名（方式 A）
  BLINKY_PASS     面板登录密码（明文存 secret，仅在浏览器内填入）
  SESSION_COOKIE  可选，整段 "session=...."（方式 B）
  SERVER_ID       可选，默认 04dd7781
  BASE_URL        可选，默认 https://blinky.baltichost.de
  TG_BOT_TOKEN    可选，Telegram 机器人 token（通知用）
  TG_CHAT_ID      可选，Telegram 接收 chat id
  HEADLESS        可选，"true" 用无头（CI 默认）；"false" 用有头（本地调试）
"""
import os
import re
import sys
import time
from datetime import datetime

try:
    from seleniumbase import SB
except ImportError:
    sys.exit("缺少 seleniumbase，请先 `pip install seleniumbase`")

EMAIL = os.environ.get("BLINKY_USER", "").strip()
PASSWORD = os.environ.get("BLINKY_PASS", "").strip()
SESSION_COOKIE = os.environ.get("SESSION_COOKIE", "").strip()
SERVER_ID = (os.environ.get("SERVER_ID") or "04dd7781").strip()
BASE = os.environ.get("BASE_URL", "https://blinky.baltichost.de").rstrip("/")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

LOGIN_URL = f"{BASE}/auth/login/"
RENEWAL_URL = f"{BASE}/manage/server/{SERVER_ID}/renewal"

# 这些文案表示“还没到续期窗口”，属正常，不算失败
NOT_IN_WINDOW_HINTS = (
    "verfügbar in",        # "Verlängerung verfügbar in 26 Tage"
    "nicht im verl",       # "nicht im Verlängerungszeitraum"
    "not available",
    "not in the renewal",
)


def send_tg(token, chat_id, message):
    if not token or not chat_id:
        return
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        print("📨 Telegram 通知已发送" if resp.status_code == 200
              else f"❌ Telegram 发送失败: {resp.text}")
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}")


def mask(s):
    if "@" in s:
        local, domain = s.split("@", 1)
        if len(local) <= 2:
            return f"{local[0]}***@{domain}"
        return f"{local[:2]}****{local[-1]}@{domain}"
    if len(s) <= 2:
        return s[0] + "*"
    return s[:2] + "****" + s[-1]


def main():
    print("#" * 28)
    print("   BalticHost 自动登录续期")
    print("#" * 28)
    if not EMAIL and not PASSWORD and not SESSION_COOKIE:
        print("❌ 请设置 BLINKY_USER/BLINKY_PASS 或 SESSION_COOKIE")
        sys.exit(1)

    print(f"🌐 启动浏览器 (headless={HEADLESS})")
    proxy = os.environ.get("PROXY_SERVER", "").strip()
    sb_kwargs = {"uc": True, "headless": HEADLESS, "xvfb": True}
    if proxy:
        sb_kwargs["proxy"] = proxy
        print(f"🔗 使用代理: {proxy}")
    with SB(**sb_kwargs) as sb:
        # 1) 进入登录页（这一步会先过反爬盾）
        print(f"🌐 打开登录页 {LOGIN_URL}")
        sb.open(LOGIN_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(2)

        # 处理可能的 Turnstile / 反爬挑战
        try:
            sb.uc_gui_click_captcha()
            print("✅ 已尝试处理反爬/验证码挑战")
        except Exception as e:
            print(f"⚠️ 反爬挑战处理跳过: {e}")

        title = sb.get_title()
        if "M.E.O.W" in title or "hiding" in title:
            msg = "❌ 仍被反爬盾拦截（M.E.O.W）。该面板对数据中心 IP 极严，" \
                  "请改用家庭网络 IP（自托管 runner / 本机定时任务）。"
            print(msg)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
            sys.exit(1)

        # 2) 登录
        if EMAIL and PASSWORD:
            print(f"🔑 填写账号 {mask(EMAIL)} ...")
            sb.type("#username", EMAIL, timeout=10)
            sb.type("#password", PASSWORD, timeout=10)
            print("🖱️ 点击登录按钮 #loginBtn")
            sb.uc_click("#loginBtn")
            # 等待跳离登录页（成功会跳到 / 或 next）
            logged_in = False
            err_text = ""
            for _ in range(20):
                cur = sb.get_current_url()
                if "login" not in cur:
                    logged_in = True
                    break
                # 读取页面内错误提示
                try:
                    err = sb.get_text("#errorMessage", timeout=0.5)
                    if err and err.strip():
                        err_text = err.strip()
                except Exception:
                    pass
                time.sleep(1)
            if not logged_in:
                # 抓取更多诊断信息
                title = sb.get_title()
                page = sb.get_page_source()
                if "M.E.O.W" in title or "hiding" in title:
                    diag = "被反爬盾拦截（M.E.O.W）"
                elif "ip_banned" in page or "Access denied from your location" in page:
                    diag = "IP 在登录端点被封 (ip_banned)"
                elif "invalid_credentials" in page or "Invalid username or password" in page:
                    diag = "账号或密码错误 (invalid_credentials)"
                else:
                    diag = f"登录失败，页面错误提示: {err_text or '无'}"
                msg = f"❌ 登录后未跳转：{diag}"
                print(msg)
                send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
                sys.exit(1)
            print(f"✅ 登录成功，当前 URL: {sb.get_current_url()}")
        elif SESSION_COOKIE:
            print("🍪 注入静态 session cookie")
            cookie_val = SESSION_COOKIE.split("session=", 1)[-1].split(";")[0].strip()
            sb.open(BASE)  # 先到域名下放 cookie
            sb.add_cookie({
                "name": "session",
                "value": cookie_val,
                "domain": BASE.split("//", 1)[-1],
                "path": "/",
            })
            sb.refresh()
        else:
            print("❌ 未配置任何鉴权方式")
            sys.exit(1)

        # 3) 进入续期页
        print(f"📄 导航到续期页 {RENEWAL_URL}")
        sb.open(RENEWAL_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        page = sb.get_page_source()

        # 提取到期日（页面里有 id=expiration-date）
        exp = re.search(r'id="expiration-date"[^>]*>\s*([0-9.\s:]+)', page)
        exp_text = exp.group(1).strip() if exp else "未知"
        print(f"🕒 当前到期时间: {exp_text}")

        # 判断是否未到窗口
        low = page.lower()
        if any(h in low for h in NOT_IN_WINDOW_HINTS):
            msg = (f"ℹ️ 尚未到续期窗口（到期前 7 天才可续），跳过。\n"
                   f"👤 账户: {mask(EMAIL or 'cookie')}\n"
                   f"📅 到期时间: {exp_text}")
            print(msg)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
            sys.exit(0)

        # 4) 在窗口内：提交续期表单 #renewalForm
        if not sb.is_element_visible("#renewalForm", timeout=8):
            msg = "❌ 未找到续期表单 #renewalForm，可能面板改版或不在续期窗口"
            print(msg)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
            sys.exit(1)

        print("🔄 点击续期提交按钮 ...")
        try:
            sb.uc_click('#renewalForm button[type="submit"]', timeout=5)
        except Exception:
            # 退路：直接 submit 表单
            sb.execute_script("document.getElementById('renewalForm').requestSubmit();")

        # 等待结果：成功会 location.reload()；失败会 alert()
        time.sleep(4)
        # 处理可能的 alert（如“未到窗口”）
        try:
            alert = sb.driver.switch_to.alert
            alert_text = alert.text
            alert.accept()
            if any(h in alert_text.lower() for h in NOT_IN_WINDOW_HINTS):
                print(f"ℹ️ 弹窗提示未到窗口，跳过: {alert_text}")
                sys.exit(0)
            print(f"⚠️ 续期弹窗: {alert_text}")
        except Exception:
            pass

        # 重新读取页面判断是否成功（到期日应变化 / 出现成功提示）
        new_page = sb.get_page_source()
        new_exp = re.search(r'id="expiration-date"[^>]*>\s*([0-9.\s:]+)', new_page)
        new_exp_text = new_exp.group(1).strip() if new_exp else exp_text

        success = ("erfolgreich" in new_page.lower()
                   or "success" in new_page.lower()
                   or new_exp_text != exp_text)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if success:
            status = "✅ 续期成功"
            msg = (f"🇪🇪 BalticHost 续期通知\n\n{status}\n"
                   f"👤 账户: {mask(EMAIL or 'cookie')}\n"
                   f"📅 新到期时间: {new_exp_text}\n"
                   f"⏱️ 续期时间: {now_str}")
        else:
            status = "❌ 续期失败（到期时间未变化）"
            msg = (f"🇪🇪 BalticHost 续期通知\n\n{status}\n"
                   f"👤 账户: {mask(EMAIL or 'cookie')}\n"
                   f"📅 到期时间: {new_exp_text}\n"
                   f"⏱️ 时间: {now_str}")
        print(msg)
        send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
        sys.exit(0 if success else 1)

    print("🏁 脚本执行完毕")


if __name__ == "__main__":
    main()
