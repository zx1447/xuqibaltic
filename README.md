# xuqibaltic (pidginhost branch)

PidginHost cloud server 自动延期 - GitHub OAuth + Playwright。

## 功能

- 用 GitHub 账号登录 PidginHost（OAuth）
- 点击「Extend 30 days」按钮延期服务器
- GitHub Actions 每 25 天自动跑一次

## 文件

- `renew_pidginhost.js` - Playwright 脚本
- `.github/workflows/renew_pidginhost.yml` - GitHub Actions workflow

## Secrets

| Secret | 说明 | 示例 |
|--------|------|------|
| `PIDGINHOST_GH_USER` | GitHub 用户名 | `zx1447` |
| `PIDGINHOST_GH_PASS` | GitHub 密码 | `zxy1715x@gmail.com` |
| `PIDGINHOST_SERVER_ID` | 服务器 ID | `3920` |

## 流程

```
1. 访问 pidginhost 登录页
2. 点 GitHub 登录
3. 填 GitHub 用户名+密码
4. 跳回 pidginhost panel (已登录)
5. 访问 /panel/cloud/servers/{ID}/
6. 点 Extend 30 days
7. 验证 "extended for 30 days"
```

## 注意

- GitHub 登录可能偶尔要 device verification（邮箱验证码），届时自动续期会失败
- 每 25 天跑一次，延期 30 天，留 5 天 buffer
