# BoxMineWorld AFK Earn

干净独立的 `boxmineworld` 分支，自动登录并保持 BoxMineWorld 官方 AFK Earn 连接。

- 使用 Discord OAuth 登录 `afk.boxmineworld.com`
- 连接官方 WebSocket：`wss://afkapi.boxmineworld.com/earn`
- 按官方协议发送订阅、Ping，并回应 `activity_check`
- 登录 Cookie 使用 Fernet 加密后保存在状态文件
- 每轮运行约 340 分钟，成功结束后自动启动下一轮
- 每 6 小时有一次兜底触发
- Discord Token 仅保存在 GitHub Secret
