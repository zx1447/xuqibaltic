# xuqibaltic — 服务器自动续期

用 GitHub Actions 定时给 BalticHost / hostingmitherz 面板的服务器自动续期。

- 面板只在 **到期前 7 天** 才允许续期，其余时间脚本会正常空跑（不会报错）。
- 每次续期后有效期 +30 天。
- 定时：每天两次（UTC 03:00 / 15:00，即北京时间 11:00 / 23:00），也可在 Actions 页手动触发。

## 一、配置 Secret（必须）

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称       | 是否必填 | 值 |
|-------------------|----------|----|
| `SESSION_COOKIE`  | 必填     | 浏览器里的 `session` cookie，整段形如 `session=.eJx....` （只填值 `.eJx....` 也可以） |
| `SERVER_ID`       | 可选     | 服务器 ID，默认 `04dd7781`。用默认值可不加 |

### 怎么拿 `SESSION_COOKIE`

1. 浏览器登录面板，打开续期页 `https://blinky.baltichost.de/manage/server/04dd7781/renewal`。
2. F12 → Application/应用 → Cookies → 选中 `blinky.baltichost.de`。
3. 复制名为 `session` 的那条 cookie 的 **Value**，填进 `SESSION_COOKIE`。

> ⚠️ **cookie 会过期**：这是登录态凭证，一旦过期或你在别处退出登录，自动续期就会失败。
> 届时 Actions 会报错（红叉），你重新按上面步骤复制新的 `session` cookie 更新这个 secret 即可。

## 二、开启定时任务

1. 仓库 **Settings → Actions → General**，确保 Actions 已启用。
2. 打开 **Actions** 标签页，如提示则点 "I understand my workflows, enable them"。
3. 可先手动跑一次：Actions → **Auto Renew Server** → **Run workflow** 验证配置是否正确。

## 三、本地测试（可选）

```bash
pip install requests
export SESSION_COOKIE='session=.eJx....'
export SERVER_ID='04dd7781'
python renew.py
```

## 退出码含义

- `0`：续期成功，或"尚未到续期窗口"（正常空跑）。
- `1`：cookie 失效 / 找不到 CSRF / 接口异常 —— 需要你更新 `SESSION_COOKIE`。
