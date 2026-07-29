#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCYED Hosting 5~14 天随机续期（真实浏览器自动过 CF 后登录）。"""
from __future__ import annotations
import datetime as dt
import json, os, random, sys, time
from cryptography.fernet import Fernet, InvalidToken

BASE = "https://scyed.com"
LOGIN_URL = f"{BASE}/en/login"
RENEW_URL = f"{BASE}/en/gameserver/6100ef84/upgrade/freeServer"
SESSION_URL = f"{BASE}/api/auth/get-session"
STATE_FILE = "scyed_state.json"
USER = os.environ.get("SCYED_USER", "").strip()
PASSWORD = os.environ.get("SCYED_PASS", "").strip()
SESSION_KEY = os.environ.get("SCYED_SESSION_KEY", "").strip()
PROXY = os.environ.get("SCYED_PROXY", "").strip()
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

class ScyedError(RuntimeError): pass

def log(x): print(x, flush=True)
def now_str(): return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
def send_tg(msg):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT_ID,"text":msg},timeout=15)
        except: pass

def load_state():
    try:
        with open(STATE_FILE,encoding="utf-8") as f:return json.load(f)
    except (FileNotFoundError,ValueError,OSError):return {}
def save_state(st):
    tmp=STATE_FILE+".tmp";open(tmp,"w",encoding="utf-8").write(json.dumps(st,ensure_ascii=False,indent=2));os.replace(tmp,STATE_FILE)
def save_browser_cookies(st,sb):
    if SESSION_KEY:
        cookies=sb.get_cookies()
        if cookies:
            st["encrypted_cookies"]=Fernet(SESSION_KEY.encode()).encrypt(json.dumps(cookies,separators=(",",":")).encode()).decode();st["session_saved_time"]=now_str()
def restore_browser_cookies(st,sb):
    if not SESSION_KEY or not st.get("encrypted_cookies"):return False
    try:cookies=json.loads(Fernet(SESSION_KEY.encode()).decrypt(st["encrypted_cookies"].encode()).decode())
    except (InvalidToken,ValueError,TypeError):return False
    try:
        sb.open(BASE);sb.wait_for_ready_state_complete()
        for c in cookies:
            try:sb.add_cookie({k:c[k] for k in ("name","value","domain","path","expiry","secure","httpOnly") if k in c})
            except:pass
        sb.refresh();sb.wait_for_ready_state_complete();time.sleep(2)
        return "login" not in sb.get_current_url().lower() and "attention required" not in sb.get_title().lower() and browser_session_valid(sb)
    except:return False

def should_run(st):
    if FORCE_RUN:return True
    nxt=int(st.get("next_renew_timestamp",0) or 0)
    if nxt and time.time()<nxt:
        log(f"⏳ SCYED 尚未到随机续期时间：{st.get('next_renew_time','未知')}");return False
    return True

def wait_cf_auto(sb):
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
    except Exception:
        sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(2)

    for i in range(12):
        title = sb.get_title().lower()
        source = sb.get_page_source().lower()
        if "sorry, you have been blocked" in source or "unable to access scyed.com" in source:
            raise ScyedError("SCYED Cloudflare 已封锁当前代理出口")
        if first_selector(sb, ["#identifier", "input[autocomplete*='username']", "input[type='email']"]):
            log(f"✅ SCYED 登录页已加载（等待约 {2*(i+1)} 秒）")
            return
        if "just a moment" not in title and "attention required" not in title:
            log(f"⏳ SCYED 页面已返回，等待登录表单渲染（第 {i+1}/12 次）")
        else:
            log(f"🛡️ 等待 SCYED Cloudflare 验证（第 {i+1}/12 次）")
        time.sleep(2)
    raise ScyedError("SCYED 登录表单未加载")

