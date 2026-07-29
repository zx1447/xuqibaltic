#!/usr/bin/env python3
"""Octopus Cloud nezha probe keepalive (Runbook 模式) - GitHub Actions version.

用环境变量配置 (不依赖本地文件), 适合 GitHub Actions cron 跑.

环境变量:
  OCTOPUS_URL       - e.g. https://zxy1715.octopus.app
  OCTOPUS_API_KEY   - e.g. API-XXXXXXXXXXXXXXXXXXXXXXXX
  OCTOPUS_SPACE     - e.g. Spaces-1
  OCTOPUS_PROJECT   - e.g. Projects-22
  OCTOPUS_ENV       - e.g. Environments-1
  OCTOPUS_RUNBOOK   - e.g. Runbooks-1
  OCTOPUS_SNAPSHOT  - e.g. RunbookSnapshots-6
  OCTOPUS_MAX_MIN   - 主动 cancel 阈值 (默认 30)
"""
import json, os, sys, time, urllib.request, urllib.error

API_KEY = os.environ.get("OCTOPUS_API_KEY", "").strip()
BASE = f"{os.environ.get('OCTOPUS_URL', '').strip()}/api"
SPACE = os.environ.get("OCTOPUS_SPACE", "Spaces-1").strip()
PROJECT = os.environ.get("OCTOPUS_PROJECT", "Projects-22").strip()
ENV = os.environ.get("OCTOPUS_ENV", "Environments-1").strip()
RUNBOOK_ID = os.environ.get("OCTOPUS_RUNBOOK", "Runbooks-1").strip()
SNAPSHOT_ID = os.environ.get("OCTOPUS_SNAPSHOT", "RunbookSnapshots-6").strip()
MAX_TASK_MINUTES = int(os.environ.get("OCTOPUS_MAX_MIN", "30") or "30")

if not API_KEY or not BASE:
    print("[keepalive] ERROR: missing OCTOPUS_API_KEY or OCTOPUS_URL")
    sys.exit(1)


def api(method, path, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Octopus-ApiKey", API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()) if r.status != 204 else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        return {"_http_error": e.code, "_body": body_text[:500]}
    except Exception as e:
        return {"_error": str(e)}


def list_active_tasks():
    r = api("GET", f"/{SPACE}/tasks?skip=0&take=50&states=Executing,Queued")
    return [t for t in r.get("Items", []) if t.get("State") in ("Executing", "Queued", "Cancelling")]


def cancel_task(task_id):
    print(f"[keepalive] canceling {task_id}...")
    r = api("POST", f"/tasks/{task_id}/cancel", {})
    print(f"  -> {r.get('State', r)}")
    return r


def create_runbook_run():
    print(f"[keepalive] creating runbook run for {RUNBOOK_ID}...")
    r = api("POST", f"/{SPACE}/runbookRuns", {
        "RunbookId": RUNBOOK_ID,
        "EnvironmentId": ENV,
        "SkipActions": [],
        "ForcePackageDownload": False,
        "ForcePackageRedeployment": False,
        "UseGuidedFailure": False,
        "TenantId": "",
        "ProjectId": PROJECT,
        "SpaceId": SPACE,
        "RunbookSnapshotId": SNAPSHOT_ID,
    })
    print(f"  -> RunbookRun {r.get('Id')}, Task {r.get('TaskId')}")
    return r


def task_duration_minutes(task):
    d = task.get("Duration", "")
    mins = 0
    if "hour" in d:
        try:
            mins += int(d.split("hour")[0].strip()) * 60
        except ValueError:
            pass
    if "minute" in d:
        try:
            mins += int(d.split("minute")[0].split()[-1])
        except (ValueError, IndexError):
            pass
    return mins


def main():
    tasks = list_active_tasks()
    print(f"[keepalive] active tasks: {len(tasks)}")
    for t in tasks:
        print(f"  - {t['Id']} | {t['State']} | {t.get('Duration','?')} | {t.get('Description','')[:60]}")

    if not tasks:
        create_runbook_run()
        return

    if len(tasks) > 1:
        tasks_sorted = sorted(tasks, key=lambda x: x.get("StartTime", ""), reverse=True)
        for t in tasks_sorted[1:]:
            print(f"[keepalive] extra task {t['Id']}, canceling")
            cancel_task(t["Id"])
        return

    t = tasks[0]
    mins = task_duration_minutes(t)
    print(f"[keepalive] task {t['Id']} running {mins} min")
    if mins >= MAX_TASK_MINUTES:
        print(f"[keepalive] reached {MAX_TASK_MINUTES} min, rotating")
        cancel_task(t["Id"])
        time.sleep(20)
        create_runbook_run()
    else:
        print(f"[keepalive] task healthy, will rotate at {MAX_TASK_MINUTES} min")


if __name__ == "__main__":
    main()
