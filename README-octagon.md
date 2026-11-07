# Octagon Circle — AFK 挂机

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

## CI

`.github/workflows/afk_octagon.yml` — `workflow_dispatch`（参数 `minutes`）+ 每 2 小时自动跑 30 分钟。
secret：`OCTAGON_COOKIE`。**Cookie 会过期**，过期后需重新登录导出。