def browser_session_valid(sb):
    script = """
    const done=arguments[arguments.length-1];
    fetch(arguments[0],{credentials:'include',headers:{'Accept':'application/json'}})
      .then(async r=>done({status:r.status,text:await r.text()})).catch(e=>done({error:String(e)}));
    """
    # SeleniumBase 的第二个位置参数是 timeout；脚本参数要走原生 WebDriver。
    result=sb.driver.execute_async_script(script,SESSION_URL)
    if result.get('status') != 200:
        return False
    try:
        data=json.loads(result.get('text','{}'))
        return bool(data.get('user') or data.get('session'))
    except ValueError:
        return False


def first_selector(sb, selectors):
    for selector in selectors:
        try:
            if sb.is_element_present(selector) and sb.is_element_visible(selector): return selector
        except: pass
    return None

def turnstile_token(sb):
    try:
        return sb.execute_script("""
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (el && el.value) return el.value;
            try { return window.turnstile?.getResponse?.() || ''; } catch (e) { return ''; }
        """) or ""
    except Exception:
        return ""


def solve_turnstile(sb):
    if turnstile_token(sb):
        return
    log("🛡️ 使用 SeleniumBase UC 模式处理 SCYED Turnstile")
    attempts = (
        lambda: sb.uc_gui_click_cf(frame="#cf-turnstile", retry=True),
        lambda: sb.uc_gui_click_captcha(),
        lambda: sb.uc_gui_handle_cf(frame="#cf-turnstile"),
    )
    for index, action in enumerate(attempts, 1):
        try:
            action()
        except Exception as exc:
            log(f"⚠️ Turnstile 第 {index} 种点击方式跳过：{type(exc).__name__}")
        for _ in range(12):
            if turnstile_token(sb):
                log("✅ SCYED Turnstile token 已生成")
                return
            time.sleep(1)
    raise ScyedError("SCYED Turnstile 验证未通过")


def login(sb):
    wait_cf_auto(sb)
    # 在执行其它 WebDriver 操作前先使用 PyAutoGUI 点击闭合 Shadow DOM 内的验证框。
    solve_turnstile(sb)
    email=first_selector(sb,["#identifier","input[autocomplete*='username']","input[type='email']","input[name='email']","#email","input[name='username']","#username","input[name='user']"])
    password=first_selector(sb,["input[type='password']","input[name='password']","#password"])
    if not email or not password:
        raise ScyedError("未找到 SCYED 账号/密码输入框")
    log("🔑 Turnstile 通过，开始填写 SCYED 登录表单")
    sb.type(email,USER,timeout=10);sb.type(password,PASSWORD,timeout=10)
    button=first_selector(sb,["button[type='submit']","input[type='submit']","button.btn-primary"])
    if not button:raise ScyedError("未找到 SCYED 登录按钮")
    try:
        sb.wait_for_element_clickable(button, timeout=10)
    except Exception:
        raise ScyedError("SCYED 登录按钮仍被禁用（Turnstile 回调未完成）")
    log("🖱️ 点击 SCYED 登录按钮")
    sb.uc_click(button)
    time.sleep(6)
    if "login" in sb.get_current_url().lower():raise ScyedError("SCYED 登录后仍在登录页")
    if not browser_session_valid(sb):raise ScyedError("SCYED 登录后 /api/auth/get-session 未确认会话")
    log("✅ SCYED 登录成功，会话接口验证通过")

