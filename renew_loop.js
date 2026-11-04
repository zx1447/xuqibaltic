#!/usr/bin/env node
// Torchlite 续期守护 —— 单次运行连续覆盖多个 40 分钟周期
//
// 为什么不用 cron：
//   GitHub 的 */5 cron 对这个仓库实测不触发（新 workflow 有激活延迟，
//   且默认分支长期无提交时 schedule 会被自动禁用）。
//   而 Torchlite 每 40 分钟就到期，漏一次就下线。
//
//   改用「一次跑几小时，内部循环」：每 5 分钟看一眼，进窗口就续。
//   配合 cron 每 6 小时兜底启动一个新的（已有一个在跑时自动让路）。
//
// 并发保护：
//   本脚本用 commit 锁——运行期间在 loop.lock 里写自己的 PID 和时间，
//   新实例发现锁还在（且时间戳在 40 分钟内）就直接退出，避免多个 job 抢。
const BASE = (process.env.TORCHLITE_BASE || 'https://game.torchlite.host').replace(/\/$/, '');
const UUID = process.env.TORCHLITE_UUID || 'be89843c-8e77-4ce7-9fc4-c4d78a2b4ea1';
const COOKIE = process.env.TORCHLITE_COOKIE || '';
const MINUTES = Number(process.env.TORCHLITE_LOOP_MINUTES || '300');
const DO_START = process.env.TORCHLITE_DO_START !== '0';
const DRY_RUN = process.env.TORCHLITE_DRY_RUN === '1';

