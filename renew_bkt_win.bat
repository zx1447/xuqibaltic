@echo off
chcp 65001 >nul
REM ============================================================
REM  bkt 免费服续期 - Windows 一键脚本
REM  前置：装 Python 3（python.org 勾选 Add to PATH）
REM        然后执行一次：
REM          pip install undetected-chromedriver pyautogui setuptools
REM        并装好 Chrome 浏览器（脚本自动下载匹配的 chromedriver）
REM  说明：
REM   · 默认 BH_PROXY=auto：会拉家宽订阅接口起 xray 出口再解 Turnstile，
REM     xray 缺失会自动下载到脚本目录（xray_bin\xray.exe）
REM   · 机器本身在住宅网络时可 set BH_PROXY=0 直连，更快
REM  用法：把下面 BH_COOKIE 的值换成你浏览器里的最新值，
REM        双击本文件即可。建议任务计划程序每小时跑一次。
REM ============================================================
cd /d %~dp0

REM ↓↓↓ 粘贴你的会话 cookie（浏览器 F12 → Network → 任意请求 → Request Headers → Cookie）↓↓↓
set BH_COOKIE=__Secure-better-auth.session_token=在这里粘贴你的token

set BH_SOLVER=browser
python renew_bkt.py
echo.
echo 退出码 %errorlevel% （0=成功/未到窗口 4=解算失败 5=续期被拒 7=家宽出口不可用）
pause
