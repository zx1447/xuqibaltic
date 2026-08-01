# BOT SERVER 正常冷却每日签到

干净独立分支：`botserver`

## 功能

- 使用 Discord OAuth 登录 `https://bot-server.site`
- 登录 Cookie 使用 Fernet 加密后保存
- 每 24 小时运行一次（北京时间 13:00），读取 `/dashboard/daily`
- 对 Cloudflare 临时错误（523/502/503 等）与网络抖动自动重试
- 严格解析网站页面返回的下一次可签到时间
- 冷却未结束时不发送签到请求
- 冷却结束后只正常签到一次
- 签到后通过余额或累计次数变化确认结果

## Secrets

- `BOTSERVER_DISCORD_TOKEN`
- `BOTSERVER_SESSION_KEY`

此分支不会绕过网站的 24 小时签到冷却。
