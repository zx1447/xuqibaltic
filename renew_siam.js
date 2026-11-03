// Siam-Node Cloud auto check-in via Discord token + Playwright
//
// 相对旧版的关键改动
// ------------------
// 1. 每次签到都打印获得后的总积分：
//      本次 +5 ฿ | 今日累计 +5 ฿ | 总积分 320 ฿ | 剩余次数 5
// 2. Cloudflare 挑战会被真正"尝试解开"：用有头 Chromium（runner 上 xvfb-run）
//    + 反自动化指纹，等 Just a moment... 自己跳到真实页面，成功后 cf_clearance
//    自动落进 context，后续请求就通了。纯 headless 基本必被拦。
// 3. "Session 有效" 不再靠 URL 判断（挑战页 URL 里没有 login，旧版因此谎报成功），
//    改为要求接口返回 JSON。
// 4. 返回 HTML 而不是 JSON 时报"被 Cloudflare 拦截"，不再抛裸 JSON SyntaxError。
// 5. Discord OAuth 改用 context.request 发（页面在挑战页上跨域 fetch discord 会被拦）。
// 6. 失败不再中断剩余次数；0 次签到成功则 exit 1，让 workflow 变红而不是假装正常。
//
// 可选环境变量
//   SIAM_CF_CLEARANCE  手动提供的 cf_clearance（本机浏览器复制）
//   SIAM_HEADFUL=1     有头模式（配合 xvfb-run），提高过挑战成功率
//   SIAM_CF_WAIT_MS    等待挑战通过的上限，默认 45000
const { chromium } = require('playwright');
const fs = require('fs');

const DISCORD_TOKEN = process.env.SIAM_DISCORD_TOKEN;
const CF_CLEARANCE = (process.env.SIAM_CF_CLEARANCE || '').trim();
const HEADFUL = process.env.SIAM_HEADFUL === '1';
const PROXY = (process.env.SIAM_PROXY || '').trim();   // 例如 socks5://127.0.0.1:40000 (Cloudflare WARP)
const CF_WAIT_MS = Math.max(5000, parseInt(process.env.SIAM_CF_WAIT_MS || '45000', 10));
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
  const head = text.slice(0, 3000).toLowerCase();
  return head.includes('just a moment')
    || head.includes('cf-mitigated')
    || head.includes('__cf_chl')
    || head.includes('challenge-platform')
    || head.includes('enable javascript and cookies to continue');
}

async function isChallenged(page) {
  try {
    const html = await page.content();
    if (looksLikeChallenge(html)) return true;
    const title = await page.title().catch(() => '');
    if (/just a moment|attention required|请稍候/i.test(title)) return true;
    const body = await page.evaluate(() => (document.body && document.body.innerText || '').slice(0, 500)).catch(() => '');
    return /verify you are human|checking your browser|进行安全验证|正在验证/i.test(body);
  } catch { return false; }
}

// 等 Cloudflare 挑战自己通过（有头模式才可能真的过）
async function passChallenge(page, label) {
  if (!(await isChallenged(page))) return true;
  log(`🧱 ${label}: 遇到 Cloudflare 挑战，等待其通过（最多 ${Math.round(CF_WAIT_MS / 1000)}s）...`);
  const deadline = Date.now() + CF_WAIT_MS;
  while (Date.now() < deadline) {
    await page.waitForTimeout(2500);
    if (!(await isChallenged(page))) {
      log(`✅ ${label}: Cloudflare 挑战通过`);
      return true;
    }
  }
  log(`❌ ${label}: Cloudflare 挑战未通过（headless=${!HEADFUL}）`);
  return false;
}

// 页面内请求；区分"挑战页"和"接口返回错误对象"
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

// 尝试从钱包页抓当前总积分（签到响应没给余额时兜底）
async function readBalance(page, label) {
  try {
    await page.goto(`${BASE}/?p=topup`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (!(await passChallenge(page, label || '钱包页'))) return null;
    await page.waitForTimeout(1500);
    const body = await page.evaluate(() => document.body.innerText || '');
    const matches = body.match(/([\d,]+(?:\.\d+)?)\s*฿/g) || [];
    const numbers = matches
      .map(m => parseFloat(m.replace(/[^\d.]/g, '')))
      .filter(n => Number.isFinite(n));
    if (numbers.length) return Math.max(...numbers);
  } catch (e) { /* ignore */ }
  return null;
}

async function gotoWithChallenge(page, url, label) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  return passChallenge(page, label);
}

