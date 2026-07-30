# Enzonic Two Accounts Every 2 Days

`enzonic` 分支同时维护两个独立账号：

## 账号密码账号

- 使用 `ENZONIC_USER` / `ENZONIC_PASS` 登录网页面板
- 每 2 天从以下三个页面中随机访问一个：
  - `server.properties`
  - `files/versions`
  - `eula.txt`

## API Key 账号

- 使用独立的 `ENZONIC_API_KEY`
- 每 2 天只请求一次 `GET /api/client` 完成 API 登录检查
- 不再随机请求服务器详情或 `resources` 接口
- 不参与三个网页地址的随机访问

两个账号使用各自独立的下次执行时间。
