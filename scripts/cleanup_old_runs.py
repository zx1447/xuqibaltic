#!/usr/bin/env python3
"""GitHub Actions 运行记录清理：按「页」保留，删除第 N 页之后的所有记录。

默认（对应「每隔 7 天删除 14 页后面的记录」）：
  按创建时间倒序，**保留最新 14 页 × 100 条 = 1400 条运行记录**，
  第 15 页往后（更早）的全部删除。

不影响后续任务的安全保险（全部默认开启）
----------------------------------------
1. 只删已结束的运行：`status != completed`（进行中 / 排队中）一律跳过
2. 只删够老的运行：默认不碰 1 天内创建的记录（`--min-age-days 1`）
3. 每个工作流无条件保留最近 1 次运行（`--keep-latest-per-workflow 1`），
   这样「最近一次成功/失败」永远可查
4. 保护窗口内的记录一律不删（默认 14 页）
5. 删除不改变 run_number 序列，也不影响 cron 调度 —— 脚本会在结束时
   校验所有工作流仍然是 active，并把每个 cron 打出来对照

运行记录和「任务调度」本来就是两件事：cron 由仓库里的 workflow 定义决定，
删历史运行不会让任何任务少跑一次。脚本最后会显式验证这一点。

用法：
  python cleanup_old_runs.py                        # 干跑，看会删多少
  python cleanup_old_runs.py --apply                # 真删（保留 14 页）
  python cleanup_old_runs.py --keep-pages 30 --apply
  python cleanup_old_runs.py --min-age-days 3 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
_RATE_LOCK_UNTIL = 0.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "actions-run-cleaner",
    }


def _wait_rate_limit() -> None:
    global _RATE_LOCK_UNTIL
    now = time.time()
    if _RATE_LOCK_UNTIL > now:
        s = _RATE_LOCK_UNTIL - now
        print(f"  ⏳ 限流中，等待 {s/60:.1f} 分钟…", flush=True)
        time.sleep(min(s, 3600))


def request(path: str, method: str = "GET") -> tuple[int, object, dict]:
    global _RATE_LOCK_UNTIL
    if not TOKEN:
        raise SystemExit("ERROR: 缺少 GH_TOKEN / GITHUB_TOKEN")
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(5):
        _wait_rate_limit()
        req = urllib.request.Request(url, method=method, headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(body) if body.strip() else None), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            hdrs = dict(exc.headers or {})
            exc.read()
            if exc.code == 403 and hdrs.get("x-ratelimit-remaining") == "0":
                reset = int(hdrs.get("x-ratelimit-reset") or 0)
                _RATE_LOCK_UNTIL = max(_RATE_LOCK_UNTIL, reset + 5)
                continue
            if exc.code in (429, 403):
                time.sleep(20 * (attempt + 1))
                continue
            return exc.code, None, hdrs
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                print(f"  ERR {method} {url} -> {type(exc).__name__}: {exc}", flush=True)
                return 0, None, {}
            time.sleep(2 * (attempt + 1))
    return 0, None, {}


def parse_ts(v: str) -> datetime | None:
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def enumerвате_runs(repo: str, per_page: int = 100):
    """按时间倒序枚举所有运行记录（GitHub 默认就是最新在前）。"""
    page = 1
    seen = 0
    total = None
    while True:
        st, data, _ = request(f"/repos/{repo}/actions/runs?per_page={per_page}&page={page}")
        if st != 200 or not isinstance(data, dict):
            print(f"  ⚠️ 第 {page} 页拉取失败 (HTTP {st})，停止枚举", flush=True)
            return
        batch = data.get("workflow_runs") or []
        if total is None:
            total = data.get("total_count") or 0
            print(f"  仓库共 {total} 条运行记录", flush=True)
        if not batch:
            return
        for r in batch:
            yield r
            seen += 1
        if len(batch) < per_page or (total and seen >= total):
            return
        if page % 5 == 0:
            print(f"  已枚举 {seen}/{total}…", flush=True)
        page += 1


def audit_workflows(repo: str) -> dict:
    """清理后校验：所有工作流仍然 active，并把 cron 打出来。"""
    st, data, _ = request(f"/repos/{repo}/actions/workflows?per_page=100")
    result = {"total": 0, "active": 0, "not_active": [], "crons": {}}
    if st != 200 or not isinstance(data, dict):
        return result
    wfs = data.get("workflows") or []
    result["total"] = len(wfs)
    for wf in wfs:
        name = wf.get("name") or wf.get("path")
        if wf.get("state") == "active":
            result["active"] += 1
        else:
            result["not_active"].append(f"{name} -> {wf.get('state')}")
        # 从文件里读 cron
        path = wf.get("path") or ""
        if path:
            st2, f, _ = request(f"/repos/{repo}/contents/{path}")
            if st2 == 200 and isinstance(f, dict) and f.get("content"):
                import base64
                try:
                    text = base64.b64decode(f["content"]).decode("utf-8", "replace")
                    crons = re.findall(r'cron:\s*["\']([^"\']+)["\']', text)
                    if crons:
                        result["crons"][name] = crons
                except Exception:  # noqa: BLE001
                    pass
    return result


def write_summary(ctx: dict) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    keep = ctx["keep_pages"] * ctx["per_page"]
    lines = [
        "## Actions 运行记录清理（按页保留）",
        "",
        f"- 保留窗口：**最新 {ctx['keep_pages']} 页 × {ctx['per_page']} = {keep} 条**",
        f"- 模式：{'真删' if ctx['apply'] else '干跑'}",
        f"- 清理前：{ctx['total']} 条 → 清理后：{ctx['after']} 条",
        f"- 命中：**{ctx['hits']}**，已删：**{ctx['deleted']}**，失败：{ctx['failed']}",
        f"- 跳过（进行中/排队）：{ctx['skipped_running']}",
        f"- 跳过（未满 {ctx['min_age_days']} 天）：{ctx['skipped_young']}",
        "",
        "### 调度完整性校验",
        "",
        f"- 工作流总数：{ctx['audit'].get('total')}，active：{ctx['audit'].get('active')}",
        f"- 非 active：{ctx['audit'].get('not_active') or '无 ✅'}",
        "",
    ]
    crons = ctx["audit"].get("crons") or {}
    if crons:
        lines += ["| 工作流 | cron |", "|---|---|"]
        for name, cs in list(crons.items())[:30]:
            lines.append(f"| {name} | `{', '.join(cs)}` |")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    ap.add_argument("--keep-pages", type=int, default=int(os.getenv("KEEP_PAGES", "14")))
    ap.add_argument("--per-page", type=int, default=int(os.getenv("PER_PAGE", "100")))
    ap.add_argument("--min-age-days", type=float, default=float(os.getenv("MIN_AGE_DAYS", "1")))
    ap.add_argument("--keep-latest-per-workflow", type=int,
                    default=int(os.getenv("KEEP_LATEST_PER_WORKFLOW", "1")))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=int(os.getenv("CLEANUP_WORKERS", "6")))
    args = ap.parse_args()

    repo = args.repo
    if not repo or "/" not in repo:
        raise SystemExit("ERROR: 需要 --repo owner/name 或 GITHUB_REPOSITORY")

    keep_n = args.keep_pages * args.per_page
    min_age_cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)

    print("=" * 76, flush=True)
    print(f"仓库            : {repo}", flush=True)
    print(f"保留窗口        : 最新 {args.keep_pages} 页 × {args.per_page} = {keep_n} 条", flush=True)
    print(f"删除范围        : 第 {args.keep_pages + 1} 页及之后（更早的）全部", flush=True)
    print(f"最小年龄保护    : 不删 {args.min_age_days} 天内创建的记录", flush=True)
    print(f"每工作流保留    : 最近 {args.keep_latest_per_workflow} 次（无条件）", flush=True)
    print(f"模式            : {'真删 APPLY' if args.apply else '干跑 DRY-RUN'}", flush=True)
    print("=" * 76, flush=True)

    before_total = 0
    st, data, _ = request(f"/repos/{repo}/actions/runs?per_page=1")
    if st == 200 and isinstance(data, dict):
        before_total = data.get("total_count") or 0

    # 枚举全部（倒序）
    ordered: list[dict] = []
    for run in enumerвате_runs(repo, args.per_page):
        ordered.append(run)
    print(f"\n枚举完成：{len(ordered)} 条", flush=True)

    # 每个工作流最近 N 次的 id 集合（无条件豁免）
    per_wf: dict[int, list[int]] = {}
    for run in ordered:  # ordered 是最新在前
        wid = run.get("workflow_id") or 0
        lst = per_wf.setdefault(wid, [])
        if len(lst) < args.keep_latest_per_workflow:
            lst.append(run["id"])
    protected_ids: set[int] = {i for lst in per_wf.values() for i in lst}

    hits: list[dict] = []
    skipped_running = 0
    skipped_young = 0
    by_branch: dict[str, int] = {}

    for idx, run in enumerate(ordered):
        # 前 keep_n 条 = 保护窗口，一律不删
        if idx < keep_n:
            continue
        if run["id"] in protected_ids:
            continue
        if run.get("status") != "completed":
            skipped_running += 1
            continue
        dt = parse_ts(run.get("created_at") or "")
        if dt is not None and dt >= min_age_cutoff:
            skipped_young += 1
            continue
        branch = run.get("head_branch") or "(none)"
        hits.append({
            "id": run["id"],
            "number": run.get("run_number"),
            "branch": branch,
            "created_at": run.get("created_at"),
            "conclusion": run.get("conclusion"),
            "workflow": run.get("name") or run.get("path"),
        })
        by_branch[branch] = by_branch.get(branch, 0) + 1

    after_expected = len(ordered) - len(hits)
    print("\n" + "=" * 76, flush=True)
    print(f"清理前记录      : {before_total}（枚举到 {len(ordered)}）", flush=True)
    print(f"命中待删        : {len(hits)}", flush=True)
    print(f"保留            : {len(ordered) - len(hits)} 条（目标 ≥ {keep_n}）", flush=True)
    print(f"跳过-进行中     : {skipped_running}", flush=True)
    print(f"跳过-太新       : {skipped_young}", flush=True)
    print("=" * 76, flush=True)

    if by_branch:
        print("\n按分支统计:", flush=True)
        for b, c in sorted(by_branch.items(), key=lambda x: -x[1])[:15]:
            print(f"   {b:<24} {c}", flush=True)

    ctx = {
        "keep_pages": args.keep_pages, "per_page": args.per_page,
        "apply": args.apply, "total": before_total, "after": after_expected,
        "hits": len(hits), "deleted": 0, "failed": 0,
        "skipped_running": skipped_running, "skipped_young": skipped_young,
        "min_age_days": args.min_age_days, "audit": {},
    }

    if not hits:
        print("\n✅ 已在保留窗口内，无需清理", flush=True)
        ctx["audit"] = audit_workflows(repo)
        write_summary(ctx)
        return 0

    if not args.apply:
        print("\n--- 待删示例（最早 20 条）---", flush=True)
        for it in hits[-20:]:
            print(f"  #{it['number']:<6} {it['created_at']}  {it['branch']:<20} "
                  f"{str(it['conclusion']):<10} {str(it['workflow'])[:38]}", flush=True)
        print(f"\n💡 干跑结束，加 --apply 执行删除（{len(hits)} 条）", flush=True)
        ctx["audit"] = audit_workflows(repo)
        write_summary(ctx)
        return 0

    print(f"\n--- 删除 {len(hits)} 条（{args.workers} 并发）---", flush=True)
    deleted = failed = 0
    t0 = time.time()

    def do_delete(run_id: int) -> bool:
        code, _, _ = request(f"/repos/{repo}/actions/runs/{run_id}", method="DELETE")
        return code in (200, 204, 404)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(do_delete, it["id"]): it for it in hits}
        for i, fut in enumerate(as_completed(futs), 1):
            if fut.result():
                deleted += 1
            else:
                failed += 1
            if i % 100 == 0 or i == len(hits):
                el = time.time() - t0
                rate = i / el if el else 0
                eta = (len(hits) - i) / rate if rate else 0
                print(f"  进度 {i}/{len(hits)}  成功 {deleted}  失败 {failed}  "
                      f"({rate:.1f}/s，剩余约 {eta/60:.1f} 分钟)", flush=True)

    st, data, _ = request(f"/repos/{repo}/actions/runs?per_page=1")
    after = (data or {}).get("total_count", "?") if isinstance(data, dict) else "?"

    print(f"\n✅ 完成：删除 {deleted} 条，失败 {failed} 条，耗时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    print(f"   清理后记录总数：{after}", flush=True)

    print("\n--- 调度完整性校验（删历史不影响 cron）---", flush=True)
    audit = audit_workflows(repo)
    print(f"   工作流 {audit['total']} 个，active {audit['active']} 个", flush=True)
    print(f"   非 active：{audit['not_active'] or '无 ✅'}", flush=True)
    for name, cs in list(audit["crons"].items())[:20]:
        print(f"   ⏰ {name}: {', '.join(cs)}", flush=True)

    ctx.update({"deleted": deleted, "failed": failed, "after": after, "audit": audit})
    write_summary(ctx)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
