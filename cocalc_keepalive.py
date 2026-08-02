#!/usr/bin/env python3
"""CoCalc nezha probe keepalive (Playwright 版).

策略:
1. 用 remember_me cookie 打开 cocalc 项目
2. 创建新 terminal (触发 .bashrc 自启 nezha agent)
3. 等 10 秒让 nezha 启动
4. 关闭浏览器 (项目保持 active 30 分钟)

每 10 分钟跑一次, 项目永远不会 idle stop。
"""
import json, os, sys, time, urllib.request, urllib.error

COCALC_PROJECT = os.environ.get("COCALC_PROJECT", "").strip()
COCALC_COOKIE = os.environ.get("COCALC_COOKIE", "").strip()
NEZHA_PANEL = os.environ.get("NEZHA_PANEL", "https://nz.zxydk1715.dpdns.org").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

if not COCALC_PROJECT or not COCALC_COOKIE:
    print("[cocalc] ERROR: missing COCALC_PROJECT or COCALC_COOKIE")
    sys.exit(1)


def log(msg):
    print(f"[cocalc] {msg}", flush=True)


def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": TG_CHAT_ID, "text": f"[cocalc] {msg}"}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            data=data, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def check_nezha_online():
    """检查 nezha 面板 cocalc 是否在线 (用首页公开数据)"""
    try:
        req = urllib.request.Request(f"{NEZHA_PANEL}/api/v1/server-group",
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            # nezha v1 首页不返回 server 详情, 只能看公开数据
            return None  # unknown
    except Exception:
        return None


def main():
    log("=== cocalc keepalive start (Playwright) ===")
    
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        )
        
        # 设置 remember_me cookie
        ctx.add_cookies([{
            "name": "remember_me",
            "value": COCALC_COOKIE,
            "domain": ".cocalc.com",
            "path": "/",
        }, {
            "name": "remember_me",
            "value": COCALC_COOKIE,
            "domain": ".cocalc.ai",
            "path": "/",
        }])
        
        page = ctx.new_page()
        
        # 1. 打开项目
        log("🔄 打开 cocalc 项目...")
        try:
            page.goto(f"https://cocalc.ai/projects/{COCALC_PROJECT}/files/", 
                wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
        except Exception as e:
            log(f"❌ 打开项目失败: {e}")
            send_tg(f"打开项目失败: {e}")
            browser.close()
            return 1
        
        # 检查是不是真的登录了 (没跳到登录页)
        if "/auth/" in page.url or "sign_in" in page.url or "login" in page.url.lower():
            log(f"❌ cookie 过期, 跳到登录页: {page.url}")
            send_tg("cookie 过期, 需要更新 COCALC_COOKIE secret")
            browser.close()
            return 1
        
        log(f"  ✅ 项目页面已打开: {page.url[:60]}")
        
        # 2. 创建 terminal (点 "New" → "Terminal")
        log("🔄 创建 terminal...")
        terminal_created = False
        
        # 方法1: 点 New → Terminal 菜单
        try:
            # 等 "New" 按钮出现
            for _ in range(10):
                new_btn = page.query_selector('button:has-text("New")')
                if new_btn:
                    new_btn.click()
                    time.sleep(1)
                    # 点 Terminal
                    term_btn = page.query_selector('text=Terminal')
                    if term_btn:
                        term_btn.click()
                        log("  ✅ 通过 New → Terminal 创建")
                        terminal_created = True
                        break
                time.sleep(1)
        except Exception as e:
            log(f"  方法1失败: {e}")
        
        # 方法2: 直接用 URL 创建 terminal
        if not terminal_created:
            try:
                # CoCalc 支持 URL 直接开 terminal
                page.goto(f"https://cocalc.ai/projects/{COCALC_PROJECT}/files/keepalive.term",
                    wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
                log("  ✅ 通过 .term URL 创建")
                terminal_created = True
            except Exception as e:
                log(f"  方法2失败: {e}")
        
        # 方法3: 用键盘快捷键
        if not terminal_created:
            try:
                page.keyboard.press("Control+Shift+T")
                time.sleep(2)
                log("  ✅ 通过快捷键创建")
                terminal_created = True
            except Exception as e:
                log(f"  方法3失败: {e}")
        
        if terminal_created:
            log("  ✅ terminal 已创建, .bashrc 会自启 nezha-agent")
            # 等 10 秒让 nezha 启动
            log("  ⏳ 等 10 秒让 nezha 启动...")
            time.sleep(10)
        else:
            log("  ⚠️ 未能创建 terminal, 但项目已访问 (可能保持 active)")
        
        # 3. 检查 nezha (可选)
        online = check_nezha_online()
        if online is True:
            log("nezha: cocalc ONLINE")
        elif online is False:
            log("nezha: cocalc OFFLINE (terminal 刚开, nezha 可能还在启动)")
        else:
            log("nezha: 状态未知 (API 不公开)")
        
        browser.close()
    
    log("=== cocalc keepalive done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
