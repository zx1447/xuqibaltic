# VolyxHost AFK Earn

干净独立的 `volyxhost` 分支，自动完成：

1. 使用账号密码通过 Livewire 登录 VolyxHost。
2. 打开 `https://dash.volyxhost.com/coins/earn`。
3. 切换到 AFK Earning 页面。
4. 点击 `Start AFK Earn`。
5. 每 61 秒调用官方 `tickAfk`，保持 AFK Session 并累计金币。
6. 遇到页面的数学活跃验证时提交正确答案。
7. 本轮结束时点击 `Stop Earning`，然后自动启动下一轮。

登录 Cookie 使用 Fernet 加密保存在状态文件中，账号密码只存放于 GitHub Secrets。
