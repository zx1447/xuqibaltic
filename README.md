# LemonHost AFK Claim

`lemo` 分支用于 LemonHost 面板的 AFK 积分自动领取。

## 接口

```
POST https://app.lemonhost.me/api/client/account/afk/claim
```

LemonHost 用的是 Pterodactyl 客户端面板，所以这个路由走的是标准 Client API，
支持 **API Key (Bearer)** 认证，不需要走网页登录，也就完全绕开登录页的
Cloudflare Turnstile。

## 需要的 Secrets

| Secret | 必填 | 说明 |
|---|---|---|
| `LEMONHOST_API_KEY` | ✅ 推荐 | Pterodactyl 账号 API Key（`ptlc_...`）。面板 → Account → API Credentials 生成 |
| `LEMONHOST_SESSION_COOKIE` | 备选 | 浏览器里 `pterodactyl_session` 与 `XSRF-TOKEN` 拼成的 Cookie 头 |
| `LEMONHOST_SESSION_KEY` | 可选 | 预留，用于加密保存 cookie |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 | 本轮结束 / 认证失败时通知 |
| `GH_PAT` | 可选 | 用于每轮结束后自动派发下一轮 |

两个认证方式二选一，`LEMONHOST_API_KEY` 优先。

## 文件

- `earn_lemonhost.py` — 主脚本
- `earn_lemonhost.yml` — 实际使用的工作流（在 `main` 分支上）
- `lemo_state.json` — 运行状态，由 CI 自动写回本分支

## 手动试跑

工作流支持两个输入：

- `run_minutes`：本轮跑多久（默认 340）
- `dry_run`：`true` 只探测接口、不实际领取，用来验证 token 是否有效

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 本轮正常结束 |
| 1 | 认证/配置问题，需要人工换 token |
| 2 | 接口不可达，稍后重试 |
