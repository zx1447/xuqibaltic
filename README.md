# BlackFlame Studio 自动签到（看广告领金币）

对 `dashbord.blackflamestudio.us.ci` 的无人值守签到：每天自动"看广告"领金币（coins）。
金币是该站的积分/资源币 —— 开服按 **Coins/Hour** 烧币，余额不足服务器会被暂停。

## 机制（2026-11-11 逆向实测）

站点 = Next.js + NextAuth（Discord OAuth 登录）。"签到"实为**看广告领币**，
由三个 Server Action 组成（`Next-Action` 头直调，广告弹窗只是前端摆设）：

| 顺序 | Action | 说明 |
|---|---|---|
| 1 | `startAdWatch` | 返回 `{success, startTime, requiredSeconds: 30}` |
| 2 | 等 ≥30 秒 | 服务端只校验时间间隔 |
| 3 | `verifyAndRewardAdWatch` | **+5 coins/轮**，页面重渲染 |

- 无冷却、可连续多轮（实测 2 连）；金币累积（5 → 10 ✅）
- 会话（`__Secure-next-auth.session-token`）**约 6 小时短命** → 脚本每次运行都走
  "Discord Token → OAuth 自动批准 → 新会话"，不复用 Cookie
- 站点重建后 Action ID 会变 → 脚本带 `rediscover_ids()` 自动从页面 chunk 重挖

## 登录链（脚本全自动）

```
Discord Token
  → GET  /api/auth/csrf
  → POST /api/auth/signin/discord        （拿 discord.com/authorize URL）
  → POST discord.com/api/v9/oauth2/authorize   （Token 自动批准，换 code）
  → GET  /api/auth/callback/discord      （换站点会话 Cookie）
  → Server Action 签到
```

## 使用

本地：

```bash
export BFLAME_DISCORD_TOKEN='MTxx...'   # Discord 账号 token
export BFLAME_WATCHES=1                  # 可选，本轮次数（默认 1）
python3 checkin_blackflame.py
```

CI（main 分支 `checkin_blackflame.yml`）：

- cron `0 1 * * *`（北京 09:00）自动跑；`workflow_dispatch` 可手动指定轮数
- Secret：`BFLAME_DISCORD_TOKEN`（Discord token，敏感勿外传）
- 结果写进 Job Summary（金币前后对比）

## 注意

- **站点掐机房 IP**：TLS/网络层直连 Azure/GCP 等大机房 ASN 会被 RST（实测 GH runner 裸连失败），
  放行 Cloudflare（WARP 104.28.x）和小厂出口。CI 因此先装 `cloudflare-warp` 再签到
- Discord token 等同账号密码，只放 GitHub Secret，勿提交进文件
- token 在改密码/登出所有设备时会失效，到时更新 Secret 即可
- 适度刷币：默认每天 1 轮（+5），手动 dispatch 可加轮
