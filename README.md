# Q-Fi Cloud 免费服务器续期（每 2 天）

`https://gp.qficloud.com` · 服务器 UUID `2909e505-6516-4d30-86ca-ae75af6aa91b`

## 端点

```
POST /api/client/dezerx/server-create/<UUID>/renew
Authorization: Bearer ptlc_xxxxxxxx     ← Pterodactyl Client API key
→ 200 OK       续期成功
→ 401          Unauthenticated（key 无效/过期）
```

`dezerx` 是套在 Pterodactyl 上的第三方主题（`server-create` 是它自己的路由前缀），
所以续期端点在 `/api/client/dezerx/...` 而不是标准的 `/api/client/servers/...`。

## 认证怎么拿

**必须用 Client API key**，网页登录的 cookie 不顶用（Pterodactyl 的 API 中间件只认 `Authorization: Bearer`，
实测空 cookie / 空 Bearer 一律 401 `AuthenticationException`）。

获取步骤：

1. 浏览器登录 `https://gp.qficloud.com`（登录页带 Google reCAPTCHA，这步只能真人做）
2. 左侧菜单 → **API credentials**（地址 `https://gp.qficloud.com/account/api`）
3. Description 随便填（如 `renew-bot`），权限至少勾上：
   - `renew` / `server.create`（Dezerx 的续期权限，名字可能略有差异，**全勾最省事**）
4. 生成后复制 `ptlc_` 开头那串，**只显示一次**
5. 填进仓库 secret `QI_API_KEY`

## 实测状态（2026-11-06）

| 探测 | 结果 |
|---|---|
| `POST .../renew`（无 key） | `401 AuthenticationException` |
| `POST .../renew`（假 UUID） | `401`（先过认证再校验资源，说明路由存在） |
| `GET /api/client/dezerx/server-create` | `401`（路由存在，不是 404） |
| `GET /api/client/dezerx/renewables` | `404`（没有这个官方端点） |
| `GET /api/client/account`（假 ptlc_ key） | `401` |
| 登录页 | `Sign in · Q-Fi Cloud`，Google **reCAPTCHA enabled**，有 Discord 链接 |

结论：**认证是唯一门槛**，拿到 API key 之后 `POST renew` 就是纯 HTTP，不需要浏览器、不需要验证码。
站点也没有像 MineHost/Severoo 那种「看广告 / 过人机验证」的环节，属于最好自动化的一类。

## 节奏

`QI_INTERVAL_DAYS=2`：距上次 **成功**（200）续期满 2 天才提交下一次。

cron 用**每天唤醒**（`41 7 * * *`），真正判定在脚本里：

- 满 2 天 → 提交 `POST renew`
- 服务端回「太早」→ 只记 `too_early_since`，**不写 `last_click_at`**，并把间隔调小即可贴近窗口
- 429 / 5xx → 8 秒后重试一次

## 文件

| 文件 | 作用 |
|---|---|
| `renew_qi.js` | 主脚本（读状态 → 判定间隔 → POST renew → 写状态） |
| `.github/workflows/renew_qi.yml` | 仓库工作流 `Q-Fi Cloud Renew`，cron `41 7 * * *` |
| `qi_state.json` | `last_click_at` / `last_verdict` / `too_early_since` |
