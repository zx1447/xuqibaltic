# Discord 面板自动续期（多账号）

一个脚本管两个 Discord 面板续期。两者机制**完全不同**，共用点击 + 网关判定。

| | McServers | Antagonix |
|---|---|---|
| 有效期 | 7 天 | **5 天** |
| 有效期内点击 | ❌ 回 `You can renew again later.` | ✅ **直接续，从现在起重置 +5 天** |
| 续期节奏 | 每 5 天探一次 | 剩余 ≤ 4.5 天才点 |
| 面板消息 | `1567495211016519802` | `1567073789395730475` |
| 频道 | `1567486173495173181` | `1567073787143524383` |
| bot / application_id | `1524370665631649865` | `1565149630260322418` |
| 按钮 custom_id | `renew_server` | `b\|fa67c4d1a69f9e08bc484195\|renew` |
| 成功文案 | （无回执） | `Hébergement renouvelé pour 5 jours...` |

## 通用请求格式

```http
POST https://discord.com/api/v9/interactions
Authorization: <该账号的用户 token>

{
  "type": 3,
  "nonce": "<19 位数字>",
  "guild_id": "...",
  "channel_id": "...",
  "message_flags": 0,
  "message_id": "...",
  "application_id": "<bot 的用户 ID>",
  "session_id": "<32 位十六进制>",
  "data": { "component_type": 2, "custom_id": "..." }
}
```

成功返回 **HTTP 204**。

### 三个必踩的坑

| 报错 | 原因 |
|---|---|
| `{"code":10005,"message":"未知集成"}` | `type` 写成 `2`（应用命令），组件交互必须 `3` |
| `{"code":50035}` 说 `application_id` 必填 | 消息 JSON 里 `application_id` 是 `null`，必须手填 |
| `{"code":10002,"Application inconnue"}` | 用了 guild id；应该用 **bot 的用户 ID** |

## 怎么确认真续上了

bot 的回复是 **ephemeral（私密）**，普通消息接口读不到：

```
GET /channels/{c}/messages/{m}  →  403 {"code":20002,"detail":"只有机器人可使用此端点"}
```

所以脚本连 **Discord 网关**监听推送：

```
【MESSAGE_CREATE】flags=192 ephemeral=true
【MESSAGE_UPDATE】"Hébergement renouvelé pour 5 jours à partir de maintenant."
INTERACTION_SUCCESS
```

判定时**只挑短句回执**（≤120 字）来匹配，避免面板 embed 那种大段文字误命中。

三种结果：

| verdict | 触发 | 后续 |
|---|---|---|
| `renewed` | 匹配成功文案 | 记录 `last_click_at`，下次按间隔判断 |
| `too-early` | `You can renew again later.` | 不记点击时间，等下一个周期 |
| `no-reply` | 204 + INTERACTION_SUCCESS 但无文案 | 按成功计 |

## 调度

```yaml
- cron: "37 6 */2 * *"     # 每 2 天一次
```

cron 只负责"定期唤醒"，真正节奏由脚本判定：

- **McServers**：距上次成功点击满 **5 天** 才点
- **Antagonix**：读面板 embed 里的 `<t:...>` 到期时间戳，剩余 **≤ 4.5 天** 才点

每 2 天唤醒一次，意味着窗口最多耽误 2 天，对 5 天 / 7 天有效期都安全。

## 手动触发

Actions → **Discord Panel Renew** → Run workflow

| 输入 | 说明 |
|---|---|
| `target` | `mcservers` / `antagonix` / `all`（默认 all） |
| `force` | `true` = 无视间隔立即点 |
| `dry_run` | `true` = 只看判定不点击 |

## 本地跑

```bash
npm install

export DISCORDERENEW_TOKEN='...'   # McServers 账号
export ANTAGONIX_TOKEN='...'       # Antagonix 账号

node renew_mcservers.js                    # 两个都处理
DR_TARGET=antagonix node renew_mcservers.js
DR_DRY_RUN=1 node renew_mcservers.js
DR_FORCE=1 node renew_mcservers.js
```

## 当前状态

| 服务 | 到期 | Secret |
|---|---|---|
| McServers | 2026-11-11 12:01 UTC | `DISCORDERENEW_TOKEN` |
| Antagonix | 2026-11-09 12:26 UTC | `ANTAGONIX_TOKEN` |

## 需要维护的地方

| 情况 | 怎么办 |
|---|---|
| token 失效（401） | 换对应 secret |
| 面板消息被删/重建 | Antagonix 会自动探测新消息 id；McServers 需改 `DR_*` 或代码里的 message |
| 机器人改了按钮 id | 改 `TARGETS` 里对应项的 `customId` |

> ⚠️ Discord 用户 token 会因改密码 / 退出所有设备 / **跨地区使用**被吊销。
> 之前 Antagonix 的 token 就是拿到 GitHub 美国 IP 用之后被杀的（活不过 2 分钟）。
> 如果日志报 `401`，需要重新抓 token。
