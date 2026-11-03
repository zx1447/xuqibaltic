# Siam 自动签到 —— 已修好（2026-11-03）

## 结论

**根因不是 Discord token，是 Cloudflare。** `my.siam-node.cloud` 对机房 IP 一律下发
JS 挑战页（`cf-mitigated: challenge`），所以过去 107 次全绿的运行其实一次都没签到成功。

**解决办法来自本仓库 `domainvip` 分支**：用 `wgcf` + `wireproxy` 起一个 Cloudflare WARP
SOCKS5 代理，Playwright 走它出网。实测同一台 runner：

| 出口 | 出口 IP | Siam 响应 |
|---|---|---|
| 直连（Azure） | 172.214.110.66 | `403` + `cf-mitigated: challenge` ❌ |
| **WARP** | `2a09:bac5:…`（`warp=on`） | `302`，无挑战 ✅ |

## 真实成功记录（run #113，2026-11-03 10:38）

```
[siam] ⚠️ Session 已失效（接口提示: กรุณาเข้าสู่ระบบก่อน），改走 Discord OAuth
[siam] ✅ Discord code 获取成功
[siam] ✅ OAuth callback: Cloudflare 挑战通过
[siam] ✅ 登录成功
[siam] ✅ 本次 +35 ฿ | 今日累计 +35 ฿ | 总积分 1506 ฿ | 剩余次数 5
[siam] ✅ 本次 +2 ฿  | 今日累计 +37 ฿ | 总积分 1508 ฿ | 剩余次数 4
[siam] ✅ 本次 +2 ฿  | 今日累计 +39 ฿ | 总积分 1510 ฿ | 剩余次数 3
[siam] ✅ 本次 +2 ฿  | 今日累计 +41 ฿ | 总积分 1512 ฿ | 剩余次数 2
[siam] ✅ 本次 +2 ฿  | 今日累计 +43 ฿ | 总积分 1514 ฿ | 剩余次数 1
[siam] ✅ 本次 +2 ฿  | 今日累计 +45 ฿ | 总积分 1516 ฿ | 剩余次数 0
[siam] 📊 今日汇总: 成功 6 次, 共 +45 ฿, 当前总积分 1516 ฿
```

每天 6 次：第一次是首签奖励（+35 ฿），之后每次 +2 ฿，共 **+45 ฿/天**。

## 修掉的问题清单

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | 机房 IP 撞 Cloudflare 挑战 | 接口回 HTML，`r.json()` 抛错，107 次全 0 | WARP SOCKS5 出口 |
| 2 | `Session 有效` 只看 URL 里有没有 `login` | 挑战页 URL 没 login → 谎报成功 | 必须接口回正常 JSON |
| 3 | 接口回 `กรุณาเข้าสู่ระบบก่อน`（请先登录）也当 JSON 成功 | 跳过 OAuth，6 次全失败 | 识别未登录文案，强制走 OAuth |
| 4 | 挑战页里跨域 `fetch` discord.com | `TypeError: Failed to fetch` | 改用 `context.request.post` |
| 5 | 失败就 `break`，0 次仍 `exit 0` | 绿灯假象 | `continue` + 0 次 `exit 1` |
| 6 | 每次运行都 `wgcf register` | 撞 Cloudflare `429 Too Many Requests` | 复用 secret `SIAM_WARP_PROFILE` |
| 7 | `process.exit()` 跳过 `finally` | `cf_clearance` 等 cookie 存不下来 | 退出前显式持久化（已存 20 个） |
| 8 | 额度用完被当失败 | 每天重跑都红 | 识别泰文"今日已满"→ 正常退出 |
| 9 | 钱包页抓 `฿` 抓到 35（其实是首签奖励文案） | 总积分显示错误 | 只用接口返回的 `balance`，缺失时回退上次记录 |

## 现在的状态

- 每天北京时间 08:00 自动跑（`cron: 0 0 * * *`）
- 成功则绿，输出每次签到后的总积分
- 幂等：当天已签满时正常退出（绿色），不会误报失败
- session（含 `cf_clearance`）保存到 `siam_state.json`，第二天优先复用，只有失效才重新 OAuth
- 手动触发：`Actions → Siam Auto Check-in → Run workflow`

## 相关 secret

| 名字 | 用途 |
|---|---|
| `SIAM_DISCORD_TOKEN` | Discord OAuth |
| `SIAM_WARP_PROFILE` | 已注册的 WARP 配置（避免 `wgcf` 429） |
| `SIAM_CF_CLEARANCE` | 备用，一般不需要 |
