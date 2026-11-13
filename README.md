# acorehost — AcoreHosting 免费服计划自动续期

目标：`dash.acorehosting.com`（Pterodactyl 定制壳 + `dev.sqidgeon.serverplans` 扩展）上的
免费 MC 服 **nice**（uuid `23a11fd8-5b0f-458f-8a04-f0d422b2d07e`，9G/20G/400%，节点 France Roubaix 1，
`acorezero.qzz.io:20236`）永不掉线。

## 一、面板机制（全部实测，2026-11-13）

| 项 | 值 / 规则 |
|---|---|
| 鉴权 | `Authorization: Bearer <c7sp_ 客户端 API key>` |
| 清单 | `GET /api/client/servers` → `{servers:{data:[{uuid,name,is_suspended,status,...}]}}` |
| **续期** | `POST /api/client/extensions/dev.sqidgeon.serverplans/renew` `{"server":"<uuid>"}` —— **仅剩 <8 小时**可打（否则 422 `Renewal is available when less than 8 hours remain`）|
| **恢复** | `POST /api/client/extensions/dev.sqidgeon.serverplans/restore` `{"server":"<uuid>"}` —— 从**计划挂起**拉回（否则 `Server is not in plan suspension`）|
| UUID 要求 | body 的 `server` 必须是**完整 UUID**（短 uuid 报 `UUID parsing failed`）|
| 计划周期 | 计划到期 → 服务器进入 plan suspension（挂起停机）；renew 窗口 = 到期前 8h |

## 二、守护策略

每 30 分钟一轮（长驻循环 330min/次 + GH_PAT 自派发接力，防 GitHub schedule 调度饿死——bkt 同款装甲）：

1. **renew 优先**：窗口一开直接续命（200 = 🚀 已续期）
2. **restore 兜底**：万一错过窗口被挂起 → 拉回（200 = 🆘 已恢复）
3. 两个报错都是良性（"没到 8 小时窗口"+"没被挂起"）= ✅ 健康等待

## 三、使用

```bash
export ACORE_API_KEY='c7sp_...'
python3 renew_acorehost.py            # 单轮
python3 renew_acorehost.py --loop 330 # 长驻（每 30 分钟一轮）
```

CI（main 分支 `renew_acorehost.yml`）：cron `26 */6` 兜底 + 接力自滚动。
Secrets：`ACORE_API_KEY`、`GH_PAT`（接力派发用）。
