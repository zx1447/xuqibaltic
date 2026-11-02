# LemonHost AFK Claim

`lemo` 分支用于 LemonHost 面板的 AFK 积分自动领取。

## 接口（已对真实面板验证）

```
POST https://app.lemonhost.me/api/client/account/afk/claim
Authorization: Bearer ptlc_...
Content-Type: application/json

{"sessionId": "<任意字符串>"}
```

| 状态码 | 响应 | 含义 |
|---|---|---|
| 200 | `{"success":true,"credits":N,"afk_credits":N,"reward":1,"claimed_today":N,"daily_limit":1000}` | +1 credit |
| 429 | `{"error":"AFK reward already claimed. Wait for the timer to complete."}` | 冷却中，**不是错误** |
| 400 | `{"error":"Session ID is required"}` | 参数名写错（必须驼峰 `sessionId`） |

### 实测参数

| 项目 | 值 |
|---|---|
| 冷却 | **约 60 秒**（t=0/30s 返回 429，t=60s 返回 200） |
| 单次奖励 | 1 credit |
| 每日上限 | 1000 credits / 账号 |
| 认证 | Pterodactyl Client API Key（`ptlc_` 前缀） |

注意：服务端**不校验 sessionId 真伪**，传任意字符串都能领取。脚本仍会生成并持久化一个固定 ID，保持行为一致。

### 排查记录

`/api/client/account/afk/start` 和 `/afk/stop` 这两个路由也存在，但无论传什么参数都返回
`500 UnexpectedValueException` —— 实际领取只需要 `/afk/claim`，`start` 是废弃/未实现的死路由。

## 需要的 Secrets

| Secret | 必填 | 说明 |
|---|---|---|
| `LEMONHOST_API_KEY` | ✅ | 面板 → Account → API Credentials 生成（`ptlc_...`） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 | 本轮结束 / 认证失败时通知 |
| `GH_PAT` | 可选 | 每轮结束后自动派发下一轮 |

## 文件

- `earn_lemonhost.py` — 主脚本
- `earn_lemonhost.yml` — 实际使用的工作流（在 `main` 分支上）
- `lemo_state.json` — 运行状态，由 CI 自动写回本分支

## 手动试跑

工作流输入：

- `run_minutes`：本轮跑多久（默认 340）
- `dry_run`：`true` 只试一次、不持续领取（默认 `false`）

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 本轮正常结束 |
| 1 | 认证/配置问题，需要人工换 Key |
| 2 | 接口不可达，稍后重试 |
