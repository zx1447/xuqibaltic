
## 时间线（北京时间）

| 时刻 | 事件 |
|---|---|
| 11-07 14:52 | 首次 GET，存量 46h，进入 11h 冷却 |
| 11-07 15:02 | 部署完成，run `42349557845` / `42349742811` 验证通过（冷却中，未点） |
| **11-08 01:52** | 冷却到期 |
| **11-08 02:13** | cron 首次真正点击 → 存量回满 48h |
| 之后 | 每 11h 允许一次，cron 每 30 分钟探测，到期即点，稳定维持在 48h 上限 |

存量归零只会让服务器 `paused`（暂停），不会删除，且 46h 缓冲 + 11h 冷却留了充足余量。

## 与仓库其他分支的关系

结构同 `octagon`：
- **workflow 放 `main`** —— GitHub 的 `schedule` 和 `workflow_dispatch` 只认默认分支
- **脚本放 `hostrix` 分支** —— workflow 里 `checkout` 时 `ref: hostrix`
- secret `HOSTRIX_COOKIE` 由 workflow 自己 `gh secret set` 回写，cookie 永久自动滚动
