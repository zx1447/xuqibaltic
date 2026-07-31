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
    await page.goto('https://fenixhost.net/oauth/discord', { waitUntil: 'networkidle', timeout: 30000 });
    const discordUrl = page.url();
    const stateMatch = discordUrl.match(/state=([^&]+)/);
    if (!stateMatch) {
      log('failed to get OAuth state');
      process.exit(1);
    }
    const state = stateMatch[1];
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
