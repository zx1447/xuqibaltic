// DigitalPlat Domain auto-renew via GitHub OAuth + Playwright
// 策略: GitHub OAuth 登录 dash -> 列出域名 -> 对每个域名调 renew API
// v2: 支持设备验证码(轮询 dp_code.txt via GitHub API) + 会话缓存(dp-session.json via actions/cache)
const { chromium } = require('playwright');
const fs = require('fs');

const GH_USER = process.env.DP_GH_USER || 'zx1447';
const GH_PASS = process.env.DP_GH_PASS || '';
const OAUTH_CLIENT_ID = 'Ov23liMuiWEyVv3b9R6u';
const BASE = 'https://dash.domain.digitalplat.org';
const REPO = 'zx1447/xuqibaltic';
const BRANCH = 'domain';
const GH_TOKEN = process.env.GITHUB_TOKEN || '';
const CODE_POLL_MAX_MIN = 28;
const SESSION_FILE = 'dp-session.json';

function log(msg) { console.log(`[domain] ${msg}`); }

async function readCodeFile() {
  if (!GH_TOKEN) return '';
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/contents/dp_code.txt?ref=${BRANCH}`, {
      headers: {
        'Authorization': `Bearer ${GH_TOKEN}`,
        'Accept': 'application/vnd.github.raw',
        'User-Agent': 'domain-renew-bot',
      },
    });
    if (!r.ok) return '';
    const t = await r.text();
    const m = t.match(/\b(\d{6})\b/);
    return m ? m[1] : '';
  } catch (e) { return ''; }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
  });

  // 恢复上次缓存的 GitHub 登录态
  if (fs.existsSync(SESSION_FILE)) {
    try {
      const st = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
      if (st.cookies && st.cookies.length) {
        await context.addCookies(st.cookies);
        log(`restored ${st.cookies.length} cookies from cached session`);
      }
    } catch (e) { log('session restore failed: ' + e.message); }
  }

  const page = await context.newPage();

  try {
    log('=== DigitalPlat Domain renew start ===');

    // 1. 先访问 dash (触发 CF challenge)
    log('visiting dash (CF challenge)...');
    await page.goto(`${BASE}/auth/login`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(5000);

    let url = page.url();
    const onDash = () => page.url().includes('digitalplat.org') && !page.url().includes('login');

    // 2. 走 GitHub OAuth
    log('starting GitHub OAuth...');
    const oauthUrl = `https://github.com/login/oauth/authorize?client_id=${OAUTH_CLIENT_ID}&scope=user:email`;
    await page.goto(oauthUrl, { waitUntil: 'networkidle', timeout: 45000 }).catch(() => {});

    // 如果需要 GitHub 登录
    url = page.url();
    if (url.includes('github.com/login') && !url.includes('oauth/authorize')) {
      log('filling GitHub credentials...');
      await page.waitForSelector('input[name="login"]', { timeout: 15000 });
      await page.fill('input[name="login"]', GH_USER);
      await page.fill('input[name="password"]', GH_PASS);
      await page.click('input[type="submit"], button[type="submit"]');
      await page.waitForTimeout(6000);
    }

    // 设备验证: /sessions/verified-device — 轮询 dp_code.txt 等验证码
    for (let i = 0; i < CODE_POLL_MAX_MIN * 6; i++) {
      url = page.url();
      if (!url.includes('/sessions/verified-device')) break;
      if (i === 0) log('⏳ GitHub device verification required, waiting for code in dp_code.txt ...');
      const code = await readCodeFile();
      if (code) {
        log(`got verification code from dp_code.txt: ${code}, filling...`);
        const sels = ['input[name="otp_code"]', 'input[name="code"]', 'input[autocomplete="one-time-code"]', 'form input[type="text"]', 'form input[inputmode="numeric"]'];
        let filled = false;
        for (const s of sels) {
          const el = page.locator(s).first();
          if (await el.isVisible({ timeout: 1000 }).catch(() => false)) {
            await el.fill(code);
            filled = true;
            break;
          }
        }
        if (!filled) { log('❌ no code input found on verified-device page'); break; }
        await page.keyboard.press('Enter');
        await page.click('button[type="submit"], input[type="submit"]').catch(() => {});
        await page.waitForTimeout(6000);
        continue;
      }
      await page.waitForTimeout(10000);
    }

    // 如果有 Authorize 按钮
    url = page.url();
    if (url.includes('github.com/login/oauth/authorize') || url.includes('github.com/apps/')) {
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
    if (onDash()) {
      log('✅ logged in to dash');
      // 缓存会话供下次使用
      try {
        fs.writeFileSync(SESSION_FILE, JSON.stringify(await context.storageState()));
        log('session saved to ' + SESSION_FILE);
      } catch (e) {}
    } else if (url.includes('Performing security verification')) {
      log('❌ CF still blocking');
      process.exit(1);
    } else if (url.includes('github.com')) {
      log('❌ still stuck on github.com, login incomplete');
      process.exit(1);
    } else {
      log('⚠️ login status unclear, trying to continue...');
    }

    // 3. 访问 domains 页面
    log('visiting domains page...');
    await page.goto(`${BASE}/domains`, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);
    log(`domains page URL: ${page.url()}`);

    // 4. 用 API 列出所有域名
    log('fetching domains via API...');
    const domainsData = await page.evaluate(async () => {
      const r = await fetch('/_panel_api/api/domains', {
        headers: { 'Accept': 'application/json' },
      });
      const txt = await r.text();
      try { return JSON.parse(txt); } catch (e) { return { error: 'HTTP ' + r.status + ' non-JSON: ' + txt.slice(0, 120) }; }
    }).catch(e => ({ error: e.message }));

    if (domainsData.error) {
      log(`❌ API error: ${domainsData.error}`);
      process.exit(1);
    }

    // 解析域名列表
    let domains = [];
    if (Array.isArray(domainsData)) domains = domainsData;
    else if (domainsData.domains) domains = domainsData.domains;
    else if (domainsData.data) domains = domainsData.data;

    log(`found ${domains.length} domains`);

    // 5. 对每个域名调 renew API
    let renewed = 0;
    for (const dom of domains) {
      const domainName = dom.domain || dom.name || dom.domain_name || '?';
      const expiry = dom.expiry || dom.expires || dom.expire || dom.expiry_date || '?';
      log(`  ${domainName} | expires: ${expiry}`);

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
