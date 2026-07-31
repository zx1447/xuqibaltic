# xuqibaltic (siam branch)

Siam-Node Cloud 自动签到 - 纯 requests, 复用 session, Discord token 登录。

## 功能

- 用 Discord token 走 OAuth 登录 Siam-Node Cloud
- **复用 session cookie**（存到 `siam_state.json`，不每次都登录）
- session 失效时自动重新登录
- 每次签到最多 6 次，遇到 cooldown/limit 停止
- GitHub Actions 每 2 小时自动跑

## 签到规则

- 每天 3 次，每次 +2 泰铢
- 每小时间隔
- 每天首次 +35 泰铢
- 最多约 +41 泰铢/天

## 文件

- `renew_siam.py` - 主脚本（纯 requests，不用浏览器）
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
4. 调 POST /api/checkin.php 签到 (最多 6 次)
5. 保存新 cookie 到 siam_state.json
```
