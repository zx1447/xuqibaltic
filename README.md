# CoalByte 自动续期

对 `coalbyte.com`（Pelican 免费游戏服前端壳）的无人值守续期：**每次 +5 天**。

## 机制（2026-11-12 逆向实测）

| 项 | 结论 |
|---|---|
| 续期接口 | `POST /api/pelican/renew`，body `{"serverId":"<identifier>","email":"<邮箱>"}` |
| 效果 | 到期时间设为 **now + 5 天**（不叠加，重复续=不断重置） |
| 认证 | **无**（裸 POST 即可；浏览器带的 Authorization 头后端根本不看） |
| 冷却 | **无**（实测 5 分钟连打两次都成功） |
| 查服务器 | `GET /api/pelican/servers?email=<邮箱>` → 名下全部服务器 + `renewal.expiresAt/canRenew/exempt` |
| 面板本体 | `panel.coalbyte.fun`（Pelican Panel），coalbyte.com 只是 Next.js 前端壳 |

成功响应：

```json
{"success":true,"exempt":false,"status":"active",
 "expiresAt":"2026-11-17T10:28:58.638Z","remainingSeconds":432000,
 "message":"Server successfully renewed for 5 days!"}
```

## 使用

本地：

```bash
export COALBYTE_EMAIL='you@example.com'   # 自动发现名下全部服务器
python3 renew_coalbyte.py
# 或直接指定：COALBYTE_SERVERS=6995c463,xxxxxxxx
```

CI（main 分支 `renew_coalbyte.yml`）：

- cron 每天一跑（到期恒定保持 ~5 天余量）；`workflow_dispatch` 可手动
- Secret：`COALBYTE_EMAIL`（邮箱，防公开仓泄露）
- 结果写进 Job Summary

## 备注

- 语义是"设为 +5 天"而非"累加 5 天"，所以每天续=永远保有 5 天最大余量
- 新开服务器会由邮箱发现机制自动纳入，无需改代码
