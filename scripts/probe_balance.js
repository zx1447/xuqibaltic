// 探测站点到底哪个接口/页面元素能给出真实总积分
const { chromium } = require('playwright');
const BASE = 'https://my.siam-node.cloud';
const CLIENT_ID = '1415389053955739753';
const REDIRECT_URI = `${BASE}/DISCORDOAUTH2/process-oauth.php`;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0';
const CANDIDATES = [
  '/api/checkin.php',
  '/api/checkin.php?action=status',
  '/api/checkin.php?action=info',
  '/api/user.php',
  '/api/user.php?action=info',
  '/api/profile.php',
  '/api/me.php',
  '/api/balance.php',
  '/api/account.php',
  '/api/wallet.php',
  '/api/dashboard.php',
  '/api/points.php',
  '/api/credits.php',
];

(async () => {
  const fs = require('fs');
  let cookies = [];
  try { cookies = JSON.parse(fs.readFileSync('siam_state.json', 'utf8')).cookies || []; } catch {}
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
    proxy: { server: 'socks5://127.0.0.1:40000' },
  });
  const ctx = await browser.newContext({ userAgent: UA, locale: 'en-US' });
  if (cookies.length) { await ctx.addCookies(cookies); console.log('用 state 里的', cookies.length, '个 cookie'); }
  const page = await ctx.newPage();
  try {
    const useSession = process.env.USE_SESSION === '1';
    if (useSession) throw Object.assign(new Error('skip oauth'), { skip: true });
    const r = await ctx.request.post(
      `https://discord.com/api/v9/oauth2/authorize?client_id=${CLIENT_ID}&response_type=code&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&scope=identify+email&prompt=none`,
      { headers: { Authorization: process.env.SIAM_DISCORD_TOKEN, 'Content-Type': 'application/json' },
        data: { authorize: true, integration_type: 0 }, failOnStatusCode: false });
    const j = JSON.parse(await r.text());
    const code = (j.location || '').match(/code=([^&]+)/);
    if (!code) { console.log('OAuth fail', JSON.stringify(j).slice(0, 200)); process.exit(1); }
    await page.goto(j.location, { waitUntil: 'domcontentloaded', timeout: 60000 });
    // 等 OAuth 回调自己跳转到站点首页
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(1500);
      if (!page.url().includes('process-oauth')) break;
    }
    await page.waitForTimeout(2000);
    console.log('LOGIN URL:', page.url());

    // 1) 逐个试候选接口
    for (const path of CANDIDATES) {
      const res = await page.evaluate(async (u) => {
        try {
          const r = await fetch(u, { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
          const t = await r.text();
          return { s: r.status, ct: (r.headers.get('content-type') || '').split(';')[0], b: t.slice(0, 400) };
        } catch (e) { return { err: e.message }; }
      }, path);
      console.log(`API ${path} -> ${res.s} ${res.ct || res.err} :: ${(res.b || '').replace(/\s+/g, ' ').slice(0, 220)}`);
    }

    // 2) 页面上所有含 ฿ 的文本
    for (const p of ['/', '/?p=topup', '/?p=dashboard', '/?p=earn']) {
      await page.goto(BASE + p, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForTimeout(2500);
      const found = await page.evaluate(() => {
        const out = [];
        document.querySelectorAll('body *').forEach(el => {
          if (el.children.length === 0) {
            const t = (el.textContent || '').trim();
            if (t.includes('฿') && t.length < 80) {
              out.push(`${el.tagName.toLowerCase()}#${el.id || '-'}.${(el.className || '').toString().split(' ')[0] || '-'} = ${t}`);
            }
          }
        });
        const ids = ['balance', 'credit', 'coins', 'points'];
        const hits = [];
        ids.forEach(id => {
          const el = document.getElementById(id);
          if (el) hits.push(`#${id} = ${el.textContent.trim().slice(0, 60)}`);
        });
        return { out: out.slice(0, 25), hits };
      });
      console.log(`--- PAGE ${p} ฿ 文本 ---`);
      found.out.forEach(x => console.log('   ' + x));
      found.hits.forEach(x => console.log('   ID ' + x));
    }
    if (process.env.USE_SESSION === '1') {
      await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForTimeout(3000);
      console.log('SESSION URL:', page.url());
    }
  } catch (e) {
    if (!e.skip) console.log('ERR', e.message);
  } finally {
    await browser.close();
  }
})();
