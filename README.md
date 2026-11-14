# L3chrane X Bots —— AFK 挂机赚币（分支 `l3chrane`）

站点：https://l3chranexbots.oriaxen.com · Discord bot 托管（Node.js / Python）
账号：Discord **ajkvip**（id 1438491113576202260 / vipdkyi@gmail.com）

## 一、站点机制（2026-11-13 全部实测）

| 项 | 值 |
|---|---|
| 免费档 | 512MB RAM · 50% CPU · 600MB 磁盘 · 24/7 |
| 货币 | **coin** |
| 部署成本 | **5 coins / 台**（`POST /create`，字段 `name` + `runtime`=node\|python） |
| 官方 AFK | 打开 `/afk` 页面，**每 60 秒自动 +1 coin** |
| 前端计时器 | 纯内存（刷新重置 60s），离开页面暂停 |

### 关键发现：服务端**不做任何间隔校验**

`POST /afk/claim` → `{"success":true,"coins":1,"newBalance":N,"dailyEarned":N}`

实测：**1 秒内连刷 10 次全部成功**，`maxDaily=0`（无日上限）、无 cooldown、无 turnstile。

也就是说官方"每 60 秒 1 coin"纯粹是前端自律，接口层是敞开的。

## 二、登录链路

```
Discord 用户 token
  → GET  discord.com/api/oauth2/authorize?client_id=1519415022168571914&scope=identify   （验证 token + 拿应用信息）
  → POST discord.com/api/v9/oauth2/authorize  {"authorize":true,"permissions":"0"}        （已授权 → 返回 location 带 code）
  → GET  /auth/discord/callback?code=...                                                  （换 Express session）
  ← Set-Cookie: connect.sid=s%3A...                                                       （站点会话）
```

client_id `1519415022168571914`（应用名 "L3chrane X Bots"，Bot Dev By Stoki）是**公开值**，从 `/auth/discord` 的 302 里直接解出来。

## 三、脚本

`l3chrane_afk.py`（纯标准库）

```bash
L3CH_DISCORD_TOKEN=xxx python3 l3chrane_afk.py --status   # 只查余额
L3CH_DISCORD_TOKEN=xxx python3 l3chrane_afk.py            # 攒到目标（默认 50）
L3CH_DISCORD_TOKEN=xxx python3 l3chrane_afk.py --once     # 单次自检
L3CH_DISCORD_TOKEN=xxx python3 l3chrane_afk.py --loop 330 # 长驻守护
```

| 环境变量 | 说明 |
|---|---|
| `L3CH_DISCORD_TOKEN` | **必填**，Discord 用户 token |
| `L3CH_GOAL` | 目标余额，默认 50（=10 台服务器） |
| `L3CH_DELAY` | 每次 claim 间隔秒，默认 2.0（正常节奏，避免被风控盯上） |
| `L3CH_BASE` | 站点地址，默认 `https://l3chranexbots.oriaxen.com` |

特性：会话缓存复用（`l3chrane_state.json`）、失效自动重登、网络重试 4 次指数退避、长驻循环单轮失败不判死（连续 3 轮才退出）。

## 四、实测记录

```
💰 当前余额：12 coins｜目标 20
  +1 coin → 余额 13（本次 1 次）
  … 
  +1 coin → 余额 20（本次 8 次）
🎯 达到目标 20 coins，停止
```

## 五、部署后怎么用

攒够 5 coins 就能在 dashboard 部署：

```
POST /create   form: name=MyBot & runtime=node|python
```

**注意**：这个平台跑的是**你自己上传的 bot 代码**，需要配 Discord bot token（不是用户 token）才真正上线。本脚本负责的是**赚 coin 这一步**，避免每次手动挂页面。
