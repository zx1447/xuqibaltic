#!/usr/bin/env python3
# Xiaoxia（cn.xiaoxia.app）云端电脑自动唤醒
#
# 机制（2026-11-13 实测）：
#   登录 POST {base}/api/auth/login {"email","password"} → {"token":"<JWT 7天>"}
#   状态 GET  {base}/api/instance → {status, availability: ready|loading|offline,
#                                    health_status, quota:{today_tokens,effective_limit,...}}
#   唤醒 POST {base}/api/instance/start → 幂等（运行中回 {"status":"running"}，无副作用）
#   休眠规则：闲置"一段时间"自动休眠（availability=offline，数据保留）；
#            日 100 万 token 用完 → 停机，次日 0 点（UTC）自动恢复；
#            ⚠️ 休眠期 bot 消息不唤醒、不补收 —— 所以需要本脚本盯 offline 就拉起
#   付费唤醒令牌（/api/instance/wake）仅付费/VIP 可建，免费账号走本脚本的登录通道即可
#
# 环境变量：
#   XIAOXIA_EMAIL / XIAOXIA_PASSWORD   登录凭据（必填）
#   XIAOXIA_BASE                       默认 https://cn.xiaoxia.app
#
# 状态文件 xiaoxia_state.json（token 缓存，7 天内免重登录，变化时 workflow 回写）
#
# 退出码：0 正常/拉起成功；2 有失败；3 登录失败/缺凭据

import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("XIAOXIA_BASE", "https://cn.xiaoxia.app").rstrip("/")
EMAIL = os.environ.get("XIAOXIA_EMAIL", "").strip()
PASSWORD = os.environ.get("XIAOXIA_PASSWORD", "").strip()
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xiaoxia_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")

# availability: ready=可用 / loading=启动中 / offline=离线(休眠)
# status 侧兜底：stopped / hibernated 也拉起；loading 不动
WAKE_STATES = {"offline", "stopped", "hibernated", "sleeping"}


def log(msg):
    print(msg, flush=True)


def req(path, method="GET", body=None, token=None, retries=3):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(retries):
        r = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if token:
            r.add_header("Authorization", f"Bearer {token}")
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


def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_state(token):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"token": token,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  f, ensure_ascii=False, indent=2)


def login():
    log(f"== 登录 {EMAIL} ==")
    st, d = req("/api/auth/login", method="POST",
                body={"email": EMAIL, "password": PASSWORD})
    tok = d.get("token") if st == 200 else None
    if not tok:
        log(f"❌ 登录失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
        return None
    log(f"✅ 登录成功（JWT 已取）")
    return tok


def pass_once(token):
    """一轮巡检。返回 (ok?, new_token)"""
    st, d = req("/api/instance", token=token)
    if st != 200 or not isinstance(d, dict) or "availability" not in d:
        if isinstance(d, dict) and d.get("error") == "err_unauthorized":
            log("⚠️ token 失效，重新登录")
            token = login()
            if not token:
                return False, None
            st, d = req("/api/instance", token=token)
        else:
            log(f"❌ 取实例状态失败 HTTP {st}: {json.dumps(d, ensure_ascii=False)[:200]}")
            return False, token

    avail = (d.get("availability") or "?").lower()
    status = (d.get("status") or "?").lower()
    health = d.get("health_status")
    q = d.get("quota") or {}
    log(f"-- 实例 #{d.get('id')} | availability={avail} | status={status} | health={health} "
        f"| 今日 token {q.get('today_tokens', '?')}/{q.get('effective_limit', '?')} --")

    if avail == "loading":
        log("  ⏭ 启动中，下轮复查")
        return True, token

    if avail == "ready" and status not in WAKE_STATES:
        log("  ✅ 运行中，无需动作")
        return True, token

    # 休眠/停机 → 拉起
    log(f"  🚀 检测到休眠/停机 → 唤醒")
    st2, d2 = req("/api/instance/start", method="POST", token=token)
    log(f"     HTTP {st2} {json.dumps(d2, ensure_ascii=False)[:150]}")
    time.sleep(10)
    st3, d3 = req("/api/instance", token=token)
    avail2 = (d3.get("availability") or "?").lower()
    log(f"     复查: availability={avail2} | status={d3.get('status')}")
    return avail2 in ("ready", "loading"), token


def loop(minutes):
    """长驻守护：30 分钟一轮（防 GitHub schedule 调度饿死；接力由 workflow 负责）"""
    st = load_state()
    token = st.get("token") or ""
    deadline = time.time() + minutes * 60
    cycles = 0
    while True:
        cycles += 1
        left = (deadline - time.time()) / 60
        log(f"—— [loop] 第 {cycles} 轮（预算剩 {left:.0f} 分钟）——")
        try:
            ok, token = pass_once(token)
            if token is None:
                log("— [loop] 鉴权死，停止守护 —")
                return 3
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ [loop] 本轮异常: {type(e).__name__}: {e}")
        rest = deadline - time.time()
        if rest <= 0:
            break
        t = min(1800, rest)
        log(f"— [loop] 休眠 {t / 60:.0f} 分钟后复查 —")
        time.sleep(t)
    if token:
        save_state(token)
    log("— [loop] 本轮预算用尽，交棒下一次运行 —")
    return 0


def main():
    if not EMAIL or not PASSWORD:
        log("❌ 缺 XIAOXIA_EMAIL / XIAOXIA_PASSWORD")
        return 3
    st = load_state()
    token = st.get("token") or ""
    log(f"🎯 小虾云端电脑唤醒巡检 · {BASE}")
    ok, token = pass_once(token)
    if token:
        save_state(token)
    return 0 if ok else 2


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            mins = float(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            mins = 330.0
        sys.exit(loop(mins))
    sys.exit(main())
