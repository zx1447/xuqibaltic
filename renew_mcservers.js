#!/usr/bin/env node
// Discord 面板续期 · 多账号统一脚本
//
// 两个服务，机制不同，共用一套点击 + 网关判定逻辑：
//
// ┌───────────┬──────────────────────────────────────────────────────────┐
// │ McServers │ 7 天有效期。有效期内点会回 "You can renew again later."   │
// │           │ 必须等到期才能续。每 5 天探一次。                        │
// ├───────────┼──────────────────────────────────────────────────────────┤
// │ Antagonix │ 5 天有效期。**随时可点**，点一次即"从现在起重置 +5 天"。 │
// │           │ 回文案 "Hébergement renouvelé pour 5 jours..."           │
// └───────────┴──────────────────────────────────────────────────────────┘
//
// 通用请求格式（组件交互）
// ----------------------
//   POST https://discord.com/api/v9/interactions
//   {
//     "type": 3,                  ← 必须是 3，不是 2
//     "nonce": "<19 位数字>",
//     "guild_id": "...", "channel_id": "...",
//     "message_flags": 0,         ← 必填
//     "message_id": "...",
//     "application_id": "<bot 用户 ID>",   ← 消息 JSON 里是 null，要用 bot 的 id
//     "session_id": "<32 位十六进制>",
//     "data": { "component_type": 2, "custom_id": "..." }
//   }
//   成功返回 HTTP 204。
//
// bot 的回复是 ephemeral（私密），普通消息接口读不到（403 / code 20002），
// 所以本脚本连 Discord 网关监听 read-back 来判定真实结果。
//
// 环境变量
//   DR_TARGET=mcservers|antagonix|all        要处理哪个（默认 all）
//   DISCORDERENEW_TOKEN                      McServers 账号 token
//   ANTAGONIX_TOKEN                          Antagonix 账号 token
//   DR_FORCE=1                               无视间隔立即点
//   DR_DRY_RUN=1                             只读状态不点击
//   DR_GATEWAY=0                             关闭网关（仅凭 204 判定）
const fs = require('fs');
let WebSocket = null;
try { WebSocket = require('ws'); } catch { /* 没有 ws 时降级为纯 HTTP 模式 */ }

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0';

const STATE_FILE = process.env.DR_STATE_FILE || 'disordrenew_state.json';
const TARGET = (process.env.DR_TARGET || 'all').toLowerCase();
const FORCE = process.env.DR_FORCE === '1';
const DRY_RUN = process.env.DR_DRY_RUN === '1';
const USE_GATEWAY = process.env.DR_GATEWAY !== '0' && !!WebSocket;
const LISTEN_MS = Number(process.env.DR_LISTEN_MS || '20000');

// ---------------- 目标定义 ----------------
const TARGETS = {
  mcservers: {
    label: 'McServers',
    tokenEnv: 'DISCORDERENEW_TOKEN',
    guild: '1559978655688564848',
    channel: '1567486173495173181',
    message: '1567495211016519802',
    appId: '1524370665631649865',
    customId: 'renew_server',
    intervalDays: 5,
    // 有效期内点击会被拒
    manageable: false,
    // 只匹配"操作结果"类文案，避免面板 embed 里的普通文字命中
    successRe: /(renewed|extended|updated|has been).{0,40}(success|for \d+|successfully)|successfully (renewed|extended)|renewal (was )?successful/i,
    tooEarlyRe: /you can renew again later|not yet available|too early to renew/i,
    // 从 bot 的纯文本回执里再兜一层（只接受短句，不接收 embed 大段）
    replyOnly: true,
  },
  antagonix: {
    label: 'Antagonix',
    tokenEnv: 'ANTAGONIX_TOKEN',
    guild: '1453152446913314950',
    channel: '1567073787143524383',
    message: '1567073789395730475',
    appId: '1565149630260322418',
    customId: 'b|fa67c4d1a69f9e08bc484195|renew',
    intervalDays: 2,          // 每 2 天点一次（有效期 5 天，冗余很足）
    manageable: true,         // 随时可点，点一次重置 +5 天
    renewalDays: 5,           // 每次续期发放天数，用于到期兜底判定
    successRe: /h[eé]bergement renouvel[eé]|renouvel[eé] pour \d+ jours/i,
    tooEarlyRe: /à nouveau plus tard|pas encore|trop t[oô]t/i,
    replyOnly: true,
    // 面板 embed 里可以直接读到到期时间戳
    expiryFromEmbed: true,
  },
};

