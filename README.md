# Hostrix — 免费服务器自动续期

`https://hostrix.website` · 服务器 `cd86ccde4e34`（`nezha.hostrix.website`，Node.js，1000MB/50%CPU/1GB）

## 面板机制（实测）

| 项 | 值 |
|---|---|
| 续期接口 | `POST /servers/cd86ccde4e34/renew` |
| 表单字段 | `csrf_token`（取自页面隐藏 input，长期不变） |
| 单次收益 | **+24h** |
| 冷却 | **11 小时**（两次之间） |
| 存量 | 46h（`⏰ 46h left`） |
| 归零后果 | `Out of time = paused`（不是删除） |
| 其他免费途径 | 无。wallet 要充值，gifts 要兑换码，无挂机赚时长 |

冷却期点击的实测结果：`POST → 302 → /servers/<id>`，时长不变，**不是报错，就是静默忽略**。

## 认证

Flask 签名会话，cookie 名 `session`，值形如 `.eJy…`（zlib 压缩 + Iron 签名）。

解出来的内容：

```json
{"_permanent": true, "uid": 43, "sid": "sBLIg1ctEExiEYpKS3vR-ZHo25vkZTjz",
 "csrf": "GotEm3…", "ipseen": "89.217.27.233",
 "accounts": [{"sid": "sBLIg1ct…", "uid": 43}]}
```

- 服务端**每次响应都 Set-Cookie 回写**，`Expires` 30 天 → 只要定期请求就不会过期
- 每次滚动出的 cookie 值都不同，但 `uid` / `sid` 不变
- 登录接口需要密码，**无需 Turnstile**（与 Octagon 不同）

## 运行

```bash
HOSTRIX_COOKIE='.eJy…' python3 renew_hostrix.py
```

退出码：`0` 正常（含冷却未到）、`2` 缺 cookie、`3` 会话失效、`4` 复查失败、`5` 点击后时长未变。

## CI

`.github/workflows/renew_hostrix.yml` — 每 30 分钟（`13,43 * * * *`），冷却中只检查不动，到点自动点击；跑完写回 cookie、提交 `hostrix_state.json`。

secret：`HOSTRIX_COOKIE`、`GH_PAT`。

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
