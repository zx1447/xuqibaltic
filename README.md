# CoredLab 4-Hour Visit

`coredlab` 分支每 4 小时访问一次 CoredLab 控制台：

- 访问 `/dashboard`
- 访问 Minecraft 控制台页面
- 优先复用加密登录 Cookie
- 会话失效时使用 Discord OAuth 重新登录
- GitHub Actions 定时表达式：`0 */4 * * *`
