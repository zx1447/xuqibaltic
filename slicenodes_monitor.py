#!/usr/bin/env python3
"""Monitor every SliceNodes server hourly and start any offline server."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = os.environ.get("SLICENODES_PANEL_URL", "https://panel.slicenodes.in").rstrip("/")
API_KEY = os.environ.get("SLICENODES_API_KEY", "").strip()
MONITOR_INTERVAL_SECONDS = max(
    30, int(os.environ.get("SLICENODES_MONITOR_INTERVAL_SECONDS", "3600") or "3600")
)
RUNTIME_SECONDS = max(
    60, min(21300, int(os.environ.get("SLICENODES_RUNTIME_SECONDS", "21000") or "21000"))
)
VERIFY_INTERVAL_SECONDS = max(
    5, min(60, int(os.environ.get("SLICENODES_VERIFY_INTERVAL_SECONDS", "15") or "15"))
)
VERIFY_MAX_POLLS = max(
    1, min(30, int(os.environ.get("SLICENODES_VERIFY_MAX_POLLS", "20") or "20"))
)
STATE_FILE = Path("slicenodes_state.json")
CN_TZ = timezone(timedelta(hours=8))


class SliceNodesError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def make_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SliceNodes-All-Server-Monitor/1.0",
        }
    )
    return session


def json_data(response: requests.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {"raw": response.text[:500]}


def list_servers(session: requests.Session) -> list[dict]:
    response = session.get(f"{BASE_URL}/api/client", timeout=30)
    data = json_data(response)
    if response.status_code != 200:
        raise SliceNodesError(
            f"服务器列表 HTTP {response.status_code}: {str(data)[:300]}"
        )
    servers = []
    for item in data.get("data") or []:
        attributes = item.get("attributes") or {}
        if attributes.get("identifier"):
            servers.append(attributes)
    return servers


def resource_state(session: requests.Session, identifier: str) -> tuple[str, dict]:
    response = session.get(
        f"{BASE_URL}/api/client/servers/{identifier}/resources", timeout=30
    )
    data = json_data(response)
    if response.status_code == 200:
        attributes = data.get("attributes") or {}
        return str(attributes.get("current_state") or "unknown").lower(), attributes
    if response.status_code == 409:
        details = " ".join(
            str(error.get("detail") or "") for error in data.get("errors") or []
        ).lower()
        if "suspended" in details:
            return "suspended", {"error": details}
    raise SliceNodesError(
        f"资源状态 {identifier} HTTP {response.status_code}: {str(data)[:300]}"
    )


def send_start(session: requests.Session, identifier: str) -> dict:
    response = session.post(
        f"{BASE_URL}/api/client/servers/{identifier}/power",
        json={"signal": "start"},
        timeout=30,
    )
    data = json_data(response)
    if response.status_code != 204:
        raise SliceNodesError(
            f"启动 {identifier} HTTP {response.status_code}: {str(data)[:300]}"
        )
    return {"http_status": response.status_code, "accepted": True}


def wait_until_startable(
    session: requests.Session, identifier: str, initial_state: str
) -> tuple[str, dict]:
    state = initial_state
    resources: dict = {}
    if state != "stopping":
        return state, resources
    log(f"⏳ {identifier} 正在停止，等待离线后重新启动…")
    for _ in range(VERIFY_MAX_POLLS):
        time.sleep(VERIFY_INTERVAL_SECONDS)
        state, resources = resource_state(session, identifier)
        if state != "stopping":
            break
    return state, resources


def verify_started(session: requests.Session, identifier: str) -> tuple[bool, str, int]:
    last_state = "unknown"
    for poll in range(1, VERIFY_MAX_POLLS + 1):
        time.sleep(VERIFY_INTERVAL_SECONDS)
        try:
            last_state, _ = resource_state(session, identifier)
        except Exception as exc:
            log(f"⚠️ {identifier} 启动后状态检查异常：{type(exc).__name__}")
            continue
        if last_state == "running":
            return True, last_state, poll
        if last_state == "starting":
            log(
                f"⏳ {identifier} 正在启动 "
                f"({poll}/{VERIFY_MAX_POLLS})"
            )
        elif last_state in {"suspended", "installing", "transferring"}:
            break
    return False, last_state, VERIFY_MAX_POLLS


def inspect_server(session: requests.Session, server: dict) -> dict:
    identifier = str(server.get("identifier"))
    name = str(server.get("name") or identifier)
    result = {
        "identifier": identifier,
        "uuid": server.get("uuid"),
        "name": name,
        "node": server.get("node"),
        "checked_time": now_cn(),
        "start_attempted": False,
        "start_success": False,
    }

    if server.get("is_suspended") or str(server.get("status") or "").lower() == "suspended":
        result.update({"state": "suspended", "action": "skip_suspended"})
        log(f"⛔ {name}（{identifier}）已被面板暂停，无法启动")
        return result
    if server.get("is_node_under_maintenance"):
        result.update({"state": "maintenance", "action": "skip_maintenance"})
        log(f"🛠️ {name}（{identifier}）所在节点维护中，暂不启动")
        return result
    if server.get("is_installing"):
        result.update({"state": "installing", "action": "skip_installing"})
        log(f"📦 {name}（{identifier}）正在安装，暂不启动")
        return result
    if server.get("is_transferring"):
        result.update({"state": "transferring", "action": "skip_transferring"})
        log(f"🚚 {name}（{identifier}）正在迁移，暂不启动")
        return result

    state, resources = resource_state(session, identifier)
    result["initial_state"] = state
    result["resources"] = resources.get("resources") or {}
    if state == "running":
        result.update({"state": state, "action": "already_running"})
        log(f"🟢 {name}（{identifier}）正在运行")
        return result
    if state == "starting":
        result.update({"state": state, "action": "already_starting"})
        log(f"🟡 {name}（{identifier}）已经在启动中")
        return result
    if state == "suspended":
        result.update({"state": state, "action": "skip_suspended"})
        log(f"⛔ {name}（{identifier}）已被面板暂停，无法启动")
        return result

    state, _ = wait_until_startable(session, identifier, state)
    if state not in {"offline", "stopped"}:
        result.update({"state": state, "action": f"skip_{state}"})
        log(f"⚠️ {name}（{identifier}）状态为 {state}，未重复发送启动命令")
        return result

    log(f"🔴 {name}（{identifier}）未运行，正在启动…")
    result["start_attempted"] = True
    result["start_response"] = send_start(session, identifier)
    started, final_state, polls = verify_started(session, identifier)
    result.update(
        {
            "state": final_state,
            "start_success": started,
            "verify_polls": polls,
            "action": "started" if started else "start_sent_not_confirmed",
        }
    )
    if started:
        log(f"✅ {name}（{identifier}）启动成功")
    else:
        log(f"⚠️ {name}（{identifier}）已发送启动命令，最终状态：{final_state}")
    return result


def run_cycle(session: requests.Session, cycle_number: int, state: dict) -> bool:
    log(f"\n🔍 第 {cycle_number} 轮：检查账号拥有的全部服务器")
    servers = list_servers(session)
    log(f"📋 当前共有 {len(servers)} 台服务器")
    results = []
    failures = 0
    for server in servers:
        try:
            result = inspect_server(session, server)
        except Exception as exc:
            failures += 1
            result = {
                "identifier": server.get("identifier"),
                "name": server.get("name"),
                "checked_time": now_cn(),
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            log(
                f"❌ {server.get('name') or server.get('identifier')} 检查失败："
                f"{type(exc).__name__}: {exc}"
            )
        results.append(result)

    cycle = {
        "cycle_number": cycle_number,
        "check_time": now_cn(),
        "server_count": len(servers),
        "failures": failures,
        "servers": results,
    }
    state.update(
        {
            "last_status": "monitoring",
            "last_check_time": cycle["check_time"],
            "last_cycle": cycle,
            "total_cycles": int(state.get("total_cycles", 0)) + 1,
            "monitor_interval_seconds": MONITOR_INTERVAL_SECONDS,
            "runtime_seconds": RUNTIME_SECONDS,
            "panel_url": BASE_URL,
        }
    )
    history = list(state.get("recent_cycles") or [])
    history.append(cycle)
    state["recent_cycles"] = history[-8:]
    save_state(state)
    return failures == 0


def main() -> int:
    log("🚀 SliceNodes 全服务器持续监控启动")
    log(f"🕐 北京时间：{now_cn()}")
    log(f"🌐 面板：{BASE_URL}")
    log(
        f"⏳ 本轮持续 {RUNTIME_SECONDS // 60} 分钟；"
        f"每 {MONITOR_INTERVAL_SECONDS // 60} 分钟检查全部服务器"
    )
    if not API_KEY:
        log("❌ 缺少 SLICENODES_API_KEY")
        return 1

    state = load_state()
    state.update(
        {
            "last_start_time": now_cn(),
            "last_status": "starting",
            "monitor_interval_seconds": MONITOR_INTERVAL_SECONDS,
            "runtime_seconds": RUNTIME_SECONDS,
            "panel_url": BASE_URL,
        }
    )
    save_state(state)
    session = make_session()
    deadline = time.monotonic() + RUNTIME_SECONDS
    cycle_number = 0
    successful_cycles = 0

    while time.monotonic() < deadline:
        cycle_number += 1
        try:
            if run_cycle(session, cycle_number, state):
                successful_cycles += 1
        except Exception as exc:
            log(f"❌ 第 {cycle_number} 轮整体失败：{type(exc).__name__}: {exc}")
            state.update(
                {
                    "last_status": "cycle_error",
                    "last_check_time": now_cn(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            )
            save_state(state)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait_seconds = min(MONITOR_INTERVAL_SECONDS, remaining)
        log(f"💤 等待 {int(wait_seconds)} 秒后检查下一轮；Workflow 保持运行")
        time.sleep(wait_seconds)

    state.update(
        {
            "last_status": "completed",
            "last_end_time": now_cn(),
            "last_run_cycles": cycle_number,
            "last_run_successful_cycles": successful_cycles,
        }
    )
    save_state(state)
    log(
        f"🏁 本轮 Workflow 运行到时限结束：共检查 {cycle_number} 轮，"
        f"成功 {successful_cycles} 轮"
    )
    return 0 if successful_cycles > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
