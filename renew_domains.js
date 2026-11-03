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
    // 等待回调处理完毕 (dash 回调 URL 含 iss=...github.com 的查询参数, 不能用 includes 判断)
    await page.waitForURL(u => {
      const s = u.toString();
      return s.includes('dash.domain.digitalplat.org') && !s.includes('/auth/login') && !s.includes('/auth/kyc/');
    }, { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);

    url = page.url();
    log(`after OAuth, URL: ${url}`);
    // 只检查 HOST 是 github.com (不是查询参数里出现 github.com)
    if (url.startsWith('https://github.com')) {
      // 还在 authorize 页? 试点 Authorize
      if (url.includes('github.com/login/oauth/authorize') || url.includes('github.com/apps/')) {
        log('authorizing DigitalPlat...');
        const authBtn = page.locator('button:has-text("Authorize")').first();
        if (await authBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
          await authBtn.click();
          await page.waitForTimeout(10000);
        }
      }
      url = page.url();
      log(`after authorize attempt, URL: ${url}`);
    }

    // 只要 URL 含 dash.domain.digitalplat.org 且不在 login 页 = 成功
    if (url.includes('dash.domain.digitalplat.org') && !url.includes('/auth/login')) {
      log('✅ logged in to dash');
      // 缓存会话供下次使用
      try {
        fs.writeFileSync(SESSION_FILE, JSON.stringify(await context.storageState()));
        log('session saved to ' + SESSION_FILE);
      } catch (e) {}
    } else if (url.includes('Performing security verification')) {
      log('❌ CF still blocking');
      process.exit(1);
    } else if (url.startsWith('https://github.com')) {
      log('❌ still on github.com host, login incomplete: ' + url);
      process.exit(1);
    } else {
      log('⚠️ login status unclear, trying to continue...');
    }

    // 3. 访问 domains 页面
    log('visiting domains page...');
    await page.goto(`${BASE}/domains`, { waitUntil: 'networkidle', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(5000);
    log(`domains page URL: ${page.url()}`);

    // 4. 从页面抓取域名信息 + 查找 Renew 按钮
    const pageInfo = await page.evaluate(() => {
      const bodyText = document.body ? document.body.innerText : '';
      // 找所有可交互元素 (button/a/[wire:click]) 含 renew/续期 文字
      const els = [...document.querySelectorAll('button, a, [wire\\:click], input[type="submit"], [role="button"]')];
      const renewEls = els.map((el, i) => ({
        idx: i,
        tag: el.tagName,
        text: (el.textContent || '').trim().slice(0, 60),
        href: el.getAttribute('href') || '',
        wireClick: el.getAttribute('wire:click') || '',
        classes: el.className || '',
      })).filter(x => {
        const t = (x.text + ' ' + x.href + ' ' + x.wireClick + ' ' + x.classes).toLowerCase();
        return t.includes('renew') || t.includes('续期') || t.includes('extend');
      });
      // 提取页面中所有看起来像域名的字符串
      const domainMatches = bodyText.match(/[a-z0-9][-a-z0-9]*\.(dpdns\.org|us\.kg|is-a\.dev|github\.dev)/gi) || [];
      return {
        title: document.title,
        bodyTextHead: bodyText.slice(0, 4000),
        domains: [...new Set(domainMatches)],
        renewEls: renewEls,
        totalEls: els.length,
      };
    }).catch(e => ({ error: e.message }));

    if (pageInfo.error) {
      log(`❌ page scrape error: ${pageInfo.error}`);
      process.exit(1);
    }

    log(`PAGE TITLE: ${pageInfo.title}`);
    log(`Found ${pageInfo.domains.length} domain(s) in page text: ${pageInfo.domains.join(', ')}`);
    log(`Found ${pageInfo.renewEls.length} renew-related element(s):`);
    for (const el of pageInfo.renewEls) {
      log(`  [${el.idx}] <${el.tag}> text="${el.text}" href="${el.href}" wire:click="${el.wireClick}" class="${el.classes.slice(0,60)}"`);
    }
    // dump body text for debugging (first 2000 chars)
    log('BODY TEXT (first 2000): ' + pageInfo.bodyTextHead.slice(0, 2000));

    // 5. 逐个点击 Renew 按钮
    let renewed = 0;
    if (pageInfo.renewEls.length > 0) {
      for (const el of pageInfo.renewEls) {
        log(`clicking renew element [${el.idx}] "${el.text}"...`);
        try {
          const selector = `[wire\\:click="${el.wireClick}"]`;
          let locator;
          if (el.wireClick) {
            locator = page.locator(`[wire\\:click="${el.wireClick}"]`).first();
          } else {
            // 用文本定位
            locator = page.locator(`${el.tag}:has-text("${el.text}")`).first();
          }
          if (await locator.isVisible({ timeout: 5000 }).catch(() => false)) {
            await locator.click({ timeout: 10000 });
            await page.waitForTimeout(3000);
            // 处理可能的确认对话框 (Filament modal)
            const confirmBtn = page.locator('button:has-text("Confirm"), button:has-text("Renew"), button:has-text("OK"), button:has-text("Yes")').first();
            if (await confirmBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
              log('  confirmation dialog detected, clicking confirm...');
              await confirmBtn.click();
              await page.waitForTimeout(5000);
            }
            log(`  ✅ clicked renew [${el.idx}] "${el.text}"`);
            renewed++;
          } else {
            log(`  ⚠️ element [${el.idx}] not visible, trying JS click`);
            await page.evaluate((idx) => {
              const els = document.querySelectorAll('button, a, [wire\\:click], input[type="submit"], [role="button"]');
              if (els[idx]) els[idx].click();
            }, el.idx);
            await page.waitForTimeout(3000);
            log(`  ✅ JS-clicked renew [${el.idx}]`);
            renewed++;
          }
        } catch (e) {
          log(`  ❌ renew [${el.idx}] error: ${e.message}`);
        }
        await page.waitForTimeout(2000);
      }
    } else {
      // 备用: 尝试原 API 路径 (万一 CF 这次没挡)
      log('no renew buttons found, trying API fallback...');
      for (const dom of pageInfo.domains) {
        log(`  trying API renew for ${dom}...`);
        try {
          const result = await page.evaluate(async (domain) => {
            const csrf = document.cookie.match(/panel_csrf_token=([^;]+)/)?.[1] || '';
            const r = await fetch(`/_panel_api/api/domains/${domain}/renew`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'x-csrf-token': csrf },
              body: JSON.stringify({}),
            });
            return { status: r.status, body: await r.text() };
          }, dom);
          if (result.status === 200) { log(`  ✅ ${dom} renewed`); renewed++; }
          else { log(`  ⚠️ ${dom} renew: HTTP ${result.status} ${result.body.slice(0, 100)}`); }
        } catch (e) { log(`  ❌ ${dom} error: ${e.message}`); }
        await page.waitForTimeout(2000);
      }
    }

    const totalCount = pageInfo.renewEls.length || pageInfo.domains.length;
    log(`🎉 Renewed ${renewed}/${totalCount} domains`);
    process.exit(0);
  } catch (e) {
    log(`❌ error: ${e.message}`);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
