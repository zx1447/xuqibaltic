# xuqibaltic (fenix2 branch)

FenixHost 服务自动续期 - 通过 Discord token + Playwright 每 3 天自动续期。

## 功能

- 用 Discord token 走 OAuth 登录 FenixHost（绕过 Cloudflare Turnstile）
- 访问 service 页面，点击 Renovar 按钮续期
- GitHub Actions 每 3 天自动跑一次

## 文件

- `fenix_renew.js` - Playwright 脚本（用 Discord token 登录 + 点击续期）
- `.github/workflows/fenix_renew.yml` - GitHub Actions workflow（每 3 天跑）

## Secrets 配置

仓库 Settings → Secrets and variables → Actions：

| Secret | 说明 | 示例 |
|--------|------|------|
| `FENIX_DISCORD_TOKEN` | Discord user token | `MTQzODQ5...` |
| `FENIX_SERVICE` | FenixHost service ID | `513` |

## 如何获取 Discord token

1. 打开 Discord 网页版，登录
2. F12 → Network → 任意请求 → Headers
3. 找 `Authorization` header，复制 value（格式 `MTQz...xxx`）

## 流程

```
1. GET fenixhost.net/oauth/discord → 拿 OAuth state
2. POST discord.com/api/v9/oauth2/authorize → 用 token 拿 code
3. GET fenixhost.net/oauth/discord/callback?code=xxx&state=xxx → 登录
4. GET fenixhost.net/services/513 → 访问 service 页面
5. 点击 Renovar 按钮 → 续期
```

## 注意

- Discord token 可能过期（约 30 天），失效后需要重新获取
- 续期后服务延期 7 天
- 每 3 天跑一次，留 4 天 buffer
