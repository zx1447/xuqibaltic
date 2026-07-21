# xuqibaltic — 服务器自动续期

给 BalticHost / hostingmitherz 面板的 Minecraft 服务器自动续期。

## 已确认的关键事实

- **续期只在到期前 7 天开放**，每次续期有效期 +30 天。当前到期：`20.08.2026`，续期窗口约 `13.08.2026` 开启。
- **面板有反爬防护（M.E.O.W）**：会拦截数据中心 / 云服务器 IP。
  - ❌ **GitHub 托管 runner（ubuntu-latest）访问会被 403 拦截**，拿不到真实页面 —— 已实测确认。
  - ✅ 你自己的电脑 / 家庭宽带 IP 可以正常访问。
- 因此**推荐用本机定时任务运行**（方案 A）；或用自托管 runner（方案 B）。纯 GitHub 云端不可行。

---

## 方案 A（推荐）：本机 + Windows 任务计划程序

最简单可靠，用你家里的 IP，不会被反爬拦。

1. 安装 Python 3（勾选 Add to PATH）。
2. 下载本仓库到本地。
3. 编辑 `run_local.ps1`，把 `$SessionCookie` 改成你的 session cookie 值（获取方式见下）。
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
3. 配置下面的 Secret。

### Secret 配置（方案 B 用）

**Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名称       | 是否必填 | 值 |
|-------------------|----------|----|
| `SESSION_COOKIE`  | 必填     | 浏览器里的 `session` cookie 值（`.eJx....`，带不带 `session=` 前缀都行） |
| `SERVER_ID`       | 可选     | 服务器 ID，默认 `04dd7781` |

> 本次已通过 API 帮你写入了 `SESSION_COOKIE`，但**它随时可能过期**（见下）。

---

## 怎么获取 `SESSION_COOKIE`

1. 浏览器登录面板，打开 `https://blinky.baltichost.de/manage/server/04dd7781/renewal`。
2. F12 → Application/应用 → Cookies → 选 `blinky.baltichost.de`。
3. 复制名为 `session` 那条的 **Value**。

## ⚠️ 关于 cookie 过期（重要）

- cookie 是登录凭证，会过期。**续期窗口 8 月中旬才开，现在的 cookie 很可能撑不到那时候。**
- **建议：临近 8 月 10 号左右，重新登录复制一份新 cookie 再填进去**，可靠性最高。
- 脚本会在 cookie 失效时明确报错（找不到 csrf-token / 被反爬拦截），方便你发现并更新。

## 退出码

- `0`：续期成功，或"尚未到续期窗口"（正常空跑）。
- `1`：被反爬拦截 / cookie 失效 / 接口异常 —— 需要处理（多半是换 cookie 或换非数据中心 IP）。
