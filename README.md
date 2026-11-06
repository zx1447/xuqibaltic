# Q-Fi Cloud 免费服务器续期（每 2 天）

`https://gp.qficloud.com` · 服务器 UUID `2909e505-6516-4d30-86ca-ae75af6aa91b`

## 端点

```
POST /api/client/dezerx/server-create/<UUID>/renew
Authorization: Bearer ptlc_xxxxxxxx     ← Pterodactyl Client API key
→ 200 OK       续期成功
→ 401          Unauthenticated（key 无效/过期）
```

`dezerx` 是套在 Pterodactyl 上的第三方主题（`server-create` 是它自己的路由前缀），
所以续期端点在 `/api/client/dezerx/...` 而不是标准的 `/api/client/servers/...`。

## 认证怎么拿

**必须用 Client API key**，网页登录的 cookie 不顶用（Pterodactyl 的 API 中间件只认 `Authorization: Bearer`，
实测空 cookie / 空 Bearer 一律 401 `AuthenticationException`）。

获取步骤：

1. 浏览器登录 `https://gp.qficloud.com`（登录页带 Google reCAPTCHA，这步只能真人做）
2. 左侧菜单 → **API credentials**（地址 `https://gp.qficloud.com/account/api`）
3. Description 随便填（如 `renew-bot`），权限至少勾上：
   - `renew` / `server.create`（Dezerx 的续期权限，名字可能略有差异，**全勾最省事**）
4. 生成后复制 `ptlc_` 开头那串，**只显示一次**
5. 填进仓库 secret `QI_API_KEY`

## 实测结论（2026-11-06，用真 key 跑通）

| 项 | 值 |
|---|---|
| key | ✅ `GET /api/client/account` → 200，`maikeruisi_rshh` / `zxy1715x@gmail.com`（id 3277） |
| 服务器 | `Free Q-Fi Server Paper`，标识 `2909e505`，节点 `gs1.ca` |
| 配置 | 8192 MB 内存 / 15360 MB 盘 / 150% CPU / io 500 |
| 当前状态 | `running`，已运行 294202325 秒，占用 2.0 GB 内存、1.25 GB 盘（**没被暂停**） |
| **续期规则** | 周期 **7 天**，窗口在到期前 **48 小时**开放，`graceHours: 24`，暂停后 **5 天**清除 |
| **下次可续** | `renewAt = 2026-11-15T00:19:36-05:00` = **2026-11-15 05:19:36 UTC**（查时时还有 209 小时） |
| 上次续期 | `lastRenewedAt = 2026-11-06T06:52:55-05:00`，`renewals: 1` |
| 现在能续吗 | `renewable: false` → `POST renew` 返回 `400 DisplayException: This server cannot be renewed yet.` |

**所以「每 2 天一次」在服务端是行不通的** —— 它 7 天才开一次 48 小时的窗口，
脚本照 2 天点只会天天吃 400。现在的实现改成：

```
GET  .../renewal          → 读 renewable / renewAt（真值来源）
renewable=false           → 跳过，不空点
renewable=true 且间隔够    → POST .../renew
```

`QI_INTERVAL_DAYS=2` 保留为「防重复点」的最小间隔，cron 改成 **每 6 小时查一次**，
窗口（48 小时）一开就能在 6 小时内抓住。

顺带发现：这个站的免费额度是 `perUser: 1`、`held: 1`（已用满），
且要求 Discord 绑定（`discord.linked: true`，账号 `Lycoris`）。

## 端点探测记录（2026-11-06）

| 探测 | 结果 |
|---|---|
| `POST .../renew`（无 key） | `401 AuthenticationException` |
| `POST .../renew`（假 UUID） | `401`（先过认证再校验资源，说明路由存在） |
| `GET /api/client/dezerx/server-create` | `401`（路由存在，不是 404） |
| `GET /api/client/dezerx/renewables` | `404`（没有这个官方端点） |
| `GET /api/client/account`（假 ptlc_ key） | `401` |
| 登录页 | `Sign in · Q-Fi Cloud`，Google **reCAPTCHA enabled**，有 Discord 链接 |

结论：**认证是唯一门槛**，拿到 API key 之后 `POST renew` 就是纯 HTTP，不需要浏览器、不需要验证码。
站点也没有像 MineHost/Severoo 那种「看广告 / 过人机验证」的环节，属于最好自动化的一类。

## 精准触发（当前实现）

不再是「每 N 天盲点」，而是**算到服务端给的 renewAt，主动睡到窗口开启那一秒再提交**：

```
GET  .../renewal  →  renewAt / renewable
        │
        ├─ 距 renewAt ≤ 330 分钟 → sleep 到 renewAt + 30s 余量 → POST renew
        │                          （等待期间每 5 分钟复查，服务端提前开窗就立即续）
        └─ 距 renewAt > 330 分钟 → 只报告，交给后面每 6 小时的 cron 接力
```

具体机制：

| 参数 | 默认 | 作用 |
|---|---|---|
| `QI_WAIT` | `1` | 开启精准等待 |
| `QI_WAIT_MAX_MIN` | `330` | 单次 job 最多睡多久（GitHub 上限 350 分钟） |
| `QI_SLACK_SEC` | `30` | 到点后再补的余量，抵消服务端/runner 时钟差 |
| `QI_POLL_SEC` | `300` | 等待期间复查状态的间隔 |
| `QI_INTERVAL_DAYS` | `2` | 最小间隔兜底，防止窗口内重复点 |
| `QI_FORCE` / `QI_DRY_RUN` | `0` | 调试用 |

到点后若服务端仍说 `renewable=false`，会再复查 3 轮（每轮 60 秒）；
若服务端把窗口推后超过 1 小时，则本轮退出，等下一次 cron。

`concurrency.cancel-in-progress = false` —— 精准等待期间不会被下一次定时任务抢占。

**实测校验**（用 `QI_OVERRIDE_RENEW_AT` / `QI_OVERRIDE_RENEWABLE` 伪造时间跑通）：

```
⏳ 精准等待 0.2 分钟，到 2026-11-06 12:04:15 UTC 再提交
…还差 0.1 分钟
🎯 POST /api/client/dezerx/server-create/<uuid>/renew
📡 HTTP 400 DisplayException: This server cannot be renewed yet.
⏳ 服务端说还太早；不计入 last_click_at      ← 不会污染状态
```

## 节奏

`QI_INTERVAL_DAYS=2`：距上次 **成功**（200）续期满 2 天才提交下一次。

cron 用**每天唤醒**（`41 7 * * *`），真正判定在脚本里：

- 满 2 天 → 提交 `POST renew`
- 服务端回「太早」→ 只记 `too_early_since`，**不写 `last_click_at`**，并把间隔调小即可贴近窗口
- 429 / 5xx → 8 秒后重试一次

## 文件

| 文件 | 作用 |
|---|---|
| `renew_qi.js` | 主脚本（读状态 → 判定间隔 → POST renew → 写状态） |
| `.github/workflows/renew_qi.yml` | 仓库工作流 `Q-Fi Cloud Renew`，cron `41 7 * * *` |
| `qi_state.json` | `last_click_at` / `last_verdict` / `too_early_since` |
