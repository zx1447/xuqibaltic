// DigitalPlat Domain auto-renew via GitHub OAuth + Playwright
// 策略: GitHub OAuth 登录 dash -> 列出域名 -> 对每个域名调 renew API
const { chromium } = require('playwright');
const fs = require('fs');

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
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });
  const page = await context.newPage();

  try {
    log('=== DigitalPlat Domain renew start ===');

    // 1. 先访问 dash (触发 CF challenge, Playwright 可能过)
    log('visiting dash (CF challenge)...');
    await page.goto(`${BASE}/auth/login`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(5000);

    // 检查是否过了 CF
    let url = page.url();
    if (url.includes('dash.domain.digitalplat.org') && !page.url().includes('login')) {
      log('CF passed (already logged in)');
    } else {
      // 2. 走 GitHub OAuth
      log('starting GitHub OAuth...');
      const oauthUrl = `https://github.com/login/oauth/authorize?client_id=${OAUTH_CLIENT_ID}&scope=user:email`;
      await page.goto(oauthUrl, { waitUntil: 'networkidle', timeout: 30000 });

      // 如果需要 GitHub 登录
      url = page.url();
      if (url.includes('github.com/login')) {
        log('filling GitHub credentials...');
        await page.waitForSelector('input[name="login"]', { timeout: 10000 });
        await page.fill('input[name="login"]', GH_USER);
        await page.fill('input[name="password"]', GH_PASS);
        await page.click('input[type="submit"], button[type="submit"]');
        await page.waitForTimeout(5000);
      }

      // 如果有 Authorize 按钮
      url = page.url();
      if (url.includes('github.com/login/oauth/authorize')) {
        log('authorizing DigitalPlat...');
        const authBtn = page.locator('button:has-text("Authorize")').first();
        if (await authBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
          await authBtn.click();
          await page.waitForTimeout(10000);
        }
      }

      // 检查是否登录成功 (回到 dash)
      url = page.url();
      log(`after OAuth, URL: ${url}`);
      if (url.includes('dash.domain.digitalplat.org') && !url.includes('login')) {
        log('✅ logged in to dash');
      } else if (url.includes('Performing security verification')) {
        log('❌ CF still blocking');
        process.exit(1);
      } else {
        log('⚠️ login status unclear, trying to continue...');
      }
    }

    // 3. 访问 domains 页面
    log('visiting domains page...');
    await page.goto(`${BASE}/domains`, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);

    // 4. 用 API 列出所有域名
    log('fetching domains via API...');
    const domainsData = await page.evaluate(async () => {
      const r = await fetch('/_panel_api/api/domains', {
        headers: { 'Accept': 'application/json' },
      });
      return await r.json();
    }).catch(e => ({ error: e.message }));

    if (domainsData.error) {
      log(`❌ API error: ${domainsData.error}`);
      process.exit(1);
    }

    // 解析域名列表
    let domains = [];
    if (Array.isArray(domainsData)) {
      domains = domainsData;
    } else if (domainsData.domains) {
      domains = domainsData.domains;
    } else if (domainsData.data) {
      domains = domainsData.data;
    }

    log(`found ${domains.length} domains`);

    // 5. 对每个域名调 renew API
    let renewed = 0;
    for (const dom of domains) {
      const domainName = dom.domain || dom.name || dom.domain_name || '?';
      const expiry = dom.expiry || dom.expires || dom.expire || dom.expiry_date || '?';
      log(`  ${domainName} | expires: ${expiry}`);

      try {
        const result = await page.evaluate(async (domain) => {
          // 获取 csrf token
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
        }, domainName);

        if (result.status === 200) {
          log(`  ✅ ${domainName} renewed`);
          renewed++;
        } else {
          log(`  ⚠️ ${domainName} renew: HTTP ${result.status} ${result.body.slice(0, 100)}`);
        }
      } catch (e) {
        log(`  ❌ ${domainName} error: ${e.message}`);
      }
      await page.waitForTimeout(2000);
    }

    log(`🎉 Renewed ${renewed}/${domains.length} domains`);
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
