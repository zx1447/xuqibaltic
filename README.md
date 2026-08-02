# Active Server Monitor & Auto Renew

- **Phenix Pterodactyl 服务器检测**：每过 1 天自动检测 `ptly1.hosting-phenix.com/server/c0fbebc6` 是否在运行，如果为 `offline` / `stopped` 则通过 API 自动启动。
- **Active 面板账号**：每 2 天登录网页面板，随机访问文件页面。
- **Active API 账号**：每 2 天调用 `/api/client` 检查会话。
