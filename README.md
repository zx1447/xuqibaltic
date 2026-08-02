# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 - 纯 API 调用 (无浏览器)。

## 功能

- 用 session cookie + CSRF token 直接 POST 续期
- 可选: 用 API token 查 server 状态 (不需要 session)
- **不依赖 Playwright/浏览器**, 不会被 UI 改版影响
- 几秒完成
- GitHub Actions 每 7 天自动跑

## 续期 API

```
POST https://www.pidginhost.com/panel/cloud/servers/{id}/
Content-Type: application/x-www-form-urlencoded
Cookie: sessionid=...; csrftoken=...
X-CSRFToken: ...
Referer: https://www.pidginhost.com/panel/cloud/servers/{id}/
Body: csrfmiddlewaretoken={CSRF}&action=extend_renewal
```

注意: PidginHost 的 API token (`Authorization: Token ...`) **不能**用于续期,
因为续期 endpoint 是 panel 表单 (需要 Django session + CSRF), 不认 API token。
API token 只用于查状态 (`GET /api/v1/cloud/servers/{id}/`)。

## Secrets

| Secret | 说明 |
|--------|------|
| `PIDGINHOST_SESSION` | sessionid cookie (从浏览器 F12 获取) |
| `PIDGINHOST_CSRF` | csrftoken cookie |
| `PIDGINHOST_API_TOKEN` | API token (可选, 用于查状态) |
| `PIDGINHOST_SERVER_ID` | 服务器 ID (如 3920) |

## 如何获取 cookie

1. 登录 https://www.pidginhost.com/panel/ (用 GitHub OAuth)
2. F12 → Application → Cookies → `https://www.pidginhost.com`
3. 复制 `sessionid` 和 `csrftoken` 的 value

## 如何获取 API token

1. 登录 https://www.pidginhost.com/panel/account/sessions
2. 在 "API Tokens" 区域点 "Create new token"
3. 复制 token value

## 注意

- **Session 过期**: 通常 14-30 天过期, workflow 失败时 (401/403) 需要重新登录拿新 cookie
- **续期上限**: PidginHost 免费服务器最多续到 30 天, 已经 30 天时不能再续

## 本地测试

```bash
PIDGINHOST_SESSION=xxx PIDGINHOST_CSRF=xxx PIDGINHOST_API_TOKEN=xxx PIDGINHOST_SERVER_ID=3920 \
  python3 renew_pidginhost.py
```