(async () => {
  const launchOpts = {
    headless: !HEADFUL,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--disable-features=IsolateOrigins,site-per-process',
    ],
  };
  if (PROXY) launchOpts.proxy = { server: PROXY };
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({
    userAgent: UA,
    viewport: { width: 1366, height: 900 },
    locale: 'en-US',
    timezoneId: 'America/Chicago',
    deviceScaleFactor: 1,
    extraHTTPHeaders: {
      'accept-language': 'en-US,en;q=0.9',
      'sec-ch-ua': '"Chromium";v="150", "Microsoft Edge";v="150", "Not=A?Brand";v="24"',
      'sec-ch-ua-mobile': '?0',
      'sec-ch-ua-platform': '"Windows"',
    },
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  });

  const page = await context.newPage();
  const state = loadState();
  let count = 0;
  let earned = 0;
  let balanceTotal = null;
  let blockReason = '';
  let quotaDone = false;

  const persistCookies = async () => {
    try {
      const cookies = await context.cookies();
      if (cookies && cookies.length) {
        state.cookies = cookies;
        state.last_workflow_end_time = new Date().toISOString();
        saveState(state);
        log(`💾 Session 已保存 (${cookies.length} cookies: ${cookies.map(c => c.name).join(',')})`);
      }
    } catch { /* ignore */ }
  };

  const summary = () => {
    log(`📊 今日汇总: 成功 ${count} 次, 共 +${earned} ฿, 当前总积分 ${balanceTotal !== null ? balanceTotal + ' ฿' : '未知'}`);
    if (blockReason) log(`⛔ 失败原因: ${blockReason}`);
  };

  try {
    log(`=== Siam check-in start (headful=${HEADFUL}, proxy=${PROXY || '直接连接'}) ===`);

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

    // 1. 复用上次 session
    let loggedIn = false;
    if (state.cookies && state.cookies.length) {
      log('♻️ 尝试复用 session...');
      await context.addCookies(state.cookies);
      const passed = await gotoWithChallenge(page, `${BASE}/?p=topup`, '复用 session');
      const probe = await callJson(page, CHECKIN_URL, { method: 'GET' });
      if (looksLikeChallenge(probe.snippet)) {
        if (!passed) {
          blockReason = 'Cloudflare 挑战拦住了站点（需要 cf_clearance 或本机/住宅 IP 运行）';
          log('🧱 复用 session 时仍被 Cloudflare 挑战拦住');
        } else {
          log('⚠️ 挑战已过但接口仍返回挑战页，稍后重试');
        }
      } else if (probe.json) {
        // 返回 JSON 不等于已登录：未登录时接口也回 JSON，只是内容是
        // {"status":"error","message":"กรุณาเข้าสู่ระบบก่อน"}（请先登录）
        const msg = String(probe.json.message || '');
        const needLogin = /เข้าสู่ระบบ|login|sign in|unauthor/i.test(msg)
          || probe.json.status === 'unauthorized';
        if (needLogin) {
          log(`⚠️ Session 已失效（接口提示: ${msg || probe.json.status}），改走 Discord OAuth`);
          await context.clearCookies({ name: 'PHPSESSID' }).catch(() => {});
          state.cookies = null;
        } else {
          log('✅ Session 有效（接口返回正常 JSON）');
          loggedIn = true;
        }
      } else if ((await page.url()).includes('login')) {
        log('⚠️ Session 失效（被重定向到登录页）');
      } else {
        log(`⚠️ Session 状态未知: HTTP ${probe.status} ${probe.contentType}`);
      }
    }

    // 2. Discord OAuth（用 context.request，避免挑战页里的跨域 fetch 失败）
    if (!loggedIn) {
      log('🎫 Discord OAuth 登录...');
      const authUrl = `https://discord.com/api/v9/oauth2/authorize?client_id=${CLIENT_ID}`
        + `&response_type=code&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&scope=identify+email&prompt=none`;
      const r = await context.request.post(authUrl, {
        headers: { 'Authorization': DISCORD_TOKEN, 'Content-Type': 'application/json' },
        data: { authorize: true, integration_type: 0 },
        failOnStatusCode: false,
      });
      const authText = await r.text();
      let authResp = null;
      try { authResp = JSON.parse(authText); } catch { /* not json */ }
      const location = (authResp && authResp.location) || '';
      const codeMatch = location.match(/code=([^&]+)/);
      if (!codeMatch) {
        const msg = (authResp && (authResp.message || authResp.error)) || authText.slice(0, 150);
        log(`❌ Discord OAuth 失败 (HTTP ${r.status()}): ${msg}`);
        blockReason = `Discord OAuth 失败: ${msg}（Discord token 可能已失效或被吊销）`;
        summary();
        process.exit(1);
      }
      log('✅ Discord code 获取成功');

      const cbOk = await gotoWithChallenge(page, location, 'OAuth callback');
      if (!cbOk) {
        blockReason = 'Cloudflare 挑战拦住了 OAuth callback，登录无法完成';
        log('🧱 callback 被 Cloudflare 挑战拦住，登录失败');
      } else if (page.url().includes('login')) {
        log('❌ 仍在 login 页面, OAuth 可能失败');
        blockReason = 'OAuth 回调后仍在登录页';
      } else {
        log(`✅ 登录成功, URL: ${page.url()}`);
        loggedIn = true;
      }
    }

    if (!loggedIn) {
      // 读一次余额（若挑战已过就顺手拿到总积分）然后收尾
      balanceTotal = await readBalance(page, '钱包页');
      if (balanceTotal !== null) log(`💰 当前总积分: ${balanceTotal} ฿`);
      log('❌ 未能登录，本次不进行签到');
      await persistCookies();
    } else {
      // 注意：不在这里抓钱包页余额。页面文案里的 "฿" 不一定是账户总额，
      // 签到接口返回的 balance 才是权威值，只在接口没给时才兜底抓取。
      // 3. 签到
      for (let i = 0; i < CHECKIN_TARGET; i++) {
        log(`🖱️ 每日签到 #${i + 1}/${CHECKIN_TARGET}...`);
        const res = await callJson(page, '/api/checkin.php', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' },
          body: 'action=checkin',
        });

        if (looksLikeChallenge(res.snippet)) {
          log(`🧱 #${i + 1} 被 Cloudflare 拦截 (HTTP ${res.status})：返回的是挑战页而不是 JSON`);
          blockReason = '签到接口被 Cloudflare 挑战拦截';
          if (balanceTotal === null) balanceTotal = await readBalance(page, '钱包页');
          break;
        }
        if (!res.json) {
          log(`⚠️ #${i + 1} 返回非 JSON (HTTP ${res.status})：${(res.snippet || res.error || '').slice(0, 150)}`);
          continue;
        }

        const result = res.json;
        log(`📡 ${JSON.stringify(result).slice(0, 200)}`);

        if (result.status !== 'success') {
          const msg = String(result.message || result.status);
          log(`⚠️ #${i + 1} 未成功: ${msg}`);
          // 今日 6 次额度用完 / 已经签到过，属于正常结束，不是故障
          if (/ครบแล้ว|ครบ\s*\d+\s*ครั้ง|already|额度|上限|used up|limit/i.test(msg)) {
            quotaDone = true;
            log('ℹ️ 判定为今日额度已用完，正常结束');
            break;
          }
          continue;
        }

        count++;
        earned += result.amount || 0;
        const reported = result.balance !== undefined && result.balance !== null
          ? result.balance
          : await readBalance(page, '钱包页');
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
        delete state.last_error;
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

      state.last_checkin_time = new Date().toISOString();
      state.last_checkin_count = count;
      state.last_balance = balanceTotal;
      state.checkin_mode = 'six_times_every_14_seconds_daily';
      state.checkin_interval_seconds = CHECKIN_INTERVAL_SECONDS;
      state.last_workflow_end_time = new Date().toISOString();
      saveState(state);
    }

    summary();
    await persistCookies();
    if (count === 0) {
      if (quotaDone) {
        log('ℹ️ 今日签到额度已经用完 —— 不算失败');
        process.exit(0);
      }
      log('❌ 本次运行 0 次签到成功 —— 标记为失败，避免继续假装正常');
      process.exit(1);
    }
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    state.last_error = String(e.message).slice(0, 300);
    state.last_workflow_end_time = new Date().toISOString();
    saveState(state);
    summary();
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
