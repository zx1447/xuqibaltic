# xuqibaltic (portalmine branch)

PortalMine 自动检测 + 自动金币 (ad_begin / ad_claim)

## 功能

- 登录 PortalMine (账号密码 或 cookie)
- 检查 server_state
- **自动领取金币**：ad_begin → 等 60s → ad_claim，每轮 +20 coins
- **持续模式 (ad_forever)**：循环领金币直到 GitHub Actions 6h 超时，自动重启下一轮 → 24/7 领金币
- 状态保存到 `portalmine_state.json`，每次成功领取后立即 commit
- Telegram 通知 (可选)

## 金币接口（从 F12 + dashboard JS 逆向）

```
1. POST /api/coins.php?action=ad_begin&_=<ts>
   Headers: x-portalmine-server-id: <id>, Content-Type: application/json
   Body: {"zone":"portalmine_idle_reward_v10900","provider":"internal","required_steps":1}
   -> {"ok":true, "token":"xxx", "seconds":60, "reward":20, ...}

2. sleep(62)

3. POST /api/coins.php?action=ad_claim&_=<ts>
   Headers: x-portalmine-server-id: <id>, Content-Type: application/json
   Body: {"token":"xxx","completed_steps":1,"required_steps":1,"provider":"internal"}
   -> {"ok":true, "coins":120, "reward":20, "msg":"20 coins added."}
```

## 使用

### GitHub Actions (推荐)

Workflow: `.github/workflows/renew_portalmine.yml`

**手动触发**：Actions → PortalMine Server State Check → Run workflow

| 参数 | 说明 |
|------|------|
| `auto_coins` | 启用自动金币（固定轮数） |
| `ad_forever` | 持续模式：循环领金币直到超时，自动重启 |
| `ad_max_rounds` | 固定轮数模式的轮数（默认 1） |
| `ad_max_runtime` | forever 模式最大运行秒数（默认 18000=5h） |

**24/7 领金币**：勾选 `ad_forever=true`，workflow 会：
1. 跑 5 小时（每轮 60s + 2s = 62s，约 290 轮 = 5800 coins）
2. 5 小时后自动 re-trigger 下一轮
3. 循环不停（除非 GitHub 限制或 secrets 失效）

**定时触发**：每 6 小时自动跑一次（不带金币，只检测 server_state）

### Secrets 配置

仓库 Settings → Secrets and variables → Actions：
- `PORTALMINE_USER` - 登录用户名
- `PORTALMINE_PASS` - 登录密码
- `PORTALMINE_SERVER_ID` - 服务器 ID（从 F12 `x-portalmine-server-id` header 获取）
- `TG_BOT_TOKEN` / `TG_CHAT_ID` - (可选) Telegram 通知

### 本地运行

```bash
export PORTALMINE_USER="你的用户名"
export PORTALMINE_PASS="你的密码"
export PORTALMINE_SERVER_ID="1388"
export PORTALMINE_AUTO_COINS=1

# 固定轮数
export PORTALMINE_AD_MAX_ROUNDS=5

# 或者持续模式
export PORTALMINE_AD_FOREVER=1
export PORTALMINE_AD_MAX_RUNTIME=3600  # 1小时

python renew_portalmine.py
```

## 如何获取 PORTALMINE_SERVER_ID

1. 登录 https://portalmine.com/dashboard.html
2. F12 → Network → 刷新页面
3. 找请求 `server-v16132.php?action=server_state`
4. Request Headers 里的 `x-portalmine-server-id: 1388`

## 文件

- `renew_portalmine.py` - 主脚本
- `portalmine_state.json` - 状态文件（自动更新）
- `.github/workflows/renew_portalmine.yml` - GitHub Actions workflow

## 错误处理

- `COOLDOWN` / `RATE_LIMIT`：forever 模式下等 60s 重试，固定模式停止
- `BAD_TOKEN`：GET 方式调用导致，已修复用 POST + JSON
- `STEPS_NOT_COMPLETE`：没等够 60s 或没带 `completed_steps:1`，已修复
- 每次 ad_claim 成功后立即保存 state，即使被 kill 也能保留进度
