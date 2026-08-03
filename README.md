# Witchly AFK

自动挂机赚 Witchly Coins。

## 原理

- 用 Discord token 走 OAuth 登录 witchly.host
- 访问 /earn/afk 页面
- 每 120 秒 1 金币, 每 5 分钟自动同步
- GitHub Actions 每 5 分钟跑一次

## Secret

| Secret | 说明 |
|--------|------|
| `WITCHLY_DISCORD_TOKEN` | Discord 用户 token (MTM... 格式) |

## 获取 Discord token

1. 浏览器登录 Discord
2. F12 → Network → 找任意请求的 Authorization header
3. 复制 `MTM...` 开头的 token
