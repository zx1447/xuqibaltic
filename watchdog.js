#!/usr/bin/env node
// Torchlite 免费服务器守护 —— 关机就自动启动
//
// 策略（按需求简化）
// ----------------
// 只做一件事：服务器处于「停止」状态就过广告闸门把它启动。
// 不做窗口内续期——每次 start 都会重新发放 40 分钟，到期自然下线后
// 下一轮再拉起来，等效于持续保活，且逻辑简单、不受 5 分钟窗口约束。
//
// 广告闸门的真相（实测）
// --------------------
// 后端只校验 ad_token 是 /ad-gate 真签发的 + 从签发起过了
// count × wait_seconds 秒，**不看广告本身**。所以纯 HTTP 即可。
//
//   假 token            -> 403 ad_gate_required
//   真 token, 0 秒      -> 403 ad_gate_required
//   真 token, 等 72 秒  -> 200 ✅
//
// 三轮状态判断
// -----------
//   offline / stopped / crashed  -> 过闸门 -> start-queue
//   starting                     -> 什么都不做（过渡态，启动要几十秒）
//   running                      -> 什么都不做
//
// 超时与自愈
// ---------
//   - 每个 HTTP 请求 20s 超时（AbortSignal.timeout）
//   - 连续 8 次异常 -> 退出让 GitHub 重启
//   - cookie 失效 -> 退出码 3
const BASE = (process.env.TORCHLITE_BASE || 'https://game.torchlite.host').replace(/\/$/, '');
const UUID = process.env.TORCHLITE_UUID || 'be89843c-8e77-4ce7-9fc4-c4d78a2b4ea1';
const COOKIE = process.env.TORCHLITE_COOKIE || '';
const LOOP_MIN = Number(process.env.TORCHLITE_LOOP_MINUTES || '330');
const TICK_SEC = Number(process.env.TORCHLITE_TICK_SEC || '60');
const REQ_TIMEOUT_MS = Number(process.env.TORCHLITE_REQ_TIMEOUT_MS || '20000');
const DRY = process.env.TORCHLITE_DRY_RUN === '1';

const UA = 'torchlite-watchdog/4.0';
const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] [tl] ${m}`);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const S = (p) => `/api/client/servers/${UUID}/${p}`;

const NEED_START = ['offline', 'stopped', 'crashed'];

// ---- cookie jar：服务端每次响应都会轮换 session ----
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

async function req(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method, headers: H(), body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(REQ_TIMEOUT_MS),
  });
  absorb(res);
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
  return { status: res.status, data };
}

// ---- 过广告闸门 ----
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

async function doStart() {
  log('▶️ 开机流程');
  const t = await gate('start');
  if (!t) return false;
  const r = await req('POST', S('start-queue'), { ad_token: t });
  log(`  📡 start-queue HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  if (r.status !== 200) return false;
  const d = r.data?.data || {};
  if (d.in_queue) log(`  ✅ 已入队 #${d.position}/${d.queue_length}（节点满，等其他服务器让出）`);
  else if (d.moving) log('  ✅ 正在迁移并启动');
  else log(`  ✅ 启动请求已接受（state=${d.state || '?'}）`);
  return true;
}

async function tick() {
  const me = await req('GET', '/api/client/account');
  if (me.status !== 200) {
    if (hasRemember()) {
      log(`会话 ${me.status}，有 remember_web，重试…`);
      await sleep(2000);
      if ((await req('GET', '/api/client/account')).status === 200) log('✅ remember_web 恢复会话');
      else { log('❌ cookie 彻底失效'); return 'auth-fail'; }
    } else { log('❌ cookie 失效'); return 'auth-fail'; }
  }

  const res = (await req('GET', S('resources'))).data?.attributes;
  const state = res?.current_state || '?';
  const ren = (await req('GET', S('renewal'))).data?.data;
  const left = ren ? `${(ren.seconds_left / 60).toFixed(1)}分` : '?';
  log(`state=${state} 剩余=${left}`);

  if (DRY) return 'dry';

  if (NEED_START.includes(state)) {
    log(`⚠️ 服务器 ${state}，启动`);
    return (await doStart()) ? 'started' : 'err';
  }
  if (state === 'starting') log('⏳ 启动中，等待');
  else if (state === 'stopping') log('⏳ 停止中，等待');
  return 'ok';
}

(async () => {
  if (!COOKIE) { console.error('缺少 TORCHLITE_COOKIE'); process.exit(2); }
  const endAt = Date.now() + LOOP_MIN * 60000;
  log(`守护启动：${LOOP_MIN} 分钟，每 ${TICK_SEC}s 一轮`);
  log('职责：关机就启动（不做窗口续期）');
  log(`cookie: ${Object.keys(jar).length} 项` + (hasRemember() ? '（含 remember_web ✅）' : '（无 remember_web ⚠️）'));

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
      if (++errs >= 8) { log('连续失败过多，退出'); process.exit(1); }
    }
    // 本轮已耗时（比如跑了 75s 冷却），就不再额外睡满一轮
    const spent = Date.now() - started;
    const pause = Math.max(0, TICK_SEC * 1000 - spent);
    if (pause > 0) await sleep(pause / 1000);
  }
  log('本次计划时长结束，正常退出（等下一次 cron 接力）');
})();
