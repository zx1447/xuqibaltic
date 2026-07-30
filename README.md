# CyraHost Earn + 7-Day Renew

干净独立的 `cyrahost` 分支，直接使用 CyraHost Client API Key：

```text
POST https://panel.cyrahost.xyz/api/client/store/earn
POST https://panel.cyrahost.xyz/api/client/servers/1c8f44f1-a8b2-4abb-b5ad-3950ab451b30/renew
```

- 每轮积分任务运行 340 分钟，轮次内每 61 秒请求一次积分接口。
- 一轮结束后等待 4 天，再开始下一轮 340 分钟积分任务。
- Workflow 每 6 小时检查一次是否已经结束四天等待期。
- 服务器续期独立按每 7 天检查一次，不受四天积分等待期影响。
- API Key 仅保存在 GitHub Secret `CYRAHOST_API_KEY`，不会写入代码或状态文件。
- 不使用 VLESS、WARP 或其他代理节点，仅用 `requests` 直接请求。
- `cyrahost_state.json` 记录积分、下一轮积分和续期时间。
