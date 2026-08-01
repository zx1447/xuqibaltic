#!/usr/bin/env python3
# DigitalPlat 免费域名自动续期（Camoufox + Panel API 版本）
#
# 流程：
#   1. Developer API (domain-api.digitalplat.org) 列出域名 + 检查到期
#   2. Camoufox (反检测 Firefox) 过 CF 挑战 → GitHub OAuth 登录 dash
#   3. 在 dash 浏览器上下文里调 Panel API 续期每个域名
#      真实端点: POST /_panel_api/api/domains/{d}/renew body {"renewal_type":"free","years":1}
#      需要: dash session cookie + X-CSRF-Token header (从 cookie panel_csrf_token 读)
#   4. 状态区分: renewed / skip_not_expiring (>120 天 DigitalPlat 政策禁止) / renew_failed
#
# 历史: 旧版用 Playwright headless Chromium 点击 "申请免费续费" 按钮,
#       2026-08 失效 (页面 UI 改版, 按钮文字变了). 改用 Panel API 直调更稳.
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://domain-api.digitalplat.org/api/v1"
DATE_FORMAT = "%Y-%m-%d"
DEFAULT_RENEW_BEFORE_DAYS = 120  # DigitalPlat 政策: >120 天不允许免费续期

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


@dataclass
class DomainRecord:
    name: str
    raw: dict[str, Any]
    expiry_date: datetime
    status: str | None
    can_renew: bool | None

    @property
    def days_remaining(self) -> int:
        now = datetime.now(timezone.utc)
        return (self.expiry_date.date() - now.date()).days


