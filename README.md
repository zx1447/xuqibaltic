# plugged.host 免费服守护（Active 分支）

## 目标

服务器 **mockingbird（嘲鸫）**

| 项 | 值 |
|---|---|
| uuid | `75cd14b3-aaf3-4a1c-a214-dbd787e76d5a` |
| 面板 | `https://panel.plugged.host`（Pelican，非 Pterodactyl） |
| 节点 | pl2 (Poland) |
| 地址 | `pl2.plugged.host:34066` |
| 配置 | Minecraft Java · 12GB RAM / 50GB disk / 400% CPU |
| egg | Minecraft: Java Edition（Java 21） |

## 做什么

每 4 小时检查一次。发现 `offline` 就调用开机接口拉起。

```
running / starting  → 不动
stopping / installing / restarting / restoring → 等下轮
其余（offline 等）→ POST /power {"signal":"start"} → 轮询到 running
suspended → 跳过
```

## API（2026-11-14 实测）

```
GET  /api/client/servers
     → {"servers":{"data":[{uuid,name,is_suspended,allocation,egg,limits,...}]}}

GET  /api/client/servers/<uuid>/resources
     → {"resources":{state,uptime,cpu_absolute,memory_bytes,network:{...}}}
       state ∈ running / starting / stopping / offline / installing / restoring

POST /api/client/servers/<uuid>/power   {"signal":"start"}
     → 200 {}
     注意：字段名是 signal（Pelican 与 Pterodactyl 一致），不是 action

鉴权：Authorization: Bearer <c7sp_ 开头的客户端 API key>
```

## 文件

| 文件 | 作用 |
|---|---|
| `plugged_keepalive.py` | 主脚本：检测 + 开机 + 复查 |
| `plugged_state.py` | 只读探测，供 dry_run 用 |

## Workflow

`.github/workflows/plugged_keepalive.yml`（在 main 分支上）

```yaml
on:
  schedule:
    - cron: '17 */4 * * *'    # 每 4 小时，避开整点
  workflow_dispatch:
    inputs:
      dry_run:                # 只读状态，不开机
```

用 `ref: Active` 检出本分支。

**为什么不用长驻 330 分钟循环 + 接力链**：
这个任务太轻了（2 个 GET，偶尔 1 个 POST，几秒跑完）。
长驻 job 为了省调度次数要占一整个并发槽、还要维护接力链，
而接力的 `--ref` 一旦写错就会在别的分支上无限复制（本仓库踩过这个坑）。
定时跑更简单，也省 Actions 配额。

## Secret

`PLUGGED_API_KEY` —— `c7sp_` 开头的客户端 API key。

> 注意：这个 key 属于 **panel.plugged.host**，在 dash.acorehosting.com 上无效
> （虽然前缀 `c7sp_` 和 AcoreHosting 的一样，但它们是不同的服务）。

## 恢复 / 调整

- 改检测频率：改 cron 表达式
- 改目标服务器：改 workflow 里的 `PLUGGED_SERVERS`，或加多个 uuid 用逗号分隔
- 只看不动手：手动触发时把 `dry_run` 填 `true`
