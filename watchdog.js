#!/usr/bin/env node
// Torchlite 守护 —— 双职责：① 停了就开 ② 快到期就续
//
// 两件事互相独立，每轮都检查：
//   状态 != running            -> 过 ad-gate，POST start-queue
//   剩余 <= window_seconds     -> 过 ad-gate，POST renewal
//
// 广告闸门的真相：后端只校验 ad_token 有效 + 等够 count×wait_seconds，
// 不看广告本身。所以纯 HTTP + 会话 cookie 即可，不需要浏览器。
//
// 调度：GitHub 的 */5 cron 本仓库实测不触发，改用长驻 job
// （一次跑 5.5 小时，内部每 50 秒一轮），cron 每 6 小时接力。
const BASE = (process.env.TORCHLITE_BASE || 'https://game.torchlite.host').replace(/\/$/, '');
const UUID = process.env.TORCHLITE_UUID || 'be89843c-8e77-4ce7-9fc4-c4d78a2b4ea1';
const COOKIE = process.env.TORCHLITE_COOKIE || '';
const LOOP_MIN = Number(process.env.TORCHLITE_LOOP_MINUTES || '330');
const TICK_SEC = Number(process.env.TORCHLITE_TICK_SEC || '50');
// 请求超时：单个 HTTP 请求最多等这么久，超时算失败进入下一轮
const ENABLE_START = process.env.TORCHLITE_DO_START !== '0';
const ENABLE_RENEW = process.env.TORCHLITE_DO_RENEW !== '0';
const DRY = process.env.TORCHLITE_DRY_RUN === '1';

const UA = 'torchlite-watchdog/3.0';
const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] [tl] ${m}`);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const S = (p) => `/api/client/servers/${UUID}/${p}`;

// ---- cookie jar：服务端每轮都轮换 session，必须收下新的 ----
const jar = (() => {
  const m = {};
  for (const part of COOKIE.split(';')) {
    const t = part.trim(); if (!t) continue;
    const i = t.indexOf('='); m[t.slice(0, i)] = t.slice(i + 1);
  }
  return m;
})();
function absorb(res) {
  const sc = typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [];
  let n = 0;
  for (const c of sc) {
    const kv = c.split(';')[0]; const i = kv.indexOf('=');
    if (i < 0) continue;
    const k = kv.slice(0, i).trim(), v = kv.slice(i + 1);
    if (!v || v === 'deleted' || /expires=Thu, 01 Jan 1970/i.test(c)) { if (jar[k]) { delete jar[k]; n++; } }
    else if (jar[k] !== v) { jar[k] = v; n++; }
  }
  return n;
}
const cookieHeader = () => Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
const hasRemember = () => Object.keys(jar).some((k) => k.startsWith('remember_'));

function H() {
  const h = {
    Accept: 'application/json', 'Content-Type': 'application/json', 'User-Agent': UA,
    Cookie: cookieHeader(), 'X-Requested-With': 'XMLHttpRequest',
    Origin: BASE, Referer: `${BASE}/server/${UUID.slice(0, 8)}`,
  };
  const x = jar['XSRF-TOKEN'];
  if (x) h['X-XSRF-TOKEN'] = decodeURIComponent(x);
  return h;
}
const REQ_TIMEOUT_MS = Number(process.env.TORCHLITE_REQ_TIMEOUT_MS || '20000');

async function req(method, path, body) {
  // 关键：必须设超时。否则对方一旦挂起不响应，fetch 会永远 await 下去，
  // 整个守护循环被阻塞——实测出现过 job 卡 40 分钟、服务器到期没人管的情况。
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: H(),
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(REQ_TIMEOUT_MS),
  });
  absorb(res);
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
  return { status: res.status, data };
}

// ---- 过广告闸门：拿 token -> 等冷却 -> 返回 token ----
async function gate(action) {
  const g = await req('POST', S('ad-gate'), { action });
  if (g.status !== 200 || !g.data?.data?.token) {
    log(`  ❌ ad-gate HTTP ${g.status} ${JSON.stringify(g.data).slice(0, 180)}`);
    if (g.status === 401) log('  ⚠️ cookie 失效');
    return null;
  }
  const { token, count = 1, wait_seconds: w = 0 } = g.data.data;
  const total = Math.max(0, w) * Math.max(1, count);
  if (DRY) { log(`  DRY: token 到手，需冷却 ${total}s`); return null; }
  if (total > 0) { log(`  ⏳ 冷却 ${total}s…`); await sleep(total + 3); }
  return token;
}

async function doRenew() {
  log('🔄 续期流程开始');
  const t = await gate('renew');
  if (!t) return false;
  const r = await req('POST', S('renewal'), { ad_token: t });
  log(`  📡 renewal HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  if (r.status === 200) {
    await sleep(3000);
    const a = (await req('GET', S('renewal'))).data?.data;
    if (a) log(`  ✅ 续期成功，新到期 ${a.expires_at}（剩余 ${(a.seconds_left / 60).toFixed(1)} 分钟）`);
    else log('  ✅ 续期成功');
    return true;
  }
  return false;
}

