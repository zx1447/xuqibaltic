#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zytrano (https://cp.zytrano.top) 服务器自动登录与随机间隔续期脚本。
- 自动计算 2~10 天随机时间间隔，完美模拟真人的随机登录与续期习惯
- 使用 SeleniumBase (uc + Xvfb) 自动完成账号密码登录与 Cloudflare Turnstile 验证
- 登录完成后，构建标准 Laravel PATCH 伪造表单提交续期请求
- 续期成功后，随机计算并保存下一次触发节点至 zytrano_state.json 自动写回仓库
"""
import os
import sys
import json
import time
import random
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
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
PROXY = os.environ.get("PROXY_SERVER", "socks5://127.0.0.1:40001").strip()

BASE_URL = "https://cp.zytrano.top"
LOGIN_URL = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"
RENEW_ACTION_URL = f"{BASE_URL}/servers/renew/{SERVER_ID}"

STATE_FILE = "zytrano_state.json"


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


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ 读取状态文件失败: {e}")
    return {}


def save_state(next_renew_ts, interval_days, interval_hours):
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    next_dt = utc_now + datetime.timedelta(days=interval_days, hours=interval_hours) + datetime.timedelta(hours=8)
    
    state_data = {
        "last_renew_time": now_str(),
        "last_renew_timestamp": int(time.time()),
        "next_renew_timestamp": int(next_renew_ts),
        "next_interval_days": interval_days,
        "next_interval_hours": interval_hours,
        "next_renew_time": next_dt.strftime('%Y-%m-%d %H:%M:%S')
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)
        log(f"💾 最新运行状态与下一次随机时间 ({state_data['next_renew_time']}) 已保存至状态文件")
    except Exception as e:
        log(f"⚠️ 写入状态文件失败: {e}")


def should_execute():
    state = load_state()
    next_ts = state.get("next_renew_timestamp", 0)
    now_ts = int(time.time())

    if FORCE_RUN:
        log("⚡ 手动/环境变量触发 FORCE_RUN，忽略随机时间，立即执行续期！")
        return True

    if not next_ts:
        log("🆕 未检测到之前的续期记录，判定为第一次运行，立即执行续期！")
        return True

    if now_ts < next_ts:
        next_time_str = state.get("next_renew_time", "未知")
        diff_hours = (next_ts - now_ts) / 3600.0
        log(f"⏳ 尚未到预定的下一次随机续期节点。")
        log(f"   上次续期时间: {state.get('last_renew_time')}")
        log(f"   设定的随机间隔: {state.get('next_interval_days')} 天 {state.get('next_interval_hours', 0)} 小时")
        log(f"   下一次计划时间: {next_time_str} (还需等待约 {diff_hours:.1f} 小时)")
        log("🎲 完美模拟真人行为，优雅跳过本次检查。")
        return False

    log(f"🎯 已达到约定的随机续期时间节点！开启全自动续期流程...")
    return True


def update_random_schedule():
    # 2 ~ 10 天之间的随机天数
    rand_days = random.randint(2, 10)
    # 0 ~ 12 小时的微调抖动，实现高随机度
    rand_hours = random.randint(0, 12)
    next_ts = time.time() + (rand_days * 86400) + (rand_hours * 3600)

    log(f"🎲 成功生成下一次续期的随机间隔: {rand_days} 天 {rand_hours} 小时")
    save_state(next_ts, rand_days, rand_hours)
    return rand_days, rand_hours


def main():
    log("=" * 50)
    log("🚀 Zytrano 服务器自动登录与随机间隔续期启动")
    log(f"🕐 北京时间: {now_str()}")
    log(f"🖥 服务器 ID: {SERVER_ID}")
    log("=" * 50)

    # 校验 2~10 天随机间隔，未到时间则直接退出
    if not should_execute():
        sys.exit(0)

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

        # Step 3: 导航到服务器列表页
        log(f"🌐 打开服务器列表页面: {SERVERS_URL}")
        sb.open(SERVERS_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)

        # Step 4: 提交标准 Laravel PATCH 伪造表单
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
        log("📍 续期表单提交后 URL:", after_renew_url)

        if "login" not in after_renew_url.lower():
            # 续期成功，生成并保存下一次 2~10 天的随机触发节点
            d, h = update_random_schedule()
            msg = (f"🎉 Zytrano 服务器 ({SERVER_ID}) 全自动登录续期成功！\n"
                   f"🎲 自动计算下次续期间隔: {d} 天 {h} 小时")
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
