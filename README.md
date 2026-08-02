# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 + 自动刷新 session cookie。

## 功能

- **自动登录**: 每次跑都用 Playwright + GitHub OAuth 重新登录, 拿新 session cookie
- **纯 API 续期**: 用新 cookie POST `action=extend_renewal`, 不点按钮
- **自动刷新 secret**: 把新 cookie 写回 GitHub secret, 下次 workflow 用新 session
- **永不过期**: session 永远是新鲜的, 不需要手动更新

## 流程

```
1. Playwright 登录 pidginhost.com (GitHub OAuth)
   → 拿到新 sessionid + csrftoken
2. 用新 cookie POST /panel/cloud/servers/{id}/
   → action=extend_renewal 续期 30 天
3. 用 GitHub API 把新 cookie 写回 secret
   → PIDGINHOST_SESSION + PIDGINHOST_CSRF 更新
```

## Secrets

| Secret | 说明 |
|--------|------|
| `DP_GH_USER` | GitHub 用户名 (用于 OAuth 登录 pidginhost) |
| `DP_GH_PASS` | GitHub 密码 |
| `GH_TOKEN` | GitHub PAT (需要 repo 权限, 用于更新 secret) |
| `PIDGINHOST_SERVER_ID` | 服务器 ID (如 3920) |

注意: 不再需要 `PIDGINHOST_SESSION` 和 `PIDGINHOST_CSRF` (脚本会自动更新它们), 但第一次跑需要它们存在 (可以是空值或旧值)。

## 频率

每 7 天跑一次 (UTC 00:00), 到期前 7 天才能续。

## 本地测试

```bash
DP_GH_USER=xxx DP_GH_PASS=xxx GH_TOKEN=ghp_xxx PIDGINHOST_SERVER_ID=3920 \
  GITHUB_REPOSITORY=zx1447/xuqibaltic \
  python3 renew_pidginhost.py
```

## 注意

- GitHub OAuth 登录时如果触发 "verified-device" (异地登录验证), 脚本会失败
- 这种情况需要你手动登录一次 pidginhost.com, 让 GitHub 记住 GHA 的 IP
- 实测 GHA 数据中心 IP 登录 GitHub 不会触发 verified-device (跟 dash.domain.digitalplat.org 不同)
