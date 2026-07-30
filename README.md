# BoxMineWorld AFK Earn

干净独立的 `boxmineworld` 分支，自动登录并保持 BoxMineWorld 官方 AFK Earn 连接。

- 使用 Discord OAuth 登录 `afk.boxmineworld.com`
- 连接官方 WebSocket：`wss://afkapi.boxmineworld.com/earn`
- 按官方协议发送订阅、Ping，并回应 `activity_check`
- 每天最多获取 8 个积分
- 收到 `8/8` 或 `cooldown=true` 后立即关闭连接并结束 Workflow
- 冷却期内不会重新连接，等待每日额度重置
- Workflow 每 6 小时检查一次，重置后再开始赚取
- 登录 Cookie 使用 Fernet 加密保存
- Discord Token 仅保存在 GitHub Secret
