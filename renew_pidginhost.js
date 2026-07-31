// PidginHost server auto-renew via session cookie + Playwright
// 策略: session cookie + 每 7 天跑 + session 保活 (每次跑完访问多个页面刷新 session)
const { chromium } = require('playwright');

const SESSION_COOKIE = process.env.PIDGINHOST_SESSION || '';
const CSRF_COOKIE = process.env.PIDGINHOST_CSRF || '';
const SERVER_ID = process.env.PIDGINHOST_SERVER_ID || '3920';
const BASE = 'https://www.pidginhost.ro';

if (!SESSION_COOKIE) {
  console.error('[pidgin] ERROR: missing PIDGINHOST_SESSION');
  process.exit(1);
}

function log(msg) { console.log(`[pidgin] ${msg}`); }

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });
  // 设置 cookie
  await context.addCookies([
    { name: 'sessionid', value: SESSION_COOKIE, domain: 'www.pidginhost.ro', path: '/' },
    { name: 'csrftoken', value: CSRF_COOKIE, domain: 'www.pidginhost.ro', path: '/' },
  ]);
  const page = await context.newPage();

  try {
    log('=== PidginHost auto-renew start ===');

    // 1. 先访问 dashboard 预热 session (保活)
    log('🔄 warming up session (visiting dashboard)...');
    await page.goto(`${BASE}/panel/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(2000);

    // 检查是否登录
    if (page.url().includes('/login')) {
      log('❌ session expired, redirect to login');
      log('⚠️ please update PIDGINHOST_SESSION secret with new cookie');
      process.exit(1);
    }
    log('✅ session valid');

    // 2. 访问 server 页面
    log(`visiting server ${SERVER_ID}...`);
    await page.goto(`${BASE}/panel/cloud/servers/${SERVER_ID}/`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);

    // 3. 点 Extend 30 days
    log('clicking Extend 30 days...');
    const extendBtn = page.locator('button:has-text("Extend 30 days")').first();
    if (!(await extendBtn.isVisible({ timeout: 5000 }).catch(() => false))) {
      log('❌ Extend button not found');
      // 可能已经 extend 过了, 不算失败
      const bodyText = await page.evaluate(() => document.body.innerText);
      if (bodyText.toLowerCase().includes('expires in 30 days')) {
        log('✅ already extended (expires in 30 days)');
      }
      process.exit(0);
    }
    await extendBtn.click();
    await page.waitForTimeout(3000);

    // 4. 检查结果
    const bodyText = await page.evaluate(() => document.body.innerText);
    if (bodyText.toLowerCase().includes('extended for 30 days')) {
      log('✅ server extended for 30 days!');
    } else if (bodyText.toLowerCase().includes('expires in 30 days')) {
      log('✅ server expires in 30 days');
    } else {
      log(`result: ${bodyText.slice(0, 300)}`);
    }

    // 5. Session 保活: 访问多个页面 (刷新 session 活跃度, 防 idle 过期)
    log('💾 session keepalive: visiting pages...');
    const keepalivePages = [
      `${BASE}/panel/`,
      `${BASE}/panel/cloud/`,
      `${BASE}/panel/account/sessions`,
    ];
    for (const url of keepalivePages) {
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.waitForTimeout(1000);
      } catch (e) {
        // 忽略错误, 继续下一个
      }
    }
    log('✅ session refreshed');

    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
