# CyraHost Earn + 7-Day Renew

干净独立的 `cyrahost` 分支，直接使用 CyraHost Client API Key：

```text
POST https://panel.cyrahost.xyz/api/client/store/earn
POST https://panel.cyrahost.xyz/api/client/servers/1c8f44f1-a8b2-4abb-b5ad-3950ab451b30/renew
```

- 积分接口每 61 秒调用一次（面板前端也是约 61 秒）。
- 服务器续期接口每满 7 天调用一次，成功后重新计算下一个 7 天周期。
- 每轮运行约 340 分钟，自然结束后自动启动下一轮。
- 每 6 小时另有一次兜底触发。
- API Key 仅保存在 GitHub Secret `CYRAHOST_API_KEY`，不会写入代码或状态文件。
- 不使用 VLESS、WARP 或其他代理节点。
- `cyrahost_state.json` 记录积分、续期和下一次续期时间。
- 只有本轮成功时才会启动下一轮，避免失败后形成重复任务。
