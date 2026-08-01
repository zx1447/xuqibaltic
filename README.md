# DigitalPlat Domain Auto Renew

自动续期 DigitalPlat 免费域名。每 2 个月检查，所有域名尝试续期一次（>120 天剩余的会被 DigitalPlat 政策拒绝，自动跳过）。

## 配置

### Secrets
- `DIGITALPLAT_API_TOKEN` - DigitalPlat Developer API token (`dp_live_xxx`)，仅用于查询域名列表/到期
- `DP_GH_USER` - GitHub 用户名（用于 OAuth 登录 dash）
- `DP_GH_PASS` - GitHub 密码（用于 OAuth 登录 dash）

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

## 本地运行（已验证可行）

```bash
pip install "camoufox[geoip]"
python3 -m camoufox fetch

DIGITALPLAT_API_TOKEN=dp_live_xxx \
DIGITALPLAT_DOMAINS="domain1.com
domain2.com" \
DP_GH_USER=xxx \
DP_GH_PASS=xxx \
python3 digitalplat_auto_renew.py --state state/domains-state.json
```

加 `--dry-run` 只查询不续期；加 `--force` 即使 >120 天也尝试（DigitalPlat 会返回 400，用于测试）。

## ⚠️ 已知限制：GitHub Actions 跑不通

本地（住宅 IP）Camoufox 能稳定过 CF 挑战，但 **GitHub Actions 数据中心 IP 会被 dash 的 CF 严格防护拦截**：

- Camoufox 加载 dash 时页面一直停在 `Loading https://dash.domain.digitalplat.org/auth/login`
- 等待 180s 后超时，HTML 里只有 Cloudflare 自己的链接，没有 dash 真正内容
- 这是 CF 对 GitHub Actions IP 段的硬性限制，无法通过浏览器指纹绕过

### 可能的解决方案（待探索）

1. **本地 cron**：在自己的机器上跑（最稳）
2. **部署到稳定探针节点**：unikraft / sweb 等住宅 IP 云主机
3. **住宅代理**：通过 SOCKS5/HTTP 代理走住宅 IP（Camoufox 支持 `proxy` 参数，但需自备代理）
4. **Cloudflare Workers 中转**：用 CF Worker 反代 Panel API（但 Worker 也在 CF 网络内，可能仍被拦）

## 实测记录

- 2026-08-01：本地跑通，`cpaner.dpdns.org` 续期成功（20261111 → 20271111）
- 2026-08-01：GitHub Actions 3 次失败，均卡在 CF 挑战
