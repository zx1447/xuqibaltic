# Open Hosting AFK Keepalive

Discord OAuth 登录 Open Hosting，挂住 `wss://dash.openhosting.site/ws` AFK 心跳连接保活。
GitHub Actions 每轮挂 ~340 分钟后自然结束并自动触发下一轮，实现 7x24 持续运行。
