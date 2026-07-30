# Enzonic API Login Every 2 Days

`enzonic` 分支现在只使用 API Key 登录检查：

- 每 2 天请求一次 `GET /api/client`
- 不再启动浏览器
- 不再随机访问三个文件页面
- 不再随机请求服务器详情或资源接口
- GitHub Actions 每天检查一次是否已经达到两天间隔
