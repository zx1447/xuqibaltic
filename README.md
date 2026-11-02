# Vertos Hub AFK Earn

`vertos` 分支用于 [Vertos Hub](https://hub.vertos.in/earn/afk) 的 AFK 自动挂机赚积分。

## 协议（对真实接口验证过）

三段式会话，跟 LemonHost 那种"一个 POST 搞定"完全不同：

```
1. POST /api/auth/login       {"identifier": "...", "password": "..."}
   200 → data.user，下发 cookie vertos_hub_session（14 天）

2. POST /api/afk/start        （无 body）
   201 → {"sessionId", "sessionToken", "expiresAt", "startedAt"}
   409 → SESSION_ALREADY_ACTIVE（同账号已有会话）

3. POST /api/afk/heartbeat    {"sessionId": "...", "token": "..."}
   200 → {"sessionSeconds","todaySeconds","todayCredits","todayEarnedCredits",
          "pendingFractionalCredits","lifetimeSeconds","lifetimeCredits",
          "fractionalSeconds","secondsToNextCredit","progressPercent",
          "creditsAwarded","expiresAt"}
   403 → INVALID_TOKEN
   410 → SESSION_EXPIRED（租约过期，必须重新 start）

4. POST /api/afk/stop         {"sessionId": "...", "token": "..."}  退出时调用

辅助：GET /api/afk/status（activeSession + progress）、GET /api/credits（钱包余额）
```

### 实测参数

| 项目 | 值 |
|---|---|
| 心跳间隔 | **30 秒**（网页端同频） |
| 租约 | 初始 90 秒，每次心跳滑动续到 60 秒后 |
| 收益 | **1 credit / 50 分钟**（0.02 credits/分钟） |
| 单账号并发 | 只能一个会话，第二个 start 返回 409 |
| 验证码 | 无（无 Turnstile / hCaptcha / reCAPTCHA） |
| 登录字段 | `identifier` —— **用户名和邮箱都能用** |

### 两个坑

**1. `activeSession` 里没有 `sessionToken`**

`/api/afk/status` 返回的 `activeSession` 只有 `sessionId`，没有 token，所以
**看到的活跃会话无法复用**。遇到 409 唯一可靠的做法是等旧租约自然失效。

**2. 被 kill 的 CI job 会留下"幽灵会话"**

job 如果死在 start 和 stop 之间，会话会一直挂着，导致后续每轮都 409。脚本因此会：

- 把 `(sessionId, sessionToken)` 存进 `vertos_state.json`
- 下次遇到 409 时，先尝试用存下的凭据 `stop` 释放掉
- 释放不了就等 25 秒重试，最多 4 次

## 需要的 Secrets

| Secret | 必填 | 说明 |
|---|---|---|
| `VERTOS_IDENTIFIER` | ✅ | 登录用邮箱或用户名（实测 `450144yy@gmail.com` 可用） |
| `VERTOS_PASSWORD` | ✅ | 密码 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 | 本轮结束 / 登录失败通知 |
| `GH_PAT` | 可选 | 每轮结束自动派发下一轮 |

## 文件

- `earn_vertos.py` — 主脚本
- `earn_vertos.yml` — 工作流（同一份也放在 `main`，因为 schedule 只认默认分支）
- `vertos_state.json` — 运行状态，CI 自动写回

## 手动试跑

工作流输入：

- `run_minutes`：本轮跑多久（默认 355）
- `stop_after_credits`：今日 credits 达到该值就主动停（默认 0 = 不限）

比如填 `stop_after_credits=50`，攒够 50 credits 就停止本轮并通知——比 LemonHost
那种靠 429 反推上限要准确，因为 Vertos 直接返回 `todayCredits` 字段。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 本轮正常结束 |
| 1 | 登录/配置问题，需要人工处理 |
| 2 | 服务器不可达 |
