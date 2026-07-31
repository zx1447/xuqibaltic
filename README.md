# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 - session cookie + Playwright + session 保活。

## 功能

- 用 session cookie 直接访问 server 页面
- 点击「Extend 30 days」按钮延期
- **Session 保活**：每次跑完访问 dashboard/cloud/sessions 3 个页面刷新 session
- GitHub Actions 每 7 天自动跑

## Session 保活策略

每次 workflow 跑完会访问 3 个页面（`/panel/`, `/panel/cloud/`, `/panel/account/sessions`）刷新 session 活跃度，防止 idle 过期。每 7 天跑一次，session 通常 30 天有效，足够保持。

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

## 流程

```
1. 访问 /panel/ 预热 session (检查是否登录)
2. 访问 /panel/cloud/servers/{ID}/
3. 点 Extend 30 days
4. 验证 "extended for 30 days"
5. 访问 /panel/, /panel/cloud/, /panel/account/sessions 保活
```

## 注意

- 如果 session 过期（workflow 失败提示 "session expired"），需要重新登录获取新 cookie
- 每 7 天跑一次，延期 30 天
- Session 保活大幅降低过期概率
