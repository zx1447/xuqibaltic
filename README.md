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

## 本地运行

```bash
DIGITALPLAT_API_TOKEN=dp_live_xxx \
DIGITALPLAT_DOMAINS="domain1.com
domain2.com" \
DP_GH_USER=xxx \
DP_GH_PASS=xxx \
python3 digitalplat_auto_renew.py --state state/domains-state.json
```

加 `--dry-run` 只查询不续期；加 `--force` 即使 >120 天也尝试（DigitalPlat 会返回 400，用于测试）。
