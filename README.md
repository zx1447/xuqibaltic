# Teradata ClearScape 哪吒探针保活

节点：**Teradata 免费PaaS**（uuid 固定 `a75b0e41-ed11-8cce-d947-46d6d8455da2`）
面板：`https://nz.zxydk1715.dpdns.org`

## 平台特性

- ClearScape Analytics Experience 免费试用：1 个环境 / 60 天寿命 / 40 credits 月配额
- 环境闲置会被平台挂起（STOPPED），外部 ping 无法唤醒，必须 dashboard/API start
- 60 天到期后环境被回收 → **同账号删旧建新可续**（本分支核心能力）

## 全自动链路（纯 HTTP，无浏览器）

```
Firebase REST 登录 (identitytoolkit signInWithPassword)
  └→ Firestore 读 users/{email}.token (后端 API token)
      └→ GET  /environments            状态检查
          ├→ PATCH /environments/nzprobe {operation:start}   自动唤醒
          ├→ 剩余<=3天 或 环境不存在 → DELETE + POST 重建（同配置 nzprobe / asia-northeast）
          │    └→ Jupyter ?token= API → PUT 部署脚本 → kernel ws 执行
          │         （容器内自下载 nezha-agent v2.4.0 + md5 校验 + v4proxy + watchdog）
          └→ RUNNING → Jupyter /api/status 探活 + 面板节点核对
```

## 防休眠双保险

1. **容器内** watchdog.sh 每 5 分钟自我 ping + 每 60 秒拉起 v4proxy/nezha-agent
2. **外部** GitHub Actions 每 2 小时检查：STOPPED 自动 start，1 分钟内恢复上报

## 到期重建（60 天）

- 判定：`createDate + 60 天` 剩余 ≤ 3 天 → 提前主动重建，不断线
- 重建：删旧环境 → `POST /environments {name, password, region}` 同配置 → 等 RUNNING →
  自动重部署探针（uuid 不变，面板节点自动恢复，名字不对自动改名）
- 密码：`TD_ENV_PASSWORD` secret（创建实例密码，8+ 位大小写数字符号）

## 文件

| 文件 | 作用 |
| --- | --- |
| `td_keepalive.py` | 主脚本（保活/唤醒/重建/部署/面板校正） |
| `td_state.json` | 状态（环境域名、createDate、jupyter token、最近结果），每轮回写 |
| `td_assets/v4proxy.py` | IPv4-only CONNECT 代理（容器 IPv6 全断，agent 走 127.0.0.1:3128） |
| `td_assets/keepalive.html` | 同源保活页（可放浏览器标签持续 ping） |

## secrets

| 名称 | 说明 |
| --- | --- |
| `TD_EMAIL` / `TD_PASSWORD` | Teradata 账号 |
| `TD_ENV_PASSWORD` | 重建环境时设置的实例密码 |
| `NZ_SECRET` | 哪吒面板 agent 密钥 |
| `NZ_PANEL_USER` / `NZ_PANEL_PASS` | 哪吒面板登录（改名兜底） |
| `GH_PAT` | 回写 state / 接力用 PAT |

## 手动触发

Actions → Teradata Keepalive → Run workflow：
- mode 留空 = 正常保活
- mode 填 `dry-run` = 只登录+读状态，不做任何变更
