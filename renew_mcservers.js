#!/usr/bin/env node
// McServers 免费 MC 服续期 —— 每 5 天点一次 Discord 面板的「Renew Server」
//
// 面板机器人：McServers | Free Hosting (1524370665631649865)
// 面板消息  ：1567495211016519802  频道 1567486173495173181
// 续期按钮  ：style 2, custom_id = "renew_server"
// 服务器    ：Server ID 72 / Identifier 697de3d4
//
// 请求格式
// -------
//   POST https://discord.com/api/v9/interactions
//   {
//     "type": 3,                    ← 组件交互必须是 3（不是 2）
//     "nonce": "<19 位数字>",
//     "guild_id": "1559978655688564848",
//     "channel_id": "1567486173495173181",
//     "message_flags": 0,           ← 必填
//     "message_id": "1567495211016519802",
//     "application_id": "1524370665631649865",   ← 用 bot 的用户 ID
//     "session_id": "<32 位十六进制>",
//     "data": { "component_type": 2, "custom_id": "renew_server" }
//   }
//
// 成功返回 HTTP 204。bot 的回复是 ephemeral（私密），普通接口看不到，
// 所以本脚本用网关抓 read-back，同时也尝试用面板接口读到期时间。
//
// 判定逻辑
// -------
//   1. 网关模式：点击后抓 bot 的私密回复，看到 "renew" 相关成功文案才算成功；
//      看到 "You can renew again later." 说明还在有效期内，本次跳过。
//   2. 兜底：点击返回 204 且 5 秒内没拿到失败文案，也算成功。
//
// 调度：每 5 天一次（cron 按日字段 */5 从 1 号起算）。
const WebSocket = require('ws');

const TOKEN = process.env.DISCORDERENEW_TOKEN || process.env.DISCORD_TOKEN;
const GUILD = process.env.DR_GUILD_ID || '1559978655688564848';
const CHANNEL = process.env.DR_CHANNEL_ID || '1567486173495173181';
const MESSAGE = process.env.DR_MESSAGE_ID || '1567495211016519802';
const APP_ID = process.env.DR_APP_ID || '1524370665631649865';
const CUSTOM_ID = process.env.DR_CUSTOM_ID || 'renew_server';
const STATE_FILE = process.env.DR_STATE_FILE || 'disordrenew_state.json';
const INTERVAL_DAYS = Number(process.env.DR_INTERVAL_DAYS || '5');
const FORCE = process.env.DR_FORCE === '1';
const DRY_RUN = process.env.DR_DRY_RUN === '1';
const USE_GATEWAY = process.env.DR_GATEWAY !== '0';
const LISTEN_MS = Number(process.env.DR_LISTEN_MS || '20000');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0';

const fs = require('fs');
const log = (m) => console.log(`[disordrenew] ${m}`);
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

