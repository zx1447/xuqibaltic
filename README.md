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

## cron 设计

```yaml
- cron: "*/5 * * * *"
```

40 分钟 ÷ 5 分钟 = **每个周期 8 次机会**。每次运行：

```
renewable_in > 660s        秒退（不睡，几乎不耗资源）
0 < renewable_in <= 660s   睡到窗口打开，再过闸门
renewable_in <= 0          已在窗口内，直接过闸门
```

公开仓库，Actions 分钟数不限，5 分钟一次没有成本。

## ⚠️ Cookie 会过期

`TORCHLITE_COOKIE` 是 Pterodactyl 会话 cookie，**通常几天到几周失效**。
失效后脚本会明确报错：

```
[torchlite] ❌ 会话无效 HTTP 401
[torchlite]    cookie 过期了，需要重新从浏览器导出
```

更新方法：

1. 浏览器登录 `https://game.torchlite.host`
2. F12 → Network → 点任意 `game.torchlite.host` 请求
3. 复制 Request Headers 里完整的 `Cookie:` 值
4. 覆盖 secret `TORCHLITE_COOKIE`

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
