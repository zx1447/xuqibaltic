# DragonHost Last-Day Renew

干净独立的 `dragonhost` 分支，自动检查并续期 DragonHost 游戏服务器 `328`。

- 登录接口：`POST /api/login`
- 服务器信息：`GET /api/game/328`
- 续期接口：`POST /api/game/328/renew`
- 免费续期参数：`{"method":"balance","days":5}`
- DragonHost 只允许在有效期最后一天续期，因此每 6 小时检查一次。
- 剩余时间超过 24 小时时只检查、不提交续期。
- 登录账号和密码只保存在 GitHub Secrets。
- 成功后重新读取服务器信息，确认到期时间确实延长。
