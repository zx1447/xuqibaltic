# CyraHost Earn Heartbeat

干净独立的 `cyrahost` 分支，用 CyraHost Client API Key 持续调用：

```text
POST https://panel.cyrahost.xyz/api/client/store/earn
```

- 官方前端间隔为 61 秒，本脚本使用 62 秒，避免触发频率限制。
- 每轮运行约 340 分钟，自然结束后由 GitHub Actions 自动启动下一轮。
- 每 6 小时另有一次兜底触发。
- API Key 仅保存在 GitHub Secret `CYRAHOST_API_KEY`，不会写入代码或状态文件。
- GitHub 数据中心出口会被面板拒绝，因此通过 Secret `CYRAHOST_PROXY_VLESS_URI` 启动本地 Xray SOCKS5 代理。
- `cyrahost_state.json` 记录成功次数和最近状态。
- 只有本轮成功时才会自动启动下一轮，避免失败后形成重复任务。
