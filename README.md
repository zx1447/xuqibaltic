# xuqibaltic — 服务器自动续期

给 BalticHost / hostingmitherz 面板的 Minecraft 服务器自动续期。

## 已确认的关键事实

- **续期只在到期前 7 天开放**，每次续期有效期 +30 天。当前到期：`20.08.2026`，续期窗口约 `13.08.2026` 开启。
- **面板有反爬防护盾（M.E.O.W）**：裸 `requests` 从数据中心 IP（如 GitHub 云端 runner）会被直接拦成一张反爬 HTML 页，连登录请求都发不出去。
- **本脚本用 SeleniumBase 真浏览器（uc / undetected-chromedriver 模式）驱动**：能像真人浏览器一样通过防护盾 / Turnstile，从而完成登录与续期。**这样 GitHub 托管 runner 也能跑**（真浏览器指纹被放行）。若极端情况下数据中心 IP 被硬封，再退回到本机 / 自托管 runner。

---

## 鉴权方式（推荐方式 1：账号密码）

脚本用 SeleniumBase 打开登录页，自动填 `#username` / `#password` 并点 `#loginBtn` 登录。
你**完全不用管 cookie**——每次运行都重新登录，cookie 永不过期。

需要两个 Secret（Settings → Secrets and variables → Actions → New repository secret）：

| Secret 名称    | 是否必填 | 值 |
|----------------|----------|----|
| `BLINKY_USER`  | 必填     | 面板登录用户名 |
| `BLINKY_PASS`  | 必填     | 面板登录密码 |

（旧版 `SESSION_COOKIE` 静态 cookie 方式仍支持：填入后脚本会注入 cookie 跳过登录，但 cookie 会过期，不推荐。）

可选通知 Secret（仿参考项目用 Telegram 推送结果）：

| Secret 名称    | 说明 |
|----------------|------|
| `TG_BOT_TOKEN` | Telegram bot token（不填则不发通知） |
| `TG_CHAT_ID`   | 接收通知的 chat id |

---

## 在 GitHub Actions 上跑（推荐，最省事）

1. 配好上面的 Secret（至少 `BLINKY_USER` / `BLINKY_PASS`）。
2. workflow 已设置：每天 UTC 03:00 / 15:00 自动跑（`cron`），也可在 Actions 页面手动 **Run workflow**。
3. 脚本用真浏览器自动登录 → 进入续期页 → 窗口内点击 `#renewalForm` 提交、窗口外正常跳过。

> 若日志出现「仍被反爬盾拦截（M.E.O.W）」，说明该数据中心 IP 被硬封，请改用下方方案 A 或 B。
> 此时把 `renew.yml` 的 `runs-on: ubuntu-latest` 改成 `runs-on: [self-hosted]`。

## 方案 A：本机 + Windows 任务计划程序

用你家里 IP，最稳。

1. 装 Python 3（勾 Add to PATH），`pip install seleniumbase`，并 `python -m seleniumbase install chromedriver`。
2. 下载本仓库；在 `run_local.ps1` 填 `$BlinkyUser` / `$BlinkyPass`。
3. 手动跑一次验证：`powershell -ExecutionPolicy Bypass -File run_local.ps1`。
4. 任务计划程序 → 创建基本任务 → 每天 → 启动程序 `powershell.exe`，
   参数 `-ExecutionPolicy Bypass -File "C:\路径\run_local.ps1"`。

## 方案 B：GitHub Actions + 自托管 runner

runner 跑在你电脑上（用你家 IP）。仓库 Settings → Actions → Runners 安装并启动 runner，
再把 `runs-on` 改成 `self-hosted`。

---

## 退出码

- `0`：续期成功，或"尚未到续期窗口"（正常空跑）。
- `1`：被反爬硬拦 / 登录失败 / 表单未找到 / 续期失败 —— 需要处理。

## 参考

脚本借鉴了同类型面板（aida0710.work）的 SeleniumBase + `uc` 真浏览器方案：
用 `undetected-chromedriver` 过 Cloudflare Turnstile / 反爬盾，而不是裸 HTTP 请求。
