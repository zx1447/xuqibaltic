// Siam-Node Cloud auto check-in via Discord token + Playwright
//
// Changes vs the previous version
// ------------------------------
// 1. Every check-in now prints the running total:
//      本次 +5 ฿ | 今日累计 +5 ฿ | 总积分 320 ฿ | 剩余次数 5
// 2. "Session 有效" is no longer decided by looking at the URL. The page is
//    asked for an authenticated endpoint and the response must be JSON. A
//    Cloudflare challenge page also has no "login" in the URL, which is why the
//    old check reported success and then the check-in POST returned HTML.
// 3. A check-in that returns HTML instead of JSON is reported for what it is
//    (Cloudflare challenge) instead of surfacing a bare JSON SyntaxError.
// 4. A failed attempt no longer aborts the remaining attempts.
// 5. Zero successful check-ins now exits non-zero, so the workflow turns red
//    instead of reporting success for a no-op run.
//
// Optional: SIAM_CF_CLEARANCE, the cf_clearance cookie value from a real
// browser. Required if the runner has to pass Cloudflare.
const { chromium } = require('playwright');
const fs = require('fs');

const DISCORD_TOKEN = process.env.SIAM_DISCORD_TOKEN;
const CF_CLEARANCE = (process.env.SIAM_CF_CLEARANCE || '').trim();
const STATE_FILE = 'siam_state.json';
const BASE = 'https://my.siam-node.cloud';
const CLIENT_ID = '1415389053955739753';
const REDIRECT_URI = `${BASE}/DISCORDOAUTH2/process-oauth.php`;
const CHECKIN_URL = `${BASE}/api/checkin.php`;
const CHECKIN_TARGET = Math.max(1, Math.min(6, parseInt(process.env.SIAM_CHECKIN_TARGET || '6', 10)));
const CHECKIN_INTERVAL_SECONDS = Math.max(1, parseInt(process.env.SIAM_CHECKIN_INTERVAL_SECONDS || '14', 10));
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0';

if (!DISCORD_TOKEN) {
  console.error('[siam] ERROR: missing SIAM_DISCORD_TOKEN');
  process.exit(1);
}

function log(msg) { console.log(`[siam] ${msg}`); }
function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }

function looksLikeChallenge(text) {
  if (!text) return false;
  const head = text.slice(0, 2000).toLowerCase();
  return head.includes('just a moment')
    || head.includes('cf-mitigated')
    || head.includes('__cf_chl')
    || head.includes('challenge-platform');
}

// Ask the site for a JSON endpoint from inside the page. Distinguishes
// "challenge page came back" from "endpoint returned an error object".
async function callJson(page, url, init) {
  return page.evaluate(async ({ url, init }) => {
    try {
      const r = await fetch(url, Object.assign({ credentials: 'include' }, init || {}));
      const text = await r.text();
      let parsed = null;
      try { parsed = JSON.parse(text); } catch (e) { /* not json */ }
      return {
        ok: r.ok,
        status: r.status,
        contentType: r.headers.get('content-type') || '',
        json: parsed,
        snippet: parsed ? null : text.slice(0, 300),
      };
    } catch (e) {
      return { ok: false, status: 0, contentType: '', json: null, snippet: null, error: e.message };
    }
  }, { url, init });
}

