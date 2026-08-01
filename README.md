# DigitalPlat Domain Auto Renew

自动续期 DigitalPlat 免费域名。每 2 个月检查，所有域名尝试续期一次（>120 天剩余的会被 DigitalPlat 政策拒绝，自动跳过）。

## 配置

### Secrets
- `DIGITALPLAT_API_TOKEN` - DigitalPlat Developer API token (`dp_live_xxx`)，仅用于查询域名列表/到期
- `DP_GH_USER` - GitHub 用户名（用于 OAuth 登录 dash）
- `DP_GH_PASS` - GitHub 密码（用于 OAuth 登录 dash）
- `DP_VLESS_URL` - **（可选）** VLESS 代理 URL，用于绕过 GHA 数据中心 IP 被 CF 拦截的问题
  - 格式：`vless://UUID@HOST:PORT?encryption=none&security=tls&sni=HOST&fp=chrome&type=ws&path=/PATH#TAG`
  - 不设置时直连（仅本地/住宅 IP 能过 CF）

### Variables (Settings → Actions → Variables)
- `DIGITALPLAT_DOMAINS` - 域名列表，一行一个

## 工作原理

DigitalPlat 有两套 API：
1. **Developer API** (`domain-api.digitalplat.org`)：只有查询接口（list/get domain），README 标注的 `POST /domains/{d}/renew` 实际返回 500，不能用于续期
2. **Panel API** (`dash.domain.digitalplat.org/_panel_api/api`)：控制面板内部 API，是真实的续期接口
   - `POST /api/domains/{d}/renew` body `{"renewal_type":"free","years":1}`
   - 需要 dash 登录态（`panel_session` cookie）+ CSRF token（`X-CSRF-Token` header，从 `panel_csrf_token` cookie 读）

dash 在 Cloudflare 后，普通 Python urllib/Playwright headless Chromium 过不了 CF 挑战，必须用 **Camoufox**（反检测 Firefox）。

## 状态分类

- `renewed` - 续期成功（+1 年）
- `skip_not_expiring` - 剩余 >120 天，DigitalPlat 政策禁止续期（不是错误）
- `renew_failed` - 真正失败（网络/登录/CSRF/API 异常）

## 本地运行

```bash
pip install "camoufox[geoip]"
python3 -m camoufox fetch

# 直连方式（需要 IP reputation OK 的网络）
DIGITALPLAT_API_TOKEN=dp_live_xxx \
DIGITALPLAT_DOMAINS="domain1.com
domain2.com" \
DP_GH_USER=xxx \
DP_GH_PASS=xxx \
python3 digitalplat_auto_renew.py --state state/domains-state.json

# 通过 VLESS 代理（绕过 IP reputation 问题）
VLESS_URL='vless://uuid@host:443?...' bash scripts/start-xray.sh
CAMOUFOX_PROXY=http://127.0.0.1:10809 \
DIGITALPLAT_API_TOKEN=dp_live_xxx \
DIGITALPLAT_DOMAINS="domain1.com" \
DP_GH_USER=xxx \
DP_GH_PASS=xxx \
python3 digitalplat_auto_renew.py --state state/domains-state.json
```

加 `--dry-run` 只查询不续期；加 `--force` 即使 >120 天也尝试（DigitalPlat 会返回 400，用于测试）。

## CF 拦截问题与解决方案

### 问题
GitHub Actions 数据中心 IP 被 IP reputation 库标记为 `proxy=true`，dash 的 CF 直接拦截。
即使 Camoufox 也过不了 CF 挑战（页面停在 `Loading https://...` 180s+）。

### 实测 IP 对比

| IP 类型 | hosting | proxy | CF 拦截 |
|---------|---------|-------|---------|
| 阿里云香港 (47.57.x.x) | true | false | ❌ 不拦 |
| GitHub Actions (140.82.x.x) | true | **true** | ✅ 拦 |
| Azure 美西 | true | false | ❌ 不拦 |
| VLESS 出口德国 GoeTel | false | false | ❌ 不拦 |

### 解决方案

**方案 A**：本地直连（IP reputation OK 的网络）
**方案 B**：通过 VLESS 代理走一个 `proxy=false` 的出口
  - workflow 里设置 `DP_VLESS_URL` secret
  - scripts/start-xray.sh 自动启动 xray + VLESS
  - Camoufox 走 `http://127.0.0.1:10809` 代理

## 实测记录

- 2026-08-01：本地直连跑通，`cpaner.dpdns.org` 续期成功（20261111 → 20271111）
- 2026-08-01：GitHub Actions 3 次失败，根因是 IP reputation `proxy=true`
- 2026-08-01：本地 + VLESS 代理（德国 GoeTel 出口）跑通完整流程