def parse_expiry_days(value):
    """Parse the English date rendered by SCYED, e.g. '28. August 2026 um 23:31'."""
    try:
        normalized = value.replace(" um ", " ").strip()
        expires = dt.datetime.strptime(normalized, "%d. %B %Y %H:%M").replace(tzinfo=dt.timezone.utc)
        return (expires - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def renew_in_browser(sb):
    sb.open(RENEW_URL);sb.wait_for_ready_state_complete();time.sleep(3)
    if "login" in sb.get_current_url().lower():
        raise ScyedError("SCYED 登录会话失效")

    button = "//button[contains(normalize-space(.), 'Extend for Free')]"
    expiry_value = "//*[normalize-space(text())='Expires on']/following-sibling::*[1]"
    if not sb.is_element_visible(button, by="xpath"):
        raise ScyedError("SCYED 续期页未找到 Extend for Free 按钮")

    try:
        before = sb.get_text(expiry_value, by="xpath", timeout=3).strip()
    except Exception:
        before = "未知"
    log(f"📅 SCYED 续期前到期时间：{before}")
    remaining_days = parse_expiry_days(before)
    if remaining_days is not None and remaining_days > 14:
        log(f"ℹ️ SCYED 还有约 {remaining_days:.1f} 天到期，已接近平台允许的最长时限，本轮正常跳过")
        return False, remaining_days

    log("🖱️ 点击 Extend for Free")
    sb.click(button, by="xpath")

    success = False
    after = before
    for _ in range(20):
        time.sleep(1)
        # Next.js 源码内始终包含全部翻译文案，只能检查当前可见 Toast。
        if sb.is_text_visible("Server Extended!") or sb.is_text_visible("Your free server has been extended successfully."):
            success = True
        try:
            current = sb.get_text(expiry_value, by="xpath", timeout=0.5).strip()
            if current:
                after = current
            if before != "未知" and after != before:
                success = True
        except Exception:
            pass
        if sb.is_text_visible("Extension Failed") or sb.is_text_visible("Could not extend your server. Please try again later."):
            raise ScyedError("SCYED 页面提示续期失败")
        if success:
            break

    log(f"📅 SCYED 续期后到期时间：{after}")
    if not success:
        raise ScyedError("点击续期后未检测到成功提示或到期时间变化")
    log("✅ SCYED 页面确认续期成功")
    return True, parse_expiry_days(after)

def main():
    log("🚀 SCYED 真实浏览器随机续期启动");log(f"🕐 北京时间：{now_str()}")
    if not USER or not PASSWORD:return log("❌ 缺少 SCYED_USER/SCYED_PASS") or 1
    st=load_state()
    if not should_run(st):return 0
    try:
        from seleniumbase import SB
        kw={
            "uc": True,
            "xvfb": True,
            "headless": False,
            "incognito": True,
            "locale": "en",
            "window_size": "1280,720",
            # Turnstile 动态子域只返回 AAAA，强制通过 IPv4 Anycast 访问。
            "host_resolver_rules": "MAP *.challenges.cloudflare.com 104.18.94.41, EXCLUDE localhost",
        }
        if PROXY:kw["proxy"]=PROXY
        with SB(**kw) as sb:
            reused=restore_browser_cookies(st,sb)
            if reused:log("♻️ 复用 SCYED 浏览器登录会话")
            else:login(sb)
            try:
                renewed, remaining = renew_in_browser(sb)
            except ScyedError as e:
                if "会话失效" not in str(e):raise
                log("🔐 确认会话失效，重新登录后重试")
                login(sb);renewed, remaining = renew_in_browser(sb)

            if renewed:
                days = random.randint(5,14)
            elif remaining is not None:
                # 保证下一轮最迟在到期前约 7 天执行。
                max_wait = max(1, int(remaining - 7))
                days = random.randint(5, min(14, max_wait)) if max_wait >= 5 else max_wait
            else:
                days = 5
            nxt=int(time.time()+days*86400)
            now_ts=int(time.time()); now_text=now_str()
            st.update({"last_check_timestamp":now_ts,"last_check_time":now_text,"next_interval_days":days,"next_renew_timestamp":nxt,"next_renew_time":dt.datetime.fromtimestamp(nxt,dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")})
            if renewed:
                st.update({"last_renew_timestamp":now_ts,"last_renew_time":now_text})
            save_browser_cookies(st,sb);save_state(st)
            result_text = "续期成功" if renewed else "当前时限充足，正常跳过"
            log(f"🎲 下次 SCYED 检查：{days} 天后")
            send_tg(f"✅ SCYED {result_text}\n🕐 {now_text}\n🎲 下次检查：{days} 天后")
            return 0
    except Exception as e:
        log(f"❌ SCYED 续期失败：{type(e).__name__}: {e}");send_tg(f"❌ SCYED 续期失败\n{e}");return 1
if __name__=="__main__":sys.exit(main())
