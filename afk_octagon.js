#!/usr/bin/env node
/**
 * Octagon Circle 挂机心跳
 * 纯 HTTP，无浏览器。会话 Cookie 由环境变量 OCTAGON_COOKIE 提供（name=value[; name2=value2]）。
 * 规则（来自面板 /api/system/settings）：afk_min_per_coin=3 → 每满 3 分钟计 1 币。
 */
const BASE = process.env.OCTAGON_BASE || 'https://client.octagoncircle.org';
const COOKIE = (process.env.OCTAGON_COOKIE || '').trim();
const MINUTES = Number(process.env.AFK_MINUTES || 30);   // 单轮持续分钟数
const PER_COIN = Number(process.env.AFK_MIN_PER_COIN || 3);
const DRY = process.env.AFK_DRY === '1';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().replace('T', ' ').slice(0, 19), ...a);

async function call(path, opts = {}) {
  const res = await fetch(BASE + path, {
    ...opts,
    headers: {
      Accept: 'application/json',
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
      Cookie: COOKIE,
      ...(opts.headers || {}),
    },
    signal: AbortSignal.timeout(20000),
    redirect: 'manual',
  });
  let body = null;
  try { body = await res.json(); } catch { body = await res.text().catch(() => null); }
  return { status: res.status, body };
}

async function whoami() {
  const { status, body } = await call('/api/user/session');
  if (status !== 200) {
    log(`❌ 会话无效 HTTP ${status} · ${body && (body.error_code || body.message) || ''}`);
    return null;
  }
  const u = body?.user || body?.data || body?.session || body;
  log(`✅ 会话有效 · 用户 ${u?.username ?? u?.user?.username ?? '?'} · 余额 ${u?.coins ?? u?.user?.coins ?? '?'}`);
  return u;
}

async function beat(minute) {
  const coins = minute % PER_COIN === 0 ? 1 : 0;
  if (DRY) { log(`[dry] 第 ${minute} 分钟 → coins_earned=${coins}`); return { ok: true, coins }; }
  const { status, body } = await call('/api/user/earn/afk/work', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ coins_earned: coins, minutes_afk: 1 }),
  });
  const ok = status === 200 && (body?.success !== false);
  log(`${ok ? '💓' : '⚠️'} 第 ${minute} 分钟 · HTTP ${status} · coins_earned=${coins}` +
      (ok ? '' : ` · ${body?.error_code || body?.message || ''}`));
  return { ok, coins };
}

(async () => {
  if (!COOKIE && !DRY) {
    log('❌ 缺少 OCTAGON_COOKIE（格式：name=value）— 需要先在浏览器登录后导出 cookie');
    process.exit(2);
  }
  log(`目标 ${BASE} · 本轮 ${MINUTES} 分钟 · 每 ${PER_COIN} 分钟 1 币`);
  const u = DRY ? {} : await whoami();
  if (!DRY && !u) process.exit(3);
  if (!DRY) await sleep(2000);

  let earned = 0, fails = 0;
  for (let m = 1; m <= MINUTES; m++) {
    const r = await beat(m);
    if (r.ok) { earned += r.coins; fails = 0; } else { fails++; if (fails >= 3) { log('🛑 连续失败 3 次，中止'); break; } }
    if (m < MINUTES) await sleep(60000);
  }
  log(`🏁 本轮结束 · 预计入账 ${earned} 币`);
})().catch((e) => { log('💥', e.message); process.exit(1); });