async function doStart() {
  log('▶️ 开机流程开始');
  const t = await gate('start');
  if (!t) return false;
  const r = await req('POST', S('start-queue'), { ad_token: t });
  log(`  📡 start-queue HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  if (r.status === 200) {
    const d = r.data?.data || {};
    if (d.in_queue) log(`  ✅ 已入队，#${d.position}/${d.queue_length}`);
    else if (d.moving) log('  ✅ 正在迁移启动');
    else log(`  ✅ 开机请求已接受（state=${d.state || '?'}）`);
    return true;
  }
  return false;
}

async function tick() {
  const me = await req('GET', '/api/client/account');
  if (me.status !== 200) {
    if (hasRemember()) {
      log(`会话 ${me.status}，有 remember_web，重试…`);
      await sleep(2000);
      if ((await req('GET', '/api/client/account')).status === 200) {
        log('✅ remember_web 恢复会话');
      } else { log('❌ cookie 彻底失效，需要重新导出'); return 'auth-fail'; }
    } else { log('❌ cookie 失效'); return 'auth-fail'; }
  }

  const res = (await req('GET', S('resources'))).data?.attributes;
  const ren = (await req('GET', S('renewal'))).data?.data;
  const state = res?.current_state || '?';
  if (!ren) { log('读不到 renewal 状态'); return 'err'; }

  const left = ren.seconds_left, win = ren.window_seconds, eta = ren.renewable_in;
  log(`state=${state} 剩余=${(left / 60).toFixed(1)}分 需等=${eta}s 窗口=${win}s`);

  if (DRY) return 'dry';

  // ① 停了就开 —— 只对真正停下的状态动作。
  // starting / stopping 是过渡态，此时再发 start-queue 是浪费一次广告冷却。
  const NEED_START = ['offline', 'stopped', 'crashed'];
  if (ENABLE_START && NEED_START.includes(state)) {
    log(`⚠️ 服务器 ${state}，需要开机`);
    await doStart();
    return 'started';
  }
  if (state === 'starting') log('⏳ 正在启动中，等待…');

  // ② 快到期就续（窗口内）
  if (ENABLE_RENEW && (eta <= 0 || left <= win)) {
    await doRenew();
    return 'renewed';
  }

  return 'ok';
}

(async () => {
  if (!COOKIE) { console.error('缺少 TORCHLITE_COOKIE'); process.exit(2); }
  const endAt = Date.now() + LOOP_MIN * 60000;
  log(`守护启动：${LOOP_MIN} 分钟，每 ${TICK_SEC}s 一轮`);
  log(`职责: 开机=${ENABLE_START ? '开' : '关'} 续期=${ENABLE_RENEW ? '开' : '关'}`);
  log(`cookie: ${Object.keys(jar).length} 项` + (hasRemember() ? '（含 remember_web ✅）' : '（无 remember_web ⚠️）'));

  // 看门狗：如果超过 STALL_MS 没有完成过一轮，说明卡死了，
  // 主动退出让 GitHub 重启 job（比卡住不动强）。
  const STALL_MS = Number(process.env.TORCHLITE_STALL_MS || '300000');
  let lastOk = Date.now();
  const stallTimer = setInterval(() => {
    const idle = Date.now() - lastOk;
    if (idle > STALL_MS) {
      log(`🚨 ${Math.round(idle / 1000)}s 无进展，判定卡死，主动退出`);
      process.exit(9);
    }
  }, 30000);
  stallTimer.unref?.();

  let errs = 0;
  while (Date.now() < endAt) {
    const started = Date.now();
    try {
      const r = await tick();
      if (r === 'auth-fail') process.exit(3);
      errs = r === 'err' ? errs + 1 : 0;
      if (errs >= 8) { log('连续失败过多，退出'); process.exit(1); }
    } catch (e) {
      log(`异常: ${e.message}`);
      if (++errs >= 8) process.exit(1);
    }
    lastOk = Date.now();
    const spent = Date.now() - started;
    // 续期时要等 72s 冷却，别再多睡一整轮
    const pause = spent > TICK_SEC * 1000 ? 0 : Math.max(0, TICK_SEC * 1000 - spent);
    if (pause) await sleep(pause / 1000);
  }
  clearInterval(stallTimer);
  log('本次计划时长结束，正常退出（等下一次 cron 接力）');
})();
