# bkt — 免费服自动续期

目标：某免费 MC 面板（站点常量已 b64 混淆进脚本，公开仓库与 Actions 日志均搜不到）
上的免费服永不掉线。

## 一、面板机制（全部实测，2026-11-11）

| 项 | 值 / 规则 |
|---|---|
| 会话 | better-auth session cookie，**硬过期 7 天，不滑动**（get-session 无 Set-Cookie 回写） |
| 免费服状态 | `GET /api/v1/servers/game/free/status` → `secondsRemaining / canRenew / activeUntil / deleteAfter` |
| 续期窗口 | **到期前 2 小时内**，或过期之后（提前请求返回 `cooldown_active`，文案："only 2 hours before session expiration"） |
| 续期接口 | `POST /api/v1/servers/game/free/renew`，body `{"turnstileToken": "..."}` |
| 单次续期 | **+8 小时**（成功 toast：*"Sesja serwera została przedłużona o kolejne 8 godzin!"*） |
| 验证码 | Cloudflare Turnstile（sitekey 已 b64 内置脚本），前端显式渲染 |
| token | 一次性、300 秒内必须消费 |
| API Key | `bh_pat_` 可读状态/电源，**不能续期**（`apiKeyActionForbidden`：*"Use the Dashboard."*） |
| 过期后 | 停机，数据保留 7 天（`deleteAfter`）再删；窗口在过期后依然打开，可救回 |

### Turnstile 600010 实测死路（别再试）

600010 = **"数学上解对了，但你看起来像机器人"** —— 纯 IP + 浏览器指纹风控：

| 环境 | 结果 |
|---|---|
| Playwright chromium 无头，机房 IP | 600010 |
| Playwright chromium 无头，家宽出口 | 600010 |
| Playwright chromium 有头（Xvfb）+ 真点击，家宽出口 | 600010 |
| Firefox / Cloudflare WARP 叠加 | 600010 |

而**用户本人浏览器 POST 200**（F12 抓包实证）——家用宽带 + 真浏览器可通过。
普通自动化框架（Playwright/CDP 指纹）换什么 IP 都救不了。

## 二、自动续期怎么跑

`renew_bkt.py`（纯标准库，仅 browser 解算器需要额外依赖）：

1. 校验会话 → 读 `free/status`
2. 未到窗口：报告"窗口几点开"，退出（cron 20 分钟一轮足够；`--gate` 模式供 workflow 判断要不要装浏览器）
3. 到窗口：解 Turnstile token → `POST free/renew` → 复查 +8h 是否到账

**解算器四选一**（`BH_SOLVER`）：

| 模式 | 适用 | 说明 |
|---|---|---|
| `capsolver` | 任何网络，CI 全自动 | 配 `BH_CAPSOLVER_KEY`，付费打码 |
| `2captcha` | 同上 | 配 `BH_2CAPTCHA_KEY` |
| `browser` | **家宽出口 + UC 真浏览器**（推荐） | 拉家宽订阅接口 → xray SOCKS5 → undetected-chromedriver 真浏览器解算 → **页面内提交续期**。CI 和本地都能跑 |
| `manual` | 兜底 | 只报告窗口时间，不动手 |

`auto`（默认）：有哪个 key 用哪个 → 都没有就 browser → 再没有就 manual。

### browser 解算器的家宽出口（2026-11-11 上线，实测攻克 600010）

唯一活路是 m-ws 项目验证过的组合：**家宽出口 + undetected-chromedriver +
Xvfb + pyautogui 屏幕级真点击**。browser 模式流程：

1. 拉家宽订阅接口（运行时拉取，节点/隧道轮换自动跟随；地址已 b64 混淆进脚本）
   → 解出 vless 节点；订阅拉不到退回内置兜底节点
2. xray 起 SOCKS5（`127.0.0.1:10808`；本机没有 xray 会自动下载到 `xray_bin/`）
3. curl 验出口 IP（日志只打 `83.168.x.x (PL)` 这种掩码）
4. UC Chrome 挂 SOCKS5 + CDP 注入会话 cookie + 显式渲染 Turnstile
5. pyautogui 屏幕坐标真点勾选框（最多 6 轮）拿 token
6. **在同一浏览器页面里 fetch POST free/renew**（token 绑解算出口 IP，
   必须同出口提交；urllib 直连 POST 有 IP 不一致风险）
7. 直连复查 +8h 是否到账

### 环境变量

```bash
BH_COOKIE=...            # better-auth 会话 cookie 整行（必填，浏览器 F12 抄）
BH_SOLVER=auto|capsolver|2captcha|browser|manual
BH_PROXY=auto|1|0        # auto=尽量走家宽出口（默认）；1=强制失败即退；0=直连
BH_VLESS_URL=...         # 可选，vless:// 节点直链（覆盖订阅）
BH_SUB_URL=...           # 可选，订阅接口（默认用脚本内置 b64 地址）
XRAY_BIN=/path/to/xray   # 可选，缺省自动下载
BH_CAPSOLVER_KEY=...     # 可选
BH_2CAPTCHA_KEY=...      # 可选
```