const log = (tag, m) => console.log(`[${tag}] ${m}`);
const randDigits = (n) => Array.from({ length: n }, () => Math.floor(Math.random() * 10)).join('');
const randHex = (n) => Array.from({ length: n }, () => '0123456789abcdef'[Math.floor(Math.random() * 16)]).join('');

function superProperties() {
  return Buffer.from(JSON.stringify({
    os: 'Windows', browser: 'Edge', device: '', device_model_name: '',
    system_locale: 'zh-CN', has_client_mods: false,
    browser_user_agent: UA, browser_version: '156.0.0.0', os_version: '10',
    referrer: 'https://dracovm.xyz/', referring_domain: 'dracovm.xyz',
    referrer_current: 'https://discord.com/', referring_domain_current: 'discord.com',
    release_channel: 'stable', client_build_number: 661871, client_event_source: null,
  })).toString('base64');
}

function headers(token, t) {
  return {
    Authorization: token,
    'Content-Type': 'application/json',
    'User-Agent': UA,
    Accept: '*/*',
    Origin: 'https://discord.com',
    Referer: `https://discord.com/channels/${t.guild}/${t.channel}`,
    'X-Super-Properties': superProperties(),
    'X-Discord-Locale': 'zh-CN',
    'X-Discord-Timezone': 'Asia/Shanghai',
  };
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; }
}
function saveState(s) {
  try { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); } catch { /* ignore */ }
}

function clickPayload(t) {
  return {
    type: 3,
    nonce: randDigits(19),
    guild_id: t.guild,
    channel_id: t.channel,
    message_flags: 0,
    message_id: t.message,
    application_id: t.appId,
    session_id: randHex(32),
    data: { component_type: 2, custom_id: t.customId },
  };
}

async function click(token, t) {
  const res = await fetch('https://discord.com/api/v9/interactions', {
    method: 'POST',
    headers: headers(token, t),
    body: JSON.stringify(clickPayload(t)),
    signal: AbortSignal.timeout(20000),
  });
  return { status: res.status, body: (await res.text()).slice(0, 300) };
}

// 读面板消息里的到期时间戳（Antagonix 的 embed 里有 <t:...>）
async function readExpiry(token, t) {
  if (!t.expiryFromEmbed) return null;
  try {
    const res = await fetch(
      `https://discord.com/api/v9/channels/${t.channel}/messages?limit=20`,
      { headers: headers(token, t), signal: AbortSignal.timeout(20000) },
    );
    if (!res.ok) return null;
    const msgs = await res.json();
    for (const m of msgs) {
      const hasBtn = (m.components || []).some((row) =>
        (row.components || []).some((c) => c.custom_id === t.customId));
      if (!hasBtn) continue;
      for (const e of m.embeds || []) {
        for (const f of e.fields || []) {
          const mm = String(f.value || '').match(/<t:(\d+):/);
          if (mm) return { ts: Number(mm[1]), messageId: m.id };
        }
      }
    }
  } catch { /* ignore */ }
  return null;
}

