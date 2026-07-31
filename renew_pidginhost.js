// PidginHost server auto-renew via GitHub OAuth + Playwright
const { chromium } = require('playwright');
const fs = require('fs');

const GH_USER = process.env.PIDGINHOST_GH_USER || 'zx1447';
const GH_PASS = process.env.PIDGINHOST_GH_PASS || '';
const SERVER_ID = process.env.PIDGINHOST_SERVER_ID || '3920';
const BASE = 'https://www.pidginhost.ro';

if (!GH_PASS) {
  console.error('[pidgin] ERROR: missing PIDGINHOST_GH_PASS');
  process.exit(1);
}

function log(msg) { console.log(`[pidgin] ${msg}`); }

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });
  const page = await context.newPage();

  try {
    log('=== PidginHost auto-renew start ===');

    // 1. 访问 pidginhost 登录页
    log('opening login page...');
    await page.goto(`${BASE}/panel/account/login`, { waitUntil: 'networkidle', timeout: 30000 });

    // 2. 点 GitHub 登录
    log('clicking GitHub login...');
    const githubBtn = page.locator('a:has-text("Github")').first();
    await githubBtn.click();
    await page.waitForTimeout(3000);

    // 3. GitHub 登录
    const url = page.url();
    log('current URL after GitHub click: ' + url);
    if (url.includes('github.com/login')) {
      log('filling GitHub credentials...');
      // 等待登录表单加载
      await page.waitForSelector('input[name="login"]', { timeout: 10000 });
      await page.fill('input[name="login"]', GH_USER);
      await page.fill('input[name="password"]', GH_PASS);
      // 点 Sign in 按钮 (不是 Continue with Google)
      await page.click('input[type="submit"], button[type="submit"]');
      await page.waitForTimeout(5000);
    } else if (url.includes('github.com/sessions/verified-device')) {
      log('❌ GitHub device verification required');
      process.exit(1);
    }

    // 4. 如果有 device verification, 报错
    const finalUrl = page.url();
    if (finalUrl.includes('github.com/sessions/verified-device')) {
      log('❌ GitHub device verification required - cannot proceed');
      process.exit(1);
    }

    // 5. 检查是否登录成功 (跳回 pidginhost)
    if (finalUrl.includes('pidginhost.ro')) {
      log(`✅ logged in, URL: ${finalUrl}`);
    } else {
      log(`❌ login failed, URL: ${finalUrl}`);
      process.exit(1);
    }

    // 6. 访问 server 页面
    log(`visiting server ${SERVER_ID}...`);
    await page.goto(`${BASE}/panel/cloud/servers/${SERVER_ID}/`, { waitUntil: 'networkidle', timeout: 30000 });

    // 7. 点 Extend 30 days
    log('clicking Extend 30 days...');
    const extendBtn = page.locator('button:has-text("Extend 30 days")').first();
    if (!(await extendBtn.isVisible({ timeout: 5000 }).catch(() => false))) {
      log('❌ Extend button not found');
      process.exit(1);
    }
    await extendBtn.click();
    await page.waitForTimeout(3000);

    // 8. 检查结果
    const bodyText = await page.evaluate(() => document.body.innerText);
    if (bodyText.toLowerCase().includes('extended for 30 days')) {
      log('✅ server extended for 30 days!');
      process.exit(0);
    } else if (bodyText.toLowerCase().includes('expires in 30 days')) {
      log('✅ server expires in 30 days (extend success)');
      process.exit(0);
    } else {
      log(`result unclear, body snippet: ${bodyText.slice(0, 300)}`);
      process.exit(0);
    }
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
