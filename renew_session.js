#!/usr/bin/env node
// Torchlite 免费服务器自动续期 —— 会话 cookie 版（纯 HTTP，无需浏览器）
//
// 广告闸门的真相（实测验证）
// ------------------------
// 面板要求"看广告"才能开机/续期，但后端其实只校验两件事：
//   1. ad_token 是真的（由 POST /ad-gate 签发）
//   2. 从签发到提交，已经过了 count × wait_seconds 秒
// 广告本身（Google 奖励广告）是纯前端行为，后端完全不知道你有没有看。
//
// 实测对照：
//   假 token              -> 403 ad_gate_required
//   无 token              -> 403 ad_gate_required
//   真 token, 0 秒        -> 403 ad_gate_required   ← 冷却没到
//   真 token, 等 72 秒    -> 200 ✅
//
// 完整协议
// -------
//   POST /api/client/servers/{uuid}/ad-gate  {"action":"renew"|"start"}
//     -> {"data":{"required":true,"token":"...","count":3,"wait_seconds":24}}
//   等待 count × wait_seconds
//   POST /api/client/servers/{uuid}/renewal     {"ad_token": token}   // 续期
//   POST /api/client/servers/{uuid}/start-queue {"ad_token": token}   // 开机
//
// 注意：API key（ptlc_）会被硬拒绝（ad_gate_browser_only），必须用会话 cookie。
//
// 环境变量
//   TORCHLITE_COOKIE      必填，浏览器里复制的整条 Cookie 头
//   TORCHLITE_UUID        服务器 UUID
//   TORCHLITE_DO_START    "1" = 服务器离线时顺便开机
//   TORCHLITE_DRY_RUN     "1" = 只看状态不动作
//   TORCHLITE_FORCE       "1" = 无视续期窗口强行点
const BASE = (process.env.TORCHLITE_BASE || 'https://game.torchlite.host').replace(/\/$/, '');
const UUID = process.env.TORCHLITE_UUID || 'be89843c-8e77-4ce7-9fc4-c4d78a2b4ea1';
const COOKIE = process.env.TORCHLITE_COOKIE || '';
const DO_START = process.env.TORCHLITE_DO_START === '1';
const DRY_RUN = process.env.TORCHLITE_DRY_RUN === '1';
const FORCE = process.env.TORCHLITE_FORCE === '1';
const MAX_WAIT = Number(process.env.TORCHLITE_MAX_WAIT_SEC || '660');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36';

const log = (m) => console.log(`[torchlite] ${m}`);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));

function xsrfToken() {
  const m = COOKIE.match(/XSRF-TOKEN=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function headers() {
  const h = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'User-Agent': UA,
    Cookie: COOKIE,
    'X-Requested-With': 'XMLHttpRequest',
    Origin: BASE,
    Referer: `${BASE}/server/${UUID.slice(0, 8)}`,
  };
  const x = xsrfToken();
  if (x) h['X-XSRF-TOKEN'] = x;
  return h;
}

async function req(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: headers(),
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = text.slice(0, 300); }
  return { status: res.status, data };
}

const S = (uuid, p) => `/api/client/servers/${uuid}/${p}`;

// ad-gate -> 等冷却 -> 提交 token
async function passGate(action) {
  log(`🔓 申请 ad_token (action=${action})…`);
  const g = await req('POST', S(UUID, 'ad-gate'), { action });
  if (g.status !== 200 || !g.data?.data?.token) {
    log(`❌ ad-gate 失败 HTTP ${g.status}: ${JSON.stringify(g.data).slice(0, 250)}`);
    if (g.status === 401) log('   cookie 已过期，需要重新导出 TORCHLITE_COOKIE');
    return null;
  }
  const { token, count = 1, wait_seconds: waitS = 0, required } = g.data.data;
  const total = Math.max(0, waitS) * Math.max(1, count);
  log(`   token 已签发 (required=${required}, count=${count}, wait=${waitS}s → 共 ${total}s)`);

  if (DRY_RUN) { log('   DRY_RUN=1，不提交'); return null; }

  if (total > 0) {
    log(`⏳ 等待冷却 ${total + 3}s（广告不需要真看，后端只算时间）`);
    await sleep(total + 3);
  }
  return token;
}

