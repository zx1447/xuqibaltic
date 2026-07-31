// Siam-Node Cloud auto check-in via Discord token + Playwright
const { chromium } = require('playwright');
const fs = require('fs');

const DISCORD_TOKEN = process.env.SIAM_DISCORD_TOKEN;
const STATE_FILE = 'siam_state.json';
const BASE = 'https://my.siam-node.cloud';
const CLIENT_ID = '1415389053955739753';
const REDIRECT_URI = `${BASE}/DISCORDOAUTH2/process-oauth.php`;
const CHECKIN_URL = `${BASE}/api/checkin.php`;

if (!DISCORD_TOKEN) {
  console.error('[siam] ERROR: missing SIAM_DISCORD_TOKEN');
  process.exit(1);
}

function log(msg) { console.log(`[siam] ${msg}`); }
function loadState() { try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; } }
function saveState(s) { fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); }

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });
  const page = await context.newPage();
  const state = loadState();

  try {
    log('=== Siam check-in start ===');

    // 1. 尝试复用 session (从 state 加载 cookie)
    let loggedIn = false;
    if (state.cookies) {
      log('♻️ 尝试复用 session...');
      await context.addCookies(state.cookies);
      await page.goto(`${BASE}/?p=topup`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(2000);
      const url = page.url();
      if (!url.includes('login')) {
        log('✅ Session 有效');
        loggedIn = true;
      } else {
        log('⚠️ Session 失效');
      }
    }

    // 2. 如果没登录, 用 Discord token 走 OAuth
    if (!loggedIn) {
      log('🎫 Discord OAuth 登录...');
      // 用 Discord API 获取 code
      const authResp = await page.evaluate(async ({ token, clientId }) => {
        const r = await fetch(`https://discord.com/api/v9/oauth2/authorize?client_id=${clientId}&response_type=code&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&scope=identify+email&prompt=none`, {
          method: 'POST',
          headers: { 'Authorization': token, 'Content-Type': 'application/json' },
          body: JSON.stringify({ authorize: true, integration_type: 0 }),
        });
        return await r.json();
      }, { token: DISCORD_TOKEN, clientId: CLIENT_ID });

      const location = authResp.location || '';
      const codeMatch = location.match(/code=([^&]+)/);
      if (!codeMatch) {
        log(`❌ Discord OAuth 失败: ${JSON.stringify(authResp).slice(0, 200)}`);
        process.exit(1);
      }
      log(`✅ Discord code 获取成功`);

      // 访问 callback (Playwright 能过 CF)
      await page.goto(location, { waitUntil: 'networkidle', timeout: 30000 });
      await page.waitForTimeout(2000);
      const finalUrl = page.url();
      log(`✅ Callback 完成, URL: ${finalUrl}`);

      if (finalUrl.includes('login')) {
        log('❌ 仍在 login 页面, OAuth 可能失败');
        process.exit(1);
      }
      log('✅ 登录成功');
    }

    // 3. 签到循环 (最多 6 次)
    let count = 0;
    let earned = 0;
    for (let i = 0; i < 6; i++) {
      log(`🖱️ 签到 #${i + 1}/6...`);
      const result = await page.evaluate(async () => {
        const r = await fetch('/api/checkin.php', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' },
          body: 'action=checkin',
        });
        return await r.json();
      }).catch(e => ({ status: 'error', message: e.message }));

      log(`📡 ${JSON.stringify(result).slice(0, 200)}`);

      if (result.status === 'error') {
        log(`⚠️ 签到失败: ${result.message}`);
        break;
      }
      if (result.status === 'success') {
        count++;
        earned += result.amount || 0;
        log(`✅ +${result.amount} ฿ (余额: ${result.balance} ฿)`);
        if (result.remaining === 0 || result.remaining === '0') {
          log('ℹ️ 今日签到次数用完');
          break;
        }
      }
      await page.waitForTimeout(2000);
    }

    // 4. 保存 cookies
    const cookies = await context.cookies();
    state.cookies = cookies;
    state.last_checkin_time = new Date().toISOString();
    state.last_checkin_count = count;
    saveState(state);
    log(`🎉 签到完成: ${count} 次, +${earned} ฿`);
    log(`💾 Session 已保存 (${cookies.length} cookies)`);
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    // 清除 session
    state.cookies = null;
    saveState(state);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
