# octagon — Octagon Circle 免费服自动续期

目标：`client.octagoncircle.org` 上的免费 MC 服 **vip01**（id 4，4G/4G/200%，面板 `panel.octagoncircle.org`）永不掉线。

> 2026-11-13：官网取消 AFK 挂机（`afk_enabled=false`），本分支已从 AFK 心跳改造为**付费续期**。

## 一、面板机制（全部实测，2026-11-13）

| 项 | 值 / 规则 |
|---|---|
| 设置 | `GET /api/system/settings`（公开）→ `server_renew_cost=120` / `server_renew_days=3` / `server_renew_enabled=true` |
| 会话 | `GET /api/user/session` → `user_info.credits`（余额）；cookie `user_token` **30 分钟滑动过期**，响应 Set-Cookie 回写延续 |
| 清单 | `GET /api/user/session/servers` |
| 详情 | `GET /api/user/server/<id>` → `server.mythicaldash.expires_at`（UTC "YYYY-MM-DD HH:MM:SS"） |
| 续期 | `POST /api/user/server/<id>/renew` body `{"cost":120,"days":3}` |
| 效果 | **expires_at = now + 3 天**（SET 语义，不叠加）；**到期自动挂起、续期自动解挂** |
| 扣费 | 服务端按 settings **原价 120** 扣币（前端"过期 ×1.5=180"只是展示价，实测只扣 120） |
| 登录 | 站内账密要 **Turnstile**；**Discord OAuth** 免验证码：`GET /api/user/auth/callback/discord/login` → 302 `discord.com/api/oauth2/authorize?client_id=1568690847879405668&scope=identify guilds email guilds.join` → 授权拿 code → 回调 Set-Cookie `user_token` |
| AFK | 已下线（`afk_enabled=false`），旧 `/api/user/earn/afk/work` 通道废弃 |

## 二、续期策略

- **剩余 < 24 小时才续**（`OCTAGON_RENEW_HOURS` 可调）——SET 语义下越晚续越省（120 币实打实买 3 天）
- 经济账：约 **120 币 / 2~3 天** ≈ 1200~1800 币/月；每次运行日志/Summary 都报余额与可续次数
- 余额不足只警告不动手（Summary 标 ⚠️），等充值/收币
- cron 每天两次（05:00/17:00 UTC）+ 手动 dispatch；cookie 失效自动 Discord 重登

## 三、使用

```bash
export OCTAGON_DISCORD_TOKEN='...'      # Discord 用户 token（重登用）
export OCTAGON_COOKIE='user_token=...'  # 可选：滑动缓存
export OCTAGON_RENEW_HOURS=24           # 可选
node renew_octagon.js
```

Secrets：`OCTAGON_COOKIE`（滑动缓存，workflow 自动回写）、`OCTAGON_DISCORD_TOKEN`、`GH_PAT`（写回用）。
