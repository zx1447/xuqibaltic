#!/usr/bin/env python3
"""
BalticHost / hostingmitherz 面板自动续期脚本。

流程：
  1. 用 SESSION_COOKIE 访问续期页面 (GET)，刷新 session 并从 <meta name="csrf-token"> 取 CSRF。
  2. 带 CSRF 以 multipart 形式 POST 续期接口。
  3. 解析返回 JSON：
       - success=true                -> 续期成功，退出 0
       - 未到续期窗口(7天前才可续)   -> 视为正常空跑，退出 0
       - cookie 失效 / 未登录 / 其它 -> 退出 1（让 Actions 报错通知你）

环境变量：
  SESSION_COOKIE  必填。整段 cookie，形如 "session=.eJx....." 或只填值 ".eJx....."。
  SERVER_ID       可选。默认 04dd7781。
  BASE_URL        可选。默认 https://blinky.baltichost.de
"""
import os
import re
import sys

try:
    import requests
except ImportError:
    sys.exit("缺少 requests 库，请先 `pip install requests`")

BASE_URL = os.environ.get("BASE_URL", "https://blinky.baltichost.de").rstrip("/")
SERVER_ID = (os.environ.get("SERVER_ID") or "04dd7781").strip()
RAW_COOKIE = os.environ.get("SESSION_COOKIE", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0")

# 这些消息表示"还没到续期窗口"，属正常，不算失败
NOT_IN_WINDOW_HINTS = (
    "nicht im verl",          # nicht im Verlängerungszeitraum verfügbar
    "not in the renewal",
    "not available",
    "verfügbar in",
)


def fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def parse_cookie(raw: str):
    """返回 session cookie 的值部分（去掉 'session=' 前缀）。"""
    if not raw:
        fail("未设置 SESSION_COOKIE 环境变量")
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("session="):
        raw = raw.split("=", 1)[1]
    # 万一用户把多个 cookie 粘一起，只取 session 那段
    raw = raw.split(";")[0].strip()
    return raw


def main() -> None:
    cookie_val = parse_cookie(RAW_COOKIE)

    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    })
    s.cookies.set("session", cookie_val, domain=BASE_URL.split("//", 1)[-1])

    page_url = f"{BASE_URL}/manage/server/{SERVER_ID}/renewal"
    renew_url = f"{BASE_URL}/manage/server/api/{SERVER_ID}/renewal/renew"

    # 1) 取续期页面
    try:
        r = s.get(page_url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": page_url,
        }, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        fail(f"访问续期页面失败: {e}")

    # 被重定向到登录页 / 非续期页 => cookie 多半失效
    if "/login" in r.url or "/auth" in r.url:
        fail(f"登录态失效，被重定向到 {r.url}。请更新 SESSION_COOKIE。")

    m = re.search(r'name="csrf-token"\s+content="([a-f0-9]+)"', r.text)
    if not m:
        title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        print(f"[debug] final_url={r.url} status={r.status_code} len={len(r.text)}")
        print(f"[debug] title={title.group(1).strip() if title else 'N/A'}")
        print(f"[debug] resp_headers={dict(r.headers)}")
        print(f"[debug] set_cookies={r.cookies.get_dict()}")
        if os.environ.get("DUMP_FULL"):
            print("[debug] FULL_BODY_START")
            print(r.text)
            print("[debug] FULL_BODY_END")
        else:
            print(f"[debug] head=\n{r.text[:600]}")
        fail("未在页面中找到 csrf-token，SESSION_COOKIE 可能已失效，请更新。")
    csrf = m.group(1)
    print(f"已获取 CSRF token: {csrf[:12]}...")

    # 打印一下到期信息（如果页面里有）
    exp = re.search(r'id="expiration-date"[^>]*>\s*([0-9.\s:]+)', r.text)
    if exp:
        print(f"当前到期时间: {exp.group(1).strip()}")

    # 2) POST 续期
    try:
        pr = s.post(renew_url, files={"csrf_token": (None, csrf)}, headers={
            "Accept": "*/*",
            "Origin": BASE_URL,
            "Referer": page_url,
        }, timeout=30)
    except requests.RequestException as e:
        fail(f"提交续期请求失败: {e}")

    # 3) 解析结果
    try:
        data = pr.json()
    except ValueError:
        fail(f"续期接口返回非 JSON (HTTP {pr.status_code}): {pr.text[:300]}")

    success = bool(data.get("success"))
    message = str(data.get("message", "")).strip()

    if success:
        print(f"✅ 续期成功: {message or 'OK'}")
        sys.exit(0)

    low = message.lower()
    if any(h in low for h in NOT_IN_WINDOW_HINTS):
        print(f"ℹ️ 尚未到续期窗口，跳过（正常）: {message}")
        sys.exit(0)

    # 其它 false：可能是 cookie/csrf 问题或接口变更，报错通知
    fail(f"续期失败 (HTTP {pr.status_code}): {message or data}")


if __name__ == "__main__":
    main()
