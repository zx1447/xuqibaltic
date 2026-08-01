#!/usr/bin/env python3
# DigitalPlat 免费域名自动续期
# - 列表/查询走官方 Developer API（带浏览器 UA，绕过 Cloudflare 对 bot UA 的拦截）
# - 续期走「浏览器点击」：用 Playwright 登录控制面板，逐个域名点击「续费 → 申请免费续费」按钮
#   （官方 Developer API 没有 /renew 接口，控制面板内部接口带 CF 盾且需登录会话，HTTP 直调会被 403）
import argparse
import json
import os
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
# 控制面板内部 API（续期按钮实际调用的接口；需要登录会话，浏览器点击时由会话 cookie 携带）
PANEL_API_BASE = "https://dash.domain.digitalplat.org/_panel_api/api"
DATE_FORMAT = "%Y-%m-%d"
DEFAULT_RENEW_BEFORE_DAYS = 120

# 浏览器风格请求头，避免被 Cloudflare 当成 bot 触发 Challenge Page（仅用于查询类 API）
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

    def _request(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        last_exc: Exception | None = None
        # Cloudflare 挑战是间歇性的，遇到 403/429 退避重试通常即可通过
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
    parser = argparse.ArgumentParser(description="DigitalPlat free-domain renewal helper.")
    parser.add_argument("--state", default="state/domains-state.json", help="Path to state JSON file.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without renewing or writing state.")
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
) -> None:
    domains = state.setdefault("domains", {})
    item = domains.setdefault(name, {})
    item["last_action"] = action
    item["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def renew_via_browser(domains: list[str], api_token: str) -> tuple[list[str], list[str]]:
    """用 Playwright（系统 Chrome）登录控制面板，逐个域名点击「续费 → 申请免费续费」按钮。"""
    from playwright.sync_api import sync_playwright

    user = os.getenv("DP_GH_USER")
    pwd = os.getenv("DP_GH_PASS")
    if not (user and pwd):
        raise RuntimeError("浏览器点击续期需要 DP_GH_USER / DP_GH_PASS 两个 secret")

    renewed: list[str] = []
    failed: list[str] = []
    with sync_playwright() as p:
        # 使用系统 Chrome（比 Playwright 自带 chromium 更不易被 Cloudflare 识别）
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            user_agent=BROWSER_UA,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.set_default_timeout(60000)
        print("[BROWSER] 打开控制面板…", flush=True)
        page.goto("https://dash.domain.digitalplat.org/domains", wait_until="domcontentloaded", timeout=60000)

        # 点击 GitHub 登录按钮（CF 挑战解除后按钮才会出现，故给较长等待）
        gh_clicked = False
        for sel in [
            "text=使用 GitHub 登录",
            "text=Continue with GitHub",
            "text=GitHub",
            "a:has-text('GitHub')",
            "button:has-text('GitHub')",
        ]:
            try:
                page.click(sel, timeout=30000)
                gh_clicked = True
                break
            except Exception:
                continue

        if gh_clicked:
            print("[BROWSER] GitHub OAuth 登录页，填入凭据…", flush=True)
            page.wait_for_url("**github.com**", timeout=60000)
            page.fill("#login_field", user)
            page.fill("#password", pwd)
            page.click('input[type="submit"]')
            # 可能出现授权确认页
            try:
                page.click("text=Authorize", timeout=20000)
            except Exception:
                pass

        # 等待回到控制面板（拿到登录会话）
        page.wait_for_url("**dash.domain.digitalplat.org**", timeout=60000)
        print("[BROWSER] 登录成功，开始逐域名点击续费…", flush=True)

        for d in domains:
            try:
                page.goto(f"https://dash.domain.digitalplat.org/domains/{d}", wait_until="domcontentloaded", timeout=60000)
                # 点击「续费」区域（部分情况需要展开）
                for sel in ["text=续费", "button:has-text('续费')", "text=Renew", "#renew"]:
                    try:
                        page.click(sel, timeout=10000)
                        break
                    except Exception:
                        continue
                time.sleep(1)
                # 点击「申请免费续费」
                page.click("text=申请免费续费", timeout=20000)
                time.sleep(2)
                # 部分情况会有确认弹窗
                for confirm in ["text=确认", "text=确定", "text=Confirm", "button:has-text('确认')"]:
                    try:
                        page.click(confirm, timeout=5000)
                        break
                    except Exception:
                        continue
                renewed.append(d)
                print(f"[RENEWED] {d}", flush=True)
            except Exception as exc:
                print(f"[WARN] {d}: 浏览器点击续费失败: {exc}", flush=True)
                failed.append(d)
        browser.close()
    return renewed, failed


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
    state_changed = False
    renewed_count = 0
    errors: list[str] = []

    print(f"UTC now: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"目标域名数: {len(managed_names)}")

    for domain in managed_names:
        raw = domain_map.get(domain)
        try:
            record = normalize_domain(raw) if raw else None
        except Exception as exc:
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

    # 续期方式：浏览器点击（提供了 DP_GH_USER / DP_GH_PASS 时）
    use_browser = bool(os.getenv("DP_GH_USER") and os.getenv("DP_GH_PASS"))
    if use_browser:
        try:
            renewed, failed = renew_via_browser(managed_names, token)
            renewed_count = len(renewed)
            for d in renewed:
                state_changed = True
                update_state_for_record(state, d, "renewed")
            for d in failed:
                errors.append(f"{d}: 浏览器点击续费失败")
                update_state_for_record(state, d, "renew_failed")
        except Exception as exc:
            print(f"[ERROR] 浏览器续期流程失败: {exc}", file=sys.stderr)
            errors.append(f"browser-renew: {exc}")
    else:
        print("[INFO] 未提供 DP_GH_USER/DP_GH_PASS，跳过浏览器点击续期（仅查询）。", file=sys.stderr)

    if state_changed and not args.dry_run:
        save_state(state_path, state)
        print(f"[WRITE] Updated {state_path}")

    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        # 续期失败不阻断整个流程：查询已成功即视为运行成功
        if renewed_count > 0 or use_browser:
            print(f"[DONE] 已尝试续期；成功 {renewed_count} 个，失败 {len(errors)} 个。")
            return 0
        return 1

    if renewed_count == 0:
        print("[DONE] No domains were renewed in this run.")
    else:
        print(f"[DONE] Renewed {renewed_count} domain(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        raise
