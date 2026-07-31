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