// 连网关抓 bot 的 ephemeral 回复
function watchReplies(token, t, trigger) {
  return new Promise((resolve) => {
    const ws = new WebSocket('wss://gateway.discord.gg/?v=9&encoding=json');
    let seq = null, hb = null, done = false;
    const texts = [];
    const finish = (r) => {
      if (done) return;
      done = true;
      clearInterval(hb);
      try { ws.close(); } catch { /* ignore */ }
      resolve(r);
    };
    ws.on('message', async (raw) => {
      let p; try { p = JSON.parse(raw); } catch { return; }
      if (p.s != null) seq = p.s;
      if (p.op === 10) {
        hb = setInterval(() => { try { ws.send(JSON.stringify({ op: 1, d: seq })); } catch { /* ignore */ } }, p.d.heartbeat_interval);
        ws.send(JSON.stringify({
          op: 2,
          d: {
            token, capabilities: 30717, compress: false,
            properties: {
              os: 'Windows', browser: 'Edge', device: '', system_locale: 'zh-CN',
              browser_user_agent: UA, browser_version: '156.0.0.0', os_version: '10',
              release_channel: 'stable', client_build_number: 661871,
            },
          },
        }));
      }
      if (p.op === 0 && p.t === 'READY') setTimeout(trigger, 1200);
      if (p.op === 0 && (p.t === 'MESSAGE_CREATE' || p.t === 'MESSAGE_UPDATE') && p.d.author?.id === t.appId) {
        const m = p.d;
        if (m.content) texts.push(m.content);
        for (const e of m.embeds || []) {
          if (e.title) texts.push(e.title);
          if (e.description) texts.push(e.description);
          for (const f of e.fields || []) texts.push(`${f.name}: ${f.value}`);
        }
      }
      if (p.op === 0 && p.t === 'INTERACTION_SUCCESS') setTimeout(() => finish({ ok: true, texts }), 3500);
    });
    ws.on('error', () => {});
    setTimeout(() => finish({ ok: null, texts }), LISTEN_MS);
  });
}

function classify(t, texts) {
  // 只挑"短句回执"来判断：bot 的结果文案都很短（<120 字），
  // 而面板 embed 的字段动辄上百字，混进来容易误判。
  const replies = texts.filter((x) => String(x).length <= 120);
  for (const r of replies) {
    if (t.tooEarlyRe.test(r)) return { verdict: 'too-early', note: r.trim() };
  }
  for (const r of replies) {
    if (t.successRe.test(r)) return { verdict: 'renewed', note: r.trim() };
  }
  if (texts.length === 0) return { verdict: 'no-reply', note: '' };
  return { verdict: 'unknown', note: replies.slice(0, 2).join(' | ').slice(0, 200) };
}

