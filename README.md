# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 - 用 session cookie + Playwright。

## 功能

- 用 session cookie 直接访问 server 页面
- 点击「Extend 30 days」按钮延期
- GitHub Actions 每 25 天自动跑

## Secrets

| Secret | 说明 |
|--------|------|
| `PIDGINHOST_SESSION` | sessionid cookie (从浏览器 F12 获取) |
| `PIDGINHOST_CSRF` | csrftoken cookie |
| `PIDGINHOST_SERVER_ID` | 服务器 ID (如 3920) |

## 如何获取 cookie

1. 登录 https://www.pidginhost.ro/panel/
2. F12 → Application → Cookies → `https://www.pidginhost.ro`
3. 复制 `sessionid` 和 `csrftoken` 的 value

## 注意

- session cookie 可能过期（通常 2-4 周）
- 过期后需要重新登录获取新 cookie
- 每 25 天跑一次，延期 30 天
