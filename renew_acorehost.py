#!/usr/bin/env python3
# AcoreHosting 免费服计划自动续期（dev.sqidgeon.serverplans 扩展）
#
# 机制（2026-11-13 实测）：
#   清单 GET  {base}/api/client/servers
#        → {servers:{data:[{uuid, name, is_suspended, status, ...}]}}
#   续期 POST {base}/api/client/extensions/dev.sqidgeon.serverplans/renew   {"server":"<uuid>"}
#        → 仅剩 <8 小时时可打（否则 422 "Renewal is available when less than 8 hours remain"）
#   恢复 POST {base}/api/client/extensions/dev.sqidgeon.serverplans/restore {"server":"<uuid>"}
#        → 从"计划挂起"中拉起（否则 "Server is not in plan suspension"）
#   鉴权 Authorization: Bearer <ACORE_API_KEY>（c7sp_ 客户端 API key）
#
# 策略：每轮先 renew（窗口内直接续命）；不成功再 restore（错过窗口被挂起则拉回）。
#       两个报错都是良性"还没到时候"，只报告。窗口 8 小时很宽，30 分钟粒度足够。
#
# 环境变量：
#   ACORE_API_KEY   必填
#   ACORE_BASE      默认 https://dash.acorehosting.com
#
# 退出码：0 正常/续期/恢复成功；2 有失败；3 缺 key/鉴权死

import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("ACORE_BASE", "https://dash.acorehosting.com").rstrip("/")
KEY = os.environ.get("ACORE_API_KEY", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

EXT = "/api/client/extensions/dev.sqidgeon.serverplans"


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
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, (json.loads(raw) if raw else {})
            except Exception:  # noqa: BLE001
                return e.code, {"raw": raw[:300]}
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  [网络重试 {i + 1}/{retries}] {type(e).__name__}: {e}")
            time.sleep(2 + i * 2)
    raise last


def errmsg(d):
    if isinstance(d, dict):
        e = d.get("errors")
        if isinstance(e, list) and e:
            return str(e[0])
        return str(d.get("message") or d.get("error") or d)
    return str(d)


def pass_once():
    """一轮巡检：renew 优先，restore 兜底。返回 (ok_count, total, rows)"""
    st, lst = req("/api/client/servers")
    if st != 200 or not isinstance(lst, dict):
        log(f"❌ 取服务器清单失败 HTTP {st}: {errmsg(lst)}")
        return 0, 0, []
    servers = (lst.get("servers") or {}).get("data") or []
    if not servers:
        log("⚠️ 名下没有服务器")
        return 0, 0, []

    ok, rows = 0, []
    for s in servers:
        uuid, name = s.get("uuid"), s.get("name", "?")
        susp, status = s.get("is_suspended"), s.get("status")
        log(f"-- [{uuid[:8]}] {name} | suspended={susp} | status={status} --")

        # 1) renew：窗口内直接续命
        code, d = req(f"{EXT}/renew", method="POST", body={"server": uuid})
        if code == 200:
            log(f"  🚀 renew OK: {json.dumps(d, ensure_ascii=False)[:160]}")
            rows.append((uuid[:8], name, "🚀 已续期", errmsg(d)[:60]))
            ok += 1
            continue
        renew_msg = errmsg(d)

        # 2) restore：错过窗口被计划挂起 → 拉回
        code2, d2 = req(f"{EXT}/restore", method="POST", body={"server": uuid})
        if code2 == 200:
            log(f"  🆘 restore OK: {json.dumps(d2, ensure_ascii=False)[:160]}")
            rows.append((uuid[:8], name, "🆘 已恢复", errmsg(d2)[:60]))
            ok += 1
            continue
        restore_msg = errmsg(d2)

        # 3) 双双"还没到时候"= 健康等待
        benign = ("less than 8 hours" in renew_msg.lower()
                  and "not in plan suspension" in restore_msg.lower())
        if benign:
            log("  ✅ 健康等待（未到续期窗口，未被挂起）")
            rows.append((uuid[:8], name, "—", f"等待中（{renew_msg[:40]}）"))
            ok += 1
        else:
            log(f"  ❌ renew: HTTP {code} {renew_msg}")
            log(f"  ❌ restore: HTTP {code2} {restore_msg}")
            rows.append((uuid[:8], name, "❌", f"{renew_msg[:40]} / {restore_msg[:40]}"))
        time.sleep(1)
    return ok, len(servers), rows


def loop(minutes):
    """长驻守护：30 分钟一轮（防 GitHub schedule 调度饿死；接力由 workflow 负责）"""
    deadline = time.time() + minutes * 60
    cycles = 0
    rows_last = []
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            ok, total, rows = pass_once()
            rows_last = rows
            if total and ok < total:
                log("⚠️ 本轮有失败项（下轮复查）")
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 本轮异常: {type(e).__name__}: {e}")
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1800, rest)
        log(f"— [loop] 休眠 {t / 60:.0f} 分钟后复查 —")
        time.sleep(t)
    summary(rows_last)
    log("— [loop] 本轮预算用尽，交棒下一次运行 —")
    return 0


def summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and rows:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## AcoreHosting 续期巡检\n\n")
            f.write("| 服务器 | 名称 | 动作 | 说明 |\n|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |\n")


def main():
    if not KEY:
        log("❌ 缺 ACORE_API_KEY")
        return 3
    log(f"🎯 AcoreHosting 计划续期 · {BASE}")
    ok, total, rows = pass_once()
    summary(rows)
    if total == 0:
        return 0
    return 0 if ok == total else 2


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            mins = float(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            mins = 330.0
        sys.exit(loop(mins))
    sys.exit(main())
