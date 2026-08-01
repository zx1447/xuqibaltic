// DigitalPlat Domain auto-renew via GitHub OAuth + Playwright Firefox (bypass CF)
const { firefox } = require('playwright');

const GH_USER = process.env.DP_GH_USER || 'zx1447';
const GH_PASS = process.env.DP_GH_PASS || '';
const OAUTH_CLIENT_ID = 'Ov23liMuiWEyVv3b9R6u';
const BASE = 'https://dash.domain.digitalplat.org';

if (!GH_PASS) {
  console.error('[domain] ERROR: missing DP_GH_PASS');
  process.exit(1);
}

function log(msg) { console.log(`[domain] ${msg}`); }

(async () => {
  let browser;
  try {
    log('=== DigitalPlat Domain renew (Firefox) ===');
    
    // 用 firefox (反检测比 chromium 好)
    browser = await firefox.launch({ headless: true });
    log('✅ Firefox launched');
    
    const context = await browser.newContext();
    const page = await context.newPage();

    // 1. 走 GitHub OAuth
    log('starting GitHub OAuth...');
    const oauthUrl = `https://github.com/login/oauth/authorize?client_id=${OAUTH_CLIENT_ID}&scope=user:email`;
    await page.goto(oauthUrl, { waitUntil: 'networkidle', timeout: 30000 });

    // GitHub 登录
    let url = page.url();
    if (url.includes('github.com/login')) {
      log('filling GitHub credentials...');
      await page.waitForSelector('input[name="login"]', { timeout: 10000 });
      await page.fill('input[name="login"]', GH_USER);
      await page.fill('input[name="password"]', GH_PASS);
      await page.click('input[type="submit"], button[type="submit"]');
      await page.waitForTimeout(5000);
    }

    // Authorize 按钮
    url = page.url();
    if (url.includes('github.com/login/oauth/authorize')) {
      log('authorizing DigitalPlat...');
      const authBtn = page.locator('button:has-text("Authorize")').first();
      if (await authBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
        await authBtn.click();
        await page.waitForTimeout(10000);
      }
    }

    // 等 CF challenge 过
    url = page.url();
    log(`after OAuth, URL: ${url}`);
    log('waiting for CF challenge...');
    for (let i = 0; i < 6; i++) {
      await page.waitForTimeout(10000);
      url = page.url();
      const title = await page.title().catch(() => '');
      log(`  attempt ${i+1}: title=${title.slice(0,40)}`);
      if (!title.includes('moment') && !title.includes('verification') && !title.includes('Just a')) {
        log('✅ CF passed!');
        break;
      }
    }

    // 2. 访问 domains 页面
    log('visiting domains page...');
    await page.goto(`${BASE}/domains`, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(5000);

    // 3. 列出域名 + 续期
    const result = await page.evaluate(async () => {
      const csrf = document.cookie.match(/panel_csrf_token=([^;]+)/)?.[1] || '';
      const r = await fetch('/_panel_api/api/domains', {
        headers: { 'Accept': 'application/json' },
      });
      return await r.json();
    }).catch(e => ({ error: e.message }));

    if (result && result.domains) {
      log(`found ${result.domains.length} domains`);
      let renewed = 0;
      for (const dom of result.domains) {
        const name = dom.domain || dom.name || '?';
        const expiry = dom.expiry_date || dom.expires_at || '?';
        log(`  ${name} | expires: ${expiry}`);
        log(`  renewing ${name}...`);
        try {
          const renewRes = await page.evaluate(async (domain) => {
            const csrf = document.cookie.match(/panel_csrf_token=([^;]+)/)?.[1] || '';
            const r = await fetch(`/_panel_api/api/domains/${domain}/renew`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrf },
              body: '{}',
            });
            return { status: r.status, body: await r.text() };
          }, name);
          if (renewRes.status === 200) {
            log(`  ✅ ${name} renewed!`);
            renewed++;
          } else {
            log(`  ⚠️ ${name}: HTTP ${renewRes.status} ${renewRes.body.slice(0,100)}`);
          }
        } catch (e) {
          log(`  ❌ ${name}: ${e.message}`);
        }
        await page.waitForTimeout(2000);
      }
      log(`🎉 Renewed ${renewed}/${result.domains.length} domains`);
    } else {
      log(`result: ${JSON.stringify(result).slice(0, 200)}`);
    }
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
