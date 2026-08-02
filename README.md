# Flarelax Server Monitor & Auto Renew

- **服务器开机检测**：每 6 小时检查一次服务器 `8a4d1879` 状态，未运行（offline/stopped）时自动发送开机指令启动。
- **服务器自动续期**：每满 48 小时自动调用 `/api/server/8a4d1879/renew` 延长有效期。
- **已停止获取积分**：不再请求 `/api/afk/claim` 积分接口，任务约 15 秒极速完成。
