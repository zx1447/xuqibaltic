# xuqibaltic — 服务器自动续期

给 BalticHost / hostingmitherz 面板的 Minecraft 服务器自动续期。

## 已确认的关键事实

- **续期只在到期前 7 天开放**，每次续期有效期 +30 天。当前到期：`20.08.2026`，续期窗口约 `13.08.2026` 开启。
- **面板有反爬防护（M.E.O.W）**：会拦截数据中心 / 云服务器 IP。
  - ❌ **GitHub 托管 runner（ubuntu-latest）访问会被 403 拦截**，拿不到真实页面 —— 已实测确认。
  - ✅ 你自己的电脑 / 家庭宽带 IP 可以正常访问。
- 因此**推荐用本机定时任务运行**（方案 A）；或用自托管 runner（方案 B）。纯 GitHub 云端不可行。

---

## 鉴权方式（二选一）

脚本支持两种登录方式，**推荐方式 1**，可做到 cookie 永不过期。

### 方式 1（推荐）：账号密码自动登录 —— cookie 自动刷新

脚本每次运行先用 **RSA 加密密码**登录 `/auth/validator` 拿新鲜 session cookie，再续期。
你**完全不用再管 cookie**，只要在 Secret 里填好账号密码即可。

> 面板登录页用 JSEncrypt 对密码做 RSA 加密（公钥内嵌在页面），脚本在本地用
> `pycryptodome` 同样加密后才发出，明文密码不会出现在网络请求里。

需要两个 Secret：

| Secret 名称    | 是否必填 | 值 |
|----------------|----------|----|
| `BLINKY_USER`  | 方式1必填 | 面板登录用户名 |
| `BLINKY_PASS`  | 方式1必填 | 面板登录密码（明文存 Secret，仅本地加密后发出） |

只要设了这两个，脚本就会走登录流程，**忽略 `SESSION_COOKIE`**。

### 方式 2（旧）：静态 session cookie —— 需手动更新

| Secret 名称      | 是否必填 | 值 |
|------------------|----------|----|
| `SESSION_COOKIE` | 方式2必填 | 浏览器里的 `session` cookie 值（`.eJx....`，带不带 `session=` 前缀都行） |

> cookie 会过期，撑不到续期窗口就得重新复制（见下方「cookie 过期」）。

两种方式的 Secret 都存在 **Settings → Secrets and variables → Actions → New repository secret**。
本次已通过 API 写入过 `SESSION_COOKIE`，但**它随时可能过期**，建议改用方式 1。

### ❓ 为什么不用 Discord token？

面板确实有 Discord 登录按钮（`/auth/discord/login`），但那是 **OAuth 重定向流程**
（跳到 discord.com 让用户点「同意」再跳回面板），不是「贴一个 token」就能用的接口。
光给一个 Discord 用户 token，脚本无法完成登录握手，因此**不可用于自动化**。
要彻底免维护，请用上面的「方式 1 账号密码」。

---

## 方案 A（推荐）：本机 + Windows 任务计划程序

最简单可靠，用你家里的 IP，不会被反爬拦。

1. 安装 Python 3（勾选 Add to PATH），并 `pip install requests pycryptodome`。
2. 下载本仓库到本地。
3. 配置鉴权：
   - **方式 1（推荐）**：在 `run_local.ps1` 里填 `$BlinkyUser` / `$BlinkyPass`，留空 `$SessionCookie`。
   - 方式 2：把 `$SessionCookie` 改成你的 session cookie 值（获取方式见下），`$BlinkyUser`/`$BlinkyPass` 留空。
4. 先手动跑一次验证：右键 `run_local.ps1` → 用 PowerShell 运行。
5. 用任务计划程序每天自动跑：
   - 打开「任务计划程序」→ 创建基本任务 → 触发器选「每天」。
   - 操作选「启动程序」，程序填 `powershell.exe`，
     参数填：`-ExecutionPolicy Bypass -File "C:\路径\run_local.ps1"`。
   - 勾选「不管用户是否登录都要运行」。

## 方案 B：GitHub Actions + 自托管 runner

保留云端 workflow，但 runner 跑在你自己电脑上（用你的 IP 绕过反爬）。

1. 仓库 **Settings → Actions → Runners → New self-hosted runner**，按指引在你电脑上安装并启动 runner。
2. 把 `.github/workflows/renew.yml` 里的 `runs-on: ubuntu-latest` 改成 `runs-on: [self-hosted]`。
3. 配置上面的 Secret（方式 1 或方式 2）。

> 即便用自托管 runner，若电脑挂着代理 / 走了数据中心出口 IP，仍可能被反爬拦。

---

## 怎么获取 `SESSION_COOKIE`（仅方式 2 需要）

1. 浏览器登录面板，打开 `https://blinky.baltichost.de/manage/server/04dd7781/renewal`。
2. F12 → Application/应用 → Cookies → 选 `blinky.baltichost.de`。
3. 复制名为 `session` 那条的 **Value**。

## ⚠️ 关于 cookie 过期（方式 2 才需关心）

- cookie 是登录凭证，会过期。**续期窗口 8 月中旬才开，现在的 cookie 很可能撑不到那时候。**
- 用「方式 1 账号密码」可彻底规避此问题，无需手动维护。
- 脚本会在 cookie 失效时明确报错（找不到 csrf-token / 被反爬拦截），方便你发现并更新。

## 退出码

- `0`：续期成功，或"尚未到续期窗口"（正常空跑）。
- `1`：被反爬拦截 / 登录失败 / cookie 失效 / 接口异常 —— 需要处理。
