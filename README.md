# xuqibaltic (portalmine branch)

PortalMine 自动检测 + 自动金币 (ad_begin / ad_claim)

## 功能

- 登录 PortalMine (账号密码 或 cookie)
- 检查 server_state (服务器状态、CPU、内存、磁盘等)
- **自动领取金币**：调用 `/api/coins.php?action=ad_begin` 开始广告 → 等待 → `ad_claim` 领取
- 状态保存到 `portalmine_state.json`，每次跑后自动 commit 回仓库
- Telegram 通知 (可选)

## 使用

### 本地运行

```bash
export PORTALMINE_USER="你的用户名"
export PORTALMINE_PASS="你的密码"
# 启用自动金币
export PORTALMINE_AUTO_COINS=1
# 广告等待秒数 (默认 60)
export PORTALMINE_AD_SECONDS=60
# 最大轮数 (默认 1, 最多 20)
export PORTALMINE_AD_MAX_ROUNDS=3
python renew_portalmine.py
```

### GitHub Actions

Workflow: `.github/workflows/renew_portalmine.yml`

- **手动触发**：Actions → PortalMine Server State Check → Run workflow
  - 勾选 `auto_coins` 启用自动金币
  - `ad_max_rounds` 控制轮数
- **定时触发**：每 6 小时自动跑一次 (UTC 00:00, 06:00, 12:00, 18:00)

需要在仓库 Settings → Secrets 配置：
- `PORTALMINE_USER` - 登录用户名
- `PORTALMINE_PASS` - 登录密码
- `TG_BOT_TOKEN` / `TG_CHAT_ID` - (可选) Telegram 通知

## 金币接口流程

```
1. GET /api/coins.php?action=ad_begin
   -> {ok:true, ad_id:"xxx", duration:60, ...}

2. sleep(duration)  # 等待广告时长

3. GET /api/coins.php?action=ad_claim
   -> {ok:true, coins:50, total:1250, ...}
```

错误处理：
- `COOLDOWN` / `ALREADY_ACTIVE` / `RATE_LIMIT` → 停止循环
- 其他错误 → 等 5 秒重试

## 文件

- `renew_portalmine.py` - 主脚本
- `portalmine_state.json` - 状态文件 (自动更新)
- `.github/workflows/renew_portalmine.yml` - GitHub Actions workflow
- `run_local.ps1` - Windows PowerShell 启动脚本
