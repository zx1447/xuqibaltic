# xuqibaltic (domain branch)

DigitalPlat FreeDomain 自动续期 - GitHub OAuth + Playwright。

## 功能

- 用 GitHub 账号走 OAuth 登录 dash.domain.digitalplat.org
- 列出所有域名 + 到期时间
- 对每个域名调 renew API 续期
- GitHub Actions 每月 1 号自动跑

## 文件

- `renew_domains.js` - Playwright 脚本
- `.github/workflows/renew_domains.yml` - GitHub Actions workflow

## Secrets

| Secret | 说明 |
|--------|------|
| `DP_GH_USER` | GitHub 用户名 |
| `DP_GH_PASS` | GitHub 密码 |

## 流程

```
1. 访问 dash.domain.digitalplat.org (过 CF challenge)
2. 走 GitHub OAuth (client_id=Ov23liMuiWEyVv3b9R6u)
3. 填 GitHub 用户名+密码
4. 点 Authorize (如果需要)
5. 回到 dash (已登录)
6. GET /_panel_api/api/domains (列出所有域名)
7. POST /_panel_api/api/domains/{domain}/renew (对每个域名续期)
```

## 注意

- dash 用 CF "Under Attack" 模式，Playwright 需要过 CF challenge
- GitHub Actions IP 可能被 CF 拦，如果失败需要手动跑
- 每月 1 号跑一次
