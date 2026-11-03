# Siam 在本机跑（唯一能过 Cloudflare 的方式）

`my.siam-node.cloud` 对数据中心 IP 一律下发 Cloudflare JS 挑战（`cf-mitigated: challenge`），
GitHub Actions runner 无论 headless 还是有头（xvfb）+ 反自动化指纹都过不去——实测连续 45s
拿不到 `cf_clearance`。所以 GitHub Actions 这条路只能如实报错变红，签到必须在本机（住宅 IP + 真实浏览器指纹）跑。

## 一、需要的东西

- Node.js 18+（`node -v` 能出版本即可）
- 你的 Discord token（`ajkvip`，已经给过）
- 可选：`cf_clearance`（一般不需要，本机浏览器自己会过）

## 二、一次性准备

```bash
mkdir -p ~/siam && cd ~/siam
curl -fsSLO https://raw.githubusercontent.com/zx1447/xuqibaltic/siam/renew_siam.js
npm init -y >/dev/null
npm install playwright
npx playwright install chromium        # 第一次要下载，约 190MB；之后不用再下
```

## 三、跑一次（Windows 用 PowerShell / macOS、Linux 用 bash）

```bash
SIAM_DISCORD_TOKEN='你的 discord token' \
SIAM_HEADFUL=1 \
SIAM_CHECKIN_TARGET=6 \
SIAM_CHECKIN_INTERVAL_SECONDS=14 \
node renew_siam.js
```

Windows PowerShell：

```powershell
$env:SIAM_DISCORD_TOKEN='你的 discord token'
$env:SIAM_HEADFUL='1'
node renew_siam.js
```

`SIAM_HEADFUL=1` 会弹出真实浏览器窗口，别关它，让挑战自己过（几秒）。

## 四、你要看的输出长这样

```
[siam] === Siam check-in start (headful=true) ===
[siam] 🎫 Discord OAuth 登录...
[siam] ✅ Discord code 获取成功
[siam] ✅ 登录成功, URL: https://my.siam-node.cloud/?p=dashboard
[siam] 💰 签到前总积分: 315 ฿
[siam] 🖱️ 每日签到 #1/6...
[siam] 📡 {"status":"success","amount":5,"balance":320,"remaining":5,...}
[siam] ✅ 本次 +5 ฿ | 今日累计 +5 ฿ | 总积分 320 ฿ | 剩余次数 5
...
[siam] 📊 今日汇总: 成功 6 次, 共 +30 ฿, 当前总积分 345 ฿
```

每次签到都有一行，**`总积分` 就是签到之后的最新总额**。
如果接口不返回 `balance`，脚本会自动去钱包页（`?p=topup`）把 `N ฿` 抓回来。

## 五、每天都在同一时间自动跑（可选）

macOS / Linux 用 cron：

```bash
crontab -e
# 每天 08:05
5 8 * * * cd $HOME/siam && SIAM_DISCORD_TOKEN='...' SIAM_HEADFUL=0 SIAM_CHECKIN_TARGET=6 node renew_siam.js >> run.log 2>&1
```

Windows 用「任务计划程序」新建每日任务，程序填 `node`，参数填 `renew_siam.js`，起始于填脚本目录，
环境变量在同一目录建个 `run.cmd`：

```bat
@echo off
set SIAM_DISCORD_TOKEN=你的 discord token
set SIAM_HEADFUL=1
cd /d %~dp0
node renew_siam.js >> run.log 2>&1
```

> 在这台机器上跑过一次之后，本机浏览器会留下 `cf_clearance`，后面几天往往不用再等挑战。

## 六、GitHub Actions 那边现在什么状态

- workflow 还在，每天 08:00（北京时间）跑一次
- 会在日志里明确写出 `⛔ 失败原因: Cloudflare 挑战拦住了 OAuth callback...`
- 并以 **失败** 结束（exit 1），不再假装成功
- 如果想彻底省掉每天一次红色失败，告诉我，我把 `schedule` 关掉只保留手动触发
