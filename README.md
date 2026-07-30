# ZTO Server Auto Renew

`zto` 分支用于 ZTO 服务器自动登录和随机间隔续期。

- 环境变量：`ZTO_USER`、`ZTO_PASS`、`ZTO_COOKIE`、`ZTO_SERVER_ID`
- 主脚本：`renew_zto.py`
- 状态文件：`zto_state.json`
- Workflow：`renew_zto.yml`
- 当前自动定时运行仍保持暂停，只保留手动触发。
