# xuqibaltic (portalmine branch)

PortalMine 自动检测 + 自动金币 (ad_begin / ad_claim)

## 功能

- 登录 PortalMine (账号密码 或 cookie)
- 检查 server_state (服务器状态、CPU、内存、磁盘等)
- **自动领取金币**：调用 `/api/coins.php?action=ad_begin` 开始广告 → 等待 60s → `ad_claim` 领取
- 状态保存到 `portalmine_state.json`，每次跑后自动 commit 回仓库
- Telegram 通知 (可选)

## 金币接口流程

从 F12 抓包 + dashboard-v16132.js 逆向得到：

```
1. POST /api/coins.php?action=ad_begin&_=<timestamp>
   Headers: x-portalmine-server-id: <server_id>, Content-Type: application/json
   Body: {"zone":"portalmine_idle_reward_v10900","provider":"internal","required_steps":1}
   -> {"ok":true, "token":"xxx", "seconds":60, "reward":20, "eligible_at":<ts>, ...}

2. sleep(62)  # 等待广告时长 (60s + 2s buffer)

3. POST /api/coins.php?action=ad_claim&_=<timestamp>
   Headers: x-portalmine-server-id: <server_id>, Content-Type: application/json
   Body: {"token":"xxx","completed_steps":1,"required_steps":1,"provider":"internal"}
   -> {"ok":true, "coins":100, "reward":20, "msg":"20 coins added."}
```

**关键点**：
- 必须带 `x-portalmine-server-id` header（从 dashboard F12 抓包获取）
- 必须用 POST + JSON body（GET 会返回 BAD_TOKEN）
- ad_claim 必须带 `completed_steps: 1`（不带会返回 STEPS_NOT_COMPLETE）
- zone = `portalmine_idle_reward_v10900`（从 dashboard JS 提取）
- 每次广告 +20 coins

## 使用

### GitHub Actions

Workflow: `.github/workflows/renew_portalmine.yml`

- **手动触发**：Actions → PortalMine Server State Check → Run workflow
  - 勾选 `auto_coins` 启用自动金币
  - `ad_max_rounds` 控制轮数（每轮 60s，最多 20 轮）
- **定时触发**：每 6 小时自动跑一次 (UTC 00:00, 06:00, 12:00, 18:00)

需要在仓库 Settings → Secrets 配置：
- `PORTALMINE_USER` - 登录用户名
- `PORTALMINE_PASS` - 登录密码
- `PORTALMINE_SERVER_ID` - 服务器 ID（从 dashboard F12 的 `x-portalmine-server-id` header 获取）
- `TG_BOT_TOKEN` / `TG_CHAT_ID` - (可选) Telegram 通知

### 本地运行

```bash
export PORTALMINE_USER="你的用户名"
export PORTALMINE_PASS="你的密码"
export PORTALMINE_SERVER_ID="1388"  # 从 F12 抓包获取
# 启用自动金币
export PORTALMINE_AUTO_COINS=1
# 最大轮数 (默认 1, 最多 20)
export PORTALMINE_AD_MAX_ROUNDS=3
python renew_portalmine.py
```

## 如何获取 PORTALMINE_SERVER_ID

1. 登录 https://portalmine.com/dashboard.html
2. F12 打开开发者工具 → Network 标签
3. 点页面上任何按钮（或刷新页面）
4. 找请求 `server-v16132.php?action=server_state`
5. 看 Request Headers 里的 `x-portalmine-server-id: 1388`（数字部分就是）

## 文件

- `renew_portalmine.py` - 主脚本
- `portalmine_state.json` - 状态文件 (自动更新)
- `.github/workflows/renew_portalmine.yml` - GitHub Actions workflow
