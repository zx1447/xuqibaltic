# xuqibaltic (siam branch)

Siam-Node Cloud 自动签到 - Playwright 浏览器、复用 session、Discord token 登录。

## 功能

- 用 Discord token 走 OAuth 登录 Siam-Node Cloud
- **复用 session cookie**（存到 `siam_state.json`，不每次都登录）
- session 失效时自动重新登录
- 每次 workflow 签到 6 次，每次间隔 14 秒
- 6 次完成后立即结束 workflow，遇到每日额度用完会提前停止
- 下一天北京时间 08:00 再启动新一轮

## 签到规则

- 每天最多签到 6 次
- 6 次在同一个 workflow 中完成，每次相隔 14 秒
- 每次奖励金额以接口实际返回为准

## 文件

- `renew_siam.js` - 主脚本（Playwright + Discord OAuth）
- `.github/workflows/renew_siam.yml` - GitHub Actions workflow
- `siam_state.json` - session cookie + 状态（自动更新）

## Secrets

| Secret | 说明 |
|--------|------|
| `SIAM_DISCORD_TOKEN` | Discord user token |
| `TG_BOT_TOKEN` | (可选) Telegram bot token |
| `TG_CHAT_ID` | (可选) Telegram chat id |

## 流程

```
1. 从 siam_state.json 读 PHPSESSID cookie
2. 用 cookie 访问 profile 页面验证 session 是否有效
3. 如果失效, 用 Discord token 走 OAuth 重新登录
4. 每隔 14 秒调用一次 POST /api/checkin.php，共 6 次
5. 保存新 cookie 和签到进度到 siam_state.json
6. Workflow 结束，等待到下一天定时任务再启动
```

## Cloudflare 绕过（关键）

`my.siam-node.cloud` 对机房 IP 一律下发 Cloudflare JS 挑战（`cf-mitigated: challenge`），
表现为接口返回 `<!DOCTYPE html> Just a moment...`，旧版脚本因此 107 次运行全部 0 签到。

现在的做法（移植自本仓库 `domainvip` 分支）：workflow 里先用 `wgcf` + `wireproxy`
拉起一个 **Cloudflare WARP SOCKS5 代理（127.0.0.1:40000）**，Playwright 通过它出网。

实测同一台 runner：

| 出口 | Siam 响应 |
|---|---|
| 直连（Azure IP） | `403` + `cf-mitigated: challenge` |
| WARP（`warp=on`） | `302`，无挑战 ✅ |

## 输出样例

```
[siam] ✅ 本次 +35 ฿ | 今日累计 +35 ฿ | 总积分 1506 ฿ | 剩余次数 5
[siam] ✅ 本次 +2 ฿ | 今日累计 +37 ฿ | 总积分 1508 ฿ | 剩余次数 4
...
[siam] 📊 今日汇总: 成功 6 次, 共 +45 ฿, 当前总积分 1516 ฿
```