function headers() {
  return {
    Authorization: TOKEN,
    'Content-Type': 'application/json',
    'User-Agent': UA,
    Accept: '*/*',
    Origin: 'https://discord.com',
    Referer: `https://discord.com/channels/${GUILD}/${CHANNEL}`,
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

function clickPayload() {
  return {
    type: 3,
    nonce: randDigits(19),
    guild_id: GUILD,
    channel_id: CHANNEL,
    message_flags: 0,
    message_id: MESSAGE,
    application_id: APP_ID,
    session_id: randHex(32),
    data: { component_type: 2, custom_id: CUSTOM_ID },
  };
}

// 用网关抓 bot 的 ephemeral 回复
function watchReplies(trigger) {
  return new Promise((resolve) => {
    const ws = new WebSocket('wss://gateway.discord.gg/?v=9&encoding=json');
    let seq = null;
    let hb = null;
    let done = false;
    const texts = [];
    const finish = (result) => {
      if (done) return;
      done = true;
      clearInterval(hb);
      try { ws.close(); } catch { /* ignore */ }
      resolve(result);
    };

    ws.on('message', async (raw) => {
      let p; try { p = JSON.parse(raw); } catch { return; }
      if (p.s != null) seq = p.s;

      if (p.op === 10) {
        hb = setInterval(() => { try { ws.send(JSON.stringify({ op: 1, d: seq })); } catch { /* ignore */ } }, p.d.heartbeat_interval);
        ws.send(JSON.stringify({
          op: 2,
          d: {
            token: TOKEN, capabilities: 30717, compress: false,
            properties: {
              os: 'Windows', browser: 'Edge', device: '', system_locale: 'zh-CN',
              browser_user_agent: UA, browser_version: '156.0.0.0', os_version: '10',
              release_channel: 'stable', client_build_number: 661871,
            },
          },
        }));
      }
      if (p.op === 0 && p.t === 'READY') {
        log('网关已连接，发送点击…');
        setTimeout(trigger, 1200);
      }
      if (p.op === 0 && (p.t === 'MESSAGE_CREATE' || p.t === 'MESSAGE_UPDATE') && p.d.author?.id === APP_ID) {
        const m = p.d;
        if (m.content) texts.push(m.content);
        for (const e of m.embeds || []) {
          if (e.title) texts.push(e.title);
          if (e.description) texts.push(e.description);
          for (const f of e.fields || []) texts.push(`${f.name}: ${f.value}`);
        }
      }
      if (p.op === 0 && p.t === 'INTERACTION_SUCCESS') {
        // 点击被受理；再等一小会儿收文案
        setTimeout(() => finish({ ok: true, texts }), 3500);
      }
    });
    ws.on('error', (e) => log(`网关错误: ${e.message}`));
    setTimeout(() => finish({ ok: null, texts }), LISTEN_MS);
  });
}

async function clickOnly() {
  const res = await fetch('https://discord.com/api/v9/interactions', {
    method: 'POST', headers: headers(), body: JSON.stringify(clickPayload()),
  });
  return { status: res.status, body: (await res.text()).slice(0, 300) };
}

function classify(texts) {
  const all = texts.join('\n');
  if (/already|again later|not yet|cannot renew|can't renew|too early/i.test(all)) {
    if (/again later|not yet|too early/i.test(all)) return { verdict: 'too-early', note: all.slice(0, 200) };
  }
  if (/renew/i.test(all) && /success|renewed|extended|updated|has been/i.test(all)) {
    return { verdict: 'renewed', note: all.slice(0, 300) };
  }
  return { verdict: 'unknown', note: all.slice(0, 300) };
}

(async () => {
  if (!TOKEN) { console.error('[disordrenew] ERROR: 缺少 token'); process.exit(2); }

  const state = loadState();
  const nowSec = Math.floor(Date.now() / 1000);
  const lastTs = state.last_click_at ? Math.floor(Date.parse(state.last_click_at) / 1000) : null;
  const daysSince = lastTs ? (nowSec - lastTs) / 86400 : null;

  log(`=== McServers 续期检查 ===`);
  log(`面板消息 ${MESSAGE} | 按钮 ${CUSTOM_ID} | bot ${APP_ID}`);
  if (daysSince !== null) log(`距上次点击 ${daysSince.toFixed(2)} 天（${state.last_click_at}）`);
  else log('距上次点击：无记录');

  if (!FORCE && daysSince !== null && daysSince < INTERVAL_DAYS) {
    log(`⏭️ 距上次不足 ${INTERVAL_DAYS} 天，跳过`);
    state.last_check_at = new Date().toISOString();
    saveState(state);
    process.exit(0);
  }

  // 上一次确认还在有效期内，且距离那次检查不到 1 天，不必重复打扰
  if (!FORCE && state.last_verdict === 'too-early' && state.last_check_at) {
    const hrs = (nowSec - Math.floor(Date.parse(state.last_check_at) / 1000)) / 3600;
    if (hrs < 24) {
      log(`⏭️ 上次续期被拒（too-early）仅 ${hrs.toFixed(1)} 小时前，跳过`);
      process.exit(0);
    }
  }

  if (DRY_RUN) { log('DRY_RUN=1，不点击'); process.exit(0); }

  log(`🎯 点击 ${CUSTOM_ID}…`);
  let result;
  if (USE_GATEWAY) {
    result = await watchReplies(async () => {
      const r = await clickOnly();
      log(`📡 点击返回 HTTP ${r.status}${r.body ? ' ' + r.body : ''}`);
    });
    log(`网关捕获 ${result.texts.length} 段文本`);
    for (const t of result.texts) log(`   ▸ ${String(t).replace(/\n/g, ' | ').slice(0, 200)}`);
  } else {
    const r = await clickOnly();
    log(`📡 点击返回 HTTP ${r.status}`);
    result = { ok: r.status === 204, texts: [] };
  }

  const cls = classify(result.texts);

  // 尝试从回复里抠到期时间
  let expires = null;
  const expMatch = result.texts.join(' ').match(/Expires:?\s*([A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT)/);
  if (expMatch) expires = expMatch[1];

  state.last_check_at = new Date().toISOString();
  state.last_verdict = cls.verdict;
  state.last_note = cls.note || null;
  if (expires) { state.expires = expires; log(`到期时间：${expires}`); }

  if (cls.verdict === 'renewed' || (result.ok && cls.verdict === 'unknown')) {
    state.last_click_at = new Date().toISOString();
    delete state.last_verdict;
    log(`✅ 续期成功（判定：${cls.verdict}）`);
    log(`   下次计划 ${INTERVAL_DAYS} 天后`);
  } else if (cls.verdict === 'too-early') {
    log(`⏳ 服务器仍在有效期内，本次不续（${cls.note}）`);
  } else {
    log(`⚠️ 结果不明，返回码已记录，原文见上`);
  }

  saveState(state);
  process.exit(0);
})();
