#!/usr/bin/env node
/**
 * Octagon Circle 免费服自动续期（替代已下线的 AFK 挂机）
 *
 * 机制（2026-11-13 实测）：
 *   设置  GET  /api/system/settings            → server_renew_cost=120 / server_renew_days=3 / server_renew_enabled
 *   会话  GET  /api/user/session               → user_info.credits（余额）
 *   清单  GET  /api/user/session/servers
 *   详情  GET  /api/user/server/<id>           → server.mythicaldash.expires_at
 *   续期  POST /api/user/server/<id>/renew  body {"cost":120,"days":3}
 *         → expires_at = now + 3 天（SET 语义，不叠加）；到期自动挂起、续期自动解挂
 *         → 服务端按 settings 原价扣币（前端"过期 ×1.5"只是展示价，实扣 120）
 *   登录  Discord OAuth（站内账密要 Turnstile，走 Discord 省事）：
 *         GET  {BASE}/api/user/auth/callback/discord/login           → 302 discord.com/api/oauth2/authorize?client_id=1568690847879405668...
 *         GET  discord authorize（Authorization: <DISCORD_TOKEN>）    → application 信息
 *         POST discord authorize  {"permissions":"0","authorize":true} → {location: "...?code=..."}
 *         GET  {BASE}/api/user/auth/callback/discord/login?code=...   → Set-Cookie user_token
 *   会话  user_token 30 分钟滑动过期（每次响应 Set-Cookie 回写）→ cookie 罐延续；失效则 Discord 重登
 *
 * 环境变量：
 *   OCTAGON_COOKIE           可选  会话 cookie（user_token=...，滑动缓存）
 *   OCTAGON_DISCORD_TOKEN    Discord 用户 token（cookie 失效时自动重登；也可作唯一凭据）
 *   OCTAGON_RENEW_HOURS      续期阈值：剩余 < N 小时才续（默认 24）
 *   OCTAGON_BASE             默认 https://client.octagoncircle.org
 */
const BASE = (process.env.OCTAGON_BASE || 'https://client.octagoncircle.org').replace(/\/+$/, '');
const DISCORD_TOKEN = process.env.OCTAGON_DISCORD_TOKEN || '';
const RENEW_HOURS = Number(process.env.OCTAGON_RENEW_HOURS || 24);
const OUT_FILE = process.env.GITHUB_OUTPUT || '';
const SUMMARY = process.env.GITHUB_STEP_SUMMARY || '';

