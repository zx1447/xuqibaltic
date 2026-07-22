#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flarelax AFK 自动领取脚本。

Flarelax 会拦截 GitHub Actions 的数据中心出口，因此这里沿用仓库其他分支的
“动态节点池”思路：从公开列表中探测能通过 Flarelax IP 检查的节点，然后用
该节点完成 Flarelax OAuth callback 和 claim 请求。

安全处理：Discord Token 只发送到 discord.com，不会经过公开代理；代理仅用于
访问 Flarelax。公开代理具有不稳定性，成功节点每次运行都会动态重新寻找。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import random
import re
import sys
import urllib.parse
from typing import Optional

import requests

BASE_URL = "https://free-dash.flarelax.com"
WARNING_URL = f"{BASE_URL}/auth/warning"
AUTH_URL = f"{BASE_URL}/auth/discord"
CALLBACK_URL = f"{BASE_URL}/auth/discord/callback"
CLAIM_URL = f"{BASE_URL}/api/afk/claim"
DISCORD_API = "https://discord.com/api/v10"

DISCORD_TOKEN = os.environ.get("FLARELAX_DISCORD_TOKEN", "").strip()
CUSTOM_PROXY = os.environ.get("FLARELAX_PROXY", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PROXY_SOURCES = [
    ("http", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
]


class FlarelaxError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_str() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=15,
        )
    except Exception as exc:
        log(f"⚠️ Telegram 通知发送失败：{type(exc).__name__}: {exc}")


def make_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    # 不读取 Runner 上可能存在的全局代理；站点请求只使用本函数显式指定的节点。
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def proxy_url(scheme: str, address: str) -> str:
    if address.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return address
    return f"socks5h://{address}" if scheme == "socks5" else f"http://{address}"


def fetch_proxy_candidates(limit_per_source: int = 180) -> list[str]:
    """读取公开节点列表，只返回格式正确且去重后的代理 URL。"""
    candidates: list[str] = []
    seen: set[str] = set()
    source_session = make_session()
    log("🌐 正在获取公开代理节点列表...")
    for scheme, source in PROXY_SOURCES:
        try:
            response = source_session.get(source, timeout=20)
            response.raise_for_status()
            addresses = []
            for line in response.text.splitlines():
                address = line.strip()
                if re.fullmatch(r"[^:\s]+:\d{2,5}", address):
                    addresses.append(address)
            random.shuffle(addresses)
            for address in addresses[:limit_per_source]:
                url = proxy_url(scheme, address)
                if url not in seen:
                    seen.add(url)
                    candidates.append(url)
            log(f"   {scheme} 节点源可用格式数量：{len(addresses)}")
        except Exception as exc:
            log(f"   ⚠️ 节点源读取失败：{type(exc).__name__}")
    random.shuffle(candidates)
    log(f"🔍 已整理候选节点：{len(candidates)} 个")
    return candidates


def is_ip_block_page(text: str) -> bool:
    low = text.lower()
    return "vpn" in low and ("proxi" in low or "data center" in low)


def probe_proxy(proxy: str) -> Optional[str]:
    """不使用 Discord Token，仅探测 Flarelax 的出口 IP 检查。"""
    session = make_session(proxy)
    try:
        entry = session.get(AUTH_URL, allow_redirects=False, timeout=7)
        if entry.status_code not in (301, 302, 303, 307, 308):
            return None
        # 使用无效 code 测试 IP 过滤器；不会触发账号 OAuth，也不会发送 Discord Token。
        check = session.get(
            f"{CALLBACK_URL}?code=invalid_probe_code",
            allow_redirects=False,
            timeout=7,
        )
        if is_ip_block_page(check.text):
            return None
        # 站点正常进入 token 校验阶段时通常会返回 tokenerror/HTTP 500。
        if check.status_code in (400, 401, 404, 500) or "tokenerror" in check.text.lower():
            return proxy
    except Exception:
        return None
    return None


def find_accepted_proxies(candidates: list[str], max_workers: int = 32) -> list[str]:
    accepted: list[str] = []
    log(f"🔍 并发检测 Flarelax IP 过滤（并发数：{max_workers}）...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        jobs = {executor.submit(probe_proxy, p): p for p in candidates}
        for job in concurrent.futures.as_completed(jobs):
            result = job.result()
            if result:
                accepted.append(result)
                log(f"   ✅ 发现可尝试节点：{result}")
                # 保留一批节点，避免某个公开节点刚好失效。
                if len(accepted) >= 12:
                    break
    for job in jobs:
        if not job.done():
            job.cancel()
    log(f"🎯 IP 检测通过节点：{len(accepted)} 个")
    return accepted


def get_oauth_parameters(site_session: requests.Session) -> tuple[str, str, str, str]:
    entry = site_session.get(AUTH_URL, allow_redirects=False, timeout=20)
    location = entry.headers.get("Location", "")
    if entry.status_code not in (301, 302, 303, 307, 308) or "discord.com" not in location:
        raise FlarelaxError(f"OAuth 入口异常：HTTP {entry.status_code}")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    client_id = query.get("client_id", [""])[0]
    redirect_uri = query.get("redirect_uri", [""])[0]
    scope = query.get("scope", [""])[0]
    if not client_id or not redirect_uri:
        raise FlarelaxError("OAuth 地址缺少必要参数")
    return location, client_id, redirect_uri, scope


def discord_authorize(location: str, client_id: str, redirect_uri: str, scope: str) -> str:
    """直连 Discord，避免把用户 Token 交给公开代理。"""
    discord_session = make_session()
    response = discord_session.post(
        f"{DISCORD_API}/oauth2/authorize",
        params={
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "client_id": client_id,
        },
        json={
            "permissions": "0",
            "authorize": True,
            "integration_type": 0,
            "location_context": {
                "guild_id": "10000",
                "channel_id": "10000",
                "channel_type": 10000,
            },
        },
        headers={
            "Authorization": DISCORD_TOKEN,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": location,
            "Accept": "*/*",
        },
        allow_redirects=False,
        timeout=25,
    )
    if response.status_code == 401:
        raise FlarelaxError("Discord Token 无效或已失效")
    if response.status_code != 200:
        raise FlarelaxError(f"Discord OAuth 授权失败：HTTP {response.status_code}")
    callback = response.json().get("location", "")
    if not callback or not urllib.parse.parse_qs(urllib.parse.urlparse(callback).query).get("code"):
        raise FlarelaxError("Discord OAuth 没有返回有效 Code")
    return callback


def login_via_proxy(proxy: str) -> requests.Session:
    log(f"🔗 尝试节点：{proxy}")
    site_session = make_session(proxy)
    site_session.get(WARNING_URL, timeout=20)
    location, client_id, redirect_uri, scope = get_oauth_parameters(site_session)
    log(f"   ✅ 获取 OAuth 参数：client_id={client_id}")
    log("   🔐 直连 Discord 提交授权（Token 不经过代理）...")
    callback_url = discord_authorize(location, client_id, redirect_uri, scope)
    log("   ✅ Discord 授权 Code 获取成功，正在通过节点完成 Callback...")
    callback = site_session.get(callback_url, allow_redirects=True, timeout=25)
    if is_ip_block_page(callback.text):
        raise FlarelaxError("该节点仍被 Flarelax 判定为 VPN/代理")
    if "/login" in callback.url.lower():
        raise FlarelaxError(f"Callback 后仍在登录页：{callback.url}")
    if not site_session.cookies.get("connect.sid"):
        raise FlarelaxError("Callback 未生成 connect.sid")
    log(f"   ✅ 登录成功，当前 URL：{callback.url}")
    return site_session


def claim_afk(session: requests.Session) -> dict:
    log("4️⃣ 发起 AFK claim GET 请求...")
    response = session.get(
        CLAIM_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/dashboard/afk",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=25,
    )
    log(f"   📡 GET {CLAIM_URL} -> HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise FlarelaxError(f"claim 返回非 JSON：{response.text[:300]}") from exc
    log("   📦 返回：" + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if response.status_code == 401:
        raise FlarelaxError("会话未生效，claim 返回 Authentication required")
    if response.status_code != 200:
        raise FlarelaxError(f"claim 请求失败：HTTP {response.status_code}")
    if data.get("success") is False:
        message = str(data.get("message", "未知原因"))
        normalized = message.lower()
        if any(word in normalized for word in ("already", "cooldown", "wait", "claimed", "later")):
            log(f"ℹ️ 本次无需领取：{message}")
            return data
        raise FlarelaxError(f"claim 业务失败：{message}")
    return data


def main() -> int:
    log("=" * 58)
    log("🚀 Flarelax AFK 自动登录与领取启动")
    log(f"🕐 北京时间：{now_str()}")
    log("=" * 58)
    if not DISCORD_TOKEN:
        log("❌ 缺少 FLARELAX_DISCORD_TOKEN GitHub Secret")
        return 1

    try:
        if CUSTOM_PROXY:
            log("🔧 检测到 FLARELAX_PROXY，优先尝试自定义节点")
            accepted = [CUSTOM_PROXY]
        else:
            candidates = fetch_proxy_candidates()
            accepted = find_accepted_proxies(candidates)
        if not accepted:
            raise FlarelaxError("没有找到能通过 Flarelax IP 检查的节点")

        last_error: Optional[Exception] = None
        for index, proxy in enumerate(accepted, 1):
            try:
                session = login_via_proxy(proxy)
                result = claim_afk(session)
                message = result.get("message", "claim 请求成功")
                log(f"🎉 Flarelax AFK claim 完成：{message}")
                send_telegram(f"🎉 Flarelax AFK claim 成功\n🕐 {now_str()}\n📊 {message}")
                return 0
            except Exception as exc:
                last_error = exc
                log(f"   ⚠️ 节点 {index}/{len(accepted)} 失败：{type(exc).__name__}: {exc}")

        raise FlarelaxError(f"所有候选节点均失败，最后原因：{last_error}")
    except Exception as exc:
        log(f"❌ Flarelax 自动领取失败：{type(exc).__name__}: {exc}")
        send_telegram(f"❌ Flarelax 自动领取失败\n🕐 {now_str()}\n📊 {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
