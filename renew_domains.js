// DigitalPlat Domain auto-renew via GitHub OAuth + camoufox (bypass CF)
const { chromium } = require('playwright');
const { firefox } = require('playwright');

const GH_USER = process.env.DP_GH_USER || 'zx1447';
const GH_PASS = process.env.DP_GH_PASS || '';
const OAUTH_CLIENT_ID = 'Ov23liMuiWEyVv3b9R6u';
const BASE = 'https://dash.domain.digitalplat.org';
const API_BASE = 'https://domain-api.digitalplat.org/api/v1';
const API_TOKEN = process.env.DIGITALPLAT_API_TOKEN || '';

if (!GH_PASS) {
  console.error('[domain] ERROR: missing DP_GH_PASS');
  process.exit(1);
}

function log(msg) { console.log(`[domain] ${msg}`); }

(async () => {
  let browser;
  try {
    log('=== DigitalPlat Domain renew (camoufox) ===');
    
    // 试 camoufox (反检测 Firefox), fallback to chromium
    try {
      const { Camoufox } = require('camoufox');
      browser = await Camoufox({ headless: true });
      log('✅ camoufox launched');
    } catch (e) {
      log('camoufox not available, using chromium: ' + e.message);
      browser = await chromium.launch({ headless: true });
    }
    
    const context = await browser.newContext();
    const page = await context.newPage();

    // 1. 走 GitHub OAuth (不需要过 dash CF)
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

    // 等 CF challenge 过 (camoufox 应该能过)
    url = page.url();
    log(`after OAuth, URL: ${url}`);
    
    // 等 dash 页面加载 (CF challenge 可能需要时间)
    log('waiting for CF challenge to pass...');
    for (let i = 0; i < 6; i++) {
      await page.waitForTimeout(10000);
      url = page.url();
      const title = await page.title().catch(() => '');
      log(`  attempt ${i+1}: URL=${url.slice(0,60)}, title=${title.slice(0,30)}`);
      if (!title.includes('moment') && !title.includes('verification')) {
        log('✅ CF passed');
        break;
      }
    }

    // 2. 获取 cookies (用于 API 调用)
    const cookies = await context.cookies();
    const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join('; ');
    const csrf = cookies.find(c => c.name === 'panel_csrf_token')?.value || '';
    
    // 3. 用 API 列出域名 (直接用 API token, 不需要 cookie)
    if (API_TOKEN) {
      log('fetching domains via API...');
      // 用浏览器 fetch (过 CF)
      const domainsData = await page.evaluate(async (apiBase) => {
        const r = await fetch(`${apiBase}/domains`, {
          headers: { 'Accept': 'application/json' },
        });
        return await r.json();
      }, API_BASE).catch(e => ({ error: e.message }));

      if (domainsData.error) {
        log(`❌ API error: ${domainsData.error}`);
      } else if (domainsData.domains) {
        log(`found ${domainsData.domains.length} domains`);
        for (const dom of domainsData.domains) {
          const name = dom.domain || dom.name || '?';
          const expiry = dom.expiry_date || dom.expires_at || '?';
          log(`  ${name} | expires: ${expiry}`);
        }
      } else {
        log(`API response: ${JSON.stringify(domainsData).slice(0, 200)}`);
      }
    }

    // 4. 用 dash 页面调 renew API (通过浏览器, 过 CF)
    log('visiting domains page...');
    await page.goto(`${BASE}/domains`, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(5000);

    // 获取域名列表 (从页面或 API)
    const renewResult = await page.evaluate(async () => {
      const csrf = document.cookie.match(/panel_csrf_token=([^;]+)/)?.[1] || '';
      const r = await fetch('/_panel_api/api/domains', {
        headers: { 'Accept': 'application/json' },
      });
      return await r.json();
    }).catch(e => ({ error: e.message }));

    if (renewResult && renewResult.domains) {
      log(`found ${renewResult.domains.length} domains from dash`);
      let renewed = 0;
      for (const dom of renewResult.domains) {
        const name = dom.domain || dom.name || '?';
        log(`  renewing ${name}...`);
        try {
          const result = await page.evaluate(async (domain) => {
            const csrf = document.cookie.match(/panel_csrf_token=([^;]+)/)?.[1] || '';
            const r = await fetch(`/_panel_api/api/domains/${domain}/renew`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'x-csrf-token': csrf,
              },
              body: JSON.stringify({}),
            });
            return { status: r.status, body: await r.text() };
          }, name);
          if (result.status === 200) {
            log(`  ✅ ${name} renewed`);
            renewed++;
          } else {
            log(`  ⚠️ ${name}: HTTP ${result.status}`);
          }
        } catch (e) {
          log(`  ❌ ${name}: ${e.message}`);
        }
        await page.waitForTimeout(2000);
      }
      log(`🎉 Renewed ${renewed}/${renewResult.domains.length} domains`);
    } else {
      log(`dash API response: ${JSON.stringify(renewResult).slice(0, 200)}`);
    }

    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