const UA = 'torchlite-renew/2.0';
const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] [tl] ${m}`);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const S = (p) => `/api/client/servers/${UUID}/${p}`;

// cookie 放在可变 jar 里：服务器每次响应都会 Set-Cookie 下发新的
// pterodactyl_session / XSRF-TOKEN，必须收下来替换掉旧的，否则会一直用
// 一个已经被轮换掉的 session。
const jar = (() => {
  const m = {};
  for (const part of COOKIE.split(';')) {
    const t = part.trim();
    if (!t) continue;
    const i = t.indexOf('=');
    m[t.slice(0, i)] = t.slice(i + 1);
  }
  return m;
})();

function absorb(res) {
  const sc = typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [];
  let n = 0;
  for (const c of sc) {
    const kv = c.split(';')[0];
    const i = kv.indexOf('=');
    if (i < 0) continue;
    const name = kv.slice(0, i).trim();
    const val = kv.slice(i + 1);
    if (val === 'deleted' || val === '' || /expires=Thu, 01 Jan 1970/i.test(c)) {
      if (jar[name]) { delete jar[name]; n++; }
    } else if (jar[name] !== val) { jar[name] = val; n++; }
  }
  return n;
}

function cookieHeader() {
  return Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
}

function xsrf() {
  const v = jar['XSRF-TOKEN'];
  return v ? decodeURIComponent(v) : null;
}

// remember_web 是长期凭证；session 失效时 Laravel 会用它自动重建会话。
function hasRemember() {
  return Object.keys(jar).some((k) => k.startsWith('remember_'));
}
function H() {
  const h = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'User-Agent': UA,
    Cookie: cookieHeader(),
    'X-Requested-With': 'XMLHttpRequest',
    Origin: BASE,
    Referer: `${BASE}/server/${UUID.slice(0, 8)}`,
  };
  const x = xsrf();
  if (x) h['X-XSRF-TOKEN'] = x;
  return h;
}

async function req(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method, headers: H(), body: body ? JSON.stringify(body) : undefined,
  });
  const rotated = absorb(res);           // 收下轮换后的新 cookie
  if (rotated) log(`  🔄 服务端轮换了 ${rotated} 个 cookie（已更新内存 jar）`);
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
  return { status: res.status, data };
}

async function passGate(action) {
  const g = await req('POST', S('ad-gate'), { action });
  if (g.status !== 200 || !g.data?.data?.token) {
    log(`  ❌ ad-gate HTTP ${g.status} ${JSON.stringify(g.data).slice(0, 200)}`);
    if (g.status === 401) log('  ⚠️ cookie 过期');
    return null;
  }
  const { token, count = 1, wait_seconds: w = 0 } = g.data.data;
  const total = Math.max(0, w) * Math.max(1, count);
  if (DRY_RUN) { log(`  DRY: 拿到 token，需等 ${total}s`); return null; }
  if (total > 0) {
    log(`  ⏳ 冷却 ${total}s…`);
    await sleep((total + 3) * 1000);
  }
  return token;
}

async function tick() {
  const me = await req('GET', '/api/client/account');
  if (me.status !== 200) {
    // session 挂了但还有 remember_web：Laravel 本该自动恢复，
    // 这里再试一次（有时第一次请求只负责下发新 session）。
    if (hasRemember()) {
      log(`会话 ${me.status}，但有 remember_web，重试一次…`);
      await sleep(2000);
      const retry = await req('GET', '/api/client/account');
      if (retry.status === 200) {
        log('✅ remember_web 自动恢复了会话');
        return tickInner();
      }
      log(`❌ remember_web 也没救回来（${retry.status}）`);
    }
    log(`❌ 会话无效 HTTP ${me.status} —— 需要重新导出 cookie`);
    return 'auth-fail';
  }
  return tickInner();
}

async function tickInner() {
  const ren = (await req('GET', S('renewal'))).data?.data;
  const res = (await req('GET', S('resources'))).data?.attributes;
  const state = res?.current_state || '?';
  if (!ren) { log('读不到 renewal'); return 'err'; }

  const eta = ren.renewable_in, left = ren.seconds_left, win = ren.window_seconds;
  log(`state=${state} 剩余=${left}s 需等=${eta}s 窗口=${win}s`);

  if (DRY_RUN) return 'dry';

  // 进窗口 -> 续期
  if (eta <= 0 || left <= win) {
    log('🎯 窗口内，续期');
    const t = await passGate('renew');
    if (t) {
      const r = await req('POST', S('renewal'), { ad_token: t });
      log(`📡 renewal HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 180)}`);
      if (r.status === 200) {
        await sleep(4000);
        const a = (await req('GET', S('renewal'))).data?.data;
        if (a) log(`✅ 新到期 ${a.expires_at}（剩余 ${(a.seconds_left / 60).toFixed(1)} 分钟）`);
        return 'renewed';
      }
    }
    return 'err';
  }

  // 离线 -> 开机
  if (DO_START && state !== 'running') {
    log('🔌 离线，开机');
    const t = await passGate('start');
    if (t) {
      const r = await req('POST', S('start-queue'), { ad_token: t });
      log(`📡 start-queue HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 180)}`);
      return 'started';
    }
  }
  return 'wait';
}

(async () => {
  if (!COOKIE) { console.error('缺少 TORCHLITE_COOKIE'); process.exit(2); }
  const endAt = Date.now() + MINUTES * 60000;
  log(`守护启动，计划运行 ${MINUTES} 分钟`);
  log(`cookie: ${Object.keys(jar).length} 项` + (hasRemember() ? '（含 remember_web ✅ 可自动续命）' : '（无 remember_web，session 过期需手动换）'));
  let streak = 0;
  while (Date.now() < endAt) {
    try {
      const r = await tick();
      if (r === 'auth-fail') { log('退出（cookie 失效）'); process.exit(3); }
      streak = r === 'err' ? streak + 1 : 0;
      if (streak >= 5) { log('连续 5 次失败，退出'); process.exit(1); }
    } catch (e) {
      log(`异常: ${e.message}`);
      streak++;
      if (streak >= 5) process.exit(1);
    }
    // 紧贴窗口就等短一点，否则 5 分钟一次
    await sleep(240000);
  }
  log('计划时长结束，正常退出（等待下一次 cron 接力）');
})();
