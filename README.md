# xuqibaltic (cocalc branch)

CoCalc nezha probe keepalive - 通过 GitHub Actions 每 10 分钟保活 cocalc 项目。

## 功能

- 定期访问 cocalc 项目页面（保持项目 active，防 idle stop）
- 检查 nezha 面板 cocalc entry 是否在线
- 如果离线，尝试创建新 terminal（触发 `.bashrc` 自启 nezha agent）
- Telegram 告警（可选）

## 文件

- `cocalc_keepalive.py` - 主脚本
- `.github/workflows/cocalc_keepalive.yml` - GitHub Actions workflow（每 10 分钟跑）

## Secrets 配置

仓库 Settings → Secrets and variables → Actions：

| Secret | 说明 | 示例 |
|--------|------|------|
| `COCALC_PROJECT` | cocalc 项目 ID | `f1438fae-073d-4fd7-b135-430ad46742e3` |
| `COCALC_COOKIE` | cocalc remember_me cookie | `sha512$xxx$1000$xxx` |
| `NEZHA_PANEL` | nezha 面板 URL | `https://nz.zxydk1715.dpdns.org` |
| `COCALC_NEZHA_UUID` | cocalc 探针 UUID | `a33d05ee-55b7-56c4-869e-c76aae75843b` |
| `TG_BOT_TOKEN` | (可选) Telegram bot token | |
| `TG_CHAT_ID` | (可选) Telegram chat id | |

## 如何获取 COCALC_COOKIE

1. 登录 https://cocalc.ai
2. F12 → Application/Storage → Cookies → `https://cocalc.ai`
3. 找 `remember_me` cookie，复制 value（格式 `sha512$xxx$1000$xxx`）

## 限制

- cocalc Free 项目 idle 久了会被 stop，cookie 访问只能延缓
- 如果项目被 stop 后 cookie 失效，需要重新登录获取 cookie
- cocalc API 是 websocket，REST 创建 terminal 可能失败，最坏情况需要手动开 terminal