### 本地跑（Windows/Linux/macOS 均可）

```bash
pip install undetected-chromedriver pyautogui setuptools
# Linux 还要 python-xlib；无显示器环境用 xvfb-run 包一层
BH_COOKIE='...' BH_SOLVER=browser python3 renew_bkt.py
```

机器本身在住宅网络可加 `BH_PROXY=0` 直连（跳过订阅/xray，更快）。
Windows：双击 `renew_bkt_win.bat`。想全自动就在「任务计划程序」里建每小时触发的计划任务。

> **实测记录（2026-11-11）**：
> ① 机房 IP + Playwright —— 小部件挂起/600010，判死；
> ② 家宽出口 + Playwright 无头 —— 600010（指纹问题，IP 救不了）；
> ③ 家宽出口 + Playwright 有头（Xvfb）+ 真点击 —— 仍 600010；
> ④ 家宽出口 + UC 真浏览器 + pyautogui 真点击 —— **token 解出 + HTTP 200 续期成功**，
>    CI 全自动跑通（+6.09h 到账）。

### GitHub Actions（renew_bkt.yml → main 分支）

- cron `8,28,48 * * * *`（每 20 分钟）：先跑 `--gate` 探测窗口，窗口开才装
  xray + Chrome + UC 依赖（省掉 72 次/天 的无效全家桶安装），然后
  `xvfb-run` 内跑 browser 解算；`workflow_dispatch` 可手动指定 solver/force
- secrets：`BH_COOKIE`（必填）；`BH_CAPSOLVER_KEY`/`BH_2CAPTCHA_KEY` 可选
- 脚本从 `bkt` 分支 checkout，状态文件提交回该分支

## 三、运维须知

- **cookie 每 7 天要换一次**：脚本在会话 <48h 时会在日志里喊；到期后
  浏览器登录面板 → F12 → Network → 任意请求 → Request Headers → Cookie，
  把 better-auth session token 整段更新到 secret `BH_COOKIE`
- 续期节奏：窗口是"到期前 2h"，+8h 一轮 —— 每 8 小时点一次即可，
  cron 20 分钟探测保证窗口一开就续
- 过期也别慌：`deleteAfter` 是过期后 7 天，窗口在过期后依然打开，脚本照样能救回来

## 四、分支结构

```
bkt 分支（本分支，单提交干净历史）:
  renew_bkt.py        主脚本
  bkt_state.json      运行状态（CI 自动提交）
  README.md           本文档
main 分支:
  .github/workflows/renew_bkt.yml   定时任务（schedule 只认默认分支）
```

---

## 二、2026-11-13 过期事故复盘 + 改版

### 事故

服务器 11-13 09:33（北京）过期停机约 3 小时。**链路无罪、解算无罪——真凶是 GitHub
schedule 调度饿死**：`8,28,48` 的 20 分钟 cron 实际只送进来 ~4 次/天（全仓 35 个
workflow 抢调度，schedule 事件被成批丢弃；同仓 cocalc 3 次/天、octopus 4 次/天）。
22:53Z 最后一次 gate 还是"未到窗口"，07:33（北京）开窗后 cron 再没来过。

### 实弹验证（11-13 02:12Z）

手动 force 一发即中，**家宽解算器全链路首次实弹成功**：

```
家宽订阅 1 条 VLESS → 出口 83.168.x.x (PL) → undetected-chromedriver (Chrome 156)
→ pyautogui 真点击复选框 ×2 → Turnstile token (len=794) → POST free/renew
→ HTTP 200 {"success":true} → 0h → 8.00h（+8h）
```

注：Playwright 系（含家宽+真点击）实测全 600010；**undetected-chromedriver 才是
活路**——差别在 UC 对自动化指纹的抹除，不在 IP。

### 改版机制（同日上线）

| 件 | 说明 |
|---|---|
| `renew_bkt.py --loop 330` | 长驻守护：每 20 分钟 gate 复查，窗口一开立即解算续期；鉴权死（rc 2/3）才停 |
| workflow 自派发接力 | 每轮结束 `gh workflow run`（**GH_PAT** secret）叫醒下一轮 330min——workflow_dispatch 不受 schedule 饿死影响 |
| schedule `8,28,48` | 保留作兜底（万一接力断链） |
| concurrency `bkt-renew` | 串行不取消，多余触发自动排队合并 |

覆盖账：窗口 = 到期前 2h；循环 20 分钟粒度 → 开窗后 ≤20 分钟续上，**正常零掉线**；
单轮预算 330min < 续期间隔 ~6h，每轮恰好续一次，接力无缝续命。
