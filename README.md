# SliceNodes 全服务器持续监控

干净独立分支：`slicenodes`

## 功能

- 使用 SliceNodes Pterodactyl Client API
- 面板：`https://panel.slicenodes.in`
- 每轮 Workflow 持续运行约 350 分钟，而不是检查一次或一小时后结束
- Workflow 运行期间每 60 分钟重新获取账号拥有的全部服务器
- 发现服务器为 `offline/stopped` 时自动发送 `start` 电源信号
- 启动后持续确认服务器是否进入 `running`
- 已暂停、安装中、迁移中或维护中的服务器会安全跳过
- 当前轮结束后自动启动下一轮完整 Workflow
- 状态保存在 `slicenodes_state.json`

## Secrets

- `SLICENODES_API_KEY`：SliceNodes Client API Key
- `GH_PAT`：当前轮结束后自动 Dispatch 下一轮

## 默认时序

1. Workflow 启动后立即检查全部服务器。
2. 每隔一小时再次检查全部服务器。
3. 保持运行约 350 分钟，接近 GitHub Actions 单任务时限才结束。
4. 结束后自动排队下一轮 350 分钟 Workflow。
