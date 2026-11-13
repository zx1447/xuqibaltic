# GemHost.pl 免费服守护

对 `panel.gemhost.pl`（自研面板套 Pterodactyl）免费 Minecraft 服的**停机自动拉起**。

## 机制（2026-11-13 逆向实测）

| 项 | 结论 |
|---|---|
| 登录 | `POST /api/auth/login {"email","password"}` → Cookie `gemhost_access`(JWT **7 天**) + `gemhost_csrf` |
| 清单 | `GET /api/servers/list` |
| 状态 | `GET /api/servers/<id>/status` → `status` + `free_session{active,ends_at,remaining_ms,max_runtime_minutes:360}` |
| 启停 | `POST /api/servers/<id>/power`，body **`{"action":"start\|stop\|restart\|kill"}`**（`signal` 是错的） |
| 会话 | **免费服 = 6 小时/会话**，耗尽自动停机；**拉起即开新 6h 会话** |
| extend-session | `POST /api/servers/<id>/free/extend-session` 需 **captcha**，弃用（用不着） |
| 变更请求 | 带 `X-CSRF-Token: <gemhost_csrf>` |

## 服务器

| id | 名称 | 套餐 | 节点 |
|---|---|---|---|
| 8331623 | nice | Free 4G/10G/150% | FREE1 |

## 守护逻辑

每 15 分钟巡检一次：

- `running / starting` → 不动
- `stopping / installing / restoring` → 本轮放过，下轮复查
- 挂起（suspended）→ 只报告
- 其余（offline 等）→ **`{"action":"start"}` 拉起**，复查确认拿到新会话

cookie 缓存在 `gemhost_state.json`（7 天内免重登录，变化时 workflow 回写分支）。

## 使用

```bash
export GEMHOST_EMAIL='you@example.com'
export GEMHOST_PASSWORD='...'
python3 gemhost_keepalive.py
```

CI（main 分支 `gemhost_keepalive.yml`）：cron 每 15 分钟（`3,18,33,48 * * * *`）+ 手动 dispatch。
Secrets：`GEMHOST_EMAIL` / `GEMHOST_PASSWORD`。