const log = (...a) => console.log(new Date().toISOString().replace('T', ' ').slice(0, 19), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- Cookie 罐子（滑动会话延续） ---------- */
const jar = new Map();
function loadCookie(str) {
  for (const part of (str || '').split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    const k = part.slice(0, i).trim(), v = part.slice(i + 1).trim();
    if (k) jar.set(k, v);
  }
}
const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join('; ');

function absorb(res) {
  const list = typeof res.headers.getSetCookie === 'function'
    ? res.headers.getSetCookie()
    : (res.headers.get('set-cookie') ? [res.headers.get('set-cookie')] : []);
  for (const sc of list) {
    const [pair] = sc.split(';');
    const i = pair.indexOf('=');
    if (i < 0) continue;
    const k = pair.slice(0, i).trim(), v = pair.slice(i + 1).trim();
    if (!k) continue;
    if (/Max-Age=0/i.test(sc) || v === '' || v === 'deleted') jar.delete(k); else jar.set(k, v);
  }
}

async function call(path, opts = {}) {
  const res = await fetch(BASE + path, {
    ...opts,
    headers: {
      Accept: 'application/json',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36',
      'Accept-Language': 'en-US,en;q=0.9',
      Origin: BASE,
      Referer: BASE + '/dashboard',
      Cookie: cookieHeader(),
      ...(opts.headers || {}),
    },
    signal: AbortSignal.timeout(25000),
    redirect: 'manual',
  });
  absorb(res);
  let body = null;
  try { body = await res.json(); } catch { body = await res.text().catch(() => null); }
  return { status: res.status, body };
}

/* ---------- 登录 ---------- */
async function whoami() {
  const { status, body } = await call('/api/user/session');
  if (status === 200 && body?.success && body?.user_info?.username) {
    const u = body.user_info;
    log(`✅ 会话有效 · ${u.username} · 余额 ${u.credits} 币`);
    return u;
  }
  log(`⚠️ 会话无效 HTTP ${status} · ${body?.error_code || body?.message || ''}`);
  return null;
}

async function discordLogin() {
  if (!DISCORD_TOKEN) { log('❌ 无 OCTAGON_DISCORD_TOKEN，无法重登'); return false; }
  log('🔁 Discord OAuth 重登 ...');
  // 1) 站内入口 → 302 拿 Discord 授权 URL
  const r1 = await fetch(`${BASE}/api/user/auth/callback/discord/login`, {
    headers: { 'User-Agent': 'Mozilla/5.0 Chrome/157.0.0.0' }, redirect: 'manual', signal: AbortSignal.timeout(25000),
  });
  const loc = r1.headers.get('location') || '';
  const m = loc.match(/client_id=(\d+).*redirect_uri=([^&]+).*scope=([^&]*)/);
  if (!m) { log('❌ 入口未给出 Discord 授权 URL:', loc.slice(0, 120)); return false; }
  const authQ = `client_id=${m[1]}&redirect_uri=${m[2]}&response_type=code&scope=${m[3]}`;
  // 2) Discord：GET 应用信息 + POST 同意 → code
  const gh = await fetch(`https://discord.com/api/v9/oauth2/authorize?${authQ}`, {
    headers: { Authorization: DISCORD_TOKEN, Accept: 'application/json' }, signal: AbortSignal.timeout(25000),
  });
  if (!gh.ok) { log(`❌ Discord authorize GET ${gh.status}`); return false; }
  const gp = await fetch(`https://discord.com/api/v9/oauth2/authorize?${authQ}`, {
    method: 'POST',
    headers: { Authorization: DISCORD_TOKEN, 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ permissions: '0', authorize: true }),
    signal: AbortSignal.timeout(25000),
  });
  const pj = await gp.json().catch(() => ({}));
  const code = (pj.location || '').match(/[?&]code=([^&]+)/)?.[1];
  if (!code) { log('❌ 未拿到 code:', JSON.stringify(pj).slice(0, 160)); return false; }
  // 3) 回调换会话
  const rc = await fetch(`${BASE}/api/user/auth/callback/discord/login?code=${encodeURIComponent(code)}&iss=${encodeURIComponent('https://discord.com')}`, {
    headers: { 'User-Agent': 'Mozilla/5.0 Chrome/157.0.0.0' }, redirect: 'manual', signal: AbortSignal.timeout(25000),
  });
  absorb(rc);
  log(`🎉 重登完成（code ${code.slice(0, 8)}... → user_token ${jar.has('user_token') ? '到手' : '缺失'}）`);
  return jar.has('user_token');
}

/* ---------- 续期 ---------- */
function hoursUntil(iso) {
  // expires_at 形如 "2026-11-16 02:34:58"（UTC）
  const t = Date.parse(iso.replace(' ', 'T') + 'Z');
  return (t - Date.now()) / 36e5;
}

