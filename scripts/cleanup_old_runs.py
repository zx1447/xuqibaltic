#!/usr/bin/env python3
"""清理 GitHub Actions 运行记录：删除 N 天前的所有记录。

默认行为（对应「每隔 7 天删除 14 天前的所有记录」）：
  * 所有分支都算，不排除默认分支
  * 不排除任何工作流
  * 只删已经结束（status=completed）的运行，正在跑的不动

安全措施：
  * 默认干跑，必须 --apply 才真删
  * 自动处理 GitHub 的 403 限流（读 x-ratelimit-reset 等到恢复）
  * 单条失败不会中断整体，最后汇总

用法：
  python cleanup_old_runs.py                          # 干跑
  python cleanup_old_runs.py --apply                  # 真删（14 天前，全部分支）
  python cleanup_old_runs.py --days 30 --apply
  python cleanup_old_runs.py --keep-latest 5 --apply  # 每个工作流额外保留最近 5 次
  python cleanup_old_runs.py --only-branch-runs       # 只清非默认分支（旧行为）
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""

# 全局限流闸门：一旦撞到 403，把恢复时间记下来，所有线程一起等
_RATE_LOCK_UNTIL = 0.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "actions-run-cleaner",
    }


def wait_for_rate_limit() -> None:
    global _RATE_LOCK_UNTIL
    now = time.time()
    if _RATE_LOCK_UNTIL > now:
        sleep_for = _RATE_LOCK_UNTIL - now
        print(f"  ⏳ 触发限流，等待 {sleep_for/60:.1f} 分钟后继续…", flush=True)
        time.sleep(min(sleep_for, 3600))


def request(path: str, method: str = "GET") -> tuple[int, dict | list | None, dict]:
    global _RATE_LOCK_UNTIL
    if not TOKEN:
        raise SystemExit("ERROR: 缺少 GH_TOKEN / GITHUB_TOKEN")
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(4):
        wait_for_rate_limit()
        req = urllib.request.Request(url, method=method, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(body) if body.strip() else None), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            hdrs = dict(exc.headers or {})
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 403 and hdrs.get("x-ratelimit-remaining") == "0":
                reset = int(hdrs.get("x-ratelimit-reset") or 0)
                _RATE_LOCK_UNTIL = max(_RATE_LOCK_UNTIL, reset + 5)
                continue
            if exc.code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            return exc.code, None, hdrs
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                print(f"  ERR {method} {url} -> {type(exc).__name__}: {exc}", flush=True)
                return 0, None, {}
            time.sleep(2 * (attempt + 1))
    return 0, None, {}


def iter_runs(repo: str, per_page: int = 100):
    """按仓库维度枚举所有运行（不区分工作流、不区分分支）。"""
    page = 1
    seen = 0
    while True:
        st, data, _ = request(f"/repos/{repo}/actions/runs?per_page={per_page}&page={page}")
        if st != 200 or not isinstance(data, dict):
            print(f"  ⚠️ 拉取第 {page} 页失败 (HTTP {st})，停止枚举", flush=True)
            return
        batch = data.get("workflow_runs") or []
        if not batch:
            return
        for run in batch:
            yield run
            seen += 1
        total = data.get("total_count") or 0
        print(f"  已枚举 {seen}/{total} 条…", flush=True)
        if len(batch) < per_page:
            return
        page += 1


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def write_summary(ctx: dict) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Actions 运行记录清理",
        "",
        f"- 阈值：**{ctx['days']} 天前**（截止 {ctx['cutoff'].isoformat(timespec='seconds')}）",
        f"- 模式：{'真删' if ctx['apply'] else '干跑'}",
        f"- 枚举总数：{ctx['total']}",
        f"- 命中：**{ctx['hits']}**",
        f"- 已删：**{ctx['deleted']}**",
        f"- 失败：{ctx['failed']}",
        f"- 跳过（进行中）：{ctx['skipped_running']}",
        "",
    ]
    if ctx.get("by_branch"):
        lines += ["| 分支 | 命中 |", "|---|---|"]
        for b, c in sorted(ctx["by_branch"].items(), key=lambda x: -x[1])[:20]:
            lines.append(f"| `{b}` | {c} |")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.getenv("CLEANUP_DAYS", "14")))
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认干跑）")
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    ap.add_argument("--keep-latest", type=int,
                    default=int(os.getenv("KEEP_LATEST_PER_WORKFLOW", "0")),
                    help="每个工作流额外保留最近 N 次（默认 0 = 不保留）")
    ap.add_argument("--only-branch-runs", action="store_true",
                    help="只清非默认分支（旧行为；默认清理全部分支）")
    ap.add_argument("--workers", type=int, default=int(os.getenv("CLEANUP_WORKERS", "8")))
    ap.add_argument("--keep-workflows", default=os.getenv("KEEP_WORKFLOWS", ""),
                    help="排除某些工作流，逗号分隔子串匹配（默认不排除）")
    ap.add_argument("--keep-branches", default=os.getenv("KEEP_BRANCHES", ""),
                    help="排除某些分支，逗号分隔子串匹配（默认不排除）")
    args = ap.parse_args()

    repo = args.repo
    if not repo or "/" not in repo:
        raise SystemExit("ERROR: 需要 --repo owner/name 或 GITHUB_REPOSITORY")

    default_branch = "main"
    if args.only_branch_runs:
        st, info, _ = request(f"/repos/{repo}")
        if st == 200 and isinstance(info, dict):
            default_branch = info.get("default_branch", "main")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    keep_wf = [x.strip().lower() for x in (args.keep_workflows or "").split(",") if x.strip()]
    keep_br = [x.strip().lower() for x in (args.keep_branches or "").split(",") if x.strip()]

    print("=" * 74, flush=True)
    print(f"仓库        : {repo}", flush=True)
    print(f"范围        : {'仅非默认分支' if args.only_branch_runs else '全部分支（含 main）'}", flush=True)
    print(f"截止时间    : {cutoff.isoformat(timespec='seconds')}（{args.days} 天前）", flush=True)
    print(f"排除工作流  : {keep_wf or '(无)'}", flush=True)
    print(f"排除分支    : {keep_br or '(无)'}", flush=True)
    print(f"保留最近    : 每工作流 {args.keep_latest} 次", flush=True)
    print(f"并发        : {args.workers}", flush=True)
    print(f"模式        : {'真删 APPLY' if args.apply else '干跑 DRY-RUN'}", flush=True)
    print("=" * 74, flush=True)

    total = 0
    skipped_running = 0
    hits: list[dict] = []
    by_branch: dict[str, int] = {}
    per_wf_latest: dict[int, int] = {}

    # 先把所有运行按创建时间倒序收集齐，才能算「每个工作流最近 N 次」
    all_runs = list(iter_runs(repo))
    total = len(all_runs)
    all_runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    for run in all_runs:
        branch = run.get("head_branch") or "(none)"
        wid = (run.get("workflow_id") or 0)

        if args.only_branch_runs and branch == default_branch:
            continue
        if any(k in branch.lower() for k in keep_br):
            continue
        path = (run.get("path") or "").lower()
        name = (run.get("name") or run.get("display_title") or "").lower()
        if any(k in path or k in name for k in keep_wf):
            continue

        # 每个工作流最近 N 次豁免
        seen_cnt = per_wf_latest.get(wid, 0)
        per_wf_latest[wid] = seen_cnt + 1
        if seen_cnt < args.keep_latest:
            continue

        if run.get("status") != "completed":
            skipped_running += 1
            continue

        dt = parse_ts(run.get("created_at") or "")
        if dt is None or dt >= cutoff:
            continue

        hits.append({
            "id": run["id"],
            "number": run.get("run_number"),
            "branch": branch,
            "created_at": run.get("created_at"),
            "conclusion": run.get("conclusion"),
            "workflow": run.get("name") or path,
        })
        by_branch[branch] = by_branch.get(branch, 0) + 1

    print("\n" + "=" * 74, flush=True)
    print(f"枚举总数    : {total}", flush=True)
    print(f"命中待删    : {len(hits)}", flush=True)
    print(f"跳过（进行中）: {skipped_running}", flush=True)
    print("=" * 74, flush=True)

    if by_branch:
        print("\n按分支统计:", flush=True)
        for b, c in sorted(by_branch.items(), key=lambda x: -x[1])[:20]:
            print(f"   {b:<24} {c}", flush=True)

    ctx = {"days": args.days, "cutoff": cutoff, "apply": args.apply, "total": total,
           "hits": len(hits), "deleted": 0, "failed": 0,
           "skipped_running": skipped_running, "by_branch": by_branch}

    if not hits:
        print("\n✅ 没有需要清理的记录", flush=True)
        write_summary(ctx)
        return 0

    if not args.apply:
        print("\n--- 待删示例（最多 30 条）---", flush=True)
        for it in hits[:30]:
            print(f"  #{it['number']:<6} {it['created_at']}  {it['branch']:<20} "
                  f"{str(it['conclusion']):<10} {str(it['workflow'])[:40]}", flush=True)
        print("\n💡 干跑结束。加 --apply 执行删除。", flush=True)
        write_summary(ctx)
        return 0

    print(f"\n--- 开始删除 {len(hits)} 条（{args.workers} 并发）---", flush=True)
    deleted = 0
    failed = 0
    started = time.time()

    def do_delete(run_id: int) -> bool:
        st, _, _ = request(f"/repos/{repo}/actions/runs/{run_id}", method="DELETE")
        return st in (204, 200, 404)  # 404 = 已经没了，也算成功

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(do_delete, it["id"]): it for it in hits}
        for i, fut in enumerate(as_completed(futures), 1):
            if fut.result():
                deleted += 1
            else:
                failed += 1
            if i % 200 == 0 or i == len(hits):
                elapsed = time.time() - started
                rate = i / elapsed if elapsed else 0
                eta = (len(hits) - i) / rate if rate else 0
                print(f"  进度 {i}/{len(hits)}  成功 {deleted}  失败 {failed}  "
                      f"({rate:.1f}/s, 剩余约 {eta/60:.1f} 分钟)", flush=True)

    print(f"\n✅ 完成：删除 {deleted} 条，失败 {failed} 条，耗时 {(time.time()-started)/60:.1f} 分钟", flush=True)
    ctx.update({"deleted": deleted, "failed": failed})
    write_summary(ctx)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
