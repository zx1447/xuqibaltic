# xiaoxia — 小虾云端电脑自动唤醒

目标：`cn.xiaoxia.app`（OpenClaw 云端个人助理）的**云电脑实例**（id 599）永不休眠失联。

## 一、休眠机制（2026-11-13 实测 + 前端逆向）

| 项 | 规则 |
|---|---|
| 闲置休眠 | "一段时间"无活动自动休眠（`availability: offline`），**数据保留**，点「启动」恢复（时长官方不写明） |
| 额度停机 | 日 **100 万 token** 用完 → 停机，**次日 0 点（UTC）自动恢复**（买积分包则数分钟内恢复） |
| 状态 | `availability`：**ready**（可用）/ **loading**（启动中）/ **offline**（离线=休眠）+ `status` + `health_status` |
| ⚠️ 休眠的坑 | 绑定 bot（微信/QQ/飞书/TG）**消息叫不醒**，休眠期消息**不补收**——休眠=失联 |

## 二、API 契约（全部实测）

| 项 | 契约 |
|---|---|
| 登录 | `POST /api/auth/login {"email","password"}` → `{"token":"<JWT 7 天>"}` |
| 状态 | `GET /api/instance`（Bearer JWT）→ 状态三元组 + `quota{today_tokens,effective_limit}` |
| **唤醒** | `POST /api/instance/start` → **幂等**（运行中回 `{"status":"running"}`，无副作用）✅ 实测 |
| 远程唤醒令牌 | `POST https://xiaoxia.app/api/instance/wake` + `Authorization: Bearer <令牌>`——**仅付费/VIP 可建**（`err_wake_paid_only`，买任意 AI 积分解锁，上限 5）；免费账号走本脚本登录通道 |
| 其他 | `/api/wake-tokens`（管理）、`/api/instance/usage/daily`、`/api/apikeys`（OpenAPI/MCP）、`/api/instance/dashboard-handoff` |

## 三、守护逻辑（bkt 装甲）

每 30 分钟一轮（长驻循环 330min/次 + `GH_PAT` 自派发接力 + schedule 兜底，防 GitHub 调度饿死）：

- `ready` → 不动；`loading` → 等下轮
- `offline`（或 stopped/hibernated）→ **`POST /api/instance/start` 拉起**，复查确认
- token 缓存 `xiaoxia_state.json`（7 天免重登录，失效自动重登，变化时回写分支）
- **哪吒联动**：实例在线后经网关 WS 触发容器内 `nezha-keepalive` 任务立即补检（cron.run，零 token）；任务被删自动重建

## 三·五、哪吒监控联动（2026-11-13）

容器内哪吒 agent（`/opt/nezha/agent/nezha-agent -c config.yml`，NZ_SERVER=nz.zxydk1715.dpdns.org:443）——**每次实例启动/唤醒都必须确保它启动**，双层保险：

| 层 | 机制 | 频率 |
|---|---|---|
| 容器侧 | OpenClaw cron 脚本任务 `nezha-keepalive`（`payload.kind=script`，**零 token**）跑 `scripts/keepalive/nezha-keepalive.sh`：进程没了拉起；二进制没了按用户原命令重装 | 每 5 分钟 |
| GitHub 侧 | `wake_xiaoxia.py` 每轮经网关 WS `cron.run` 补检（`_MiniWS` 纯 stdlib）；任务丢失 `cron.add` 重建 | 每 30 分钟轮 + 每次唤醒后 |

容器访问契约（逆向实测）：
- **文件 API**（cookie `xx_hosting_token=<JWT>`）：`GET /api/instance/files[?path=]`、`download?path=`、`POST files/upload?path=.`（multipart 字段名 `file`）——根目录即 `/workspace`
- **终端 WS** `wss://…/api/instance/terminal?cols&rows`（Cookie+Origin）：帧 `[0x00]+utf8`=按键、`[0x01]+cols(u16BE)+rows(u16BE)`=尺寸；服务→客 `[0x00]`=输出（⚠️ 输入当前不执行，勿依赖）
- **网关 WS** `wss://…/api/instance/agent/ws`（协议 v4）：`{"type":"req","id","method":"connect","params":{minProtocol:4,maxProtocol:4,client:{id:"gateway-client",version:"xx-workspace",mode:"backend"},role:"operator",scopes:[operator.read/write/admin],caps:["tool-events"],auth:{}}}` → hello-ok；RPC：`chat.send{sessionKey,message,idempotencyKey}`（**长命令会被风控秒拒，改为上传脚本+短消息让助手执行**）、`chat.history`、`sessions.list`、`cron.add/list/run`（脚本任务 `payload.kind="script"` 复刻 desktop-keepalive 形状）

## 四、使用

```bash
export XIAOXIA_EMAIL='you@example.com'
export XIAOXIA_PASSWORD='...'
python3 wake_xiaoxia.py            # 单轮
python3 wake_xiaoxia.py --loop 330 # 长驻
```

Secrets：`XIAOXIA_EMAIL` / `XIAOXIA_PASSWORD` / `GH_PAT`（接力用）。
