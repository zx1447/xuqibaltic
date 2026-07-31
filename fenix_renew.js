// FenixHost service auto-renew via Discord token + Playwright
// 用 Discord token 走 OAuth 登录 fenixhost, 然后点击 Renovar 按钮
const { chromium } = require('playwright');

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const FENIX_SERVICE = process.env.FENIX_SERVICE || '513';
const FENIX_CLIENT_ID = '1367158139694223486';

if (!DISCORD_TOKEN || !FENIX_SERVICE) {
  console.error('[fenix] ERROR: missing DISCORD_TOKEN or FENIX_SERVICE');
  process.exit(1);
}

function log(msg) { console.log(`[fenix] ${msg}`); }

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });
  const page = await context.newPage();

  try {
    log('=== FenixHost auto-renew start ===');

    // 1. 访问 fenixhost OAuth 入口, 拿 state
    log('getting OAuth state...');
    await page.goto('https://fenixhost.net/oauth/discord', { waitUntil: 'domcontentloaded', timeout: 30000 });
    // 等 redirect 完成
    await page.waitForTimeout(3000);
    const discordUrl = page.url();
    log(`current URL: ${discordUrl}`);
    // state 可能在 URL 里, 或在 redirect_to 参数里 (URL encoded)
    let state = '';
    const directMatch = discordUrl.match(/state=([^&]+)/);
    if (directMatch) {
      state = directMatch[1];
    } else {
      // 从 redirect_to 参数提取 (URL encoded)
      const redirectMatch = discordUrl.match(/redirect_to=([^&]+)/);
      if (redirectMatch) {
        const decoded = decodeURIComponent(redirectMatch[1]);
        const stateInRedirect = decoded.match(/state=([^&]+)/);
        if (stateInRedirect) state = stateInRedirect[1];
      }
    }
    if (!state) {
      log('failed to get OAuth state, trying manual redirect...');
      // 可能 CF 拦了, 试直接访问 discord OAuth URL
      // 先从 fenixhost 拿 state (通过 API)
      const stateResp = await page.evaluate(async () => {
        const r = await fetch('https://fenixhost.net/oauth/discord', { redirect: 'manual' });
        return { status: r.status, location: r.headers.get('location') };
      });
      log(`manual fetch: ${JSON.stringify(stateResp)}`);
      if (stateResp.location) {
        const m = stateResp.location.match(/state=([^&]+)/);
        if (m) {
          log(`got state from manual: ${m[1].slice(0, 20)}...`);
          // 用这个 state + location
          await page.goto(stateResp.location, { waitUntil: 'domcontentloaded' });
        }
      }
      // 再检查
      const url2 = page.url();
      const m2 = url2.match(/state=([^&]+)/);
      if (!m2) {
        log(`still no state, URL: ${url2}`);
        process.exit(1);
      }
      state = '';
    }
    log(`state: ${state.slice(0, 20)}...`);

    // 2. 用 Discord token 获取 OAuth code
    log('getting OAuth code via Discord API...');
    const authResp = await page.evaluate(async ({ token, clientId, state }) => {
      const r = await fetch(`https://discord.com/api/v9/oauth2/authorize?client_id=${clientId}&response_type=code&redirect_uri=${encodeURIComponent('https://fenixhost.net/oauth/discord/callback')}&scope=identify+email&prompt=none&state=${state}`, {
        method: 'POST',
        headers: {
          'Authorization': token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ authorize: true, integration_type: 0 }),
      });
      return await r.json();
    }, { token: DISCORD_TOKEN, clientId: FENIX_CLIENT_ID, state });

    const location = authResp.location || '';
    const codeMatch = location.match(/code=([^&]+)/);
    if (!codeMatch) {
      log(`failed to get OAuth code: ${JSON.stringify(authResp).slice(0, 200)}`);
      process.exit(1);
    }
    const code = codeMatch[1];
    log(`code: ${code.slice(0, 20)}...`);

    // 3. 用 code 登录 fenixhost
    log('logging in to fenixhost...');
    await page.goto(`https://fenixhost.net/oauth/discord/callback?code=${code}&state=${state}`, { waitUntil: 'networkidle', timeout: 30000 });
    const finalUrl = page.url();
    log(`login final URL: ${finalUrl}`);

    // 4. 访问 service 页面
    log(`visiting service ${FENIX_SERVICE}...`);
    await page.goto(`https://fenixhost.net/services/${FENIX_SERVICE}`, { waitUntil: 'networkidle', timeout: 30000 });

    // 5. 点击 Renovar 按钮
    log('clicking Renovar...');
    const renewBtn = await page.locator('button:has-text("Renovar")').first();
    if (!(await renewBtn.isVisible())) {
      log('Renovar button not found');
      process.exit(1);
    }
    await renewBtn.click();
    log('clicked, waiting for response...');

    // 6. 等待结果 (toast 或页面变化)
    await page.waitForTimeout(3000);

    // 检查是否成功
    const bodyText = await page.evaluate(() => document.body.innerText);
    if (bodyText.toLowerCase().includes('renewed') || bodyText.toLowerCase().includes('next renewal')) {
      const match = bodyText.match(/(?:renewed|next renewal)[^\n]{0,80}/i);
      log(`✅ renew success: ${match ? match[0] : 'ok'}`);
      process.exit(0);
    } else if (bodyText.includes('Server Error')) {
      log('❌ server error after renew');
      process.exit(1);
    } else {
      log(`renew result unclear, body snippet: ${bodyText.slice(0, 300)}`);
      // 检查 RENOVACIÓN 时间
      const renovMatch = bodyText.match(/RENOVACIÓN[\s\S]{0,40}/);
      if (renovMatch) log(`RENOVACIÓN: ${renovMatch[0]}`);
      process.exit(0);  // 假设成功
    }
  } catch (e) {
    log(`error: ${e.message}`);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