async function getRenewal() {
  const r = await req('GET', S(UUID, 'renewal'));
  return r.status === 200 ? r.data.data : null;
}
async function getResources() {
  const r = await req('GET', S(UUID, 'resources'));
  return r.status === 200 ? r.data.attributes : null;
}

function fmt(iso) {
  try { return new Date(iso).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'; }
  catch { return iso; }
}

(async () => {
  if (!COOKIE) { console.error('[torchlite] ERROR: 缺少 TORCHLITE_COOKIE'); process.exit(2); }

  try {
    // 1. 校验会话
    const me = await req('GET', '/api/client/account');
    if (me.status !== 200) {
      log(`❌ 会话无效 HTTP ${me.status}：${JSON.stringify(me.data).slice(0, 200)}`);
      log('   cookie 过期了，需要重新从浏览器导出');
      process.exit(3);
    }
    log(`✅ 会话有效：${me.data.attributes.username}`);

    // 2. 当前状态
    const ren = await getRenewal();
    const res = await getResources();
    const state = res?.current_state || '?';
    log(`服务器状态: ${state} | 到期 ${ren ? fmt(ren.expires_at) : '?'} | `
      + `剩余 ${ren?.seconds_left}s | 还需等 ${ren?.renewable_in}s`);

    if (DRY_RUN) { log('DRY_RUN=1，结束'); process.exit(0); }

    let acted = false;

    // 3. 续期（窗口内才点）
    const inWindow = ren && (ren.renewable_in <= 0 || ren.seconds_left <= ren.window_seconds);
    if (FORCE || inWindow) {
      log(`🎯 进入续期流程${FORCE ? '（FORCE）' : '（已在窗口内）'}`);
      const token = await passGate('renew');
      if (token) {
        const r = await req('POST', S(UUID, 'renewal'), { ad_token: token });
        log(`📡 renewal -> HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 250)}`);
        if (r.status === 200) {
          acted = true;
          await sleep(4000);
          const after = await getRenewal();
          if (after) {
            log(`✅ 续期成功：新到期 ${fmt(after.expires_at)}，剩余 ${after.seconds_left}s `
              + `(${(after.seconds_left / 60).toFixed(1)} 分钟)`);
          }
        } else {
          log('❌ 续期被拒绝');
        }
      }
    } else if (ren) {
      const waitFor = ren.renewable_in;
      if (waitFor <= MAX_WAIT) {
        log(`⏳ ${waitFor}s 后进窗口，本次等待并续期…`);
        await sleep(waitFor + 3);
        const token = await passGate('renew');
        if (token) {
          const r = await req('POST', S(UUID, 'renewal'), { ad_token: token });
          log(`📡 renewal -> HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 250)}`);
          if (r.status === 200) {
            acted = true;
            await sleep(4000);
            const after = await getRenewal();
            if (after) log(`✅ 续期成功：新到期 ${fmt(after.expires_at)}`);
          }
        }
      } else {
        log(`⏭️ 还早（需等 ${waitFor}s > 上限 ${MAX_WAIT}s），本次退出`);
      }
    }

    // 4. 离线则开机
    if (DO_START && state !== 'running') {
      log(`🔌 服务器 ${state}，走开机流程`);
      const token = await passGate('start');
      if (token) {
        const r = await req('POST', S(UUID, 'start-queue'), { ad_token: token });
        log(`📡 start-queue -> HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 250)}`);
        acted = true;
      }
    }

    log(acted ? '完成' : '无需动作');
    process.exit(0);
  } catch (e) {
    console.error(`[torchlite] ❌ ${e.message}`);
    process.exit(1);
  }
})();