class DigitalPlatClient:
    def __init__(self, api_token: str, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
            "Origin": "https://dash.domain.digitalplat.org",
            "Referer": "https://dash.domain.digitalplat.org/",
        }

    def _request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(
                f"{self.api_base}{path}",
                data=body,
                headers=self.headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_exc = exc
                if exc.code in (403, 429) and attempt < 3:
                    wait = 15 * (attempt + 1)
                    print(f"⚠️ HTTP {exc.code}（attempt {attempt+1}/4），{wait}s 后重试…", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"DigitalPlat HTTP {exc.code}: {detail[:200]}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"DigitalPlat network error: {exc}") from exc
        else:
            raise RuntimeError(f"DigitalPlat: 多次重试仍失败 ({last_exc})")

        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DigitalPlat returned non-JSON response: {text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"DigitalPlat returned unexpected response: {parsed}")
        return parsed

    def list_domains(self) -> list[dict[str, Any]]:
        data = self._request("/domains")
        payload = unwrap_response(data)
        domains = payload.get("domains") if isinstance(payload, dict) else payload
        if not isinstance(domains, list):
            raise RuntimeError(f"DigitalPlat domain list response is missing domains: {data}")
        return [item for item in domains if isinstance(item, dict)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DigitalPlat free-domain renewal helper (Camoufox + Panel API).")
    parser.add_argument("--state", default="state/domains-state.json", help="Path to state JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without renewing or writing state.")
    parser.add_argument("--force", action="store_true", help="Try renew even if >120 days remaining (DigitalPlat may 400).")
    return parser.parse_args()


def unwrap_response(data: dict[str, Any]) -> Any:
    if "success" in data and data.get("success") is False:
        message = data.get("error") or data.get("message") or data
        raise RuntimeError(f"DigitalPlat API returned an error: {message}")
    if "data" in data:
        return data["data"]
    return data


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_domain_variable() -> list[str]:
    raw = require_env("DIGITALPLAT_DOMAINS")
    normalized = raw.replace(",", "\n")
    domains = [line.strip().lower() for line in normalized.splitlines() if line.strip()]
    if not domains:
        raise RuntimeError("DIGITALPLAT_DOMAINS is empty.")
    return domains


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"domains": {}}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_datetime(value: Any) -> datetime:
    if not value:
        raise ValueError("empty date")
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    if len(text) == 10:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Unsupported date format: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pick_first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_domain(raw: dict[str, Any]) -> DomainRecord:
    name = pick_first(raw, ("domain", "name", "full_domain"))
    if not name:
        raise RuntimeError(f"Domain record is missing a domain name: {raw}")
    expiry = pick_first(raw, ("expiry_date", "expires_at", "expiryDate", "expiresAt", "expiration_date"))
    if not expiry:
        raise RuntimeError(f"Domain record is missing expiry_date: {raw}")
    return DomainRecord(
        name=str(name).strip().lower(),
        raw=raw,
        expiry_date=parse_datetime(expiry),
        status=None if raw.get("status") is None else str(raw.get("status")),
        can_renew=None,
    )


def update_state_for_record(
    state: dict[str, Any],
    name: str,
    action: str,
    reason: str = "",
    new_expiry: str = "",
) -> None:
    domains = state.setdefault("domains", {})
    item = domains.setdefault(name, {})
    item["last_action"] = action
    if reason:
        item["last_action_reason"] = reason
    elif "last_action_reason" in item:
        del item["last_action_reason"]
    if new_expiry:
        item["expiry_date"] = new_expiry
        try:
            days = (parse_datetime(new_expiry).date() - datetime.now(timezone.utc).date()).days
            item["days_remaining"] = days
        except Exception:
            pass
    item["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# Camoufox + Panel API 续期
# ============================================================

def make_fetch_js(method: str, url: str, payload: dict | None = None, extra_headers: dict | None = None) -> str:
    """生成在浏览器上下文执行的 fetch JS 代码"""
    if payload is None:
        body_clause = "body: undefined,"
        ct_clause = ""
    else:
        payload_json = json.dumps(payload)
        body_clause = f"body: JSON.stringify({payload_json}),"
        ct_clause = "'Content-Type': 'application/json',"
    extra = ""
    if extra_headers:
        for k, v in extra_headers.items():
            # 转义单引号
            v_escaped = str(v).replace("'", "\\'")
            extra += f"'{k}': '{v_escaped}',"
    return f"""
    async () => {{
        try {{
            const r = await fetch('{url}', {{
                method: '{method}',
                credentials: 'include',
                headers: {{ 'Accept': 'application/json, text/plain, */*', {ct_clause} {extra} }},
                {body_clause}
            }});
            const text = await r.text();
            return {{ status: r.status, body: text.substring(0, 2000), ct: r.headers.get('content-type') || '' }};
        }} catch (e) {{
            return {{ error: String(e) }};
        }}
    }}
    """


def get_csrf_js() -> str:
    """从 cookie panel_csrf_token 读 CSRF token，若无则请求 /_panel_api/api/security/csrf"""
    return """
    async () => {
        const cookieMatch = document.cookie.match(/panel_csrf_token=([^;]+)/);
        if (cookieMatch) return { csrf: decodeURIComponent(cookieMatch[1]), source: 'cookie' };
        try {
            const r = await fetch('/_panel_api/api/security/csrf', { credentials: 'include', cache: 'no-store' });
            const data = await r.json().catch(() => null);
            if (data && typeof data.csrf_token === 'string') {
                return { csrf: data.csrf_token, source: 'endpoint' };
            }
            return { error: 'csrf endpoint returned: ' + JSON.stringify(data) };
        } catch (e) {
            return { error: String(e) };
        }
    }
    """


def wait_cf_challenge(page, timeout_seconds=180):
    """
    等 dash 登录页真正加载完。判定标准（任一满足）：
    1. 页面渲染出 GitHub 登录链接（a[href*="/auth/login/github"] 或文本 "Sign in with GitHub"）
    2. 已经登录态（url 在 dash 域且不在 /auth/ 路径）

    不依赖 title 判定 CF 挑战，因为：
    - GitHub Actions 上 Camoufox 加载时 title 会暂时是 'Loading https://...'
    - 本地 Camoufox 也会出现 title='' 的中间态
    直接尝试找元素 / 检查 url，找不到就继续等。
    """
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    while time.time() < deadline:
        # 1. 已登录态？
        url = page.url
        try:
            parsed = urllib.parse.urlparse(url)
            if (parsed.netloc == "dash.domain.digitalplat.org"
                and not parsed.path.startswith("/auth/")):
                print(f"[BROWSER] 已登录态，url={url}", flush=True)
                return True
        except Exception:
            pass

        # 2. 渲染出 GitHub 登录链接？
        try:
            gh_link = page.query_selector('a[href*="/auth/login/github"], a:has-text("Sign in with GitHub")')
            if gh_link:
                print(f"[BROWSER] dash 登录页已就绪（GitHub 链接已渲染），title={page.title()!r}", flush=True)
                return True
        except Exception:
            pass

        # 每 15s 打一次进度日志
        now = time.time()
        if now - last_log >= 15:
            print(f"[BROWSER] 等待 dash 加载… title={page.title()!r} url={url[:120]}", flush=True)
            last_log = now
        time.sleep(2)
    print(f"[BROWSER] wait_cf_challenge 超时，title={page.title()!r} url={page.url}", flush=True)
    # dump 页面状态便于调试
    try:
        html = page.content()[:3000]
        print(f"[BROWSER] 页面 HTML 前 3000 字:\n{html}", flush=True)
        # 看看是否有任何 a 标签
        all_links = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a')).slice(0, 20).map(a => ({
                text: (a.innerText || '').trim().substring(0, 50),
                href: a.href
            }));
        }""")
        print(f"[BROWSER] 页面前 20 个 a 标签: {all_links}", flush=True)
    except Exception as e:
        print(f"[BROWSER] dump HTML 失败: {e}", flush=True)
    return False


def login_via_github(page, user: str, pwd: str) -> bool:
    """走 GitHub OAuth 登录 dash"""
    print("[LOGIN] 点击 'Sign in with GitHub'...", flush=True)
    try:
        page.click("a:has-text('Sign in with GitHub')", timeout=15000)
    except Exception:
        try:
            page.click('a[href*="/auth/login/github"]', timeout=10000)
        except Exception as e:
            print(f"[LOGIN] 找不到 GitHub 登录按钮: {e}", flush=True)
            return False

    # 等 GitHub 登录页加载
    print("[LOGIN] 等待 GitHub 登录页...", flush=True)
    found_gh = False
    for _ in range(20):
        time.sleep(2)
        url = page.url
        title = page.title()
        if "github.com/login" in url or "Sign in to GitHub" in title:
            found_gh = True
            break
        if "github.com/sessions/two-factor" in url:
            print("[LOGIN] 需要 2FA！无法自动处理。", flush=True)
            return False
        if "dash.domain.digitalplat.org" in url and "/auth/login" not in url and "/auth/callback" not in url:
            print("[LOGIN] 已登录（GitHub 之前有 session）", flush=True)
            return True
    if not found_gh:
        print("[LOGIN] 没跳到 GitHub 登录页", flush=True)
        return False

    # 填用户名密码
    print("[LOGIN] 填入 GitHub 凭据...", flush=True)
    try:
        page.fill("#login_field", user, timeout=10000)
        page.fill("#password", pwd, timeout=10000)
        page.click('input[type="submit"]', timeout=10000)
    except Exception as e:
        print(f"[LOGIN] 填表失败: {e}", flush=True)
        return False

    # 等跳回 dash 的非 /auth/ 页面（避开 callback 中间页 /auth/kyc/github/callback）
    print("[LOGIN] 等待跳回 dash...", flush=True)
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(2)
        url = page.url
        if "github.com/sessions/two-factor" in url:
            print("[LOGIN] 需要 2FA！无法自动处理。", flush=True)
            return False
        if "github.com/login/oauth/authorize" in url:
            try:
                page.click("button:has-text('Authorize'), input[value='Authorize']", timeout=5000)
                print("[LOGIN] 点击 Authorize", flush=True)
            except Exception:
                pass
        # 必须在 dash 域 + 不在任何 /auth/ 路径（登录、callback、kyc 都算中间态）
        # 注意：用 urlparse 检查 netloc，避免 return_to 参数里的 dash 域名误判
        try:
            parsed = urllib.parse.urlparse(url)
            if (parsed.netloc == "dash.domain.digitalplat.org"
                and not parsed.path.startswith("/auth/")):
                # 还要等页面真正加载完（DOM 稳定）
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                print(f"[LOGIN] 登录成功！url={url}", flush=True)
                return True
        except Exception:
            pass
    print(f"[LOGIN] 登录超时，最后 url={page.url}", flush=True)
    return False


def renew_one_domain(page, domain: str, force: bool = False) -> tuple[str, str]:
    """续期单个域名，返回 (action, reason)"""
    print(f"\n[RENEW] {domain}", flush=True)

    # 1. 拿 CSRF token
    res = page.evaluate(get_csrf_js())
    csrf = res.get("csrf")
    if not csrf:
        msg = f"获取 CSRF 失败: {res}"
        print(f"  ❌ {msg}", flush=True)
        return "renew_failed", msg, ""
    print(f"  CSRF: {csrf[:20]}... (来源: {res.get('source')})", flush=True)

    # 2. 调 Panel API renew
    js = make_fetch_js(
        "POST",
        f"/_panel_api/api/domains/{domain}/renew",
        {"renewal_type": "free", "years": 1},
        {"X-CSRF-Token": csrf},
    )
    res = page.evaluate(js)
    status = res.get("status")
    body = res.get("body") or res.get("error", "")

    if status == 200:
        try:
            data = json.loads(body) if isinstance(body, str) else body
            if data.get("ok") or data.get("success"):
                domain_data = data.get("domain") or {}
                new_expiry = domain_data.get("expiry_date") or domain_data.get("expires_at") or ""
                print(f"  ✅ 续期成功！new_expiry={new_expiry}", flush=True)
                return "renewed", f"local camoufox + panel API renew, +1 year", new_expiry
            else:
                msg = f"API 200 but no success flag: {body[:200]}"
                print(f"  ⚠️ {msg}", flush=True)
                return "renew_failed", msg, ""
        except Exception as e:
            msg = f"解析 200 响应失败: {e}, body={body[:200]}"
            print(f"  ⚠️ {msg}", flush=True)
            return "renew_failed", msg, ""
    elif status == 400:
        # 通常是 >120 天不允许续期
        lower = body.lower()
        if "120 days" in lower or "more than" in lower or "cannot renew" in lower:
            msg = f">120 days remaining, DigitalPlat policy forbids renewal"
            print(f"  ℹ️ {msg}", flush=True)
            return "skip_not_expiring", msg, ""
        else:
            msg = f"HTTP 400: {body[:200]}"
            print(f"  ⚠️ {msg}", flush=True)
            return "renew_failed", msg, ""
    else:
        msg = f"HTTP {status}: {body[:200]}"
        print(f"  ❌ {msg}", flush=True)
        return "renew_failed", msg, ""


def renew_via_panel_api(
    domains: list[str],
    gh_user: str,
    gh_pass: str,
    force: bool = False,
) -> dict[str, tuple[str, str, str]]:
    """
    用 Camoufox + Panel API 续期。
    返回 {domain: (action, reason, new_expiry)}
    """
    from camoufox.sync_api import Camoufox

    results: dict[str, tuple[str, str, str]] = {}

    with Camoufox(headless=True, geoip=True, i_know_what_im_doing=True) as browser:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(45000)

        # Step 1: 打开 dash 登录页（过 CF 挑战）
        print("[BROWSER] 打开 dash 登录页...", flush=True)
        for attempt in range(3):
            try:
                page.goto(
                    "https://dash.domain.digitalplat.org/auth/login",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                if wait_cf_challenge(page, timeout_seconds=90):
                    break
            except Exception as e:
                print(f"[BROWSER] goto 失败 (attempt {attempt+1}): {e}", flush=True)
                time.sleep(5)
        print(f"[BROWSER] 当前 URL: {page.url}, Title: {page.title()}", flush=True)

        # Step 2: GitHub OAuth 登录
        if not login_via_github(page, gh_user, gh_pass):
            raise RuntimeError("GitHub OAuth 登录失败")

        # Step 3: 验证登录态
        print("\n[BROWSER] 验证登录态...", flush=True)
        js = make_fetch_js("GET", "/_panel_api/api/auth/me")
        res = page.evaluate(js)
        print(f"  GET /_panel_api/api/auth/me -> {res.get('status')}: {res.get('body', '')[:200]}", flush=True)
        if res.get("status") != 200:
            raise RuntimeError(f"登录态验证失败: {res}")

        # Step 4: 逐个续期
        print(f"\n[BROWSER] 开始续期 {len(domains)} 个域名...", flush=True)
        for d in domains:
            try:
                action, reason, new_expiry = renew_one_domain(page, d, force=force)
                results[d] = (action, reason, new_expiry)
            except Exception as e:
                print(f"  ❌ {d}: 异常: {e}", flush=True)
                results[d] = ("renew_failed", f"exception: {e}", "")
            time.sleep(2)

        # 保存 cookies 到 state 目录（供调试）
        try:
            cookies = ctx.cookies()
            cookies_path = Path("state/dp-dash-cookies.json")
            cookies_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
            print(f"\n[BROWSER] Cookies 已保存到 {cookies_path}", flush=True)
        except Exception as e:
            print(f"[BROWSER] 保存 cookies 失败: {e}", flush=True)

    return results


def main() -> int:
    args = parse_args()
    state_path = Path(args.state).resolve()
    managed_names = parse_domain_variable()
    token = require_env("DIGITALPLAT_API_TOKEN")

    client = DigitalPlatClient(token, os.getenv("DIGITALPLAT_API_BASE") or API_BASE)
    try:
        raw_domains = client.list_domains()
    except Exception as exc:
        print(f"[ERROR] 获取域名列表失败: {exc}", file=sys.stderr)
        return 1

    domain_map = {normalize_domain(raw).name: raw for raw in raw_domains}
    state = load_state(state_path)

    print(f"UTC now: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"目标域名数: {len(managed_names)}")

    # 列出每个域名状态
    for domain in managed_names:
        raw = domain_map.get(domain)
        try:
            record = normalize_domain(raw) if raw else None
        except Exception:
            record = None
        if record is not None:
            print(
                f"[CHECK] {record.name} expiry_date={record.expiry_date.strftime(DATE_FORMAT)} "
                f"days_remaining={record.days_remaining} status={record.status or '-'}"
            )
        else:
            print(f"[CHECK] {domain} (列表未返回该域名信息)")

        if args.dry_run:
            print(f"[DRY-RUN] Would renew {domain}.")
            continue

    if args.dry_run:
        return 0

    # 续期方式：Camoufox + Panel API（需要 DP_GH_USER / DP_GH_PASS）
    gh_user = os.getenv("DP_GH_USER")
    gh_pass = os.getenv("DP_GH_PASS")
    if not (gh_user and gh_pass):
        print("[ERROR] 未提供 DP_GH_USER/DP_GH_PASS，无法走 Panel API 续期", file=sys.stderr)
        return 1

    try:
        results = renew_via_panel_api(managed_names, gh_user, gh_pass, force=args.force)
    except Exception as exc:
        print(f"[ERROR] Panel API 续期流程失败: {exc}", file=sys.stderr)
        return 1

    # 更新 state
    renewed_count = 0
    skipped_count = 0
    failed_count = 0
    for domain, (action, reason, new_expiry) in results.items():
        if action == "renewed":
            renewed_count += 1
        elif action == "skip_not_expiring":
            skipped_count += 1
        else:
            failed_count += 1
        update_state_for_record(state, domain, action, reason, new_expiry)

    save_state(state_path, state)
    print(f"\n[WRITE] Updated {state_path}")
    print(f"\n========== 汇总 ==========")
    print(f"  ✅ renewed:          {renewed_count}")
    print(f"  ℹ️ skip_not_expiring: {skipped_count}")
    print(f"  ❌ renew_failed:      {failed_count}")
    print(f"  total: {len(results)}")

    # 续期失败不阻断整个流程：查询已成功即视为运行成功
    # 只有"应该续期但全部失败"才返回 1
    if renewed_count == 0 and skipped_count == 0 and failed_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        raise
