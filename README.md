# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 - 纯 API 调用 (无浏览器)。

## 功能

- 用 session cookie + CSRF token 直接 POST 续期
- **不依赖 Playwright/浏览器**, 不会被 UI 改版影响
- 几秒完成 (之前 Playwright 要 30 秒+)
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

成功响应: 页面包含 "expires in 30 days" 或 "extended for 30 days"

## Secrets

| Secret | 说明 |
|--------|------|
| `PIDGINHOST_SESSION` | sessionid cookie (从浏览器 F12 获取) |
| `PIDGINHOST_CSRF` | csrftoken cookie |
| `PIDGINHOST_SERVER_ID` | 服务器 ID (如 3920) |

## 如何获取 cookie

1. 登录 https://www.pidginhost.com/panel/ (用 GitHub OAuth)
2. F12 → Application → Cookies → `https://www.pidginhost.com`
3. 复制 `sessionid` 和 `csrftoken` 的 value

## 注意

- **Session 过期**: 通常 14-30 天过期, workflow 失败时 (401/403) 需要重新登录拿新 cookie
- **续期上限**: PidginHost 免费服务器最多续到 30 天, 已经 30 天时不能再续 (脚本会显示 "已是 30 天上限")
- **频率**: 每 7 天跑一次, 到期前 7 天才能续 (实际到期 30 天 - 7 天 = 23 天就要开始续)

## 本地测试

```bash
PIDGINHOST_SESSION=xxx PIDGINHOST_CSRF=xxx PIDGINHOST_SERVER_ID=3920 \
  python3 renew_pidginhost.py
```
