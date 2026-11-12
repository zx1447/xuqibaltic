#!/usr/bin/env python3
# CoalByte 自动续期（Pelican 免费游戏服）
#
# 机制（2026-11-12 实测）：
#   POST {base}/api/pelican/renew  body {"serverId":"<identifier>","email":"<邮箱>"}
#   → {"success":true,"expiresAt":"<now+5d>","remainingSeconds":432000,
#      "message":"Server successfully renewed for 5 days!"}
#   · 到期 = 现在 + 5 天（不叠加，重复续期只是不断重置）
#   · 无认证、无冷却（实测连打即过）；email 字段可省（浏览器带，后端不校验）
#   GET {base}/api/pelican/servers?email=<邮箱>  → 名下全部服务器 + 续期状态
#   · 面板本体 panel.coalbyte.fun（Pelican），coalbyte.com 是前端壳
#
# 环境变量：
#   COALBYTE_EMAIL     可选  用于自动发现名下全部服务器（推荐）
#   COALBYTE_SERVERS   可选  逗号分隔 serverId 列表（email 未配时的兜底）
#   COALBYTE_BASE      可选  默认 https://coalbyte.com
#
# 退出码：0 全部成功；2 有失败；3 没有可续的服务器/配置

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

BASE = os.environ.get("COALBYTE_BASE", "https://coalbyte.com").rstrip("/")
EMAIL = os.environ.get("COALBYTE_EMAIL", "").strip()
SERVERS = [s.strip() for s in os.environ.get("COALBYTE_SERVERS", "").split(",") if s.strip()]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")


def log(msg):
    print(msg, flush=True)


def req(url, method="GET", data=None, retries=4):
    """网络请求带指数重试"""
    last = None
    for i in range(retries):
        try:
            r = urllib.request.Request(url, data=data, method=method, headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
            time.sleep(2 + i * 2)
    raise last


def discover():
    """返回 [(identifier, name, expiresAt, canRenew)]"""
    out = []
    if EMAIL:
        q = urllib.parse.urlencode({"email": EMAIL})
        d = req(f"{BASE}/api/pelican/servers?{q}")
        if not d.get("userFound"):
            log(f"⚠️ 邮箱 {EMAIL} 未找到面板用户")
        for s in d.get("servers", []):
            r = s.get("renewal") or {}
            out.append((s["identifier"], s.get("name", "?"),
                        r.get("expiresAt"), r.get("canRenew")))
    for sid in SERVERS:
        if not any(o[0] == sid for o in out):
            out.append((sid, "(手动指定)", None, None))
    return out


def renew(server_id, email):
    body = {"serverId": server_id}
    if email:
        body["email"] = email
    return req(f"{BASE}/api/pelican/renew", method="POST",
               data=json.dumps(body).encode())


def main():
    servers = discover()
    if not servers:
        log("❌ 没有可续的服务器（配 COALBYTE_EMAIL 或 COALBYTE_SERVERS）")
        sys.exit(3)

    log(f"== CoalByte 续期 x{len(servers)}（{BASE}）==")
    ok = 0
    rows = []
    for ident, name, exp, can in servers:
        log(f"-- [{ident}] {name}（到期 {exp or '?'} · canRenew={can}）--")
        try:
            d = renew(ident, EMAIL)
        except Exception as e:  # noqa: BLE001
            log(f"  ❌ 请求失败: {e}")
            rows.append((ident, name, "失败", str(e)))
            continue
        if d.get("success"):
            ok += 1
            log(f"  ✅ {d.get('message')} → 到期 {d.get('expiresAt')}（剩 {d.get('remainingSeconds')}s）")
            rows.append((ident, name, "✅", d.get("expiresAt")))
        else:
            log(f"  ❌ {d.get('error') or d}")
            rows.append((ident, name, "❌", d.get("error", "?")))
        time.sleep(1)

    log(f"\n== 完成 {ok}/{len(servers)} ==")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## CoalByte 续期结果\n\n")
            f.write("| 服务器 | 名称 | 结果 | 新到期 |\n|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |\n")
    sys.exit(0 if ok == len(servers) else 2)


if __name__ == "__main__":
    main()
