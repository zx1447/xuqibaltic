#!/usr/bin/env node
/**
 * Octagon Circle 挂机心跳 + Cookie 自动续期
 *
 * 服务端每次响应都用 Set-Cookie 回写 user_token，Max-Age=1800（30 分钟滑动过期）。
 * 所以只要在 30 分钟内发一次请求，会话就能永久保持 —— 本脚本因此内置 Cookie 罐子：
 *   1. 从 OCTAGON_COOKIE 载入初始 cookie
 *   2. 吸收每个响应的 Set-Cookie
 *   3. 结束后把最新 cookie 打印成 GITHUB_OUTPUT，由 workflow 写回 secret
 * 结果是：只要一开始给一条有效 cookie，之后全自动，永不手工续。
 */
const BASE = process.env.OCTAGON_BASE || 'https://client.octagoncircle.org';
const MINUTES = Number(process.env.AFK_MINUTES || 30);
const DRY = process.env.AFK_DRY === '1';
const OUT_FILE = process.env.GITHUB_OUTPUT || '';

const log = (...a) => console.log(new Date().toISOString().replace('T', ' ').slice(0, 19), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- Cookie 罐子 ---------- */
const jar = new Map();
function loadCookie(str) {
  for (const part of (str || '').split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    const k = part.slice(0, i).trim();
    const v = part.slice(i + 1).trim();
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
    if (/Max-Age=0/i.test(sc) || v === '') jar.delete(k); else jar.set(k, v);
  }
}

async function call(path, opts = {}) {
  const res = await fetch(BASE + path, {
    ...opts,
    headers: {
      Accept: 'application/json',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
      'Accept-Language': 'en-US,en;q=0.9',
      Origin: BASE,
      Referer: BASE + '/earn/afk',
      Cookie: cookieHeader(),
      ...(opts.headers || {}),
    },
    signal: AbortSignal.timeout(20000),
    redirect: 'manual',
  });
  absorb(res);
  let body = null;
  try { body = await res.json(); } catch { body = await res.text().catch(() => null); }
  return { status: res.status, body };
}

async function whoami() {
  const { status, body } = await call('/api/user/session');
  if (status !== 200) {
    log(`❌ 会话无效 HTTP ${status} · ${body?.error_code || body?.message || ''}`);
    return null;
  }
  const u = body?.user_info || body?.user || body?.data || body;
  log(`✅ 会话有效 · ${u?.username ?? '?'} · 余额 ${u?.credits ?? '?'} · 累计挂机 ${u?.minutes_afk ?? '?'} 分`);
  return u;
}

async function beat(minute) {
  const coins = minute % 3 === 0 ? 1 : 0;   // 前端原样上报值，服务端会无视，仅保持请求形态一致
  if (DRY) { log(`[dry] 第 ${minute} 分钟 → coins_earned=${coins}`); return { ok: true, awarded: 0 }; }
  const { status, body } = await call('/api/user/earn/afk/work', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ coins_earned: coins, minutes_afk: 1 }),
  });
  const ok = status === 200 && body?.success !== false;
  const awarded = Number(body?.coins_awarded ?? 0);
  log(`${ok ? '💓' : '⚠️'} 第 ${minute} 分钟 · HTTP ${status} · 实得 ${awarded} · 余额 ${body?.total_coins ?? '?'} · 累计 ${body?.total_afk_time ?? '?'} 分` +
      (ok ? '' : ` · ${body?.error_code || body?.message || ''}`));
  return { ok, awarded };
}

async function main() {
  if (!process.env.OCTAGON_COOKIE && !DRY) {
    log('❌ 缺少 OCTAGON_COOKIE'); process.exit(2);
  }
  loadCookie(process.env.OCTAGON_COOKIE);
  log(`目标 ${BASE} · 本轮 ${MINUTES} 分钟 · 60 秒一次心跳 · cookie 罐内 ${jar.size} 项`);

  if (!DRY) {
    const u = await whoami();
    if (!u) { finish(3); return; }
    await sleep(2000);
  }

  let earned = 0, fails = 0;
  for (let m = 1; m <= MINUTES; m++) {
    const r = await beat(m);
    if (r.ok) { earned += r.awarded; fails = 0; }
    else if (++fails >= 3) { log('🛑 连续失败 3 次，中止'); break; }
    if (m < MINUTES) await sleep(60000);
  }
  log(`🏁 本轮结束 · 入账 ${earned} 币`);
  finish(fails >= 3 ? 4 : 0);
}

function finish(code) {
  // 把滑动续期后的最新 cookie 交给下一步（workflow 写回 secret）
  const out = cookieHeader();
  if (OUT_FILE && out) {
    const fs = require('fs');
    fs.appendFileSync(OUT_FILE, `cookie<<__EOF__\n${out}\n__EOF__\n`);
    log(`📤 已导出续期后 cookie（${out.length} 字节）到 GITHUB_OUTPUT`);
  }
  process.exit(code);
}

main().catch((e) => { log('💥', e.message); finish(1); });