async function main() {
  if (process.env.OCTAGON_COOKIE) loadCookie(process.env.OCTAGON_COOKIE);
  log(`目标 ${BASE} · 阈值 剩余<${RENEW_HOURS}h 才续 · cookie 罐内 ${jar.size} 项`);

  let user = await whoami();
  if (!user) {
    if (!(await discordLogin())) { finish(3); return; }
    await sleep(1500);
    user = await whoami();
    if (!user) { finish(3); return; }
  }

  // 设置（价格/天数以服务端为准）
  const { body: st } = await call('/api/system/settings');
  const S = st?.settings || {};
  const cost = Number(S.server_renew_cost || 120);
  const days = parseInt(S.server_renew_days || '3', 10);
  if (String(S.server_renew_enabled) !== 'true' && S.server_renew_enabled !== true) {
    log('⚠️ server_renew_enabled=false —— 续期功能被官方关闭，只报告');
  }
  log(`💰 续期价 ${cost} 币 / ${days} 天 · 余额 ${user.credits} 币 ≈ 可续 ${Math.floor(Number(user.credits) / cost)} 次`);

  const { body: ls } = await call('/api/user/session/servers');
  const servers = ls?.servers || [];
  if (!servers.length) { log('⚠️ 名下没有服务器'); finish(0); return; }

  let ok = 0, rows = [];
  for (const s of servers) {
    const id = s.id;
    const { body: det } = await call(`/api/user/server/${id}`);
    const srv = Array.isArray(det?.server) ? det.server[0] : det?.server;
    const exp = srv?.mythicaldash?.expires_at;
    const left = exp ? hoursUntil(exp) : NaN;
    log(`-- [${id}] ${s.name} | suspended=${s.suspended} | 到期 ${exp || '?'}（剩 ${isNaN(left) ? '?' : left.toFixed(1)}h）--`);

    if (isNaN(left)) { rows.push([id, s.name, '?', '⏭ 无到期字段', '?']); ok++; continue; }
    if (left >= RENEW_HOURS) {
      log('  ✅ 余量充足，无需动作');
      rows.push([id, s.name, `${left.toFixed(1)}h`, '—', exp]);
      ok++; continue;
    }
    if (String(S.server_renew_enabled) !== 'true' && S.server_renew_enabled !== true) {
      rows.push([id, s.name, `${left.toFixed(1)}h`, '⏭ 官方关续期', exp]); ok++; continue;
    }
    if (Number(user.credits) < cost) {
      log(`  ⚠️ 余额不足（${user.credits} < ${cost}），无法续期！`);
      rows.push([id, s.name, `${left.toFixed(1)}h`, '⚠️ 余额不足', exp]);
      continue;
    }

    log(`  🚀 剩余 <${RENEW_HOURS}h → 续期（${cost} 币 / ${days} 天）`);
    const { status, body: rr } = await call(`/api/user/server/${id}/renew`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cost, days }),
    });
    if (status === 200 && rr?.success) {
      const exp2 = rr?.server?.mythicaldash?.expires_at;
      log(`  ✅ ${rr.message || 'renewed'} → 新到期 ${exp2 || '?'}`);
      rows.push([id, s.name, `${left.toFixed(1)}h`, `🚀 +${days}d`, exp2 || '?']);
      ok++;
    } else {
      log(`  ❌ 续期失败 HTTP ${status}: ${JSON.stringify(rr).slice(0, 200)}`);
      rows.push([id, s.name, `${left.toFixed(1)}h`, `❌ HTTP ${status}`, exp]);
    }
    await sleep(1200);
  }

  log(`\n== 完成 ${ok}/${servers.length} ==`);
  if (SUMMARY) {
    const fs = require('fs');
    fs.appendFileSync(SUMMARY,
      `## Octagon 续期结果\n\n余额 **${user.credits}** 币 · 价格 ${cost} 币/${days} 天 · 阈值 ${RENEW_HOURS}h\n\n` +
      `| 服务器 | 名称 | 原余量 | 动作 | 到期 |\n|---|---|---|---|---|\n` +
      rows.map(r => `| ${r.join(' | ')} |`).join('\n') + '\n');
  }
  finish(ok === servers.length ? 0 : 2);
}

function finish(code) {
  const out = cookieHeader();
  if (OUT_FILE && out) {
    const fs = require('fs');
    console.log(`::add-mask::${out}`);   // Actions 日志公开，先掩码
    fs.appendFileSync(OUT_FILE, `cookie<<__EOF__\n${out}\n__EOF__\n`);
    log('📤 已导出续期后的 cookie 到 GITHUB_OUTPUT');
  }
  process.exit(code);
}

main().catch((e) => { log('💥', e.message); finish(1); });
