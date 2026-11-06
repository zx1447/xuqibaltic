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
//   QI_INTERVAL_DAYS      默认 2（距上次成功续期满 2 天才点）
//   QI_FORCE=1            无视间隔立即续
//   QI_DRY_RUN=1          只读状态不提交
//   QI_BASE               默认 https://gp.qficloud.com
const fs = require('fs');

const BASE = (process.env.QI_BASE || 'https://gp.qficloud.com').replace(/\/$/, '');
const KEY = (process.env.QI_API_KEY || '').trim();
const STATE_FILE = process.env.QI_STATE_FILE || 'qi_state.json';
const INTERVAL_DAYS = Number(process.env.QI_INTERVAL_DAYS || '2');
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

  for (const uuid of UUIDS) {
    const st = (state[uuid] = state[uuid] || {});
    log(`--- ${uuid} ---`);

    const info = await serverInfo(uuid);
    if (info.ok) {
      log(`  服务器: ${info.name}（${info.identifier}）状态 ${info.status}${info.suspended ? ' · 已暂停' : ''}`);
      st.name = info.name;
      st.identifier = info.identifier;
      st.status = info.status;
      changed = true;
    } else {
      log(`  ⚠️ 标准 /api/client/servers 读不到（${info.error}），只按自定义 renew 端点执行`);
    }

    // 关键：先问服务端「现在能不能续」
    const rn = await readRenewal(uuid);
    if (rn.ok) {
      st.renewAt = rn.renewAt || null;
      st.lastRenewedAt = rn.lastRenewedAt || null;
      st.renewals = rn.renewals ?? null;
      st.renewable = rn.renewable === true;
      st.overdue = rn.overdue === true;
      changed = true;
      if (rn.renewAt) {
        const left = Date.parse(rn.renewAt) - now;
        log(`  可续窗口：${new Date(rn.renewAt).toISOString().replace('T', ' ').slice(0, 19)} UTC`
          + `（${left > 0 ? '还有 ' + (left / 3600000).toFixed(1) + ' 小时' : '已开启'}）`);
      }
      log(`  服务端判定 renewable=${rn.renewable} · 已续 ${rn.renewals} 次`
        + (rn.lastRenewedAt ? ` · 上次 ${new Date(rn.lastRenewedAt).toISOString().slice(0, 19)} UTC` : '')
        + (rn.overdue ? ' · ⚠️ 已逾期' : ''));
    } else {
      log(`  ⚠️ 读不到 renewAt（${rn.error}），退回按间隔判定`);
    }

    const last = st.last_click_at ? Date.parse(st.last_click_at) : null;
    const daysSince = last ? (now - last) / 86400000 : null;
    if (daysSince !== null) log(`  距上次成功续期 ${daysSince.toFixed(2)} 天`);

    const spaced = daysSince === null || daysSince >= INTERVAL_DAYS;
    // renewable=false 时不要发 POST，否则只会拿到 400
    const need = FORCE || (spaced && (rn.ok ? st.renewable : true));
    if (!need) {
      log(rn.ok && !st.renewable
        ? `  判定：还没到续期窗口，跳过（不空点，避免 400）`
        : `  判定：距上次不足 ${INTERVAL_DAYS} 天，跳过`);
      st.last_check_at = new Date().toISOString();
      changed = true;
      continue;
    }
    log(`  判定：现在可以续（间隔 ${INTERVAL_DAYS} 天已满足）`);
    st.last_check_at = new Date().toISOString();
    if (DRY) { log('  DRY_RUN=1，不提交'); changed = true; continue; }

    log('  🎯 POST /api/client/dezerx/server-create/<uuid>/renew');
    let r = await renew(uuid);
    log(`  📡 HTTP ${r.status} ${r.status === 200 ? JSON.stringify(r.body).slice(0, 160) : errOf(r)}`);

    // 429 / 5xx 重试一次
    if (r.status === 429 || r.status >= 500) {
      log('  ⏳ 稍后重试一次');
      await sleep(8000);
      r = await renew(uuid);
      log(`  📡 重试 HTTP ${r.status} ${r.status === 200 ? JSON.stringify(r.body).slice(0, 160) : errOf(r)}`);
    }

    if (r.status === 200) {
      st.last_click_at = new Date().toISOString();
      st.last_verdict = 'renewed';
      st.last_note = JSON.stringify(r.body).slice(0, 200);
      delete st.too_early_since;
      log('  ✅ 续期成功');
      const after = await readRenewal(uuid);
      if (after.ok && after.renewAt) {
        st.renewAt = after.renewAt;
        st.lastRenewedAt = after.lastRenewedAt || null;
        st.renewals = after.renewals ?? null;
        log(`     下次可续：${new Date(after.renewAt).toISOString().replace('T', ' ').slice(0, 19)} UTC`);
      }
    } else if (/too.?early|cannot be renewed yet|Come back closer/i.test(errOf(r))) {
      st.last_verdict = 'too-early';
      st.last_note = errOf(r);
      st.too_early_since = st.too_early_since || new Date().toISOString();
      log(`  ⏳ 服务端说还太早（自 ${st.too_early_since}）；不计入 last_click_at`);
      log('     把 QI_INTERVAL_DAYS 调小一点就能更贴近服务端的窗口');
    } else if (r.status === 401) {
      st.last_verdict = 'auth-failed';
      st.last_note = errOf(r);
      log('  ❌ API key 被拒，请重新生成');
    } else {
      st.last_verdict = 'failed';
      st.last_note = errOf(r);
      log(`  ⚠️ 失败：${errOf(r)}`);
    }
    changed = true;
  }

  state._meta = { ...(state._meta || {}), last_run: new Date().toISOString(), interval_days: INTERVAL_DAYS };
  if (changed) saveState(state);
  log('完成');
}

main().catch((e) => { console.error('[qi] ❌', e.message); process.exit(1); });
