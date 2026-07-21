#!/usr/bin/env python3
"""
BalticHost / hostingmitherz 面板自动续期脚本。

两种鉴权方式（二选一）：

  A. 账号密码自动登录（推荐，cookie 永不过期）
     设置 BLINKY_USER 与 BLINKY_PASS，脚本每次运行先 RSA 加密密码登录
     /auth/validator 拿新鲜 session cookie，再续期。无需手动维护 cookie。

  B. 静态 session cookie（旧方式，需手动更新）
     只设置 SESSION_COOKIE（整段或仅值）。cookie 过期后需重新复制。

流程：
  1. 取得已登录的 session（方式 A 登录 / 方式 B 用给定 cookie）。
  2. GET 续期页面，从 <meta name="csrf-token"> 取 CSRF token。
  3. 带 CSRF 以 multipart 形式 POST 续期接口。
  4. 解析返回 JSON：
       - success=true                -> 续期成功，退出 0
       - 未到续期窗口(到期前7天才可续) -> 视为正常空跑，退出 0
       - cookie 失效 / 登录失败 / 其它 -> 退出 1（让 Actions 报错通知你）

环境变量：
  BLINKY_USER     可选。面板登录用户名（与 BLINKY_PASS 一起用 = 方式 A）。
  BLINKY_PASS     可选。面板登录密码（明文存在 secret 里，脚本端 RSA 加密后才发出）。
  SESSION_COOKIE  可选。整段 cookie "session=.eJx....." 或仅值 ".eJx....."（方式 B）。
  SERVER_ID       可选。默认 04dd7781。
  BASE_URL        可选。默认 https://blinky.baltichost.de
"""
import os
import re
import sys
import base64

try:
    import requests
except ImportError:
    sys.exit("缺少 requests 库，请先 `pip install requests`")

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:
    sys.exit("缺少 pycryptodome 库，请先 `pip install pycryptodome`")

BASE_URL = os.environ.get("BASE_URL", "https://blinky.baltichost.de").rstrip("/")
SERVER_ID = (os.environ.get("SERVER_ID") or "04dd7781").strip()
RAW_COOKIE = os.environ.get("SESSION_COOKIE", "").strip()
BLINKY_USER = os.environ.get("BLINKY_USER", "").strip()
BLINKY_PASS = os.environ.get("BLINKY_PASS", "").strip()

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


def antibot_guard(r) -> None:
    """被反爬防护盾拦截（数据中心 IP 常被拦，如 GitHub Actions 免费 runner）。"""
    if "M.E.O.W" in r.text or "I see you hiding" in r.text or r.status_code == 403:
        fail("被反爬防护页拦截 (M.E.O.W / 403)。该面板会拦截数据中心 IP，"
             "无法从 GitHub 云端 runner 访问。请在自己的电脑/家庭网络 IP 上运行"
             "（本机任务计划程序 或 自托管 runner）。")


def rsa_encrypt_password(pub_pem: str, password: str) -> str:
    """用页面内嵌的 RSA 公钥（PKCS1 v1.5，与 JSEncrypt 一致）加密密码，返回 base64。"""
    key = RSA.import_key(pub_pem)
    cipher = PKCS1_v1_5.new(key)
    enc = cipher.encrypt(password.encode("utf-8"))
    return base64.b64encode(enc).decode("ascii")


def extract_pubkey(html: str) -> str:
    m = re.search(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", html, re.S)
    if not m:
        fail("登录页未找到 RSA 公钥，面板可能已改版，请检查 /auth/login/。")
    return m.group(0)


def login(s: requests.Session) -> None:
    """方式 A：用账号密码登录，把已认证的 session cookie 写入 s.cookies。"""
    print(f"[login] 使用账号 {BLINKY_USER} 登录 {BASE_URL}/auth/validator ...")
    lr = s.get(f"{BASE_URL}/auth/login/", headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"{BASE_URL}/auth/login/",
    }, timeout=30, allow_redirects=True)
    if lr.status_code != 200:
        fail(f"获取登录页失败 (HTTP {lr.status_code})")
    antibot_guard(lr)

    pub = extract_pubkey(lr.text)
    enc_pwd = rsa_encrypt_password(pub, BLINKY_PASS)

    vr = s.post(f"{BASE_URL}/auth/validator",
                data={"username": BLINKY_USER, "password": enc_pwd},
                headers={
                    "Accept": "*/*",
                    "Referer": f"{BASE_URL}/auth/login/",
                    "Origin": BASE_URL,
                },
                allow_redirects=False, timeout=30)

    if not s.cookies.get("session"):
        fail("登录后未返回 session cookie，可能账号或密码错误。")

    body = vr.text or ""
    if "success" in body.lower() or vr.status_code in (200, 302):
        print("✅ 登录成功，已获取新的 session cookie。")
    else:
        # 失败响应通常带德文/英文错误，如 "Dieser Benutzername existiert nicht."
        fail(f"登录失败: {body[:200]}")


def parse_static_cookie(raw: str) -> str:
    """返回 session cookie 的值部分（去掉 'session=' 前缀）。"""
    if not raw:
        fail("未设置 SESSION_COOKIE，也未设置 BLINKY_USER/BLINKY_PASS。请至少配置一种鉴权方式。")
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("session="):
        raw = raw.split("=", 1)[1]
    raw = raw.split(";")[0].strip()   # 万一用户把多个 cookie 粘一起，只取 session 那段
    return raw


def main() -> None:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    })

    # 鉴权：优先方式 A（账号密码），否则方式 B（静态 cookie）
    if BLINKY_USER and BLINKY_PASS:
        login(s)
    else:
        cookie_val = parse_static_cookie(RAW_COOKIE)
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

    # 被重定向到登录页 => 登录态失效
    if "/login" in r.url or "/auth" in r.url:
        fail(f"登录态失效，被重定向到 {r.url}。请检查 BLINKY_USER/BLINKY_PASS 是否正确，"
             "或更新 SESSION_COOKIE。")
    antibot_guard(r)

    m = re.search(r'name="csrf-token"\s+content="([a-f0-9]+)"', r.text)
    if not m:
        fail("未在页面中找到 csrf-token，SESSION_COOKIE 可能已失效，请重新登录复制新的 session cookie。")
    csrf = m.group(1)
    print(f"已获取 CSRF token: {csrf[:12]}...")

    # 打印到期信息（页面里有的话）
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
