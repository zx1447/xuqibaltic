# run_local.ps1 —— 在本机（家庭网络 IP）运行自动续期
# 用法：
#   1) 编辑下面的 $SessionCookie，粘贴浏览器里的 session cookie 值
#   2) 右键用 PowerShell 运行，或用 Windows 任务计划程序每天调用
#
# 说明：BalticHost 面板有反爬防护(M.E.O.W)，会拦截数据中心 IP（GitHub Actions 等），
#       所以必须在你自己的电脑/家庭宽带 IP 上运行。

$ErrorActionPreference = "Stop"

# ===== 需要你填写的部分 =====
$SessionCookie = "在这里粘贴 session cookie 值，形如 .eJx....."
$ServerId      = "04dd7781"
# ============================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# 找 python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "未找到 Python，请先安装 Python 3。"; exit 1 }

# 确保 requests 已安装
& $py -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) { & $py -m pip install requests }

$env:SESSION_COOKIE = $SessionCookie
$env:SERVER_ID      = $ServerId

& $py "$ScriptDir\renew.py"
exit $LASTEXITCODE
