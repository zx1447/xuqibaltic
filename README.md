# Octagon Circle — AFK 挂机

本分支已整理为专用分支，只保留 Octagon AFK 的一个 workflow、执行脚本和说明。GitHub 定时任务只会从仓库默认分支运行；实际连续定时运行仍保留在默认分支 `main`，此分支不改默认分支，也不影响仓库其他项目。


`POST /api/user/earn/afk/work` 每 60 秒一次心跳，纯 HTTP，无浏览器。

## 结论（实测）

| 项 | 值 |
|---|---|
| 用户 | `livelyeagle814` / `zxy1715x@gmail.com` |
| 端点 | `POST https://client.octagoncircle.org/api/user/earn/afk/work` |
| 请求体 | `{"coins_earned":0,"minutes_afk":1}` |
| 鉴权 | Cookie `user_token`（登录后由服务端下发） |
| 回报 | 每次心跳 **固定 3 credits**，`total_afk_time` 每次 +1 分钟 |
| 客户端上报值 | **完全被忽略**（传 0 也发 3，无作弊空间） |
| 响应字段 | `coins_awarded` / `total_coins` / `total_afk_time` |
| rate limit | 50 |

## 登录闸门（无法绕过）

`login` / `register` / `forgot` / `reset` / `2fa` **五个入口全部强制 Turnstile**。
实测：错误密码 + 假 token → `TURNSTILE_FAILED`（服务器先验验证码，再验密码）。

- 本机出口 `34.169.2.232`：widget 渲染 ✅ token ❌（headless + 坐标点击均失败）
- GitHub runner `52.176.36.132`：headless ❌ / headed(Xvfb) ❌

结论：**换 IP 无用**，Turnstile 判的是浏览器指纹与自动化痕迹。Cookie 必须由真人在浏览器登录后导出。

## 运行

```bash
OCTAGON_COOKIE='user_token=...; csrf_token=...' AFK_MINUTES=30 node afk_octagon.js
```

- `AFK_DRY=1` 干跑（不发请求，只打印节奏）
- 会话失效 → `HTTP 401 MISSING_ACCOUNT_TOKEN` → 退出码 3
- 连续失败 3 次自动中止，避免把账号刷进风控


## Cookie 有效期：滑动 30 分钟，可永久自动续

关键发现：**服务端每个响应都会 `Set-Cookie` 回写**，`Max-Age=1800`。

```
set-cookie: user_token=azrY1Eti…; Max-Age=1800; HttpOnly; SameSite=Strict
set-cookie: session_fingerprint=…; Max-Age=1800     ← 每次都在变
set-cookie: csrf_token=…;          Max-Age=1800     ← 每次都在变
set-cookie: last_activity=…;       Max-Age=1800
```

实测结论：

| 问题 | 答案 |
|---|---|
| 有效期多久 | **30 分钟**（`Max-Age=1800`），30 分钟内无请求即掉线 |
| 是滑动过期吗 | **是**。任意一次心跳都会重置为新的 30 分钟 |
| `user_token` 会变吗 | **不变**，永远同一个值；变的只有 fingerprint / csrf / last_activity |
| 能否自动续 | **能**。不需要密码，不需要 Turnstile |
| 传什么 cookie 才够 | 只要 `user_token` 一项就能鉴权（fingerprint/csrf/last_activity 都验证过可省） |
| 能否伪造 last_activity | 能放（200），但无意义——服务端写自己的时间戳 |

### 自动化链路（已实测闭环）

```
afk_octagon.js  ← 从 secret 读 cookie
   ├─ 吸收每个响应里的 Set-Cookie（cookie 罐子）
   ├─ 跑 15 分钟心跳
   └─ 结束把最新 cookie 写进 GITHUB_OUTPUT
        ↓
gh secret set OCTAGON_COOKIE  ← workflow 写回
        ↓
gh workflow run afk_octagon.yml  ← 接力下一轮
        ↓（兜底）
cron */20 * * * *
```

只要第一次给一条有效 cookie，之后**全自动，永不手工续**。

> 唯一的死局：Cookie 彻底失效（改密码 / 主动登出 / 服务端清 Redis）。那时才需要重新登录一次。

## CI

`.github/workflows/afk_octagon.yml` — 每 20 分钟一轮跑 15 分钟（留 5 分钟余量），跑完写回 secret 并接力下一轮，cron 兜底。

| 项 | 值 |
|---|---|
| 节奏 | `*/20 * * * *`，每轮 15 分钟心跳 |
| 收益 | 15 次心跳 × 3 = **45 币/轮**，约 **135 币/小时** |
| secret | `OCTAGON_COOKIE`（自动续期写回）、`GH_PAT`（写 secret + dispatch） |
| 实测 | run `42342894614` 入账 9 币 → secret 更新时间 `05:31:04Z` → 接力 run `42343056893` 于 `05:31:05Z` 拉起 |
