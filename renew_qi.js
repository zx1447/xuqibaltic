#!/usr/bin/env node
// Q-Fi Cloud（Dezerx 主题 / Pterodactyl）免费服务器自动续期 · 每 2 天一次
//
// 端点
//   GET  /api/client/dezerx/server-create/<UUID>/renewal
//        → {renewAt, lastRenewedAt, renewals, renewable, overdue, suspended}
//          renewable=true 才允许续，否则 POST 会 400 "cannot be renewed yet"
//   POST /api/client/dezerx/server-create/<UUID>/renew
//        → 200 续期成功 / 400 DisplayException 还没到时间 / 401 key 无效
//   GET  /api/client/dezerx/server-create
//        → 免费额度状态：refusal/remaining/held/perUser/discord + renewals 周期规则
//
// 认证
//   Pterodactyl Client API key：Authorization: Bearer ptlc_xxx
//   （站点 /account/api 页面就是这套 key 的管理界面；普通网页登录用的是 cookie，
//     cookie 拿不到 API key 的权限，所以必须用 API key）
//
// 环境变量
//   QI_API_KEY            必填，ptlc_ 开头的 Client API key
//   QI_SERVER_UUID        可选，默认用下面那个；支持逗号分隔多个 UUID
//   QI_STATE_FILE         默认 qi_state.json
//   QI_INTERVAL_DAYS      默认 2（距上次成功续期满 2 天才点，防止重复点击）
//   QI_WAIT=1             精准模式：算到 renewAt，主动睡到窗口开启再续（默认 1）
//   QI_WAIT_MAX_MIN       最多愿意睡多久（默认 330，GitHub job 上限 350 分钟）
//   QI_SLACK_SEC          到点后再补几秒余量（默认 30，抵消服务端时钟差）
//   QI_POLL_SEC           等待中每隔多久复查一次状态（默认 300）
//   QI_FORCE=1            无视间隔立即续
//   QI_DRY_RUN=1          只读状态不提交
//   QI_BASE               默认 https://gp.qficloud.com
const fs = require('fs');

const BASE = (process.env.QI_BASE || 'https://gp.qficloud.com').replace(/\/$/, '');
const KEY = (process.env.QI_API_KEY || '').trim();
const STATE_FILE = process.env.QI_STATE_FILE || 'qi_state.json';
const INTERVAL_DAYS = Number(process.env.QI_INTERVAL_DAYS || '2');
const WAIT = process.env.QI_WAIT !== '0';                       // 默认开启精准等待
const WAIT_MAX_MIN = Number(process.env.QI_WAIT_MAX_MIN || '330');
const SLACK_SEC = Number(process.env.QI_SLACK_SEC || '30');
const POLL_SEC = Number(process.env.QI_POLL_SEC || '300');
const OVERRIDE_RENEW_AT = process.env.QI_OVERRIDE_RENEW_AT || '';  // 仅调试：伪造 renewAt 验证等待逻辑
const OVERRIDE_RENEWABLE = process.env.QI_OVERRIDE_RENEWABLE === '1';  // 仅调试：伪造 renewable=true 验证提交路径
const FORCE = process.env.QI_FORCE === '1';
const DRY = process.env.QI_DRY_RUN === '1';
const DEFAULT_UUID = '2909e505-6516-4d30-86ca-ae75af6aa91b';
const UUIDS = (process.env.QI_SERVER_UUID || DEFAULT_UUID).split(',').map((s) => s.trim()).filter(Boolean);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';

const log = (...a) => console.log('[qi]', ...a);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));

function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2) + '\n'); }

