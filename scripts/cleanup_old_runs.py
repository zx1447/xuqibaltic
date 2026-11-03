#!/usr/bin/env python3
"""删除 14 天前的分支工作流（workflow runs）。

规则
----
1. 只看「非默认分支」的运行 —— main / master 上的运行一律不动。
2. 只看创建时间早于 N 天前的运行（默认 14 天，可用 --days 覆盖）。
3. 默认 dry-run，只打印将被删除的东西；确认无误后加 --apply 才真删。
4. 保护名单里的工作流（默认包含 siam / vertos / lemo / arkya / zyf 等常驻任务）
   以及每个工作流最近 1 次运行，一律不删，避免把有用的历史清光。
5. 保护名单里的分支同样跳过。

用法
----
  python cleanup_old_runs.py                  # 干跑，看会删什么
  python cleanup_old_runs.py --apply          # 真删
  python cleanup_old_runs.py --days 30 --apply
  python cleanup_old_runs.py --keep-workflows foo,bar --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"


def api(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | list | None]:
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit("ERROR: 缺少 GH_TOKEN / GITHUB_TOKEN 环境变量")
    url = path if path.startswith("http") else f"{API}{path}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "branch-run-cleaner",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"  HTTP {exc.code} {method} {url} -> {body[:200]}", flush=True)
        return exc.code, None
    except Exception as exc:  # noqa: BLE001
        print(f"  ERR {method} {url} -> {type(exc).__name__}: {exc}", flush=True)
        return 0, None


def parse_list(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.getenv("CLEANUP_DAYS", "14")))
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只干跑）")
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    ap.add_argument("--keep-workflows", default=os.getenv("KEEP_WORKFLOWS", ""),
                    help="额外保护的工作流名，逗号分隔（会追加到默认保护名单）")
    ap.add_argument("--keep-branches", default=os.getenv("KEEP_BRANCHES", ""))
    ap.add_argument("--keep-latest", type=int, default=int(os.getenv("KEEP_LATEST_PER_WORKFLOW", "1")))
    args = ap.parse_args()

    repo = args.repo
    if not repo or "/" not in repo:
        raise SystemExit("ERROR: 需要 --repo owner/name 或 GITHUB_REPOSITORY")

    # 常驻任务的运行记录默认保护（这些每天都会跑，删了不便排查）
    default_keep = "siam,vertos,lemonhost,lemo,arkya,lunorahost,witchly,volyxhost"
    keep_workflows = {w.lower() for w in parse_list(default_keep + "," + (args.keep_workflows or ""))}
    keep_branches = {b.lower() for b in parse_list(args.keep_branches)}

    st, repo_info = api(f"/repos/{repo}")
    default_branch = (repo_info or {}).get("default_branch", "main")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    print("=" * 74, flush=True)
    print(f"仓库           : {repo}", flush=True)
    print(f"默认分支       : {default_branch}（永远不清理）", flush=True)
    print(f"截止时间       : {cutoff.isoformat(timespec='seconds')}（{args.days} 天前）", flush=True)
    print(f"保护工作流     : {sorted(keep_workflows) or '(无)'}", flush=True)
    print(f"保护分支       : {sorted(keep_branches) or '(无)'}", flush=True)
    print(f"每个工作流保留 : 最近 {args.keep_latest} 次", flush=True)
    print(f"模式           : {'真删 APPLY' if args.apply else '干跑 DRY-RUN'}", flush=True)
    print("=" * 74, flush=True)

    # 1) 拉全部工作流
    workflows: list[dict] = []
    page = 1
    while True:
        st, data = api(f"/repos/{repo}/actions/workflows?per_page=100&page={page}")
        if st != 200 or not isinstance(data, dict):
            break
        batch = data.get("workflows") or []
        workflows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    print(f"\n共 {len(workflows)} 个工作流", flush=True)

    to_delete: list[dict] = []
    protected_count = 0
    scanned = 0

    for wf in workflows:
        wid = wf["id"]
        wname = wf.get("name") or "(无名)"
        wpath = wf.get("path") or ""
        # 支持三种匹配：完整名字、子串（如 siam 命中 "Siam Auto Check-in"）、文件路径
        haystack = f"{wname.lower()} {wpath.lower()}"
        if any(k in haystack for k in keep_workflows):
            print(f"\n🔒 保护工作流，整组跳过: {wname} ({wpath})", flush=True)
            protected_count += 1
            continue

        # 拉该工作流的运行历史
        runs: list[dict] = []
        page = 1
        while True:
            st, data = api(f"/repos/{repo}/actions/workflows/{wid}/runs?per_page=100&page={page}")
            if st != 200 or not isinstance(data, dict):
                break
            batch = data.get("workflow_runs") or []
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        scanned += len(runs)

        kept_latest = 0
        hits = 0
        for run in runs:
            branch = run.get("head_branch") or ""
            created = run.get("created_at") or ""
            run_id = run.get("id")

            # 默认分支不动
            if branch == default_branch:
                continue
            if branch.lower() in keep_branches:
                continue
            # 每个工作流最近 N 次不删
            if kept_latest < args.keep_latest:
                kept_latest += 1
                continue
            try:
                dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt >= cutoff:
                continue

            hits += 1
            to_delete.append({
                "workflow": wname,
                "path": wpath,
                "branch": branch,
                "id": run_id,
                "created_at": created,
                "conclusion": run.get("conclusion"),
                "event": run.get("event"),
                "number": run.get("run_number"),
            })

        if hits:
            print(f"\n📋 {wname} ({wpath}): {hits} 条待删 / 共 {len(runs)} 条历史", flush=True)

    print("\n" + "=" * 74, flush=True)
    print(f"扫描运行记录: {scanned} 条", flush=True)
    print(f"命中待删    : {len(to_delete)} 条", flush=True)
    print(f"保护工作流  : {protected_count} 个", flush=True)
    print("=" * 74, flush=True)

    if not to_delete:
        print("\n✅ 没有需要清理的运行记录", flush=True)
        write_summary(0, 0, args.days, args.apply, [])
        return 0

    print("\n--- 待删清单（最多显示 60 条）---", flush=True)
    for item in to_delete[:60]:
        print(f"  #{item['number']:<5} {item['created_at']}  {item['branch']:<28} "
              f"{str(item['conclusion']):<9} {item['workflow']}", flush=True)
    if len(to_delete) > 60:
        print(f"  …另有 {len(to_delete) - 60} 条", flush=True)

    if not args.apply:
        print("\n💡 这是干跑。确认无误后加 --apply 执行删除。", flush=True)
        write_summary(len(to_delete), 0, args.days, False, to_delete)
        return 0

    print("\n--- 开始删除 ---", flush=True)
    deleted = 0
    failed = 0
    for i, item in enumerate(to_delete, 1):
        st, _ = api(f"/repos/{repo}/actions/runs/{item['id']}", method="DELETE")
        if st in (204, 200):
            deleted += 1
        else:
            failed += 1
        if i % 25 == 0:
            print(f"  进度 {i}/{len(to_delete)}  成功 {deleted}  失败 {failed}", flush=True)
        # 温和限速，避免踩 secondary rate limit
        time.sleep(0.35)

    print(f"\n✅ 完成：删除 {deleted} 条，失败 {failed} 条", flush=True)
    write_summary(len(to_delete), deleted, args.days, True, to_delete)
    return 0 if failed == 0 else 1


def write_summary(candidates: int, deleted: int, days: int, applied: bool, items: list[dict]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## 分支工作流清理",
        "",
        f"- 阈值：{days} 天前",
        f"- 模式：{'真删' if applied else '干跑'}",
        f"- 命中：{candidates} 条",
        f"- 已删：{deleted} 条",
        "",
    ]
    if items:
        lines += ["| 运行 | 分支 | 创建时间 | 工作流 |", "|---|---|---|---|"]
        for it in items[:50]:
            lines.append(f"| #{it['number']} | `{it['branch']}` | {it['created_at']} | {it['workflow']} |")
        if len(items) > 50:
            lines.append(f"| … | | | 另有 {len(items) - 50} 条 |")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
