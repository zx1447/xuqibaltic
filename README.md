# Torchlite 免费服务器自动续期

`Minecraft (Paper) - zxy1715x` · UUID `be89843c-8e77-4ce7-9fc4-c4d78a2b4ea1`

## 结论先行：广告闸门已破解

面板要求"看广告才能开机/续期"，但**后端根本不检查你有没有看广告**。
它只校验两件事：

1. `ad_token` 是 `/ad-gate` 真实签发的
2. 从签发到提交，过了 `count × wait_seconds` 秒

所以纯 HTTP 就能续期，不需要浏览器、不需要播广告。

### 实测对照

| 提交的东西 | 结果 |
|---|---|
| 假 token | `403 ad_gate_required` |
| 无 token | `403 ad_gate_required` |
| 真 token，0 秒后 | `403 ad_gate_required` ← 冷却没到 |
| **真 token，等 72 秒** | **`200 OK` ✅** |

## 完整协议

```http
# 1. 校验会话（必须有 cookie，API key 不行）
GET /api/client/account

# 2. 申请闸门令牌
POST /api/client/servers/{uuid}/ad-gate
     {"action": "renew" | "start"}
→ {"data": {"required": true, "token": "...", "count": 3, "wait_seconds": 24}}

# 3. 等 count × wait_seconds 秒（3 × 24 = 72 秒）

# 4. 提交令牌
POST /api/client/servers/{uuid}/renewal      {"ad_token": token}   # 续期
POST /api/client/servers/{uuid}/start-queue  {"ad_token": token}   # 开机
```

### 为什么不能用 API key

```
POST /ad-gate  with  Authorization: Bearer ptlc_...
→ 403 {"code":"ad_gate_browser_only",
       "detail":"Free servers can only be started or renewed from the panel
                 in a browser, not with an API key."}
```

平台明确拒绝 API key。必须用**浏览器会话 cookie**。

## 续期机制

| 项目 | 值 |
|---|---|
| 每次发放 | **40 分钟** |
| 可续期窗口 | 到期前**最后 5 分钟**（300 秒） |
| 提前点的结果 | `409 renewal_not_expired` + `{renewable_in: N}` |

**每 40 分钟必须续一次**，错过 5 分钟窗口服务器就下线。

## 调度设计

**不用 `*/5` cron —— 实测本仓库的短周期 schedule 完全不触发**（新 workflow 有激活
延迟，且默认分支长期无提交时 GitHub 会自动禁用 schedule）。40 分钟周期漏一次就下线，
不能赌这个。

改成**长驻 job**：

```yaml
- cron: "7 */6 * * *"     # 每 6 小时接力一次
```

一次 job 跑 **5.5 小时**（330 分钟），内部每 4 分钟检查一次状态，
进窗口就过闸门续期。6 小时接力 5.5 小时，无缝衔接。

```js
while (now < endAt) {
  读 renewal 状态
  进窗口 -> ad-gate -> 等 72s -> renewal
  离线   -> ad-gate -> 等 72s -> start-queue
  sleep 4 分钟
}
```

公开仓库，Actions 分钟数不限，长驻没有成本。

> ⚠️ 每个 job 上限 6 小时（GitHub 硬限制），连跑 5.5 小时是安全余量。

## Cookie 与自动续命

### 关键：`remember_web` 能自己恢复会话

cookie 里成分：

| cookie | 作用 | 寿命 |
|---|---|---|
| `pterodactyl_session` | 主会话 | **短**，几天 |
| `XSRF-TOKEN` | CSRF 令牌 | 跟随 session |
| **`remember_web_...`** | **Laravel remember me** | **长**，默认几个月 |
| `cf_clearance` | Cloudflare | 约 30 分钟（脚本不依赖） |

实测（删掉 session 只留 remember）：

| cookie | `/api/client/account` |
|---|---|
| 完整 cookie | `200` ✅ |
| 去掉 `pterodactyl_session` | `200` ✅ |
| **只剩 `remember_web`** | **`200`** ✅ |

服务器还会在响应里 `Set-Cookie` 下发新的 `pterodactyl_session` 和 `XSRF-TOKEN`。

所以脚本做了两件事：

1. **cookie jar** —— 每次响应都把 `Set-Cookie` 收进内存，自动更新轮换后的 session
2. **失效重试** —— 会话 401 但还有 `remember_web` 时，等 2 秒重试一次，
   让 Laravel 用 remember 重建会话

> 前提：登录时必须勾 **「记住我 / Remember me」**，否则不会有 `remember_web`。

### 什么时候还需要重新抓 cookie

- `remember_web` 本身过期（默认几个月）或你改密码 / 手动登出
- 日志会明确提示：

```
[torchlite] ❌ 会话无效 HTTP 401 —— 需要重新导出 cookie
```

更新方法：浏览器登录 → F12 → Network → 任意请求 → 复制整个 `Cookie:` → 覆盖 secret `TORCHLITE_COOKIE`

## 本地跑

```bash
# 只看状态
TORCHLITE_COOKIE='...' TORCHLITE_DRY_RUN=1 node renew_session.js

# 正常续期（窗口内才动）
TORCHLITE_COOKIE='...' node renew_session.js

# 强行续期（测试用，非窗口期会 409）
TORCHLITE_COOKIE='...' TORCHLITE_FORCE=1 node renew_session.js

# 离线时顺便开机
TORCHLITE_COOKIE='...' TORCHLITE_DO_START=1 node renew_session.js
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `renew_session.js` | **主脚本**，纯 HTTP，cookie 认证 |
| `renew_torchlite.js` | 早期 API key 版，被 `ad_gate_browser_only` 挡死，仅留作记录 |
| `browser/renew_browser.js` | Playwright 版，破解过程用的，现在不需要 |
| `browser/token_compare.js` | 对照实验脚本，证明"不看广告也能过" |
| `BROWSER-方案结论.md` | 探路记录 |

## 需要维护的地方

| 情况 | 怎么办 |
|---|---|
| cookie 过期（401） | 重新导出，覆盖 `TORCHLITE_COOKIE` |
| 换了服务器 | 改 `TORCHLITE_UUID` |
| 面板换域名 | 改 `TORCHLITE_BASE` |
| 广告数量/冷却变了 | 脚本自动读 `count` / `wait_seconds`，不用改 |