async function api(path, method = 'GET', body) {
  const r = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${KEY}`,
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'User-Agent': UA,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(Number(process.env.QI_TIMEOUT_MS || 25000)),
  });
  const text = await r.text();
  let j = null;
  try { j = text ? JSON.parse(text) : null; } catch { j = text; }
  return { status: r.status, body: j };
}

// Pterodactyl 错误结构：{ errors:[{code,status,detail}] }
function errOf(r) {
  const e = r.body && r.body.errors && r.body.errors[0];
  return e ? `${e.code} ${e.status}: ${e.detail}` : JSON.stringify(r.body).slice(0, 200);
}

// 读服务器状态：优先用标准 client API（拿到 name / 状态 / 到期信息如果有）
async function serverInfo(uuid) {
  const r = await api(`/api/client/servers/${uuid}`);
  if (r.status === 200 && r.body && r.body.attributes) {
    const a = r.body.attributes;
    return { ok: true, name: a.name, identifier: a.identifier, status: a.status,
      suspended: a.is_suspended === true || a.status === 'suspended', raw: a };
  }
  // Dezerx 的自定义接口在某些版本下不暴露标准端点，退回只做续期
  return { ok: false, error: errOf(r), status: r.status };
}

// 读「什么时候能续」——有这个就不用盲点，也不会吃 400
async function readRenewal(uuid) {
  const r = await api(`/api/client/dezerx/server-create/${uuid}/renewal`);
  if (r.status === 200 && r.body && r.body.uuid) return { ok: true, ...r.body };
  return { ok: false, status: r.status, error: errOf(r) };
}

async function renew(uuid) {
  return api(`/api/client/dezerx/server-create/${uuid}/renew`, 'POST', {});
}


// 处理一台服务器：读窗口 → 需要就（等到点）续 → 写状态
// 返回 { verdict, waitedSec, renewAt }
async function handleServer(uuid, st, state) {
  const now0 = Date.now();

  const info = await serverInfo(uuid);
  if (info.ok) {
    log(`  服务器: ${info.name}（${info.identifier}）状态 ${info.status}${info.suspended ? ' · 已暂停' : ''}`);
    st.name = info.name; st.identifier = info.identifier; st.status = info.status;
  } else {
    log(`  ⚠️ 标准 /api/client/servers 读不到（${info.error}），只按自定义 renew 端点执行`);
  }

  const rn = await readRenewal(uuid);
  if (OVERRIDE_RENEW_AT) { rn.ok = true; rn.renewAt = OVERRIDE_RENEW_AT; log(`  ⚠️ 调试覆盖 renewAt=${OVERRIDE_RENEW_AT}`); }
  if (rn.ok) {
    st.renewAt = rn.renewAt || null;
    st.lastRenewedAt = rn.lastRenewedAt || null;
    st.renewals = rn.renewals ?? null;
    st.renewable = rn.renewable === true;
    st.overdue = rn.overdue === true;

    const parseOk = st.renewAt && !Number.isNaN(Date.parse(st.renewAt));
    if (parseOk) {
      // renewAt 是「下次允许续期的时间」，窗口 = renewAt 起 48 小时
      const open = Date.parse(st.renewAt);
      const winEnd = open + 48 * 3600000;
      log(`  窗口：${fmt(open)} ～ ${fmt(winEnd)} UTC`
        + `（${open > now0 ? '还有 ' + hrs(open - now0) : '已开放'}）`);
    }
    log(`  服务端：renewable=${st.renewable} · 已续 ${st.renewals} 次`
      + (st.lastRenewedAt ? ` · 上次 ${fmt(Date.parse(st.lastRenewedAt))} UTC` : '')
      + (st.overdue ? ' · ⚠️ 已逾期' : ''));
  } else {
    log(`  ⚠️ 读不到 renewAt（${rn.error}）`);
  }

  const last = st.last_click_at ? Date.parse(st.last_click_at) : null;
  const daysSince = last ? (now0 - last) / 86400000 : null;
  if (daysSince !== null) log(`  距上次成功续期 ${daysSince.toFixed(2)} 天`);
  const spaced = daysSince === null || daysSince >= INTERVAL_DAYS;

  // ---------- 智能等待：算到 renewAt，睡到点再续 ----------
  const openAt = st.renewAt && !Number.isNaN(Date.parse(st.renewAt)) ? Date.parse(st.renewAt) : null;
  const remainingMs = openAt ? openAt - Date.now() : null;

  if (WAIT && !FORCE && openAt && remainingMs > 0 && !st.renewable) {
    const waitMin = remainingMs / 60000 + SLACK_SEC / 60;
    if (waitMin > WAIT_MAX_MIN) {
      log(`  距开放窗口还有 ${(remainingMs / 3600000).toFixed(1)} 小时，超过本次可等上限 `
        + `${WAIT_MAX_MIN} 分钟 → 本轮只报告，交给后续 cron 接力`);
      st.last_check_at = new Date().toISOString();
      st.last_verdict = 'waiting-next-run';
      st.last_note = `窗口 ${fmt(openAt)} 开启`;
      state[uuid] = st;
      return { verdict: 'waiting-next-run', waitedSec: 0, renewAt: st.renewAt };
    }
    const deadline = openAt + SLACK_SEC * 1000;
    log(`  ⏳ 精准等待 ${(remainingMs / 60000).toFixed(1)} 分钟，到 ${fmt(deadline)} UTC 再提交`);
    // 分段睡，期间定期复查（服务端可能提前开窗，也可能时钟有偏差）
    while (true) {
      const left = deadline - Date.now();
      if (left <= 0) break;
      await sleep(Math.min(Math.ceil(left / 1000), POLL_SEC));
      const stillLeft = deadline - Date.now();
      if (stillLeft <= 0) break;
      log(`  …还差 ${(stillLeft / 60000).toFixed(1)} 分钟`);
      const again = await readRenewal(uuid);
      if (again.ok && again.renewable) {
        log('  ⚡ 服务端已提前开窗，立刻续期');
        st.renewable = true;
        break;
      }
    }
  } else if (WAIT && !FORCE && openAt && remainingMs <= 0 && !st.renewable) {
    // 到点了但服务端还不认（时钟偏差 / 服务端自己推迟）：再等最多 3 轮
    for (let k = 1; k <= 3 && !st.renewable; k++) {
      log(`  到点了但 renewable=false，第 ${k}/3 次复查（60s 后）`);
      await sleep(60);
      const again = await readRenewal(uuid);
      if (again.ok) {
        st.renewable = again.renewable === true;
        if (again.renewAt) {
          st.renewAt = again.renewAt;
          const nb = Date.parse(again.renewAt);
          if (nb > Date.now() + 3600000) { log(`  服务端把窗口推后到 ${fmt(nb)} UTC，本轮退出`); break; }
        }
      }
    }
  }

  if (OVERRIDE_RENEWABLE) { st.renewable = true; log('  ⚠️ 调试覆盖 renewable=true'); }
  const need = FORCE || (spaced && (st.renewable === true || (!rn.ok)));
  if (!need) {
    log(st.renewable === false && openAt && remainingMs > 0
      ? '  判定：还没到续期窗口，跳过（不空点，避免 400）'
      : `  判定：距上次不足 ${INTERVAL_DAYS} 天，跳过`);
    st.last_check_at = new Date().toISOString();
    st.last_verdict = 'skipped';
    state[uuid] = st;
    return { verdict: 'skipped', waitedSec: 0, renewAt: st.renewAt };
  }

  st.last_check_at = new Date().toISOString();
  if (DRY) {
    log('  DRY_RUN=1，不提交');
    state[uuid] = st;
    return { verdict: 'dry-run', waitedSec: 0, renewAt: st.renewAt };
  }

  log('  🎯 POST /api/client/dezerx/server-create/<uuid>/renew');
  let r = await renew(uuid);
  log(`  📡 HTTP ${r.status} ${r.status === 200 ? JSON.stringify(r.body).slice(0, 160) : errOf(r)}`);

  if (r.status === 429 || r.status >= 500) {
    log('  ⏳ 稍后重试一次');
    await sleep(8);
    r = await renew(uuid);
    log(`  📡 重试 HTTP ${r.status} ${r.status === 200 ? JSON.stringify(r.body).slice(0, 160) : errOf(r)}`);
  }

  let verdict = 'failed';
  if (r.status === 200) {
    st.last_click_at = new Date().toISOString();
    st.last_verdict = 'renewed';
    st.last_note = JSON.stringify(r.body).slice(0, 200);
    delete st.too_early_since;
    verdict = 'renewed';
    log('  ✅ 续期成功');
    const after = await readRenewal(uuid);
    if (after.ok && after.renewAt) {
      st.renewAt = after.renewAt;
      st.lastRenewedAt = after.lastRenewedAt || null;
      st.renewals = after.renewals ?? null;
      st.renewable = after.renewable === true;
      log(`     下次窗口：${fmt(Date.parse(after.renewAt))} UTC`);
    }
  } else if (/too.?early|cannot be renewed yet|Come back closer/i.test(errOf(r))) {
    st.last_verdict = 'too-early';
    st.last_note = errOf(r);
    st.too_early_since = st.too_early_since || new Date().toISOString();
    verdict = 'too-early';
    log('  ⏳ 服务端说还太早；不计入 last_click_at');
  } else if (r.status === 401) {
    st.last_verdict = 'auth-failed';
    st.last_note = errOf(r);
    verdict = 'auth-failed';
    log('  ❌ API key 被拒，请重新生成');
  } else {
    st.last_verdict = 'failed';
    st.last_note = errOf(r);
    log(`  ⚠️ 失败：${errOf(r)}`);
  }
  state[uuid] = st;
  return { verdict, waitedSec: Math.round((Date.now() - now0) / 1000), renewAt: st.renewAt };
}

const fmt = (ms) => new Date(ms).toISOString().replace('T', ' ').slice(0, 19);
const hrs = (ms) => (ms / 3600000).toFixed(1) + ' 小时';

async function main() {
  if (!KEY) throw new Error('缺少 QI_API_KEY（Pterodactyl Client API key，ptlc_ 开头；在 /account/api 页创建）');
  if (!/^ptlc_/.test(KEY)) log('⚠️ QI_API_KEY 不是 ptlc_ 开头，Pterodactyl 可能不认');

  const state = loadState();
  const now = Date.now();
  let changed = false;

  // 先验一下 key 是否有效
  const acct = await api('/api/client/account');
  if (acct.status !== 200) {
    throw new Error(`API key 无效（GET /api/client/account -> ${acct.status} ${errOf(acct)}）`);
  }
  const me = acct.body && acct.body.attributes;
  log(`账号 ${me && (me.username || me.email)} · 已确认 key 可用`);

  const results = [];
  for (const uuid of UUIDS) {
    const st = (state[uuid] = state[uuid] || {});
    log(`--- ${uuid} ---`);
    results.push({ uuid, ...(await handleServer(uuid, st, state)) });
  }
  for (const r of results) {
    log(`结果 ${r.uuid.slice(0, 8)}: ${r.verdict}${r.waitedSec ? `（等了 ${Math.round(r.waitedSec / 60)} 分钟）` : ''}`
      + (r.renewAt ? ` · 下次窗口 ${r.renewAt}` : ''));
  }

  state._meta = { ...(state._meta || {}), last_run: new Date().toISOString(), interval_days: INTERVAL_DAYS };
  if (changed) saveState(state);
  log('完成');
}

main().catch((e) => { console.error('[qi] ❌', e.message); process.exit(1); });
