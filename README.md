# Hostrix — 免费服务器自动续期（账密登录 + 精准定时）

`https://hostrix.website` · 服务器 `cd86ccde4e34`（`nezha.hostrix.website`，Node.js，1000MB / 50% CPU / 1GB）

> 不复用 cookie：每轮直接用账号密码登录换新会话，签名会话过期、cookie 被顶掉都不影响。

## 一、登录

```
GET  /login                                  → 200，取 <input name="csrf_token">
POST /login   csrf_token & username & password → 302 /dashboard  （成功）
                                                 200 原页 + flash（失败）
```

- **没有 Turnstile / hCaptcha / reCAPTCHA**（与 Octagon 完全不同，可以纯脚本跑）
- 失败原文：`Wrong username or password.`（HTML 里是 `<div class="flash error">`）
- 登录后用 `Set-Cookie: session=…` 走完全部页面，无需保留

## 二、续期

| 项 | 值 |
|---|---|
| 接口 | `POST /servers/cd86ccde4e34/renew` |
| 字段 | `csrf_token`（服务器页隐藏 input，长期不变） |
| 单次收益 | **+48h（2026-11-10 实测）** |
| 冷却 | **12 小时（当前服务端提示；脚本会动态读取）** |
| 存量上限 | 暂未确认；本次续期后服务端显示剩余 3d 9h（81h） |
| 归零后果 | `Out of time = paused`（暂停，不删除） |
| 其他免费途径 | 无。wallet 需充值、gifts 需兑换码、**没有挂机赚时长** |

### 服务端提示（实测，判定的唯一可靠依据）

| 场景 | flash 原文 | 判定 |
|---|---|---|
| 冷却中点 | `You can add more time in 11h.` | **失败**，静默不加时间 |
| 成功 | 形如 added / renewed（未观察到，靠时长增长 + 正则双保险） | 成功 |

⚠️ 这里踩过坑：冷却期点击返回的是 `302` 而不是错误，**时长一点都不变但页面照常刷新**。早期版本用"时长没变但按钮变灰 = 已封顶成功"来判定，会把 `You can add more time in 11h.` 误判成续期成功。现在一律**以 flash 文案定生死**，时长增长作为主信号。

## 三、精准定时

页面只给整点小时（`⏰ 46h left`、`+24h in 11h`），翻遍 `panel.js` 也没有秒级时间源 —— 所以精确边界由脚本自己算：

1. 每次真实点击时记录 `last_click_epoch`（秒）
2. `next_allowed = last_click_epoch + 冷却(11h)`
3. 本次 job 里若 `next_allowed − now ≤ HR_WAIT_MAX(1100s)`：直接 `sleep` 到边界 +5 秒再点 → **秒级命中**
4. 否则退出，由每 20 分钟的 cron 接力；cron 每次先看按钮状态，**页面一显示可点就立刻点**（不依赖估算）
5. 首次运行没有点击记录时取保守上界 `now + 11h`，宁晚不早

cron：`3,23,43 * * * *`（20 分钟），`WAIT_MAX=1100s` → 按 `hostrix_state.json` 中服务端提示给出的冷却时间等待；其余情况由 cron 接力。

## 四、运行

```bash
HOSTRIX_USER='…' HOSTRIX_PASS='…' python3 renew_hostrix.py          # 正常一轮
HOSTRIX_USER='…' HOSTRIX_PASS='…' HR_FORCE=1 python3 renew_hostrix.py  # 无视冷却强点（调试）
```

| 退出码 | 含义 |
|---|---|
| 0 | 正常（续期成功 / 冷却中不点 / 交给 cron） |
| 2 | 缺 `HOSTRIX_USER` / `HOSTRIX_PASS` |
| 3 | 登录失败（含服务端原文） |
| 4 | 服务器页解析失败 |
| 5 | 点了但服务端拒绝（时长未变，flash 为冷却文案） |

`hostrix_state.json` 记录：`hours_left`、`cooldown_h`、`last_click_epoch`、`next_allowed_epoch`、`last_action`、`last_success_epoch`、`last_error`。

## 五、CI

`.github/workflows/renew_hostrix.yml`（**必须在 `main`**，GitHub 只认默认分支的 schedule / dispatch）：

- `ref: hostrix` checkout 脚本，跑完把 `hostrix_state.json` 提交回 `hostrix`
- secret：`HOSTRIX_USER`、`HOSTRIX_PASS`
- 日志里账号密码自动脱敏（`***`）

实测通过：`42350048416`（登录 → 冷却中 → 不动 → 状态提交）。

## 六、时间线（北京时间）

| 时刻 | 事件 |
|---|---|
| 11-07 15:02 | cookie 版首次验证通过 |
| 11-07 15:09 | 切账密版，run `42350048416` 通过 |
| 11-08 01:52 ~ 02:52 | 冷却到期，cron 巡检命中即点（第一次真实生效） |
| 之后 | 每约 11h 一次，稳定维持在 48h 上限 |
