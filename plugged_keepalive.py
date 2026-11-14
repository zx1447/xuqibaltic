#!/usr/bin/env python3
# plugged.host 免费服守护：检测停止 → 自动开机
#
# 面板：Pelican（不是 Pterodactyl，注意差异见下）
# 服务器：mockingbird（嘲鸫）uuid 75cd14b3-aaf3-4a1c-a214-dbd787e76d5a
#        Minecraft: Java Edition · pl2 (Poland) · pl2.plugged.host:34066
#        12GB RAM / 50GB disk / 400% CPU
#
# 机制（2026-11-14 实测）：
#   清单 GET  {base}/api/client/servers
#        → {"servers":{"data":[{uuid, name, is_suspended, allocation, egg, limits, ...}]}}
#   状态 GET  {base}/api/client/servers/<uuid>/resources
#        → {"resources":{state, uptime, cpu_absolute, memory_bytes, network:{...}}}
#          state ∈ running / starting / stopping / offline
#   开机 POST {base}/api/client/servers/<uuid>/power   {"signal":"start"}
#        → 200 {}（Pelican 与 Pterodactyl 同为 signal 字段，不是 action）
#   鉴权 Authorization: Bearer <c7sp_ 开头客户端 API key>
#
# 免费档行为：
#   会话耗尽后服务端会自动停机，本脚本巡检发现 offline 即拉起（拉起=新会话）。
#   auto_start_behavior 字段值为 "unless_stopped"，说明只有人工 stop 才不自动起。
#
# 环境变量：
#   PLUGGED_API_KEY   必填，c7sp_ 开头
#   PLUGGED_BASE      默认 https://panel.plugged.host
#   PLUGGED_SERVERS   可选，逗号分隔的 uuid 列表；留空=名下全部服务器
#   POWER_WAIT_SECONDS  拉起后等待回到 running 的最长秒数，默认 90
#
# 退出码：0 全部正常/拉起成功；2 有失败；3 缺 key/鉴权死

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PLUGGED_BASE", "https://panel.plugged.host").rstrip("/")
KEY = os.environ.get("PLUGGED_API_KEY", "").strip()
ONLY = [x.strip() for x in os.environ.get("PLUGGED_SERVERS", "").split(",") if x.strip()]
POWER_WAIT = int(os.environ.get("POWER_WAIT_SECONDS", "90"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

# 视为"活着"的状态
OK_STATES = {"running", "starting"}
# 过渡状态：本轮放过，下轮自然变 offline 再拉
SKIP_STATES = {"stopping", "restarting", "installing", "restoring"}


def log(msg):
    print(msg, flush=True)


def req(path, method="GET", body=None, retries=3):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(retries):
        r = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KEY}",
        })
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            # 401/403 直接抛，避免无意义重试
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: {raw[:200]}")
            try:
                return {"http_error": e.code, "body": json.loads(raw)}
            except Exception:  # noqa: BLE001
                return {"http_error": e.code, "body": raw[:300]}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
            time.sleep(2 + i * 2)
    raise last


class AuthError(Exception):
    pass


def get_state(uuid):
    """返回 (state, uptime_seconds)。"""
    d = req(f"/api/client/servers/{uuid}/resources")
    r = (d or {}).get("resources") or {}
    if not r:
        return "?", None
    return (r.get("state") or "?").lower(), r.get("uptime")


def wait_until_up(uuid, limit=POWER_WAIT):
    """开启后轮询到 running，返回最终 state。"""
    waited = 0
    st = "?"
    while waited < limit:
        time.sleep(6)
        waited += 6
        try:
            st, _ = get_state(uuid)
        except AuthError:
            raise
        except Exception:  # noqa: BLE001
            pass
        if st in OK_STATES:
            return st
    return st


def main():
    if not KEY:
        log("❌ 缺 PLUGGED_API_KEY")
        sys.exit(3)

    try:
        lst = req("/api/client/servers")
    except AuthError as e:
        log(f"❌ 鉴权失败：{e}")
        sys.exit(3)

    servers = ((lst or {}).get("servers") or {}).get("data") or []
    if ONLY:
        servers = [s for s in servers if s.get("uuid") in ONLY]

    if not servers:
        log("⚠️ 名下没有匹配的服务器")
        sys.exit(0)

    log(f"== plugged.host 守护 x{len(servers)}（{BASE}）==")
    ok = 0
    rows = []

    for s in servers:
        uuid = s.get("uuid")
        name = s.get("name", "?")
        short = uuid[:8] if uuid else "?"
        susp = s.get("is_suspended")
        lim = s.get("limits") or {}
        alloc = s.get("allocation") or {}
        addr = f"{alloc.get('ip_alias')}:{alloc.get('port')}" if alloc else "-"

        try:
            state, uptime = get_state(uuid)
        except AuthError as e:
            log(f"-- [{short}] {name} | 鉴权失败：{e}")
            rows.append((short, name, "?", "❌ 鉴权", "-"))
            continue

        up_h = round(uptime / 3600, 1) if isinstance(uptime, (int, float)) and uptime else 0
        log(f"-- [{short}] {name} | state={state} | 已运行 {up_h}h | {addr}"
            f" | {lim.get('memory')}MB/{lim.get('disk')}MB")

        if susp:
            log("  ⏭ 已挂起（suspended），跳过")
            rows.append((short, name, state, "⏭ 挂起", addr))
            ok += 1
            continue

        if state in OK_STATES:
            log("  ✅ 运行中，无需动作")
            rows.append((short, name, state, "—", addr))
            ok += 1
            continue

        if state in SKIP_STATES:
            log("  ⏭ 过渡状态，下轮复查")
            rows.append((short, name, state, "⏭ 等下轮", addr))
            ok += 1
            continue

        log(f"  🚀 检测到 {state} → 开机")
        try:
            r = req(f"/api/client/servers/{uuid}/power", method="POST",
                    body={"signal": "start"})
            log(f"     {json.dumps(r, ensure_ascii=False)[:150]}")
        except AuthError as e:
            log(f"     ❌ 开机请求鉴权失败：{e}")
            rows.append((short, name, state, "❌ 鉴权", addr))
            continue

        final = wait_until_up(uuid)
        log(f"     复查: state={final}")
        if final in OK_STATES:
            ok += 1
            rows.append((short, name, state, "🚀 已开机", addr))
        else:
            rows.append((short, name, state, f"❌ 开机后 {final}", addr))
            log(f"     ⚠️ 开机后状态仍为 {final}")

    log(f"\n== 完成 {ok}/{len(servers)} ==")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## plugged.host 守护结果\n\n")
            f.write("| uuid | 名称 | 原状态 | 动作 | 地址 |\n|---|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |\n")

    sys.exit(0 if ok == len(servers) else 2)


if __name__ == "__main__":
    main()
