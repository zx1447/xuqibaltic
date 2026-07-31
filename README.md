# xuqibaltic (siam branch)

Siam-Node Cloud 自动签到 - Playwright 浏览器、复用 session、Discord token 登录。

## 功能

- 用 Discord token 走 OAuth 登录 Siam-Node Cloud
- **复用 session cookie**（存到 `siam_state.json`，不每次都登录）
- session 失效时自动重新登录
- 每次 workflow 签到 6 次，两次之间等待约 1 小时
- 单轮总运行时间约 5 小时，遇到每日额度用完会提前停止
- GitHub Actions 每天运行一次（北京时间 08:00）

## 签到规则

- 每天最多签到 6 次
- 两次签到需要约 1 小时间隔
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
4. 调 POST /api/checkin.php 签到 6 次，每次间隔约 1 小时
5. 等待期间每 5 分钟访问站内页面维持 session
6. 保存新 cookie 和签到进度到 siam_state.json
```
