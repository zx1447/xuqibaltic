# run_local.ps1 —— 在本机（家庭网络 IP）运行自动续期
# 用法：
#   - 方式 1（推荐，cookie 永不过期）：填 $BlinkyUser / $BlinkyPass，留空 $SessionCookie
#   - 方式 2（旧，需手动更新）：填 $SessionCookie，留空用户名密码
#   然后右键用 PowerShell 运行，或用 Windows 任务计划程序每天调用
#
# 说明：BalticHost 面板有反爬防护(M.E.O.W)，会拦截数据中心 IP（GitHub Actions 等），
#       所以必须在你自己的电脑/家庭宽带 IP 上运行。

$ErrorActionPreference = "Stop"

# ===== 需要你填写的部分 =====
# 方式 1：账号密码自动登录（推荐）
$BlinkyUser = "在这里填面板登录用户名"
$BlinkyPass = "在这里填面板登录密码"

# 方式 2：静态 session cookie（留空上面两项时启用）
$SessionCookie = ""

$ServerId      = "04dd7781"
# ============================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# 找 python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "未找到 Python，请先安装 Python 3。"; exit 1 }

# 确保依赖已安装
& $py -c "import requests, Crypto" 2>$null
if ($LASTEXITCODE -ne 0) { & $py -m pip install requests pycryptodome }

if ($BlinkyUser -and $BlinkyPass) {
    $env:BLINKY_USER = $BlinkyUser
    $env:BLINKY_PASS = $BlinkyPass
} elseif ($SessionCookie) {
    $env:SESSION_COOKIE = $SessionCookie
} else {
    Write-Error "请至少填写一种鉴权方式：BlinkyUser/BlinkyPass 或 SessionCookie。"
    exit 1
}
$env:SERVER_ID = $ServerId

& $py "$ScriptDir\renew.py"
exit $LASTEXITCODE