// Best-effort wallet balance, used when a check-in response omits it.
async function readBalance(page) {
  try {
    await page.goto(`${BASE}/?p=topup`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(1200);
    const body = await page.evaluate(() => document.body.innerText || '');
    const matches = body.match(/([\d,]+(?:\.\d+)?)\s*฿/g) || [];
    const numbers = matches
      .map(m => parseFloat(m.replace(/[^\d.]/g, '')))
      .filter(n => Number.isFinite(n));
    if (numbers.length) return Math.max(...numbers);
  } catch (e) { /* ignore */ }
  return null;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ userAgent: UA });
  const page = await context.newPage();
  const state = loadState();
  let count = 0;
  let earned = 0;
  let balanceTotal = null;

  try {
    log('=== Siam check-in start ===');

    if (CF_CLEARANCE) {
      log('🍪 使用提供的 cf_clearance');
      await context.addCookies([{
        name: 'cf_clearance',
        value: CF_CLEARANCE,
        domain: '.siam-node.cloud',
        path: '/',
        httpOnly: true,
        secure: true,
        sameSite: 'None',
      }]);
    }

    // 1. session reuse
    let loggedIn = false;
    if (state.cookies && state.cookies.length) {
      log('♻️ 尝试复用 session...');
      await context.addCookies(state.cookies);
      await page.goto(`${BASE}/?p=topup`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(2000);

      const probe = await callJson(page, `${BASE}/api/checkin.php`, { method: 'GET' });
      if (looksLikeChallenge(probe.snippet)) {
        log('🧱 复用 session 后仍被 Cloudflare 挑战拦住 (403 Just a moment...)');
        log('   → 需要 SIAM_CF_CLEARANCE，或改在本机运行');
      } else if (probe.json) {
        log('✅ Session 有效（接口返回 JSON）');
        loggedIn = true;
      } else if ((await page.url()).includes('login')) {
        log('⚠️ Session 失效（被重定向到登录页）');
      } else {
        log(`⚠️ Session 状态未知: HTTP ${probe.status} ${probe.contentType}`);
      }
    }

    // 2. Discord OAuth if needed
    if (!loggedIn) {
      log('🎫 Discord OAuth 登录...');
      const authResp = await page.evaluate(async ({ token, clientId, redirectUri }) => {
        const r = await fetch(`https://discord.com/api/v9/oauth2/authorize?client_id=${clientId}&response_type=code&redirect_uri=${encodeURIComponent(redirectUri)}&scope=identify+email&prompt=none`, {
          method: 'POST',
          headers: { 'Authorization': token, 'Content-Type': 'application/json' },
          body: JSON.stringify({ authorize: true, integration_type: 0 }),
        });
        return await r.json();
      }, { token: DISCORD_TOKEN, clientId: CLIENT_ID, redirectUri: REDIRECT_URI });

      const location = authResp.location || '';
      const codeMatch = location.match(/code=([^&]+)/);
      if (!codeMatch) {
        log(`❌ Discord OAuth 失败: ${JSON.stringify(authResp).slice(0, 200)}`);
        throw new Error('Discord OAuth did not return a code');
      }
      log('✅ Discord code 获取成功');

      await page.goto(location, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(1500);
      const html = await page.content();
      if (looksLikeChallenge(html)) {
        log('🧱 callback 被 Cloudflare 挑战拦住，登录失败');
      } else if (page.url().includes('login')) {
        log('❌ 仍在 login 页面, OAuth 可能失败');
        throw new Error('still on login page after OAuth');
      } else {
        log(`✅ 登录成功, URL: ${page.url()}`);
        loggedIn = true;
      }
    }

    // 3. check-ins
    balanceTotal = await readBalance(page);
    if (balanceTotal !== null) log(`💰 当前总积分: ${balanceTotal} ฿`);

    for (let i = 0; i < CHECKIN_TARGET; i++) {
      log(`🖱️ 每日签到 #${i + 1}/${CHECKIN_TARGET}...`);
      const res = await callJson(page, '/api/checkin.php', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' },
        body: 'action=checkin',
      });

      if (looksLikeChallenge(res.snippet)) {
        log(`🧱 #${i + 1} 被 Cloudflare 拦截 (HTTP ${res.status})：返回的是挑战页而不是 JSON`);
        log('   → 这就是历史上 107 次运行全部 0 签到的原因');
        if (balanceTotal === null) balanceTotal = await readBalance(page);
        break;
      }
      if (!res.json) {
        log(`⚠️ #${i + 1} 返回非 JSON (HTTP ${res.status})：${(res.snippet || res.error || '').slice(0, 150)}`);
        continue;
      }

      const result = res.json;
      log(`📡 ${JSON.stringify(result).slice(0, 200)}`);

      if (result.status !== 'success') {
        log(`⚠️ #${i + 1} 未成功: ${result.message || result.status}`);
        continue;
      }

      count++;
      earned += result.amount || 0;
      const reported = result.balance !== undefined && result.balance !== null
        ? result.balance
        : await readBalance(page);
      if (reported !== null) balanceTotal = reported;
      const remaining = result.remaining !== undefined ? result.remaining : (CHECKIN_TARGET - count);

      log(
        `✅ 本次 +${result.amount || 0} ฿ | 今日累计 +${earned} ฿`
        + ` | 总积分 ${balanceTotal !== null ? balanceTotal + ' ฿' : '未知'}`
        + ` | 剩余次数 ${remaining}`
      );

      state.last_checkin_time = new Date().toISOString();
      state.last_checkin_count = count;
      state.last_balance = balanceTotal;
      state.current_workflow_target = CHECKIN_TARGET;
      saveState(state);

      if (String(remaining) === '0') {
        log('ℹ️ 今日签到额度已经用完');
        break;
      }
      if (i + 1 < CHECKIN_TARGET) {
        log(`⏳ 等待 ${CHECKIN_INTERVAL_SECONDS} 秒后进行下一次签到`);
        await page.waitForTimeout(CHECKIN_INTERVAL_SECONDS * 1000);
      }
    }

    // 4. persist
    const cookies = await context.cookies();
    state.cookies = cookies;
    state.last_checkin_time = new Date().toISOString();
    state.last_checkin_count = count;
    state.last_balance = balanceTotal;
    state.checkin_mode = 'six_times_every_14_seconds_daily';
    state.checkin_interval_seconds = CHECKIN_INTERVAL_SECONDS;
    state.last_workflow_end_time = new Date().toISOString();
    saveState(state);

    log(`📊 今日汇总: 成功 ${count} 次, 共 +${earned} ฿, 当前总积分 ${balanceTotal !== null ? balanceTotal + ' ฿' : '未知'}`);
    log(`💾 Session 已保存 (${cookies.length} cookies)`);

    if (count === 0) {
      log('❌ 本次运行 0 次签到成功 —— 标记为失败，避免继续假装正常');
      process.exit(1);
    }
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    state.last_error = String(e.message).slice(0, 300);
    state.last_workflow_end_time = new Date().toISOString();
    saveState(state);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
