# McServers 免费 MC 服自动续期

每 **5 天**点一次 Discord 面板上的 **Renew Server** 按钮。

## 面板信息

| 项目 | 值 |
|---|---|
| 面板机器人 | `McServers \| Free Hosting`（bot `1524370665631649865`） |
| 服务器 | guild `1559978655688564848` |
| 频道 | `1567486173495173181`（✣🧿・〢free-hosting） |
| 面板消息 | `1567495211016519802` |
| 续期按钮 | `Renew Server`，style 2，`custom_id = renew_server` |
| 面板后台 | https://dash.mcservers.in |
| Server ID | `72` |
| Identifier | `697de3d4` |
| 现在到期 | **2026-11-11 12:01:04 GMT** |

## 请求格式

```http
POST https://discord.com/api/v9/interactions
Authorization: <用户 token>

{
  "type": 3,
  "nonce": "<19 位数字>",
  "guild_id": "1559978655688564848",
  "channel_id": "1567486173495173181",
  "message_flags": 0,
  "message_id": "1567495211016519802",
  "application_id": "1524370665631649865",
  "session_id": "<32 位十六进制>",
  "data": { "component_type": 2, "custom_id": "renew_server" }
}
```

成功返回 **HTTP 204**。

### 两个必踩的坑

| 错误 | 原因 |
|---|---|
| `{"code":10005,"message":"未知集成"}` | `type` 写成 `2`（应用命令），组件交互必须 `3` |
| `{"code":50035}` 说 `application_id` 必填 | 漏了字段；消息 JSON 里 `application_id` 是 `null` |
| `{"code":10002,"Application inconnue"}` | 用了 guild id；应该用 **bot 的用户 ID** |

## 怎么知道续期成功了

bot 的回复是 **ephemeral（私密）**，普通消息接口读不到：

```
GET /channels/{c}/messages/{m}  → 403 {"code":20002,"detail":"只有机器人可使用此端点"}
```

所以脚本**连 Discord 网关**，监听 bot 推给本人的私密消息：

```
【MESSAGE_CREATE】flags=192 ephemeral=true    ← 先发空消息占位
【MESSAGE_UPDATE】"You can renew again later."
INTERACTION_SUCCESS
```

三种结果：

| 回复文本 | 含义 | 脚本判定 |
|---|---|---|
| `You can renew again later.` | 还在有效期内，没到可续期时间 | `too-early`，跳过 |
| 含 success / renewed / extended | 续期成功 | `renewed`，记录点击时间 |
| 其它 | 未知 | `unknown`，记录原文 |

## 调度

```yaml
- cron: "23 5 */4 * *"
```

cron 的日字段 `*/4` 从 1 号起算 → 每月落在 **1/5/9/13/17/21/25/29 号**（约 4 天一次），
脚本内部再按 **距上次成功点击是否满 5 天** 判定一次，不满就跳过。

**净效果 = 每 5 天点一次**，同时不会因为 cron 抖动而重复点。

## 手动触发

Actions → **McServers Renew** → Run workflow

| 输入 | 说明 |
|---|---|
| `force` | `true` = 无视 5 天间隔立即点 |
| `dry_run` | `true` = 只看判定，不点击 |

## 本地跑

```bash
npm install
DISCORDERENEW_TOKEN='...' node renew_mcservers.js
DISCORDERENEW_TOKEN='...' DR_DRY_RUN=1 node renew_mcservers.js
DISCORDERENEW_TOKEN='...' DR_FORCE=1 node renew_mcservers.js
```

## 需要维护的地方

| 情况 | 怎么办 |
|---|---|
| token 失效（401） | 换 secret `DISCORDERENEW_TOKEN` |
| 面板消息被删/重建 | 改 `DR_MESSAGE_ID`（脚本也可以改成自动搜最新带 `renew_server` 的消息） |
| 换了服务器 | 改 `DR_*` 环境变量 |
| 机器人改了按钮 id | 改 `DR_CUSTOM_ID` |

> ⚠️ Discord 用户 token 会因改密码 / 退出所有设备 / **跨地区使用**被吊销。
> 实测把同一个 token 拿到美国 IP 用可能触发风控，届时日志会报 `401`。
