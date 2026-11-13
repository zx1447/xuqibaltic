# Ajolote Hosting 免费服自动续期（axolotl）

站点：https://ajolote.lat （墨西哥 Minecraft 主机，Next.js + Pterodactyl + NextAuth）
服务器：`cmwem4vv00002w8fo5kvc11pf` — **Ajolote Free - zxy1715x_de48e7**（Paper，1G RAM / 10G 盘，`mundo-a6fe3819.ajolote.lat`）
账号：zxy1715x@gmail.com（显示名 maike，pterodactylUserId 449）

## 续期规则（2026-11-13 实测）

- 免费计划**没有 Stripe 订阅**，续期不花钱：代价是看一个 **~40 秒的站内广告**（`provider: "placeholder"`，自有视频，非 Google AdSense，无需真人点击）。
- **每次续期 +7 天**（面板文案写"一个月"，接口实测 `expiresAt = now + 7d`）。
- **每次开机也会自动续期**（"Cada vez que lo enciendes también se renueva"）——第二条免费通道。
- 到期后**服务器停机、无法启动**；再拖 **30 天（deleteAt）连世界一起删除**。

## 接口流程

| 步骤 | 请求 |
|---|---|
| 1. csrf | `GET /api/auth/csrf` → `{csrfToken}` |
| 2. 登录 | `POST /api/auth/callback/credentials`（form: `csrfToken`/`email`/`password`/`json=true`）→ 302 + `__Secure-authjs.session-token`（30 天） |
| 3. 查状态 | `GET /api/servers/{id}/renew` → `{free, renewedAt, expiresAt, expired, deleteAt}` |
| 4. 开广告 | `POST /api/servers/{id}/renew/start` → `{token, requiredMs≈40411, commercial:{url,mediaType,title,link,durationMs}}` |
| 5. 等满 | sleep `requiredMs/1000 + 3`（不足会被服务端拒） |
| 6. 落续期 | `POST /api/servers/{id}/renew/complete` body `{"token":"…"}` → `{"ok":true,"expiresAt":…}` |

错误码：`409` = 已在续期/刚续过（视为成功）；`rate_limited` = 限频，下一轮再试；`already_running` 同上。

## 自动化

- 脚本 `renew_ajolote.py`（纯 stdlib）：剩余 **< 3 天**自动续，`--force` 强制续一次验证，`--loop 330` 长驻守护（30 分钟一轮）。
- workflow `.github/workflows/ajolote_renew.yml`（**main** 分支）：`ref: axolotl` 检出脚本；schedule `47 */6 * * *` 兜底 + GH_PAT 自派发接力（330min）。
- secrets：`AJOLOTE_EMAIL` / `AJOLOTE_PASSWORD`。
- session token 缓存在 `ajolote_state.json`（workflow 自动回写），失效则自动重登。
- CI 验证：run **43384879276** ✅（`--force` 实弹续期，expiresAt 推进到 2026-11-20 07:19:56Z）；守夜 43384988174 接管。

## 手动操作

```bash
AJOLOTE_EMAIL=… AJOLOTE_PASSWORD=… python3 renew_ajolote.py          # 巡检（临期才续）
AJOLOTE_EMAIL=… AJOLOTE_PASSWORD=… python3 renew_ajolote.py --force  # 立刻续一次
```