// ---------------- 单个目标处理 ----------------
async function processTarget(name, t, state) {
  const tag = name;
  const token = process.env[t.tokenEnv];
  if (!token) { log(tag, `跳过：缺少 ${t.tokenEnv}`); return; }

  const st = state[name] || {};
  const nowSec = Math.floor(Date.now() / 1000);

  log(tag, `=== ${t.label} ===`);

  // 面板当前到期时间
  const exp = await readExpiry(token, t);
  if (exp) {
    const days = (exp.ts - nowSec) / 86400;
    log(tag, `到期 ${new Date(exp.ts * 1000).toISOString().replace('T', ' ').slice(0, 19)} UTC（剩余 ${days.toFixed(2)} 天）`);
    st.expiresAt = new Date(exp.ts * 1000).toISOString();
    st.remainingDays = Number(days.toFixed(2));
    if (exp.messageId && exp.messageId !== t.message) {
      log(tag, `⚠️ 面板消息已变为 ${exp.messageId}（原 ${t.message}），本次按新 id 使用`);
      t.message = exp.messageId;
    }
  }

  // 间隔判定
  const lastTs = st.last_click_at ? Math.floor(Date.parse(st.last_click_at) / 1000) : null;
  const daysSince = lastTs ? (nowSec - lastTs) / 86400 : null;
  if (daysSince !== null) log(tag, `距上次成功点击 ${daysSince.toFixed(2)} 天`);

  // 用到期时间做兜底判定：剩余天数还多就不用点
  let needByExpiry = true;
  if (st.remainingDays != null && t.manageable) {
    // 可随时点的服务：按「有效期 - 间隔」做兜底，剩余不足才补点
    const floor = (t.renewalDays || 5) - t.intervalDays + 0.5;
    needByExpiry = st.remainingDays <= floor;
    st._floor = floor;
    log(tag, `按到期判定：${needByExpiry ? '需要续期' : `剩余 ${st.remainingDays} 天，暂不续（阈值 ${st._floor} 天）`}`);
  }

  if (!FORCE) {
    if (daysSince !== null && daysSince < t.intervalDays) {
      log(tag, `⏭️ 距上次不足 ${t.intervalDays} 天，跳过`);
      st.last_check_at = new Date().toISOString();
      state[name] = st;
      return;
    }
    if (!needByExpiry) {
      log(tag, '⏭️ 剩余有效期充足，跳过');
      st.last_check_at = new Date().toISOString();
      state[name] = st;
      return;
    }
  }

  if (DRY_RUN) { log(tag, 'DRY_RUN=1，不点击'); state[name] = st; return; }

  log(tag, `🎯 点击 ${t.customId}`);
  let result;
  if (USE_GATEWAY) {
    result = await watchReplies(token, t, async () => {
      const r = await click(token, t);
      log(tag, `📡 点击返回 HTTP ${r.status}${r.body ? ' ' + r.body : ''}`);
    });
    if (result.texts.length) {
      log(tag, `网关捕获 ${result.texts.length} 段文本`);
      for (const x of result.texts) log(tag, `   ▸ ${String(x).replace(/\n/g, ' | ').slice(0, 160)}`);
    }
  } else {
    const r = await click(token, t);
    log(tag, `📡 点击返回 HTTP ${r.status}`);
    result = { ok: r.status === 204, texts: [] };
  }

  const cls = classify(t, result.texts);
  st.last_check_at = new Date().toISOString();
  st.last_verdict = cls.verdict;
  st.last_note = cls.note || null;

  if (cls.verdict === 'renewed') {
    st.last_click_at = new Date().toISOString();
    delete st.last_verdict;
    log(tag, `✅ 续期成功：${cls.note}`);
    // 续完再读一次确认新到期
    const after = await readExpiry(token, t);
    if (after) {
      st.expiresAt = new Date(after.ts * 1000).toISOString();
      st.remainingDays = Number(((after.ts - Date.now() / 1000) / 86400).toFixed(2));
      log(tag, `   新到期 ${st.expiresAt}（剩余 ${st.remainingDays} 天）`);
    }
  } else if (cls.verdict === 'too-early') {
    log(tag, `⏳ 未到可续期时间（${cls.note}）`);
  } else if (cls.verdict === 'no-reply' && result.ok) {
    // 没有文案但 204 且 INTERACTION_SUCCESS，按成功处理（Antagonix 常这样）
    st.last_click_at = new Date().toISOString();
    log(tag, '✅ 点击被受理（无回执文案，按成功计）');
  } else {
    log(tag, `⚠️ 结果不明：${cls.note || '(无回复)'}`);
  }

  state[name] = st;
}

// ---------------- 主流程 ----------------
(async () => {
  const state = loadState();
  if (!state._meta) state._meta = {};
  state._meta.last_run = new Date().toISOString();

  let names = Object.keys(TARGETS);
  if (TARGET !== 'all') names = names.filter((n) => n === TARGET);
  if (!names.length) { console.error(`未知 DR_TARGET=${TARGET}`); process.exit(2); }

  if (DRY_RUN) log('main', 'DRY_RUN 模式');
  log('main', `处理目标：${names.join(', ')}`);

  for (const n of names) {
    try {
      await processTarget(n, TARGETS[n], state);
    } catch (e) {
      log(n, `❌ 异常：${e.message}`);
      state[n] = state[n] || {};
      state[n].last_error = e.message;
    }
    log(n, '');
  }

  saveState(state);
  log('main', '完成');
  process.exit(0);
})();
